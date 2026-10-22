"""Tests for timezone-aware and repeating reminders, delivery, buttons, RSVPs,
the unprompted-post budget and welcomes."""
import os
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIRECTORY", tempfile.mkdtemp())

import discord

from src.cogs.community_commands import CommunityCommands
from src.cogs.reminder_commands import BUTTON_PREFIX, ReminderCommands, format_confirmation
from src.utils import events_service, reminder_service, restraint, time_parse
from src.utils.timeutil import local_tz, now_local_naive

from tests.dashboard_support import DashboardTestCase

CHICAGO = ZoneInfo("America/Chicago")


class TimeParseTests(unittest.TestCase):
    now = datetime(2026, 10, 22, 10, 0, tzinfo=CHICAGO)  # a Thursday

    def test_common_phrasings(self):
        cases = {
            "in 2 hours": datetime(2026, 10, 22, 12, 0),
            "tomorrow at 3pm": datetime(2026, 10, 23, 15, 0),
            "next tuesday at noon": datetime(2026, 10, 27, 12, 0),
            "Dec 3 at 9am": datetime(2026, 12, 3, 9, 0),
            "friday": datetime(2026, 10, 23, 9, 0),
            "at 9am": datetime(2026, 10, 23, 9, 0),
            "at 5pm": datetime(2026, 10, 22, 17, 0),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                parsed = time_parse.parse_when(text, CHICAGO, self.now)
                self.assertEqual(parsed.replace(tzinfo=None), expected)

    def test_past_and_nonsense(self):
        self.assertIsNone(time_parse.parse_when("yesterday", CHICAGO, self.now))
        self.assertIsNone(time_parse.parse_when("whenever", CHICAGO, self.now))

    def test_recurrence(self):
        self.assertEqual(time_parse.parse_recurrence("every weekday at 9am"),
                         ("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "at 9am"))
        self.assertEqual(time_parse.parse_recurrence("every day at 7pm"), ("FREQ=DAILY", "at 7pm"))
        self.assertEqual(time_parse.parse_recurrence("every mon and thu at 6pm")[0], "FREQ=WEEKLY;BYDAY=MO,TH")
        self.assertEqual(time_parse.parse_recurrence("buy milk"), (None, "buy milk"))
        self.assertEqual(time_parse.describe_rule("FREQ=WEEKLY;BYDAY=MO,TH"), "every Mon & Thu")

    def test_weekday_rule_skips_the_weekend(self):
        saturday = datetime(2026, 10, 24, 10, 0, tzinfo=CHICAGO)
        rule, rest = time_parse.parse_recurrence("every weekday at 9am")
        first = time_parse.first_occurrence(rule, time_parse.parse_when(rest, CHICAGO, saturday))
        self.assertEqual(first.replace(tzinfo=None), datetime(2026, 10, 26, 9, 0))

    def test_next_occurrence_keeps_wall_clock_across_dst(self):
        previous = datetime(2026, 10, 31, 9, 0, tzinfo=CHICAGO)  # DST ends Nov 1
        upcoming = time_parse.next_occurrence("FREQ=DAILY", previous, CHICAGO,
                                              datetime(2026, 10, 31, 9, 1, tzinfo=CHICAGO))
        self.assertEqual((upcoming.hour, upcoming.day), (9, 1))
        self.assertEqual(upcoming.utcoffset(), timedelta(hours=-6))

    def test_timezone_names(self):
        self.assertEqual(str(time_parse.resolve_timezone("america/new_york")), "America/New_York")
        self.assertEqual(str(time_parse.resolve_timezone("Tokyo")), "Asia/Tokyo")
        self.assertIsNone(time_parse.resolve_timezone("Atlantis"))


class PostBudgetTests(unittest.TestCase):
    def test_sliding_window(self):
        clock = types.SimpleNamespace(now=0.0)
        budget = restraint.PostBudget(lambda: 2, clock=lambda: clock.now)
        self.assertTrue(budget.allow("c"))
        self.assertTrue(budget.allow("c"))
        self.assertFalse(budget.allow("c"))
        self.assertTrue(budget.allow("other"))
        clock.now = 3601
        self.assertTrue(budget.allow("c"))

    def test_zero_means_unlimited(self):
        budget = restraint.PostBudget(lambda: 0)
        self.assertTrue(all(budget.allow("c") for _ in range(100)))


def _user(id_, tz=None):
    return types.SimpleNamespace(id=id_, display_name=f"user{id_}")


class ReminderPlanningTests(DashboardTestCase):
    def make_cog(self):
        bot = types.SimpleNamespace(state_manager=self.state, openrouter_client=None, openai_client=None,
                                    ai_horde_client=None)
        cog = ReminderCommands(bot, None, None, None)
        cog.parse_time_with_ai = mock.AsyncMock(return_value=None)
        return cog

    async def test_user_timezone_is_used(self):
        cog = self.make_cog()
        self.state.set_user_timezone("1", "Asia/Tokyo")
        plan = await cog.plan_reminder("in 2 hours", "c", _user(1))
        self.assertEqual(plan["timezone"], "Asia/Tokyo")
        self.assertAlmostEqual((plan["due"] - now_local_naive()).total_seconds(), 7200, delta=120)

        plan = await cog.plan_reminder("tomorrow at 9am", "c", _user(1))
        due_in_tokyo = time_parse.from_server_naive(plan["due"], ZoneInfo("Asia/Tokyo"))
        self.assertEqual((due_in_tokyo.hour, due_in_tokyo.minute), (9, 0))

    async def test_repeat_and_fallback(self):
        cog = self.make_cog()
        plan = await cog.plan_reminder("every day at 7pm", "c", _user(2))
        self.assertEqual(plan["recurrence"], "FREQ=DAILY")
        self.assertIsNone(await cog.plan_reminder("sometime-ish", "c", _user(2)))
        cog.parse_time_with_ai.assert_awaited()

    async def test_unknown_timezone_rejected(self):
        with self.assertRaises(ValueError):
            self.state.set_user_timezone("1", "Atlantis")
        self.assertEqual(self.state.get_user_timezone("1"), local_tz())

    def test_confirmation_text(self):
        text = format_confirmation(7, "stretch", datetime(2026, 10, 23, 9, 0), "FREQ=WEEKLY;BYDAY=MO", True)
        self.assertIn("**Repeats:** every Mon", text)
        self.assertIn("by DM", text)


class _Channel:
    def __init__(self, id_=100):
        self.id = id_
        self.sent = []

    async def send(self, content=None, view=None, **kwargs):
        self.sent.append((content, view))
        return types.SimpleNamespace(id=len(self.sent))


class ReminderDeliveryTests(DashboardTestCase):
    def make_bot(self, channel, dm_fails=False):
        user = types.SimpleNamespace(send=mock.AsyncMock(side_effect=discord.Forbidden(
            types.SimpleNamespace(status=403, reason="no"), "closed") if dm_fails else None))
        return types.SimpleNamespace(
            state_manager=self.state, openrouter_client=None, openai_client=None, ai_horde_client=None,
            get_channel=lambda cid: channel if cid == channel.id else None,
            get_user=lambda uid: user, fetch_user=mock.AsyncMock(return_value=user)), user

    def due_now(self, **options):
        return self.state.add_reminder(user_id="1", channel_id="100", message="stretch",
                                       due_timestamp=now_local_naive() - timedelta(minutes=1), **options)

    async def test_one_off_is_sent_with_buttons_and_recorded(self):
        channel = _Channel()
        bot, _ = self.make_bot(channel)
        reminder_id = self.due_now()
        await reminder_service.deliver_due_reminders(bot)
        content, view = channel.sent[0]
        self.assertIn("<@1>", content)
        self.assertIn("stretch", content)
        ids = [item.custom_id for item in view.children]
        self.assertIn(f"{BUTTON_PREFIX}done:{reminder_id}", ids)
        self.assertTrue(self.state.get_reminder(reminder_id)["sent"])
        self.assertIn("stretch", self.state.get_channel_history("100")[-1]["content"])

    async def test_repeating_reminder_moves_to_next_time(self):
        channel = _Channel()
        bot, _ = self.make_bot(channel)
        reminder_id = self.due_now(recurrence="FREQ=DAILY")
        await reminder_service.deliver_due_reminders(bot)
        row = self.state.get_reminder(reminder_id)
        self.assertFalse(row["sent"])
        self.assertGreater(reminder_service._as_datetime(row["due_timestamp"]), now_local_naive())

    async def test_dm_delivery_falls_back_to_channel(self):
        channel = _Channel()
        bot, user = self.make_bot(channel, dm_fails=True)
        self.due_now(deliver_dm=True)
        await reminder_service.deliver_due_reminders(bot)
        user.send.assert_awaited()
        self.assertEqual(len(channel.sent), 1)

    async def test_many_due_at_once_are_batched(self):
        channel = _Channel()
        bot, _ = self.make_bot(channel)
        ids = [self.due_now() for _ in range(4)]
        await reminder_service.deliver_due_reminders(bot)
        self.assertEqual(len(channel.sent), 1)
        self.assertIn("**Reminders:**", channel.sent[0][0])
        self.assertTrue(all(self.state.get_reminder(i)["sent"] for i in ids))


class _Interaction:
    def __init__(self, custom_id, user_id):
        self.type = discord.InteractionType.component
        self.data = {"custom_id": custom_id}
        self.user = types.SimpleNamespace(id=user_id)
        self.response = types.SimpleNamespace(edit_message=mock.AsyncMock(), send_message=mock.AsyncMock())
        self.followup = types.SimpleNamespace(send=mock.AsyncMock())


class ReminderButtonTests(DashboardTestCase):
    def cog(self):
        bot = types.SimpleNamespace(state_manager=self.state, openrouter_client=None, openai_client=None,
                                    ai_horde_client=None)
        return ReminderCommands(bot, None, None, None)

    async def test_snooze_one_off_reschedules_it(self):
        rid = self.state.add_reminder(user_id="1", channel_id="100", message="x", due_timestamp=now_local_naive())
        self.state.mark_reminder_sent(rid)
        await self.cog().on_interaction(_Interaction(f"{BUTTON_PREFIX}snooze:{rid}:60", 1))
        row = self.state.get_reminder(rid)
        self.assertFalse(row["sent"])
        self.assertGreater(reminder_service._as_datetime(row["due_timestamp"]), now_local_naive() + timedelta(minutes=55))

    async def test_snooze_repeating_creates_a_copy(self):
        rid = self.state.add_reminder(user_id="1", channel_id="100", message="x",
                                      due_timestamp=now_local_naive() + timedelta(days=1), recurrence="FREQ=DAILY")
        await self.cog().on_interaction(_Interaction(f"{BUTTON_PREFIX}snooze:{rid}:10", 1))
        reminders = self.state.get_user_reminders("1")
        self.assertEqual(len(reminders), 2)
        self.assertEqual(sum(1 for r in reminders if r["recurrence"]), 1)

    async def test_only_the_owner_can_press(self):
        rid = self.state.add_reminder(user_id="1", channel_id="100", message="x", due_timestamp=now_local_naive())
        interaction = _Interaction(f"{BUTTON_PREFIX}done:{rid}", 2)
        await self.cog().on_interaction(interaction)
        interaction.response.send_message.assert_awaited()
        interaction.response.edit_message.assert_not_called()


class RsvpTests(DashboardTestCase):
    def add_event(self, hours_ahead=3):
        return self.db.events.add_event(
            guild_id="g", channel_id="100", name="Game night", description="",
            start_time=now_local_naive() + timedelta(hours=hours_ahead), duration_minutes=60,
            discord_event_id=None, created_by="1")

    async def test_going_creates_a_reminder_and_changing_removes_it(self):
        event_id = self.add_event()
        events_service.set_rsvp(self.state, event_id, "5", "going")
        reminders = self.state.get_user_reminders("5")
        self.assertEqual(len(reminders), 1)
        self.assertIn("Game night", reminders[0]["message"])
        events_service.set_rsvp(self.state, event_id, "5", "maybe")
        self.assertEqual(self.state.get_user_reminders("5"), [])
        self.assertEqual(self.db.events.rsvp_counts(event_id)["maybe"], ["5"])

    async def test_card_lists_people(self):
        event_id = self.add_event()
        events_service.set_rsvp(self.state, event_id, "5", "going")
        events_service.set_rsvp(self.state, event_id, "6", "no")
        embed = events_service.build_card(self.db.events.get_event(event_id), self.db.events.rsvp_counts(event_id))
        fields = {f.name: f.value for f in embed.fields}
        self.assertEqual(fields["✅ Going (1)"], "<@5>")
        self.assertEqual(fields["❌ Can't make it (1)"], "<@6>")

    async def test_button_updates_the_card(self):
        event_id = self.add_event()
        bot = types.SimpleNamespace(state_manager=self.state)
        interaction = _Interaction(f"{events_service.RSVP_PREFIX}going:{event_id}", 7)
        self.assertTrue(await events_service.handle_rsvp_interaction(bot, interaction))
        interaction.response.edit_message.assert_awaited()
        self.assertEqual(self.db.events.rsvp_counts(event_id)["going"], ["7"])
        self.assertFalse(await events_service.handle_rsvp_interaction(bot, _Interaction("other:1", 7)))


class WelcomeTests(DashboardTestCase):
    def make_cog(self, channel):
        sent = []

        async def send_response(ch, text, channel_id, **kwargs):
            sent.append(text)

        self.sent = sent
        bot = types.SimpleNamespace(state_manager=self.state, openrouter_client=None, openai_client=None,
                                    ai_horde_client=None,
                                    webhook_sender=types.SimpleNamespace(send_response=send_response))
        return CommunityCommands(bot)

    def member(self, channel, bot=False):
        guild = types.SimpleNamespace(id=9, name="Friends", system_channel=channel, text_channels=[],
                                      default_role=None, get_channel=lambda cid: None)
        return types.SimpleNamespace(id=5, bot=bot, display_name="Newbie", mention="<@5>", guild=guild)

    async def test_off_by_default(self):
        channel = types.SimpleNamespace(id=100)
        cog = self.make_cog(channel)
        await cog.on_member_join(self.member(channel))
        self.assertEqual(self.sent, [])

    async def test_welcome_uses_fallback_without_llm_and_respects_budget(self):
        channel = types.SimpleNamespace(id=100)
        cog = self.make_cog(channel)
        await self.state.set_welcome_enabled(True)
        await self.state.set_max_unprompted_posts_per_hour(1)
        with mock.patch.object(restraint, "_budget", None):
            await cog.on_member_join(self.member(channel))
            await cog.on_member_join(self.member(channel))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("<@5>", self.sent[0])
        self.assertIn("Friends", self.sent[0])

    async def test_bots_are_not_welcomed(self):
        channel = types.SimpleNamespace(id=100)
        cog = self.make_cog(channel)
        await self.state.set_welcome_enabled(True)
        await cog.on_member_join(self.member(channel, bot=True))
        self.assertEqual(self.sent, [])

    async def test_dashboard_round_trip(self):
        await self.login()
        resp = await self.client.put("/api/settings", json={
            "welcome_enabled": True, "welcome_channel_id": "123456789012345678",
            "welcome_note": "Read #rules", "max_unprompted_posts_per_hour": 3})
        self.assertEqual(resp.status, 200, await resp.text())
        body = await (await self.client.get("/api/settings")).json()
        self.assertEqual((body["welcome_enabled"], body["welcome_channel_id"], body["welcome_note"],
                          body["max_unprompted_posts_per_hour"]),
                         (True, "123456789012345678", "Read #rules", 3))
        resp = await self.client.put("/api/settings", json={"welcome_channel_id": "general"})
        self.assertEqual(resp.status, 400)


if __name__ == "__main__":
    unittest.main()
