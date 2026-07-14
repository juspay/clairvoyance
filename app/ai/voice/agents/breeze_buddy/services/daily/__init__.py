"""Daily transport service for Breeze Buddy.

Intentionally side-effect free: ``bot_runner`` runs via ``python -m``, which
imports this package BEFORE the module body executes — any import here would
pull ``daily.py`` (and with it static config, freezing values from the
pre-dotenv environment) before ``bot_runner``'s ``load_dotenv()`` line runs.
Import ``start_daily_session`` from ``.daily`` directly.
"""
