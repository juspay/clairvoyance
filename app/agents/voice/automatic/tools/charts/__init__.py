from app.agents.voice.automatic.features.charts.chart_tools import (
        generate_bar_chart,
        generate_line_chart, 
        generate_donut_chart,
        generate_single_stat_card
    )

from . import generate_ui

tool_functions = {
    "generate_bar_chart": generate_bar_chart,
    "generate_line_chart": generate_line_chart,
    "generate_donut_chart": generate_donut_chart,
    "generate_single_stat_card": generate_single_stat_card
}

__all__ = ["generate_ui", "tool_functions"]
