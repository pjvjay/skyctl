#!/usr/bin/env python3
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
Audit the licences of skyctl's installed dependency closure against the ASF
third-party licence policy (https://www.apache.org/legal/resolved.html):

    Category A  may be included               Apache-2.0, MIT, BSD, ISC, PSF, Unlicense, 0BSD, ...
    Category B  binary inclusion only          MPL-2.0, EPL, CDDL, ... (reported, allowed)
    Category X  must not be depended upon      GPL, LGPL, AGPL, SSPL, BUSL, CC-BY-NC, ...

    python scripts/check_dependency_licenses.py                 # extras: all
    python scripts/check_dependency_licenses.py --extras aws,mcp --json

Exit 1 on any Category X or unknown licence (unknowns need a human decision,
recorded in KNOWN below).
"""

from __future__ import annotations

import json
import re
import sys
from importlib import metadata

from packaging.markers import default_environment
from packaging.requirements import Requirement

CATEGORY_X = re.compile(
    r"\b(A?GPL|LGPL|GNU (Affero |Lesser )?General Public|SSPL|Server Side Public|BUSL|Business Source|"
    r"CC[- ]BY[- ]NC|Commons Clause|JSON License)\b",
    re.I,
)
CATEGORY_B = re.compile(r"\b(MPL|Mozilla Public|EPL|Eclipse Public|CDDL|CPL)\b", re.I)
CATEGORY_A = re.compile(
    r"\b(Apache|MIT|BSD|ISC|PSF|Python Software Foundation|Unlicense|0BSD|Zlib|HPND|"
    r"Public Domain|CC0|UPL|X11|W3C)\b",
    re.I,
)
# Packages whose metadata is ambiguous, reviewed by hand (name → licence).
KNOWN = {
    "typing-extensions": "PSF-2.0",
    "certifi": "MPL-2.0",  # Category B: CA bundle, consumed as a binary dependency
}


def licence_of(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expr = meta.get("License-Expression")
    if expr:
        return expr
    classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        return " / ".join(classifiers)
    text = (meta.get("License") or "").strip()
    return text.splitlines()[0][:80] if text else ""


def category(name: str, licence: str) -> str:
    licence = KNOWN.get(name, licence)
    if CATEGORY_X.search(licence):
        return "X"
    if CATEGORY_A.search(licence):
        return "A"
    if CATEGORY_B.search(licence):
        return "B"
    return "unknown"


def closure(root: str, extras: set[str]) -> dict[str, metadata.Distribution]:
    """Every installed distribution reachable from root[extras], following extras transitively."""
    env = default_environment()
    seen: dict[str, metadata.Distribution] = {}
    visited: set[tuple[str, frozenset[str]]] = set()
    todo: list[tuple[str, frozenset[str]]] = [(root, frozenset(extras))]
    while todo:
        name, want = todo.pop()
        key = name.lower().replace("_", "-")
        if (key, want) in visited:
            continue
        visited.add((key, want))
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue  # an optional dependency that is not installed
        seen[key] = dist
        for raw in dist.requires or []:
            req = Requirement(raw)
            if req.marker is not None and not any(req.marker.evaluate({**env, "extra": e}) for e in (want or {""})):
                continue
            todo.append((req.name, frozenset(req.extras)))
    seen.pop(root.lower(), None)
    return seen


def main(argv: list[str]) -> int:
    extras = {"all"}
    if "--extras" in argv:
        extras = set(argv[argv.index("--extras") + 1].split(","))
    rows = []
    for name, dist in sorted(closure("skyctl", extras).items()):
        licence = licence_of(dist)
        rows.append(
            {
                "package": name,
                "version": dist.version,
                "license": KNOWN.get(name, licence) or "?",
                "category": category(name, licence),
            }
        )
    if "--json" in argv:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"{r['category']:>7}  {r['package']:<34} {r['version']:<12} {r['license']}")
    bad = [r for r in rows if r["category"] in ("X", "unknown")]
    if bad:
        print("\nNot acceptable for an ASF project (Category X) or needs review (unknown):", file=sys.stderr)
        for r in bad:
            print(f"  {r['package']} {r['version']}: {r['license']}", file=sys.stderr)
        return 1
    print(f"\n{len(rows)} dependencies: all Category A or B.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
