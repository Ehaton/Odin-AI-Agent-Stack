"""
image_gen — ComfyUI image generation tool for Odin.

Supports Flux.1 Dev/Schnell (unet-based) and SDXL checkpoints
(Juggernaut XL, DreamShaper XL) via a unified interface.

Model selection is automatic based on what's available in ComfyUI,
or can be overridden by the caller via the `style` parameter.

VRAM management:
  Before generation, asks Ollama to unload any large models that would
  conflict with Flux on a 24GB GPU. The models listed in `unload_models`
  are unloaded. Currently qwen3.6:35b-a3b is the only model large enough
  to conflict (~22GB). qwen3-coder:30b (~17GB) can coexist with Flux Schnell
  (~10GB active) but not Flux Dev (~20GB active) — so Dev also triggers
  an unload.

After generation, the next Ollama request reloads whatever model is needed.
Generated images are saved to ODIN_IMAGE_DIR (default /opt/Odin/generated_images).

Requires:
  - ComfyUI running at COMFYUI_URL (default http://localhost:8188)
  - At least one of the following model sets downloaded:
      Flux Schnell: flux1-schnell.safetensors in /models/unet/
                    ae.safetensors in /models/vae/
                    clip_l.safetensors + t5xxl_fp8_e4m3fn.safetensors in /models/clip/
      Flux Dev:     flux1-dev.safetensors in /models/unet/ (same VAE + CLIP)
      SDXL:         any .safetensors checkpoint in /models/checkpoints/
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


# ── Workflow templates ────────────────────────────────────────────────────────

FLUX_WORKFLOW_TEMPLATE = {
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {"clip": ["11", 0], "text": "__PROMPT__"},
    },
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {"clip": ["11", 0], "text": ""},  # negative (unused by Flux)
    },
    "8": {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["13", 0], "vae": ["10", 0]},
    },
    "9": {
        "class_type": "SaveImage",
        "inputs": {"filename_prefix": "odin_flux_", "images": ["8", 0]},
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
            "unet_name": "__UNET__",          # flux1-dev.safetensors or flux1-schnell.safetensors
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
            "negative": ["7", 0],
            "positive": ["6", 0],
            "sampler_name": "euler",
            "scheduler": "simple",
            "seed": 0,
            "steps": 20,               # Dev: 20 steps. Schnell: override to 4.
        },
    },
    "14": {
        "class_type": "EmptyLatentImage",
        "inputs": {"batch_size": 1, "height": 1024, "width": 1024},
    },
}

SDXL_WORKFLOW_TEMPLATE = {
    "4": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "__CHECKPOINT__"},
    },
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {"clip": ["4", 1], "text": "__PROMPT__"},
    },
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {
            "clip": ["4", 1],
            "text": "low quality, blurry, watermark, text, deformed, ugly, bad anatomy",
        },
    },
    "8": {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
    },
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "cfg": 7.0,
            "denoise": 1.0,
            "latent_image": ["5", 0],
            "model": ["4", 0],
            "negative": ["7", 0],
            "positive": ["6", 0],
            "sampler_name": "dpmpp_2m",
            "scheduler": "karras",
            "seed": 0,
            "steps": 30,
        },
    },
    "5": {
        "class_type": "EmptyLatentImage",
        "inputs": {"batch_size": 1, "height": 1024, "width": 1024},
    },
    "9": {
        "class_type": "SaveImage",
        "inputs": {"filename_prefix": "odin_sdxl_", "images": ["8", 0]},
    },
}

# Style → (backend, model_file, steps, cfg) presets
STYLE_PRESETS = {
    # Flux Dev — highest quality, all-purpose
    "default":      ("flux_dev",    "flux1-dev.safetensors",      20, 1.0),
    "photorealistic": ("flux_dev",  "flux1-dev.safetensors",      25, 1.0),
    "portrait":     ("flux_dev",    "flux1-dev.safetensors",      25, 1.0),
    "landscape":    ("flux_dev",    "flux1-dev.safetensors",      20, 1.0),
    "cinematic":    ("flux_dev",    "flux1-dev.safetensors",      20, 1.0),
    "concept_art":  ("flux_dev",    "flux1-dev.safetensors",      20, 1.0),
    "abstract":     ("flux_dev",    "flux1-dev.safetensors",      20, 1.0),
    # Flux Schnell — fast iteration, 4 steps
    "fast":         ("flux_schnell","flux1-schnell.safetensors",   4, 1.0),
    "sketch":       ("flux_schnell","flux1-schnell.safetensors",   4, 1.0),
    # SDXL — photorealism workhorse
    "fantasy":      ("sdxl", "dreamshaper_xl.safetensors",        30, 7.0),
    "dnd":          ("sdxl", "dreamshaper_xl.safetensors",        30, 7.0),
    "artistic":     ("sdxl", "dreamshaper_xl.safetensors",        30, 7.0),
    "oil_painting": ("sdxl", "dreamshaper_xl.safetensors",        30, 7.0),
    "photo":        ("sdxl", "juggernautXL_v10.safetensors",      30, 7.0),
    "architecture": ("sdxl", "juggernautXL_v10.safetensors",      30, 7.0),
}

# Models that consume too much VRAM to coexist with Flux on 24GB
VRAM_HOG_MODELS = ["qwen3.6:35b-a3b", "qwen3-coder:30b"]


class ImageGenTool(Tool):
    name = "image_gen"
    description = (
        "Generate an image from a text prompt using ComfyUI. "
        "Supports Flux Dev (highest quality, all styles), Flux Schnell (fast), "
        "DreamShaper XL (fantasy/artistic/DnD), and Juggernaut XL (photorealistic). "
        "Style options: default, photorealistic, portrait, landscape, cinematic, "
        "concept_art, abstract, fast, sketch, fantasy, dnd, artistic, oil_painting, "
        "photo, architecture. "
        "Generation takes 15-60 seconds. Image is saved to disk and path returned."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "Detailed description of the image. Be specific about subject, "
                    "lighting, style, mood, and composition. More detail = better results."
                ),
            },
            "style": {
                "type": "string",
                "description": (
                    "Image style preset. Controls which model and sampler settings are used. "
                    "Options: default, photorealistic, portrait, landscape, cinematic, "
                    "concept_art, abstract, fast, sketch, fantasy, dnd, artistic, "
                    "oil_painting, photo, architecture."
                ),
                "enum": list(STYLE_PRESETS.keys()),
                "default": "default",
            },
            "width": {
                "type": "integer",
                "description": "Width in pixels (default 1024, must be multiple of 64, max 2048).",
                "default": 1024,
            },
            "height": {
                "type": "integer",
                "description": "Height in pixels (default 1024, must be multiple of 64, max 2048).",
                "default": 1024,
            },
            "seed": {
                "type": "integer",
                "description": "Seed for reproducibility. 0 = random.",
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
            or os.environ.get("OLLAMA_HOST")
            or "http://localhost:11434"
        ).rstrip("/")
        self.output_dir = Path(
            self.config.get("output_dir")
            or os.environ.get("ODIN_IMAGE_DIR")
            or "/opt/Odin/generated_images"
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.poll_timeout = int(self.config.get("timeout", 300))  # 5 min max
        self.unload_models: list[str] = self.config.get(
            "unload_models", VRAM_HOG_MODELS
        )

    # ── VRAM management ───────────────────────────────────────────────────────

    def _unload_ollama_models(self, models: list[str]) -> None:
        """Unload specified models from Ollama to free VRAM for image generation."""
        for model in models:
            try:
                requests.post(
                    f"{self.ollama_url}/api/generate",
                    json={"model": model, "prompt": "", "keep_alive": 0},
                    timeout=10,
                )
            except Exception:
                pass  # model wasn't loaded — that's fine

    # ── ComfyUI helpers ───────────────────────────────────────────────────────

    def _check_comfyui(self) -> bool:
        """Return True if ComfyUI is reachable."""
        try:
            r = requests.get(f"{self.comfyui_url}/system_stats", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    def _get_available_models(self) -> dict[str, list[str]]:
        """Fetch model lists from ComfyUI to know what's available."""
        try:
            r = requests.get(f"{self.comfyui_url}/object_info/UNETLoader", timeout=5)
            unet = r.json().get("UNETLoader", {}).get("input", {}).get(
                "required", {}).get("unet_name", [{}])[0]
        except Exception:
            unet = []
        try:
            r = requests.get(f"{self.comfyui_url}/object_info/CheckpointLoaderSimple", timeout=5)
            ckpts = r.json().get("CheckpointLoaderSimple", {}).get("input", {}).get(
                "required", {}).get("ckpt_name", [{}])[0]
        except Exception:
            ckpts = []
        return {"unet": unet if isinstance(unet, list) else [],
                "checkpoints": ckpts if isinstance(ckpts, list) else []}

    def _build_flux_workflow(
        self,
        prompt: str,
        unet_name: str,
        steps: int,
        width: int,
        height: int,
        seed: int,
    ) -> dict:
        wf = json.loads(json.dumps(FLUX_WORKFLOW_TEMPLATE))
        wf["6"]["inputs"]["text"] = prompt
        wf["12"]["inputs"]["unet_name"] = unet_name
        wf["13"]["inputs"]["steps"] = steps
        wf["13"]["inputs"]["seed"] = seed
        wf["14"]["inputs"]["width"] = width
        wf["14"]["inputs"]["height"] = height
        return wf

    def _build_sdxl_workflow(
        self,
        prompt: str,
        checkpoint: str,
        steps: int,
        cfg: float,
        width: int,
        height: int,
        seed: int,
    ) -> dict:
        wf = json.loads(json.dumps(SDXL_WORKFLOW_TEMPLATE))
        wf["4"]["inputs"]["ckpt_name"] = checkpoint
        wf["6"]["inputs"]["text"] = prompt
        wf["3"]["inputs"]["steps"] = steps
        wf["3"]["inputs"]["cfg"] = cfg
        wf["3"]["inputs"]["seed"] = seed
        wf["5"]["inputs"]["width"] = width
        wf["5"]["inputs"]["height"] = height
        return wf

    def _queue_prompt(self, workflow: dict) -> str:
        client_id = str(uuid.uuid4())
        resp = requests.post(
            f"{self.comfyui_url}/prompt",
            json={"prompt": workflow, "client_id": client_id},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"ComfyUI rejected workflow: {data['error']}")
        return data["prompt_id"]

    def _wait_for_completion(self, prompt_id: str) -> dict:
        deadline = time.time() + self.poll_timeout
        while time.time() < deadline:
            try:
                resp = requests.get(
                    f"{self.comfyui_url}/history/{prompt_id}", timeout=10
                )
                if resp.status_code == 200:
                    history = resp.json()
                    if prompt_id in history:
                        job = history[prompt_id]
                        # Check for errors in the job result
                        status = job.get("status", {})
                        if status.get("status_str") == "error":
                            msgs = status.get("messages", [])
                            raise RuntimeError(f"ComfyUI job failed: {msgs}")
                        return job
            except RuntimeError:
                raise
            except Exception:
                pass
            time.sleep(2)
        raise TimeoutError(
            f"ComfyUI job {prompt_id} did not complete in {self.poll_timeout}s"
        )

    def _fetch_image(self, filename: str, subfolder: str, folder_type: str) -> bytes:
        resp = requests.get(
            f"{self.comfyui_url}/view",
            params={"filename": filename, "subfolder": subfolder, "type": folder_type},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.content

    # ── Main execute ──────────────────────────────────────────────────────────

    def execute(self, **kwargs: Any) -> ToolResult:
        prompt = kwargs.get("prompt", "").strip()
        if not prompt:
            return ToolResult(ok=False, error="prompt is required")

        style = kwargs.get("style", "default")
        if style not in STYLE_PRESETS:
            style = "default"

        width  = min(2048, (int(kwargs.get("width",  1024)) // 64) * 64)
        height = min(2048, (int(kwargs.get("height", 1024)) // 64) * 64)
        seed   = int(kwargs.get("seed", 0)) or (int(time.time() * 1000) % (2**32))

        backend, model_file, steps, cfg = STYLE_PRESETS[style]

        # Step 1: Verify ComfyUI is up
        if not self._check_comfyui():
            return ToolResult(
                ok=False,
                error=(
                    f"ComfyUI is not reachable at {self.comfyui_url}. "
                    "Check `docker logs comfyui` and ensure it has finished starting."
                ),
            )

        # Step 2: Verify the requested model file exists
        available = self._get_available_models()
        if backend in ("flux_dev", "flux_schnell"):
            if model_file not in available["unet"]:
                # Try to fall back to whatever Flux model IS available
                flux_available = [m for m in available["unet"] if "flux" in m.lower()]
                if not flux_available:
                    return ToolResult(
                        ok=False,
                        error=(
                            f"Model '{model_file}' not found in ComfyUI. "
                            f"Available UNet models: {available['unet'] or 'none'}. "
                            "Download Flux models first — see the Obsidian vault: "
                            "ComfyUI Security & Access — BeanLab.md"
                        ),
                    )
                # Use the best available Flux model
                model_file = flux_available[0]
                backend = "flux_schnell" if "schnell" in model_file else "flux_dev"
                _, _, steps, cfg = STYLE_PRESETS[
                    "fast" if backend == "flux_schnell" else "default"
                ][2:]
        else:  # sdxl
            if model_file not in available["checkpoints"]:
                sdxl_available = available["checkpoints"]
                if not sdxl_available:
                    # Fall back to Flux if no checkpoints available
                    flux_available = [m for m in available["unet"] if "flux" in m.lower()]
                    if flux_available:
                        backend = "flux_schnell" if "schnell" in flux_available[0] else "flux_dev"
                        model_file = flux_available[0]
                        steps = 4 if backend == "flux_schnell" else 20
                        cfg = 1.0
                    else:
                        return ToolResult(
                            ok=False,
                            error=(
                                f"No SDXL checkpoints or Flux models found. "
                                f"Available: {available}"
                            ),
                        )
                else:
                    model_file = sdxl_available[0]

        # Step 3: Unload Ollama models to free VRAM
        # Flux Dev needs more VRAM — unload everything large
        # Flux Schnell can coexist with llama3.2:3b but not the 30B+ models
        models_to_unload = self.unload_models if backend in ("flux_dev", "flux_schnell") else []
        if models_to_unload:
            self._unload_ollama_models(models_to_unload)
            time.sleep(2)  # brief pause for VRAM to actually free

        # Step 4: Build workflow
        if backend in ("flux_dev", "flux_schnell"):
            workflow = self._build_flux_workflow(
                prompt, model_file, steps, width, height, seed
            )
        else:
            workflow = self._build_sdxl_workflow(
                prompt, model_file, steps, cfg, width, height, seed
            )

        # Step 5: Queue and wait
        try:
            prompt_id = self._queue_prompt(workflow)
        except Exception as e:
            return ToolResult(ok=False, error=f"Failed to queue ComfyUI job: {e}")

        try:
            result = self._wait_for_completion(prompt_id)
        except TimeoutError as e:
            return ToolResult(ok=False, error=str(e))
        except RuntimeError as e:
            return ToolResult(ok=False, error=str(e))
        except Exception as e:
            return ToolResult(ok=False, error=f"ComfyUI polling failed: {e}")

        # Step 6: Find output image
        outputs = result.get("outputs", {})
        image_info = None
        for node_output in outputs.values():
            if "images" in node_output:
                image_info = node_output["images"][0]
                break
        if image_info is None:
            return ToolResult(
                ok=False,
                error="ComfyUI completed but no image output found in result",
            )

        # Step 7: Download and save
        try:
            image_bytes = self._fetch_image(
                image_info["filename"],
                image_info.get("subfolder", ""),
                image_info.get("type", "output"),
            )
        except Exception as e:
            return ToolResult(ok=False, error=f"Failed to fetch image from ComfyUI: {e}")

        timestamp = int(time.time())
        filename = f"odin_{style}_{timestamp}_{seed}.png"
        local_path = self.output_dir / filename
        local_path.write_bytes(image_bytes)

        return ToolResult(
            ok=True,
            data={
                "path": str(local_path),
                "filename": filename,
                "prompt": prompt,
                "style": style,
                "backend": backend,
                "model": model_file,
                "seed": seed,
                "width": width,
                "height": height,
                "steps": steps,
            },
            metadata={
                "comfyui_prompt_id": prompt_id,
                "file_size_kb": round(len(image_bytes) / 1024, 1),
            },
        )
