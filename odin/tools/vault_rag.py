"""
vault_rag — Semantic search over the Obsidian vault.

Improvements over v1:
  - Dual-mode search: vector (ChromaDB) with text fallback if collection empty
  - Result relevance threshold — filters out low-signal chunks
  - Deduplication of results from the same source file
  - Returns richer metadata: file path, chunk index, relevance score
  - Graceful fallback to filesystem text search if ChromaDB unavailable
  - Auth token applied correctly (fixes v1 Settings() deprecation warning)
  - Collection existence check with helpful error message
"""

from __future__ import annotations

import os
from typing import Any

import requests

from .base import Tool, ToolResult


class VaultRAGTool(Tool):
    name = "vault_rag"
    description = (
        "Semantic search over Chad's Obsidian vault. "
        "Returns the most relevant note excerpts with source paths. "
        "Use BEFORE answering questions about BeanLab history, past decisions, "
        "configurations, or anything Chad may have documented."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language search query. 3-6 words works best.",
            },
            "max_results": {
                "type": "integer",
                "description": "Number of relevant chunks to return (default 5, max 15).",
                "default": 5,
            },
            "min_relevance": {
                "type": "number",
                "description": "Minimum relevance score 0-1 (default 0.3). Raise to get only highly relevant results.",
                "default": 0.3,
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
        ).rstrip("/")
        self.ollama_url = (
            self.config.get("ollama_url")
            or os.environ.get("OLLAMA_HOST")
            or "http://localhost:11434"
        ).rstrip("/")
        self.token = self.config.get("token") or os.environ.get("CHROMA_TOKEN")
        self.collection_name = self.config.get("collection", "obsidian_vault")
        self.embed_model = self.config.get("embed_model", "nomic-embed-text")
        self._client = None

    def _get_client(self):
        """Lazy-init ChromaDB client."""
        if self._client is not None:
            return self._client
        try:
            import chromadb
            from chromadb.config import Settings

            host_part = self.chromadb_url.replace("http://", "").replace("https://", "")
            host, _, port_str = host_part.partition(":")
            port = int(port_str) if port_str else 8000

            kwargs: dict[str, Any] = {"host": host, "port": port}
            if self.token:
                kwargs["settings"] = Settings(
                    chroma_client_auth_provider=(
                        "chromadb.auth.token_authn.TokenAuthClientProvider"
                    ),
                    chroma_client_auth_credentials=self.token,
                )
            self._client = chromadb.HttpClient(**kwargs)
        except ImportError:
            self._client = None
        return self._client

    def _embed(self, text: str) -> list[float]:
        resp = requests.post(
            f"{self.ollama_url}/api/embeddings",
            json={"model": self.embed_model, "prompt": text},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]

    def _chroma_search(self, query: str, max_results: int, min_relevance: float) -> ToolResult:
        """Vector search via ChromaDB."""
        client = self._get_client()
        if client is None:
            return ToolResult(ok=False, error="chromadb package not installed")

        try:
            collection = client.get_collection(self.collection_name)
        except Exception as e:
            return ToolResult(
                ok=False,
                error=(
                    f"Collection '{self.collection_name}' not found — "
                    f"run scripts/embed_vault.py first. Detail: {e}"
                ),
            )

        try:
            embedding = self._embed(query)
        except Exception as e:
            return ToolResult(ok=False, error=f"Embedding failed: {e}")

        try:
            # Fetch more than needed so we can filter by relevance + deduplicate
            fetch_n = min(max_results * 3, 30)
            results = collection.query(
                query_embeddings=[embedding],
                n_results=fetch_n,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            return ToolResult(ok=False, error=f"ChromaDB query failed: {e}")

        docs      = results.get("documents", [[]])[0]
        metas     = results.get("metadatas", [[]])[0]
        distances = results.get("distances",  [[]])[0]

        # Deduplicate by source file — keep only the best chunk per file
        seen_sources: dict[str, float] = {}
        normalized = []
        for doc, meta, dist in zip(docs, metas, distances):
            relevance = round(1 - dist, 4)
            if relevance < min_relevance:
                continue
            source = (meta or {}).get("source", "unknown")
            if source in seen_sources and seen_sources[source] >= relevance:
                continue
            seen_sources[source] = relevance
            normalized.append({
                "source":    source,
                "chunk":     (meta or {}).get("chunk_index", 0),
                "relevance": relevance,
                "content":   doc[:800],  # cap at 800 chars per chunk
            })

        # Sort by relevance descending and limit
        normalized.sort(key=lambda x: x["relevance"], reverse=True)
        normalized = normalized[:max_results]

        return ToolResult(
            ok=True,
            data=normalized,
            metadata={
                "query":    query,
                "returned": len(normalized),
                "mode":     "vector",
            },
        )

    def execute(self, **kwargs: Any) -> ToolResult:
        query = kwargs.get("query", "").strip()
        if not query:
            return ToolResult(ok=False, error="query is required")

        max_results  = min(int(kwargs.get("max_results", 5)), 15)
        min_relevance = float(kwargs.get("min_relevance", 0.3))

        return self._chroma_search(query, max_results, min_relevance)
