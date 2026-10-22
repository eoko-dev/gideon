import asyncio
import logging
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from tests.dashboard_support import (
    DashboardTestCase, FakeClock, make_guild, make_text_channel,
)

SQLITE_MAGIC = b"SQLite format 3\x00"


class ApiTestCase(DashboardTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.login()

    def add_message(self, content="hello", user_name="Alice", channel_id="100", **kwargs):
        self.db.messages.add_message(
            role=kwargs.pop("role", "user"), content=content, timestamp=datetime.now(),
            channel_id=channel_id, user_id="u1", user_name=user_name, **kwargs)


class SettingsTests(ApiTestCase):
    async def test_valid_update_applies_every_field(self):
        resp = await self.client.put("/api/settings", json={
            "max_channel_history": 20,
            "time_window_hours": 12,
            "memory_summary_enabled": False,
            "tool_calling_max_iterations": 3,
            "global_model": "openrouter/good/model",
            "global_provider": "openrouter",
            "global_system_prompt": "be brief",
        })
        self.assertEqual(resp.status, 200, await resp.text())
        body = await resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["updated"][0], "global_model")
        self.assertEqual(self.state.get_max_channel_history(), 20)
        self.assertEqual(self.state.get_time_window_hours(), 12)
        self.assertFalse(self.state.get_memory_summary_enabled())
        self.assertEqual(self.state.get_global_model(), "openrouter/good/model")
        self.assertEqual(self.state.get_global_system_prompt(), "be brief")

    async def test_unchanged_unverifiable_model_does_not_block_save(self):
        self.state.global_model = "google/gemini-2.0-flash-exp"
        current = self.state.get_global_model()
        resp = await self.client.put("/api/settings", json={
            "global_model": current, "max_channel_history": 20})
        self.assertEqual(resp.status, 200, await resp.text())
        self.assertEqual(self.state.get_max_channel_history(), 20)
        self.assertEqual(self.state.get_global_model(), current)

    async def test_one_bad_field_applies_nothing(self):
        before = (self.state.get_max_channel_history(), self.state.get_global_model(),
                  self.state.get_prune_frequency_hours())
        resp = await self.client.put("/api/settings", json={
            "max_channel_history": 20,
            "global_model": "openrouter/good/model",
            "prune_frequency_hours": 0,
        })
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertEqual(body["error"], "Validation failed")
        self.assertEqual(list(body["details"]), ["prune_frequency_hours"])
        after = (self.state.get_max_channel_history(), self.state.get_global_model(),
                 self.state.get_prune_frequency_hours())
        self.assertEqual(before, after)

    async def test_all_errors_are_collected_per_field(self):
        resp = await self.client.put("/api/settings", json={
            "max_channel_history": 101,
            "time_window_hours": "abc",
            "session_timeout_hours": 169,
            "max_memory_summaries": 51,
            "tool_calling_max_iterations": 11,
            "global_provider": "nonexistent",
            "global_model": "openrouter/not-a-model",
        })
        self.assertEqual(resp.status, 400)
        details = (await resp.json())["details"]
        self.assertEqual(set(details), {
            "max_channel_history", "time_window_hours", "session_timeout_hours",
            "max_memory_summaries", "tool_calling_max_iterations", "global_provider", "global_model"})

    async def test_booleans_must_be_json_booleans(self):
        before = self.state.get_tool_calling_enabled()
        for value in ("true", 1, "false", None):
            resp = await self.client.put("/api/settings", json={"tool_calling_enabled": value})
            self.assertEqual(resp.status, 400, value)
            self.assertIn("tool_calling_enabled", (await resp.json())["details"])
        # Rejected values leave the setting as it was
        self.assertEqual(self.state.get_tool_calling_enabled(), before)

    async def test_booleans_are_not_accepted_as_integers(self):
        resp = await self.client.put("/api/settings", json={"max_channel_history": True})
        self.assertEqual(resp.status, 400)

    async def test_rejects_non_object_body(self):
        resp = await self.client.put("/api/settings", json=[1, 2])
        self.assertEqual(resp.status, 400)


class ChannelAndThreadTests(ApiTestCase):
    async def test_channel_update_is_validated_before_applying(self):
        resp = await self.client.put("/api/channels/100", json={
            "system_prompt": "channel prompt",
            "model": "openrouter/not-a-model",
        })
        self.assertEqual(resp.status, 400)
        self.assertIn("model", (await resp.json())["details"])
        self.assertIsNone(self.state.get_channel_system_prompt("100"))

        resp = await self.client.put("/api/channels/100", json={
            "session_timeout_hours": 0, "provider": "nope"})
        self.assertEqual(resp.status, 400)
        self.assertEqual(set((await resp.json())["details"]), {"session_timeout_hours", "provider"})

    async def test_channel_update_applies_all_fields(self):
        resp = await self.client.put("/api/channels/100", json={
            "model": "openrouter/good/model",
            "provider": "openrouter",
            "system_prompt": "channel prompt",
            "session_timeout_hours": 6,
            "memory_summary_enabled": False,
            "max_memory_summaries": 4,
        })
        self.assertEqual(resp.status, 200, await resp.text())
        self.assertEqual(self.state.get_channel_model("100"), "openrouter/good/model")
        self.assertEqual(self.state.get_channel_system_prompt("100"), "channel prompt")
        self.assertEqual(self.state.get_effective_session_timeout("100"), 6)
        self.assertEqual(self.state.get_effective_max_memory_summaries("100"), 4)

    async def test_channel_detail_reports_raw_provider_override(self):
        body = await (await self.client.get("/api/channels/100")).json()
        self.assertIsNone(body["provider"])
        self.assertEqual(body["effective_provider"], self.state.get_global_provider())
        await self.client.put("/api/channels/100", json={"provider": "openrouter"})
        body = await (await self.client.get("/api/channels/100")).json()
        self.assertEqual(body["provider"], "openrouter")

    async def test_channel_memory_override_cleared_by_null(self):
        await self.client.put("/api/channels/100", json={
            "session_timeout_hours": 6, "max_memory_summaries": 4})
        resp = await self.client.put("/api/channels/100", json={"session_timeout_hours": None})
        self.assertEqual(resp.status, 200, await resp.text())
        self.assertEqual(self.state.get_effective_session_timeout("100"),
                         self.state.get_session_timeout_hours())
        self.assertEqual(self.state.get_effective_max_memory_summaries("100"), 4)

    async def test_thread_update_validation(self):
        self.db.add_thread("t1", "100", "original", datetime.now())
        for name in ("", "   ", "x" * 101, 5):
            resp = await self.client.put("/api/threads/t1", json={"name": name, "system_prompt": "p"})
            self.assertEqual(resp.status, 400, repr(name))
            self.assertIn("name", (await resp.json())["details"])
        self.assertEqual(self.state.get_discord_thread("t1")["name"], "original")
        self.assertIsNone(self.state.get_discord_thread_config("t1")["system_prompt"])

        resp = await self.client.put("/api/threads/t1", json={"name": " renamed ", "system_prompt": "p"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.state.get_discord_thread("t1")["name"], "renamed")

        resp = await self.client.put("/api/threads/missing", json={"name": "x"})
        self.assertEqual(resp.status, 404)

    async def test_threads_list_has_counts_and_channel_names(self):
        guild = make_guild(1, "Guild")
        self.bot.channels[100] = make_text_channel(100, "general", guild)
        self.db.add_thread("t1", "100", "first", datetime(2025, 1, 1))
        self.db.add_thread("t2", "100", "second", datetime(2025, 1, 2))
        for _ in range(3):
            self.add_message(channel_id=None, thread_id="t1")
        resp = await self.client.get("/api/threads")
        rows = {r["thread_id"]: r for r in await resp.json()}
        self.assertEqual(rows["t1"]["message_count"], 3)
        self.assertEqual(rows["t2"]["message_count"], 0)
        self.assertEqual(rows["t1"]["channel_name"], "general")
        self.assertEqual(rows["t1"]["guild_name"], "Guild")

    async def test_channels_all_counts_memory_overrides(self):
        guild = make_guild(1, "Guild")
        general = make_text_channel(100, "general", guild)
        other = make_text_channel(200, "other", guild)
        plain = make_text_channel(300, "plain", guild)
        guild.channels = [plain, other, general]
        self.bot.guilds = [guild]
        self.db.set_channel_model("100", "openrouter/good/model")
        self.state.set_channel_memory_config("200", session_timeout_hours=12, memory_summary_enabled=False)

        resp = await self.client.get("/api/channels/all")
        rows = {r["channel_id"]: r for r in await resp.json()}
        self.assertEqual(rows["100"]["overrides"], ["model"])
        self.assertTrue(rows["100"]["has_override"])
        self.assertEqual(rows["200"]["overrides"], ["session_timeout_hours", "memory_summary_enabled"])
        self.assertTrue(rows["200"]["has_override"])
        self.assertEqual(rows["300"]["overrides"], [])
        self.assertFalse(rows["300"]["has_override"])

    async def test_channel_detail_persona_and_sources(self):
        resp = await self.client.get("/api/channels/100")
        body = await resp.json()
        self.assertIsNone(body["persona"])
        self.assertEqual(body["sources"], {
            "model": "global", "system_prompt": "global", "session_timeout_hours": "global",
            "memory_summary_enabled": "global", "max_memory_summaries": "global",
            "user_memory_enabled": "global"})

        self.db.set_channel_model("100", "openrouter/good/model")
        self.db.set_channel_system_prompt("100", "channel prompt")
        self.state.set_channel_memory_config("100", max_memory_summaries=7)
        body = await (await self.client.get("/api/channels/100")).json()
        self.assertEqual(body["sources"]["model"], "channel")
        self.assertEqual(body["sources"]["system_prompt"], "channel")
        self.assertEqual(body["sources"]["max_memory_summaries"], "channel")
        self.assertEqual(body["sources"]["session_timeout_hours"], "global")
        self.assertEqual(body["effective_max_memory_summaries"], 7)

        self.state.set_channel_persona("100", "Bob", model="openrouter/other/model", template_id="tpl")
        body = await (await self.client.get("/api/channels/100")).json()
        self.assertEqual(body["persona"], {"display_name": "Bob", "is_active": True, "template_id": "tpl"})
        self.assertEqual(body["sources"]["model"], "persona")
        self.assertEqual(body["sources"]["system_prompt"], "channel")
        self.assertEqual(body["effective_model"], "openrouter/other/model")

        self.state.toggle_channel_persona("100")
        body = await (await self.client.get("/api/channels/100")).json()
        self.assertFalse(body["persona"]["is_active"])
        self.assertEqual(body["sources"]["model"], "channel")

    async def test_persona_responses_hide_webhook_credentials(self):
        self.state.set_channel_persona("100", "Bob")
        self.db.personas._conn.execute(
            "UPDATE CHANNEL_PERSONAS SET webhook_id='w', webhook_token='tok' WHERE channel_id='100'")
        for path in ("/api/personas/channels", "/api/personas/channels/100"):
            text = await (await self.client.get(path)).text()
            self.assertNotIn("tok", text)
            self.assertNotIn("webhook", text)


class QueryParamTests(ApiTestCase):
    async def test_bad_query_params_return_400(self):
        self.enable_key_service()
        for path in (
            "/api/messages?page=0", "/api/messages?page=abc", "/api/messages?page=-1",
            "/api/messages?per_page=0", "/api/messages?per_page=101", "/api/messages?per_page=1.5",
            "/api/memory/100?limit=0", "/api/memory/100?limit=101", "/api/memory/100?limit=x",
            "/api/keys/audit?limit=0", "/api/keys/audit?limit=201", "/api/keys/audit?limit=x",
            "/api/logs/errors?limit=0", "/api/logs/errors?limit=101", "/api/logs/errors?limit=x",
        ):
            resp = await self.client.get(path)
            self.assertEqual(resp.status, 400, path)
            self.assertIn("must be", (await resp.json())["error"], path)

    async def test_good_query_params_are_accepted(self):
        self.enable_key_service()
        for path in ("/api/messages?page=1&per_page=100", "/api/memory/100?limit=100",
                     "/api/keys/audit?limit=200", "/api/logs/errors?limit=100", "/api/messages"):
            resp = await self.client.get(path)
            self.assertEqual(resp.status, 200, path)

    async def test_message_pagination(self):
        for i in range(5):
            self.add_message(content=f"m{i}")
        body = await (await self.client.get("/api/messages?per_page=2&page=3")).json()
        self.assertEqual((body["total"], body["total_pages"], len(body["messages"])), (5, 3, 1))


class MessageTests(ApiTestCase):
    async def test_messages_include_user_name(self):
        guild = make_guild(1, "Guild")
        self.bot.channels[100] = make_text_channel(100, "general", guild)
        self.add_message(content="hi", user_name="Alice")
        self.add_message(content="anon", user_name=None)
        body = await (await self.client.get("/api/messages")).json()
        by_content = {m["content"]: m for m in body["messages"]}
        self.assertEqual(by_content["hi"]["user_name"], "Alice")
        self.assertIsNone(by_content["anon"]["user_name"])
        self.assertEqual(by_content["hi"]["channel_name"], "general")

    async def test_message_sources(self):
        self.db.add_thread("t1", "100", "thread one", datetime.now())
        self.add_message()
        self.add_message(channel_id=None, thread_id="t1")
        body = await (await self.client.get("/api/messages/sources")).json()
        self.assertEqual(body["channels"][0]["id"], "100")
        self.assertEqual(body["threads"][0]["name"], "thread one")


class ConcurrencyTests(ApiTestCase):
    async def test_concurrent_requests_share_the_connection_safely(self):
        for i in range(30):
            self.add_message(content=f"m{i}")
        requests = []
        for i in range(20):
            requests.append(self.client.get("/api/messages?per_page=10"))
            requests.append(self.client.get("/api/threads"))
            requests.append(self.client.get("/api/overview"))
            requests.append(self.client.put("/api/settings", json={"max_channel_history": 10 + i}))
        responses = await asyncio.gather(*requests)
        self.assertEqual({r.status for r in responses}, {200})


class BackupTests(ApiTestCase):
    async def test_database_export_streams_and_leaves_no_temp_file(self):
        self.add_message()
        resp = await self.client.get("/api/backup/database")
        self.assertEqual(resp.status, 200)
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        data = await resp.read()
        self.assertTrue(data.startswith(SQLITE_MAGIC))
        self.assertEqual(int(resp.headers["Content-Length"]), len(data))
        self.assertEqual(self.leftover_temp_files(), [])

    async def test_database_export_failure_leaves_no_temp_file(self):
        with mock.patch.object(self.db, "create_sqlite_backup", side_effect=RuntimeError("disk on fire")):
            resp = await self.client.get("/api/backup/database")
        self.assertEqual(resp.status, 500)
        self.assertEqual(await resp.json(), {"error": "Internal server error"})
        self.assertEqual(self.leftover_temp_files(), [])

    async def test_oversized_db_upload_is_rejected_with_413(self):
        self.server.MAX_DB_UPLOAD_BYTES = 1024
        for path in ("/api/backup/database/validate", "/api/backup/database/restore"):
            resp = await self.client.post(path, data=self.upload(SQLITE_MAGIC + b"x" * 5000))
            self.assertEqual(resp.status, 413, path)
            self.assertEqual(self.leftover_temp_files(), [], path)

    async def test_oversized_config_upload_is_rejected_with_413(self):
        self.server.MAX_CONFIG_UPLOAD_BYTES = 100
        for path in ("/api/backup/config/validate", "/api/backup/config/import"):
            resp = await self.client.post(path, data=self.upload(b"{" + b" " * 500 + b"}"))
            self.assertEqual(resp.status, 413, path)
        self.assertEqual(self.leftover_temp_files(), [])

    async def test_upload_requires_a_file(self):
        resp = await self.client.post("/api/backup/database/validate", json={"a": 1})
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.leftover_temp_files(), [])

    async def test_db_validate_and_restore_clean_up_temp_files(self):
        backup = await (await self.client.get("/api/backup/database")).read()

        resp = await self.client.post("/api/backup/database/validate", data=self.upload(backup))
        self.assertEqual(resp.status, 200)
        self.assertTrue((await resp.json())["valid"])
        self.assertEqual(self.leftover_temp_files(), [])

        resp = await self.client.post("/api/backup/database/restore", data=self.upload(backup))
        body = await resp.json()
        self.assertEqual(resp.status, 200, body)
        self.assertEqual(body["status"], "ok")
        self.assertNotIn("bak_path", body)
        self.assertNotIn(".bak", str(body))
        self.assertEqual(self.leftover_temp_files(), [])

    async def test_invalid_restore_is_rejected_and_cleaned_up(self):
        resp = await self.client.post("/api/backup/database/restore", data=self.upload(b"not a database"))
        self.assertEqual(resp.status, 400)
        self.assertEqual((await resp.json())["error"], "Validation failed")
        self.assertEqual(self.leftover_temp_files(), [])

    async def test_config_export_validate_import_round_trip(self):
        config = await (await self.client.get("/api/backup/config")).read()
        resp = await self.client.post("/api/backup/config/validate", data=self.upload(config))
        self.assertTrue((await resp.json())["valid"])
        resp = await self.client.post("/api/backup/config/import", data=self.upload(config))
        self.assertEqual(resp.status, 200, await resp.text())
        resp = await self.client.post("/api/backup/config/import", data=self.upload(b"nope"))
        self.assertEqual(resp.status, 400)
        resp = await self.client.post("/api/backup/config/import", data=self.upload(b'{"a": 1}'))
        self.assertEqual(resp.status, 400)
        self.assertEqual((await resp.json())["error"], "Validation failed")


class KeyTests(ApiTestCase):
    async def test_key_test_validates_without_persisting(self):
        service = self.enable_key_service()
        with mock.patch.object(service, "validate_key", mock.AsyncMock(
                return_value={"valid": True, "message": "Key is valid", "details": {"secret": "x"}})) as validate:
            resp = await self.client.post("/api/keys/test", json={"provider": "openai", "key": " sk-test "})
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), {"valid": True, "message": "Key is valid"})
        validate.assert_awaited_once_with("openai", "sk-test")

        self.assertEqual(await (await self.client.get("/api/keys")).json(), [])
        self.assertEqual(self.db.get_all_api_keys(), [])
        self.assertEqual(self.db.get_api_key_audit_log(None, 10), [])

    async def test_key_test_reports_invalid_keys(self):
        service = self.enable_key_service()
        with mock.patch.object(service, "validate_key", mock.AsyncMock(
                return_value={"valid": False, "message": "Invalid API key"})):
            resp = await self.client.post("/api/keys/test", json={"provider": "openai", "key": "bad"})
        self.assertEqual(await resp.json(), {"valid": False, "message": "Invalid API key"})

    async def test_key_test_input_validation(self):
        self.enable_key_service()
        for body in ({"provider": "nope", "key": "k"}, {"provider": "openai"}, {"key": "k"},
                     {"provider": "openai", "key": 5}, {}):
            resp = await self.client.post("/api/keys/test", json=body)
            self.assertEqual(resp.status, 400, body)

    async def test_key_endpoints_report_501_without_service(self):
        resp = await self.client.post("/api/keys/test", json={"provider": "openai", "key": "k"})
        self.assertEqual(resp.status, 501)


class ImageVideoTests(ApiTestCase):
    async def test_image_provider_whitelist(self):
        for body in ({"active_provider": "evil"},
                     {"provider": "evil", "config": {"size": "1"}},
                     {"provider": "../x", "config": {}},
                     {"provider": "openai", "config": "not-an-object"},
                     {"openrouter_modalities": "image"}):
            resp = await self.client.put("/api/image/settings", json=body)
            self.assertEqual(resp.status, 400, body)
        self.assertIsNone(self.db.get_global_config("image_config_evil"))
        self.assertIsNone(self.db.get_global_config("image_active_provider"))

    async def test_image_settings_valid_update(self):
        resp = await self.client.put("/api/image/settings", json={
            "active_provider": "openai", "provider": "openai", "config": {"size": "512x512"}})
        self.assertEqual(resp.status, 200)
        body = await (await self.client.get("/api/image/settings")).json()
        self.assertEqual(body["active_provider"], "openai")
        self.assertEqual(body["configs"]["openai"]["size"], "512x512")
        self.assertEqual(body["configs"]["openai"]["model"] if "model" in body["configs"]["openai"] else "dall-e-3",
                         "dall-e-3")

    async def test_video_config_provider_must_be_openrouter(self):
        for body in ({"config": {"a": 1}, "provider": "other"},
                     {"active_provider": "other"}):
            resp = await self.client.put("/api/video/settings", json=body)
            self.assertEqual(resp.status, 400, body)
        self.assertIsNone(self.db.get_global_config("video_config_other"))
        resp = await self.client.put("/api/video/settings", json={"config": {"duration": 4}, "provider": "openrouter"})
        self.assertEqual(resp.status, 200)

    async def test_video_job_id_validation(self):
        client = SimpleNamespace(get_video_status=mock.AsyncMock(
            return_value={"success": True, "status": "completed", "unsigned_urls": ["u"]}))
        self.bot.cogs["VideoCommands"] = SimpleNamespace(video_client=client)

        for job_id in ("bad%20id", "a.b", "a%2Fb", "x" * 129, "id%0A", "id;rm"):
            resp = await self.client.get(f"/api/video/jobs/{job_id}")
            self.assertEqual(resp.status, 400, job_id)
        client.get_video_status.assert_not_called()

        resp = await self.client.get("/api/video/jobs/" + "Ab_-9" * 25)
        self.assertEqual(resp.status, 200)
        self.assertEqual((await resp.json())["status"], "completed")

    async def test_video_generate_requires_string_prompt(self):
        resp = await self.client.post("/api/video/generate", json={"prompt": 5})
        self.assertEqual(resp.status, 400)


class ErrorHandlingTests(ApiTestCase):
    async def test_500_is_generic_and_logged_with_traceback(self):
        with mock.patch.object(self.state, "get_message_count", side_effect=RuntimeError("secret-detail")):
            with self.assertLogs("src.utils.dashboard.server", level="ERROR") as logs:
                resp = await self.client.get("/api/overview")
        self.assertEqual(resp.status, 500)
        self.assertEqual(await resp.json(), {"error": "Internal server error"})
        self.assertNotIn("secret-detail", await resp.text())
        self.assertTrue(logs.records[0].exc_info)
        self.assertIn("secret-detail", logs.output[0])

    async def test_other_failing_endpoints_do_not_leak_details(self):
        with mock.patch.object(self.state, "prune_old_data", side_effect=RuntimeError("secret-detail")):
            resp = await self.client.post("/api/diagnostics/prune")
        self.assertEqual((resp.status, await resp.json()), (500, {"error": "Internal server error"}))

        with mock.patch.object(self.model_manager, "get_models", side_effect=RuntimeError("secret-detail")):
            resp = await self.client.get("/api/models/openrouter")
            self.assertEqual((resp.status, await resp.json()), (500, {"error": "Internal server error"}))
            body = await (await self.client.get("/api/diagnostics")).text()
            self.assertNotIn("secret-detail", body)

    async def test_validation_errors_stay_specific(self):
        resp = await self.client.get("/api/models/nonexistent")
        self.assertEqual(resp.status, 400)
        self.assertIn("nonexistent", (await resp.json())["error"])

    async def test_error_log_ring_buffer(self):
        log = logging.getLogger("tests.error_buffer")
        log.error("first line\nsecond line with traceback-ish details")
        log.warning("not recorded")
        log.error("x" * 900)
        body = await (await self.client.get("/api/logs/errors")).json()
        self.assertEqual(len(body), 2)
        self.assertEqual(body[0]["message"], "x" * 500)
        self.assertEqual(body[1]["message"], "first line")
        self.assertEqual(set(body[1]), {"time", "level", "logger", "message"})
        self.assertEqual((body[1]["level"], body[1]["logger"]), ("ERROR", "tests.error_buffer"))
        datetime.fromisoformat(body[1]["time"])

        body = await (await self.client.get("/api/logs/errors?limit=1")).json()
        self.assertEqual(len(body), 1)

    async def test_error_log_is_capped_and_detached_on_shutdown(self):
        log = logging.getLogger("tests.error_buffer")
        for i in range(150):
            log.error("e%d", i)
        body = await (await self.client.get("/api/logs/errors?limit=100")).json()
        self.assertEqual(len(body), 100)
        self.assertEqual(body[0]["message"], "e149")

        self.assertIn(self.server.error_log, logging.getLogger().handlers)
        await self.client.close()
        self.assertNotIn(self.server.error_log, logging.getLogger().handlers)


class LifecycleTests(DashboardTestCase):
    async def test_start_and_stop_manage_the_log_handler_and_clients(self):
        server = type(self.server)(self.bot, "secret", port=0)
        await server.start()
        try:
            self.assertIn(server.error_log, logging.getLogger().handlers)
            self.assertEqual(server.ws_client_count, 0)
        finally:
            await server.stop()
        self.assertNotIn(server.error_log, logging.getLogger().handlers)


class SearchTests(ApiTestCase):
    async def test_search_across_groups(self):
        guild = make_guild(1, "Guild")
        guild.channels = [make_text_channel(100, "general-chat", guild), make_text_channel(101, "random", guild)]
        self.bot.guilds = [guild]
        self.bot.channels[100] = guild.channels[0]
        self.db.add_thread("t1", "100", "General planning", datetime.now())
        self.db.add_thread("t2", "100", "Other", datetime.now())
        self.db.add_persona_template("tpl1", "generalist", "The Generalist")
        self.db.add_persona_template("tpl2", "pirate", "Pirate")
        self.state.set_channel_persona("100", "Gen Bot")

        body = await (await self.client.get("/api/search?q=GEN")).json()
        self.assertEqual(body["channels"], [
            {"channel_id": "100", "channel_name": "general-chat", "guild_name": "Guild"}])
        self.assertEqual(body["threads"], [
            {"thread_id": "t1", "name": "General planning", "channel_name": "general-chat"}])
        self.assertEqual(body["persona_templates"], [
            {"template_id": "tpl1", "name": "generalist", "display_name": "The Generalist"}])
        self.assertEqual(body["channel_personas"], [
            {"channel_id": "100", "channel_name": "general-chat", "display_name": "Gen Bot"}])

    async def test_short_or_missing_query_returns_empty_groups(self):
        empty = {"channels": [], "threads": [], "persona_templates": [], "channel_personas": []}
        for path in ("/api/search", "/api/search?q=", "/api/search?q=a", "/api/search?q=%20a%20"):
            self.assertEqual(await (await self.client.get(path)).json(), empty, path)

    async def test_results_are_capped_at_ten_per_group(self):
        guild = make_guild(1, "Guild")
        guild.channels = [make_text_channel(i, f"chan-{i}", guild) for i in range(15)]
        self.bot.guilds = [guild]
        for i in range(15):
            self.db.add_thread(f"t{i}", "1", f"chan thread {i}", datetime.now())
        body = await (await self.client.get("/api/search?q=chan")).json()
        self.assertEqual((len(body["channels"]), len(body["threads"])), (10, 10))

    async def test_like_wildcards_are_literal(self):
        self.db.add_thread("t1", "1", "plain", datetime.now())
        body = await (await self.client.get("/api/search?q=%25%25")).json()
        self.assertEqual(body["threads"], [])


class CommandSyncTests(ApiTestCase):
    async def test_sync_has_cooldown(self):
        clock = FakeClock(500.0)
        self.server._monotonic = clock

        resp = await self.client.post("/api/commands/sync")
        self.assertEqual((resp.status, await resp.json()), (200, {"status": "ok"}))
        self.bot.sync_commands.assert_awaited_once()

        clock.advance(20)
        resp = await self.client.post("/api/commands/sync")
        self.assertEqual(resp.status, 429)
        self.assertEqual(resp.headers["Retry-After"], "40")
        body = await resp.json()
        self.assertEqual(body["retry_after"], 40)
        self.assertIn("error", body)
        self.bot.sync_commands.assert_awaited_once()

        clock.advance(41)
        resp = await self.client.post("/api/commands/sync")
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.bot.sync_commands.await_count, 2)

    async def test_sync_in_progress_is_rejected(self):
        gate = asyncio.Event()

        async def slow_sync():
            await gate.wait()

        self.bot.sync_commands = slow_sync
        first = asyncio.ensure_future(self.client.post("/api/commands/sync"))
        for _ in range(100):
            if self.server._sync_lock.locked():
                break
            await asyncio.sleep(0.01)
        resp = await self.client.post("/api/commands/sync")
        self.assertEqual(resp.status, 429)
        self.assertIn("Retry-After", resp.headers)
        gate.set()
        self.assertEqual((await first).status, 200)

    async def test_sync_failure_is_generic_500(self):
        self.bot.sync_commands = mock.AsyncMock(side_effect=RuntimeError("discord said no"))
        resp = await self.client.post("/api/commands/sync")
        self.assertEqual((resp.status, await resp.json()), (500, {"error": "Internal server error"}))


class OverviewTests(ApiTestCase):
    async def test_uptime_uses_bot_start_time(self):
        self.bot.start_time = time.time() - 500
        body = await (await self.client.get("/api/overview")).json()
        self.assertGreaterEqual(body["bot_uptime_seconds"], 500)
        self.assertEqual(body["uptime_seconds"], body["bot_uptime_seconds"])
        diag = await (await self.client.get("/api/diagnostics")).json()
        self.assertGreaterEqual(diag["bot_uptime_seconds"], 500)
        self.assertEqual(diag["uptime_seconds"], diag["bot_uptime_seconds"])

    async def test_uptime_falls_back_to_dashboard_start(self):
        body = await (await self.client.get("/api/overview")).json()
        self.assertLess(body["bot_uptime_seconds"], 60)

    async def test_health_block(self):
        body = await (await self.client.get("/api/overview")).json()
        self.assertEqual(body["health"], {
            "providers": {"openrouter": "configured"}, "api_keys": None, "recent_error_count": 0})

        await self.client.get("/api/models/openrouter")
        logging.getLogger("tests.health").error("boom")
        service = self.enable_key_service()
        service.add_key("openai", "sk-a")
        bad = service.add_key("openai", "sk-b")
        self.db.update_api_key_validation_status(bad["key_id"], "invalid")
        good = service.add_key("openrouter", "sk-c")
        self.db.update_api_key_validation_status(good["key_id"], "valid")

        health = (await (await self.client.get("/api/overview")).json())["health"]
        self.assertEqual(health["providers"], {"openrouter": "ok"})
        self.assertEqual(health["api_keys"], {"total": 3, "invalid": 1, "untested": 1})
        self.assertEqual(health["recent_error_count"], 1)

    async def test_non_finite_latency_is_null(self):
        self.bot.latency = float("nan")
        resp = await self.client.get("/api/overview")
        self.assertIsNone((await resp.json())["latency_ms"])


if __name__ == "__main__":
    unittest.main()
