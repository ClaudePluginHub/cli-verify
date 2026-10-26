import json
import subprocess
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

    def test_missing_dependency_blocks_loading_even_with_other_errors(self):
        # Claude Code 2.1.310 session probe: docs/dependency-loading.md.
        for other_types in ([], ["hook-load-failed"], ["something-new"], ["generic-error"]):
            with self.subTest(other_types=other_types):
                record = {
                    "id": "p@m",
                    "errors": ['Dependency "document-skills@anthropic-agent-skills" is not installed'],
                    "errorDetails": [{"type": t} for t in ["dependency-unsatisfied", *other_types]],
                }
                self.assertEqual(verify.load_outcome(json.dumps([record]), "p@m"), "load_refused")

    def test_unknown_errors_stay_unclassified_without_a_known_blocker(self):
        for types in (["something-new"], ["hook-load-failed", "something-new"]):
            with self.subTest(types=types):
                record = {"id": "p@m", "errors": ["unplaced error"],
                          "errorDetails": [{"type": t} for t in types]}
                self.assertEqual(verify.load_outcome(json.dumps([record]), "p@m"), "load_errors")

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
        # Same outcome, other words (a path normalization missed): the first attempt's result stands.
        result, _ = self.run_with(("load_warnings", "Path not found: /tmp/a"), ("load_warnings", "Path not found: /tmp/b"))
        self.assertEqual((result["outcome"], result["message"]), ("load_warnings", "Path not found: /tmp/a"))

    def test_disagreeing_attempts_are_an_error(self):
        result, calls = self.run_with(("install_refused", "bad manifest"), ("installed", None))
        self.assertEqual((result["outcome"], calls), ("error", 2))
        self.assertTrue(result["message"].startswith("flaky: attempts disagreed: install_refused then installed"))
        self.assertNotIn("loadErrors", result)
        self.assertIn("1: bad manifest", result["message"])
        # A transient failure on the second attempt is not confirmation.
        result, _ = self.run_with(("add_refused", "bad name"), ("error", "Could not resolve host"))
        self.assertEqual(result["outcome"], "error")

    def test_dependency_refusal_must_repeat_in_a_fresh_attempt(self):
        message = 'Dependency "document-skills@anthropic-agent-skills" is not installed'
        result, calls = self.run_with(("load_refused", message), ("load_refused", message))
        self.assertEqual((result["outcome"], result["message"], calls), ("load_refused", message, 2))
        result, calls = self.run_with(("load_refused", message), ("installed", None))
        self.assertEqual((result["outcome"], calls), ("error", 2))


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

    def test_full_lane_retries_unresolved_results_but_keys_lane_does_not(self):
        for outcome in ("error", "load_errors", "installed", "add_refused", "install_refused", "load_refused", "load_warnings"):
            with self.subTest(outcome=outcome):
                last = {o: {"cliVersion": "2.1.310", "checkKey": "k1", "outcome": outcome} for o in verify.OSES}
                target = {**self.target, "last": last}
                full = verify.plan([target], "2.1.310")["work"]
                keys = verify.plan([target], "2.1.310", mode="keys")["work"]
                expected = 1 if outcome in {"error", "load_errors"} else 0
                self.assertEqual({o: len(w) for o, w in full.items()}, dict.fromkeys(verify.OSES, expected))
                self.assertEqual({o: len(w) for o, w in keys.items()}, dict.fromkeys(verify.OSES, 0))

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
            # Shifting year 1 or 9999 to UTC overflows: unreadable, not a crash.
            for edge in ("0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"):
                body, _ = verify.report(Path(d), self.plan, 1, "u", "2.1.302", "fast", "", edge)
                self.assertIsNone(body["requestedAt"], edge)

    def test_cli_takes_a_requested_at_that_looks_like_a_flag(self):
        # The workflow passes --requested-at=<input>: a dispatch input of "-x" must not end the report.
        with tempfile.TemporaryDirectory() as d:
            plan_path, out = Path(d) / "plan.json", Path(d) / "report.json"
            plan_path.write_text(json.dumps(self.plan))
            subprocess.run(
                [sys.executable, str(Path(verify.__file__)), "report", "--results-dir", d, "--plan", str(plan_path),
                 "--run-id", "1", "--run-url", "u", "--cli-version", "2.1.302", "--lane", "fast",
                 "--run-started-at=2026-10-24T10:00:05Z", "--requested-at=-x", "--out", str(out)],
                check=True, capture_output=True,
            )
            body = json.loads(out.read_text())
            self.assertEqual((body["requestedAt"], body["runStartedAt"]), (None, "2026-10-24T10:00:05Z"))

    def test_drops_a_file_that_is_not_a_list(self):
        accepted, dropped = self.build({"linux": {"pluginId": "p1"}})
        self.assertEqual((accepted, dropped), ([], {"not a list": 1}))


def cmd(line, kind=None):
    """A README command the way the site plans it (claudepluginhub readme-commands.ts)."""
    argv = line.split()
    kind = kind or ("add" if argv[2:4] == ["marketplace", "add"] else "install" if argv[2] in ("install", "i") else "unknown_subcommand")
    return {"line": line, "kind": kind, "argv": argv}


def done(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


UNKNOWN_ADD = "error: unknown command 'add'\n"
NOT_FOUND = (
    '{"command":"install","outcome":"failed","plugin":"p@m","scope":"user","message":"Plugin \\"p\\" not found in '
    'marketplace \\"m\\".","failureCode":"not_found"}'
)


class ReadmeArgsTest(unittest.TestCase):
    def test_accepts_the_three_shapes(self):
        self.assertEqual(
            verify.readme_args(cmd("claude plugin marketplace add acme/tools --scope project --sparse a b")["argv"]),
            ("add", ["plugin", "marketplace", "add", "acme/tools", "--scope", "project", "--sparse", "a", "b"]),
        )
        self.assertEqual(
            verify.readme_args(cmd("claude plugins i p@m -s user --config k=v")["argv"]),
            ("install", ["plugin", "install", "p@m", "-s", "user", "--config", "k=v"]),
        )
        self.assertEqual(
            verify.readme_args(cmd("claude plugin install p --config a=b --config c=d")["argv"]),
            ("install", ["plugin", "install", "p", "--config", "a=b", "--config", "c=d"]),
        )
        self.assertEqual(verify.readme_args(cmd("claude plugin add /path/to/x --force")["argv"]), ("unknown_subcommand", ["plugin", "add"]))

    def test_refuses_everything_else(self):
        for line in (
            "claude plugin install p@m -y",
            "claude plugin install p@m --yes",
            "claude plugin install p@m --accept-command",
            "claude plugin install p@m --registry r",
            "claude plugin install p@m --scope bogus",
            "claude plugin install p@m --scope",
            "claude plugin install p@m --scope user --scope user",
            "claude plugin install p@m --config a=b;c",
            "claude plugin install p@m --marketplace ./local",
            "claude plugin install a b",
            "claude plugin marketplace add",
            "claude plugin marketplace add ./x",
            "claude plugin marketplace add ../x",
            "claude plugin marketplace add /x",
            "claude plugin marketplace add ~/x",
            "claude plugin marketplace add C:\\x",
            "claude plugin marketplace add file:///etc",
            "claude plugin marketplace add git@gitlab.com:a/b.git",
            "claude plugin marketplace add a/b --claudeai",
            "claude plugin marketplace add a/b --json",
            "claude plugin marketplace remove a",
            "claude plugin marketplace update",
            "claude plugin uninstall p@m",
            "claude plugin list",
            "claude plugin",
            "claude plugin -p hi",
            "claude plugin ad$d x",
            "claude mcp list",
            "claude -p hi",
            "bash plugin add x",
        ):
            with self.assertRaises(ValueError, msg=line):
                verify.readme_args(line.split())
        for argv in (None, "claude plugin add x", ["claude", "plugin", 3], ["claude", "plugin", "add\nx"], ["claude", "plugin", "x" * 301]):
            with self.assertRaises(ValueError, msg=argv):
                verify.readme_args(argv)

    def test_the_site_cannot_widen_the_cli_allowlist(self):
        verify.cli_argv("claude", ["plugin", "marketplace", "list", "--json"])
        with self.assertRaises(ValueError):
            verify.cli_argv("claude", ["plugin", "marketplace", "remove", "x"])


class ReadmePlanTest(unittest.TestCase):
    def target(self, readme):
        return {"pluginId": "p1", "slug": "a", "addArg": "x", "installArg": "a@m", "checkKey": "k1", "last": {}, "readme": readme}

    def readme(self, last=None):
        return {"checkKey": "r1", "sourceRef": "abc", "commands": [cmd("claude plugin add /x")], "last": last}

    def test_plans_a_new_key_or_cli_version_or_error(self):
        ok = {"cliVersion": "2.1.310", "checkKey": "r1", "outcome": "ok"}
        planned = lambda last, mode="full": len(verify.plan([self.target(self.readme(last))], "2.1.310", mode=mode)["readme"])
        self.assertEqual(planned(None), 1)
        self.assertEqual(planned({**ok, "checkKey": "r0"}), 1)
        self.assertEqual(planned(ok), 0)
        self.assertEqual(planned({**ok, "cliVersion": "2.1.309"}), 1)
        self.assertEqual(planned({**ok, "outcome": "error"}), 1)
        self.assertEqual(planned({**ok, "cliVersion": "2.1.309"}, "keys"), 0)
        self.assertEqual(planned({**ok, "outcome": "error"}, "keys"), 0)

    def test_plan_carries_only_what_the_runner_needs_and_is_capped(self):
        targets = [{**self.target(self.readme()), "pluginId": str(i), "slug": f"s{i}"} for i in range(3)]
        planned = verify.plan(targets, "2.1.310", max_per_os=2)["readme"]
        self.assertEqual([p["slug"] for p in planned], ["s0", "s1"])
        self.assertEqual(set(planned[0]), {"pluginId", "slug", "checkKey", "commands"})
        self.assertEqual(set(planned[0]["commands"][0]), {"line", "kind", "argv"})

    def test_a_target_without_a_readme_plans_nothing(self):
        self.assertEqual(verify.plan([{**self.target(None)}], "2.1.310")["readme"], [])
        self.assertEqual(verify.plan([{k: v for k, v in self.target(None).items() if k != "readme"}], "2.1.310")["readme"], [])


class ReadmeAttemptTest(unittest.TestCase):
    def attempt(self, command, adds=(), replies=None):
        """Runs readme_attempt with the CLI faked: `replies` maps the args' 2nd-3rd words to a result."""
        calls = []

        def fake(argv, env, root):
            calls.append(argv[1:])
            for needle, reply in (replies or {}).items():
                if needle in " ".join(argv[1:]):
                    return reply
            return done()

        with mock.patch.object(verify, "run_cli", fake):
            return verify.readme_attempt("claude", [list(a) for a in adds], command), calls

    def test_unknown_subcommand_is_run_bare(self):
        result, calls = self.attempt(cmd("claude plugin add /path/to/x"), replies={"plugin add": done(1, "", UNKNOWN_ADD)})
        self.assertEqual(result[0], "unknown_command")
        self.assertEqual(calls, [["plugin", "add"]])

    def test_a_word_the_cli_knows_now_is_ok_and_other_failures_are_errors(self):
        self.assertEqual(self.attempt(cmd("claude plugin add x"))[0][0], "ok")
        self.assertEqual(self.attempt(cmd("claude plugin add x"), replies={"plugin add": done(1, "", "boom")})[0][0], "error")

    def test_add_line(self):
        self.assertEqual(self.attempt(cmd("claude plugin marketplace add a/b"))[0], ("ok", None))
        refused = {"marketplace add": done(1, "", "Invalid marketplace schema")}
        self.assertEqual(self.attempt(cmd("claude plugin marketplace add a/b"), replies=refused)[0][0], "add_refused")
        net = {"marketplace add": done(1, "", "Could not resolve host: github.com")}
        self.assertEqual(self.attempt(cmd("claude plugin marketplace add a/b"), replies=net)[0][0], "error")
        auth = {"marketplace add": done(1, "", "fatal: could not read Username for 'https://github.com'")}
        self.assertEqual(self.attempt(cmd("claude plugin marketplace add a/b"), replies=auth)[0][0], "not_checked")

    def test_install_runs_the_earlier_adds_first_then_install_json(self):
        add = ["plugin", "marketplace", "add", "a/b"]
        result, calls = self.attempt(cmd("claude plugin install p@m"), adds=[add], replies={"install": done(0, OK)})
        self.assertEqual(result, ("ok", None))
        self.assertEqual(calls, [add, ["plugin", "marketplace", "list", "--json"], ["plugin", "install", "p@m", "--json"]])

    def test_install_refused_only_when_the_readme_added_that_marketplace(self):
        add = ["plugin", "marketplace", "add", "a/b"]
        listed = done(0, '[{"name":"m","source":"github"}]')
        replies = {"marketplace list": listed, "install": done(1, NOT_FOUND)}
        self.assertEqual(self.attempt(cmd("claude plugin install p@m"), adds=[add], replies=replies)[0][0], "install_refused")
        other = {"marketplace list": done(0, '[{"name":"zzz"}]'), "install": done(1, NOT_FOUND)}
        result = self.attempt(cmd("claude plugin install p@m"), adds=[add], replies=other)[0]
        self.assertEqual(result[0], "not_checked")

    def test_a_failed_earlier_add_says_nothing_about_the_install(self):
        add = ["plugin", "marketplace", "add", "a/b"]
        result, calls = self.attempt(cmd("claude plugin install p@m"), adds=[add], replies={"marketplace add": done(1, "", "Invalid marketplace schema")})
        self.assertEqual(result[0], "not_checked")
        self.assertEqual(calls, [add])
        net = {"marketplace add": done(1, "", "Could not resolve host")}
        self.assertEqual(self.attempt(cmd("claude plugin install p@m"), adds=[add], replies=net)[0][0], "error")

    def test_install_outcomes(self):
        add = ["plugin", "marketplace", "add", "a/b"]
        listed = {"marketplace list": done(0, '[{"name":"m"}]')}
        for out, expected in ((COMMAND_SOURCE, "not_checked"), (CLONE_NETWORK, "error"), (REFUSED, "install_refused")):
            result = self.attempt(cmd("claude plugin install p@m"), adds=[add], replies={**listed, "install": done(1, out)})[0]
            self.assertEqual(result[0], expected, out)

    def test_a_line_the_runner_will_not_run_is_not_checked_and_runs_nothing(self):
        bad = {"line": "claude plugin install p@m -y", "kind": "install", "argv": ["claude", "plugin", "install", "p@m", "-y"]}
        result, calls = self.attempt(bad)
        self.assertEqual(result[0], "not_checked")
        self.assertEqual(calls, [])
        # The site's kind must match what the runner sees.
        result, calls = self.attempt({**cmd("claude plugin install p@m"), "kind": "add"})
        self.assertEqual((result[0], calls), ("not_checked", []))

    def test_timeout_is_an_error(self):
        def slow(argv, env, root):
            raise subprocess.TimeoutExpired(argv, 1)

        with mock.patch.object(verify, "run_cli", slow):
            self.assertEqual(verify.readme_attempt("claude", [], cmd("claude plugin marketplace add a/b"))[0], "error")


class ReadmeRetryTest(unittest.TestCase):
    item = {"pluginId": "p1", "checkKey": "r1"}

    def run_with(self, outcomes):
        seq = iter(outcomes)
        with mock.patch.object(verify, "readme_attempt", lambda *a: next(seq)):
            return verify.readme_check_one("claude", self.item, cmd("claude plugin add x"), [])

    def test_a_refusal_must_repeat(self):
        self.assertEqual(self.run_with([("unknown_command", "m1"), ("unknown_command", "m2")])["outcome"], "unknown_command")
        self.assertEqual(self.run_with([("unknown_command", "m1"), ("unknown_command", "m2")])["message"], "m1")

    def test_disagreement_is_an_error(self):
        r = self.run_with([("install_refused", "m1"), ("ok", None)])
        self.assertEqual(r["outcome"], "error")
        self.assertTrue(r["message"].startswith("flaky: attempts disagreed: install_refused then ok"))

    def test_ok_error_and_not_checked_run_once(self):
        for outcome in ("ok", "error", "not_checked"):
            self.assertEqual(self.run_with([(outcome, None)])["outcome"], outcome)


class AcceptReadmeTest(unittest.TestCase):
    plan_data = {"readme": [{"pluginId": "p1", "slug": "a", "checkKey": "r1", "commands": [cmd("claude plugin add x"), cmd("claude plugin install p@m")]}]}

    def result(self, **over):
        return {"pluginId": "p1", "os": "linux", "checkKey": "r1", "line": "claude plugin add x", "outcome": "unknown_command",
                "message": "error: unknown command 'add'", "checkedAt": "2026-10-25T10:00:00Z", **over}

    def test_keeps_a_planned_line_and_rebuilds_it(self):
        accepted, dropped = verify.accept_readme_results(self.plan_data, [self.result(extra="x", os="windows")])
        self.assertEqual(dropped, {})
        self.assertEqual(accepted, [{**self.result(), "os": "linux"}])

    def test_drops_what_the_plan_did_not_ask_for(self):
        accepted, dropped = verify.accept_readme_results(
            self.plan_data,
            [
                self.result(pluginId="p2"),
                self.result(checkKey="old"),
                self.result(line="claude plugin add y"),
                self.result(outcome="installed"),
                self.result(checkedAt="yesterday"),
                self.result(message=["x"]),
                self.result(pluginId=["p1"]),
                "junk",
            ],
        )
        self.assertEqual(accepted, [])
        self.assertEqual(dropped, {"not planned": 3, "malformed": 3, "bad id, key or line": 1, "not an object": 1})

    def test_a_repeated_line_is_dropped_whole(self):
        other = self.result(line="claude plugin install p@m", outcome="ok", message=None)
        accepted, dropped = verify.accept_readme_results(self.plan_data, [self.result(), other, self.result(outcome="ok")])
        self.assertEqual([r["line"] for r in accepted], ["claude plugin install p@m"])
        self.assertEqual(dropped, {"duplicate": 2})

    def test_not_a_list(self):
        self.assertEqual(verify.accept_readme_results(self.plan_data, {"a": 1}), ([], {"not a list": 1}))

    def test_report_carries_the_linux_file_only(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            for os_name in ("linux", "macos"):
                (Path(d) / f"results-{os_name}").mkdir()
            (Path(d) / "results-linux" / "readme-linux.json").write_text(json.dumps([self.result()]))
            (Path(d) / "results-macos" / "readme-macos.json").write_text(json.dumps([self.result()]))
            body, dropped = verify.report(Path(d), {"work": {}, **self.plan_data}, 1, "u", "2.1.310")
            self.assertEqual([r["line"] for r in body["readmeResults"]], ["claude plugin add x"])
            self.assertEqual(dropped, {})
            # A file that is not valid JSON is counted, not fatal.
            (Path(d) / "results-linux" / "readme-linux.json").write_text("{")
            body, dropped = verify.report(Path(d), {"work": {}, **self.plan_data}, 1, "u", "2.1.310")
            self.assertEqual((body["readmeResults"], dropped), ([], {"readme: not a list": 1}))


if __name__ == "__main__":
    unittest.main()
