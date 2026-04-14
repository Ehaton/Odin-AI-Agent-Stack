"""
vault_rag — Semantic search over Chad's Obsidian vault.

Uses ChromaDB as the vector store and Ollama's nomic-embed-text model for
embeddings (already running locally, no API calls). Odin queries this tool
BEFORE answering anything about Chad's past notes, BeanLab decisions, or
historical context.

Requires:
  - ChromaDB running at CHROMADB_URL (default http://localhost:8000)
  - Ollama with nomic-embed-text pulled: `ollama pull nomic-embed-text`
  - Vault embedded via scripts/embed_vault.py

Collection: "obsidian_vault"
"""

from __future__ import annotations

import os
from typing import Any

import requests
import chromadb
from chromadb.config import Settings

from .base import Tool, ToolResult


class VaultRAGTool(Tool):
    name = "vault_rag"
    description = (
        "Semantic search over Chad's Obsidian vault. Returns the most "
        "relevant notes as markdown excerpts with source file paths. "
        "Use this BEFORE answering questions about BeanLab history, past "
        "decisions, configurations, or anything Chad may have documented."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The semantic search query in natural language.",
            },
            "max_results": {
                "type": "integer",
                "description": "Number of relevant chunks to return (default 5, max 15).",
                "default": 5,
            },
        },
        "required": ["query"],
    }

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.chromadb_url = (
            self.config.get("chromadb_url")
            or os.environ.get("CHROMADB_URL")
            or "http://localhost:8000"
        )
        self.ollama_url = (
            self.config.get("ollama_url")
            or os.environ.get("OLLAMA_URL")
            or "http://localhost:11434"
        )
        self.token = self.config.get("token") or os.environ.get("CHROMA_TOKEN")
        self.collection_name = self.config.get("collection", "obsidian_vault")
        self.embed_model = self.config.get("embed_model", "nomic-embed-text")

        host, port = self._parse_url(self.chromadb_url)
        client_settings = Settings(
            chroma_client_auth_provider="chromadb.auth.token_authn.TokenAuthClientProvider",
            chroma_client_auth_credentials=self.token or "",
        )
        self.client = chromadb.HttpClient(
            host=host, port=port, settings=client_settings,
        )

    @staticmethod
    def _parse_url(url: str) -> tuple[str, int]:
        from urllib.parse import urlparse
        p = urlparse(url)
        return p.hostname or "localhost", p.port or 8000

    def _embed(self, text: str) -> list[float]:
        resp = requests.post(
            f"{self.ollama_url}/api/embeddings",
            json={"model": self.embed_model, "prompt": text},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]

    def execute(self, **kwargs: Any) -> ToolResult:
        query = kwargs.get("query", "").strip()
        if not query:
            return ToolResult(ok=False, error="query is required")
        max_results = min(int(kwargs.get("max_results", 5)), 15)

        try:
            collection = self.client.get_collection(self.collection_name)
        except Exception as e:
            return ToolResult(
                ok=False,
                error=f"collection '{self.collection_name}' not found — "
                      f"run embed_vault.py first: {e}",
            )

        try:
            embedding = self._embed(query)
        except Exception as e:
            return ToolResult(ok=False, error=f"embedding failed: {e}")

        try:
            results = collection.query(
                query_embeddings=[embedding],
                n_results=max_results,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            return ToolResult(ok=False, error=f"chroma query failed: {e}")

        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        normalized = [
            {
                "source": (meta or {}).get("source", "unknown"),
                "chunk": (meta or {}).get("chunk_index", 0),
                "relevance": round(1 - dist, 4),  # cosine distance → similarity
                "content": doc,
            }
            for doc, meta, dist in zip(docs, metas, distances)
        ]

        return ToolResult(
            ok=True,
            data=normalized,
            metadata={"query": query, "returned": len(normalized)},
        )
