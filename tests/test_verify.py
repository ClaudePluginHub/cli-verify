import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import verify  # noqa: E402

# Recorded Claude Code 2.1.302 output (claudepluginhub cli-oracle c28 and the 2026-10-17 probes).
REFUSED = (
    '{"command":"install","outcome":"failed","plugin":"p@cpd-p","scope":"user","message":"Plugin temp_github_1792291888483_qr227e has an '
    'invalid manifest file at /tmp/cliv-x/cfg/plugins/cache/temp_github_1792291888483_qr227e/.claude-plugin/plugin.json.\\n\\nValidation '
    'errors: author: Invalid input: expected object, received string","failureCode":"error_validation"}'
)
COMMAND_SOURCE = (
    '{"command":"install","outcome":"failed","plugin":"c@m","scope":"user","message":"c@m is installed by running a '
    'command on this machine","failureCode":"command_source_refused"}'
)
CLONE_404 = (
    '{"command":"install","outcome":"failed","plugin":"h@m","scope":"user","message":"Failed to clone repository: '
    "fatal: repository 'https://github.com/gone/repo/' not found\",\"failureCode\":\"error_not_found\"}"
)
CLONE_NETWORK = (
    '{"command":"install","outcome":"failed","plugin":"h@m","scope":"user","message":"Failed to clone repository: '
    "fatal: unable to access 'https://github.com/a/b/': Could not resolve host: github.com\",\"failureCode\":\"error_network\"}"
)
OK = (
    '{"command":"install","outcome":"ok","plugin":"code-review@claude-plugins-official","pluginId":'
    '"code-review@claude-plugins-official","scope":"user","message":"Successfully installed plugin: '
    'code-review@claude-plugins-official (scope: user)"}'
)


class CliArgvTest(unittest.TestCase):
    def test_allows_exactly_the_three_plugin_commands(self):
        self.assertEqual(verify.cli_argv("claude", ["plugin", "marketplace", "add", "acme/tools"])[1:4], ["plugin", "marketplace", "add"])
        verify.cli_argv("claude", ["plugin", "install", "p@m", "--json"])
        verify.cli_argv("claude", ["plugin", "list", "--json"])

    def test_refuses_anything_else(self):
        for args in (
            ["-p", "hi"],
            ["hello"],
            ["mcp", "list"],
            ["plugin", "install", "p@m"],
            ["plugin", "install", "--force", "--json"],
            ["plugin", "marketplace", "add", "--scope=project"],
            ["plugin", "marketplace", "add", "a", "b"],
            ["plugin", "details", "p@m"],
        ):
            with self.assertRaises(ValueError, msg=args):
                verify.cli_argv("claude", args)


class ClassifyTest(unittest.TestCase):
    def test_install_outcomes(self):
        self.assertEqual(verify.classify_install(verify.parse_install("Installing…\n" + REFUSED), ""), "install_refused")
        self.assertEqual(verify.classify_install(verify.parse_install(COMMAND_SOURCE), ""), "command_source_refused")
        # A repository that is gone is a fact about the listing, not a transient failure.
        self.assertEqual(verify.classify_install(verify.parse_install(CLONE_404), ""), "install_refused")
        self.assertEqual(verify.classify_install(verify.parse_install(CLONE_NETWORK), ""), "error")
        self.assertEqual(verify.classify_install(verify.parse_install(OK + "\n✔ done"), ""), "installed")
        self.assertEqual(verify.classify_install(None, "segfault"), "error")
        self.assertEqual(verify.classify_install({"command": "install", "outcome": "installed"}, ""), "error")
        self.assertEqual(
            verify.classify_install({"outcome": "failed", "failureCode": "error_network", "message": "x"}, ""), "error"
        )

    def test_names_never_read_as_transient(self):
        for name in ("acme/ssl-tools", "acme/mtls-proxy", "tls-audit@m"):
            self.assertEqual(verify.classify_add_failure(f"fatal: repository '{name}' not found"), "add_refused")
            failed = {"outcome": "failed", "failureCode": "error_not_found", "message": f"Plugin '{name}' not found"}
            self.assertEqual(verify.classify_install(failed, ""), "install_refused")

    def test_add_failures(self):
        self.assertEqual(verify.classify_add_failure("Error: Marketplace name cannot contain spaces"), "add_refused")
        self.assertEqual(verify.classify_add_failure("fatal: unable to access: Could not resolve host"), "error")
        self.assertEqual(verify.classify_add_failure("Request failed with status code 503"), "error")

    def test_load_errors_for_the_installed_plugin_only(self):
        listed = json.dumps([
            {"id": "other@m", "errors": ["nope"]},
            {"id": "p@cpd-p", "errors": ["Path not found: /x/hooks (hooks)"]},
        ])
        self.assertEqual(verify.load_errors(listed, "p@cpd-p"), ["Path not found: /x/hooks (hooks)"])
        self.assertEqual(verify.load_errors(json.dumps([{"id": "q@m", "errors": []}]), "q@m"), [])
        # Not listed or unreadable is not proof it loads.
        self.assertIsNone(verify.load_errors(listed, "q@m"))
        self.assertIsNone(verify.load_errors("not json", "p@cpd-p"))

    def test_load_outcome_follows_c25_error_types(self):
        # Recorded `plugin list --json` records (claudepluginhub cli-oracle c25, Claude Code 2.1.302).
        whole = {"id": "h@m", "errors": ["hooks.json declares PreToolUse/PermissionRequest at its top level"],
                 "errorDetails": [{"type": "generic-error", "plugin": "h"}]}
        hook = {"id": "h@m", "errors": ["Hook load failed: [...]"],
                "errorDetails": [{"type": "hook-load-failed", "plugin": "h", "path": "/x/hooks/hooks.json"}]}
        path = {"id": "h@m", "errors": ["Path not found: /x/agents (agents)"],
                "errorDetails": [{"type": "path-not-found", "plugin": "h", "path": "/x/agents"}]}
        mixed = {**hook, "errorDetails": hook["errorDetails"] + whole["errorDetails"]}
        unplaced = {**hook, "errorDetails": [{"type": "something-new", "plugin": "h"}]}
        legacy = {"id": "h@m", "errors": ["Hook load failed"]}
        for record, outcome in (
            ({"id": "h@m", "errors": []}, "installed"),
            (whole, "load_refused"),
            (hook, "load_warnings"),
            (path, "load_warnings"),
            (mixed, "load_refused"),
            (unplaced, "load_errors"),
            (legacy, "load_errors"),
        ):
            self.assertEqual(verify.load_outcome(json.dumps([record]), "h@m"), outcome, record)

    def test_installed_ref_from_the_cli_record(self):
        sha = "c494ecef09253c615807669e7b28fb9723bc3c61"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(verify.installed_ref(root, "p@m"))  # no file: a failed install
            (root / "cfg" / "plugins").mkdir(parents=True)
            record = root / "cfg" / "plugins" / "installed_plugins.json"
            record.write_text(json.dumps({"plugins": {"p@m": [{"version": "1.2.3", "gitCommitSha": sha}],
                                                      "l@m": [{"version": "1.0.0", "gitCommitSha": None}],
                                                      "x@m": [{"gitCommitSha": "main"}]}}))
            self.assertEqual(verify.installed_ref(root, "p@m"), sha)
            self.assertIsNone(verify.installed_ref(root, "l@m"))  # not a git clone (c10)
            self.assertIsNone(verify.installed_ref(root, "x@m"))  # not a full commit
            self.assertIsNone(verify.installed_ref(root, "q@m"))
            record.write_text("not json")
            self.assertIsNone(verify.installed_ref(root, "p@m"))

    def test_mcp_servers_of_the_installed_plugin(self):
        listed = json.dumps([{"id": "p@m", "mcpServers": {"web": {"command": "node"}, "db": {"command": "x"}}}])
        self.assertEqual(verify.mcp_servers(listed, "p@m"), ["db", "web"])
        self.assertEqual(verify.mcp_servers(json.dumps([{"id": "p@m"}]), "p@m"), [])

    def test_normalize_shows_paths_as_the_users_own(self):
        root = Path("/tmp/cliv-x")
        message = verify.parse_install(REFUSED)["message"]
        self.assertIn("~/.claude/plugins/cache/temp_github_…/.claude-plugin/plugin.json", verify.normalize(message, root))
        self.assertNotIn("/tmp/cliv-x", verify.normalize(message, root))
        # The random clone-folder name is dropped, so the same refusal reads the same everywhere.
        self.assertIn("Plugin temp_github_… has an invalid manifest", verify.normalize(message, root))
        self.assertEqual(len(verify.normalize("x" * 5000, root)), verify.MESSAGE_MAX)

    def test_normalize_resolved_paths_whole(self):
        root = Path(tempfile.mkdtemp())
        resolved = str((root / "cfg").resolve())
        for _ in range(5):
            self.assertEqual(verify.normalize(f"at {resolved}/plugins/x", root), "at ~/.claude/plugins/x")


class CheckOneTest(unittest.TestCase):
    def test_a_bad_target_is_an_error_not_a_crash(self):
        target = {"pluginId": "1", "slug": "a", "addArg": "-x", "installArg": "a@m", "checkKey": "k1"}
        result = verify.check_one("/nonexistent/claude", target, "linux")
        self.assertEqual(result["outcome"], "error")
        self.assertIn("refusing to run claude", result["message"])


class RetryTest(unittest.TestCase):
    """check_one with check_attempt stubbed: each call returns the next scripted result."""

    target = {"pluginId": "1", "slug": "a", "addArg": "x", "installArg": "a@m", "checkKey": "k1"}

    def run_with(self, *attempts):
        calls = []

        def attempt(claude, target, os_name):
            outcome, message = attempts[len(calls)]
            calls.append(outcome)
            return {"pluginId": "1", "os": os_name, "checkKey": "k1", "outcome": outcome, "message": message,
                    "checkedAt": "2026-10-24T10:00:00Z", "durationMs": 100, "loadErrors": []}

        with mock.patch.object(verify, "check_attempt", attempt):
            return verify.check_one("claude", self.target, "linux"), len(calls)

    def test_installed_and_error_are_not_retried(self):
        for outcome in ("installed", "error"):
            result, calls = self.run_with((outcome, None))
            self.assertEqual((result["outcome"], calls), (outcome, 1))

    def test_agreeing_attempts_keep_the_result(self):
        result, calls = self.run_with(("install_refused", "bad manifest"), ("install_refused", "bad manifest"))
        self.assertEqual((result["outcome"], result["message"], calls), ("install_refused", "bad manifest", 2))
        self.assertEqual(result["durationMs"], 200)
        self.assertEqual(result["loadErrors"], [])

    def test_disagreeing_attempts_are_an_error(self):
        result, calls = self.run_with(("install_refused", "bad manifest"), ("installed", None))
        self.assertEqual((result["outcome"], calls), ("error", 2))
        self.assertTrue(result["message"].startswith("flaky: attempts disagreed: install_refused then installed"))
        self.assertNotIn("loadErrors", result)
        # Same outcome, other words: also flaky.
        result, _ = self.run_with(("load_warnings", "Path not found: a"), ("load_warnings", "Path not found: b"))
        self.assertTrue(result["message"].startswith("flaky: attempts disagreed: load_warnings then load_warnings"))
        # A transient failure on the second attempt is not confirmation.
        result, _ = self.run_with(("add_refused", "bad name"), ("error", "Could not resolve host"))
        self.assertEqual(result["outcome"], "error")


class EnvTest(unittest.TestCase):
    def test_strips_secrets_and_isolates_config(self):
        import os
        import tempfile

        os.environ["GITHUB_TOKEN"] = "x"
        os.environ["SOME_SECRET"] = "y"
        root = Path(tempfile.mkdtemp())
        env = verify.isolated_env(root)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("SOME_SECRET", env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(root / "cfg"))
        self.assertIn("insteadOf = git@github.com:", Path(env["GIT_CONFIG_GLOBAL"]).read_text())


class PlanTest(unittest.TestCase):
    target = {"pluginId": "1", "slug": "a", "addArg": "x", "installArg": "a@m", "checkKey": "k1"}

    def test_checks_only_what_changed(self):
        last = {
            "linux": {"cliVersion": "2.1.302", "checkKey": "k1", "outcome": "installed"},
            "macos": {"cliVersion": "2.1.301", "checkKey": "k1", "outcome": "installed"},
            "windows": {"cliVersion": "2.1.302", "checkKey": "k0", "outcome": "installed"},
        }
        work = verify.plan([{**self.target, "last": last}], "2.1.302")["work"]
        self.assertEqual({o: len(w) for o, w in work.items()}, {"linux": 0, "macos": 1, "windows": 1})

    def test_retries_errors_and_new_targets(self):
        last = {o: {"cliVersion": "2.1.302", "checkKey": "k1", "outcome": "installed"} for o in verify.OSES}
        last["linux"]["outcome"] = "error"
        work = verify.plan([{**self.target, "last": last}, {**self.target, "pluginId": "2", "slug": "b", "last": {}}], "2.1.302")["work"]
        self.assertEqual([t["slug"] for t in work["linux"]], ["a", "b"])
        self.assertEqual([t["slug"] for t in work["macos"]], ["b"])

    def test_force_and_slugs(self):
        last = {o: {"cliVersion": "2.1.302", "checkKey": "k1", "outcome": "installed"} for o in verify.OSES}
        t = {**self.target, "last": last}
        self.assertEqual(len(verify.plan([t], "2.1.302", force=True)["work"]["linux"]), 1)
        self.assertEqual(len(verify.plan([t], "2.1.302", slugs={"a"})["work"]["windows"]), 1)
        self.assertEqual(len(verify.plan([t], "2.1.302", slugs={"zzz"})["work"]["windows"]), 0)


    def test_keys_mode_ignores_cli_version_and_errors(self):
        last = {
            "linux": {"cliVersion": "2.1.301", "checkKey": "k1", "outcome": "installed"},
            "macos": {"cliVersion": "2.1.302", "checkKey": "k1", "outcome": "error"},
            "windows": {"cliVersion": "2.1.302", "checkKey": "k0", "outcome": "installed"},
        }
        targets = [{**self.target, "last": last}, {**self.target, "pluginId": "2", "slug": "b", "last": {}}]
        work = verify.plan(targets, "2.1.302", mode="keys")["work"]
        self.assertEqual({o: [t["slug"] for t in w] for o, w in work.items()},
                         {"linux": ["b"], "macos": ["b"], "windows": ["a", "b"]})

    def test_max_caps_each_os(self):
        targets = [{**self.target, "pluginId": str(i), "slug": f"s{i}", "last": {}} for i in range(5)]
        work = verify.plan(targets, "2.1.302", mode="keys", max_per_os=2)["work"]
        self.assertEqual([t["slug"] for t in work["linux"]], ["s0", "s1"])
        self.assertEqual(len(work["windows"]), 2)


class ReportTest(unittest.TestCase):
    """A check job's results file is untrusted: only what the plan asked of that OS is reported."""

    plan = {
        "cliVersion": "2.1.302",
        "work": {
            "linux": [{"pluginId": "p1", "checkKey": "k1"}, {"pluginId": "p2", "checkKey": "k2"}],
            "macos": [],
            "windows": [{"pluginId": "p1", "checkKey": "k1"}],
        },
    }

    def result(self, plugin_id, os_name, key, outcome="installed"):
        return {"pluginId": plugin_id, "os": os_name, "checkKey": key, "outcome": outcome}

    def build(self, files):
        with tempfile.TemporaryDirectory() as d:
            for os_name, content in files.items():
                folder = Path(d) / f"results-{os_name}"
                folder.mkdir()
                (folder / f"results-{os_name}.json").write_text(json.dumps(content))
            body, dropped = verify.report(Path(d), self.plan, 1, "https://github.com/o/r/actions/runs/1", "2.1.302")
        return [(r["pluginId"], r["os"]) for r in body["results"]], dropped

    def test_keeps_a_planned_result(self):
        accepted, dropped = self.build({"linux": [self.result("p1", "linux", "k1")]})
        self.assertEqual(accepted, [("p1", "linux")])
        self.assertEqual(dropped, {})

    def test_drops_forged_results(self):
        accepted, dropped = self.build(
            {
                "linux": [
                    self.result("p1", "linux", "k1"),
                    self.result("p9", "linux", "k9"),  # not planned at all
                    self.result("p2", "linux", "stale"),  # planned plugin, other key
                    self.result("p1", "windows", "k1"),  # another OS's result from the Linux job
                    self.result("p2", "linux", "k2"),
                    self.result("p2", "linux", "k2", "install_refused"),  # duplicate: both go
                ],
                "macos": [self.result("p1", "macos", "k1")],  # nothing planned on macOS
                "windows": [self.result("p1", "windows", "k1")],
            }
        )
        self.assertEqual(accepted, [("p1", "linux"), ("p1", "windows")])
        self.assertEqual(dropped, {"not planned": 3, "wrong os": 1, "duplicate": 2})

    def test_drops_non_string_ids_and_keys(self):
        accepted, dropped = self.build(
            {
                "linux": [
                    {"pluginId": ["p1"], "os": "linux", "checkKey": "k1"},
                    {"pluginId": "p1", "os": "linux", "checkKey": {"k": 1}},
                    {"os": "linux", "checkKey": "k1"},
                    self.result("p2", "linux", "k2"),
                ]
            }
        )
        self.assertEqual((accepted, dropped), ([("p2", "linux")], {"bad id or key": 3}))

    def test_lane_and_timestamps_at_the_top_level(self):
        with tempfile.TemporaryDirectory() as d:
            body, _ = verify.report(Path(d), self.plan, 1, "u", "2.1.302", "fast", "2026-10-24T10:00:05Z",
                                    "2026-10-24T11:59:58.123+02:00")
            self.assertEqual((body["lane"], body["runStartedAt"], body["requestedAt"]),
                             ("fast", "2026-10-24T10:00:05Z", "2026-10-24T09:59:58Z"))
            body, _ = verify.report(Path(d), self.plan, 1, "u", "2.1.302", "full", "2026-10-24T10:00:05Z", "")
            self.assertIsNone(body["requestedAt"])
            body, _ = verify.report(Path(d), self.plan, 1, "u", "2.1.302", "fast", "", "not a date")
            self.assertEqual((body["runStartedAt"], body["requestedAt"]), (None, None))

    def test_drops_a_file_that_is_not_a_list(self):
        accepted, dropped = self.build({"linux": {"pluginId": "p1"}})
        self.assertEqual((accepted, dropped), ([], {"not a list": 1}))


if __name__ == "__main__":
    unittest.main()
