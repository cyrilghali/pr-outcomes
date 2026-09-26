import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from pr_outcomes import blame
from pr_outcomes.fetch import PR, Actor


class SubjectParserTests(unittest.TestCase):
    def test_merge_commit_subject(self):
        self.assertEqual(blame.parse_pr_number_from_subject("Merge pull request #123 from tryriot/fix-x"), 123)

    def test_squash_subject(self):
        self.assertEqual(blame.parse_pr_number_from_subject("Fix parser crash (#456)"), 456)

    def test_squash_subject_takes_last_match(self):
        # a title that itself quotes another PR number must not win
        self.assertEqual(blame.parse_pr_number_from_subject("Fix (#1) crash from (#789)"), 789)

    def test_no_pr_number(self):
        self.assertIsNone(blame.parse_pr_number_from_subject("chore: bump deps"))


class DiffHunkParserTests(unittest.TestCase):
    def test_modified_and_deleted_ranges(self):
        diff_text = (
            "diff --git a/lib/parser.ex b/lib/parser.ex\n"
            "index 111..222 100644\n"
            "--- a/lib/parser.ex\n"
            "+++ b/lib/parser.ex\n"
            "@@ -10,3 +10,2 @@\n"
            "-old line 1\n"
            "-old line 2\n"
            "-old line 3\n"
            "@@ -40 +38 @@\n"
            "-old line 40\n"
        )
        hunks = blame.parse_diff_hunks(diff_text)
        self.assertEqual(hunks, {"lib/parser.ex": [(10, 3), (40, 1)]})

    def test_pure_addition_hunk_skipped(self):
        diff_text = (
            "diff --git a/lib/new.ex b/lib/new.ex\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/lib/new.ex\n"
            "@@ -0,0 +1,5 @@\n"
            "+added line\n"
        )
        self.assertEqual(blame.parse_diff_hunks(diff_text), {})

    def test_multiple_files(self):
        diff_text = (
            "diff --git a/a.ex b/a.ex\n"
            "@@ -1,2 +1,2 @@\n"
            "-a\n"
            "diff --git a/b.ex b/b.ex\n"
            "@@ -5,1 +5,1 @@\n"
            "-b\n"
        )
        hunks = blame.parse_diff_hunks(diff_text)
        self.assertEqual(hunks, {"a.ex": [(1, 2)], "b.ex": [(5, 1)]})


class BlameCacheTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_root = blame.CACHE_ROOT
            blame.CACHE_ROOT = tmp
            try:
                self.assertIsNone(blame._load_cached_blame("o", "r", "deadbeef"))
                blame._save_cached_blame("o", "r", "deadbeef", {12, 34})
                self.assertEqual(blame._load_cached_blame("o", "r", "deadbeef"), {12, 34})
            finally:
                blame.CACHE_ROOT = old_root


def _dummy_pr(number: int, title: str, merged: datetime) -> PR:
    return PR(
        number=number, title=title, body="", url=f"https://github.com/o/r/pull/{number}",
        author=Actor(login="alice", is_bot=False), base="staging",
        created=merged, merged=merged, ready=merged,
        additions=1, deletions=1, changed_files=1,
    )


class ComputeFollowupFixesEndToEndTests(unittest.TestCase):
    """Builds a throwaway git repo in a tempdir with a real, first-parent
    commit history and runs compute_followup_fixes against it end to end,
    instead of mocking git."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name

        self.old_cache_root = blame.CACHE_ROOT
        blame.CACHE_ROOT = os.path.join(self.repo, ".cache")
        self.addCleanup(lambda: setattr(blame, "CACHE_ROOT", self.old_cache_root))

        subprocess.run(["git", "init", "-q", "-b", "staging", self.repo], check=True)

    def _commit(self, subject: str, day: int, changes: dict[str, str]) -> str:
        for path, content in changes.items():
            full = os.path.join(self.repo, path)
            os.makedirs(os.path.dirname(full) or self.repo, exist_ok=True)
            with open(full, "w") as f:
                f.write(content)
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        date = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day)).isoformat()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
        }
        subprocess.run(
            ["git", "-C", self.repo, "commit", "-q", "-m", subject],
            check=True, env=env,
        )
        return subprocess.run(
            ["git", "-C", self.repo, "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    def test_blame_attribution_end_to_end(self):
        self._commit("init", 0, {
            "file.txt": "a1\na2\na3\na4\n",
            "mix.lock": "lockline\n",
        })
        # PR #1 introduces two changed lines (a2, a4) in file.txt, plus a
        # lockfile change.
        self._commit("feat (#1)", 1, {
            "file.txt": "a1\na2-v1\na3\na4-v1\n",
            "mix.lock": "lockline-v1\n",
        })
        # PR #2, a real fix merged the next day, changes the line PR #1
        # touched (a2): blame must attribute it to PR #1.
        self._commit("fix: x (#2)", 2, {
            "file.txt": "a1\na2-v2\na3\na4-v1\n",
            "mix.lock": "lockline-v1\n",
        })
        # PR #3 changes the *other* line PR #1 touched (a4), but merges 30
        # days later -- outside a 7-day fix window, so it must not be
        # attributed even though blame would otherwise agree on PR #1.
        self._commit("fix: y (#3)", 30, {
            "file.txt": "a1\na2-v2\na3\na4-v2\n",
            "mix.lock": "lockline-v1\n",
        })
        # PR #4 only touches the lockfile line PR #1 introduced: lockfile
        # hunks are excluded from blame, so this must not be attributed to
        # anything.
        self._commit("fix: z (#4)", 3, {
            "file.txt": "a1\na2-v2\na3\na4-v2\n",
            "mix.lock": "lockline-v2\n",
        })

        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/staging", "refs/heads/staging"],
            cwd=self.repo, check=True,
        )

        base_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        prs = [
            _dummy_pr(1, "feat", base_date + timedelta(days=1)),
            _dummy_pr(2, "fix: x", base_date + timedelta(days=2)),
            _dummy_pr(3, "fix: y", base_date + timedelta(days=30)),
            _dummy_pr(4, "fix: z", base_date + timedelta(days=3)),
        ]

        result = blame.compute_followup_fixes(self.repo, "o", "r", "staging", prs, fix_window_days=7)

        self.assertEqual(result.get(2), {1})
        self.assertNotIn(3, result)
        self.assertNotIn(4, result)
        # A PR is never blamed for itself.
        for fix_number, introducing in result.items():
            self.assertNotIn(fix_number, introducing)


if __name__ == "__main__":
    unittest.main()
