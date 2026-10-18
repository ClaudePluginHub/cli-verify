import json
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
