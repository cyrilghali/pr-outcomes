#!/bin/bash
# Weekly tryriot/parrot report: overall plus team, author and size breakdowns, as tables.
# Waits out GitHub's hourly GraphQL limit instead of failing; finished weeks stay cached.
set -u
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin"
out="$HOME/dev/cyrilghali/pr-outcomes/reports/$(date +%F)"
mkdir -p "$out"
run() {
  until pr-outcomes tryriot/parrot --repo-path "$HOME/dev/riot/parrot" --format table "$@"; do
    reset=$(gh api rate_limit -q .resources.graphql.reset) || exit 1
    sleep $(( reset - $(date +%s) + 30 ))
  done
}
run > "$out/all.txt" 2> "$out/errors.txt"
for g in team author size; do run --group-by "$g" > "$out/$g.txt" 2>> "$out/errors.txt"; done
