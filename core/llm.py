"""LLM provider router (plan section 9).

Providers: fireworks, chutes, ollama, openai_compat — all spoken to through
the OpenAI-compatible chat-completions shape. Routes are ordered
``(provider, model)`` pairs resolved from ``{MODE}_PROVIDERS`` ->
``COMPANION_PROVIDERS`` -> ``LLM_CHAIN`` inheritance (plan section 8.2).

Failover: missing credentials skip with one warning; timeout, HTTP error,
malformed JSON, empty choices, and empty content move to the next provider.
The total chain is bounded by ``LLM_CHAIN_DEADLINE_SECONDS`` and usage is
additive across attempts. One shared async httpx client per router.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Config, parse_chain

log = logging.getLogger("bridge.llm")


class LLMChainExhausted(RuntimeError):
    """Every route in the chain failed (or the chain deadline expired)."""


@dataclass(frozen=True)
class ProviderRoute:
    provider: str
    model: str


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    usage: dict[str, int] | None
    attempts: int
    tool_calls: list[dict] | None = None  # normalized OpenAI-style calls


def normalize_base_url(url: str) -> str:
    """Normalize an OpenAI-compatible base URL with or without ``/v1``."""
    base = url.strip().rstrip("/")
    if not base:
        return base
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


class LLMRouter:
    def __init__(self, config: Config, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- route resolution ---------------------------------------------------

    def routes_for(self, mode: str) -> list[ProviderRoute]:
        mode = mode.lower()
        env_name = f"{mode.upper()}_PROVIDERS"
        raw = getattr(self._config, env_name, "") or ""
        if not raw.strip() and mode != "companion":
            raw = self._config.COMPANION_PROVIDERS
        if not raw.strip():
            raw = self._config.LLM_CHAIN
        routes: list[ProviderRoute] = []
        for provider in parse_chain(raw):
            model = self._model_for(mode, provider)
            routes.append(ProviderRoute(provider=provider, model=model))
        return routes

    def _model_for(self, mode: str, provider: str) -> str:
        """Per-mode/per-provider override, then mode default, then provider default."""
        override = self._config.env_override(
            f"{mode.upper()}_{provider.upper()}_MODEL"
        )
        if override:
            return override
        mode_model = getattr(self._config, f"{mode.upper()}_MODEL", "") or ""
        if mode_model.strip():
            return mode_model.strip()
        return getattr(self._config, f"{provider.upper()}_MODEL", "") or ""

    # -- provider settings ---------------------------------------------------

    def _provider_settings(self, route: ProviderRoute) -> dict[str, Any] | None:
        """Return connection settings, or None when credentials are missing."""
        cfg = self._config
        name = route.provider
        if name == "fireworks":
            url, key, timeout = cfg.FIREWORKS_URL, cfg.FIREWORKS_API_KEY, cfg.FIREWORKS_TIMEOUT
            tier = cfg.FIREWORKS_SERVICE_TIER
            if not key.strip() or not route.model.strip():
                return None
        elif name == "chutes":
            url, key, timeout = cfg.CHUTES_URL, cfg.CHUTES_API_KEY, cfg.CHUTES_TIMEOUT
            tier = ""
            if not key.strip() or not url.strip() or not route.model.strip():
                return None
        elif name == "ollama":
            url, key, timeout = cfg.OLLAMA_URL, "", cfg.OLLAMA_TIMEOUT
            tier = ""
            if not url.strip() or not route.model.strip():
                return None
        elif name == "openai_compat":
            url, key, timeout = (
                cfg.OPENAI_COMPAT_URL,
                cfg.OPENAI_COMPAT_API_KEY,
                cfg.OPENAI_COMPAT_TIMEOUT,
            )
            tier = ""
            if not url.strip() or not route.model.strip():
                return None
        else:
            return None
        return {
            "url": normalize_base_url(url),
            "key": key.strip(),
            "timeout": timeout if timeout and timeout > 0 else None,
            "service_tier": tier.strip(),
        }

    # -- calling ---------------------------------------------------------------

    async def chat(
        self,
        mode: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        pinned: ProviderRoute | None = None,
    ) -> LLMResult:
        """One routed chat call.

        ``tools`` carries OpenAI function schemas; when given, tool calls
        come back on the result instead of failing validation. ``pinned``
        restricts the chain to one already-successful route (tool loops pin
        the first successful provider per plan 9.3); if that route fails
        the exception propagates and the caller may unpin.
        """
        routes = [pinned] if pinned is not None else self.routes_for(mode)
        if not routes or routes[0] is None:
            raise LLMChainExhausted(f"No LLM routes configured for mode {mode!r}")

        deadline = time.monotonic() + self._config.LLM_CHAIN_DEADLINE_SECONDS
        total_usage: dict[str, int] = {}
        saw_usage = False
        attempts = 0
        errors: list[str] = []
        skipped: set[str] = set()

        for route in routes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                errors.append("chain deadline exceeded")
                break
            settings = self._provider_settings(route)
            if settings is None:
                if route.provider not in skipped:
                    log.warning(
                        "Skipping provider %s: missing credentials/model configuration",
                        route.provider,
                    )
                    skipped.add(route.provider)
                continue
            attempts += 1
            try:
                text, usage, tool_calls = await self._call_provider(
                    route, settings, messages, mode, remaining, tools
                )
            except Exception as exc:  # noqa: BLE001 - failover boundary
                log.warning(
                    "Provider %s attempt failed for mode %s: %s",
                    route.provider,
                    mode,
                    exc,
                )
                errors.append(f"{route.provider}: {exc}")
                continue
            if usage:
                saw_usage = True
                for key, value in usage.items():
                    total_usage[key] = total_usage.get(key, 0) + int(value)
            return LLMResult(
                text=text,
                provider=route.provider,
                model=route.model,
                usage=total_usage if saw_usage else None,
                attempts=attempts,
                tool_calls=tool_calls,
            )

        raise LLMChainExhausted(
            f"All LLM routes failed for mode {mode!r}: {'; '.join(errors) or 'no usable routes'}"
        )

    async def _call_provider(
        self,
        route: ProviderRoute,
        settings: dict[str, Any],
        messages: list[dict],
        mode: str,
        budget_seconds: float,
        tools: list[dict] | None = None,
    ) -> tuple[str, dict[str, int] | None, list[dict] | None]:
        cfg = self._config
        body: dict[str, Any] = {
            "model": route.model,
            "messages": messages,
            "temperature": getattr(cfg, f"{mode.upper()}_TEMPERATURE", 0.8),
            "max_tokens": getattr(cfg, f"{mode.upper()}_MAX_TOKENS", 1200),
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if settings["service_tier"]:
            body["service_tier"] = settings["service_tier"]

        headers = {"Content-Type": "application/json"}
        if settings["key"]:
            headers["Authorization"] = f"Bearer {settings['key']}"

        timeout = settings["timeout"]
        if timeout is None or timeout > budget_seconds:
            timeout = max(budget_seconds, 0.001)
        response = await self._client.post(
            f"{settings['url']}/chat/completions",
            json=body,
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        content, usage, tool_calls = self._parse_response(response)
        return content, usage, tool_calls

    @staticmethod
    def _parse_response(
        response: httpx.Response,
    ) -> tuple[str, dict[str, int] | None, list[dict] | None]:
        """Validation contract of plan section 9.2 — never assume shapes."""
        try:
            data = response.json()
        except json.JSONDecodeError:
            raise RuntimeError("LLM returned malformed JSON") from None

        if not isinstance(data, dict):
            raise RuntimeError("LLM returned a non-object response")

        choices = data.get("choices")
        if not choices:
            error_obj = data.get("error") or {}
            if isinstance(error_obj, dict):
                error_message = error_obj.get("message", "Unknown provider error")
            else:
                error_message = str(error_obj)
            raise RuntimeError(f"LLM provider error: {error_message}")

        if not isinstance(choices, list):
            raise RuntimeError("LLM provider error: choices is not a list")

        first = choices[0]
        if not isinstance(first, dict):
            raise RuntimeError("LLM provider error: malformed choice entry")
        message = first.get("message")
        if not isinstance(message, dict):
            raise RuntimeError("LLM provider error: missing message object")

        raw_calls = message.get("tool_calls")
        tool_calls: list[dict] | None = None
        if isinstance(raw_calls, list) and raw_calls:
            tool_calls = []
            for call in raw_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if not isinstance(function, dict):
                    continue
                tool_calls.append(
                    {
                        "id": str(call.get("id") or f"call_{len(tool_calls)}"),
                        "type": "function",
                        "name": str(function.get("name") or ""),
                        "arguments": str(function.get("arguments") or "{}"),
                    }
                )
            if not tool_calls:
                tool_calls = None

        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            # Tool-call turns may legitimately carry no prose.
            if tool_calls:
                content = ""
            else:
                raise RuntimeError("LLM provider error: empty content")

        usage: dict[str, int] | None = None
        raw_usage = data.get("usage")
        if isinstance(raw_usage, dict):
            usage = {}
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = raw_usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    usage[key] = value

        return content, usage, tool_calls
