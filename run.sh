#!/bin/sh
# Launch log_check using the bundled venv (created with: python3 -m venv .venv
# && .venv/bin/pip install PyQt5). Falls back to the system python otherwise.
DIR=$(cd "$(dirname "$0")" && pwd)
if [ -x "$DIR/.venv/bin/python" ]; then
    exec "$DIR/.venv/bin/python" "$DIR/log_check.py" "$@"
fi
exec python3 "$DIR/log_check.py" "$@"
