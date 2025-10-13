#!/usr/bin/env python3
"""
Quick script to check if there are pending RTVI events in the system.
"""

import os
import sys

# Add the app directory to Python path
sys.path.insert(0, os.path.abspath("."))

from app.agents.voice.automatic.rtvi.events_store import get_pending_rtvi_events

# Check for the session ID from the logs
session_id = "c4bdcc4b-77bf-4ac1-903a-119dd28d5f20"

print(f"Checking pending RTVI events for session: {session_id}")
events = get_pending_rtvi_events(session_id)

print(f"Found {len(events)} pending events:")
for i, event in enumerate(events):
    print(f"  Event {i+1}:")
    print(f"    Type: {event['type']}")
    if event["type"] == "ui-component":
        payload = event.get("payload", {})
        print(f"    Component Type: {payload.get('type')}")
        props = payload.get("props", {})
        print(f"    Image URL: {props.get('imageUrl')}")
        print(f"    Title: {props.get('title')}")

if events:
    print("\n✅ There ARE pending events that need to be emitted!")
else:
    print("\n❌ No pending events found")
