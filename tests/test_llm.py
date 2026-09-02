"""LLM router tests (plan section 9). No network: httpx.MockTransport only."""

from __future__ import annotations

import json
import unittest

import httpx

from core.llm import LLMChainExhausted, LLMRouter, normalize_base_url

from fakes import make_config


def good_payload(text: str = "[EMOTION: happy]\nHi there.") -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def router_for(handler, **config_overrides) -> LLMRouter:
    config = make_config(**config_overrides)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LLMRouter(config, client=client)


BASE_CONFIG = {
    "FIREWORKS_API_KEY": "fw-key",
    "FIREWORKS_MODEL": "fw-model",
    "OPENAI_COMPAT_URL": "http://local.test",
    "OPENAI_COMPAT_MODEL": "oc-model",
}


class NormalizeUrlTest(unittest.TestCase):
    def test_with_and_without_v1(self):
        self.assertEqual(
            normalize_base_url("http://host:11434"), "http://host:11434/v1"
        )
        self.assertEqual(
            normalize_base_url("http://host:11434/"), "http://host:11434/v1"
        )
        self.assertEqual(
            normalize_base_url("http://host:11434/v1"), "http://host:11434/v1"
        )
        self.assertEqual(
            normalize_base_url("https://api.fireworks.ai/inference/v1/"),
            "https://api.fireworks.ai/inference/v1",
        )


class RouteResolutionTest(unittest.TestCase):
    def test_mode_providers_inherit_companion_then_chain(self):
        config = make_config(
            LLM_CHAIN="fireworks,ollama",
            COMPANION_PROVIDERS="openai_compat",
            LIFE_PROVIDERS="",
            OLLAMA_MODEL="llama",
            OPENAI_COMPAT_URL="http://x",
            OPENAI_COMPAT_MODEL="m",
            FIREWORKS_MODEL="fw",
        )
        router = LLMRouter(config, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
        self.assertEqual(
            [r.provider for r in router.routes_for("companion")], ["openai_compat"]
        )
        # life inherits COMPANION_PROVIDERS
        self.assertEqual([r.provider for r in router.routes_for("life")], ["openai_compat"])
        # with COMPANION_PROVIDERS empty, modes inherit LLM_CHAIN
        config2 = make_config(
            LLM_CHAIN="fireworks,ollama", COMPANION_PROVIDERS="",
            OLLAMA_MODEL="llama", FIREWORKS_MODEL="fw",
        )
        router2 = LLMRouter(config2, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
        self.assertEqual(
            [r.provider for r in router2.routes_for("work")], ["fireworks", "ollama"]
        )

    def test_mode_and_provider_model_overrides(self):
        config = make_config(
            LLM_CHAIN="fireworks",
            FIREWORKS_MODEL="base-model",
            COMPANION_MODEL="mode-model",
            env={"COMPANION_FIREWORKS_MODEL": "override-model"},
        )
        router = LLMRouter(config, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
        routes = router.routes_for("companion")
        self.assertEqual(routes[0].model, "override-model")
        # without the per-provider override, the mode model wins
        config.env = {}
        self.assertEqual(router.routes_for("companion")[0].model, "mode-model")
        # without either, the provider default model wins
        config2 = make_config(LLM_CHAIN="fireworks", FIREWORKS_MODEL="base-model")
        router2 = LLMRouter(config2, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
        self.assertEqual(router2.routes_for("companion")[0].model, "base-model")


class FailoverTest(unittest.IsolatedAsyncioTestCase):
    async def test_http_error_fails_over_to_second_provider(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "fireworks" in str(request.url):
                return httpx.Response(500, json={"error": {"message": "boom"}})
            return httpx.Response(200, json=good_payload())

        router = router_for(handler, LLM_CHAIN="fireworks,openai_compat", **BASE_CONFIG)
        result = await router.chat("companion", [{"role": "user", "content": "hi"}])
        self.assertEqual(result.provider, "openai_compat")
        self.assertEqual(result.text, "[EMOTION: happy]\nHi there.")
        self.assertEqual(result.usage["total_tokens"], 5)

    async def test_timeout_fails_over(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "fireworks" in str(request.url):
                raise httpx.ConnectTimeout("timed out", request=request)
            return httpx.Response(200, json=good_payload())

        router = router_for(handler, LLM_CHAIN="fireworks,openai_compat", **BASE_CONFIG)
        result = await router.chat("companion", [])
        self.assertEqual(result.provider, "openai_compat")

    async def test_malformed_json_fails_over(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "fireworks" in str(request.url):
                return httpx.Response(200, content=b"<html>not json</html>")
            return httpx.Response(200, json=good_payload())

        router = router_for(handler, LLM_CHAIN="fireworks,openai_compat", **BASE_CONFIG)
        result = await router.chat("companion", [])
        self.assertEqual(result.provider, "openai_compat")

    async def test_empty_choices_fails_over(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "fireworks" in str(request.url):
                return httpx.Response(200, json={"choices": []})
            return httpx.Response(200, json=good_payload())

        router = router_for(handler, LLM_CHAIN="fireworks,openai_compat", **BASE_CONFIG)
        result = await router.chat("companion", [])
        self.assertEqual(result.provider, "openai_compat")

    async def test_non_dict_response_does_not_crash(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=json.dumps(["not", "a", "dict"]).encode())

        router = router_for(handler, LLM_CHAIN="fireworks", **BASE_CONFIG)
        with self.assertRaises(LLMChainExhausted) as ctx:
            await router.chat("companion", [])
        self.assertIn("non-object", str(ctx.exception))

    async def test_error_object_response_does_not_crash(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": {"message": "rate limited"}})

        router = router_for(handler, LLM_CHAIN="fireworks", **BASE_CONFIG)
        with self.assertRaises(LLMChainExhausted) as ctx:
            await router.chat("companion", [])
        self.assertIn("rate limited", str(ctx.exception))

    async def test_missing_message_object_does_not_crash(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"delta": {}}]})

        router = router_for(handler, LLM_CHAIN="fireworks", **BASE_CONFIG)
        with self.assertRaises(LLMChainExhausted):
            await router.chat("companion", [])

    async def test_empty_content_fails_over(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "fireworks" in str(request.url):
                return httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]})
            return httpx.Response(200, json=good_payload())

        router = router_for(handler, LLM_CHAIN="fireworks,openai_compat", **BASE_CONFIG)
        result = await router.chat("companion", [])
        self.assertEqual(result.provider, "openai_compat")

    async def test_missing_credentials_skip_with_one_warning(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json=good_payload())

        # fireworks has no API key -> skipped; chain repeats provider names
        router = router_for(
            handler,
            LLM_CHAIN="fireworks,fireworks,openai_compat",
            OPENAI_COMPAT_URL="http://local.test",
            OPENAI_COMPAT_MODEL="m",
            FIREWORKS_MODEL="fw-model",
            FIREWORKS_API_KEY="",
        )
        with self.assertLogs("bridge.llm", level="WARNING") as logs:
            result = await router.chat("companion", [])
        self.assertEqual(result.provider, "openai_compat")
        skip_warnings = [line for line in logs.output if "Skipping provider fireworks" in line]
        self.assertEqual(len(skip_warnings), 1)
        self.assertTrue(all("fireworks" not in url for url in calls))

    async def test_chain_deadline_bounds_total_time(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("slow", request=request)

        router = router_for(
            handler,
            LLM_CHAIN="fireworks,openai_compat",
            LLM_CHAIN_DEADLINE_SECONDS=30.0,
            **BASE_CONFIG,
        )
        with self.assertRaises(LLMChainExhausted):
            await router.chat("companion", [])

    async def test_ollama_local_endpoint_needs_no_bearer(self):
        seen_auth = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_auth.append(request.headers.get("authorization"))
            return httpx.Response(200, json=good_payload())

        router = router_for(
            handler, LLM_CHAIN="ollama", OLLAMA_URL="http://127.0.0.1:11434",
            OLLAMA_MODEL="llama3",
        )
        result = await router.chat("companion", [])
        self.assertEqual(result.provider, "ollama")
        self.assertEqual(seen_auth, [None])

    async def test_all_routes_failed_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "down"})

        router = router_for(handler, LLM_CHAIN="fireworks,openai_compat", **BASE_CONFIG)
        with self.assertRaises(LLMChainExhausted):
            await router.chat("companion", [])


if __name__ == "__main__":
    unittest.main()
