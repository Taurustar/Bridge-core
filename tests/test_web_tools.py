"""Web tool safety tests (plan section 24.4)."""

from __future__ import annotations

import asyncio
import gzip
import random
import time
import unittest
from unittest.mock import patch

import httpx

from core.config import Config
from core.web_tools import (
    PrivateAddressError,
    WebToolError,
    WebTools,
    extract_text,
    validate_public_url,
)

from fakes import make_config


class AsyncChunks(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes, delay: float = 0) -> None:
        self.chunks = chunks
        self.delay = delay
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class UrlValidationTest(unittest.TestCase):
    def test_https_only(self):
        with self.assertRaises(WebToolError):
            validate_public_url("http://example.com/page")
        with self.assertRaises(WebToolError):
            validate_public_url("file:///etc/passwd")

    def test_userinfo_and_ports_rejected(self):
        with self.assertRaises(WebToolError):
            validate_public_url("https://user:pass@example.com/")
        with self.assertRaises(WebToolError):
            validate_public_url("https://example.com:8080/")

    def test_private_and_local_hosts_rejected(self):
        with self.assertRaises(WebToolError):
            validate_public_url("https://localhost/")
        with self.assertRaises(WebToolError):
            validate_public_url("https://127.0.0.1/")
        with self.assertRaises(WebToolError):
            validate_public_url("https://192.168.1.10/")
        with self.assertRaises(WebToolError):
            validate_public_url("https://[::1]/")
        with self.assertRaises(WebToolError):
            validate_public_url("https://169.254.169.254/latest/meta-data")

    def test_unknown_host_rejected(self):
        with self.assertRaises(WebToolError):
            validate_public_url("https://this-host-does-not-exist-invalid/")

    def test_public_host_passes_and_normalizes(self):
        with patch("core.web_tools.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 443)),
        ]):
            url = validate_public_url("https://example.com/path?q=1")
        self.assertTrue(url.startswith("https://example.com"))
        self.assertIn("q=1", url)


class TextExtractionTest(unittest.TestCase):
    def test_scripts_and_tags_removed_and_bounded(self):
        html = "<html><script>evil()</script><style>x{}</style><body><p>Hello world</p></body></html>"
        self.assertEqual(extract_text(html, 1000), "Hello world")
        self.assertLessEqual(len(extract_text("word " * 5000, 40)), 40)


class WebToolsFailClosedTest(unittest.TestCase):
    def test_search_fails_closed_without_key(self):
        config = make_config(DAILY_WEB_ENABLED=True, TAVILY_API_KEY="")
        tools = WebTools(config)
        self.assertFalse(tools.available())

        async def run():
            return await tools.for_turn().search("hello")

        result = asyncio.run(run())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "web_disabled")

    def test_caps_enforced_per_turn(self):
        config = make_config(
            DAILY_WEB_ENABLED=True,
            TAVILY_API_KEY="tvly-test",
            DAILY_WEB_SEARCH_CAP=1,
        )
        tools = WebTools(config)

        async def run():
            turn = tools.for_turn()
            first = await turn.search("")
            second = await turn.search("anything")
            return first, second

        first, second = asyncio.run(run())
        self.assertEqual(first["error"], "invalid_query")
        self.assertEqual(second["error"], "search_cap_reached")

    def test_concurrent_turn_budgets_are_independent(self):
        requests = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            return httpx.Response(200, json={"results": []})

        config = make_config(
            DAILY_WEB_ENABLED=True,
            TAVILY_API_KEY="tvly-test",
            DAILY_WEB_SEARCH_CAP=1,
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        tools = WebTools(config, client=client)

        async def run():
            spent_turn = tools.for_turn()
            other_turn = tools.for_turn()
            await spent_turn.search("")
            return await asyncio.gather(
                spent_turn.search("capped"), other_turn.search("allowed")
            )

        with patch("core.web_tools.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 443)),
        ]):
            capped, allowed = asyncio.run(run())
        self.assertEqual(capped["error"], "search_cap_reached")
        self.assertTrue(allowed["ok"])
        self.assertEqual(requests, 1)


class WebToolsTransportTest(unittest.TestCase):
    def setUp(self):
        self.dns = patch(
            "core.web_tools.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
        )
        self.dns_mock = self.dns.start()
        self.addCleanup(self.dns.stop)

    def _tools(self, handler, **overrides) -> WebTools:
        config = make_config(
            DAILY_WEB_ENABLED=True,
            TAVILY_API_KEY="tvly-test",
            **overrides,
        )
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )
        return WebTools(config, client=client)

    def test_search_returns_bounded_results(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "results": [
                    {"title": "Example", "url": "https://example.com/a",
                     "content": "x" * 2000}
                ]
            })

        tools = self._tools(handler)

        async def run():
            return await tools.for_turn().search("hello")

        result = asyncio.run(run())
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["title"], "Example")
        self.assertLessEqual(len(result["results"][0]["content"]), 400)

    def test_open_follows_https_redirect_and_revalidates(self):
        calls: list[httpx.Request] = []

        def resolve(host, port, proto):
            address = {
                "example.com": "93.184.216.34",
                "www.iana.org": "192.0.33.8",
            }[host]
            return [(2, 1, 6, "", (address, port))]

        self.dns_mock.side_effect = resolve

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if request.url.path == "/start":
                return httpx.Response(
                    302, headers={"Location": "https://www.iana.org/final"}
                )
            return httpx.Response(200, content="<p>final page</p>")

        tools = self._tools(handler)

        async def run():
            return await tools.for_turn().open("https://example.com/start")

        result = asyncio.run(run())
        self.assertTrue(result["ok"])
        self.assertEqual(str(calls[0].url), "https://93.184.216.34/start")
        self.assertEqual(str(calls[-1].url), "https://192.0.33.8/final")
        self.assertEqual(calls[-1].headers["host"], "www.iana.org")
        self.assertEqual(calls[-1].extensions["sni_hostname"], "www.iana.org")
        self.assertIn("final page", result["text"])

    def test_search_connects_to_validated_ip_with_tavily_host_and_sni(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"results": []})

        tools = self._tools(handler)
        result = asyncio.run(tools.for_turn().search("hello"))

        self.assertTrue(result["ok"])
        self.assertEqual(seen[0].url.host, "93.184.216.34")
        self.assertEqual(seen[0].headers["host"], "api.tavily.com")
        self.assertEqual(seen[0].extensions["sni_hostname"], "api.tavily.com")

    def test_open_rejects_redirect_to_private_host(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/start":
                return httpx.Response(302, headers={"Location": "https://127.0.0.1/steal"})
            return httpx.Response(200, content="secret")

        tools = self._tools(handler)

        async def run():
            return await tools.for_turn().open("https://example.com/start")

        result = asyncio.run(run())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "private_address")

    def test_open_rejects_too_many_redirects(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                302, headers={"Location": "https://example.com/next"}
            )

        tools = self._tools(handler)

        async def run():
            return await tools.for_turn().open("https://example.com/start")

        result = asyncio.run(run())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "too_many_redirects")

    def test_open_rejects_oversized_body(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content="a" * 3_000_000)

        tools = self._tools(handler, DAILY_WEB_MAX_BYTES=8192)

        async def run():
            return await tools.for_turn().open("https://example.com/big")

        result = asyncio.run(run())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "response_too_large")

    def test_open_rejects_oversized_compressed_wire_body(self):
        body = random.Random(1).randbytes(1010)
        compressed = gzip.compress(body, mtime=0)
        self.assertLessEqual(len(body), 1024)
        self.assertGreater(len(compressed), 1024)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                stream=AsyncChunks(compressed[:600], compressed[600:]),
                headers={"Content-Encoding": "gzip"},
            )

        tools = self._tools(handler, DAILY_WEB_MAX_BYTES=1024)
        result = asyncio.run(tools.for_turn().open("https://example.com/big"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "response_too_large")

    def test_open_rejects_oversized_decompressed_body(self):
        body = b"a" * 10_000
        compressed = gzip.compress(body, mtime=0)
        self.assertLess(len(compressed), 1024)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                stream=AsyncChunks(compressed),
                headers={"Content-Encoding": "gzip"},
            )

        tools = self._tools(handler, DAILY_WEB_MAX_BYTES=1024)
        result = asyncio.run(tools.for_turn().open("https://example.com/big"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "response_too_large")

    def test_search_total_deadline_bounds_slow_stream(self):
        stream = AsyncChunks(b'{"results":', b"[]}", delay=0.03)

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=stream)

        tools = self._tools(handler, DAILY_WEB_TIMEOUT=0.05)
        result = asyncio.run(tools.for_turn().search("hello"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "timeout")
        self.assertTrue(stream.closed)

    def test_search_total_deadline_bounds_dns_resolution(self):
        def slow_resolve(host, port, proto):
            time.sleep(0.05)
            return [(2, 1, 6, "", ("93.184.216.34", port))]

        self.dns_mock.side_effect = slow_resolve

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"results": []})

        tools = self._tools(handler, DAILY_WEB_TIMEOUT=0.01)
        result = asyncio.run(tools.for_turn().search("hello"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "timeout")

    def test_open_total_deadline_spans_redirect_chain(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.03)
            if request.url.path == "/start":
                return httpx.Response(
                    302, headers={"Location": "https://example.com/final"}
                )
            return httpx.Response(200, content="final")

        tools = self._tools(handler, DAILY_WEB_TIMEOUT=0.05)
        result = asyncio.run(tools.for_turn().open("https://example.com/start"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "timeout")

    def test_search_http_error_is_structured(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content="boom")

        tools = self._tools(handler)

        async def run():
            return await tools.for_turn().search("hello")

        result = asyncio.run(run())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "search_failed")


if __name__ == "__main__":
    unittest.main()
