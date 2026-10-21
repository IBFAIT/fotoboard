#!/bin/bash
# Doppelklick startet den Fotoboard-Editor im Browser.
# Das Fenster muss offen bleiben, solange du arbeitest.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "Richte Python einmalig ein …"
  python3 -m venv .venv && .venv/bin/pip install --quiet pillow || exit 1
fi
exec .venv/bin/python fotoboard.py "$@"
