"""Remote RPC client + cookie exchange against the scout-owned ``dsh web``.

All errors are scrubbed of the launch token before being raised so logs and
state records never see it.
"""

from __future__ import annotations

import http.cookiejar
import json
import urllib.error
import urllib.request
import uuid
from typing import Any

from .errors import AuthError, ProtocolError, TransportError, WebBackendError
from .protocol import clip, scrub_token


DEFAULT_SOCKET_TIMEOUT = 15.0


class HostClient:
    """Cookie-authenticated POST client for ``/api/<namespace>/<method>``."""

    def __init__(
        self,
        origin: str,
        jar: http.cookiejar.CookieJar,
        *,
        timeout: float = DEFAULT_SOCKET_TIMEOUT,
        token: str | None = None,
    ) -> None:
        self.origin = origin.rstrip("/")
        self.jar = jar
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )
        self.timeout = timeout
        self.token = token
        self._next = 0

    def _next_rpc_id(self) -> str:
        self._next += 1
        return f"dsh-scout-{uuid.uuid4().hex[:8]}-{self._next}"

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.jar)

    def call(self, endpoint: str, args: dict[str, Any]) -> Any:
        result = self._post(endpoint, args)
        if result.get("ok") is True:
            return result.get("value")
        error = result.get("error")
        if isinstance(error, dict):
            code = clip(error.get("code", "gateway/error"), 80)
            message = scrub_token(str(error.get("message", "")), self.token)
            raise WebBackendError(f"{endpoint}: {code}: {message}")
        raise ProtocolError(f"{endpoint}: malformed error result")

    def call_unchecked(self, endpoint: str, args: dict[str, Any]) -> dict[str, Any]:
        """Return the raw ``result`` envelope (used to detect ``accepted: false``
        on ``session/cancel`` without treating it as an outright error)."""
        return self._post(endpoint, args)

    def _post(self, endpoint: str, args: dict[str, Any]) -> dict[str, Any]:
        rpc_id = self._next_rpc_id()
        body = {
            "type": "client-request",
            "rpcId": rpc_id,
            "method": endpoint,
            "payload": {"args": args},
        }
        request = urllib.request.Request(
            f"{self.origin}/api/{endpoint}",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise AuthError(f"{endpoint}: HTTP {error.code}") from None
            raise TransportError(f"{endpoint}: HTTP {error.code}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            reason = getattr(error, "reason", error)
            raise TransportError(f"{endpoint}: {clip(reason)}") from None
        except json.JSONDecodeError as error:
            raise ProtocolError(f"{endpoint}: non-JSON response") from error
        if not isinstance(payload, dict) or payload.get("type") != "server-response":
            raise ProtocolError(f"{endpoint}: unexpected envelope")
        if payload.get("rpcId") != rpc_id:
            raise ProtocolError(f"{endpoint}: rpcId mismatch")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ProtocolError(f"{endpoint}: missing result")
        return result


def exchange_cookie(
    origin: str,
    path: str,
    token: str | None,
    *,
    timeout: float = DEFAULT_SOCKET_TIMEOUT,
) -> http.cookiejar.CookieJar:
    """Exchange the per-activation launch token for a browser cookie."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    url = f"{origin}{path}"
    if token:
        url = f"{url}?token={urllib.parse.quote(token, safe='')}"
    try:
        with opener.open(
            urllib.request.Request(url, method="GET"), timeout=timeout
        ) as response:
            response.read()
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise AuthError(f"cookie exchange HTTP {error.code}") from None
        raise TransportError(f"cookie exchange HTTP {error.code}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        raise TransportError(f"cookie exchange: {clip(reason)}") from None
    if not list(jar):
        raise AuthError("runtime did not provide an authentication cookie")
    return jar
