"""Shared fixtures for the dashboard tests: a fake bot driving the real DashboardServer."""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIRECTORY", tempfile.mkdtemp())

import aiohttp
import discord
from aiohttp.test_utils import TestClient, TestServer

from src.utils.api_key_service import APIKeyService
from src.utils.dashboard import auth
from src.utils.dashboard.server import DashboardServer
from src.utils.database import DatabaseManager
from src.utils.encryption import EncryptionManager
from src.utils.model_manager import ProviderManager
from src.utils.state_manager import BotStateManager

SECRET = "correct-horse-battery"


class FakeLLMClient:
    def __init__(self, model_ids):
        self.model_ids = model_ids

    async def get_available_models(self):
        return {"success": True, "models": [{"id": m} for m in self.model_ids]}


class FakeUser:
    id = 4242

    def __str__(self):
        return "Gideon#0001"


class FakeBot:
    def __init__(self, state, model_manager, openrouter_client):
        self.state_manager = state
        self.model_manager = model_manager
        self.openrouter_client = openrouter_client
        self.openai_client = None
        self.ai_horde_client = None
        self.api_key_service = None
        self.guilds = []
        self.channels = {}
        self.cogs = {}
        self.latency = 0.042
        self.user = FakeUser()
        self.sync_commands = mock.AsyncMock()

    def is_ready(self):
        return True

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    def get_cog(self, name):
        return self.cogs.get(name)


def make_text_channel(channel_id, name, guild):
    channel = mock.MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.name = name
    channel.guild = guild
    channel.category = None
    channel.type = discord.ChannelType.text
    return channel


def make_guild(guild_id, name):
    guild = mock.MagicMock()
    guild.id = guild_id
    guild.name = name
    guild.channels = []
    return guild


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class DashboardTestCase(unittest.IsolatedAsyncioTestCase):
    """Runs a real DashboardServer app over a real temp SQLite database."""

    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = os.path.join(tmp.name, "data")
        self.temp_dir = os.path.join(tmp.name, "tmp")
        os.makedirs(self.data_dir)
        os.makedirs(self.temp_dir)

        for patcher in (
            mock.patch("src.utils.database.core.DATA_DIRECTORY", self.data_dir),
            mock.patch.object(tempfile, "tempdir", self.temp_dir),
            mock.patch("src.utils.state_manager.DatabaseManager", lambda: DatabaseManager()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        auth._active_sessions.clear()
        self.addCleanup(auth._active_sessions.clear)

        self.state = object.__new__(BotStateManager)
        await self.state.initialize_state()
        self.addCleanup(lambda: self.state.close_db())

        self.llm = FakeLLMClient(["good/model", "other/model"])
        self.model_manager = ProviderManager(self.llm, None, None, self.data_dir)
        self.state.set_model_manager(self.model_manager)

        self.bot = FakeBot(self.state, self.model_manager, self.llm)
        self.db = self.state.db_manager

        self.server = DashboardServer(self.bot, SECRET, port=0)
        self.server.create_app()
        self.client = TestClient(TestServer(self.server.app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def enable_key_service(self):
        service = APIKeyService(self.db, EncryptionManager("test-master-key"))
        self.bot.api_key_service = service
        return service

    async def login(self, client=None):
        client = client or self.client
        resp = await client.post("/api/auth/login", json={"secret": SECRET})
        self.assertEqual(resp.status, 200, await resp.text())
        return resp

    def leftover_temp_files(self):
        return sorted(os.listdir(self.temp_dir))

    @property
    def origin(self):
        return f"http://{self.client.server.host}:{self.client.server.port}"

    @staticmethod
    def upload(content: bytes):
        form = aiohttp.FormData()
        form.add_field("file", content, filename="upload.bin", content_type="application/octet-stream")
        return form
