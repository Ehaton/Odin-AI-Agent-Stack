"""
web_search — SearXNG-backed web search tool for Odin.

Hits a self-hosted SearXNG instance and returns normalized results.
"""

from __future__ import annotations

import os
from typing import Any

import requests

from .base import Tool, ToolResult


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search the web via SearXNG. Returns top results with title, URL, "
        "and snippet. Use when you need current information, news, software "
        "versions, or anything beyond the model's training data."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query. Keep it focused — 3-8 words works best.",
            },
            "max_results": {
                "type": "integer",
                "description": "Number of results to return (default 5, max 20).",
                "default": 5,
            },
            "category": {
                "type": "string",
                "description": "Result category filter.",
                "enum": ["general", "news", "it", "science", "files"],
                "default": "general",
            },
        },
        "required": ["query"],
    }

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.base_url = (
            self.config.get("base_url")
            or os.environ.get("SEARXNG_URL")
            or "http://searxng.beanlab:8080"
        ).rstrip("/")
        self.timeout = int(self.config.get("timeout", 15))

    def execute(self, **kwargs: Any) -> ToolResult:
        query = kwargs.get("query", "").strip()
        if not query:
            return ToolResult(ok=False, error="query is required")

        max_results = min(int(kwargs.get("max_results", 5)), 20)
        category = kwargs.get("category", "general")

        params = {
            "q": query,
            "format": "json",
            "categories": category,
            "language": "en",
            "safesearch": 0,
        }

        try:
            resp = requests.get(
                f"{self.base_url}/search",
                params=params,
                timeout=self.timeout,
                headers={"User-Agent": "Odin-Agent/1.0"},
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.exceptions.Timeout:
            return ToolResult(ok=False, error=f"SearXNG timeout after {self.timeout}s")
        except requests.exceptions.RequestException as e:
            return ToolResult(ok=False, error=f"SearXNG request failed: {e}")
        except ValueError as e:
            return ToolResult(ok=False, error=f"SearXNG returned non-JSON: {e}")

        results = payload.get("results", [])[:max_results]
        normalized = [
            {
                "title": r.get("title", "").strip(),
                "url": r.get("url", "").strip(),
                "snippet": r.get("content", "").strip(),
                "engine": r.get("engine", "unknown"),
            }
            for r in results
        ]

        return ToolResult(
            ok=True,
            data=normalized,
            metadata={
                "query": query,
                "total_returned": len(normalized),
                "category": category,
            },
        )
