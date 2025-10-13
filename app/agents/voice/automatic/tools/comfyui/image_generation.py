"""
ComfyUI image generation tool functions.
"""

import asyncio
import os
import random
import shutil
import subprocess
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List

import aiofiles
import aiohttp
from PIL import Image, ImageOps
from pipecat.services.llm_service import FunctionCallParams

from app.agents.voice.automatic.features.charts.session_storage import (
    register_image_for_navigation,
)
from app.agents.voice.automatic.rtvi.events_store import register_pending_rtvi_event
from app.agents.voice.automatic.services.comfyui.client import ComfyUIService
from app.agents.voice.automatic.utils.image_context import (
    get_current_image,
    get_logo_url,
    has_current_image,
    set_current_image,
    set_logo_url,
)
from app.agents.voice.automatic.utils.logo_context import store_logo_request_context
from app.agents.voice.automatic.utils.session_context import get_current_session_id
from app.core import config
from app.core.logger import logger


async def _convert_image_url_to_file_path(image_url: str) -> str:
    """
    Convert an image URL to a local file path for fal.ai image editing.

    Args:
        image_url: URL of the image (ComfyUI proxy URL or static file URL)

    Returns:
        Local file path that can be used for fal.ai image editing
    """
    try:
        # Handle static file URLs (already saved locally)
        if image_url.startswith("/static/"):
            # Convert to absolute file path
            file_path = Path(image_url[1:])  # Remove leading slash
            if file_path.exists():
                return str(file_path.absolute())

        # Handle ComfyUI proxy URLs - download and save locally
        if image_url.startswith("/api/v1/images/comfyui"):
            # Extract filename from proxy URL and convert to ComfyUI direct URL
            import urllib.parse

            parsed = urllib.parse.urlparse(image_url)
            query_params = urllib.parse.parse_qs(parsed.query)
            filename = query_params.get("filename", [None])[0]
            subfolder = query_params.get("subfolder", [""])[0]

            if filename:
                if subfolder:
                    actual_url = f"http://localhost:8188/view?filename={filename}&subfolder={subfolder}"
                else:
                    actual_url = f"http://localhost:8188/view?filename={filename}"

                # Download and save to temp directory
                temp_dir = Path("temp/image_editing")
                temp_dir.mkdir(parents=True, exist_ok=True)

                temp_file_path = temp_dir / f"edit_input_{uuid.uuid4()}.png"

                async with aiohttp.ClientSession() as session:
                    async with session.get(actual_url) as response:
                        if response.status == 200:
                            image_data = await response.read()
                            async with aiofiles.open(temp_file_path, "wb") as f:
                                await f.write(image_data)

                            logger.info(
                                f"Downloaded image for editing: {temp_file_path}"
                            )
                            return str(temp_file_path.absolute())
                        else:
                            logger.error(
                                f"Failed to download image from {actual_url}: {response.status}"
                            )

        # Handle external URLs - download and save locally
        if image_url.startswith("http"):
            temp_dir = Path("temp/image_editing")
            temp_dir.mkdir(parents=True, exist_ok=True)

            temp_file_path = temp_dir / f"edit_input_{uuid.uuid4()}.png"

            async with aiohttp.ClientSession() as session:
                async with session.get(image_url) as response:
                    if response.status == 200:
                        image_data = await response.read()
                        async with aiofiles.open(temp_file_path, "wb") as f:
                            await f.write(image_data)

                        logger.info(
                            f"Downloaded external image for editing: {temp_file_path}"
                        )
                        return str(temp_file_path.absolute())
                    else:
                        logger.error(
                            f"Failed to download external image from {image_url}: {response.status}"
                        )

        logger.error(f"Could not convert image URL to file path: {image_url}")
        return None

    except Exception as e:
        logger.error(f"Error converting image URL to file path: {e}")
        return None


# Development mode configuration
USE_MOCK_IMAGES = os.getenv("USE_MOCK_IMAGES", "true").lower() == "true"
DEVELOPMENT_MODE = os.getenv("DEVELOPMENT_MODE", "true").lower() == "true"


def _get_mock_image_url() -> str:
    """Return a mock image URL for development."""
    # Use a pool of mock image filenames to simulate different generations
    mock_images = [
        "fal_flux_dev__00004_.png",
    ]
    filename = random.choice(mock_images)
    return f"/api/v1/images/comfyui?filename={filename}"


async def _convert_svg_to_png(svg_path: Path) -> Path:
    """
    Convert SVG file to PNG using ImageMagick convert command.

    Args:
        svg_path: Path to the SVG file

    Returns:
        Path to the converted PNG file
    """
    try:
        # Check if ImageMagick convert is available
        if not shutil.which("convert"):
            logger.error(
                "ImageMagick 'convert' command not found. Cannot process SVG files."
            )
            raise Exception("ImageMagick not available")

        # Create output PNG path
        output_path = svg_path.parent / f"{svg_path.stem}_converted.png"

        # Use ImageMagick to convert SVG to PNG
        # -background transparent preserves transparency
        # -flatten merges layers while preserving alpha channel
        cmd = [
            "convert",
            "-background",
            "transparent",
            "-flatten",
            str(svg_path),
            str(output_path),
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            logger.error(f"ImageMagick conversion failed: {result.stderr}")
            raise Exception(f"ImageMagick conversion failed: {result.stderr}")

        logger.info(f"Successfully converted SVG to PNG: {output_path}")
        return output_path

    except subprocess.TimeoutExpired:
        logger.error("SVG conversion timed out")
        raise Exception("SVG conversion timed out")
    except Exception as e:
        logger.error(f"Error converting SVG to PNG: {e}")
        raise


async def _remove_logo_background(
    logo_image: Image.Image, tolerance: int = 30
) -> Image.Image:
    """
    Remove white/light background from logo image to make it transparent.

    Args:
        logo_image: PIL Image object of the logo
        tolerance: Color tolerance for background removal (0-255)

    Returns:
        Logo image with transparent background
    """
    try:
        # Convert to RGBA if not already
        if logo_image.mode != "RGBA":
            logo_image = logo_image.convert("RGBA")

        # Create a new transparent image with same dimensions
        transparent_logo = Image.new("RGBA", logo_image.size, (0, 0, 0, 0))

        # Get pixel data
        pixels = logo_image.load()
        new_pixels = transparent_logo.load()

        width, height = logo_image.size

        # Process each pixel
        for y in range(height):
            for x in range(width):
                r, g, b, a = pixels[x, y]

                # Check if pixel is close to white (within tolerance)
                is_white = (
                    r >= (255 - tolerance)
                    and g >= (255 - tolerance)
                    and b >= (255 - tolerance)
                )

                if is_white:
                    # Make white pixels transparent
                    new_pixels[x, y] = (r, g, b, 0)
                else:
                    # Keep non-white pixels as they are
                    new_pixels[x, y] = (r, g, b, a)

        logger.info("Successfully removed white background from logo")
        return transparent_logo

    except Exception as e:
        logger.warning(f"Could not remove background from logo: {e}")
        # Return original image if background removal fails
        return logo_image


async def _composite_logo_on_image(
    base_image_url: str, logo_url: str, logo_position: str, width: int, height: int
) -> str:
    """
    Composite a logo onto a base advertisement image.

    Args:
        base_image_url: URL of the base advertisement image (from fal.ai)
        logo_url: URL of the logo image (uploaded by user)
        logo_position: Position to place the logo ("bottom right", "bottom left", "top right", "top left")
        width: Target width of the final image
        height: Target height of the final image

    Returns:
        URL of the composited image (saved to static directory)
    """
    try:
        from io import BytesIO

        # Create output directory
        output_dir = Path("static/uploads/advertisements")
        output_dir.mkdir(parents=True, exist_ok=True)

        # Download base image
        async with aiohttp.ClientSession() as session:
            # Convert ComfyUI proxy URL to actual ComfyUI URL
            if base_image_url.startswith("/api/v1/images/comfyui"):
                # Extract filename from proxy URL and convert to ComfyUI direct URL
                import urllib.parse

                parsed = urllib.parse.urlparse(base_image_url)
                query_params = urllib.parse.parse_qs(parsed.query)
                filename = query_params.get("filename", [None])[0]
                subfolder = query_params.get("subfolder", [""])[0]

                if filename:
                    if subfolder:
                        actual_url = f"http://localhost:8188/view?filename={filename}&subfolder={subfolder}"
                    else:
                        actual_url = f"http://localhost:8188/view?filename={filename}"
                else:
                    logger.error(
                        f"Failed to extract filename from proxy URL: {base_image_url}"
                    )
                    return None
            else:
                actual_url = base_image_url

            # Download base advertisement image
            async with session.get(actual_url) as response:
                if response.status != 200:
                    logger.error(
                        f"Failed to download base image from {actual_url}: {response.status}"
                    )
                    return None
                base_image_data = await response.read()

            # Download logo image
            logo_file_path = None
            if logo_url.startswith("/static/"):
                # Local file path
                logo_file_path = Path(logo_url[1:])  # Remove leading slash
            else:
                # Remote URL - download it
                async with session.get(logo_url) as response:
                    if response.status != 200:
                        logger.error(f"Failed to download logo: {response.status}")
                        return None
                    logo_image_data = await response.read()

                    # Save logo temporarily
                    temp_logo_path = output_dir / f"temp_logo_{uuid.uuid4()}.png"
                    async with aiofiles.open(temp_logo_path, "wb") as f:
                        await f.write(logo_image_data)
                    logo_file_path = temp_logo_path

        # Open images with PIL
        base_image = Image.open(BytesIO(base_image_data))

        if logo_file_path and logo_file_path.exists():
            # Check if the logo is an SVG file
            if logo_file_path.suffix.lower() == ".svg":
                logger.info(f"Detected SVG logo, converting to PNG: {logo_file_path}")
                try:
                    # Convert SVG to PNG
                    converted_png_path = await _convert_svg_to_png(logo_file_path)
                    logo_image = Image.open(converted_png_path)
                    logger.info(
                        f"Successfully loaded converted PNG logo: {converted_png_path}"
                    )
                except Exception as e:
                    logger.error(f"Failed to convert SVG logo: {e}")
                    return None
            else:
                # Regular image file (PNG, JPG, etc.)
                try:
                    logo_image = Image.open(logo_file_path)
                except Exception as e:
                    logger.error(f"Failed to open logo image: {e}")
                    return None
        else:
            logger.error(f"Logo file not found: {logo_file_path}")
            return None

        # Remove white background from logo to make it transparent
        logo_image = await _remove_logo_background(logo_image)

        # Resize base image to target dimensions
        base_image = base_image.resize((width, height), Image.Resampling.LANCZOS)

        # Calculate logo size (10% of image width, maintaining aspect ratio)
        logo_width = int(width * 0.1)
        logo_aspect_ratio = logo_image.height / logo_image.width
        logo_height = int(logo_width * logo_aspect_ratio)

        # Resize logo
        logo_image = logo_image.resize(
            (logo_width, logo_height), Image.Resampling.LANCZOS
        )

        # Convert logo to RGBA if it's not already
        if logo_image.mode != "RGBA":
            logo_image = logo_image.convert("RGBA")

        # Calculate position based on logo_position
        margin = 20  # 20px margin from edges
        if logo_position.lower() == "bottom right":
            x = width - logo_width - margin
            y = height - logo_height - margin
        elif logo_position.lower() == "bottom left":
            x = margin
            y = height - logo_height - margin
        elif logo_position.lower() == "top right":
            x = width - logo_width - margin
            y = margin
        elif logo_position.lower() == "top left":
            x = margin
            y = margin
        else:
            # Default to bottom right
            x = width - logo_width - margin
            y = height - logo_height - margin

        # Convert base image to RGBA for proper compositing
        if base_image.mode != "RGBA":
            base_image = base_image.convert("RGBA")

        # Create a new image for compositing
        composite_image = Image.new("RGBA", (width, height), (255, 255, 255, 0))
        composite_image.paste(base_image, (0, 0))

        # Paste logo with transparency
        composite_image.paste(logo_image, (x, y), logo_image)

        # Convert back to RGB for JPEG output
        final_image = Image.new("RGB", (width, height), (255, 255, 255))
        final_image.paste(
            composite_image, mask=composite_image.split()[-1]
        )  # Use alpha channel as mask

        # Save the composited image
        output_filename = f"advertisement_with_logo_{uuid.uuid4()}.jpg"
        output_path = output_dir / output_filename
        final_image.save(output_path, "JPEG", quality=95)

        # Clean up temporary logo file
        if logo_file_path and logo_file_path.name.startswith("temp_logo_"):
            try:
                logo_file_path.unlink()
            except:
                pass

        # Return the URL for the composited image
        composited_url = f"/static/uploads/advertisements/{output_filename}"
        logger.info(
            f"Successfully composited logo onto advertisement: {composited_url}"
        )
        return composited_url

    except Exception as e:
        logger.error(f"Error compositing logo: {e}")
        return None


# Global ComfyUI service instance
_comfyui_service: ComfyUIService = None


async def _get_comfyui_service() -> ComfyUIService:
    """Get or create ComfyUI service instance."""
    global _comfyui_service
    if _comfyui_service is None:
        _comfyui_service = ComfyUIService()
    return _comfyui_service


async def generate_advertisement_image(params: FunctionCallParams):
    """
    Generate an advertisement image using ComfyUI.

    Extracts parameters from FunctionCallParams and returns result via callback.
    """
    if not config.ENABLE_COMFYUI:
        logger.warning("ComfyUI is disabled in configuration")
        await params.result_callback(
            {
                "success": False,
                "error": "ComfyUI is not enabled. Please enable ENABLE_COMFYUI in your environment configuration.",
                "image_urls": [],
            }
        )
        return

    try:
        # Extract parameters from the function call
        prompt = params.arguments.get("prompt", "")
        product_type = params.arguments.get("product_type", "product")
        style = params.arguments.get("style", "modern advertising")
        logo_url = params.arguments.get("logo_url", None)
        logo_position = params.arguments.get("logo_position", "bottom right")
        width = params.arguments.get("width", 1024)
        height = params.arguments.get("height", 1024)

        logger.info(f"Generating advertisement image: {prompt}")
        logger.info(f"Product type: {product_type}, Style: {style}")

        # Check if logo is provided for advertisement
        if not logo_url:
            # First check if this session already has a logo stored
            session_id = get_current_session_id()
            if session_id:
                existing_logo_url = get_logo_url(session_id)
                if existing_logo_url:
                    logger.info(
                        f"Found existing logo for session {session_id}: {existing_logo_url}"
                    )
                    # Use the existing logo and generate advertisement directly
                    logo_url = existing_logo_url

        # Check if user has already uploaded their own product image
        if not logo_url:
            # First check if user has uploaded their own image - if so, use it directly without asking for logo
            if session_id and has_current_image(session_id):
                current_image_url = get_current_image(session_id)
                logger.info(
                    f"User has already uploaded their own image ({current_image_url}), proceeding without logo request"
                )

                # Update session context to show we're creating advertisement from their uploaded image
                set_current_image(
                    session_id,
                    current_image_url,
                    "advertisement_from_user_image",
                    f"Creating {style} advertisement from user's uploaded image",
                    {
                        "product_type": product_type,
                        "style": style,
                        "prompt": prompt,
                        "user_provided_image": True,
                    },
                )

                # Create UI component to show we're using their uploaded image
                ui_component = {
                    "id": f"user_image_advertisement_{uuid.uuid4()}",
                    "type": "image",
                    "url": current_image_url,
                    "title": f"Your {product_type.title()} Advertisement",
                    "description": f"Using your uploaded image to create a {style} advertisement",
                    "props": {
                        "imageUrl": current_image_url,
                        "imageUrls": [current_image_url],
                        "title": f"Your {product_type.title()} Advertisement",
                        "description": f"Using your uploaded image for {style} advertisement",
                        "productType": product_type,
                        "style": style,
                        "prompt": prompt,
                        "operation": "user_image_advertisement",
                        "user_provided_image": True,
                        "generation_info": {
                            "width": width,
                            "height": height,
                            "workflow": "user_image_advertisement",
                        },
                    },
                }

                logger.info(
                    f"User has uploaded image, now generating advertisement using their image: {current_image_url}"
                )

                # Actually generate an advertisement using the user's uploaded image
                if DEVELOPMENT_MODE and USE_MOCK_IMAGES:
                    logger.info(
                        "Using mock image for development - skipping actual advertisement generation"
                    )
                    # Return mock image URL to save API costs during development
                    generated_image_urls = [_get_mock_image_url()]
                    await asyncio.sleep(1.0)  # Simulate generation time
                else:
                    service = await _get_comfyui_service()

                    # Convert user's image URL to file path for image-to-image generation
                    input_image_path = await _convert_image_url_to_file_path(
                        current_image_url
                    )

                    if input_image_path:
                        logger.info(
                            f"Using user's image for advertisement generation: {input_image_path}"
                        )

                        # Enhanced prompt to create advertisement using their uploaded image
                        enhanced_prompt = f"Create a professional {style} advertisement using this {product_type}. {prompt}. High quality commercial photography, professional marketing design, studio lighting, clean professional background suitable for advertising"

                        try:
                            # Use fal.ai image editing to create advertisement from user's image
                            generated_image_urls, _ = (
                                await service.generate_fal_image_edit(
                                    prompt=enhanced_prompt,
                                    input_image_path=input_image_path,
                                )
                            )
                        except Exception as edit_error:
                            logger.warning(
                                f"Image editing failed, falling back to Flux Kontext: {edit_error}"
                            )
                            # Fallback to Flux Kontext for image-to-image
                            try:
                                generated_image_urls, _ = (
                                    await service.generate_fal_flux_kontext(
                                        prompt=enhanced_prompt,
                                        input_image_path=input_image_path,
                                    )
                                )
                            except Exception as kontext_error:
                                logger.error(
                                    f"Flux Kontext also failed: {kontext_error}"
                                )
                                generated_image_urls = []

                        # Clean up temporary file if it was created
                        if (
                            input_image_path.startswith("/Users/")
                            and "temp/image_editing" in input_image_path
                        ):
                            try:
                                Path(input_image_path).unlink()
                                logger.info(
                                    f"Cleaned up temporary file: {input_image_path}"
                                )
                            except:
                                pass

                    else:
                        logger.warning(
                            "Could not convert user image to file path, falling back to text-to-image"
                        )
                        # Fallback to text-to-image generation
                        enhanced_prompt = f"professional {style} advertisement for {product_type}, {prompt}, high quality, commercial photography, studio lighting, clean background, marketing poster, promotional design"

                        generated_image_urls, _ = (
                            await service.generate_fal_text_to_image(
                                prompt=enhanced_prompt,
                                aspect_ratio="1:1",
                                guidance_scale=7.5,
                                output_format="png",
                            )
                        )

                if generated_image_urls:
                    # Update session context with the newly generated advertisement
                    set_current_image(
                        session_id,
                        generated_image_urls[0],
                        "advertisement_from_user_image",
                        f"Generated {style} advertisement from user's uploaded {product_type}",
                        {
                            "product_type": product_type,
                            "style": style,
                            "prompt": prompt,
                            "user_provided_image": True,
                            "original_user_image": current_image_url,
                        },
                    )

                    logger.info(
                        f"Successfully generated advertisement from user's uploaded image"
                    )

                    # Create UI component for the generated advertisement
                    ui_component = {
                        "id": f"user_image_advertisement_{uuid.uuid4()}",
                        "type": "image",
                        "url": generated_image_urls[0],
                        "title": f"Your {product_type.title()} Advertisement",
                        "description": f"Generated {style} advertisement from your uploaded {product_type}",
                        "props": {
                            "imageUrl": generated_image_urls[0],
                            "imageUrls": generated_image_urls,
                            "title": f"Your {product_type.title()} Advertisement",
                            "description": f"Generated {style} advertisement from your uploaded {product_type}",
                            "productType": product_type,
                            "style": style,
                            "prompt": prompt,
                            "operation": "generate_from_user_image",
                            "user_provided_image": True,
                            "original_user_image": current_image_url,
                            "generation_info": {
                                "width": width,
                                "height": height,
                                "workflow": "advertisement_from_user_image",
                            },
                        },
                    }

                    # Register RTVI event to show the generated advertisement
                    register_pending_rtvi_event(
                        session_id, "ui-component", ui_component
                    )
                    register_image_for_navigation(session_id, ui_component)
                    logger.info(
                        f"Registered generated advertisement RTVI event for session {session_id}"
                    )

                    result = {
                        "success": True,
                        "message": f"Perfect! I've created a {style} advertisement using your uploaded {product_type}. The ad incorporates your product beautifully!",
                        "image_urls": generated_image_urls,
                        "user_provided_image": True,
                        "original_user_image": current_image_url,
                        "generation_info": {
                            "width": width,
                            "height": height,
                            "workflow": "advertisement_from_user_image",
                        },
                    }
                    await params.result_callback(result)
                    return
                else:
                    logger.warning(
                        "Failed to generate advertisement from user's uploaded image"
                    )
                    # Fall back to just showing their uploaded image with a message
                    result = {
                        "success": False,
                        "error": f"I couldn't generate the advertisement right now, but I have your {product_type} image ready. Please try again or let me know if you'd like to make any edits.",
                        "image_urls": [current_image_url],
                        "user_provided_image": True,
                    }
                    await params.result_callback(result)
                    return

            logger.info(
                "No logo provided, no existing logo found, and no user uploaded image - requesting logo upload for advertisement"
            )

            # Create UI component for logo upload request
            ui_component = {
                "type": "logo_upload_request",
                "props": {
                    "action_required": "upload_logo",
                    "message": f"I'd love to create a {style} advertisement for {product_type}! Please upload your brand logo. After uploading, ask me to show you the advertisement!",
                    "upload_endpoint": "/api/v1/upload/logo",
                    "continue_with": "generate_advertisement_with_logo",
                    "prompt": prompt,
                    "product_type": product_type,
                    "style": style,
                    "logo_position": logo_position,
                    "title": f"{product_type.title()} Advertisement - Logo Required",
                    "description": "Upload your brand logo, then ask me to show you the result",
                    "session_id": session_id,  # Include session ID for auto-continuation
                },
            }

            # Get session ID and store context + register RTVI event for logo upload request
            session_id = get_current_session_id()
            if session_id:
                # Store the original request context for auto-continuation after logo upload
                logo_request_context = {
                    "prompt": prompt,
                    "product_type": product_type,
                    "style": style,
                    "logo_position": logo_position,
                    "width": width,
                    "height": height,
                }
                store_logo_request_context(session_id, logo_request_context)

                logo_event_payload = {
                    "action_required": "upload_logo",
                    "message": f"I'd love to create a {style} advertisement for {product_type}! Please upload your brand logo. After uploading, ask me to show you the advertisement!",
                    "upload_endpoint": "/api/v1/upload/logo",
                    "session_id": session_id,  # Include session ID for auto-continuation
                    "continue_with": "generate_advertisement_with_logo",
                    "prompt": prompt,
                    "product_type": product_type,
                    "style": style,
                    "logo_position": logo_position,
                    "title": f"{product_type.title()} Advertisement - Logo Required",
                    "description": "Upload your brand logo, then ask me to show you the result",
                }

                register_pending_rtvi_event(
                    session_id, "logo-upload-request", logo_event_payload
                )
                logger.info(
                    f"Stored context and registered RTVI logo upload request for session {session_id}"
                )

            await params.result_callback(
                {
                    "success": False,
                    "action_required": "upload_logo",
                    "message": f"I'd love to create a {style} advertisement for {product_type}! Please upload your brand logo. After uploading, ask me to show you the advertisement!",
                    "upload_endpoint": "/api/v1/upload/logo",
                    "continue_with": "generate_advertisement_with_logo",
                    "prompt": prompt,
                    "product_type": product_type,
                    "style": style,
                    "logo_position": logo_position,
                }
            )
            return

        # If we have a logo (either provided or found existing), generate advertisement with logo
        if logo_url:
            logger.info(
                f"Logo available ({logo_url}), generating advertisement with logo"
            )

            # Create mock function call params to call generate_advertisement_with_logo
            class MockFunctionCallParams:
                def __init__(self, arguments):
                    self.arguments = arguments
                    self.result = None

                async def result_callback(self, result):
                    self.result = result

            # Prepare arguments for logo advertisement generation
            logo_args = {
                "prompt": prompt,
                "product_type": product_type,
                "style": style,
                "logo_url": logo_url,
                "logo_position": logo_position,
                "width": width,
                "height": height,
            }

            # Call generate_advertisement_with_logo function
            mock_params = MockFunctionCallParams(logo_args)
            await generate_advertisement_with_logo(mock_params)

            # Pass through the result
            await params.result_callback(mock_params.result)
            return

        # Check if we should use mock images for development
        if DEVELOPMENT_MODE and USE_MOCK_IMAGES:
            logger.info("Using mock image for development - skipping fal.ai API call")
            # Return mock image URL to save API costs during development
            image_urls = [_get_mock_image_url()]
            await asyncio.sleep(0.5)  # Simulate brief generation time
        else:
            service = await _get_comfyui_service()

            # Use fal.ai for advertisement generation (high quality)
            enhanced_prompt = f"professional {style} advertisement for {product_type}, {prompt}, high quality, commercial photography, studio lighting, clean background, marketing poster, promotional design"

            # Generate the image using fal.ai Flux Dev text-to-image
            image_urls, _ = await service.generate_fal_text_to_image(
                prompt=enhanced_prompt,
                aspect_ratio="1:1",
                guidance_scale=7.5,
                output_format="png",
            )

        if image_urls:
            # Update session context with new image
            session_id = get_current_session_id()
            if session_id:
                set_current_image(
                    session_id,
                    image_urls[0],
                    "generate_advertisement",
                    f"Generated {style} advertisement for {product_type}",
                    {"product_type": product_type, "style": style, "prompt": prompt},
                )

            logger.info(
                f"Successfully generated {len(image_urls)} advertisement image(s)"
            )

            # Create UI component for RTVI event
            ui_component = {
                "id": f"advertisement_{uuid.uuid4()}",
                "type": "image",
                "url": image_urls[0] if image_urls else None,  # Add URL for navigation
                "title": f"{product_type.title()} Advertisement",  # Add title for navigation
                "description": f"Generated {style} advertisement for {product_type}",  # Add description for navigation
                "props": {
                    "imageUrl": image_urls[0] if image_urls else None,
                    "imageUrls": image_urls,
                    "title": f"{product_type.title()} Advertisement",
                    "description": f"Generated {style} advertisement for {product_type}",
                    "productType": product_type,
                    "style": style,
                    "prompt": prompt,
                    "operation": "generate",  # Add operation for navigation
                    "generation_info": {
                        "width": width,
                        "height": height,
                        "workflow": "advertisement",
                    },
                },
            }

            # Register RTVI event to send images to frontend
            session_id = get_current_session_id()
            if session_id:
                register_pending_rtvi_event(session_id, "ui-component", ui_component)
                # Also register with unified navigator for navigation
                register_image_for_navigation(session_id, ui_component)
                logger.info(
                    f"Registered image display RTVI event and navigation for session {session_id}"
                )

            result = {
                "success": True,
                "message": f"Successfully generated advertisement for {product_type}",
                "image_urls": image_urls,
                "generation_info": {
                    "width": width,
                    "height": height,
                    "workflow": "advertisement",
                },
            }
            await params.result_callback(result)
        else:
            logger.warning("No images were generated")
            await params.result_callback(
                {
                    "success": False,
                    "error": "No images were generated. Please check ComfyUI server status and workflow configuration.",
                    "image_urls": [],
                }
            )

    except asyncio.TimeoutError:
        logger.error("ComfyUI generation timeout")
        await params.result_callback(
            {
                "success": False,
                "error": "Image generation timed out. Please try again or use simpler parameters.",
                "image_urls": [],
            }
        )
    except Exception as e:
        logger.error(f"Error generating advertisement image: {e}")
        await params.result_callback(
            {
                "success": False,
                "error": f"Failed to generate image: {str(e)}",
                "image_urls": [],
            }
        )


async def generate_custom_image(params: FunctionCallParams):
    """
    Generate a custom image using ComfyUI with advanced parameters.

    Extracts parameters from FunctionCallParams and returns result via callback.
    """
    if not config.ENABLE_COMFYUI:
        logger.warning("ComfyUI is disabled in configuration")
        await params.result_callback(
            {
                "success": False,
                "error": "ComfyUI is not enabled. Please enable ENABLE_COMFYUI in your environment configuration.",
                "image_urls": [],
            }
        )
        return

    try:
        # Extract parameters from the function call
        prompt = params.arguments.get("prompt", "")
        negative_prompt = params.arguments.get(
            "negative_prompt", "text, watermark, low quality, worst quality"
        )
        width = params.arguments.get("width", 1024)
        height = params.arguments.get("height", 1024)
        steps = params.arguments.get("steps", 20)
        cfg = params.arguments.get("cfg", 8.0)
        sampler_name = params.arguments.get("sampler_name", "euler")

        logger.info(f"Generating custom image: {prompt}")
        logger.info(f"Parameters: {width}x{height}, steps={steps}, cfg={cfg}")

        # Check if we should use mock images for development
        if DEVELOPMENT_MODE and USE_MOCK_IMAGES:
            logger.info("Using mock image for development - skipping fal.ai API call")
            # Return mock image URL to save API costs during development
            image_urls = [_get_mock_image_url()]
            await asyncio.sleep(0.5)  # Simulate brief generation time
        else:
            service = await _get_comfyui_service()

            # Use fal.ai for custom image generation (more reliable than local models)
            # Map dimensions to aspect ratio
            if width == height:
                aspect_ratio = "1:1"
            elif width > height:
                if width / height > 1.7:
                    aspect_ratio = "16:9"
                else:
                    aspect_ratio = "4:3"
            else:
                if height / width > 1.7:
                    aspect_ratio = "9:16"
                else:
                    aspect_ratio = "3:4"

            # Generate the image using fal.ai Flux Pro text-to-image
            image_urls, _ = await service.generate_fal_text_to_image(
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                max_quality=False,  # Standard quality for custom images
                guidance_scale=cfg if cfg <= 20.0 else 7.5,  # Clamp guidance scale
                output_format="png",
            )

        if image_urls:
            # Update session context with new image
            session_id = get_current_session_id()
            if session_id:
                set_current_image(
                    session_id,
                    image_urls[0],
                    "generate_custom_image",
                    f"Generated custom image: {prompt}",
                    {
                        "prompt": prompt,
                        "width": width,
                        "height": height,
                        "steps": steps,
                        "cfg": cfg,
                    },
                )

            logger.info(f"Successfully generated {len(image_urls)} custom image(s)")

            # Create UI component for RTVI event
            ui_component = {
                "id": f"custom_image_{uuid.uuid4()}",
                "type": "image",
                "url": image_urls[0] if image_urls else None,
                "title": "Custom Generated Image",
                "description": f"Generated image: {prompt}",
                "props": {
                    "imageUrl": image_urls[0] if image_urls else None,
                    "imageUrls": image_urls,
                    "title": "Custom Generated Image",
                    "description": f"Generated image: {prompt}",
                    "prompt": prompt,
                    "operation": "generate",
                    "generation_info": {
                        "width": width,
                        "height": height,
                        "steps": steps,
                        "cfg": cfg,
                        "sampler": sampler_name,
                        "workflow": "text_to_image",
                    },
                },
            }

            # Register RTVI event to send images to frontend
            session_id = get_current_session_id()
            if session_id:
                register_pending_rtvi_event(session_id, "ui-component", ui_component)
                # Also register with unified navigator for navigation
                register_image_for_navigation(session_id, ui_component)
                logger.info(
                    f"Registered custom image display RTVI event and navigation for session {session_id}"
                )

            result = {
                "success": True,
                "message": f"Successfully generated custom image",
                "image_urls": image_urls,
                "generation_info": {
                    "width": width,
                    "height": height,
                    "steps": steps,
                    "cfg": cfg,
                    "sampler": sampler_name,
                    "workflow": "text_to_image",
                },
            }
            await params.result_callback(result)
        else:
            logger.warning("No images were generated")
            await params.result_callback(
                {
                    "success": False,
                    "error": "No images were generated. Please check ComfyUI server status and workflow configuration.",
                    "image_urls": [],
                }
            )

    except asyncio.TimeoutError:
        logger.error("ComfyUI generation timeout")
        await params.result_callback(
            {
                "success": False,
                "error": "Image generation timed out. Please try again or use simpler parameters.",
                "image_urls": [],
            }
        )
    except Exception as e:
        logger.error(f"Error generating custom image: {e}")
        await params.result_callback(
            {
                "success": False,
                "error": f"Failed to generate image: {str(e)}",
                "image_urls": [],
            }
        )


async def generate_advertisement_with_logo(params: FunctionCallParams):
    """
    Generate an advertisement image with logo integration using ComfyUI.

    This function is called after the user uploads a logo.
    """
    if not config.ENABLE_COMFYUI:
        logger.warning("ComfyUI is disabled in configuration")
        await params.result_callback(
            {
                "success": False,
                "error": "ComfyUI is not enabled. Please enable ENABLE_COMFYUI in your environment configuration.",
                "image_urls": [],
            }
        )
        return

    try:
        # Extract parameters from the function call
        prompt = params.arguments.get("prompt", "")
        product_type = params.arguments.get("product_type", "product")
        style = params.arguments.get("style", "modern advertising")
        logo_url = params.arguments.get("logo_url", None)
        logo_position = params.arguments.get("logo_position", "bottom right")
        width = params.arguments.get("width", 1024)
        height = params.arguments.get("height", 1024)

        # Get session ID for context management
        session_id = get_current_session_id()

        if not logo_url:
            # First check if user has uploaded their own image - if so, use it directly without asking for logo
            if session_id and has_current_image(session_id):
                current_image_url = get_current_image(session_id)
                logger.info(
                    f"User has already uploaded their own image ({current_image_url}), proceeding without logo request for advertisement with logo"
                )

                # Update session context to show we're using their uploaded image for advertisement
                set_current_image(
                    session_id,
                    current_image_url,
                    "advertisement_with_user_image",
                    f"Creating {style} advertisement with user's uploaded {product_type} image",
                    {
                        "product_type": product_type,
                        "style": style,
                        "prompt": prompt,
                        "user_provided_image": True,
                        "logo_position": logo_position,
                    },
                )

                # Create UI component to show we're using their uploaded image
                ui_component = {
                    "id": f"user_image_advertisement_with_logo_{uuid.uuid4()}",
                    "type": "image",
                    "url": current_image_url,
                    "title": f"Your {product_type.title()} Advertisement",
                    "description": f"Using your uploaded {product_type} image for {style} advertisement",
                    "props": {
                        "imageUrl": current_image_url,
                        "imageUrls": [current_image_url],
                        "title": f"Your {product_type.title()} Advertisement",
                        "description": f"Using your uploaded {product_type} image for {style} advertisement",
                        "productType": product_type,
                        "style": style,
                        "prompt": prompt,
                        "operation": "user_image_advertisement",
                        "user_provided_image": True,
                        "generation_info": {
                            "width": width,
                            "height": height,
                            "workflow": "advertisement_with_user_image",
                            "logo_position": logo_position,
                        },
                    },
                }

                logger.info(
                    f"User has uploaded image, now generating advertisement with logo using their image: {current_image_url}"
                )

                # Actually generate an advertisement using the user's uploaded image
                if DEVELOPMENT_MODE and USE_MOCK_IMAGES:
                    logger.info(
                        "Using mock image for development - skipping actual advertisement generation"
                    )
                    # Return mock image URL to save API costs during development
                    generated_image_urls = [_get_mock_image_url()]
                    await asyncio.sleep(1.0)  # Simulate generation time
                else:
                    service = await _get_comfyui_service()

                    # Convert user's image URL to file path for image-to-image generation
                    input_image_path = await _convert_image_url_to_file_path(
                        current_image_url
                    )

                    if input_image_path:
                        logger.info(
                            f"Using user's image for advertisement generation: {input_image_path}"
                        )

                        # Enhanced prompt to create advertisement using their uploaded image
                        enhanced_prompt = f"Create a professional {style} advertisement using this {product_type}. {prompt}. High quality commercial photography, professional marketing design, studio lighting, clean professional background suitable for advertising"

                        try:
                            # Use fal.ai image editing to create advertisement from user's image
                            generated_image_urls, _ = (
                                await service.generate_fal_image_edit(
                                    prompt=enhanced_prompt,
                                    input_image_path=input_image_path,
                                )
                            )
                        except Exception as edit_error:
                            logger.warning(
                                f"Image editing failed, falling back to Flux Kontext: {edit_error}"
                            )
                            # Fallback to Flux Kontext for image-to-image
                            try:
                                generated_image_urls, _ = (
                                    await service.generate_fal_flux_kontext(
                                        prompt=enhanced_prompt,
                                        input_image_path=input_image_path,
                                    )
                                )
                            except Exception as kontext_error:
                                logger.error(
                                    f"Flux Kontext also failed: {kontext_error}"
                                )
                                generated_image_urls = []

                        # Clean up temporary file if it was created
                        if (
                            input_image_path.startswith("/Users/")
                            and "temp/image_editing" in input_image_path
                        ):
                            try:
                                Path(input_image_path).unlink()
                                logger.info(
                                    f"Cleaned up temporary file: {input_image_path}"
                                )
                            except:
                                pass

                    else:
                        logger.warning(
                            "Could not convert user image to file path, falling back to text-to-image"
                        )
                        # Fallback to text-to-image generation
                        enhanced_prompt = f"professional {style} advertisement for {product_type}, {prompt}, high quality, commercial photography, studio lighting, clean background, marketing poster, promotional design"

                        generated_image_urls, _ = (
                            await service.generate_fal_text_to_image(
                                prompt=enhanced_prompt,
                                aspect_ratio="1:1",
                                guidance_scale=7.5,
                                output_format="png",
                            )
                        )

                if generated_image_urls:
                    # Update session context with the newly generated advertisement
                    set_current_image(
                        session_id,
                        generated_image_urls[0],
                        "advertisement_with_user_image",
                        f"Generated {style} advertisement from user's uploaded {product_type}",
                        {
                            "product_type": product_type,
                            "style": style,
                            "prompt": prompt,
                            "user_provided_image": True,
                            "original_user_image": current_image_url,
                            "logo_position": logo_position,
                        },
                    )

                    logger.info(
                        f"Successfully generated advertisement from user's uploaded image"
                    )

                    # Create UI component for the generated advertisement
                    ui_component = {
                        "id": f"user_image_advertisement_with_logo_{uuid.uuid4()}",
                        "type": "image",
                        "url": generated_image_urls[0],
                        "title": f"Your {product_type.title()} Advertisement",
                        "description": f"Generated {style} advertisement from your uploaded {product_type}",
                        "props": {
                            "imageUrl": generated_image_urls[0],
                            "imageUrls": generated_image_urls,
                            "title": f"Your {product_type.title()} Advertisement",
                            "description": f"Generated {style} advertisement from your uploaded {product_type}",
                            "productType": product_type,
                            "style": style,
                            "prompt": prompt,
                            "operation": "generate_from_user_image",
                            "user_provided_image": True,
                            "original_user_image": current_image_url,
                            "generation_info": {
                                "width": width,
                                "height": height,
                                "workflow": "advertisement_with_user_image",
                                "logo_position": logo_position,
                            },
                        },
                    }

                    # Register RTVI event to show the generated advertisement
                    register_pending_rtvi_event(
                        session_id, "ui-component", ui_component
                    )
                    register_image_for_navigation(session_id, ui_component)
                    logger.info(
                        f"Registered generated advertisement RTVI event for session {session_id}"
                    )

                    result = {
                        "success": True,
                        "message": f"Perfect! I've created a {style} advertisement using your uploaded {product_type}. The ad incorporates your product beautifully!",
                        "image_urls": generated_image_urls,
                        "user_provided_image": True,
                        "original_user_image": current_image_url,
                        "generation_info": {
                            "width": width,
                            "height": height,
                            "workflow": "advertisement_with_user_image",
                            "logo_position": logo_position,
                        },
                    }
                    await params.result_callback(result)
                    return
                else:
                    logger.warning(
                        "Failed to generate advertisement from user's uploaded image"
                    )
                    # Fall back to just showing their uploaded image with a message
                    result = {
                        "success": False,
                        "error": f"I couldn't generate the advertisement right now, but I have your {product_type} image ready. Please try again or let me know if you'd like to make any edits.",
                        "image_urls": [current_image_url],
                        "user_provided_image": True,
                    }
                    await params.result_callback(result)
                    return

            # Check if there's an existing logo for this session
            if session_id:
                existing_logo_url = get_logo_url(session_id)
                if existing_logo_url:
                    logger.info(
                        f"Found existing logo for session {session_id}: {existing_logo_url}"
                    )
                    logo_url = existing_logo_url

            if not logo_url:
                # Need to request logo upload
                logger.info(
                    "Logo URL not provided, no user image found, triggering logo upload workflow"
                )

                # Create UI component for logo upload request
                ui_component = {
                    "type": "logo_upload_request",
                    "props": {
                        "action_required": "upload_logo",
                        "message": f"I'd love to create a {style} advertisement for {product_type}! Please upload your brand logo. After uploading, ask me to show you the advertisement!",
                        "upload_endpoint": "/api/v1/upload/logo",
                        "continue_with": "generate_advertisement_with_logo",
                        "prompt": prompt,
                        "product_type": product_type,
                        "style": style,
                        "logo_position": logo_position,
                        "title": f"{product_type.title()} Advertisement - Logo Required",
                        "description": "Upload your brand logo, then ask me to show you the result",
                        "session_id": session_id,
                    },
                }

                # Store the original request context for auto-continuation after logo upload
                if session_id:
                    logo_request_context = {
                        "prompt": prompt,
                        "product_type": product_type,
                        "style": style,
                        "logo_position": logo_position,
                        "width": width,
                        "height": height,
                    }
                    store_logo_request_context(session_id, logo_request_context)

                    logo_event_payload = {
                        "action_required": "upload_logo",
                        "message": f"I'd love to create a {style} advertisement for {product_type}! Please upload your brand logo. After uploading, ask me to show you the advertisement!",
                        "upload_endpoint": "/api/v1/upload/logo",
                        "session_id": session_id,
                        "continue_with": "generate_advertisement_with_logo",
                        "prompt": prompt,
                        "product_type": product_type,
                        "style": style,
                        "logo_position": logo_position,
                        "title": f"{product_type.title()} Advertisement - Logo Required",
                        "description": "Upload your brand logo, then ask me to show you the result",
                    }

                    register_pending_rtvi_event(
                        session_id, "logo-upload-request", logo_event_payload
                    )
                    logger.info(
                        f"Stored context and registered RTVI logo upload request for session {session_id}"
                    )

                await params.result_callback(
                    {
                        "success": False,
                        "action_required": "upload_logo",
                        "message": f"I'd love to create a {style} advertisement for {product_type}! Please upload your brand logo. After uploading, ask me to show you the advertisement!",
                        "upload_endpoint": "/api/v1/upload/logo",
                        "continue_with": "generate_advertisement_with_logo",
                        "prompt": prompt,
                        "product_type": product_type,
                        "style": style,
                        "logo_position": logo_position,
                    }
                )
                return

        logger.info(f"Generating advertisement with logo: {prompt}")
        logger.info(f"Product type: {product_type}, Style: {style}, Logo: {logo_url}")

        # Check if we should use mock images for development
        if DEVELOPMENT_MODE and USE_MOCK_IMAGES:
            logger.info("Using mock image for development - skipping logo integration")
            # Return mock image URL to save API costs during development
            image_urls = [_get_mock_image_url()]
            await asyncio.sleep(
                1.0
            )  # Simulate longer generation time for logo integration
        else:
            # Use simplified fal.ai approach instead of complex ComfyUI workflow
            service = await _get_comfyui_service()

            # Step 1: Generate base advertisement using fal.ai
            enhanced_prompt = f"professional {style} advertisement for {product_type}, {prompt}, high quality, commercial photography, studio lighting, clean background, marketing poster, promotional design, space for logo in {logo_position} corner"

            logger.info("Generating base advertisement with fal.ai")
            base_image_urls, _ = await service.generate_fal_text_to_image(
                prompt=enhanced_prompt,
                aspect_ratio="1:1",
                guidance_scale=7.5,
                output_format="png",
            )

            if base_image_urls:
                # Step 2: Composite logo onto the base advertisement
                logger.info("Compositing logo onto base advertisement")
                try:
                    composited_image_url = await _composite_logo_on_image(
                        base_image_urls[0], logo_url, logo_position, width, height
                    )
                    if composited_image_url:
                        image_urls = [composited_image_url]
                        logger.info("Successfully composited logo onto advertisement")
                    else:
                        logger.warning(
                            "Logo compositing failed, using base advertisement"
                        )
                        image_urls = base_image_urls
                except Exception as e:
                    logger.error(f"Error during logo compositing: {e}")
                    logger.info("Using base advertisement without logo")
                    image_urls = base_image_urls
            else:
                logger.error("Failed to generate base advertisement")
                image_urls = []

        if image_urls:
            # Update session context with new image
            session_id = get_current_session_id()
            if session_id:
                set_current_image(
                    session_id,
                    image_urls[0],
                    "generate_advertisement_with_logo",
                    f"Generated {style} advertisement for {product_type} with logo",
                    {
                        "product_type": product_type,
                        "style": style,
                        "logo_position": logo_position,
                        "prompt": prompt,
                    },
                )
                # Also store the logo URL for future reference
                set_logo_url(session_id, logo_url)

            logger.info(
                f"Successfully generated {len(image_urls)} advertisement(s) with logo"
            )

            # Create UI component for RTVI event
            ui_component = {
                "id": f"advertisement_logo_{uuid.uuid4()}",
                "type": "image",
                "url": image_urls[0] if image_urls else None,
                "title": f"{product_type.title()} Advertisement with Logo",
                "description": f"Generated {style} advertisement for {product_type} with brand logo",
                "props": {
                    "imageUrl": image_urls[0] if image_urls else None,
                    "imageUrls": image_urls,
                    "title": f"{product_type.title()} Advertisement with Logo",
                    "description": f"Generated {style} advertisement for {product_type} with brand logo",
                    "productType": product_type,
                    "style": style,
                    "prompt": prompt,
                    "hasLogo": True,
                    "logoPosition": logo_position,
                    "operation": "generate_with_logo",
                    "generation_info": {
                        "width": width,
                        "height": height,
                        "workflow": "advertisement_with_logo",
                        "logo_position": logo_position,
                    },
                },
            }

            # Register RTVI event to send images to frontend
            session_id = get_current_session_id()
            if session_id:
                register_pending_rtvi_event(session_id, "ui-component", ui_component)
                # Also register with unified navigator for navigation
                register_image_for_navigation(session_id, ui_component)
                logger.info(
                    f"Registered advertisement with logo display RTVI event and navigation for session {session_id}"
                )

            result = {
                "success": True,
                "message": f"Successfully generated advertisement for {product_type} with your brand logo",
                "image_urls": image_urls,
                "ui_component": ui_component,
                "generation_info": {
                    "width": width,
                    "height": height,
                    "workflow": "advertisement_with_logo",
                    "logo_position": logo_position,
                },
            }
            await params.result_callback(result)
        else:
            logger.warning("No images with logo were generated")
            await params.result_callback(
                {
                    "success": False,
                    "error": "No advertisement with logo was generated. Please check ComfyUI server status and logo file.",
                    "image_urls": [],
                }
            )

    except asyncio.TimeoutError:
        logger.error("ComfyUI logo advertisement generation timeout")
        await params.result_callback(
            {
                "success": False,
                "error": "Advertisement with logo generation timed out. Please try again.",
                "image_urls": [],
            }
        )
    except Exception as e:
        logger.error(f"Error generating advertisement with logo: {e}")
        await params.result_callback(
            {
                "success": False,
                "error": f"Failed to generate advertisement with logo: {str(e)}",
                "image_urls": [],
            }
        )


async def edit_image_background(params: FunctionCallParams):
    """
    Edit the background of the current working image.

    Uses the current image from session context and applies background changes.
    """
    if not config.ENABLE_COMFYUI:
        logger.warning("ComfyUI is disabled in configuration")
        await params.result_callback(
            {
                "success": False,
                "error": "ComfyUI is not enabled. Please enable ENABLE_COMFYUI in your environment configuration.",
                "image_urls": [],
            }
        )
        return

    try:
        # Extract parameters
        new_background = params.arguments.get("background_description", "")
        if not new_background:
            await params.result_callback(
                {
                    "success": False,
                    "error": "Background description is required for editing.",
                    "image_urls": [],
                }
            )
            return

        # Get current session and image
        session_id = get_current_session_id()
        if not session_id:
            await params.result_callback(
                {
                    "success": False,
                    "error": "No active session found.",
                    "image_urls": [],
                }
            )
            return

        current_image_url = get_current_image(session_id)
        if not current_image_url:
            await params.result_callback(
                {
                    "success": False,
                    "error": "No current image found. Please generate an image first.",
                    "image_urls": [],
                }
            )
            return

        logger.info(f"Editing background of current image: {current_image_url}")
        logger.info(f"New background: {new_background}")

        # Check if we should use mock images for development
        if DEVELOPMENT_MODE and USE_MOCK_IMAGES:
            logger.info("Using mock image for development - skipping fal.ai API call")
            image_urls = [_get_mock_image_url()]
            await asyncio.sleep(1.0)  # Simulate generation time
        else:
            service = await _get_comfyui_service()

            # Convert current image URL to file path for fal.ai image editing
            input_image_path = await _convert_image_url_to_file_path(current_image_url)

            if input_image_path:
                logger.info(
                    f"Using image-to-image editing with input: {input_image_path}"
                )

                # Use fal.ai image editing for background replacement
                enhanced_prompt = f"change the background to {new_background}, keep the main subject, professional quality"

                try:
                    image_urls, _ = await service.generate_fal_image_edit(
                        prompt=enhanced_prompt, input_image_path=input_image_path
                    )
                except Exception as edit_error:
                    logger.warning(
                        f"Image editing failed, falling back to Flux Kontext: {edit_error}"
                    )
                    # Fallback to Flux Kontext for image-to-image
                    try:
                        image_urls, _ = await service.generate_fal_flux_kontext(
                            prompt=enhanced_prompt, input_image_path=input_image_path
                        )
                    except Exception as kontext_error:
                        logger.error(f"Flux Kontext also failed: {kontext_error}")
                        image_urls = []

                # Clean up temporary file if it was created
                if (
                    input_image_path.startswith("/Users/")
                    and "temp/image_editing" in input_image_path
                ):
                    try:
                        Path(input_image_path).unlink()
                        logger.info(f"Cleaned up temporary file: {input_image_path}")
                    except:
                        pass

            else:
                logger.warning(
                    "Could not convert image URL to file path, falling back to text-to-image"
                )
                # Fallback to text-to-image with modified prompt
                enhanced_prompt = f"professional advertisement with {new_background} background, high quality, commercial photography"

                image_urls, _ = await service.generate_fal_text_to_image(
                    prompt=enhanced_prompt,
                    aspect_ratio="1:1",
                    guidance_scale=7.5,
                    output_format="png",
                )

        if image_urls:
            # Update session context with edited image
            set_current_image(
                session_id,
                image_urls[0],
                "edit_background",
                f"Changed background to: {new_background}",
                {"background_description": new_background},
            )

            logger.info(f"Successfully edited background: {len(image_urls)} image(s)")

            # Create UI component for RTVI event
            ui_component = {
                "id": f"background_edit_{uuid.uuid4()}",
                "type": "image",
                "url": image_urls[0],
                "title": "Background Edited",
                "description": f"Changed background to: {new_background}",
                "props": {
                    "imageUrl": image_urls[0],
                    "imageUrls": image_urls,
                    "title": "Background Edited",
                    "description": f"Changed background to: {new_background}",
                    "operation": "edit_background",
                    "previousImage": current_image_url,
                    "generation_info": {
                        "workflow": "background_edit",
                        "background_description": new_background,
                    },
                },
            }

            # Register RTVI event
            if session_id:
                register_pending_rtvi_event(session_id, "ui-component", ui_component)
                # Also register with unified navigator for navigation
                register_image_for_navigation(session_id, ui_component)
                logger.info(
                    f"Registered background edit RTVI event and navigation for session {session_id}"
                )

            result = {
                "success": True,
                "message": f"Successfully changed background to: {new_background}",
                "image_urls": image_urls,
                "operation": "edit_background",
                "previous_image": current_image_url,
                "generation_info": {
                    "workflow": "background_edit",
                    "background_description": new_background,
                },
            }
            await params.result_callback(result)
        else:
            logger.warning("No edited images were generated")
            await params.result_callback(
                {
                    "success": False,
                    "error": "Failed to edit background. Please try again.",
                    "image_urls": [],
                }
            )

    except Exception as e:
        logger.error(f"Error editing image background: {e}")
        await params.result_callback(
            {
                "success": False,
                "error": f"Failed to edit background: {str(e)}",
                "image_urls": [],
            }
        )


async def mask_and_edit_object(params: FunctionCallParams):
    """
    Mask and edit specific objects in the current working image.

    Supports operations like changing color, style, or replacing objects.
    """
    if not config.ENABLE_COMFYUI:
        logger.warning("ComfyUI is disabled in configuration")
        await params.result_callback(
            {
                "success": False,
                "error": "ComfyUI is not enabled. Please enable ENABLE_COMFYUI in your environment configuration.",
                "image_urls": [],
            }
        )
        return

    try:
        # Extract parameters
        object_description = params.arguments.get("object_description", "")
        edit_instruction = params.arguments.get("edit_instruction", "")

        if not object_description or not edit_instruction:
            await params.result_callback(
                {
                    "success": False,
                    "error": "Both object description and edit instruction are required.",
                    "image_urls": [],
                }
            )
            return

        # Get current session and image
        session_id = get_current_session_id()
        if not session_id:
            await params.result_callback(
                {
                    "success": False,
                    "error": "No active session found.",
                    "image_urls": [],
                }
            )
            return

        current_image_url = get_current_image(session_id)
        if not current_image_url:
            await params.result_callback(
                {
                    "success": False,
                    "error": "No current image found. Please generate an image first.",
                    "image_urls": [],
                }
            )
            return

        # Check if current image URL is a placeholder - if so, try to find the real uploaded image
        if (
            current_image_url
            in ["user_uploaded_image_url", "user_upload_whiskey_bottle_image_url"]
            or not current_image_url.startswith(("/static/", "/api/", "http"))
            or current_image_url in ["whiskey bottle", "bottle", "product", "image"]
        ):
            logger.warning(
                f"Current image URL is a placeholder: {current_image_url}, searching for real uploaded image"
            )

            # Get image context and find the most recent real uploaded image
            from app.agents.voice.automatic.utils.image_context import get_image_context

            image_context = get_image_context(session_id)

            real_image_url = None
            if image_context and hasattr(image_context, "editing_history"):
                # Look for the most recent real uploaded image (not placeholder)
                for entry in reversed(image_context.editing_history):
                    if (
                        hasattr(entry, "operation")
                        and entry.operation == "user_upload"
                        and hasattr(entry, "image_url")
                        and entry.image_url
                        and not entry.image_url.startswith("user_upload")
                        and entry.image_url.startswith("/static/")
                    ):
                        real_image_url = entry.image_url
                        logger.info(
                            f"Found real uploaded image in history: {real_image_url}"
                        )
                        break

            if real_image_url:
                # Update current image to the real uploaded image
                set_current_image(
                    session_id,
                    real_image_url,
                    "corrected_reference",
                    f"Corrected reference from placeholder to real uploaded image",
                    {"original_placeholder": current_image_url},
                )
                current_image_url = real_image_url
                logger.info(
                    f"Updated current image from placeholder to real image: {current_image_url}"
                )
            else:
                logger.warning(
                    "No real uploaded image found in history, proceeding with placeholder"
                )

        logger.info(f"Masking and editing object in current image: {current_image_url}")
        logger.info(f"Object: {object_description}, Edit: {edit_instruction}")

        # Check if we should use mock images for development
        if DEVELOPMENT_MODE and USE_MOCK_IMAGES:
            logger.info("Using mock image for development - skipping fal.ai API call")
            image_urls = [_get_mock_image_url()]
            await asyncio.sleep(1.5)  # Simulate longer generation time for masking
        else:
            service = await _get_comfyui_service()

            # Convert current image URL to file path for fal.ai image editing
            input_image_path = await _convert_image_url_to_file_path(current_image_url)

            if input_image_path:
                logger.info(
                    f"Using image-to-image editing for object modification: {input_image_path}"
                )

                # Detect size-related keywords for intelligent prompt construction
                size_keywords_smaller = [
                    "smaller",
                    "reduce",
                    "shrink",
                    "minimize",
                    "downsize",
                    "tiny",
                    "little",
                ]
                size_keywords_bigger = [
                    "bigger",
                    "larger",
                    "expand",
                    "increase",
                    "enlarge",
                    "grow",
                    "huge",
                    "massive",
                ]
                size_keywords_zoom = [
                    "zoom out",
                    "show more",
                    "wider view",
                    "full view",
                    "more background",
                ]

                edit_lower = edit_instruction.lower()
                is_size_smaller = any(
                    keyword in edit_lower for keyword in size_keywords_smaller
                )
                is_size_bigger = any(
                    keyword in edit_lower for keyword in size_keywords_bigger
                )
                is_zoom_out = any(
                    keyword in edit_lower for keyword in size_keywords_zoom
                )

                # Use fal.ai for object masking and editing with size-aware prompt construction
                if object_description.lower() == "background":
                    # For background changes, be very specific about preserving the main subject
                    enhanced_prompt = f"Change only the background to: {edit_instruction}. Keep the bottle/product in the foreground exactly the same, preserve all details of the main subject, only replace the background, high quality professional editing"
                elif is_size_smaller or is_zoom_out:
                    # For making objects smaller or zooming out to show more background
                    enhanced_prompt = f"Make the {object_description} smaller and show more of the surrounding background. {edit_instruction}. Maintain the {object_description} quality and details while expanding the visible background area. Professional composition with good balance between subject and background."
                elif is_size_bigger:
                    # For making objects larger
                    enhanced_prompt = f"Make the {object_description} larger and more prominent in the frame. {edit_instruction}. Fill more of the image with the {object_description} while maintaining high quality and proper cropping. Focus on the {object_description} as the main subject."
                else:
                    # For other object modifications
                    enhanced_prompt = f"Modify only the {object_description}: {edit_instruction}. Keep everything else in the image exactly the same, preserve all other details, high quality"

                logger.info(
                    f"Using enhanced prompt for image editing: {enhanced_prompt}"
                )

                # Determine optimal parameters based on size change type
                if is_size_smaller or is_zoom_out:
                    # For size reduction/zoom out, use higher guidance for better composition control
                    size_guidance_scale = 8.0
                    size_strength = 0.8  # Higher strength for more significant changes
                    preferred_model = "kontext"  # Better for composition changes
                elif is_size_bigger:
                    # For enlargement, use moderate guidance to avoid over-processing
                    size_guidance_scale = 7.0
                    size_strength = 0.7
                    preferred_model = "qwen"  # Better for object focus
                else:
                    # Default parameters for other edits
                    size_guidance_scale = 7.5
                    size_strength = 0.7
                    preferred_model = "auto"

                # Implement dynamic model selection based on size change requests and preferred model
                try:
                    if object_description.lower() == "background":
                        logger.info(
                            "Using Flux Kontext for background editing (better preservation)"
                        )
                        image_urls, _ = await service.generate_fal_flux_kontext(
                            prompt=enhanced_prompt,
                            input_image_path=input_image_path,
                            guidance_scale=size_guidance_scale,
                            strength=size_strength,
                        )
                    elif preferred_model == "kontext" or (
                        is_size_smaller or is_zoom_out
                    ):
                        logger.info(
                            f"Using Flux Kontext for size reduction/composition changes (guidance: {size_guidance_scale}, strength: {size_strength})"
                        )
                        image_urls, _ = await service.generate_fal_flux_kontext(
                            prompt=enhanced_prompt,
                            input_image_path=input_image_path,
                            guidance_scale=size_guidance_scale,
                            strength=size_strength,
                        )
                    elif preferred_model == "qwen" or is_size_bigger:
                        logger.info(
                            f"Using Qwen Image Edit for object enlargement/focus (guidance: {size_guidance_scale})"
                        )
                        image_urls, _ = await service.generate_fal_image_edit(
                            prompt=enhanced_prompt,
                            input_image_path=input_image_path,
                            guidance_scale=size_guidance_scale,
                        )
                    else:
                        logger.info(
                            "Using fal image edit for standard object modification"
                        )
                        image_urls, _ = await service.generate_fal_image_edit(
                            prompt=enhanced_prompt,
                            input_image_path=input_image_path,
                            guidance_scale=size_guidance_scale,
                        )
                except Exception as primary_error:
                    logger.warning(f"Primary method failed: {primary_error}")
                    # Intelligent fallback based on original choice
                    try:
                        if (
                            preferred_model == "kontext"
                            or object_description.lower() == "background"
                        ):
                            logger.info(
                                "Falling back to Qwen Image Edit from Flux Kontext"
                            )
                            image_urls, _ = await service.generate_fal_image_edit(
                                prompt=enhanced_prompt,
                                input_image_path=input_image_path,
                                guidance_scale=size_guidance_scale,
                            )
                        else:
                            logger.info(
                                "Falling back to Flux Kontext from Qwen Image Edit"
                            )
                            image_urls, _ = await service.generate_fal_flux_kontext(
                                prompt=enhanced_prompt,
                                input_image_path=input_image_path,
                                guidance_scale=size_guidance_scale,
                                strength=size_strength,
                            )
                    except Exception as fallback_error:
                        logger.error(f"Fallback method also failed: {fallback_error}")
                        image_urls = []

                # Clean up temporary file if it was created
                if (
                    input_image_path.startswith("/Users/")
                    and "temp/image_editing" in input_image_path
                ):
                    try:
                        Path(input_image_path).unlink()
                        logger.info(f"Cleaned up temporary file: {input_image_path}")
                    except:
                        pass

            else:
                logger.warning(
                    "Could not convert image URL to file path, falling back to text-to-image"
                )
                # Fallback to text-to-image with modified prompt
                enhanced_prompt = f"professional advertisement with modified {object_description}: {edit_instruction}, high quality, commercial photography"

                image_urls, _ = await service.generate_fal_text_to_image(
                    prompt=enhanced_prompt,
                    aspect_ratio="1:1",
                    guidance_scale=7.5,
                    output_format="png",
                )

        if image_urls:
            # Update session context with edited image
            operation_desc = f"masked {object_description} and {edit_instruction}"
            set_current_image(
                session_id,
                image_urls[0],
                "mask_and_edit",
                operation_desc,
                {
                    "object_description": object_description,
                    "edit_instruction": edit_instruction,
                },
            )

            logger.info(
                f"Successfully masked and edited object: {len(image_urls)} image(s)"
            )

            # Create UI component for RTVI event
            ui_component = {
                "id": f"object_edit_{uuid.uuid4()}",
                "type": "image",
                "url": image_urls[0],
                "title": "Object Edited",
                "description": f"Modified {object_description}: {edit_instruction}",
                "props": {
                    "imageUrl": image_urls[0],
                    "imageUrls": image_urls,
                    "title": "Object Edited",
                    "description": f"Modified {object_description}: {edit_instruction}",
                    "operation": "mask_and_edit",
                    "previousImage": current_image_url,
                    "generation_info": {
                        "workflow": "object_masking",
                        "object_description": object_description,
                        "edit_instruction": edit_instruction,
                    },
                },
            }

            # Register RTVI event
            if session_id:
                register_pending_rtvi_event(session_id, "ui-component", ui_component)
                # Also register with unified navigator for navigation
                register_image_for_navigation(session_id, ui_component)
                logger.info(
                    f"Registered object edit RTVI event and navigation for session {session_id}"
                )

            result = {
                "success": True,
                "message": f"Successfully modified {object_description}: {edit_instruction}",
                "image_urls": image_urls,
                "operation": "mask_and_edit",
                "previous_image": current_image_url,
                "generation_info": {
                    "workflow": "object_masking",
                    "object_description": object_description,
                    "edit_instruction": edit_instruction,
                },
            }
            await params.result_callback(result)
        else:
            logger.warning("No edited images were generated")
            await params.result_callback(
                {
                    "success": False,
                    "error": "Failed to edit object. Please try again.",
                    "image_urls": [],
                }
            )

    except Exception as e:
        logger.error(f"Error masking and editing object: {e}")
        await params.result_callback(
            {
                "success": False,
                "error": f"Failed to edit object: {str(e)}",
                "image_urls": [],
            }
        )


async def add_to_current_image(params: FunctionCallParams):
    """
    Add objects or elements to the current working image.

    Uses the current image from session context and adds specified elements while preserving the original.
    """
    if not config.ENABLE_COMFYUI:
        logger.warning("ComfyUI is disabled in configuration")
        await params.result_callback(
            {
                "success": False,
                "error": "ComfyUI is not enabled. Please enable ENABLE_COMFYUI in your environment configuration.",
                "image_urls": [],
            }
        )
        return

    try:
        # Extract parameters
        addition_description = params.arguments.get("addition_description", "")
        if not addition_description:
            await params.result_callback(
                {
                    "success": False,
                    "error": "Addition description is required.",
                    "image_urls": [],
                }
            )
            return

        # Get current session and image
        session_id = get_current_session_id()
        if not session_id:
            await params.result_callback(
                {
                    "success": False,
                    "error": "No active session found.",
                    "image_urls": [],
                }
            )
            return

        current_image_url = get_current_image(session_id)
        if not current_image_url:
            await params.result_callback(
                {
                    "success": False,
                    "error": "No current image found. Please generate an image first.",
                    "image_urls": [],
                }
            )
            return

        logger.info(f"Adding to current image: {current_image_url}")
        logger.info(f"Addition: {addition_description}")

        # Check if we should use mock images for development
        if DEVELOPMENT_MODE and USE_MOCK_IMAGES:
            logger.info("Using mock image for development - skipping fal.ai API call")
            image_urls = [_get_mock_image_url()]
            await asyncio.sleep(1.0)  # Simulate generation time
        else:
            service = await _get_comfyui_service()

            # Convert current image URL to file path for fal.ai image editing
            input_image_path = await _convert_image_url_to_file_path(current_image_url)

            if input_image_path:
                logger.info(
                    f"Using image-to-image editing to add elements: {input_image_path}"
                )

                # Use fal.ai for adding elements to the image
                enhanced_prompt = f"add {addition_description} to this image, keep the existing content, seamlessly integrate the new elements, high quality"

                try:
                    image_urls, _ = await service.generate_fal_image_edit(
                        prompt=enhanced_prompt, input_image_path=input_image_path
                    )
                except Exception as edit_error:
                    logger.warning(
                        f"Image editing failed, falling back to Flux Kontext: {edit_error}"
                    )
                    # Fallback to Flux Kontext for image-to-image
                    try:
                        image_urls, _ = await service.generate_fal_flux_kontext(
                            prompt=enhanced_prompt, input_image_path=input_image_path
                        )
                    except Exception as kontext_error:
                        logger.error(f"Flux Kontext also failed: {kontext_error}")
                        image_urls = []

                # Clean up temporary file if it was created
                if (
                    input_image_path.startswith("/Users/")
                    and "temp/image_editing" in input_image_path
                ):
                    try:
                        Path(input_image_path).unlink()
                        logger.info(f"Cleaned up temporary file: {input_image_path}")
                    except:
                        pass

            else:
                logger.warning(
                    "Could not convert image URL to file path, falling back to text-to-image"
                )
                # Fallback to text-to-image with modified prompt
                enhanced_prompt = f"professional advertisement with {addition_description} added, high quality, commercial photography"

                image_urls, _ = await service.generate_fal_text_to_image(
                    prompt=enhanced_prompt,
                    aspect_ratio="1:1",
                    guidance_scale=7.5,
                    output_format="png",
                )

        if image_urls:
            # Update session context with edited image
            operation_desc = f"added {addition_description}"
            set_current_image(
                session_id,
                image_urls[0],
                "add_to_image",
                operation_desc,
                {"addition_description": addition_description},
            )

            logger.info(
                f"Successfully added elements to image: {len(image_urls)} image(s)"
            )

            # Create UI component for RTVI event
            ui_component = {
                "id": f"add_to_image_{uuid.uuid4()}",
                "type": "image",
                "url": image_urls[0],
                "title": "Added to Image",
                "description": f"Added {addition_description}",
                "props": {
                    "imageUrl": image_urls[0],
                    "imageUrls": image_urls,
                    "title": "Added to Image",
                    "description": f"Added {addition_description}",
                    "operation": "add_to_image",
                    "previousImage": current_image_url,
                    "generation_info": {
                        "workflow": "add_to_image",
                        "addition_description": addition_description,
                    },
                },
            }

            # Register RTVI event
            if session_id:
                register_pending_rtvi_event(session_id, "ui-component", ui_component)
                # Also register with unified navigator for navigation
                register_image_for_navigation(session_id, ui_component)
                logger.info(
                    f"Registered add-to-image RTVI event and navigation for session {session_id}"
                )

            result = {
                "success": True,
                "message": f"Successfully added {addition_description} to the image",
                "image_urls": image_urls,
                "operation": "add_to_image",
                "previous_image": current_image_url,
                "generation_info": {
                    "workflow": "add_to_image",
                    "addition_description": addition_description,
                },
            }
            await params.result_callback(result)
        else:
            logger.warning("No edited images were generated")
            await params.result_callback(
                {
                    "success": False,
                    "error": "Failed to add elements to image. Please try again.",
                    "image_urls": [],
                }
            )

    except Exception as e:
        logger.error(f"Error adding to image: {e}")
        await params.result_callback(
            {
                "success": False,
                "error": f"Failed to add elements to image: {str(e)}",
                "image_urls": [],
            }
        )


async def upload_user_image(params: FunctionCallParams):
    """
    Handle user's own image upload for processing. Use this when user wants to upload their own bottle/product image
    and then apply masking, editing, or logo overlay operations on it.
    """
    try:
        # Get session ID for context management
        session_id = get_current_session_id()

        # Extract parameters
        image_description = params.arguments.get("image_description", "product image")
        next_action = params.arguments.get(
            "next_action", "edit"
        )  # what user wants to do after upload

        logger.info(
            f"User wants to upload their own {image_description} for {next_action}"
        )

        # Store context for auto-continuation after image upload (reuse logo context storage)
        if session_id:
            upload_context = {
                "prompt": f"user uploaded {image_description}",
                "image_description": image_description,
                "next_action": next_action,
                "upload_type": "user_image",
            }

            # Store in session context using existing logo context function
            store_logo_request_context(session_id, upload_context)

            # Register RTVI event for image upload
            upload_event_payload = {
                "action_required": "upload_image",
                "message": f"Please upload your {image_description}. After uploading, I'll help you {next_action} it!",
                "upload_endpoint": "/api/v1/upload/image",
                "session_id": session_id,
                "continue_with": "process_uploaded_image",
                "image_description": image_description,
                "next_action": next_action,
                "title": f"Upload Your {image_description.title()}",
                "description": f"Upload your {image_description} to get started",
            }

            register_pending_rtvi_event(
                session_id, "image-upload-request", upload_event_payload
            )
            logger.info(
                f"Registered RTVI image upload request for session {session_id}"
            )

        await params.result_callback(
            {
                "success": False,
                "action_required": "upload_image",
                "message": f"Please upload your {image_description}. After uploading, I'll help you {next_action} it!",
                "upload_endpoint": "/api/v1/upload/image",
                "continue_with": "process_uploaded_image",
                "image_description": image_description,
                "next_action": next_action,
            }
        )
        return

    except Exception as e:
        logger.error(f"Error handling user image upload request: {e}")
        await params.result_callback(
            {
                "success": False,
                "error": f"Failed to process image upload request: {str(e)}",
                "image_urls": [],
            }
        )


async def process_uploaded_image(params: FunctionCallParams):
    """
    Process the user's uploaded image and set it as current working image.
    This function is called after user uploads their image via the upload interface.
    """
    try:
        # Extract parameters
        image_url = params.arguments.get("image_url", None)
        image_description = params.arguments.get("image_description", "uploaded image")
        next_action = params.arguments.get("next_action", "edit")

        if not image_url:
            await params.result_callback(
                {
                    "success": False,
                    "error": "Image URL is required to process uploaded image.",
                    "image_urls": [],
                }
            )
            return

        # Get session ID
        session_id = get_current_session_id()

        logger.info(f"Processing uploaded {image_description}: {image_url}")

        # Validate image URL and correct if it's a placeholder
        if not image_url.startswith(("/static/", "/api/", "http")) or image_url in [
            "whiskey bottle",
            "bottle",
            "product",
            "image",
            "user_uploaded_image_url",
        ]:
            logger.warning(
                f"Image URL appears to be placeholder text: {image_url}, searching for real uploaded image"
            )

            # Get image context and find the most recent real uploaded image
            from app.agents.voice.automatic.utils.image_context import get_image_context

            image_context = get_image_context(session_id)

            real_image_url = None
            if image_context and hasattr(image_context, "editing_history"):
                # Look for the most recent real uploaded image (not placeholder)
                for entry in reversed(image_context.editing_history):
                    if (
                        hasattr(entry, "operation")
                        and entry.operation == "user_upload"
                        and hasattr(entry, "image_url")
                        and entry.image_url
                        and entry.image_url.startswith(("/static/", "/api/", "http"))
                        and not entry.image_url
                        in ["whiskey bottle", "bottle", "product", "image"]
                    ):
                        real_image_url = entry.image_url
                        logger.info(
                            f"Found real uploaded image in history: {real_image_url}"
                        )
                        break

            if real_image_url:
                image_url = real_image_url
                logger.info(
                    f"Corrected image URL from placeholder to real uploaded image: {image_url}"
                )
            else:
                logger.warning(
                    "No real uploaded image found in history, proceeding with placeholder"
                )

        # Set the uploaded image as the current working image
        if session_id:
            set_current_image(
                session_id,
                image_url,
                "user_upload",
                f"uploaded {image_description}",
                {"image_description": image_description, "next_action": next_action},
            )

        # Create UI component to show the uploaded image
        ui_component = {
            "id": f"uploaded_image_{uuid.uuid4()}",
            "type": "image",
            "url": image_url,
            "title": f"Your {image_description.title()}",
            "description": f"Successfully uploaded! Now you can ask me to {next_action} it.",
            "metadata": {
                "operation": "user_upload",
                "image_description": image_description,
                "next_action": next_action,
            },
        }

        # Register RTVI event to display the uploaded image
        if session_id:
            register_pending_rtvi_event(session_id, "ui-component", ui_component)
            logger.info(f"Registered uploaded image display for session {session_id}")

        await params.result_callback(
            {
                "success": True,
                "message": f"Successfully uploaded your {image_description}! Now you can ask me to mask it, change the background, add a logo, or make other edits.",
                "image_urls": [image_url],
                "operation": "user_upload",
                "current_image": image_url,
                "suggestions": [
                    f"Mask the {image_description} and change the background",
                    f"Add your brand logo to the {image_description}",
                    f"Change the lighting or style of the {image_description}",
                ],
            }
        )

    except Exception as e:
        logger.error(f"Error processing uploaded image: {e}")
        await params.result_callback(
            {
                "success": False,
                "error": f"Failed to process uploaded image: {str(e)}",
                "image_urls": [],
            }
        )


from app.agents.voice.automatic.tools.comfyui.smart_image_handler import (
    smart_image_handler,
)

# Tool function mapping
tool_functions = {
    "generate_advertisement_image": generate_advertisement_image,
    "generate_custom_image": generate_custom_image,
    "generate_advertisement_with_logo": generate_advertisement_with_logo,
    "edit_image_background": edit_image_background,
    "mask_and_edit_object": mask_and_edit_object,
    "add_to_current_image": add_to_current_image,
    "smart_image_handler": smart_image_handler,
    "upload_user_image": upload_user_image,
    "process_uploaded_image": process_uploaded_image,
}
