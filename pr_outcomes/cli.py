"""argparse entry point plus table/JSON rendering."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone

from pr_outcomes import blame, fetch, metrics

DEFINITIONS = {
    "count": "Number of PRs in this group.",
    "revert_rate": "Share of PRs later reverted by another PR (0-1).",
    "revert_count": "Number of PRs later reverted by another PR.",
    "followup_fix_rate": "Share of PRs later blamed for a follow-up fix, within the fix window (0-1); null without --repo-path.",
    "followup_fix_count": "Number of PRs later blamed for a follow-up fix; null without --repo-path.",
    "approval_share_changed_by_review": "Share of approvals where the reviewer commented, the author pushed a change, then that reviewer approved (0-1).",
    "approval_share_commented": "Share of approvals where the reviewer commented but no push followed before approving (0-1).",
    "approval_share_silent": "Share of approvals with no comment, not classified as rubber-stamp (0-1).",
    "approval_share_rubber_stamp": "Share of approvals on a 200+ line diff that landed under 5 minutes after ready/push/request (0-1).",
    "approval_count": "Number of human non-author approvals classified.",
    "review_rounds_mean": "Mean number of comment-then-push-then-review rounds per PR.",
    "review_rounds_ge1_share": "Share of PRs with at least one review round (0-1).",
    "human_comments_mean": "Mean number of human comments per PR.",
    "bot_comments_mean": "Mean number of bot comments per PR.",
    "time_to_first_human_review_h_median": "Median hours from PR creation to first human review or comment.",
    "time_to_first_human_review_h_p75": "75th percentile hours from PR creation to first human review or comment.",
    "time_to_first_bot_review_h_median": "Median hours from PR creation to first bot review or comment.",
    "time_to_first_bot_review_h_p75": "75th percentile hours from PR creation to first bot review or comment.",
    "time_to_first_approval_h_median": "Median hours from PR creation to first human approval.",
    "time_to_first_approval_h_p75": "75th percentile hours from PR creation to first human approval.",
    "time_to_merge_h_median": "Median hours from PR creation to merge.",
    "time_to_merge_h_p75": "75th percentile hours from PR creation to merge.",
    "ready_to_merge_h_median": "Median hours from ready-for-review to merge.",
    "ready_to_merge_h_p75": "75th percentile hours from ready-for-review to merge.",
    "merged_within_1h": "Share of PRs merged within 1 hour of creation (0-1).",
    "merged_within_24h": "Share of PRs merged within 24 hours of creation (0-1).",
    "throughput_per_week": "PR count divided by the number of weeks in [--since, --until].",
    "throughput_by_week": "PR count per ISO week (year-Www), merges in [--since, --until].",
    "size_median": "Median PR size, additions + deletions, in lines.",
    "size_p75": "75th percentile PR size, additions + deletions, in lines.",
}

OUTCOMES_ROWS = [
    ("revert_rate", "Revert rate"),
    ("followup_fix_rate", "Follow-up fix rate"),
]
REVIEW_DEPTH_ROWS = [
    ("approval_share_changed_by_review", "Approvals: changed by review"),
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
        return f"{value:g}"
    return str(value)


def _fmt_share(value) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


SHARE_KEYS = {
    "revert_rate", "followup_fix_rate", "approval_share_changed_by_review",
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


def parse_repo(s: str) -> str:
    if not re.match(r"^[\w.-]+/[\w.-]+$", s):
        raise argparse.ArgumentTypeError(f"repo must be OWNER/NAME, e.g. tryriot/parrot (got {s!r})")
    return s


def parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"date must be YYYY-MM-DD, e.g. 2026-08-01 (got {s!r})")


def resolve_format(fmt: str | None, json_flag: bool, is_tty: bool) -> str:
    """table when stdout is a TTY, json otherwise; --json always forces json;
    an explicit --format wins over the TTY default. Raises ValueError (caller
    turns this into a usage error) for the contradictory --json --format
    table."""
    if json_flag and fmt == "table":
        raise ValueError("--json conflicts with --format table -- pass one or the other")
    if json_flag:
        return "json"
    return fmt or ("table" if is_tty else "json")


def resolve_since_until(since: date | None, until: date | None, today: date) -> tuple[date, date]:
    """--until defaults to today; --since defaults to 90 days before --until.
    Raises ValueError (caller turns this into a usage error) when an explicit
    --since lands after --until."""
    until = until or today
    since = since or (until - timedelta(days=90))
    if since > until:
        raise ValueError(
            f"--since ({since}) must not be after --until ({until}), "
            f"e.g. --since {until - timedelta(days=90)} --until {until}"
        )
    return since, until


EPILOG = """\
Examples:
  pr-outcomes tryriot/parrot --since 2026-08-01 --until 2026-09-18
  pr-outcomes tryriot/parrot --group-by depth --repo-path ~/dev/riot/parrot
  pr-outcomes tryriot/parrot --group-by team --teams awareness,inbox,platform,simulation,sonar
  pr-outcomes tryriot/parrot --team sonar --group-by author
  pr-outcomes tryriot/parrot --group-by size
  pr-outcomes tryriot/parrot --prs | jq '.prs[] | select(.number == 1234)'
  pr-outcomes tryriot/parrot --since 2026-08-01 --until 2026-09-18 --format table

Exit codes: 0 ok, 1 GitHub/git error, 2 usage error.

A cold first run on a busy repo takes ~20+ minutes (one GraphQL request per
PR timeline, four in flight at a time). A cached rerun of the same range
takes ~2 minutes. Run a cold first run in the background.
"""


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="pr-outcomes",
        description="PR review/outcome diagnostics from GitHub's GraphQL API",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("repo", type=parse_repo, help="owner/repo, e.g. tryriot/parrot")
    p.add_argument("--since", type=parse_date, help="YYYY-MM-DD. Default: 90 days before --until (or today).")
    p.add_argument("--until", type=parse_date, help="YYYY-MM-DD. Default: today.")
    p.add_argument("--base", default=None, help="Base branch to filter merged PRs on. Default: the repo's default branch.")
    p.add_argument(
        "--group-by", choices=["reviewed", "label", "author", "approver", "depth", "team", "size"], default=None,
        help="How to split PRs into groups. 'team' splits by the author's GitHub org team(s) "
             "(see --teams). 'size' buckets additions+deletions into xs (<100), s (100-299), "
             "m (300-699), l (700+). Default: one 'all' group.",
    )
    p.add_argument(
        "--teams", default=None,
        help="Comma-separated org team slugs to restrict --group-by team to, "
             "e.g. awareness,inbox,platform,simulation,sonar. Default: every team the author belongs to. "
             "Only applies with --group-by team.",
    )
    p.add_argument(
        "--team", default=None,
        help="Keep only PRs authored by a member of this GitHub org team slug, e.g. sonar. "
             "Combine with any --group-by. Default: none.",
    )
    p.add_argument(
        "--fix-window-days", type=int, default=7,
        help="Days past --until to look for reverts and follow-up fixes. Default: 7.",
    )
    p.add_argument(
        "--repo-path", default=None,
        help="Local clone for blame-based follow-up-fix attribution (local git only, never fetches). "
             "Without it, follow-up-fix metrics are null. Default: none.",
    )
    p.add_argument(
        "--format", choices=["table", "json"], default=None,
        help="Output format. Default: json when stdout is not a TTY, table when it is.",
    )
    p.add_argument("--json", action="store_true", help="Alias for --format json. Default: off.")
    p.add_argument(
        "--prs", action="store_true",
        help="Include per-PR facts (json: under the 'prs' key; table: a JSON block printed after the table). "
             "Default: off.",
    )
    p.add_argument("--refresh", action="store_true", help="Bypass the on-disk PR, blame, and team caches. Default: off.")
    p.add_argument(
        "--verbose", action="store_true",
        help="Print progress lines (cache hit / fetching) even when stderr isn't a TTY. Default: off.",
    )
    args = p.parse_args(argv)
    try:
        args.since, args.until = resolve_since_until(args.since, args.until, date.today())
        resolve_format(args.format, args.json, is_tty=False)  # validate only; actual value depends on runtime stdout
    except ValueError as e:
        p.error(str(e))
    return args


def _warn(msg: str, warnings: list[str]) -> None:
    print(f"pr-outcomes: {msg}", file=sys.stderr)
    warnings.append(msg)


def gh_error_hint(stderr: str) -> str:
    lowered = stderr.lower()
    if "rate limit" in lowered or "abuse" in lowered:
        return "retry later, or rerun the same command -- finished weeks stay cached"
    if "could not resolve" in lowered or "not found" in lowered:
        return "check owner/repo spelling and that you have access"
    if "auth" in lowered or "401" in lowered or "403" in lowered:
        return "run `gh auth status` to check your GitHub authentication"
    return "run `gh auth status`, or retry -- finished weeks stay cached"


def main(argv=None) -> int:
    args = parse_args(argv)
    today = date.today()
    since, until = args.since, args.until
    fmt = resolve_format(args.format, args.json, sys.stdout.isatty())
    owner, name = args.repo.split("/", 1)  # parse_repo already validated the OWNER/NAME shape

    warnings: list[str] = []
    complete_until = until - timedelta(days=args.fix_window_days)
    if until > today - timedelta(days=args.fix_window_days):
        _warn(
            f"window truncated: PRs merged after {complete_until} have fewer than "
            f"--fix-window-days ({args.fix_window_days}) to be reverted or fixed as of today ({today})",
            warnings,
        )

    try:
        # Always resolve the repo, even with --base given, so a nonexistent
        # repo/owner fails here with the not-found hint instead of silently
        # returning zero PRs from the (never-run) search below.
        default_branch = fetch.get_default_branch(owner, name)
        base = args.base or default_branch
        if args.verbose or sys.stderr.isatty():
            print(f"pr-outcomes: base={base} since={since} until={until}", file=sys.stderr)
        all_prs, fetch_warnings = fetch.fetch_prs(
            owner, name, base, since, until, args.fix_window_days, refresh=args.refresh, verbose=args.verbose,
        )
        warnings.extend(fetch_warnings)
    except fetch.GhError as e:
        print(f"pr-outcomes: gh error: {e}", file=sys.stderr)
        print(f"pr-outcomes: hint: {gh_error_hint(str(e))}", file=sys.stderr)
        return 1

    followup_map = None
    if args.repo_path:
        try:
            followup_map = blame.compute_followup_fixes(
                args.repo_path, owner, name, base, all_prs, args.fix_window_days, refresh=args.refresh,
            )
        except blame.BlameError as e:
            print(f"pr-outcomes: blame error: {e}", file=sys.stderr)
            print(
                "pr-outcomes: hint: --repo-path needs a local clone with 'origin/<base>' fetched",
                file=sys.stderr,
            )
            return 1
    else:
        _warn("follow-up fixes need --repo-path", warnings)

    facts = metrics.compute_all_facts(all_prs, args.fix_window_days, followup_map)
    reportable = metrics.in_report_window(all_prs, since, until)

    teams_by_login, selected_teams = None, None
    if args.group_by == "team" or args.team:
        if args.group_by == "team":
            _warn(
                "--group-by team uses today's GitHub org membership, not membership at PR merge time",
                warnings,
            )
        try:
            teams_by_login, team_warnings = fetch.fetch_org_teams(owner, name, refresh=args.refresh)
        except fetch.GhError as e:
            print(f"pr-outcomes: gh error: {e}", file=sys.stderr)
            print(f"pr-outcomes: hint: {gh_error_hint(str(e))}", file=sys.stderr)
            return 1
        for w in team_warnings:
            print(f"pr-outcomes: {w}", file=sys.stderr)
        warnings.extend(team_warnings)
        if args.group_by == "team" and args.teams:
            selected_teams = {t.strip() for t in args.teams.split(",") if t.strip()}

    if args.teams and args.group_by != "team":
        _warn("--teams only applies with --group-by team; ignored", warnings)

    if args.team:
        valid_slugs = {slug for slugs in teams_by_login.values() for slug in slugs}
        if args.team not in valid_slugs:
            print(
                f"pr-outcomes: unknown team '{args.team}'; "
                f"valid teams: {', '.join(sorted(valid_slugs)) or '(none found)'}",
                file=sys.stderr,
            )
            return 2
        reportable = metrics.filter_by_team(reportable, teams_by_login, args.team)

    groups_prs = metrics.group_prs(reportable, facts, args.group_by, teams_by_login, selected_teams)
    groups = {
        name_: metrics.aggregate_group(prs, facts, since, until)
        for name_, prs in groups_prs.items()
    }

    if fmt == "json":
        used_keys = {k for g in groups.values() for k in g}
        payload = {
            "repo": args.repo,
            "base": base,
            **({"team": args.team} if args.team else {}),
            "since": since.isoformat(),
            "until": until.isoformat(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "fix_window_days": args.fix_window_days,
            "complete_until": complete_until.isoformat(),
            "warnings": warnings,
            "definitions": {k: v for k, v in DEFINITIONS.items() if k in used_keys},
            "groups": groups,
        }
        if args.prs:
            payload["prs"] = [build_pr_json(pr, facts[pr.number]) for pr in reportable]
        print(json.dumps(payload, indent=2))
    else:
        print(render_table(groups))
        if args.prs:
            print(json.dumps([build_pr_json(pr, facts[pr.number]) for pr in reportable], indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
