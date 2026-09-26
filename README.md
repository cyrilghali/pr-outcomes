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
            [--group-by reviewed|label|author|approver|depth] [--fix-window-days 7]
            [--repo-path PATH] [--json] [--refresh]
```

| Flag | Default | Notes |
|---|---|---|
| `--since` | 90 days ago | |
| `--until` | today | |
| `--base` | repo's default branch | On `tryriot/parrot` this is `staging`, so the "Deploy to production" PRs based on `production` are excluded. |
| `--group-by` | none (one "all" group) | `reviewed`, `label`, `author`, `approver`, or `depth`. Label and approver groups can overlap: a PR with two labels counts in both. A PR with none lands in a `(none)` group. |
| `--fix-window-days` | 7 | How many days past `--until` to look for reverts and follow-up fixes. |
| `--repo-path` | none | Local clone to blame-attribute follow-up fixes against (see below). Only local git commands are run; the clone is never fetched or written to. Without it, follow-up-fix metrics are null (`-` in the table). |
| `--json` | off | Machine-readable output instead of the table. |
| `--refresh` | off | Bypass the on-disk PR and blame caches. |

The cache lives at `~/.cache/pr-outcomes/`. Older weekly chunks are cached
forever, and only the current week is refetched, so a second run against the
same range is fast. Blame attribution is cached per commit sha under
`blame/<sha>.json`, since a fix PR's attribution depends only on that commit
and never changes; uncached commits are blamed in parallel.

GitHub's GraphQL API silently truncates a PR's timeline (commits, force
pushes, ready-for-review, review-requested) when one request resolves
timelines for more than a handful of PRs, with no signal in the response that
it happened. To avoid that, each PR's timeline is fetched in its own request,
after the search page that lists the PRs; the fetches run four at a time.
Other connections (reviews, comments, review threads, files) are still
fetched inside the search query. If GitHub truncates one of those too, the
tool prints one warning per affected PR to stderr instead of silently
under-counting.

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
| `followup_fix_rate` | The share of PRs later blamed as having introduced a bug that a fix PR corrected, within the fix window. Requires `--repo-path`; without it this metric is null. |

Follow-up fixes are found by blame attribution, SZZ-style, not file overlap.
For every merged PR titled `fix:`, `hotfix:`, or `bugfix:` that is not a
revert, we diff its merge commit against its parent, take the old-side line
ranges it changed or deleted (a pure addition carries no blame), and run
`git blame` on those ranges in the parent commit. Each blamed commit is
mapped back to the PR that merged it, using `git log --first-parent`, and
that PR counts as followed up by the fix if it merged no earlier than the
fix and within `--fix-window-days` of it. Lockfiles are excluded, matching
the revert metric.

A fix whose diff only adds lines is not attributed to anything, since there
are no old lines to blame. A fix whose title lacks a recognised
`fix:`/`hotfix:`/`bugfix:` prefix is missed entirely. Only local git history
is read, never GitHub state, so this needs a local clone passed through
`--repo-path`.

### Review depth

For each human non-author approver, we classify their first `APPROVED`
review:

| Class | Meaning |
|---|---|
| `substantive` | They commented, a push followed that comment, and they then approved: a push strictly between their first comment and their approval. Real back-and-forth happened. |
| `commented` | They commented, but no push landed between that comment and their approval (including a comment and approval submitted together, even if a rebase follows before merge). |
| `rubber_stamp` | No comment, the diff is 200+ lines, and the approval landed under 5 minutes after the PR was ready, last pushed to, or requested from them. Too fast to have been read. |
| `silent` | Every other no-comment approval. We cannot tell if it was a real review, so we do not guess. |

The table also reports two more numbers. A review round is a human
non-author review or comment, then a push, then another human review or
comment; we report the mean per PR and the share of PRs with at least one
round. Human
and bot comments count review bodies and inline comments alongside plain
comments, excluding the author's own, and are reported as a mean per PR.

`--group-by depth` splits PRs on these classes: `substantive-review` has at
least one `substantive` approval, `light-review` has a human approval but
none `substantive`, and `no-human-approval` has neither. On a repo where
almost every PR gets some human approval, the plain `reviewed` split barely
separates anything; `depth` is the split that does.

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
