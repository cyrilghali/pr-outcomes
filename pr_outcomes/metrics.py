"""Pure metric functions: per-PR facts, approval classification, grouping,
aggregation. No I/O — everything here takes PR/Event dataclasses in and
returns plain data out."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from pr_outcomes.fetch import PR

# Approval-class thresholds, named so the "why" travels with the number.
RUBBER_STAMP_MIN_SIZE = 200  # below this a fast approval could still be a real read of a tiny diff
RUBBER_STAMP_MAX_MINUTES = 5  # GitHub's own "too fast to have been read" bar for a big diff
HOT_FILE_SHARE = 0.05  # files touched by >5% of PRs are shared/config noise, not a real coupling signal
LOCKFILES = {"mix.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"}

FOLLOWUP_TITLE_RE = re.compile(r"^(fix|hotfix|bugfix)\b", re.IGNORECASE)
REVERT_QUOTED_RE = re.compile(r'^Revert "(.+)"\s*$')
REVERT_PREFIX_RE = re.compile(r"^revert\b", re.IGNORECASE)


def is_revert_title(title: str) -> bool:
    return title.startswith('Revert "') or bool(REVERT_PREFIX_RE.match(title))


def _is_human_reviewer(pr: PR, actor) -> bool:
    return actor is not None and not actor.is_bot and actor.login != pr.author.login


@dataclass
class PRFacts:
    number: int
    time_to_first_human_review_h: float | None
    time_to_first_bot_review_h: float | None
    time_to_first_approval_h: float | None
    time_to_merge_h: float | None
    ready_to_merge_h: float | None
    review_rounds: int
    changes_requested: int
    human_comments: int
    bot_comments: int
    size: int
    changed_files: int
    reviewed_group: str
    approval_classes: dict = field(default_factory=dict)  # login -> class
    is_revert: bool = False
    reverted: bool = False
    reverted_by: list = field(default_factory=list)
    followup_fix: bool = False
    followup_fix_by: list = field(default_factory=list)
    labels: list = field(default_factory=list)
    author: str = ""


def _hours(a, b) -> float | None:
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 3600


def _first_event(pr: PR, predicate):
    for e in pr.events:
        if predicate(e):
            return e
    return None


def compute_review_rounds(pr: PR) -> int:
    human_events = [e for e in pr.events if e.kind in ("review", "comment") and _is_human_reviewer(pr, e.actor)]
    pushes = [e.at for e in pr.events if e.kind == "push"]
    rounds = 0
    for a, b in zip(human_events, human_events[1:]):
        if any(a.at < p < b.at for p in pushes):
            rounds += 1
    return rounds


def compute_approval_classes(pr: PR) -> dict:
    """One class per human non-author approver, keyed by that reviewer's
    first APPROVED review."""
    size = pr.additions + pr.deletions
    seen_approvers: set[str] = set()
    classes: dict[str, str] = {}
    pushes = sorted(e.at for e in pr.events if e.kind == "push")

    for e in pr.events:
        if e.kind != "review" or e.state != "APPROVED" or not _is_human_reviewer(pr, e.actor):
            continue
        login = e.actor.login
        if login in seen_approvers:
            continue
        seen_approvers.add(login)
        approval_time = e.at

        reviewer_comments = sorted(
            ev.at for ev in pr.events
            if ev.kind == "comment" and ev.actor and ev.actor.login == login and ev.at <= approval_time
        )
        if reviewer_comments:
            first_comment = reviewer_comments[0]
            pushed_after = any(p > first_comment for p in pushes)
            classes[login] = "substantive" if pushed_after else "commented"
            continue

        last_push_before = max((p for p in pushes if p < approval_time), default=None)
        request_times = [
            ev.at for ev in pr.events
            if ev.kind == "review_requested" and ev.requested == login and ev.at < approval_time
        ]
        last_request = max(request_times, default=None)
        reference = max(t for t in (pr.ready, last_push_before, last_request) if t is not None)
        minutes = (approval_time - reference).total_seconds() / 60
        if size >= RUBBER_STAMP_MIN_SIZE and 0 <= minutes < RUBBER_STAMP_MAX_MINUTES:
            classes[login] = "rubber_stamp"
        else:
            classes[login] = "silent"

    return classes


def compute_reviewed_group(pr: PR) -> str:
    for e in pr.events:
        if e.kind == "review" and e.state == "APPROVED" and _is_human_reviewer(pr, e.actor):
            return "human-approved"
    for e in pr.events:
        if e.kind in ("review", "comment") and e.actor and e.actor.is_bot:
            return "bot-only"
    return "no-review"


def compute_hot_files(prs: list[PR]) -> set[str]:
    if not prs:
        return set(LOCKFILES)
    counts = Counter()
    for pr in prs:
        counts.update(set(pr.files))
    threshold = HOT_FILE_SHARE * len(prs)
    hot = {path for path, n in counts.items() if n > threshold}
    return hot | LOCKFILES


def find_original(revert_pr: PR, prs_by_number: dict) -> PR | None:
    for m in re.finditer(r"#(\d+)", revert_pr.body):
        num = int(m.group(1))
        if num != revert_pr.number and num in prs_by_number:
            return prs_by_number[num]
    for pr in prs_by_number.values():
        if pr.number != revert_pr.number and pr.url in revert_pr.body:
            return pr
    m = REVERT_QUOTED_RE.match(revert_pr.title)
    if m:
        candidate_title = m.group(1)
        for pr in prs_by_number.values():
            if pr.number != revert_pr.number and pr.title == candidate_title:
                return pr
    else:
        for pr in prs_by_number.values():
            if pr.number != revert_pr.number and pr.title and pr.title.lower() in revert_pr.title.lower():
                return pr
    return None


def compute_reverts(prs: list[PR]) -> dict:
    """number -> list of revert PR numbers that reverted it."""
    prs_by_number = {pr.number: pr for pr in prs}
    reverted_by: dict[int, list[int]] = defaultdict(list)
    for pr in prs:
        if is_revert_title(pr.title):
            original = find_original(pr, prs_by_number)
            if original is not None:
                reverted_by[original.number].append(pr.number)
    return dict(reverted_by)


def find_followups(pr: PR, prs: list[PR], hot_files: set[str], fix_window_days: int) -> list[int]:
    if pr.merged is None:
        return []
    relevant_files = set(pr.files) - hot_files
    if not relevant_files:
        return []
    window_end = pr.merged + timedelta(days=fix_window_days)
    fixes = []
    for q in prs:
        if q.number == pr.number or q.merged is None:
            continue
        if not (pr.merged < q.merged <= window_end):
            continue
        if is_revert_title(q.title):
            continue
        if not FOLLOWUP_TITLE_RE.match(q.title):
            continue
        shared = (relevant_files & set(q.files)) - hot_files
        if shared:
            fixes.append(q.number)
    return fixes


def compute_all_facts(prs: list[PR], fix_window_days: int) -> dict:
    """Per-PR facts for the whole fetched set (needed because reverts and
    follow-up fixes can land after --until)."""
    hot_files = compute_hot_files(prs)
    reverted_by = compute_reverts(prs)
    revert_numbers = {n for pr in prs if is_revert_title(pr.title) for n in [pr.number]}

    facts = {}
    for pr in prs:
        followups = find_followups(pr, prs, hot_files, fix_window_days)
        facts[pr.number] = PRFacts(
            number=pr.number,
            time_to_first_human_review_h=_hours(pr.created, _first_event_time(pr, lambda e: e.kind in ("review", "comment") and _is_human_reviewer(pr, e.actor))),
            time_to_first_bot_review_h=_hours(pr.created, _first_event_time(pr, lambda e: e.kind in ("review", "comment") and e.actor and e.actor.is_bot)),
            time_to_first_approval_h=_hours(pr.created, _first_event_time(pr, lambda e: e.kind == "review" and e.state == "APPROVED" and _is_human_reviewer(pr, e.actor))),
            time_to_merge_h=_hours(pr.created, pr.merged),
            ready_to_merge_h=_hours(pr.ready, pr.merged),
            review_rounds=compute_review_rounds(pr),
            changes_requested=sum(1 for e in pr.events if e.kind == "review" and e.state == "CHANGES_REQUESTED"),
            human_comments=sum(1 for e in pr.events if e.kind == "comment" and e.actor and not e.actor.is_bot),
            bot_comments=sum(1 for e in pr.events if e.kind == "comment" and e.actor and e.actor.is_bot),
            size=pr.additions + pr.deletions,
            changed_files=pr.changed_files,
            reviewed_group=compute_reviewed_group(pr),
            approval_classes=compute_approval_classes(pr),
            is_revert=pr.number in revert_numbers,
            reverted=pr.number in reverted_by,
            reverted_by=reverted_by.get(pr.number, []),
            followup_fix=bool(followups),
            followup_fix_by=followups,
            labels=pr.labels,
            author=pr.author.login,
        )
    return facts


def _first_event_time(pr: PR, predicate):
    e = _first_event(pr, predicate)
    return e.at if e else None


# --- Grouping -----------------------------------------------------------

def group_prs(prs: list[PR], facts: dict, group_by: str | None) -> dict:
    groups: dict[str, list[PR]] = defaultdict(list)
    if group_by is None:
        groups["all"] = list(prs)
    elif group_by == "reviewed":
        for pr in prs:
            groups[facts[pr.number].reviewed_group].append(pr)
    elif group_by == "author":
        for pr in prs:
            groups[pr.author.login].append(pr)
    elif group_by == "label":
        for pr in prs:
            if pr.labels:
                for label in pr.labels:
                    groups[label].append(pr)
            else:
                groups["(none)"].append(pr)
    elif group_by == "approver":
        for pr in prs:
            approvers = facts[pr.number].approval_classes.keys()
            if approvers:
                for login in approvers:
                    groups[login].append(pr)
            else:
                groups["(none)"].append(pr)
    else:
        raise ValueError(f"unknown group-by: {group_by}")
    return dict(groups)


# --- Aggregation ----------------------------------------------------------

def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _median_p75(values: list[float]) -> tuple[float | None, float | None]:
    clean = [v for v in values if v is not None]
    med = percentile(clean, 0.5)
    p75 = percentile(clean, 0.75)
    return (round(med, 1) if med is not None else None, round(p75, 1) if p75 is not None else None)


def _mean(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 2)


def _share(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 3)


def aggregate_group(prs: list[PR], facts: dict, since: date, until: date) -> dict:
    group_facts = [facts[pr.number] for pr in prs]
    non_revert = [f for f in group_facts if not f.is_revert]
    n_non_revert = len(non_revert)

    out: dict = {"count": len(prs)}

    # Outcomes
    reverted_count = sum(1 for f in non_revert if f.reverted)
    followup_count = sum(1 for f in non_revert if f.followup_fix)
    out["revert_rate"] = _share(reverted_count, n_non_revert)
    out["revert_count"] = reverted_count
    out["followup_fix_rate"] = _share(followup_count, n_non_revert)
    out["followup_fix_count"] = followup_count

    # Review depth
    all_classes = [cls for f in group_facts for cls in f.approval_classes.values()]
    n_approvals = len(all_classes)
    class_counts = Counter(all_classes)
    for cls in ("substantive", "commented", "silent", "rubber_stamp"):
        out[f"approval_share_{cls}"] = _share(class_counts.get(cls, 0), n_approvals)
    out["approval_count"] = n_approvals

    rounds = [f.review_rounds for f in group_facts]
    out["review_rounds_mean"] = _mean(rounds)
    out["review_rounds_ge1_share"] = _share(sum(1 for r in rounds if r >= 1), len(rounds))
    out["human_comments_mean"] = _mean([f.human_comments for f in group_facts])
    out["bot_comments_mean"] = _mean([f.bot_comments for f in group_facts])

    # Speed
    for metric in (
        "time_to_first_human_review_h", "time_to_first_bot_review_h",
        "time_to_first_approval_h", "time_to_merge_h", "ready_to_merge_h",
    ):
        med, p75 = _median_p75([getattr(f, metric) for f in group_facts])
        out[f"{metric}_median"] = med
        out[f"{metric}_p75"] = p75

    merge_times = [f.time_to_merge_h for f in group_facts if f.time_to_merge_h is not None]
    out["merged_within_1h"] = _share(sum(1 for t in merge_times if t <= 1), len(merge_times))
    out["merged_within_24h"] = _share(sum(1 for t in merge_times if t <= 24), len(merge_times))

    weeks = max((until - since).days / 7, 1 / 7)
    out["throughput_per_week"] = round(len(prs) / weeks, 2)
    week_counts: Counter = Counter()
    for pr in prs:
        if pr.merged:
            iso = pr.merged.isocalendar()
            week_counts[f"{iso[0]}-W{iso[1]:02d}"] += 1
    out["throughput_by_week"] = dict(sorted(week_counts.items()))

    size_med, size_p75 = _median_p75([f.size for f in group_facts])
    out["size_median"] = size_med
    out["size_p75"] = size_p75

    return out
