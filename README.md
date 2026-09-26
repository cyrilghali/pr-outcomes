# pr-outcomes

Command-line diagnostics for how a GitHub repo's pull requests get reviewed
and merged. It reads GitHub's GraphQL API through `gh`, and never writes
anything to GitHub.

## Install

Requires Python 3.12+ and the [`gh` CLI](https://cli.github.com/), already
logged in (`gh auth status`).

```
uv tool install -e .
```

## Usage

```
pr-outcomes tryriot/parrot --since 2026-08-01
pr-outcomes tryriot/parrot --since 2026-08-01 --group-by reviewed --json
```

```
pr-outcomes OWNER/REPO [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--base BRANCH]
            [--group-by reviewed|label|author|approver] [--fix-window-days 7]
            [--json] [--refresh]
```

| Flag | Default | Notes |
|---|---|---|
| `--since` | 90 days ago | |
| `--until` | today | |
| `--base` | repo's default branch | On `tryriot/parrot` this is `staging`, so the "Deploy to production" PRs based on `production` are excluded. |
| `--group-by` | none (one "all" group) | `reviewed`, `label`, `author`, or `approver`. Label and approver groups can overlap: a PR with two labels counts in both. A PR with none lands in a `(none)` group. |
| `--fix-window-days` | 7 | How many days past `--until` to look for reverts and follow-up fixes. |
| `--json` | off | Machine-readable output instead of the table. |
| `--refresh` | off | Bypass the on-disk cache. |

The cache lives at `~/.cache/pr-outcomes/`. Older weekly chunks are cached
forever, and only the current week is refetched, so a second run against the
same range is fast.

## Metrics

Every metric is computed per PR first, then aggregated per group as a
median, p75, mean, or share. A PR counts toward a group only if it merged
inside `[--since, --until]`. Reverts and follow-up fixes are the exception:
they are detected up to `--fix-window-days` days after `--until`, so one
landing just after the window still counts.

### Outcomes

| Metric | Definition |
|---|---|
| `revert_rate` | The share of PRs later reverted by another PR. A revert is GitHub's revert button or a `revert`-prefixed title, matched back to the original by issue reference, URL, or exact title. The denominator excludes revert PRs themselves. |
| `followup_fix_rate` | The share of PRs followed within the fix window by a merged PR titled `fix:`, `hotfix:`, or `bugfix:` that touches at least one file in common. Lockfiles and "hot" files, touched by more than 5% of all fetched PRs such as shared config or generated files, are ignored, since they would otherwise look coupled to every PR. |

### Review depth

For each human non-author approver, we classify their first `APPROVED`
review:

| Class | Meaning |
|---|---|
| `substantive` | They commented, and a push followed their comment. Real back-and-forth happened. |
| `commented` | They commented, but nothing was pushed after. |
| `rubber_stamp` | No comment, the diff is 200+ lines, and the approval landed under 5 minutes after the PR was ready, last pushed to, or requested from them. Too fast to have been read. |
| `silent` | Every other no-comment approval. We cannot tell if it was a real review, so we do not guess. |

The table also reports two more numbers. A review round is a human
non-author review or comment followed by a push before the next one; we
report the mean per PR and the share of PRs with at least one round. Human
and bot comments count review bodies and inline comments alongside plain
comments, excluding the author's own, and are reported as a mean per PR.

### Speed

| Metric | Definition |
|---|---|
| Review and approval timings | Time to first human review, time to first bot review, time to first approval, time to merge (from creation), and ready to merge (from the first "ready for review" event, or creation if the PR was never drafted). Each is reported as a median and a p75, in hours. |
| `merged_within_1h`, `merged_within_24h` | The share of PRs merged within one hour, or 24 hours, of creation. |
| `throughput_per_week` | PR count divided by the number of weeks in range. The JSON output also breaks this down by ISO week. |
| `size` | Additions plus deletions, reported as a median and a p75. |

## Goodhart warning

These are diagnostics, not targets. A single number here is easy to game:
fewer review rounds or a faster approval says nothing about whether the code
shipped was any good. Read the speed numbers next to the outcome numbers
(revert rate, follow-up fix rate), never instead of them, and do not set a
target on one metric in isolation.

## Development

```
python3 -m unittest discover -s tests
```

`fetch.py` handles GitHub I/O and JSON normalisation. `metrics.py` is pure
functions with no I/O, tested against hand-built fixtures in
`tests/fixtures/`. `cli.py` wires the two together and renders the output.
