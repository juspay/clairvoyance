"""
ComfyUI client service for generating images via ComfyUI API.
"""

import asyncio
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from websockets import connect as ws_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from app.core import config
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session


class ComfyUIClient:
    """Client for interacting with ComfyUI API for image generation."""

    def __init__(
        self,
        base_url: str = None,
        websocket_url: str = None,
        timeout: int = 60,  # 1 minute default timeout for voice agents
    ):
        self.base_url = base_url or config.COMFYUI_BASE_URL
        self.websocket_url = websocket_url or config.COMFYUI_WEBSOCKET_URL
        self.timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = create_aiohttp_session()
        return self._session

    async def close(self):
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            logger.debug("ComfyUI client session closed")

    async def get_system_stats(self) -> Dict[str, Any]:
        """Get ComfyUI system statistics."""
        session = await self._get_session()
        try:
            async with session.get(f"{self.base_url}/system_stats") as response:
                if response.status == 200:
                    return await response.json()
                else:
                    raise Exception(f"Failed to get system stats: {response.status}")
        except Exception as e:
            logger.error(f"Error getting ComfyUI system stats: {e}")
            raise

    async def queue_prompt(self, workflow: Dict[str, Any]) -> str:
        """
        Queue a workflow for processing in ComfyUI.

        Args:
            workflow: The ComfyUI workflow dictionary

        Returns:
            str: The prompt ID for tracking
        """
        session = await self._get_session()

        prompt_id = str(uuid.uuid4())
        payload = {"prompt": workflow, "client_id": prompt_id}

        try:
            async with session.post(
                f"{self.base_url}/prompt",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    logger.info(
                        f"ComfyUI prompt queued successfully: {result.get('prompt_id', prompt_id)}"
                    )
                    return result.get("prompt_id", prompt_id)
                else:
                    error_text = await response.text()
                    raise Exception(
                        f"Failed to queue prompt: {response.status} - {error_text}"
                    )
        except Exception as e:
            logger.error(f"Error queuing ComfyUI prompt: {e}")
            raise

    async def wait_for_completion(self, prompt_id: str) -> Dict[str, Any]:
        """
        Wait for a prompt to complete processing via WebSocket.

        Args:
            prompt_id: The prompt ID to track

        Returns:
            Dict containing the execution results
        """
        try:
            uri = f"{self.websocket_url}?clientId={prompt_id}"

            async with ws_connect(uri) as websocket:
                logger.info(f"Connected to ComfyUI WebSocket for prompt {prompt_id}")

                start_time = asyncio.get_event_loop().time()

                while True:
                    # Check timeout
                    if asyncio.get_event_loop().time() - start_time > self.timeout:
                        raise asyncio.TimeoutError(
                            f"ComfyUI generation timeout after {self.timeout}s"
                        )

                    try:
                        # Wait for message with timeout
                        message = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                        data = json.loads(message)

                        msg_type = data.get("type")

                        if msg_type == "executing":
                            node_id = data.get("data", {}).get("node")
                            if node_id is None:
                                # Execution finished
                                logger.info(
                                    f"ComfyUI execution completed for prompt {prompt_id}"
                                )
                                # Get the final results
                                return await self.get_history(prompt_id)

                        elif msg_type == "progress":
                            value = data.get("data", {}).get("value", 0)
                            max_value = data.get("data", {}).get("max", 100)
                            logger.debug(f"ComfyUI progress: {value}/{max_value}")

                        elif msg_type == "status":
                            exec_info = (
                                data.get("data", {})
                                .get("status", {})
                                .get("exec_info", {})
                            )
                            queue_remaining = exec_info.get("queue_remaining", 0)
                            if queue_remaining > 0:
                                logger.info(
                                    f"ComfyUI queue position: {queue_remaining}"
                                )

                        elif msg_type == "error":
                            error_msg = data.get("data", {})
                            logger.error(f"ComfyUI execution error: {error_msg}")
                            raise Exception(f"ComfyUI execution failed: {error_msg}")

                    except asyncio.TimeoutError:
                        # Keep waiting, just log occasionally
                        logger.debug(f"Waiting for ComfyUI completion: {prompt_id}")

                        # Check if generation completed by polling history
                        # This is especially important for fal.ai nodes that may not send standard completion messages
                        try:
                            history = await self.get_history(prompt_id)
                            if history and "outputs" in history:
                                logger.info(
                                    f"ComfyUI execution completed for prompt {prompt_id} (detected via history)"
                                )
                                return history
                        except Exception as hist_e:
                            logger.debug(f"History check failed: {hist_e}")

                        continue

        except (ConnectionClosed, WebSocketException) as e:
            logger.error(f"WebSocket connection error: {e}")
            raise Exception(f"WebSocket connection failed: {e}")
        except Exception as e:
            logger.error(f"Error waiting for ComfyUI completion: {e}")
            raise

    async def get_history(self, prompt_id: str) -> Dict[str, Any]:
        """Get the execution history for a prompt."""
        session = await self._get_session()

        try:
            async with session.get(f"{self.base_url}/history/{prompt_id}") as response:
                if response.status == 200:
                    history = await response.json()
                    return history.get(prompt_id, {})
                else:
                    raise Exception(f"Failed to get history: {response.status}")
        except Exception as e:
            logger.error(f"Error getting ComfyUI history: {e}")
            raise

    async def get_image_urls(self, history: Dict[str, Any]) -> List[str]:
        """Extract image URLs from execution history."""
        image_urls = []

        try:
            outputs = history.get("outputs", {})
            for node_id, node_output in outputs.items():
                if "images" in node_output:
                    for image_info in node_output["images"]:
                        filename = image_info.get("filename")
                        subfolder = image_info.get("subfolder", "")
                        if filename:
                            # Use Clairvoyance backend proxy URL instead of direct ComfyUI URL for CORS compatibility
                            if subfolder:
                                # For frontend consumption, use the backend proxy endpoint
                                image_url = f"/api/v1/images/comfyui?filename={filename}&subfolder={subfolder}"
                                # Store the original ComfyUI URL for internal use
                                original_url = f"{self.base_url}/view?filename={filename}&subfolder={subfolder}"
                            else:
                                image_url = (
                                    f"/api/v1/images/comfyui?filename={filename}"
                                )
                                original_url = (
                                    f"{self.base_url}/view?filename={filename}"
                                )
                            image_urls.append(image_url)
                            logger.info(
                                f"Generated image URL: {image_url} (proxying {original_url})"
                            )

        except Exception as e:
            logger.error(f"Error extracting image URLs: {e}")

        return image_urls

    async def generate_image(
        self, prompt: str, workflow_template: str = "text_to_image", **kwargs
    ) -> Tuple[List[str], Dict[str, Any]]:
        """
        High-level method to generate an image from a text prompt.

        Args:
            prompt: Text description for image generation
            workflow_template: The workflow template to use
            **kwargs: Additional parameters for the workflow

        Returns:
            Tuple of (image_urls, execution_history)
        """
        try:
            # Build workflow from template
            workflow = self.build_workflow(prompt, workflow_template, **kwargs)

            # Queue the prompt
            prompt_id = await self.queue_prompt(workflow)

            # Wait for completion
            history = await self.wait_for_completion(prompt_id)

            # Extract image URLs
            image_urls = await self.get_image_urls(history)

            if not image_urls:
                logger.warning("No images generated from ComfyUI workflow")

            return image_urls, history

        except Exception as e:
            logger.error(f"Error in ComfyUI image generation: {e}")
            raise

    def build_workflow(
        self, prompt: str, template: str = "text_to_image", **kwargs
    ) -> Dict[str, Any]:
        """
        Build a ComfyUI workflow from a template.

        Args:
            prompt: The text prompt for image generation
            template: The workflow template name
            **kwargs: Additional parameters

        Returns:
            Dict: The complete workflow
        """
        if template == "text_to_image":
            return self._build_fal_flux_text_to_image_workflow(
                prompt, **kwargs
            )  # Use fal.ai Flux Pro 1.1
        elif template == "advertisement":
            return self._build_fal_flux_text_to_image_workflow(
                prompt, **kwargs
            )  # Use fal.ai Flux Pro 1.1
        elif template == "advertisement_with_logo":
            return self._build_advertisement_with_logo_workflow(
                prompt, **kwargs
            )  # Use fal.ai + logo compositing
        elif template == "fal_flux_pro_kontext":
            return self._build_fal_flux_pro_kontext_workflow(prompt, **kwargs)
        elif template == "fal_image_edit":
            return self._build_fal_image_edit_workflow(prompt, **kwargs)
        elif template == "fal_text_to_image":
            return self._build_fal_flux_text_to_image_workflow(prompt, **kwargs)
        elif template == "mask_and_background_change":
            return self._build_mask_and_background_change_workflow(prompt, **kwargs)
        elif template == "mask_first_then_edit":
            return self._build_mask_first_then_edit_workflow(prompt, **kwargs)
        elif template == "edit_previous_image":
            return self._build_edit_previous_image_workflow(prompt, **kwargs)
        else:
            raise ValueError(f"Unknown workflow template: {template}")

    def _build_text_to_image_workflow(
        self,
        prompt: str,
        width: int = 1024,
        height: int = 1024,
        steps: int = 15,  # Reduced from 20 for speed
        cfg: float = 7.5,  # Reduced from 8.0 for speed
        sampler_name: str = "dpm_fast",  # Changed from euler for speed
        scheduler: str = "simple",  # Changed from normal for speed
        model_name: str = "v1-5-pruned-emaonly.safetensors",  # Use SafeTensors by default
        seed: int = -1,
        **kwargs,
    ) -> Dict[str, Any]:
        """Build a basic text-to-image workflow."""

        if seed == -1:
            seed = int(uuid.uuid4().int % (2**32))

        workflow = {
            "1": {
                "inputs": {"ckpt_name": model_name},
                "class_type": "CheckpointLoaderSimple",
                "_meta": {"title": "Load Checkpoint"},
            },
            "2": {
                "inputs": {"width": width, "height": height, "batch_size": 1},
                "class_type": "EmptyLatentImage",
                "_meta": {"title": "Empty Latent Image"},
            },
            "3": {
                "inputs": {"text": prompt, "clip": ["1", 1]},
                "class_type": "CLIPTextEncode",
                "_meta": {"title": "CLIP Text Encode (Prompt)"},
            },
            "4": {
                "inputs": {
                    "text": "text, watermark, low quality, worst quality",
                    "clip": ["1", 1],
                },
                "class_type": "CLIPTextEncode",
                "_meta": {"title": "CLIP Text Encode (Negative)"},
            },
            "5": {
                "inputs": {
                    "seed": seed,
                    "steps": steps,
                    "cfg": cfg,
                    "sampler_name": sampler_name,
                    "scheduler": scheduler,
                    "denoise": 1.0,
                    "model": ["1", 0],
                    "positive": ["3", 0],
                    "negative": ["4", 0],
                    "latent_image": ["2", 0],
                },
                "class_type": "KSampler",
                "_meta": {"title": "KSampler"},
            },
            "6": {
                "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
                "class_type": "VAEDecode",
                "_meta": {"title": "VAE Decode"},
            },
            "7": {
                "inputs": {"filename_prefix": "ComfyUI", "images": ["6", 0]},
                "class_type": "SaveImage",
                "_meta": {"title": "Save Image"},
            },
        }

        return workflow

    def _build_advertisement_workflow(
        self,
        prompt: str,
        product_type: str = "shoes",
        style: str = "modern advertising",
        **kwargs,
    ) -> Dict[str, Any]:
        """Build a workflow specifically for advertisement generation."""

        # Enhance the prompt for advertisement
        enhanced_prompt = f"professional {style} advertisement for {product_type}, {prompt}, high quality, commercial photography, studio lighting, clean background"

        # Set advertisement-specific defaults optimized for speed, but allow kwargs to override
        ad_defaults = {
            "steps": 15,  # Reduced from 25 for faster generation
            "cfg": 7.5,  # Reduced from 9.0 for faster generation
            "sampler_name": "dpm_fast",  # Faster sampler
            "scheduler": "simple",  # Simpler scheduler
        }
        # Merge defaults with provided kwargs (kwargs take precedence)
        final_kwargs = {**ad_defaults, **kwargs}

        return self._build_text_to_image_workflow(
            prompt=enhanced_prompt, **final_kwargs
        )

    def _build_gemini_workflow(self, prompt: str, **kwargs) -> Dict[str, Any]:
        """Build a Gemini-based image generation workflow."""

        seed = kwargs.get("seed", -1)
        if seed == -1:
            seed = int(uuid.uuid4().int % (2**32))

        workflow = {
            "1": {
                "inputs": {
                    "prompt": prompt,  # Changed from "text" to "prompt"
                    "model": "gemini-2.5-flash-image-preview",
                    "seed": seed,
                    "control": "randomize",
                },
                "class_type": "GeminiImageNode",
                "_meta": {"title": "Gemini Image Generation"},
            },
            "2": {
                "inputs": {"filename_prefix": "ComfyUI_Gemini", "images": ["1", 0]},
                "class_type": "SaveImage",
                "_meta": {"title": "Save Generated Image"},
            },
        }

        return workflow

    def _build_gemini_advertisement_workflow(
        self,
        prompt: str,
        product_type: str = "shoes",
        style: str = "modern advertising",
        **kwargs,
    ) -> Dict[str, Any]:
        """Build a Gemini workflow specifically for advertisement generation."""

        # Enhance the prompt for advertisement
        enhanced_prompt = f"professional {style} advertisement for {product_type}, {prompt}, high quality, commercial photography, studio lighting, clean background, marketing poster, promotional design"

        return self._build_gemini_workflow(prompt=enhanced_prompt, **kwargs)

    def _build_flux_workflow(
        self,
        prompt: str,
        width: int = 1024,
        height: int = 1024,
        steps: int = 20,
        guidance: float = 3.5,
        **kwargs,
    ) -> Dict[str, Any]:
        """Build a FLUX.1-dev image generation workflow."""

        seed = kwargs.get("seed", -1)
        if seed == -1:
            seed = int(uuid.uuid4().int % (2**32))

        workflow = {
            "1": {
                "inputs": {"ckpt_name": "flux1-dev.safetensors"},
                "class_type": "CheckpointLoaderSimple",
                "_meta": {"title": "Load FLUX.1-dev"},
            },
            "2": {
                "inputs": {"width": width, "height": height, "batch_size": 1},
                "class_type": "EmptyLatentImage",
                "_meta": {"title": "Empty Latent Image"},
            },
            "3": {
                "inputs": {
                    "clip": ["1", 1],
                    "clip_l": prompt,
                    "t5xxl": prompt,
                    "guidance": guidance,
                },
                "class_type": "CLIPTextEncodeFlux",
                "_meta": {"title": "CLIP Text Encode (FLUX)"},
            },
            "4": {
                "inputs": {
                    "noise": ["5", 0],
                    "guider": ["6", 0],
                    "sampler": ["7", 0],
                    "sigmas": ["8", 0],
                    "latent_image": ["2", 0],
                },
                "class_type": "SamplerCustomAdvanced",
                "_meta": {"title": "SamplerCustomAdvanced"},
            },
            "5": {
                "inputs": {"noise_seed": seed},
                "class_type": "RandomNoise",
                "_meta": {"title": "RandomNoise"},
            },
            "6": {
                "inputs": {"model": ["1", 0], "conditioning": ["3", 0]},
                "class_type": "BasicGuider",
                "_meta": {"title": "BasicGuider"},
            },
            "7": {
                "inputs": {"sampler_name": "euler"},
                "class_type": "KSamplerSelect",
                "_meta": {"title": "KSamplerSelect"},
            },
            "8": {
                "inputs": {
                    "scheduler": "simple",
                    "steps": steps,
                    "denoise": 1.0,
                    "model": ["1", 0],
                },
                "class_type": "BasicScheduler",
                "_meta": {"title": "BasicScheduler"},
            },
            "9": {
                "inputs": {"samples": ["4", 0], "vae": ["1", 2]},
                "class_type": "VAEDecode",
                "_meta": {"title": "VAE Decode"},
            },
            "10": {
                "inputs": {"filename_prefix": "ComfyUI_FLUX", "images": ["9", 0]},
                "class_type": "SaveImage",
                "_meta": {"title": "Save Image"},
            },
        }

        return workflow

    def _build_flux_advertisement_workflow(
        self,
        prompt: str,
        product_type: str = "shoes",
        style: str = "modern advertising",
        **kwargs,
    ) -> Dict[str, Any]:
        """Build a FLUX workflow specifically for advertisement generation."""

        # Enhanced prompt for advertisement with FLUX-specific styling
        enhanced_prompt = f"high-quality commercial advertisement photograph, {style} style, featuring {product_type}, {prompt}, professional studio lighting, clean background, marketing photography, crisp details, vibrant colors, commercial grade"

        # FLUX-optimized settings for advertisements
        flux_ad_defaults = {
            "steps": 25,  # Higher steps for quality
            "guidance": 4.0,  # Higher guidance for better prompt adherence
            "width": 1024,
            "height": 1024,
        }

        # Merge defaults with provided kwargs
        final_kwargs = {**flux_ad_defaults, **kwargs}

        return self._build_flux_workflow(prompt=enhanced_prompt, **final_kwargs)

    def _build_fal_flux_pro_kontext_workflow(
        self,
        prompt: str,
        input_image_path: str = "example.png",
        aspect_ratio: str = "1:1",
        max_quality: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """Build a workflow using fal.ai Flux Pro Kontext for image-to-image."""

        seed = kwargs.get("seed", -1)
        if seed == -1:
            seed = int(uuid.uuid4().int % (2**32))

        workflow = {
            "1": {
                "inputs": {"image": input_image_path},
                "class_type": "LoadImage",
                "_meta": {"title": "Load Input Image"},
            },
            "2": {
                "inputs": {
                    "prompt": prompt,
                    "image": ["1", 0],
                    "aspect_ratio": aspect_ratio,
                    "max_quality": max_quality,
                    "guidance_scale": kwargs.get("guidance_scale", 3.5),
                    "num_images": kwargs.get("num_images", 1),
                    "safety_tolerance": kwargs.get("safety_tolerance", "2"),
                    "output_format": kwargs.get("output_format", "png"),
                    "sync_mode": kwargs.get("sync_mode", False),
                    "seed": seed,
                },
                "class_type": "FluxProKontext_fal",
                "_meta": {"title": "Flux Pro Kontext (fal.ai)"},
            },
            "3": {
                "inputs": {"filename_prefix": "fal_kontext_", "images": ["2", 0]},
                "class_type": "SaveImage",
                "_meta": {"title": "Save Generated Image"},
            },
        }

        return workflow

    def _build_fal_image_edit_workflow(
        self, prompt: str, input_image_path: str = "example.png", **kwargs
    ) -> Dict[str, Any]:
        """Build a workflow using fal.ai QwenImageEdit for image editing."""

        seed = kwargs.get("seed", -1)
        if seed == -1:
            seed = int(uuid.uuid4().int % (2**32))

        workflow = {
            "1": {
                "inputs": {"image": input_image_path},
                "class_type": "LoadImage",
                "_meta": {"title": "Load Input Image"},
            },
            "2": {
                "inputs": {
                    "prompt": prompt,
                    "image": ["1", 0],
                    "image_size": kwargs.get("image_size", "square_hd"),
                    "width": kwargs.get("width", 512),
                    "height": kwargs.get("height", 512),
                    "num_inference_steps": kwargs.get("num_inference_steps", 30),
                    "guidance_scale": kwargs.get("guidance_scale", 4.0),
                    "num_images": kwargs.get("num_images", 1),
                    "enable_safety_checker": kwargs.get("enable_safety_checker", True),
                    "output_format": kwargs.get("output_format", "png"),
                    "acceleration": kwargs.get("acceleration", "none"),
                    "sync_mode": kwargs.get("sync_mode", False),
                    "negative_prompt": kwargs.get("negative_prompt", ""),
                    "seed": seed,
                },
                "class_type": "QwenImageEdit_fal",
                "_meta": {"title": "Qwen Image Edit (fal.ai)"},
            },
            "3": {
                "inputs": {"filename_prefix": "fal_edit_", "images": ["2", 0]},
                "class_type": "SaveImage",
                "_meta": {"title": "Save Edited Image"},
            },
        }

        return workflow

    def _build_fal_flux_text_to_image_workflow(
        self,
        prompt: str,
        aspect_ratio: str = "1:1",
        max_quality: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """Build a workflow using fal.ai Flux Pro 1.1 for text-to-image generation."""

        seed = kwargs.get("seed", -1)
        if seed <= 0:
            seed = int(uuid.uuid4().int % (2**32))

        # Map aspect ratios to image sizes supported by FluxPro11
        aspect_ratio_map = {
            "1:1": "square_hd",
            "4:3": "landscape_4_3",
            "3:4": "portrait_4_3",
            "16:9": "landscape_16_9",
            "9:16": "portrait_16_9",
        }

        image_size = aspect_ratio_map.get(aspect_ratio, "square_hd")

        workflow = {
            "1": {
                "inputs": {
                    "prompt": prompt,
                    "image_size": image_size,
                    "width": kwargs.get("width", 1024),
                    "height": kwargs.get("height", 1024),
                    "num_images": kwargs.get("num_images", 1),
                    "safety_tolerance": kwargs.get("safety_tolerance", "2"),
                    "seed": seed,
                },
                "class_type": "FluxPro11_fal",
                "_meta": {"title": "Flux Pro 1.1 (fal.ai)"},
            },
            "2": {
                "inputs": {"filename_prefix": "fal_flux_pro11_", "images": ["1", 0]},
                "class_type": "SaveImage",
                "_meta": {"title": "Save Generated Image"},
            },
        }

        return workflow

    def _build_advertisement_with_logo_workflow(
        self,
        prompt: str,
        logo_path: str,
        product_type: str = "product",
        style: str = "modern advertising",
        logo_position: str = "bottom right",
        logo_scale: float = 0.15,
        **kwargs,
    ) -> Dict[str, Any]:
        """Build a workflow for advertisement generation with logo overlay."""

        seed = kwargs.get("seed", -1)
        if seed <= 0:
            seed = int(uuid.uuid4().int % (2**32))

        # Copy logo to ComfyUI input directory if it's a local file
        import shutil
        from pathlib import Path

        if logo_path.startswith("/static/"):
            # Convert to local path
            local_logo_path = logo_path[1:]  # Remove leading slash
            if Path(local_logo_path).exists():
                # Copy to ComfyUI input directory
                comfyui_input_dir = Path(
                    "/Users/anurag.dwivedi/work_dir/temp/ComfyUI/input"
                )
                logo_filename = f"logo_{uuid.uuid4().hex[:8]}.png"
                target_path = comfyui_input_dir / logo_filename
                shutil.copy2(local_logo_path, target_path)
                logo_path = logo_filename  # Use just the filename for ComfyUI
                logger.info(f"Copied logo to ComfyUI input: {logo_filename}")

        # Enhanced prompt for advertisement with space for logo
        enhanced_prompt = f"professional {style} advertisement for {product_type}, {prompt}, high quality, commercial photography, studio lighting, clean background, marketing poster, promotional design, space for logo in {logo_position} corner"

        workflow = {
            # Step 1: Generate base advertisement using fal.ai
            "1": {
                "inputs": {
                    "prompt": enhanced_prompt,
                    "image_size": "square_hd",
                    "width": kwargs.get("width", 1024),
                    "height": kwargs.get("height", 1024),
                    "num_inference_steps": kwargs.get("num_inference_steps", 28),
                    "guidance_scale": kwargs.get("guidance_scale", 3.5),
                    "num_images": kwargs.get("num_images", 1),
                    "safety_tolerance": kwargs.get("safety_tolerance", "2"),
                    "seed": seed,
                },
                "class_type": "FluxPro11_fal",
                "_meta": {"title": "Generate Base Advertisement (fal.ai)"},
            },
            # Step 2: Load the logo image
            "2": {
                "inputs": {"image": logo_path},
                "class_type": "LoadImage",
                "_meta": {"title": "Load Brand Logo"},
            },
            # Step 3: Resize logo to appropriate size
            "3": {
                "inputs": {
                    "image": ["2", 0],
                    "width": int(1024 * logo_scale),  # Scale logo relative to ad size
                    "height": int(1024 * logo_scale),
                    "upscale_method": "lanczos",
                    "crop": "disabled",
                },
                "class_type": "ImageScale",
                "_meta": {"title": "Resize Logo"},
            },
            # Step 4: Calculate position for logo placement
            "4": {
                "inputs": {
                    "destination": ["1", 0],  # Base advertisement image
                    "source": ["3", 0],  # Resized logo image
                    "x": 924,  # Position from left (1024-100 for bottom right)
                    "y": 924,  # Position from top (1024-100 for bottom right)
                    "resize_source": False,
                    "mode": "SRC_OVER",
                    "destination_alpha": 1.0,
                    "source_alpha": 0.9,
                },
                "class_type": "PorterDuffImageComposite",
                "_meta": {"title": "Composite Logo onto Advertisement"},
            },
            # Step 5: Save the final advertisement with logo
            "5": {
                "inputs": {"filename_prefix": "ad_with_logo_", "images": ["4", 0]},
                "class_type": "SaveImage",
                "_meta": {"title": "Save Final Advertisement"},
            },
        }

        return workflow

    def _build_mask_and_background_change_workflow(
        self,
        prompt: str,
        input_image_path: str,
        mask_prompt: str = "",
        background_prompt: str = "",
        **kwargs,
    ) -> Dict[str, Any]:
        """Build a workflow for masking objects and changing backgrounds using multiple approaches."""

        seed = kwargs.get("seed", -1)
        if seed <= 0:
            seed = int(uuid.uuid4().int % (2**32))

        # Enhanced prompts for masking and background change
        if not background_prompt:
            background_prompt = prompt

        full_edit_prompt = f"Change background to: {background_prompt}. {mask_prompt if mask_prompt else 'Keep the main subject/object intact.'}"

        workflow = {
            # Step 1: Load the input image
            "1": {
                "inputs": {"image": input_image_path},
                "class_type": "LoadImage",
                "_meta": {"title": "Load Input Image"},
            },
            # Step 2: Use Flux Pro Kontext for intelligent background change
            # This is good for automatic subject detection and background replacement
            "2": {
                "inputs": {
                    "prompt": background_prompt,
                    "image": ["1", 0],
                    "aspect_ratio": kwargs.get("aspect_ratio", "1:1"),
                    "max_quality": kwargs.get("max_quality", True),
                    "guidance_scale": kwargs.get("guidance_scale", 3.5),
                    "num_images": kwargs.get("num_images", 1),
                    "safety_tolerance": kwargs.get("safety_tolerance", "2"),
                    "output_format": kwargs.get("output_format", "png"),
                    "sync_mode": kwargs.get("sync_mode", False),
                    "seed": seed,
                },
                "class_type": "FluxProKontext_fal",
                "_meta": {"title": "Flux Pro Kontext Background Change"},
            },
            # Step 3: Alternative approach using Qwen Image Edit for more precise control
            "3": {
                "inputs": {
                    "prompt": full_edit_prompt,
                    "image": ["1", 0],
                    "image_size": kwargs.get("image_size", "square_hd"),
                    "width": kwargs.get("width", 1024),
                    "height": kwargs.get("height", 1024),
                    "num_inference_steps": kwargs.get("num_inference_steps", 30),
                    "guidance_scale": kwargs.get("qwen_guidance_scale", 4.0),
                    "num_images": 1,
                    "enable_safety_checker": kwargs.get("enable_safety_checker", True),
                    "output_format": kwargs.get("output_format", "png"),
                    "acceleration": kwargs.get("acceleration", "none"),
                    "sync_mode": kwargs.get("sync_mode", False),
                    "negative_prompt": kwargs.get(
                        "negative_prompt", "blurry, low quality, artifacts"
                    ),
                    "seed": seed + 1,  # Different seed for variety
                },
                "class_type": "QwenImageEdit_fal",
                "_meta": {"title": "Qwen Image Edit Alternative"},
            },
            # Step 4: Third approach using SeedEdit for subtle background changes
            "4": {
                "inputs": {
                    "prompt": f"Replace background with {background_prompt}",
                    "image": ["1", 0],
                    "guidance_scale": kwargs.get("seed_guidance_scale", 0.5),
                    "seed": seed + 2,
                },
                "class_type": "SeedEditV3_fal",
                "_meta": {"title": "SeedEdit Background Change"},
            },
            # Step 5: Save Flux Pro Kontext result
            "5": {
                "inputs": {"filename_prefix": "kontext_bg_change_", "images": ["2", 0]},
                "class_type": "SaveImage",
                "_meta": {"title": "Save Kontext Result"},
            },
            # Step 6: Save Qwen Edit result
            "6": {
                "inputs": {"filename_prefix": "qwen_bg_change_", "images": ["3", 0]},
                "class_type": "SaveImage",
                "_meta": {"title": "Save Qwen Result"},
            },
            # Step 7: Save SeedEdit result
            "7": {
                "inputs": {"filename_prefix": "seed_bg_change_", "images": ["4", 0]},
                "class_type": "SaveImage",
                "_meta": {"title": "Save SeedEdit Result"},
            },
        }

        return workflow

    def _build_mask_first_then_edit_workflow(
        self,
        prompt: str,
        input_image_path: str,
        mask_object: str = "bottle",
        background_prompt: str = "",
        **kwargs,
    ) -> Dict[str, Any]:
        """Build an improved workflow that first creates a mask, then uses it for precise editing."""

        seed = kwargs.get("seed", -1)
        if seed <= 0:
            seed = int(uuid.uuid4().int % (2**32))

        if not background_prompt:
            background_prompt = prompt

        # Step 1: Generate mask using Recraft or create manual mask
        mask_prompt = f"segment and extract the {mask_object}, create clean mask"
        edit_prompt = f"Replace background with {background_prompt}, preserve the {mask_object} exactly"

        workflow = {
            # Step 1: Load the input image
            "1": {
                "inputs": {"image": input_image_path},
                "class_type": "LoadImage",
                "_meta": {"title": "Load Input Image"},
            },
            # Step 2: Generate a clean background version using Flux Pro for high quality
            "2": {
                "inputs": {
                    "prompt": f"clean white background, professional product photography of {mask_object}",
                    "image": ["1", 0],
                    "aspect_ratio": kwargs.get("aspect_ratio", "1:1"),
                    "max_quality": kwargs.get("max_quality", True),
                    "guidance_scale": kwargs.get("guidance_scale", 3.5),
                    "num_images": 1,
                    "safety_tolerance": kwargs.get("safety_tolerance", "2"),
                    "output_format": "png",
                    "sync_mode": kwargs.get("sync_mode", False),
                    "seed": seed,
                },
                "class_type": "FluxProKontext_fal",
                "_meta": {"title": "Extract Object to Clean Background"},
            },
            # Step 3: Extract alpha channel as mask from clean background result
            "3": {
                "inputs": {"image": ["2", 0], "channel": "alpha"},
                "class_type": "ImageToMask",
                "_meta": {"title": "Extract Alpha Mask from Clean Background"},
            },
            # Step 4: Use the extracted mask with Qwen Image Edit for precise background replacement
            "4": {
                "inputs": {
                    "prompt": edit_prompt,
                    "image": ["1", 0],  # Original image
                    "mask": ["3", 0],  # Generated mask
                    "image_size": kwargs.get("image_size", "square_hd"),
                    "width": kwargs.get("width", 1024),
                    "height": kwargs.get("height", 1024),
                    "num_inference_steps": kwargs.get("num_inference_steps", 30),
                    "guidance_scale": kwargs.get("qwen_guidance_scale", 4.0),
                    "num_images": 1,
                    "enable_safety_checker": kwargs.get("enable_safety_checker", True),
                    "output_format": "png",
                    "acceleration": kwargs.get("acceleration", "none"),
                    "sync_mode": kwargs.get("sync_mode", False),
                    "negative_prompt": kwargs.get(
                        "negative_prompt",
                        f"remove {mask_object}, delete {mask_object}, blurry, artifacts",
                    ),
                    "seed": seed + 1,
                },
                "class_type": "QwenImageEdit_fal",
                "_meta": {"title": "Masked Background Replacement"},
            },
            # Step 5: Alternative approach - Invert mask for background-only editing
            "5": {
                "inputs": {"mask": ["3", 0]},
                "class_type": "InvertMask",
                "_meta": {"title": "Invert Mask for Background"},
            },
            # Step 6: Use inverted mask for background-only replacement with SeedEdit
            "6": {
                "inputs": {
                    "prompt": f"change background to {background_prompt}, keep foreground object",
                    "image": ["1", 0],
                    "mask": ["5", 0],  # Inverted mask (background only)
                    "guidance_scale": kwargs.get("seed_guidance_scale", 0.7),
                    "seed": seed + 2,
                },
                "class_type": "SeedEditV3_fal",
                "_meta": {"title": "Background-Only Edit with Mask"},
            },
            # Step 7: Composite final result using the original object and new background
            "7": {
                "inputs": {
                    "destination": ["6", 0],  # New background
                    "source": ["2", 0],  # Clean extracted object
                    "x": 0,
                    "y": 0,
                    "resize_source": False,
                    "mode": "SRC_OVER",
                    "destination_alpha": 1.0,
                    "source_alpha": 1.0,
                },
                "class_type": "PorterDuffImageComposite",
                "_meta": {"title": "Composite Object onto New Background"},
            },
            # Step 8: Save the mask for inspection
            "8": {
                "inputs": {"filename_prefix": "extracted_mask_", "images": ["3", 0]},
                "class_type": "SaveImage",
                "_meta": {"title": "Save Generated Mask"},
            },
            # Step 9: Save clean extracted object
            "9": {
                "inputs": {"filename_prefix": "clean_object_", "images": ["2", 0]},
                "class_type": "SaveImage",
                "_meta": {"title": "Save Clean Object"},
            },
            # Step 10: Save masked edit result
            "10": {
                "inputs": {"filename_prefix": "masked_edit_", "images": ["4", 0]},
                "class_type": "SaveImage",
                "_meta": {"title": "Save Masked Edit Result"},
            },
            # Step 11: Save final composite result
            "11": {
                "inputs": {"filename_prefix": "final_composite_", "images": ["7", 0]},
                "class_type": "SaveImage",
                "_meta": {"title": "Save Final Composite"},
            },
        }

        return workflow

    def _build_edit_previous_image_workflow(
        self,
        prompt: str,
        previous_image_path: str = None,
        edit_type: str = "background_change",
        **kwargs,
    ) -> Dict[str, Any]:
        """Build a workflow that edits the previous image from session history."""

        # Import here to avoid circular imports
        from app.agents.voice.automatic.utils.session_context import (
            get_current_session_id,
            get_nth_previous_image,
            get_previous_image,
        )

        seed = kwargs.get("seed", -1)
        if seed <= 0:
            seed = int(uuid.uuid4().int % (2**32))

        # If no previous image path provided, try to get from session history
        if not previous_image_path:
            session_id = get_current_session_id()
            if session_id:
                previous_image = get_previous_image(session_id)
                if previous_image:
                    previous_image_path = previous_image.file_path
                    logger.info(
                        f"Using previous image from session: {previous_image_path}"
                    )
                else:
                    raise ValueError("No previous image found in session history")
            else:
                raise ValueError("No session ID available and no image path provided")

        workflow = {
            # Step 1: Load the previous image
            "1": {
                "inputs": {"image": previous_image_path},
                "class_type": "LoadImage",
                "_meta": {"title": "Load Previous Image"},
            },
            # Step 2: Apply edit based on type
            "2": {
                "inputs": {
                    "prompt": prompt,
                    "image": ["1", 0],
                    "image_size": kwargs.get("image_size", "square_hd"),
                    "width": kwargs.get("width", 1024),
                    "height": kwargs.get("height", 1024),
                    "num_inference_steps": kwargs.get("num_inference_steps", 30),
                    "guidance_scale": kwargs.get("guidance_scale", 4.0),
                    "num_images": 1,
                    "enable_safety_checker": kwargs.get("enable_safety_checker", True),
                    "output_format": kwargs.get("output_format", "png"),
                    "acceleration": kwargs.get("acceleration", "none"),
                    "sync_mode": kwargs.get("sync_mode", False),
                    "negative_prompt": kwargs.get(
                        "negative_prompt", "blurry, low quality, artifacts"
                    ),
                    "seed": seed,
                },
                "class_type": "QwenImageEdit_fal",
                "_meta": {"title": "Edit Previous Image"},
            },
            # Step 3: Alternative edit approach
            "3": {
                "inputs": {
                    "prompt": prompt,
                    "image": ["1", 0],
                    "aspect_ratio": kwargs.get("aspect_ratio", "1:1"),
                    "max_quality": kwargs.get("max_quality", True),
                    "guidance_scale": kwargs.get("kontext_guidance_scale", 3.5),
                    "num_images": 1,
                    "safety_tolerance": kwargs.get("safety_tolerance", "2"),
                    "output_format": kwargs.get("output_format", "png"),
                    "sync_mode": kwargs.get("sync_mode", False),
                    "seed": seed + 1,
                },
                "class_type": "FluxProKontext_fal",
                "_meta": {"title": "Alternative Edit with Kontext"},
            },
            # Step 4: Save original for reference
            "4": {
                "inputs": {
                    "filename_prefix": "previous_image_ref_",
                    "images": ["1", 0],
                },
                "class_type": "SaveImage",
                "_meta": {"title": "Save Previous Image Reference"},
            },
            # Step 5: Save edited result
            "5": {
                "inputs": {"filename_prefix": "edited_previous_", "images": ["2", 0]},
                "class_type": "SaveImage",
                "_meta": {"title": "Save Edited Result"},
            },
            # Step 6: Save alternative result
            "6": {
                "inputs": {"filename_prefix": "alt_edited_", "images": ["3", 0]},
                "class_type": "SaveImage",
                "_meta": {"title": "Save Alternative Edit"},
            },
        }

        return workflow


class ComfyUIService:
    """Service wrapper for ComfyUI client with session management."""

    def __init__(self):
        self._client: Optional[ComfyUIClient] = None

    async def get_client(self) -> ComfyUIClient:
        """Get or create ComfyUI client."""
        if self._client is None:
            self._client = ComfyUIClient()
        return self._client

    async def cleanup(self):
        """Clean up resources."""
        if self._client:
            await self._client.close()
            self._client = None

    async def generate_advertisement_image(
        self, prompt: str, product_type: str = "product", **kwargs
    ) -> Tuple[List[str], Dict[str, Any]]:
        """
        Generate an advertisement image.

        Args:
            prompt: Description of the advertisement
            product_type: Type of product being advertised
            **kwargs: Additional parameters

        Returns:
            Tuple of (image_urls, execution_history)
        """
        client = await self.get_client()
        return await client.generate_image(
            prompt=prompt,
            workflow_template="advertisement",
            product_type=product_type,
            **kwargs,
        )

    async def generate_fal_image_edit(
        self, prompt: str, input_image_path: str = "example.png", **kwargs
    ) -> Tuple[List[str], Dict[str, Any]]:
        """
        Generate an edited image using fal.ai image editing.

        Args:
            prompt: Description of the desired edits
            input_image_path: Path to the input image
            **kwargs: Additional parameters

        Returns:
            Tuple of (image_urls, execution_history)
        """
        client = await self.get_client()
        return await client.generate_image(
            prompt=prompt,
            workflow_template="fal_image_edit",
            input_image_path=input_image_path,
            **kwargs,
        )

    async def generate_fal_flux_kontext(
        self, prompt: str, input_image_path: str = "example.png", **kwargs
    ) -> Tuple[List[str], Dict[str, Any]]:
        """
        Generate an image using fal.ai Flux Pro Kontext for image-to-image.

        Args:
            prompt: Description of the desired image
            input_image_path: Path to the input image
            **kwargs: Additional parameters

        Returns:
            Tuple of (image_urls, execution_history)
        """
        client = await self.get_client()
        return await client.generate_image(
            prompt=prompt,
            workflow_template="fal_flux_pro_kontext",
            input_image_path=input_image_path,
            **kwargs,
        )

    async def generate_fal_text_to_image(
        self, prompt: str, **kwargs
    ) -> Tuple[List[str], Dict[str, Any]]:
        """
        Generate an image using fal.ai Flux Pro text-to-image.

        Args:
            prompt: Description of the desired image
            **kwargs: Additional parameters

        Returns:
            Tuple of (image_urls, execution_history)
        """
        client = await self.get_client()
        return await client.generate_image(
            prompt=prompt, workflow_template="fal_text_to_image", **kwargs
        )

    async def generate_advertisement_with_logo(
        self,
        prompt: str,
        logo_path: str,
        product_type: str = "product",
        style: str = "modern advertising",
        logo_position: str = "bottom right",
        **kwargs,
    ) -> Tuple[List[str], Dict[str, Any]]:
        """
        Generate an advertisement with logo integration using ComfyUI workflow.

        Args:
            prompt: Description of the advertisement
            logo_path: Local path to the logo image file
            product_type: Type of product being advertised
            style: Advertisement style
            logo_position: Where to place the logo (e.g., "bottom right", "bottom center")
            **kwargs: Additional parameters

        Returns:
            Tuple of (image_urls, execution_history)
        """
        # Import here to avoid circular imports
        from app.agents.voice.automatic.utils.session_context import (
            add_image_to_history,
            get_current_session_id,
        )

        client = await self.get_client()

        # Generate advertisement with logo using the integrated workflow
        image_urls, history = await client.generate_image(
            prompt=prompt,
            workflow_template="advertisement_with_logo",
            logo_path=logo_path,
            product_type=product_type,
            style=style,
            logo_position=logo_position,
            **kwargs,
        )

        # Add generated images to session history
        session_id = get_current_session_id()
        if session_id and image_urls:
            for image_url in image_urls:
                # Convert URL to file path (remove /api/v1/images/comfyui?filename= prefix)
                if "filename=" in image_url:
                    filename = image_url.split("filename=")[1].split("&")[0]
                    file_path = (
                        f"/Users/anurag.dwivedi/work_dir/temp/ComfyUI/output/{filename}"
                    )
                else:
                    file_path = image_url

                add_image_to_history(
                    session_id=session_id,
                    file_path=file_path,
                    url=image_url,
                    prompt=prompt,
                    workflow_type="advertisement_with_logo",
                    metadata={
                        "product_type": product_type,
                        "style": style,
                        "logo_position": logo_position,
                        "logo_path": logo_path,
                    },
                )
                logger.info(f"Added advertisement to history: {file_path}")

        return image_urls, history

    async def generate_mask_and_background_change(
        self,
        prompt: str,
        input_image_path: str,
        mask_prompt: str = "",
        background_prompt: str = "",
        **kwargs,
    ) -> Tuple[List[str], Dict[str, Any]]:
        """
        Generate multiple background change variations using different AI models.

        Args:
            prompt: Base description for the background change
            input_image_path: Path to the input image
            mask_prompt: Optional specific instructions for preserving objects
            background_prompt: Specific background description
            **kwargs: Additional parameters

        Returns:
            Tuple of (image_urls, execution_history)
        """
        client = await self.get_client()

        # Generate background change using multiple approaches
        image_urls, history = await client.generate_image(
            prompt=prompt,
            workflow_template="mask_and_background_change",
            input_image_path=input_image_path,
            mask_prompt=mask_prompt,
            background_prompt=background_prompt,
            **kwargs,
        )

        # Add generated images to session history
        from app.agents.voice.automatic.utils.session_context import (
            add_image_to_history,
            get_current_session_id,
        )

        session_id = get_current_session_id()
        if session_id and image_urls:
            for image_url in image_urls:
                # Convert URL to file path
                if "filename=" in image_url:
                    filename = image_url.split("filename=")[1].split("&")[0]
                    file_path = (
                        f"/Users/anurag.dwivedi/work_dir/temp/ComfyUI/output/{filename}"
                    )
                else:
                    file_path = image_url

                add_image_to_history(
                    session_id=session_id,
                    file_path=file_path,
                    url=image_url,
                    prompt=prompt,
                    workflow_type="mask_and_background_change",
                    metadata={
                        "input_image_path": input_image_path,
                        "mask_prompt": mask_prompt,
                        "background_prompt": background_prompt,
                    },
                )

        return image_urls, history

    async def edit_previous_image(
        self,
        prompt: str,
        previous_image_path: str = None,
        edit_type: str = "background_change",
        **kwargs,
    ) -> Tuple[List[str], Dict[str, Any]]:
        """
        Edit the previous image from session history.

        Args:
            prompt: Description of the desired edits
            previous_image_path: Optional specific image path (if None, uses session history)
            edit_type: Type of edit to perform
            **kwargs: Additional parameters

        Returns:
            Tuple of (image_urls, execution_history)
        """
        client = await self.get_client()

        # Generate edited image using previous image workflow
        image_urls, history = await client.generate_image(
            prompt=prompt,
            workflow_template="edit_previous_image",
            previous_image_path=previous_image_path,
            edit_type=edit_type,
            **kwargs,
        )

        # Add generated images to session history
        from app.agents.voice.automatic.utils.session_context import (
            add_image_to_history,
            get_current_session_id,
        )

        session_id = get_current_session_id()
        if session_id and image_urls:
            for image_url in image_urls:
                # Convert URL to file path
                if "filename=" in image_url:
                    filename = image_url.split("filename=")[1].split("&")[0]
                    file_path = (
                        f"/Users/anurag.dwivedi/work_dir/temp/ComfyUI/output/{filename}"
                    )
                else:
                    file_path = image_url

                add_image_to_history(
                    session_id=session_id,
                    file_path=file_path,
                    url=image_url,
                    prompt=prompt,
                    workflow_type="edit_previous_image",
                    metadata={
                        "previous_image_path": previous_image_path,
                        "edit_type": edit_type,
                    },
                )

        return image_urls, history
