#!/bin/bash
# Double-click this file (Finder) to update clipper to the newest version.
cd "$(dirname "$0")" || exit 1
python3 -m clipper update
echo
read -n 1 -s -r -p "Press any key to close..."
