"""
Base Tool class for the Odin agent stack.

All tools inherit from Tool and implement execute(). The name, description,
and parameters schema are used by Odin.py to expose tools to the model via
the Ollama function-calling interface.

Parameter schemas follow the JSON Schema subset used by OpenAI function
calling and Ollama tool use — dict with type, properties, required.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


logger = logging.getLogger("odin.tools")


@dataclass
class ToolResult:
    """Standardized return value for all tool executions."""
    ok: bool
    data: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str, indent=2)


class Tool(ABC):
    """Base class for all Odin tools.

    Subclasses must define:
        name:        str — tool identifier used by the model
        description: str — one or two sentences, what the tool does
        parameters:  dict — JSON schema for the tool's arguments

    And implement:
        execute(**kwargs) -> ToolResult
    """

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        if not self.name:
            raise ValueError(f"{type(self).__name__} must define a name")
        if not self.description:
            raise ValueError(f"{type(self).__name__} must define a description")

    @abstractmethod
    def execute(self, **kwargs: Any) -> ToolResult:
        """Run the tool with the provided arguments."""
        ...

    def to_ollama_schema(self) -> dict[str, Any]:
        """Emit the Ollama/OpenAI function-calling schema for this tool."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def __call__(self, **kwargs: Any) -> ToolResult:
        """Wrap execute() with error handling and logging."""
        logger.info("tool.%s called with %s", self.name, kwargs)
        try:
            result = self.execute(**kwargs)
            if not result.ok:
                logger.warning("tool.%s returned error: %s", self.name, result.error)
            return result
        except Exception as e:
            logger.exception("tool.%s raised exception", self.name)
            return ToolResult(ok=False, error=f"{type(e).__name__}: {e}")


class ToolRegistry:
    """Holds and dispatches tools by name."""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            logger.warning("tool.%s already registered, overwriting", tool.name)
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all_schemas(self) -> list[dict[str, Any]]:
        return [t.to_ollama_schema() for t in self._tools.values()]

    def execute(self, name: str, **kwargs: Any) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult(ok=False, error=f"unknown tool: {name}")
        return tool(**kwargs)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self):
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)
