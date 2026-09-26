"""Blame-based (SZZ-style) follow-up-fix attribution. All git commands run
locally against a caller-provided clone; never fetches or writes.

Returns dict[fix_pr_number, set[introducing_pr_number]]; metrics.py stays
pure and takes that mapping as input."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from pr_outcomes.fetch import CACHE_ROOT, PR
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
    # errors="replace": a diff or blame can carry non-UTF-8 file content
    # (e.g. a Latin-1 source file); the lines this module parses (diff
    # headers, hunk headers, blame's porcelain sha) are always ASCII, so a
    # replacement character in unrelated content is harmless.
    proc = subprocess.run(
        ["git", "-C", repo_path, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
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


def _blame_cache_path(owner: str, name: str, sha: str) -> str:
    return os.path.join(CACHE_ROOT, f"{owner}__{name}", "blame", f"{sha}.json")


def _load_cached_blame(owner: str, name: str, sha: str) -> set[int] | None:
    path = _blame_cache_path(owner, name, sha)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return set(json.load(f))


def _save_cached_blame(owner: str, name: str, sha: str, introducing: set[int]) -> None:
    path = _blame_cache_path(owner, name, sha)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(sorted(introducing), f)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def compute_followup_fixes(
    repo_path: str, owner: str, name: str, base: str, prs: list[PR], fix_window_days: int,
    refresh: bool = False,
) -> dict[int, set[int]]:
    """The unfiltered blame result (introducing PRs before the fix-window
    check) is cached per commit sha, since it never changes; the window
    filter stays here because it depends on --fix-window-days."""
    sha_to_pr = build_sha_to_pr(repo_path, base)
    pr_to_sha: dict[int, str] = {}
    for sha, number in sha_to_pr.items():
        pr_to_sha.setdefault(number, sha)

    merged_by_number = {pr.number: pr.merged for pr in prs if pr.merged}

    candidates: list[tuple[PR, str]] = []
    for pr in prs:
        if pr.merged is None or is_revert_title(pr.title) or not FOLLOWUP_TITLE_RE.match(pr.title):
            continue
        commit = pr_to_sha.get(pr.number)
        if commit is not None:
            candidates.append((pr, commit))

    introducing_by_commit: dict[str, set[int]] = {}
    to_compute: list[str] = []
    for _, commit in candidates:
        if commit in introducing_by_commit or commit in to_compute:
            continue
        cached = None if refresh else _load_cached_blame(owner, name, commit)
        if cached is not None:
            introducing_by_commit[commit] = cached
        else:
            to_compute.append(commit)

    if to_compute:
        with ThreadPoolExecutor(max_workers=os.cpu_count()) as pool:
            futures = {
                commit: pool.submit(_blame_introducing_prs, repo_path, commit, sha_to_pr)
                for commit in to_compute
            }
            for commit, future in futures.items():
                introducing = future.result()
                introducing_by_commit[commit] = introducing
                _save_cached_blame(owner, name, commit, introducing)

    result: dict[int, set[int]] = {}
    for pr, commit in candidates:
        matched = set()
        for candidate in introducing_by_commit.get(commit, set()):
            if candidate == pr.number or candidate not in merged_by_number:
                continue
            delta = pr.merged - merged_by_number[candidate]
            if timedelta(0) <= delta <= timedelta(days=fix_window_days):
                matched.add(candidate)
        if matched:
            result[pr.number] = matched
    return result
