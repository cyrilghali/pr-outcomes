import json
import os
import unittest
from datetime import date

from pr_outcomes.fetch import PR, normalise_pr
from pr_outcomes import metrics

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "prs.json")

with open(FIXTURES) as f:
    _RAW = json.load(f)


def pr(name: str) -> PR:
    return normalise_pr(_RAW[name])


class ReviewRoundsTests(unittest.TestCase):
    def test_two_rounds(self):
        self.assertEqual(metrics.compute_review_rounds(pr("rounds_two")), 2)

    def test_zero_rounds_pushes_only_before_review(self):
        self.assertEqual(metrics.compute_review_rounds(pr("rounds_zero")), 0)

    def test_three_events_only_the_pair_with_a_push_between_counts(self):
        # Three human events (comments at 01:00, 03:00, 05:00). A push at
        # 00:30 sits before the first event and never counts; a push at
        # 02:00 sits strictly between the first and second event and counts
        # once; no push sits between the second and third. -> 1 round, not 0
        # (proving the "push before first review" case doesn't leak into a
        # later pair) and not 2 (proving only pairs with a push between them
        # count).
        self.assertEqual(metrics.compute_review_rounds(pr("rounds_three_mixed")), 1)


class ApprovalClassTests(unittest.TestCase):
    def test_changed_by_review(self):
        classes = metrics.compute_approval_classes(pr("approval_changed_by_review"))
        self.assertEqual(classes["carol"], "changed_by_review")

    def test_commented(self):
        classes = metrics.compute_approval_classes(pr("approval_commented"))
        self.assertEqual(classes["dave"], "commented")

    def test_rubber_stamp(self):
        classes = metrics.compute_approval_classes(pr("approval_rubber_stamp"))
        self.assertEqual(classes["erin"], "rubber_stamp")

    def test_silent_small_pr_fast_approval(self):
        classes = metrics.compute_approval_classes(pr("approval_silent"))
        self.assertEqual(classes["frank"], "silent")

    def test_rubber_stamp_at_4_minutes_after_last_push(self):
        classes = metrics.compute_approval_classes(pr("rubber_stamp_4min"))
        self.assertEqual(classes["ivan"], "rubber_stamp")

    def test_silent_at_6_minutes_after_last_push(self):
        # Same setup as the 4-minute case, just past the 5-minute bar.
        classes = metrics.compute_approval_classes(pr("rubber_stamp_6min"))
        self.assertEqual(classes["ivan"], "silent")

    def test_late_review_request_moves_the_reference_point(self):
        # The push happened over an hour before the approval, but a review
        # request to this reviewer landed only 4 minutes before it. The
        # reference is the latest of ready/last-push/last-request, so the
        # request -- not the earlier push -- decides this is rubber_stamp.
        classes = metrics.compute_approval_classes(pr("rubber_stamp_late_request"))
        self.assertEqual(classes["ivan"], "rubber_stamp")

    def test_commented_when_push_only_after_approval(self):
        # comment+approve back to back, then a rebase before merge: the push
        # never sat between the comment and the approval, so this is not
        # changed_by_review even though a push happened somewhere in the PR.
        classes = metrics.compute_approval_classes(pr("approval_commented_then_rebase"))
        self.assertEqual(classes["heidi"], "commented")

    def test_approval_body_alone_is_not_commented(self):
        # "LGTM" as the approval's own body is not engagement: with no other
        # comment and a small diff, this is silent, not commented.
        classes = metrics.compute_approval_classes(pr("approval_body_only"))
        self.assertEqual(classes["gina"], "silent")

    def test_inline_comments_on_the_approval_review_count(self):
        # Inline comments attached to the approval review itself are real
        # feedback, unlike the approval's own body text.
        classes = metrics.compute_approval_classes(pr("approval_inline_comments_on_approval"))
        self.assertEqual(classes["heidi"], "commented")


class BotReviewNeverCountsAsHumanTests(unittest.TestCase):
    def test_bot_approval_excluded_from_approval_classes(self):
        self.assertEqual(metrics.compute_approval_classes(pr("bot_approval")), {})

    def test_bot_approval_does_not_make_reviewed_group_human_approved(self):
        self.assertEqual(metrics.compute_reviewed_group(pr("bot_approval")), "bot-only")

    def test_bot_approval_does_not_count_as_first_human_review_or_approval(self):
        facts = metrics.compute_all_facts([pr("bot_approval")], fix_window_days=7)[801]
        self.assertIsNone(facts.time_to_first_human_review_h)
        self.assertIsNone(facts.time_to_first_approval_h)


class RevertTests(unittest.TestCase):
    def test_matched_by_body_reference(self):
        original = pr("revert_original")
        revert = pr("revert_pr")
        reverted_by = metrics.compute_reverts([original, revert])
        self.assertEqual(reverted_by, {300: [301]})

    def test_matched_by_quoted_title_with_no_body_reference(self):
        original = pr("revert_quoted_original")
        revert = pr("revert_quoted_pr")
        self.assertEqual(revert.body, "")
        reverted_by = metrics.compute_reverts([original, revert])
        self.assertEqual(reverted_by, {310: [311]})

    def test_revert_rate_excludes_revert_prs_from_denominator(self):
        original = pr("revert_quoted_original")
        revert = pr("revert_quoted_pr")
        facts = metrics.compute_all_facts([original, revert], fix_window_days=7)
        out = metrics.aggregate_group([original, revert], facts, date(2026, 1, 1), date(2026, 1, 31))
        # 1 reverted PR out of 1 non-revert PR: the revert PR itself is
        # excluded from the denominator, or this would read 0.5.
        self.assertEqual(out["revert_rate"], 1.0)
        self.assertEqual(out["revert_count"], 1)


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
        assert hours is not None
        self.assertAlmostEqual(hours, 6.0)

    def test_median_p75(self):
        med, p75 = metrics._median_p75([2.0, 4.0, 10.0])
        self.assertEqual(med, 4.0)
        self.assertEqual(p75, 7.0)

    def test_median_p75_empty(self):
        med, p75 = metrics._median_p75([])
        self.assertIsNone(med)
        self.assertIsNone(p75)


class ThroughputTests(unittest.TestCase):
    def test_inclusive_last_day_counts_as_a_full_week(self):
        # since..until spans exactly 7 calendar days (Jan 1 through Jan 7
        # inclusive): one week, not (7-1)/7 of one.
        since, until = date(2026, 1, 1), date(2026, 1, 7)
        prs = [pr("rounds_two")] * 7
        facts = {pr("rounds_two").number: metrics.compute_all_facts([pr("rounds_two")], 7)[pr("rounds_two").number]}
        out = metrics.aggregate_group(prs, facts, since, until)
        self.assertEqual(out["throughput_per_week"], 7.0)


class ReportWindowTests(unittest.TestCase):
    def test_revert_merged_after_until_still_marks_earlier_pr_reverted(self):
        # The revert PR merges 2026-05-05, after --until=2026-05-02, but
        # inside the fix window. It must not appear in the reported groups
        # itself, while still marking PR 330 (merged inside the window) as
        # reverted -- exactly what cli.main does by computing facts over
        # every fetched PR and only filtering the *group* membership.
        original = pr("window_original")
        revert = pr("window_revert_after_until")
        since, until = date(2026, 5, 1), date(2026, 5, 2)
        facts = metrics.compute_all_facts([original, revert], fix_window_days=7)
        self.assertTrue(facts[330].reverted)
        self.assertEqual(facts[330].reverted_by, [331])

        reportable = metrics.in_report_window([original, revert], since, until)
        self.assertEqual([p.number for p in reportable], [330])

        groups = metrics.group_prs(reportable, facts, None)
        out = metrics.aggregate_group(groups["all"], facts, since, until)
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["revert_rate"], 1.0)


class MergedWithinSharesTests(unittest.TestCase):
    def test_1h_and_24h_shares_on_mixed_merge_times(self):
        prs = [pr("merged_0_5h"), pr("merged_5h"), pr("merged_30h")]
        facts = metrics.compute_all_facts(prs, fix_window_days=7)
        out = metrics.aggregate_group(prs, facts, date(2026, 4, 1), date(2026, 4, 30))
        # 0.5h counts in both; 5h only in the 24h share; 30h in neither.
        self.assertAlmostEqual(out["merged_within_1h"], 1 / 3, places=3)
        self.assertAlmostEqual(out["merged_within_24h"], 2 / 3, places=3)


class TimeToMergeFromCreatedTests(unittest.TestCase):
    def test_time_to_merge_uses_created_not_ready(self):
        # Opened as a draft at 00:00, marked ready at 02:00, merged at
        # 03:00: time_to_merge is 3h from creation, ready_to_merge is 1h
        # from the ready event -- distinct numbers proving each uses its
        # own reference point.
        facts = metrics.compute_all_facts([pr("draft_then_ready")], fix_window_days=7)[240]
        assert facts.time_to_merge_h is not None
        assert facts.ready_to_merge_h is not None
        self.assertAlmostEqual(facts.time_to_merge_h, 3.0)
        self.assertAlmostEqual(facts.ready_to_merge_h, 1.0)


class TeamGroupTests(unittest.TestCase):
    def _facts_stub(self, prs: list[PR]):
        return {p.number: metrics.compute_all_facts([p], fix_window_days=7)[p.number] for p in prs}

    def test_groups_by_author_org_team_membership(self):
        alice_pr, bob_pr = pr("rounds_two"), pr("revert_pr")
        facts = self._facts_stub([alice_pr, bob_pr])
        teams_by_login = {"alice": {"platform", "sonar"}}  # bob: no team membership

        groups = metrics.group_prs([alice_pr, bob_pr], facts, "team", teams_by_login, selected_teams=None)

        self.assertEqual({p.number for p in groups["platform"]}, {alice_pr.number})
        self.assertEqual({p.number for p in groups["sonar"]}, {alice_pr.number})
        self.assertEqual({p.number for p in groups["(none)"]}, {bob_pr.number})

    def test_selected_teams_restricts_and_filters_out_unselected(self):
        alice_pr, bob_pr = pr("rounds_two"), pr("revert_pr")
        facts = self._facts_stub([alice_pr, bob_pr])
        teams_by_login = {"alice": {"platform", "backend"}}

        groups = metrics.group_prs(
            [alice_pr, bob_pr], facts, "team", teams_by_login, selected_teams={"platform"},
        )

        self.assertEqual({p.number for p in groups["platform"]}, {alice_pr.number})
        self.assertNotIn("backend", groups)
        self.assertEqual({p.number for p in groups["(none)"]}, {bob_pr.number})

    def test_no_membership_map_lands_everyone_in_none(self):
        alice_pr = pr("rounds_two")
        facts = self._facts_stub([alice_pr])

        groups = metrics.group_prs([alice_pr], facts, "team", teams_by_login=None, selected_teams=None)

        self.assertEqual({p.number for p in groups["(none)"]}, {alice_pr.number})


class FilterByTeamTests(unittest.TestCase):
    def test_keeps_only_members_of_the_given_team(self):
        alice_pr, bob_pr = pr("rounds_two"), pr("revert_pr")
        teams_by_login = {"alice": {"sonar", "platform"}, "bob": {"platform"}}

        filtered = metrics.filter_by_team([alice_pr, bob_pr], teams_by_login, "sonar")

        self.assertEqual([p.number for p in filtered], [alice_pr.number])

    def test_author_with_no_membership_is_dropped(self):
        alice_pr = pr("rounds_two")
        filtered = metrics.filter_by_team([alice_pr], {}, "sonar")
        self.assertEqual(filtered, [])


class PeriodGroupTests(unittest.TestCase):
    def test_week_key_and_bounds(self):
        label = metrics.period_key(date(2026, 8, 5), "week")
        self.assertEqual(label, "2026-W32")
        start, end = metrics.period_bounds(label, "week")
        self.assertEqual((start, end), (date(2026, 8, 3), date(2026, 8, 9)))

    def test_month_key_and_bounds(self):
        label = metrics.period_key(date(2026, 8, 5), "month")
        self.assertEqual(label, "2026-08")
        start, end = metrics.period_bounds(label, "month")
        self.assertEqual((start, end), (date(2026, 8, 1), date(2026, 8, 31)))

    def test_group_by_month_orders_chronologically_regardless_of_pr_order(self):
        # rounds_two merges 2026-01-01; build a second PR-like fact by hand
        # via a later-merged fixture so two different months are exercised
        # out of chronological order in the input list.
        later = pr("merged_30h")  # merges well after rounds_two, per fixtures
        earlier = pr("rounds_two")
        facts = {
            later.number: metrics.compute_all_facts([later], fix_window_days=7)[later.number],
            earlier.number: metrics.compute_all_facts([earlier], fix_window_days=7)[earlier.number],
        }
        groups = metrics.group_prs([later, earlier], facts, "month")
        # later (April) comes first in the input list; the output must still
        # come out chronological, not insertion-ordered.
        self.assertEqual(list(groups.keys()), ["2026-01", "2026-04"])

    def test_empty_period_between_since_and_until_still_appears(self):
        # Only January and April have PRs, but --since/--until span
        # January-April: February and March must still show up, at count 0,
        # so a quiet month doesn't vanish from a trend.
        later = pr("merged_30h")  # 2026-04
        earlier = pr("rounds_two")  # 2026-01
        facts = {
            later.number: metrics.compute_all_facts([later], fix_window_days=7)[later.number],
            earlier.number: metrics.compute_all_facts([earlier], fix_window_days=7)[earlier.number],
        }
        since, until = date(2026, 1, 1), date(2026, 4, 30)
        groups = metrics.group_prs([later, earlier], facts, "month", since=since, until=until)
        self.assertEqual(list(groups.keys()), ["2026-01", "2026-02", "2026-03", "2026-04"])
        self.assertEqual(groups["2026-02"], [])
        self.assertEqual(groups["2026-03"], [])

        out = metrics.aggregate_group(groups["2026-02"], facts, *metrics.group_window("2026-02", "month", since, until))
        self.assertEqual(out["count"], 0)
        self.assertIsNone(out["revert_rate"])
        self.assertIsNone(out["time_to_merge_h_median"])

    def test_period_labels_between_covers_weeks_spanning_a_partial_range(self):
        labels = metrics.period_labels_between(date(2026, 8, 5), date(2026, 8, 20), "week")
        self.assertEqual(labels, ["2026-W32", "2026-W33", "2026-W34"])

    def test_week_label_is_zero_padded_so_string_sort_matches_chronological(self):
        # Week 9 must read "W09", not "W9" -- otherwise "2026-W10" sorts
        # before "2026-W9" as plain strings, breaking the chronological
        # ordering group_prs relies on.
        w9 = metrics.period_key(date(2026, 2, 23), "week")  # ISO week 9
        w10 = metrics.period_key(date(2026, 3, 2), "week")  # ISO week 10
        self.assertEqual(w9, "2026-W09")
        self.assertEqual(w10, "2026-W10")
        self.assertLess(w9, w10)


class SizeGroupTests(unittest.TestCase):
    def test_buckets_by_additions_plus_deletions(self):
        self.assertEqual(metrics.size_bucket(0), "xs")
        self.assertEqual(metrics.size_bucket(99), "xs")
        self.assertEqual(metrics.size_bucket(100), "s")
        self.assertEqual(metrics.size_bucket(299), "s")
        self.assertEqual(metrics.size_bucket(300), "m")
        self.assertEqual(metrics.size_bucket(699), "m")
        self.assertEqual(metrics.size_bucket(700), "l")

    def test_group_by_size_orders_buckets_xs_s_m_l_even_when_populated_out_of_order(self):
        prs = [pr("rounds_two"), pr("revert_pr")]
        facts = {
            prs[0].number: metrics.compute_all_facts([prs[0]], fix_window_days=7)[prs[0].number],
            prs[1].number: metrics.compute_all_facts([prs[1]], fix_window_days=7)[prs[1].number],
        }
        # Force a known size on each so the bucketing is deterministic
        # regardless of the fixtures' own diff size.
        facts[prs[0].number].size = 800  # l
        facts[prs[1].number].size = 10  # xs

        groups = metrics.group_prs(prs, facts, "size")

        self.assertEqual(list(groups.keys()), ["xs", "s", "m", "l"])
        self.assertEqual({p.number for p in groups["l"]}, {prs[0].number})
        self.assertEqual({p.number for p in groups["xs"]}, {prs[1].number})
        self.assertEqual(groups["s"], [])
        self.assertEqual(groups["m"], [])


class DepthGroupTests(unittest.TestCase):
    def test_groups_by_review_depth(self):
        prs = [pr("approval_changed_by_review"), pr("approval_rubber_stamp"), pr("no_review")]
        facts = {p.number: metrics.PRFacts(
            number=p.number,
            time_to_first_human_review_h=None, time_to_first_bot_review_h=None,
            time_to_first_approval_h=None, time_to_merge_h=None, ready_to_merge_h=None,
            review_rounds=0, changes_requested=0, human_comments=0, bot_comments=0,
            size=0, changed_files=0, reviewed_group="", approval_classes=metrics.compute_approval_classes(p),
        ) for p in prs}
        groups = metrics.group_prs(prs, facts, "depth")
        self.assertEqual({n.number for n in groups["changed-by-review"]}, {pr("approval_changed_by_review").number})
        self.assertEqual({n.number for n in groups["light-review"]}, {pr("approval_rubber_stamp").number})
        self.assertEqual({n.number for n in groups["no-human-approval"]}, {pr("no_review").number})


class ReviewedGroupPrsTests(unittest.TestCase):
    def test_groups_by_human_approved_bot_only_no_review(self):
        prs = [pr("human_approved"), pr("bot_only"), pr("no_review")]
        facts = metrics.compute_all_facts(prs, fix_window_days=7)

        groups = metrics.group_prs(prs, facts, "reviewed")

        self.assertEqual({p.number for p in groups["human-approved"]}, {pr("human_approved").number})
        self.assertEqual({p.number for p in groups["bot-only"]}, {pr("bot_only").number})
        self.assertEqual({p.number for p in groups["no-review"]}, {pr("no_review").number})


class LabelGroupPrsTests(unittest.TestCase):
    def test_multi_label_pr_lands_in_both_groups_and_unlabeled_in_none(self):
        multi = pr("no_review")
        multi.labels = ["bug", "urgent"]
        unlabeled = pr("human_approved")  # fixture has no labels

        facts = metrics.compute_all_facts([multi, unlabeled], fix_window_days=7)
        groups = metrics.group_prs([multi, unlabeled], facts, "label")

        self.assertEqual({p.number for p in groups["bug"]}, {multi.number})
        self.assertEqual({p.number for p in groups["urgent"]}, {multi.number})
        self.assertEqual({p.number for p in groups["(none)"]}, {unlabeled.number})


class ApproverGroupPrsTests(unittest.TestCase):
    def test_pr_with_two_approvers_lands_in_both_and_no_approver_in_none(self):
        two_approvers, none_approved = pr("human_approved"), pr("no_review")
        facts = {
            two_approvers.number: metrics.PRFacts(
                number=two_approvers.number,
                time_to_first_human_review_h=None, time_to_first_bot_review_h=None,
                time_to_first_approval_h=None, time_to_merge_h=None, ready_to_merge_h=None,
                review_rounds=0, changes_requested=0, human_comments=0, bot_comments=0,
                size=0, changed_files=0, reviewed_group="",
                approval_classes={"grace": "silent", "carol": "changed_by_review"},
            ),
            none_approved.number: metrics.PRFacts(
                number=none_approved.number,
                time_to_first_human_review_h=None, time_to_first_bot_review_h=None,
                time_to_first_approval_h=None, time_to_merge_h=None, ready_to_merge_h=None,
                review_rounds=0, changes_requested=0, human_comments=0, bot_comments=0,
                size=0, changed_files=0, reviewed_group="", approval_classes={},
            ),
        }

        groups = metrics.group_prs([two_approvers, none_approved], facts, "approver")

        self.assertEqual({p.number for p in groups["grace"]}, {two_approvers.number})
        self.assertEqual({p.number for p in groups["carol"]}, {two_approvers.number})
        self.assertEqual({p.number for p in groups["(none)"]}, {none_approved.number})


class GroupWindowTests(unittest.TestCase):
    def test_period_group_is_clipped_to_the_report_window(self):
        since, until = date(2026, 6, 10), date(2026, 9, 18)
        self.assertEqual(metrics.group_window("2026-06", "month", since, until), (date(2026, 6, 10), date(2026, 6, 30)))
        self.assertEqual(metrics.group_window("2026-07", "month", since, until), (date(2026, 7, 1), date(2026, 7, 31)))
        self.assertEqual(metrics.group_window("2026-09", "month", since, until), (date(2026, 9, 1), date(2026, 9, 18)))

    def test_non_period_group_uses_the_whole_window(self):
        since, until = date(2026, 6, 10), date(2026, 9, 18)
        self.assertEqual(metrics.group_window("sonar", "team", since, until), (since, until))


if __name__ == "__main__":
    unittest.main()
