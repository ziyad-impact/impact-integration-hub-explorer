#!/usr/bin/env bash
# Codespaces auto-start hook. Called by postStartCommand on every container start.
# Defensive on purpose — installs deps if missing (handles partial postCreate),
# then backgrounds uvicorn with full logging so anything that goes wrong is
# inspectable via `tail -f /tmp/uvicorn.log`.
set -e

cd "$(dirname "$0")/.."

# Idempotent — `pip install` is a no-op if everything is already present.
# Quiet, but errors still surface.
pip install -q -r requirements.txt

# Background uvicorn. `nohup` keeps it alive after this shell exits.
# Output goes to /tmp/uvicorn.log so any crash is debuggable.
nohup python -m uvicorn main:app --host 0.0.0.0 --port 8001 \
  > /tmp/uvicorn.log 2>&1 &

echo "uvicorn started (PID $!). Logs: tail -f /tmp/uvicorn.log"
