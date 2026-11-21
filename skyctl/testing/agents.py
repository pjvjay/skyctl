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
Scripted agents for tests and demos: they read real tool results (from a
World or a real network) and act like a careful model would, without an API.
"""

from __future__ import annotations

import json
import re

from ..ports import ChatTurn, ToolCall


def _results(messages) -> dict:
    return {r.name: json.loads(r.content) for r in messages[-1].tool_results}


class FactCheckerAgent:
    """
    Plan-mode agent for "find a post about X, read the comments, reply to blatant
    misinformation with facts from recent news":

        search_posts → get_post_thread → web_search → fetch_page → plan_check → plan_create

    A comment counts as misinformation when it contains one of `false_markers`
    (a test stand-in for the model's judgement). Replies cite the article URL.
    """

    model, tool_format = "script:fact-checker", "anthropic"

    def __init__(
        self, topic: str, false_markers: tuple[str, ...], account: str = "work", news_query: str | None = None
    ):
        self.topic, self.false_markers, self.account = topic, false_markers, account
        self.news_query = news_query or topic
        self.state: dict = {}

    def chat(self, system, messages, tools):
        n = len(messages)
        if n == 1:
            return ChatTurn(
                f"Searching Bluesky for posts about {self.topic}.",
                [ToolCall("s", "search_posts", {"account": self.account, "query": self.topic})],
                "tool_use",
            )
        if n == 3:
            posts = _results(messages)["search_posts"]["data"]
            if not posts:
                return ChatTurn(f"No posts about {self.topic} found; nothing to do.", [], "end")
            words = set(re.findall(r"\w+", self.topic.lower()))
            best = max(
                posts, key=lambda p: len(words & set(re.findall(r"\w+", p["text"].lower())))
            )  # the post, not a comment
            self.state["post"] = best["uri"]
            return ChatTurn(
                "Reading the comments.",
                [ToolCall("t", "get_post_thread", {"account": self.account, "uri": best["uri"]})],
                "tool_use",
            )
        if n == 5:
            replies = _results(messages)["get_post_thread"]["data"]["replies"]
            self.state["false"] = [r for r in replies if any(m in r["text"].lower() for m in self.false_markers)]
            if not self.state["false"]:
                return ChatTurn("No blatant misinformation in the comments; nothing to correct.", [], "end")
            return ChatTurn(
                "Checking the claim against recent news.",
                [ToolCall("w", "web_search", {"query": self.news_query})],
                "tool_use",
            )
        if n == 7:
            hits = _results(messages)["web_search"]["data"]["results"]
            self.state["source"] = hits[0]["url"]
            return ChatTurn(
                "Reading the most recent article.", [ToolCall("f", "fetch_page", {"url": hits[0]["url"]})], "tool_use"
            )
        if n == 9:
            page = _results(messages)["fetch_page"]["data"]
            fact = re.split(r"(?<=\.)\s", page["text"])[0][:180]
            steps = []
            seen_authors = set()
            for i, c in enumerate(self.state["false"]):
                if c["author"] in seen_authors:
                    continue
                seen_authors.add(c["author"])
                steps.append(
                    {
                        "id": f"r{i + 1}",
                        "tool": "reply",
                        "why": f'@{c["author"]} claimed: "{c["text"][:80]}"; contradicted by {self.state["source"]}',
                        "args": {
                            "account": self.account,
                            "parent_uri": c["uri"],
                            "text": f"That's not accurate: {fact} Source: {self.state['source']}",
                        },
                    }
                )
            self.state["steps"] = len(steps)
            plan = {
                "goal": f"Correct misinformation in the comments under the post about {self.topic}",
                "intent": "engage",
                "assumptions": ["'most recent news' means the last 7 days"],
                "steps": steps,
            }
            return ChatTurn("", [ToolCall("c", "plan_check", {"plan": plan})], "tool_use")
        if n == 11:
            check = _results(messages)["plan_check"]
            if not check["ok"]:
                return ChatTurn(
                    "The plan failed its checks: " + "; ".join(e["message"] for e in check["errors"]), [], "end"
                )
            plan = self._last_plan(messages)
            return ChatTurn("", [ToolCall("p", "plan_create", {"plan": plan})], "tool_use")
        n_false, n_steps = len(self.state.get("false", [])), self.state.get("steps", 0)
        return ChatTurn(
            f"Found {n_false} false comment(s); proposed {n_steps} reply(ies) (one per author), each citing "
            f"{self.state.get('source')}. Awaiting approval.",
            [],
            "end",
        )

    @staticmethod
    def _last_plan(messages) -> dict:
        for m in reversed(messages):
            for c in getattr(m, "tool_calls", []):
                if c.name == "plan_check":
                    return c.args["plan"]
        raise RuntimeError("no plan drafted")
