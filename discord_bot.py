#!/usr/bin/env python3
"""
Odin Discord Bot
----------------
DM-only, owner-only gateway to the Odin agent stack.
Proxies messages to POST /api/chat and returns the response,
chunked at natural line breaks if >2000 chars, or as a .md
attachment if >4000 chars.

Requirements:
    pip install discord.py aiohttp python-dotenv

Environment variables (add to /opt/Odin/.env):
    DISCORD_BOT_TOKEN   — bot token from Discord Developer Portal
    DISCORD_OWNER_ID    — your numeric Discord user ID
    DISCORD_MODEL       — model to use (default: auto)
    ODIN_USER           — Basic auth username (same as web UI)
    ODIN_PASS           — Basic auth password (same as web UI)
    ODIN_URL            — base URL of Odin (default: https://localhost:5050)
"""

import os
import io
import asyncio
import logging
import aiohttp

import discord
from discord.ext import commands
from dotenv import load_dotenv

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

BOT_TOKEN    = os.getenv("DISCORD_BOT_TOKEN", "")
OWNER_ID     = int(os.getenv("DISCORD_OWNER_ID", "0"))
MODEL        = os.getenv("DISCORD_MODEL", "auto")
ODIN_USER    = os.getenv("ODIN_USER", "cfiaschetti")
ODIN_PASS    = os.getenv("ODIN_PASS", "")
ODIN_URL     = os.getenv("ODIN_URL", "https://localhost:5050").rstrip("/")

CHUNK_LIMIT  = 1900   # leave headroom under Discord's 2000-char limit
FILE_LIMIT   = 4000   # responses longer than this become a .md attachment
CHAT_ID      = "discord-dm"   # fixed chat bucket for all Discord DMs

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [discord] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("odin-discord")

# ── Bot setup ─────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages     = True

bot = discord.Client(intents=intents)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _chunk(text: str) -> list[str]:
    """
    Split text into chunks ≤ CHUNK_LIMIT characters, breaking at
    newlines where possible so code blocks and bullets stay intact.
    """
    if len(text) <= CHUNK_LIMIT:
        return [text]

    chunks = []
    while text:
        if len(text) <= CHUNK_LIMIT:
            chunks.append(text)
            break
        # prefer a newline split
        split = text.rfind("\n", 0, CHUNK_LIMIT)
        if split == -1:
            split = CHUNK_LIMIT
        chunks.append(text[:split])
        text = text[split:].lstrip("\n")
    return chunks


def _tool_badge_line(tools: list) -> str | None:
    if not tools:
        return None
    names = []
    for t in tools:
        if isinstance(t, dict):
            names.append(t.get("name") or t.get("tool") or str(t))
        else:
            names.append(str(t))
    return "`" + "  ·  ".join(names) + "`"


async def _call_odin(message_text: str) -> dict:
    """POST to /api/chat and return the JSON response dict."""
    auth = aiohttp.BasicAuth(ODIN_USER, ODIN_PASS)
    payload = {
        "message":  message_text,
        "chat_id":  CHAT_ID,
        "model":    MODEL,
    }
    # Skip TLS verification for the self-signed Tailscale cert when running
    # locally on the same VM. Set ODIN_URL to the Tailscale hostname for
    # external access (cert will be valid in that case).
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.post(
            f"{ODIN_URL}/api/chat",
            json=payload,
            auth=auth,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

# ── Event handlers ────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    log.info(f"Owner ID: {OWNER_ID} | Model: {MODEL} | Odin: {ODIN_URL}")


@bot.event
async def on_message(message: discord.Message):
    # Gate 1 — ignore own messages
    if message.author == bot.user:
        return

    # Gate 2 — DMs only
    if not isinstance(message.channel, discord.DMChannel):
        return

    # Gate 3 — owner only
    if message.author.id != OWNER_ID:
        log.warning(f"Rejected message from non-owner {message.author} ({message.author.id})")
        await message.channel.send("⛔ Unauthorized.")
        return

    user_text = message.content.strip()
    if not user_text:
        return

    log.info(f"DM received: {user_text[:80]}{'...' if len(user_text) > 80 else ''}")

    # Show typing indicator while Odin processes
    async with message.channel.typing():
        try:
            data = await _call_odin(user_text)
        except aiohttp.ClientResponseError as e:
            log.error(f"Odin API error: {e.status} {e.message}")
            await message.channel.send(f"❌ Odin returned HTTP {e.status}.")
            return
        except asyncio.TimeoutError:
            log.error("Odin API timed out")
            await message.channel.send("⏱️ Odin timed out. The model may still be processing.")
            return
        except Exception as e:
            log.error(f"Unexpected error: {e}")
            await message.channel.send(f"❌ Error: {e}")
            return

    response_text = data.get("response", "").strip()
    tools_used    = data.get("tools", [])
    latency_ms    = data.get("latency_ms", 0)

    if not response_text:
        await message.channel.send("⚠️ Odin returned an empty response.")
        return

    # Build tool badge line
    badge = _tool_badge_line(tools_used)
    footer = f"\n{badge}" if badge else ""
    footer += f"\n`{latency_ms}ms`" if latency_ms else ""

    full = response_text + footer

    # Long responses → .md file attachment
    if len(full) > FILE_LIMIT:
        log.info(f"Response too long ({len(full)} chars), sending as file")
        file_bytes = full.encode("utf-8")
        discord_file = discord.File(io.BytesIO(file_bytes), filename="odin_response.md")
        await message.channel.send(
            content=f"_Response too long — attached as markdown_ ({len(full)} chars)",
            file=discord_file,
        )
        return

    # Chunk and send
    chunks = _chunk(full)
    total  = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        prefix = f"**[{i}/{total}]**\n" if total > 1 else ""
        await message.channel.send(prefix + chunk)
        if total > 1 and i < total:
            await asyncio.sleep(0.4)   # brief pause between chunks

    log.info(f"Sent response: {len(full)} chars, {len(tools_used)} tools, {latency_ms}ms")

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is not set in .env")
    if not OWNER_ID:
        raise SystemExit("DISCORD_OWNER_ID is not set in .env")
    if not ODIN_PASS:
        raise SystemExit("ODIN_PASS is not set in .env")

    log.info("Starting Odin Discord bot...")
    bot.run(BOT_TOKEN, log_handler=None)
