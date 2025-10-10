import math
import statistics
from collections import Counter
from typing import List

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

from app.core.logger import logger


def convert_to_float_list(numbers: list) -> List[float]:
    """
    Convert a list of numbers to float with proper error handling.
    """
    try:
        return [float(n) for n in numbers]
    except (ValueError, TypeError):
        raise ValueError(f"All elements in the list {numbers} must be valid numbers")


async def arithmetic_calculator(params: FunctionCallParams):
    """
    Performs various arithmetic operations on numbers.
    """
    try:
        operation = params.arguments.get("operation")
        numbers = params.arguments.get("numbers", [])

        if not operation:
            raise ValueError("Operation parameter is required")

        if not numbers or not isinstance(numbers, list):
            raise ValueError("Numbers parameter must be a non-empty list")

        # Convert all numbers to float for consistent handling
        numbers = convert_to_float_list(numbers)

        result = None

        if operation == "add":
            result = sum(numbers)
        elif operation == "subtract":
            if len(numbers) < 2:
                raise ValueError("Subtraction requires at least 2 numbers")
            result = numbers[0]
            for num in numbers[1:]:
                result -= num
        elif operation == "multiply":
            result = 1
            for num in numbers:
                result *= num
        elif operation == "divide":
            if len(numbers) < 2:
                raise ValueError("Division requires at least 2 numbers")
            if any(num == 0 for num in numbers[1:]):
                raise ValueError("Cannot divide by zero")
            result = numbers[0]
            for num in numbers[1:]:
                result /= num
        elif operation == "percentage_of":
            if len(numbers) != 2:
                raise ValueError(
                    "Percentage_of calculation requires exactly 2 numbers: [percent, total]"
                )
            percent, total = numbers
            result = (percent / 100) * total
        elif operation == "percentage_ratio":
            if len(numbers) != 2:
                raise ValueError(
                    "Percentage_ratio calculation requires exactly 2 numbers: [part, whole]"
                )
            part, whole = numbers
            if whole == 0:
                raise ValueError(
                    "Cannot calculate percentage ratio with zero denominator"
                )
            result = (part / whole) * 100
        elif operation == "power":
            if len(numbers) != 2:
                raise ValueError(
                    "Power calculation requires exactly 2 numbers: [base, exponent]"
                )
            base, exponent = numbers
            result = math.pow(base, exponent)
        elif operation == "sqrt":
            if len(numbers) != 1:
                raise ValueError("Square root requires exactly 1 number")
            if numbers[0] < 0:
                raise ValueError("Cannot calculate square root of negative number")
            result = math.sqrt(numbers[0])
        elif operation == "abs":
            if len(numbers) != 1:
                raise ValueError("Absolute value requires exactly 1 number")
            result = abs(numbers[0])
        else:
            raise ValueError(f"Unsupported operation: {operation}")

        logger.info(f"Arithmetic operation '{operation}' completed successfully")
        await params.result_callback(
            {"result": result, "operation": operation, "input_numbers": numbers}
        )

    except Exception as e:
        logger.error(
            f"Arithmetic calculator operation: {operation} with input: {numbers} failed with error: {e}"
        )
        await params.result_callback({"error": str(e)})


async def sort_and_rank(params: FunctionCallParams):
    """
    Performs sorting, ranking, and statistical operations on arrays of numbers.
    """
    try:
        operation = params.arguments.get("operation")
        numbers = params.arguments.get("numbers", [])
        order = params.arguments.get("order", "ascending")
        percentile = params.arguments.get("percentile", 50)

        if not operation:
            raise ValueError("Operation parameter is required")

        if not numbers or not isinstance(numbers, list):
            raise ValueError("Numbers parameter must be a non-empty list")

        # Convert all numbers to float for consistent handling
        numbers = convert_to_float_list(numbers)

        result = None

        if operation == "sort":
            if order == "ascending":
                result = sorted(numbers)
            elif order == "descending":
                result = sorted(numbers, reverse=True)
            else:
                raise ValueError("Order must be 'ascending' or 'descending'")
        elif operation == "rank":
            # Create list of (value, original_index) pairs
            indexed_numbers = [(num, i) for i, num in enumerate(numbers)]
            # Sort by value
            sorted_indexed = sorted(indexed_numbers, key=lambda x: x[0])
            # Assign ranks (handling ties by giving same rank)
            ranks = [0] * len(numbers)
            current_rank = 1
            for i, (value, original_index) in enumerate(sorted_indexed):
                if i > 0 and value != sorted_indexed[i - 1][0]:
                    current_rank = i + 1
                ranks[original_index] = current_rank
            result = {"numbers": numbers, "ranks": ranks}
        elif operation == "median":
            result = statistics.median(numbers)
        elif operation == "mode":
            try:
                result = statistics.mode(numbers)
            except statistics.StatisticsError:
                # No unique mode, return the most frequent values

                counter = Counter(numbers)
                max_count = max(counter.values())
                modes = [num for num, count in counter.items() if count == max_count]
                result = modes if len(modes) > 1 else [modes[0]]
        elif operation == "min_max":
            result = {"min": min(numbers), "max": max(numbers)}
        elif operation == "percentile":
            if not (0 <= percentile <= 100):
                raise ValueError("Percentile must be between 0 and 100")
            # Use numpy-style percentile calculation
            sorted_numbers = sorted(numbers)
            n = len(sorted_numbers)
            index = (percentile / 100) * (n - 1)
            if index.is_integer():
                result = sorted_numbers[int(index)]
            else:
                lower = sorted_numbers[int(index)]
                upper = sorted_numbers[int(index) + 1]
                result = lower + (upper - lower) * (index - int(index))
        else:
            raise ValueError(f"Unsupported operation: {operation}")

        logger.info(f"Sort and rank operation '{operation}' completed successfully")
        await params.result_callback(
            {"result": result, "operation": operation, "input_numbers": numbers}
        )

    except Exception as e:
        logger.error(
            f"Sort and rank operation: {operation} with input: {numbers} failed with error: {e}"
        )
        await params.result_callback({"error": str(e)})


async def array_operations(params: FunctionCallParams):
    """
    Performs vector and array operations including dot product, cross product, and element-wise operations.
    """
    try:
        operation = params.arguments.get("operation")
        array1 = params.arguments.get("array1", [])
        array2 = params.arguments.get("array2", [])

        if not operation:
            raise ValueError("Operation parameter is required")

        if not array1 or not isinstance(array1, list):
            raise ValueError("array1 parameter must be a non-empty list")

        # Convert array1 to float
        array1 = convert_to_float_list(array1)

        # Convert array2 to float if provided
        if array2:
            array2 = convert_to_float_list(array2)

        result = None

        if operation == "dot_product":
            if not array2:
                raise ValueError("Dot product requires both array1 and array2")
            if len(array1) != len(array2):
                raise ValueError("Arrays must have the same length for dot product")
            result = sum(a * b for a, b in zip(array1, array2))
        elif operation == "cross_product":
            if not array2:
                raise ValueError("Cross product requires both array1 and array2")
            if len(array1) != 3 or len(array2) != 3:
                raise ValueError(
                    "Cross product requires both arrays to have exactly 3 elements"
                )
            result = [
                array1[1] * array2[2] - array1[2] * array2[1],
                array1[2] * array2[0] - array1[0] * array2[2],
                array1[0] * array2[1] - array1[1] * array2[0],
            ]
        elif operation == "magnitude":
            result = math.sqrt(sum(x * x for x in array1))
        elif operation == "normalize":
            magnitude = math.sqrt(sum(x * x for x in array1))
            if magnitude == 0:
                raise ValueError("Cannot normalize zero vector")
            result = [x / magnitude for x in array1]
        elif operation == "sum":
            result = sum(array1)
        elif operation == "average":
            result = sum(array1) / len(array1)
        elif operation == "add_arrays":
            if not array2:
                raise ValueError("Array addition requires both array1 and array2")
            if len(array1) != len(array2):
                raise ValueError("Arrays must have the same length for addition")
            result = [a + b for a, b in zip(array1, array2)]
        elif operation == "subtract_arrays":
            if not array2:
                raise ValueError("Array subtraction requires both array1 and array2")
            if len(array1) != len(array2):
                raise ValueError("Arrays must have the same length for subtraction")
            result = [a - b for a, b in zip(array1, array2)]
        elif operation == "multiply_arrays":
            if not array2:
                raise ValueError("Array multiplication requires both array1 and array2")
            if len(array1) != len(array2):
                raise ValueError("Arrays must have the same length for multiplication")
            result = [a * b for a, b in zip(array1, array2)]
        elif operation == "divide_arrays":
            if not array2:
                raise ValueError("Array division requires both array1 and array2")
            if len(array1) != len(array2):
                raise ValueError("Arrays must have the same length for division")
            if any(b == 0 for b in array2):
                raise ValueError("Cannot divide by zero in array division")
            result = [a / b for a, b in zip(array1, array2)]
        else:
            raise ValueError(f"Unsupported operation: {operation}")

        logger.info(f"Array operation '{operation}' completed successfully")
        await params.result_callback(
            {
                "result": result,
                "operation": operation,
                "array1": array1,
                "array2": array2 if array2 else None,
            }
        )

    except Exception as e:
        logger.error(
            f"Array operation: {operation} with input: {array1, array2} failed with error: {e}"
        )
        await params.result_callback({"error": str(e)})


# Function schemas for the tools
arithmetic_calculator_function = FunctionSchema(
    name="arithmetic_calculator",
    description="ALWAYS use this tool for ANY arithmetic calculations. Never perform math manually - use this tool for addition, subtraction, multiplication, division, percentages, powers, square roots, and absolute values. OPERATION ORDER: subtract=[numbers[0] - numbers[1] - numbers[2]...], divide=[numbers[0] ÷ numbers[1] ÷ numbers[2]...], add/multiply=order irrelevant.",
    properties={
        "operation": {
            "type": "string",
            "description": "Choose operation: add=sum all numbers, subtract=sequential subtraction, multiply=product of all, divide=sequential division, percentage_of=calculate X% of Y, percentage_ratio=what % X is of Y, power=base^exponent, sqrt=square root, abs=absolute value",
            "enum": [
                "add",
                "subtract",
                "multiply",
                "divide",
                "percentage_of",
                "percentage_ratio",
                "power",
                "sqrt",
                "abs",
            ],
        },
        "numbers": {
            "type": "array",
            "items": {"type": "number"},
            "description": "Array of numbers in SPECIFIC ORDER: subtract=[first-second-third...], divide=[first÷second÷third...], percentage_of=[percent, total], percentage_ratio=[part, whole], power=[base, exponent], sqrt/abs=[single number]. Order determines result for subtract/divide!",
        },
    },
    required=["operation", "numbers"],
)

sort_and_rank_function = FunctionSchema(
    name="sort_and_rank",
    description="ALWAYS use this tool for ANY sorting, ranking, or statistical operations on numbers. Never sort or calculate statistics manually - use this tool for sorting, ranking, median, mode, min/max, and percentiles. ORDERING: sort=ascending(small→large)/descending(large→small), rank=assigns 1 to smallest value in ascending order, percentile=based on ascending sorted position.",
    properties={
        "operation": {
            "type": "string",
            "description": "Choose operation: sort=arrange in order, rank=assign position numbers (1=smallest), median=middle value, mode=most frequent value, min_max=returns minimum and maximum values, percentile=value at specific percentage position",
            "enum": ["sort", "rank", "median", "mode", "min_max", "percentile"],
        },
        "numbers": {
            "type": "array",
            "items": {"type": "number"},
            "description": "Array of numbers to process with this tool - never sort or analyze manually. Original input order preserved in rank results.",
        },
        "order": {
            "type": "string",
            "description": "Sort order for 'sort' operation: ascending=smallest to largest, descending=largest to smallest",
            "enum": ["ascending", "descending"],
            "default": "ascending",
        },
        "percentile": {
            "type": "number",
            "description": "Percentile value (0-100) for 'percentile' operation - calculated on ascending sorted array",
            "minimum": 0,
            "maximum": 100,
            "default": 50,
        },
    },
    required=["operation", "numbers"],
)

array_operations_function = FunctionSchema(
    name="array_operations",
    description="ALWAYS use this tool for ANY vector or array operations. Never perform array calculations manually - use this tool for dot products, cross products, magnitude, normalization, and element-wise operations. ELEMENT ORDER: element-wise ops=[array1[i] OP array2[i] for each position i], cross_product=[y1*z2-z1*y2, z1*x2-x1*z2, x1*y2-y1*x2] for 3D vectors.",
    properties={
        "operation": {
            "type": "string",
            "description": "Choose operation: dot_product=sum of element-wise products, cross_product=perpendicular vector (3D only), magnitude=vector length, normalize=unit vector, sum=add all elements, average=mean of elements, add_arrays=element-wise addition, subtract_arrays=element-wise subtraction, multiply_arrays=element-wise multiplication, divide_arrays=element-wise division",
            "enum": [
                "dot_product",
                "cross_product",
                "magnitude",
                "normalize",
                "sum",
                "average",
                "add_arrays",
                "subtract_arrays",
                "multiply_arrays",
                "divide_arrays",
            ],
        },
        "array1": {
            "type": "array",
            "items": {"type": "number"},
            "description": "First array for the operation - use this tool instead of manual array processing. For element-wise ops, this is the left operand (array1[i] OP array2[i]).",
        },
        "array2": {
            "type": "array",
            "items": {"type": "number"},
            "description": "Second array for two-array operations. Must match array1 length for element-wise ops. For subtract_arrays: array1[i] - array2[i], divide_arrays: array1[i] ÷ array2[i]. For cross_product: requires exactly 3 elements in both arrays.",
        },
    },
    required=["operation", "array1"],
)

# Tools schema
tools = ToolsSchema(
    standard_tools=[
        arithmetic_calculator_function,
        sort_and_rank_function,
        array_operations_function,
    ]
)

# Tool functions dictionary
tool_functions = {
    "arithmetic_calculator": arithmetic_calculator,
    "sort_and_rank": sort_and_rank,
    "array_operations": array_operations,
}
