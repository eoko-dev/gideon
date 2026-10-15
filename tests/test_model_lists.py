import asyncio
import json
import os
import re
import tempfile
import time
import types
import unittest
from collections import Counter
from pathlib import Path

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIRECTORY", tempfile.mkdtemp())

import discord
from aiohttp import web
from aiohttp.test_utils import TestServer

from src.cogs import video_commands
from src.cogs.unified_image_commands import UnifiedImageCommands
from src.cogs.video_commands import VideoCommands
from src.utils.model_list_refresher import ModelListRefresher
from src.utils.openrouter_image_client import OpenRouterImageClient
from src.utils.openrouter_video_client import OpenRouterVideoClient

ROOT = Path(__file__).resolve().parent.parent

IMAGE_ENTRIES = [
    {"id": "text-only", "architecture": {"output_modalities": ["text"], "modality": "text->text"}},
    {"id": "image-only-id", "name": "Image Only", "architecture": {"output_modalities": ["image"]}},
    {"id": "image-text-id", "architecture": {"output_modalities": ["image", "text"]}},
    {"id": "legacy-modality-id", "architecture": {"output_modalities": [], "modality": "text->image"}},
]
VIDEO_ENTRIES = [
    {"id": "google/veo-3.1", "name": "Veo 3.1", "generate_audio": True,
     "supported_frame_images": ["first_frame", "last_frame"],
     "pricing_skus": {"per_second": "0.4"}},
    {"id": "other/clip", "name": "Clip", "generate_audio": False, "supported_frame_images": []},
]


class FakeOpenRouter:
    def __init__(self):
        self.hits = Counter()
        self.queries = {}
        self.chat_bodies = []
        self.status = 200
        self.image_entries = IMAGE_ENTRIES
        self.video_entries = VIDEO_ENTRIES
        app = web.Application()
        app.router.add_get("/models", self.models)
        app.router.add_get("/videos/models", self.video_models)
        app.router.add_post("/chat/completions", self.chat)
        self.server = TestServer(app)

    async def models(self, request):
        self.hits["/models"] += 1
        self.queries["/models"] = dict(request.query)
        if self.status != 200:
            return web.Response(status=self.status, text="boom")
        return web.json_response({"data": self.image_entries})

    async def video_models(self, request):
        self.hits["/videos/models"] += 1
        if self.status != 200:
            return web.Response(status=self.status, text="boom")
        return web.json_response({"data": self.video_entries})

    async def chat(self, request):
        self.hits["/chat/completions"] += 1
        self.chat_bodies.append(await request.json())
        return web.json_response({"choices": [{"message": {"content": "x"}}]})

    @property
    def base_url(self):
        return str(self.server.make_url("")).rstrip("/")


class FakeServerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeOpenRouter()
        await self.fake.server.start_server()
        self.clients = []

    async def asyncTearDown(self):
        for c in self.clients:
            if c._session is not None:
                await c._session.close()
        await self.fake.server.close()

    def image_client(self, key="k"):
        c = OpenRouterImageClient(key)
        if key:
            c.base_url = self.fake.base_url
        self.clients.append(c)
        return c

    def video_client(self, key="k"):
        c = OpenRouterVideoClient(key)
        c.base_url = self.fake.base_url
        self.clients.append(c)
        return c


class ImageModelsTests(FakeServerTestCase):
    async def test_filters_and_query(self):
        res = await self.image_client().get_image_models()
        self.assertTrue(res["success"])
        self.assertEqual(self.fake.queries["/models"].get("output_modalities"), "image")
        ids = [m["id"] for m in res["models"]]
        self.assertEqual(ids, ["image-only-id", "image-text-id", "legacy-modality-id"])

    async def test_cache_and_force_refresh(self):
        c = self.image_client()
        await c.get_image_models()
        res = await c.get_image_models()
        self.assertEqual(res["source"], "cache")
        self.assertEqual(self.fake.hits["/models"], 1)
        res = await c.get_image_models(force_refresh=True)
        self.assertEqual(res["source"], "api")
        self.assertEqual(self.fake.hits["/models"], 2)

    async def test_http_error(self):
        self.fake.status = 500
        res = await self.image_client().get_image_models()
        self.assertFalse(res["success"])
        self.assertNotIn("models", res)

    async def test_unconfigured(self):
        res = await self.image_client("").get_image_models()
        self.assertFalse(res["success"])
        self.assertEqual(self.fake.hits["/models"], 0)


class ImageModalitiesTests(FakeServerTestCase):
    async def test_resolve_modalities(self):
        c = self.image_client()
        await c.get_image_models()
        self.assertEqual(c._resolve_modalities("image-only-id", ["image", "text"]), ["image"])
        self.assertEqual(c._resolve_modalities("image-text-id", ["image", "text"]), ["image", "text"])
        self.assertEqual(c._resolve_modalities("unknown", ["image", "text"]), ["image", "text"])

    async def test_generate_image_sends_image_only_modalities(self):
        c = self.image_client()
        await c.generate_image("a cat", model="image-only-id")
        self.assertEqual(self.fake.hits["/models"], 1)
        self.assertEqual(len(self.fake.chat_bodies), 1)
        self.assertEqual(self.fake.chat_bodies[0]["modalities"], ["image"])

    async def test_generate_image_keeps_text_for_dual_model(self):
        c = self.image_client()
        await c.generate_image("a cat", model="image-text-id")
        self.assertEqual(self.fake.chat_bodies[0]["modalities"], ["image", "text"])


class VideoModelsTests(FakeServerTestCase):
    async def test_mapping_and_endpoint(self):
        res = await self.video_client().list_video_models()
        self.assertTrue(res["success"])
        self.assertEqual(self.fake.hits["/videos/models"], 1)
        self.assertEqual(self.fake.hits["/models"], 0)
        veo, clip = res["models"]
        self.assertTrue(veo["supports_audio"])
        self.assertTrue(veo["supports_image_input"])
        self.assertEqual(veo["pricing"], {"per_second": "0.4"})
        self.assertFalse(clip["supports_audio"])
        self.assertFalse(clip["supports_image_input"])

    async def test_absent_frame_images_means_no_image_input(self):
        self.fake.video_entries = [{"id": "x/y"}]
        res = await self.video_client().list_video_models()
        self.assertFalse(res["models"][0]["supports_image_input"])

    async def test_cache_and_force_refresh(self):
        c = self.video_client()
        await c.list_video_models()
        res = await c.list_video_models()
        self.assertEqual(res["source"], "cache")
        self.assertEqual(self.fake.hits["/videos/models"], 1)
        await c.list_video_models(force_refresh=True)
        self.assertEqual(self.fake.hits["/videos/models"], 2)

    async def test_http_error_has_no_fallback(self):
        self.fake.status = 500
        res = await self.video_client().list_video_models()
        self.assertFalse(res["success"])
        self.assertNotIn("models", res)
        self.assertFalse(hasattr(OpenRouterVideoClient, "_fallback_models"))

    async def test_unconfigured(self):
        res = await self.video_client("").list_video_models()
        self.assertFalse(res["success"])
        self.assertEqual(self.fake.hits["/videos/models"], 0)


class ModelListRefresherTests(unittest.IsolatedAsyncioTestCase):
    def make(self, fetch):
        return ModelListRefresher(fetch, "Test")

    async def test_refresh_wait_times_out_but_task_continues(self):
        done = []

        async def slow():
            await asyncio.sleep(1)
            done.append(True)
            return {"success": True}

        r = self.make(slow)
        t0 = time.monotonic()
        await r.refresh(wait=True, timeout=0.1)
        self.assertLess(time.monotonic() - t0, 0.8)
        self.assertFalse(r._task.done())
        await r._task
        self.assertEqual(done, [True])

    async def test_start_twice_returns_same_task(self):
        calls = []

        async def slow():
            calls.append(1)
            await asyncio.sleep(0.2)

        r = self.make(slow)
        t1, t2 = r.start(), r.start()
        self.assertIs(t1, t2)
        await t1
        self.assertEqual(len(calls), 1)

    async def test_cancel(self):
        async def slow():
            await asyncio.sleep(5)

        r = self.make(slow)
        task = r.start()
        await asyncio.sleep(0)
        r.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(task.cancelled())

    async def test_failures_do_not_propagate(self):
        async def failing():
            return {"success": False, "error": "x"}

        async def raising():
            raise RuntimeError("nope")

        for fetch in (failing, raising):
            r = self.make(fetch)
            await r.refresh(wait=True, timeout=1)
            await r._task


class CogAutocompleteTests(FakeServerTestCase):
    async def test_video_cog(self):
        client = self.video_client()
        cog = VideoCommands.__new__(VideoCommands)
        cog.video_client = client
        cog._models_refresher = ModelListRefresher(client.list_video_models, "Video")

        models = await cog._get_models()
        self.assertEqual([m["id"] for m in models], ["google/veo-3.1", "other/clip"])

        choices = await cog.model_autocomplete(types.SimpleNamespace(value="veo"))
        self.assertEqual(len(choices), 1)
        self.assertIsInstance(choices[0], discord.OptionChoice)
        self.assertEqual(choices[0].value, "google/veo-3.1")
        self.assertEqual(self.fake.hits["/videos/models"], 1)

    async def test_video_cog_without_client(self):
        cog = VideoCommands.__new__(VideoCommands)
        cog.video_client = None
        cog._models_refresher = None
        self.assertEqual(await cog._get_models(), [])

    async def test_image_cog_prepends_configured_model(self):
        client = self.image_client()
        cog = UnifiedImageCommands.__new__(UnifiedImageCommands)
        cog.openrouter_image_client = client
        cog._openrouter_models_refresher = ModelListRefresher(client.get_image_models, "Image")
        cog.db = types.SimpleNamespace(
            get_global_config=lambda *a, **k: json.dumps({"model": "some/configured-model"})
        )

        res = await cog.openrouter_model_autocomplete(types.SimpleNamespace(value=""))
        self.assertEqual(res, [
            "✓ some/configured-model (current)",
            "image-only-id", "image-text-id", "legacy-modality-id",
        ])
        res = await cog.openrouter_model_autocomplete(types.SimpleNamespace(value="legacy"))
        self.assertEqual(res, ["legacy-modality-id"])


class CacheStalenessTests(unittest.TestCase):
    def test_empty_cache_is_always_stale(self):
        for client in (OpenRouterImageClient("k"), OpenRouterVideoClient("k")):
            client._models_cache_at = time.monotonic()
            self.assertTrue(client.models_cache_is_stale())


class StaticTests(unittest.TestCase):
    def test_python_resolution_choices_include_2k(self):
        self.assertIn("2K", [c.value for c in video_commands.RESOLUTION_CHOICES])

    def test_dashboard_resolution_select_includes_2k(self):
        html = (ROOT / "src/dashboard/index.html").read_text(encoding="utf-8")
        m = re.search(r'<select id="video-resolution">(.*?)</select>', html, re.S)
        self.assertIsNotNone(m)
        self.assertIn('<option value="2K">2K</option>', m.group(1))


if __name__ == "__main__":
    unittest.main()
