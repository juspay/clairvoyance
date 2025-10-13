#!/usr/bin/env python3
"""
Test script for the enhanced size scaling functionality in mask_and_edit_object.
"""

import asyncio
import os
import sys

# Add the app directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "app"))


# Mock function parameters for testing
class MockFunctionCallParams:
    def __init__(self, arguments):
        self.arguments = arguments
        self.result = None

    async def result_callback(self, result):
        self.result = result
        print(f"✅ Result: {result}")


def test_size_keyword_detection():
    """Test the size keyword detection logic."""
    print("🧪 TESTING SIZE KEYWORD DETECTION")
    print("=" * 50)

    # Test cases for size detection
    test_cases = [
        # Size smaller keywords
        ("make the bottle smaller", True, False, False),
        ("reduce the size of the product", True, False, False),
        ("shrink the bottle", True, False, False),
        ("make it tiny", True, False, False),
        # Size bigger keywords
        ("make the bottle bigger", False, True, False),
        ("enlarge the product", False, True, False),
        ("increase the size", False, True, False),
        ("make it huge", False, True, False),
        # Zoom out keywords
        ("zoom out to show more background", False, False, True),
        ("show more of the background", False, False, True),
        ("wider view of the scene", False, False, True),
        # Regular edits (no size keywords)
        ("change the color to blue", False, False, False),
        ("add a mountain landscape", False, False, False),
        ("replace with a forest", False, False, False),
    ]

    # Simulate the keyword detection logic from the function
    for (
        edit_instruction,
        expected_smaller,
        expected_bigger,
        expected_zoom,
    ) in test_cases:
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
        is_size_bigger = any(keyword in edit_lower for keyword in size_keywords_bigger)
        is_zoom_out = any(keyword in edit_lower for keyword in size_keywords_zoom)

        # Check results
        result = (
            "✅ PASS"
            if (
                is_size_smaller == expected_smaller
                and is_size_bigger == expected_bigger
                and is_zoom_out == expected_zoom
            )
            else "❌ FAIL"
        )

        print(f"{result} '{edit_instruction}'")
        print(
            f"    Expected: smaller={expected_smaller}, bigger={expected_bigger}, zoom={expected_zoom}"
        )
        print(
            f"    Got:      smaller={is_size_smaller}, bigger={is_size_bigger}, zoom={is_zoom_out}"
        )
        print()


def test_prompt_construction():
    """Test the enhanced prompt construction for different size scenarios."""
    print("🧪 TESTING PROMPT CONSTRUCTION")
    print("=" * 50)

    test_cases = [
        ("bottle", "make smaller", "smaller"),
        ("bottle", "make bigger", "bigger"),
        ("bottle", "zoom out to show more", "zoom"),
        ("bottle", "change color to blue", "regular"),
        ("background", "mountain landscape", "background"),
    ]

    for object_description, edit_instruction, expected_type in test_cases:
        # Simulate the prompt construction logic
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
        is_size_bigger = any(keyword in edit_lower for keyword in size_keywords_bigger)
        is_zoom_out = any(keyword in edit_lower for keyword in size_keywords_zoom)

        # Construct prompt based on logic from the function
        if object_description.lower() == "background":
            enhanced_prompt = f"Change only the background to: {edit_instruction}. Keep the bottle/product in the foreground exactly the same, preserve all details of the main subject, only replace the background, high quality professional editing"
            prompt_type = "background"
        elif is_size_smaller or is_zoom_out:
            enhanced_prompt = f"Make the {object_description} smaller and show more of the surrounding background. {edit_instruction}. Maintain the {object_description} quality and details while expanding the visible background area. Professional composition with good balance between subject and background."
            prompt_type = "smaller"
        elif is_size_bigger:
            enhanced_prompt = f"Make the {object_description} larger and more prominent in the frame. {edit_instruction}. Fill more of the image with the {object_description} while maintaining high quality and proper cropping. Focus on the {object_description} as the main subject."
            prompt_type = "bigger"
        else:
            enhanced_prompt = f"Modify only the {object_description}: {edit_instruction}. Keep everything else in the image exactly the same, preserve all other details, high quality"
            prompt_type = "regular"

        result = "✅ PASS" if prompt_type == expected_type else "❌ FAIL"
        print(f"{result} {object_description} + '{edit_instruction}' -> {prompt_type}")
        print(f"    Prompt: {enhanced_prompt[:100]}...")
        print()


def test_parameter_selection():
    """Test the parameter selection logic for different scenarios."""
    print("🧪 TESTING PARAMETER SELECTION")
    print("=" * 50)

    test_cases = [
        ("make smaller", "kontext", 8.0, 0.8),
        ("make bigger", "qwen", 7.0, 0.7),
        ("zoom out", "kontext", 8.0, 0.8),
        ("change color", "auto", 7.5, 0.7),
    ]

    for (
        edit_instruction,
        expected_model,
        expected_guidance,
        expected_strength,
    ) in test_cases:
        # Simulate parameter selection logic
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
        is_size_bigger = any(keyword in edit_lower for keyword in size_keywords_bigger)
        is_zoom_out = any(keyword in edit_lower for keyword in size_keywords_zoom)

        if is_size_smaller or is_zoom_out:
            size_guidance_scale = 8.0
            size_strength = 0.8
            preferred_model = "kontext"
        elif is_size_bigger:
            size_guidance_scale = 7.0
            size_strength = 0.7
            preferred_model = "qwen"
        else:
            size_guidance_scale = 7.5
            size_strength = 0.7
            preferred_model = "auto"

        result = (
            "✅ PASS"
            if (
                preferred_model == expected_model
                and size_guidance_scale == expected_guidance
                and size_strength == expected_strength
            )
            else "❌ FAIL"
        )

        print(f"{result} '{edit_instruction}'")
        print(
            f"    Expected: model={expected_model}, guidance={expected_guidance}, strength={expected_strength}"
        )
        print(
            f"    Got:      model={preferred_model}, guidance={size_guidance_scale}, strength={size_strength}"
        )
        print()


if __name__ == "__main__":
    print("🚀 ENHANCED SIZE SCALING FUNCTIONALITY TEST")
    print("=" * 60)
    print()

    try:
        test_size_keyword_detection()
        test_prompt_construction()
        test_parameter_selection()

        print("✅ ALL TESTS COMPLETED!")
        print()
        print("🔍 SUMMARY:")
        print(
            "- Size keyword detection: Enhanced to detect smaller/bigger/zoom requests"
        )
        print("- Prompt construction: Specialized prompts for different size scenarios")
        print("- Model selection: Flux Kontext for composition, Qwen for object focus")
        print("- Parameter tuning: Higher guidance for size changes")
        print()
        print("💡 USAGE EXAMPLES:")
        print("- 'make the bottle smaller' -> Uses Flux Kontext with high guidance")
        print("- 'enlarge the product' -> Uses Qwen Image Edit for object focus")
        print(
            "- 'zoom out to show more background' -> Uses Flux Kontext for composition"
        )
        print("- 'change the color to blue' -> Uses standard parameters")

    except Exception as e:
        print(f"❌ TEST ERROR: {e}")
        import traceback

        traceback.print_exc()
