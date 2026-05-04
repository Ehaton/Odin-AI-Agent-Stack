"""
ODIN — Voice-Enabled AI Assistant
==================================
Multi-chat sidebar, voice modal with audio-reactive visualizer,
manual model selection, file uploads, and PWA-installable on
Windows and Android.

Setup:
    pip install -r requirements.txt

Configuration (via environment variables or .env file):
    OLLAMA_HOST         URL of your Ollama instance
    OBSIDIAN_API_KEY    Local REST API key for Obsidian (optional)
    OBSIDIAN_URL        Obsidian Local REST API URL (default: localhost:27123)
    TAILNET_NAME        Tailscale MagicDNS name for HTTPS (optional)
    ODIN_USER           Basic auth username
    ODIN_PASS           Basic auth password (REQUIRED in production)
    ODIN_PORT           Web port (default: 5050)
    ODIN_DB             SQLite database path (default: odin.db)
    ODIN_HOSTS_FILE     Path to SSH hosts JSON config (default: hosts.json)
    ODIN_ALLOW_NOAUTH   Set to "1" to permit running without ODIN_PASS (dev only)

See .env.example and hosts.example.json for configuration templates.

Usage:
    python odin.py

    Open in any browser on your network or tailnet.
    To install as app: Chrome/Edge -> menu -> "Install Odin"
                       Android Chrome -> menu -> "Add to home screen"
"""

import os
import sys
import json

# Load .env file if present (optional dependency)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optional - env vars can still be set externally

import time
import re
import datetime
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import collections
import hashlib
import base64
import requests as req
from pathlib import Path
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    MST = ZoneInfo("America/Denver")
except Exception:
    # Windows without `tzdata` package, or other failure
    # Fixed-offset fallback — no DST, but keeps the app running.
    # Recommend: pip install tzdata
    print("  ⚠️  zoneinfo unavailable (install `tzdata` on Windows for DST-aware scheduling)")
    MST = datetime.timezone(datetime.timedelta(hours=-7))

try:
    from flask import Flask, request, jsonify, Response
    from flask_cors import CORS
except ImportError:
    print("Missing packages. Run:")
    print("  pip install flask flask-cors requests paramiko")
    sys.exit(1)

try:
    import paramiko
    HAS_SSH = True
except ImportError:
    HAS_SSH = False

# Dynamic model registry — loaded from models.yaml on startup.
# Edit models.yaml to add/remove/tune models without touching this file.
try:
    from model_registry import registry
except ImportError as e:
    print("Failed to load model_registry. Is models.yaml present and pyyaml installed?")
    print(f"  {e}")
    print("  pip install pyyaml")
    sys.exit(1)

_odin_start_time = time.time()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OBSIDIAN_URL = os.environ.get("OBSIDIAN_URL", "http://127.0.0.1:27123")
OBSIDIAN_API_KEY = os.environ.get("OBSIDIAN_API_KEY")
VAULT_PATH = os.environ.get("ODIN_VAULT_PATH", "")  # Path to Obsidian vault on filesystem (e.g. SMB mount)
HASS_URL = os.environ.get("HASS_URL", "")            # e.g. http://homeassistant.local:8123
HASS_TOKEN = os.environ.get("HASS_TOKEN", "")        # Long-lived access token from HA profile

# ── Claude API (optional cloud reasoner) ──
# Add ANTHROPIC_API_KEY to .env to enable. Leave blank for fully local operation.
# Set a monthly spend cap at console.anthropic.com → Billing → Usage Limits.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Per-call token cap (default 2048 ≈ $0.03/call on Sonnet). Controls max spend per response.
ANTHROPIC_MAX_TOKENS = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "2048"))
WEB_PORT = int(os.environ.get("ODIN_PORT", "5050"))
DB_PATH = os.environ.get("ODIN_DB", "odin.db")
HOSTS_FILE = os.environ.get("ODIN_HOSTS_FILE", "hosts.json")
PURGE_AFTER_DAYS = 7

# ---------------------------------------------------------------------------
# Model Configuration — loaded from models.yaml
# ---------------------------------------------------------------------------
# All model definitions, role assignments, and routing thresholds live in
# models.yaml. Edit that file to swap models or tune routing without
# touching this code. Use `python model_registry.py` to validate the YAML.
#
# - MODELS:              role → Ollama model tag (used by classify() and UI)
# - MODEL_INFO:          model tag → metadata dict (UI picker + inference params)
# - CATEGORIES:          classify() category → role
# - DEFAULT_VISION_MODEL: the tag used when auto-routing images
# - _ROUTING:            heuristic thresholds used by classify()
#
MODELS = registry.roles
MODEL_INFO = registry.models
CATEGORIES = registry.categories
DEFAULT_VISION_MODEL = registry.default_vision_model
_ROUTING = registry.routing

def _load_ssh_hosts():
    """Load SSH host definitions from a JSON file. Returns empty dict if not found."""
    try:
        if os.path.exists(HOSTS_FILE):
            with open(HOSTS_FILE, "r") as f:
                return json.load(f)
        return {}
    except Exception as e:
        print(f"  ⚠️  Could not load {HOSTS_FILE}: {e}")
        return {}

SSH_HOSTS = _load_ssh_hosts()

def _resolve_host_ip(info: dict) -> str:
    """Return host IP — supports both flat {host} and nested {lan/tailscale} formats."""
    ip = info.get("host") or info.get("lan") or info.get("tailscale")
    if not ip:
        raise ValueError(f"Host has no IP: {info}")
    return ip

DANGEROUS_PATTERNS = [
    r"\brm\b", r"\brmdir\b", r"\bdel\b", r"\breboot\b", r"\bshutdown\b", r"\bpoweroff\b",
    r"\bsystemctl\s+(stop|disable|mask|restart networking)", r"\bdocker\s+(rm|stop|kill|prune|restart)",
    r"\bqm\s+(stop|destroy|shutdown)", r"\bpct\s+(stop|destroy|shutdown)",
    r"\bmkfs\b", r"\bfdisk\b", r"\bdd\b", r"\buserdel\b",
    # Network-modifying — added after ARP table incident
    r"\bip\s+neigh\s+(flush|del)", r"\bip\s+link\s+set\b", r"\bip\s+route\s+del\b",
    r"\biptables\s+-F\b", r"\bnft\s+flush\b",
    r"\bsystemctl\s+restart\s+(networking|network|NetworkManager|systemd-networkd)\b",
]

def is_dangerous(cmd):
    return any(re.search(p, cmd, re.IGNORECASE) for p in DANGEROUS_PATTERNS)


# ---------------------------------------------------------------------------
# Backend Components
# ---------------------------------------------------------------------------
class FileSystemVault:
    """Read/write/search an Obsidian vault directly from the filesystem (e.g. SMB mount)."""
    def __init__(self, vault_path):
        self.root = Path(vault_path)
        self.connected = self.root.is_dir()
        if self.connected:
            # Quick sanity: count .md files
            md_count = sum(1 for _ in self.root.rglob("*.md"))
            print(f"  📚 Vault (filesystem): {self.root} ({md_count} notes)")

    def search(self, query, n=5):
        if not self.connected: return []
        query_lower = query.lower()
        results = []
        try:
            for md in self.root.rglob("*.md"):
                # Skip .obsidian and .trash directories
                parts = md.relative_to(self.root).parts
                if any(p.startswith(".") for p in parts):
                    continue
                try:
                    text = md.read_text(encoding="utf-8", errors="replace")
                    if query_lower in text.lower():
                        # Find matching context
                        idx = text.lower().find(query_lower)
                        start = max(0, idx - 100)
                        end = min(len(text), idx + len(query) + 200)
                        context = text[start:end].strip()
                        rel_path = str(md.relative_to(self.root)).replace("\\", "/")
                        results.append({
                            "filename": rel_path,
                            "matches": [{"context": context}]
                        })
                        if len(results) >= n:
                            break
                except Exception:
                    continue
        except Exception:
            pass
        return results

    def read(self, path):
        if not self.connected: return "Vault not connected"
        try:
            target = self.root / path
            # Security: prevent path traversal
            target = target.resolve()
            if not str(target).startswith(str(self.root.resolve())):
                return "Access denied: path traversal attempt"
            if target.is_file():
                return target.read_text(encoding="utf-8", errors="replace")
            return f"Not found: {path}"
        except Exception as e:
            return str(e)

    def write(self, path, content, mode="append"):
        if not self.connected: return "Vault not connected"
        try:
            target = self.root / path
            target = target.resolve()
            if not str(target).startswith(str(self.root.resolve())):
                return "Access denied: path traversal attempt"
            target.parent.mkdir(parents=True, exist_ok=True)
            if mode == "overwrite":
                target.write_text(content, encoding="utf-8")
            elif mode == "prepend":
                existing = target.read_text(encoding="utf-8") if target.exists() else ""
                target.write_text(content + "\n" + existing, encoding="utf-8")
            else:  # append
                with open(target, "a", encoding="utf-8") as f:
                    f.write("\n" + content)
            return "Success"
        except Exception as e:
            return str(e)


class ObsidianVault:
    """Access vault via Obsidian Local REST API (requires Obsidian app running)."""
    def __init__(self, url, key):
        self.url, self.headers, self.connected = url.rstrip("/"), {"Authorization": f"Bearer {key}"}, False
        try:
            r = req.get(f"{self.url}/", headers=self.headers, timeout=3)
            self.connected = r.status_code == 200 and r.json().get("authenticated", False)
        except: pass

    def search(self, query, n=5):
        if not self.connected: return []
        try:
            r = req.post(f"{self.url}/search/simple/?query={req.utils.quote(query)}", headers=self.headers, timeout=10)
            return r.json()[:n] if r.status_code == 200 else []
        except: return []

    def read(self, path):
        if not self.connected: return "Vault not connected"
        try:
            r = req.get(f"{self.url}/vault/{path}", headers={**self.headers, "Accept": "text/markdown"}, timeout=10)
            return r.text if r.status_code == 200 else f"Not found: {path}"
        except Exception as e: return str(e)

    def write(self, path, content, mode="append"):
        if not self.connected: return "Vault not connected"
        try:
            if mode == "overwrite":
                r = req.put(f"{self.url}/vault/{path}", headers={**self.headers, "Content-Type": "text/markdown"}, data=content.encode(), timeout=10)
            else:
                r = req.post(f"{self.url}/vault/{path}", headers={**self.headers, "Content-Type": "text/markdown", "Content-Insertion-Position": "end" if mode == "append" else "beginning"}, data=content.encode(), timeout=10)
            return "Success" if r.status_code in (200, 204) else f"Failed: {r.status_code}"
        except Exception as e: return str(e)

class ShellExecutor:
    def __init__(self):
        self.ssh_clients = {}
        # Per-host locks guard both client creation and command execution.
        # paramiko's SSHClient.exec_command is not safe to call concurrently
        # from multiple threads on the same client — channels can interleave.
        self._host_locks = {}
        self._host_locks_lock = threading.Lock()

    def _lock_for(self, alias):
        with self._host_locks_lock:
            lock = self._host_locks.get(alias)
            if lock is None:
                lock = threading.Lock()
                self._host_locks[alias] = lock
            return lock

    def run_local(self, cmd, timeout=30):
        # subprocess.run is fully thread-safe — each call spawns its own process.
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            return {"stdout": r.stdout[:3000], "stderr": r.stderr[:1000], "returncode": r.returncode}
        except Exception as e: return {"error": str(e)}

    def run_ssh(self, alias, cmd, timeout=30):
        if not HAS_SSH: return {"error": "paramiko not installed"}
        if alias not in SSH_HOSTS: return {"error": f"Unknown host: {alias}"}
        info = SSH_HOSTS[alias]
        lock = self._lock_for(alias)
        with lock:
            try:
                if alias not in self.ssh_clients or not self.ssh_clients[alias].get_transport():
                    c = paramiko.SSHClient()
                    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    c.connect(_resolve_host_ip(info), username=info["user"], timeout=10)
                    self.ssh_clients[alias] = c
                _, stdout, stderr = self.ssh_clients[alias].exec_command(cmd, timeout=timeout)
                return {"stdout": stdout.read().decode("utf-8", errors="replace")[:3000],
                        "stderr": stderr.read().decode("utf-8", errors="replace")[:1000],
                        "returncode": stdout.channel.recv_exit_status()}
            except Exception as e: return {"error": str(e)}

class WebFetcher:
    def fetch(self, url, max_len=4000):
        try:
            r = req.get(url, headers={"User-Agent": "Jarvis/4.0"}, timeout=15)
            text = re.sub(r'<script.*?</script>', '', r.text, flags=re.DOTALL)
            text = re.sub(r'<style.*?</style>', '', text, flags=re.DOTALL)
            text = re.sub(r'<[^>]+>', ' ', text)
            return {"content": re.sub(r'\s+', ' ', text).strip()[:max_len]}
        except Exception as e: return {"error": str(e)}

    def public_ip(self):
        try: return req.get("https://api.ipify.org", timeout=5).text
        except: return "unavailable"


class HomeAssistant:
    """Client for Home Assistant's REST API.

    Activates only if both HASS_URL and HASS_TOKEN env vars are set. Otherwise
    `connected` stays False and all tool calls return a clear "not configured"
    error so the voice model understands why things don't work.

    Only implements a minimal surface — turn_on/off/toggle, state query,
    entity listing. Add more as needed.
    """
    def __init__(self, url, token):
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        self.connected = False
        if not self.url or not self.token:
            return
        try:
            r = req.get(f"{self.url}/api/", headers=self.headers, timeout=3)
            # /api/ returns {"message": "API running."} when auth is valid
            self.connected = r.status_code == 200
        except Exception:
            self.connected = False

    def _not_configured(self):
        return {"error": "home_assistant_not_configured",
                "message": "Home Assistant integration is not set up. "
                           "Set HASS_URL and HASS_TOKEN in .env and restart Odin."}

    def call_service(self, domain, service, entity_id=None, **service_data):
        """Generic /api/services/<domain>/<service> POST."""
        if not self.connected:
            return self._not_configured()
        try:
            payload = dict(service_data)
            if entity_id:
                payload["entity_id"] = entity_id
            r = req.post(
                f"{self.url}/api/services/{domain}/{service}",
                headers=self.headers,
                json=payload,
                timeout=10,
            )
            if r.status_code in (200, 201):
                # HA returns a list of changed state objects — keep a short summary
                try:
                    changed = r.json()
                    summary = [{"entity_id": s.get("entity_id"),
                                "state": s.get("state")}
                               for s in changed[:5]]
                    return {"ok": True, "service": f"{domain}.{service}",
                            "changed": summary}
                except Exception:
                    return {"ok": True, "service": f"{domain}.{service}"}
            return {"error": f"HA returned HTTP {r.status_code}",
                    "body": r.text[:200]}
        except Exception as e:
            return {"error": str(e)[:200]}

    def get_state(self, entity_id):
        """Fetch current state of a single entity."""
        if not self.connected:
            return self._not_configured()
        try:
            r = req.get(f"{self.url}/api/states/{entity_id}",
                        headers=self.headers, timeout=5)
            if r.status_code == 200:
                d = r.json()
                return {"entity_id": d.get("entity_id"),
                        "state": d.get("state"),
                        "attributes": d.get("attributes", {})}
            if r.status_code == 404:
                return {"error": f"Entity not found: {entity_id}"}
            return {"error": f"HA HTTP {r.status_code}"}
        except Exception as e:
            return {"error": str(e)[:200]}

    def list_entities(self, domain_filter=None):
        """List available entities, optionally filtered by domain (e.g. 'light').

        Returns a trimmed list — HA setups often have 200+ entities and we
        don't want to dump them all into the model's context window.
        """
        if not self.connected:
            return self._not_configured()
        try:
            r = req.get(f"{self.url}/api/states",
                        headers=self.headers, timeout=5)
            if r.status_code != 200:
                return {"error": f"HA HTTP {r.status_code}"}
            all_states = r.json()
            items = []
            for s in all_states:
                eid = s.get("entity_id", "")
                if domain_filter and not eid.startswith(f"{domain_filter}."):
                    continue
                items.append({
                    "entity_id": eid,
                    "state": s.get("state"),
                    "friendly_name": s.get("attributes", {}).get("friendly_name", ""),
                })
                if len(items) >= 50:  # cap to keep context small
                    break
            return {"entities": items, "count": len(items),
                    "truncated": len(items) >= 50}
        except Exception as e:
            return {"error": str(e)[:200]}


# ---------------------------------------------------------------------------
# Logger — in-memory ring buffer + file log
# ---------------------------------------------------------------------------
class JarvisLogger:
    def __init__(self, max_events=500, log_file="odin.log"):
        self.buffer = collections.deque(maxlen=max_events)
        self.log_file = log_file
        self.lock = threading.Lock()

    def log(self, event_type, **fields):
        entry = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "type": event_type,
            **fields,
        }
        with self.lock:
            self.buffer.append(entry)
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, default=str) + "\n")
            except Exception:
                pass
        # Also print to stdout for terminal visibility
        print(f"[{entry['ts']}] {event_type}: {json.dumps({k: v for k, v in fields.items() if k != 'result'}, default=str)[:200]}")

    def recent(self, n=100, session_id=None):
        with self.lock:
            items = list(self.buffer)
        if session_id:
            items = [e for e in items if e.get("session_id") == session_id]
        return items[-n:]

logger = JarvisLogger()


# ---------------------------------------------------------------------------
# Database — SQLite chat persistence
# ---------------------------------------------------------------------------
def _iso_to_ms(iso_str):
    """Parse our stored ISO timestamp (with MST tz) into millis since epoch."""
    if not iso_str:
        return int(time.time() * 1000)
    try:
        return int(datetime.datetime.fromisoformat(iso_str).timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)


class ChatDatabase:
    def __init__(self, path=DB_PATH):
        self.path = path
        self.lock = threading.Lock()
        self._init_schema()

    def _conn(self):
        c = sqlite3.connect(self.path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    def _init_schema(self):
        with self.lock, self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'New chat',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tool_calls TEXT,
                tool_call_id TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                icon TEXT NOT NULL DEFAULT '💬',
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id);
            CREATE INDEX IF NOT EXISTS idx_chats_updated ON chats(updated_at);
            """)
            # Migration: add project_id to chats if missing
            cols = {r["name"] for r in c.execute("PRAGMA table_info(chats)").fetchall()}
            if "project_id" not in cols:
                c.execute("ALTER TABLE chats ADD COLUMN project_id TEXT NOT NULL DEFAULT 'general'")
                c.execute("CREATE INDEX IF NOT EXISTS idx_chats_project ON chats(project_id)")
            # Seed default General project if projects table is empty
            count = c.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            if count == 0:
                now = datetime.datetime.now(MST).isoformat()
                c.execute(
                    "INSERT INTO projects (id, name, icon, sort_order, created_at) VALUES (?, ?, ?, ?, ?)",
                    ("general", "General", "💬", 0, now)
                )

    def create_chat(self, chat_id, title="New chat", project_id="general"):
        now = datetime.datetime.now(MST).isoformat()
        with self.lock, self._conn() as c:
            c.execute(
                "INSERT INTO chats (id, title, project_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (chat_id, title, project_id, now, now)
            )

    def list_chats(self):
        with self.lock, self._conn() as c:
            rows = c.execute(
                "SELECT id, title, project_id, created_at, updated_at FROM chats ORDER BY updated_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def move_chat(self, chat_id, project_id):
        with self.lock, self._conn() as c:
            c.execute("UPDATE chats SET project_id = ? WHERE id = ?", (project_id, chat_id))

    def get_messages(self, chat_id):
        with self.lock, self._conn() as c:
            rows = c.execute(
                "SELECT role, content, tool_calls, tool_call_id FROM messages WHERE chat_id = ? ORDER BY id ASC",
                (chat_id,)).fetchall()
            out = []
            for r in rows:
                m = {"role": r["role"], "content": r["content"]}
                if r["tool_calls"]:
                    m["tool_calls"] = json.loads(r["tool_calls"])
                if r["tool_call_id"]:
                    m["tool_call_id"] = r["tool_call_id"]
                out.append(m)
            return out

    def get_messages_display(self, chat_id):
        """Messages formatted for the frontend: ts, tool badges, user/ai roles merged."""
        with self.lock, self._conn() as c:
            rows = c.execute(
                "SELECT role, content, tool_calls, tool_call_id, created_at "
                "FROM messages WHERE chat_id = ? ORDER BY id ASC",
                (chat_id,)
            ).fetchall()
        out = []
        pending_badges = []
        for r in rows:
            role = r["role"]
            content = r["content"] or ""
            # Collect tool names from assistant's tool_calls to show as badges on
            # the next assistant message that actually has content.
            if role == "assistant" and r["tool_calls"]:
                try:
                    calls = json.loads(r["tool_calls"])
                    for tc in calls:
                        fn = (tc.get("function") or {}).get("name") or tc.get("name")
                        if fn:
                            pending_badges.append(fn)
                except Exception:
                    pass
            if role == "tool":
                continue  # tool results are internal, not shown
            if not content.strip():
                continue  # skip empty assistant rows that were just tool_calls
            ts_ms = _iso_to_ms(r["created_at"])
            out.append({
                "role": "user" if role == "user" else "ai",
                "content": content,
                "ts": ts_ms,
                "toolBadges": pending_badges if role == "assistant" else [],
            })
            if role == "assistant":
                pending_badges = []
        return out

    # ─── Projects ───
    def list_projects(self):
        with self.lock, self._conn() as c:
            rows = c.execute(
                "SELECT id, name, icon, sort_order FROM projects ORDER BY sort_order ASC, created_at ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def create_project(self, project_id, name, icon="💬"):
        now = datetime.datetime.now(MST).isoformat()
        with self.lock, self._conn() as c:
            max_order = c.execute("SELECT COALESCE(MAX(sort_order), 0) FROM projects").fetchone()[0]
            c.execute(
                "INSERT INTO projects (id, name, icon, sort_order, created_at) VALUES (?, ?, ?, ?, ?)",
                (project_id, name, icon, max_order + 1, now)
            )

    def delete_project(self, project_id):
        if project_id == "general":
            return False
        with self.lock, self._conn() as c:
            # Move any chats in this project back to General, then delete
            c.execute("UPDATE chats SET project_id = 'general' WHERE project_id = ?", (project_id,))
            c.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return True

    def add_message(self, chat_id, role, content, tool_calls=None, tool_call_id=None):
        now = datetime.datetime.now(MST).isoformat()
        with self.lock, self._conn() as c:
            c.execute("INSERT INTO messages (chat_id, role, content, tool_calls, tool_call_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                      (chat_id, role, content or "",
                       json.dumps(tool_calls) if tool_calls else None,
                       tool_call_id, now))
            c.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id))

    def rename_chat(self, chat_id, title):
        with self.lock, self._conn() as c:
            c.execute("UPDATE chats SET title = ? WHERE id = ?", (title[:100], chat_id))

    def delete_chat(self, chat_id):
        with self.lock, self._conn() as c:
            c.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            c.execute("DELETE FROM chats WHERE id = ?", (chat_id,))

    def purge_old(self, days=PURGE_AFTER_DAYS):
        cutoff = (datetime.datetime.now(MST) - datetime.timedelta(days=days)).isoformat()
        with self.lock, self._conn() as c:
            # Find chats to purge
            old_chats = c.execute("SELECT id FROM chats WHERE updated_at < ?", (cutoff,)).fetchall()
            old_ids = [r["id"] for r in old_chats]
            if old_ids:
                placeholders = ",".join("?" * len(old_ids))
                c.execute(f"DELETE FROM messages WHERE chat_id IN ({placeholders})", old_ids)
                c.execute(f"DELETE FROM chats WHERE id IN ({placeholders})", old_ids)
            return len(old_ids)


db = ChatDatabase()


# ---------------------------------------------------------------------------
# Scheduled purge — Mondays 00:00 MST (Sunday midnight)
# ---------------------------------------------------------------------------
def purge_scheduler():
    """Sleep until next Monday 00:00 America/Denver, purge, repeat."""
    while True:
        try:
            now = datetime.datetime.now(MST)
            # Next Monday 00:00 local
            days_until_monday = (7 - now.weekday()) % 7
            if days_until_monday == 0 and now.hour == 0 and now.minute == 0:
                days_until_monday = 7  # just purged, wait a week
            target = (now + datetime.timedelta(days=days_until_monday or 7)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            sleep_seconds = (target - now).total_seconds()
            logger.log("purge_scheduled", next_run=target.isoformat(),
                       sleep_seconds=int(sleep_seconds))
            time.sleep(max(60, sleep_seconds))
            purged = db.purge_old(days=PURGE_AFTER_DAYS)
            logger.log("purge_completed", chats_deleted=purged)
        except Exception as e:
            logger.log("purge_error", error=str(e))
            time.sleep(3600)  # retry in an hour

threading.Thread(target=purge_scheduler, daemon=True).start()


# ---------------------------------------------------------------------------
# Tool definitions and handler
# ---------------------------------------------------------------------------
def get_tools(vault, scope="worker"):
    """Return the tool manifest for a given agent scope.

    scope="worker" — full suite: vault, shell, SSH, web, HA, utilities
    scope="coder"  — full suite too. With qwen3:4b as the coder (which
                     supports native tool calling, unlike qwen2.5-coder),
                     the code specialist can now run shell commands,
                     SSH, and fetch web content to help with code tasks.
                     The difference from "worker" is the SYSTEM PROMPT,
                     not the tool list.
    scope="voice"  — HA tools only. The 3B voice model shouldn't see SSH
                     or shell tools because it will misuse them.
    """
    # coder and worker both get the full tool list now
    tools = []

    # ─── Home Assistant tools (voice + worker) ─────────────────────────
    # Always include HA tool definitions so the voice model knows it's
    # supposed to use them, even when HA isn't actually configured yet.
    # The handlers return a clear "not configured" error in that case.
    ha_tools = [
        {"type": "function", "function": {
            "name": "ha_turn_on",
            "description": (
                "Turn ON a Home Assistant entity (light, switch, fan, etc.). "
                "USE FOR: 'turn on the kitchen light', 'activate the fan', 'start the coffee maker'. "
                "DO NOT USE FOR: dimming or setting a specific brightness — use ha_set_state for that. "
                "RETURNS: {ok: true, changed: [{entity_id, state}]}."
            ),
            "parameters": {"type": "object",
                           "properties": {"entity_id": {"type": "string",
                                                        "description": "Full entity_id like 'light.kitchen' or 'switch.coffee_maker'. Use ha_list_entities if unsure."}},
                           "required": ["entity_id"]}}},
        {"type": "function", "function": {
            "name": "ha_turn_off",
            "description": (
                "Turn OFF a Home Assistant entity. "
                "USE FOR: 'turn off the light', 'stop the music', 'close the blinds'. "
                "DO NOT USE FOR: pausing media (use ha_set_state with service=media_pause). "
                "RETURNS: {ok: true, changed: [...]}."
            ),
            "parameters": {"type": "object",
                           "properties": {"entity_id": {"type": "string"}},
                           "required": ["entity_id"]}}},
        {"type": "function", "function": {
            "name": "ha_toggle",
            "description": (
                "TOGGLE a Home Assistant entity on/off. "
                "USE FOR: 'toggle the lamp', 'flip the switch'. "
                "RETURNS: {ok: true, changed: [...]}."
            ),
            "parameters": {"type": "object",
                           "properties": {"entity_id": {"type": "string"}},
                           "required": ["entity_id"]}}},
        {"type": "function", "function": {
            "name": "ha_set_state",
            "description": (
                "Call an arbitrary HA service to change an entity's state. "
                "USE FOR: dimming lights (domain=light, service=turn_on, brightness=128), "
                "setting thermostat temperature (domain=climate, service=set_temperature, temperature=72), "
                "playing media (domain=media_player, service=media_play), "
                "activating scenes (domain=scene, service=turn_on). "
                "DO NOT USE FOR: simple on/off — use ha_turn_on/off instead, they're simpler. "
                "RETURNS: {ok: true, service, changed: [...]}."
            ),
            "parameters": {"type": "object",
                           "properties": {
                               "domain": {"type": "string", "description": "HA domain like 'light', 'climate', 'media_player', 'scene'."},
                               "service": {"type": "string", "description": "HA service name like 'turn_on', 'set_temperature', 'media_play'."},
                               "entity_id": {"type": "string", "description": "Target entity_id (optional for some services)."},
                               "extra": {"type": "object", "description": "Additional service data as a flat object, e.g. {\"brightness\": 128, \"color_name\": \"red\"}."},
                           },
                           "required": ["domain", "service"]}}},
        {"type": "function", "function": {
            "name": "ha_list_entities",
            "description": (
                "List available Home Assistant entities, optionally filtered by domain. "
                "USE FOR: 'what lights do I have', discovering entity_ids before calling turn_on, "
                "finding entities by friendly name. "
                "DO NOT USE FOR: checking a specific entity's current state (use ha_get_state). "
                "RETURNS: {entities: [{entity_id, state, friendly_name}], count, truncated}."
            ),
            "parameters": {"type": "object",
                           "properties": {"domain": {"type": "string", "description": "Optional domain filter like 'light', 'switch', 'climate'. Omit to list everything (capped at 50)."}}}}},
        {"type": "function", "function": {
            "name": "ha_get_state",
            "description": (
                "Get the current state and attributes of a specific HA entity. "
                "USE FOR: 'is the kitchen light on?', 'what's the thermostat set to?', checking if a change took effect. "
                "RETURNS: {entity_id, state, attributes}."
            ),
            "parameters": {"type": "object",
                           "properties": {"entity_id": {"type": "string"}},
                           "required": ["entity_id"]}}},
    ]
    tools.extend(ha_tools)

    # Voice model only gets HA tools. Returning early keeps its schema tiny
    # which helps the 3B model stay focused on the task.
    if scope == "voice":
        return tools

    # ─── Vault tools (worker only) ─────────────────────────────────────
    if vault and vault.connected:
        tools.extend([
            {"type": "function", "function": {
                "name": "vault_search",
                "description": (
                    "Search the user's Obsidian knowledge vault for notes matching a query. "
                    "USE FOR: finding homelab documentation, configs, past decisions, personal notes, "
                    "BeanLab architecture references. Always try this FIRST for homelab questions "
                    "before running shell commands. "
                    "DO NOT USE FOR: general knowledge questions, anything unrelated to the user's notes. "
                    "RETURNS: list of matching files with snippet previews."
                ),
                "parameters": {"type": "object",
                               "properties": {"query": {"type": "string", "description": "Search keywords. Short queries (2-4 words) work best."}},
                               "required": ["query"]}}},
            {"type": "function", "function": {
                "name": "vault_read",
                "description": (
                    "Read the full contents of a specific note from the vault. "
                    "USE FOR: fetching a note you found via vault_search, reading a note by known path. "
                    "DO NOT USE FOR: searching — use vault_search instead. "
                    "RETURNS: note contents as markdown (first 3000 chars)."
                ),
                "parameters": {"type": "object",
                               "properties": {"path": {"type": "string", "description": "Relative path from the vault root, e.g. 'BeanLab/Network.md'."}},
                               "required": ["path"]}}},
            {"type": "function", "function": {
                "name": "vault_write",
                "description": (
                    "Write or append content to a note in the vault. "
                    "USE FOR: saving facts, logging decisions, recording user-requested notes. "
                    "DO NOT USE FOR: writes without explicit user intent — always confirm destructive overwrites. "
                    "RETURNS: success/failure status."
                ),
                "parameters": {"type": "object",
                               "properties": {"path": {"type": "string"},
                                              "content": {"type": "string"},
                                              "mode": {"type": "string", "enum": ["overwrite", "append", "prepend"], "description": "Default is append. Only use overwrite when explicitly asked."}},
                               "required": ["path", "content"]}}},
        ])
    tools.extend([
        {"type": "function", "function": {
            "name": "run_command",
            "description": (
                "Execute a shell command on the local Odin host (ai-stack-420). "
                "USE FOR: checking local processes, reading local files, querying local services, "
                "commands that don't need to target a remote host. "
                "DO NOT USE FOR: destructive operations (they're auto-blocked), interactive commands, "
                "long-running processes (30s timeout), commands that belong on a remote host (use run_ssh instead). "
                "RETURNS: {stdout, stderr, returncode}."
            ),
            "parameters": {"type": "object",
                           "properties": {"command": {"type": "string", "description": "Shell command to run. Keep it single-line and non-interactive."}},
                           "required": ["command"]}}},
        {"type": "function", "function": {
            "name": "run_ssh",
            "description": (
                "Execute a shell command on a remote BeanLab host via SSH. "
                "USE FOR: checking remote system status, reading remote files, running diagnostics "
                "(df, free, uptime, nvidia-smi, systemctl status, etc.), querying service state on "
                "specific hosts. You can call this multiple times in parallel for different hosts. "
                "DO NOT USE FOR: destructive operations (auto-blocked), interactive commands, "
                "long-running processes (30s timeout). "
                "RETURNS: {stdout, stderr, returncode}. "
                "AVAILABLE HOSTS: " + ", ".join(
                    f"{alias} ({SSH_HOSTS[alias].get('description', 'host')})"
                    for alias in SSH_HOSTS
                ) if SSH_HOSTS else "no hosts configured"
            ),
            "parameters": {"type": "object",
                           "properties": {"host": {"type": "string", "enum": list(SSH_HOSTS.keys()), "description": "Alias of the target host from the list above."},
                                          "command": {"type": "string", "description": "Shell command to run on the remote host."}},
                           "required": ["host", "command"]}}},
        {"type": "function", "function": {
            "name": "web_fetch",
            "description": (
                "Fetch and extract plain text from a web URL. "
                "USE FOR: reading documentation pages, fetching current info from a known URL, "
                "checking a specific site. "
                "DO NOT USE FOR: search engines (no URL known in advance), sites requiring auth, "
                "dumping raw HTML to the user. Always summarize the content, never paste it verbatim. "
                "RETURNS: {content} with scripts/styles/tags stripped, capped at 4000 chars."
            ),
            "parameters": {"type": "object",
                           "properties": {"url": {"type": "string", "description": "Full URL including https:// scheme."}},
                           "required": ["url"]}}},
        {"type": "function", "function": {
            "name": "web_search",
            "description": (
                "Search the web via SearXNG for current information. "
                "USE FOR: looking up current events, software versions, documentation, troubleshooting, "
                "anything beyond your training data or that changes over time. "
                "DO NOT USE FOR: questions you can answer directly from knowledge or the vault. "
                "RETURNS: list of results with title, URL, and snippet."
            ),
            "parameters": {"type": "object",
                           "properties": {
                               "query": {"type": "string", "description": "Search query. 3-8 words works best."},
                               "max_results": {"type": "integer", "description": "Number of results (default 5, max 10)."},
                           },
                           "required": ["query"]}}},
        {"type": "function", "function": {
            "description": (
                "Get the user's current public WAN IP address. "
                "USE FOR: 'what's my public IP', network debugging, confirming outbound connectivity. "
                "DO NOT USE FOR: internal LAN IPs (those are in the SSH_HOSTS config or need ip addr on a host). "
                "RETURNS: {public_ip} as a string."
            ),
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "get_current_datetime",
            "description": (
                "Get the current local date, time, and day of week. "
                "USE FOR: 'what time is it', 'what day is it', timestamping a note. "
                "DO NOT USE FOR: timezone conversions (not supported), historical dates. "
                "RETURNS: {date, time, day}."
            ),
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "image_gen",
            "description": (
                "Generate an image from a text prompt using ComfyUI and Flux/SDXL models. "
                "USE FOR: any request to create, generate, draw, or render an image. "
                "Style options: default, photorealistic, portrait, landscape, cinematic, "
                "concept_art, abstract, fast, sketch, fantasy, dnd, artistic, oil_painting, "
                "photo, architecture. "
                "RETURNS: {path, filename, prompt, style, model, seed, width, height}."
            ),
            "parameters": {"type": "object",
                           "properties": {
                               "prompt": {"type": "string",
                                          "description": "Detailed image description. Be specific about subject, lighting, style, mood."},
                               "style":  {"type": "string",
                                          "description": "Style preset controlling which model and sampler are used.",
                                          "enum": ["default","photorealistic","portrait","landscape","cinematic","concept_art","abstract","fast","sketch","fantasy","dnd","artistic","oil_painting","photo","architecture"]},
                               "width":  {"type": "integer", "description": "Width in pixels, multiple of 64 (default 1024, max 2048)."},
                               "height": {"type": "integer", "description": "Height in pixels, multiple of 64 (default 1024, max 2048)."},
                               "seed":   {"type": "integer", "description": "Random seed (0 = random)."},
                           },
                           "required": ["prompt"]}}},
    ])
    return tools

def handle_tool(name, args, vault, shell, web):
    if name == "vault_search":
        results = vault.search(args.get("query",""))
        return json.dumps({"results": [{"file": r.get("filename",""), "snippets": [m.get("context","")[:200] for m in r.get("matches",[])[:2]]} for r in results]})
    elif name == "vault_read": return json.dumps({"content": vault.read(args.get("path",""))[:3000]})
    elif name == "vault_write": return json.dumps({"result": vault.write(args.get("path",""), args.get("content",""), args.get("mode","append"))})
    elif name == "run_command":
        cmd = args.get("command","")
        if is_dangerous(cmd): return json.dumps({"error": f"BLOCKED: '{cmd}' is potentially destructive. Please confirm."})
        return json.dumps(shell.run_local(cmd))
    elif name == "run_ssh":
        cmd = args.get("command","")
        if is_dangerous(cmd): return json.dumps({"error": f"BLOCKED: '{cmd}' on {args.get('host','')} is potentially destructive."})
        return json.dumps(shell.run_ssh(args.get("host",""), cmd))
    elif name == "web_fetch": return json.dumps(web.fetch(args.get("url","")))
    elif name == "web_search":
        query = args.get("query", "").strip()
        max_results = min(int(args.get("max_results", 5)), 15)
        primary_url = os.environ.get("SEARXNG_URL", "http://localhost:8080").rstrip("/")
        fallback_url = "http://searxng:8080"
        payload = None
        last_error = None
        for searxng_url in [primary_url, fallback_url]:
            try:
                r = req.get(f"{searxng_url}/search", params={
                    "q": query, "format": "json", "categories": "general",
                    "language": "en", "safesearch": 0,
                }, timeout=15, headers={"User-Agent": "Odin-Agent/1.0"})
                r.raise_for_status()
                payload = r.json()
                break
            except Exception as e:
                last_error = str(e)
        if payload is None:
            return json.dumps({"error": f"Web search failed: {last_error}"})
        seen_urls = set()
        normalized = []
        for x in payload.get("results", []):
            url = x.get("url", "").strip()
            if url in seen_urls:
                continue
            seen_urls.add(url)
            normalized.append({"title": x.get("title","").strip(),
                               "url": url,
                               "snippet": x.get("content","").strip()[:400]})
            if len(normalized) >= max_results:
                break
        return json.dumps({"results": normalized, "query": query})
    elif name == "get_public_ip": return json.dumps({"public_ip": web.public_ip()})
    elif name == "get_current_datetime":
        now = datetime.datetime.now()
        return json.dumps({"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%I:%M %p"), "day": now.strftime("%A")})
    # ─── Home Assistant tools ────────────────────────────────────────
    # All HA handlers go through the `ha` global. If HA isn't configured,
    # the client returns a clean "not configured" error which the model sees.
    elif name == "ha_turn_on":
        return json.dumps(ha.call_service("homeassistant", "turn_on",
                                          entity_id=args.get("entity_id","")))
    elif name == "ha_turn_off":
        return json.dumps(ha.call_service("homeassistant", "turn_off",
                                          entity_id=args.get("entity_id","")))
    elif name == "ha_toggle":
        return json.dumps(ha.call_service("homeassistant", "toggle",
                                          entity_id=args.get("entity_id","")))
    elif name == "ha_set_state":
        domain = args.get("domain","")
        service = args.get("service","")
        entity_id = args.get("entity_id")
        extra = args.get("extra") or {}
        if not isinstance(extra, dict):
            extra = {}
        return json.dumps(ha.call_service(domain, service,
                                          entity_id=entity_id, **extra))
    elif name == "ha_list_entities":
        return json.dumps(ha.list_entities(args.get("domain")))
    elif name == "ha_get_state":
        return json.dumps(ha.get_state(args.get("entity_id","")))
    elif name == "image_gen":
        try:
            from tools.image_gen import ImageGenTool
            tool = ImageGenTool()
            result = tool.execute(
                prompt=args.get("prompt", ""),
                style=args.get("style", "default"),
                width=int(args.get("width", 1024)),
                height=int(args.get("height", 1024)),
                seed=int(args.get("seed", 0)),
            )
            if result.ok:
                data = result.data
                return json.dumps({
                    "ok": True,
                    "path": data.get("path"),
                    "filename": data.get("filename"),
                    "style": data.get("style"),
                    "model": data.get("model"),
                    "seed": data.get("seed"),
                    "message": f"Image generated and saved to {data.get('path')}",
                })
            else:
                return json.dumps({"ok": False, "error": result.error})
        except Exception as e:
            return json.dumps({"ok": False, "error": f"image_gen failed: {e}"})
    return json.dumps({"error": f"Unknown tool: {name}"})


# ---------------------------------------------------------------------------
# LLM call abstraction — uses Ollama's native /api/chat endpoint so we can
# pass `think: false` for reasoning models like Qwen3 that would otherwise
# burn 30-60 seconds per iteration on <think> tokens. The OpenAI SDK path
# silently drops this flag.
#
# Returns a normalized response object with OpenAI-ish fields so the rest
# of process_message doesn't care which path was taken:
#
#   {
#     "content":         str,     # assistant text, <think> blocks stripped
#     "tool_calls":      list,    # each: {"id": str, "name": str, "arguments": dict}
#     "total_duration_ms": int,   # end-to-end server time
#     "eval_count":      int,     # tokens generated
#   }
# ---------------------------------------------------------------------------
def _translate_history_to_native(messages):
    """Convert DB-stored OpenAI-format history rows to native Ollama /api/chat shape.

    The DB stores messages the way the OpenAI SDK accepts them:
      - assistant rows may have tool_calls: [{"id", "type", "function": {"name", "arguments": str}}]
      - tool rows use tool_call_id to reference the assistant's call

    Native Ollama wants:
      - assistant rows with tool_calls: [{"function": {"name", "arguments": dict}}]
      - tool rows with tool_name (no tool_call_id)

    We also have to handle mixed content: some content fields are strings
    (plain text) and some are multimodal lists (for vision). Ollama native
    accepts both.
    """
    out = []
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            native_tcs = []
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments", {})
                # DB stores OpenAI-style string arguments; parse to dict for native
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args else {}
                    except Exception:
                        args = {"_raw": args}
                native_tcs.append({"function": {"name": name, "arguments": args}})
            out.append({
                "role": "assistant",
                "content": m.get("content") or "",
                "tool_calls": native_tcs,
            })
        elif role == "tool":
            # Ollama native uses tool_name, not tool_call_id. Freshly-built
            # history rows from the current turn carry tool_name directly;
            # rows reloaded from the DB don't, so we try to recover it by
            # walking back to the most recent assistant tool_calls entry
            # and matching by position (N-th tool row -> N-th tool_call).
            tool_name = m.get("tool_name", "")
            if not tool_name:
                # Find the most recent assistant with tool_calls
                prior_asst_idx = None
                for j in range(len(out) - 1, -1, -1):
                    if out[j].get("role") == "assistant" and out[j].get("tool_calls"):
                        prior_asst_idx = j
                        break
                if prior_asst_idx is not None:
                    prior_tcs = out[prior_asst_idx].get("tool_calls", [])
                    # Count how many tool rows we've already emitted since
                    # that assistant row — that's the index into prior_tcs
                    tool_rows_since = sum(
                        1 for k in range(prior_asst_idx + 1, len(out))
                        if out[k].get("role") == "tool"
                    )
                    if tool_rows_since < len(prior_tcs):
                        tool_name = prior_tcs[tool_rows_since].get("function", {}).get("name", "")
            out.append({
                "role": "tool",
                "tool_name": tool_name,
                "content": m.get("content") or "",
            })
        else:
            # user, system, plain assistant — pass through unchanged
            out.append({k: v for k, v in m.items()
                        if k in ("role", "content", "images")})
    return out


def _call_claude(model, info, messages, max_tokens, temperature, timeout):
    """Send a request to the Anthropic Messages API.

    Claude is used as a pure text reasoner — no tools forwarded.
    Tool calls (SSH, HA, vault, web) always stay on local Ollama models.
    Per-call tokens are capped at ANTHROPIC_MAX_TOKENS to control spend.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Add it to .env or select a local model."
        )
    api_model = info.get("api_model", "claude-sonnet-4-6")
    capped_tokens = min(max_tokens, ANTHROPIC_MAX_TOKENS)

    # Convert history to Anthropic format.
    # System messages become the top-level system param.
    # Tool role messages are skipped (Claude doesn't use them in this path).
    anthropic_messages = []
    system_parts = []
    for m in messages:
        role = m.get("role", "")
        text = (m.get("content") or "")
        if role == "system":
            system_parts.append(text)
        elif role == "user":
            anthropic_messages.append({"role": "user", "content": text})
        elif role == "assistant" and text:
            anthropic_messages.append({"role": "assistant", "content": text})
        # tool role: skipped

    # Merge consecutive same-role messages
    merged = []
    for msg in anthropic_messages:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"] += "\n\n" + msg["content"]
        else:
            merged.append(dict(msg))

    if not merged:
        return {"content": "", "tool_calls": [], "total_duration_ms": 0, "eval_count": 0}

    # Anthropic requires the last message to be from the user
    if merged[-1]["role"] != "user":
        merged.append({"role": "user", "content": "Please continue."})

    payload = {
        "model": api_model,
        "max_tokens": capped_tokens,
        "temperature": temperature,
        "messages": merged,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    t0 = time.time()
    try:
        r = req.post("https://api.anthropic.com/v1/messages",
                     json=payload, headers=headers, timeout=timeout)
    except req.exceptions.Timeout:
        raise RuntimeError(f"Claude API timed out after {timeout}s")
    except req.exceptions.ConnectionError as e:
        raise RuntimeError(f"Cannot reach Anthropic API: {e}")

    elapsed_ms = int((time.time() - t0) * 1000)
    if not r.ok:
        raise RuntimeError(f"Claude API HTTP {r.status_code}: {r.text[:400]}")

    data = r.json()
    text = " ".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ).strip()
    usage = data.get("usage", {})
    logger.log("claude_api_call", model=api_model,
               input_tokens=usage.get("input_tokens", 0),
               output_tokens=usage.get("output_tokens", 0),
               elapsed_ms=elapsed_ms)
    return {
        "content": text,
        "tool_calls": [],
        "total_duration_ms": elapsed_ms,
        "eval_count": usage.get("output_tokens", 0),
    }


def call_llm(model, messages, tools=None, max_tokens=2048, temperature=0.7,
             timeout=180):
    """Invoke a model via Ollama's native /api/chat endpoint.

    Handles thinking suppression, tool calls, and response normalization.
    Raises RuntimeError on HTTP failure — caller should catch.
    """
    info = MODEL_INFO.get(model, {})
    # Route to Claude API if this model is configured with provider: anthropic
    if info.get("provider") == "anthropic":
        return _call_claude(model, info, messages, max_tokens, temperature, timeout)

    disable_thinking = info.get("disable_thinking", False)
    num_ctx = info.get("num_ctx", 8192)  # Safe default: 8K context

    native_messages = _translate_history_to_native(messages)

    payload = {
        "model": model,
        "messages": native_messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
            "num_ctx": num_ctx,
        },
    }
    # `think` is a TOP-LEVEL field, not inside options. Inside options it's
    # silently ignored — this is a well-known Ollama quirk. Only set it for
    # models that actually have a thinking mode; setting think on a non-
    # reasoning model (gemma, qwen2.5) is a no-op but adds noise.
    if disable_thinking:
        payload["think"] = False

    if tools:
        payload["tools"] = tools

    try:
        r = req.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=timeout)
    except req.exceptions.Timeout:
        raise RuntimeError(f"Ollama request timed out after {timeout}s")
    except req.exceptions.ConnectionError as e:
        raise RuntimeError(f"Cannot reach Ollama at {OLLAMA_HOST}: {e}")

    if not r.ok:
        raise RuntimeError(f"Ollama HTTP {r.status_code}: {r.text[:300]}")

    data = r.json()
    msg = data.get("message", {})

    # Extract and normalize tool calls. Ollama native returns arguments as
    # a dict (or orderedmap); we keep them as a dict in the normalized form
    # but synthesize a stable-ish id for OpenAI-compat in DB storage.
    raw_tcs = msg.get("tool_calls") or []
    normalized_tcs = []
    for i, tc in enumerate(raw_tcs):
        fn = tc.get("function", {})
        name = fn.get("name", "")
        args = fn.get("arguments", {})
        # Defensive: some model outputs still stringify args
        if isinstance(args, str):
            try:
                args = json.loads(args) if args else {}
            except Exception:
                args = {"_raw": args}
        # Synthesize an id: Ollama doesn't assign one, but we need it for
        # OpenAI-format DB storage and for dedup sig stability across iterations.
        synth_id = f"call_{hashlib.md5((name + json.dumps(args, sort_keys=True, default=str)).encode()).hexdigest()[:12]}"
        normalized_tcs.append({
            "id": synth_id,
            "name": name,
            "arguments": args,
        })

    # Strip thinking artifacts. Three sources of leakage:
    #   1. Ollama native `thinking` field — separate from content, discard
    #   2. <think>...</think> tags leaked into content — strip with regex
    #   3. Raw thought paragraphs before answer (qwen3 partial suppression)
    content = msg.get("content") or ""
    # Remove <think> blocks (tagged form)
    if "<think>" in content:
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    # Strip Ollama native thinking field if it leaked into content
    thinking_field = msg.get("thinking") or ""
    if thinking_field and content.startswith(thinking_field[:80].strip()):
        content = content[len(thinking_field):].strip()

    return {
        "content": content,
        "tool_calls": normalized_tcs,
        "total_duration_ms": int(data.get("total_duration", 0) / 1_000_000),
        "eval_count": data.get("eval_count", 0),
    }


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
def classify(text):
    """Route a query to a category. Returns one of CATEGORIES keys.

    Heuristic-only — no LLM call. Routing priority:
      1. Voice/HA intent        → llama3.2:3b with HA tools only
      2. Code or simple infra   → qwen3-coder:30b (tools, fast, no thinking)
      3. Complex multi-host     → qwen3.6:35b-a3b (full tools, reasoning)
      4. Reasoning/synthesis    → qwen3.6:35b-a3b
      5. Trivial chat           → llama3.2:3b
      6. Default                → qwen3.6:35b-a3b (safe fallback for ambiguous queries)

    qwen3-coder:30b handles all tool-calling work (SSH, code, infra, HA).
    qwen3.6:35b-a3b handles conversational reasoning, planning, synthesis.
    Both are MoE models — only one loads into VRAM at a time.
    """
    if not text:
        return "general"

    stripped = text.strip()
    lower = stripped.lower()
    word_count = len(stripped.split())

    # ─── 1. Voice / Home Assistant intent ────────────────────────────
    ha_verbs = (
        "turn on", "turn off", "toggle", "dim", "brighten", "set ",
        "lock ", "unlock ", "play ", "pause ", "stop ", "resume ",
        "activate ", "deactivate ", "run scene", "trigger ",
    )
    ha_nouns = (
        "light", "lights", "lamp", "lamps", "fan", "thermostat",
        "temperature", "temp", "ac", "heat", "heater", "tv", "music",
        "song", "playlist", "scene", "lock", "door", "blinds", "curtain",
        "shades", "switch", "outlet", "plug", "vacuum", "roomba",
    )
    has_ha_verb = any(v in lower for v in ha_verbs)
    has_ha_noun = any(n in lower.split() or n + "s" in lower.split() for n in ha_nouns)
    if word_count <= _ROUTING["voice_max_words"] and has_ha_verb and has_ha_noun:
        return "voice"

    # ─── 2. Code or simple infra (qwen3-coder:30b handles both) ──────
    code_keywords = (
        "python", "javascript", "typescript", "bash", "shell script",
        "function", "class", "method", "variable", "loop", "if statement",
        "exception", "stack trace", "traceback", "syntax error",
        "refactor", "debug", "optimize", "rewrite",
        "write a script", "write a function", "write code", "code for",
        "regex", "regular expression", "sql query", "yaml", "json schema",
        "dockerfile", "compose file", "systemd unit",
    )
    has_code = any(k in lower for k in code_keywords)

    # Count SSH host mentions — strong signal of how much coordination is needed
    host_mentions = sum(1 for alias in SSH_HOSTS if alias.lower() in lower)

    # Complex-query signals that push back to Gemma regardless of code status
    complex_signals = (
        " compare ", " analyze ", " analyse ", " synthesize ",
        " recommend ", " pros and cons ", " break down ",
        " walk me through ", " step by step ",
    )
    has_complex = any(s in f" {lower} " for s in complex_signals)

    # Route code queries to the coder — BUT escalate to worker if:
    #   - complex_host_threshold+ hosts involved
    #   - complex synthesis verbs present
    #   - query exceeds complex_word_threshold words
    # Thresholds are configurable via models.yaml → routing section.
    if has_code:
        if (host_mentions >= _ROUTING["complex_host_threshold"]
                or has_complex
                or word_count > _ROUTING["complex_word_threshold"]):
            return "reasoning"  # → worker
        return "code"  # → coder

    # ─── 3. Simple infra without code keywords ───────────────────────
    # Short, single-host queries like "check uptime on ai-stack-420" can
    # run on qwen3-coder:30b — it has tool calling and is much faster.
    simple_infra_verbs = (
        "check ", "show ", "get ", "list ", "what is the", "what's the",
        "is ", "are ", "how much ", "how many ",
    )
    has_simple_verb = any(lower.startswith(v) or f" {v}" in lower[:20]
                           for v in simple_infra_verbs)
    if (word_count <= 15 and host_mentions <= 1 and has_simple_verb
            and not has_complex):
        return "infra_simple"  # → qwen3-coder:30b via CATEGORIES mapping

    # ─── 4. Trivial chat ────────────────────────────────────────────
    trivial_exact = (
        "hi", "hey", "hello", "thanks", "thank you", "thx", "ok", "okay",
        "yes", "no", "sup", "yo", "morning", "evening", "good night",
    )
    if lower in trivial_exact or lower.rstrip("?.!") in trivial_exact:
        return "trivial"

    # ─── 5. Everything else → qwen3.6:35b-a3b ──────────────────────
    # Reasoning, multi-host coordination, vault synthesis, vision — all
    # go to the primary worker. We still compute sub-categories for logging.
    scores = {
        "home": sum(1 for k in ["schedule", "automation", "routine",
                                 "morning routine", "bedtime"]
                    if k in lower),
        "infra": host_mentions + sum(1 for k in ["server","vm","docker","proxmox",
                                                  "nginx","wireguard","tailscale",
                                                  "beanlab","kidneybean","networkbean",
                                                  "storagebean","truenas","nextcloud",
                                                  "jellyfin","pi-hole","bookstack"]
                                     if k in lower),
        "reasoning": sum(1 for k in ["explain","why","analyze","compare","think","reason",
                                      "step by step","pros and cons","break down","plan",
                                      "recommend","suggest","should i","which is better",
                                      "walk me through","diagnose"]
                         if k in lower),
    }
    mx = max(scores.values())
    if mx >= 2:
        return max(scores, key=scores.get)
    if mx == 1:
        matching = [k for k, v in scores.items() if v == 1]
        if len(matching) == 1:
            return matching[0]
    return "general"


# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)


# --- Authentication ---
JARVIS_USERNAME = os.environ.get("ODIN_USER", "odin")
JARVIS_PASSWORD = os.environ.get("ODIN_PASS", "")
ALLOW_NOAUTH = os.environ.get("ODIN_ALLOW_NOAUTH", "").lower() in ("1", "true", "yes")

if not JARVIS_PASSWORD and not ALLOW_NOAUTH:
    print("=" * 60)
    print("  ⚠️  SECURITY: ODIN_PASS is not set.")
    print("=" * 60)
    print("  Odin refuses to start without authentication by default.")
    print("  This protects SSH access, shell execution, and vault writes.")
    print()
    print("  To fix:")
    print("    export ODIN_PASS='your-strong-password'")
    print("    export ODIN_USER='your-username'  # optional, defaults to 'odin'")
    print()
    print("  To override for local dev only (NOT RECOMMENDED):")
    print("    export ODIN_ALLOW_NOAUTH=1")
    print("=" * 60)
    sys.exit(1)

def check_auth(username, password):
    return username == JARVIS_USERNAME and password == JARVIS_PASSWORD

def requires_auth(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not JARVIS_PASSWORD:
            return f(*args, **kwargs)  # No password set, skip auth
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return Response('Unauthorized', 401,
                {'WWW-Authenticate': 'Basic realm="Odin"'})
        return f(*args, **kwargs)
    return decorated

@app.before_request
def auth_all():
    if not JARVIS_PASSWORD:
        return

    # Allow static + PWA assets
    if request.endpoint in ('static',):
        return

    if request.path in ('/manifest.json', '/sw.js', '/icon.svg'):
        return

    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return Response('Unauthorized', 401,
            {'WWW-Authenticate': 'Basic realm="Odin"'})

# Globals
vault = None
ha = None  # HomeAssistant client, initialized in main() if HASS_URL+HASS_TOKEN are set
shell = ShellExecutor()
web = WebFetcher()


def _load_prompt_file(scope):
    """Load a system prompt from Odins_Self/prompts/v3/{scope}.md.

    Falls back to a minimal builtin if the file doesn't exist so Odin
    never crashes from a missing prompt file.
    """
    base = os.path.join(os.path.dirname(__file__), "Odins_Self", "prompts", "v3")
    path = os.path.join(base, f"{scope}.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.log("prompt_fallback", scope=scope,
                   message=f"Prompt file not found: {path}, using builtin")
        return None


# Cache loaded prompts to avoid re-reading on every request.
# Cleared on /api/models/reload.
_prompt_cache = {}


def get_system_prompt(scope="worker"):
    """Return the system prompt for a given agent scope.

    Loads from Odins_Self/prompts/v3/{scope}.md with {current_time} and
    {hosts} template variables. Edit the .md files directly; hit
    POST /api/models/reload to pick up changes without restarting.
    """
    now = datetime.datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    hosts = "\n".join(
        f"  - {a}: {i['description']} ({_resolve_host_ip(i)})"
        for a, i in SSH_HOSTS.items()
    )

    # Check cache first
    if scope not in _prompt_cache:
        _prompt_cache[scope] = _load_prompt_file(scope)

    template = _prompt_cache.get(scope)
    if template:
        return template.replace("{current_time}", now).replace("{hosts}", hosts)

    # Minimal fallback if no file exists
    return f"""You are Odin, an AI assistant for the BeanLab homelab.
Current time: {now}

AVAILABLE SSH HOSTS:
{hosts}

Answer concisely. Use tools when needed. FORMATTING: Markdown only, no HTML tags.
"""


def process_message(user_input, chat_id, model_override=None, attachments=None):
    global vault

    attachments = attachments or []

    # Separate text-ish attachments from images
    text_attachments = [a for a in attachments if a.get("type") != "image"]
    image_attachments = [a for a in attachments if a.get("type") == "image"]
    has_images = len(image_attachments) > 0

    # Build text portion of user input (text attachments + user's prompt)
    text_parts = []
    for att in text_attachments:
        name = att.get("name", "file")
        content = att.get("content", "")
        text_parts.append(f"[Attached file: {name}]\n{content}")
    text_portion = ("\n\n".join(text_parts) + ("\n\n" if text_parts else "") + user_input).strip()

    # Build DB-safe display text (omit base64 blobs)
    db_display_parts = []
    for att in text_attachments:
        db_display_parts.append(f"[Attached file: {att.get('name','file')}]")
    for att in image_attachments:
        db_display_parts.append(f"[Image: {att.get('name','image')}]")
    db_display = ((" ".join(db_display_parts) + "\n\n") if db_display_parts else "") + user_input

    # Decide model with vision routing
    vision_override_note = None
    if has_images:
        # Need a vision-capable model
        if model_override and model_override in MODEL_INFO and MODEL_INFO[model_override].get("supports_vision"):
            model = model_override
            category = "vision"
        else:
            # Auto-pick vision model (override user selection if needed)
            model = DEFAULT_VISION_MODEL
            category = "vision"
            if model_override and model_override != "auto":
                orig = MODEL_INFO.get(model_override, {}).get("label", model_override)
                vision_override_note = f"_(switched from {orig} to {MODEL_INFO[DEFAULT_VISION_MODEL]['label']} for image analysis)_"
    elif model_override and model_override != "auto" and model_override in MODEL_INFO:
        model = model_override
        category = "manual"
    else:
        category = classify(user_input)
        model = MODELS[CATEGORIES.get(category, "general")]

    # Determine tool scope from the chosen role. The scope controls which
    # tools the model sees — voice gets only HA, coder gets none (it has
    # no tool-calling capability), everything else gets the full suite.
    role = CATEGORIES.get(category, "general")
    if role == "voice":
        tool_scope = "voice"
    elif role == "coder":
        tool_scope = "coder"
    else:
        tool_scope = "worker"

    # Manual override: if the user picked a specific model, derive scope
    # from that model's own capabilities rather than the category.
    if category == "manual":
        if not MODEL_INFO.get(model, {}).get("supports_tools", True):
            tool_scope = "coder"  # no tools

    # Build the message content for the model.
    # Ollama native /api/chat format: content is a plain string; images go
    # in a sibling `images` list as raw base64 (no data URL prefix).
    user_msg_for_model = {"role": "user", "content": text_portion}
    if has_images:
        image_b64s = []
        for img in image_attachments:
            b64 = img.get("b64", "")
            if b64:
                image_b64s.append(b64)
        if image_b64s:
            user_msg_for_model["images"] = image_b64s

    # Load history from DB (stored as plain-text, no images)
    history = db.get_messages(chat_id)
    # Append new user message (for model: text + optional images; for DB: text-only)
    history.append(user_msg_for_model)
    db.add_message(chat_id, "user", db_display)

    # Auto-title the chat from the first user message
    chat_list = [c for c in db.list_chats() if c["id"] == chat_id]
    if chat_list and chat_list[0]["title"] == "New chat":
        title = (user_input or db_display).strip()[:60].replace("\n", " ")
        if title:
            db.rename_chat(chat_id, title)

    # Token-aware context trimming. Estimate ~4 chars per token.
    # Leave 30% of num_ctx for the response.
    model_ctx = MODEL_INFO.get(model, {}).get("num_ctx", 8192)
    max_input_tokens = int(model_ctx * 0.7)
    max_input_chars = max_input_tokens * 4

    total_chars = sum(len(m.get("content", "")) for m in history)
    if total_chars > max_input_chars:
        # Keep system prompt slot + last user message, trim from the front
        trimmed = []
        budget = max_input_chars
        # Always keep the last 6 messages (current exchange)
        keep_tail = history[-6:]
        budget -= sum(len(m.get("content", "")) for m in keep_tail)
        # Fill remaining budget from newest-to-oldest
        for m in reversed(history[:-6]):
            msg_len = len(m.get("content", ""))
            if budget - msg_len < 0:
                break
            trimmed.insert(0, m)
            budget -= msg_len
        history = trimmed + keep_tail
        logger.log("context_trimmed", chat_id=chat_id,
                   original_chars=total_chars, trimmed_to=sum(len(m.get("content","")) for m in history))

    tools = get_tools(vault, scope=tool_scope)
    supports_tools = MODEL_INFO.get(model, {}).get("supports_tools", True)
    tool_calls_log = []
    seen_calls = set()
    MAX_ITERS = 5

    logger.log("user_message", chat_id=chat_id, text=user_input[:200],
               category=category, model=model, scope=tool_scope,
               text_attachments=len(text_attachments), image_attachments=len(image_attachments))

    for iteration in range(MAX_ITERS):
        is_final_iter = (iteration == MAX_ITERS - 1)

        # Build the message list for this iteration. On the final iteration
        # we append a "stop calling tools" nudge to force a text answer.
        iter_messages = [{"role": "system", "content": get_system_prompt(scope=tool_scope)}, *history]
        if is_final_iter:
            iter_messages.append({
                "role": "system",
                "content": "You've made several tool calls. Do NOT call any more tools. "
                           "Answer the user's original question directly based on what you've "
                           "already gathered, even if incomplete. Be concise."
            })

        # Only pass tools if the model supports them AND we're not on the
        # forced-final iteration. Omitting tools on the final pass helps
        # models that otherwise get stuck in a tool-call loop.
        iter_tools = tools if (tools and supports_tools and not is_final_iter) else None

        try:
            model_meta = MODEL_INFO.get(model, {})
            t_llm = time.time()
            resp = call_llm(
                model=model,
                messages=iter_messages,
                tools=iter_tools,
                max_tokens=model_meta.get("num_ctx", 8192) // 4,  # 25% of ctx for output
                temperature=model_meta.get("temperature", 0.7),
                timeout=model_meta.get("timeout", 180),
            )
            llm_elapsed_ms = int((time.time() - t_llm) * 1000)
            logger.log("llm_call", chat_id=chat_id, model=model,
                       iteration=iteration, elapsed_ms=llm_elapsed_ms,
                       eval_count=resp.get("eval_count", 0),
                       had_tool_calls=bool(resp.get("tool_calls")))
        except Exception as e:
            logger.log("model_error", chat_id=chat_id, error=str(e), iteration=iteration)
            err = f"Model error: {e}"
            db.add_message(chat_id, "assistant", err)
            return {"response": err, "category": category, "model": model, "tools": []}

        content = resp["content"]
        tool_calls = resp["tool_calls"]  # list of {id, name, arguments(dict)}

        if tool_calls and not is_final_iter:
            # Persist the assistant's tool-call turn to history + DB.
            # We store in OpenAI format (string arguments, type=function) so
            # the DB schema and display logic don't need to change.
            tc_list = []
            for tc in tool_calls:
                tc_list.append({
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["arguments"], default=str),
                    }
                })
            assistant_turn = {
                "role": "assistant",
                "content": content or "",
                "tool_calls": tc_list,
            }
            history.append(assistant_turn)
            db.add_message(chat_id, "assistant", content or "", tool_calls=tc_list)

            # ─── Parallel tool dispatch ───
            # Partition incoming tool calls into (a) duplicates handled inline
            # and (b) fresh calls that get dispatched to the thread pool. We
            # preserve the original order when appending results to `history`
            # because many chat formats require tool messages to appear in
            # the same order as the assistant's tool_calls.
            fresh_jobs = []          # list of (tc, fn, fa)
            duplicate_results = {}   # tc["id"] -> (fn, fa, result_json_str)

            for tc in tool_calls:
                fn = tc["name"]
                fa = tc["arguments"]
                if not isinstance(fa, dict):
                    fa = {"_raw": str(fa)}

                sig = hashlib.md5((fn + json.dumps(fa, sort_keys=True, default=str)).encode()).hexdigest()
                if sig in seen_calls:
                    logger.log("tool_call_skipped", chat_id=chat_id, tool=fn, args=fa,
                               reason="duplicate")
                    dup_result = json.dumps({
                        "error": "DUPLICATE_CALL",
                        "message": f"You already called {fn} with these exact arguments. "
                                   "Do NOT repeat this call. Answer the user's question with the "
                                   "information you already have, or try a different approach."
                    })
                    duplicate_results[tc["id"]] = (fn, fa, dup_result)
                    tool_calls_log.append({"tool": fn, "args": fa, "skipped": True})
                else:
                    seen_calls.add(sig)
                    fresh_jobs.append((tc, fn, fa))
                    tool_calls_log.append({"tool": fn, "args": fa})
                    logger.log("tool_call", chat_id=chat_id, tool=fn, args=fa,
                               iteration=iteration)

            # Run fresh jobs concurrently. Cap workers at 4: matches typical
            # per-turn tool-call volume and avoids hammering SSH hosts.
            fresh_results = {}  # tc["id"] -> result_json_str
            if fresh_jobs:
                max_workers = min(4, len(fresh_jobs))
                t_pool_start = time.time()
                with ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="odin-tool") as pool:
                    future_to_tc = {
                        pool.submit(handle_tool, fn, fa, vault, shell, web): (tc, fn, fa)
                        for (tc, fn, fa) in fresh_jobs
                    }
                    for fut in as_completed(future_to_tc):
                        tc, fn, fa = future_to_tc[fut]
                        try:
                            result = fut.result()
                        except Exception as tool_exc:
                            result = json.dumps({
                                "error": "TOOL_EXCEPTION",
                                "tool": fn,
                                "message": str(tool_exc)[:400],
                            })
                            logger.log("tool_exception", chat_id=chat_id, tool=fn,
                                       error=str(tool_exc)[:400])
                        fresh_results[tc["id"]] = result

                        # Log result preview
                        try:
                            result_preview = json.loads(result)
                            for k, v in list(result_preview.items()):
                                if isinstance(v, str) and len(v) > 300:
                                    result_preview[k] = v[:300] + "...[truncated]"
                            logger.log("tool_result", chat_id=chat_id, tool=fn,
                                       result=result_preview)
                        except Exception:
                            logger.log("tool_result", chat_id=chat_id, tool=fn,
                                       result=result[:300])

                logger.log("tool_batch_complete", chat_id=chat_id,
                           count=len(fresh_jobs),
                           elapsed_ms=int((time.time() - t_pool_start) * 1000),
                           workers=max_workers)

            # Append tool results to history in original order, with tool_name
            # populated so _translate_history_to_native doesn't have to guess.
            for tc in tool_calls:
                tc_id = tc["id"]
                tool_name = tc["name"]
                if tc_id in duplicate_results:
                    _, _, result = duplicate_results[tc_id]
                elif tc_id in fresh_results:
                    result = fresh_results[tc_id]
                else:
                    result = json.dumps({"error": "MISSING_RESULT", "tool_call_id": tc_id})
                history.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "tool_name": tool_name,
                    "content": result,
                })
                db.add_message(chat_id, "tool", result, tool_call_id=tc_id)
            continue

        # No tool calls — check for empty response and attempt recovery.
        if not content.strip() and not is_final_iter and iteration > 0:
            # The model returned nothing. This usually means the context
            # overflowed or the model choked on accumulated tool results.
            # Inject a synthesis nudge and retry once.
            logger.log("empty_response_recovery", chat_id=chat_id,
                       iteration=iteration, history_len=len(history))
            history.append({
                "role": "system",
                "content": "Your previous response was empty. Summarize the tool results you've "
                           "gathered so far and answer the user's original question. Be concise."
            })
            continue

        # This is the final answer.
        text = content
        if vision_override_note:
            text = f"{vision_override_note}\n\n{text}" if text else vision_override_note
        history.append({"role": "assistant", "content": text})
        db.add_message(chat_id, "assistant", text)
        logger.log("assistant_response", chat_id=chat_id, length=len(text),
                   iterations=iteration + 1)
        return {"response": text, "category": category, "model": model, "tools": tool_calls_log}

    logger.log("loop_exhausted", chat_id=chat_id, iterations=MAX_ITERS)
    fallback = "I got stuck. Could you rephrase?"
    db.add_message(chat_id, "assistant", fallback)
    return {"response": fallback, "category": category, "model": model, "tools": tool_calls_log}


@app.route("/")
def index():
    html_path = os.path.join(os.path.dirname(__file__), "static", "odin.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "Odin UI not found. Expected: static/odin.html", 500

@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "Odin",
        "short_name": "Odin",
        "description": "BeanLab AI Assistant",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#13161a",
        "theme_color": "#c9a961",
        "icons": [
            {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}
        ]
    })

@app.route("/icon.svg")
def icon():
    # Minimal runic O icon on dark charcoal
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="96" fill="#13161a"/>
<circle cx="256" cy="256" r="150" fill="none" stroke="#c9a961" stroke-width="16"/>
<circle cx="256" cy="256" r="18" fill="#c9a961"/>
</svg>'''
    return Response(svg, mimetype="image/svg+xml")

@app.route("/favicon.ico")
def favicon():
    # Browsers auto-request /favicon.ico; serve the SVG icon
    return icon()

@app.route("/sw.js")
def service_worker():
    # Minimal service worker — enables "install app" prompt, caches shell
    sw = '''
const CACHE = 'odin-v1';
const SHELL = ['/', '/icon.svg', '/manifest.json'];
self.addEventListener('install', e => {
    e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
    self.skipWaiting();
});
self.addEventListener('activate', e => { self.clients.claim(); });
self.addEventListener('fetch', e => {
    // Network-first for API; cache-first for shell
    if (e.request.url.includes('/api/')) return;
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
'''
    return Response(sw, mimetype="application/javascript")

@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json or {}
    msg = (data.get("message") or "").strip()
    chat_id = data.get("chat_id")
    model_override = data.get("model")
    attachments = data.get("attachments") or []
    if not msg and not attachments:
        return jsonify({"error": "Empty message"}), 400
    if not chat_id:
        return jsonify({"error": "chat_id required"}), 400
    # Auto-create chat if it doesn't exist
    existing = [c for c in db.list_chats() if c["id"] == chat_id]
    if not existing:
        db.create_chat(chat_id, project_id=data.get("project_id") or "general")
    start = time.time()
    result = process_message(msg, chat_id, model_override=model_override, attachments=attachments)
    result["latency_ms"] = int((time.time() - start) * 1000)
    return jsonify(result)

@app.route("/api/chats", methods=["GET"])
def list_chats():
    return jsonify({"chats": db.list_chats()})

@app.route("/api/chats", methods=["POST"])
def create_chat():
    data = request.json or {}
    chat_id = data.get("chat_id") or hashlib.md5(str(time.time()).encode()).hexdigest()[:16]
    title = data.get("title") or "New chat"
    project_id = data.get("project_id") or "general"
    db.create_chat(chat_id, title=title, project_id=project_id)
    return jsonify({"chat_id": chat_id, "title": title, "project_id": project_id})

@app.route("/api/chats/<chat_id>", methods=["GET"])
def get_chat(chat_id):
    messages = db.get_messages_display(chat_id)
    return jsonify({"chat_id": chat_id, "messages": messages})

@app.route("/api/chats/<chat_id>", methods=["DELETE"])
def delete_chat(chat_id):
    db.delete_chat(chat_id)
    return jsonify({"status": "deleted"})

@app.route("/api/chats/<chat_id>/rename", methods=["POST"])
def rename_chat(chat_id):
    data = request.json or {}
    title = (data.get("title") or "").strip() or "Untitled"
    db.rename_chat(chat_id, title)
    return jsonify({"status": "renamed", "title": title})

@app.route("/api/chats/<chat_id>/move", methods=["POST"])
def move_chat(chat_id):
    data = request.json or {}
    project_id = data.get("project_id") or "general"
    db.move_chat(chat_id, project_id)
    return jsonify({"status": "moved", "project_id": project_id})

@app.route("/api/projects", methods=["GET"])
def list_projects():
    return jsonify({"projects": db.list_projects()})

@app.route("/api/projects", methods=["POST"])
def create_project():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    icon = data.get("icon") or "💬"
    pid = "proj_" + hashlib.md5((name + str(time.time())).encode()).hexdigest()[:10]
    db.create_project(pid, name[:40], icon)
    return jsonify({"id": pid, "name": name[:40], "icon": icon})

@app.route("/api/projects/<project_id>", methods=["DELETE"])
def delete_project(project_id):
    ok = db.delete_project(project_id)
    if not ok:
        return jsonify({"error": "cannot delete default project"}), 400
    return jsonify({"status": "deleted"})

@app.route("/api/models", methods=["GET"])
def list_models():
    # Query Ollama for actually available models, cross-reference with MODEL_INFO
    try:
        r = req.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        available = {m["name"] for m in r.json().get("models", [])}
    except Exception:
        available = set()
    items = []
    for key, info in MODEL_INFO.items():
        if info.get("provider") == "anthropic":
            ok = bool(ANTHROPIC_API_KEY)
        else:
            ok = key == "auto" or key in available
        items.append({"id": key, "available": ok, **info})
    return jsonify({"models": items})


@app.route("/api/models/reload", methods=["POST"])
def reload_models():
    """Reload models.yaml without restarting Odin.

    Updates MODELS, MODEL_INFO, CATEGORIES, DEFAULT_VISION_MODEL, and the
    routing thresholds in place. Prewarming doesn't re-run — restart for that.

    Returns the new role assignments so a client can verify.
    """
    global MODELS, MODEL_INFO, CATEGORIES, DEFAULT_VISION_MODEL, _ROUTING
    try:
        registry.reload()
        MODELS = registry.roles
        MODEL_INFO = registry.models
        CATEGORIES = registry.categories
        DEFAULT_VISION_MODEL = registry.default_vision_model
        _ROUTING = registry.routing
        # Clear cached system prompts so edits to v3/*.md take effect
        _prompt_cache.clear()
        logger.log("models_reloaded",
                   roles=MODELS,
                   model_count=len(MODEL_INFO) - 1)  # -1 for the "auto" pseudo-entry
        return jsonify({
            "ok": True,
            "roles": MODELS,
            "default_vision_model": DEFAULT_VISION_MODEL,
            "routing": _ROUTING,
            "model_count": len(MODEL_INFO) - 1,
        })
    except Exception as e:
        logger.log("models_reload_failed", error=str(e))
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/upload", methods=["POST"])
def upload():
    """Accept a file and return extracted text content (for use as attachment)."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file"}), 400
    name = f.filename or "unnamed"
    raw = f.read()
    # Size limit 5MB
    if len(raw) > 5 * 1024 * 1024:
        return jsonify({"error": "File too large (5MB max)"}), 400
    # Try to decode as text
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in ("txt", "md", "py", "js", "ts", "json", "yaml", "yml", "toml", "ini",
               "conf", "cfg", "sh", "bash", "ps1", "log", "csv", "html", "css", "xml"):
        try:
            content = raw.decode("utf-8", errors="replace")[:50000]
            return jsonify({"name": name, "content": content, "type": "text"})
        except Exception as e:
            return jsonify({"error": str(e)}), 400
    elif ext == "pdf":
        try:
            # Try pypdf if available
            import io
            try:
                from pypdf import PdfReader
            except ImportError:
                from PyPDF2 import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            text = "\n\n".join(p.extract_text() or "" for p in reader.pages)
            return jsonify({"name": name, "content": text[:50000], "type": "pdf"})
        except Exception as e:
            return jsonify({"error": f"PDF parse failed: {e}. Install pypdf."}), 400
    elif ext in ("png", "jpg", "jpeg", "gif", "webp"):
        mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "gif": "image/gif", "webp": "image/webp"}
        b64 = base64.b64encode(raw).decode()
        return jsonify({
            "name": name,
            "content": f"[Image: {name}]",  # text fallback for non-vision models
            "type": "image",
            "b64": b64,
            "mime": mime_map.get(ext, "image/jpeg"),
            "size": len(raw),
        })
    else:
        # Try as text with loose decoding
        try:
            content = raw.decode("utf-8", errors="replace")[:50000]
            return jsonify({"name": name, "content": content, "type": "text"})
        except Exception as e:
            return jsonify({"error": f"Unsupported file type: {ext}"}), 400

@app.route("/api/status")
def status():
    vault_notes = None
    if vault and vault.connected:
        try:
            vault_notes = sum(1 for _ in vault.root.rglob("*.md")) if hasattr(vault, "root") else None
        except Exception:
            pass
    return jsonify({
        "ollama": OLLAMA_HOST,
        "vault": vault.connected if vault else False,
        "vault_notes": vault_notes,
        "ha_connected": ha.connected if ha else False,
        "ssh_hosts": list(SSH_HOSTS.keys()),
        "models": list(MODELS.values()),
        "active_request": False,
        "claude_api": bool(ANTHROPIC_API_KEY),
        "total_requests": 0,
    })

@app.route("/api/hosts")
def hosts():
    return jsonify(SSH_HOSTS)

@app.route("/api/purge-now", methods=["POST"])
def purge_now():
    """Manual purge trigger (same logic as the scheduled one)."""
    n = db.purge_old(days=PURGE_AFTER_DAYS)
    return jsonify({"purged": n, "days": PURGE_AFTER_DAYS})

@app.route("/api/logs")
def logs():
    n = int(request.args.get("n", 100))
    chat_id = request.args.get("chat_id")
    events = logger.recent(n=n)
    if chat_id:
        events = [e for e in events if e.get("chat_id") == chat_id]
    return jsonify({"events": events})





# ---------------------------------------------------------------------------
# HA + Terminal routes (Phase 2)
# ---------------------------------------------------------------------------

@app.route("/api/ha/states")
def ha_states():
    """Proxy HA /api/states so the frontend can render the device grid."""
    if not ha or not ha.connected:
        return jsonify({"error": "Home Assistant not configured"}), 503
    try:
        r = req.get(
            f"{ha.url}/api/states",
            headers=ha.headers,
            timeout=10,
        )
        r.raise_for_status()
        states = r.json()
        # Filter to domains the UI cares about; strip noisy attributes
        DOMAINS = {"light", "switch", "climate", "media_player",
                   "input_boolean", "automation", "script", "scene", "cover"}
        filtered = [
            {
                "entity_id":  s["entity_id"],
                "state":      s["state"],
                "attributes": {
                    k: v for k, v in s.get("attributes", {}).items()
                    if k in {"friendly_name", "brightness", "color_temp",
                              "rgb_color", "temperature", "current_temperature",
                              "hvac_mode", "volume_level", "media_title",
                              "icon", "device_class"}
                },
                "last_changed": s.get("last_changed"),
            }
            for s in states
            if s["entity_id"].split(".")[0] in DOMAINS
        ]
        filtered.sort(key=lambda s: s["entity_id"])
        return jsonify({"states": filtered})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/ha/call", methods=["POST"])
def ha_call():
    """Proxy HA /api/services/<domain>/<service>.
    Body: {domain, service, entity_id?, brightness?, ...extra service data}
    """
    if not ha or not ha.connected:
        return jsonify({"error": "Home Assistant not configured"}), 503
    data = request.get_json(silent=True) or {}
    domain    = str(data.pop("domain", "")).strip()
    service   = str(data.pop("service", "")).strip()
    entity_id = data.pop("entity_id", None)
    if not domain or not service:
        return jsonify({"error": "domain and service are required"}), 400
    result = ha.call_service(domain, service, entity_id=entity_id, **data)
    if "error" in result:
        return jsonify(result), 502
    return jsonify(result)


@app.route("/api/terminal/connect", methods=["POST"])
def terminal_connect():
    """Validate that SSH connectivity to a host works.
    Body: {host: "<alias from hosts.json>"}
    Returns: {ok, host_ip, hostname}
    """
    data = request.get_json(silent=True) or {}
    alias = str(data.get("host", "")).strip()
    if not alias:
        return jsonify({"error": "host alias required"}), 400
    if alias not in SSH_HOSTS:
        return jsonify({"error": f"Unknown host: {alias}. Check hosts.json."}), 404
    # Run a quick hostname command to verify connectivity
    result = shell.run_ssh(alias, "hostname", timeout=10)
    if "error" in result:
        return jsonify({"error": result["error"]}), 502
    hostname = (result.get("stdout") or "").strip()
    info     = SSH_HOSTS[alias]
    return jsonify({
        "ok":       True,
        "alias":    alias,
        "host_ip":  info.get("host", ""),
        "hostname": hostname,
        "user":     info.get("user", ""),
    })


@app.route("/api/terminal/exec", methods=["POST"])
def terminal_exec():
    """Execute a shell command on a remote host via SSH.
    Body: {host: "<alias>", command: "<shell command>"}
    Returns: {stdout, stderr, returncode}

    Guardrail: same destructive-command block list as the agent tool.
    """
    data    = request.get_json(silent=True) or {}
    alias   = str(data.get("host", "")).strip()
    command = str(data.get("command", "")).strip()

    if not alias or not command:
        return jsonify({"error": "host and command are required"}), 400
    if alias not in SSH_HOSTS:
        return jsonify({"error": f"Unknown host: {alias}"}), 404

    # Reuse the same destructive-command guard the agent uses
    BLOCKED_PATTERNS = [
        "rm -rf /", "rm -rf /*", ":(){ :|:& };", "> /dev/sda",
        "dd if=/dev/zero", "mkfs", "fdisk", "parted",
        "shutdown", "reboot", "halt", "poweroff",
        "passwd", "userdel", "usermod",
        "chmod -R 777 /", "chown -R",
        "iptables -F", "ufw disable",
        "systemctl restart networking",
        "systemctl stop networking", 
        "ip neigh flush",
        "ip neigh del",
        "ip link set",
        "ip route del",
    ]
    lower_cmd = command.lower()
    for blocked in BLOCKED_PATTERNS:
        if blocked.lower() in lower_cmd:
            return jsonify({
                "error": f"Command blocked by Odin safety policy: contains '{blocked}'",
                "stdout": "",
                "stderr": "",
                "returncode": 1,
            }), 403

    # ── Interactive command substitutions ──────────────────────────────────────
    # SSH exec_command does not allocate a PTY so interactive full-screen
    # programs (top, htop, vim, nano) block waiting for terminal input and
    # return nothing. Substitute them with non-interactive equivalents.
    INTERACTIVE_SUBS = {
        "top":          "top -bn1 | head -30",
        "htop":         "top -bn1 | head -30",
        "vim":          None,   # no safe substitute — reject
        "vi":           None,
        "nano":         None,
        "less":         None,
        "more":         None,
        "man":          None,
    }
    base_cmd = command.strip().split()[0].lower() if command.strip() else ""
    if base_cmd in INTERACTIVE_SUBS:
        sub = INTERACTIVE_SUBS[base_cmd]
        if sub is None:
            return jsonify({
                "error":      f"'{base_cmd}' requires an interactive terminal (PTY). "
                              f"Use a non-interactive alternative, e.g. 'cat file' instead of 'nano file'.",
                "stdout":     "",
                "stderr":     "",
                "returncode": 1,
            }), 400
        command = sub

    # ── cd handling ────────────────────────────────────────────────────────────
    # SSH exec_command is stateless — each call opens a new channel with no
    # shared environment. 'cd /path' produces no output and the directory is
    # forgotten on the next call.
    # Solution: prepend the cwd from the request so the command runs in the
    # right directory. The client tracks cwd and sends it with each request.
    cwd = str(data.get("cwd") or "").strip()

    # If the command IS a cd, resolve the new path and return it as cwd
    # without running anything on the host.
    cd_match = command.strip().split()
    if cd_match and cd_match[0] == "cd":
        target = cd_match[1] if len(cd_match) > 1 else "~"
        if target == "-":
            # 'cd -' requires shell state we don't have; tell the client
            return jsonify({
                "stdout":     "",
                "stderr":     "cd - not supported in stateless mode. Use an absolute path.",
                "returncode": 1,
                "cwd":        cwd,
            })
        # Resolve relative paths on the remote host
        resolve_cmd = f"cd {target} 2>&1 && pwd"
        if cwd:
            resolve_cmd = f"cd {cwd} && " + resolve_cmd
        result = shell.run_ssh(alias, resolve_cmd, timeout=10)
        if result.get("returncode", 1) != 0 or "error" in result:
            return jsonify({
                "stdout":     "",
                "stderr":     result.get("stdout", result.get("stderr", result.get("error", ""))),
                "returncode": 1,
                "cwd":        cwd,
            })
        new_cwd = result.get("stdout", "").strip().split("\n")[-1].strip()
        return jsonify({
            "stdout":     "",
            "stderr":     "",
            "returncode": 0,
            "cwd":        new_cwd,
        })

    # Prepend cwd to all other commands
    if cwd:
        command = f"cd {cwd} && {command}"

    result = shell.run_ssh(alias, command, timeout=30)
    # run_ssh returns {stdout, stderr, returncode} or {error}
    if "error" in result and "stdout" not in result:
        return jsonify({
            "error":      result["error"],
            "stdout":     "",
            "stderr":     "",
            "returncode": 1,
            "cwd":        cwd,
        }), 502
    return jsonify({
        "stdout":     result.get("stdout", ""),
        "stderr":     result.get("stderr", ""),
        "returncode": result.get("returncode", 0),
        "cwd":        cwd,
    })

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def prewarm_models(model_tags, ollama_host):
    """Fire a trivial chat request at each model so Ollama loads weights
    into VRAM before the first real user query. Runs in a background thread
    so it doesn't block Flask startup — the first request that hits a model
    still in the process of warming will just wait a bit longer, but the
    HTTP server is already up and accepting connections.

    Uses /api/chat (not /api/generate) with the same num_ctx the real code
    path uses, so the prewarmed model stays loaded with the right context
    buffer. Otherwise a prewarm with num_ctx=default (e.g. 32K) would
    allocate a huge KV cache, and the real request with num_ctx=8192 would
    force a reload with a smaller buffer — paying the cold-load cost twice.
    """
    def _warm():
        for tag in model_tags:
            try:
                info = MODEL_INFO.get(tag, {})
                num_ctx = info.get("num_ctx", 8192)
                disable_thinking = info.get("disable_thinking", False)
                payload = {
                    "model": tag,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                    "options": {
                        "num_predict": 1,
                        "num_ctx": num_ctx,
                    },
                }
                if disable_thinking:
                    payload["think"] = False

                t0 = time.time()
                r = req.post(f"{ollama_host}/api/chat", json=payload, timeout=180)
                elapsed = time.time() - t0
                if r.ok:
                    print(f"  🔥 Prewarmed {tag} ({elapsed:.1f}s, num_ctx={num_ctx})")
                else:
                    print(f"  ⚠️  Prewarm {tag} failed: HTTP {r.status_code}: {r.text[:200]}")
            except Exception as e:
                print(f"  ⚠️  Prewarm {tag} error: {e}")

    t = threading.Thread(target=_warm, daemon=True, name="odin-prewarm")
    t.start()
    return t


def main():
    global vault

    print("=" * 60)
    print("  ⚔️  ODIN — BeanLab AI Assistant")
    print("=" * 60)
    print()

    print(f"  🧠 Ollama: {OLLAMA_HOST}")
    try:
        r = req.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        available = [m["name"] for m in r.json().get("models", [])]
        # Show only the tags this Odin install actively routes to, not every
        # model in Ollama. Deduped in role order so worker/vision share a line.
        active_tags = []
        for role in ("worker", "coder", "voice", "vision"):
            tag = MODELS.get(role)
            if tag and tag in available and tag not in active_tags:
                active_tags.append(tag)
        if active_tags:
            print(f"  📦 Active: {', '.join(active_tags)}")
        missing = [MODELS[r] for r in ("worker", "coder", "voice")
                   if MODELS.get(r) and MODELS[r] not in available]
        if missing:
            print(f"  ⚠️  Missing tags (pull or build): {', '.join(set(missing))}")
    except:
        available = []
        print("  ⚠️  Ollama not reachable")

    # Prewarm models tagged `prewarm: true` in models.yaml.
    # Skips tags that aren't actually pulled locally so missing models
    # don't block startup — a warning is printed instead.
    # Skip cloud models (provider: anthropic) — no local weights to load
    all_prewarm = registry.prewarm_targets()
    warm_targets = [tag for tag in all_prewarm if tag in available]
    missing_warm = [tag for tag in all_prewarm
                    if tag not in available
                    and MODEL_INFO.get(tag, {}).get("provider") != "anthropic"]
    if missing_warm:
        print(f"  ⚠️  Prewarm targets not found locally: {', '.join(missing_warm)}")
        print(f"     Pull or build them, or unset prewarm: true in models.yaml")
    if warm_targets:
        print(f"  🔥 Prewarming: {', '.join(warm_targets)} (in background)")
        prewarm_models(warm_targets, OLLAMA_HOST)
    else:
        print(f"  ⚠️  No prewarm targets available — first request will be cold")

    if VAULT_PATH:
        vault = FileSystemVault(VAULT_PATH)
        if not vault.connected:
            print(f"  ⚠️  Vault path not found: {VAULT_PATH}")
    elif OBSIDIAN_API_KEY:
        vault = ObsidianVault(OBSIDIAN_URL, OBSIDIAN_API_KEY)
        if vault.connected:
            print(f"  📚 Vault (REST API): connected")
        else:
            print(f"  ⚠️  Vault REST API not reachable at {OBSIDIAN_URL}")
    else:
        print(f"  📚 Vault not configured (set ODIN_VAULT_PATH or OBSIDIAN_API_KEY)")

    # Home Assistant — optional, only activates if both env vars are set
    global ha
    if HASS_URL and HASS_TOKEN:
        ha = HomeAssistant(HASS_URL, HASS_TOKEN)
        if ha.connected:
            print(f"  🏠 Home Assistant: connected ({HASS_URL})")
        else:
            print(f"  ⚠️  Home Assistant not reachable at {HASS_URL}")
    else:
        ha = HomeAssistant("", "")  # Creates a stub with connected=False
        print(f"  🏠 Home Assistant: not configured (set HASS_URL and HASS_TOKEN)")

    print(f"  🔑 SSH hosts: {len(SSH_HOSTS)}")
    print(f"  🌐 Web access: enabled")
    if ANTHROPIC_API_KEY:
        claude_info = next((v for v in MODEL_INFO.values() if v.get("provider") == "anthropic"), {})
        claude_model_str = claude_info.get("api_model", "claude-sonnet-4-6")
        print(f"  ☁️  Claude API: enabled (model: {claude_model_str}, cap: {ANTHROPIC_MAX_TOKENS} tokens/call)")
    else:
        print(f"  ☁️  Claude API: not configured (add ANTHROPIC_API_KEY to .env to enable)")
    print()

    # Get local IP for display
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except:
        local_ip = "localhost"

    print(f"  🌐 Open in browser:")
    print(f"     http://localhost:{WEB_PORT}")
    print(f"     http://{local_ip}:{WEB_PORT}")
    print(f"     (accessible from any device on your network)")
    print()

    import ssl
    tailnet_name = os.environ.get("TAILNET_NAME", "")

    if tailnet_name:
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        cert_file = os.path.join(BASE_DIR, f"{tailnet_name}.crt")
        key_file = os.path.join(BASE_DIR, f"{tailnet_name}.key")
        if os.path.exists(cert_file) and os.path.exists(key_file):
            print(f"  🔒 HTTPS enabled with Tailscale cert")
            print(f"     https://{tailnet_name}:{WEB_PORT}")
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(cert_file, key_file)
            app.run(host="0.0.0.0", port=WEB_PORT, debug=False, ssl_context=ssl_ctx)
        else:
            print(f"  ⚠️  Cert files not found for {tailnet_name}")
            print(f"     Run: tailscale cert {tailnet_name}")
            app.run(host="0.0.0.0", port=WEB_PORT, debug=False)
    else:
        print(f"  ⚠️  No TAILNET_NAME set — running HTTP only")
        print(f"     Set $env:TAILNET_NAME to enable HTTPS")
        app.run(host="0.0.0.0", port=WEB_PORT, debug=False)


if __name__ == "__main__":
    main()
