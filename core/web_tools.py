"""Tavily web search/open with closed-by-default SSRF guards (plan 24.4).

Rules implemented here:
- HTTPS only; no userinfo, no credentials in the URL, no non-443 ports.
- DNS resolved before each request; every resolved address must be a global
  unicast address (loopback, private, link-local, multicast, unspecified,
  reserved ranges rejected), IPv4 and IPv6. The request connects to one of
  those validated IPs while TLS SNI and Host retain the original hostname.
- Redirects are followed manually, at most three hops, revalidating scheme,
  port, and every redirect target's DNS.
- Bounded connect/read timeouts, compressed/decompressed byte caps, and a
  bounded plain-text extraction.
- Fail closed without ``TAVILY_API_KEY``.

"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import re
import socket
import zlib
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import Config

log = logging.getLogger("bridge.web")

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"

_MAX_REDIRECTS = 3
_MAX_SEARCH_RESULTS = 5
_SEARCH_RESULT_MAX_CHARS = 400
_USER_AGENT = "bridge-core-engine/0.6 (+private companion; web tool)"


class WebToolError(RuntimeError):
    """Structured web-tool failure; never escapes as a Python exception."""


class PrivateAddressError(WebToolError):
    """A resolved address is not a global internet address."""


@dataclass
class WebTurnBudget:
    """Mutable counters owned by exactly one companion or work turn."""

    search_attempts: int = 0
    open_attempts: int = 0


@dataclass(frozen=True)
class _ValidatedTarget:
    public_url: str
    connect_url: str
    hostname: str
    host_header: str


def validate_public_url(raw_url: str) -> str:
    """HTTPS-only, no userinfo, port 443 only; returns the normalized URL."""
    return _resolve_public_target(raw_url).public_url


def _resolve_public_target(raw_url: str) -> _ValidatedTarget:
    """Validate and resolve a URL to the IP used for this request."""
    try:
        parts = urlsplit((raw_url or "").strip())
    except ValueError as exc:
        raise WebToolError("invalid_url") from exc
    if parts.scheme.lower() != "https":
        raise WebToolError("https_only")
    if not parts.hostname:
        raise WebToolError("invalid_url")
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise WebToolError("credentials_in_url")
    if parts.port not in (None, 443):
        raise WebToolError("port_not_allowed")
    if parts.query and len(parts.query) > 1024:
        raise WebToolError("query_too_long")
    host = parts.hostname.strip(".")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_global(host):
            log.warning("Blocked web target using a non-global address")
            raise PrivateAddressError("private_address")
        infos = [(0, 0, 0, "", (host, 443))]
    else:
        try:
            infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise WebToolError("dns_failure") from exc
    if not infos:
        raise WebToolError("dns_failure")
    addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        if not _is_global(address):
            log.warning("Blocked web target resolving to non-global address")
            raise PrivateAddressError("private_address")
        if address not in addresses:
            addresses.append(address)
    host_for_url = host
    if ":" in host:  # IPv6 literal needs brackets in the URL
        host_for_url = f"[{host}]"
    address = addresses[0]
    address_for_url = f"[{address}]" if ":" in address else address
    public_url = urlunsplit(("https", host_for_url, parts.path or "/", "", parts.query or ""))
    connect_url = urlunsplit(("https", address_for_url, parts.path or "/", "", parts.query or ""))
    return _ValidatedTarget(public_url, connect_url, host, host_for_url)


def _is_global(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    return parsed.is_global


_TAG_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


def extract_text(raw_html: str, max_chars: int) -> str:
    """Bounded plain-text extraction from an HTML body."""
    text = _TAG_RE.sub(" ", raw_html or "")
    text = _ANY_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text


class WebTools:
    """Tavily search/open with per-turn caps owned by the caller."""

    def __init__(self, config: Config, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._owns_client = client is None

    def available(self) -> bool:
        return bool(self.config.DAILY_WEB_ENABLED and self.config.TAVILY_API_KEY.strip())

    def for_turn(self) -> "TurnWebTools":
        return TurnWebTools(self, WebTurnBudget())

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- tools -----------------------------------------------------------------

    async def search(self, query: str, budget: WebTurnBudget) -> dict:
        if not self.config.TAVILY_API_KEY.strip():
            return {"ok": False, "error": "web_disabled"}
        if budget.search_attempts >= self.config.DAILY_WEB_SEARCH_CAP:
            return {"ok": False, "error": "search_cap_reached"}
        budget.search_attempts += 1
        query = (query or "").strip()
        if not query or len(query) > 400:
            return {"ok": False, "error": "invalid_query"}
        try:
            async with asyncio.timeout(self.config.DAILY_WEB_TIMEOUT):
                _response, body = await self._request(TAVILY_SEARCH_URL, {
                    "api_key": self.config.TAVILY_API_KEY.strip(),
                    "query": query,
                    "search_depth": "basic",
                    "max_results": _MAX_SEARCH_RESULTS,
                    "include_answer": False,
                })
        except (TimeoutError, httpx.TimeoutException):
            return {"ok": False, "error": "timeout"}
        except WebToolError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:  # noqa: BLE001 - structured tool boundary
            log.warning("Tavily search failed", exc_info=True)
            return {"ok": False, "error": "search_failed"}
        results = []
        try:
            data = json.loads(body)
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "search_failed"}
        for item in (data or {}).get("results") or []:
            if not isinstance(item, dict):
                continue
            results.append({
                "title": str(item.get("title", ""))[:200],
                "url": str(item.get("url", ""))[:500],
                "content": str(item.get("content", ""))[:_SEARCH_RESULT_MAX_CHARS],
            })
        return {"ok": True, "results": results}

    async def open(self, raw_url: str, budget: WebTurnBudget) -> dict:
        if not self.config.TAVILY_API_KEY.strip():
            return {"ok": False, "error": "web_disabled"}
        if budget.open_attempts >= self.config.DAILY_WEB_OPEN_CAP:
            return {"ok": False, "error": "open_cap_reached"}
        budget.open_attempts += 1
        try:
            async with asyncio.timeout(self.config.DAILY_WEB_TIMEOUT):
                target = await _resolve_public_target_async(raw_url)
                url = target.public_url
                response, body = await self._fetch(target)
        except (TimeoutError, httpx.TimeoutException):
            return {"ok": False, "error": "timeout"}
        except WebToolError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:  # noqa: BLE001
            log.warning("Web open failed", exc_info=True)
            return {"ok": False, "error": "open_failed"}
        text = extract_text(
            body.decode(response.encoding or "utf-8", errors="replace"),
            self.config.DAILY_WEB_MAX_TEXT_CHARS,
        )
        return {"ok": True, "url": url, "text": text}

    # -- transport ----------------------------------------------------------------

    async def _request(self, url: str, payload: dict) -> tuple[httpx.Response, bytes]:
        """POST to Tavily itself (api.tavily.com is fixed, still validated)."""
        target = await _resolve_public_target_async(url)
        # An IP-keyed pooled TLS connection must not be reused for a
        # different original hostname that happens to resolve to the same IP.
        request = self._client.build_request(
            "POST",
            target.connect_url,
            json=payload,
            headers={
                "User-Agent": _USER_AGENT,
                "Host": target.host_header,
                "Accept-Encoding": "gzip, deflate",
                "Connection": "close",
            },
            timeout=self.config.DAILY_WEB_TIMEOUT,
            extensions={"sni_hostname": target.hostname},
        )
        return await self._send_capped(request)

    async def _fetch(
        self, target: _ValidatedTarget, hops: int = 0
    ) -> tuple[httpx.Response, bytes]:
        """GET with per-hop revalidation and bounded manual redirects."""
        if hops > _MAX_REDIRECTS:
            raise WebToolError("too_many_redirects")
        # See _request: each hostname gets a fresh TLS/SNI handshake.
        request = self._client.build_request(
            "GET",
            target.connect_url,
            headers={
                "User-Agent": _USER_AGENT,
                "Host": target.host_header,
                "Accept-Encoding": "gzip, deflate",
                "Connection": "close",
            },
            timeout=self.config.DAILY_WEB_TIMEOUT,
            extensions={"sni_hostname": target.hostname},
        )
        response, body = await self._send_capped(request)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            if not location:
                raise WebToolError("empty_redirect")
            redirect_target = _resolve_redirect(target.public_url, location)
            return await self._fetch(
                await _resolve_public_target_async(redirect_target), hops + 1
            )
        if response.status_code >= 400:
            raise WebToolError(f"http_{response.status_code}")
        return response, body

    async def _send_capped(
        self, request: httpx.Request
    ) -> tuple[httpx.Response, bytes]:
        """Stream with independent wire and decompressed body byte caps."""
        response = await self._client.send(request, stream=True)
        chunks: list[bytes] = []
        wire_total = 0
        decoded_total = 0
        encoding = response.headers.get("content-encoding", "").strip().lower()
        if encoding in ("", "identity"):
            decoder = None
        elif encoding in ("gzip", "x-gzip"):
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            decoder = zlib.decompressobj()
        else:
            await response.aclose()
            raise WebToolError("unsupported_content_encoding")

        def append_decoded(chunk: bytes) -> None:
            nonlocal decoded_total
            decoded_total += len(chunk)
            if decoded_total > self.config.DAILY_WEB_MAX_BYTES:
                raise WebToolError("response_too_large")
            if chunk:
                chunks.append(chunk)

        async def iter_raw(chunk_size: int):
            if response.is_stream_consumed:
                yield response.content
                return
            async for chunk in response.aiter_raw(chunk_size=chunk_size):
                yield chunk

        try:
            chunk_size = min(64 * 1024, self.config.DAILY_WEB_MAX_BYTES + 1)
            async for raw_chunk in iter_raw(chunk_size):
                wire_total += len(raw_chunk)
                if wire_total > self.config.DAILY_WEB_MAX_BYTES:
                    raise WebToolError("response_too_large")
                if decoder is None:
                    append_decoded(raw_chunk)
                    continue

                pending = raw_chunk
                while pending:
                    decoded = decoder.decompress(
                        pending,
                        self.config.DAILY_WEB_MAX_BYTES - decoded_total + 1,
                    )
                    pending = decoder.unconsumed_tail
                    append_decoded(decoded)
                if decoder.unused_data:
                    raise WebToolError("invalid_content_encoding")

            if decoder is not None:
                append_decoded(decoder.flush(
                    self.config.DAILY_WEB_MAX_BYTES - decoded_total + 1
                ))
                if not decoder.eof:
                    raise WebToolError("invalid_content_encoding")
        except zlib.error as exc:
            raise WebToolError("invalid_content_encoding") from exc
        finally:
            await response.aclose()
        return response, b"".join(chunks)


class TurnWebTools:
    """A web-tool view whose budget cannot be reset by another turn."""

    def __init__(self, tools: WebTools, budget: WebTurnBudget) -> None:
        self._tools = tools
        self.budget = budget

    async def search(self, query: str) -> dict:
        return await self._tools.search(query, self.budget)

    async def open(self, url: str) -> dict:
        return await self._tools.open(url, self.budget)


def _resolve_redirect(base_url: str, location: str) -> str:
    try:
        parts = urlsplit(location)
    except ValueError as exc:
        raise WebToolError("invalid_redirect") from exc
    if parts.scheme:
        return location
    base = urlsplit(base_url)
    if location.startswith("/"):
        return urlunsplit(("https", base.netloc, location, "", ""))
    raise WebToolError("relative_redirect_not_allowed")


async def _resolve_public_target_async(raw_url: str) -> _ValidatedTarget:
    """Resolve off-loop so the operation deadline also bounds DNS waiting."""
    return await asyncio.to_thread(_resolve_public_target, raw_url)


__all__ = [
    "WebTools",
    "TurnWebTools",
    "WebTurnBudget",
    "WebToolError",
    "PrivateAddressError",
    "validate_public_url",
    "extract_text",
]
