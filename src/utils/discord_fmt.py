"""Shared Discord message formatting helpers."""
from typing import List

import discord

# Gideon's replies carry model output and echoed user text, so they must never
# ping @everyone/@here or roles. User pings stay allowed: apply_outbound_mentions
# only produces them for known conversation participants.
SAFE_ALLOWED_MENTIONS = discord.AllowedMentions(
    everyone=False, roles=False, users=True, replied_user=True
)


def chunk_message(text: str, limit: int = 2000) -> List[str]:
    """Splits text into Discord-sized chunks, preferring line boundaries.

    Falls back to a hard split for single lines longer than the limit, so
    every returned chunk is guaranteed to fit.
    """
    if len(text) <= limit:
        return [text] if text else [""]

    chunks: List[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        # Hard-split pathological single lines that exceed the limit on their own
        while len(line) > limit:
            space = limit - len(current)
            current += line[:space]
            chunks.append(current)
            current = ""
            line = line[space:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks or [""]
