"""Sentry new-issue attribution: for every new production issue since the
report window opened, blame its oldest event's in-app stack frames at the
deploy that shipped, and keep only PRs that shipped in that same deploy.
Never writes to Sentry; every call here is a read."""

from __future__ import annotations

import configparser
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import TYPE_CHECKING, Any

from pr_outcomes.blame import BLAME_SHA_RE
from pr_outcomes.fetch import CACHE_ROOT, RETRY_BACKOFF_SECONDS

if TYPE_CHECKING:
    from pr_outcomes.deploys import Deploy

BASE_URL = "https://sentry.io"
LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next";\s*results="true"')


class SentryError(RuntimeError):
    pass


def load_token() -> str | None:
    token = os.environ.get("SENTRY_AUTH_TOKEN")
    if token:
        return token
    path = os.path.expanduser("~/.sentryclirc")
    if not os.path.exists(path):
        return None
    config = configparser.ConfigParser()
    config.read(path)
    return config.get("auth", "token", fallback=None)


def _get(path_or_url: str, params: dict[str, Any] | None = None) -> tuple[Any, dict[str, str]]:
    token = load_token()
    if not token:
        raise SentryError("no Sentry auth token: set SENTRY_AUTH_TOKEN or run `sentry-cli login`")

    url = path_or_url if path_or_url.startswith("http") else BASE_URL + path_or_url
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})

    for attempt, backoff in enumerate((*RETRY_BACKOFF_SECONDS, None)):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read()), dict(resp.headers.items())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if backoff is not None and (e.code == 429 or e.code >= 500):
                time.sleep(backoff)
                continue
            raise SentryError(f"HTTP {e.code}: {body[:200]}")
        # HTTPError is a URLError subclass, so this only catches a connection
        # failure or timeout, not an HTTP error response -- both are worth
        # the same retry as a 429/5xx before giving up.
        except (urllib.error.URLError, TimeoutError) as e:
            if backoff is not None:
                time.sleep(backoff)
                continue
            raise SentryError(f"connection error: {e}")
    raise AssertionError("unreachable")


def _project_id(org: str, project: str) -> str:
    data, _ = _get(f"/api/0/projects/{org}/{project}/")
    return data["id"]


def fetch_new_issues(org: str, project: str, since: date, end_exclusive: date) -> list[str]:
    """shortIds of new production issues with firstSeen in [since, end_exclusive)."""
    project_id = _project_id(org, project)
    since_s = f"{since.isoformat()}T00:00:00"
    end_s = f"{end_exclusive.isoformat()}T00:00:00"
    params = {
        "project": project_id,
        "environment": "production",
        "query": f"firstSeen:>={since_s} firstSeen:<{end_s}",
        "start": since_s,
        "end": end_s,
        "limit": 100,
    }

    short_ids: list[str] = []
    path_or_url = f"/api/0/organizations/{org}/issues/"
    while True:
        data, headers = _get(path_or_url, params if path_or_url.startswith("/") else None)
        short_ids.extend(issue["shortId"] for issue in data)
        link = headers.get("Link") or headers.get("link") or ""
        m = LINK_NEXT_RE.search(link)
        if not m:
            break
        path_or_url = m.group(1)
    return short_ids


def extract_frames(event: dict[str, Any]) -> tuple[str | None, list[tuple[str, int]]]:
    """(release, in-app (file, line) frames, deduped, order kept)."""
    release = event.get("release")
    if isinstance(release, dict):
        release = release.get("version")

    frames: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for entry in event.get("entries", []):
        if entry.get("type") != "exception":
            continue
        for value in entry.get("data", {}).get("values", []):
            stacktrace = value.get("stacktrace")
            if not stacktrace:
                continue
            for frame in stacktrace.get("frames", []):
                if not frame.get("inApp"):
                    continue
                filename, line_no = frame.get("filename"), frame.get("lineNo")
                if not filename or not line_no:
                    continue
                key = (filename, line_no)
                if key not in seen:
                    seen.add(key)
                    frames.append(key)
    return release, frames


def _event_cache_path(owner: str, name: str, short_id: str) -> str:
    return os.path.join(CACHE_ROOT, f"{owner}__{name}", "sentry", f"{short_id}.json")


def _load_cached_event(owner: str, name: str, short_id: str) -> tuple[str | None, list[tuple[str, int]]] | None:
    path = _event_cache_path(owner, name, short_id)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        raw = json.load(f)
    return raw["release"], [tuple(fr) for fr in raw["frames"]]


def _save_cached_event(
    owner: str, name: str, short_id: str, release: str | None, frames: list[tuple[str, int]],
) -> None:
    path = _event_cache_path(owner, name, short_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"release": release, "frames": [list(fr) for fr in frames]}, f)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def fetch_oldest_event(org: str, short_id: str) -> dict[str, Any]:
    data, _ = _get(f"/api/0/organizations/{org}/issues/{short_id}/events/oldest/", {"environment": "production"})
    return data


def _fetch_and_cache_event(org: str, owner: str, name: str, short_id: str) -> tuple[str, str | None, list[tuple[str, int]]]:
    event = fetch_oldest_event(org, short_id)
    release, frames = extract_frames(event)
    _save_cached_event(owner, name, short_id, release, frames)
    return short_id, release, frames


def load_oldest_events(
    org: str, owner: str, name: str, short_ids: list[str], refresh: bool = False,
) -> dict[str, tuple[str | None, list[tuple[str, int]]]]:
    """shortId -> (release, frames), cached forever on disk, bypassed by
    refresh. Uncached ones are fetched 8 at a time."""
    result: dict[str, tuple[str | None, list[tuple[str, int]]]] = {}
    to_fetch: list[str] = []
    for short_id in short_ids:
        cached = None if refresh else _load_cached_event(owner, name, short_id)
        if cached is not None:
            result[short_id] = cached
        else:
            to_fetch.append(short_id)

    if to_fetch:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for short_id, release, frames in pool.map(
                lambda s: _fetch_and_cache_event(org, owner, name, s), to_fetch,
            ):
                result[short_id] = (release, frames)
    return result


def _run_blame(repo_path: str, commit: str, path: str, lines: list[int]) -> str | None:
    args = ["blame", "--first-parent", "--porcelain"]
    for n in lines:
        args += ["-L", f"{n},{n}"]
    args += [commit, "--", path]
    proc = subprocess.run(
        ["git", "-C", repo_path, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.stdout if proc.returncode == 0 else None


def blamed_prs(repo_path: str, commit: str, frames: list[tuple[str, int]], sha_to_pr: dict[str, int]) -> set[int]:
    """One `git blame` per file, covering every blamed line in that file at
    once, mapped back to introducing PRs via sha_to_pr."""
    lines_by_file: dict[str, list[int]] = defaultdict(list)
    for filename, line_no in frames:
        lines_by_file[filename].append(line_no)

    prs: set[int] = set()
    for path, lines in lines_by_file.items():
        output = _run_blame(repo_path, commit, path, lines)
        if output is None:
            # The batched call failed -- e.g. the file doesn't exist at this
            # commit, or one requested line is past its end. Retry each line
            # alone so one bad line doesn't drop the file's valid ones.
            per_line_outputs: list[str] = []
            for n in lines:
                single = _run_blame(repo_path, commit, path, [n])
                if single is not None:
                    per_line_outputs.append(single)
            output = "".join(per_line_outputs)
        for line in output.splitlines():
            m = BLAME_SHA_RE.match(line)
            if m:
                number = sha_to_pr.get(m.group(1))
                if number is not None:
                    prs.add(number)
    return prs


def attribute_new_issues(
    repo_path: str, owner: str, name: str, sentry_project: str,
    since: date, end_exclusive: date, deploys: list[Deploy], sha_to_pr: dict[str, int],
    refresh: bool = False,
) -> tuple[dict[int, list[str]], list[str]]:
    """PR number -> sorted shortIds it's blamed for. Each new issue is
    attributed via its oldest event's release (== a Deploy.sha), blaming at
    that deploy's release_head and keeping only PRs that deploy shipped."""
    org, project = sentry_project.split("/", 1)
    short_ids = fetch_new_issues(org, project, since, end_exclusive)
    events = load_oldest_events(org, owner, name, short_ids, refresh=refresh)
    deploy_by_sha = {d.sha: d for d in deploys}

    no_frames = 0
    unknown_release = 0
    tasks: list[tuple[str, Deploy]] = []
    for short_id in short_ids:
        release, frames = events.get(short_id, (None, []))
        if not frames:
            no_frames += 1
            continue
        deploy = deploy_by_sha.get(release) if release is not None else None
        if deploy is None:
            unknown_release += 1
            continue
        tasks.append((short_id, deploy))

    def _work(item: tuple[str, Deploy]) -> tuple[str, set[int]]:
        short_id, deploy = item
        _, frames = events[short_id]
        return short_id, blamed_prs(repo_path, deploy.release_head, frames, sha_to_pr) & deploy.prs

    with ThreadPoolExecutor(max_workers=os.cpu_count()) as pool:
        results = list(pool.map(_work, tasks)) if tasks else []

    attributed: dict[int, list[str]] = defaultdict(list)
    n_attributed = 0
    for short_id, prs in results:
        if prs:
            n_attributed += 1
        for pr in prs:
            attributed[pr].append(short_id)
    for pr in attributed:
        attributed[pr].sort()

    warning = (
        f"sentry: {n_attributed} of {len(short_ids)} new production issues attributed to a PR "
        f"shipped in the same deploy ({no_frames} without app stack frames, {unknown_release} on "
        "a release that is not a known deploy)"
    )
    return dict(attributed), [warning]
