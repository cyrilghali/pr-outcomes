import os
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone

from pr_outcomes.fetch import _chunk_reusable, merge_timeline_into_node


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


if __name__ == "__main__":
    unittest.main()
