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
    from openai import OpenAI
except ImportError:
    print("Missing packages. Run:")
    print("  pip install flask flask-cors openai requests paramiko")
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
    r"\bsystemctl\s+(stop|disable|mask)", r"\bdocker\s+(rm|stop|kill|prune|restart)",
    r"\bqm\s+(stop|destroy|shutdown)", r"\bpct\s+(stop|destroy|shutdown)",
    r"\bmkfs\b", r"\bfdisk\b", r"\bdd\b", r"\buserdel\b",
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
            "name": "get_public_ip",
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


def call_llm(model, messages, tools=None, max_tokens=2048, temperature=0.7,
             timeout=180):
    """Invoke a model via Ollama's native /api/chat endpoint.

    Handles thinking suppression, tool calls, and response normalization.
    Raises RuntimeError on HTTP failure — caller should catch.
    """
    info = MODEL_INFO.get(model, {})
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
def classify(text, client):
    """Route a query to a category. Returns one of CATEGORIES keys.

    Heuristic-only — no LLM call. Routing priority:
      1. Voice/HA intent        → llama3.2:3b with HA tools
      2. Code or simple infra   → qwen3:4b (has tools, fast, no thinking)
      3. Complex multi-host     → gemma4:26b (full tools, full power)
      4. Reasoning/synthesis    → gemma4:26b
      5. Trivial chat           → llama3.2:3b
      6. Default                → gemma4:26b (safe fallback for ambiguous queries)

    Key change from earlier versions: qwen3:4b supports tool calling natively,
    so "code + infra" hybrid queries (like "debug my python script on
    ai-stack-420") can now stay on the small model instead of falling back
    to Gemma. Only genuinely complex queries escalate to Gemma.
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

    # ─── 2. Code or simple infra (qwen3:4b handles both) ─────────────
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
    # run on qwen3:4b too — it has tool calling and is much faster.
    simple_infra_verbs = (
        "check ", "show ", "get ", "list ", "what is the", "what's the",
        "is ", "are ", "how much ", "how many ",
    )
    has_simple_verb = any(lower.startswith(v) or f" {v}" in lower[:20]
                           for v in simple_infra_verbs)
    if (word_count <= 15 and host_mentions <= 1 and has_simple_verb
            and not has_complex):
        return "infra_simple"  # → qwen3:4b via CATEGORIES mapping

    # ─── 4. Trivial chat ────────────────────────────────────────────
    trivial_exact = (
        "hi", "hey", "hello", "thanks", "thank you", "thx", "ok", "okay",
        "yes", "no", "sup", "yo", "morning", "evening", "good night",
    )
    if lower in trivial_exact or lower.rstrip("?.!") in trivial_exact:
        return "trivial"

    # ─── 5. Everything else → Gemma 4 ───────────────────────────────
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
client = None
vault = None
ha = None  # HomeAssistant client, initialized in main() if HASS_URL+HASS_TOKEN are set
shell = ShellExecutor()
web = WebFetcher()


def get_system_prompt(scope="worker"):
    """Return the system prompt for a given agent scope.

    Each scope gets a focused prompt tailored to its model's capabilities
    and its assigned job. Smaller/focused prompts = faster inference and
    fewer mistakes, especially on small models like llama3.2:3b.
    """
    now = datetime.datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")

    if scope == "voice":
        # Tiny, laser-focused prompt for the 3B voice model. Only covers
        # Home Assistant intent-to-tool mapping. No mention of SSH, vault,
        # or shell — those aren't in its tool list anyway.
        return f"""You are Odin, a voice-control assistant for a smart home running Home Assistant.
Current time: {now}

YOUR JOB: Translate the user's spoken command into the correct Home Assistant tool call, then confirm briefly.

RULES:
1. When the user says "turn on/off" something, call ha_turn_on or ha_turn_off with the matching entity_id.
2. If you don't know the exact entity_id, call ha_list_entities first with an appropriate domain filter (light, switch, climate, media_player).
3. For dimming, brightness, color, temperature: use ha_set_state with the right domain and service.
4. Keep replies under 15 words. You're being spoken aloud.
5. Never make up entity_ids. If you can't find one, say so and ask which one they meant.
6. Formatting: plain text only. No markdown, no code blocks.
"""

    if scope == "coder":
        # Qwen3:4b with tool access. Focused on code and simple infra tasks,
        # but can call SSH/shell/web/vault tools when needed. Kept short
        # because the 4B model benefits from focused prompts.
        return f"""You are Odin's fast agent, powered by Qwen 3 4B.
Current time: {now}

YOUR JOB: Handle code questions and simple infrastructure tasks quickly.

CAPABILITIES: SSH, local shell, vault search, web fetch, Home Assistant.

CRITICAL — OUTPUT FORMAT:
- Output ONLY your final answer. No preamble, no reasoning, no "let me think".
- Do NOT explain your thought process. Do NOT narrate what you are doing.
- Start your response with the answer itself. Never with "Okay", "First", "Let me", "I need to".
- If you catch yourself writing a reasoning paragraph — stop and delete it. Write only the result.

RULES:
1. For code questions — answer directly. Write code that works. Use Markdown fences like ```python.
2. For "check X on host Y" queries — use run_ssh with the right host alias, then report the result in one line.
3. Make ONE or TWO tool calls maximum. If the problem needs more, say so and escalate.
4. Never repeat the same tool call with the same arguments.
5. If a tool errors, try ONE alternative — do not loop.
6. Keep responses concise. You're the fast path, not the deep path.
7. FORMATTING: plain text or Markdown only. No HTML tags.
"""

    # Default: worker — full BeanLab operations prompt with SSH hosts.
    hosts = "\n".join(
        f"  - {a}: {i['description']} ({_resolve_host_ip(i)})"
        for a, i in SSH_HOSTS.items()
    )
    return f"""You are Odin, an AI assistant for the BeanLab homelab network.
You are helpful, sharp, and efficient. Address the user as "sir" occasionally. Dry sense of humor.
Always respond in English.
Current time: {now}

CRITICAL — OUTPUT FORMAT:
- Output ONLY your final answer. No preamble, no internal reasoning, no narration.
- Do NOT write "Okay, let me...", "First, I need to...", "I should...", or any thought process.
- Start your response directly with the information or action. Never with meta-commentary.
- If you catch yourself writing a reasoning paragraph — stop and delete it. Answer directly.

CAPABILITIES: vault search/read/write, local shell, SSH to remote machines, web fetch, Home Assistant control, public IP lookup.

AVAILABLE SSH HOSTS:
{{hosts}}

RULES:
1. For homelab questions — search the vault FIRST.
2. For system checks — use run_ssh to the appropriate host.
3. For smart home control — use the ha_* tools.
4. Destructive commands are blocked automatically. Explain what you wanted to do.
5. Maximum 4 tool calls per response.
6. Keep responses concise — you may be speaking out loud.
7. NEVER repeat a tool call with the same arguments. If a tool returns empty or errors, try DIFFERENT arguments or answer with what you have.
8. If you've gathered partial information, answer with what you have rather than retrying.
9. Summarize web content — never dump raw HTML.
10. FORMATTING: Respond in plain text or Markdown only. NEVER emit HTML tags (no <code>, <br>, <strong>, etc.). Use `backticks` for inline code and ```fences``` for code blocks.
"""


def process_message(user_input, chat_id, model_override=None, attachments=None):
    global client, vault

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
        category = classify(user_input, client)
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

    # Trim long histories
    if len(history) > 30:
        history = history[-30:]

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
            t_llm = time.time()
            resp = call_llm(
                model=model,
                messages=iter_messages,
                tools=iter_tools,
                max_tokens=2048,
                temperature=0.7,
                timeout=180,
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

        # No tool calls — this is the final answer.
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
    return HTML_PAGE

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
    return jsonify({
        "ollama": OLLAMA_HOST,
        "vault": vault.connected if vault else False,
        "ssh_hosts": list(SSH_HOSTS.keys()),
        "models": list(MODELS.values()),
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

"""
ODIN COMMAND CENTER — NEW API ENDPOINTS
========================================
 
Add these routes to Odin.py after the existing /api/logs route (~line 1946).
 
These provide the data feeds for the Command Center UI:
  - /api/hosts/status   — live ping + port check for all hosts
  - /api/hosts/<alias>/metrics — CPU, RAM, disk for a specific host via SSH
  - /api/telemetry      — aggregated metrics for the dashboard widgets
  - /api/session        — current session state (active tools, recent actions)
"""
@app.route("/cc")
def command_center():
    with open(os.path.join(os.path.dirname(__file__), "static", "odin-ui.html")) as f:
        return f.read()
 
# ─── Host status with live ping ──────────────────────────────────────────
@app.route("/api/hosts/status")
def hosts_status():
    """Return all hosts with live reachability status.
 
    Pings each host in parallel and checks key ports. Results are cached
    for 30 seconds to avoid hammering the network on rapid UI polls.
    """
    import socket as _sock
 
    def _check_host(alias, info):
        ip = _resolve_host_ip(info)
        # Quick ping — 1 packet, 2s timeout
        try:
            r = subprocess.run(
                ["ping", "-c", "1", "-W", "2", ip],
                capture_output=True, text=True, timeout=5
            )
            reachable = r.returncode == 0
            latency = None
            if reachable:
                for line in r.stdout.split("\n"):
                    if "time=" in line:
                        try:
                            latency = float(line.split("time=")[1].split(" ")[0].replace("ms", ""))
                        except (ValueError, IndexError):
                            pass
                        break
        except Exception:
            reachable = False
            latency = None
 
        # Quick port check on first configured port (if description mentions one)
        port_status = None
        # We don't have ports in hosts.json, so just check SSH (22)
        try:
            s = _sock.create_connection((ip, 22), timeout=2)
            s.close()
            port_status = "open"
        except Exception:
            port_status = "closed"
 
        return {
            "alias": alias,
            "ip": ip,
            "user": info.get("user", ""),
            "description": info.get("description", ""),
            "reachable": reachable,
            "latency_ms": latency,
            "ssh_port": port_status,
        }
 
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_check_host, a, i): a for a, i in SSH_HOSTS.items()}
        results = []
        for f in as_completed(futures):
            try:
                results.append(f.result())
            except Exception as e:
                alias = futures[f]
                results.append({"alias": alias, "reachable": False, "error": str(e)})
 
    # Sort by alias for consistent ordering
    results.sort(key=lambda r: r["alias"])
    online = sum(1 for r in results if r.get("reachable"))
 
    return jsonify({
        "hosts": results,
        "total": len(results),
        "online": online,
        "offline": len(results) - online,
        "timestamp": datetime.datetime.now(MST).isoformat(),
    })
 
 
# ─── Per-host metrics via SSH ────────────────────────────────────────────
@app.route("/api/hosts/<alias>/metrics")
def host_metrics(alias):
    """Fetch CPU, RAM, disk, and uptime from a host via SSH."""
    if alias not in SSH_HOSTS:
        return jsonify({"error": f"Unknown host: {alias}"}), 404
 
    # One-liner that outputs JSON-ish stats
    cmd = (
        "echo '{';"
        "echo '\"cpu_percent\"':$(top -bn1 | grep 'Cpu(s)' | awk '{print 100-$8}');"
        "echo ',\"ram_total_mb\"':$(free -m | awk '/Mem:/{print $2}');"
        "echo ',\"ram_used_mb\"':$(free -m | awk '/Mem:/{print $3}');"
        "echo ',\"ram_percent\"':$(free | awk '/Mem:/{printf \"%.1f\", $3/$2*100}');"
        "echo ',\"disk_percent\"':$(df / | awk 'NR==2{gsub(/%/,\"\",$5); print $5}');"
        "echo ',\"disk_total_gb\"':$(df -BG / | awk 'NR==2{gsub(/G/,\"\",$2); print $2}');"
        "echo ',\"disk_used_gb\"':$(df -BG / | awk 'NR==2{gsub(/G/,\"\",$3); print $3}');"
        "echo ',\"uptime\"':\"'\"$(uptime -p)\"'\";"
        "echo ',\"load_1m\"':$(cat /proc/loadavg | awk '{print $1}');"
        "echo ',\"load_5m\"':$(cat /proc/loadavg | awk '{print $2}');"
        "echo ',\"load_15m\"':$(cat /proc/loadavg | awk '{print $3}');"
        "echo '}'"
    )
 
    result = shell.run_ssh(alias, cmd, timeout=10)
    if "error" in result:
        return jsonify({"error": result["error"], "alias": alias}), 500
 
    # Parse the rough JSON output
    import re as _re
    raw = result.get("stdout", "")
    try:
        # Clean up the output — it's not perfect JSON
        cleaned = _re.sub(r"(\w+):", r'"\1":', raw.replace("'", '"'))
        data = json.loads(cleaned)
    except Exception:
        # Fall back to raw output
        data = {"raw": raw.strip()}
 
    data["alias"] = alias
    data["ip"] = _resolve_host_ip(SSH_HOSTS[alias])
    data["timestamp"] = datetime.datetime.now(MST).isoformat()
 
    return jsonify(data)
 
 
# ─── Aggregated telemetry (polled by dashboard) ─────────────────────────
@app.route("/api/telemetry")
def telemetry():
    """Quick telemetry for the local machine (ai-stack-420).
 
    Uses /proc directly — no SSH needed since we're on the host.
    Falls back gracefully if psutil isn't available.
    """
    data = {
        "timestamp": datetime.datetime.now(MST).isoformat(),
        "hostname": "ai-stack-420",
    }
 
    # CPU
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            data["load_1m"] = float(parts[0])
            data["load_5m"] = float(parts[1])
            data["load_15m"] = float(parts[2])
    except Exception:
        pass
 
    # Memory
    try:
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                key, val = line.split(":")
                meminfo[key.strip()] = int(val.strip().split()[0])
            total = meminfo.get("MemTotal", 1)
            available = meminfo.get("MemAvailable", 0)
            data["ram_total_mb"] = total // 1024
            data["ram_used_mb"] = (total - available) // 1024
            data["ram_percent"] = round((1 - available / total) * 100, 1)
    except Exception:
        pass
 
    # Disk
    try:
        r = subprocess.run(["df", "/", "--output=size,used,pcent"],
                           capture_output=True, text=True, timeout=5)
        lines = r.stdout.strip().split("\n")
        if len(lines) >= 2:
            parts = lines[1].split()
            data["disk_total_gb"] = round(int(parts[0]) / 1024 / 1024, 1)
            data["disk_used_gb"] = round(int(parts[1]) / 1024 / 1024, 1)
            data["disk_percent"] = int(parts[2].replace("%", ""))
    except Exception:
        pass
 
    # GPU (nvidia-smi)
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            parts = [p.strip() for p in r.stdout.strip().split(",")]
            data["gpu_util_percent"] = int(parts[0])
            data["gpu_vram_used_mb"] = int(parts[1])
            data["gpu_vram_total_mb"] = int(parts[2])
            data["gpu_temp_c"] = int(parts[3])
    except Exception:
        pass
 
    # Ollama models currently loaded
    try:
        r = req.get(f"{OLLAMA_HOST}/api/ps", timeout=3)
        if r.ok:
            running = r.json().get("models", [])
            data["ollama_loaded"] = [
                {"name": m.get("name"), "size_mb": m.get("size", 0) // 1_000_000}
                for m in running
            ]
    except Exception:
        pass
 
    return jsonify(data)
 
 
# ─── Session state (active context for the sidebar) ────────────────────
@app.route("/api/session")
def session_state():
    """Return current session context for the Command Center sidebar."""
    # Active SSH connections
    active_ssh = []
    for alias in shell.ssh_clients:
        try:
            transport = shell.ssh_clients[alias].get_transport()
            if transport and transport.is_active():
                active_ssh.append(alias)
        except Exception:
            pass
 
    # Recent tool usage from logger
    recent_tools = []
    for event in logger.recent(n=50):
        if event.get("type") == "tool_call":
            recent_tools.append({
                "tool": event.get("tool"),
                "ts": event.get("ts"),
                "host": event.get("host", ""),
            })
 
    # Vault status
    vault_status = {
        "connected": vault.connected if vault else False,
        "type": "filesystem" if isinstance(vault, FileSystemVault) else "rest_api" if vault else "none",
    }
 
    # HA status
    ha_status = {
        "connected": ha.connected if ha else False,
        "url": HASS_URL if ha and ha.connected else None,
    }
 
    return jsonify({
        "active_ssh": active_ssh,
        "recent_tools": recent_tools[-10:],  # Last 10
        "vault": vault_status,
        "home_assistant": ha_status,
        "host_count": len(SSH_HOSTS),
        "chat_count": len(db.list_chats()),
        "uptime_seconds": int(time.time() - _odin_start_time),
    })

# ---------------------------------------------------------------------------
# HTML/CSS/JS — The Jarvis UI
# ---------------------------------------------------------------------------
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="#0d0d0f">
<title>Odin — BeanLab</title>
<link rel="manifest" href="/manifest.json">
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<link rel="shortcut icon" href="/favicon.ico">
<style>
/* ─────────────────────────────────────────────────────────────
   ODIN — REFINED MINIMAL
   Typography: Fraunces (display) + Geist (body) + JetBrains Mono (code)
   Palette: warm off-black + single copper accent
   ───────────────────────────────────────────────────────────── */
:root {
  --bg:            #0e0e10;
  --bg-elev:       #141418;
  --bg-sidebar:    #0b0b0d;
  --bg-input:      #17171c;
  --bg-code:       #0a0a0c;

  --line:          rgba(255,255,255,0.06);
  --line-strong:   rgba(255,255,255,0.12);

  --text:          #ececee;
  --text-mute:     #8a8a94;
  --text-dim:      #55555f;

  --bubble-you:    #1b2836;   /* cool slate for user, right side */
  --bubble-you-br: rgba(120,160,210,0.22);
  --bubble-odin:   #15151a;   /* warm dark for assistant, left side */
  --bubble-odin-br:rgba(255,255,255,0.08);

  --accent:        #c08b5c;   /* single copper accent, used sparingly */
  --accent-soft:   rgba(192,139,92,0.14);
  --danger:        #d96b6b;
  --success:       #6dbd94;

  --radius:        10px;
  --radius-sm:     6px;
  --radius-lg:     16px;

  --sidebar-w:     264px;
  --topbar-h:      48px;
  --max-read:      760px;

  --ease:          cubic-bezier(.22,.61,.36,1);
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

html, body {
  height: 100%;
  background: var(--bg);
  color: var(--text);
  font-family: 'Geist', 'Inter', -apple-system, system-ui, sans-serif;
  font-size: 14px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
  overflow: hidden;
}

::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.06); border-radius: 8px; }
::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.12); }

button { font: inherit; color: inherit; background: none; border: none; cursor: pointer; }
input, textarea { font: inherit; color: inherit; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* ─── LAYOUT ─── */
.app { display: flex; height: 100vh; }

/* ─── SIDEBAR ─── */
.sidebar {
  width: var(--sidebar-w);
  min-width: var(--sidebar-w);
  background: var(--bg-sidebar);
  border-right: 1px solid var(--line);
  display: flex;
  flex-direction: column;
  transition: margin-left .24s var(--ease);
  z-index: 50;
}
.sidebar.collapsed { margin-left: calc(-1 * var(--sidebar-w)); }

.sidebar-header {
  padding: 18px 18px 14px;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}
.status-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--text-dim);
  transition: background .3s;
}
.status-dot.online { background: var(--success); box-shadow: 0 0 0 3px rgba(109,189,148,0.12); }
.sidebar-logo {
  font-family: 'Fraunces', Georgia, serif;
  font-weight: 500;
  font-size: 20px;
  letter-spacing: -0.01em;
  color: var(--text);
}
.sidebar-logo em { font-style: italic; color: var(--accent); font-weight: 400; }

.new-chat-btn {
  margin: 0 14px 10px;
  padding: 10px 12px;
  background: transparent;
  border: 1px solid var(--line-strong);
  border-radius: var(--radius);
  color: var(--text);
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 8px;
  transition: border-color .18s, background .18s;
}
.new-chat-btn:hover { border-color: var(--accent); background: var(--accent-soft); }
.new-chat-btn svg { width: 14px; height: 14px; fill: currentColor; }

.sidebar-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 6px 8px 14px;
}

.project-group { margin-bottom: 4px; }

.project-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  color: var(--text-mute);
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  cursor: pointer;
  transition: color .15s, background .15s;
}
.project-header:hover { color: var(--text); background: rgba(255,255,255,0.02); }
.project-header .project-icon { font-size: 13px; }
.project-header .project-name { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.project-header .chevron {
  font-size: 9px;
  color: var(--text-dim);
  transition: transform .2s;
}
.project-header.open .chevron { transform: rotate(90deg); }
.project-add-btn {
  width: 18px; height: 18px;
  border-radius: 4px;
  color: var(--text-dim);
  font-size: 14px;
  line-height: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}
.project-add-btn:hover { background: rgba(255,255,255,0.05); color: var(--accent); }

.project-chats {
  overflow: hidden;
  max-height: 0;
  transition: max-height .2s var(--ease);
}
.project-chats.open { max-height: 1200px; }

.chat-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 12px 7px 24px;
  margin: 1px 0;
  border-radius: var(--radius-sm);
  color: var(--text-mute);
  font-size: 13px;
  cursor: pointer;
  position: relative;
  transition: background .15s, color .15s;
}
.chat-item:hover { background: rgba(255,255,255,0.03); color: var(--text); }
.chat-item.active {
  background: rgba(255,255,255,0.05);
  color: var(--text);
}
.chat-item.active::before {
  content: '';
  position: absolute;
  left: 12px;
  top: 50%;
  transform: translateY(-50%);
  width: 2px;
  height: 14px;
  background: var(--accent);
  border-radius: 2px;
}
.chat-item .chat-title {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.chat-item .chat-del {
  opacity: 0;
  color: var(--text-dim);
  font-size: 12px;
  padding: 0 4px;
  transition: opacity .15s, color .15s;
}
.chat-item:hover .chat-del { opacity: 1; }
.chat-item .chat-del:hover { color: var(--danger); }

.add-project-btn {
  width: calc(100% - 4px);
  margin: 8px 2px 0;
  padding: 8px 10px;
  border: 1px dashed var(--line-strong);
  border-radius: var(--radius-sm);
  color: var(--text-dim);
  font-size: 11px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  transition: border-color .18s, color .18s;
}
.add-project-btn:hover { border-color: var(--accent); color: var(--accent); }

.sidebar-footer {
  padding: 12px 18px;
  border-top: 1px solid var(--line);
  font-size: 10px;
  color: var(--text-dim);
  letter-spacing: 0.05em;
  text-transform: uppercase;
  flex-shrink: 0;
}

/* ─── MAIN ─── */
.main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  position: relative;
}

.topbar {
  height: var(--topbar-h);
  border-bottom: 1px solid var(--line);
  display: flex;
  align-items: center;
  padding: 0 16px;
  gap: 12px;
  flex-shrink: 0;
}
.topbar-btn {
  width: 30px; height: 30px;
  border-radius: var(--radius-sm);
  color: var(--text-mute);
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background .15s, color .15s;
}
.topbar-btn:hover { background: rgba(255,255,255,0.05); color: var(--text); }
.topbar-btn svg { width: 16px; height: 16px; fill: currentColor; }

.model-selector { position: relative; }
.model-current {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  font-size: 12px;
  color: var(--text-mute);
  cursor: pointer;
  transition: border-color .15s, color .15s;
}
.model-current:hover { border-color: var(--line-strong); color: var(--text); }
.model-current svg { width: 10px; height: 10px; fill: currentColor; }
.model-dropdown {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  min-width: 200px;
  background: var(--bg-elev);
  border: 1px solid var(--line-strong);
  border-radius: var(--radius);
  padding: 4px;
  display: none;
  box-shadow: 0 16px 40px rgba(0,0,0,0.5);
  z-index: 80;
}
.model-dropdown.open { display: block; }
.model-opt {
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  font-size: 13px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  cursor: pointer;
  transition: background .12s;
}
.model-opt:hover { background: rgba(255,255,255,0.04); }
.model-opt.active { background: var(--accent-soft); color: var(--accent); }
.model-badge {
  font-size: 9px;
  letter-spacing: 0.05em;
  color: var(--text-dim);
  padding: 2px 6px;
  border: 1px solid var(--line);
  border-radius: 3px;
  text-transform: uppercase;
}

.topbar-spacer { flex: 1; }

.volume-ctrl {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 4px;
  color: var(--text-mute);
}
.volume-ctrl svg { width: 14px; height: 14px; fill: currentColor; flex-shrink: 0; }
.volume-slider {
  -webkit-appearance: none; appearance: none;
  width: 80px;
  height: 2px;
  background: var(--line-strong);
  border-radius: 2px;
  cursor: pointer;
}
.volume-slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 12px; height: 12px;
  border-radius: 50%;
  background: var(--accent);
  border: 2px solid var(--bg);
}
.volume-slider::-moz-range-thumb {
  width: 12px; height: 12px;
  border-radius: 50%;
  background: var(--accent);
  border: 2px solid var(--bg);
}

.latency-badge {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--text-dim);
  padding: 3px 8px;
  border: 1px solid var(--line);
  border-radius: 3px;
  min-width: 48px;
  text-align: center;
}

/* ─── CHAT AREA ─── */
.chat-wrap {
  flex: 1;
  overflow-y: auto;
  position: relative;
  scroll-behavior: smooth;
}
.chat-inner {
  max-width: var(--max-read);
  margin: 0 auto;
  padding: 28px 28px 40px;
}

/* Welcome */
.welcome {
  min-height: calc(100vh - var(--topbar-h) - 180px);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: 40px 20px;
}
.welcome h1 {
  font-family: 'Fraunces', Georgia, serif;
  font-weight: 400;
  font-size: 72px;
  letter-spacing: -0.03em;
  color: var(--text);
  margin-bottom: 8px;
}
.welcome h1 em {
  font-style: italic;
  font-weight: 300;
  color: var(--accent);
}
.welcome p {
  color: var(--text-mute);
  font-size: 14px;
  margin-bottom: 36px;
  max-width: 440px;
}
.suggestions {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  max-width: 520px;
  width: 100%;
}
.suggestion {
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  font-size: 13px;
  color: var(--text-mute);
  text-align: left;
  cursor: pointer;
  transition: border-color .15s, color .15s, background .15s;
}
.suggestion:hover {
  border-color: var(--line-strong);
  color: var(--text);
  background: rgba(255,255,255,0.02);
}

/* ─── MESSAGES: LEFT / RIGHT BUBBLES ─── */
.messages { display: flex; flex-direction: column; gap: 18px; }

.msg-row {
  display: flex;
  gap: 10px;
  animation: msgIn .28s var(--ease);
}
.msg-row.user { justify-content: flex-end; }
.msg-row.ai   { justify-content: flex-start; }

@keyframes msgIn {
  from { opacity: 0; transform: translateY(6px); }
  to   { opacity: 1; transform: translateY(0); }
}

.msg-col {
  display: flex;
  flex-direction: column;
  max-width: 78%;
  min-width: 0;
}
.msg-row.user .msg-col { align-items: flex-end; }
.msg-row.ai   .msg-col { align-items: flex-start; }

.msg-meta {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--text-dim);
  margin-bottom: 4px;
  padding: 0 4px;
  display: flex;
  gap: 6px;
  align-items: center;
}
.msg-meta .who {
  color: var(--text-mute);
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.msg-meta .dot { color: var(--text-dim); }

.msg-bubble {
  padding: 11px 15px;
  border-radius: 14px;
  border: 1px solid var(--bubble-odin-br);
  background: var(--bubble-odin);
  font-size: 14px;
  line-height: 1.6;
  word-wrap: break-word;
  overflow-wrap: break-word;
  white-space: pre-wrap;
}
.msg-row.user .msg-bubble {
  background: var(--bubble-you);
  border-color: var(--bubble-you-br);
  border-bottom-right-radius: 4px;
  white-space: pre-wrap;
}
.msg-row.ai .msg-bubble {
  border-bottom-left-radius: 4px;
  white-space: normal;
}

/* Markdown inside bubbles */
.msg-bubble p { margin: 0; }
.msg-bubble p + p { margin-top: 10px; }
.msg-bubble h1, .msg-bubble h2, .msg-bubble h3 {
  font-family: 'Fraunces', Georgia, serif;
  font-weight: 500;
  margin: 14px 0 6px;
  color: var(--text);
  letter-spacing: -0.01em;
}
.msg-bubble h1 { font-size: 20px; }
.msg-bubble h2 { font-size: 17px; }
.msg-bubble h3 { font-size: 15px; }
.msg-bubble ul, .msg-bubble ol { margin: 8px 0 8px 20px; }
.msg-bubble li { margin: 3px 0; }
.msg-bubble blockquote {
  border-left: 2px solid var(--accent);
  padding: 2px 0 2px 12px;
  color: var(--text-mute);
  margin: 8px 0;
}
.msg-bubble hr {
  border: none;
  border-top: 1px solid var(--line);
  margin: 12px 0;
}
.msg-bubble code {
  font-family: 'JetBrains Mono', monospace;
  font-size: 12.5px;
  background: rgba(255,255,255,0.06);
  padding: 1px 5px;
  border-radius: 3px;
}

/* Code blocks */
.code-block-wrap {
  background: var(--bg-code);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  margin: 10px 0;
  overflow: hidden;
}
.code-block-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border-bottom: 1px solid var(--line);
  background: rgba(255,255,255,0.02);
}
.code-lang {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--text-dim);
  letter-spacing: 0.05em;
  text-transform: uppercase;
  flex: 1;
}
.code-btn {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--text-mute);
  padding: 3px 8px;
  border-radius: 4px;
  transition: color .15s, background .15s;
}
.code-btn:hover { color: var(--accent); background: var(--accent-soft); }
.code-btn svg { width: 11px; height: 11px; fill: currentColor; }
.code-block-wrap pre {
  margin: 0;
  padding: 12px 14px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--text);
  overflow-x: auto;
  white-space: pre;
}

/* Tool badges */
.tool-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 6px;
  padding: 0 4px;
}
.tool-badge {
  font-family: 'JetBrains Mono', monospace;
  font-size: 9.5px;
  color: var(--accent);
  background: var(--accent-soft);
  padding: 2px 7px;
  border-radius: 3px;
  letter-spacing: 0.02em;
}

/* Message actions */
.msg-actions {
  display: flex;
  gap: 2px;
  margin-top: 4px;
  opacity: 0;
  transition: opacity .15s;
}
.msg-row:hover .msg-actions { opacity: 1; }
.msg-action-btn {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--text-dim);
  padding: 3px 7px;
  border-radius: 4px;
  transition: color .15s, background .15s;
}
.msg-action-btn:hover { color: var(--accent); background: var(--accent-soft); }
.msg-action-btn svg { width: 11px; height: 11px; fill: currentColor; }

/* Thinking indicator */
.thinking {
  display: inline-flex;
  gap: 4px;
  padding: 2px 0;
}
.thinking span {
  width: 5px; height: 5px;
  border-radius: 50%;
  background: var(--text-mute);
  animation: thinkPulse 1.2s infinite ease-in-out;
}
.thinking span:nth-child(2) { animation-delay: 0.15s; }
.thinking span:nth-child(3) { animation-delay: 0.3s; }
@keyframes thinkPulse {
  0%,60%,100% { opacity: 0.3; transform: scale(0.85); }
  30% { opacity: 1; transform: scale(1); }
}

/* ─── SCROLL-TO-BOTTOM BUTTON ─── */
.scroll-btn {
  position: absolute;
  right: 24px;
  bottom: 130px;
  width: 36px;
  height: 36px;
  border-radius: 50%;
  background: var(--bg-elev);
  border: 1px solid var(--line-strong);
  color: var(--text-mute);
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 6px 18px rgba(0,0,0,0.4);
  opacity: 0;
  pointer-events: none;
  transform: translateY(8px);
  transition: opacity .2s, transform .2s, color .15s, border-color .15s;
  z-index: 10;
}
.scroll-btn.visible {
  opacity: 1;
  pointer-events: auto;
  transform: translateY(0);
}
.scroll-btn:hover {
  color: var(--accent);
  border-color: var(--accent);
}
.scroll-btn svg { width: 16px; height: 16px; fill: currentColor; }

/* ─── INPUT AREA ─── */
.input-area {
  padding: 12px 28px 20px;
  flex-shrink: 0;
  background: linear-gradient(to top, var(--bg) 70%, transparent);
}
.input-shell {
  max-width: var(--max-read);
  margin: 0 auto;
}
.attachments-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.attach-chip {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  background: var(--bg-elev);
  border: 1px solid var(--line);
  border-radius: 20px;
  font-size: 11px;
  color: var(--text-mute);
}
.attach-chip button {
  color: var(--text-dim);
  font-size: 12px;
  line-height: 1;
  padding: 0 2px;
}
.attach-chip button:hover { color: var(--danger); }

.input-row {
  display: flex;
  align-items: flex-end;
  gap: 6px;
  background: var(--bg-input);
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-lg);
  padding: 6px 6px 6px 10px;
  transition: border-color .2s;
}
.input-row:focus-within { border-color: var(--accent); }

.input-btn {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  color: var(--text-mute);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: background .15s, color .15s;
}
.input-btn:hover { background: rgba(255,255,255,0.05); color: var(--text); }
.input-btn svg { width: 17px; height: 17px; fill: currentColor; }
.input-btn.send {
  background: var(--accent);
  color: #0e0e10;
}
.input-btn.send:hover { background: #d49b6a; color: #0e0e10; }
.input-btn.danger { color: var(--danger); }
.input-btn.danger:hover { background: rgba(217,107,107,0.12); color: var(--danger); }

.chat-textarea {
  flex: 1;
  background: transparent;
  border: none;
  outline: none;
  resize: none;
  padding: 8px 4px;
  font-size: 14px;
  color: var(--text);
  max-height: 200px;
  line-height: 1.5;
  font-family: inherit;
}
.chat-textarea::placeholder { color: var(--text-dim); }

.input-hint {
  margin-top: 6px;
  text-align: center;
  font-size: 10px;
  color: var(--text-dim);
  letter-spacing: 0.02em;
}
.input-hint kbd {
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  padding: 1px 5px;
  border: 1px solid var(--line);
  border-radius: 3px;
  color: var(--text-mute);
}

/* ─── VOICE MODAL ─── */
.voice-overlay {
  position: fixed;
  inset: 0;
  background: rgba(8,8,10,0.92);
  backdrop-filter: blur(8px);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 300;
}
.voice-overlay.open { display: flex; }
.voice-modal {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 32px;
}
.voice-bubble-wrap {
  position: relative;
  width: 200px;
  height: 200px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.voice-ring {
  position: absolute;
  inset: 0;
  border: 1px solid var(--accent);
  border-radius: 50%;
  opacity: 0;
  animation: voiceRing 3s infinite ease-out;
}
.voice-ring:nth-child(2) { animation-delay: 1s; }
.voice-ring:nth-child(3) { animation-delay: 2s; }
@keyframes voiceRing {
  0%   { transform: scale(0.7); opacity: 0.6; }
  100% { transform: scale(1.4); opacity: 0; }
}
.voice-bubble {
  width: 120px;
  height: 120px;
  border-radius: 50%;
  background: radial-gradient(circle at 30% 30%, var(--accent), #7a5432);
  transition: transform .2s;
}
.voice-bubble.active { transform: scale(1.08); }
.voice-status {
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  color: var(--text-mute);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  min-height: 18px;
}
.voice-close {
  padding: 8px 20px;
  border: 1px solid var(--line-strong);
  border-radius: 20px;
  color: var(--text-mute);
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  transition: border-color .15s, color .15s;
}
.voice-close:hover { border-color: var(--danger); color: var(--danger); }

/* ─── PROJECT MODAL ─── */
.modal-bg {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.6);
  backdrop-filter: blur(4px);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 250;
}
.modal-bg.open { display: flex; }
.modal {
  background: var(--bg-elev);
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-lg);
  padding: 24px;
  width: 360px;
  max-width: 90vw;
}
.modal h3 {
  font-family: 'Fraunces', Georgia, serif;
  font-weight: 500;
  font-size: 20px;
  margin-bottom: 14px;
  letter-spacing: -0.01em;
}
.modal input[type="text"] {
  width: 100%;
  background: var(--bg-input);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
  font-size: 14px;
  color: var(--text);
  outline: none;
  transition: border-color .15s;
}
.modal input[type="text"]:focus { border-color: var(--accent); }
.emoji-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 6px;
  margin: 14px 0;
}
.emoji-opt {
  aspect-ratio: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: border-color .15s, background .15s;
}
.emoji-opt:hover { border-color: var(--line-strong); }
.emoji-opt.selected { border-color: var(--accent); background: var(--accent-soft); }
.modal-btns {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  margin-top: 8px;
}
.modal-btns button {
  padding: 8px 16px;
  border-radius: var(--radius-sm);
  font-size: 13px;
  color: var(--text-mute);
  transition: color .15s, background .15s;
}
.modal-btns button:hover { color: var(--text); background: rgba(255,255,255,0.04); }
.modal-btns .btn-primary {
  background: var(--accent);
  color: #0e0e10;
}
.modal-btns .btn-primary:hover { background: #d49b6a; color: #0e0e10; }

/* ─── LOGS PANEL ─── */
#logsPanel {
  display: none;
  position: fixed;
  top: calc(var(--topbar-h) + 8px);
  right: 16px;
  width: 420px;
  max-height: 60vh;
  background: var(--bg-elev);
  border: 1px solid var(--line-strong);
  border-radius: var(--radius);
  overflow-y: auto;
  padding: 12px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  color: var(--text-mute);
  z-index: 100;
  box-shadow: 0 16px 40px rgba(0,0,0,0.5);
}
#logsPanel.open { display: block; }

/* ─── MOBILE ─── */
@media (max-width: 720px) {
  .sidebar { position: fixed; top: 0; bottom: 0; left: 0; }
  .sidebar.collapsed { margin-left: calc(-1 * var(--sidebar-w)); }
  .chat-inner, .input-shell { padding-left: 14px; padding-right: 14px; }
  .input-area { padding: 10px 14px 16px; }
  .welcome h1 { font-size: 48px; }
  .msg-col { max-width: 88%; }
  .volume-ctrl .volume-slider { width: 56px; }
  .scroll-btn { right: 14px; bottom: 120px; }
}

/* Load Fraunces + Geist */
@import url('https://fonts.googleapis.com/css2?family=Fraunces:ital,wght@0,300..700;1,300..700&family=Geist:wght@300..600&family=JetBrains+Mono:wght@400;500&display=swap');
</style>
</head>
<body>
<div class="app">

  <!-- ══════════ SIDEBAR ══════════ -->
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-header">
      <div class="status-dot" id="statusDot"></div>
      <div class="sidebar-logo">Od<em>i</em>n</div>
    </div>

    <button class="new-chat-btn" onclick="newChat()">
      <svg viewBox="0 0 24 24"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg>
      New chat
    </button>

    <div class="sidebar-scroll" id="projectSection"></div>

    <div class="sidebar-footer">Auto-purge · Sun 00:00 MST</div>
  </aside>

  <!-- ══════════ MAIN ══════════ -->
  <div class="main">

    <!-- TOPBAR -->
    <div class="topbar">
      <button class="topbar-btn" onclick="toggleSidebar()" title="Toggle sidebar">
        <svg viewBox="0 0 24 24"><path d="M3 6h18v2H3zm0 5h18v2H3zm0 5h18v2H3z"/></svg>
      </button>

      <div class="model-selector">
        <div class="model-current" onclick="toggleModelDropdown(event)">
          <span id="modelLabel">Auto</span>
          <svg viewBox="0 0 24 24"><path d="M7 10l5 5 5-5z"/></svg>
        </div>
        <div class="model-dropdown" id="modelDropdown"></div>
      </div>

      <button class="topbar-btn" onclick="toggleLogs()" title="Logs">
        <svg viewBox="0 0 24 24"><path d="M3 3h18v2H3zm4 4h14v2H7zm-4 4h18v2H3zm4 4h14v2H7zm-4 4h18v2H3z"/></svg>
      </button>

      <div class="topbar-spacer"></div>

      <!-- Volume control -->
      <div class="volume-ctrl" title="TTS volume">
        <svg id="volIcon" viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02z"/></svg>
        <input type="range" class="volume-slider" id="volSlider" min="0" max="1" step="0.05" value="0.9" oninput="setVolume(this.value)">
      </div>

      <span class="latency-badge" id="latencyBadge">—</span>
    </div>

    <!-- CHAT WRAP -->
    <div class="chat-wrap" id="chatWrap">
      <div class="chat-inner">
        <div id="welcome" class="welcome">
          <h1>Od<em>i</em>n</h1>
          <p>BeanLab AI — voice, network, vault. All connected.</p>
          <div class="suggestions">
            <div class="suggestion" onclick="useSuggestion(this)">Check GPU on ai-stack-420</div>
            <div class="suggestion" onclick="useSuggestion(this)">What's my public IP?</div>
            <div class="suggestion" onclick="useSuggestion(this)">Uptime on all Proxmox nodes</div>
            <div class="suggestion" onclick="useSuggestion(this)">TrueNAS disk usage</div>
          </div>
        </div>
        <div class="messages" id="messages"></div>
      </div>
    </div>

    <!-- SCROLL TO BOTTOM -->
    <button class="scroll-btn" id="scrollBtn" onclick="scrollToBottom(true)" title="Jump to latest">
      <svg viewBox="0 0 24 24"><path d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6z"/></svg>
    </button>

    <!-- INPUT AREA -->
    <div class="input-area">
      <div class="input-shell">
        <div class="attachments-row" id="attachmentsRow"></div>
        <div class="input-row">
          <button class="input-btn" onclick="document.getElementById('fileInput').click()" title="Attach file">
            <svg viewBox="0 0 24 24"><path d="M16.5 6v11.5c0 2.21-1.79 4-4 4s-4-1.79-4-4V5c0-1.38 1.12-2.5 2.5-2.5s2.5 1.12 2.5 2.5v10.5c0 .55-.45 1-1 1s-1-.45-1-1V6H10v9.5c0 1.38 1.12 2.5 2.5 2.5s2.5-1.12 2.5-2.5V5c0-2.21-1.79-4-4-4S7 2.79 7 5v12.5c0 3.04 2.46 5.5 5.5 5.5s5.5-2.46 5.5-5.5V6h-1.5z"/></svg>
          </button>
          <input type="file" id="fileInput" style="display:none" onchange="handleFiles(event)" multiple
            accept=".txt,.md,.py,.js,.ts,.json,.yaml,.yml,.toml,.ini,.conf,.cfg,.sh,.bash,.ps1,.log,.csv,.html,.css,.xml,.pdf,.png,.jpg,.jpeg,.gif,.webp">

          <textarea class="chat-textarea" id="chatInput"
            placeholder="Ask Odin anything…"
            rows="1"
            onkeydown="handleInputKey(event)"
            oninput="autoResize(this)"
            onpaste="handlePaste(event)"></textarea>

          <button class="input-btn" onclick="openVoice()" title="Voice chat">
            <svg viewBox="0 0 24 24"><path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm6-3c0 2.76-2.24 5-5 5h-2c-2.76 0-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z"/></svg>
          </button>
          <button class="input-btn send" id="sendBtn" onclick="sendMessage()" title="Send">
            <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
          </button>
          <button class="input-btn danger" id="stopBtn" onclick="stopRequest()" title="Stop" style="display:none">
            <svg viewBox="0 0 24 24"><path d="M6 6h12v12H6z"/></svg>
          </button>
        </div>
        <div class="input-hint"><kbd>Enter</kbd> send &nbsp;·&nbsp; <kbd>Shift</kbd>+<kbd>Enter</kbd> new line</div>
      </div>
    </div>
  </div>
</div>

<!-- VOICE MODAL -->
<div class="voice-overlay" id="voiceOverlay">
  <div class="voice-modal">
    <div class="voice-bubble-wrap">
      <div class="voice-ring"></div>
      <div class="voice-ring"></div>
      <div class="voice-ring"></div>
      <div class="voice-bubble" id="voiceBubble"></div>
    </div>
    <div class="voice-status" id="voiceStatus">Ready</div>
    <button class="voice-close" onclick="closeVoice()">Close</button>
  </div>
</div>

<!-- PROJECT MODAL -->
<div class="modal-bg" id="projectModalBg" onclick="closeProjectModal(event)">
  <div class="modal" onclick="event.stopPropagation()">
    <h3>New project</h3>
    <input type="text" id="projectNameInput" placeholder="Project name…" maxlength="40">
    <div class="emoji-grid" id="emojiGrid"></div>
    <div class="modal-btns">
      <button onclick="closeProjectModal()">Cancel</button>
      <button class="btn-primary" onclick="saveProject()">Create</button>
    </div>
  </div>
</div>

<!-- LOGS PANEL -->
<div id="logsPanel"></div>

<script>
// ═══════════════════════════════════════════════
//  STATE
// ═══════════════════════════════════════════════
const SESSION_ID = 'odin_' + Math.random().toString(36).slice(2, 10);
let currentModel = localStorage.getItem('odin_model') || 'auto';
let activeAbort = null;
let voiceMode = false;
let ttsVolume = parseFloat(localStorage.getItem('odin_vol') || '0.9');
let pendingAttachments = [];
let isNearBottom = true;

// Server-backed caches (no message bodies — those are fetched per-chat)
// projects: [{id, name, icon, sort_order}]
// chats:    [{id, title, project_id, created_at, updated_at}]
let projects = [];
let chats = [];
let activeChatId = null;
let activeMessages = []; // Messages for the currently-open chat, fetched from server
// Client-only UI state: which project groups are expanded
let projectOpen = JSON.parse(localStorage.getItem('odin_proj_open') || '{}');

// MODELS populated dynamically from /api/models on init.
// Edit models.yaml — no JS changes needed to update the dropdown.
let MODELS = [
  { id: 'auto', label: 'Auto', badge: 'smart' },
];

async function loadModelList() {
  try {
    const d = await api('/api/models');
    const serverModels = (d.models || []).filter(m => m.available !== false);
    MODELS = [{ id: 'auto', label: 'Auto', badge: 'smart' }].concat(
      serverModels
        .filter(m => m.id !== 'auto')
        .map(m => ({
          id: m.id,
          label: m.label || m.id,
          badge: (m.role_hints && m.role_hints[0]) || 'model',
          available: m.available !== false,
        }))
    );
    buildModelDropdown();
    const saved = MODELS.find(m => m.id === currentModel);
    if (saved) document.getElementById('modelLabel').textContent = saved.label;
    else document.getElementById('modelLabel').textContent = 'Auto';
  } catch (e) {
    console.warn('Could not load model list from server:', e);
    // Fall back to just Auto
    buildModelDropdown();
  }
}

const EMOJIS = ['💬','⚡','🔬','🏗️','🤖','🧠','🛡️','🌐','📊','🎯','🔧','📁'];

// ═══════════════════════════════════════════════
//  INIT
// ═══════════════════════════════════════════════
async function init() {
  // Load models dynamically from server, then build dropdown
  await loadModelList();

  setupScrollWatcher();
  setVolume(ttsVolume);
  document.getElementById('volSlider').value = ttsVolume;
  pingStatus();
  setInterval(pingStatus, 30000);

  try {
    await Promise.all([loadProjects(), loadChats()]);
  } catch (e) {
    console.error('Initial load failed:', e);
    toast('Could not reach Odin backend');
  }
  buildProjectSection();

  if (chats.length === 0) {
    await createChat('general');
  } else {
    // Open the most recently updated chat
    activeChatId = chats[0].id; // list_chats returns DESC by updated_at
    await loadActiveMessages();
    renderMessages();
    buildProjectSection();
  }
}

// ═══════════════════════════════════════════════
//  API LAYER
// ═══════════════════════════════════════════════
async function api(path, opts) {
  const resp = await fetch(path, Object.assign({
    headers: { 'Content-Type': 'application/json' }
  }, opts || {}));
  if (!resp.ok) {
    const body = await resp.text().catch(() => '');
    throw new Error('HTTP ' + resp.status + (body ? ': ' + body.slice(0, 120) : ''));
  }
  return resp.json();
}

async function loadProjects() {
  const d = await api('/api/projects');
  projects = d.projects || [];
}
async function loadChats() {
  const d = await api('/api/chats');
  chats = d.chats || [];
}
async function loadActiveMessages() {
  if (!activeChatId) { activeMessages = []; return; }
  try {
    const d = await api('/api/chats/' + encodeURIComponent(activeChatId));
    activeMessages = d.messages || [];
  } catch (e) {
    console.error('Failed to load messages for', activeChatId, e);
    activeMessages = [];
  }
}

function getActiveChat() {
  return chats.find(c => c.id === activeChatId) || null;
}

// ═══════════════════════════════════════════════
//  CHAT MANAGEMENT
// ═══════════════════════════════════════════════
async function createChat(projectId, title) {
  try {
    const d = await api('/api/chats', {
      method: 'POST',
      body: JSON.stringify({
        project_id: projectId || 'general',
        title: title || 'New chat'
      })
    });
    await loadChats();
    activeChatId = d.chat_id;
    activeMessages = [];
    // Expand the project so the new chat is visible
    projectOpen[projectId || 'general'] = true;
    saveUiState();
    buildProjectSection();
    renderMessages();
    document.getElementById('chatInput').focus();
  } catch (e) {
    toast('Could not create chat');
    console.error(e);
  }
}

async function newChat() {
  await createChat('general');
  showWelcome(true);
}

async function switchChat(chatId) {
  if (chatId === activeChatId) return;
  activeChatId = chatId;
  activeMessages = [];
  renderMessages(); // clear UI immediately while we fetch
  buildProjectSection();
  await loadActiveMessages();
  renderMessages();
  const chat = getActiveChat();
  showWelcome(!chat || activeMessages.length === 0);
  scrollToBottom(false);
}

async function deleteChat(chatId, e) {
  if (e) e.stopPropagation();
  if (!confirm('Delete this chat? This cannot be undone.')) return;
  try {
    await api('/api/chats/' + encodeURIComponent(chatId), { method: 'DELETE' });
    await loadChats();
    if (activeChatId === chatId) {
      if (chats.length > 0) {
        activeChatId = chats[0].id;
        await loadActiveMessages();
      } else {
        await createChat('general');
        return;
      }
    }
    buildProjectSection();
    renderMessages();
  } catch (err) {
    toast('Could not delete chat');
    console.error(err);
  }
}

async function moveChatToProject(chatId, projectId) {
  try {
    await api('/api/chats/' + encodeURIComponent(chatId) + '/move', {
      method: 'POST',
      body: JSON.stringify({ project_id: projectId })
    });
    await loadChats();
    projectOpen[projectId] = true;
    saveUiState();
    buildProjectSection();
  } catch (e) {
    toast('Could not move chat');
    console.error(e);
  }
}

// ═══════════════════════════════════════════════
//  PROJECT MANAGEMENT
// ═══════════════════════════════════════════════
let selectedEmoji = '💬';

function saveUiState() {
  localStorage.setItem('odin_proj_open', JSON.stringify(projectOpen));
}

function buildProjectSection() {
  const el = document.getElementById('projectSection');
  el.innerHTML = '';

  projects.forEach(proj => {
    const projChats = chats.filter(c => c.project_id === proj.id);
    // Default to open if not explicitly tracked
    const isOpen = projectOpen[proj.id] !== false;

    const group = document.createElement('div');
    group.className = 'project-group';

    const header = document.createElement('div');
    header.className = 'project-header' + (isOpen ? ' open' : '');
    header.innerHTML = `
      <span class="project-icon">${proj.icon}</span>
      <span class="project-name">${escHtml(proj.name)}</span>
      <button class="project-add-btn" title="New chat in ${escHtml(proj.name)}">+</button>
      ${proj.id !== 'general' ? '<button class="project-del-btn" title="Delete project">✕</button>' : ''}
      <span class="chevron">▶</span>
    `;
    header.querySelector('.project-add-btn').onclick = (ev) => {
      ev.stopPropagation();
      createChat(proj.id);
    };
    const delBtn = header.querySelector('.project-del-btn');
    if (delBtn) {
      delBtn.onclick = (ev) => {
        ev.stopPropagation();
        deleteProject(proj.id, proj.name);
      };
    }

    const chatList = document.createElement('div');
    chatList.className = 'project-chats' + (isOpen ? ' open' : '');

    header.onclick = () => {
      const newOpen = !(projectOpen[proj.id] !== false);
      projectOpen[proj.id] = newOpen;
      saveUiState();
      header.classList.toggle('open', newOpen);
      chatList.classList.toggle('open', newOpen);
    };

    if (projChats.length === 0) {
      const empty = document.createElement('div');
      empty.style.cssText = 'padding:6px 12px 6px 24px;font-size:11px;color:var(--text-dim);font-style:italic';
      empty.textContent = 'No chats yet';
      chatList.appendChild(empty);
    } else {
      projChats.forEach(chat => {
        const item = document.createElement('div');
        item.className = 'chat-item' + (chat.id === activeChatId ? ' active' : '');
        item.innerHTML = `
          <span class="chat-title">${escHtml(chat.title)}</span>
          <button class="chat-del" title="Delete">✕</button>
        `;
        item.onclick = () => switchChat(chat.id);
        item.querySelector('.chat-del').onclick = (ev) => { ev.stopPropagation(); deleteChat(chat.id, ev); };
        chatList.appendChild(item);
      });
    }

    group.appendChild(header);
    group.appendChild(chatList);
    el.appendChild(group);
  });

  const addProj = document.createElement('button');
  addProj.className = 'add-project-btn';
  addProj.textContent = '+ New project';
  addProj.onclick = openProjectModal;
  el.appendChild(addProj);
}

function openProjectModal() {
  const grid = document.getElementById('emojiGrid');
  grid.innerHTML = '';
  selectedEmoji = EMOJIS[0];
  EMOJIS.forEach(em => {
    const el = document.createElement('div');
    el.className = 'emoji-opt' + (em === selectedEmoji ? ' selected' : '');
    el.textContent = em;
    el.onclick = () => {
      grid.querySelectorAll('.emoji-opt').forEach(e => e.classList.remove('selected'));
      el.classList.add('selected');
      selectedEmoji = em;
    };
    grid.appendChild(el);
  });
  document.getElementById('projectNameInput').value = '';
  document.getElementById('projectModalBg').classList.add('open');
  setTimeout(() => document.getElementById('projectNameInput').focus(), 50);
}
function closeProjectModal(e) {
  if (!e || e.target === document.getElementById('projectModalBg'))
    document.getElementById('projectModalBg').classList.remove('open');
}
async function saveProject() {
  const name = document.getElementById('projectNameInput').value.trim();
  if (!name) return;
  try {
    const d = await api('/api/projects', {
      method: 'POST',
      body: JSON.stringify({ name, icon: selectedEmoji })
    });
    await loadProjects();
    projectOpen[d.id] = true;
    saveUiState();
    buildProjectSection();
    closeProjectModal();
  } catch (e) {
    toast('Could not create project');
    console.error(e);
  }
}

async function deleteProject(projectId, projectName) {
  if (!confirm('Delete project "' + projectName + '"?\nChats inside will move to General.')) return;
  try {
    await api('/api/projects/' + encodeURIComponent(projectId), { method: 'DELETE' });
    await Promise.all([loadProjects(), loadChats()]);
    buildProjectSection();
  } catch (e) {
    toast('Could not delete project');
    console.error(e);
  }
}

// ═══════════════════════════════════════════════
//  TOAST (simple)
// ═══════════════════════════════════════════════
function toast(msg) {
  let t = document.getElementById('odinToast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'odinToast';
    t.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--bg-elev);border:1px solid var(--line-strong);color:var(--text);padding:10px 18px;border-radius:20px;font-size:13px;box-shadow:0 8px 24px rgba(0,0,0,0.5);z-index:500;opacity:0;transition:opacity .2s';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.style.opacity = '1';
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.style.opacity = '0'; }, 2600);
}

// ═══════════════════════════════════════════════
//  RENDER MESSAGES
// ═══════════════════════════════════════════════
function renderMessages() {
  const container = document.getElementById('messages');
  container.innerHTML = '';
  if (!activeChatId || activeMessages.length === 0) {
    showWelcome(true);
    return;
  }
  showWelcome(false);
  activeMessages.forEach(msg => appendMessageEl(msg.role, msg.content, msg.toolBadges, msg.ts));
  scrollToBottom(false);
}

function showWelcome(show) {
  document.getElementById('welcome').style.display = show ? 'flex' : 'none';
}

function appendMessageEl(role, content, toolBadges, ts) {
  showWelcome(false);
  const container = document.getElementById('messages');

  const row = document.createElement('div');
  row.className = 'msg-row ' + role;

  const col = document.createElement('div');
  col.className = 'msg-col';

  // Meta: who + timestamp
  const meta = document.createElement('div');
  meta.className = 'msg-meta';
  meta.innerHTML = `<span class="who">${role === 'user' ? 'You' : 'Odin'}</span><span class="dot">·</span><span>${formatTs(ts || Date.now())}</span>`;
  col.appendChild(meta);

  // Bubble
  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';
  if (role === 'ai') bubble.innerHTML = renderMarkdown(content);
  else bubble.textContent = content;
  col.appendChild(bubble);

  // Tool badges
  if (toolBadges && toolBadges.length > 0) {
    const badgeRow = document.createElement('div');
    badgeRow.className = 'tool-badges';
    toolBadges.forEach(t => {
      const b = document.createElement('span');
      b.className = 'tool-badge';
      b.textContent = '⚡ ' + t;
      badgeRow.appendChild(b);
    });
    col.appendChild(badgeRow);
  }

  // Actions
  const actions = document.createElement('div');
  actions.className = 'msg-actions';

  const copyBtn = document.createElement('button');
  copyBtn.className = 'msg-action-btn';
  copyBtn.innerHTML = `<svg viewBox="0 0 24 24"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>Copy`;
  copyBtn.onclick = () => {
    navigator.clipboard.writeText(content).then(() => {
      copyBtn.innerHTML = `<svg viewBox="0 0 24 24"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>Copied`;
      setTimeout(() => { copyBtn.innerHTML = `<svg viewBox="0 0 24 24"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>Copy`; }, 1800);
    });
  };
  actions.appendChild(copyBtn);

  if (role === 'ai' && content.length > 80) {
    const dlBtn = document.createElement('button');
    dlBtn.className = 'msg-action-btn';
    dlBtn.innerHTML = `<svg viewBox="0 0 24 24"><path d="M5 20h14v-2H5v2zm0-10h4v6h6v-6h4l-7-7-7 7z"/></svg>Download`;
    dlBtn.onclick = () => downloadText(content, 'odin_response_' + Date.now() + '.md');
    actions.appendChild(dlBtn);
  }
  col.appendChild(actions);

  row.appendChild(col);
  container.appendChild(row);

  if (isNearBottom) scrollToBottom(false);
  return bubble;
}

// ═══════════════════════════════════════════════
//  THINKING ROW
// ═══════════════════════════════════════════════
function addThinkingRow() {
  showWelcome(false);
  const container = document.getElementById('messages');
  const row = document.createElement('div');
  row.className = 'msg-row ai';
  row.id = 'thinkingRow';

  const col = document.createElement('div');
  col.className = 'msg-col';

  const meta = document.createElement('div');
  meta.className = 'msg-meta';
  meta.innerHTML = `<span class="who">Odin</span><span class="dot">·</span><span>${formatTs(Date.now())}</span>`;
  col.appendChild(meta);

  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';
  bubble.innerHTML = `<div class="thinking"><span></span><span></span><span></span></div>`;
  col.appendChild(bubble);

  row.appendChild(col);
  container.appendChild(row);
  scrollToBottom(false);
  return bubble;
}
function removeThinkingRow() {
  const el = document.getElementById('thinkingRow');
  if (el) el.remove();
}

// ═══════════════════════════════════════════════
//  SEND MESSAGE
// ═══════════════════════════════════════════════
async function sendMessage() {
  const input = document.getElementById('chatInput');
  const text = input.value.trim();
  if (!text && pendingAttachments.length === 0) return;

  input.value = '';
  input.style.height = 'auto';
  input.rows = 1;

  // Ensure we have an active chat on the server before sending
  if (!activeChatId || !getActiveChat()) {
    await createChat('general');
  }

  // Optimistically render + cache the user message
  const userTs = Date.now();
  activeMessages.push({ role: 'user', content: text, toolBadges: [], ts: userTs });
  appendMessageEl('user', text, [], userTs);

  addThinkingRow();
  document.getElementById('sendBtn').style.display = 'none';
  document.getElementById('stopBtn').style.display = 'flex';

  const t0 = performance.now();
  activeAbort = new AbortController();

  const attachData = pendingAttachments.filter(a => a);
  pendingAttachments = [];
  document.getElementById('attachmentsRow').innerHTML = '';

  // Capture which chat this was sent from, in case the user switches mid-flight
  const sendingChatId = activeChatId;
  let fullText = '';
  let toolBadges = [];
  let succeeded = false;

  try {
    const payload = {
      message: text,
      chat_id: sendingChatId,
      session_id: SESSION_ID,
      model: currentModel,
      attachments: attachData
    };

    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: activeAbort.signal
    });

    if (!resp.ok) throw new Error('HTTP ' + resp.status);

    const ctype = resp.headers.get('content-type') || '';

    if (ctype.includes('text/event-stream')) {
      // Streaming path (if backend ever adds SSE)
      removeThinkingRow();
      const streamBubble = appendMessageEl('ai', '', [], Date.now());
      const streamRow = streamBubble.closest('.msg-row');
      const streamActions = streamRow.querySelector('.msg-actions');
      if (streamActions) streamActions.remove();

      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        const chunk = dec.decode(value, { stream: true });
        const lines = chunk.split('\n');
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          try {
            const d = JSON.parse(line.slice(6));
            if (d.token) {
              fullText += d.token;
              streamBubble.innerHTML = renderMarkdown(fullText);
              if (isNearBottom) scrollToBottom(false);
            }
            if (d.tool) toolBadges.push(d.tool);
            if (d.error) { fullText = '[Error] ' + d.error; streamBubble.textContent = fullText; }
          } catch(_) {}
        }
      }
      // Re-attach badges + actions on the streamed row
      attachBadgesAndActions(streamRow, fullText, toolBadges);
    } else {
      // JSON path (current backend)
      const d = await resp.json();
      fullText = d.response || d.answer || d.text || JSON.stringify(d);
      toolBadges = (d.tools || []).map(t =>
        typeof t === 'string' ? t : (t.name || t.tool || JSON.stringify(t))
      );
      if (d.latency_ms) document.getElementById('latencyBadge').textContent = d.latency_ms + 'ms';

      removeThinkingRow();
      // Only render into the DOM if the user is still looking at this chat
      if (activeChatId === sendingChatId) {
        appendMessageEl('ai', fullText, toolBadges, Date.now());
      }
    }

    succeeded = true;
    const latency = Math.round(performance.now() - t0);
    document.getElementById('latencyBadge').textContent = latency + 'ms';

    // Cache the assistant message locally so switching away and back
    // doesn't require a round-trip
    if (activeChatId === sendingChatId) {
      activeMessages.push({ role: 'ai', content: fullText, toolBadges, ts: Date.now() });
    }

    // Refresh the chat list so the auto-generated title and updated_at
    // ordering appear in the sidebar
    try {
      await loadChats();
      buildProjectSection();
    } catch (_) {}

    if (voiceMode && fullText) speakText(fullText);

  } catch (err) {
    removeThinkingRow();
    if (err.name !== 'AbortError') {
      if (activeChatId === sendingChatId) {
        appendMessageEl('ai', '⚠️ Connection error: ' + err.message, [], Date.now());
      }
      // Roll the optimistic user message out of the local cache on hard failure
      // (the server never received it, so it's not persisted either)
      if (!succeeded) {
        const idx = activeMessages.findIndex(m => m.ts === userTs && m.role === 'user');
        if (idx >= 0) activeMessages.splice(idx, 1);
      }
    }
  }

  document.getElementById('sendBtn').style.display = 'flex';
  document.getElementById('stopBtn').style.display = 'none';
  activeAbort = null;
}

// Helper: attach tool badges + copy/download buttons to a streamed row
function attachBadgesAndActions(streamRow, fullText, toolBadges) {
  const streamCol = streamRow.querySelector('.msg-col');
  if (toolBadges.length > 0) {
    const badgeRow = document.createElement('div');
    badgeRow.className = 'tool-badges';
    toolBadges.forEach(t => {
      const b = document.createElement('span');
      b.className = 'tool-badge';
      b.textContent = '⚡ ' + t;
      badgeRow.appendChild(b);
    });
    streamCol.appendChild(badgeRow);
  }

  const actions = document.createElement('div');
  actions.className = 'msg-actions';
  const copyBtn = document.createElement('button');
  copyBtn.className = 'msg-action-btn';
  copyBtn.innerHTML = `<svg viewBox="0 0 24 24"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>Copy`;
  copyBtn.onclick = () => {
    navigator.clipboard.writeText(fullText);
    copyBtn.innerHTML = `<svg viewBox="0 0 24 24"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>Copied`;
    setTimeout(() => { copyBtn.innerHTML = `<svg viewBox="0 0 24 24"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>Copy`; }, 1800);
  };
  actions.appendChild(copyBtn);
  if (fullText.length > 80) {
    const dlBtn = document.createElement('button');
    dlBtn.className = 'msg-action-btn';
    dlBtn.innerHTML = `<svg viewBox="0 0 24 24"><path d="M5 20h14v-2H5v2zm0-10h4v6h6v-6h4l-7-7-7 7z"/></svg>Download`;
    dlBtn.onclick = () => downloadText(fullText, 'odin_response_' + Date.now() + '.md');
    actions.appendChild(dlBtn);
  }
  streamCol.appendChild(actions);
}

function stopRequest() {
  if (activeAbort) { activeAbort.abort(); activeAbort = null; }
  removeThinkingRow();
  document.getElementById('sendBtn').style.display = 'flex';
  document.getElementById('stopBtn').style.display = 'none';
}

// ═══════════════════════════════════════════════
//  INPUT HANDLING
// ═══════════════════════════════════════════════
function handleInputKey(e) {
  // Shift+Enter = new line (default textarea behavior, just don't preventDefault)
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
  // else: let the textarea wrap naturally on Shift+Enter
}
function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 200) + 'px';
}
function useSuggestion(el) {
  const input = document.getElementById('chatInput');
  input.value = el.textContent;
  autoResize(input);
  sendMessage();
}

// ═══════════════════════════════════════════════
//  SCROLL
// ═══════════════════════════════════════════════
function setupScrollWatcher() {
  const wrap = document.getElementById('chatWrap');
  wrap.addEventListener('scroll', () => {
    const dist = wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight;
    isNearBottom = dist < 80;
    document.getElementById('scrollBtn').classList.toggle('visible', dist > 200);
  });
}
function scrollToBottom(smooth) {
  const wrap = document.getElementById('chatWrap');
  wrap.scrollTo({ top: wrap.scrollHeight, behavior: smooth ? 'smooth' : 'instant' });
  isNearBottom = true;
  document.getElementById('scrollBtn').classList.remove('visible');
}

// ═══════════════════════════════════════════════
//  SIDEBAR / MODEL DROPDOWN
// ═══════════════════════════════════════════════
function toggleSidebar() { document.getElementById('sidebar').classList.toggle('collapsed'); }

function buildModelDropdown() {
  const dd = document.getElementById('modelDropdown');
  dd.innerHTML = '';
  MODELS.forEach(m => {
    const opt = document.createElement('div');
    opt.className = 'model-opt' + (m.id === currentModel ? ' active' : '');
    opt.innerHTML = `<span>${m.label}</span><span class="model-badge">${m.badge}</span>`;
    opt.onclick = () => {
      currentModel = m.id;
      localStorage.setItem('odin_model', currentModel);
      document.getElementById('modelLabel').textContent = m.label;
      dd.querySelectorAll('.model-opt').forEach(o => o.classList.remove('active'));
      opt.classList.add('active');
      dd.classList.remove('open');
    };
    dd.appendChild(opt);
  });
}
function toggleModelDropdown(e) {
  e.stopPropagation();
  document.getElementById('modelDropdown').classList.toggle('open');
}
document.addEventListener('click', () => document.getElementById('modelDropdown').classList.remove('open'));

// ═══════════════════════════════════════════════
//  VOLUME
// ═══════════════════════════════════════════════
function setVolume(v) {
  ttsVolume = parseFloat(v);
  localStorage.setItem('odin_vol', String(ttsVolume));
  const svg = document.getElementById('volIcon');
  if (!svg) return;
  if (ttsVolume === 0)
    svg.innerHTML = '<path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z"/>';
  else if (ttsVolume < 0.5)
    svg.innerHTML = '<path d="M18.5 12c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM5 9v6h4l5 5V4L9 9H5z"/>';
  else
    svg.innerHTML = '<path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z"/>';
}

// ═══════════════════════════════════════════════
//  TTS
// ═══════════════════════════════════════════════
async function speakText(text) {
  const plain = text.replace(/```[\s\S]*?```/g, 'code block').replace(/[#*_`>~]/g, '').trim();
  try {
    const resp = await fetch('/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: plain })
    });
    if (!resp.ok) throw new Error('TTS failed');
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.volume = ttsVolume;
    audio.play();
    audio.onended = () => URL.revokeObjectURL(url);
  } catch (e) {
    const utt = new SpeechSynthesisUtterance(plain.slice(0, 400));
    utt.volume = ttsVolume;
    speechSynthesis.speak(utt);
  }
}

// ═══════════════════════════════════════════════
//  VOICE MODAL
// ═══════════════════════════════════════════════
let recognition = null;
function openVoice() {
  voiceMode = true;
  document.getElementById('voiceOverlay').classList.add('open');
  startListening();
}
function closeVoice() {
  voiceMode = false;
  document.getElementById('voiceOverlay').classList.remove('open');
  if (recognition) recognition.stop();
  document.getElementById('voiceStatus').textContent = 'Ready';
  document.getElementById('voiceBubble').classList.remove('active');
}
function startListening() {
  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRec) {
    document.getElementById('voiceStatus').textContent = 'Not supported';
    return;
  }
  recognition = new SpeechRec();
  recognition.continuous = false;
  recognition.interimResults = false;
  recognition.lang = 'en-US';

  recognition.onstart = () => {
    document.getElementById('voiceStatus').textContent = 'Listening…';
    document.getElementById('voiceBubble').classList.add('active');
  };
  recognition.onresult = (e) => {
    const transcript = e.results[0][0].transcript;
    document.getElementById('voiceStatus').textContent = transcript;
    document.getElementById('voiceBubble').classList.remove('active');
    document.getElementById('chatInput').value = transcript;
    autoResize(document.getElementById('chatInput'));
    sendMessage();
  };
  recognition.onerror = () => {
    document.getElementById('voiceStatus').textContent = 'Error — try again';
    document.getElementById('voiceBubble').classList.remove('active');
  };
  recognition.onend = () => {
    document.getElementById('voiceBubble').classList.remove('active');
    if (voiceMode) setTimeout(startListening, 800);
  };
  recognition.start();
}

// ═══════════════════════════════════════════════
//  FILE ATTACHMENTS
// ═══════════════════════════════════════════════
function handleFiles(e) { Array.from(e.target.files).forEach(readFile); e.target.value = ''; }
function handlePaste(e) {
  const items = e.clipboardData?.items;
  if (!items) return;
  for (const item of items) {
    if (item.kind === 'file') { e.preventDefault(); readFile(item.getAsFile()); }
  }
}
function readFile(file) {
  const reader = new FileReader();
  reader.onload = ev => {
    pendingAttachments.push({ name: file.name, type: file.type, data: ev.target.result });
    addAttachChip(file.name, pendingAttachments.length - 1);
  };
  if (file.type.startsWith('image/')) reader.readAsDataURL(file);
  else reader.readAsText(file);
}
function addAttachChip(name, idx) {
  const row = document.getElementById('attachmentsRow');
  const chip = document.createElement('div');
  chip.className = 'attach-chip';
  chip.id = 'chip_' + idx;
  chip.innerHTML = `📎 ${escHtml(name)} <button onclick="removeAttachment(${idx})">✕</button>`;
  row.appendChild(chip);
}
function removeAttachment(idx) {
  pendingAttachments[idx] = null;
  const chip = document.getElementById('chip_' + idx);
  if (chip) chip.remove();
}

// ═══════════════════════════════════════════════
//  LOGS + STATUS
// ═══════════════════════════════════════════════
async function toggleLogs() {
  const panel = document.getElementById('logsPanel');
  if (panel.classList.toggle('open')) {
    try {
      const r = await fetch('/api/logs?n=80&session_id=' + SESSION_ID);
      const d = await r.json();
      panel.innerHTML = (d.events || []).map(ev =>
        `<div style="margin-bottom:4px"><span style="color:var(--accent)">[${ev.type}]</span> ${escHtml(JSON.stringify(ev).slice(0, 120))}</div>`
      ).join('') || '<div style="color:var(--text-dim)">No events</div>';
    } catch { panel.innerHTML = 'Failed to load logs'; }
  }
}
async function pingStatus() {
  try {
    const r = await fetch('/api/status');
    const dot = document.getElementById('statusDot');
    dot.classList.toggle('online', r.ok);
  } catch { document.getElementById('statusDot').classList.remove('online'); }
}

// ═══════════════════════════════════════════════
//  MARKDOWN
// ═══════════════════════════════════════════════
function renderMarkdown(text) {
  let html = escHtml(text);

  html = html.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    const ext = langExt(lang);
    const id = 'cb_' + Math.random().toString(36).slice(2, 8);
    return `<div class="code-block-wrap">
      <div class="code-block-header">
        <span class="code-lang">${lang || 'text'}</span>
        <button class="code-btn" onclick="copyCode('${id}')">
          <svg viewBox="0 0 24 24"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>Copy
        </button>
        <button class="code-btn" onclick="downloadCode('${id}', '${ext}')">
          <svg viewBox="0 0 24 24"><path d="M5 20h14v-2H5v2zm0-10h4v6h6v-6h4l-7-7-7 7z"/></svg>Download
        </button>
      </div>
      <pre id="${id}">${code}</pre>
    </div>`;
  });

  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
  html = html.replace(/_(.+?)_/g, '<em>$1</em>');
  html = html.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');
  html = html.replace(/^---$/gm, '<hr>');

  html = html.replace(/((?:^[*\-] .+\n?)+)/gm, (block) => {
    const items = block.trim().split('\n').map(l => `<li>${l.replace(/^[*\-] /, '')}</li>`).join('');
    return `<ul>${items}</ul>`;
  });
  html = html.replace(/((?:^\d+\. .+\n?)+)/gm, (block) => {
    const items = block.trim().split('\n').map(l => `<li>${l.replace(/^\d+\. /, '')}</li>`).join('');
    return `<ol>${items}</ol>`;
  });

  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  html = html.replace(/\n\n+/g, '</p><p>');
  html = '<p>' + html + '</p>';
  html = html.replace(/<p>\s*<\/p>/g, '');
  html = html.replace(/(?<!>)\n(?!<)/g, '<br>');
  return html;
}

function langExt(lang) {
  const map = { python:'py', javascript:'js', typescript:'ts', bash:'sh', shell:'sh', json:'json', yaml:'yaml', html:'html', css:'css', sql:'sql', rust:'rs', go:'go', markdown:'md' };
  return map[(lang || '').toLowerCase()] || lang || 'txt';
}
function copyCode(id) {
  const el = document.getElementById(id);
  if (el) navigator.clipboard.writeText(el.textContent);
}
function downloadCode(id, ext) {
  const el = document.getElementById(id);
  if (!el) return;
  downloadText(el.textContent, 'odin_code_' + Date.now() + '.' + ext);
}

// ═══════════════════════════════════════════════
//  UTILS
// ═══════════════════════════════════════════════
function escHtml(str) {
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function formatTs(ms) {
  const d = new Date(ms);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (sameDay) return time;
  const date = d.toLocaleDateString([], { month: 'short', day: 'numeric', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric' });
  return `${date} · ${time}`;
}
function downloadText(text, filename) {
  const blob = new Blob([text], { type: 'text/plain' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

// BOOT
init();
</script>
</body>
</html>
"""



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
    global client, vault

    print("=" * 60)
    print("  ⚔️  ODIN — BeanLab AI Assistant")
    print("=" * 60)
    print()

    client = OpenAI(base_url=f"{OLLAMA_HOST}/v1", api_key="ollama")
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
    warm_targets = [tag for tag in registry.prewarm_targets() if tag in available]
    missing_warm = [tag for tag in registry.prewarm_targets() if tag not in available]
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
