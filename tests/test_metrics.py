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

    def test_commented_when_push_only_after_approval(self):
        # comment+approve back to back, then a rebase before merge: the push
        # never sat between the comment and the approval, so this is not
        # substantive even though a push happened somewhere in the PR.
        classes = metrics.compute_approval_classes(pr("approval_commented_then_rebase"))
        self.assertEqual(classes["heidi"], "commented")


class RevertTests(unittest.TestCase):
    def test_matched_by_body_reference(self):
        original = pr("revert_original")
        revert = pr("revert_pr")
        reverted_by = metrics.compute_reverts([original, revert])
        self.assertEqual(reverted_by, {300: [301]})


class FollowupFixTests(unittest.TestCase):
    """followup_fix comes from a caller-supplied blame mapping (see
    blame.compute_followup_fixes); metrics.py just consumes it."""

    def test_mapping_marks_introducing_pr(self):
        original = pr("followup_match_original")
        fix = pr("followup_match_fix")
        facts = metrics.compute_all_facts([original, fix], fix_window_days=7, followup_by_fix={501: {500}})
        self.assertTrue(facts[500].followup_fix)
        self.assertEqual(facts[500].followup_fix_by, [501])
        self.assertFalse(facts[501].followup_fix)

    def test_no_repo_path_leaves_followup_fix_null(self):
        original = pr("followup_match_original")
        fix = pr("followup_match_fix")
        facts = metrics.compute_all_facts([original, fix], fix_window_days=7, followup_by_fix=None)
        self.assertIsNone(facts[500].followup_fix)
        self.assertEqual(facts[500].followup_fix_by, [])


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


class DepthGroupTests(unittest.TestCase):
    def test_groups_by_review_depth(self):
        prs = [pr("approval_substantive"), pr("approval_rubber_stamp"), pr("no_review")]
        facts = {p.number: metrics.PRFacts(
            number=p.number,
            time_to_first_human_review_h=None, time_to_first_bot_review_h=None,
            time_to_first_approval_h=None, time_to_merge_h=None, ready_to_merge_h=None,
            review_rounds=0, changes_requested=0, human_comments=0, bot_comments=0,
            size=0, changed_files=0, reviewed_group="", approval_classes=metrics.compute_approval_classes(p),
        ) for p in prs}
        groups = metrics.group_prs(prs, facts, "depth")
        self.assertEqual({n.number for n in groups["substantive-review"]}, {pr("approval_substantive").number})
        self.assertEqual({n.number for n in groups["light-review"]}, {pr("approval_rubber_stamp").number})
        self.assertEqual({n.number for n in groups["no-human-approval"]}, {pr("no_review").number})


if __name__ == "__main__":
    unittest.main()
