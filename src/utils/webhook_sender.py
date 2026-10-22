"""Webhook management for per-channel persona message sending."""
import discord
import logging
import asyncio
from typing import Optional, Dict, List

from .discord_fmt import SAFE_ALLOWED_MENTIONS

logger = logging.getLogger(__name__)

WEBHOOK_NAME_PREFIX = "Gideon Persona"


class WebhookSender:
    """Manages Discord webhooks for persona-based message sending."""

    def __init__(self, bot):
        self.bot = bot
        self._webhook_cache: Dict[str, discord.Webhook] = {}
        self._lock = asyncio.Lock()

    async def get_or_create_webhook(
        self,
        channel: discord.TextChannel,
        channel_id: str
    ) -> Optional[discord.Webhook]:
        """
        Gets an existing webhook or creates one for the channel.
        Checks: memory cache -> DB stored credentials -> Discord API scan -> create new.
        """
        # 1. Check in-memory cache
        if channel_id in self._webhook_cache:
            return self._webhook_cache[channel_id]

        state = self.bot.state_manager
        persona = state.get_effective_persona(channel_id)

        # 2. Check DB for stored webhook credentials
        if persona and persona.get('webhook_id') and persona.get('webhook_token'):
            try:
                webhook = discord.Webhook.partial(
                    id=int(persona['webhook_id']),
                    token=persona['webhook_token'],
                    session=self.bot.http._HTTPClient__session
                )
                self._webhook_cache[channel_id] = webhook
                return webhook
            except Exception as e:
                logger.warning(
                    f"Stored webhook for channel {channel_id} invalid: {e}. "
                    "Will find or create a new one."
                )

        # 3. Search existing webhooks / create new (with lock to prevent races)
        async with self._lock:
            # Re-check cache after acquiring lock
            if channel_id in self._webhook_cache:
                return self._webhook_cache[channel_id]

            try:
                existing_webhooks = await channel.webhooks()
                for wh in existing_webhooks:
                    if (wh.name and wh.name.startswith(WEBHOOK_NAME_PREFIX)
                            and wh.user and wh.user.id == self.bot.user.id):
                        self._webhook_cache[channel_id] = wh
                        state.db_manager.update_channel_persona_webhook(
                            channel_id, str(wh.id), wh.token
                        )
                        logger.info(f"Found existing webhook for channel {channel_id}")
                        return wh

                # 4. Create new webhook
                webhook = await channel.create_webhook(
                    name=f"{WEBHOOK_NAME_PREFIX} {channel_id[-4:]}"
                )
                self._webhook_cache[channel_id] = webhook
                state.db_manager.update_channel_persona_webhook(
                    channel_id, str(webhook.id), webhook.token
                )
                logger.info(f"Created new webhook for channel {channel_id}")
                return webhook

            except discord.Forbidden:
                logger.error(
                    f"Missing 'Manage Webhooks' permission in channel {channel_id}. "
                    "Falling back to bot account."
                )
                return None
            except discord.HTTPException as e:
                logger.error(f"Discord API error for webhook in channel {channel_id}: {e}")
                return None

    async def send_response(
        self,
        channel: discord.abc.Messageable,
        content: str,
        channel_id: str,
        embeds: Optional[List[discord.Embed]] = None,
        files: Optional[List[discord.File]] = None,
        reference: Optional[discord.Message] = None,
    ) -> Optional[discord.Message]:
        """
        Sends a response through the persona webhook if active,
        otherwise falls back to the standard bot account.

        ``reference`` makes a bot-account message a Discord reply (without
        pinging the author). Webhooks can't reply, so persona messages ignore it.
        """
        state = self.bot.state_manager
        persona = state.get_effective_persona(channel_id)

        if not persona:
            return await self._standard_send(channel, content, embeds=embeds, files=files, reference=reference)

        # Webhooks only work in TextChannels; for threads, use parent channel
        target_channel = channel
        thread_param = None

        if isinstance(channel, discord.Thread):
            target_channel = channel.parent
            thread_param = channel
            # Use the parent channel's persona
            parent_id = str(channel.parent_id)
            persona = state.get_effective_persona(parent_id)
            if not persona:
                return await self._standard_send(channel, content, embeds=embeds, files=files, reference=reference)
            channel_id = parent_id

        if not isinstance(target_channel, discord.TextChannel):
            return await self._standard_send(channel, content, embeds=embeds, files=files, reference=reference)

        webhook = await self.get_or_create_webhook(target_channel, channel_id)
        if not webhook:
            return await self._standard_send(channel, content, embeds=embeds, files=files, reference=reference)

        try:
            return await self._webhook_send(
                webhook, content, persona, embeds=embeds, files=files, thread=thread_param
            )
        except discord.NotFound:
            # Webhook was deleted - clear cache and retry once
            logger.warning(f"Webhook for channel {channel_id} deleted. Retrying.")
            self._webhook_cache.pop(channel_id, None)
            state.db_manager.update_channel_persona_webhook(channel_id, None, None)

            webhook = await self.get_or_create_webhook(target_channel, channel_id)
            if webhook:
                try:
                    return await self._webhook_send(
                        webhook, content, persona, embeds=embeds, files=files, thread=thread_param
                    )
                except Exception as retry_e:
                    logger.error(f"Webhook retry failed: {retry_e}")

            return await self._standard_send(channel, content, embeds=embeds, files=files, reference=reference)

        except discord.HTTPException as e:
            if e.status == 429:
                logger.warning(f"Webhook rate limited for channel {channel_id}. Falling back.")
            else:
                logger.error(f"Webhook send failed for channel {channel_id}: {e}")
            return await self._standard_send(channel, content, embeds=embeds, files=files, reference=reference)

        except Exception as e:
            logger.error(f"Unexpected webhook error for channel {channel_id}: {e}", exc_info=True)
            return await self._standard_send(channel, content, embeds=embeds, files=files, reference=reference)

    async def _webhook_send(
        self,
        webhook: discord.Webhook,
        content: str,
        persona: dict,
        embeds: Optional[List[discord.Embed]] = None,
        files: Optional[List[discord.File]] = None,
        thread: Optional[discord.Thread] = None,
    ) -> discord.Message:
        """Sends a message via webhook with persona identity."""
        kwargs = {
            "content": content,
            "username": persona['display_name'],
            "wait": True,
            "allowed_mentions": SAFE_ALLOWED_MENTIONS,
        }
        if persona.get('avatar_url'):
            kwargs["avatar_url"] = persona['avatar_url']
        if embeds:
            kwargs["embeds"] = embeds
        if files:
            kwargs["files"] = files
        if thread:
            kwargs["thread"] = thread

        return await webhook.send(**kwargs)

    async def _standard_send(
        self,
        channel: discord.abc.Messageable,
        content: str,
        embeds: Optional[List[discord.Embed]] = None,
        files: Optional[List[discord.File]] = None,
        reference: Optional[discord.Message] = None,
    ) -> Optional[discord.Message]:
        """Standard bot account send (no webhook)."""
        kwargs = {"allowed_mentions": SAFE_ALLOWED_MENTIONS}
        if reference is not None and hasattr(reference, "to_reference"):
            # A deleted original shouldn't make the reply fail
            kwargs["reference"] = reference.to_reference(fail_if_not_exists=False)
            kwargs["mention_author"] = False
        if content:
            kwargs["content"] = content
        if embeds:
            kwargs["embeds"] = embeds
        if files:
            kwargs["files"] = files
        try:
            return await channel.send(**kwargs)
        except Exception as e:
            logger.error(f"Standard send failed: {e}", exc_info=True)
            return None

    def invalidate_cache(self, channel_id: str):
        """Removes a channel's webhook from cache."""
        self._webhook_cache.pop(channel_id, None)
