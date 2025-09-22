from app.core.config import ENABLE_CHARTS


def get_chart_visualization_instructions() -> str:
    """
    Returns chart visualization instructions if charts are enabled.
    """
    if ENABLE_CHARTS:
        return """
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
        2. Sales by channel/product/category → Donut chart
        3. Time trends (daily, weekly, monthly) → Line chart
        4. Comparisons between items → Bar chart
        5. Single metric → Single-stat chart

    RULE 4: FUNCTION RESULT SCANNING
        SCAN EVERY function result for: arrays, categories, values, percentages
        If you see componentType: 'DONUT_CHART' → MANDATORY generate_donut_chart call
        If you see componentType: 'BAR_CHART' → MANDATORY generate_bar_chart call
        If you see componentType: 'LINE_CHART' → MANDATORY generate_line_chart call

    RULE 5: FLEXIBLE HANDLING
        1. Always attempt a chart first
        2. If chart generation fails or is not meaningful, provide a clear text response instead
        3. Never leave the user without an answer

    RULE 7: NARRATION HIGHLIGHTING

        1. Always wrap category mentions in <highlight> XML tags
        2. Use exact category names from chart data
        3. Example: <highlight category="Credit Card">credit cards</highlight>
        4. ONLY highlight the top 1–2 most important categories, never all
        5. Importance = highest value (for totals) OR biggest change (for trends)
        6. Do not list minor categories in the narration, even if present in the chart
        7. Voice descriptions must stay short (2–3 sentences max), focusing on key insights

        """
    return ""
