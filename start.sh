#!/bin/zsh
set -e
cd "${0:A:h}"
exec uv run python app.py
