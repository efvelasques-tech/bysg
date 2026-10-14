#!/bin/zsh
cd "${0:A:h}"
export BYSG_PORT="${BYSG_PORT:-4180}"
export BYSG_BASE_URL="${BYSG_BASE_URL:-http://127.0.0.1:4180}"
exec python3 server.py
