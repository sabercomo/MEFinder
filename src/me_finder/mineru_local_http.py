"""Shared HTTP transport for the locally deployed MinerU service.

Both supported MinerU protocols (the 3.x ``/tasks`` API and the 4.x ``/v1``
jobs API) talk plain JSON over ``http.client``, so connection handling, the
response size cap, and the provider-neutral error mapping live here.
"""

from __future__ import annotations

import http.client
import json
import socket
from typing import Dict, Optional
from urllib.parse import urlparse

from .parser_provider import ParserProviderError


MINERU_LOCAL_PROVIDER_ID = "mineru-local"
MAX_LOCAL_JSON_RESPONSE_BYTES = 64 * 1024 * 1024


class MinerULocalTransport:
    """Low-level request helper bound to one endpoint."""

    def __init__(
        self,
        endpoint: str,
        *,
        timeout_seconds: float,
        api_key: str = "",
    ) -> None:
        self.base = urlparse(endpoint.rstrip("/"))
        self.timeout_seconds = timeout_seconds
        self.api_key = api_key

    def json_request(
        self,
        method: str,
        endpoint: str,
        *,
        body: Optional[object] = None,
    ) -> Dict[str, object]:
        """Send an optional JSON body and decode a JSON object response."""

        payload = None
        headers = self.headers()
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = self.connection()
        try:
            connection.request(
                method, self.path(endpoint), body=payload, headers=headers
            )
            return self.decode_response(connection.getresponse())
        except ParserProviderError:
            raise
        except (OSError, socket.timeout) as exc:
            raise self.connection_error(exc) from exc
        finally:
            connection.close()

    def stream_file(
        self,
        method: str,
        endpoint: str,
        *,
        path,
        content_type: str,
        chunk_bytes: int = 8 * 1024 * 1024,
    ) -> bytes:
        """Send a file body without buffering it in memory."""

        from pathlib import Path as _Path

        source = _Path(path)
        size = source.stat().st_size
        connection = self.connection()
        try:
            connection.putrequest(method, self.path(endpoint))
            connection.putheader("Content-Type", content_type)
            connection.putheader("Content-Length", str(size))
            for name, value in self.headers().items():
                connection.putheader(name, value)
            connection.endheaders()
            with source.open("rb") as stream:
                while True:
                    chunk = stream.read(chunk_bytes)
                    if not chunk:
                        break
                    connection.send(chunk)
            response = connection.getresponse()
            raw = self.read_capped(response)
            self.raise_for_status(response, raw)
            return raw
        except ParserProviderError:
            raise
        except (OSError, socket.timeout) as exc:
            raise self.connection_error(exc) from exc
        finally:
            connection.close()

    def download(self, endpoint: str) -> bytes:
        """Fetch one artifact body without requiring a JSON payload."""

        headers = self.headers()
        headers["Accept"] = "*/*"
        connection = self.connection()
        try:
            connection.request("GET", self.path(endpoint), headers=headers)
            response = connection.getresponse()
            raw = self.read_capped(response)
            self.raise_for_status(response, raw)
            return raw
        except ParserProviderError:
            raise
        except (OSError, socket.timeout) as exc:
            raise self.connection_error(exc) from exc
        finally:
            connection.close()

    def headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def connection(self):
        connection_type = (
            http.client.HTTPSConnection
            if self.base.scheme == "https"
            else http.client.HTTPConnection
        )
        return connection_type(
            self.base.hostname,
            self.base.port,
            timeout=self.timeout_seconds,
        )

    def path(self, endpoint: str) -> str:
        prefix = self.base.path.rstrip("/")
        return f"{prefix}{endpoint}" or "/"

    def connection_error(self, exc: BaseException) -> ParserProviderError:
        return ParserProviderError(
            f"MinerU Local connection failed: {exc}",
            provider_id=MINERU_LOCAL_PROVIDER_ID,
            retryable=True,
        )

    def read_capped(self, response) -> bytes:
        raw = response.read(MAX_LOCAL_JSON_RESPONSE_BYTES + 1)
        if len(raw) > MAX_LOCAL_JSON_RESPONSE_BYTES:
            raise ParserProviderError(
                "MinerU Local JSON response exceeds the configured safety limit",
                provider_id=MINERU_LOCAL_PROVIDER_ID,
            )
        return raw

    def raise_for_status(self, response, raw: bytes) -> None:
        status = int(response.status)
        if 200 <= status < 300:
            return
        remote_missing = status in {404, 410}
        raise ParserProviderError(
            f"MinerU Local HTTP {status}: {raw[:500].decode('utf-8', 'replace')}",
            provider_id=MINERU_LOCAL_PROVIDER_ID,
            retryable=status >= 500 or status in {408, 429} or remote_missing,
            rate_limited=status == 429,
            remote_task_missing=remote_missing,
            status_code=status,
        )

    def decode_response(self, response) -> Dict[str, object]:
        raw = self.read_capped(response)
        self.raise_for_status(response, raw)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ParserProviderError(
                "MinerU Local returned malformed JSON",
                provider_id=MINERU_LOCAL_PROVIDER_ID,
            ) from exc
        if not isinstance(value, dict):
            raise ParserProviderError(
                "MinerU Local response must be a JSON object",
                provider_id=MINERU_LOCAL_PROVIDER_ID,
            )
        return value

