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
        tool_format: str = "functions",
    ) -> None:
        if tool_format not in ("functions", "tools"):
            raise ValueError(
                f"unknown tool_format={tool_format!r}; expected 'functions' (legacy GigaChat) "
                f"or 'tools' (OpenAI-compatible proxies)"
            )
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
        self._tool_format = tool_format

        self._token: Optional[_Token] = None
        self._token_lock = asyncio.Lock()
        self._http: Optional[httpx.AsyncClient] = None

        if tls_insecure:
            log.warning("TLS verification DISABLED (GIGACHAT_TLS_INSECURE=1)")
        if self._extra_headers:
            log.info("extra HTTP headers: %s", sorted(self._extra_headers))
        log.info("tool wire format: %s", self._tool_format)

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

        tf = (os.environ.get("GIGACHAT_TOOL_FORMAT") or "functions").strip().lower()
        kwargs["tool_format"] = tf

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

        ser_msg = _serialize_message_openai if self._tool_format == "tools" else _serialize_message

        ser_msgs = [ser_msg(m) for m in messages]
        # GigaChat v1: detect whether the conversation already entered a
        # function-calling chain (any function-result message present).
        has_function_result = (
            self._tool_format == "functions"
            and any(m.get("role") == "function" for m in ser_msgs)
        )

        payload: Dict[str, Any] = {
            "model": model or self._default_model,
            "messages": ser_msgs,
        }
        if tools:
            if self._tool_format == "tools":
                # OpenAI-compatible proxies expect this shape on every turn.
                payload["tools"] = [_to_openai_tool(t) for t in tools]
                payload["tool_choice"] = "auto"
            elif not has_function_result:
                # GigaChat v1 quirk: `functions[]` + `function_call` are only
                # valid on the FIRST request of a function-calling chain.
                # Re-sending them together with an assistant-function_call +
                # function-result pair in `messages[]` triggers 422
                # "INVALID_PARAMS: functions ... should only appear in user,
                # function messages or random role messages".
                #
                # On follow-up turns we must keep BOTH the assistant turn AND
                # the function-result in `messages[]` (otherwise the server
                # complains: "every assistant function call must have a result
                # in history") -- but omit the top-level `functions[]` /
                # `function_call`. The chain is resumed via `functions_state_id`
                # already echoed on the messages themselves.
                payload["functions"] = [_to_giga_function(t) for t in tools]
                payload["function_call"] = "auto"
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        headers: Dict[str, str] = dict(self._extra_headers)
        headers.update({
            "Authorization": f"Bearer {token}",
            "RqUID": str(uuid.uuid4()),
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

        if log.isEnabledFor(logging.DEBUG):
            log.debug("chat payload: %s", json.dumps(payload, ensure_ascii=False)[:2000])

        http = await self._get_http()
        resp = await http.post(self._chat_url, headers=headers, json=payload)
        if resp.status_code >= 400:
            # Dump full payload to a file (overwrite each time), so the operator
            # can inspect even when it's bigger than any sane log line.
            dump_path = "/tmp/harness_last_failed_payload.json"
            try:
                import pathlib as _pl
                _pl.Path(dump_path).write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
                )
            except Exception:    # noqa: BLE001
                dump_path = "(failed to write dump)"

            # Also show the TAIL of the payload (which is where the offending
            # function_call / function-result messages live).
            payload_str = json.dumps(payload, ensure_ascii=False)
            head = payload_str[:400]
            tail = payload_str[-1200:] if len(payload_str) > 400 else ""
            preview = head + ("\n  ...[middle truncated]...\n" + tail if tail else "")

            raise RuntimeError(
                f"GigaChat chat request failed: HTTP {resp.status_code} from {self._chat_url}.\n"
                f"  tool_format={self._tool_format!r}\n"
                f"  response body: {resp.text[:1000]!r}\n"
                f"  full payload dumped to: {dump_path}\n"
                f"  payload (head + tail): {preview}"
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
    """Translate our internal `Message` to GigaChat's wire format.

    Mapping vs OpenAI-compat shape:
        * tool result          role="tool"     -> role="function"   (name + content, no id)
        * assistant tool_calls -> assistant + function_call (first call only -- GigaChat
                                  doesn't support parallel calls per turn)
        * everything else      -> identical

    The `content` field is ALWAYS present (null when there is no text). Several
    GigaChat-compatible proxies reject messages that omit it.
    """
    if m.role == "tool":
        # Function-result: pair with the preceding assistant turn by name.
        # We intentionally do NOT echo `functions_state_id` -- having it on
        # both the assistant and the function message confuses the validator.
        #
        # GigaChat v1 also REQUIRES `content` to be a JSON-parseable string
        # ("invalid function result for function X json string `...`,
        # JSON parse error..."). Plain text like "2026-06-08 12:00 UTC" fails
        # at the first non-numeric character. We wrap via json.dumps so any
        # tool return becomes a valid JSON string literal; tool returns that
        # already parse as JSON (numbers, objects) pass through.
        raw = m.content or ""
        try:
            json.loads(raw)
            wrapped = raw
        except (ValueError, TypeError):
            wrapped = json.dumps(raw, ensure_ascii=False)
        return {
            "role": "function",
            "name": m.name or "",
            "content": wrapped,
        }

    if m.tool_calls:
        first = m.tool_calls[0] or {}
        fn = first.get("function") or {}
        raw_args = fn.get("arguments", "{}")
        # GigaChat v1 (developers.sber.ru) expects `arguments` as a JSON OBJECT,
        # not a string (the latter is the OpenAI-legacy convention). Parse so we
        # serialize as an object.
        if isinstance(raw_args, str):
            try:
                args_obj: Any = json.loads(raw_args)
                if not isinstance(args_obj, dict):
                    args_obj = {}
            except (ValueError, TypeError):
                args_obj = {}
        elif isinstance(raw_args, dict):
            args_obj = raw_args
        else:
            args_obj = {}
        # The Sber wire convention uses content="" (not null) on assistant turns
        # that carry a function_call. Match what the API itself emits.
        return {
            "role": m.role,
            "content": m.content if m.content is not None else "",
            "function_call": {
                "name": fn.get("name", ""),
                "arguments": args_obj,
            },
        }

    return {
        "role": m.role,
        "content": m.content,        # explicit null is OK and safer than omission
        **({"name": m.name} if m.name else {}),
    }


def _serialize_message_openai(m: Message) -> Dict[str, Any]:
    """OpenAI-compatible wire shape. Used when tool_format='tools'.

    Always emits an explicit `content` field (null when empty) -- some proxies
    reject messages without it.
    """
    if m.role == "tool":
        out: Dict[str, Any] = {"role": "tool", "content": m.content or ""}
        if m.tool_call_id:
            out["tool_call_id"] = m.tool_call_id
        if m.name:
            out["name"] = m.name
        return out
    d: Dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        d["tool_calls"] = m.tool_calls
    if m.name:
        d["name"] = m.name
    return d


def _to_openai_tool(t: Dict[str, Any]) -> Dict[str, Any]:
    """Pass through OpenAI-shape tools verbatim; wrap a bare function dict if needed."""
    if t.get("type") == "function" and "function" in t:
        return t
    return {"type": "function", "function": t}


def _to_giga_function(t: Dict[str, Any]) -> Dict[str, Any]:
    """Translate an OpenAI-shape tool entry (`{type: "function", function: {...}}`)
    to GigaChat's `functions[]` shape. Passes through GigaChat extensions when
    present (`few_shot_examples`, `return_parameters`)."""
    fn = t.get("function") if t.get("type") == "function" else t
    out: Dict[str, Any] = {
        "name": fn["name"],
        "description": fn.get("description", ""),
        "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
    }
    if "few_shot_examples" in fn:
        out["few_shot_examples"] = fn["few_shot_examples"]
    if "return_parameters" in fn:
        out["return_parameters"] = fn["return_parameters"]
    return out


def _parse_response(data: Dict[str, Any]) -> CompletionResult:
    """Normalise GigaChat's `function_call` back to our internal `tool_calls`
    shape so callers don't see the legacy schema."""
    choices = data.get("choices") or []
    msg_data: Dict[str, Any] = (choices[0].get("message") if choices else {}) or {}
    usage_data: Dict[str, Any] = data.get("usage") or {}

    tool_calls: List[Dict[str, Any]] = list(msg_data.get("tool_calls") or [])
    if not tool_calls:
        fc = msg_data.get("function_call")
        if isinstance(fc, dict) and fc.get("name"):
            args = fc.get("arguments", "{}")
            if not isinstance(args, str):
                # GigaChat returns parsed dict -- keep as JSON string in the internal
                # contract; the giga serializer parses it back to object on outgoing.
                args = json.dumps(args, ensure_ascii=False)
            tool_calls = [{
                "id": "call_" + uuid.uuid4().hex[:12],
                "type": "function",
                "function": {"name": fc["name"], "arguments": args},
            }]

    # GigaChat-specific: opaque state token tying together one function-call chain.
    # We propagate it back on subsequent turns so the server can correlate the
    # function-result with the original call.
    provider_meta: Optional[Dict[str, Any]] = None
    state_id = msg_data.get("functions_state_id")
    if state_id:
        provider_meta = {"functions_state_id": state_id}

    message = Message(
        role=msg_data.get("role") or "assistant",
        content=msg_data.get("content"),
        tool_calls=tool_calls,
        provider_meta=provider_meta,
    )
    usage = Usage(
        prompt_tokens=int(usage_data.get("prompt_tokens") or 0),
        completion_tokens=int(usage_data.get("completion_tokens") or 0),
        total_tokens=int(usage_data.get("total_tokens") or 0),
    )
    return CompletionResult(message=message, usage=usage, raw=data)
