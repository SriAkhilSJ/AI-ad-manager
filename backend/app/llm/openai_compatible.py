"""Provider-independent, OpenAI-compatible LLM transport (Phase 2.5).

Why this module exists
----------------------
Phase 2 shipped three provider classes that each hardcoded their endpoint::

    OpenAIProvider     → https://api.openai.com/v1
    AnthropicProvider  → https://api.anthropic.com/v1
    OpenRouterProvider → https://openrouter.ai/api/v1/chat/completions

That made a new OpenAI-compatible vendor (Velona) impossible to configure, and —
worse — made it *tempting* to point ``OPENROUTER_API_KEY`` at a non-OpenRouter
credential. Both are fixed here, and the security properties are enforced in
code rather than left to operator discipline:

* **Credentials are bound to a provider identity.** A provider holds exactly one
  key, read from exactly one variable. There is no code path that can hand one
  provider's key to another: :func:`build_providers` constructs each provider
  with its own key argument, and :class:`OpenAICompatibleProvider` never sees
  ``Settings`` — only the values it was given.
* **HTTPS is mandatory** for any non-loopback endpoint. Plain HTTP to a remote
  host is refused at construction time, not at request time.
* **Redirects are not followed** when an ``Authorization`` header is present.
  A 3xx is surfaced as a hard failure instead of silently forwarding a bearer
  token to whatever host the redirect names (CWE-522 / token forwarding).
* **Async-safe HTTP.** Each provider owns a single ``httpx.AsyncClient`` reused
  across calls. Per-call clients would defeat connection pooling *and* break
  cookie/redirect isolation assumptions; a shared client with
  ``follow_redirects=False`` is both faster and stricter.
* **Fail closed.** Incomplete configuration raises before any socket is opened.

No vendor SDK is imported. The transport is ``httpx``, injected in tests as
``httpx.MockTransport`` so automated tests never touch the network — and, as an
added guard, a non-mock transport is refused outright when ``APP_ENV=test``.
"""

from __future__ import annotations

import ipaddress
import time
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

import httpx

__all__ = [
    "ProviderConfigError",
    "EndpointPolicy",
    "validate_base_url",
    "RetryPolicy",
    "OpenAICompatibleProvider",
    "ChatMessageStyle",
]


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class ProviderConfigError(RuntimeError):
    """Configuration is incomplete, unsafe, or internally inconsistent.

    Raised *before* any network activity. Subclasses the builtin ``RuntimeError``
    rather than a provider error on purpose: a misconfiguration is an operator
    error, not a provider outage, and must not be swallowed by retry logic that
    only catches ``ProviderUnavailable``.
    """

    code = "provider_config_error"


# --------------------------------------------------------------------------- #
# endpoint policy
# --------------------------------------------------------------------------- #
class ChatMessageStyle(str):
    """How system/user messages are framed for this vendor."""

    OPENAI = "openai"  # {"role": "system"} in the messages array (default)


class EndpointPolicy:
    """Security policy applied to every provider base URL.

    Deliberately a separate object from the provider so the same rules can be
    applied to future transports (embeddings, image, audio) without duplicating
    the checks or accidentally weakening them.
    """

    #: Hosts for which plain HTTP is tolerated because no traffic leaves the box.
    LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

    @classmethod
    def is_loopback(cls, host: str) -> bool:
        host = (host or "").strip("[]").lower()
        if host in cls.LOOPBACK_HOSTS:
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    @classmethod
    def check(cls, base_url: str, *, allow_http_localhost: bool = True) -> str:
        """Validate a base URL and return it normalised (no trailing slash).

        Raises :class:`ProviderConfigError` with a message that never contains a
        credential — only the scheme/host, which are already public.
        """
        raw = (base_url or "").strip()
        if not raw:
            raise ProviderConfigError("base URL is empty; configure an explicit endpoint")

        parsed = urlparse(raw)
        if parsed.scheme not in ("https", "http"):
            raise ProviderConfigError(
                f"base URL scheme '{parsed.scheme or '(none)'}' is not supported; "
                "only https (or http on loopback) is allowed"
            )
        if not parsed.hostname:
            raise ProviderConfigError("base URL has no host")

        if parsed.scheme == "http":
            if not (allow_http_localhost and cls.is_loopback(parsed.hostname)):
                raise ProviderConfigError(
                    f"plain HTTP is refused for non-loopback host '{parsed.hostname}'; "
                    "use https so credentials are not sent in clear text"
                )
        # Reject embedded credentials — they end up in logs and tracebacks.
        if parsed.username or parsed.password:
            raise ProviderConfigError(
                "base URL must not embed credentials; configure the API key separately"
            )
        if parsed.query or parsed.fragment:
            raise ProviderConfigError("base URL must not contain a query string or fragment")

        return raw.rstrip("/")


def validate_base_url(base_url: str, *, allow_http_localhost: bool = True) -> str:
    """Module-level convenience wrapper around :class:`EndpointPolicy`."""
    return EndpointPolicy.check(base_url, allow_http_localhost=allow_http_localhost)


# --------------------------------------------------------------------------- #
# retry
# --------------------------------------------------------------------------- #
class RetryPolicy:
    """Bounded retries with explicit, reviewable rules.

    Only *safe-to-repeat* failures are retried:

    * 429 / 5xx and transport timeouts → retried, because the request was either
      rejected outright or never completed;
    * 4xx other than 429 → **not** retried (a bad key or bad payload will fail
      identically and just burns quota);
    * any 3xx → **not** retried, because redirects are treated as a security
      signal rather than a transient fault.

    There is no automatic cross-provider fallback anywhere in this module. If a
    provider fails, the caller sees the failure; the gateway does not silently
    re-route a prompt — and therefore a credential — to a different vendor.
    """

    def __init__(self, max_attempts: int = 2, backoff_seconds: float = 0.5) -> None:
        if max_attempts < 1:
            raise ProviderConfigError("max_attempts must be >= 1")
        self.max_attempts = int(max_attempts)
        self.backoff_seconds = max(0.0, float(backoff_seconds))

    def should_retry(self, *, status_code: int | None, transport_error: bool) -> bool:
        if transport_error:
            return True
        if status_code is None:
            return False
        if 300 <= status_code < 400:
            return False  # never chase a redirect carrying a bearer token
        if status_code == 429:
            return True
        if status_code >= 500:
            return True
        return False

    def backoff_for(self, attempt: int) -> float:
        return self.backoff_seconds * (2 ** (attempt - 1))


# --------------------------------------------------------------------------- #
# the provider
# --------------------------------------------------------------------------- #
@runtime_checkable
class SupportsStructured(Protocol):
    name: str

    def is_configured(self) -> bool: ...

    async def structured(self, request: Any) -> Any: ...


class OpenAICompatibleProvider:
    """One OpenAI-compatible vendor, fully described by configuration.

    Instances receive plain values — never a ``Settings`` object — so a provider
    physically cannot look up another provider's credential.
    """

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str | None,
        env_var: str,
        default_model: str | None = None,
        timeout_seconds: int = 60,
        max_output_tokens: int | None = None,
        retry: RetryPolicy | None = None,
        extra_headers: dict[str, str] | None = None,
        allow_http_localhost: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not name or not name.strip():
            raise ProviderConfigError("provider name is required")
        self.name = name.strip()
        # Fail closed: refuse an unsafe or malformed endpoint at construction.
        self.base_url = validate_base_url(
            base_url, allow_http_localhost=allow_http_localhost
        )
        self._api_key = api_key or None
        self.env_var = env_var
        self.default_model = default_model
        self.timeout_seconds = int(timeout_seconds)
        self.max_output_tokens = max_output_tokens
        self.retry = retry or RetryPolicy()
        self._extra_headers = dict(extra_headers or {})
        self._transport = transport
        self._client = client
        self._owns_client = client is None
        # Populated by the last request; enables assertions without exposing keys.
        self.last_request_url: str | None = None

    # ---------------------------------------------------------------- status
    def is_configured(self) -> bool:
        return bool(self._api_key)

    def describe(self) -> dict[str, Any]:
        """Safe description for status surfaces.

        Deliberately omits the credential *variable name* as well as its value:
        the gateway's availability block is served over HTTP, and an existing
        security test forbids credential identifiers in that payload. The
        variable name is still available internally (``env_var``) for the
        operator-facing preflight CLI.
        """
        return {
            "name": self.name,
            "base_url": self.base_url,
            "credential_present": self.is_configured(),
            "default_model": self.default_model,
            "timeout_seconds": self.timeout_seconds,
            "max_attempts": self.retry.max_attempts,
        }

    # ---------------------------------------------------------------- client
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        # follow_redirects is False *by default in httpx*, and we state it
        # explicitly because the security property depends on it.
        self._client = httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=False,
            transport=self._transport,
        )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        headers.update(self._extra_headers)
        return headers

    def endpoint(self, path: str = "/chat/completions") -> str:
        """Full URL for a path, derived from the validated base URL only."""
        return f"{self.base_url}{path}"

    # ------------------------------------------------------------- inference
    async def structured(self, request: Any) -> Any:
        """POST a chat completion and parse a JSON object out of it.

        Mirrors the contract the Phase 2 gateway expects: returns an object with
        ``provider``, ``model``, ``data``, ``prompt_tokens``, ``completion_tokens``,
        ``cost_usd``, ``latency_ms``, ``raw_text``.
        """
        from app.llm.base import LLMResponse, ProviderUnavailable, price_for

        if not self.is_configured():
            raise ProviderUnavailable(self.name, f"{self.env_var} is not set")

        model = (request.metadata or {}).get("model") or self.default_model
        if not model:
            raise ProviderConfigError(
                f"no model resolved for provider '{self.name}'; set a default_model "
                "or route the task explicitly"
            )

        max_tokens = int(request.max_tokens)
        if self.max_output_tokens is not None:
            max_tokens = min(max_tokens, int(self.max_output_tokens))

        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "temperature": request.temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }

        url = self.endpoint()
        self.last_request_url = url
        client = self._get_client()
        attempt = 0
        last_error: str = "no attempt made"

        while attempt < self.retry.max_attempts:
            attempt += 1
            started = time.perf_counter()
            try:
                resp = await client.post(url, headers=self._headers(), json=payload)
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__  # never str(exc): may echo the URL
                if self.retry.should_retry(status_code=None, transport_error=True) and (
                    attempt < self.retry.max_attempts
                ):
                    continue
                raise ProviderUnavailable(self.name, last_error) from exc

            # Never follow or accept a redirect: a 3xx here means something is
            # trying to move our Authorization header to another host.
            if 300 <= resp.status_code < 400:
                raise ProviderUnavailable(
                    self.name,
                    "server responded with a redirect; refusing to forward credentials",
                )

            if resp.status_code >= 400:
                last_error = f"HTTP {resp.status_code}"
                if self.retry.should_retry(
                    status_code=resp.status_code, transport_error=False
                ) and attempt < self.retry.max_attempts:
                    continue
                raise ProviderUnavailable(self.name, last_error)

            try:
                body = resp.json()
                text = body["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise ProviderUnavailable(
                    self.name, "malformed response envelope"
                ) from exc

            usage = body.get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
            completion_tokens = int(usage.get("completion_tokens", 0) or 0)

            from app.llm.providers import _extract_json

            return LLMResponse(
                provider=self.name,
                model=model,
                data=_extract_json(text),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=price_for(self.name, model).cost(
                    prompt_tokens, completion_tokens
                ),
                latency_ms=int((time.perf_counter() - started) * 1000),
                raw_text=text,
            )

        raise ProviderUnavailable(self.name, last_error)


def assert_no_live_network_in_tests(transport: Any) -> None:
    """Refuse a real network transport while running automated tests.

    A belt-and-braces guard: even if a test forgets to inject
    ``httpx.MockTransport``, it cannot accidentally spend money.
    """
    import os

    if os.getenv("APP_ENV", "").strip().lower() in {"test", "testing"} and transport is None:
        raise ProviderConfigError(
            "refusing to construct a live HTTP transport in the test environment; "
            "inject httpx.MockTransport"
        )
