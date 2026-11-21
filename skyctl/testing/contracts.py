# Copyright 2026 The skyctl Authors
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Executable port contracts. Each check_* function raises AssertionError with
a message naming the violated clause. Use them from pytest:

    from skyctl.testing.contracts import check_object_store
    def test_my_store():
        check_object_store(MyStore(...))
"""

from __future__ import annotations

import threading

from ..ports import PreconditionFailed, cas_update


def check_object_store(store, *, atomic: bool = True) -> None:
    """Basic operations, versioning and conditional writes."""
    assert store.get("c/missing") is None, "get() of a missing key must return None"
    assert not store.exists("c/a.txt"), "fresh store must not contain c/a.txt"

    v1 = store.put("c/a.txt", b"one", if_absent=True)
    assert isinstance(v1, str) and v1, "put() must return a non-empty version token"
    got = store.get("c/a.txt")
    assert got is not None and got.data == b"one" and got.version == v1, (
        "get() must return data and the version put() returned"
    )

    try:
        store.put("c/a.txt", b"two", if_absent=True)
        raise AssertionError("put(if_absent=True) must fail when the key exists")
    except PreconditionFailed:
        pass
    assert store.read("c/a.txt") == b"one", "a failed conditional put must not change the object"

    v2 = store.put("c/a.txt", b"two", if_version=v1)
    assert v2 != v1, "a successful write must change the version"
    try:
        store.put("c/a.txt", b"three", if_version=v1)
        raise AssertionError("put(if_version=stale) must fail")
    except PreconditionFailed:
        pass
    assert store.read("c/a.txt") == b"two"

    store.write("c/b.txt", b"b")
    store.write("other.txt", b"o")
    assert store.list("c") == ["c/a.txt", "c/b.txt"], f"list(prefix) wrong: {store.list('c')}"
    assert set(store.list()) >= {"c/a.txt", "c/b.txt", "other.txt"}
    store.append_line("c/log.jsonl", '{"n": 1}')
    store.append_line("c/log.jsonl", '{"n": 2}\n')
    assert store.read("c/log.jsonl") == b'{"n": 1}\n{"n": 2}\n', (
        "append_line must add exactly one newline-terminated line"
    )

    store.delete("c/a.txt")
    store.delete("c/a.txt")
    assert not store.exists("c/a.txt") and store.get("c/a.txt") is None, "delete must be idempotent"
    assert store.list("nope/") == []
    try:
        store.read("../escape")
        raise AssertionError("keys containing '..' must be rejected")
    except ValueError:
        pass
    assert getattr(store, "atomic_cas", None) is atomic, f"atomic_cas must be {atomic}"


def check_cas_under_contention(store, threads: int = 8, increments: int = 10) -> None:
    """N writers increment one counter with cas_update; no increment may be lost."""
    key = "contention/counter"
    errors: list[BaseException] = []

    def worker():
        try:
            for _ in range(increments):
                cas_update(store, key, lambda cur: str(int(cur or b"0") + 1).encode(), retries=200)
        except BaseException as e:  # pragma: no cover - surfaced below
            errors.append(e)

    pool = [threading.Thread(target=worker) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    assert not errors, f"cas_update raised: {errors[0]!r}"
    total = int(store.read(key))
    assert total == threads * increments, f"lost updates: {total} != {threads * increments}"


def check_job_store_lease(job_store) -> None:
    """Leases are exclusive, re-entrant for the holder, and released only by the holder."""
    assert job_store.lease("a", 60), "first lease must succeed"
    token_a = job_store.fencing_token
    assert not job_store.lease("b", 60), "a held lease must exclude others"
    assert job_store.lease("a", 60), "the holder may renew"
    assert job_store.fencing_token != token_a, "renewal must advance the fencing token"
    job_store.release("b")
    assert not job_store.lease("b", 60), "release by a non-holder must have no effect"
    job_store.release("a")
    assert job_store.lease("b", 60), "after release another owner may take the lease"
    job_store.release("b")


def check_work_queue(queue, *, supports_delay: bool = True) -> None:
    """At-least-once semantics: receive hides, ack removes, nack redelivers."""
    queue.send({"n": 1})
    [m] = queue.receive(max_messages=1, visibility_seconds=30)
    assert m.body == {"n": 1}
    assert m.attempt >= 1
    assert queue.receive(max_messages=1, visibility_seconds=30) == [], "an in-flight message must be invisible"
    queue.nack(m, delay_seconds=0)
    [again] = queue.receive(max_messages=1, visibility_seconds=30)
    assert again.body == {"n": 1}, "nack(0) must make the message visible again"
    if hasattr(queue, "extend"):
        queue.extend(again, 60)
        assert queue.receive(max_messages=1, visibility_seconds=30) == [], "extend must keep the message hidden"
    queue.ack(again)
    assert queue.receive(max_messages=1, visibility_seconds=1) == [], "ack must remove the message"
    if supports_delay:
        queue.send({"n": 2}, delay_seconds=60)
        assert queue.receive(max_messages=1, visibility_seconds=1) == [], "delayed messages must not be visible early"
