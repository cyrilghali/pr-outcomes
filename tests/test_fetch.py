import json
import os
import tempfile
import subprocess
import unittest
from unittest import mock
from datetime import date, datetime, time, timedelta, timezone

from pr_outcomes import fetch
from pr_outcomes.fetch import _chunk_reusable, _is_transient_gh_error, merge_timeline_into_node


class MergeTimelineIntoNodeTests(unittest.TestCase):
    def test_replaces_timeline_and_leaves_rest_untouched(self):
        node = {"number": 42, "title": "Fix thing", "timelineItems": {"nodes": []}}
        timeline_nodes = [
            {"__typename": "PullRequestCommit", "commit": {"committedDate": "2026-01-01T00:00:00Z"}},
            {"__typename": "HeadRefForcePushedEvent", "createdAt": "2026-01-01T01:00:00Z"},
        ]

        merged = merge_timeline_into_node(node, timeline_nodes)

        self.assertEqual(merged["timelineItems"], {"nodes": timeline_nodes})
        self.assertEqual(merged["number"], 42)
        self.assertEqual(merged["title"], "Fix thing")
        # original node is untouched
        self.assertEqual(node["timelineItems"], {"nodes": []})


class ChunkReusableTests(unittest.TestCase):
    def test_not_reusable_when_written_before_chunk_day_ends(self):
        to = date(2026, 6, 10)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "chunk.json")
            open(path, "w").close()
            # written mid-day on the chunk's last day, UTC: not reusable, or
            # PRs merged later that same day would be lost forever.
            mid_day = datetime.combine(to, time(12), tzinfo=timezone.utc)
            os.utime(path, (mid_day.timestamp(), mid_day.timestamp()))
            self.assertFalse(_chunk_reusable(path, to))

    def test_reusable_once_written_after_chunk_day_ends_utc(self):
        to = date(2026, 6, 10)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "chunk.json")
            open(path, "w").close()
            after_day = datetime.combine(to + timedelta(days=1), time(0, 1), tzinfo=timezone.utc)
            os.utime(path, (after_day.timestamp(), after_day.timestamp()))
            self.assertTrue(_chunk_reusable(path, to))

    def test_missing_file_not_reusable(self):
        self.assertFalse(_chunk_reusable("/nonexistent/chunk.json", date(2026, 6, 10)))


class TransientGhErrorTests(unittest.TestCase):
    def test_secondary_rate_limit(self):
        self.assertTrue(_is_transient_gh_error("You have exceeded a secondary rate limit"))

    def test_abuse_detection(self):
        self.assertTrue(_is_transient_gh_error("triggered an abuse detection mechanism"))

    def test_submitted_too_quickly(self):
        self.assertTrue(_is_transient_gh_error("was submitted too quickly"))

    def test_gateway_502(self):
        self.assertTrue(_is_transient_gh_error("HTTP 502: Bad Gateway"))

    def test_gateway_504(self):
        self.assertTrue(_is_transient_gh_error("HTTP 504: Gateway Timeout"))

    def test_timeout(self):
        self.assertTrue(_is_transient_gh_error("context deadline exceeded: timeout"))

    def test_case_insensitive(self):
        self.assertTrue(_is_transient_gh_error("RATE LIMIT exceeded"))

    def test_non_transient_error_not_retried(self):
        self.assertFalse(_is_transient_gh_error('Could not resolve to a Repository with the name "x/y"'))


if __name__ == "__main__":
    unittest.main()


class RunGhGraphqlRetryTests(unittest.TestCase):
    def _run(self, stderrs: list[str]):
        results = [subprocess.CompletedProcess([], 1, "", e) for e in stderrs]
        with mock.patch.object(fetch.subprocess, "run", side_effect=results) as run, \
                mock.patch.object(fetch.time, "sleep") as sleep:
            with self.assertRaises(fetch.GhError):
                fetch._run_gh_graphql("query{}", {})
        return run.call_count, [c.args[0] for c in sleep.call_args_list]

    def test_transient_errors_retry_with_backoff_then_fail(self):
        calls, sleeps = self._run(["gh: Bad Gateway (HTTP 502)"] * 5)
        self.assertEqual(calls, 5)
        self.assertEqual(sleeps, [2, 4, 8, 16])

    def test_other_errors_fail_immediately(self):
        calls, sleeps = self._run(["Could not resolve to a PullRequest with the number of 15023"])
        self.assertEqual((calls, sleeps), (1, []))


class FetchOrgTeamsTests(unittest.TestCase):
    def _with_cache_root(self, tmp: str):
        return mock.patch.object(fetch, "CACHE_ROOT", tmp)

    def test_builds_login_to_teams_map_from_two_teams(self):
        teams_json = json.dumps([{"slug": "platform"}, {"slug": "sonar"}])
        platform_members = json.dumps([{"login": "alice"}])
        sonar_members = json.dumps([{"login": "alice"}, {"login": "bob"}])
        results = [
            subprocess.CompletedProcess([], 0, teams_json, ""),
            subprocess.CompletedProcess([], 0, platform_members, ""),
            subprocess.CompletedProcess([], 0, sonar_members, ""),
        ]
        with tempfile.TemporaryDirectory() as tmp, self._with_cache_root(tmp), \
                mock.patch.object(fetch.subprocess, "run", side_effect=results):
            teams_by_login, warnings = fetch.fetch_org_teams("acme", "widgets")

        self.assertEqual(teams_by_login, {"alice": {"platform", "sonar"}, "bob": {"sonar"}})
        self.assertEqual(warnings, [])

    def test_404_falls_back_to_empty_map_with_warning(self):
        not_found = subprocess.CompletedProcess([], 1, "", "gh: Not Found (HTTP 404)")
        with tempfile.TemporaryDirectory() as tmp, self._with_cache_root(tmp), \
                mock.patch.object(fetch.subprocess, "run", return_value=not_found):
            teams_by_login, warnings = fetch.fetch_org_teams("someuser", "widgets")

        self.assertEqual(teams_by_login, {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("someuser", warnings[0])

    def test_fresh_cache_is_reused_without_calling_gh(self):
        with tempfile.TemporaryDirectory() as tmp, self._with_cache_root(tmp):
            path = fetch._teams_cache_path("acme", "widgets")
            os.makedirs(os.path.dirname(path))
            with open(path, "w") as f:
                json.dump({"alice": ["platform"]}, f)
            with mock.patch.object(fetch.subprocess, "run") as run:
                teams_by_login, warnings = fetch.fetch_org_teams("acme", "widgets")
            run.assert_not_called()
        self.assertEqual(teams_by_login, {"alice": {"platform"}})

    def test_stale_cache_is_refetched(self):
        teams_json = json.dumps([])
        with tempfile.TemporaryDirectory() as tmp, self._with_cache_root(tmp):
            path = fetch._teams_cache_path("acme", "widgets")
            os.makedirs(os.path.dirname(path))
            with open(path, "w") as f:
                json.dump({"alice": ["platform"]}, f)
            stale = datetime.now(timezone.utc).timestamp() - fetch.TEAMS_CACHE_MAX_AGE_SECONDS - 1
            os.utime(path, (stale, stale))
            with mock.patch.object(
                fetch.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, teams_json, ""),
            ) as run:
                teams_by_login, warnings = fetch.fetch_org_teams("acme", "widgets")
            run.assert_called_once()
        self.assertEqual(teams_by_login, {})


class CubicScoreTest(unittest.TestCase):
    def test_last_score_before_merge_wins(self):
        def review(at: str, score: int) -> dict[str, object]:
            return {"author": {"login": "cubic-dev-ai", "__typename": "Bot"}, "state": "COMMENTED",
                    "submittedAt": at, "comments": {"totalCount": 0},
                    "body": f"<!-- cubic:review-summary:confidence-score:{score}/5 -->"}
        node = {
            "number": 1, "title": "t", "url": "u", "author": {"login": "a"}, "baseRefName": "staging",
            "createdAt": "2026-09-01T00:00:00Z", "mergedAt": "2026-09-01T12:00:00Z",
            "reviews": {"nodes": [review("2026-09-01T01:00:00Z", 3), review("2026-09-01T02:00:00Z", 5),
                                  review("2026-09-01T13:00:00Z", 2)]},
            "timelineItems": {"nodes": []},
        }
        pr = fetch.normalise_pr(node)
        self.assertEqual((pr.cubic_first_score, pr.cubic_score), (3, 5))

    def test_score_is_read_from_edit_history_when_an_edit_drops_it(self):
        node = {
            "number": 1, "title": "t", "url": "u", "author": {"login": "a"}, "baseRefName": "staging",
            "createdAt": "2026-09-01T00:00:00Z", "mergedAt": "2026-09-01T12:00:00Z",
            "reviews": {"nodes": [{
                "author": {"login": "cubic-dev-ai", "__typename": "Bot"}, "state": "COMMENTED",
                "submittedAt": "2026-09-01T01:00:00Z", "comments": {"totalCount": 0},
                "body": "Review completed against the latest diff",
                "userContentEdits": {"nodes": [
                    {"editedAt": "2026-09-01T03:00:00Z", "diff": "Review completed against the latest diff"},
                    {"editedAt": "2026-09-01T02:00:00Z", "diff": "<!-- cubic:review-summary:confidence-score:4/5 -->"},
                    {"editedAt": "2026-09-01T01:00:00Z", "diff": "<!-- cubic:review-summary:confidence-score:2/5 -->"},
                ]},
            }]},
            "timelineItems": {"nodes": []},
        }
        pr = fetch.normalise_pr(node)
        self.assertEqual((pr.cubic_first_score, pr.cubic_score), (2, 4))

    def test_edit_with_null_edited_at_is_dated_at_submission(self):
        node = {
            "number": 1, "title": "t", "url": "u", "author": {"login": "a"}, "baseRefName": "staging",
            "createdAt": "2026-09-01T00:00:00Z", "mergedAt": "2026-09-01T12:00:00Z",
            "reviews": {"nodes": [{
                "author": {"login": "cubic-dev-ai", "__typename": "Bot"}, "state": "COMMENTED",
                "submittedAt": "2026-09-01T01:00:00Z", "comments": {"totalCount": 0}, "body": "",
                "userContentEdits": {"nodes": [
                    {"editedAt": None, "diff": "<!-- cubic:review-summary:confidence-score:3/5 -->"},
                ]},
            }]},
            "timelineItems": {"nodes": []},
        }
        pr = fetch.normalise_pr(node)
        self.assertEqual((pr.cubic_first_score, pr.cubic_score), (3, 3))
