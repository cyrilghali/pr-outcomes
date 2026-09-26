"""Production deploy tracking: which merges to origin/production shipped,
when they deployed, and which staging PRs each one carried for the first
time. All git commands run locally against a caller-provided clone; never
fetches or writes. GitHub Actions run history is read via `gh`, never
written."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from pr_outcomes import blame, fetch
from pr_outcomes.fetch import GhError

PRODUCTION_BRANCH = "production"
# deploy-production.yml was renamed from cd-production.yml on 2026-08-28; GitHub
# keeps the older runs filed under the old (deleted) workflow name, so both
# must be queried to cover a range that spans the rename.
DEPLOY_WORKFLOWS = ("deploy-production.yml", "cd-production.yml")


@dataclass(frozen=True)
class Deploy:
    sha: str              # production merge commit = Sentry release = Datadog version tag
    release_head: str      # the staging commit it shipped (merge's second parent)
    deployed_at: datetime  # updated_at of the earliest successful deploy run for sha, UTC
    prs: frozenset[int]    # staging PRs this deploy shipped for the first time


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _run_git(repo_path: str, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", "-C", repo_path, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise blame.BlameError(proc.stderr.strip())
    return proc.stdout


def production_merges(repo_path: str) -> list[tuple[str, str, datetime]]:
    """(merge sha, second-parent sha, committer date), oldest first, from the
    first-parent chain of origin/production (falling back to a local
    `production` branch, like blame.build_sha_to_pr does for origin/<base>).
    Merges with fewer than two parents are skipped."""
    ref = f"origin/{PRODUCTION_BRANCH}"
    check = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "--verify", "--quiet", ref],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        ref = PRODUCTION_BRANCH
    out = _run_git(repo_path, ["log", "--first-parent", "--format=%H %P %cI", ref])

    merges: list[tuple[str, str, datetime]] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        sha = parts[0]
        parents = parts[1:-1]
        committer_date = parts[-1]
        if len(parents) < 2:
            continue
        merges.append((sha, parents[1], _parse_dt(committer_date)))
    merges.reverse()  # git log is newest-first; we want oldest-first
    return merges


def fetch_deploy_runs(owner: str, name: str, created_from: date) -> dict[str, datetime]:
    """sha -> earliest successful updated_at, for deploy-production.yml (and
    its pre-rename cd-production.yml) runs on the production branch created
    on or after created_from."""
    earliest: dict[str, datetime] = {}
    for workflow in DEPLOY_WORKFLOWS:
        path = (
            f"repos/{owner}/{name}/actions/workflows/{workflow}/runs"
            f"?branch={PRODUCTION_BRANCH}&status=success&created=>={created_from.isoformat()}&per_page=100"
        )
        proc = subprocess.run(
            ["gh", "api", "--paginate", path, "--jq", ".workflow_runs[] | [.head_sha, .updated_at] | @tsv"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise GhError(proc.stderr.strip())

        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            sha, _, updated_at = line.partition("\t")
            at = _parse_dt(updated_at)
            if sha not in earliest or at < earliest[sha]:
                earliest[sha] = at
    return earliest


def link_deploys(
    merges: list[tuple[str, str, datetime]],
    run_times: dict[str, datetime],
    commits_between: Callable[[str, str], list[str]],
    sha_to_pr: dict[str, int],
) -> list[Deploy]:
    """Keep only merges with a successful deploy run. Each kept deploy ships
    the staging first-parent commits in (previous kept deploy's release_head,
    its release_head]; the first kept deploy's lower bound is the
    release_head of the merge immediately before it on the production
    first-parent chain, or it's skipped if there is none (unknown range)."""
    deploys: list[Deploy] = []
    prev_release_head: str | None = None

    for i, (sha, release_head, _) in enumerate(merges):
        if sha not in run_times:
            continue
        if prev_release_head is None:
            if i == 0:
                continue  # no preceding merge -- lower bound unknown, skip
            prev_release_head = merges[i - 1][1]
        commits = commits_between(prev_release_head, release_head)
        prs = frozenset(sha_to_pr[c] for c in commits if c in sha_to_pr)
        deploys.append(Deploy(sha=sha, release_head=release_head, deployed_at=run_times[sha], prs=prs))
        prev_release_head = release_head

    return deploys


def _commits_between(repo_path: str, lo: str, hi: str) -> list[str]:
    out = _run_git(repo_path, ["rev-list", "--first-parent", f"{lo}..{hi}"])
    return [line for line in out.splitlines() if line]


def build_deploys(
    repo_path: str, owner: str, name: str, base: str, since: date, until: date,
) -> tuple[list[Deploy], list[str]]:
    warnings: list[str] = []
    merges = production_merges(repo_path)

    if merges:
        last_date = merges[-1][2].date()
        if last_date < until:
            warnings.append(
                f"origin/production was last updated {last_date}; PRs deployed after that count "
                "as not deployed (the clone is never fetched)"
            )

    created_from = since - timedelta(days=7)
    run_times = fetch_deploy_runs(owner, name, created_from)

    no_run_count = sum(
        1 for sha, _, committer_date in merges
        if committer_date.date() >= since and sha not in run_times
    )
    if no_run_count:
        warnings.append(
            f"{no_run_count} production merges had no successful deploy run; their PRs count "
            "toward the next successful deploy"
        )

    sha_to_pr = blame.build_sha_to_pr(repo_path, base)
    deploys = link_deploys(merges, run_times, lambda lo, hi: _commits_between(repo_path, lo, hi), sha_to_pr)
    return deploys, warnings
