#!/bin/bash
# Weekly tryriot/parrot report: overall plus team, author and size breakdowns, as tables.
# The done marker keeps it to one report per ISO week, however often it is launched.
# Waits out GitHub's hourly GraphQL limit instead of failing; finished weeks stay cached.
set -eu
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin"
out="$HOME/dev/cyrilghali/pr-outcomes/reports/$(date +%G-W%V)"
[ -e "$out/done" ] && exit 0
mkdir -p "$out"
git -C "$HOME/dev/riot/parrot" fetch -q origin staging && git -C "$HOME/dev/riot/parrot" checkout -q --detach FETCH_HEAD
run() {
  until pr-outcomes tryriot/parrot --repo-path "$HOME/dev/riot/parrot" --format table "$@"; do
    reset=$(gh api rate_limit -q .resources.graphql.reset) || exit 1
    sleep $(( reset - $(date +%s) + 30 ))
  done
}
run > "$out/all.txt" 2> "$out/errors.txt"
for g in team author size; do run --group-by "$g" > "$out/$g.txt" 2>> "$out/errors.txt"; done
touch "$out/done"
