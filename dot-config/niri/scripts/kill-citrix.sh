#!/bin/bash

# Ensure niri socket path is exported so IPC messages work properly
export NIRI_SOCKET="${NIRI_SOCKET:-/run/user/$(id -u)/niri-0}"

# 1. Count how many active outputs are registered in niri using its JSON output
# (Requires 'jq' to be installed for clean JSON parsing)
MONITOR_COUNT=$(niri msg --json outputs | jq '. | length')

# 2. Check if you are docked/connected to an external monitor
if [ "$MONITOR_COUNT" -gt 1 ]; then
    # More than 1 screen detected: you are safely docked. Exit doing nothing.
    exit 0
fi

# 3. Target and terminate the wfica (Citrix) program gently, then forcefully if needed
if pgrep -x "wfica" > /dev/null; then
    # Attempt to close it gracefully first
    pkill -15 -x "wfica"
    sleep 1

    # Force kill if it's hanging or stuck
    pkill -9 -x "wfica"
fi

