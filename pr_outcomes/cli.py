"""argparse entry point plus table/JSON rendering."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta

from pr_outcomes import fetch, metrics

OUTCOMES_ROWS = [
    ("revert_rate", "Revert rate"),
    ("followup_fix_rate", "Follow-up fix rate"),
]
REVIEW_DEPTH_ROWS = [
    ("approval_share_substantive", "Approvals: substantive"),
    ("approval_share_commented", "Approvals: commented"),
    ("approval_share_silent", "Approvals: silent"),
    ("approval_share_rubber_stamp", "Approvals: rubber-stamp"),
    ("review_rounds_mean", "Review rounds (mean)"),
    ("review_rounds_ge1_share", "PRs with >=1 round"),
    ("human_comments_mean", "Human comments (mean)"),
    ("bot_comments_mean", "Bot comments (mean)"),
]
SPEED_ROWS = [
    ("time_to_first_human_review_h_median", "Time to first human review, h (median)"),
    ("time_to_first_human_review_h_p75", "Time to first human review, h (p75)"),
    ("time_to_first_bot_review_h_median", "Time to first bot review, h (median)"),
    ("time_to_first_approval_h_median", "Time to first approval, h (median)"),
    ("time_to_merge_h_median", "Time to merge, h (median)"),
    ("time_to_merge_h_p75", "Time to merge, h (p75)"),
    ("ready_to_merge_h_median", "Ready to merge, h (median)"),
    ("merged_within_1h", "Merged within 1h"),
    ("merged_within_24h", "Merged within 24h"),
    ("throughput_per_week", "Throughput / week"),
    ("size_median", "Size, lines (median)"),
    ("size_p75", "Size, lines (p75)"),
]


def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.1%}" if 0 <= value <= 1 and value != int(value) else f"{value:g}"
    return str(value)


def _fmt_share(value) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


SHARE_KEYS = {
    "revert_rate", "followup_fix_rate", "approval_share_substantive",
    "approval_share_commented", "approval_share_silent", "approval_share_rubber_stamp",
    "review_rounds_ge1_share", "merged_within_1h", "merged_within_24h",
}


def render_table(groups: dict) -> str:
    names = list(groups.keys())
    col_width = max([len(n) for n in names] + [12]) + 2
    label_width = max(len(label) for _, label in OUTCOMES_ROWS + REVIEW_DEPTH_ROWS + SPEED_ROWS) + 2

    lines = []

    def header(label):
        return label.ljust(label_width) + "".join(n.rjust(col_width) for n in names)

    lines.append(header(""))
    lines.append("PR count".ljust(label_width) + "".join(str(groups[n]["count"]).rjust(col_width) for n in names))
    lines.append("")

    def section(title, rows):
        lines.append(f"-- {title} --")
        for key, label in rows:
            values = []
            for n in names:
                v = groups[n].get(key)
                values.append(_fmt_share(v) if key in SHARE_KEYS else _fmt(v))
            lines.append(label.ljust(label_width) + "".join(v.rjust(col_width) for v in values))
        lines.append("")

    section("Outcomes", OUTCOMES_ROWS)
    section("Review depth", REVIEW_DEPTH_ROWS)
    section("Speed", SPEED_ROWS)

    return "\n".join(lines).rstrip() + "\n"


def build_pr_json(pr, fact) -> dict:
    return {
        "number": pr.number,
        "title": pr.title,
        "url": pr.url,
        "author": fact.author,
        "labels": fact.labels,
        "created": pr.created.isoformat(),
        "merged": pr.merged.isoformat() if pr.merged else None,
        "time_to_first_human_review_h": fact.time_to_first_human_review_h,
        "time_to_first_bot_review_h": fact.time_to_first_bot_review_h,
        "time_to_first_approval_h": fact.time_to_first_approval_h,
        "time_to_merge_h": fact.time_to_merge_h,
        "ready_to_merge_h": fact.ready_to_merge_h,
        "review_rounds": fact.review_rounds,
        "changes_requested": fact.changes_requested,
        "human_comments": fact.human_comments,
        "bot_comments": fact.bot_comments,
        "size": fact.size,
        "changed_files": fact.changed_files,
        "reviewed_group": fact.reviewed_group,
        "approval_classes": fact.approval_classes,
        "is_revert": fact.is_revert,
        "reverted": fact.reverted,
        "reverted_by": fact.reverted_by,
        "followup_fix": fact.followup_fix,
        "followup_fix_by": fact.followup_fix_by,
    }


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="pr-outcomes", description="PR review/outcome diagnostics from GitHub's GraphQL API")
    p.add_argument("repo", help="owner/repo")
    p.add_argument("--since", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    p.add_argument("--until", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    p.add_argument("--base", default=None)
    p.add_argument("--group-by", choices=["reviewed", "label", "author", "approver"], default=None)
    p.add_argument("--fix-window-days", type=int, default=7)
    p.add_argument("--json", action="store_true")
    p.add_argument("--refresh", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    today = date.today()
    since = args.since or (today - timedelta(days=90))
    until = args.until or today

    try:
        owner, name = args.repo.split("/", 1)
    except ValueError:
        print("pr-outcomes: repo must be OWNER/NAME", file=sys.stderr)
        return 2

    try:
        base = args.base or fetch.get_default_branch(owner, name)
        print(f"pr-outcomes: base={base} since={since} until={until}", file=sys.stderr)
        all_prs = fetch.fetch_prs(owner, name, base, since, until, args.fix_window_days, refresh=args.refresh)
    except fetch.GhError as e:
        print(f"pr-outcomes: gh error: {e}", file=sys.stderr)
        return 1

    facts = metrics.compute_all_facts(all_prs, args.fix_window_days)
    reportable = [pr for pr in all_prs if pr.merged and since <= pr.merged.date() <= until]

    groups_prs = metrics.group_prs(reportable, facts, args.group_by)
    groups = {
        name_: metrics.aggregate_group(prs, facts, since, until)
        for name_, prs in groups_prs.items()
    }

    if args.json:
        payload = {
            "repo": args.repo,
            "base": base,
            "since": since.isoformat(),
            "until": until.isoformat(),
            "groups": groups,
            "prs": [build_pr_json(pr, facts[pr.number]) for pr in reportable],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(render_table(groups))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
