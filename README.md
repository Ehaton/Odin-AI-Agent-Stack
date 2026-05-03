# Odin AI Agent Stack

> A self-hosted, multi-model AI agent for homelab and enterprise infrastructure orchestration. SSH across your entire network, control Home Assistant, search the web, manage a knowledge vault, and chat — all running locally on your own GPU.

---

## Table of Contents

- [What Odin Does](https://claude.ai/chat/8635d1c5-7bb8-4b4a-921a-26bf07148e85#what-odin-does)
- [Architecture](https://claude.ai/chat/8635d1c5-7bb8-4b4a-921a-26bf07148e85#architecture)
- [Prerequisites](https://claude.ai/chat/8635d1c5-7bb8-4b4a-921a-26bf07148e85#prerequisites)
- [Installation](https://claude.ai/chat/8635d1c5-7bb8-4b4a-921a-26bf07148e85#installation)
- [Configuration](https://claude.ai/chat/8635d1c5-7bb8-4b4a-921a-26bf07148e85#configuration)
- [Model Selection Guide](https://claude.ai/chat/8635d1c5-7bb8-4b4a-921a-26bf07148e85#model-selection-guide)
- [The Interface](https://claude.ai/chat/8635d1c5-7bb8-4b4a-921a-26bf07148e85#the-interface)
- [Full Capabilities](https://claude.ai/chat/8635d1c5-7bb8-4b4a-921a-26bf07148e85#full-capabilities)
- [Limitations](https://claude.ai/chat/8635d1c5-7bb8-4b4a-921a-26bf07148e85#limitations)
- [Custom Branding](https://claude.ai/chat/8635d1c5-7bb8-4b4a-921a-26bf07148e85#custom-branding)
- [API Reference](https://claude.ai/chat/8635d1c5-7bb8-4b4a-921a-26bf07148e85#api-reference)
- [FAQ](https://claude.ai/chat/8635d1c5-7bb8-4b4a-921a-26bf07148e85#faq)

---

## What Odin Does

Odin is a locally-hosted AI agent stack built for people who run serious homelab infrastructure. It's not a chatbot wrapper — it's an orchestration layer that gives a language model real tools: SSH sessions across your entire network, Home Assistant service calls, a searchable knowledge vault, web search via SearXNG, and a live terminal in the browser.

Everything runs on your hardware. No data leaves your network unless you explicitly route a query to the Claude API.

```
You type a message →
  Odin classifies the intent →
    Routes to the right model →
      Model calls tools in parallel →
        Results synthesized into a response
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Browser UI                           │
│  ┌──────────┐  ┌──────────────────┐  ┌────────────────────┐ │
│  │ Sidebar  │  │   Chat / Views   │  │  Agents / HA /     │ │
│  │ Projects │  │  (resizable)     │  │  Terminal          │ │
│  │ Chats    │  │                  │  │  (resizable)       │ │
│  └──────────┘  └──────────────────┘  └────────────────────┘ │
└────────────────────────┬────────────────────────────────────┘
                         │ HTTPS (Tailscale or LAN)
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                     Odin.py (Flask)                         │
│                                                             │
│  Classifier → Model Router → Tool Dispatcher               │
│                                                             │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌───────────────┐  │
│  │  SSH     │ │  Vault   │ │   HA     │ │  Web Search   │  │
│  │ Executor │ │ (Obsidian│ │  REST    │ │  (SearXNG)    │  │
│  │ (Paramiko│ │  FS)     │ │  API     │ │               │  │
│  └──────────┘ └──────────┘ └──────────┘ └───────────────┘  │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────┐          ┌──────────────────────┐
│  Ollama          │          │  Docker Services      │
│  qwen3-coder:30b │          │  SearXNG :8080        │
│  llama3.2:3b     │          │  ChromaDB :8000       │
│  nomic-embed     │          │  Whisper  :10300      │
│  (RTX 3090)      │          │  Piper    :10200      │
└──────────────────┘          │  OpenWakeWord :10400  │
                              └──────────────────────┘
```

### Request Flow

1. User sends a message
2. `classify()` heuristic (no LLM call) assigns a category: `voice`, `code`, `infra_simple`, `reasoning`, `trivial`, or `general`
3. Category maps to a model role via `models.yaml`
4. Model is called with tools enabled (SSH, vault, web search, HA, shell)
5. Model may call tools in parallel — up to 4 concurrent workers
6. Tool results injected back into context; model synthesizes a final answer
7. Response streamed back to the browser

---

## Prerequisites

- Ubuntu 22.04+ (or Debian 12) VM or bare metal
- NVIDIA GPU with 12+ GB VRAM (tested on RTX 3090 24GB)
- NVIDIA drivers + CUDA installed
- Docker + Docker Compose
- Python 3.11+
- Git
- Network access to your homelab hosts (SSH keys recommended)
- Optional: Home Assistant, Obsidian with Local REST API plugin, Tailscale

---

## Installation

### Step 1 — Clone the repo

```bash
git clone https://github.com/Ehaton/Odin-AI-Agent-Stack.git /opt/Odin
cd /opt/Odin
```

### Step 2 — Install Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh

# Verify GPU is detected
ollama list
```

### Step 3 — Pull models

Choose your lineup based on available VRAM. See the [Ollama library]([https://claude.ai/chat/8635d1c5-7bb8-4b4a-921a-26bf07148e85#model-selection-guide](https://ollama.com/library)) for details.

```bash
# Recommended for RTX 3090 (24GB)
ollama pull qwen3-coder:30b   # ~17 GB — primary workhorse
ollama pull llama3.2:3b        # ~2.5 GB — voice + trivial
ollama pull nomic-embed-text   # ~0.3 GB — vault embeddings

# Verify all loaded
ollama list
```

### Step 4 — Python environment

```bash
cd /opt/Odin
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Step 5 — Configure environment

```bash
cp example.env .env
nano .env
```

Minimum required fields:

```env
# Authentication
ODIN_USER=yourusername
ODIN_PASS=your-strong-password

# Ollama (default is localhost, change if Ollama is remote)
OLLAMA_HOST=http://localhost:11434

# Vault — path to your Obsidian vault folder
ODIN_VAULT_PATH=/mnt/your-obsidian-vault/Notes

# Home Assistant (optional but recommended)
HASS_URL=http://192.168.1.x:8123
HASS_TOKEN=your-long-lived-token
```

See the example .env file for reference.

### Step 6 — Configure SSH hosts

```bash
cp example.hosts.json hosts.json
nano hosts.json
```

```json
{
  "WebServer": {
    "host": "192.168.1.100",
    "user": "root",
    "description": "Nginx web server"
  },
  "NAS": {
    "host": "192.168.1.50",
    "user": "admin",
    "description": "TrueNAS primary storage"
  }
}
```

Deploy your SSH key to all hosts:

```bash
# Generate a key if you don't have one
ssh-keygen -t ed25519

# Deploy to each host
for host in $(jq -r 'to_entries[] | "\(.value.user)@\(.value.host)"' hosts.json); do
  echo "Deploying to $host..."
  ssh-copy-id -i ~/.ssh/id_ed25519.pub "$host"
done

# Test all connections
for entry in $(jq -r 'to_entries[] | "\(.key):\(.value.user)@\(.value.host)"' hosts.json); do
  name="${entry%%:*}"; target="${entry##*:}"
  printf "%-20s " "$name"
  ssh -o ConnectTimeout=5 -o BatchMode=yes "$target" hostname 2>&1 || echo "FAILED"
done
```

### Step 7 — Start Docker services

```bash
docker compose up -d

# Verify all containers running
docker compose ps
```

Expected output:

```
NAME           STATUS          PORTS
ollama         running         0.0.0.0:11434->11434/tcp
searxng        running         0.0.0.0:8080->8080/tcp
chromadb       running         0.0.0.0:8000->8000/tcp
whisper        running         0.0.0.0:10300->10300/tcp
piper          running         0.0.0.0:10200->10200/tcp
wakeword       running         0.0.0.0:10400->10400/tcp
n8n            running         0.0.0.0:5678->5678/tcp
```

### Step 8 — Start Odin

```bash
cd /opt/Odin
source venv/bin/activate
python Odin.py
```

You'll see:

```
============================================================
  ⚔️  ODIN — AI Assistant
============================================================
  🧠 Ollama: http://localhost:11434
  📦 Active: qwen3-coder:30b, llama3.2:3b
  🔥 Prewarming: qwen3-coder:30b (in background)
  📚 Vault (filesystem): /mnt/your-vault (45 notes)
  🏠 Home Assistant: connected (http://192.168.1.xx:8123)
  🔑 SSH hosts: 14
  🌐 Web access: enabled
  🌐 Open in browser:
     http://localhost:5050
     http://192.168.1.x:5050
```

### Step 9 — Enable HTTPS (Tailscale, recommended)

```bash
# Install Tailscale if not already installed
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up

# Generate a cert for your machine
tailscale cert your-machine.tailXXXX.ts.net

# Copy certs to /opt/Odin
cp your-machine.tailXXXX.ts.net.crt /opt/Odin/
cp your-machine.tailXXXX.ts.net.key /opt/Odin/

# Set TAILNET_NAME in .env
echo 'TAILNET_NAME=your-machine.tailXXXX.ts.net' >> .env

# Restart Odin — it will auto-detect the certs
python Odin.py
```

Access via: `https://your-machine.tailXXXX.ts.net:5050`

### Step 10 — Run as a service (optional)

To keep Odin running after you close your SSH session:

```bash
# Create a systemd service
cat > /etc/systemd/system/odin.service << 'EOF'
[Unit]
Description=Odin AI Agent
After=network.target docker.service

[Service]
Type=simple
User=your-username
WorkingDirectory=/opt/Odin
ExecStart=/opt/Odin/venv/bin/python /opt/Odin/Odin.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now odin
systemctl status odin
```

---

## Configuration

All configuration lives in `.env`. Copy `example.env` to get started.

|Variable|Required|Default|Description|
|---|---|---|---|
|`ODIN_USER`|✅|—|Login username for the web UI|
|`ODIN_PASS`|✅|—|Login password. Leave blank to disable auth (local only)|
|`OLLAMA_HOST`|✅|`http://localhost:11434`|Ollama API endpoint|
|`ODIN_PORT`|—|`5050`|Port Odin listens on|
|`ODIN_VAULT_PATH`|—|—|Absolute path to Obsidian vault folder|
|`OBSIDIAN_API_KEY`|—|—|Obsidian Local REST API key (alternative to filesystem vault)|
|`HASS_URL`|—|—|Home Assistant base URL e.g. `http://192.168.1.x:8123`|
|`HASS_TOKEN`|—|—|HA long-lived access token|
|`TAILNET_NAME`|—|—|Tailscale machine name for HTTPS e.g. `host.tail1234.ts.net`|
|`SEARXNG_URL`|—|`http://localhost:8080`|SearXNG search endpoint|
|`CHROMA_TOKEN`|—|—|ChromaDB auth token|
|`PVE_TOKEN_ID`|—|—|Proxmox API token ID e.g. `user@pam!token-name`|
|`PVE_TOKEN_SECRET`|—|—|Proxmox API token secret|
|`ODIN_DB`|—|`odin.db`|SQLite database path|
|`ODIN_HOSTS_FILE`|—|`hosts.json`|SSH hosts inventory path|

### models.yaml

Edit `models.yaml` to change which models handle which tasks. No code changes required.

```yaml
roles:
  worker: qwen3-coder:30b   # Complex reasoning and multi-host tasks
  coder:  qwen3-coder:30b   # Code generation, scripts, SSH tool chains
  voice:  llama3.2:3b        # Home Assistant voice commands
```

After editing, hot-reload without restarting:

```bash
curl -X POST http://localhost:5050/api/models/reload
```

### System prompts

Edit the markdown files in `Odins_Self/prompts/v3/` to change how each model behaves. Two template variables are available:

- `{current_time}` — replaced with the current date and time
- `{hosts}` — replaced with the SSH host list from `hosts.json`

Hot-reload after editing:

```bash
curl -X POST http://localhost:5050/api/models/reload
```

---

## Model Selection Guide

The right model depends on your VRAM budget and use case. All models are run via Ollama.

|Model|VRAM (Q4)|Speed|Best For|Notes|
|---|---|---|---|---|
|`llama3.2:3b`|~2.5 GB|~60 tok/s|Voice commands, greetings, simple queries|Meta's native tool-calling 3B. Fastest response, minimal VRAM.|
|`qwen3:4b`|~3 GB|~50 tok/s|Light coding, basic infra checks|Good balance for systems with 8GB VRAM. Tool-calling capable.|
|`llama3.1:8b`|~5 GB|~35 tok/s|General chat, moderate reasoning|Solid all-rounder if you can't run 14B+ models.|
|`qwen3:8b`|~5 GB|~35 tok/s|Reasoning and planning tasks|Dense 8B with strong reasoning. Use as reasoner with a smaller coder.|
|`qwen2.5-coder:14b`|~9 GB|~25 tok/s|Code generation, script writing|Purpose-built coder. Excellent for code if 30B MoE is too large.|
|`deepseek-r1:14b`|~9 GB|~20 tok/s|Complex multi-step reasoning|Heavy thinking model. Slow but thorough.|
|**`qwen3-coder:30b`** ⭐|~17 GB|~40 tok/s|**Everything — code, SSH, analysis**|**MoE: 30B total, 3.3B active. Runs at 3B speed with 14B+ quality. Recommended primary model for 24GB GPUs.**|
|`qwen3:30b`|~19 GB|~18 tok/s|Dense 30B reasoning|Slower than the MoE variant. Only use if you specifically need dense architecture.|

### Recommended Lineups by VRAM

**8 GB VRAM** (e.g. RTX 3070)

```yaml
roles:
  coder:  qwen3:4b
  worker: llama3.1:8b   # hot-swap — can't fit both
  voice:  llama3.2:3b
```

**16 GB VRAM** (e.g. RTX 4080)

```yaml
roles:
  coder:  qwen2.5-coder:14b
  worker: qwen3:8b
  voice:  llama3.2:3b
```

**24 GB VRAM** (e.g. RTX 3090 / 4090) — current BeanLab config

```yaml
roles:
  coder:  qwen3-coder:30b   # MoE — primary workhorse
  worker: qwen3-coder:30b   # same model, different prompt
  voice:  llama3.2:3b
```

### VRAM Budget Breakdown (24 GB)

```
qwen3-coder:30b  ████████████████████████████░░░░░░░  17.0 GB
llama3.2:3b      ███░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░   2.5 GB
nomic-embed      ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░   0.3 GB
KV Cache (8K)    ████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░   2.1 GB
─────────────────────────────────────────────────────
Headroom         ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░   2.1 GB
```

---

## The Interface

### Three-Column Layout

The UI is divided into three resizable panels. Drag the handles between columns to resize. Double-click any handle to reset to the default width.

```
┌─────────────┬──────────────────────────┬───────────────┐
│  Sidebar    │         Chat             │  Right Panel  │
│             │                          │               │
│  Projects   │  [message bubbles]       │  Agents tab   │
│  └ Chats    │                          │  Console tab  │
│  └ Chats    │  [input bar]             │  HA tab       │
│             │  [drag & drop zone]      │               │
│  + New      │                          │               │
└─────────────┴──────────────────────────┴───────────────┘
```

### Tab Navigation

The top bar contains three global view tabs:

- **Chat** — the main conversation interface
- **Agents** — full-width agent status grid
- **Home Assistant** — full-width device and scene grid

### Sidebar — Chat Management

- **New chat** button creates a chat in General
- **+ New project** creates a named folder with a custom emoji
- **Drag** any chat item onto a project folder to move it
- **Hover** a chat to reveal the checkbox (for multi-select) and `···` menu
- **Multi-select**: check multiple chats, then use the action bar to bulk delete, bulk move, or create a new project from the selection
- **`···` menu** (on hover): Rename, Move to project, Delete

### Right Panel — Agents Tab

Live status cards for each Odin agent role:

|Card|Role|What it shows|
|---|---|---|
|Coder Agent|`qwen3-coder:30b`|Active request status, % utilization|
|Worker Agent|`qwen3-coder:30b`|Standby / processing|
|Voice Agent|`llama3.2:3b`|Listening / idle|
|Vault Agent|`nomic-embed-text`|Note count, embedding status|
|HA Agent|`home-assistant`|Connected / disconnected|
|Search Agent|`searxng`|Ready / error|

Click any card to log its details to the console.

### Right Panel — Console Tab

A browser-based SSH terminal proxied through Odin:

1. Select a host from the dropdown (populated from `hosts.json`)
2. Click **Connect** — Odin verifies SSH and establishes the session
3. Type commands — executed on the remote host via Paramiko, output streamed back
4. **Arrow up/down** for command history
5. **Double-click** a command in history to re-run
6. Four sub-tabs: Shell, Agent Log, Network, Errors
7. You can also open a terminal via chat: _"Open a terminal on NetworkBean"_

Blocked commands (same guardrail as the agent): `rm -rf /`, `shutdown`, `reboot`, `mkfs`, `dd if=/dev/zero`, `iptables -F`, and others.

### Right Panel — HA Tab

Compact device grid pulled from Home Assistant:

- **Toggle** — power button on each card
- **Click** any card to open a detail popup with all entity attributes
- **Right-click** any card for the same detail popup
- **Brightness slider** appears for light entities that support it
- Organized by domain: lights, switches, climate, media players, automations, scenes

### File Attachments

- **Drag and drop** files anywhere onto the chat area — a drop zone overlay appears
- **Paste** images directly from clipboard
- **Paperclip button** for manual file selection
- Images show a thumbnail preview chip
- Non-image files show an extension badge chip (`.py`, `.log`, `.yaml`, etc.)
- Supported: `.txt .md .py .js .ts .json .yaml .yml .sh .bash .log .csv .html .css .xml .pdf .png .jpg .jpeg .gif .webp`

---

## Full Capabilities

### ✅ What Odin Can Do

**Infrastructure**

- SSH into any host in `hosts.json` and run commands
- Fan out commands in parallel across multiple hosts
- Parse and summarize command output (disk usage, uptime, logs, process lists)
- Run local shell commands on the ai-stack host itself
- Check service status, restart services, tail logs
- Access the Proxmox API for VM/container info (with `PVE_TOKEN_ID` configured)

**Code & Scripts**

- Write Python, Bash, JavaScript, Go, Rust, YAML, SQL, and most other languages
- Debug stack traces and syntax errors
- Refactor and optimize existing code
- Generate full scripts from a description and optionally execute them
- Save generated code directly to the Obsidian vault

**Web & Research**

- Search the web via self-hosted SearXNG (no API keys required)
- Fetch and summarize content from specific URLs
- Look up current software versions, CVEs, documentation
- Find your public IP address

**Knowledge Vault**

- Search your Obsidian vault by keyword (vector or text search)
- Read specific notes by path
- Write new notes or update existing ones
- Save research summaries, infrastructure reports, and generated scripts
- Build persistent memory across sessions

**Home Assistant**

- Turn devices on and off by name or entity ID
- Set brightness, color temperature, thermostat targets
- Query device states
- Activate scenes and run automations
- List all entities by domain

**Voice**

- Wake word detection via OpenWakeWord ("Hey Odin")
- Speech-to-text via Wyoming Whisper
- Text-to-speech responses via Wyoming Piper
- Voice mode in the browser (microphone button)

**Workflows (via n8n)**

- Trigger n8n workflows by name
- Scheduled tasks: morning briefing, backup verification, media scans, update checks
- Webhook-based automation from external events

**Chat Management**

- Persistent multi-chat history across browser sessions
- Project folder organization with custom emoji
- Drag-and-drop chat reorganization
- Bulk operations: multi-select, bulk delete, bulk move
- Chat renaming and project assignment

---

## Limitations

### Hard Limits (by design)

|Limitation|Reason|
|---|---|
|**No destructive SSH commands**|`rm -rf /`, `shutdown`, `mkfs`, `iptables -F`, and similar commands are blocked at the API level|
|**No credential exfiltration**|The model will refuse to extract or print SSH keys, passwords, or tokens|
|**No attacks on external systems**|The model refuses requests to scan, probe, or attack systems not in your `hosts.json`|
|**16-host SSH fan-out max per turn**|Prevents a single prompt from hammering the entire network simultaneously|

### Technical Constraints

|Constraint|Detail|
|---|---|
|**Context window**|8,192 tokens by default for all models. Long tool-heavy conversations may get truncated|
|**Single GPU**|Cannot run two large models simultaneously on 24GB — the 30B MoE coder is the workaround|
|**No streaming from Ollama**|Responses appear all at once after processing (streaming support is partially scaffolded)|
|**SSH timeout**|30 seconds per command. Long-running processes will time out|
|**Tool call limit**|5 iterations maximum per response. Complex multi-stage tasks may need to be broken up|
|**Voice latency**|End-to-end voice (wake → STT → model → TTS → speaker) is ~3-4 seconds. Not instant|
|**HA state refresh**|Device states are fetched on page load and on toggle. Not real-time pushed|
|**No persistent terminal sessions**|Each terminal command is a fresh SSH exec. No interactive programs (vim, htop, etc.)|

### Known Issues

- The `qwen3:8b` model is known to return empty tool names on some queries — route all tool-using tasks through `qwen3-coder:30b` instead
- The vault writer fires in parallel with SSH commands on some prompts; vault entries may contain placeholder data if SSH hadn't responded yet when the write fired
- The Whisper STT medium model running on CPU adds ~1-2s to voice latency

---

## Custom Branding

Odin can be rebranded for personal use, team deployments, or enterprise environments without modifying the core Python.

### Rename the assistant

**1. System prompts** — Edit `Odins_Self/prompts/v3/worker.md` and `coder.md`:

```markdown
# Change "You are Odin" to your assistant name
You are Jarvis, the AI assistant for Stark Industries infrastructure.
```

**2. Web UI title and logo** — In `static/odin.html`, find and replace:

```html
<!-- Find: -->
<title>Odin — BeanLab</title>
<!-- Change to: -->
<title>Jarvis — Stark Industries</title>

<!-- Find the logo in the topbar: -->
<div class="topbar-logo">Od<em>i</em>n</div>
<!-- Change to: -->
<div class="topbar-logo">Jar<em>v</em>is</div>

<!-- Find the welcome screen: -->
<h1>Od<em>i</em>n</h1>
<p>BeanLab AI — voice, network, vault. All connected.</p>
<!-- Change to: -->
<h1>Jar<em>v</em>is</h1>
<p>Stark Industries AI — voice, network, vault. All connected.</p>
```

**3. Suggestion cards** — Still in `odin.html`, update the quick-start suggestions:

```html
<div class="suggestion" onclick="useSuggestion(this)">Check GPU on ai-stack-420</div>
<div class="suggestion" onclick="useSuggestion(this)">What's my public IP?</div>
<!-- Add your own: -->
<div class="suggestion" onclick="useSuggestion(this)">Status of all prod servers</div>
<div class="suggestion" onclick="useSuggestion(this)">Deploy the staging build</div>
```

**4. Color accent** — In the `:root` CSS block at the top of `odin.html`:

```css
:root {
  --accent: #c08b5c;        /* copper — change to your brand color */
  --accent-soft: rgba(192,139,92,0.12);  /* match with 12% opacity */
  --accent-dim:  rgba(192,139,92,0.06);  /* match with 6% opacity  */
}
```

**5. Application name** — In `config.py`:

```python
APP_NAME = "Jarvis"
APP_VERSION = "1.0.0"
```

**6. Startup banner** — In `Odin.py`, the `main()` function:

```python
print("============================================================")
print("  🤖  JARVIS — Stark Industries AI Assistant")
print("============================================================")
```

**7. Reload prompts without restarting:**

```bash
curl -X POST http://localhost:5050/api/models/reload
```

---

## API Reference

All endpoints require HTTP Basic Auth unless `ODIN_PASS` is not set.

### Chat

|Method|Endpoint|Description|
|---|---|---|
|`POST`|`/api/chat`|Send a message. Body: `{message, chat_id, model, attachments[]}`|
|`GET`|`/api/chats`|List all chats (sorted by updated_at DESC)|
|`POST`|`/api/chats`|Create a chat. Body: `{title, project_id}`|
|`GET`|`/api/chats/<id>`|Get messages for a chat|
|`DELETE`|`/api/chats/<id>`|Delete a chat|
|`POST`|`/api/chats/<id>/rename`|Rename. Body: `{title}`|
|`POST`|`/api/chats/<id>/move`|Move to project. Body: `{project_id}`|

### Projects

|Method|Endpoint|Description|
|---|---|---|
|`GET`|`/api/projects`|List all projects|
|`POST`|`/api/projects`|Create. Body: `{name, icon}`|
|`DELETE`|`/api/projects/<id>`|Delete (chats move to General)|

### Models & Status

|Method|Endpoint|Description|
|---|---|---|
|`GET`|`/api/models`|List available models with metadata|
|`POST`|`/api/models/reload`|Hot-reload `models.yaml` and system prompts|
|`GET`|`/api/status`|System status (Ollama connection, vault, HA, SSH hosts)|
|`GET`|`/api/hosts`|SSH host inventory from `hosts.json`|
|`GET`|`/api/logs`|Recent event log. Params: `n`, `chat_id`|

### Home Assistant

|Method|Endpoint|Description|
|---|---|---|
|`GET`|`/api/ha/states`|All HA entity states (filtered to useful domains)|
|`POST`|`/api/ha/call`|Call a service. Body: `{domain, service, entity_id, ...extras}`|

### Terminal

|Method|Endpoint|Description|
|---|---|---|
|`POST`|`/api/terminal/connect`|Verify SSH to a host. Body: `{host}`|
|`POST`|`/api/terminal/exec`|Run a command. Body: `{host, command}`. Returns `{stdout, stderr, returncode}`|

### Misc

|Method|Endpoint|Description|
|---|---|---|
|`POST`|`/api/upload`|Upload a file for attachment|
|`POST`|`/api/tts`|Text-to-speech. Body: `{text}`. Returns audio|
|`POST`|`/api/purge-now`|Manually trigger old-chat purge|

---

## FAQ

**Q: Does any data leave my network?** By default, no. All model inference runs locally via Ollama. SearXNG aggregates search results without sending queries to Google's API directly. The only exception is if you configure `claude-sonnet` as an optional cloud reasoner — that routes specific queries to Anthropic's API, clearly documented in `models.yaml`.

---

**Q: How do I add a new SSH host?** Edit `hosts.json`, add the entry, then restart Odin (or `POST /api/models/reload` — the host list reloads with the model registry). Deploy your SSH key to the new host before adding it.

---

**Q: Can I run this without a GPU?** Technically yes, but practically no for the larger models. `llama3.2:3b` runs acceptably on CPU (4-8 seconds per response). `qwen3-coder:30b` on CPU would take 3-10 minutes per response. If you only have CPU, use `llama3.1:8b` or `qwen3:4b` and accept the reduced code quality.

---

**Q: Why is the model not using tools?** Check `odin.log` for `"tool": ""` entries — this means the model is generating tool calls with empty names. This is a known issue with `qwen3:8b` and smaller models. The fix is to route all tool-using tasks through `qwen3-coder:30b` by setting `worker: qwen3-coder:30b` in `models.yaml`.

---

**Q: The response was blank / showed raw JSON. What happened?** The most common cause is context overflow — too many tool results exceeded the model's 8K context window. The recovery logic will inject a synthesis prompt and retry. If it keeps happening, reduce `num_ctx` in `models.yaml` to force earlier trimming, or break the task into smaller prompts.

---

**Q: How do I update Odin?**

```bash
cd /opt/Odin
git pull origin main
source venv/bin/activate
pip install -r requirements.txt  # in case dependencies changed
# Restart Odin
```

---

**Q: Can multiple users access Odin simultaneously?** Yes, but they share the same conversation history and the same GPU. Concurrent requests queue behind each other — Flask is single-threaded by default. For multi-user deployments, run Odin under Gunicorn with 2-3 workers: `gunicorn -w 3 -b 0.0.0.0:5050 Odin:app`.

---

**Q: How do I connect Home Assistant?**

1. In HA, go to your Profile → Security → Long-Lived Access Tokens → Create Token
2. Copy the token into `.env` as `HASS_TOKEN`
3. Set `HASS_URL=http://your-ha-ip:8123`
4. Restart Odin — it will log `🏠 Home Assistant: connected`

---

**Q: Why can't I use interactive commands in the terminal (vim, htop, top)?** The terminal is an SSH exec proxy, not a full PTY. Each command opens a new channel, runs, and closes. Interactive full-screen TUI programs require a persistent PTY allocation, which is a planned v2 feature. For now, use non-interactive alternatives: `cat`, `less`, `ps aux`, `df -h`, `journalctl -n 50`, etc.

---

**Q: The vault search isn't finding my notes. What's wrong?** Check that `ODIN_VAULT_PATH` points to the correct folder (the folder containing your `.md` files, not the parent). The filesystem vault does substring search on file content. If you've set up ChromaDB for vector search, check that `CHROMA_TOKEN` is set and `scripts/embed_vault.py` has been run to index your notes.

---

**Q: How do I change which model handles voice commands?** Edit the `voice` role in `models.yaml`:

```yaml
roles:
  voice: llama3.2:3b   # change to any model in your catalog
```

Then `POST /api/models/reload`. Keep in mind that voice needs to respond in under 2-3 seconds — stick to 3B-8B models for voice.

---

**Q: Can I use this with OpenAI or Anthropic instead of Ollama?** The `call_llm()` function in `Odin.py` is designed to be extended. Adding a `provider: anthropic` field to a model entry in `models.yaml` and a corresponding branch in `call_llm()` routes that model to the Anthropic API. The message format is nearly identical. See the roadmap for implementation details.

---

**Q: How do I wipe all chats and start fresh?**

```bash
# Stop Odin first
rm /opt/Odin/odin.db
# Restart — it will create a fresh database
python Odin.py
```

---

## License

MIT — see [LICENSE](https://claude.ai/chat/LICENSE).

## Contributing

This is a personal homelab project published as a reference. Issues and pull requests are welcome but response time may vary. If you build something on top of this, open a PR to add it to a community section.

---

_Built on: [Ollama](https://ollama.com/) · [SearXNG](https://github.com/searxng/searxng) · [ChromaDB](https://github.com/chroma-core/chroma) · [Wyoming Protocol](https://github.com/rhasspy/wyoming) · [Flask](https://flask.palletsprojects.com/) · [n8n](https://n8n.io/)_