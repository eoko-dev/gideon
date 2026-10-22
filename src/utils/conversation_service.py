"""The one reply pipeline behind every way of talking to Gideon.

@mentions, /chat, AI threads and /thread message used to each build history,
resolve the model, check vision, call the LLM and send the reply in their own
way, and had drifted apart: only mentions had tools, threads hardcoded the
OpenRouter client and swapped its shared ``model`` attribute mid-request
(racing other conversations), and error strings leaked into history.

``ConversationService.respond()`` does all of it once:

1. resolve provider/model/client for the conversation (persona > thread >
   channel > global), always passing ``model=`` explicitly
2. load history (channel or thread, anchored to the message being answered)
3. build the system prompt with channel memory and people notes
4. collect images the model can see
5. run the tool loop (or a plain call when tool calling is off)
6. record the reply and send it through the persona webhook when active

Callers only decide *where* a conversation lives (``Conversation``) and how the
triggering message was recorded.
"""

import inspect
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import discord

from . import budget, llm_result
from .discord_fmt import chunk_message
from .llm_formatting import apply_outbound_mentions, collect_mention_targets
from .llm_result import ChatResult, call_chat
from .memory_service import check_and_rotate_session, llm_clients, schedule_compaction
from .tool_registry import execute_tool, get_tool_definitions, is_no_repeat_tool
from .usage import usage_scope

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif', '.webp')


@dataclass
class Conversation:
    """Where a conversation's history lives and where replies go.

    ``channel_id`` is always the configuration/persona/memory scope: for a
    thread it is the parent channel. ``thread_id`` is set when the history is
    the thread's own.
    """

    channel: Any                    # Messageable the reply is posted to
    channel_id: str
    thread_id: Optional[str] = None

    @property
    def is_thread(self) -> bool:
        return self.thread_id is not None


@dataclass
class ReplyRequest:
    """One turn to answer."""

    conversation: Conversation
    author: Any
    content: str
    # The triggering discord.Message (or a MessageLike for slash commands);
    # tool handlers post and read through it
    message: Any = None
    attachments: List[Any] = field(default_factory=list)
    # Timestamp the triggering turn was recorded with; history stops there so
    # messages that arrive mid-reply aren't mistaken for the turn being answered
    history_until: Optional[datetime] = None
    # False when recording the turn failed; it is then appended to the context
    recorded: bool = True
    extra_people: List[Any] = field(default_factory=list)
    use_tools: bool = True
    feature: str = "chat"


class MessageLike:
    """Minimal discord.Message stand-in for replies started by a slash command.

    Tool handlers only read author, channel, guild and content, and post via
    ``channel.send``; an interaction has all of those but no Message.
    """

    def __init__(self, ctx, content: str):
        self.id = getattr(getattr(ctx, "interaction", None), "id", None)
        self.author = ctx.author
        self.channel = ctx.channel
        self.guild = getattr(ctx, "guild", None)
        self.content = content
        self.mentions: List[Any] = []
        self.role_mentions: List[Any] = []
        self.channel_mentions: List[Any] = []
        self.attachments: List[Any] = []
        self.mention_everyone = False
        self.webhook_id = None
        self.reference = None


class ConversationService:
    """Shared reply pipeline. One instance lives on ``bot.conversation_service``."""

    def __init__(self, bot):
        self.bot = bot

    # ─── Collaborators (read live so key rotation and reloads apply) ────────

    @property
    def state(self):
        return self.bot.state_manager

    @property
    def clients(self) -> Dict[str, Any]:
        return llm_clients(self.bot)

    # ─── Resolution ─────────────────────────────────────────────────────────

    def resolve(self, conversation: Conversation):
        """Returns (provider, model_name, client) for a conversation.

        Precedence: persona model > thread model > channel model > global.
        """
        provider, model_name = self.state.resolve_model(conversation.channel_id)
        if conversation.is_thread:
            thread_model = self._thread_config(conversation).get("model")
            persona = self._persona(conversation.channel_id)
            if thread_model and not (persona and persona.get("model")):
                provider, model_name = self._split_model(thread_model, provider)
        return provider, model_name, self.clients.get(provider)

    def _split_model(self, model_id: str, fallback_provider: str):
        if "/" in model_id:
            provider, name = model_id.split("/", 1)
            if provider in self.clients:
                return provider, name
        return fallback_provider, model_id

    def _persona(self, channel_id: str):
        getter = getattr(self.state, "get_effective_persona", None)
        try:
            return getter(channel_id) if getter else None
        except Exception:
            return None

    def _thread_config(self, conversation: Conversation) -> Dict[str, Any]:
        if not conversation.is_thread:
            return {}
        try:
            return self.state.get_discord_thread_config(conversation.thread_id) or {}
        except Exception as e:
            logger.error(f"[Conversation] Could not load thread config {conversation.thread_id}: {e}")
            return {}

    def _budget_error(self, author) -> Optional[str]:
        """Today's spend limit message for this author, or None."""
        if not hasattr(self.state, "get_daily_user_cost_budget"):
            return None
        return budget.chat_budget_error(self.state, author, self.bot)

    # ─── History ────────────────────────────────────────────────────────────

    def history(self, conversation: Conversation, until: Optional[datetime] = None) -> List[Dict[str, Any]]:
        if conversation.is_thread:
            return self.state.get_discord_thread_history(
                conversation.thread_id, hours_limit=self.state.get_time_window_hours(), until=until)
        return self.state.get_channel_history(conversation.channel_id, until=until)

    async def record(self, conversation: Conversation, entry: Dict[str, Any]) -> Optional[datetime]:
        """Stores one turn. Returns the timestamp it was stored with (None if skipped)."""
        if conversation.is_thread:
            return await self.state.add_discord_thread_message(conversation.thread_id, entry)
        return await self.state.add_to_channel_history(conversation.channel_id, {
            **entry, "timestamp": entry.get("timestamp") or datetime.now()})

    async def record_user_turn(self, conversation: Conversation, author, content: str) -> Optional[datetime]:
        """Records a human turn, rotating/compacting channel sessions around it."""
        if not conversation.is_thread:
            try:
                await check_and_rotate_session(conversation.channel_id, self.state, self.clients)
            except Exception as e:
                logger.error(f"[Conversation] Session rotation failed for {conversation.channel_id}: {e}",
                             exc_info=True)
        try:
            recorded_at = await self.record(conversation, {
                "role": "user",
                "name": author.display_name,
                "user_id": str(author.id),
                "is_bot": bool(getattr(author, "bot", False)),
                "content": content,
            })
        except Exception as e:
            logger.error(f"[Conversation] Failed to record user turn: {e}", exc_info=True)
            recorded_at = None
        if not conversation.is_thread:
            schedule_compaction(conversation.channel_id, self.state, self.clients)
        return recorded_at

    def system_prompt(self, conversation: Conversation, query: str, speaker_id: str,
                      history: List[Dict[str, Any]]) -> str:
        kwargs = {"query": query, "speaker_id": speaker_id, "history": history}
        base_prompt = self._thread_config(conversation).get("system_prompt")
        if base_prompt:
            kwargs["base_prompt"] = base_prompt
        return self.state.get_effective_system_prompt(conversation.channel_id, **kwargs)

    # ─── Images ─────────────────────────────────────────────────────────────

    async def collect_images(self, attachments, client, model_name: str, notify=None) -> List[Dict[str, Any]]:
        """Downloads image attachments when the model accepts image input."""
        candidates = [a for a in (attachments or [])
                      if getattr(a, "filename", "").lower().endswith(IMAGE_EXTENSIONS)]
        if not candidates or client is None or not hasattr(client, "model_supports_vision"):
            return []
        supports = client.model_supports_vision(model_name)
        if inspect.isawaitable(supports):
            supports = await supports
        if not supports:
            logger.info(f"[Conversation] Model '{model_name}' has no image input; ignoring attachments")
            return []
        images = []
        for attachment in candidates:
            try:
                images.append({"data": await attachment.read(),
                               "type": attachment.content_type or "image/jpeg"})
            except Exception as e:
                logger.warning(f"[Conversation] Could not read image {attachment.filename}: {e}")
                if notify:
                    await notify(f"⚠️ I couldn't read the image {attachment.filename}.")
        return images

    # ─── Generation ─────────────────────────────────────────────────────────

    async def generate(self, client, model_name: str, history: List[Dict[str, Any]],
                       system_prompt: str, images: Optional[List[Dict[str, Any]]] = None,
                       tool_context: Optional[Dict[str, Any]] = None) -> ChatResult:
        """One reply: the tool loop when ``tool_context`` is given, else a plain call."""
        if client is None:
            return ChatResult.failure(llm_result.UNAVAILABLE, model_name)
        if tool_context is not None:
            return await self._run_tool_loop(client, history, model_name, system_prompt,
                                             images, tool_context)
        result = await call_chat(client, messages=history, model=model_name,
                                 system_prompt=system_prompt, images=images or [])
        return result

    async def _run_tool_loop(self, client, conversation_context: List[Dict[str, Any]],
                             model_name: str, system_prompt: Optional[str],
                             images: Optional[List[Dict[str, Any]]],
                             tool_context: Dict[str, Any]) -> ChatResult:
        """Runs the model with tools, feeding results back until it produces text.

        Each round's tool results are appended to the conversation so the model
        can synthesize an answer, chain tools, or recover from a tool error.
        """
        tools = get_tool_definitions()
        max_iterations = self.state.get_tool_calling_max_iterations()
        executed_calls: set = set()

        for iteration in range(max_iterations):
            result = await call_chat(
                client, messages=conversation_context, model=model_name,
                system_prompt=system_prompt, images=images if iteration == 0 else None,
                tools=tools, tool_choice="auto")
            if not result.ok or not result.tool_calls:
                return result

            tool_calls = result.tool_calls
            logger.info(f"[Tool] Iteration {iteration + 1}/{max_iterations}: "
                        f"model requested {len(tool_calls)} tool call(s)")

            # Record the assistant's tool-call turn
            conversation_context.append({
                "role": "assistant",
                "content": result.content or "",
                "tool_calls": tool_calls,
            })

            for tc in tool_calls:
                func_name = tc.get("function", {}).get("name", "")
                func_args_str = tc.get("function", {}).get("arguments", "{}")
                try:
                    func_args = json.loads(func_args_str) if func_args_str else {}
                except json.JSONDecodeError as e:
                    logger.error(f"[Tool] Failed to parse tool arguments for '{func_name}': {e}")
                    func_args = {}

                # Side-effect tools must not run twice with identical arguments
                # in one reply — a repeat means the model ignored the earlier
                # result, so point it back at that instead of re-posting.
                call_key = ((func_name, json.dumps(func_args, sort_keys=True))
                            if is_no_repeat_tool(func_name) else None)
                if call_key and call_key in executed_calls:
                    logger.warning(f"[Tool] Skipping duplicate '{func_name}' call with identical arguments")
                    tool_result = (f"Duplicate call skipped: '{func_name}' already ran with these exact "
                                   f"arguments during this reply. Use its earlier result above to answer "
                                   f"the user now — do not call it again.")
                else:
                    tool_result = await execute_tool(func_name, func_args, tool_context)
                    # Record only successful runs so a transient failure can be retried
                    if call_key and not tool_result.startswith("Error"):
                        executed_calls.add(call_key)

                conversation_context.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": tool_result,
                })

        # Iteration budget exhausted — force a text answer (no tools offered)
        logger.warning(f"[Tool] Iteration budget ({max_iterations}) exhausted; forcing text response")
        return await call_chat(client, messages=conversation_context, model=model_name,
                               system_prompt=system_prompt)

    # ─── Sending ────────────────────────────────────────────────────────────

    async def send(self, conversation: Conversation, text: str,
                   history: Optional[List[Dict[str, Any]]] = None,
                   people: Optional[List[Any]] = None) -> List[Any]:
        """Sends a reply in Discord-sized chunks, via the persona webhook when active.

        '@Name' written by the model becomes a real ping for known participants.
        """
        targets = collect_mention_targets(history or [], [p for p in (people or []) if p])
        sent = []
        for chunk in chunk_message(apply_outbound_mentions(text, targets)):
            sent.append(await self.bot.webhook_sender.send_response(
                conversation.channel, chunk, conversation.channel_id))
        return sent

    async def send_error(self, conversation: Conversation, result: ChatResult):
        """Posts a failure as a short, user-facing message (never recorded)."""
        await conversation.channel.send(f"⚠️ {result.error or llm_result.friendly_error(llm_result.PROVIDER)}")

    # ─── The whole turn ─────────────────────────────────────────────────────

    async def respond(self, request: ReplyRequest) -> ChatResult:
        """Generates, records and sends the reply to one turn."""
        conversation = request.conversation
        speaker_id = str(request.author.id)

        with usage_scope(request.feature, user_id=speaker_id, channel_id=conversation.channel_id):
            provider, model_name, client = self.resolve(conversation)
            logger.info(f"[Conversation] Replying in {conversation.thread_id or conversation.channel_id} "
                        f"with {provider}/{model_name}")
            if client is None:
                result = ChatResult.failure(llm_result.UNAVAILABLE, model_name)
                await self.send_error(conversation, result)
                return result

            limit_error = self._budget_error(request.author)
            if limit_error:
                await conversation.channel.send(f"⛔ {limit_error}")
                return ChatResult.failure(llm_result.BUDGET, model_name)

            history = self.history(conversation, until=request.history_until)
            system_prompt = self.system_prompt(conversation, request.content, speaker_id, history)
            if not request.recorded:
                history.append({"role": "user", "name": request.author.display_name,
                                "user_id": speaker_id, "content": request.content})

            images = await self.collect_images(request.attachments, client, model_name,
                                               notify=conversation.channel.send)

            tool_context = None
            if request.use_tools and request.message is not None and self.state.get_tool_calling_enabled():
                tool_context = {
                    "bot": self.bot,
                    "state": self.state,
                    "message": request.message,
                    "channel_id": conversation.channel_id,
                    "conversation": conversation,
                    "history": list(history),
                    "clients": self.clients,
                    "cog": self.bot.get_cog("MentionCommands") if hasattr(self.bot, "get_cog") else None,
                }

            typing = getattr(conversation.channel, "typing", None)
            if typing is not None:
                async with typing():
                    result = await self.generate(client, model_name, history, system_prompt,
                                                 images, tool_context)
            else:
                result = await self.generate(client, model_name, history, system_prompt,
                                             images, tool_context)

            if not result.ok:
                await self.send_error(conversation, result)
                return result

            text = result.text.strip()
            if not text:
                # Tools may have done all the talking (image, poll, reminder)
                if tool_context is None:
                    logger.warning("[Conversation] Model returned an empty reply")
                return result

            await self.record(conversation, {"role": "assistant", "content": text})
            people = [request.author, *getattr(request.message, "mentions", []), *request.extra_people]
            await self.send(conversation, text, history, people)
            return result
