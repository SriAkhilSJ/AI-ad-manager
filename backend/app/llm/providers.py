"""HTTP providers for the LLM gateway.

Each provider is a thin, dependency-free (httpx) implementation of the structured
call contract. Errors are converted to :class:`ProviderUnavailable` so agent
retry/budget logic has a single failure type to handle, and so no vendor
traceback or key ever reaches a client response.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.core.config import Settings
from app.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderUnavailable,
    price_for,
)
from app.llm.openai_compatible import (
    OpenAICompatibleProvider,
    ProviderConfigError,
    RetryPolicy,
    validate_base_url,
)


def _extract_json(text: str) -> dict[str, Any]:
    """Tolerate fenced code blocks and leading prose around a JSON object."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderUnavailable("provider", f"non-JSON response ({exc.msg})") from exc
    if not isinstance(value, dict):
        raise ProviderUnavailable("provider", "response was not a JSON object")
    return value


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, settings: Settings) -> None:
        self._key = settings.openai_api_key
        self._timeout = settings.llm_request_timeout_seconds
        self._base = "https://api.openai.com/v1"

    def is_configured(self) -> bool:
        return bool(self._key)

    async def structured(self, request: LLMRequest) -> LLMResponse:
        if not self.is_configured():
            raise ProviderUnavailable(self.name, "OPENAI_API_KEY is not set")
        model = request.metadata.get("model") or "gpt-4o-mini"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "response_format": {"type": "json_object"},
        }
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base}/chat/completions",
                    headers={"Authorization": f"Bearer {self._key}"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(self.name, type(exc).__name__) from exc
        if resp.status_code >= 400:
            raise ProviderUnavailable(self.name, f"HTTP {resp.status_code}")
        body = resp.json()
        text = body["choices"][0]["message"]["content"]
        usage = body.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        return LLMResponse(
            provider=self.name,
            model=model,
            data=_extract_json(text),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=price_for(self.name, model).cost(prompt_tokens, completion_tokens),
            latency_ms=int((time.perf_counter() - started) * 1000),
            raw_text=text,
        )


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, settings: Settings) -> None:
        self._key = settings.anthropic_api_key
        self._timeout = settings.llm_request_timeout_seconds

    def is_configured(self) -> bool:
        return bool(self._key)

    async def structured(self, request: LLMRequest) -> LLMResponse:
        if not self.is_configured():
            raise ProviderUnavailable(self.name, "ANTHROPIC_API_KEY is not set")
        model = request.metadata.get("model") or "claude-3-5-haiku"
        payload = {
            "model": model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "system": request.system_prompt,
            "messages": [{"role": "user", "content": request.user_prompt}],
        }
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self._key or "",
                        "anthropic-version": "2023-06-01",
                    },
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(self.name, type(exc).__name__) from exc
        if resp.status_code >= 400:
            raise ProviderUnavailable(self.name, f"HTTP {resp.status_code}")
        body = resp.json()
        text = "".join(
            block.get("text", "") for block in body.get("content", []) if isinstance(block, dict)
        )
        usage = body.get("usage") or {}
        prompt_tokens = int(usage.get("input_tokens", 0))
        completion_tokens = int(usage.get("output_tokens", 0))
        return LLMResponse(
            provider=self.name,
            model=model,
            data=_extract_json(text),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=price_for(self.name, model).cost(prompt_tokens, completion_tokens),
            latency_ms=int((time.perf_counter() - started) * 1000),
            raw_text=text,
        )


class OpenRouterProvider(LLMProvider):
    """OpenRouter, expressed as a configured OpenAI-compatible provider.

    The endpoint is no longer a literal in this file: it comes from
    ``OPENROUTER_BASE_URL`` and is validated for scheme/host. The credential is
    ``OPENROUTER_API_KEY`` and nothing else — this class is never handed a
    Velona key.
    """

    name = "openrouter"

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._impl = OpenAICompatibleProvider(
            name=self.name,
            base_url=settings.openrouter_base_url,
            api_key=settings.openrouter_api_key,
            env_var="OPENROUTER_API_KEY",
            default_model="openai/gpt-4o-mini",
            timeout_seconds=settings.openrouter_timeout_seconds,
            retry=RetryPolicy(max_attempts=settings.openrouter_max_retries),
            extra_headers={"HTTP-Referer": "https://crusor.in", "X-Title": "Crusor AI"},
            allow_http_localhost=settings.llm_allow_http_localhost,
            transport=transport,
            client=client,
        )

    # surface the resolved endpoint for diagnostics/tests
    @property
    def base_url(self) -> str:
        return self._impl.base_url

    def is_configured(self) -> bool:
        return self._impl.is_configured()

    def describe(self) -> dict:
        return self._impl.describe()

    async def structured(self, request: LLMRequest) -> LLMResponse:
        return await self._impl.structured(request)

    async def aclose(self) -> None:
        await self._impl.aclose()


class VelonaProvider(LLMProvider):
    """Velona — an OpenAI-compatible vendor with a configured, verified endpoint.

    Neither the key nor the URL is invented here. Both come from
    ``VELONA_API_KEY`` and ``VELONA_BASE_URL``. If either is missing the provider
    is constructed in an *unconfigured* state and every call fails closed with a
    message naming the missing variable — it never falls back to another vendor's
    endpoint or credential.

    ``VELONA_BASE_URL`` must be https (or loopback http). A malformed or
    plain-HTTP remote URL raises :class:`ProviderConfigError` at construction,
    so a misconfiguration is caught at startup rather than after a token has
    been sent in clear text.
    """

    name = "velona"

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        base_url = settings.velona_base_url
        if not base_url:
            # Fail closed. Use a placeholder that is *valid* but unusable so the
            # object still reports unconfigured instead of raising at import time
            # about a URL the operator simply has not supplied yet.
            base_url = "https://unconfigured.invalid/v1"
        self._impl = OpenAICompatibleProvider(
            name=self.name,
            base_url=base_url,
            api_key=settings.velona_api_key,
            env_var="VELONA_API_KEY",
            default_model=settings.llm_reasoning_model,
            timeout_seconds=settings.velona_timeout_seconds,
            retry=RetryPolicy(max_attempts=settings.velona_max_retries),
            allow_http_localhost=settings.llm_allow_http_localhost,
            transport=transport,
            client=client,
        )
        self._url_supplied = bool(settings.velona_base_url)

    @property
    def base_url(self) -> str:
        return self._impl.base_url

    def is_configured(self) -> bool:
        # Both halves are required: a key with no endpoint is not usable.
        return self._impl.is_configured() and self._url_supplied

    def describe(self) -> dict:
        described = self._impl.describe()
        described["base_url_supplied"] = self._url_supplied
        return described

    async def structured(self, request: LLMRequest) -> LLMResponse:
        if not self._url_supplied:
            raise ProviderConfigError(
                "VELONA_BASE_URL is not set; the Velona endpoint must be supplied "
                "through configuration and must not be guessed"
            )
        return await self._impl.structured(request)

    async def aclose(self) -> None:
        await self._impl.aclose()


def build_providers(
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, LLMProvider]:
    """Construct every provider with its own credential and endpoint.

    Each provider receives values, never the whole ``Settings`` object it could
    mine for another vendor's key. There is no cross-provider fallback: the
    returned dict is a lookup table, and a miss raises rather than substituting.
    """
    providers: list[LLMProvider] = [
        OpenAIProvider(settings),
        AnthropicProvider(settings),
        OpenRouterProvider(settings, transport=transport),
        VelonaProvider(settings, transport=transport),
    ]
    if getattr(settings, "content_provider", "mock") == "mock":
        # Phase 3A: a deterministic, free, self-identifying provider so content
        # generation is demonstrable without credentials or spend.
        from app.llm.mock_content import build_mock_provider

        providers.append(build_mock_provider())
    return {p.name: p for p in providers}
