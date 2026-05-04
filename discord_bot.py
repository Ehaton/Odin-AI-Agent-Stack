"""
discord_bot.py — Odin DM-only Discord bot
==========================================

Listens for direct messages sent to the bot. Only responds to messages
from DISCORD_OWNER_ID. All other DMs and every server message are silently
ignored — the bot never replies inside a server channel.

Flow:
  Discord DM → on_message() → POST /api/chat → format response → reply in DM

Setup:
  1. discord.com/developers/applications → New Application → Bot
  2. Enable: Message Content Intent, Direct Messages
  3. OAuth2 → URL Generator → scope: bot → permission: Send Messages
  4. Copy token → DISCORD_BOT_TOKEN in .env
  5. Copy your user ID (Settings → Advanced → Developer Mode → right-click name)
     → DISCORD_OWNER_ID in .env
  6. pip install discord.py aiohttp
  7. python discord_bot.py

.env additions required:
  DISCORD_BOT_TOKEN=your-bot-token
  DISCORD_OWNER_ID=your-numeric-user-id
  ODIN_URL=https://ai-stack-420.tail7a9f9b.ts.net:5050
  ODIN_USER=cfiaschetti
  ODIN_PASS=your-odin-password
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import textwrap
from typing import Optional

import aiohttp
import discord
from dotenv import load_dotenv

# ─── Config ──────────────────────────────────────────────────────────────────

load_dotenv()

DISCORD_BOT_TOKEN: str  = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_OWNER_ID:  int  = int(os.environ["DISCORD_OWNER_ID"])
ODIN_URL:          str  = os.environ.get("ODIN_URL", "https://localhost:5050").rstrip("/")
ODIN_USER:         str  = os.environ.get("ODIN_USER", "")
ODIN_PASS:         str  = os.environ.get("ODIN_PASS", "")

# Model to use for Discord DMs. "auto" lets Odin's classifier decide.
# Override with "claude-sonnet" to always route DMs to Claude.
DISCORD_MODEL: str = os.environ.get("DISCORD_MODEL", "auto")

# Discord hard-limits messages to 2000 chars. We chunk at 1900 to leave room
# for the chunk counter suffix ("… [1/3]").
DISCORD_MAX_CHARS: int = 1900

# Chat ID prefix for DM sessions. Each user gets an isolated chat history.
CHAT_ID_PREFIX: str = "discord_dm_"

# SSL verify — False for self-signed Tailscale certs.
SSL_VERIFY: bool = False

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("odin_discord")

# ─── Discord client ───────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True   # required to read message text
intents.dm_messages     = True   # receive DM events

client = discord.Client(intents=intents)

# ─── Odin API ─────────────────────────────────────────────────────────────────

async def call_odin(
    session:  aiohttp.ClientSession,
    chat_id:  str,
    message:  str,
    model:    str = DISCORD_MODEL,
) -> dict:
    """POST a message to Odin's /api/chat endpoint.

    Returns the full response dict. Raises on HTTP error or network failure.
    Auth is HTTP Basic, same as the web UI. SSL verification is disabled
    because Odin uses a self-signed Tailscale cert.
    """
    auth = aiohttp.BasicAuth(ODIN_USER, ODIN_PASS) if ODIN_USER else None
    connector = aiohttp.TCPConnector(ssl=SSL_VERIFY)

    payload = {
        "message":  message,
        "chat_id":  chat_id,
        "model":    model,
        "project_id": "discord",   # auto-creates a Discord project in the sidebar
    }

    async with session.post(
        f"{ODIN_URL}/api/chat",
        json=payload,
        auth=auth,
        timeout=aiohttp.ClientTimeout(total=300),   # 5 min — long tool chains
        ssl=SSL_VERIFY,
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


# ─── Response formatting ──────────────────────────────────────────────────────

def format_tool_badges(tools: list[dict]) -> str:
    """Return a compact string of tool badges, e.g. '`run_ssh` `vault_write`'."""
    if not tools:
        return ""
    names = []
    for t in tools:
        name = t.get("tool") or t.get("name") or ""
        if name and name not in names:
            names.append(name)
    return " ".join(f"`{n}`" for n in names) if names else ""


def chunk_message(text: str, max_len: int = DISCORD_MAX_CHARS) -> list[str]:
    """Split a long response into Discord-safe chunks.

    Splits on newlines where possible to avoid cutting mid-sentence.
    Each chunk except the last gets a [n/total] suffix.
    """
    if len(text) <= max_len:
        return [text]

    # Try to split on paragraph breaks first, then line breaks, then chars
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        # Find the last newline before the limit
        cut = remaining.rfind("\n", 0, max_len)
        if cut == -1:
            # No newline — cut on last space
            cut = remaining.rfind(" ", 0, max_len)
        if cut == -1:
            # No space either — hard cut
            cut = max_len
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    if remaining:
        chunks.append(remaining)

    # Add chunk counter if more than one chunk
    if len(chunks) > 1:
        total = len(chunks)
        chunks = [f"{c}\n*[{i+1}/{total}]*" for i, c in enumerate(chunks)]

    return chunks


def strip_markdown_for_code(text: str) -> str:
    """Wrap responses that contain code blocks so Discord renders them."""
    # Discord renders ```lang\n...\n``` natively — Odin outputs the same format.
    # Nothing needs to change; just pass through.
    return text


def build_reply(response: str, tools: list[dict]) -> str:
    """Combine the response text and tool badges into the final reply string."""
    parts = []

    if response:
        parts.append(strip_markdown_for_code(response))

    badges = format_tool_badges(tools)
    if badges:
        parts.append(f"\n-# Tools used: {badges}")

    return "\n".join(parts).strip()


# ─── Long-response handling ───────────────────────────────────────────────────

async def send_long_response(
    channel: discord.DMChannel,
    text:    str,
    tools:   list[dict],
) -> None:
    """Send a response, chunking if necessary and attaching as file if very long."""
    badges  = format_tool_badges(tools)
    badge_s = f"\n-# Tools: {badges}" if badges else ""

    # Under 4K total: chunk into Discord messages
    if len(text) <= 4000:
        chunks = chunk_message(text)
        for i, chunk in enumerate(chunks):
            suffix = badge_s if i == len(chunks) - 1 else ""
            await channel.send(chunk + suffix)
        return

    # Over 4K: send as a .md file attachment so nothing gets truncated
    log.info("Response too long (%d chars), sending as file attachment", len(text))
    file_content = text.encode("utf-8")
    discord_file = discord.File(
        io.BytesIO(file_content),
        filename="odin_response.md",
    )
    summary = textwrap.shorten(text, width=200, placeholder="…")
    await channel.send(
        f"Response is too long to display inline. Here it is as a file:{badge_s}\n> {summary}",
        file=discord_file,
    )


# ─── Discord events ───────────────────────────────────────────────────────────

@client.event
async def on_ready() -> None:
    log.info("Odin Discord bot ready — logged in as %s (ID: %s)", client.user, client.user.id)
    log.info("Accepting DMs only from user ID: %d", DISCORD_OWNER_ID)
    log.info("Odin endpoint: %s", ODIN_URL)
    log.info("Default model: %s", DISCORD_MODEL)


@client.event
async def on_message(message: discord.Message) -> None:
    """Main message handler.

    Security model:
    - Only process messages in DM channels (isinstance DMChannel).
    - Only process messages from DISCORD_OWNER_ID.
    - Bot's own messages are ignored (message.author == client.user).
    - Server/guild messages are silently dropped — no reply, no log.

    This means the bot can be in servers without responding to anything there.
    """
    # 1. Ignore the bot's own messages
    if message.author == client.user:
        return

    # 2. Only DMs — drop everything in guilds/servers silently
    if not isinstance(message.channel, discord.DMChannel):
        return

    # 3. Only the owner
    if message.author.id != DISCORD_OWNER_ID:
        log.warning(
            "Ignored DM from unauthorized user %s (ID: %d)",
            message.author.name, message.author.id,
        )
        return

    # 4. Non-empty message
    content = message.content.strip()
    if not content:
        return

    log.info("DM from %s: %s", message.author.name, content[:120])

    # 5. Show typing indicator while Odin thinks
    async with message.channel.typing():
        chat_id = f"{CHAT_ID_PREFIX}{DISCORD_OWNER_ID}"

        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                result = await call_odin(session, chat_id, content)

        except aiohttp.ClientResponseError as e:
            log.error("Odin API error: HTTP %d", e.status)
            if e.status == 401:
                await message.channel.send(
                    "Authentication failed — check `ODIN_USER`/`ODIN_PASS` in the bot's `.env`."
                )
            elif e.status == 503:
                await message.channel.send(
                    "Odin is unavailable. Check that `Odin.py` is running on ai-stack-420."
                )
            else:
                await message.channel.send(f"Odin API error: HTTP {e.status}")
            return

        except aiohttp.ClientConnectorError as e:
            log.error("Cannot reach Odin: %s", e)
            await message.channel.send(
                f"Cannot reach Odin at `{ODIN_URL}`. "
                "Check your Tailscale connection and that Odin.py is running."
            )
            return

        except asyncio.TimeoutError:
            log.error("Odin request timed out (>5 min)")
            await message.channel.send(
                "Odin took too long to respond (>5 minutes). "
                "The request may still be processing — check the web UI."
            )
            return

        except Exception as e:
            log.exception("Unexpected error calling Odin")
            await message.channel.send(f"Unexpected error: `{type(e).__name__}: {e}`")
            return

    # 6. Parse and send the response
    response_text = result.get("response") or result.get("answer") or ""
    tools         = result.get("tools") or []
    latency_ms    = result.get("latency_ms")

    # Guard against empty/raw JSON responses (same fix as in odin.html)
    if not response_text and tools:
        tool_count = len(tools)
        response_text = (
            f"Processing completed ({tool_count} tool{'s' if tool_count != 1 else ''} used) "
            "but no summary was generated. Ask me to summarize, or check the vault."
        )
    elif not response_text:
        response_text = "No response generated. Try rephrasing."

    if response_text.lstrip().startswith("{") and '"tool"' in response_text:
        response_text = (
            "Processing completed but no summary was generated. "
            "Check the vault or ask me to summarize."
        )

    # Log latency
    if latency_ms:
        log.info("Response ready (%d chars, %dms latency)", len(response_text), latency_ms)

    await send_long_response(message.channel, response_text, tools)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    missing = []
    if not os.environ.get("DISCORD_BOT_TOKEN"):
        missing.append("DISCORD_BOT_TOKEN")
    if not os.environ.get("DISCORD_OWNER_ID"):
        missing.append("DISCORD_OWNER_ID")
    if missing:
        print(f"ERROR: Missing required environment variables: {', '.join(missing)}")
        print("Add them to .env and restart.")
        sys.exit(1)

    log.info("Starting Odin Discord bot…")
    client.run(DISCORD_BOT_TOKEN, log_handler=None)
