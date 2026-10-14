#!/bin/sh
# Spustenie stránky s počasím SHMÚ (macOS: dvojklik, Linux: sh spustit.command).
# Okno terminálu nechajte otvorené, kým stránku používate.
cd "$(dirname "$0")" || exit 1
exec python3 server.py --otvorit "$@"
