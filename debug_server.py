#!/usr/bin/env python3
"""
Fast debugging server script with enhanced logging for ComfyUI integration.
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add the app directory to Python path
sys.path.insert(0, str(Path(__file__).parent))

# Set up enhanced logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("debug.log", mode="w"),
    ],
)

# Enable specific debug logging for ComfyUI
logging.getLogger("app.agents.voice.automatic.services.comfyui").setLevel(logging.DEBUG)
logging.getLogger("app.agents.voice.automatic.tools.comfyui").setLevel(logging.DEBUG)

from app.agents.voice.automatic.__main__ import main

if __name__ == "__main__":
    print("🚀 Starting Clairvoyance Voice Agent with Enhanced Debugging")
    print("📋 ComfyUI debugging enabled")
    print("📄 Logs are saved to debug.log")
    print("🔍 Use Ctrl+C to stop")
    print("-" * 60)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Server stopped by user")
    except Exception as e:
        print(f"\n❌ Server error: {e}")
        logging.exception("Server crashed")
