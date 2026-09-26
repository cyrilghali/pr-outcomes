import os
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone

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
