"""Shared helpers for turning stored chat history into LLM API messages.

The bot talks in multi-user Discord channels, so every user turn has to carry
the speaker's name or the model cannot tell participants apart. Labelling is
centralised here (and applied inside the LLM clients) so every code path —
mentions, threads, /chat, search, tool loops — gets it for free.
"""

import re
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Keys the chat-completions API accepts. Anything else stored alongside a
# message (timestamps, user ids, names) is bookkeeping and must not be sent.
_PASSTHROUGH_KEYS = ("tool_calls", "tool_call_id")

_USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
_ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
_CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")


def format_speaker_content(name: Optional[str], content: Any) -> Any:
    """Prefixes content with the speaker's name, e.g. 'Alice: hello'.

    Returns content unchanged when there is no name, when it is already
    prefixed with that name, or when it isn't plain text (e.g. multimodal
    content that has already been expanded into a list of parts).
    """
    if not name or not isinstance(content, str):
        return content

    prefix = f"{name}: "
    if content.startswith(prefix):
        return content
    return prefix + content


def prepare_llm_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalises stored history dicts into chat-completions API messages.

    - user turns with a stored name are prefixed 'Name: content' so the model
      can tell participants apart
    - only API-valid keys survive; bookkeeping keys are dropped
    - tool-calling keys are preserved so tool loops round-trip correctly

    Safe to call more than once on the same messages.
    """
    prepared: List[Dict[str, Any]] = []

    for message in messages or []:
        if not isinstance(message, dict):
            logger.warning(f"Skipping non-dict message in history: {type(message).__name__}")
            continue

        role = message.get("role")
        content = message.get("content")

        if role == "user":
            content = format_speaker_content(message.get("name"), content)

        clean: Dict[str, Any] = {"role": role, "content": content}
        for key in _PASSTHROUGH_KEYS:
            if message.get(key) is not None:
                clean[key] = message[key]

        prepared.append(clean)

    return prepared


def resolve_discord_mentions(message) -> str:
    """Returns message.content with mention tokens replaced by readable names.

    Discord stores mentions as raw '<@123456789>' tokens; left as-is the model
    sees opaque numeric ids instead of the people being talked about. Unlike
    discord.py's clean_content this leaves the rest of the text untouched (no
    markdown escaping, no zero-width space injected into @everyone).
    """
    content = message.content or ""

    members = {str(user.id): user for user in getattr(message, "mentions", []) or []}
    roles = {str(role.id): role for role in getattr(message, "role_mentions", []) or []}
    channels = {str(channel.id): channel for channel in getattr(message, "channel_mentions", []) or []}

    def replace_user(match):
        user = members.get(match.group(1))
        if not user:
            return match.group(0)
        return "@" + (getattr(user, "display_name", None) or user.name)

    def replace_role(match):
        role = roles.get(match.group(1))
        return f"@{role.name}" if role else match.group(0)

    def replace_channel(match):
        channel = channels.get(match.group(1))
        return f"#{channel.name}" if channel else match.group(0)

    content = _USER_MENTION_RE.sub(replace_user, content)
    content = _ROLE_MENTION_RE.sub(replace_role, content)
    content = _CHANNEL_MENTION_RE.sub(replace_channel, content)
    return content


def collect_mention_targets(history: List[Dict[str, Any]], users=()) -> Dict[str, str]:
    """Maps lowercase display names to user ids for everyone in the conversation.

    Sources are stored history rows (which keep each speaker's id and name)
    plus any extra Discord users, e.g. the triggering message's author and
    the people it mentioned. Bots are skipped so the model can't ping itself.
    """
    targets: Dict[str, str] = {}
    for row in history or []:
        name, user_id = row.get("name"), row.get("user_id")
        if row.get("role") == "user" and name and user_id:
            targets[name.lower()] = str(user_id)

    for user in users:
        if user is None or getattr(user, "bot", False):
            continue
        for name in {getattr(user, "display_name", None), getattr(user, "name", None)}:
            if name:
                targets[name.lower()] = str(user.id)
    return targets


def apply_outbound_mentions(text: str, targets: Dict[str, str]) -> str:
    """Turns '@Name' written by the model into a real '<@id>' Discord ping.

    The model only ever sees readable '@Name' text (resolve_discord_mentions
    strips the raw ids), so it can't produce a ping on its own. Only names of
    known conversation participants are converted; longest names are matched
    first so 'Ann Lee' wins over 'Ann', and a match must stand alone as a
    word ('@Anna' and 'me@ann.com' are left untouched).
    """
    if not text or not targets:
        return text

    names = sorted(targets, key=len, reverse=True)
    pattern = re.compile(
        r"(?<![\w@<])@(" + "|".join(re.escape(name) for name in names) + r")(?![\w])",
        re.IGNORECASE,
    )
    return pattern.sub(lambda m: f"<@{targets[m.group(1).lower()]}>", text)
