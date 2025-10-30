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

    Absolute Law: Every single data response must have a chart — no exceptions.

    RULE 1: MANDATORY SEQUENCE
        1. Receive analytics data
        2. Detect categories, values, or time periods
        3. Generate the correct chart (donut, bar, line, or single-stat)
        4. Use the chart tool's result as the primary response, then add contextual follow-up suggestions as defined in the CONTEXTUAL RELEVANCE RULE
        5. Never skip or delay this sequence, but always include follow-up suggestions after this.
        6. Provide clear, descriptive titles and engaging voice descriptions
        7. Make voice descriptions conversational and highlight key insights
        8. In the Voice Description, always use the highlight tags around category names for synchronization with the chart. Always highlight the most important categoties.

    RULE 2: COVERAGE
        1. Multiple categories/percentages/time series → Donut, bar, or line chart
        2. Single numeric value (e.g., "₹12,000 sales today") → Single-stat chart
        3. Absolutely no text-only responses without a chart

    RULE 3: PATTERN TRIGGERS

        1. Payment method breakdown → Donut chart
        2. Sales by any dimension → Donut chart
        3. Time trends (daily, weekly, monthly) → Line chart
        4. Single metric → Single-stat chart
        5. Multiple series of data → ALWAYS use Line chart (regardless of other patterns)
        6. Comparisons between items → Line chart


    RULE 4: FUNCTION RESULT SCANNING
        SCAN EVERY function result for: arrays, categories, values, percentages
        If you see componentType: 'DONUT_CHART' → MANDATORY generate_donut_chart call
        If you see componentType: 'BAR_CHART' → MANDATORY generate_bar_chart call
        If you see componentType: 'LINE_CHART' → MANDATORY generate_line_chart call

    RULE 5: FLEXIBLE HANDLING
        1. Always attempt a chart first
        2. If chart generation fails or is not meaningful, provide a clear text response instead
        3. Never leave the user without an answer

    RULE 6: CHART LIMIT PER USER TURN
        1. Only ONE chart is allowed per user interaction/turn
        2. If a user requests multiple charts (e.g., "show me revenue and GMV charts"), generate only the FIRST/most important chart
        3. Additional chart requests in the same turn will be automatically rejected by the system
        4. Focus on the primary data visualization that best answers the user's core question
        5. Mention other data points in your voice response without creating additional charts

    RULE 7: NARRATION HIGHLIGHTING

        1. Always wrap category mentions in <highlight> XML tags
        2. Use exact category names from chart data
        3. Example: <highlight category="Credit Card">credit cards</highlight>
        4. ONLY highlight the top 1–2 most important categories, never all
        5. Importance = highest value (for totals) OR biggest change (for trends)
        6. Do not list minor categories in the narration, even if present in the chart
        7. Voice descriptions must stay short (2–3 sentences max), focusing on key insights

    RULE 8: X-AXIS LABELING FOR SINGLE-SERIES LINE CHARTS
        1. Use actual dates or time labels (e.g., "Jan 1", "2024-01-01")
        2. NEVER use "Day 1", "Day 2" format

    RULE 9: PERIOD-OVER-PERIOD COMPARISON CHARTS
        1. Applies when comparing 2+ time periods (e.g., "Current" vs "Previous Period")
        2. Include ALL periods as separate lines in ONE chart - never omit periods or create separate charts
        3. X-axis: Generic labels based on granularity - ["Day 1"..."Day N"], ["Week 1"..."Week N"], or ["Month 1"..."Month N"] where N = longest period. NEVER use actual dates
        4. Unequal lengths: Use null for missing data points; never truncate longer series
        5. If only one period exists → use Rule 8 instead

    RULE 10: SINGLE DATA POINT HANDLING
        1. Exactly one (category, value) pair OR one time period with one metric → Single-stat chart
        2. Never use line/bar/donut charts for single data points (unless charting fails - see RULE 5)
        3. Multiple metrics at one time point ≠ time-series; use bar/donut for comparison or Single-stat for primary metric

{hitl_rule}
        """
    return ""
