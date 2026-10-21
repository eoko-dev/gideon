<div align="center">

# 🤖 Gideon - AI Assistant for Discord

<img src="assets/images/gideon-logo.jpeg" alt="Gideon Logo" width="400"/>

<a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.8+-blue.svg?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.8+"></a>
<a href="https://github.com/Pycord-Development/pycord"><img src="https://img.shields.io/badge/py--cord-2.4+-5865F2.svg?style=for-the-badge&logo=discord&logoColor=white" alt="Py-Cord 2.4+"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg?style=for-the-badge" alt="License MIT"></a>

*Your server's intelligent companion powered by cutting-edge AI models*

[Installation](#-installation) • 
[Features](#-features) • 
[Commands](#-commands) • 
[Models](#-supported-models) • 
[Troubleshooting](#-troubleshooting) •
[Developer Guide](CLAUDE.MD)

</div>

## 🌟 Overview

Gideon transforms your Discord server into an AI-powered hub, connecting members to state-of-the-art language and image models. With Gideon, users can have intelligent conversations, generate creative images, analyze visual content, manage URLs, play AI-powered trivia games, and organize discussions through an intuitive thread system.

## ✨ Features

### 🧠 Intelligence

* **Multiple AI Models** - Access OpenAI, Anthropic Claude, Google Gemini, and more through OpenRouter
* **Persistent Channel Memory** - Long-term, channel-specific memory that grows over time. When a channel goes quiet for a configurable period (default 24 hours), Gideon automatically summarizes the conversation and stores it as a memory entry. Future conversations in that channel are informed by all past summaries — injected into the AI's context on every message — so the bot never loses track of recurring topics, decisions, or channel history. Memories are channel-isolated: `#general` never bleeds into `#dev`. Configurable via the dashboard or per-channel overrides.
* **Per-User Memory, Per Channel** - Gideon also keeps short notes about each person (preferences, projects, how they like answers), separately in every channel: what it learns about you in `#dev` is never used in `#general`. Threads share their parent channel's notes. Say "@Gideon remember that I prefer metric" to add one; see yours with `/memory me`, delete them with `/memory forget`, or opt out everywhere with `/memory optout`.
* **Native Tool Calling** - Simply @mention Gideon with natural language to automatically execute commands without needing to remember slash command syntax. The primary LLM model natively decides whether to call a tool or respond conversationally — no separate intent classification step needed:
  + **Reminders**: "@Gideon remind me to check the server logs tomorrow at 3pm"
  + **Image Generation**: "@Gideon draw a sunset over mountains with vibrant colors"
  + **Web Search**: "@Gideon what are the current best games on Xbox Game Pass"
  + **Calculations**: "@Gideon what's 15% of 250?"
  + **Translations**: "@Gideon translate 'hello world' to French"
  + **Definitions**: "@Gideon define serendipity"
  + **Polls**: "@Gideon create a poll: pizza vs burgers vs tacos"
  + **Timezone Conversion**: "@Gideon what's 3pm EST in Tokyo?"
  + **Unit Conversion**: "@Gideon convert 5 miles to kilometers"
  + **Dice Rolls**: "@Gideon roll 3d6"
  + **Events**: "@Gideon schedule a team meeting next Friday at 2pm"
  + **Multi-Tool**: "@Gideon calculate 15% of 250 and translate the result to French"
  + **Conversation**: Any other @mention automatically engages in natural conversation
* **Web Search** - Search the web for current information through conversational AI responses or natural language (@mention)
* **Image Generation** - Create stunning visuals using the unified `/dream` command or natural language @mentions. Supports multiple backend providers (AI Horde, Cloudflare Worker, OpenAI DALL-E, ComfyUI, OpenRouter). Admins can configure the active provider and its default settings.
* **Video Generation** - Generate AI videos with the `/video` command or directly from the dashboard, powered by OpenRouter's asynchronous video API. Supports multiple models (Veo 3.1, Sora 2 Pro, Seedance 2.0/1.5, Wan 2.7/2.6, Kling O1, etc.) with configurable duration, aspect ratio, resolution (480p, 720p, 1080p, 2K, or 4K), audio, seed, and optional reference image (image-to-video).

### 🎭 Per-Channel Personas

Give Gideon a unique identity in every channel using Discord webhooks. Each persona can have its own display name, avatar, and system prompt — letting you tailor the bot's personality to different communities, topics, or server roles.

* **Custom Personas** — Set a unique name, avatar URL, and system prompt per channel
* **Built-in Templates** — Get started quickly with seeded templates: *Helpful Assistant*, *Tech Guru*, and *Creative Writer*
* **Template Inheritance** — Channel personas can reference templates without being locked to them; template updates don't overwrite existing configurations
* **Webhook-Powered** — Responses route through Discord webhooks so the persona appears as its own identity, not the bot account
* **Automatic Fallback** — If webhook creation fails (e.g., missing permissions), Gideon falls back to the bot account gracefully
* **Thread Support** — Personas inherit from parent channels in threads

**Commands**: `/persona set`, `/persona template`, `/persona view`, `/persona list`, `/persona toggle`, `/persona remove`, `/persona preview`, `/persona templates`

### 🎮 Entertainment

* **AI-Powered Trivia** - Play interactive trivia games with questions generated dynamically by AI
  + **Solo Mode**: Test your knowledge in a personal trivia session
  + **Competitive Mode**: Race against other players for the highest score
  + **Dynamic Categories**: Ask questions about ANY topic - the AI generates questions on-demand
  + **Smart Scoring**: Earn points based on difficulty, speed, and answer streaks
  + **Achievements**: Unlock badges for milestones like perfect games, speed records, and win streaks
  + **Leaderboards**: Compete on daily, weekly, monthly, and all-time rankings
  + **Natural Gameplay**: Just type your answer directly in the thread - no complex commands needed!

### 🧵 Organization

* **Conversation Threads** - Create dedicated topics with independent histories using Discord's native threads
* **Auto-Responses** - Bot automatically responds to all messages in AI threads
* **Dynamic URL Summarization** - Instantly extracts and distills key information from any shared webpage

### 🖥️ Admin Dashboard

* **Web-Based Management** - Full browser-based admin dashboard as an alternative to Discord slash commands
* **Real-Time Monitoring** - Live activity feed with WebSocket-powered updates
* **API Key Management** - Add, edit, validate, and delete API keys for all providers via the dashboard, with Fernet-encrypted database storage and full audit logging
* **Personas Tab** - Browse, create, edit, and apply channel personas and templates from the browser
* **Backup & Restore** - Export and import your configuration (JSON) or full database (SQLite) directly from the dashboard
* **Settings Management** - Edit global settings, channel overrides, session timeout, auto-summarize toggle, and tool calling from the browser
* **Thread & Channel Management** - View, edit, and delete threads and channel configurations
* **System Diagnostics** - Run diagnostics, check provider status, and trigger data pruning
* **Secure Authentication** - Token-based authentication with configurable secret key
* **Theme Support** - Dark and light mode toggle with persistent preference

### 💾 Backup & Restore

Protect your bot's configuration and data with a full backup and restore system, all accessible from the web dashboard.

* **Configuration Backups (JSON)** — Export all configuration tables; API keys and webhook credentials are excluded from exports for security, but webhook credentials are re-applied automatically on import
* **Full Database Backups (SQLite)** — Complete database snapshot using SQLite's online backup API
* **Pre-import Validation** — Multi-level validation (structure, required fields, schema version) before any data is applied
* **Transaction Safety** — Config imports are wrapped in database transactions to ensure atomicity
* **File Limits** — 10 MB for JSON config exports, 100 MB for full database backups

### 🛠️ Customization

* **Model Switching** - Change AI models on-the-fly with simple commands
* **Channel Personalities** - Set different system prompts per channel, or go further with full per-channel personas
* **Admin Controls** - Comprehensive configuration options for server admins

## 🚀 Installation

There are two ways to install and run Gideon: using Docker (recommended for ease of deployment and management) or directly with Python.

### Prerequisites

**For both methods:**

* Git installed
* Discord bot token with Message Content Intent enabled ([Discord Developer Portal](https://discord.com/developers/applications))
* OpenRouter API key ([OpenRouter.ai](https://openrouter.ai/))
* AI Horde API key (optional, for `/dream` command - [AI Horde](https://aihorde.net/register))
* Cloudflare Worker URL & API Key (optional, requires self-setup — see [Cloudflare Worker Configuration](#cloudflare-worker-configuration-optional))

**For Docker Installation:**

* Docker installed
* Docker Compose installed (usually included with Docker Desktop)

**For Python Installation:**

* Python 3.8+

### Docker Installation (Recommended)

1. **Clone the Repository:**

   ```bash
   git clone https://github.com/eoko-dev/gideon
   cd gideon
   ```

2. **Configure Environment:**

   ```bash
   cp .env.example .env
   # Edit .env with your API keys and tokens
   ```

   Leave `DATA_DIRECTORY` blank or commented out when using Docker Compose — the volume mount handles data persistence.

3. **Build the Docker Image:**

   ```bash
   docker build -t gideon-bot:latest .
   ```

4. **Configure Docker Compose:**

   Open `docker-compose.yml` and verify the `volumes` section:

   ```yaml
   volumes:
     - ./gideon_data:/app/data
   ```

   Ensure the host path is writable by Docker.

5. **Run the Container:**

   ```bash
   docker-compose up -d
   docker-compose logs -f   # View logs
   docker-compose down      # Stop bot
   ```

### Python Installation

```bash
# Clone and enter repository
git clone https://github.com/eoko-dev/gideon
cd gideon

# Set up environment and dependencies
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt

# Configure bot
cp .env.example .env
# Edit .env with your Discord token, API keys,
# and set DATA_DIRECTORY to an absolute path where the bot can write data.
# Example: DATA_DIRECTORY=/home/user/gideon_data

# Launch
python3 -m src
```

### Discord Configuration

1. Create an application at [Discord Developer Portal](https://discord.com/developers/applications)
2. Under "Bot" tab:
   * Enable "Message Content Intent"
   * Copy your bot token for the `.env` file
3. Generate invite URL in "OAuth2 > URL Generator":
   * Scopes: `bot`, `applications.commands`
   * Permissions: Send Messages, Read Message History, Embed Links, Use Slash Commands, Manage Threads, **Manage Webhooks** (required for persona feature)

### Tool Calling Configuration (Optional)

Gideon's native tool calling feature allows users to interact naturally with the bot through @mentions instead of slash commands. The primary LLM model receives tool definitions and decides natively whether to call a tool or respond conversationally — no separate intent classification step needed. Enabled by default.

**Environment Variables:**

```env
TOOL_CALLING_ENABLED=TRUE
TOOL_CALLING_MAX_ITERATIONS=3
```

> **Backward Compatibility:** The legacy `INTENT_DISCOVERY=TRUE` env var still works as an alias for `TOOL_CALLING_ENABLED`. The old `INTENT_DETECTION_MODEL` and `INTENT_CONFIDENCE_THRESHOLD` vars are deprecated and no longer used.

**How It Works:**
When you @mention Gideon, the bot sends your message to the primary LLM model with tool definitions attached:

1. The model decides natively whether to call a tool or respond conversationally
2. If the model requests tool calls, each tool is executed and results are fed back
3. The model generates a final text response incorporating the tool results
4. If tool calling is disabled, falls back to plain conversation (no tools)

**Available Tools:** `set_reminder`, `generate_image`, `web_search`, `calculate`, `translate`, `define`, `create_poll`, `convert_timezone`, `convert_units`, `roll_dice`, `schedule_event`

**Key advantages over the old intent classification system:**
- Single LLM call instead of two (classification + response)
- No separate classification model or confidence threshold
- Supports multi-tool requests (e.g., "calculate 15% of 250 and translate the result to French")
- Uses full conversation context (the model sees chat history)

### Admin Dashboard Configuration (Optional)

Gideon includes a web-based admin dashboard that provides a browser interface for managing all bot settings, personas, backup/restore, channels, threads, and diagnostics.

**⚠️ WARNING: This feature is experimental and should NOT be exposed to the public internet without additional authentication measures in place.**

**Environment Variables:**

```env
DASHBOARD_ENABLED=TRUE
DASHBOARD_PORT=8080
DASHBOARD_SECRET=your_secret_here
```

**Quick Start:**

1. Generate a secret: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
2. Add to `.env` and restart the bot
3. Open `http://localhost:8080` in your browser
4. Log in with the dashboard secret

**Docker:** The dashboard port is automatically exposed via docker-compose.

**API Key Management (Optional):**

The dashboard supports encrypted API key storage in the database. To enable:

1. Generate an encryption key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
2. Add to `.env`: `ENCRYPTION_MASTER_KEY=your_generated_key`
3. In the dashboard, go to the **API Keys** tab to manage keys

Keys stored in the database take priority over `.env` values. This is fully backward compatible — the bot works without `ENCRYPTION_MASTER_KEY` set.

**Security model:**

The dashboard listens on all interfaces (`0.0.0.0`) by design and is meant for internal, secured networks only — never expose it to the public internet.

* **Login lockout** — after 5 failed attempts from an IP, logins are refused for 90 seconds; each further lockout doubles (up to 24 hours). A successful login resets the counter.
* **Sessions** — random 24-hour session tokens in an HttpOnly, `SameSite=Strict` cookie (marked `Secure` when served over HTTPS). The WebSocket feed authenticates with the same cookie and only accepts same-origin connections.
* **Browser hardening** — every response carries a Content-Security-Policy (no inline scripts), `X-Frame-Options: DENY`, `nosniff` and `Referrer-Policy: no-referrer`; API responses are never cached.
* **Backups** — database exports and uploaded files use unique temporary files that are deleted as soon as the request finishes.

Dashboard tests run with the rest of the suite: `python -m unittest discover -s tests`.

**Dashboard Sections:**

Press **Ctrl/⌘ + K** anywhere to jump to a tab, setting, channel, thread or persona.

| Group | Tab | Description |
| --- | --- | --- |
| Core | Overview | Bot status, provider and API key health, recent errors, uptime, latency, quick actions (sync slash commands) |
| Core | Settings | Global model, provider, system prompt, history limits, memory/session settings and tool calling |
| Core | API Keys | Encrypted API key management with test-before-save, import from `.env`, and audit logging |
| Conversations | Channels | Per-channel model, provider, prompt and memory overrides, showing where each effective value comes from (global, channel or persona) |
| Conversations | Threads | Manage AI conversation threads (rename, configure, delete) |
| Conversations | Personas | Browse templates, configure per-channel personas, create custom templates |
| Conversations | Memory | Browse and manage long-term channel memories; clear memories or trigger summarization |
| Conversations | Messages | Browse stored conversation history with filtering and pagination |
| Generation | Image Gen | Active image provider, per-provider settings and OpenRouter modalities |
| Generation | Video Gen | Video model defaults and a test-generation panel |
| Operations | Diagnostics | System health checks, provider status, manual data pruning |
| Operations | Backup & Restore | Export/import configuration JSON or full SQLite database backup |
| Operations | Live Activity | Real-time WebSocket feed of bot events and message activity |

### Persona Configuration (Optional)

To use per-channel personas, the bot requires the **Manage Webhooks** permission in channels where personas are active. Without this permission, Gideon falls back to responding as the bot account.

Configure personas via the dashboard **Personas** tab or via slash commands:

```
/persona set display_name:TechBot system_prompt:You are a helpful tech support assistant.
/persona template helpful-assistant
/persona toggle
```

### Image Generation Provider Configuration (Optional)

Gideon's unified `/dream` command supports multiple backend providers.

**Cloudflare Worker (Optional)**

⚠️ Setting up and deploying the Cloudflare Worker is the responsibility of the end user. Gideon does not provide support for configuring or troubleshooting Cloudflare Workers.

```env
CLOUDFLARE_WORKER_URL=https://your-worker.your-subdomain.workers.dev
CLOUDFLARE_API_KEY=your_api_key  # optional, if your worker requires auth
```

An example worker tested with Gideon: [flux1-cloudflare-worker](https://github.com/eoko-dev/flux1-cloudflare-worker)

**OpenAI DALL-E (Optional)**

```env
OPENAI_API_KEY=your_openai_key
```

**ComfyUI (Optional)**

```env
COMFYUI_URL=http://127.0.0.1:8188
```

Use `/dream_manage comfyui_test` to verify connectivity, `/dream_manage comfyui_workflow` to load a custom workflow JSON.

**OpenRouter (Optional)**

Uses the same key as chat. Activate with `/dream_manage set_provider provider:OpenRouter`.

### Video Generation Configuration (Optional)

Gideon's `/video` command uses OpenRouter's asynchronous video generation API. It reuses the existing `OPENROUTER_API_KEY` (or a key stored in the dashboard's encrypted key store) — no extra configuration is required.

Available models include Veo 3.1, Veo 3, Sora 2 Pro, Seedance 2.0/1.5, Wan 2.7/2.6, and Kling Video O1. Configure defaults via the dashboard **Video Gen** tab or with `/video_manage configure`. Per-invocation overrides for `model`, `duration`, `aspect_ratio`, `resolution`, `audio`, `seed`, and `image_url` (image-to-video) are accepted directly on `/video`.

Generation typically takes 30 seconds to several minutes; the bot polls until the job reaches a terminal state and posts the result inline (or links to the unsigned URL when the file exceeds Discord's 25 MB attachment limit).

## 🤖 Commands

> **Tip:** Use `/help` in Discord to browse all commands with an interactive menu. Admin commands are automatically hidden from regular users.

### General Commands

| Command | Description |
| --- | --- |
| `/help` | View all commands with interactive category navigation |
| `/chat` | Start a conversation with the AI (supports image uploads for vision models) |
| `/search` | Search the web for current information using the AI |
| `/reset` | Clear the conversation history for the current channel |
| `/summarize` | Summarize the current conversation history |
| `/memory status` | Show conversation statistics: message count, stored memory summaries, session age, session timeout, auto-summarize and per-user memory status |
| `/memory me` | See what Gideon remembers about you in this channel (only you can see it) |
| `/memory forget` | Delete Gideon's notes about you in this channel, or everywhere |
| `/memory optout` / `/memory optin` | Stop (and delete) or resume per-user memory for you in every channel |
| `/memory forget_user` | Admin: delete Gideon's notes about a member in this channel |
| `/summarizeurl` | Fetch and summarize the content of a given URL |

### Persona Commands (Admin)

Configure unique bot identities per channel using Discord webhooks.

| Command | Description |
| --- | --- |
| `/persona set` | Set a custom persona for this channel (name, avatar, system prompt) |
| `/persona template` | Apply a built-in or custom template to this channel |
| `/persona view` | View the current persona configuration for this channel |
| `/persona list` | List all channels with configured personas |
| `/persona templates` | Browse all available persona templates |
| `/persona toggle` | Enable or disable the persona without removing it |
| `/persona remove` | Remove the persona from this channel |
| `/persona preview` | Send a test message using the current channel persona |

### Settings Commands (Admin)

| Command | Description |
| --- | --- |
| `/settings show` | View all current global settings |
| `/settings model` | Set global AI model (format: provider/model) |
| `/settings system` | Set global system prompt |
| `/settings provider` | Set global AI provider (openrouter/openai) |
| `/settings memory` | Set message history limit |
| `/settings window` | Set time window for history (hours) |
| `/settings restore` | Reset all settings to defaults |

### Channel Commands (Admin)

| Command | Description |
| --- | --- |
| `/channel show` | View current channel settings |
| `/channel model` | Set AI model for this channel |
| `/channel system` | Set system prompt for this channel |
| `/channel provider` | Set AI provider for this channel |
| `/channel reset` | Clear all channel overrides |
| `/channel list` | List all channels with custom settings |

### Thread Commands

| Command | Description |
| --- | --- |
| `/thread new` | Create a new AI conversation thread |
| `/thread message` | Send a message to a specific thread |
| `/thread list` | View all active AI threads in the channel |
| `/thread show` | View thread configuration settings |
| `/thread model` | Set AI model for this thread |
| `/thread system` | Set system prompt for this thread |
| `/thread rename` | Change the name of an AI thread |
| `/thread delete` | Remove an AI thread and its history |

### Trivia Commands

| Command | Description |
| --- | --- |
| `/trivia start` | Start a new trivia game (solo or competitive mode) |
| `/trivia stop` | End the current trivia game |
| `/trivia stats [user]` | View your trivia statistics or another player's stats |
| `/trivia leaderboard [timeframe]` | View server rankings (daily/weekly/monthly/all-time) |
| `/trivia achievements` | Display your earned achievement badges |

### Admin Commands

| Command | Description |
| --- | --- |
| `/admin sync` | Sync slash commands with Discord (Owner only) |
| `/admin debug` | Show debug information |
| `/admin state` | Display database state information |
| `/admin diagnostic` | Run system diagnostics |
| `/admin vision_models` | List all vision-capable AI models |

### Dashboard Commands (Admin)

| Command | Description |
| --- | --- |
| `/dashboard status` | Check dashboard server status |
| `/dashboard restart` | Restart the dashboard server (Owner only) |

### Image Commands

| Command | Description | Permissions |
| --- | --- | --- |
| `/dream prompt:...` | Generate an image using the active provider | All Users |
| `/dream_manage set_provider` | Set the active image generation provider | Admin |
| `/dream_manage view_config` | View the current active provider and configuration | Admin |
| `/dream_manage configure ai_horde` | Configure AI Horde defaults | Admin |
| `/dream_manage configure cloudflare` | Configure Cloudflare defaults | Admin |
| `/dream_manage configure openai` | Configure OpenAI/DALL-E defaults | Admin |
| `/dream_manage configure comfyui` | Configure ComfyUI defaults | Admin |
| `/dream_manage configure openrouter` | Configure OpenRouter defaults | Admin |
| `/dream_manage comfyui_models` | List available ComfyUI checkpoint models | Admin |
| `/dream_manage comfyui_test` | Test ComfyUI server connection | Admin |
| `/dream_manage comfyui_workflow` | Set a custom ComfyUI workflow (JSON) | Admin |

### Video Commands

| Command | Description | Permissions |
| --- | --- | --- |
| `/video prompt:... [model] [duration] [aspect_ratio] [resolution] [audio] [seed] [image_url]` | Generate a video via OpenRouter; per-invocation options override saved defaults | All Users |
| `/video_manage view_config` | View current video generation defaults | Admin |
| `/video_manage models` | List available video generation models | Admin |
| `/video_manage configure` | Set defaults (model, duration, aspect ratio, resolution, audio) | Admin |

## 📚 Supported Models

### Text Models (via OpenRouter)

* **OpenAI**: GPT-4o, GPT-4o-mini, GPT-4 Turbo, etc.
* **Anthropic**: Claude 3.7 Sonnet, Claude 3 Opus, Claude 3 Haiku, etc.
* **Google**: Gemini 2.0 Flash, Gemini Pro 1.5, etc.
* **Meta**: Llama 3 70B, 8B, etc.
* **Mistral**: Mistral Large, Mixtral 8x22B, etc.
* **Perplexity**: Sonar Large, Sonar Small
* **And many more!** Check [OpenRouter.ai](https://openrouter.ai/models) for the full list.

### Image Models

**Via AI Horde** — Stable Diffusion SD 2.1, SDXL, and fine-tuned community models

**Via Cloudflare Worker** — Custom model implementation (your worker, your models)

**Via OpenRouter** — Google Gemini image models, FLUX variants, OpenAI GPT image models, ByteDance Seedream, and more

**Via OpenAI** — DALL-E 2 and DALL-E 3

**Via ComfyUI** — Any model supported by your local ComfyUI instance (FLUX, SDXL, SD3, etc.)

### Video Models (via OpenRouter)

* **Google**: Veo 3.1, Veo 3
* **OpenAI**: Sora 2 Pro
* **ByteDance**: Seedance 2.0, Seedance 1.5
* **Alibaba**: Wan 2.7, Wan 2.6
* **Kuaishou**: Kling Video O1
* **And more** as OpenRouter adds providers — the model picker queries `/api/v1/videos/models` at runtime (no hardcoded fallback list).

## 📁 Project Structure

```
gideon/
├── src/
│   ├── bot.py                      # Bot initialization & core logic
│   ├── config.py                   # Configuration loading (.env)
│   ├── __main__.py                 # Entry point
│   ├── cogs/                       # Command modules (features)
│   │   ├── admin_commands.py       # Admin tools (/admin group)
│   │   ├── channel_commands.py     # Channel settings (/channel group)
│   │   ├── chat_commands.py        # AI chat (/chat, /reset, etc.)
│   │   ├── dashboard_commands.py   # Web admin dashboard (/dashboard)
│   │   ├── mention_commands.py     # Bot @mention handling & native tool calling
│   │   ├── persona_commands.py     # Per-channel personas (/persona group)
│   │   ├── reminder_commands.py    # Reminder management
│   │   ├── settings_commands.py    # Global settings (/settings group)
│   │   ├── thread_commands.py      # Thread management (/thread group)
│   │   ├── trivia_commands.py      # Trivia game system (/trivia group)
│   │   ├── unified_image_commands.py # Image generation (/dream)
│   │   ├── video_commands.py       # Video generation (/video)
│   │   └── url_commands.py         # URL summarization
│   ├── dashboard/                  # Dashboard frontend assets
│   │   ├── index.html              # Main dashboard page
│   │   ├── css/dashboard.css       # Dashboard styles
│   │   └── js/
│   │       ├── core.js             # Shared UI kit (API client, toasts, modals, escaping)
│   │       ├── palette.js          # Ctrl/⌘+K command palette
│   │       ├── theme.js            # Theme bootstrap (loaded before first paint)
│   │       └── dashboard.js        # Dashboard application logic
│   └── utils/
│       ├── ai_horde_client.py
│       ├── api_key_service.py      # Encrypted API key management
│       ├── cloudflare_client.py
│       ├── encryption.py           # Fernet encryption utility
│       ├── game_session.py         # In-memory trivia game state
│       ├── memory_service.py       # Session rotation, channel memory summarization & per-user notes
│       ├── user_memory.py          # Per-user note formatting & prompt block
│       ├── model_manager.py
│       ├── openai_client.py
│       ├── openrouter_client.py
│       ├── openrouter_video_client.py # Async OpenRouter video generation client
│       ├── permissions.py
│       ├── persona_templates.py    # Built-in persona template definitions
│       ├── state_manager.py        # Centralized state management
│       ├── tool_registry.py        # Native LLM tool definitions & execution
│       ├── trivia_ai.py
│       ├── trivia_config.py
│       ├── webhook_sender.py       # Webhook lifecycle management for personas
│       ├── dashboard/
│       │   ├── server.py           # aiohttp web server & API routes
│       │   ├── auth.py             # Sessions, login lockout, security headers
│       │   └── error_log.py        # Recent-errors ring buffer for the Overview tab
│       └── database/
│           ├── core.py             # DatabaseManager (delegates to sub-managers)
│           ├── schema.py           # Table definitions
│           ├── api_key_manager.py
│           ├── backup_manager.py   # Config/database backup & restore
│           ├── memory_manager.py   # Channel memory CRUD (CHANNEL_MEMORY table)
│           ├── user_memory_manager.py # Per-user, per-channel notes (CHANNEL_USER_MEMORY table)
│           ├── persona_manager.py  # Persona template & channel persona CRUD
│           ├── trivia_manager.py
│           └── ...
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── README.md
├── CLAUDE.MD                       # Developer guide & architecture reference
└── index.md                        # GitHub Pages landing page
```

## ❓ Troubleshooting

* **Bot Offline/Unresponsive:**
  * **Python:** Check if the `python3 -m src` process is running. Check console logs for startup errors.
  * **Docker:** Check container status (`docker ps`). View logs with `docker-compose logs -f`.
  * Verify `DISCORD_TOKEN` in `.env` is correct.
  * Ensure the bot has `Send Messages`, `Read History`, `Use Slash Commands`, and `Manage Threads` permissions.

* **Commands Not Working:**
  * Verify you have the required permissions (Admin for config/persona commands).
  * Check API keys in `.env` are correct for the features being used.
  * Check the bot's logs for specific error messages.

* **Personas Not Showing:**
  * Ensure the bot has **Manage Webhooks** permission in the channel.
  * Verify the persona is active via `/persona view` or the dashboard Personas tab.
  * Check logs for webhook creation errors.

* **Image Generation (`/dream`) Failures:**
  * Verify provider configuration (`/dream_manage view_config`) and API keys.
  * Ensure the bot has `Attach Files` and `Embed Links` permissions.
  * Check AI Horde status/kudos if using that provider.

* **Video Generation (`/video`) Failures:**
  * Confirm an OpenRouter API key is set (in `.env` or the dashboard's encrypted key store) and that the account has video generation credits.
  * If the bot reports `Timed out…`, the model is taking longer than the 15-minute polling cap — re-run with a shorter duration or smaller resolution.
  * If a job ends with status `failed`, run `/video_manage models` to verify the chosen model still exists, and check that the requested duration / aspect ratio / resolution combination is supported by that model.
  * Files larger than 25 MB cannot be attached directly; the bot will post the unsigned URL instead.

* **Database/State Issues:**
  * Ensure `DATA_DIRECTORY` (Python) or the Docker volume mount is correct and writable.
  * Check logs for SQLite errors (`database is locked`, `unable to open database file`).
  * Use the dashboard **Backup & Restore** tab to export a backup before troubleshooting.

## 📖 Documentation

For detailed technical information about Gideon's architecture, development patterns, and implementation details, see the [Developer Guide](CLAUDE.MD).

<div align="center">
Made with ❤️ by <a href="https://github.com/eoko-dev">eoko</a>
</div>
