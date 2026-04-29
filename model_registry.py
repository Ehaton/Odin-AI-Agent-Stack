"""
Model Registry — dynamic model configuration loader for Odin.

Reads models.yaml at startup and exposes:
    - MODELS dict (role → model tag)
    - MODEL_INFO dict (model tag → metadata)
    - CATEGORIES dict (category → role)
    - ROUTING config (thresholds for classify())
    - N8N config (webhook URLs for workflow integration)
    - DEFAULT_VISION_MODEL

Usage in Odin.py:
    from model_registry import registry
    MODELS = registry.roles
    MODEL_INFO = registry.models
    CATEGORIES = registry.categories

Adding a new model at runtime is as simple as editing models.yaml and
restarting Odin. No code changes required.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("  ⚠️  PyYAML not installed. Run: pip install pyyaml")
    sys.exit(1)


DEFAULT_CONFIG_PATH = os.environ.get(
    "ODIN_MODELS_CONFIG",
    str(Path(__file__).parent / "models.yaml")
)


class ModelRegistry:
    """Lazy-loaded registry of models, roles, and routing config."""

    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH):
        self.config_path = config_path
        self._config: dict[str, Any] | None = None
        self._load()

    def _load(self) -> None:
        """Load and validate the YAML config."""
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(
                f"Model config not found: {self.config_path}\n"
                f"Set ODIN_MODELS_CONFIG env var or create models.yaml in the Odin directory."
            )

        with open(self.config_path, "r", encoding="utf-8") as f:
            self._config = yaml.safe_load(f)

        self._validate()

    def _validate(self) -> None:
        """Check that the config has everything Odin.py needs."""
        if not self._config:
            raise ValueError("Empty model config")

        required_keys = ["roles", "models", "categories"]
        missing = [k for k in required_keys if k not in self._config]
        if missing:
            raise ValueError(f"Model config missing required keys: {missing}")

        # Every role must point at a model that exists in the catalog
        for role, tag in self._config["roles"].items():
            if tag not in self._config["models"]:
                raise ValueError(
                    f"Role '{role}' points at undefined model '{tag}'. "
                    f"Add it under `models:` in {self.config_path}"
                )

        # Every category must point at a role that exists
        for cat, role in self._config["categories"].items():
            if role not in self._config["roles"]:
                raise ValueError(
                    f"Category '{cat}' points at undefined role '{role}'. "
                    f"Add it under `roles:` in {self.config_path}"
                )

        # Each model must have at minimum a label and a description
        for tag, info in self._config["models"].items():
            if not isinstance(info, dict):
                raise ValueError(f"Model '{tag}' metadata must be a mapping, got {type(info).__name__}")
            if "label" not in info:
                info["label"] = tag
            if "description" not in info:
                info["description"] = ""
            # Apply sane defaults for optional fields
            info.setdefault("supports_tools", True)
            info.setdefault("supports_vision", False)
            info.setdefault("disable_thinking", False)
            info.setdefault("num_ctx", 8192)
            info.setdefault("temperature", 0.7)
            info.setdefault("top_p", 0.9)
            info.setdefault("prewarm", False)
            info.setdefault("role_hints", [])

    def reload(self) -> None:
        """Re-read the YAML file. Useful for hot-reloading without restart."""
        self._load()

    # ─── Public API — the shape Odin.py expects ──────────────────────────

    @property
    def roles(self) -> dict[str, str]:
        """Role → model tag map (replaces the old MODELS dict)."""
        return dict(self._config["roles"])

    @property
    def models(self) -> dict[str, dict[str, Any]]:
        """Model tag → metadata map (replaces the old MODEL_INFO dict).

        Also injects an "auto" entry that the web UI expects to find.
        """
        out = dict(self._config["models"])
        # Inject the "auto" pseudo-model used by the picker
        if "auto" not in out:
            out = {"auto": {
                "label": "Auto (smart routing)",
                "description": (
                    "Routes by query type: code/simple infra → Loki Executor, "
                    "HA voice → Llama 3.2, complex/vision → Odin Reasoner."
                ),
                "supports_tools": True,
                "supports_vision": False,
            }, **out}
        return out

    @property
    def categories(self) -> dict[str, str]:
        """Category → role map used by classify()."""
        return dict(self._config["categories"])

    @property
    def routing(self) -> dict[str, int]:
        """Threshold config for classify()."""
        return dict(self._config.get("routing", {
            "complex_host_threshold": 2,
            "complex_word_threshold": 30,
            "voice_max_words": 15,
            "simple_infra_max_words": 15,
        }))

    @property
    def default_vision_model(self) -> str:
        """Model to route to for image-bearing messages."""
        return self._config.get("default_vision_model", self.roles.get("vision", "odin-reasoner:v2"))

    @property
    def n8n(self) -> dict[str, Any]:
        """n8n webhook config (optional)."""
        return dict(self._config.get("n8n", {"enabled": False, "workflows": {}}))

    def prewarm_targets(self) -> list[str]:
        """Return deduplicated list of model tags that should be prewarmed."""
        tags = []
        for tag, info in self._config["models"].items():
            if info.get("prewarm") and tag not in tags:
                tags.append(tag)
        return tags

    def resolve_role(self, role: str) -> str | None:
        """Get the model tag for a given role, or None if undefined."""
        return self._config["roles"].get(role)

    def resolve_category(self, category: str) -> str:
        """Get the model tag for a given classification category.

        Looks up the category's role, then resolves the role to a tag.
        Falls back to the 'general' role, then to the first defined role.
        """
        role = self._config["categories"].get(category)
        if role is None:
            role = self._config["categories"].get("general", "worker")
        tag = self._config["roles"].get(role)
        if tag is None:
            # Last-resort fallback: first role in the dict
            tag = next(iter(self._config["roles"].values()))
        return tag


# Singleton — imported by Odin.py
registry = ModelRegistry()


if __name__ == "__main__":
    # CLI smoke test: `python model_registry.py`
    print(f"Config: {registry.config_path}")
    print()
    print("Roles:")
    for role, tag in registry.roles.items():
        print(f"  {role:12} → {tag}")
    print()
    print("Models:")
    for tag, info in registry.models.items():
        if tag == "auto":
            continue
        prewarm = "🔥" if info.get("prewarm") else "  "
        print(f"  {prewarm} {tag:30} num_ctx={info['num_ctx']:>6} tools={info['supports_tools']} vision={info['supports_vision']}")
    print()
    print("Prewarm targets:", registry.prewarm_targets())
    print("Default vision:", registry.default_vision_model)
    print("n8n enabled:", registry.n8n.get("enabled", False))
