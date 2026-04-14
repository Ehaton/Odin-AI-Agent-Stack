"""
image_gen — FLUX.1-schnell image generation via ComfyUI.

Handles the VRAM dance: before submitting a job, asks Ollama to unload
gemma4:26b (the only model too big to coexist with FLUX on a 24GB 3090).
The smaller models (llama3.2:3b, qwen3:4b) can stay loaded.

After generation completes, the next request to Ollama will reload whatever
model is needed.

Requires:
  - ComfyUI running at COMFYUI_URL
  - FLUX.1-schnell checkpoint downloaded into ComfyUI models directory
  - requests library

Generated images are saved to /opt/Odin/generated_images/ and the path is
returned to the caller for display/vault linking.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import requests

from .base import Tool, ToolResult


# FLUX.1-schnell workflow template (ComfyUI format)
# This is the minimal workflow for text-to-image with FLUX-schnell.
# For more complex workflows (LoRAs, ControlNet, etc.), load a saved workflow
# via ComfyUI's UI and export it here.
FLUX_WORKFLOW = {
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {"clip": ["11", 0], "text": "PROMPT_PLACEHOLDER"},
    },
    "8": {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["13", 0], "vae": ["10", 0]},
    },
    "9": {
        "class_type": "SaveImage",
        "inputs": {"filename_prefix": "odin_", "images": ["8", 0]},
    },
    "10": {
        "class_type": "VAELoader",
        "inputs": {"vae_name": "ae.safetensors"},
    },
    "11": {
        "class_type": "DualCLIPLoader",
        "inputs": {
            "clip_name1": "t5xxl_fp8_e4m3fn.safetensors",
            "clip_name2": "clip_l.safetensors",
            "type": "flux",
        },
    },
    "12": {
        "class_type": "UNETLoader",
        "inputs": {
            "unet_name": "flux1-schnell-fp8.safetensors",
            "weight_dtype": "fp8_e4m3fn",
        },
    },
    "13": {
        "class_type": "KSampler",
        "inputs": {
            "cfg": 1.0,
            "denoise": 1.0,
            "latent_image": ["14", 0],
            "model": ["12", 0],
            "negative": ["6", 0],
            "positive": ["6", 0],
            "sampler_name": "euler",
            "scheduler": "simple",
            "seed": 0,
            "steps": 4,
        },
    },
    "14": {
        "class_type": "EmptyLatentImage",
        "inputs": {"batch_size": 1, "height": 1024, "width": 1024},
    },
}


class ImageGenTool(Tool):
    name = "image_gen"
    description = (
        "Generate an image from a text prompt using FLUX.1-schnell. "
        "Returns the path to the generated PNG. Note: generation takes "
        "10-30 seconds and temporarily unloads the reasoner model from GPU."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Detailed text description of the image to generate. "
                               "Be specific — FLUX responds well to detailed prompts.",
            },
            "width": {
                "type": "integer",
                "description": "Image width in pixels (default 1024, must be multiple of 64).",
                "default": 1024,
            },
            "height": {
                "type": "integer",
                "description": "Image height in pixels (default 1024, must be multiple of 64).",
                "default": 1024,
            },
            "seed": {
                "type": "integer",
                "description": "Random seed for reproducibility. 0 means random.",
                "default": 0,
            },
        },
        "required": ["prompt"],
    }

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.comfyui_url = (
            self.config.get("comfyui_url")
            or os.environ.get("COMFYUI_URL")
            or "http://localhost:8188"
        ).rstrip("/")
        self.ollama_url = (
            self.config.get("ollama_url")
            or os.environ.get("OLLAMA_URL")
            or "http://localhost:11434"
        )
        self.output_dir = Path(
            self.config.get("output_dir")
            or os.environ.get("ODIN_IMAGE_DIR")
            or "/opt/Odin/generated_images"
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = int(self.config.get("timeout", 180))
        # Model(s) to unload before generation (free VRAM)
        self.unload_models = self.config.get(
            "unload_models", ["gemma4:26b"]
        )

    def _unload_ollama_models(self) -> None:
        """Ask Ollama to unload heavy models before FLUX takes the GPU."""
        for model in self.unload_models:
            try:
                # Sending a generate request with keep_alive=0 unloads the model
                requests.post(
                    f"{self.ollama_url}/api/generate",
                    json={"model": model, "prompt": "", "keep_alive": 0},
                    timeout=10,
                )
            except Exception:
                # If the model wasn't loaded, this errors — that's fine
                pass

    def _queue_prompt(self, workflow: dict) -> str:
        """Submit workflow to ComfyUI and return the prompt_id."""
        client_id = str(uuid.uuid4())
        resp = requests.post(
            f"{self.comfyui_url}/prompt",
            json={"prompt": workflow, "client_id": client_id},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["prompt_id"]

    def _wait_for_completion(self, prompt_id: str) -> dict:
        """Poll ComfyUI history until the job is done."""
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                resp = requests.get(
                    f"{self.comfyui_url}/history/{prompt_id}", timeout=10
                )
                if resp.status_code == 200:
                    history = resp.json()
                    if prompt_id in history:
                        return history[prompt_id]
            except requests.exceptions.RequestException:
                pass
            time.sleep(1)
        raise TimeoutError(f"ComfyUI job {prompt_id} did not complete in {self.timeout}s")

    def _fetch_image(self, filename: str, subfolder: str, folder_type: str) -> bytes:
        """Download the generated image bytes from ComfyUI."""
        resp = requests.get(
            f"{self.comfyui_url}/view",
            params={"filename": filename, "subfolder": subfolder, "type": folder_type},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.content

    def execute(self, **kwargs: Any) -> ToolResult:
        prompt = kwargs.get("prompt", "").strip()
        if not prompt:
            return ToolResult(ok=False, error="prompt is required")

        width = int(kwargs.get("width", 1024))
        height = int(kwargs.get("height", 1024))
        seed = int(kwargs.get("seed", 0)) or int(time.time() * 1000) % (2**32)

        # Snap dimensions to multiples of 64
        width = (width // 64) * 64
        height = (height // 64) * 64

        workflow = json.loads(json.dumps(FLUX_WORKFLOW))  # deep copy
        workflow["6"]["inputs"]["text"] = prompt
        workflow["13"]["inputs"]["seed"] = seed
        workflow["14"]["inputs"]["width"] = width
        workflow["14"]["inputs"]["height"] = height

        # Step 1: Free VRAM for FLUX
        self._unload_ollama_models()

        # Step 2: Queue the job
        try:
            prompt_id = self._queue_prompt(workflow)
        except Exception as e:
            return ToolResult(ok=False, error=f"failed to queue ComfyUI job: {e}")

        # Step 3: Wait for completion
        try:
            result = self._wait_for_completion(prompt_id)
        except TimeoutError as e:
            return ToolResult(ok=False, error=str(e))
        except Exception as e:
            return ToolResult(ok=False, error=f"ComfyUI polling failed: {e}")

        # Step 4: Find the output image in the history
        outputs = result.get("outputs", {})
        image_info = None
        for node_output in outputs.values():
            if "images" in node_output:
                image_info = node_output["images"][0]
                break
        if image_info is None:
            return ToolResult(ok=False, error="ComfyUI completed but no image output found")

        # Step 5: Download and save locally
        try:
            image_bytes = self._fetch_image(
                image_info["filename"],
                image_info.get("subfolder", ""),
                image_info.get("type", "output"),
            )
        except Exception as e:
            return ToolResult(ok=False, error=f"failed to fetch generated image: {e}")

        local_path = self.output_dir / f"odin_{int(time.time())}_{seed}.png"
        local_path.write_bytes(image_bytes)

        return ToolResult(
            ok=True,
            data={
                "path": str(local_path),
                "prompt": prompt,
                "seed": seed,
                "width": width,
                "height": height,
            },
            metadata={"comfyui_prompt_id": prompt_id},
        )
