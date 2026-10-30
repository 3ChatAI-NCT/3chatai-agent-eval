"""Minimal platform HTTP transport.

Uses the standard library on purpose: the executor is synchronous and we do not want
another runtime dependency for a handful of JSON calls. The transport is injectable
so tests never touch the network.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol


class PlatformHTTPError(RuntimeError):
    def __init__(self, path: str, status: int | None, detail: str) -> None:
        self.path = path
        self.status = status
        self.detail = detail
        super().__init__(f"{path} failed ({status if status is not None else 'network'}): {detail}")

    @property
    def is_auth_error(self) -> bool:
        return self.status in {401, 403}


OK_BUSINESS_CODES = {None, 200, "200", 0, "0"}


def ensure_business_ok(payload: Any, path: str) -> Any:
    """平台常用「HTTP 200 + 业务错误码」表达失败（例如 `{"code":401,"message":"鉴权失败"}`）。

    这种响应如果不检查，就会被下游当成"这一页没有数据"，于是采集"成功"但一条都没有——必须报错。
    """

    if not isinstance(payload, dict):
        return payload
    code = payload.get("code")
    succeeded = payload.get("ret")
    failed = (code not in OK_BUSINESS_CODES) or succeeded is False
    if not failed:
        return payload
    message = str(
        payload.get("message")
        or payload.get("errorMsg")
        or payload.get("error_code")
        or "platform business error"
    )
    status = int(code) if isinstance(code, (int, str)) and str(code).isdigit() else None
    raise PlatformHTTPError(path, status, message[:300])


class HttpTransport(Protocol):
    def post(
        self, url: str, *, headers: dict[str, str], payload: dict[str, Any],
        response_headers: dict[str, str] | None = None,
    ) -> Any: ...

    def get(
        self, url: str, *, headers: dict[str, str], params: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> Any: ...


class UrllibTransport:
    def __init__(self, timeout_seconds: float = 45.0) -> None:
        self._timeout_seconds = timeout_seconds

    def post(
        self, url: str, *, headers: dict[str, str], payload: dict[str, Any],
        response_headers: dict[str, str] | None = None,
    ) -> Any:
        """传入 response_headers 时把响应头（键转小写）写进去，供调用方读取网关回写的值。"""

        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                if response_headers is not None:
                    response_headers.update(
                        (key.lower(), value) for key, value in response.headers.items()
                    )
                return json.load(response)
        except urllib.error.HTTPError as exc:
            body = exc.read(1000).decode("utf-8", errors="replace")
            raise PlatformHTTPError(url, exc.code, body) from exc
        except TimeoutError as exc:
            raise PlatformHTTPError(url, None, self._timeout_detail()) from exc
        except urllib.error.URLError as exc:
            raise self._network_error(url, exc) from exc

    def get(
        self, url: str, *, headers: dict[str, str], params: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> Any:
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        request = urllib.request.Request(url, headers=headers, method="GET")
        timeout = (self._timeout_seconds if timeout_seconds is None
                   else min(self._timeout_seconds, timeout_seconds))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            body = exc.read(1000).decode("utf-8", errors="replace")
            raise PlatformHTTPError(url, exc.code, body) from exc
        except TimeoutError as exc:
            raise PlatformHTTPError(url, None, self._timeout_detail()) from exc
        except urllib.error.URLError as exc:
            raise self._network_error(url, exc) from exc

    def get_bytes(self, url: str, *, max_bytes: int = 20_000_000) -> tuple[bytes, str]:
        """Download a media file (attachments live on CDN hosts, not on the platform API)."""

        request = urllib.request.Request(url, headers={"Accept": "*/*"}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                content_type = response.headers.get("Content-Type", "application/octet-stream")
                data = response.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise PlatformHTTPError(url, exc.code, "attachment download failed") from exc
        except TimeoutError as exc:
            raise PlatformHTTPError(url, None, self._timeout_detail()) from exc
        except urllib.error.URLError as exc:
            raise self._network_error(url, exc) from exc
        if len(data) > max_bytes:
            raise PlatformHTTPError(url, None, f"attachment larger than {max_bytes} bytes")
        return data, content_type

    def _timeout_detail(self) -> str:
        return (
            f"timeout after {self._timeout_seconds:g}s"
            "（请检查平台响应耗时与调用方 HTTP 超时配置）"
        )

    def _network_error(self, url: str, exc: urllib.error.URLError) -> PlatformHTTPError:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return PlatformHTTPError(url, None, self._timeout_detail())
        return PlatformHTTPError(url, None, str(reason))
