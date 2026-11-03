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
    name = "openrouter"

    def __init__(self, settings: Settings) -> None:
        self._key = settings.openrouter_api_key
        self._timeout = settings.llm_request_timeout_seconds

    def is_configured(self) -> bool:
        return bool(self._key)

    async def structured(self, request: LLMRequest) -> LLMResponse:
        if not self.is_configured():
            raise ProviderUnavailable(self.name, "OPENROUTER_API_KEY is not set")
        model = request.metadata.get("model") or "openai/gpt-4o-mini"
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
                    "https://openrouter.ai/api/v1/chat/completions",
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


def build_providers(settings: Settings) -> dict[str, LLMProvider]:
    return {
        p.name: p
        for p in (
            OpenAIProvider(settings),
            AnthropicProvider(settings),
            OpenRouterProvider(settings),
        )
    }
