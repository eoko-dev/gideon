"""Tool implementation modules for native LLM tool calling.

Pure tools expose compute functions that return data for the model;
side-effect tools (polls, events) expose handlers that post to Discord.
"""

from .calculation import evaluate_expression
from .poll import handle_poll_creation
from .timezone import convert_time
from .dice import roll
from .event import handle_event_scheduling

__all__ = [
    'evaluate_expression',
    'handle_poll_creation',
    'convert_time',
    'roll',
    'handle_event_scheduling',
]
