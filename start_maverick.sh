#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
export MAVERICK_AGENT="${MAVERICK_AGENT:-codex}"
python3 bridge.py
