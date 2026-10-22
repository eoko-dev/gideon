"""Shared Discord message formatting helpers."""
from typing import List

import discord

# Gideon's replies carry model output and echoed user text, so they must never
# ping @everyone/@here or roles. User pings stay allowed: apply_outbound_mentions
# only produces them for known conversation participants.
SAFE_ALLOWED_MENTIONS = discord.AllowedMentions(
    everyone=False, roles=False, users=True, replied_user=True
)


# Room kept in each chunk for closing and re-opening a code fence
_FENCE_RESERVE = 24


def _split_lines(text: str, limit: int) -> List[str]:
    """Splits text into pieces of at most ``limit``, preferring line boundaries."""
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
    return chunks


def _open_fence(text: str, opening: str = None):
    """The fence line still open at the end of ``text`` (e.g. "```python"), or None."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            opening = None if opening is not None else stripped
    return opening


def chunk_message(text: str, limit: int = 2000) -> List[str]:
    """Splits text into Discord-sized chunks, preferring line boundaries.

    A code block cut across chunks is closed at the end of one chunk and
    re-opened (with its language) at the start of the next, so formatting
    survives the split. Every returned chunk fits within ``limit``.
    """
    if len(text) <= limit:
        return [text] if text else [""]

    pieces = _split_lines(text, limit - _FENCE_RESERVE)
    chunks: List[str] = []
    carried = None  # fence opened in an earlier chunk and still open
    for piece in pieces:
        chunk = (carried + "\n" + piece) if carried else piece
        still_open = _open_fence(piece, carried)
        if still_open:
            chunk = chunk.rstrip("\n") + "\n```"
        chunks.append(chunk)
        carried = still_open
    return chunks or [""]
