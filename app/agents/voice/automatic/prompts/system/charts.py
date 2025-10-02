from app.core.config import ENABLE_CHARTS, HITL_ENABLE


def get_chart_visualization_instructions() -> str:
    """
    Returns chart visualization instructions if charts are enabled.
    """
    if ENABLE_CHARTS:
        # Conditionally include HITL rule as last rule
        hitl_rule = ""

        if HITL_ENABLE:
            hitl_rule = """
    LAST RULE: HITL OPERATIONS RESTRICTION
        1. Charts are automatically blocked after ANY HITL (Human-in-the-Loop) operation in the same turn
        2. HITL operations include: creating offers, updating settings, deleting data, or any operation that modifies system state
        3. When a HITL operation occurs, charts will be silently rejected by the system
        4. In these cases, focus your response on confirming the action taken and its results
        5. Do not attempt to generate charts after HITL operations - provide clear voice responses about what was accomplished instead
        6. This restriction applies in addition to the one-chart-per-turn limit
        7. This rule overrides the "Absolute Law" for the current turn when a HITL operation occurs
"""

        return f"""
    🔒 AUTOMATIC DATA VISUALIZATION (MANDATORY)

    1) MANDATORY SEQUENCE
        Receive analytics data → Detect categories/values/time periods → Pick chart → Render chart as primary response → Add brief follow-up suggestions → Provide title + voice description with highlights

    2) CHART SELECTION LOGIC (evaluated in order)
        • PRIORITY: If EXACTLY ONE data point (one category-value pair OR one time period with single metric) → MUST use Single-stat. NEVER use Line/Bar/Donut
        • Multiple categories/percentages (payment method, sales by product/channel): Donut (or Bar if absolute comparisons are clearer)
        • Time trend (single series, multiple time points): Line (use actual dates on X-axis)
        • Multiple series over time (comparisons): Line with one line per series
        • If function result specifies componentType:
          - DONUT_CHART → Donut (mandatory)
          - BAR_CHART → Bar (mandatory)
          - LINE_CHART → Line (mandatory)

    3) COMPARISON RULES (PERIOD-OVER-PERIOD)
        • Applies when comparing multiple time periods (e.g., "Current Period" vs "Previous Period", "Last 7 Days" vs "Previous 7 Days")
        • MUST show all periods as separate lines in ONE line chart - NEVER omit any period
        • X-axis labels: MUST use generic day labels ["Day 1", "Day 2", ..., "Day N"] where N = longest period length. NEVER use actual dates for comparisons
        • For unequal lengths: use null for missing days; do not truncate longer series
        • If only one period exists, it's NOT a comparison → use actual dates instead (see §4)

    4) X-AXIS & DATA HANDLING
        • Regular single-series time charts: actual dates (e.g., "2025-01-15", "Jan 15")
        • Never use "Day 1…Day N" for regular single-series charts
        • Multiple metrics at a single timestamp are NOT a time series: prefer Bar/Donut; Single-stat if intent is a single primary metric

    5) RENDERING CONSTRAINTS
        • Exactly ONE chart per user turn. If multiple charts requested, render only the first/most important; mention rest in narration
        • Always attempt a chart first. If charting is impossible/not meaningful, return clear text answer (fallback only)

    6) TITLES & VOICE DESCRIPTION
        • Provide clear, descriptive title
        • Voice description: 2–3 sentences, conversational, highlight key insights
        • Wrap ONLY top 1–2 most important categories (highest value or biggest change) in highlight tags
        • Example: <highlight category="Credit Card">credit cards</highlight>
        • Do not highlight all categories

    7) FOLLOW-UP SUGGESTIONS
        • After chart, add 1–3 short, relevant suggestions (e.g., "Compare last 7 vs previous 7 days?", "Drill into top product?")

    8) FUNCTION RESULT SCANNING
        • Always scan function results for arrays/categories/values/percentages and any componentType to enforce §2 mappings

    9) ABSOLUTE LAW
        • Every data response must include a chart (see §5 for the only allowed fallback)
{hitl_rule}
        """
    return ""
