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

## For agents

Output is JSON by default whenever stdout isn't a TTY (a pipe, a captured
subprocess output), and a human-readable table when it is; `--format
table|json` picks explicitly, and `--json` is a shorthand for `--format
json`. The default JSON payload stays small (a few KB): pass `--prs` to also
get per-PR facts under `"prs"`. Every metric key in `"groups"` has a one-line
explanation with its unit under `"definitions"`, so a payload is
self-describing without this README. A `"warnings"` array names anything
that makes a metric null or a window shorter than expected (e.g. follow-up
fixes need `--repo-path`, a truncated GitHub connection, a truncated
revert/fix window); every warning is also printed to stderr.

The exit code is `0` on success, `1` on a GitHub or git error (gh/git's own
stderr plus one corrective line: `gh auth status` for auth, spelling/access
for not-found, retry later for rate limits), and `2` on a usage error (bad
flags, a malformed repo or date). Progress lines (`cache hit`, `fetching`)
print only when stderr is a TTY or `--verbose` is passed, so piped stderr
stays limited to warnings and errors.

A cold first run on a busy repo takes ~20+ minutes: GitHub's GraphQL API
forces one request per PR's timeline (see below), fetched four at a time. A
cached rerun of the same range takes ~2 minutes. Run the first cold run in
the background, or in the foreground with a generous timeout.

## Usage

```
pr-outcomes tryriot/parrot --since 2026-08-01
pr-outcomes tryriot/parrot --since 2026-08-01 --group-by reviewed
pr-outcomes tryriot/parrot --group-by depth --repo-path ~/dev/riot/parrot
pr-outcomes tryriot/parrot --group-by team --teams awareness,inbox,platform,simulation,sonar
pr-outcomes tryriot/parrot --team sonar --group-by author
pr-outcomes tryriot/parrot --group-by size
pr-outcomes tryriot/parrot --team sonar --group-by week
pr-outcomes tryriot/parrot --prs | jq '.prs[] | select(.number == 1234)'
pr-outcomes tryriot/parrot --repo-path ~/dev/riot/parrot --production --team sonar --group-by month
```

```
pr-outcomes OWNER/REPO [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--base BRANCH]
            [--group-by reviewed|label|author|approver|depth|team|size|week|month] [--teams SLUG,...] [--team SLUG]
            [--fix-window-days 7] [--repo-path PATH] [--production]
            [--format table|json] [--json] [--prs] [--refresh] [--verbose]
```

Run `pr-outcomes --help` for every flag's default and an `Examples:` block.

| Flag | Default | Notes |
|---|---|---|
| `--since` | 90 days before `--until` | |
| `--until` | today | |
| `--base` | repo's default branch | On `tryriot/parrot` this is `staging`, so the "Deploy to production" PRs based on `production` are excluded. |
| `--group-by` | none (one "all" group) | `reviewed`, `label`, `author`, `approver`, `depth`, `team`, `size`, `week`, or `month`. Label, approver, and team groups can overlap: a PR with two labels, or an author on two teams, counts in both. A PR with none lands in a `(none)` group. `size` buckets additions+deletions: `xs` (<100), `s` (100-299), `m` (300-699), `l` (700+), always shown in that order. `week`/`month` bucket by the merge date's ISO week (`2026-W32`) or calendar month (`2026-08`), always shown chronologically, including a period with zero PRs (count 0, other metrics null) so a quiet period doesn't vanish from a trend; combine with `--team` for one team's trend over time. |
| `--teams` | every team the author belongs to | Comma-separated GitHub org team slugs, only used with `--group-by team`, e.g. `awareness,inbox,platform,simulation,sonar`. Given without `--group-by team`, it's ignored with a warning. |
| `--team` | none | Keep only PRs authored by a member of this single GitHub org team slug, e.g. `sonar`. Combines with any `--group-by` (`--team sonar --group-by author`). Reuses the same cached membership as `--group-by team`. An unknown slug exits 2 and lists the valid ones. |
| `--fix-window-days` | 7 | How many days past `--until` to look for reverts and follow-up fixes. PRs merged in the last `--fix-window-days` days before `--until` have a truncated real window, since the fetch range is capped at today: for a baseline measurement, pick an `--until` at least that far in the past. |
| `--repo-path` | none | Local clone to blame-attribute follow-up fixes against (see below). Only local git commands are run; the clone is never fetched or written to. Without it, follow-up-fix metrics are null (`-` in the table, `null` in JSON). |
| `--production` | off | Add lead time to production, deploy frequency, and change failure rate from `origin/production` in `--repo-path` and the `deploy-production.yml`/`cd-production.yml` GitHub Actions runs (the workflow was renamed 2026-08-28; both are queried, since GitHub keeps older runs under the old name). Requires `--repo-path`. |
| `--format` | `json` when piped, `table` when a TTY | `--json` is a shorthand for `--format json`. |
| `--prs` | off | Include per-PR facts (JSON: under `"prs"`; table: a JSON block printed after the table). |
| `--refresh` | off | Bypass the on-disk PR, blame, and team caches. |
| `--verbose` | off | Print progress lines (`cache hit` / `fetching`) even when stderr isn't a TTY. |

The cache lives at `~/.cache/pr-outcomes/`. Older weekly chunks are cached
forever, and only the current week is refetched, so a second run against the
same range is fast. Blame attribution is cached per commit sha under
`blame/<sha>.json`, since a fix PR's attribution depends only on that commit
and never changes; uncached commits are blamed in parallel. Org team
membership is cached at `<owner>__<repo>/teams.json`, refetched once a day or
on `--refresh`.

GitHub's GraphQL API silently truncates a PR's timeline (commits, force
pushes, ready-for-review, review-requested) when one request resolves
timelines for more than a handful of PRs, with no signal in the response that
it happened. To avoid that, each PR's timeline is fetched in its own request,
after the search page that lists the PRs; the fetches run four at a time.
Other connections (reviews, comments, review threads) are still fetched
inside the search query. If GitHub truncates one of those too, the
tool prints one warning per affected PR to stderr (and into the JSON
`"warnings"` array) instead of silently under-counting.

### `--group-by team`

Team membership comes from the GitHub org, not the repo: `gh api
orgs/<owner>/teams` plus each team's `members` endpoint (read-only, cached
and refetched daily). An author on no selected team, or on none at all,
lands in `(none)`, with no warning. If the repo's owner is a user account
rather than an org, the teams API 404s and every PR falls back to `(none)`,
with a warning. Membership reflects today, not the PR's merge date, and the
tool always warns about that when `--group-by team` is used.

### `--group-by week|month`

Buckets PRs by the ISO week (`2026-W32`) or calendar month (`2026-08`) of
their merge date, and reports each period in chronological order, so it can
be read as a trend rather than a single number. Combine with `--team` to see
one team's trend over time (`--team sonar --group-by week`).

Two warnings are period-specific and only fire for `week`/`month`: a period
that starts before `--since` or ends after `--until` is partial, since only
PRs merged inside `[--since, --until]` are counted for it; a period whose
end (clipped to `--until`) is within the last `--fix-window-days` days
before today has had less time than the fix window for reverts and
follow-up fixes to land, the same rule as the top-level truncation warning,
but scoped to that one period.

## Metrics

Every metric is computed per PR first, then aggregated per group as a
median, p75, mean, or share. A PR counts toward a group only if it merged
inside `[--since, --until]`. Reverts and follow-up fixes are the exception:
they are detected up to `--fix-window-days` days after `--until`, so one
landing just after the window still counts.

### Outcomes

| Metric | Definition |
|---|---|
| `revert_rate` | The share of PRs later reverted by another PR. A revert is GitHub's revert button or a `revert`-prefixed title, matched back to the original by issue reference, URL, an exact `Revert "<title>"` quote, or (failing those) the original's title appearing as a substring anywhere in the revert PR's title. The denominator excludes revert PRs themselves. |
| `followup_fix_rate` | The share of PRs later blamed as having introduced a bug that a fix PR corrected, within the fix window. Requires `--repo-path`; without it this metric is null. |

Follow-up fixes are found by blame attribution, SZZ-style, not file overlap.
For every merged PR whose title starts with `fix`, `hotfix`, or `bugfix` as
a whole word, case-insensitive (`fix:`, `fix(scope): ...`, `Fix crash`,
`hotfix: ...`, `bugfix: ...`, but not `fixture: ...`) and that is not a
revert, we diff its merge commit against its parent, take the old-side line
ranges it changed or deleted (a pure addition carries no blame), and run
`git blame` on those ranges in the parent commit. Each blamed commit is
mapped back to the PR that merged it, using `git log --first-parent`, and
that PR counts as followed up by the fix if it merged no later than the fix
and within `--fix-window-days` of it. Lockfiles are excluded.

A fix whose diff only adds lines is not attributed to anything, since there
are no old lines to blame. A fix whose title doesn't start with `fix`,
`hotfix`, or `bugfix` is missed entirely. Only local git history is read,
never GitHub state, so this needs a local clone passed through
`--repo-path`.

### Production outcomes

With `--production`:

| Metric | Definition |
|---|---|
| Lead time to production | Hours from PR merge to the production deploy that shipped it (median, p75). Null for a PR not yet in a known deploy. |
| Deploy frequency | `deploy_count` (distinct production deploys carrying this group's PRs) and `deploys_per_week`. |
| Change failure rate | Share of this group's deploys where a shipped PR was later reverted or blamed for a follow-up fix. |

Limits:

- Deploy frequency and change failure rate count deploys carrying that
  group's PRs, not the group's own deploys in isolation: a deploy shared
  across groups counts for each.
- The clone in `--repo-path` is never fetched, so a stale local
  `origin/production` makes recently-deployed PRs look not-deployed.
- Change failure rate inherits the revert and follow-up-fix detection
  limits above, and counts code remediation, not customer-facing incidents.

### Review depth

A push, wherever it's used below, is each commit's commit date plus each
force-push event's timestamp.

For each human non-author approver, we classify their first `APPROVED`
review. "Commented" means the reviewer left, at or before that approval, a
non-approval review with a body or inline comments, a review-thread comment,
or an issue comment, or inline comments attached to the approval review
itself. The approval review's own body text ("LGTM", a thumbs-up) does not
count on its own, since it never prompted a re-look.

| Class | Meaning |
|---|---|
| `changed_by_review` | The reviewer left a comment, the author pushed a change, then that reviewer approved: a push strictly between their first comment and their approval. The one class where we can see the review changed the code. |
| `commented` | They commented, but no push landed between that comment and their approval (including a comment and approval submitted together, even if a rebase follows before merge). |
| `rubber_stamp` | No comment, the diff is 200+ lines, and the approval landed under 5 minutes after the PR was ready, last pushed to, or requested from them. Too fast to have been read. |
| `silent` | Every other no-comment approval. We cannot tell if it was a real review, so we do not guess. |

A higher `changed_by_review` share is not better in itself: it matters most on
large or risky PRs, and it must not become a target, since a reviewer can
game it with a trivial nit comment on any PR.

The table also reports two more numbers. A review round is a human
non-author review or comment, then a push, then another human review or
comment; we report the mean per PR and the share of PRs with at least one
round. Human
and bot comments count non-approval review bodies and inline comments
alongside plain comments, excluding the author's own, and are reported as a mean per PR.

`--group-by depth` splits PRs on these classes: `changed-by-review` has at
least one `changed_by_review` approval, `light-review` has a human approval
but none `changed_by_review`, and `no-human-approval` has neither. On a repo where
almost every PR gets some human approval, the plain `reviewed` split barely
separates anything; `depth` is the split that does.

### Speed

| Metric | Definition |
|---|---|
| Review and approval timings | Time to first human review, time to first bot review, time to first approval, time to merge (from creation), and ready to merge (from the first "ready for review" event, or creation if the PR was never drafted). Each is reported as a median and a p75, in hours. |
| `merged_within_1h`, `merged_within_24h` | The share of PRs merged within one hour, or 24 hours, of creation. |
| `throughput_per_week` | PR count divided by the number of weeks in range; with `--group-by week` or `month`, the weeks of that period inside the range. The JSON output also breaks this down by ISO week. |
| `size` | Additions plus deletions, reported as a median and a p75. |

`--group-by size` buckets PRs the same way (`xs` <100, `s` 100-299, `m`
300-699, `l` 700+) so size can be read against outcomes: on `tryriot/parrot`
the follow-up fix rate climbs from 1.9% for `xs` PRs to 14.3% for `m` PRs.

## Limits

- A fix PR whose first-parent commit subject has no `#N` in it is skipped
  entirely for follow-up-fix attribution: a squash merge without the PR
  number in its subject, or a stacked PR merged through another PR's
  commit. On `tryriot/parrot` this was 6 of 481 fix PRs.
- Blame attribution has some noise: a fix that only edits a line another
  PR happened to also touch (e.g. an import line) gets credited to that
  PR, whether or not that line caused the bug.

## Goodhart warning

These are diagnostics, not targets. A single number here is easy to game:
fewer review rounds or a faster approval says nothing about whether the code
shipped was any good. Read the speed numbers next to the outcome numbers
(revert rate, follow-up fix rate), never instead of them, and do not set a
target on one metric in isolation.

## Share it

For someone who doesn't want to clone the repo or install the package:

```
bash scripts/build.sh
```

This produces `dist/pr-outcomes`, a ~65 KB [Python zipapp](https://docs.python.org/3/library/zipapp.html)
built from the standard library alone. It is not a native binary: the
recipient needs [`uv`](https://docs.astral.sh/uv/) installed (it runs the
zipapp under `uv run --python >=3.12`, which fetches a matching Python if
none is on their machine) and `gh auth login` done. A local clone
(`--repo-path`) is optional and only needed for follow-up-fix attribution.
Send them `dist/pr-outcomes` and they run it exactly like the installed
command:

```
./pr-outcomes tryriot/parrot --since 2026-08-01
```

## Development

```
python3 -m unittest discover -s tests
```

Static types are checked with `pyright` (via `uvx`) as part of `tests/test_types.py`, so the unittest run above is the single gate for both.

`fetch.py` handles GitHub I/O and JSON normalisation. `metrics.py` is pure
functions with no I/O, tested against hand-built fixtures in
`tests/fixtures/`. `cli.py` wires the two together and renders the output.
`deploys.py` links `origin/production` merges to their GitHub Actions deploy
runs and the staging PRs each one shipped.
