import argparse
import io
import unittest
from datetime import date
from unittest import mock

from pr_outcomes import cli


class FormatDefaultTests(unittest.TestCase):
    """--format defaults to json when stdout isn't a TTY, table when it is;
    --json always forces json regardless of --format or the TTY."""

    def _fmt(self, argv, isatty: bool) -> str:
        args = cli.parse_args(argv)
        fake_stdout = io.StringIO()
        fake_stdout.isatty = lambda: isatty
        with mock.patch.object(cli.sys, "stdout", fake_stdout):
            return "json" if args.json else (args.format or ("json" if not cli.sys.stdout.isatty() else "table"))

    def test_defaults_to_json_when_piped(self):
        self.assertEqual(self._fmt(["o/r"], isatty=False), "json")

    def test_defaults_to_table_when_tty(self):
        self.assertEqual(self._fmt(["o/r"], isatty=True), "table")

    def test_explicit_format_wins_over_tty(self):
        self.assertEqual(self._fmt(["o/r", "--format", "table"], isatty=False), "table")

    def test_json_flag_wins_even_when_tty(self):
        self.assertEqual(self._fmt(["o/r", "--json"], isatty=True), "json")


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


class GhErrorHintTests(unittest.TestCase):
    def test_auth_problem(self):
        self.assertIn("gh auth status", cli.gh_error_hint("HTTP 401: Bad credentials"))

    def test_not_found(self):
        self.assertIn("owner/repo", cli.gh_error_hint('Could not resolve to a Repository with the name "x/y"'))

    def test_rate_limit(self):
        self.assertIn("retry later", cli.gh_error_hint("You have exceeded a secondary rate limit"))


if __name__ == "__main__":
    unittest.main()
