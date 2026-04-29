# Odin — BeanLab AI Agent Stack

![Python](https://img.shields.io/badge/python-3.12-blue)
![Ollama](https://img.shields.io/badge/ollama-native-green)
![Models](https://img.shields.io/badge/models-qwen3.6%3A27b%20%7C%20qwen3%3A4b%20%7C%20llama3.1%3A8b-orange)
![License](https://img.shields.io/github/license/Ehaton/Odin-AI-Agent-Stack)

A self-hosted, voice-enabled AI agent running locally on Ollama. Built for homelab operators who want a single assistant that can SSH across their infrastructure, reason about it, control Home Assistant, generate images, search the web, and remember everything via an Obsidian vault — without sending any data to third-party APIs.

> **Heads up:** This is a personal homelab project built for BeanLab. Hostnames, IPs, and paths are specific to that environment and will need editing for yours. Published as a reference, not a turnkey product.

---

## What Odin Does

- **Multi-model routing.** Three Ollama models work together: a fast coder/executor, a heavy reasoner with vision, and a voice-optimized model for Home Assistant. Routing is deterministic heuristic — no LLM call required to classify.
- **Parallel SSH orchestration.** Single-prompt commands fan out to every host simultaneously using `ThreadPoolExecutor`. Destructive-command guardrails block dangerous operations.
- **Home Assistant control.** Natural language → HA service calls via REST API. Turn on lights, set thermostat, control media.
- **Proxmox VE management.** Direct REST API access to cluster nodes, VMs, and LXC containers via `proxmoxer`.
- **Vault-backed memory.** Filesystem-level search over an Obsidian vault. Reads/writes notes directly.
- **Web search.** Self-hosted SearXNG aggregator. No third-party API keys.
- **Local image generation.** ComfyUI with FLUX.1-schnell. VRAM-aware — Ollama auto-unloads models when image generation needs the GPU.
- **Voice interface.** Wyoming Protocol stack: Whisper STT, Piper TTS, OpenWakeWord with custom wake word.
- **Web UI.** Flask-based chat with model picker, file uploads (PDF, images, code), voice modal, multi-chat sidebar with project grouping, PWA-installable.

---

## Architecture

```
User (browser / voice)
        │
        ▼
 classify() heuristic
        │
   ┌────┴────────────────┐
   │                     │
   ▼                     ▼
infra_simple / code    infra / reasoning / vision
qwen3-fast:4b          qwen3.6:27b
(fast executor)        (heavy reasoner)
   │                     │
   └────────┬────────────┘
            │
            ▼
       Tool Dispatcher (parallel)
            │
    ┌───────┼───────────────────┐
    │       │                   │
    ▼       ▼                   ▼
 run_ssh  vault_search    web_fetch
 run_cmd  vault_read/write ha_* tools
 get_ip   image_gen        proxmox_api
```

Routing priority (heuristic, no LLM call):
1. **Voice / HA intent** → `llama3.1:8b` with HA-only tools
2. **Code + simple single-host infra** → `qwen3-fast:4b` (fast, thinking disabled)
3. **Complex multi-host / reasoning / vision** → `qwen3.6:27b`
4. **Trivial greetings** → `llama3.1:8b`

---

## Model Lineup (April 2026)

| Role | Ollama Tag | VRAM | Purpose |
|------|-----------|------|---------|
| Worker / Reasoner | `qwen3.6:27b` | ~18 GB | Complex reasoning, multi-host synthesis, vision, written output |
| Coder / Executor | `qwen3-fast:4b` | ~3.5 GB | Code gen, simple infra, SSH, fast tool calls. Thinking permanently disabled. |
| Voice | `llama3.1:8b` | ~5 GB | Home Assistant voice control. Short, spoken responses. |
| Embeddings | `nomic-embed-text` | ~0.3 GB | Vault/RAG embeddings (ChromaDB) |

Total active VRAM: ~22 GB on a 24 GB RTX 3090. Image generation (FLUX.1-schnell, ~12 GB) triggers automatic model unload via Ollama's keep-alive system.

---

## Stack Components

| Component | Image | Port | Purpose |
|-----------|-------|------|---------|
| Ollama | `ollama/ollama` | 11434 | LLM inference, GPU passthrough |
| Open WebUI | `open-webui` | 3000 | Secondary UI with RAG, knowledge base |
| Odin | `beanlab/odin` | 5050 | Primary Flask UI + agent backend |
| ComfyUI | `pytorch/pytorch` | 8188 | Image generation (FLUX.1-schnell) |
| SearXNG | `searxng/searxng` | 8080 | Self-hosted web search |
| ChromaDB | `chromadb/chroma` | 8000 | Vector store for vault RAG |
| Whisper | `wyoming-whisper` | 10300 | Speech-to-text (medium, CPU) |
| Piper | `wyoming-piper` | 10200 | Text-to-speech (en_US-lessac-high) |
| OpenWakeWord | `wyoming-openwakeword` | 10400 | Wake word detection (hey_odin) |

---

## Requirements

- Linux host with NVIDIA GPU (tested on RTX 3090, 24 GB VRAM)
- Docker + Docker Compose v2
- Python 3.11+
- SSH key access to managed hosts
- Optional: Proxmox VE cluster, Home Assistant, Obsidian vault mount

---

## Installation

### 1. Clone and configure

```bash
git clone https://github.com/Ehaton/Odin-AI-Agent-Stack.git
cd Odin-AI-Agent-Stack

cp .env.example .env
# Edit .env — set ODIN_USER, ODIN_PASS, OLLAMA_HOST, HASS_URL, HASS_TOKEN, etc.
```

### 2. Pull models

```bash
ollama pull qwen3.6:27b
ollama pull qwen3:4b
ollama pull llama3.1:8b
ollama pull nomic-embed-text

# Build the no-think coder variant
docker exec -it ollama bash -c "
cat > /tmp/Modelfile << 'EOF'
FROM qwen3:4b
PARAMETER num_ctx 8192
PARAMETER temperature 0.3
SYSTEM \"Output only your final answer. No reasoning, no preamble.\"
EOF
ollama create qwen3-fast:4b -f /tmp/Modelfile
"
```

### 3. Configure your host inventory

Edit `hosts.json` with your machines:

```json
{
  "YourHost": {
    "host": "192.168.1.100",
    "user": "your-user",
    "description": "What this host does"
  }
}
```

Deploy SSH keys: `./scripts/push_ssh_keys.sh`

### 4. Deploy

```bash
chmod +x deploy.sh

# Dev mode (direct Python, no container)
./deploy.sh dev

# Production (Docker container)
./deploy.sh deploy
```

Web UI: `http://YOUR_IP:5050`
With Tailscale TLS: `https://YOUR_HOST.tailXXXX.ts.net:5050`

---

## Project Layout

```
Odin-AI-Agent-Stack/
├── Odin.py                    # Main Flask app + Ollama client + tool dispatch
├── model_registry.py          # models.yaml loader + hot-reload support
├── models.yaml                # Model definitions and role assignments
├── hosts.json                 # SSH host inventory
├── hosts.example.json         # Template — copy and edit
├── orchestrating_engine.py    # Multi-agent security response simulation
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── deploy.sh                  # ./deploy.sh [deploy|dev|venv|restart]
├── searxng-settings.yml       # SearXNG engine configuration
├── tools/
│   ├── __init__.py
│   ├── base.py
│   ├── home_assistant.py
│   ├── image_gen.py
│   ├── proxmox_api.py
│   ├── vault_rag.py
│   └── web_search.py
├── Odins_Self/
│   └── prompts/               # Historical prompt versions (v1, v2, baseline)
├── scripts/
│   └── push_ssh_keys.sh
├── .env.example
└── .gitignore
```

---

## Customizing for Your Environment

Things you'll need to edit:

- **`hosts.json`** — your actual host inventory and SSH users
- **`.env`** — service URLs, auth tokens, vault path, Tailscale hostname
- **`models.yaml`** — swap in your own models, tune context windows and temperature
- **`Odin.py`** `get_system_prompt()` — the worker persona mentions BeanLab/Chad; edit to match your identity
- **`Odin.py`** `classify()` — host alias keywords in the infra scoring dict reference BeanLab names

---

## Key Features Deep Dive

### Dynamic Model Registry
`models.yaml` drives everything — labels, VRAM allocation, context window, thinking mode, routing thresholds. Hot-reload without restart:
```bash
curl -X POST http://localhost:5050/api/models/reload
```

### Parallel Tool Execution
Multi-host queries fan out all SSH calls simultaneously. "Uptime on all Proxmox nodes" fires 3 concurrent SSH sessions and returns in ~350ms.

### Thinking Suppression
`qwen3-fast:4b` has thinking disabled at three layers: Modelfile SYSTEM prompt, `think=false` in Ollama API payload, and `<think>` tag stripping in the response parser.

### Destructive Command Guardrail
`is_dangerous()` pattern-matches against `rm`, `reboot`, `shutdown`, `docker stop`, `qm destroy`, and 15 other patterns before any shell command executes.

---

## Acknowledgments

- [Ollama](https://ollama.com) — GPU inference runtime
- [SearXNG](https://github.com/searxng/searxng), [ChromaDB](https://github.com/chroma-core/chroma), [ComfyUI](https://github.com/comfyanonymous/ComfyUI) — self-hosted backbone
- [rhasspy/wyoming](https://github.com/rhasspy/wyoming) — voice pipeline (Whisper, Piper, OpenWakeWord)

---

## Security Warning

This codebase gives an LLM SSH access to your infrastructure. The destructive-command guardrail catches many bad outcomes but not all. Run inside a trust boundary you're comfortable with. Do not expose port 5050 to the public internet without the `ODIN_USER`/`ODIN_PASS` auth enforced. Read the code before you run it.

---

## License

MIT — see [LICENSE](LICENSE).
