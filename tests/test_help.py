"""Tests for /help being built from the registered commands."""
import os
import tempfile
import types
import unittest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIRECTORY", tempfile.mkdtemp())

import discord

from src.cogs.help_commands import build_categories, build_overview_embed


async def _noop(ctx):
    pass


def _command(name, description="desc"):
    return discord.SlashCommand(_noop, name=name, description=description)


class HelpCategoryTests(unittest.TestCase):
    def make_bot(self):
        memory = discord.SlashCommandGroup("memory", "Memory")
        memory.add_command(_command("me", "What Gideon remembers about you"))
        settings = discord.SlashCommandGroup("settings", "Settings")
        settings.add_command(_command("show", "Show settings"))
        commands = [_command("chat", "Talk to Gideon"), memory, settings, _command("mystery", "Unmapped")]
        return types.SimpleNamespace(application_commands=commands)

    def test_commands_come_from_the_bot(self):
        categories = build_categories(self.make_bot())
        chat_names = [c["name"] for c in categories["chat"]["commands"]]
        self.assertEqual(chat_names, ["/chat", "/memory me"])
        self.assertEqual(categories["chat"]["commands"][1]["description"], "What Gideon remembers about you")

    def test_admin_groups_are_admin_only(self):
        categories = build_categories(self.make_bot())
        self.assertTrue(categories["settings"]["admin_only"])
        self.assertEqual(categories["settings"]["admin_commands"][0]["name"], "/settings show")

    def test_unknown_commands_land_in_other_and_empty_categories_are_hidden(self):
        categories = build_categories(self.make_bot())
        self.assertEqual(categories["other"]["commands"][0]["name"], "/mystery")
        self.assertNotIn("trivia", categories)

    def test_overview_mentions_tools_only_when_enabled(self):
        categories = build_categories(self.make_bot())
        self.assertIn("remind me", build_overview_embed(categories, tools_enabled=True).description)
        self.assertNotIn("remind me", build_overview_embed(categories, tools_enabled=False).description)


if __name__ == "__main__":
    unittest.main()
