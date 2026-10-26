#!/bin/bash
# Double-click to convert everything in the plugins folder and install it into Ryujinx.
cd "$(dirname "$0")" || exit 1
python3 -m pip install -q -r requirements.txt 2>/dev/null
python3 silknx.py
echo
read -n 1 -s -r -p "Press any key to close this window"
