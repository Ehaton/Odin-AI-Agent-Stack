#!/usr/bin/env python3
"""
embed_vault.py — Embed the Obsidian vault into ChromaDB for semantic search.

Walks /opt/Odin/obsidian_vault/, chunks each markdown file, embeds via
Ollama's nomic-embed-text, and stores in the "obsidian_vault" collection.

Run modes:
  --full       Wipe and rebuild the entire collection (first run, or reset)
  --update     Only re-embed files modified since last run (default)
  --dry-run    Show what would be embedded without writing

Usage:
  python embed_vault.py --full
  python embed_vault.py --update
  python embed_vault.py --update --vault-path /some/other/vault

Cron suggestion (run every 6 hours):
  0 */6 * * * /opt/Odin/venv/bin/python /opt/Odin/scripts/embed_vault.py --update
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Iterator

import requests
import chromadb
from chromadb.config import Settings


# ----- Configuration (override via env or CLI) -----

DEFAULT_VAULT = os.environ.get("OBSIDIAN_VAULT", "/opt/Odin/obsidian_vault")
DEFAULT_CHROMADB = os.environ.get("CHROMADB_URL", "http://localhost:8000")
DEFAULT_OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
CHROMA_TOKEN = os.environ.get("CHROMA_TOKEN", "")
COLLECTION = "obsidian_vault"
EMBED_MODEL = "nomic-embed-text"

# Chunking parameters
CHUNK_SIZE = 500       # ~words per chunk
CHUNK_OVERLAP = 50     # word overlap between chunks
MIN_CHUNK_SIZE = 50    # skip chunks smaller than this

# State file tracks file mtimes to avoid re-embedding unchanged files
STATE_FILE = Path("/opt/Odin/Odins_Self/vault_embed_state.json")


# ----- Chunking -----

def chunk_text(text: str, source: str) -> Iterator[tuple[str, int]]:
    """Yield (chunk_text, chunk_index) for the given text."""
    words = text.split()
    if len(words) < MIN_CHUNK_SIZE:
        if words:
            yield " ".join(words), 0
        return

    idx = 0
    pos = 0
    while pos < len(words):
        chunk_words = words[pos : pos + CHUNK_SIZE]
        if len(chunk_words) < MIN_CHUNK_SIZE and idx > 0:
            break
        yield " ".join(chunk_words), idx
        idx += 1
        pos += CHUNK_SIZE - CHUNK_OVERLAP


# ----- Embedding -----

def embed(text: str, ollama_url: str) -> list[float]:
    resp = requests.post(
        f"{ollama_url}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


# ----- State management -----

def load_state() -> dict[str, float]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict[str, float]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ----- Chroma client -----

def make_client(url: str, token: str):
    from urllib.parse import urlparse
    p = urlparse(url)
    settings = Settings(
        chroma_client_auth_provider="chromadb.auth.token_authn.TokenAuthClientProvider",
        chroma_client_auth_credentials=token,
    )
    return chromadb.HttpClient(
        host=p.hostname or "localhost",
        port=p.port or 8000,
        settings=settings,
    )


# ----- Main -----

def walk_vault(vault_path: Path) -> Iterator[Path]:
    for p in vault_path.rglob("*.md"):
        # Skip Obsidian metadata and hidden dirs
        if any(part.startswith(".") for part in p.parts):
            continue
        yield p


def doc_id(source: str, chunk_idx: int) -> str:
    h = hashlib.sha256(f"{source}:{chunk_idx}".encode()).hexdigest()[:16]
    return f"{h}_{chunk_idx}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="Wipe and rebuild")
    parser.add_argument("--update", action="store_true", help="Incremental update")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--vault-path", default=DEFAULT_VAULT)
    parser.add_argument("--chromadb-url", default=DEFAULT_CHROMADB)
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA)
    args = parser.parse_args()

    if not args.full and not args.update:
        args.update = True  # default mode

    vault = Path(args.vault_path)
    if not vault.is_dir():
        print(f"ERROR: vault path {vault} does not exist", file=sys.stderr)
        sys.exit(1)

    client = make_client(args.chromadb_url, CHROMA_TOKEN)

    if args.full:
        print(f"FULL rebuild: wiping collection '{COLLECTION}'")
        try:
            client.delete_collection(COLLECTION)
        except Exception:
            pass
        state = {}
    else:
        state = load_state()

    collection = client.get_or_create_collection(
        name=COLLECTION,
        metadata={"description": "Obsidian vault for Odin agent"},
    )

    files = list(walk_vault(vault))
    print(f"Found {len(files)} markdown files in {vault}")

    new_state: dict[str, float] = {}
    processed = 0
    chunks_embedded = 0
    skipped = 0

    for md_file in files:
        rel = str(md_file.relative_to(vault))
        mtime = md_file.stat().st_mtime
        new_state[rel] = mtime

        if args.update and state.get(rel) == mtime:
            skipped += 1
            continue

        try:
            text = md_file.read_text(encoding="utf-8")
        except Exception as e:
            print(f"WARN: could not read {rel}: {e}")
            continue

        if not text.strip():
            continue

        if args.dry_run:
            print(f"[dry-run] would embed: {rel}")
            processed += 1
            continue

        # Remove old chunks for this file if updating
        if not args.full:
            try:
                collection.delete(where={"source": rel})
            except Exception:
                pass

        ids, documents, metadatas, embeddings = [], [], [], []
        for chunk_text_str, idx in chunk_text(text, rel):
            try:
                emb = embed(chunk_text_str, args.ollama_url)
            except Exception as e:
                print(f"WARN: embedding failed for {rel} chunk {idx}: {e}")
                continue
            ids.append(doc_id(rel, idx))
            documents.append(chunk_text_str)
            metadatas.append({"source": rel, "chunk_index": idx, "mtime": mtime})
            embeddings.append(emb)

        if ids:
            try:
                collection.add(
                    ids=ids,
                    documents=documents,
                    metadatas=metadatas,
                    embeddings=embeddings,
                )
                chunks_embedded += len(ids)
                processed += 1
                print(f"  embedded {len(ids)} chunks from {rel}")
            except Exception as e:
                print(f"WARN: chroma add failed for {rel}: {e}")

    if not args.dry_run:
        save_state(new_state)

    print()
    print(f"Done. Processed: {processed}, Skipped (unchanged): {skipped}, "
          f"Chunks embedded: {chunks_embedded}")
    print(f"Collection size: {collection.count()}")


if __name__ == "__main__":
    main()
