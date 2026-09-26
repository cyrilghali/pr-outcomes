import subprocess
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from pr_outcomes import deploys


class LinkDeploysTests(unittest.TestCase):
    def setUp(self):
        self.d0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # s0: earliest merge, no run recorded for it at all.
        # s1: first *kept* deploy -- its lower bound must come from s0's
        #     release_head, since there is no earlier kept deploy.
        # s2: has no successful run -- its PRs must roll into s3.
        # s3: next kept deploy after s1, spanning (h1, h3].
        self.merges = [
            ("s0", "h0", self.d0),
            ("s1", "h1", self.d0 + timedelta(days=1)),
            ("s2", "h2", self.d0 + timedelta(days=2)),
            ("s3", "h3", self.d0 + timedelta(days=3)),
        ]
        self.run_times = {
            "s1": self.d0 + timedelta(days=1, hours=1),
            "s3": self.d0 + timedelta(days=3, hours=1),
        }
        self.sha_to_pr = {"c1": 1, "c2": 2, "c3": 3}
        self.commits_between_calls = []

    def _commits_between(self, lo: str, hi: str) -> list[str]:
        self.commits_between_calls.append((lo, hi))
        table = {
            ("h0", "h1"): ["c1"],
            ("h1", "h3"): ["c2", "c3"],
        }
        return table.get((lo, hi), [])

    def test_failed_run_merge_rolls_into_next_deploy(self):
        result = deploys.link_deploys(self.merges, self.run_times, self._commits_between, self.sha_to_pr)
        self.assertEqual([d.sha for d in result], ["s1", "s3"])
        self.assertEqual(result[1].prs, frozenset({2, 3}))
        self.assertIn(("h1", "h3"), self.commits_between_calls)

    def test_first_kept_deploy_lower_bound_is_preceding_merge(self):
        result = deploys.link_deploys(self.merges, self.run_times, self._commits_between, self.sha_to_pr)
        self.assertEqual(result[0].sha, "s1")
        self.assertEqual(result[0].prs, frozenset({1}))
        self.assertIn(("h0", "h1"), self.commits_between_calls)

    def test_unknown_range_skipped_when_no_preceding_merge(self):
        merges = [("s0", "h0", self.d0)]
        run_times = {"s0": self.d0}
        result = deploys.link_deploys(merges, run_times, self._commits_between, self.sha_to_pr)
        self.assertEqual(result, [])

    def test_pr_mapping_drops_commits_not_in_sha_to_pr(self):
        result = deploys.link_deploys(self.merges, self.run_times, self._commits_between, {"c1": 1})
        self.assertEqual(result[0].prs, frozenset({1}))
        self.assertEqual(result[1].prs, frozenset())

    def test_deployed_at_is_the_run_times_value_for_that_sha(self):
        result = deploys.link_deploys(self.merges, self.run_times, self._commits_between, self.sha_to_pr)
        self.assertEqual(result[0].deployed_at, self.run_times["s1"])
        self.assertEqual(result[1].deployed_at, self.run_times["s3"])


class FetchDeployRunsTests(unittest.TestCase):
    def test_earliest_success_wins(self):
        # Same workflow twice in one response; earliest updated_at per sha wins.
        tsv = "sha1\t2026-01-02T00:00:00Z\nsha1\t2026-01-01T00:00:00Z\nsha2\t2026-01-05T00:00:00Z\n"
        with mock.patch.object(
            deploys.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, tsv, ""),
        ):
            result = deploys.fetch_deploy_runs("acme", "widgets", date(2026, 1, 1))
        self.assertEqual(result["sha1"], datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(result["sha2"], datetime(2026, 1, 5, tzinfo=timezone.utc))

    def test_queries_both_workflows_and_merges_earliest_per_sha(self):
        # deploy-production.yml (post-rename) only knows sha1, at a later
        # time than the pre-rename cd-production.yml also has it under;
        # cd-production.yml alone has sha2. The earliest per sha must win
        # across workflows, and both must be queried and merged.
        new_workflow_tsv = "sha1\t2026-01-02T00:00:00Z\n"
        old_workflow_tsv = "sha1\t2026-01-01T00:00:00Z\nsha2\t2026-01-05T00:00:00Z\n"
        responses = [
            subprocess.CompletedProcess([], 0, new_workflow_tsv, ""),
            subprocess.CompletedProcess([], 0, old_workflow_tsv, ""),
        ]
        with mock.patch.object(deploys.subprocess, "run", side_effect=responses) as run:
            result = deploys.fetch_deploy_runs("acme", "widgets", date(2026, 1, 1))

        self.assertEqual(result["sha1"], datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(result["sha2"], datetime(2026, 1, 5, tzinfo=timezone.utc))
        queried_paths = [call.args[0][3] for call in run.call_args_list]
        self.assertTrue(any("deploy-production.yml" in p for p in queried_paths))
        self.assertTrue(any("cd-production.yml" in p for p in queried_paths))

    def test_gh_failure_on_either_workflow_raises_gherror(self):
        with mock.patch.object(
            deploys.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "gh: error"),
        ):
            with self.assertRaises(deploys.GhError):
                deploys.fetch_deploy_runs("acme", "widgets", date(2026, 1, 1))


class BuildDeploysWarningsTests(unittest.TestCase):
    def test_stale_production_and_no_run_count_warnings(self):
        last_merge_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        merges = [
            ("s0", "h0", last_merge_date),
            ("s1", "h1", last_merge_date + timedelta(days=1)),  # since <= this date, and no run -> counted
        ]
        with mock.patch.object(deploys, "production_merges", return_value=merges), \
             mock.patch.object(deploys, "fetch_deploy_runs", return_value={}), \
             mock.patch.object(deploys.blame, "build_sha_to_pr", return_value={}):
            result, warnings = deploys.build_deploys(
                "/repo", "acme", "widgets", "staging", date(2026, 1, 1), date(2026, 2, 1),
            )

        self.assertEqual(result, [])
        self.assertTrue(any("origin/production was last updated" in w for w in warnings))
        self.assertTrue(any("production merges had no successful deploy run" in w for w in warnings))

    def test_no_warnings_when_up_to_date_and_every_merge_has_a_run(self):
        merges = [("s0", "h0", datetime(2026, 2, 1, tzinfo=timezone.utc))]
        run_times = {"s0": datetime(2026, 2, 1, 1, tzinfo=timezone.utc)}
        with mock.patch.object(deploys, "production_merges", return_value=merges), \
             mock.patch.object(deploys, "fetch_deploy_runs", return_value=run_times), \
             mock.patch.object(deploys.blame, "build_sha_to_pr", return_value={}):
            result, warnings = deploys.build_deploys(
                "/repo", "acme", "widgets", "staging", date(2026, 1, 1), date(2026, 2, 1),
            )
        self.assertEqual(warnings, [])
        # a single merge with no preceding merge is skipped (unknown range)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
