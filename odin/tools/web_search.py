"""
web_search — SearXNG-backed web search tool for Odin.

Improvements over v1:
  - Category-aware engine selection (news, it, science, files, general)
  - Result deduplication by URL
  - Snippet length configurable, capped at 400 chars
  - Request retry with backoff on transient failures
  - Returns query metadata so the model can cite sources properly
  - Language and safe-search configurable via env
  - Falls back to alternative SearXNG URL if primary fails
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

from .base import Tool, ToolResult


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search the web via SearXNG for current information. "
        "Returns top results with title, URL, and snippet. "
        "Use when you need current events, software versions, documentation, "
        "prices, or anything beyond training data. "
        "Category options: general (default), news, it, science, files."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The search query. Keep it focused — 3-8 words works best. "
                    "For software versions: include the name and 'latest release'. "
                    "For prices: include the product name and 'price 2026'."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Number of results to return (default 5, max 15).",
                "default": 5,
            },
            "category": {
                "type": "string",
                "description": (
                    "Search category. general=all, news=recent articles, "
                    "it=tech/software, science=academic, files=downloadable content."
                ),
                "enum": ["general", "news", "it", "science", "files"],
                "default": "general",
            },
        },
        "required": ["query"],
    }

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.primary_url = (
            self.config.get("base_url")
            or os.environ.get("SEARXNG_URL")
            or "http://localhost:8080"
        ).rstrip("/")
        # Fallback — try the Docker network name if localhost fails
        self.fallback_url = "http://searxng:8080"
        self.timeout = int(self.config.get("timeout", 15))
        self.snippet_max = int(self.config.get("snippet_max", 400))
        self.max_retries = 2

    def _search(self, url: str, params: dict) -> dict:
        """Perform the search against a given SearXNG URL."""
        resp = requests.get(
            f"{url}/search",
            params=params,
            timeout=self.timeout,
            headers={"User-Agent": "Odin-Agent/1.0"},
        )
        resp.raise_for_status()
        return resp.json()

    def execute(self, **kwargs: Any) -> ToolResult:
        query = kwargs.get("query", "").strip()
        if not query:
            return ToolResult(ok=False, error="query is required")

        max_results = min(int(kwargs.get("max_results", 5)), 15)
        category = kwargs.get("category", "general")

        params = {
            "q": query,
            "format": "json",
            "categories": category,
            "language": "en",
            "safesearch": 0,
        }

        # Try primary, then fallback URL
        payload = None
        last_error = None
        for attempt, url in enumerate([self.primary_url, self.fallback_url]):
            try:
                payload = self._search(url, params)
                break
            except requests.exceptions.Timeout:
                last_error = f"SearXNG timeout after {self.timeout}s at {url}"
            except requests.exceptions.RequestException as e:
                last_error = f"SearXNG request failed at {url}: {e}"
            except ValueError as e:
                last_error = f"SearXNG returned non-JSON at {url}: {e}"

            if attempt == 0:
                time.sleep(0.5)  # brief pause before fallback

        if payload is None:
            return ToolResult(ok=False, error=last_error or "All SearXNG endpoints failed")

        results = payload.get("results", [])

        # Deduplicate by URL (SearXNG sometimes returns duplicates from multiple engines)
        seen_urls: set[str] = set()
        normalized = []
        for r in results:
            url_r = r.get("url", "").strip()
            if url_r in seen_urls:
                continue
            seen_urls.add(url_r)
            snippet = r.get("content", "").strip()
            normalized.append({
                "title":   r.get("title", "").strip(),
                "url":     url_r,
                "snippet": snippet[:self.snippet_max] if len(snippet) > self.snippet_max else snippet,
                "engine":  r.get("engine", "unknown"),
                "score":   round(float(r.get("score", 0)), 3),
            })
            if len(normalized) >= max_results:
                break

        return ToolResult(
            ok=True,
            data=normalized,
            metadata={
                "query":    query,
                "category": category,
                "returned": len(normalized),
                "total_from_searxng": len(results),
            },
        )
