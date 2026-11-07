#!/bin/bash
# Start the AIS Phase 2 voice-browser server (FastAPI, port 7861).
# No API keys needed: without keys in ais-voice/.env it runs in demo mode
# (page + status + transcript work; live voice answers 503).
# Phase 1 dashboard (port 7860) is NOT touched by this script.
#
# NOTE: the VM's no_proxy env contains bracketed IPv6 ("[::1]") which
# httpx (Anthropic SDK) cannot parse — service construction would raise
# InvalidURL before any key is used. app/server.py sanitizes it at
# import; this strips it here too so `uvicorn` sees clean env as well.
set -e
cd "$(dirname "$0")"
for var in no_proxy NO_PROXY; do
  val="${!var:-}"
  if [ -n "$val" ]; then
    clean="$(printf '%s' "$val" | tr ',' '\n' | grep -v '^\[.*\]$' | paste -sd, -)"
    export "$var=$clean"
  fi
done
exec .venv/bin/python -m uvicorn app.server:app --host "${HOST:-127.0.0.1}" --port "${PORT:-7861}"
