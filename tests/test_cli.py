import io
import unittest
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


if __name__ == "__main__":
    unittest.main()
