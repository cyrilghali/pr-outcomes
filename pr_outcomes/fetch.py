"""GitHub GraphQL fetching: gh CLI calls, pagination, disk cache, and
normalisation of raw search-API JSON into PR dataclasses."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from datetime import time as time_of_day

CACHE_ROOT = os.path.expanduser("~/.cache/pr-outcomes")

# GitHub's GraphQL API silently truncates timelineItems (and other nested
# connections) once a single request resolves more than a handful of PRs'
# worth of them, with no signal in the response that it happened. So the
# search query below carries no timelineItems: each PR's timeline is fetched
# in its own request by _fetch_pr_timeline. totalCount on the other
# connections lets _warn_truncated_connections catch the same failure mode
# there without needing a second request for those.
QUERY = """
query($q:String!,$cursor:String){ search(query:$q,type:ISSUE,first:25,after:$cursor){ issueCount pageInfo{hasNextPage endCursor} nodes{ ... on PullRequest{
 number title body url createdAt mergedAt baseRefName additions deletions changedFiles
 author{login __typename}
 labels(first:20){nodes{name}}
 comments(first:50){totalCount nodes{author{login __typename} createdAt}}
 reviews(first:50){totalCount nodes{author{login __typename} state submittedAt body comments{totalCount}}}
 reviewThreads(first:50){totalCount nodes{path comments(first:20){nodes{author{login __typename} createdAt}}}}
}}}}
"""

TIMELINE_ITEM_TYPES = "[PULL_REQUEST_COMMIT,HEAD_REF_FORCE_PUSHED_EVENT,READY_FOR_REVIEW_EVENT,REVIEW_REQUESTED_EVENT]"

TIMELINE_QUERY = f"""
query($owner:String!,$name:String!,$number:Int!,$cursor:String){{ repository(owner:$owner,name:$name){{ pullRequest(number:$number){{
 timelineItems(first:100,after:$cursor,itemTypes:{TIMELINE_ITEM_TYPES}){{ pageInfo{{hasNextPage endCursor}} nodes{{__typename
   ... on PullRequestCommit{{commit{{committedDate}}}}
   ... on HeadRefForcePushedEvent{{createdAt}}
   ... on ReadyForReviewEvent{{createdAt}}
   ... on ReviewRequestedEvent{{createdAt requestedReviewer{{... on User{{login}} ... on Bot{{login}}}}}}}}}}
}}}}}}
"""

DEFAULT_BRANCH_QUERY = """
query($owner:String!,$name:String!){ repository(owner:$owner,name:$name){ defaultBranchRef{ name } } }
"""

TRUNCATION_WARNING_KEYS = ("reviews", "comments", "reviewThreads")


@dataclass(frozen=True)
class Actor:
    login: str
    is_bot: bool


@dataclass(frozen=True)
class Event:
    at: datetime
    kind: str  # "push" | "review" | "comment" | "ready" | "review_requested"
    actor: Actor | None = None
    state: str | None = None
    path: str | None = None
    requested: str | None = None


@dataclass
class PR:
    number: int
    title: str
    body: str
    url: str
    author: Actor
    base: str
    created: datetime
    merged: datetime | None
    ready: datetime
    additions: int
    deletions: int
    changed_files: int
    labels: list[str] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)


class GhError(RuntimeError):
    pass


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


TRANSIENT_ERROR_MARKERS = (
    "rate limit", "abuse", "was submitted too quickly", "timeout", "http 502", "http 504",
)
RETRY_BACKOFF_SECONDS = (2, 4, 8, 16)


def _is_transient_gh_error(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in TRANSIENT_ERROR_MARKERS)


def _run_gh_graphql(query: str, variables: dict) -> dict:
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is None:
            continue
        # -F (not -f) so an int variable (e.g. $number:Int!) is sent typed,
        # not as the string -f always produces.
        flag = "-F" if isinstance(value, int) else "-f"
        args += [flag, f"{key}={value}"]

    for attempt, backoff in enumerate((*RETRY_BACKOFF_SECONDS, None)):
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        stderr = proc.stderr.strip()
        if backoff is None or not _is_transient_gh_error(stderr):
            raise GhError(stderr)
        print(
            f"pr-outcomes: transient gh error ({stderr[:120]}), retrying in {backoff}s "
            f"(attempt {attempt + 1}/{len(RETRY_BACKOFF_SECONDS)})",
            file=sys.stderr,
        )
        time.sleep(backoff)
    raise AssertionError("unreachable")


def get_default_branch(owner: str, name: str) -> str:
    args = [
        "gh", "api", "graphql",
        "-f", f"query={DEFAULT_BRANCH_QUERY}",
        "-f", f"owner={owner}",
        "-f", f"name={name}",
    ]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip())
    data = json.loads(proc.stdout)
    ref = data["data"]["repository"]["defaultBranchRef"]
    if ref is None:
        raise GhError(f"{owner}/{name} has no default branch")
    return ref["name"]


def _actor_from_json(raw: dict | None) -> Actor:
    if raw is None:
        return Actor(login="ghost", is_bot=False)
    login = raw.get("login", "ghost")
    is_bot = raw.get("__typename") == "Bot" or login.endswith("[bot]")
    return Actor(login=login, is_bot=is_bot)


def merge_timeline_into_node(node: dict, timeline_nodes: list[dict]) -> dict:
    """Return a copy of `node` with its timelineItems replaced by
    `timeline_nodes`, fetched separately per-PR. Pure so the merge can be
    unit-tested without a network call."""
    merged = dict(node)
    merged["timelineItems"] = {"nodes": timeline_nodes}
    return merged


def normalise_pr(node: dict) -> PR:
    """Turn one raw GraphQL PullRequest node into a PR dataclass. Pure and
    network-free so tests can feed hand-built fixtures through it."""
    author = _actor_from_json(node.get("author"))
    events: list[Event] = []
    ready_at: datetime | None = None

    for item in node.get("timelineItems", {}).get("nodes", []):
        typename = item.get("__typename")
        if typename == "PullRequestCommit":
            committed = item.get("commit", {}).get("committedDate")
            if committed:
                events.append(Event(at=_parse_dt(committed), kind="push"))
        elif typename == "HeadRefForcePushedEvent":
            events.append(Event(at=_parse_dt(item["createdAt"]), kind="push"))
        elif typename == "ReadyForReviewEvent":
            at = _parse_dt(item["createdAt"])
            events.append(Event(at=at, kind="ready"))
            if ready_at is None or at < ready_at:
                ready_at = at
        elif typename == "ReviewRequestedEvent":
            requested_raw = item.get("requestedReviewer")
            requested = requested_raw.get("login") if requested_raw else None
            events.append(Event(
                at=_parse_dt(item["createdAt"]),
                kind="review_requested",
                requested=requested,
            ))

    for review in node.get("reviews", {}).get("nodes", []):
        reviewer = _actor_from_json(review.get("author"))
        if reviewer.login == author.login:
            continue  # the PR author's own reviews never count
        at = _parse_dt(review["submittedAt"])
        state = review.get("state")
        events.append(Event(at=at, kind="review", actor=reviewer, state=state))
        body = (review.get("body") or "").strip()
        comment_count = review.get("comments", {}).get("totalCount", 0)
        # An approval's own body ("LGTM", a thumbs-up) is not engagement: it
        # never prompted a re-look. Inline comments attached to the approval
        # still count, since those are real, addressable feedback.
        counts_as_comment = comment_count > 0 or (body and state != "APPROVED")
        if counts_as_comment:
            events.append(Event(at=at, kind="comment", actor=reviewer))

    for comment in node.get("comments", {}).get("nodes", []):
        commenter = _actor_from_json(comment.get("author"))
        if commenter.login == author.login:
            continue
        events.append(Event(at=_parse_dt(comment["createdAt"]), kind="comment", actor=commenter))

    for thread in node.get("reviewThreads", {}).get("nodes", []):
        path = thread.get("path")
        for comment in thread.get("comments", {}).get("nodes", []):
            commenter = _actor_from_json(comment.get("author"))
            if commenter.login == author.login:
                continue
            events.append(Event(
                at=_parse_dt(comment["createdAt"]),
                kind="comment",
                actor=commenter,
                path=path,
            ))

    events.sort(key=lambda e: e.at)
    created = _parse_dt(node["createdAt"])
    merged = _parse_dt(node["mergedAt"]) if node.get("mergedAt") else None

    return PR(
        number=node["number"],
        title=node["title"],
        body=node.get("body") or "",
        url=node["url"],
        author=author,
        base=node["baseRefName"],
        created=created,
        merged=merged,
        ready=ready_at if ready_at is not None else created,
        additions=node.get("additions", 0),
        deletions=node.get("deletions", 0),
        changed_files=node.get("changedFiles", 0),
        labels=[l["name"] for l in node.get("labels", {}).get("nodes", [])],
        events=events,
    )


def _week_chunks(start: date, end: date):
    """Yield (from_date, to_date) Monday-Sunday chunks covering [start, end]."""
    cur = start - timedelta(days=start.weekday())  # back up to Monday
    while cur <= end:
        chunk_end = min(cur + timedelta(days=6), end)
        chunk_start = max(cur, start)
        yield chunk_start, chunk_end
        cur += timedelta(days=7)


def _cache_path(owner: str, name: str, base: str, frm: date, to: date) -> str:
    # v2: the query shape changed (timelineItems moved to a per-PR fetch);
    # bump this segment again if the cached node shape changes further.
    return os.path.join(
        CACHE_ROOT, "v2", f"{owner}__{name}", base, f"{frm.isoformat()}_{to.isoformat()}.json"
    )


def _warn_truncated_connections(nodes: list[dict], warnings: list[str]) -> None:
    for n in nodes:
        for key in TRUNCATION_WARNING_KEYS:
            conn = n.get(key) or {}
            total = conn.get("totalCount")
            returned = len(conn.get("nodes", []))
            if total is not None and total > returned:
                msg = f"PR #{n.get('number')} {key} truncated ({returned}/{total}, not refetched)"
                print(f"pr-outcomes: {msg}", file=sys.stderr)
                warnings.append(msg)


def _fetch_pr_timeline(owner: str, name: str, number: int) -> list[dict]:
    nodes: list[dict] = []
    cursor = None
    while True:
        variables = {"owner": owner, "name": name, "number": number, "cursor": cursor}
        data = _run_gh_graphql(TIMELINE_QUERY, variables)
        timeline = data["data"]["repository"]["pullRequest"]["timelineItems"]
        nodes.extend(timeline["nodes"])
        page_info = timeline["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
    return nodes


def _fetch_chunk_nodes(owner: str, name: str, base: str, frm: date, to: date, warnings: list[str]) -> list[dict]:
    q = f"repo:{owner}/{name} is:pr is:merged base:{base} merged:{frm.isoformat()}..{to.isoformat()}"
    nodes: list[dict] = []
    cursor = None
    while True:
        variables = {"q": q, "cursor": cursor}
        data = _run_gh_graphql(QUERY, variables)
        search = data["data"]["search"]
        nodes.extend(n for n in search["nodes"] if n)
        page_info = search["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]

    _warn_truncated_connections(nodes, warnings)

    # One request per PR, so GitHub's silent per-request truncation of
    # timelineItems (see the QUERY comment above) can't drop force-pushes.
    with ThreadPoolExecutor(max_workers=4) as pool:
        timelines = list(pool.map(lambda n: _fetch_pr_timeline(owner, name, n["number"]), nodes))
    return [merge_timeline_into_node(n, t) for n, t in zip(nodes, timelines)]


def _chunk_reusable(path: str, to: date) -> bool:
    """A cached chunk is only safe to reuse once the chunk's last day is
    fully over in UTC (GitHub search dates are UTC) as of when the cache
    file was written. A chunk fetched while its last day was still in
    progress must be refetched, or PRs merged later that day are silently
    lost forever."""
    if not os.path.exists(path):
        return False
    chunk_end = datetime.combine(to + timedelta(days=1), time_of_day(), tzinfo=timezone.utc)
    mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
    return mtime > chunk_end


def _progress(msg: str, verbose: bool) -> None:
    if verbose or sys.stderr.isatty():
        print(f"pr-outcomes: {msg}", file=sys.stderr)


def _load_or_fetch_chunk(
    owner: str, name: str, base: str, frm: date, to: date, refresh: bool, warnings: list[str], verbose: bool,
) -> list[dict]:
    path = _cache_path(owner, name, base, frm, to)
    if not refresh and _chunk_reusable(path, to):
        _progress(f"cache hit {frm}..{to}", verbose)
        with open(path) as f:
            return json.load(f)

    _progress(f"fetching {owner}/{name} {frm}..{to}", verbose)
    started = time.time()
    nodes = _fetch_chunk_nodes(owner, name, base, frm, to, warnings)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(nodes, f)
        os.replace(tmp_path, path)
        # Stamp with the fetch start: _chunk_reusable must not trust PRs
        # merged between the search and the write.
        os.utime(path, (started, started))
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    return nodes


def fetch_prs(
    owner: str, name: str, base: str, since: date, until: date, fix_window_days: int,
    refresh: bool = False, verbose: bool = False,
) -> tuple[list[PR], list[str]]:
    """Fetch every merged PR in [since, min(until + fix_window_days, today)],
    normalised to PR dataclasses, newest-fetch-range chunks refetched, older
    ones cached forever on disk. Returns (prs, warnings) -- warnings are also
    printed to stderr as they're found. Progress lines ("cache hit",
    "fetching") print only when stderr is a TTY or verbose=True; warnings and
    errors always print regardless."""
    today = datetime.now(timezone.utc).date()
    fetch_until = min(until + timedelta(days=fix_window_days), today)
    all_nodes: list[dict] = []
    warnings: list[str] = []
    for frm, to in _week_chunks(since, fetch_until):
        all_nodes.extend(_load_or_fetch_chunk(owner, name, base, frm, to, refresh, warnings, verbose))
    return [normalise_pr(n) for n in all_nodes], warnings
