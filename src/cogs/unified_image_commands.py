import discord
from discord.ext import commands
from discord import option # Use discord.option for type hinting
import asyncio
import json
import logging
from datetime import datetime
from typing import Optional, Literal, Dict, Any, List

# Import clients and config
import io
from ..utils.ai_horde_client import AIHordeClient
from ..utils.cloudflare_client import CloudflareWorkerClient
from ..utils.openai_client import OpenAIClient
from ..utils.comfyui_client import ComfyUIClient
from ..utils.model_list_refresher import ModelListRefresher
from ..utils.openrouter_image_client import OpenRouterImageClient
from ..utils.database import DatabaseManager
from ..config import (
    AI_HORDE_API_KEY,
    CLOUDFLARE_WORKER_URL,
    CLOUDFLARE_API_KEY,
    OPENAI_API_KEY,
    OPENROUTER_API_KEY,
    COMFYUI_URL,
    DATA_DIRECTORY, # Needed for DatabaseManager default path
    DASHBOARD_ENABLED,
)

# Configure logging
logger = logging.getLogger('unified_image_commands')

# Define provider types for clarity
ProviderType = Literal['ai_horde', 'cloudflare', 'openai', 'comfyui', 'openrouter']

# Database keys
DB_KEY_ACTIVE_PROVIDER = "image_active_provider"
DB_KEY_CONFIG_PREFIX = "image_config_" # e.g., image_config_openai

# Default configurations (used if nothing is set in DB)
DEFAULT_PROVIDER: ProviderType = 'ai_horde'
DEFAULT_CONFIGS: Dict[ProviderType, Dict[str, Any]] = {
    'ai_horde': {
        "model": "stable_diffusion_xl",
        "size": "1024x1024",
        "steps": 30
    },
    'cloudflare': {
        "size": "768x768",
        "steps": 25,
        "seed": None # Let CF client handle random seed if None
    },
    'openai': {
        "model": "dall-e-3",
        "size": "1024x1024", # Fixed for OpenAI in this implementation
        "quality": "standard",
        "style": "vivid"
    },
    'comfyui': {
        "model": None,  # Use server default
        "size": "512x512",
        "steps": 20,
        "seed": None,
        "workflow": None  # Use default workflow
    },
    'openrouter': {
        "model": "google/gemini-2.0-flash-exp",
        "aspect_ratio": "1:1",
        "image_size": "1K"
    }
}

class UnifiedImageCommands(commands.Cog):
    """Unified command for AI image generation with backend management."""

    def __init__(self, bot):
        self.bot = bot
        self.db = DatabaseManager() # Assumes default DB path is okay

        # Initialize clients (only if API keys/URLs are provided)
        self.horde_client = AIHordeClient(AI_HORDE_API_KEY) if AI_HORDE_API_KEY else None
        self.cf_client = CloudflareWorkerClient(CLOUDFLARE_WORKER_URL, CLOUDFLARE_API_KEY) if CLOUDFLARE_WORKER_URL else None
        self.openai_client = OpenAIClient(OPENAI_API_KEY) if OPENAI_API_KEY else None
        self.comfyui_client = ComfyUIClient(COMFYUI_URL) if COMFYUI_URL else None
        self.openrouter_image_client = OpenRouterImageClient(OPENROUTER_API_KEY) if OPENROUTER_API_KEY else None

        # Override with DB-stored keys if API key service is available
        if hasattr(bot, 'api_key_service') and bot.api_key_service:
            svc = bot.api_key_service
            or_key = svc.get_key_for_provider('openrouter')
            if or_key:
                self.openrouter_image_client = OpenRouterImageClient(or_key)
            cf_key = svc.get_key_for_provider('cloudflare')
            cf_url = svc.get_key_for_provider('cloudflare_url') or CLOUDFLARE_WORKER_URL
            if cf_key and cf_url:
                self.cf_client = CloudflareWorkerClient(cf_url, cf_key)
            oa_key = svc.get_key_for_provider('openai')
            if oa_key:
                self.openai_client = OpenAIClient(oa_key)
            horde_key = svc.get_key_for_provider('ai_horde')
            if horde_key:
                self.horde_client = AIHordeClient(horde_key)
            comfyui_url = svc.get_key_for_provider('comfyui')
            if comfyui_url:
                self.comfyui_client = ComfyUIClient(comfyui_url)

        self._openrouter_models_refresher = (
            ModelListRefresher(self.openrouter_image_client.get_image_models, "OpenRouter image")
            if self.openrouter_image_client else None
        )

        # Log which clients are available
        if not self.horde_client: logger.warning("AI Horde client not initialized (API key missing).")
        if not self.cf_client: logger.warning("Cloudflare client not initialized (Worker URL missing).")
        if not self.openai_client: logger.warning("OpenAI client not initialized (API key missing).")
        if not self.comfyui_client: logger.warning("ComfyUI client not initialized (URL missing).")
        if not self.openrouter_image_client: logger.warning("OpenRouter Image client not initialized (API key missing).")

    def cog_unload(self):
        if self._openrouter_models_refresher:
            self._openrouter_models_refresher.cancel()

    async def _get_openrouter_image_model_ids(self) -> List[str]:
        """Live image model ids for autocomplete, bounded by Discord's response deadline."""
        client = self.openrouter_image_client
        if not client:
            return []
        if client.models_cache_is_stale():
            await self._openrouter_models_refresher.refresh(wait=not client.get_cached_image_models())
        return [m["id"] for m in client.get_cached_image_models()]

    async def openrouter_model_autocomplete(self, ctx):
        """Autocomplete for OpenRouter image generation models."""
        current_input = ctx.value.lower() if ctx.value else ""

        # Get current configured model to highlight it
        config_key = f"{DB_KEY_CONFIG_PREFIX}openrouter"
        config_json = self.db.get_global_config(config_key)
        current_model = None
        if config_json:
            try:
                config = json.loads(config_json)
                current_model = config.get("model")
            except json.JSONDecodeError:
                pass

        # Format models with current selection marked
        formatted_models = []
        model_ids = await self._get_openrouter_image_model_ids()
        if current_model and current_model not in model_ids:
            model_ids.insert(0, current_model)
        for model_id in model_ids:
            if model_id == current_model:
                formatted_models.append(f"✓ {model_id} (current)")
            else:
                formatted_models.append(model_id)

        # If no input, return first 25
        if not current_input:
            return formatted_models[:25]

        # Filter by user input
        matching_models = [model for model in formatted_models if current_input in model.lower()]

        # Prioritize models that start with the input
        priority_matches = [m for m in matching_models if m.lower().startswith(current_input) or m.lower().startswith("✓ " + current_input)]
        secondary_matches = [m for m in matching_models if m not in priority_matches]

        # Combine and limit to 25
        filtered_models = (priority_matches + secondary_matches)[:25]

        # If no matches, return first 25 anyway
        return filtered_models if filtered_models else formatted_models[:25]

    # --- User Command ---

    def _store_dream_messages(self, channel_id: str, user_id: str, prompt: str,
                               negative_prompt: Optional[str], result: dict,
                               provider_name: str, user_name: Optional[str] = None):
        """Store /dream interaction in channel history for dashboard visibility."""
        try:
            state = getattr(self.bot, 'state_manager', None)
            if not state:
                return

            # Store user's prompt
            user_content = f"/dream: {prompt}"
            if negative_prompt:
                user_content += f" (negative: {negative_prompt})"

            state.db_manager.add_message(
                role="user",
                content=user_content,
                timestamp=datetime.now(),
                channel_id=channel_id,
                user_id=user_id,
                user_name=user_name,
            )

            # Store assistant response with image reference
            if result.get("success"):
                image_url = result.get("image_url", "")
                model_used = result.get("model_used", "")
                # Use a marker format the dashboard can detect and render
                if image_url:
                    assistant_content = f"[image:{image_url}] Generated image for: {prompt} | Provider: {provider_name}"
                else:
                    # image_data or local_path - no persistent URL available
                    assistant_content = f"[image:attachment] Generated image for: {prompt} | Provider: {provider_name}"
                if model_used:
                    assistant_content += f" | Model: {model_used}"
            else:
                assistant_content = f"[image generation failed] {result.get('error', 'Unknown error')} | Provider: {provider_name}"

            state.db_manager.add_message(
                role="assistant",
                content=assistant_content,
                timestamp=datetime.now(),
                channel_id=channel_id,
            )
        except Exception as e:
            logger.error(f"Error storing dream messages: {e}", exc_info=True)

    @discord.slash_command(
        name="dream",
        description="Generate an image using the configured AI backend."
    )
    @option("prompt", str, description="Describe the image you want to create.", required=True)
    @option("negative_prompt", str, description="What to exclude from the image (if supported).", required=False, default=None)
    async def dream(self, ctx: discord.ApplicationContext, prompt: str, negative_prompt: Optional[str]):
        await ctx.defer()

        # 1. Get active provider from DB
        active_provider: ProviderType = self.db.get_global_config(DB_KEY_ACTIVE_PROVIDER, DEFAULT_PROVIDER)

        # 2. Get provider config from DB
        config_key = f"{DB_KEY_CONFIG_PREFIX}{active_provider}"
        config_json = self.db.get_global_config(config_key)
        provider_config = {}
        if config_json:
            try:
                provider_config = json.loads(config_json)
            except json.JSONDecodeError:
                logger.error(f"Failed to parse config JSON for {active_provider} from DB. Using defaults.")
                provider_config = DEFAULT_CONFIGS.get(active_provider, {})
        else:
            # Use hardcoded defaults if nothing in DB
            provider_config = DEFAULT_CONFIGS.get(active_provider, {})

        # Ensure all expected keys from defaults are present
        default_provider_config = DEFAULT_CONFIGS.get(active_provider, {})
        for key, value in default_provider_config.items():
            provider_config.setdefault(key, value)


        # 3. Select client and prepare parameters
        client = None
        params = {"prompt": prompt}
        provider_display_name = "Unknown"

        if active_provider == 'ai_horde':
            client = self.horde_client
            provider_display_name = "AI Horde"
            if client:
                width, height = map(int, provider_config.get("size", "512x512").split('x'))
                params.update({
                    "negative_prompt": negative_prompt,
                    "width": round(width / 64) * 64, # Ensure multiple of 64
                    "height": round(height / 64) * 64,
                    "steps": provider_config.get("steps", 30),
                    "model": provider_config.get("model", "stable_diffusion_xl")
                })
            else:
                await ctx.respond("⚠️ AI Horde client is not configured (missing API key). Please contact an admin.", ephemeral=True)
                return

        elif active_provider == 'cloudflare':
            client = self.cf_client
            provider_display_name = "Cloudflare"
            if client:
                width, height = map(int, provider_config.get("size", "768x768").split('x'))
                params.update({
                    "negative_prompt": negative_prompt,
                    "width": width,
                    "height": height,
                    "steps": provider_config.get("steps", 25),
                    "seed": provider_config.get("seed") # Can be None
                })
            else:
                await ctx.respond("⚠️ Cloudflare client is not configured (missing Worker URL). Please contact an admin.", ephemeral=True)
                return

        elif active_provider == 'openai':
            client = self.openai_client
            provider_display_name = "OpenAI"
            if client:
                # OpenAI size is fixed in this plan, but we read model/quality/style
                params.update({
                    "model": provider_config.get("model", "dall-e-3"),
                    "size": "1024x1024", # Enforce size
                    "quality": provider_config.get("quality") if provider_config.get("model") == "dall-e-3" else None,
                    "style": provider_config.get("style") if provider_config.get("model") == "dall-e-3" else None
                })
                # OpenAI doesn't support negative_prompt directly
                if negative_prompt:
                    # Optionally append to prompt or just log a warning
                    logger.info(f"Negative prompt '{negative_prompt}' provided for OpenAI, which doesn't support it directly. Ignoring.")
                    # params["prompt"] += f". Avoid: {negative_prompt}" # Basic workaround - uncomment if desired
            else:
                await ctx.respond("⚠️ OpenAI client is not configured (missing API key). Please contact an admin.", ephemeral=True)
                return

        elif active_provider == 'comfyui':
            client = self.comfyui_client
            provider_display_name = "ComfyUI"
            if client:
                width, height = map(int, provider_config.get("size", "512x512").split('x'))
                params.update({
                    "negative_prompt": negative_prompt or "",
                    "width": width,
                    "height": height,
                    "steps": provider_config.get("steps", 20),
                    "model": provider_config.get("model"),
                    "seed": provider_config.get("seed"),
                    "workflow_json": provider_config.get("workflow")
                })
            else:
                await ctx.respond("⚠️ ComfyUI client is not configured (missing URL). Please contact an admin.", ephemeral=True)
                return

        elif active_provider == 'openrouter':
            client = self.openrouter_image_client
            provider_display_name = "OpenRouter"
            if client:
                params.update({
                    "model": provider_config.get("model", "google/gemini-2.0-flash-exp"),
                    "aspect_ratio": provider_config.get("aspect_ratio", "1:1"),
                    "image_size": provider_config.get("image_size", "1K")
                })
                if negative_prompt:
                    params["negative_prompt"] = negative_prompt
                # Load modalities from database config (allows dashboard override)
                modalities_json = self.db.get_global_config('image_openrouter_modalities')
                if modalities_json:
                    try:
                        params["modalities"] = json.loads(modalities_json)
                    except json.JSONDecodeError:
                        pass  # Fall back to client default ["image", "text"]
            else:
                await ctx.respond("⚠️ OpenRouter client is not configured (missing API key). Please contact an admin.", ephemeral=True)
                return

        else:
            await ctx.respond(f"⚠️ Unknown or unsupported image provider configured: '{active_provider}'. Please contact an admin.", ephemeral=True)
            return

        # 4. Call the client's generate_image method
        thinking_msg = await ctx.followup.send(f"🎨 Generating image with **{provider_display_name}**: `{prompt}`\n*Please wait...*")

        try:
            result = await client.generate_image(**params)

            # 5. Format and send response
            if result.get("success"):
                embed = discord.Embed(
                    title="Generated Image",
                    description=f"**Prompt:** {prompt}",
                    color=discord.Color.blue() # Or provider-specific color
                )
                if negative_prompt and active_provider != 'openai': # Show negative if provided and supported
                     embed.add_field(name="Negative Prompt", value=negative_prompt, inline=False)

                footer_parts = [f"Provider: {provider_display_name}"]
                if result.get("model_used"): footer_parts.append(f"Model: {result.get('model_used')}")
                if result.get("seed"): footer_parts.append(f"Seed: {result.get('seed')}")
                if params.get("steps"): footer_parts.append(f"Steps: {params.get('steps')}")
                if params.get("size"): footer_parts.append(f"Size: {params.get('size')}")
                elif params.get("width"): footer_parts.append(f"Size: {params.get('width')}x{params.get('height')}")

                embed.set_footer(text=" | ".join(footer_parts))

                if result.get("revised_prompt"):
                    embed.add_field(name="Revised Prompt (DALL-E 3)", value=result["revised_prompt"], inline=False)

                if "image_url" in result:
                    embed.set_image(url=result["image_url"])
                    await thinking_msg.edit(content=None, embed=embed)
                elif "image_data" in result:
                    # ComfyUI returns raw bytes - create Discord file from memory
                    file = discord.File(
                        io.BytesIO(result["image_data"]),
                        filename="generated_image.png"
                    )
                    embed.set_image(url="attachment://generated_image.png")
                    await thinking_msg.edit(content=None, embed=embed, file=file)
                elif "local_path" in result: # Handle direct image data from Cloudflare if needed
                    file = discord.File(result["local_path"], filename="generated_image.png")
                    embed.set_image(url="attachment://generated_image.png")
                    await thinking_msg.edit(content=None, embed=embed, file=file)
                else:
                     await thinking_msg.edit(content="⚠️ Generation succeeded but no image URL or data found in response.")

            else:
                error_msg = result.get("error", "Unknown error")
                # Specific error handling (e.g., AI Horde Kudos) can be added here if needed
                await thinking_msg.edit(content=f"⚠️ Failed to generate image via {provider_display_name}: {error_msg}")

            # Store the interaction in channel history for dashboard visibility
            self._store_dream_messages(
                channel_id=str(ctx.channel_id),
                user_id=str(ctx.author.id),
                user_name=ctx.author.display_name,
                prompt=prompt,
                negative_prompt=negative_prompt,
                result=result,
                provider_name=provider_display_name,
            )

        except Exception as e:
            logger.exception(f"Error during image generation with {active_provider}: {e}")
            await thinking_msg.edit(content=f"⚠️ An unexpected error occurred while generating the image: {str(e)}")


    # --- Admin Commands (Group Definition) ---

    manage = discord.SlashCommandGroup(
        "dream_manage",
        "Manage image generation backend settings (Admin Only)",
        default_member_permissions=discord.Permissions(administrator=True)
    )

    # Define subcommands attached to the group
    @manage.command(name="set_provider", description="Set the active image generation provider.")
    @commands.has_permissions(administrator=True) # Re-enabled permission check
    @option("provider", description="Choose the backend provider.", choices=[
        discord.OptionChoice(name="AI Horde", value="ai_horde"),
        discord.OptionChoice(name="Cloudflare", value="cloudflare"),
        discord.OptionChoice(name="OpenAI", value="openai"),
        discord.OptionChoice(name="ComfyUI", value="comfyui"),
        discord.OptionChoice(name="OpenRouter", value="openrouter")
    ], required=True)
    async def manage_set_provider(self, ctx: discord.ApplicationContext, provider: str): # Changed ProviderType to str
        """Sets the active image generation provider."""
        # Check if the selected provider's client is initialized
        valid_provider = False
        if provider == 'ai_horde' and self.horde_client: valid_provider = True
        elif provider == 'cloudflare' and self.cf_client: valid_provider = True
        elif provider == 'openai' and self.openai_client: valid_provider = True
        elif provider == 'comfyui' and self.comfyui_client: valid_provider = True
        elif provider == 'openrouter' and self.openrouter_image_client: valid_provider = True

        if not valid_provider:
             await ctx.respond(f"⚠️ Cannot set provider to '{provider}'. Its client is not configured/initialized (missing API key/URL?).", ephemeral=True)
             return

        try:
            self.db.set_global_config(DB_KEY_ACTIVE_PROVIDER, provider, 'string') # Added back with correct type string
            await ctx.respond(f"✅ Image generation provider set to: **{provider}**", ephemeral=True)
            logger.info(f"Admin {ctx.author} set image provider to {provider}")
        except Exception as e:
            logger.error(f"Failed to set image provider in DB: {e}")
            await ctx.respond(f"❌ Failed to save provider setting to database: {e}", ephemeral=True)

    @manage.command(name="view_config", description="View the current image generation configuration.")
    @commands.has_permissions(administrator=True) # Re-enabled permission check
    async def manage_view_config(self, ctx: discord.ApplicationContext):
        """Displays the current image generation configuration."""
        active_provider = self.db.get_global_config(DB_KEY_ACTIVE_PROVIDER, DEFAULT_PROVIDER)

        embed = discord.Embed(title="Image Generation Configuration", color=discord.Color.orange())
        embed.add_field(name="Active Provider", value=f"**{active_provider}**", inline=False)

        for provider_name in ProviderType.__args__: # Use provider_name to avoid conflict
            config_key = f"{DB_KEY_CONFIG_PREFIX}{provider_name}"
            config_json = self.db.get_global_config(config_key)
            config_display = "Not Set (Using Defaults)"
            if config_json:
                try:
                    config_data = json.loads(config_json)

                    # For ComfyUI, hide workflow JSON if present (too long for Discord)
                    if provider_name == 'comfyui' and 'workflow' in config_data:
                        display_data = config_data.copy()
                        display_data['workflow'] = f"<Custom workflow: {len(config_data['workflow'])} chars>"
                        config_display = f"```json\n{json.dumps(display_data, indent=2)}\n```"
                    else:
                        config_display = f"```json\n{json.dumps(config_data, indent=2)}\n```"

                    # Truncate if still too long (Discord limit: 1024 chars per field)
                    if len(config_display) > 1024:
                        config_display = config_display[:1000] + "...\n```\n*(Truncated)*"

                except json.JSONDecodeError:
                    config_display = f"Error parsing stored JSON: `{config_json[:100]}...`"
            else:
                 default_conf = DEFAULT_CONFIGS.get(provider_name, {})
                 config_display = f"*Using Defaults:*\n```json\n{json.dumps(default_conf, indent=2)}\n```"

            embed.add_field(name=f"⚙️ {provider_name.capitalize()} Config", value=config_display, inline=False)

        await ctx.respond(embed=embed, ephemeral=True)

    # Define the subgroup directly from the main group
    configure = manage.create_subgroup("configure", "Configure default settings for a provider.")
    # Group permissions apply to commands within the subgroup

    # Define commands attached to the subgroup
    @configure.command(name="ai_horde", description="Configure AI Horde default settings.")
    @commands.has_permissions(administrator=True) # Re-enabled permission check
    @option("model", str, description="Default model name (e.g., stable_diffusion_xl).", required=True)
    @option("size", str, description="Default image size.", choices=[
        discord.OptionChoice(name="512x512", value="512x512"),
        discord.OptionChoice(name="768x768", value="768x768"),
        discord.OptionChoice(name="1024x1024", value="1024x1024"),
        discord.OptionChoice(name="512x768", value="512x768"),
        discord.OptionChoice(name="768x512", value="768x512"),
        discord.OptionChoice(name="1024x768", value="1024x768"),
        discord.OptionChoice(name="768x1024", value="768x1024")
    ], required=True)
    @option("steps", int, description="Default generation steps (e.g., 30).", min_value=10, max_value=100, required=True)
    async def configure_ai_horde(self, ctx: discord.ApplicationContext, model: str, size: str, steps: int):
        """Configures default settings for the AI Horde provider."""
        config = {"model": model, "size": size, "steps": steps}
        await self._save_provider_config(ctx, 'ai_horde', config)

    @configure.command(name="cloudflare", description="Configure Cloudflare default settings.")
    @commands.has_permissions(administrator=True) # Re-enabled permission check
    @option("size", str, description="Default image size.", choices=[
        discord.OptionChoice(name="256x256", value="256x256"),
        discord.OptionChoice(name="512x512", value="512x512"),
        discord.OptionChoice(name="768x768", value="768x768"),
        discord.OptionChoice(name="1024x1024", value="1024x1024"),
        discord.OptionChoice(name="512x768", value="512x768"),
        discord.OptionChoice(name="768x512", value="768x512"),
        discord.OptionChoice(name="1024x768", value="1024x768"),
        discord.OptionChoice(name="768x1024", value="768x1024")
    ], required=True)
    @option("steps", int, description="Default generation steps (e.g., 25).", min_value=10, max_value=50, required=True)
    @option("seed", int, description="Default seed (optional, leave blank for random).", required=False, default=None)
    async def configure_cloudflare(self, ctx: discord.ApplicationContext, size: str, steps: int, seed: Optional[int]):
        """Configures default settings for the Cloudflare provider."""
        config = {"size": size, "steps": steps, "seed": seed}
        # Remove seed if None for cleaner storage, although None is valid JSON
        config = {k: v for k, v in config.items() if v is not None}
        await self._save_provider_config(ctx, 'cloudflare', config)

    @configure.command(name="openai", description="Configure OpenAI default settings.")
    @commands.has_permissions(administrator=True) # Re-enabled permission check
    @option("model", description="Default OpenAI model.", choices=[
        discord.OptionChoice(name="DALL-E 3", value="dall-e-3"),
        discord.OptionChoice(name="DALL-E 2", value="dall-e-2")
    ], required=True)
    @option("quality", description="Default quality (DALL-E 3 only).", type=str, choices=[ # Added type=str
        discord.OptionChoice(name="Standard", value="standard"),
        discord.OptionChoice(name="HD", value="hd")
    ], required=False, default=None) # Default None, let user choose standard/hd explicitly
    @option("style", description="Default style (DALL-E 3 only).", type=str, choices=[ # Added type=str
        discord.OptionChoice(name="Vivid", value="vivid"),
        discord.OptionChoice(name="Natural", value="natural")
    ], required=False, default=None) # Default None
    async def configure_openai(self, ctx: discord.ApplicationContext, model: str, quality: Optional[str], style: Optional[str]): # Changed Literals to str/Optional[str]
        """Configures default settings for the OpenAI provider."""
        config = {
            "model": model,
            "size": "1024x1024", # Size is fixed for OpenAI in this plan
            "quality": quality if model == 'dall-e-3' else None,
            "style": style if model == 'dall-e-3' else None
        }
        # Remove None values for cleaner storage
        config = {k: v for k, v in config.items() if v is not None}
        await self._save_provider_config(ctx, 'openai', config)

    @configure.command(name="comfyui", description="Configure ComfyUI default settings.")
    @commands.has_permissions(administrator=True)
    @option("size", str, description="Default image size.", choices=[
        discord.OptionChoice(name="512x512", value="512x512"),
        discord.OptionChoice(name="768x768", value="768x768"),
        discord.OptionChoice(name="1024x1024", value="1024x1024"),
        discord.OptionChoice(name="512x768 (Portrait)", value="512x768"),
        discord.OptionChoice(name="768x512 (Landscape)", value="768x512"),
        discord.OptionChoice(name="1024x768 (Landscape)", value="1024x768"),
        discord.OptionChoice(name="768x1024 (Portrait)", value="768x1024")
    ], required=True)
    @option("steps", int, description="Default generation steps (10-150).", min_value=10, max_value=150, required=True)
    @option("model", str, description="Checkpoint model name (use /dream_manage comfyui_models to list).", required=False, default=None)
    async def configure_comfyui(self, ctx: discord.ApplicationContext, size: str, steps: int, model: Optional[str]):
        """Configures default settings for the ComfyUI provider."""
        config = {"size": size, "steps": steps}
        if model:
            config["model"] = model
        await self._save_provider_config(ctx, 'comfyui', config)

    @configure.command(name="openrouter", description="Configure OpenRouter default settings.")
    @commands.has_permissions(administrator=True)
    @option(
        "model",
        str,
        description="Select OpenRouter image generation model",
        required=True,
        autocomplete=openrouter_model_autocomplete
    )
    @option("aspect_ratio", str, description="Image aspect ratio (Gemini models only)", choices=[
        discord.OptionChoice(name="Square (1:1)", value="1:1"),
        discord.OptionChoice(name="Landscape (16:9)", value="16:9"),
        discord.OptionChoice(name="Portrait (9:16)", value="9:16"),
        discord.OptionChoice(name="Landscape (4:3)", value="4:3"),
        discord.OptionChoice(name="Portrait (3:4)", value="3:4")
    ], required=False, default="1:1")
    @option("image_size", str, description="Image size (Gemini models only)", choices=[
        discord.OptionChoice(name="1K (1024px)", value="1K"),
        discord.OptionChoice(name="2K (2048px)", value="2K"),
        discord.OptionChoice(name="4K (4096px)", value="4K")
    ], required=False, default="1K")
    async def configure_openrouter(self, ctx: discord.ApplicationContext, model: str, aspect_ratio: str, image_size: str):
        """Configures default settings for the OpenRouter provider."""
        # Strip the "✓ " prefix and " (current)" suffix if present
        clean_model = model.replace("✓ ", "").replace(" (current)", "").strip()

        config = {
            "model": clean_model,
            "aspect_ratio": aspect_ratio,
            "image_size": image_size
        }
        await self._save_provider_config(ctx, 'openrouter', config)

    # --- ComfyUI-specific Admin Commands ---

    @manage.command(name="comfyui_models", description="List available ComfyUI checkpoint models.")
    @commands.has_permissions(administrator=True)
    async def comfyui_models(self, ctx: discord.ApplicationContext):
        """Lists available checkpoint models from ComfyUI server."""
        if not self.comfyui_client:
            await ctx.respond("⚠️ ComfyUI is not configured (missing URL).", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)

        result = await self.comfyui_client.get_available_models("checkpoints")

        if not result.get("success"):
            await ctx.respond(f"⚠️ Error: {result.get('error', 'Unknown error')}", ephemeral=True)
            return

        models = result.get("models", [])

        if not models:
            await ctx.respond("No checkpoint models found on ComfyUI server.", ephemeral=True)
            return

        embed = discord.Embed(
            title="ComfyUI Checkpoint Models",
            description=f"Found {len(models)} available models",
            color=discord.Color.green()
        )

        # Show first 25 models (Discord embed field limit)
        model_list = "\n".join([f"`{m['name']}`" for m in models[:25]])
        if len(models) > 25:
            model_list += f"\n... and {len(models) - 25} more"

        embed.add_field(name="Available Models", value=model_list or "None", inline=False)
        embed.set_footer(text="Use model names with /dream_manage configure comfyui")

        await ctx.respond(embed=embed, ephemeral=True)

    @manage.command(name="comfyui_test", description="Test ComfyUI server connection.")
    @commands.has_permissions(administrator=True)
    async def comfyui_test(self, ctx: discord.ApplicationContext):
        """Tests connection to the ComfyUI server."""
        if not self.comfyui_client:
            await ctx.respond("⚠️ ComfyUI is not configured (missing URL).", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)

        result = await self.comfyui_client.test_connection()

        if result.get("success"):
            stats = result.get("system_stats", {})
            embed = discord.Embed(
                title="ComfyUI Connection Test",
                description="Successfully connected to ComfyUI server",
                color=discord.Color.green()
            )
            embed.add_field(name="URL", value=self.comfyui_client.api_url, inline=False)

            # Display system info if available
            if "system" in stats:
                sys_info = stats["system"]
                embed.add_field(name="OS", value=sys_info.get("os", "Unknown"), inline=True)
                embed.add_field(name="Python", value=sys_info.get("python_version", "Unknown"), inline=True)

            if "devices" in stats:
                devices = stats["devices"]
                if devices:
                    device_info = devices[0]
                    embed.add_field(name="GPU", value=device_info.get("name", "Unknown"), inline=False)
                    vram_total = device_info.get("vram_total", 0)
                    vram_free = device_info.get("vram_free", 0)
                    if vram_total:
                        embed.add_field(name="VRAM", value=f"{vram_free / 1e9:.1f} / {vram_total / 1e9:.1f} GB free", inline=True)

            await ctx.respond(embed=embed, ephemeral=True)
        else:
            await ctx.respond(
                f"⚠️ Connection failed: {result.get('error', 'Unknown error')}\n\nURL: `{self.comfyui_client.api_url}`",
                ephemeral=True
            )

    @manage.command(name="comfyui_workflow", description="Set a custom ComfyUI workflow (JSON).")
    @commands.has_permissions(administrator=True)
    @option("workflow_json", str, description="Workflow JSON string (or 'reset' to use default)", required=True)
    async def comfyui_workflow(self, ctx: discord.ApplicationContext, workflow_json: str):
        """Sets a custom workflow for ComfyUI generation."""
        if not self.comfyui_client:
            await ctx.respond("⚠️ ComfyUI is not configured (missing URL).", ephemeral=True)
            return

        if workflow_json.lower() == 'reset':
            # Clear custom workflow
            config_key = f"{DB_KEY_CONFIG_PREFIX}comfyui"
            current = self.db.get_global_config(config_key)
            if current:
                try:
                    config = json.loads(current)
                    config.pop('workflow', None)
                    self.db.set_global_config(config_key, json.dumps(config), 'json')
                except json.JSONDecodeError:
                    pass
            await ctx.respond("✅ Custom workflow cleared. Using default workflow.", ephemeral=True)
            return

        # Validate JSON
        try:
            workflow = json.loads(workflow_json)
            # Basic validation - check for some expected node types
            class_types = [n.get("class_type") for n in workflow.values() if isinstance(n, dict)]
            if not class_types:
                await ctx.respond(
                    "⚠️ Invalid workflow: No nodes found. Workflow should be a dict of node definitions.",
                    ephemeral=True
                )
                return
        except json.JSONDecodeError as e:
            await ctx.respond(f"⚠️ Invalid JSON: {e}", ephemeral=True)
            return

        # Save to config
        config_key = f"{DB_KEY_CONFIG_PREFIX}comfyui"
        current = self.db.get_global_config(config_key)
        try:
            config = json.loads(current) if current else {}
        except json.JSONDecodeError:
            config = {}

        config['workflow'] = workflow_json
        self.db.set_global_config(config_key, json.dumps(config), 'json')

        await ctx.respond(
            f"✅ Custom workflow saved ({len(class_types)} nodes detected). It will be used for future generations.",
            ephemeral=True
        )

    async def _save_provider_config(self, ctx: discord.ApplicationContext, provider: ProviderType, config: Dict[str, Any]):
        """Helper function to save provider config JSON to the database."""
        config_key = f"{DB_KEY_CONFIG_PREFIX}{provider}"
        try:
            # Fetch existing config from database
            existing_json = self.db.get_global_config(config_key)
            if existing_json:
                try:
                    existing_config = json.loads(existing_json)
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse existing config for {provider}, starting fresh")
                    existing_config = {}
            else:
                existing_config = {}

            # Merge new config with existing (new values override)
            merged_config = {**existing_config, **config}

            # Save merged config back to database
            config_json = json.dumps(merged_config)
            self.db.set_global_config(config_key, config_json, 'json') # Store as JSON string

            # Prepare response - truncate if too long for Discord (2000 char limit)
            # For ComfyUI, hide workflow JSON in response if present
            if provider == 'comfyui' and 'workflow' in merged_config:
                display_config = merged_config.copy()
                display_config['workflow'] = f"<Custom workflow: {len(merged_config['workflow'])} chars>"
                config_display = json.dumps(display_config, indent=2)
            else:
                config_display = json.dumps(merged_config, indent=2)

            response_msg = f"✅ Default configuration saved for **{provider}**:\n```json\n{config_display}\n```"

            # Truncate if still too long (leave room for message formatting)
            if len(response_msg) > 1900:
                response_msg = f"✅ Default configuration saved for **{provider}**.\n\nConfig preview:\n```json\n{config_display[:1800]}\n... (truncated)\n```"

            await ctx.respond(response_msg, ephemeral=True)
            logger.info(f"Admin {ctx.author} updated config for {provider}: {config_json}")
        except json.JSONDecodeError:
             await ctx.respond(f"❌ Internal error: Failed to serialize configuration to JSON.", ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to save config for {provider} to DB: {e}")
            await ctx.respond(f"❌ Failed to save configuration to database: {e}", ephemeral=True)


def setup(bot):
    cog = UnifiedImageCommands(bot)

    # When dashboard is enabled, remove admin-only /dream_manage commands
    # since those settings are managed through the web dashboard instead
    if DASHBOARD_ENABLED:
        # Remove the manage command group so it doesn't register with Discord
        cog.__cog_commands__ = [
            cmd for cmd in cog.__cog_commands__
            if not (hasattr(cmd, 'name') and cmd.name == 'dream_manage')
        ]
        logger.info("Dashboard enabled - /dream_manage commands hidden from Discord.")

    bot.add_cog(cog)
    if cog._openrouter_models_refresher:
        cog._openrouter_models_refresher.start()
    logger.info("UnifiedImageCommands cog loaded.")