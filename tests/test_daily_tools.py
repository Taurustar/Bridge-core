"""Private daily tools tests (plan section 24)."""

from __future__ import annotations

import asyncio
import json
import unittest

from core.cache import RedisCache
from core.bridge import Bridge
from core.daily_tools import (
    DailyToolExecutor,
    IdempotencyStore,
    ReminderStore,
    ToolContext,
    convert_units,
    daily_tool_schemas,
    mutation_intent_present,
    safe_arithmetic,
    sanitize_daily_reply,
)
from core.memory import MemoryBackend
from core.llm import LLMChainExhausted
from core.user_schedule import UserSchedule

from fakes import FakeLLM, FakeRedis, make_cache, make_config


def make_executor(**overrides):
    cache, fake = make_cache()
    config = make_config(**overrides)
    return (
        DailyToolExecutor(config),
        ReminderStore(cache),
        IdempotencyStore(cache),
        fake,
    )


def make_context(
    reminders: ReminderStore,
    idempotency: IdempotencyStore,
    *,
    user_text: str = "",
    turn_id: str = "turn_1",
    tool_call_id: str = "call_1",
    user_schedule=None,
) -> ToolContext:
    return ToolContext(
        owner="owner",
        user_text=user_text,
        turn_id=turn_id,
        tool_call_id=tool_call_id,
        reminders=reminders,
        idempotency=idempotency,
        user_schedule=user_schedule,
        character_timezone="UTC",
    )


class DeterministicToolsTest(unittest.TestCase):
    def test_safe_arithmetic(self):
        self.assertEqual(safe_arithmetic("2*(3+4)/5"), 2.8)
        self.assertEqual(safe_arithmetic("2**3"), 8.0)
        with self.assertRaises(ValueError):
            safe_arithmetic("1/0")
        with self.assertRaises((ValueError, SyntaxError)):
            safe_arithmetic("__import__('os')")

    def test_convert_units(self):
        self.assertEqual(convert_units(1, "kg", "lb")["unit"], "lb")
        self.assertAlmostEqual(convert_units(0, "c", "f")["value"], 32.0, places=2)
        self.assertFalse(convert_units(1, "m", "f")["ok"])

    def test_sanitize_removes_tool_narration(self):
        text = "I used a Tavily search and the tool call returned an API answer."
        cleaned = sanitize_daily_reply(text)
        lowered = cleaned.lower()
        for banned in ("tavily", "tool", "api", "schema"):
            self.assertNotIn(banned, lowered)
        self.assertTrue(cleaned.strip())

    def test_intent_detection(self):
        self.assertTrue(mutation_intent_present("remind me to stretch", ("remind",)))
        self.assertFalse(mutation_intent_present("hello there", ("remind",)))

    def test_intent_rejects_negation_and_read_only_questions(self):
        self.assertFalse(mutation_intent_present("do not remind me", ("remind", "reminder")))
        self.assertFalse(mutation_intent_present("am I busy?", ("busy", "schedule")))
        self.assertFalse(mutation_intent_present("explain my schedule", ("schedule",)))
        self.assertFalse(mutation_intent_present("do not schedule Monday as busy", ("schedule", "busy")))

    def test_intent_allows_explicit_mutations(self):
        words = ("remind", "reminder")
        self.assertTrue(mutation_intent_present("create a reminder for lunch", words))
        self.assertTrue(mutation_intent_present("update my reminder", words))
        self.assertTrue(mutation_intent_present("delete that reminder", words))

    def test_intent_allows_natural_schedule_and_contrast_commands(self):
        words = ("schedule", "calendar", "availability", "available", "busy", "free")
        self.assertTrue(mutation_intent_present("Schedule Monday as busy", words))
        self.assertTrue(mutation_intent_present("I am not busy; set Monday free", words))

    def test_intent_allows_do_not_forget_reminder_command(self):
        words = ("remind", "reminder", "remember")
        self.assertTrue(mutation_intent_present("don't forget to remind me", words))


class ReminderStoreTest(unittest.TestCase):
    def test_create_list_delete(self):
        cache, fake = make_cache()
        store = ReminderStore(cache)

        async def run():
            created = await store.create(
                "owner", text="stretch", due_ts=100.0, timezone_name="UTC"
            )
            self.assertEqual(len(await store.list("owner")), 1)
            self.assertTrue(await store.delete("owner", created["id"]))
            self.assertFalse(await store.delete("owner", created["id"]))
            self.assertEqual(await store.list("owner"), [])

        asyncio.run(run())


class ExecutorTest(unittest.TestCase):
    def test_unknown_tool_is_structured_error(self):
        executor, reminders, idempotency, _ = make_executor()
        context = make_context(reminders, idempotency)

        async def run():
            return await executor.execute("nope", {}, context)

        result = asyncio.run(run())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "unknown_tool")

    def test_invalid_expression_is_structured_error(self):
        executor, reminders, idempotency, _ = make_executor()
        context = make_context(reminders, idempotency)

        async def run():
            return await executor.execute("calculate", {"expression": "1//"}, context)

        result = asyncio.run(run())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_expression")

    def test_get_now_rejects_unknown_timezone(self):
        executor, reminders, idempotency, _ = make_executor()
        context = make_context(reminders, idempotency)

        async def run():
            return await executor.execute(
                "get_now", {"timezone": "Mars/Olympus"}, context
            )

        result = asyncio.run(run())
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("needs_clarification"))


class MutationGatesTest(unittest.TestCase):
    def test_reminder_create_requires_explicit_intent(self):
        executor, reminders, idempotency, fake = make_executor()
        context = make_context(reminders, idempotency, user_text="hello there")

        async def run():
            return await executor.execute(
                "reminder_create",
                {"text": "stretch", "due_ts": 100.0, "timezone": "UTC"},
                context,
            )

        result = asyncio.run(run())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "explicit_intent_required")
        self.assertNotIn("core:daily:reminders:owner", fake.store)

    def test_reminder_negation_is_not_mutation_intent(self):
        executor, reminders, idempotency, fake = make_executor()
        context = make_context(reminders, idempotency, user_text="do not remind me")
        result = asyncio.run(executor.execute(
            "reminder_create",
            {"text": "stretch", "due_ts": 100.0, "timezone": "UTC"},
            context,
        ))
        self.assertEqual(result["error"], "explicit_intent_required")
        self.assertNotIn("core:daily:reminders:owner", fake.store)

    def test_reminder_create_is_idempotent_per_turn_and_call(self):
        executor, reminders, idempotency, fake = make_executor()
        context = make_context(reminders, idempotency, user_text="please remind me")

        async def run():
            first = await executor.execute(
                "reminder_create",
                {"text": "stretch", "due_ts": 100.0, "timezone": "UTC"},
                context,
            )
            duplicate = await executor.execute(
                "reminder_create",
                {"text": "stretch", "due_ts": 100.0, "timezone": "UTC"},
                context,
            )
            return first, duplicate

        first, duplicate = asyncio.run(run())
        self.assertTrue(first["ok"])
        self.assertTrue(duplicate.get("duplicate"))
        self.assertEqual(len(asyncio.run(reminders.list("owner"))), 1)

    def test_failed_validation_does_not_consume_idempotency_key(self):
        executor, reminders, idempotency, _ = make_executor()
        context = make_context(reminders, idempotency, user_text="please remind me")

        async def run():
            failed = await executor.execute(
                "reminder_create", {"text": "stretch", "due_ts": 100.0}, context
            )
            retried = await executor.execute(
                "reminder_create",
                {"text": "stretch", "due_ts": 100.0, "timezone": "UTC"},
                context,
            )
            return failed, retried, await reminders.list("owner")

        failed, retried, rows = asyncio.run(run())
        self.assertFalse(failed["ok"])
        self.assertTrue(retried["ok"])
        self.assertEqual(len(rows), 1)

    def test_failed_write_does_not_consume_idempotency_key(self):
        executor, reminders, idempotency, fake = make_executor()
        context = make_context(reminders, idempotency, user_text="please remind me")
        original = reminders.cache.atomic_replace_list_once
        attempts = 0

        async def fail_once(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("write failed")
            return await original(*args, **kwargs)

        reminders.cache.atomic_replace_list_once = fail_once

        async def run():
            args = {"text": "stretch", "due_ts": 100.0, "timezone": "UTC"}
            failed = await executor.execute("reminder_create", args, context)
            self.assertNotIn("core:daily:idempotency:owner", fake.store)
            retried = await executor.execute("reminder_create", args, context)
            return failed, retried, await reminders.list("owner")

        failed, retried, rows = asyncio.run(run())
        self.assertEqual(failed["error"], "tool_failed")
        self.assertTrue(retried["ok"])
        self.assertEqual(len(rows), 1)

    def test_concurrent_duplicate_mutates_once(self):
        executor, reminders, idempotency, _ = make_executor()
        context = make_context(reminders, idempotency, user_text="create a reminder")

        async def run():
            args = {"text": "stretch", "due_ts": 100.0, "timezone": "UTC"}
            results = await asyncio.gather(
                executor.execute("reminder_create", args, context),
                executor.execute("reminder_create", args, context),
            )
            return results, await reminders.list("owner")

        results, rows = asyncio.run(run())
        self.assertEqual(sum(bool(result.get("duplicate")) for result in results), 1)
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(len(rows), 1)

    def test_reminder_update_validates_applies_and_is_idempotent(self):
        executor, reminders, idempotency, _ = make_executor()

        async def run():
            created = await reminders.create(
                "owner", text="stretch", due_ts=100.0, timezone_name="UTC"
            )
            context = make_context(
                reminders, idempotency, user_text="update my reminder"
            )
            invalid = await executor.execute(
                "reminder_update", {"id": created["id"], "timezone": "Mars/Olympus"}, context
            )
            first = await executor.execute(
                "reminder_update",
                {"id": created["id"], "text": "walk", "due_ts": 200.0},
                context,
            )
            duplicate = await executor.execute(
                "reminder_update",
                {"id": created["id"], "text": "ignored on replay"},
                context,
            )
            return invalid, first, duplicate, (await reminders.list("owner"))[0]

        invalid, first, duplicate, row = asyncio.run(run())
        self.assertEqual(invalid["error"], "unknown_timezone")
        self.assertTrue(first["ok"])
        self.assertTrue(duplicate.get("duplicate"))
        self.assertEqual(row["text"], "walk")
        self.assertEqual(row["due_ts"], 200.0)
        self.assertEqual(set(row), {"id", "text", "due_ts", "timezone", "created_ts"})

    def test_concurrent_reminder_update_applies_once(self):
        executor, reminders, idempotency, _ = make_executor()

        async def run():
            created = await reminders.create(
                "owner", text="stretch", due_ts=100.0, timezone_name="UTC"
            )
            context = make_context(reminders, idempotency, user_text="update my reminder")
            args = {"id": created["id"], "text": "walk"}
            results = await asyncio.gather(
                executor.execute("reminder_update", args, context),
                executor.execute("reminder_update", args, context),
            )
            return results, (await reminders.list("owner"))[0]

        results, row = asyncio.run(run())
        self.assertEqual(sum(bool(result.get("duplicate")) for result in results), 1)
        self.assertEqual(row["text"], "walk")

    def test_schedule_update_without_intent_refused(self):
        executor, reminders, idempotency, fake = make_executor()
        context = make_context(reminders, idempotency, user_text="do the thing")

        async def run():
            return await executor.execute(
                "owner_schedule_update",
                {"day": "mon", "blocks": [{"start": "09:00", "end": "17:00", "state": "busy"}]},
                context,
            )

        result = asyncio.run(run())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "explicit_intent_required")
        self.assertNotIn("core:user_schedule:owner", fake.store)

    def test_schedule_read_only_question_is_refused(self):
        cache, fake = make_cache()
        config = make_config(USER_SCHEDULE_ENABLED=True)
        reminders = ReminderStore(cache)
        idempotency = IdempotencyStore(cache)
        context = make_context(
            reminders, idempotency, user_text="am I busy?",
            user_schedule=UserSchedule(config, cache),
        )
        result = asyncio.run(DailyToolExecutor(config).execute(
            "owner_schedule_update", {"day": "mon", "blocks": []}, context
        ))
        self.assertEqual(result["error"], "explicit_intent_required")
        self.assertNotIn("core:user_schedule:owner", fake.strings)


class SchemaOfferTest(unittest.TestCase):
    def test_schemas_gate_on_flags(self):
        base = daily_tool_schemas(
            web_enabled=False, schedule_available=False, user_schedule_available=False
        )
        names = {schema["function"]["name"] for schema in base}
        self.assertNotIn("web_search", names)
        self.assertNotIn("character_schedule_read", names)
        self.assertNotIn("owner_schedule_update", names)
        self.assertIn("reminder_update", names)

        everything = daily_tool_schemas(
            web_enabled=True, schedule_available=True, user_schedule_available=True
        )
        names = {schema["function"]["name"] for schema in everything}
        self.assertIn("web_search", names)
        self.assertIn("web_open", names)
        self.assertIn("character_schedule_read", names)
        self.assertIn("owner_schedule_update", names)


class CompanionSanitizerFallbackTest(unittest.TestCase):
    @staticmethod
    def make_bridge(retry):
        replies = [
            {
                "text": "",
                "tool_calls": [{"id": "call_1", "name": "calculate", "arguments": '{"expression":"1+1"}'}],
            },
            "tool",
            retry,
        ]
        cache, _ = make_cache()
        return Bridge(make_config(DAILY_TOOLS_ENABLED=True), cache, llm=FakeLLM(replies))

    def test_empty_sanitized_retry_uses_safe_fallback(self):
        bridge = self.make_bridge("API")
        result = asyncio.run(bridge._companion_tool_loop([{"role": "user", "content": "calculate"}], "calculate"))
        self.assertEqual(result.text, "[EMOTION: neutral]\nI cannot answer that safely right now.")
        self.assertNotIn("API", result.text)

    def test_failed_sanitizer_retry_uses_safe_fallback(self):
        bridge = self.make_bridge(LLMChainExhausted("failed"))
        result = asyncio.run(bridge._companion_tool_loop([{"role": "user", "content": "calculate"}], "calculate"))
        self.assertEqual(result.text, "[EMOTION: neutral]\nI cannot answer that safely right now.")
        self.assertNotIn("tool", result.text.lower())

    def test_first_response_without_tool_call_is_sanitized(self):
        llm = FakeLLM(["I used the API. Monday is free."])
        cache, _ = make_cache()
        bridge = Bridge(make_config(DAILY_TOOLS_ENABLED=True), cache, llm=llm)
        result = asyncio.run(bridge._companion_tool_loop(
            [{"role": "user", "content": "Is Monday free?"}], "Is Monday free?"
        ))
        self.assertNotIn("api", result.text.lower())
        self.assertIn("Monday is free", result.text)
        self.assertEqual(len(llm.calls), 1)

    def test_first_response_without_tool_call_retries_once_then_falls_back(self):
        llm = FakeLLM(["API", "tool call"])
        cache, _ = make_cache()
        bridge = Bridge(make_config(DAILY_TOOLS_ENABLED=True), cache, llm=llm)
        result = asyncio.run(bridge._companion_tool_loop(
            [{"role": "user", "content": "hello"}], "hello"
        ))
        self.assertEqual(result.text, "[EMOTION: neutral]\nI cannot answer that safely right now.")
        self.assertNotIn("api", result.text.lower())
        self.assertNotIn("tool", result.text.lower())
        self.assertEqual(len(llm.calls), 2)


class MemorySearchToolTest(unittest.TestCase):
    def test_memory_search_uses_backend_ranking(self):
        cache, fake = make_cache()
        config = make_config()
        backend = MemoryBackend(config, cache)
        executor, reminders, idempotency, _ = make_executor()
        context = make_context(reminders, idempotency)
        context.longterm = backend

        async def run():
            await backend.add(
                "owner",
                backend.make_record(
                    kind="commitment",
                    text="The owner will water the plants on Friday.",
                    source="extraction",
                    source_mode="companion",
                ),
            )
            return await executor.execute(
                "memory_search", {"query": "plants friday"}, context
            )

        result = asyncio.run(run())
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["memories"]), 1)
        self.assertIn("plants", result["memories"][0]["text"])


if __name__ == "__main__":
    unittest.main()
