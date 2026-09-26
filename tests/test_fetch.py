import unittest

from pr_outcomes.fetch import merge_timeline_into_node


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


if __name__ == "__main__":
    unittest.main()
