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
skyctl.llm
==========
Pluggable language-model backends for agent groups.

A model is named by a spec of the form "provider:model":

    anthropic:claude-opus-5-5      Claude API (default; needs ANTHROPIC_API_KEY)
    openai:<model>                 OpenAI (needs OPENAI_API_KEY)
    openrouter:<vendor/model>      OpenRouter (needs OPENROUTER_API_KEY)
    ollama:<model>                 Local Ollama server, no key
    compat:<model>                 Any OpenAI-compatible server
                                   (SKYCTL_LLM_BASE_URL, SKYCTL_LLM_API_KEY)

Cloud-hosted models, using each platform's workload identity:

    bedrock:<model-id>             AWS Bedrock via the Anthropic SDK (AWS_REGION)
    vertex:<model>                 Google Vertex AI via the Anthropic SDK
                                   (CLOUD_ML_REGION, GOOGLE_CLOUD_PROJECT)
    azure-openai:<deployment>      Azure OpenAI (AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY)
    foundry:<deployment>           Claude on Microsoft Foundry (FOUNDRY_BASE_URL, FOUNDRY_API_KEY)

A spec with no provider prefix is treated as an Anthropic model. Add your
own backend with register_provider().
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Protocol

DEFAULT_MODEL = "anthropic:claude-opus-5-5"

# Claude models that take the server-side refusal fallback parameter.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FALLBACK_MODELS = {
    "claude-fable-5-2",
    "claude-fable-5-1",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5-5",
}
_WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}
_MAX_PAUSE_RESUMES = 5


class LLMError(RuntimeError):
    """A model call failed."""


class LLMRefusal(LLMError):
    """The model declined to answer."""


class LLMProvider(Protocol):
    """Anything with this method can drive an agent."""

    # Set supports_web_search = True on a backend that searches the web
    # itself when complete() is called with web_search=True.

    def complete(self, system: str, prompt: str, web_search: bool = False) -> str: ...


def default_model() -> str:
    return os.getenv("SKYCTL_AGENT_MODEL", "").strip() or DEFAULT_MODEL


def parse_model_spec(spec: str | None) -> tuple[str, str]:
    """Split "provider:model" into its parts. A bare model means Anthropic."""
    spec = (spec or "").strip() or default_model()
    provider, sep, model = spec.partition(":")
    if sep and provider.lower() in _PROVIDERS:
        if not model:
            raise LLMError(f"Model spec '{spec}' names a provider but no model.")
        return provider.lower(), model
    return "anthropic", spec


# ----------------------------------------------------------------------
# Anthropic
# ----------------------------------------------------------------------


def make_anthropic_client(provider: str = "anthropic"):
    """
    An Anthropic SDK client for the given hosting provider. Every variant
    speaks the same Messages API, so AnthropicProvider and AnthropicChat
    work unchanged on top of it.
    """
    try:
        import anthropic
    except ImportError as e:
        raise LLMError("The Anthropic SDK is not installed. Run: pip install 'skyctl[agents]'") from e
    if provider == "bedrock":
        # Credentials and region from the AWS default chain (task role etc.).
        return anthropic.AnthropicBedrock(aws_region=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or None)
    if provider == "vertex":
        region = os.getenv("CLOUD_ML_REGION")
        project = os.getenv("ANTHROPIC_VERTEX_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")
        # Unset values are NOT_GIVEN, so the SDK applies its own discovery rather than receiving None.
        return anthropic.AnthropicVertex(
            region=region or anthropic.NOT_GIVEN, project_id=project or anthropic.NOT_GIVEN
        )
    if provider == "foundry":
        base_url = _require_env("FOUNDRY_BASE_URL", "foundry")
        api_key = _require_env("FOUNDRY_API_KEY", "foundry")
        foundry_cls = getattr(anthropic, "AnthropicFoundry", None)
        if foundry_cls is not None:  # newer SDKs ship a dedicated client
            return foundry_cls(base_url=base_url, api_key=api_key)
        # Fallback: the Messages API route with the key in both header styles.
        return anthropic.Anthropic(base_url=base_url, api_key=api_key, default_headers={"api-key": api_key})
    return anthropic.Anthropic()


def _azure_entra_token() -> str | None:
    """An Entra ID token for Azure OpenAI from the workload's managed identity, if available."""
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError:
        return None
    try:
        return DefaultAzureCredential().get_token("https://cognitiveservices.azure.com/.default").token
    except Exception:
        return None


def openai_compat_settings(provider: str) -> tuple[str, str | None, dict[str, str]]:
    """(base_url, api_key, extra headers) for an OpenAI-protocol provider."""
    if provider == "openai":
        base = os.getenv("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1"
        return base, _require_env("OPENAI_API_KEY", "openai"), {}
    if provider == "openrouter":
        return "https://openrouter.ai/api/v1", _require_env("OPENROUTER_API_KEY", "openrouter"), {}
    if provider == "ollama":
        host = os.getenv("OLLAMA_HOST", "").strip() or "http://localhost:11434"
        if not host.startswith("http"):
            host = f"http://{host}"
        return f"{host.rstrip('/')}/v1", None, {}
    if provider == "azure-openai":
        endpoint = _require_env("AZURE_OPENAI_ENDPOINT", "azure-openai").rstrip("/")
        key = os.getenv("AZURE_OPENAI_API_KEY", "").strip()
        token = os.getenv("AZURE_OPENAI_TOKEN", "").strip() or (None if key else _azure_entra_token())
        if not key and not token:
            raise LLMError(
                "No credentials for 'azure-openai:' models: set AZURE_OPENAI_API_KEY, or give the workload a "
                "managed identity with 'Cognitive Services OpenAI User' (needs azure-identity)."
            )
        headers = {"api-key": key} if key else {"Authorization": f"Bearer {token}"}
        return f"{endpoint}/openai/v1", None, headers
    if provider == "compat":
        return (
            _require_env("SKYCTL_LLM_BASE_URL", "compat"),
            os.getenv("SKYCTL_LLM_API_KEY", "").strip() or None,
            {},
        )
    raise LLMError(f"Unknown OpenAI-compatible provider '{provider}'.")


class AnthropicProvider:
    """Claude through the official Anthropic SDK."""

    supports_web_search = True

    def __init__(
        self,
        model: str,
        effort: str | None = None,
        max_tokens: int = 16000,
        client=None,
    ):
        try:
            import anthropic
        except ImportError as e:
            raise LLMError("The Anthropic SDK is not installed. Run: pip install 'skyctl[agents]'") from e
        self._anthropic = anthropic
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        # Reads ANTHROPIC_API_KEY (or another configured credential) itself.
        self._client = client or anthropic.Anthropic()

    def _create(self, messages: list, system: str, web_search: bool):
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
        }
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        if web_search:
            kwargs["tools"] = [_WEB_SEARCH_TOOL]
        if self.model in _FALLBACK_MODELS:
            # If a safety classifier declines, the API retries the request
            # on a fallback model within the same call.
            # Sent as extra_body so SDK versions without a typed `fallbacks` argument still work.
            return self._client.beta.messages.create(
                betas=[_FALLBACK_BETA], extra_body={"fallbacks": "default"}, **kwargs
            )
        return self._client.messages.create(**kwargs)

    def complete(self, system: str, prompt: str, web_search: bool = False) -> str:
        anthropic = self._anthropic
        messages = [{"role": "user", "content": prompt}]
        try:
            response = self._create(messages, system, web_search)
            # A long web-search turn can pause; send it back to continue.
            for _ in range(_MAX_PAUSE_RESUMES):
                if response.stop_reason != "pause_turn":
                    break
                messages.append({"role": "assistant", "content": response.content})
                response = self._create(messages, system, web_search)
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic rejected the credentials. Check ANTHROPIC_API_KEY.") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"Unknown Anthropic model '{self.model}'.") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Anthropic rate limit reached; try again shortly.") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Could not reach the Anthropic API.") from e

        if response.stop_reason == "refusal":
            raise LLMRefusal(f"{self.model} declined this request.")
        if response.stop_reason == "pause_turn":
            raise LLMError(f"{self.model} did not finish its web search turn.")
        return "".join(b.text for b in response.content if b.type == "text").strip()


# ----------------------------------------------------------------------
# OpenAI-compatible servers (OpenAI, OpenRouter, Ollama, vLLM, LM Studio…)
# ----------------------------------------------------------------------


class OpenAICompatProvider:
    """Any server that speaks the OpenAI chat-completions protocol."""

    supports_web_search = False

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 300.0,
        headers: dict[str, str] | None = None,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.headers = dict(headers or {})

    def complete(self, system: str, prompt: str, web_search: bool = False) -> str:
        # web_search is ignored here: it is a Claude API server tool.
        import httpx

        headers = dict(self.headers)
        if self.api_key:
            headers.setdefault("Authorization", f"Bearer {self.api_key}")
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers=headers,
                timeout=self.timeout,
            )
        except httpx.HTTPError as e:
            raise LLMError(f"Could not reach {self.base_url}: {e}") from e
        if resp.status_code >= 400:
            raise LLMError(f"{self.base_url} returned HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LLMError(f"Unexpected response from {self.base_url}.") from e
        return (content or "").strip()


def _require_env(name: str, provider: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise LLMError(f"{name} is not set (needed for '{provider}:' models).")
    return value


def _openai(model: str, **options) -> LLMProvider:
    base = os.getenv("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1"
    return OpenAICompatProvider(model, base, _require_env("OPENAI_API_KEY", "openai"))


def _openrouter(model: str, **options) -> LLMProvider:
    return OpenAICompatProvider(
        model,
        "https://openrouter.ai/api/v1",
        _require_env("OPENROUTER_API_KEY", "openrouter"),
    )


def _ollama(model: str, **options) -> LLMProvider:
    host = os.getenv("OLLAMA_HOST", "").strip() or "http://localhost:11434"
    if not host.startswith("http"):
        host = f"http://{host}"
    return OpenAICompatProvider(model, f"{host.rstrip('/')}/v1")


def _compat(model: str, **options) -> LLMProvider:
    return OpenAICompatProvider(
        model,
        _require_env("SKYCTL_LLM_BASE_URL", "compat"),
        os.getenv("SKYCTL_LLM_API_KEY", "").strip() or None,
    )


def _anthropic(model: str, **options) -> LLMProvider:
    return AnthropicProvider(model, effort=options.get("effort"))


def _hosted_anthropic(provider: str):
    """Bedrock / Vertex / Foundry: same API, different client, no server-side web search."""

    def build(model: str, **options) -> LLMProvider:
        p = AnthropicProvider(model, effort=options.get("effort"), client=make_anthropic_client(provider))
        p.supports_web_search = False
        return p

    return build


def _azure_openai(model: str, **options) -> LLMProvider:
    base, key, headers = openai_compat_settings("azure-openai")
    return OpenAICompatProvider(model, base, key, headers=headers)


_PROVIDERS: dict[str, Callable[..., LLMProvider]] = {
    "anthropic": _anthropic,
    "openai": _openai,
    "openrouter": _openrouter,
    "ollama": _ollama,
    "compat": _compat,
    "bedrock": _hosted_anthropic("bedrock"),
    "vertex": _hosted_anthropic("vertex"),
    "foundry": _hosted_anthropic("foundry"),
    "azure-openai": _azure_openai,
}


def register_provider(name: str, factory: Callable[..., LLMProvider]) -> None:
    """
    Add a backend. `factory(model, **options)` must return an object with
    a complete(system, prompt, web_search=False) -> str method, and may set
    supports_web_search = True if it can search the web itself.
    """
    _PROVIDERS[name.lower()] = factory


def get_provider(spec: str | None = None, **options) -> LLMProvider:
    """Build the backend for a "provider:model" spec."""
    provider, model = parse_model_spec(spec)
    return _PROVIDERS[provider](model, **options)
