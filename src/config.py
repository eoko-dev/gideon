"""Configuration for the bot."""
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Bot token from Discord
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

# OpenRouter API Key
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')

# Default system prompt
SYSTEM_PROMPT = os.getenv('SYSTEM_PROMPT', """You are a helpful AI assistant named Gideon. You provide clear, accurate, and thoughtful responses. You strive to be helpful, but you'll acknowledge when you don't know something. Messages are labelled with each sender's name, so use those labels to keep speakers straight and address people by name.

You should adapt your tone to be conversational and friendly, while maintaining professionalism. You aim to be concise but thorough, providing sufficient context without unnecessary verbosity.

You are powered by the Openrouter API and have access to a variety of models to assist you in your responses. You can provide information, answer questions, and engage in conversation with users. You can also provide recommendations, summaries, and explanations on a wide range of topics.
""")

# Get allowed models from environment (comma-separated string)
allowed_models_str = os.getenv('ALLOWED_MODELS', "openai:gpt-4o-mini,openai:gpt-4o,anthropic:claude-3.7-sonnet,perplexity:sonar-pro,google:gemini-2.0-flash-exp")
ALLOWED_MODELS = [model.strip().replace('/', ':') for model in allowed_models_str.split(',') if model.strip()]

# Default model to use (must include provider in provider/model_name format)
# Default model (provider/model_name). The provider must have a client (openrouter, openai, ai_horde).
DEFAULT_MODEL = os.getenv('DEFAULT_MODEL', 'openrouter/openai/gpt-4o-mini')

# Data storage configuration
DATA_DIRECTORY = os.getenv("DATA_DIRECTORY", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))

# Ensure DATA_DIRECTORY is an absolute path
if not os.path.isabs(DATA_DIRECTORY):
    # If relative path is provided, make it absolute based on the script location
    DATA_DIRECTORY = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), DATA_DIRECTORY))

# AI Horde Configuration
AI_HORDE_API_KEY = os.getenv('AI_HORDE_API_KEY', '')

# Cloudflare Worker Configuration
CLOUDFLARE_WORKER_URL = os.getenv('CLOUDFLARE_WORKER_URL', 'https://your-worker-url.workers.dev/')
CLOUDFLARE_API_KEY = os.getenv('CLOUDFLARE_API_KEY', '')

# OpenAI Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')

# ComfyUI Configuration
# URL of your ComfyUI server (local or remote)
# Examples: http://127.0.0.1:8188 or http://your-server.com:8188
COMFYUI_URL = os.getenv('COMFYUI_URL', '')

# ─── Native Tool Calling ────────────────────────────────────────────────────
# Replaces the old intent classification system. When enabled, the primary LLM
# model receives tool definitions and decides natively whether to call a tool
# (reminder, image generation, calculation, etc.) or respond conversationally.
# This eliminates the need for a separate classification LLM call.
#
# The INTENT_DISCOVERY env var is kept for backward compatibility — if set to
# TRUE, it enables tool calling. New installs should use TOOL_CALLING_ENABLED.
# On by default: only @mentions, /chat and threads offer tools. The .env value
# seeds the database on first start; after that the dashboard/`/settings
# tools toggle` value wins.
TOOL_CALLING_ENABLED = os.getenv('TOOL_CALLING_ENABLED', 'TRUE').upper() == 'TRUE'
INTENT_DISCOVERY = os.getenv('INTENT_DISCOVERY', 'FALSE').upper() == 'TRUE'

# Combined flag: tool calling is enabled if either new or old env var is TRUE
# (backward compatibility: existing INTENT_DISCOVERY=TRUE users get tool calling)
CONFIG_TOOL_CALLING_ENABLED = TOOL_CALLING_ENABLED or INTENT_DISCOVERY

# Maximum number of tool-call round-trips before forcing a text response
# Prevents infinite loops if the model keeps calling tools
try:
    TOOL_CALLING_MAX_ITERATIONS = int(os.getenv('TOOL_CALLING_MAX_ITERATIONS', '3'))
except ValueError:
    TOOL_CALLING_MAX_ITERATIONS = 3

# Dashboard Configuration
DASHBOARD_ENABLED = os.getenv('DASHBOARD_ENABLED', 'FALSE').upper() == 'TRUE'
DASHBOARD_PORT = int(os.getenv('DASHBOARD_PORT', '8080'))
DASHBOARD_SECRET = os.getenv('DASHBOARD_SECRET', '')

# Encryption Master Key for API key storage (required for dashboard key management)
ENCRYPTION_MASTER_KEY = os.getenv('ENCRYPTION_MASTER_KEY', '')