# pr-outcomes

![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)
![No dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![Read-only](https://img.shields.io/badge/GitHub-read--only-lightgrey?logo=github)

**Does code review on your repo change the code, and do the PRs it lets through hold up?**

`pr-outcomes` answers that from a repo's merged pull requests. For each PR it records how it was reviewed (real back-and-forth, a silent approval, a rubber stamp), how fast it moved, and what happened after merge: was it reverted, or did a later fix change its lines?

It reads GitHub through the `gh` CLI and never writes anything.

## What does the output look like?

Every PR merged into `cli/cli`'s default branch between 1 August and 15 September 2026, split by how deep the review went:

```
$ pr-outcomes cli/cli --since 2026-08-01 --until 2026-09-15 --group-by depth --repo-path ~/src/cli

                                               light-review  no-human-approval  changed-by-review
PR count                                                105                 15                 12

-- Outcomes --
Revert rate                                            1.9%               0.0%               0.0%
Follow-up fix rate                                     1.0%               6.7%               0.0%

-- Review depth --
Approvals: changed by review                           0.0%                  -              85.7%
Approvals: commented                                  11.4%                  -               0.0%
Approvals: silent                                     85.1%                  -              14.3%
Approvals: rubber-stamp                                3.5%                  -               0.0%
Review rounds (mean)                                   0.21                  0               1.75
PRs with >=1 round                                    17.1%               0.0%             100.0%
Human comments (mean)                                  0.63                  0               9.58
Bot comments (mean)                                     1.7                3.6               2.42

-- Speed --
Time to first human review, h (median)                  7.7                  -               71.2
Time to first approval, h (median)                     12.4                  -              162.1
Time to merge, h (median)                              16.8                0.6              195.2
Merged within 24h                                     60.0%             100.0%               8.3%
Throughput / week                                     15.98               2.28               1.83
Size, lines (median)                                     18                 90                286
```

*Rows trimmed. The full table also has p75 timings, time to first bot review, merged within 1h, and [Cubic](https://www.cubic.dev/) review scores.*

Only 12 of 132 PRs had a review that visibly changed the code. Those PRs were large (286 lines at the median) and took about eight days to merge. The 15 PRs merged with no human approval took 36 minutes at the median, and they have the highest follow-up fix rate.

Group by week and the same metrics become a trend:

```
$ pr-outcomes cli/cli --since 2026-08-01 --until 2026-09-15 --group-by week --repo-path ~/src/cli

                          2026-W31  2026-W32  2026-W33  2026-W34  2026-W35  2026-W36  2026-W37  2026-W38
PR count                         1        27        13        15        18        24        22        12

-- Outcomes --
Revert rate                   0.0%      0.0%      0.0%      7.1%      0.0%      0.0%      0.0%      9.1%
Follow-up fix rate            0.0%      0.0%      7.7%      0.0%      0.0%      4.2%      0.0%      0.0%
...
```

## How do I install it?

You need Python 3.12+ and the [`gh` CLI](https://cli.github.com/), logged in (`gh auth status`).

```sh
git clone https://github.com/cyrilghali/pr-outcomes && cd pr-outcomes
uv tool install -e .
```

The first run on a busy repo is slow, because each PR's timeline needs its own request (see [Why is the first run slow?](#why-is-the-first-run-slow)). The 132 PRs above took under two minutes. A repo with thousands of PRs can take 20+ minutes cold, and about 2 minutes once cached.

## How do I use it?

```sh
pr-outcomes OWNER/REPO                                            # last 90 days, one "all" group
pr-outcomes OWNER/REPO --since 2026-08-01 --group-by size
pr-outcomes OWNER/REPO --group-by depth --repo-path ~/src/repo    # adds follow-up-fix metrics
pr-outcomes OWNER/REPO --team backend --group-by week             # one team's trend
pr-outcomes OWNER/REPO --prs | jq '.prs[] | select(.number == 1234)'
```

`--repo-path` points at a local clone. Only the follow-up-fix metrics need it, and the tool only reads it: no fetch, no writes.

<details>
<summary><b>All flags</b></summary>

| Flag | Default | What it does |
|---|---|---|
| `--since` | 90 days before `--until` | Start of the merge window. |
| `--until` | today | End of the merge window. |
| `--base` | repo's default branch | Count only PRs merged into this branch. |
| `--group-by` | one `all` group | `reviewed`, `label`, `author`, `approver`, `depth`, `team`, `size`, `week`, `month`. See below. |
| `--teams` | every team of the author | Comma-separated org team slugs, used with `--group-by team`. |
| `--team` | none | Keep only PRs whose author is on this org team. Works with any `--group-by`. An unknown slug exits 2 and lists the valid ones. |
| `--fix-window-days` | 7 | Days past `--until` to keep looking for reverts and follow-up fixes. |
| `--repo-path` | none | Local clone for blame-based follow-up-fix attribution. Without it those metrics are null. |
| `--format` | `table` on a TTY, `json` otherwise | `--json` is short for `--format json`. |
| `--prs` | off | Add per-PR facts (JSON under `"prs"`; after the table in table mode). |
| `--refresh` | off | Ignore the PR, blame and team caches. |
| `--verbose` | off | Print progress lines even when stderr isn't a TTY. |

**Groups.** `label`, `approver` and `team` can overlap: a PR with two labels counts in both, and a PR with none lands in `(none)`. `size` buckets additions + deletions into `xs` (<100), `s` (100–299), `m` (300–699) and `l` (700+). `week` and `month` bucket by merge date in chronological order, and keep empty periods so a quiet week doesn't vanish from a trend.

**Teams** come from the GitHub org (`orgs/<owner>/teams`), cached for a day. Membership is today's, not the membership at merge time, and the tool warns about this. A user-owned repo has no teams, so every PR lands in `(none)` with a warning.

**Recent PRs** have had less time to be reverted or fixed. For a baseline, pick an `--until` at least `--fix-window-days` in the past. The tool warns when a window or a week/month period is cut short.

</details>

## What does each metric mean?

Each metric is computed per PR, then summarised per group as a median, p75, mean or share. A PR counts if it merged inside `[--since, --until]`.

### Did the PR hold up after merge?

| Metric | Meaning |
|---|---|
| Revert rate | Share of PRs later reverted by another PR. A revert is GitHub's revert button or a title starting with `revert`, matched back to the original by issue reference, URL or title. Revert PRs themselves are left out. |
| Follow-up fix rate | Share of PRs whose lines a later fix PR changed, within the fix window. Needs `--repo-path`. |

Follow-up fixes use blame attribution, the [SZZ](https://www.st.cs.uni-saarland.de/papers/msr2005/) approach. For every PR whose title starts with `fix`, `hotfix` or `bugfix` (`fix:`, `Fix crash`, but not `fixture:`), the tool finds the old lines the fix changed or deleted and runs `git blame` on them. Each PR that last touched those lines, and merged up to `--fix-window-days` before the fix, counts as followed up. Lockfiles are ignored.

### Did the review change anything?

Each human approval from someone other than the author falls into one class:

| Class | Meaning |
|---|---|
| **changed by review** | The reviewer commented, the author pushed, then the reviewer approved. The only case where you can see the review changed the code. |
| **commented** | The reviewer commented, but nothing was pushed between that comment and the approval. |
| **rubber-stamp** | No comment, a diff of 200+ lines, and an approval under 5 minutes after the PR was ready, pushed or requested. Too fast to have been read. |
| **silent** | Any other approval without a comment. It may have been a real review; the tool doesn't guess. |

An "LGTM" inside the approval itself doesn't count as a comment. A push is a commit or a force-push.

A **review round** is a human review or comment, then a push, then another human review or comment. The table shows rounds per PR and the share of PRs with at least one.

`--group-by depth` splits PRs into `changed-by-review` (at least one such approval), `light-review` (a human approval, but none that changed the code) and `no-human-approval`. Where nearly every PR gets some approval, `--group-by reviewed` barely separates anything; `depth` does.

### How fast did it move?

| Metric | Meaning |
|---|---|
| Time to first human review / bot review / approval | Hours from creation, median and p75. |
| Time to merge | Hours from creation to merge. |
| Ready to merge | Hours from the first "ready for review" (or creation, if never a draft) to merge. |
| Merged within 1h / 24h | Share of PRs merged that soon after creation. |
| Throughput / week | PRs per week in the range, or in the period with `week`/`month`. |
| Size | Lines added + deleted, median and p75. |

### Can I set targets on these numbers?

No. They're diagnostics. A faster approval or fewer review rounds says nothing about whether the shipped code was good, and a reviewer can raise the *changed by review* share with one nit per PR. Read the speed and review numbers next to the outcome numbers, never instead of them.

## Can an agent or script use it?

Yes:

- **JSON when piped.** Output is JSON when stdout isn't a TTY. The default payload is a few KB; `--prs` adds per-PR facts.
- **Self-describing.** Every metric key in `"groups"` has a one-line definition with its unit under `"definitions"`.
- **Warnings are data.** A missing input (follow-up fixes without `--repo-path`), truncated GitHub data or a shortened window goes into a `"warnings"` array and to stderr. A group with no approvals still has null approval metrics and no warning.
- **Exit codes.** `0` ok, `1` GitHub or git error (with one line on how to fix it), `2` bad flags or input.
- **Quiet stderr.** Progress lines print only on a TTY or with `--verbose`.

A shortened `--group-by size` payload (the real one has all four size groups and every metric key):

```json
{
  "repo": "cli/cli", "since": "2026-08-01", "until": "2026-09-15",
  "warnings": [],
  "definitions": { "revert_rate": "Share of PRs later reverted by another PR (0-1).", "...": "..." },
  "groups": {
    "xs": { "count": 89, "revert_rate": 0.023, "followup_fix_rate": 0.023, "...": "..." },
    "s":  { "count": 19, "revert_rate": 0.0,   "followup_fix_rate": 0.0,   "...": "..." }
  }
}
```

## Why is the first run slow?

GitHub's GraphQL API silently truncates PR timelines (commits, force-pushes, review requests) when one request covers more than a few PRs, and the response doesn't say so. The tool therefore fetches each timeline in its own request, four at a time. Reviews and comments still come in the bulk query; if one of those is truncated, the tool warns for that PR instead of under-counting.

Everything is cached in `~/.cache/pr-outcomes/`. Past weeks are cached forever and only the current week is refetched. Blame results are cached per commit, team membership for a day.

## What are the known limits?

- A fix PR whose merge commit subject has no `#N` (a squash without the PR number, or a stacked PR merged through another) is skipped for follow-up-fix attribution.
- A fix that only adds lines has nothing to blame, and a fix whose title doesn't start with `fix`, `hotfix` or `bugfix` is missed.
- A fix that edits a line another PR happened to touch (an import, say) credits that PR, whether or not that line caused the bug, so blame attribution carries some noise.

## How do I share it without installing?

```sh
bash scripts/build.sh
```

This builds `dist/pr-outcomes`, a ~65 KB [Python zipapp](https://docs.python.org/3/library/zipapp.html) that uses only the standard library. The recipient needs [`uv`](https://docs.astral.sh/uv/), which fetches a matching Python if needed, and `gh auth login`. They run it like the installed command; from this repository, that is `./dist/pr-outcomes OWNER/REPO`.

## How do I work on it?

```sh
python3 -m unittest discover -s tests
```

That one command runs the tests and the `pyright` type check (`tests/test_types.py`).

| File | Role |
|---|---|
| `pr_outcomes/fetch.py` | GitHub I/O and JSON normalisation |
| `pr_outcomes/metrics.py` | Pure functions, no I/O, tested against fixtures in `tests/fixtures/` |
| `pr_outcomes/cli.py` | Wires the two together and renders the output |
