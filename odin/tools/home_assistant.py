"""
home_assistant — Home Assistant REST API tool for Odin.

Read states, call services, and control smart home devices from any HA
instance that has the REST API enabled (default in all recent versions).

Requires a Long-Lived Access Token from your HA user profile:
  Profile (bottom left) -> Security -> Long-Lived Access Tokens -> Create Token
"""

from __future__ import annotations

import os
from typing import Any

import requests

from .base import Tool, ToolResult


class HomeAssistantTool(Tool):
    name = "home_assistant"
    description = (
        "Query state and control devices in Home Assistant. Can list entities, "
        "read current state of any entity, and call services to turn things "
        "on/off, set values, or trigger automations."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "The operation to perform.",
                "enum": [
                    "list_states",
                    "get_state",
                    "call_service",
                    "list_services",
                    "fire_event",
                ],
            },
            "entity_id": {
                "type": "string",
                "description": "Entity ID for get_state or call_service (e.g. 'light.office').",
            },
            "domain": {
                "type": "string",
                "description": "Service domain for call_service (e.g. 'light', 'switch', 'script').",
            },
            "service": {
                "type": "string",
                "description": "Service name for call_service (e.g. 'turn_on', 'turn_off').",
            },
            "service_data": {
                "type": "object",
                "description": "Optional data payload for the service call.",
            },
            "event_type": {
                "type": "string",
                "description": "Event type for fire_event.",
            },
            "filter_prefix": {
                "type": "string",
                "description": "Optional prefix filter for list_states (e.g. 'sensor.' or 'light.').",
            },
        },
        "required": ["action"],
    }

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.base_url = (
            self.config.get("base_url")
            or os.environ.get("HASS_URL")
            or "http://homeassistant.local:8123"
        ).rstrip("/")
        self.token = self.config.get("token") or os.environ.get("HASS_TOKEN")
        if not self.token:
            raise ValueError(
                "home_assistant requires HASS_TOKEN env var or token in config"
            )
        self.timeout = int(self.config.get("timeout", 10))

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.base_url}/api{path}"
        resp = requests.request(
            method, url, headers=self._headers(), timeout=self.timeout, **kwargs
        )
        resp.raise_for_status()
        if resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return resp.text

    def execute(self, **kwargs: Any) -> ToolResult:
        action = kwargs.get("action")

        try:
            if action == "list_states":
                data = self._request("GET", "/states")
                prefix = kwargs.get("filter_prefix")
                if prefix:
                    data = [s for s in data if s.get("entity_id", "").startswith(prefix)]
                summary = [
                    {
                        "entity_id": s["entity_id"],
                        "state": s["state"],
                        "friendly_name": s.get("attributes", {}).get("friendly_name"),
                    }
                    for s in data
                ]
                return ToolResult(ok=True, data=summary, metadata={"count": len(summary)})

            if action == "get_state":
                entity_id = kwargs.get("entity_id")
                if not entity_id:
                    return ToolResult(ok=False, error="entity_id is required")
                data = self._request("GET", f"/states/{entity_id}")
                return ToolResult(ok=True, data=data)

            if action == "list_services":
                data = self._request("GET", "/services")
                return ToolResult(ok=True, data=data)

            if action == "call_service":
                domain = kwargs.get("domain")
                service = kwargs.get("service")
                if not domain or not service:
                    return ToolResult(
                        ok=False, error="domain and service are required"
                    )
                payload = kwargs.get("service_data") or {}
                entity_id = kwargs.get("entity_id")
                if entity_id and "entity_id" not in payload:
                    payload["entity_id"] = entity_id
                data = self._request(
                    "POST", f"/services/{domain}/{service}", json=payload
                )
                return ToolResult(
                    ok=True,
                    data=data,
                    metadata={"domain": domain, "service": service},
                )

            if action == "fire_event":
                event_type = kwargs.get("event_type")
                if not event_type:
                    return ToolResult(ok=False, error="event_type is required")
                data = self._request(
                    "POST", f"/events/{event_type}", json=kwargs.get("service_data") or {}
                )
                return ToolResult(ok=True, data=data)

            return ToolResult(ok=False, error=f"unknown action: {action}")

        except requests.exceptions.RequestException as e:
            return ToolResult(ok=False, error=f"home assistant API error: {e}")
