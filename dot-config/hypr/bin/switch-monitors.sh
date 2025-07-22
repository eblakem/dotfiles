#!/usr/bin/env sh 

output=$(hyprctl monitors)

monitor_count=$(echo "$output" | grep -c "^Monitor")

if [ "$monitor_count" -gt 1 ]; then
    hyprctl keyword monitor "eDP-1, disable"
fi 

