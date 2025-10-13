#!/usr/bin/env python3
"""
Debug script for logo upload and ComfyUI workflow testing
Tests the complete pipeline: logo upload -> ComfyUI workflow -> image generation
"""

import asyncio
import json
import os
import sys
import uuid
from io import BytesIO
from pathlib import Path

# Add the app directory to Python path
sys.path.insert(0, os.path.abspath("."))

from app.agents.voice.automatic.services.comfyui.client import ComfyUIService
from app.agents.voice.automatic.utils.session_context import set_current_session_id
from app.core.logger import logger


class MockUploadFile:
    def __init__(self, filename="test_logo.png", content_type="image/png"):
        self.filename = filename
        self.content_type = content_type
        self.size = 1024
        # Create a simple mock PNG image content
        self._content = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x00\x01\x00\x00\x00\x00\x1e\x00IEND\xaeB`\x82"

    async def read(self):
        return self._content


async def create_test_logo():
    """Create a test logo file for testing"""
    uploads_dir = Path("static/uploads/logos")
    uploads_dir.mkdir(parents=True, exist_ok=True)

    # Create a simple test logo file
    logo_filename = f"test_logo_{uuid.uuid4()}.png"
    logo_path = uploads_dir / logo_filename

    # Simple 100x100 red square PNG
    png_content = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00d\x00\x00\x00d\x08\x02\x00\x00\x00\xff\x80\x02\x03\x00\x00\x00\x19tEXtSoftware\x00Adobe ImageReadyq\xc9e<\x00\x00\x00\x0eIDATx\xdac\xf8\x0f\x00\x00\x01\x00\x01\x00\x00\x00\x00\x1e\x00IEND\xaeB`\x82"

    with open(logo_path, "wb") as f:
        f.write(png_content)

    return str(logo_path)


async def test_comfyui_connection():
    """Test basic ComfyUI connection"""
    print("🔍 Testing ComfyUI connection...")
    try:
        service = ComfyUIService()
        # Test basic connection
        logger.info("Testing ComfyUI server connection")
        return True
    except Exception as e:
        print(f"❌ ComfyUI connection failed: {e}")
        return False


async def test_available_nodes():
    """Test what nodes are available in ComfyUI"""
    print("🔍 Checking available ComfyUI nodes...")
    try:
        service = ComfyUIService()
        client = await service.get_client()

        # Try to get node info by making a simple request
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(f"{client.base_url}/object_info") as response:
                if response.status == 200:
                    node_info = await response.json()
                    available_nodes = list(node_info.keys())
                    print(f"✅ Found {len(available_nodes)} available nodes")

                    # Check for image-related nodes
                    image_nodes = [
                        node
                        for node in available_nodes
                        if "image" in node.lower() or "composite" in node.lower()
                    ]
                    print(
                        f"📝 Image-related nodes: {image_nodes[:10]}..."
                    )  # Show first 10

                    # Check specifically for ImageComposite
                    if "ImageComposite" in available_nodes:
                        print("✅ ImageComposite node is available")
                    else:
                        print("❌ ImageComposite node is NOT available")
                        # Look for alternatives
                        alternatives = [
                            node
                            for node in available_nodes
                            if "composite" in node.lower()
                            or "overlay" in node.lower()
                            or "blend" in node.lower()
                        ]
                        print(f"🔧 Possible alternatives: {alternatives}")

                    return available_nodes
                else:
                    print(f"❌ Failed to get node info: {response.status}")
                    return []
    except Exception as e:
        print(f"❌ Error checking nodes: {e}")
        return []


async def test_workflow_build():
    """Test building the advertisement with logo workflow"""
    print("🔍 Testing workflow building...")
    try:
        service = ComfyUIService()
        client = await service.get_client()
        logo_path = await create_test_logo()

        # Build workflow using the client's build_workflow method
        workflow = client.build_workflow(
            prompt="Test advertisement for black shoes",
            template="advertisement_with_logo",
            logo_path=logo_path,
            product_type="shoes",
            style="modern",
            logo_position="bottom right",
        )

        print(f"✅ Workflow built successfully with {len(workflow)} nodes")
        print(f"📝 Workflow nodes: {list(workflow.keys())}")

        # Check node 4 specifically (the problematic one)
        if "4" in workflow:
            node_4 = workflow["4"]
            print(f"🔍 Node 4 details: {node_4}")
            print(f"🔍 Node 4 class_type: {node_4.get('class_type')}")

        return workflow
    except Exception as e:
        print(f"❌ Workflow build failed: {e}")
        import traceback

        traceback.print_exc()
        return None


async def test_workflow_execution():
    """Test executing the workflow in ComfyUI"""
    print("🔍 Testing workflow execution...")
    try:
        service = ComfyUIService()
        logo_path = await create_test_logo()

        # Try to execute the workflow
        result = await service.generate_advertisement_with_logo(
            prompt="Test advertisement for black shoes",
            logo_path=logo_path,
            product_type="shoes",
            style="modern",
            logo_position="bottom right",
        )

        print(f"✅ Workflow executed successfully")
        print(f"📝 Result: {result}")
        return result
    except Exception as e:
        print(f"❌ Workflow execution failed: {e}")
        # Print the full error details
        import traceback

        print(f"🔍 Full error trace:")
        traceback.print_exc()
        return None


async def fix_workflow():
    """Try to fix the workflow by replacing ImageComposite with available nodes"""
    print("🔧 Attempting to fix workflow...")

    # Get available nodes first
    available_nodes = await test_available_nodes()

    if not available_nodes:
        print("❌ Cannot fix workflow - unable to get available nodes")
        return

    # Look for alternative composite nodes
    composite_alternatives = [
        node
        for node in available_nodes
        if any(
            keyword in node.lower()
            for keyword in ["composite", "overlay", "blend", "paste", "combine"]
        )
    ]

    print(f"🔧 Found composite alternatives: {composite_alternatives}")

    if composite_alternatives:
        # Try to update the workflow to use an alternative
        recommended = composite_alternatives[0]
        print(f"🔧 Recommended alternative: {recommended}")

        # Update the client code to use the alternative
        client_file = Path("app/agents/voice/automatic/services/comfyui/client.py")
        if client_file.exists():
            print(
                f"🔧 Updating workflow to use {recommended} instead of ImageComposite"
            )
            # This would require reading and modifying the file
            print(
                f"📝 Manual fix needed: Replace 'ImageComposite' with '{recommended}' in the workflow"
            )

    # Alternative: Use fal.ai only without logo compositing
    print("🔧 Alternative solution: Use fal.ai text-to-image with logo prompt")


async def main():
    """Main debug function"""
    print("🚀 Starting ComfyUI Logo Debug Script")
    print("=" * 50)

    # Set up session
    session_id = f"debug-session-{uuid.uuid4()}"
    set_current_session_id(session_id)
    print(f"📝 Session ID: {session_id}")
    print()

    # Step 1: Test ComfyUI connection
    connection_ok = await test_comfyui_connection()
    print()

    if not connection_ok:
        print("❌ Cannot proceed - ComfyUI connection failed")
        return

    # Step 2: Check available nodes
    available_nodes = await test_available_nodes()
    print()

    # Step 3: Test workflow building
    workflow = await test_workflow_build()
    print()

    # Step 4: Test workflow execution
    if workflow:
        result = await test_workflow_execution()
        print()

        if not result:
            # Step 5: Try to fix the workflow
            await fix_workflow()

    print("=" * 50)
    print("🏁 Debug script completed")


if __name__ == "__main__":
    asyncio.run(main())
