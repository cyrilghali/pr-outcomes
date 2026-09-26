"""Blame-based (SZZ-style) follow-up-fix attribution. All git commands run
locally against a caller-provided clone; never fetches or writes.

Returns dict[fix_pr_number, set[introducing_pr_number]]; metrics.py stays
pure and takes that mapping as input."""

from __future__ import annotations

import re
import subprocess
from datetime import timedelta

from pr_outcomes.fetch import PR
from pr_outcomes.metrics import FOLLOWUP_TITLE_RE, LOCKFILES, is_revert_title

MERGE_SUBJECT_RE = re.compile(r"Merge pull request #(\d+)")
SQUASH_SUBJECT_RE = re.compile(r"\(#(\d+)\)")
DIFF_FILE_RE = re.compile(r"^diff --git a/(.*) b/(?:.*)$")
HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+")
BLAME_SHA_RE = re.compile(r"^([0-9a-f]{40}) ")


class BlameError(RuntimeError):
    pass


def parse_pr_number_from_subject(subject: str) -> int | None:
    m = MERGE_SUBJECT_RE.search(subject)
    if m:
        return int(m.group(1))
    matches = SQUASH_SUBJECT_RE.findall(subject)
    return int(matches[-1]) if matches else None


def parse_diff_hunks(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """Old-side (start, count) ranges with count > 0, per old file path, from
    a `git diff -U0` text. Pure-addition hunks (count == 0) are skipped."""
    hunks: dict[str, list[tuple[int, int]]] = {}
    current_path = None
    for line in diff_text.splitlines():
        m = DIFF_FILE_RE.match(line)
        if m:
            current_path = m.group(1)
            continue
        m = HUNK_HEADER_RE.match(line)
        if m and current_path is not None:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            if count > 0:
                hunks.setdefault(current_path, []).append((start, count))
    return hunks


def _run_git(repo_path: str, args: list[str]) -> str:
    proc = subprocess.run(["git", "-C", repo_path, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise BlameError(proc.stderr.strip())
    return proc.stdout


def build_sha_to_pr(repo_path: str, base: str) -> dict[str, int]:
    ref = f"origin/{base}"
    check = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "--verify", "--quiet", ref],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        ref = base
    out = _run_git(repo_path, ["log", "--first-parent", ref, "--format=%H %s"])
    sha_to_pr: dict[str, int] = {}
    for line in out.splitlines():
        sha, _, subject = line.partition(" ")
        number = parse_pr_number_from_subject(subject)
        if number is not None:
            sha_to_pr[sha] = number
    return sha_to_pr


def _is_lockfile(path: str) -> bool:
    return path.rsplit("/", 1)[-1] in LOCKFILES


def _blame_introducing_prs(repo_path: str, commit: str, sha_to_pr: dict[str, int]) -> set[int]:
    diff_text = _run_git(repo_path, ["diff", "-U0", "--no-color", f"{commit}^1", commit])
    introducing: set[int] = set()
    for path, ranges in parse_diff_hunks(diff_text).items():
        if _is_lockfile(path):
            continue
        args = ["blame", "--first-parent", "--porcelain"]
        for start, count in ranges:
            args += ["-L", f"{start},+{count}"]
        args += [f"{commit}^1", "--", path]
        proc = subprocess.run(["git", "-C", repo_path, *args], capture_output=True, text=True)
        if proc.returncode != 0:
            continue  # e.g. the path did not exist on the parent (pure rename/new file)
        for line in proc.stdout.splitlines():
            m = BLAME_SHA_RE.match(line)
            if m:
                number = sha_to_pr.get(m.group(1))
                if number is not None:
                    introducing.add(number)
    return introducing


def compute_followup_fixes(repo_path: str, base: str, prs: list[PR], fix_window_days: int) -> dict[int, set[int]]:
    sha_to_pr = build_sha_to_pr(repo_path, base)
    pr_to_sha: dict[int, str] = {}
    for sha, number in sha_to_pr.items():
        pr_to_sha.setdefault(number, sha)

    merged_by_number = {pr.number: pr.merged for pr in prs if pr.merged}

    result: dict[int, set[int]] = {}
    for pr in prs:
        if pr.merged is None or is_revert_title(pr.title) or not FOLLOWUP_TITLE_RE.match(pr.title):
            continue
        commit = pr_to_sha.get(pr.number)
        if commit is None:
            continue
        matched = set()
        for candidate in _blame_introducing_prs(repo_path, commit, sha_to_pr):
            if candidate == pr.number or candidate not in merged_by_number:
                continue
            delta = pr.merged - merged_by_number[candidate]
            if timedelta(0) <= delta <= timedelta(days=fix_window_days):
                matched.add(candidate)
        if matched:
            result[pr.number] = matched
    return result
