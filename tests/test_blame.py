import unittest

from pr_outcomes import blame


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


if __name__ == "__main__":
    unittest.main()
