"""Tests for usage recording, daily limits and the usage/settings dashboard API."""
import types
import unittest

from src.utils import budget
from src.utils.llm_result import ChatResult
from src.utils.usage import UsageScope

from tests.dashboard_support import DashboardTestCase


def _member(id_, admin=False):
    return types.SimpleNamespace(id=id_, guild_permissions=types.SimpleNamespace(administrator=admin))


class BudgetTests(DashboardTestCase):
    async def test_usage_sink_records_rows(self):
        sink = budget.make_usage_sink(self.state)
        sink(UsageScope("chat", "1", "c1"),
             ChatResult(content="x", model="m", provider="openrouter",
                        usage={"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.25}))
        self.assertAlmostEqual(self.db.usage.user_cost_since("1", budget.start_of_day()), 0.25)

    async def test_chat_budget(self):
        await self.state.set_daily_user_cost_budget(0.5)
        member = _member(1)
        self.assertIsNone(budget.chat_budget_error(self.state, member))

        sink = budget.make_usage_sink(self.state)
        sink(UsageScope("chat", "1", "c1"), ChatResult(content="x", usage={"cost": 0.6}))
        self.assertIsNotNone(budget.chat_budget_error(self.state, member))
        # Admins and other users are not affected
        self.assertIsNone(budget.chat_budget_error(self.state, _member(1, admin=True)))
        self.assertIsNone(budget.chat_budget_error(self.state, _member(2)))

    async def test_zero_budget_means_unlimited(self):
        await self.state.set_daily_user_cost_budget(0)
        sink = budget.make_usage_sink(self.state)
        sink(UsageScope("chat", "1", "c1"), ChatResult(content="x", usage={"cost": 99}))
        self.assertIsNone(budget.chat_budget_error(self.state, _member(1)))

    async def test_generation_limits(self):
        await self.state.set_daily_user_video_limit(2)
        member = _member(7)
        for _ in range(2):
            self.assertIsNone(budget.generation_limit_error(self.state, member, budget.VIDEO))
            budget.record_generation(self.state, budget.VIDEO, 7, "c1")
        self.assertIn("2 videos", budget.generation_limit_error(self.state, member, budget.VIDEO))
        # Images are counted separately
        self.assertIsNone(budget.generation_limit_error(self.state, member, budget.IMAGE))

    async def test_bot_owner_is_exempt(self):
        await self.state.set_daily_user_video_limit(1)
        budget.record_generation(self.state, budget.VIDEO, 9, "c1")
        bot = types.SimpleNamespace(owner_id=9, owner_ids=None)
        self.assertIsNone(budget.generation_limit_error(self.state, _member(9), budget.VIDEO, bot))


class UsageApiTests(DashboardTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.login()

    async def test_usage_summary(self):
        sink = budget.make_usage_sink(self.state)
        sink(UsageScope("mention", "1", "c1"),
             ChatResult(content="x", model="m1", usage={"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.1}))
        sink(UsageScope("chat", "2", "c1"),
             ChatResult(content="x", model="m2", usage={"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.3}))
        budget.record_generation(self.state, budget.IMAGE, 1, "c1")

        resp = await self.client.get("/api/usage?days=7")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["totals"]["calls"], 3)
        self.assertAlmostEqual(body["totals"]["cost"], 0.4)
        self.assertEqual(body["totals"]["units"], 1)
        self.assertEqual({r["key"] for r in body["by_feature"]}, {"mention", "chat", "image"})
        self.assertEqual(body["by_user"][0]["key"], "2")  # highest cost first
        self.assertIn("limits", body)

    async def test_usage_days_validated(self):
        resp = await self.client.get("/api/usage?days=500")
        self.assertEqual(resp.status, 400)

    async def test_limit_settings_round_trip(self):
        resp = await self.client.put("/api/settings", json={
            "daily_user_cost_budget": 1.5, "daily_user_image_limit": 10, "daily_user_video_limit": 0,
            "utility_model": "openrouter/good/model"})
        self.assertEqual(resp.status, 200, await resp.text())
        body = await (await self.client.get("/api/settings")).json()
        self.assertEqual(body["daily_user_cost_budget"], 1.5)
        self.assertEqual(body["daily_user_image_limit"], 10)
        self.assertEqual(body["daily_user_video_limit"], 0)
        self.assertEqual(body["utility_model"], "openrouter/good/model")

        resp = await self.client.put("/api/settings", json={"utility_model": None})
        self.assertEqual(resp.status, 200)
        self.assertIsNone((await (await self.client.get("/api/settings")).json())["utility_model"])

    async def test_invalid_limit_settings(self):
        for body in ({"daily_user_cost_budget": -1}, {"daily_user_cost_budget": "lots"},
                     {"daily_user_image_limit": -3}, {"utility_model": "missing/model"}):
            with self.subTest(body=body):
                resp = await self.client.put("/api/settings", json=body)
                self.assertEqual(resp.status, 400)


if __name__ == "__main__":
    unittest.main()
