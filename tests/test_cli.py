import argparse
import io
import json
import os
import unittest
from datetime import date
from unittest import mock

from pr_outcomes import cli
from pr_outcomes.fetch import normalise_pr

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "prs.json")
with open(FIXTURES) as f:
    _RAW = json.load(f)


def _fixed_today(today: date):
    """A date subclass whose .today() is pinned, for tests that need
    cli.py's `date.today()` calls to be deterministic."""
    class _FixedDate(date):
        @classmethod
        def today(cls):
            return today
    return _FixedDate


def _run_main(argv, prs, teams_by_login=None):
    """Run cli.main() with GitHub/git I/O mocked out; returns (exit_code,
    stdout). teams_by_login, when given, backs fetch.fetch_org_teams."""
    fake_stdout = io.StringIO()
    fake_stdout.isatty = lambda: False
    with mock.patch.object(cli.fetch, "get_default_branch", return_value="main"), \
         mock.patch.object(cli.fetch, "fetch_prs", return_value=(prs, [])), \
         mock.patch.object(cli.fetch, "fetch_org_teams", return_value=(teams_by_login or {}, [])), \
         mock.patch.object(cli.sys, "stdout", fake_stdout):
        code = cli.main(argv)
    return code, fake_stdout.getvalue()


class ResolveFormatTests(unittest.TestCase):
    """--format defaults to json when stdout isn't a TTY, table when it is;
    --json always forces json regardless of --format or the TTY; --json
    together with --format table is a usage error."""

    def test_defaults_to_json_when_piped(self):
        self.assertEqual(cli.resolve_format(None, False, is_tty=False), "json")

    def test_defaults_to_table_when_tty(self):
        self.assertEqual(cli.resolve_format(None, False, is_tty=True), "table")

    def test_explicit_format_wins_over_tty(self):
        self.assertEqual(cli.resolve_format("table", False, is_tty=False), "table")

    def test_json_flag_wins_even_when_tty(self):
        self.assertEqual(cli.resolve_format(None, True, is_tty=True), "json")

    def test_json_and_format_json_is_not_a_conflict(self):
        self.assertEqual(cli.resolve_format("json", True, is_tty=True), "json")

    def test_json_and_format_table_raises(self):
        with self.assertRaises(ValueError):
            cli.resolve_format("table", True, is_tty=True)

    def test_json_and_format_table_exits_2_via_parse_args(self):
        with self.assertRaises(SystemExit) as ctx:
            cli.parse_args(["o/r", "--json", "--format", "table"])
        self.assertEqual(ctx.exception.code, 2)


class ResolveSinceUntilTests(unittest.TestCase):
    def test_until_defaults_to_today(self):
        since, until = cli.resolve_since_until(None, None, date(2026, 9, 26))
        self.assertEqual(until, date(2026, 9, 26))

    def test_since_defaults_to_90_days_before_until(self):
        since, until = cli.resolve_since_until(None, date(2026, 9, 18), date(2026, 9, 26))
        self.assertEqual(since, date(2026, 6, 20))
        self.assertEqual(until, date(2026, 9, 18))

    def test_since_defaults_off_today_when_until_also_defaulted(self):
        since, until = cli.resolve_since_until(None, None, date(2026, 9, 26))
        self.assertEqual(since, date(2026, 6, 28))

    def test_since_after_until_raises_with_corrected_example(self):
        with self.assertRaises(ValueError) as ctx:
            cli.resolve_since_until(date(2026, 9, 20), date(2026, 9, 1), date(2026, 9, 26))
        self.assertIn("2026-09-01", str(ctx.exception))

    def test_since_after_until_exits_2_via_parse_args(self):
        with self.assertRaises(SystemExit) as ctx:
            cli.parse_args(["o/r", "--since", "2026-09-20", "--until", "2026-09-01"])
        self.assertEqual(ctx.exception.code, 2)


class RepoValidatorTests(unittest.TestCase):
    def test_valid_owner_slash_name(self):
        self.assertEqual(cli.parse_repo("tryriot/parrot"), "tryriot/parrot")

    def test_missing_slash_raises_with_corrected_example(self):
        with self.assertRaises(argparse.ArgumentTypeError) as ctx:
            cli.parse_repo("parrot")
        self.assertIn("tryriot/parrot", str(ctx.exception))

    def test_empty_name_raises(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            cli.parse_repo("tryriot/")


class DateValidatorTests(unittest.TestCase):
    def test_valid_date(self):
        import datetime
        self.assertEqual(cli.parse_date("2026-08-01"), datetime.date(2026, 8, 1))

    def test_invalid_date_raises_with_corrected_example(self):
        with self.assertRaises(argparse.ArgumentTypeError) as ctx:
            cli.parse_date("08/01/2026")
        self.assertIn("2026-08-01", str(ctx.exception))


class TeamFilterTests(unittest.TestCase):
    ARGV = ["o/r", "--since", "2026-01-01", "--until", "2026-01-31", "--format", "json"]

    def test_unknown_team_slug_exits_2_and_lists_valid_ones(self):
        pr = normalise_pr(_RAW["rounds_two"])  # author: alice
        code, out = _run_main(
            self.ARGV + ["--team", "nope"], [pr], teams_by_login={"alice": {"sonar"}},
        )
        self.assertEqual(code, 2)

    def test_known_team_filters_to_its_members(self):
        alice_pr, bob_pr = normalise_pr(_RAW["rounds_two"]), normalise_pr(_RAW["revert_pr"])
        code, out = _run_main(
            self.ARGV + ["--team", "sonar", "--group-by", "author"],
            [alice_pr, bob_pr],
            teams_by_login={"alice": {"sonar"}, "bob": {"platform"}},
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["team"], "sonar")
        self.assertIn("alice", payload["groups"])
        self.assertNotIn("bob", payload["groups"])

    def test_teams_without_group_by_team_warns(self):
        pr = normalise_pr(_RAW["rounds_two"])
        code, out = _run_main(self.ARGV + ["--teams", "sonar"], [pr])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(any("--teams only applies" in w for w in payload["warnings"]))


class JsonPayloadContractTests(unittest.TestCase):
    ARGV = ["o/r", "--since", "2026-01-01", "--until", "2026-01-31", "--format", "json"]

    def test_every_group_key_has_a_definition(self):
        pr = normalise_pr(_RAW["rounds_two"])
        code, out = _run_main(self.ARGV, [pr])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        for group in payload["groups"].values():
            for key in group:
                self.assertIn(key, payload["definitions"], f"{key!r} has no definition")

    def test_warnings_present_when_followup_fix_is_null(self):
        pr = normalise_pr(_RAW["rounds_two"])
        _, out = _run_main(self.ARGV, [pr])  # no --repo-path
        payload = json.loads(out)
        self.assertTrue(any("--repo-path" in w for w in payload["warnings"]))
        for group in payload["groups"].values():
            self.assertIsNone(group["followup_fix_rate"])

    def test_no_prs_key_without_the_prs_flag(self):
        pr = normalise_pr(_RAW["rounds_two"])
        _, out = _run_main(self.ARGV, [pr])
        payload = json.loads(out)
        self.assertNotIn("prs", payload)

    def test_prs_key_present_with_the_prs_flag(self):
        pr = normalise_pr(_RAW["rounds_two"])
        _, out = _run_main(self.ARGV + ["--prs"], [pr])
        payload = json.loads(out)
        self.assertIn("prs", payload)


class NonexistentRepoTests(unittest.TestCase):
    def test_base_given_still_resolves_the_repo_and_fails_1(self):
        fake_stdout = io.StringIO()
        fake_stdout.isatty = lambda: False
        with mock.patch.object(
            cli.fetch, "get_default_branch", side_effect=cli.fetch.GhError("Could not resolve to a Repository"),
        ), mock.patch.object(cli.sys, "stdout", fake_stdout):
            code = cli.main(["o/r", "--base", "staging"])
        self.assertEqual(code, 1)


class PeriodGroupWarningTests(unittest.TestCase):
    def test_period_crossing_since_boundary_warns_partial(self):
        # rounds_two merges 2026-01-01 (Thu), inside ISO week 2026-W01 (Mon
        # 2025-12-29 - Sun 2026-01-04). --since lands on the merge day
        # itself, after the week started, so the week is partial.
        pr = normalise_pr(_RAW["rounds_two"])
        _, out = _run_main(
            ["o/r", "--since", "2026-01-01", "--until", "2026-06-01", "--format", "json", "--group-by", "week"],
            [pr],
        )
        payload = json.loads(out)
        self.assertTrue(any("is partial" in w for w in payload["warnings"]))

    def test_period_near_today_warns_truncated_fix_window(self):
        # rounds_two's week (2025-12-29 - 2026-01-04) is fully inside
        # [--since, --until]. With "today" mocked to 2026-01-10, the fetch
        # cutoff for reverts/fixes is capped at today, and this week's end
        # (2026-01-04) is within the last --fix-window-days (7) days before
        # that today (2026-01-03) -- a genuine truncation.
        pr = normalise_pr(_RAW["rounds_two"])  # merges 2026-01-01
        with mock.patch.object(cli, "date", _fixed_today(date(2026, 1, 10))):
            _, out = _run_main(
                ["o/r", "--since", "2025-12-01", "--until", "2026-01-04", "--fix-window-days", "7",
                 "--format", "json", "--group-by", "week"],
                [pr],
            )
        payload = json.loads(out)
        self.assertTrue(any("fewer than --fix-window-days" in w for w in payload["warnings"]))

    def test_period_near_until_but_far_from_today_does_not_warn(self):
        # Same since/until/week as above (period_end 2026-01-04 is within
        # --fix-window-days of --until 2026-01-04), but "today" is mocked far
        # in the future (2026-06-01): the fetch cutoff already covers this
        # week's fix window in full, so it must not warn -- the bug this
        # guards against warned on proximity to --until alone, regardless of
        # today.
        pr = normalise_pr(_RAW["rounds_two"])
        with mock.patch.object(cli, "date", _fixed_today(date(2026, 6, 1))):
            _, out = _run_main(
                ["o/r", "--since", "2025-12-01", "--until", "2026-01-04", "--fix-window-days", "7",
                 "--format", "json", "--group-by", "week"],
                [pr],
            )
        payload = json.loads(out)
        self.assertFalse(any("fewer than --fix-window-days" in w for w in payload["warnings"]))

    def test_groups_stay_chronological_in_json(self):
        jan_pr = normalise_pr(_RAW["rounds_two"])  # 2026-01
        apr_pr = normalise_pr(_RAW["merged_30h"])  # 2026-04
        _, out = _run_main(
            ["o/r", "--since", "2026-01-01", "--until", "2026-04-30", "--format", "json", "--group-by", "month"],
            [apr_pr, jan_pr],
        )
        payload = json.loads(out)
        self.assertEqual(list(payload["groups"].keys()), ["2026-01", "2026-04"])


class GhErrorHintTests(unittest.TestCase):
    def test_auth_problem(self):
        self.assertIn("gh auth status", cli.gh_error_hint("HTTP 401: Bad credentials"))

    def test_not_found(self):
        self.assertIn("owner/repo", cli.gh_error_hint('Could not resolve to a Repository with the name "x/y"'))

    def test_rate_limit(self):
        self.assertIn("retry later", cli.gh_error_hint("You have exceeded a secondary rate limit"))


if __name__ == "__main__":
    unittest.main()
