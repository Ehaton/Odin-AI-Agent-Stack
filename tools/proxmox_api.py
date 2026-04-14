"""
proxmox_api — Proxmox VE REST API tool for Odin.

Wraps the Proxmox VE API for read and write operations against all
Proxmox nodes in BeanLab (NetworkBean, StorageBean, KidneyBean).

Uses API token auth — no password, no ticket management.
Create tokens in: Datacenter -> Permissions -> API Tokens
Required permissions: PVEAuditor for read-only, PVEAdmin for full control.
"""

from __future__ import annotations

import os
from typing import Any

import requests
import urllib3

from .base import Tool, ToolResult

# Self-signed certs are the norm for Proxmox home installs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class ProxmoxAPITool(Tool):
    name = "proxmox_api"
    description = (
        "Query and control Proxmox VE nodes via REST API. Supports listing "
        "VMs and LXC containers, checking node status, storage usage, and "
        "starting/stopping/rebooting guests. Faster and more reliable than "
        "SSH-based pct/qm commands for queries."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "The action to perform.",
                "enum": [
                    "list_nodes",
                    "list_vms",
                    "list_lxc",
                    "node_status",
                    "storage_status",
                    "vm_status",
                    "vm_start",
                    "vm_stop",
                    "vm_shutdown",
                    "vm_reboot",
                    "lxc_start",
                    "lxc_stop",
                    "lxc_reboot",
                ],
            },
            "node": {
                "type": "string",
                "description": "Proxmox node name (e.g. 'NetworkBean'). Required for most actions.",
            },
            "vmid": {
                "type": "integer",
                "description": "VM or LXC ID. Required for per-guest actions.",
            },
        },
        "required": ["action"],
    }

    # BeanLab Proxmox cluster nodes
    NODES = {
        "NetworkBean":  "https://192.168.1.206:8006",
        "StorageBean":  "https://192.168.1.207:8006",
        "KidneyBean":   "https://192.168.1.109:8006",
    }

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.token_id = self.config.get("token_id") or os.environ.get("PVE_TOKEN_ID")
        self.token_secret = self.config.get("token_secret") or os.environ.get("PVE_TOKEN_SECRET")
        if not self.token_id or not self.token_secret:
            raise ValueError(
                "proxmox_api requires PVE_TOKEN_ID and PVE_TOKEN_SECRET "
                "(env vars or config dict)"
            )
        self.timeout = int(self.config.get("timeout", 15))
        self.verify_ssl = bool(self.config.get("verify_ssl", False))

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"PVEAPIToken={self.token_id}={self.token_secret}",
        }

    def _request(self, method: str, node: str, path: str, **kwargs) -> dict[str, Any]:
        base = self.NODES.get(node)
        if not base:
            raise ValueError(f"unknown proxmox node: {node}")
        url = f"{base}/api2/json{path}"
        resp = requests.request(
            method, url,
            headers=self._headers(),
            verify=self.verify_ssl,
            timeout=self.timeout,
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    def execute(self, **kwargs: Any) -> ToolResult:
        action = kwargs.get("action")
        node = kwargs.get("node")
        vmid = kwargs.get("vmid")

        try:
            if action == "list_nodes":
                # Hit any node to list all cluster members
                first = next(iter(self.NODES))
                data = self._request("GET", first, "/nodes")
                return ToolResult(ok=True, data=data)

            if action == "list_vms":
                if not node:
                    # List across all known nodes
                    out = {}
                    for n in self.NODES:
                        try:
                            out[n] = self._request("GET", n, f"/nodes/{n}/qemu")
                        except Exception as e:
                            out[n] = {"error": str(e)}
                    return ToolResult(ok=True, data=out)
                data = self._request("GET", node, f"/nodes/{node}/qemu")
                return ToolResult(ok=True, data=data)

            if action == "list_lxc":
                if not node:
                    out = {}
                    for n in self.NODES:
                        try:
                            out[n] = self._request("GET", n, f"/nodes/{n}/lxc")
                        except Exception as e:
                            out[n] = {"error": str(e)}
                    return ToolResult(ok=True, data=out)
                data = self._request("GET", node, f"/nodes/{node}/lxc")
                return ToolResult(ok=True, data=data)

            if action == "node_status":
                if not node:
                    return ToolResult(ok=False, error="node is required")
                data = self._request("GET", node, f"/nodes/{node}/status")
                return ToolResult(ok=True, data=data)

            if action == "storage_status":
                if not node:
                    return ToolResult(ok=False, error="node is required")
                data = self._request("GET", node, f"/nodes/{node}/storage")
                return ToolResult(ok=True, data=data)

            if action == "vm_status":
                if not node or not vmid:
                    return ToolResult(ok=False, error="node and vmid are required")
                data = self._request("GET", node, f"/nodes/{node}/qemu/{vmid}/status/current")
                return ToolResult(ok=True, data=data)

            # State-changing actions
            state_actions = {
                "vm_start":    ("POST", "qemu",  "status/start"),
                "vm_stop":     ("POST", "qemu",  "status/stop"),
                "vm_shutdown": ("POST", "qemu",  "status/shutdown"),
                "vm_reboot":   ("POST", "qemu",  "status/reboot"),
                "lxc_start":   ("POST", "lxc",   "status/start"),
                "lxc_stop":    ("POST", "lxc",   "status/stop"),
                "lxc_reboot":  ("POST", "lxc",   "status/reboot"),
            }
            if action in state_actions:
                if not node or not vmid:
                    return ToolResult(ok=False, error="node and vmid are required")
                method, guest_type, endpoint = state_actions[action]
                data = self._request(
                    method, node,
                    f"/nodes/{node}/{guest_type}/{vmid}/{endpoint}",
                )
                return ToolResult(
                    ok=True,
                    data=data,
                    metadata={"action": action, "node": node, "vmid": vmid},
                )

            return ToolResult(ok=False, error=f"unknown action: {action}")

        except requests.exceptions.RequestException as e:
            return ToolResult(ok=False, error=f"proxmox API error: {e}")
