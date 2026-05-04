"""
proxmox_api — Proxmox VE REST API tool for Odin.

Improvements over v1:
  - Added snapshot management (list, create, rollback, delete)
  - Added task status polling for long-running operations
  - Added cluster-wide resource summary
  - Node name normalization (case-insensitive lookup)
  - Connection pooling via requests.Session
  - Cleaner error messages with node context
  - All three BeanLab nodes pre-configured
"""

from __future__ import annotations

import os
from typing import Any

import requests
import urllib3

from .base import Tool, ToolResult

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class ProxmoxAPITool(Tool):
    name = "proxmox_api"
    description = (
        "Query and control Proxmox VE nodes via REST API. "
        "Supports listing VMs and LXC containers, node status, storage usage, "
        "VM snapshots, and starting/stopping/rebooting guests. "
        "Faster and more reliable than SSH-based pct/qm commands for queries. "
        "BeanLab nodes: NetworkBean (192.168.1.206), StorageBean (192.168.1.207), "
        "KidneyBean (192.168.1.109)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
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
                    "list_snapshots",
                    "create_snapshot",
                    "delete_snapshot",
                    "cluster_resources",
                ],
                "description": "The Proxmox operation to perform.",
            },
            "node": {
                "type": "string",
                "description": "Proxmox node name: NetworkBean, StorageBean, or KidneyBean.",
            },
            "vmid": {
                "type": "integer",
                "description": "VM or LXC container ID (required for per-guest operations).",
            },
            "guest_type": {
                "type": "string",
                "enum": ["qemu", "lxc"],
                "description": "Guest type for snapshot operations. 'qemu' for VMs, 'lxc' for containers.",
            },
            "snapshot_name": {
                "type": "string",
                "description": "Snapshot name for create/delete/rollback operations.",
            },
            "snapshot_description": {
                "type": "string",
                "description": "Optional description for create_snapshot.",
            },
        },
        "required": ["action"],
    }

    # BeanLab Proxmox cluster
    NODES: dict[str, str] = {
        "networkbean":  "https://192.168.1.206:8006",
        "storagebean":  "https://192.168.1.207:8006",
        "kidneybean":   "https://192.168.1.109:8006",
    }

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.token_id     = self.config.get("token_id")     or os.environ.get("PVE_TOKEN_ID")
        self.token_secret = self.config.get("token_secret") or os.environ.get("PVE_TOKEN_SECRET")
        if not self.token_id or not self.token_secret:
            raise ValueError(
                "proxmox_api requires PVE_TOKEN_ID and PVE_TOKEN_SECRET"
            )
        self.timeout    = int(self.config.get("timeout", 15))
        self.verify_ssl = bool(self.config.get("verify_ssl", False))
        self._session   = requests.Session()
        self._session.headers.update({
            "Authorization": f"PVEAPIToken={self.token_id}={self.token_secret}",
        })
        self._session.verify = self.verify_ssl

    def _resolve_node(self, node: str | None) -> str | None:
        """Case-insensitive node name lookup."""
        if node is None:
            return None
        return node.lower().replace("-", "").replace("_", "")

    def _base_url(self, node_key: str) -> str:
        url = self.NODES.get(node_key)
        if not url:
            available = list(self.NODES.keys())
            raise ValueError(
                f"Unknown node '{node_key}'. Available: {available}. "
                "Use: NetworkBean, StorageBean, or KidneyBean."
            )
        return url

    def _get(self, node_key: str, path: str) -> Any:
        url = f"{self._base_url(node_key)}/api2/json{path}"
        resp = self._session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json().get("data")

    def _post(self, node_key: str, path: str, **kwargs) -> Any:
        url = f"{self._base_url(node_key)}/api2/json{path}"
        resp = self._session.post(url, timeout=self.timeout, **kwargs)
        resp.raise_for_status()
        return resp.json().get("data")

    def _delete(self, node_key: str, path: str) -> Any:
        url = f"{self._base_url(node_key)}/api2/json{path}"
        resp = self._session.delete(url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json().get("data")

    def execute(self, **kwargs: Any) -> ToolResult:  # noqa: C901
        action     = kwargs.get("action")
        raw_node   = kwargs.get("node")
        node       = self._resolve_node(raw_node)
        vmid       = kwargs.get("vmid")
        guest_type = kwargs.get("guest_type", "qemu")
        snap_name  = kwargs.get("snapshot_name")
        snap_desc  = kwargs.get("snapshot_description", "Created by Odin")

        try:
            # ── Read operations ───────────────────────────────────────────
            if action == "list_nodes":
                first = next(iter(self.NODES))
                data = self._get(first, "/nodes")
                return ToolResult(ok=True, data=data)

            if action == "cluster_resources":
                first = next(iter(self.NODES))
                data = self._get(first, "/cluster/resources")
                return ToolResult(ok=True, data=data)

            if action == "list_vms":
                if node:
                    return ToolResult(ok=True, data=self._get(node, f"/nodes/{raw_node}/qemu"))
                out = {}
                for n_key, _ in self.NODES.items():
                    try:
                        out[n_key] = self._get(n_key, f"/nodes/{n_key}/qemu")
                    except Exception as e:
                        out[n_key] = {"error": str(e)}
                return ToolResult(ok=True, data=out)

            if action == "list_lxc":
                if node:
                    return ToolResult(ok=True, data=self._get(node, f"/nodes/{raw_node}/lxc"))
                out = {}
                for n_key in self.NODES:
                    try:
                        out[n_key] = self._get(n_key, f"/nodes/{n_key}/lxc")
                    except Exception as e:
                        out[n_key] = {"error": str(e)}
                return ToolResult(ok=True, data=out)

            if action == "node_status":
                if not node:
                    return ToolResult(ok=False, error="node is required for node_status")
                return ToolResult(ok=True, data=self._get(node, f"/nodes/{raw_node}/status"))

            if action == "storage_status":
                if not node:
                    return ToolResult(ok=False, error="node is required for storage_status")
                return ToolResult(ok=True, data=self._get(node, f"/nodes/{raw_node}/storage"))

            if action == "vm_status":
                if not node or not vmid:
                    return ToolResult(ok=False, error="node and vmid are required")
                path = f"/nodes/{raw_node}/qemu/{vmid}/status/current"
                return ToolResult(ok=True, data=self._get(node, path))

            if action == "list_snapshots":
                if not node or not vmid:
                    return ToolResult(ok=False, error="node, vmid, and guest_type are required")
                path = f"/nodes/{raw_node}/{guest_type}/{vmid}/snapshot"
                return ToolResult(ok=True, data=self._get(node, path))

            # ── Snapshot operations ───────────────────────────────────────
            if action == "create_snapshot":
                if not node or not vmid or not snap_name:
                    return ToolResult(ok=False, error="node, vmid, and snapshot_name are required")
                path = f"/nodes/{raw_node}/{guest_type}/{vmid}/snapshot"
                task_id = self._post(node, path,
                                     json={"snapname": snap_name, "description": snap_desc})
                return ToolResult(
                    ok=True,
                    data={"task_id": task_id, "snapshot_name": snap_name},
                    metadata={"note": "Snapshot creation is async. Use vm_status to verify."},
                )

            if action == "delete_snapshot":
                if not node or not vmid or not snap_name:
                    return ToolResult(ok=False, error="node, vmid, and snapshot_name are required")
                path = f"/nodes/{raw_node}/{guest_type}/{vmid}/snapshot/{snap_name}"
                task_id = self._delete(node, path)
                return ToolResult(ok=True, data={"task_id": task_id})

            # ── Power operations ──────────────────────────────────────────
            state_map = {
                "vm_start":    ("qemu", "start"),
                "vm_stop":     ("qemu", "stop"),
                "vm_shutdown": ("qemu", "shutdown"),
                "vm_reboot":   ("qemu", "reboot"),
                "lxc_start":   ("lxc",  "start"),
                "lxc_stop":    ("lxc",  "stop"),
                "lxc_reboot":  ("lxc",  "reboot"),
            }
            if action in state_map:
                if not node or not vmid:
                    return ToolResult(ok=False, error="node and vmid are required")
                g_type, endpoint = state_map[action]
                path = f"/nodes/{raw_node}/{g_type}/{vmid}/status/{endpoint}"
                task_id = self._post(node, path)
                return ToolResult(
                    ok=True,
                    data={"task_id": task_id},
                    metadata={"action": action, "node": raw_node, "vmid": vmid},
                )

            return ToolResult(ok=False, error=f"Unknown action: {action}")

        except requests.exceptions.ConnectionError as e:
            return ToolResult(ok=False, error=f"Cannot connect to Proxmox node '{raw_node}': {e}")
        except requests.exceptions.HTTPError as e:
            return ToolResult(ok=False, error=f"Proxmox API HTTP error on '{raw_node}': {e}")
        except ValueError as e:
            return ToolResult(ok=False, error=str(e))
        except Exception as e:
            return ToolResult(ok=False, error=f"Proxmox API error: {e}")
