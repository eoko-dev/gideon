"""
Tool registry for native LLM tool calling.

Each tool is registered with the @tool decorator, pairing an OpenAI
function schema with its executor. Executors return a *data* string that is
fed back to the model as the tool result, so the model can synthesize a
final reply, chain tools, or recover from errors.

Side-effect tools (image generation, polls, events, reminders) also post
their own Discord output; their return value tells the model what happened so
it can acknowledge it without repeating it. Web search posts nothing — its
results are returned to the model, which weaves them into its normal reply
(only the /search command renders results as an embed). These tools are
registered with no_repeat=True so the tool loop skips identical repeat calls
instead of re-running expensive or channel-visible actions.
"""

import logging
from typing import Any, Awaitable, Callable, Dict, List

from . import user_memory

logger = logging.getLogger('tool_registry')

_TOOLS: Dict[str, Callable[..., Awaitable[str]]] = {}
TOOL_DEFINITIONS: List[Dict[str, Any]] = []
_NO_REPEAT_TOOLS: set = set()


def tool(schema: Dict[str, Any], no_repeat: bool = False):
    """Registers an async executor for the given OpenAI function schema.

    no_repeat: mark tools whose side effects (Discord posts, saved
    reminders, generated images) must not run twice for identical
    arguments within a single reply. The tool loop uses this to skip
    duplicate calls instead of re-executing them.
    """
    def wrap(fn: Callable[..., Awaitable[str]]):
        TOOL_DEFINITIONS.append({"type": "function", "function": schema})
        _TOOLS[schema["name"]] = fn
        if no_repeat:
            _NO_REPEAT_TOOLS.add(schema["name"])
        return fn
    return wrap


def is_no_repeat_tool(tool_name: str) -> bool:
    """True if identical repeat calls to this tool should be skipped."""
    return tool_name in _NO_REPEAT_TOOLS


def get_tool_definitions() -> List[Dict[str, Any]]:
    """Return the list of tool definitions for the LLM API."""
    return TOOL_DEFINITIONS


def get_tool_names() -> List[str]:
    """Return a list of all registered tool names."""
    return list(_TOOLS)


async def execute_tool(tool_name: str, arguments: Dict[str, Any],
                       context: Dict[str, Any]) -> str:
    """
    Execute a tool by name with the given arguments.

    Args:
        tool_name: Name of the tool to execute
        arguments: Parsed JSON arguments from the LLM
        context: Dictionary containing:
            - bot: Discord bot instance
            - state: BotStateManager instance
            - message: Discord message object
            - channel_id: Channel ID as string
            - clients: Dict of provider clients
            - cog: MentionCommands cog (for side-effect handlers)

    Returns:
        String result to feed back to the LLM as the tool response.
    """
    fn = _TOOLS.get(tool_name)
    if fn is None:
        return f"Error: Unknown tool '{tool_name}'"

    logger.info(f"[Tool] Executing tool '{tool_name}' with args: {arguments}")
    try:
        return await fn(arguments, context)
    except ValueError as e:
        # Expected validation errors — phrased for the model to relay
        return f"Error: {e}"
    except Exception as e:
        logger.exception(f"[Tool] Error executing tool '{tool_name}': {e}")
        return f"Error executing tool '{tool_name}': {e}"


def _resolve_chat_client(ctx: Dict[str, Any]):
    """Returns (client, model_name) for the channel's effective model."""
    state = ctx["state"]
    channel_id = ctx["channel_id"]
    provider, model_name = state.resolve_model(channel_id)
    client = ctx.get("clients", {}).get(provider)
    if client is None or not hasattr(client, "send_message_with_history"):
        raise ValueError(f"Chat client for provider '{provider}' is not available.")
    return client, model_name


# ─── Side-effect tools (post their own Discord output) ──────────────────────

@tool({
    "name": "set_reminder",
    "description": "Set a reminder for the user. Use when the user asks to be reminded about something at a future time.",
    "parameters": {
        "type": "object",
        "properties": {
            "reminder_message": {
                "type": "string",
                "description": "What to remind the user about"
            },
            "time_expression": {
                "type": "string",
                "description": "When to send the reminder, as a natural language time expression (e.g., 'in 2 hours', 'tomorrow at 3pm', 'at 5pm')"
            }
        },
        "required": ["reminder_message", "time_expression"]
    }
}, no_repeat=True)
async def _set_reminder(args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    cog = ctx.get("cog")
    if not cog or not hasattr(cog, "handle_reminder_request"):
        return "Error: Reminder handler not available"

    reminder_message = args.get("reminder_message", "")
    time_expression = args.get("time_expression", "")
    await cog.handle_reminder_request(
        ctx["message"], ctx["channel_id"], reminder_message, time_expression
    )
    return (f"Reminder saved and confirmation posted in the channel: "
            f"'{reminder_message}' at '{time_expression}'.")


@tool({
    "name": "generate_image",
    "description": "Generate an AI image. Use when the user asks to draw, create, or generate a picture or image.",
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The main description of what to generate"
            },
            "negative_prompt": {
                "type": "string",
                "description": "What to exclude from the image (optional)"
            },
            "size": {
                "type": "string",
                "description": "Dimensions if specified (e.g., '512x512', '1024x1024', 'landscape', 'portrait'). Empty string if not specified."
            },
            "quality": {
                "type": "string",
                "description": "Quality setting: 'hd', 'high quality', or 'standard'. Empty string if not specified."
            },
            "style": {
                "type": "string",
                "description": "Style preference: 'vivid', 'natural', 'realistic', 'artistic'. Empty string if not specified."
            }
        },
        "required": ["prompt"]
    }
}, no_repeat=True)
async def _generate_image(args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    cog = ctx.get("cog")
    if not cog or not hasattr(cog, "handle_image_generation_request"):
        return "Error: Image generation handler not available"

    prompt = args.get("prompt", "")
    await cog.handle_image_generation_request(
        ctx["message"], ctx["channel_id"],
        prompt,
        args.get("negative_prompt", ""),
        args.get("size", ""),
        args.get("quality", ""),
        args.get("style", "")
    )
    return f"Image generated and posted in the channel for prompt: '{prompt}'."


@tool({
    "name": "web_search",
    "description": "Search the web for current/real-time information. Use for time-sensitive queries, current events, news, weather, stock prices, etc.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query"
            }
        },
        "required": ["query"]
    }
}, no_repeat=True)
async def _web_search(args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    cog = ctx.get("cog")
    if not cog or not hasattr(cog, "handle_search_request"):
        return "Error: Search handler not available"

    query = args.get("query", "")
    # Returns the actual search results (or why the search didn't run) for
    # the model to weave into its reply; nothing is posted to the channel.
    return await cog.handle_search_request(ctx["message"], ctx["channel_id"], query,
                                           history=ctx.get("history"))


@tool({
    "name": "create_poll",
    "description": "Create a poll or vote in Discord. Use when the user wants to create a poll with a question and options.",
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The poll question"
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of poll options (2-10 options)"
            },
            "duration": {
                "type": "integer",
                "description": "Poll duration in hours (default: 24)"
            }
        },
        "required": ["question", "options"]
    }
}, no_repeat=True)
async def _create_poll(args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    from .intent_handlers.poll import handle_poll_creation

    cog = ctx.get("cog")
    question = args.get("question", "")
    options = args.get("options", [])
    await handle_poll_creation(
        cog, ctx["message"], ctx["channel_id"],
        question, options, args.get("duration", 24)
    )
    return f"Poll posted in the channel: '{question}' with {len(options)} options."


@tool({
    "name": "schedule_event",
    "description": "Create a scheduled Discord event. Use when the user wants to schedule or create an event with a specific time.",
    "parameters": {
        "type": "object",
        "properties": {
            "event_name": {
                "type": "string",
                "description": "Name of the event (e.g., 'movie night', 'gaming', 'team meeting')"
            },
            "date_time": {
                "type": "string",
                "description": "When the event occurs, as a natural language expression (e.g., 'Friday 8pm', 'tomorrow at 2pm')"
            },
            "duration": {
                "type": "integer",
                "description": "Event duration in minutes (default: 60)"
            },
            "description": {
                "type": "string",
                "description": "Event details or description (optional)"
            }
        },
        "required": ["event_name", "date_time"]
    }
}, no_repeat=True)
async def _schedule_event(args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    from .intent_handlers.event import handle_event_scheduling

    cog = ctx.get("cog")
    event_name = args.get("event_name", "")
    date_time = args.get("date_time", "")
    await handle_event_scheduling(
        cog, ctx["message"], ctx["channel_id"],
        event_name, date_time,
        args.get("duration", 60), args.get("description", "")
    )
    return f"Event scheduling handled for '{event_name}' at {date_time}; confirmation posted in the channel."


# ─── Pure tools (return data; the model phrases the reply) ──────────────────

@tool({
    "name": "calculate",
    "description": "Evaluate a mathematical expression. Use for arithmetic, percentages, square roots, powers, etc.",
    "parameters": {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "The mathematical expression to evaluate (e.g., '15% of 250', '45 * 89', 'sqrt(144)', '2^8')"
            }
        },
        "required": ["expression"]
    }
})
async def _calculate(args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    from .intent_handlers.calculation import evaluate_expression
    return evaluate_expression(args.get("expression", ""))


@tool({
    "name": "roll_dice",
    "description": "Roll dice, flip coins, pick random options, or generate random numbers. Use for any random selection or dice rolling request.",
    "parameters": {
        "type": "object",
        "properties": {
            "dice_notation": {
                "type": "string",
                "description": "Standard dice notation (e.g., '2d20', '1d6'). Empty string if not applicable."
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of options to choose from (for 'pick one' scenarios). Empty array if not applicable."
            },
            "range_min": {
                "type": "integer",
                "description": "Minimum value for random number generation (null if not applicable)"
            },
            "range_max": {
                "type": "integer",
                "description": "Maximum value for random number generation (null if not applicable)"
            }
        },
        "required": []
    }
})
async def _roll_dice(args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    from .intent_handlers.dice import roll
    return roll(
        args.get("dice_notation", ""),
        args.get("options", []),
        args.get("range_min"),
        args.get("range_max")
    )


@tool({
    "name": "convert_timezone",
    "description": "Convert a time from one timezone to another. Use when the user wants to know what time something is in a different timezone.",
    "parameters": {
        "type": "object",
        "properties": {
            "time": {
                "type": "string",
                "description": "The time to convert (e.g., '3pm', '14:00')"
            },
            "source_timezone": {
                "type": "string",
                "description": "Source timezone (e.g., 'EST', 'UTC', 'Tokyo')"
            },
            "target_timezone": {
                "type": "string",
                "description": "Target timezone (e.g., 'PST', 'London', 'Tokyo')"
            }
        },
        "required": ["time", "source_timezone", "target_timezone"]
    }
})
async def _convert_timezone(args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    from .intent_handlers.timezone import convert_time
    return convert_time(
        args.get("time", ""),
        args.get("source_timezone", ""),
        args.get("target_timezone", "")
    )


@tool({
    "name": "convert_units",
    "description": "Convert between units (distance, temperature, currency, etc.). Use when the user wants to convert a value from one unit to another.",
    "parameters": {
        "type": "object",
        "properties": {
            "value": {
                "type": "number",
                "description": "The numeric value to convert"
            },
            "source_unit": {
                "type": "string",
                "description": "Source unit (e.g., 'miles', 'F', 'USD')"
            },
            "target_unit": {
                "type": "string",
                "description": "Target unit (e.g., 'km', 'celsius', 'EUR')"
            },
            "category": {
                "type": "string",
                "description": "Unit category hint: 'distance', 'temperature', 'currency', etc."
            }
        },
        "required": ["value", "source_unit", "target_unit"]
    }
})
async def _convert_units(args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    from .intent_handlers.unit_conversion import convert_units
    client, model_name = _resolve_chat_client(ctx)
    return await convert_units(
        client, model_name,
        args.get("value", ""),
        args.get("source_unit", ""),
        args.get("target_unit", ""),
        args.get("category", "")
    )


@tool({
    "name": "translate",
    "description": "Translate text between languages. Use when the user wants to translate specific text.",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The text to translate"
            },
            "source_language": {
                "type": "string",
                "description": "Source language (empty string if unknown, for auto-detection)"
            },
            "target_language": {
                "type": "string",
                "description": "Target language to translate to"
            }
        },
        "required": ["text", "target_language"]
    }
})
async def _translate(args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    from .intent_handlers.translation import translate_text
    client, model_name = _resolve_chat_client(ctx)
    return await translate_text(
        client, model_name,
        args.get("text", ""),
        args.get("source_language", ""),
        args.get("target_language", "")
    )


@tool({
    "name": "define",
    "description": "Look up a factual definition or explanation of a term. Use for timeless knowledge, general concepts, people, etc.",
    "parameters": {
        "type": "object",
        "properties": {
            "term": {
                "type": "string",
                "description": "The term or concept to define"
            },
            "depth": {
                "type": "string",
                "enum": ["brief", "detailed"],
                "description": "How detailed the definition should be"
            }
        },
        "required": ["term"]
    }
})
async def _define(args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    from .intent_handlers.definition import define_term
    client, model_name = _resolve_chat_client(ctx)
    return await define_term(client, model_name, args.get("term", ""), args.get("depth", "brief"))


# ─── Per-user memory (scoped to the requesting user in this channel) ────────
#
# Neither schema takes a user or channel: the key always comes from the
# message author and the channel the conversation happens in, so the model
# can never read or write anyone else's notes, or another channel's.

def _memory_owner(ctx: Dict[str, Any]):
    """Returns (state, channel_id, author) for the requesting user, or raises ValueError."""
    state, channel_id, author = ctx["state"], ctx["channel_id"], ctx["message"].author
    if state.is_user_memory_opted_out(str(author.id)):
        raise ValueError("This user has opted out of per-user memory, so nothing can be stored "
                         "about them. They can run /memory optin to allow it.")
    if not state.get_effective_user_memory_enabled(channel_id):
        raise ValueError("Per-user memory is disabled in this channel.")
    return state, channel_id, author


@tool({
    "name": "remember_about_me",
    "description": ("Save a lasting fact or preference that the user explicitly asks you to remember about "
                    "themselves (e.g. 'remember that I prefer metric units'). Only for the person speaking, "
                    "never about someone else. Notes are kept for this channel only."),
    "parameters": {
        "type": "object",
        "properties": {
            "fact": {
                "type": "string",
                "description": "The fact to remember, as one short sentence about the user"
            }
        },
        "required": ["fact"]
    }
}, no_repeat=True)
async def _remember_about_me(args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    fact = " ".join(str(args.get("fact", "")).split())
    if not fact:
        raise ValueError("No fact was given to remember.")
    state, channel_id, author = _memory_owner(ctx)
    row = state.get_user_notes(channel_id, str(author.id))
    bullets = user_memory.split_notes(row["notes"] if row else "") + [fact]
    state.set_user_notes(channel_id, str(author.id), user_memory.join_notes(bullets),
                         display_name=author.display_name)
    return f"Saved to {author.display_name}'s notes for this channel: {fact}"


@tool({
    "name": "forget_about_me",
    "description": ("Delete notes about the user when they ask you to forget something about them, or "
                    "everything. Only affects the person speaking, in this channel."),
    "parameters": {
        "type": "object",
        "properties": {
            "fact": {
                "type": "string",
                "description": "Words identifying the note(s) to forget; omit to forget everything about the user here"
            }
        }
    }
}, no_repeat=True)
async def _forget_about_me(args: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    state, channel_id, author = ctx["state"], ctx["channel_id"], ctx["message"].author
    user_id = str(author.id)
    target = " ".join(str(args.get("fact") or "").split()).lower()
    if not target:
        deleted = state.delete_user_notes(channel_id, user_id)
        return ("Deleted all of the user's notes for this channel." if deleted
                else "There were no notes about the user in this channel.")

    row = state.get_user_notes(channel_id, user_id)
    bullets = user_memory.split_notes(row["notes"] if row else "")
    kept = [b for b in bullets if target not in b.lower()]
    if len(kept) == len(bullets):
        return "No note about the user in this channel matched that."
    if kept:
        state.set_user_notes(channel_id, user_id, user_memory.join_notes(kept), display_name=author.display_name)
    else:
        state.delete_user_notes(channel_id, user_id)
    return f"Forgot {len(bullets) - len(kept)} note(s) about the user in this channel."
