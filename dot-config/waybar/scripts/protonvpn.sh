#!/usr/bin/env bash
set -euo pipefail

CONN="laptop-sng-SG-8"
ICON=$'\xef\x84\xb2'  # nf-fa-shield

active_wg_connection() {
  nmcli -t -f TYPE,NAME connection show --active 2>/dev/null | awk -F: '$1=="wireguard"{print $2; exit}'
}

case "${1:-status}" in
  toggle)
    active=$(active_wg_connection)
    if [[ -n "$active" ]]; then
      nmcli connection down "$active" >/dev/null
    else
      nmcli connection up "$CONN" >/dev/null
    fi
    ;;
  *)
    active=$(active_wg_connection)
    if [[ -n "$active" ]]; then
      printf '{"text":"%s","class":"connected","tooltip":"ProtonVPN connected — %s\\nClick to disconnect"}\n' "$ICON" "$active"
    else
      printf '{"text":"%s","class":"disconnected","tooltip":"ProtonVPN disconnected\\nClick to connect"}\n' "$ICON"
    fi
    ;;
esac
