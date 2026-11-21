"""
skyctl.agent.chat
=================
Tool-using chat backends for the task agent (the ChatProvider port).

Two protocol implementations cover every cloud:

    AnthropicChat      Anthropic Messages API. Clients:
                         anthropic:<model>     api.anthropic.com   ANTHROPIC_API_KEY
                         bedrock:<model-id>    AWS Bedrock         AWS_REGION + task role
                         vertex:<model>        Google Vertex AI    CLOUD_ML_REGION, GOOGLE_CLOUD_PROJECT
                         foundry:<deployment>  Microsoft Foundry   FOUNDRY_BASE_URL, FOUNDRY_API_KEY
    OpenAIChat         OpenAI chat-completions + function calling:
                         openai:, openrouter:, ollama:, compat:   (as in skyctl.llm)
                         azure-openai:<deployment>   AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY

Messages are kept in the provider-neutral shape from skyctl.ports and
converted at the edge, so the runner never sees vendor formats.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Callable, Optional

from ..llm import LLMError, LLMRefusal, make_anthropic_client, openai_compat_settings
from ..ports import ChatTurn, Message, ToolCall

_STOP = {
    "end_turn": "end",
    "stop_sequence": "end",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
    "pause_turn": "pause",
    "stop": "end",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


# ----------------------------------------------------------------------
# Anthropic Messages API (direct, Bedrock, Vertex, Foundry)
# ----------------------------------------------------------------------

class AnthropicChat:
    tool_format = "anthropic"

    def __init__(self, model: str, client=None, effort: Optional[str] = None, max_tokens: int = 4096):
        try:
            import anthropic
        except ImportError as e:
            raise LLMError("The Anthropic SDK is not installed. Run: pip install 'skyctl[agents]'") from e
        self._anthropic = anthropic
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self._client = client or anthropic.Anthropic()

    @staticmethod
    def _to_api(messages: list[Message]) -> list[dict]:
        out = []
        for m in messages:
            if m.role == "user":
                out.append({"role": "user", "content": m.text})
            elif m.role == "assistant":
                if m.raw is not None:
                    out.append({"role": "assistant", "content": m.raw})
                else:
                    blocks: list[dict] = [{"type": "text", "text": m.text}] if m.text else []
                    blocks += [
                        {"type": "tool_use", "id": c.id, "name": c.name, "input": c.args}
                        for c in m.tool_calls
                    ]
                    out.append({"role": "assistant", "content": blocks})
            elif m.role == "tool":
                out.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": r.call_id,
                            "content": r.content,
                            "is_error": r.is_error,
                        }
                        for r in m.tool_results
                    ],
                })
        return out

    def chat(self, system: str, messages: list[Message], tools: list[dict]) -> ChatTurn:
        anthropic = self._anthropic
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": self._to_api(messages),
        }
        if tools:
            kwargs["tools"] = tools
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        try:
            response = self._client.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            raise LLMError("The model endpoint rejected the credentials.") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"Unknown model '{self.model}' at this endpoint.") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Model rate limit reached; try again shortly.") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Model API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Could not reach the model endpoint.") from e

        text = "".join(b.text for b in response.content if b.type == "text").strip()
        calls = [
            ToolCall(id=b.id, name=b.name, args=dict(b.input or {}))
            for b in response.content
            if b.type == "tool_use"
        ]
        stop = _STOP.get(response.stop_reason or "", response.stop_reason or "end")
        if stop == "refusal":
            raise LLMRefusal(f"{self.model} declined this request.")
        usage = {}
        if getattr(response, "usage", None) is not None:
            usage = {
                "input_tokens": getattr(response.usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(response.usage, "output_tokens", 0) or 0,
            }
        # Keep the native blocks so the next turn echoes them back exactly.
        raw = [b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else b for b in response.content]
        return ChatTurn(text=text, tool_calls=calls, stop_reason=stop, usage=usage, raw=raw)


# ----------------------------------------------------------------------
# OpenAI-compatible chat completions (OpenAI, Azure OpenAI, OpenRouter, Ollama, vLLM…)
# ----------------------------------------------------------------------

class OpenAIChat:
    tool_format = "openai"

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        timeout: float = 300.0,
        max_tokens: Optional[int] = None,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        if api_key and "api-key" not in self.headers:
            self.headers["Authorization"] = f"Bearer {api_key}"
        self.timeout = timeout
        self.max_tokens = max_tokens

    @staticmethod
    def _to_api(system: str, messages: list[Message]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "user":
                out.append({"role": "user", "content": m.text})
            elif m.role == "assistant":
                item: dict[str, Any] = {"role": "assistant", "content": m.text or None}
                if m.tool_calls:
                    item["tool_calls"] = [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.args)},
                        }
                        for c in m.tool_calls
                    ]
                out.append(item)
            elif m.role == "tool":
                for r in m.tool_results:
                    out.append({"role": "tool", "tool_call_id": r.call_id, "content": r.content})
        return out

    def chat(self, system: str, messages: list[Message], tools: list[dict]) -> ChatTurn:
        import httpx

        body: dict[str, Any] = {"model": self.model, "messages": self._to_api(system, messages)}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions", json=body, headers=self.headers, timeout=self.timeout
            )
        except httpx.HTTPError as e:
            raise LLMError(f"Could not reach {self.base_url}: {e}") from e
        if resp.status_code >= 400:
            raise LLMError(f"{self.base_url} returned HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
            choice = data["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LLMError(f"Unexpected response from {self.base_url}.") from e

        calls = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {"_raw": fn.get("arguments")}
            calls.append(ToolCall(id=tc.get("id") or uuid.uuid4().hex, name=fn.get("name", ""), args=args))
        finish = choice.get("finish_reason") or "stop"
        stop = _STOP.get(finish, finish)
        if stop == "refusal":
            raise LLMRefusal(f"{self.model} declined this request (content filter).")
        if calls and stop == "end":
            stop = "tool_use"
        usage = {}
        if isinstance(data.get("usage"), dict):
            usage = {
                "input_tokens": data["usage"].get("prompt_tokens", 0) or 0,
                "output_tokens": data["usage"].get("completion_tokens", 0) or 0,
            }
        return ChatTurn(text=(message.get("content") or "").strip(), tool_calls=calls, stop_reason=stop, usage=usage)


# ----------------------------------------------------------------------
# Factory
# ----------------------------------------------------------------------

def _anthropic_family(provider: str):
    def build(model: str, **options) -> AnthropicChat:
        return AnthropicChat(model, client=make_anthropic_client(provider), effort=options.get("effort"))

    return build


def _openai_family(provider: str):
    def build(model: str, **options) -> OpenAIChat:
        base_url, api_key, headers = openai_compat_settings(provider)
        return OpenAIChat(model, base_url, api_key, headers=headers)

    return build


_CHAT_PROVIDERS: dict[str, Callable[..., Any]] = {
    "anthropic": _anthropic_family("anthropic"),
    "bedrock": _anthropic_family("bedrock"),
    "vertex": _anthropic_family("vertex"),
    "foundry": _anthropic_family("foundry"),
    "openai": _openai_family("openai"),
    "openrouter": _openai_family("openrouter"),
    "ollama": _openai_family("ollama"),
    "compat": _openai_family("compat"),
    "azure-openai": _openai_family("azure-openai"),
}


def register_chat_provider(name: str, factory: Callable[..., Any]) -> None:
    """Add a backend: factory(model, **options) -> object with chat(system, messages, tools)."""
    _CHAT_PROVIDERS[name.lower()] = factory


def get_chat_provider(spec: Optional[str] = None, **options):
    from ..llm import default_model

    spec = (spec or "").strip() or default_model()
    provider, sep, model = spec.partition(":")
    if sep and provider.lower() in _CHAT_PROVIDERS:
        if not model:
            raise LLMError(f"Model spec '{spec}' names a provider but no model.")
        return _CHAT_PROVIDERS[provider.lower()](model, **options)
    return _CHAT_PROVIDERS["anthropic"](spec, **options)
