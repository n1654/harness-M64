"""GigaChat implementation of `LLMClient`.

References:
    * OAuth token:        https://developers.sber.ru/docs/ru/gigachat/api/reference/rest/post-token
    * Chat completions:   https://developers.sber.ru/docs/ru/gigachat/api/reference/rest/post-chat

Two authentication modes, selected by `GIGACHAT_AUTH_MODE`:

* `credentials` (default) -- Basic auth with `GIGACHAT_AUTH_KEY`
  (base64 of "client_id:client_secret" from developers.sber.ru).

* `mtls` -- mutual TLS: the client presents its X.509 certificate, the server
  validates the chain. No Authorization header is sent on the token request.
  Use when your integration is enrolled through Sberbank's certificate-based
  enterprise flow.

TLS verification:

* `GIGACHAT_TLS_INSECURE=1` -- skip cert verification entirely (dev only).
* `GIGACHAT_CA_BUNDLE=/path/to/ca.pem` -- pin a custom CA bundle
  (typical case: Russian Trust CA chain not present in the base image).
* Default -- use the system trust store.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

import httpx

from harness.llm.base import CompletionResult, LLMClient, Message, Usage

log = logging.getLogger("harness.llm.gigachat")

TOKEN_URL_DEFAULT = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
CHAT_URL_DEFAULT = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

_TOKEN_REFRESH_LEAD_SEC = 60     # refresh this many seconds before expiry


@dataclass
class _Token:
    value: str
    expires_at: float    # epoch seconds


class GigaChatClient(LLMClient):
    """GigaChat client.

    Single instance is safe for concurrent use; one shared `httpx.AsyncClient`
    handles connection pooling. Call `close()` (or use as an async context
    manager) to release sockets.
    """

    def __init__(
        self,
        *,
        auth_mode: str = "credentials",
        auth_key: Optional[str] = None,
        scope: str = "GIGACHAT_API_PERS",
        client_cert: Optional[str] = None,
        client_key: Optional[str] = None,
        client_key_password: Optional[str] = None,
        ca_bundle: Optional[str] = None,
        tls_insecure: bool = False,
        model: str = "GigaChat",
        token_url: str = TOKEN_URL_DEFAULT,
        chat_url: str = CHAT_URL_DEFAULT,
        timeout: float = 120.0,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        if auth_mode not in ("credentials", "mtls"):
            raise ValueError(
                f"unknown auth_mode={auth_mode!r}; expected 'credentials' or 'mtls'"
            )
        if auth_mode == "credentials" and not auth_key:
            raise ValueError("auth_mode='credentials' requires auth_key")
        if auth_mode == "mtls" and not (client_cert and client_key):
            raise ValueError("auth_mode='mtls' requires client_cert and client_key")

        self._auth_mode = auth_mode
        self._auth_key = auth_key
        self._scope = scope
        self._client_cert = client_cert
        self._client_key = client_key
        self._client_key_password = client_key_password
        self._ca_bundle = ca_bundle
        self._tls_insecure = tls_insecure
        self._default_model = model
        self._token_url = token_url
        self._chat_url = chat_url
        self._timeout = timeout

        self._extra_headers: Dict[str, str] = {str(k): str(v) for k, v in (extra_headers or {}).items()}

        self._token: Optional[_Token] = None
        self._token_lock = asyncio.Lock()
        self._http: Optional[httpx.AsyncClient] = None

        if tls_insecure:
            log.warning("TLS verification DISABLED (GIGACHAT_TLS_INSECURE=1)")
        if self._extra_headers:
            log.info("extra HTTP headers: %s", sorted(self._extra_headers))

    # ---- construction from env ----

    @classmethod
    def from_env(cls) -> "GigaChatClient":
        """Build a client from `GIGACHAT_*` env vars. See README for full list."""
        auth_mode = os.environ.get("GIGACHAT_AUTH_MODE", "credentials").strip().lower() or "credentials"

        kwargs: Dict[str, Any] = {
            "auth_mode": auth_mode,
            "scope": os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
            "model": os.environ.get("GIGACHAT_MODEL", "GigaChat"),
            "tls_insecure": _env_bool("GIGACHAT_TLS_INSECURE", default=False),
            "ca_bundle": (os.environ.get("GIGACHAT_CA_BUNDLE") or "").strip() or None,
        }

        if auth_mode == "credentials":
            kwargs["auth_key"] = (os.environ.get("GIGACHAT_AUTH_KEY") or "").strip() or None
        elif auth_mode == "mtls":
            kwargs["client_cert"] = (os.environ.get("GIGACHAT_CLIENT_CERT") or "").strip() or None
            kwargs["client_key"] = (os.environ.get("GIGACHAT_CLIENT_KEY") or "").strip() or None
            kwargs["client_key_password"] = (
                os.environ.get("GIGACHAT_CLIENT_KEY_PASSWORD") or ""
            ).strip() or None

        if url := os.environ.get("GIGACHAT_TOKEN_URL"):
            kwargs["token_url"] = url.strip()
        if url := os.environ.get("GIGACHAT_CHAT_URL"):
            kwargs["chat_url"] = url.strip()

        try:
            kwargs["timeout"] = float(os.environ.get("GIGACHAT_TIMEOUT", "120"))
        except ValueError:
            pass

        raw_headers = (os.environ.get("GIGACHAT_HEADERS") or "").strip()
        if raw_headers:
            try:
                parsed = json.loads(raw_headers)
                if isinstance(parsed, dict):
                    kwargs["extra_headers"] = {str(k): str(v) for k, v in parsed.items()}
                else:
                    log.warning("GIGACHAT_HEADERS must be a JSON object; ignoring")
            except (ValueError, TypeError):
                log.warning("GIGACHAT_HEADERS is not valid JSON; ignoring")

        return cls(**kwargs)

    # ---- lifecycle ----

    async def __aenter__(self) -> "GigaChatClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ---- http client ----

    def _httpx_kwargs(self) -> Dict[str, Any]:
        """Common kwargs for `httpx.AsyncClient` (TLS + mTLS + timeout)."""
        kw: Dict[str, Any] = {"timeout": self._timeout}

        if self._tls_insecure:
            kw["verify"] = False
        elif self._ca_bundle:
            kw["verify"] = self._ca_bundle
        # else: rely on system trust store (httpx default)

        if self._auth_mode == "mtls":
            if self._client_key_password:
                kw["cert"] = (self._client_cert, self._client_key, self._client_key_password)
            else:
                kw["cert"] = (self._client_cert, self._client_key)

        return kw

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(**self._httpx_kwargs())
        return self._http

    # ---- token ----

    async def _ensure_token(self) -> str:
        """Return a valid access token. Refreshes when within the lead window."""
        # Fast path: still valid.
        if self._token and self._token.expires_at - time.time() > _TOKEN_REFRESH_LEAD_SEC:
            return self._token.value

        async with self._token_lock:
            # Double-check after acquiring the lock.
            if self._token and self._token.expires_at - time.time() > _TOKEN_REFRESH_LEAD_SEC:
                return self._token.value

            headers: Dict[str, str] = dict(self._extra_headers)
            headers.update({
                "RqUID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            })
            # In `credentials` mode the secret travels in the header.
            # In `mtls` mode the client certificate IS the credential — Sberbank
            # validates the chain at TLS handshake time, no Authorization header.
            if self._auth_mode == "credentials":
                headers["Authorization"] = f"Basic {self._auth_key}"

            log.info(
                "requesting GigaChat access token (mode=%s scope=%s)",
                self._auth_mode, self._scope,
            )

            http = await self._get_http()
            resp = await http.post(
                self._token_url,
                headers=headers,
                data={"scope": self._scope},
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"GigaChat token request failed: HTTP {resp.status_code} from {self._token_url}. "
                    f"Response body: {resp.text[:1000]!r}"
                )
            data = resp.json()

            value = data.get("access_token") or ""
            if not value:
                raise RuntimeError(f"GigaChat token response missing access_token: {data!r}")

            # Sberbank returns `expires_at` as epoch milliseconds.
            expires_ms = float(data.get("expires_at") or 0)
            expires_at = expires_ms / 1000.0 if expires_ms > 0 else time.time() + 1800

            self._token = _Token(value=value, expires_at=expires_at)
            log.info("got token, expires in %d s", int(expires_at - time.time()))
            return value

    # ---- chat ----

    async def chat(
        self,
        messages: List[Message],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> CompletionResult:
        token = await self._ensure_token()

        payload: Dict[str, Any] = {
            "model": model or self._default_model,
            "messages": [_serialize_message(m) for m in messages],
        }
        if tools:
            payload["tools"] = tools
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        headers: Dict[str, str] = dict(self._extra_headers)
        headers.update({
            "Authorization": f"Bearer {token}",
            "RqUID": uuid.uuid4().hex,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

        http = await self._get_http()
        resp = await http.post(self._chat_url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"GigaChat chat request failed: HTTP {resp.status_code} from {self._chat_url}. "
                f"Response body: {resp.text[:1000]!r}"
            )
        data = resp.json()

        return _parse_response(data)


# ---- helpers ----

def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _serialize_message(m: Message) -> Dict[str, Any]:
    d: Dict[str, Any] = {"role": m.role}
    if m.content is not None:
        d["content"] = m.content
    if m.tool_calls:
        d["tool_calls"] = m.tool_calls
    if m.name:
        d["name"] = m.name
    if m.tool_call_id:
        d["tool_call_id"] = m.tool_call_id
    return d


def _parse_response(data: Dict[str, Any]) -> CompletionResult:
    choices = data.get("choices") or []
    msg_data: Dict[str, Any] = (choices[0].get("message") if choices else {}) or {}
    usage_data: Dict[str, Any] = data.get("usage") or {}

    message = Message(
        role=msg_data.get("role") or "assistant",
        content=msg_data.get("content"),
        tool_calls=msg_data.get("tool_calls") or [],
    )
    usage = Usage(
        prompt_tokens=int(usage_data.get("prompt_tokens") or 0),
        completion_tokens=int(usage_data.get("completion_tokens") or 0),
        total_tokens=int(usage_data.get("total_tokens") or 0),
    )
    return CompletionResult(message=message, usage=usage, raw=data)
