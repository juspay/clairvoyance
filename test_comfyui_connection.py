#!/usr/bin/env python3
"""
Quick test script to verify ComfyUI connection with Clairvoyance
"""

import asyncio
import os
import sys
from pathlib import Path

# Add the app directory to the path
sys.path.insert(0, str(Path(__file__).parent / "app"))


async def test_comfyui_connection():
    """Test ComfyUI connection and functionality."""

    print("🧪 TESTING COMFYUI CONNECTION WITH CLAIRVOYANCE")
    print("=" * 60)

    # Test 1: Check configuration
    print("\n1️⃣ CHECKING CONFIGURATION")
    print("-" * 30)

    try:
        from app.core.config import (
            COMFYUI_BASE_URL,
            COMFYUI_TIMEOUT,
            COMFYUI_WEBSOCKET_URL,
            ENABLE_COMFYUI,
        )

        print(f"✅ ENABLE_COMFYUI: {ENABLE_COMFYUI}")
        print(f"✅ COMFYUI_BASE_URL: {COMFYUI_BASE_URL}")
        print(f"✅ COMFYUI_WEBSOCKET_URL: {COMFYUI_WEBSOCKET_URL}")
        print(f"✅ COMFYUI_TIMEOUT: {COMFYUI_TIMEOUT}")

        if not ENABLE_COMFYUI:
            print("❌ ComfyUI is DISABLED in configuration!")
            print("   Set ENABLE_COMFYUI=true in your .env file")
            return False

    except ImportError as e:
        print(f"❌ Configuration import failed: {e}")
        return False

    # Test 2: Check ComfyUI service availability
    print("\n2️⃣ CHECKING COMFYUI SERVICE")
    print("-" * 30)

    try:
        import json

        import aiohttp

        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Test basic connectivity
            try:
                async with session.get(f"{COMFYUI_BASE_URL}/system_stats") as response:
                    if response.status == 200:
                        stats = await response.json()
                        print(f"✅ ComfyUI is running at {COMFYUI_BASE_URL}")
                        print(f"   System stats: {stats}")
                    else:
                        print(f"❌ ComfyUI responded with status {response.status}")
                        return False
            except Exception as e:
                print(f"❌ Cannot connect to ComfyUI at {COMFYUI_BASE_URL}")
                print(f"   Error: {e}")
                print("   Make sure ComfyUI is running!")
                return False

            # Test object_info endpoint (lists available nodes)
            try:
                async with session.get(f"{COMFYUI_BASE_URL}/object_info") as response:
                    if response.status == 200:
                        object_info = await response.json()

                        # Check for fal-API nodes
                        fal_nodes = [
                            node for node in object_info.keys() if "fal" in node.lower()
                        ]

                        if fal_nodes:
                            print(f"✅ Found {len(fal_nodes)} fal-API nodes:")
                            for node in fal_nodes[:5]:  # Show first 5
                                print(f"   - {node}")
                            if len(fal_nodes) > 5:
                                print(f"   ... and {len(fal_nodes) - 5} more")
                        else:
                            print("❌ No fal-API nodes found!")
                            print(
                                "   Make sure ComfyUI-fal-API is installed in custom_nodes/"
                            )
                            return False
                    else:
                        print(f"❌ Failed to get object_info: {response.status}")
                        return False
            except Exception as e:
                print(f"❌ Error checking ComfyUI nodes: {e}")
                return False

    except ImportError:
        print("❌ aiohttp not available - cannot test ComfyUI connection")
        return False

    # Test 3: Check Clairvoyance ComfyUI service
    print("\n3️⃣ CHECKING CLAIRVOYANCE COMFYUI SERVICE")
    print("-" * 40)

    try:
        from app.agents.voice.automatic.services.comfyui.client import ComfyUIService

        # Initialize service
        service = ComfyUIService()
        print("✅ ComfyUIService imported successfully")

        # Test service connection
        try:
            client = await service.get_client()
            print("✅ ComfyUI client initialized")

            # Test basic functionality
            if hasattr(client, "get_system_stats"):
                stats = await client.get_system_stats()
                print(f"✅ System stats via service: {stats}")

        except Exception as e:
            print(f"❌ Service connection failed: {e}")
            return False

    except ImportError as e:
        print(f"❌ ComfyUIService import failed: {e}")
        return False

    # Test 4: Check image generation functions
    print("\n4️⃣ CHECKING IMAGE GENERATION FUNCTIONS")
    print("-" * 40)

    try:
        from app.agents.voice.automatic.tools.comfyui.image_generation import (
            generate_advertisement_image,
            mask_and_edit_object,
        )

        print("✅ Image generation functions imported successfully")
        print("   - generate_advertisement_image")
        print("   - mask_and_edit_object")

    except ImportError as e:
        print(f"❌ Image generation functions import failed: {e}")
        return False

    # Test 5: Quick generation test (optional)
    print("\n5️⃣ QUICK GENERATION TEST")
    print("-" * 25)

    test_generation = (
        input("Run a quick image generation test? (y/N): ").lower().strip()
    )

    if test_generation == "y":
        try:
            print("🎨 Testing simple image generation...")

            # Import the function call parameters mock
            class MockParams:
                def __init__(self, arguments):
                    self.arguments = arguments
                    self.result = None

                async def result_callback(self, result):
                    self.result = result
                    print(f"✅ Generation result: {result}")

            # Test parameters
            params = MockParams(
                {
                    "prompt": "a simple red circle on white background",
                    "width": 512,
                    "height": 512,
                }
            )

            # Call the function
            await generate_advertisement_image(params)

            if params.result and params.result.get("success"):
                print("✅ Image generation test PASSED!")
                if "image_urls" in params.result:
                    print(f"   Generated images: {params.result['image_urls']}")
            else:
                print(f"❌ Image generation test FAILED: {params.result}")
                return False

        except Exception as e:
            print(f"❌ Generation test failed: {e}")
            return False
    else:
        print("⏭️  Skipping generation test")

    print("\n🎉 ALL TESTS PASSED!")
    print("=" * 60)
    print("ComfyUI is properly connected to Clairvoyance!")
    print()
    print("🚀 QUICK START:")
    print("1. Make sure ComfyUI is running: http://localhost:8188")
    print("2. Start Clairvoyance voice interface")
    print("3. Try voice commands like:")
    print("   'Create an ad for a whiskey bottle'")
    print("   'Generate an image of a mountain landscape'")
    print()

    return True


def check_comfyui_running():
    """Quick check if ComfyUI is running."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            "http://localhost:8188/system_stats", timeout=5
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, ConnectionRefusedError, TimeoutError):
        return False


async def main():
    """Main test function."""

    # Quick pre-check
    if not check_comfyui_running():
        print("❌ ComfyUI is not running at http://localhost:8188")
        print()
        print("🔧 TO START COMFYUI:")
        print("cd /Users/$(whoami)/work_dir/temp/ComfyUI")
        print("source comfyui_venv/bin/activate")
        print("python main.py --listen 0.0.0.0 --port 8188")
        print()
        print("Or use the startup script:")
        print("cd /Users/$(whoami)/work_dir/temp/ComfyUI")
        print("./start_comfyui.sh")
        print()

        start_comfyui = input("Start ComfyUI now? (y/N): ").lower().strip()
        if start_comfyui == "y":
            print(
                "Please start ComfyUI manually in another terminal and run this test again."
            )
        return

    # Run full test suite
    success = await test_comfyui_connection()

    if success:
        print("✅ SUCCESS: ComfyUI + Clairvoyance integration is working!")
    else:
        print("❌ FAILED: Issues found with ComfyUI integration")
        print()
        print("🔧 TROUBLESHOOTING:")
        print("1. Check ComfyUI is running: http://localhost:8188")
        print("2. Verify fal-API nodes are installed")
        print("3. Check .env configuration")
        print("4. Review setup documentation: COMFYUI_SETUP.md")


if __name__ == "__main__":
    asyncio.run(main())
