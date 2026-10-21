#!/bin/bash
# Doppelklick stellt alle Boards online (GitHub Pages).
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "Richte Python einmalig ein …"
  python3 -m venv .venv && .venv/bin/pip install --quiet pillow || exit 1
fi
.venv/bin/python fotoboard.py veroeffentlichen "$@"
echo
read -r -p "Enter drücken, um das Fenster zu schliessen. "
