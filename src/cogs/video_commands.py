"""Video generation commands for Gideon.

Provides:
    /video - User command to generate a video from a text prompt via OpenRouter.
    /video_manage - Admin commands to configure defaults and view available models.

Backend uses OpenRouter's asynchronous /api/v1/videos endpoint:
    1. Submit job
    2. Poll until terminal status
    3. Download from unsigned_urls and post the result
"""
import asyncio
import io
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp
import discord

from ..utils import budget
from discord import option
from discord.ext import commands

from ..config import DASHBOARD_ENABLED, OPENROUTER_API_KEY
from ..utils.database import DatabaseManager
from ..utils.model_list_refresher import ModelListRefresher
from ..utils.openrouter_video_client import OpenRouterVideoClient

logger = logging.getLogger('video_commands')

DB_KEY_ACTIVE_PROVIDER = "video_active_provider"
DB_KEY_CONFIG_PREFIX = "video_config_"

DEFAULT_PROVIDER = "openrouter"
DEFAULT_OPENROUTER_CONFIG: Dict[str, Any] = {
    "model": "google/veo-3.1",
    "duration": 8,
    "aspect_ratio": "16:9",
    "resolution": "720p",
    "audio": True,
}

ASPECT_CHOICES = [
    discord.OptionChoice(name="Landscape (16:9)", value="16:9"),
    discord.OptionChoice(name="Portrait (9:16)", value="9:16"),
    discord.OptionChoice(name="Square (1:1)", value="1:1"),
    discord.OptionChoice(name="Landscape (4:3)", value="4:3"),
    discord.OptionChoice(name="Portrait (3:4)", value="3:4"),
]
RESOLUTION_CHOICES = [
    discord.OptionChoice(name="480p", value="480p"),
    discord.OptionChoice(name="720p (HD)", value="720p"),
    discord.OptionChoice(name="1080p (Full HD)", value="1080p"),
    discord.OptionChoice(name="2K", value="2K"),
    discord.OptionChoice(name="4K", value="4K"),
]
DURATION_CHOICES = [
    discord.OptionChoice(name="4 seconds", value=4),
    discord.OptionChoice(name="5 seconds", value=5),
    discord.OptionChoice(name="6 seconds", value=6),
    discord.OptionChoice(name="8 seconds", value=8),
    discord.OptionChoice(name="10 seconds", value=10),
    discord.OptionChoice(name="12 seconds", value=12),
    discord.OptionChoice(name="15 seconds", value=15),
    discord.OptionChoice(name="20 seconds", value=20),
]

# Discord per-attachment upload limit for non-boosted servers (25 MB).
# We avoid uploading larger files to prevent guaranteed failure.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

_CONTENT_TYPE_EXT = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/ogg": ".ogv",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/mpeg": ".mpg",
    "application/octet-stream": ".mp4",
}


def _ext_from_content_type(content_type: str) -> str:
    ct = content_type.lower().split(";")[0].strip()
    return _CONTENT_TYPE_EXT.get(ct, ".mp4")


class VideoCommands(commands.Cog):
    """Video generation slash commands."""

    def __init__(self, bot):
        self.bot = bot
        self.db = DatabaseManager()
        api_key = OPENROUTER_API_KEY
        if hasattr(bot, 'api_key_service') and bot.api_key_service:
            db_key = bot.api_key_service.get_key_for_provider('openrouter')
            if db_key:
                api_key = db_key

        self.video_client = OpenRouterVideoClient(api_key) if api_key else None
        if not self.video_client:
            logger.warning("OpenRouter Video client not initialized (API key missing).")
        self._models_refresher = (
            ModelListRefresher(self.video_client.list_video_models, "Video")
            if self.video_client else None
        )

    def cog_unload(self):
        if self._models_refresher:
            self._models_refresher.cancel()

    # ── Helpers ────────────────────────────────────────────────

    def _load_config(self) -> Dict[str, Any]:
        """Load the OpenRouter video config from DB, merged with defaults."""
        provider = self.db.get_global_config(DB_KEY_ACTIVE_PROVIDER, DEFAULT_PROVIDER)
        config_key = f"{DB_KEY_CONFIG_PREFIX}{provider}"
        config_json = self.db.get_global_config(config_key)
        cfg: Dict[str, Any] = {}
        if config_json:
            try:
                cfg = json.loads(config_json)
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse video config for {provider}; using defaults")
        merged = {**DEFAULT_OPENROUTER_CONFIG, **cfg}
        return merged

    async def _get_models(self) -> List[Dict[str, Any]]:
        """Live model list for autocomplete, bounded by Discord's response deadline."""
        if not self.video_client:
            return []
        if self.video_client.models_cache_is_stale():
            await self._models_refresher.refresh(wait=not self.video_client.get_cached_video_models())
        return self.video_client.get_cached_video_models()

    async def model_autocomplete(self, ctx: discord.AutocompleteContext):
        """Autocomplete for video model picker."""
        try:
            models = await self._get_models()
        except Exception as exc:
            logger.warning("Video model autocomplete failed: %s", exc)
            models = []
        current = (ctx.value or "").lower()
        results = []
        for m in models:
            mid = m.get("id", "")
            name = m.get("name", mid)
            display = f"{name} ({mid})" if name and name != mid else mid
            if not current or current in mid.lower() or current in name.lower():
                # Discord autocomplete option name max 100 chars; value max 100 chars
                results.append(discord.OptionChoice(name=display[:100], value=mid[:100]))
            if len(results) >= 25:
                break
        return results

    def _store_video_messages(
        self,
        channel_id: str,
        user_id: str,
        prompt: str,
        result: Dict[str, Any],
        params: Dict[str, Any],
        user_name: Optional[str] = None,
    ):
        """Store /video interaction in channel history for dashboard visibility."""
        try:
            state = getattr(self.bot, 'state_manager', None)
            if not state:
                return

            user_content = f"/video: {prompt}"
            state.db_manager.add_message(
                role="user",
                content=user_content,
                timestamp=datetime.now(),
                channel_id=channel_id,
                user_id=user_id,
                user_name=user_name,
            )

            if result.get("success"):
                video_url = result.get("video_url", "")
                model_used = params.get("model", "")
                if video_url:
                    assistant_content = f"[video:{video_url}] Generated video for: {prompt} | Model: {model_used}"
                else:
                    assistant_content = f"[video:attachment] Generated video for: {prompt} | Model: {model_used}"
                duration = params.get("duration")
                if duration:
                    assistant_content += f" | Duration: {duration}s"
            else:
                assistant_content = f"[video generation failed] {result.get('error', 'Unknown error')}"

            state.db_manager.add_message(
                role="assistant",
                content=assistant_content,
                timestamp=datetime.now(),
                channel_id=channel_id,
            )
        except Exception as e:
            logger.error(f"Error storing video messages: {e}", exc_info=True)

    # ── User Command ───────────────────────────────────────────

    @discord.slash_command(
        name="video",
        description="Generate a video from a text prompt using AI.",
    )
    @option("prompt", str, description="Describe the video you want to create.", required=True)
    @option(
        "model",
        str,
        description="Video generation model (leave blank for the configured default).",
        required=False,
        default=None,
        autocomplete=model_autocomplete,
    )
    @option(
        "duration",
        int,
        description="Length of the video in seconds.",
        required=False,
        default=None,
        choices=DURATION_CHOICES,
    )
    @option(
        "aspect_ratio",
        str,
        description="Aspect ratio of the generated video.",
        required=False,
        default=None,
        choices=ASPECT_CHOICES,
    )
    @option(
        "resolution",
        str,
        description="Resolution of the generated video.",
        required=False,
        default=None,
        choices=RESOLUTION_CHOICES,
    )
    @option(
        "audio",
        bool,
        description="Generate audio along with the video (model-dependent).",
        required=False,
        default=None,
    )
    @option(
        "seed",
        int,
        description="Optional seed for reproducible generation.",
        required=False,
        default=None,
    )
    @option(
        "image_url",
        str,
        description="Optional URL of a reference/start frame image (image-to-video).",
        required=False,
        default=None,
    )
    async def video(
        self,
        ctx: discord.ApplicationContext,
        prompt: str,
        model: Optional[str],
        duration: Optional[int],
        aspect_ratio: Optional[str],
        resolution: Optional[str],
        audio: Optional[bool],
        seed: Optional[int],
        image_url: Optional[str],
    ):
        await ctx.defer()

        limit_error = budget.generation_limit_error(self.bot.state_manager, ctx.author, budget.VIDEO, self.bot)
        if limit_error:
            await ctx.followup.send(f"⛔ {limit_error}", ephemeral=True)
            return

        if not self.video_client or not self.video_client.is_configured:
            await ctx.followup.send(
                "⚠️ Video generation is not configured. An admin must add an OpenRouter API key.",
                ephemeral=True,
            )
            return

        cfg = self._load_config()
        # User-supplied params override stored defaults.
        chosen_model = model or cfg.get("model") or DEFAULT_OPENROUTER_CONFIG["model"]
        chosen_duration = duration if duration is not None else cfg.get("duration")
        chosen_aspect = aspect_ratio or cfg.get("aspect_ratio")
        chosen_resolution = resolution or cfg.get("resolution")
        chosen_audio = audio if audio is not None else cfg.get("audio")

        params = {
            "prompt": prompt,
            "model": chosen_model,
            "duration": chosen_duration,
            "aspect_ratio": chosen_aspect,
            "resolution": chosen_resolution,
            "audio": chosen_audio,
            "seed": seed,
            "image": image_url,
        }

        info_lines = [f"**Model:** `{chosen_model}`"]
        if chosen_duration is not None:
            info_lines.append(f"**Duration:** {chosen_duration}s")
        if chosen_aspect:
            info_lines.append(f"**Aspect Ratio:** {chosen_aspect}")
        if chosen_resolution:
            info_lines.append(f"**Resolution:** {chosen_resolution}")
        info = " · ".join(info_lines)

        progress = await ctx.followup.send(
            f"🎬 Submitting video generation job…\n{info}\n*Prompt:* `{prompt[:200]}`"
        )

        submit_result = await self.video_client.submit_video(
            prompt=prompt,
            model=chosen_model,
            aspect_ratio=chosen_aspect,
            duration=chosen_duration,
            resolution=chosen_resolution,
            seed=seed,
            audio=chosen_audio,
            image=image_url,
        )

        if not submit_result.get("success"):
            err = submit_result.get("error", "Unknown error")
            await progress.edit(content=f"⚠️ Failed to submit video job: {err}")
            self._store_video_messages(
                channel_id=str(ctx.channel_id),
                user_id=str(ctx.author.id),
                user_name=ctx.author.display_name,
                prompt=prompt,
                result={"success": False, "error": err},
                params=params,
            )
            return

        job_id = submit_result["job_id"]
        budget.record_generation(self.bot.state_manager, budget.VIDEO, ctx.author.id, ctx.channel_id,
                                 provider="openrouter", model=params.get("model"))

        # ── Animated progress bar ──────────────────────────────
        # The bar fills over BAR_FULL_SECS seconds, capped at 95% until done.
        BAR_LEN = 20
        BAR_FULL_SECS = 240
        SPINNER = ["◐", "◓", "◑", "◒"]
        poll_start = asyncio.get_running_loop().time()
        spinner_state = {"idx": 0, "status": "queued"}

        def _render_progress_bar() -> str:
            elapsed = asyncio.get_running_loop().time() - poll_start
            pct = min(elapsed / BAR_FULL_SECS, 0.95)
            filled = int(pct * BAR_LEN)
            bar = "▓" * filled + "░" * (BAR_LEN - filled)
            spin = SPINNER[spinner_state["idx"] % len(SPINNER)]
            spinner_state["idx"] += 1
            mins, secs = divmod(int(elapsed), 60)
            elapsed_str = f"{mins}m {secs}s" if mins else f"{secs}s"
            status = spinner_state["status"]
            return (
                f"🎬 Generating your video…\n\n"
                f"`[{bar}]` {spin}  **{int(pct * 100)}%** · {elapsed_str} elapsed\n"
                f"Status: **{status}** · Job `{job_id}`\n"
                f"{info}\n\n"
                f"-# Polls every 15s — will post the video when ready"
            )

        await progress.edit(content=_render_progress_bar())

        async def on_status(poll_result: Dict[str, Any]):
            status = poll_result.get("status", "")
            if status:
                spinner_state["status"] = status
            try:
                await progress.edit(content=_render_progress_bar())
            except discord.HTTPException:
                pass

        final = await self.video_client.wait_for_completion(
            job_id=job_id,
            poll_interval=15.0,
            timeout=900.0,
            on_status=on_status,
        )

        if not final.get("success"):
            err = final.get("error", "Unknown error")
            await progress.edit(content=f"⚠️ Video generation failed: {err}")
            self._store_video_messages(
                channel_id=str(ctx.channel_id),
                user_id=str(ctx.author.id),
                user_name=ctx.author.display_name,
                prompt=prompt,
                result={"success": False, "error": err},
                params=params,
            )
            return

        status = final.get("status")
        if status != "completed":
            err_detail = (final.get("raw") or {}).get("error") or status
            await progress.edit(content=f"⚠️ Video job ended with status `{status}`: {err_detail}")
            self._store_video_messages(
                channel_id=str(ctx.channel_id),
                user_id=str(ctx.author.id),
                user_name=ctx.author.display_name,
                prompt=prompt,
                result={"success": False, "error": str(err_detail)},
                params=params,
            )
            return

        urls: List[str] = final.get("unsigned_urls") or []
        if not urls:
            await progress.edit(content="⚠️ Video job completed but no download URLs were returned.")
            self._store_video_messages(
                channel_id=str(ctx.channel_id),
                user_id=str(ctx.author.id),
                user_name=ctx.author.display_name,
                prompt=prompt,
                result={"success": False, "error": "No URLs returned"},
                params=params,
            )
            return

        video_url = urls[0]

        await progress.edit(content=f"🎬 Job `{job_id}` completed! Downloading video file…")

        # Attempt download with one retry before giving up.
        download = await self.video_client.download_video(video_url)
        if not download.get("success"):
            logger.warning(
                f"Video download attempt 1 failed for job {job_id}: {download.get('error')}; retrying in 5s"
            )
            await asyncio.sleep(5)
            download = await self.video_client.download_video(video_url)
            if not download.get("success"):
                logger.error(f"Video download failed after retry for job {job_id}: {download.get('error')}")

        embed = discord.Embed(
            title="Generated Video",
            description=f"**Prompt:** {prompt}",
            color=discord.Color.purple(),
        )
        footer_parts = [f"Model: {chosen_model}"]
        if chosen_duration is not None:
            footer_parts.append(f"{chosen_duration}s")
        if chosen_aspect:
            footer_parts.append(chosen_aspect)
        if chosen_resolution:
            footer_parts.append(chosen_resolution)
        embed.set_footer(text=" | ".join(footer_parts))

        video_data = download.get("data", b"") if download.get("success") else b""
        attached = False

        if download.get("success") and len(video_data) <= MAX_UPLOAD_BYTES:
            ext = _ext_from_content_type(download.get("content_type", "video/mp4"))
            filename = f"generated_video{ext}"
            disc_file = discord.File(io.BytesIO(video_data), filename=filename)
            try:
                # Discord doesn't support editing a message to add file attachments,
                # so send the video as a new followup. Edit the progress stub to a
                # minimal state afterwards — deleting it causes Discord to show the
                # video message as quoting a deleted message.
                await ctx.followup.send(embed=embed, file=disc_file)
                attached = True
                logger.info(f"Video job {job_id}: uploaded {len(video_data)/1024/1024:.1f} MB as {filename}")
                try:
                    await progress.edit(content="✅ Video generated.")
                except discord.HTTPException:
                    pass
            except discord.HTTPException as e:
                logger.warning(f"Video job {job_id}: file send failed ({e}); falling back to URL embed")

        if not attached:
            if not download.get("success"):
                embed.add_field(
                    name="⚠️ Download failed",
                    value=(
                        f"Could not retrieve the video file: {download.get('error', 'Unknown error')}\n"
                        f"[View/Download here]({video_url})"
                    ),
                    inline=False,
                )
            else:
                size_mb = len(video_data) / (1024 * 1024)
                embed.add_field(name="Video URL", value=video_url, inline=False)
                embed.add_field(
                    name="Note",
                    value=f"File is {size_mb:.1f} MB — too large to attach directly to Discord.",
                    inline=False,
                )
            try:
                await progress.edit(content=None, embed=embed)
            except discord.HTTPException:
                await ctx.followup.send(embed=embed)

        self._store_video_messages(
            channel_id=str(ctx.channel_id),
            user_id=str(ctx.author.id),
            user_name=ctx.author.display_name,
            prompt=prompt,
            result={"success": True, "video_url": video_url},
            params=params,
        )

    # ── Admin Group ────────────────────────────────────────────

    manage = discord.SlashCommandGroup(
        "video_manage",
        "Manage video generation backend settings (Admin Only)",
        default_member_permissions=discord.Permissions(administrator=True),
    )

    @manage.command(name="view_config", description="View the current video generation configuration.")
    @commands.has_permissions(administrator=True)
    async def manage_view_config(self, ctx: discord.ApplicationContext):
        """Display the current video generation configuration."""
        cfg = self._load_config()
        configured = self.video_client and self.video_client.is_configured

        embed = discord.Embed(title="Video Generation Configuration", color=discord.Color.purple())
        embed.add_field(name="Provider", value="OpenRouter", inline=True)
        embed.add_field(
            name="Status",
            value="✅ Configured" if configured else "⚠️ API key missing",
            inline=True,
        )
        embed.add_field(name="Defaults", value=f"```json\n{json.dumps(cfg, indent=2)}\n```", inline=False)
        await ctx.respond(embed=embed, ephemeral=True)

    @manage.command(name="models", description="List available video generation models.")
    @commands.has_permissions(administrator=True)
    async def manage_models(self, ctx: discord.ApplicationContext):
        """List available video models from OpenRouter."""
        await ctx.defer(ephemeral=True)
        if not self.video_client:
            await ctx.followup.send("⚠️ OpenRouter video client is not configured.", ephemeral=True)
            return

        result = await self.video_client.list_video_models()
        if not result.get("success"):
            await ctx.followup.send(f"⚠️ Failed to fetch models: {result.get('error')}", ephemeral=True)
            return

        models = result.get("models", [])
        if not models:
            await ctx.followup.send("No video models available.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Available Video Models",
            description=f"Source: `{result.get('source', 'unknown')}` · {len(models)} model(s)",
            color=discord.Color.purple(),
        )
        # Show up to 25 models (Discord embed field cap)
        for m in models[:25]:
            details = []
            if m.get("supported_resolutions"):
                details.append("res: " + ", ".join(map(str, m["supported_resolutions"])))
            if m.get("supported_aspect_ratios"):
                details.append("ar: " + ", ".join(map(str, m["supported_aspect_ratios"])))
            if m.get("supported_durations"):
                details.append("dur: " + ", ".join(map(str, m["supported_durations"])) + "s")
            if m.get("supports_audio"):
                details.append("audio")
            value = " · ".join(details) if details else "—"
            embed.add_field(name=m.get("name") or m.get("id"), value=f"`{m.get('id')}`\n{value}", inline=False)

        await ctx.respond(embed=embed, ephemeral=True)

    @manage.command(name="configure", description="Set defaults for video generation.")
    @commands.has_permissions(administrator=True)
    @option("model", str, description="Default model id.", required=True, autocomplete=model_autocomplete)
    @option("duration", int, description="Default duration in seconds.", required=False, default=None, choices=DURATION_CHOICES)
    @option("aspect_ratio", str, description="Default aspect ratio.", required=False, default=None, choices=ASPECT_CHOICES)
    @option("resolution", str, description="Default resolution.", required=False, default=None, choices=RESOLUTION_CHOICES)
    @option("audio", bool, description="Generate audio by default (model-dependent).", required=False, default=None)
    async def manage_configure(
        self,
        ctx: discord.ApplicationContext,
        model: str,
        duration: Optional[int],
        aspect_ratio: Optional[str],
        resolution: Optional[str],
        audio: Optional[bool],
    ):
        """Save default video configuration."""
        existing = self._load_config()
        new_config: Dict[str, Any] = {**existing, "model": model}
        if duration is not None:
            new_config["duration"] = duration
        if aspect_ratio is not None:
            new_config["aspect_ratio"] = aspect_ratio
        if resolution is not None:
            new_config["resolution"] = resolution
        if audio is not None:
            new_config["audio"] = audio

        try:
            self.db.set_global_config(DB_KEY_ACTIVE_PROVIDER, DEFAULT_PROVIDER, "string")
            self.db.set_global_config(
                f"{DB_KEY_CONFIG_PREFIX}{DEFAULT_PROVIDER}",
                json.dumps(new_config),
                "json",
            )
            await ctx.respond(
                f"✅ Saved video defaults:\n```json\n{json.dumps(new_config, indent=2)}\n```",
                ephemeral=True,
            )
            logger.info(f"Admin {ctx.author} updated video config: {new_config}")
        except Exception as e:
            logger.error(f"Failed to save video config: {e}", exc_info=True)
            await ctx.respond(f"❌ Failed to save configuration: {e}", ephemeral=True)


def setup(bot):
    cog = VideoCommands(bot)

    # When the dashboard is enabled, hide /video_manage admin commands since
    # those settings are managed through the web dashboard instead.
    if DASHBOARD_ENABLED:
        cog.__cog_commands__ = [
            cmd for cmd in cog.__cog_commands__
            if not (hasattr(cmd, 'name') and cmd.name == 'video_manage')
        ]
        logger.info("Dashboard enabled - /video_manage commands hidden from Discord.")

    bot.add_cog(cog)
    if cog._models_refresher:
        cog._models_refresher.start()
    logger.info("VideoCommands cog loaded.")
