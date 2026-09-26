import json
import os
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

from pr_outcomes import sentry
from pr_outcomes.deploys import Deploy


class ExtractFramesTests(unittest.TestCase):
    def test_string_release(self):
        release, frames = sentry.extract_frames({"release": "abc123", "entries": []})
        self.assertEqual(release, "abc123")
        self.assertEqual(frames, [])

    def test_dict_release(self):
        release, _ = sentry.extract_frames({"release": {"version": "abc123"}, "entries": []})
        self.assertEqual(release, "abc123")

    def test_null_stacktrace_skipped(self):
        event = {"release": "abc", "entries": [
            {"type": "exception", "data": {"values": [{"stacktrace": None}]}},
        ]}
        self.assertEqual(sentry.extract_frames(event)[1], [])

    def test_non_exception_entries_ignored(self):
        event = {"release": "abc", "entries": [{"type": "breadcrumbs", "data": {}}]}
        self.assertEqual(sentry.extract_frames(event)[1], [])

    def test_non_in_app_frames_dropped(self):
        event = {"release": "abc", "entries": [
            {"type": "exception", "data": {"values": [{"stacktrace": {"frames": [
                {"filename": "lib/vendor.ex", "lineNo": 10, "inApp": False},
                {"filename": "lib/app.ex", "lineNo": 20, "inApp": True},
            ]}}]}},
        ]}
        self.assertEqual(sentry.extract_frames(event)[1], [("lib/app.ex", 20)])

    def test_dedupes_frames_keeping_order(self):
        event = {"release": "abc", "entries": [
            {"type": "exception", "data": {"values": [{"stacktrace": {"frames": [
                {"filename": "lib/app.ex", "lineNo": 20, "inApp": True},
                {"filename": "lib/app.ex", "lineNo": 20, "inApp": True},
                {"filename": "lib/app.ex", "lineNo": 30, "inApp": True},
            ]}}]}},
        ]}
        self.assertEqual(sentry.extract_frames(event)[1], [("lib/app.ex", 20), ("lib/app.ex", 30)])


class TokenLoadingTests(unittest.TestCase):
    def test_env_beats_file(self):
        with mock.patch.dict(os.environ, {"SENTRY_AUTH_TOKEN": "env-token"}):
            self.assertEqual(sentry.load_token(), "env-token")

    def test_file_used_when_no_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".sentryclirc")
            with open(path, "w") as f:
                f.write("[auth]\ntoken=file-token\n")
            env = dict(os.environ)
            env.pop("SENTRY_AUTH_TOKEN", None)
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(sentry.os.path, "expanduser", return_value=path):
                self.assertEqual(sentry.load_token(), "file-token")


class FakeResponse:
    def __init__(self, payload: object, headers: dict[str, str]):
        self._payload = json.dumps(payload).encode()
        self.headers = headers

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *a: object) -> bool:
        return False


class FetchNewIssuesPaginationTests(unittest.TestCase):
    def test_paginates_via_link_header(self):
        responses = [
            FakeResponse({"id": "42"}, {}),  # project id lookup
            FakeResponse([{"shortId": "PARROT-1"}], {"Link": '<https://sentry.io/next-page>; rel="next"; results="true"'}),
            FakeResponse([{"shortId": "PARROT-2"}], {"Link": '<https://sentry.io/x>; rel="next"; results="false"'}),
        ]
        with mock.patch.dict(os.environ, {"SENTRY_AUTH_TOKEN": "tok"}), \
             mock.patch.object(sentry.urllib.request, "urlopen", side_effect=responses):
            short_ids = sentry.fetch_new_issues("acme", "widgets", date(2026, 1, 1), date(2026, 2, 1))
        self.assertEqual(short_ids, ["PARROT-1", "PARROT-2"])


class LoadOldestEventsCacheTests(unittest.TestCase):
    def test_cache_hit_skips_http(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sentry, "CACHE_ROOT", tmp):
            sentry._save_cached_event("acme", "widgets", "PARROT-1", "sha", [("lib/a.ex", 1)])
            with mock.patch.object(sentry, "fetch_oldest_event") as fetch_mock:
                result = sentry.load_oldest_events("acme", "acme", "widgets", ["PARROT-1"])
            fetch_mock.assert_not_called()
        self.assertEqual(result["PARROT-1"], ("sha", [("lib/a.ex", 1)]))

    def test_uncached_issue_is_fetched(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sentry, "CACHE_ROOT", tmp), \
             mock.patch.object(sentry, "fetch_oldest_event", return_value={"release": "sha", "entries": []}):
            result = sentry.load_oldest_events("acme", "acme", "widgets", ["PARROT-2"])
        self.assertEqual(result["PARROT-2"], ("sha", []))


class GetRetriesConnectionErrorsTests(unittest.TestCase):
    """_get must retry a connection failure or timeout with the same backoff
    as an HTTP 429/5xx, then raise SentryError (not propagate the raw
    urllib exception) once retries are exhausted."""

    def test_url_error_retries_then_succeeds(self):
        responses = [sentry.urllib.error.URLError("connection refused"), FakeResponse({"ok": True}, {})]
        with mock.patch.dict(os.environ, {"SENTRY_AUTH_TOKEN": "tok"}), \
             mock.patch.object(sentry.urllib.request, "urlopen", side_effect=responses), \
             mock.patch.object(sentry.time, "sleep"):
            data, _ = sentry._get("/api/0/x/")
        self.assertEqual(data, {"ok": True})

    def test_timeout_error_retries_then_succeeds(self):
        responses = [TimeoutError("timed out"), FakeResponse({"ok": True}, {})]
        with mock.patch.dict(os.environ, {"SENTRY_AUTH_TOKEN": "tok"}), \
             mock.patch.object(sentry.urllib.request, "urlopen", side_effect=responses), \
             mock.patch.object(sentry.time, "sleep"):
            data, _ = sentry._get("/api/0/x/")
        self.assertEqual(data, {"ok": True})

    def test_persistent_url_error_raises_sentry_error_not_url_error(self):
        with mock.patch.dict(os.environ, {"SENTRY_AUTH_TOKEN": "tok"}), \
             mock.patch.object(sentry.urllib.request, "urlopen", side_effect=sentry.urllib.error.URLError("down")), \
             mock.patch.object(sentry.time, "sleep"):
            with self.assertRaises(sentry.SentryError):
                sentry._get("/api/0/x/")


class BlamedPrsRetriesLinesIndividuallyTests(unittest.TestCase):
    """A batched `git blame -L n,n -L m,m` fails entirely when one requested
    line is past the file's end; blamed_prs must retry each line alone so a
    single bad line doesn't drop the file's valid ones."""

    def test_valid_line_survives_an_invalid_line_in_the_same_batch(self):
        with tempfile.TemporaryDirectory() as repo:
            subprocess.run(["git", "init", "-q", "-b", "staging", repo], check=True)
            with open(os.path.join(repo, "file.txt"), "w") as f:
                f.write("a1\na2\na3\n")  # only 3 lines -- line 99 doesn't exist
            subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
            }
            subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "init"], check=True, env=env)
            commit = subprocess.run(
                ["git", "-C", repo, "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
            ).stdout.strip()

            prs = sentry.blamed_prs(repo, commit, [("file.txt", 2), ("file.txt", 99)], {commit: 42})

        self.assertEqual(prs, {42})


class AttributeNewIssuesTests(unittest.TestCase):
    def test_filters_by_deploy_membership_and_counts_warnings(self):
        deploy = Deploy(
            sha="prodsha", release_head="head1",
            deployed_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
            prs=frozenset({10, 20}),
        )
        events = {
            "P-1": ("prodsha", [("lib/a.ex", 5)]),          # blamed PR 10 -> in deploy.prs -> kept
            "P-2": ("prodsha", [("lib/b.ex", 5)]),          # blamed PR 99 -> not in deploy.prs -> dropped
            "P-3": ("unknown-release", [("lib/c.ex", 5)]),  # release not a known deploy
            "P-4": ("prodsha", []),                         # no in-app frames
        }

        def fake_blamed_prs(
            repo_path: str, commit: str, frames: list[tuple[str, int]], sha_to_pr: dict[str, int],
        ) -> set[int]:
            if frames == [("lib/a.ex", 5)]:
                return {10}
            if frames == [("lib/b.ex", 5)]:
                return {99}
            return set()

        with mock.patch.object(sentry, "fetch_new_issues", return_value=list(events.keys())), \
             mock.patch.object(sentry, "load_oldest_events", return_value=events), \
             mock.patch.object(sentry, "blamed_prs", side_effect=fake_blamed_prs):
            attributed, warnings = sentry.attribute_new_issues(
                "/repo", "acme", "widgets", "acme/widgets",
                date(2026, 1, 1), date(2026, 2, 1), [deploy], {},
            )

        self.assertEqual(attributed, {10: ["P-1"]})
        self.assertIn("1 of 4", warnings[0])
        self.assertIn("1 without app stack frames", warnings[0])
        self.assertIn("1 on a release that is not a known deploy", warnings[0])


if __name__ == "__main__":
    unittest.main()
