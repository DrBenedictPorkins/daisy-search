#!/usr/bin/env bash
# vpncheck.sh — report whether any VPN service is currently connected.
# Exit 0 if a VPN is up, 1 if none.

set -euo pipefail

connected=$(scutil --nc list | awk '/\(Connected\)/ && /VPN:/ {
    match($0, /"[^"]+"/)
    print substr($0, RSTART+1, RLENGTH-2)
}')

if [[ -n "$connected" ]]; then
    echo "VPN active: $connected"
    exit 0
else
    echo "VPN inactive"
    exit 1
fi
