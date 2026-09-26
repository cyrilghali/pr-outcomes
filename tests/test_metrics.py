import json
import os
import unittest

from pr_outcomes.fetch import normalise_pr
from pr_outcomes import metrics

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "prs.json")

with open(FIXTURES) as f:
    _RAW = json.load(f)


def pr(name):
    return normalise_pr(_RAW[name])


class ReviewRoundsTests(unittest.TestCase):
    def test_two_rounds(self):
        self.assertEqual(metrics.compute_review_rounds(pr("rounds_two")), 2)

    def test_zero_rounds_pushes_only_before_review(self):
        self.assertEqual(metrics.compute_review_rounds(pr("rounds_zero")), 0)


class ApprovalClassTests(unittest.TestCase):
    def test_substantive(self):
        classes = metrics.compute_approval_classes(pr("approval_substantive"))
        self.assertEqual(classes["carol"], "substantive")

    def test_commented(self):
        classes = metrics.compute_approval_classes(pr("approval_commented"))
        self.assertEqual(classes["dave"], "commented")

    def test_rubber_stamp(self):
        classes = metrics.compute_approval_classes(pr("approval_rubber_stamp"))
        self.assertEqual(classes["erin"], "rubber_stamp")

    def test_silent_small_pr_fast_approval(self):
        classes = metrics.compute_approval_classes(pr("approval_silent"))
        self.assertEqual(classes["frank"], "silent")


class RevertTests(unittest.TestCase):
    def test_matched_by_body_reference(self):
        original = pr("revert_original")
        revert = pr("revert_pr")
        reverted_by = metrics.compute_reverts([original, revert])
        self.assertEqual(reverted_by, {300: [301]})


class FollowupFixTests(unittest.TestCase):
    def test_shared_file_matches(self):
        original = pr("followup_match_original")
        fix = pr("followup_match_fix")
        fixes = metrics.find_followups(original, [original, fix], set(metrics.LOCKFILES), fix_window_days=7)
        self.assertEqual(fixes, [501])

    def test_hot_file_ignored(self):
        original = pr("followup_hotfile_original")
        fix = pr("followup_hotfile_fix")
        hot_files = {"lib/config.ex"}
        fixes = metrics.find_followups(original, [original, fix], hot_files, fix_window_days=7)
        self.assertEqual(fixes, [])

    def test_lockfile_ignored(self):
        original = pr("followup_lockfile_original")
        fix = pr("followup_lockfile_fix")
        fixes = metrics.find_followups(original, [original, fix], set(metrics.LOCKFILES), fix_window_days=7)
        self.assertEqual(fixes, [])


class ReviewedGroupTests(unittest.TestCase):
    def test_bot_only(self):
        self.assertEqual(metrics.compute_reviewed_group(pr("bot_only")), "bot-only")

    def test_no_review(self):
        self.assertEqual(metrics.compute_reviewed_group(pr("no_review")), "no-review")

    def test_human_approved(self):
        self.assertEqual(metrics.compute_reviewed_group(pr("human_approved")), "human-approved")


class TimeAndAggregateTests(unittest.TestCase):
    def test_time_to_merge(self):
        p = pr("rounds_two")
        hours = metrics._hours(p.created, p.merged)
        self.assertAlmostEqual(hours, 6.0)

    def test_median_p75(self):
        med, p75 = metrics._median_p75([2.0, 4.0, 10.0])
        self.assertEqual(med, 4.0)
        self.assertEqual(p75, 7.0)

    def test_median_p75_empty(self):
        med, p75 = metrics._median_p75([])
        self.assertIsNone(med)
        self.assertIsNone(p75)


class HotFilesTests(unittest.TestCase):
    def test_includes_lockfiles_and_hot_paths(self):
        prs = [pr("followup_match_original"), pr("followup_match_fix")]
        hot = metrics.compute_hot_files(prs)
        self.assertIn("mix.lock", hot)  # lockfiles always included
        self.assertIn("lib/parser.ex", hot)  # touched by both PRs in this tiny set


if __name__ == "__main__":
    unittest.main()
