#!/usr/bin/env python3
"""
Test script to verify that logo upload is skipped when user has already uploaded an image.
"""

import asyncio
import os
import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))


# Mock the FunctionCallParams class for testing
class MockFunctionCallParams:
    def __init__(self, arguments):
        self.arguments = arguments
        self.result = None

    async def result_callback(self, result):
        self.result = result
        print(f"Result callback called with: {result}")


async def test_logo_skip_logic():
    """Test that logo request is skipped when user has uploaded image."""
    print("Testing logo skip logic...")

    try:
        # Import the necessary functions
        from app.agents.voice.automatic.tools.comfyui.image_generation import (
            generate_advertisement_image,
        )
        from app.agents.voice.automatic.utils.image_context import (
            clear_image_context,
            has_current_image,
            set_current_image,
        )
        from app.agents.voice.automatic.utils.session_context import (
            set_current_session_id,
        )

        # Test scenario: User has uploaded their own bottle image
        test_session_id = "test_session_123"
        test_image_url = "/static/uploads/images/user_image_test.jpg"

        # Set up test session
        set_current_session_id(test_session_id)

        # Clear any existing context
        clear_image_context(test_session_id)

        print(f"Test session: {test_session_id}")
        print(f"Has current image before upload: {has_current_image(test_session_id)}")

        # Simulate user uploading their bottle image
        set_current_image(
            test_session_id,
            test_image_url,
            "user_upload",
            "User uploaded bottle image",
            {"product_type": "bottle", "original_filename": "my_bottle.jpg"},
        )

        print(f"Has current image after upload: {has_current_image(test_session_id)}")

        # Now test advertisement generation - should NOT ask for logo
        mock_params = MockFunctionCallParams(
            {
                "prompt": "Create an ad for my whiskey bottle",
                "product_type": "whiskey bottle",
                "style": "sophisticated",
            }
        )

        print("\n--- Testing advertisement generation with user's uploaded image ---")
        await generate_advertisement_image(mock_params)

        # Check result
        result = mock_params.result
        if result:
            print(f"Success: {result.get('success', False)}")
            print(f"Message: {result.get('message', 'No message')}")
            print(f"User provided image: {result.get('user_provided_image', False)}")
            print(f"Action required: {result.get('action_required', 'None')}")

            # Test passes if:
            # 1. Success is True
            # 2. user_provided_image is True
            # 3. No action_required (no logo upload request)
            if (
                result.get("success")
                and result.get("user_provided_image")
                and not result.get("action_required")
            ):
                print("\n✅ TEST PASSED: Logo request was correctly skipped!")
                return True
            else:
                print("\n❌ TEST FAILED: Logo request was not skipped properly")
                return False
        else:
            print("\n❌ TEST FAILED: No result received")
            return False

    except Exception as e:
        print(f"\n❌ TEST ERROR: {e}")
        import traceback

        traceback.print_exc()
        return False
    finally:
        # Clean up test data
        try:
            clear_image_context(test_session_id)
        except:
            pass


async def test_logo_request_when_no_image():
    """Test that logo request still works when user has NOT uploaded image."""
    print("\n\nTesting logo request when no user image...")

    try:
        # Import the necessary functions
        from app.agents.voice.automatic.tools.comfyui.image_generation import (
            generate_advertisement_image,
        )
        from app.agents.voice.automatic.utils.image_context import (
            clear_image_context,
            has_current_image,
        )
        from app.agents.voice.automatic.utils.session_context import (
            set_current_session_id,
        )

        # Test scenario: User has NOT uploaded any image
        test_session_id = "test_session_456"

        # Set up test session
        set_current_session_id(test_session_id)

        # Clear any existing context
        clear_image_context(test_session_id)

        print(f"Test session: {test_session_id}")
        print(f"Has current image: {has_current_image(test_session_id)}")

        # Test advertisement generation - SHOULD ask for logo
        mock_params = MockFunctionCallParams(
            {
                "prompt": "Create an ad for my product",
                "product_type": "product",
                "style": "modern",
            }
        )

        print("\n--- Testing advertisement generation without user image ---")
        await generate_advertisement_image(mock_params)

        # Check result
        result = mock_params.result
        if result:
            print(f"Success: {result.get('success', False)}")
            print(f"Message: {result.get('message', 'No message')}")
            print(f"Action required: {result.get('action_required', 'None')}")

            # Test passes if:
            # 1. Success is False (because logo upload is required)
            # 2. action_required is "upload_logo"
            if (
                not result.get("success")
                and result.get("action_required") == "upload_logo"
            ):
                print("\n✅ TEST PASSED: Logo request was correctly triggered!")
                return True
            else:
                print("\n❌ TEST FAILED: Logo request was not triggered properly")
                return False
        else:
            print("\n❌ TEST FAILED: No result received")
            return False

    except Exception as e:
        print(f"\n❌ TEST ERROR: {e}")
        import traceback

        traceback.print_exc()
        return False
    finally:
        # Clean up test data
        try:
            clear_image_context(test_session_id)
        except:
            pass


async def main():
    """Run all tests."""
    print("=" * 60)
    print("TESTING LOGO SKIP LOGIC")
    print("=" * 60)

    test1_passed = await test_logo_skip_logic()
    test2_passed = await test_logo_request_when_no_image()

    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    print(
        f"Test 1 (Skip logo when user image exists): {'PASS' if test1_passed else 'FAIL'}"
    )
    print(
        f"Test 2 (Request logo when no user image): {'PASS' if test2_passed else 'FAIL'}"
    )

    if test1_passed and test2_passed:
        print("\n🎉 ALL TESTS PASSED! Logo skip logic is working correctly.")
        return 0
    else:
        print("\n💥 SOME TESTS FAILED! Please check the implementation.")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
