# Odin

A self-hosted, voice-enabled AI agent running locally on Ollama. Built for homelab operators who want a single assistant that can SSH across their infrastructure, reason about it, control Home Assistant, search the web, and remember everything via an Obsidian vault — all without sending data to third-party APIs.

> **Heads up:** This is a personal homelab project built for BeanLab. Hostnames, IPs, and paths are specific to that environment and will need editing for yours. It's published as a reference, not a turnkey product.

## What Odin Does

- **Multi-model routing.** A heuristic classifier sends each query to the right model — code tasks to a fast MoE coder, complex reasoning to a dense model, voice commands to a tiny HA-focused model.
- **SSH orchestration.** Fan out commands across every host in `hosts.json` with parallel execution and destructive-command guardrails.
- **Home Assistant control.** Natural language → HA service calls via the REST API.
- **Vault-backed memory.** Search, read, and write to an Obsidian vault for persistent knowledge.
- **Web search.** Self-hosted SearXNG aggregator, no third-party API keys.
- **Voice interface.** Wyoming Protocol stack: Whisper (STT), Piper (TTS), OpenWakeWord.
- **Web UI.** Flask-based chat with model picker, file uploads, voice modal, multi-chat persistence.

## Architecture

```
User (web UI / voice)
        │
        ▼
  Heuristic Classifier
        │
   ┌────┼────────────────┐
   ▼    ▼                ▼
 Coder  Worker          Voice
 (MoE)  (dense 8B)      (3B)
   │    │                │
   └────┴───┬────────────┘
            ▼
      Tool Layer
   ┌──────────────────────────────┐
   │ SSH (14 hosts)              │
   │ Vault (Obsidian filesystem) │
   │ Web Search (SearXNG)        │
   │ Home Assistant REST API     │
   │ Shell (local commands)      │
   └──────────────────────────────┘
```

Requests are classified by a deterministic heuristic function (no LLM call) that maps each user message to one of three roles. System prompts are loaded from external markdown files at runtime — edit them without restarting.

## Model Lineup

| Role | Model | VRAM (Q4) | Speed | Purpose |
|------|-------|-----------|-------|---------|
| Coder | `qwen3-coder:30b` | ~17 GB | ~40 tok/s | MoE (3.3B active). Code, scripts, tool chains, SSH, infra. Primary workhorse. |
| Worker | `qwen3-coder:30b` | (shared) | (shared) | Complex reasoning, synthesis, planning. Same model, different system prompt. |
| Voice | `llama3.2:3b` | ~2.5 GB | ~60 tok/s | Home Assistant voice control. Meta's native tool calling. |
| Embeddings | `nomic-embed-text` | ~0.3 GB | N/A | Vault RAG via ChromaDB. |

Total VRAM budget: ~17-20 GB on a 24 GB RTX 3090. The MoE coder model activates only 3.3B parameters per token, giving large-model quality at small-model speed.

## Requirements

- Linux host with NVIDIA GPU (tested on RTX 3090, 24 GB)
- Docker + Docker Compose (for supporting services)
- [Ollama](https://ollama.com) with GPU support
- Python 3.11+
- Optional: Home Assistant, Obsidian vault, Proxmox VE cluster

## Installation

### 1. Clone and set up Python

```bash
git clone https://github.com/Ehaton/Odin-AI-Agent-Stack.git /opt/Odin
cd /opt/Odin
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp example.env .env
```

Edit `.env` with your values:

```env
# Required
ODIN_USER=odin
ODIN_PASS=your-strong-password

# Ollama
OLLAMA_HOST=http://localhost:11434

# Vault (pick one)
ODIN_VAULT_PATH=/path/to/your/obsidian/vault    # Filesystem mount
# OR
OBSIDIAN_API_KEY=your-key                        # Obsidian REST API

# Optional
HASS_URL=http://homeassistant.local:8123
HASS_TOKEN=your-ha-long-lived-token
TAILNET_NAME=your-machine.tailnet-name.ts.net    # Enables HTTPS
SEARXNG_URL=http://localhost:8080                 # If not using Docker networking
```

### 3. Pull models

```bash
ollama pull qwen3-coder:30b
ollama pull qwen3:8b
ollama pull llama3.2:3b
ollama pull nomic-embed-text
```

### 4. Configure your host inventory

```bash
cp example.hosts.json hosts.json
```

Edit `hosts.json` to list the machines Odin should manage:

```json
{
  "MyServer": {
    "host": "192.168.1.100",
    "user": "root",
    "description": "Web server running nginx"
  }
}
```

Deploy SSH keys to all hosts:

```bash
for host in $(jq -r 'to_entries[] | "\(.value.user)@\(.value.host)"' hosts.json); do
  ssh-copy-id -i ~/.ssh/id_ed25519.pub "$host"
done
```

### 5. Deploy supporting services

```bash
docker compose up -d
```

This starts Ollama (with GPU), SearXNG, ChromaDB, Whisper, Piper, and OpenWakeWord. Odin itself runs outside Docker.

### 6. Start Odin

```bash
source venv/bin/activate
python Odin.py
```

Web UI: `http://localhost:5050` (or your Tailscale hostname for HTTPS).

To install as an app: Chrome/Edge → menu → "Install Odin", or on Android → "Add to home screen".

## Project Layout

```
/opt/Odin/
├── Odin.py                     # Main Flask app + tool dispatch + model routing
├── model_registry.py           # Dynamic model config loader from models.yaml
├── models.yaml                 # Model lineup, roles, routing thresholds
├── hosts.json                  # SSH host inventory (not committed)
├── requirements.txt
├── docker-compose.yml          # Supporting services (Ollama, SearXNG, etc.)
├── static/
│   └── odin.html               # Web UI (single-page chat interface)
├── Odins_Self/
│   └── prompts/
│       └── v3/                 # Active system prompts (editable at runtime)
│           ├── worker.md       # Complex reasoning, synthesis, multi-host
│           ├── coder.md        # Code generation, simple infra, tool chains
│           └── voice.md        # Home Assistant voice control
├── scripts/
│   └── embed_vault.py          # ChromaDB vault embedding script
├── tools/                      # Tool module (future use — not yet wired in)
├── example.env                 # Template for .env
├── example.hosts.json          # Template for hosts.json
└── searxng-settings.yml        # SearXNG configuration
```

## Customizing

### Swap models

Edit `models.yaml` — change the `roles:` section to point at different Ollama tags. Hot-reload without restarting:

```bash
curl -X POST http://localhost:5050/api/models/reload
```

### Edit system prompts

The prompts live in `Odins_Self/prompts/v3/*.md`. Each file supports two template variables:

- `{current_time}` — replaced with the current date and time
- `{hosts}` — replaced with the SSH host inventory from `hosts.json`

Edit any file, then reload:

```bash
curl -X POST http://localhost:5050/api/models/reload
```

Or restart Odin. No Modelfile rebuilds needed.

### Add SSH hosts

Edit `hosts.json`, then restart Odin. The host list is injected into system prompts and the `run_ssh` tool's enum automatically.

### Routing thresholds

The `routing:` section in `models.yaml` controls when queries escalate from the fast coder to the full reasoner:

```yaml
routing:
  complex_host_threshold: 2     # 2+ host mentions → worker model
  complex_word_threshold: 30    # 30+ word queries → worker model
  voice_max_words: 15           # Short HA commands → voice model
```

## Ollama Performance Tuning

The `docker-compose.yml` sets these for the RTX 3090:

```yaml
environment:
  - OLLAMA_KEEP_ALIVE=10m           # Unload after 10 min idle
  - OLLAMA_MAX_LOADED_MODELS=2      # Max concurrent models in VRAM
  - OLLAMA_FLASH_ATTENTION=1        # ~15-25% faster on 30-series GPUs
  - OLLAMA_NUM_PARALLEL=1           # Single user — serialize requests
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/chat` | POST | Send a message, get a response |
| `/api/chats` | GET | List all chats |
| `/api/chats` | POST | Create a new chat |
| `/api/chats/<id>` | GET | Get chat messages |
| `/api/chats/<id>` | DELETE | Delete a chat |
| `/api/chats/<id>/rename` | POST | Rename a chat |
| `/api/models` | GET | List available models |
| `/api/models/reload` | POST | Hot-reload models.yaml and prompts |
| `/api/upload` | POST | Upload a file for analysis |
| `/api/status` | GET | System status |
| `/api/hosts` | GET | SSH host inventory |
| `/api/logs` | GET | Recent event log |

## Security Warning

This codebase gives an LLM SSH access to your infrastructure. The destructive-command guardrail catches many bad outcomes, but not all of them. Run this inside a trust boundary you're comfortable with, don't expose the web UI to the public internet without authentication, and read the code before you run it.

## Acknowledgments

- [Ollama](https://ollama.com) — the model runtime that makes this practical on a single GPU
- [SearXNG](https://github.com/searxng/searxng), [ChromaDB](https://github.com/chroma-core/chroma) — the self-hosted backbone
- [dontriskit/awesome-ai-system-prompts](https://github.com/dontriskit/awesome-ai-system-prompts) — reference for agent prompt patterns

## License

MIT — see [LICENSE](LICENSE).
