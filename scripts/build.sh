#!/usr/bin/env bash
# Builds dist/pr-outcomes: a stdlib zipapp that a recipient runs with uv,
# without installing this package. See README's "Share it" section.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="${1:-$repo_root/dist/pr-outcomes}"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cp -r "$repo_root/pr_outcomes" "$tmp/pr_outcomes"
find "$tmp" -name __pycache__ -type d -exec rm -rf {} +

mkdir -p "$(dirname "$out")"
python3 -m zipapp "$tmp" \
    -m "pr_outcomes.cli:main" \
    -p "/usr/bin/env -S uv run --python >=3.12 --no-project python" \
    -o "$out"
chmod +x "$out"
