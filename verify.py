#!/usr/bin/env python3
"""Install plugins with the real Claude Code CLI and record what it did.

Three steps, one per workflow job:
  plan    targets.json (from claudepluginhub.com/api/cli-checks/targets) -> plan.json: per OS, the
          targets whose commit, route or CLI version changed since their stored result.
  check   plan.json -> results-<os>.json: install each planned target in a throwaway HOME and
          CLAUDE_CONFIG_DIR, 8 at a time. Runs third-party code paths; the job holds no secrets.
  report  results-*.json -> report.json, the body POSTed to /api/cli-checks/results.

Only `claude plugin marketplace add <x>`, `claude plugin install <x> --json` and
`claude plugin list --json` are ever run (see cli_argv). Never a session, `-p` or `claude mcp`
(which starts a plugin's MCP servers). stdin is never a terminal, so Claude Code never runs a
marketplace entry's `command` source (it refuses them non-interactively; claudepluginhub c28).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

OSES = ("linux", "macos", "windows")
TIMEOUT_S = 120
MESSAGE_MAX = 2000

# A failure that says nothing about the plugin: retried next run, never reported as a refusal.
# Phrases only: the text echoes repo and plugin names, so a bare word like "tls" would turn a
# refusal of `acme/mtls-proxy` into an hourly retry.
TRANSIENT_RE = re.compile(
    r"could not resolve host|connection (?:timed out|reset|refused)|etimedout|econnreset|eai_again"
    r"|timed out|operation too slow|ssl (?:connect|certificate)|tls handshake|gnutls|schannel"
    r"|http (?:429|5\d\d)|status code (?:429|5\d\d)|rate limit|early eof"
    r"|rpc failed|unexpected disconnect|temporary failure|network is unreachable",
    re.IGNORECASE,
)
# Environment never passed to the CLI, whatever the job happens to have.
SECRET_ENV_RE = re.compile(r"TOKEN|SECRET|PASSWORD|CREDENTIAL|_KEY$|^ACTIONS_|^GITHUB_", re.IGNORECASE)


# ---------------------------------------------------------------- plan


def needs_check(target: dict, os_name: str, cli_version: str) -> bool:
    last = (target.get("last") or {}).get(os_name)
    return (
        last is None
        or last.get("checkKey") != target["checkKey"]
        or last.get("cliVersion") != cli_version
        or last.get("outcome") == "error"
    )


def plan(targets: list[dict], cli_version: str, force: bool = False, slugs: set[str] | None = None) -> dict:
    work: dict[str, list[dict]] = {os_name: [] for os_name in OSES}
    for target in targets:
        if slugs and target["slug"] not in slugs:
            continue
        for os_name in OSES:
            if force or slugs or needs_check(target, os_name, cli_version):
                work[os_name].append(
                    {k: target[k] for k in ("pluginId", "slug", "addArg", "installArg", "checkKey")}
                )
    return {"cliVersion": cli_version, "work": work}


# ---------------------------------------------------------------- check


def cli_argv(claude: str, args: list[str]) -> list[str]:
    """The only CLI invocations this runner makes. Anything else is a bug and raises."""
    shapes = (
        len(args) == 4 and args[:3] == ["plugin", "marketplace", "add"],
        len(args) == 4 and args[:2] == ["plugin", "install"] and args[3] == "--json",
        args == ["plugin", "list", "--json"],
    )
    positional = args[3] if args[:3] == ["plugin", "marketplace", "add"] else args[2] if args[:2] == ["plugin", "install"] else ""
    if not any(shapes) or positional.startswith("-"):
        raise ValueError(f"refusing to run claude {args!r}")
    return [claude, *args]


def isolated_env(root: Path) -> dict[str, str]:
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    gitconfig = home / ".gitconfig"
    # Runners have no SSH key; public repos clone anonymously over HTTPS.
    gitconfig.write_text(
        '[url "https://github.com/"]\n\tinsteadOf = git@github.com:\n\tinsteadOf = ssh://git@github.com/\n'
    )
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV_RE.search(k)}
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        CLAUDE_CONFIG_DIR=str(root / "cfg"),
        GIT_CONFIG_GLOBAL=str(gitconfig),
        GIT_TERMINAL_PROMPT="0",
        DISABLE_AUTOUPDATER="1",
        CI="1",
    )
    return env


def normalize(text: str, root: Path) -> str:
    """Claude Code's words with our throwaway paths shown as a user's own (`~/.claude/…`)."""
    for base, shown in ((root / "cfg", "~/.claude"), (root / "home", "~"), (root, "~")):
        # Longest first: macOS /var/… is a suffix of its resolved /private/var/….
        forms = {str(base), str(base).replace("\\", "/"), str(base.resolve())}
        for form in sorted(forms, key=len, reverse=True):
            text = text.replace(form, shown)
    text = text.strip()
    return text if len(text) <= MESSAGE_MAX else text[: MESSAGE_MAX - 1] + "…"


def parse_install(stdout: str) -> dict | None:
    """The `--json` result line of `plugin install` (human lines may surround it)."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and '"command"' in line:
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("command") == "install":
                return data
    return None


def classify_add_failure(output: str) -> str:
    return "error" if TRANSIENT_RE.search(output) else "add_refused"


def classify_install(result: dict | None, output: str) -> str:
    if result is None:
        return "error"
    if result.get("outcome") == "ok":
        return "installed"
    if result.get("outcome") != "failed":
        return "error"
    if result.get("failureCode") == "command_source_refused":
        return "command_source_refused"
    if result.get("failureCode") == "error_network" or TRANSIENT_RE.search(result.get("message") or output):
        return "error"
    return "install_refused"


def load_errors(list_stdout: str, install_arg: str) -> list[str] | None:
    """The installed plugin's load errors, or None when `plugin list` doesn't show it."""
    try:
        records = json.loads(list_stdout)
    except json.JSONDecodeError:
        return None
    for record in records if isinstance(records, list) else []:
        if isinstance(record, dict) and record.get("id") == install_arg:
            return [str(e) for e in record.get("errors") or []]
    return None


def kill_tree(proc: subprocess.Popen) -> None:
    """A timeout must also end the CLI's git children, or they hold the pipes and the disk."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()
    try:
        proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def check_one(claude: str, target: dict, os_name: str) -> dict:
    root = Path(tempfile.mkdtemp(prefix="cliv-"))
    started = time.monotonic()

    def run(args: list[str]) -> subprocess.CompletedProcess:
        group = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        )
        proc = subprocess.Popen(
            cli_argv(claude, args),
            env=env,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **group,
        )
        try:
            out, err = proc.communicate(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            raise
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)

    def done(outcome: str, message: str | None) -> dict:
        return {
            "pluginId": target["pluginId"],
            "os": os_name,
            "checkKey": target["checkKey"],
            "outcome": outcome,
            "message": normalize(message, root) if message else None,
            "checkedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "durationMs": int((time.monotonic() - started) * 1000),
        }

    try:
        env = isolated_env(root)
        added = run(["plugin", "marketplace", "add", target["addArg"]])
        if added.returncode != 0:
            output = added.stdout + added.stderr
            return done(classify_add_failure(output), output)
        installed = run(["plugin", "install", target["installArg"], "--json"])
        result = parse_install(installed.stdout)
        outcome = classify_install(result, installed.stdout + installed.stderr)
        if outcome != "installed":
            return done(outcome, (result or {}).get("message") or installed.stdout + installed.stderr)
        listed = run(["plugin", "list", "--json"])
        errors = load_errors(listed.stdout, target["installArg"]) if listed.returncode == 0 else None
        if errors is None:
            return done("error", "plugin list did not show the installed plugin\n" + listed.stdout + listed.stderr)
        return done("load_errors", "\n".join(errors)) if errors else done("installed", None)
    except subprocess.TimeoutExpired:
        return done("error", f"timed out after {TIMEOUT_S} s")
    except Exception as error:  # noqa: BLE001 — one bad target must not take the OS's batch with it
        return done("error", f"runner: {type(error).__name__}: {error}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check(plan_data: dict, os_name: str, concurrency: int) -> list[dict]:
    claude = shutil.which("claude")
    if not claude:
        raise SystemExit("claude is not on PATH")
    work = plan_data["work"][os_name]
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(lambda t: check_one(claude, t, os_name), work))
    counts: dict[str, int] = {}
    for r in results:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    print(f"{os_name}: {len(results)} checked {json.dumps(counts, sort_keys=True)}", file=sys.stderr)
    return results


# ---------------------------------------------------------------- report


def report(result_files: list[Path], run_id: int, run_url: str, cli_version: str) -> dict:
    results: list[dict] = []
    for path in result_files:
        results.extend(json.loads(path.read_text()))
    return {"runId": run_id, "runUrl": run_url, "cliVersion": cli_version, "results": results}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--targets", type=Path, required=True)
    p.add_argument("--cli-version", required=True)
    p.add_argument("--force", action="store_true")
    p.add_argument("--slugs", default="")
    p.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("check")
    c.add_argument("--plan", type=Path, required=True)
    c.add_argument("--os", choices=OSES, required=True)
    c.add_argument("--concurrency", type=int, default=8)
    c.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("report")
    r.add_argument("results", type=Path, nargs="*")
    r.add_argument("--run-id", type=int, required=True)
    r.add_argument("--run-url", required=True)
    r.add_argument("--cli-version", required=True)
    r.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if args.step == "plan":
        targets = json.loads(args.targets.read_text())["targets"]
        slugs = {s.strip() for s in args.slugs.split(",") if s.strip()} or None
        data = plan(targets, args.cli_version, args.force, slugs)
        args.out.write_text(json.dumps(data))
        oses = [o for o in OSES if data["work"][o]]
        print(json.dumps({"os": oses, "counts": {o: len(data["work"][o]) for o in OSES}}))
    elif args.step == "check":
        args.out.write_text(json.dumps(check(json.loads(args.plan.read_text()), args.os, args.concurrency)))
    else:
        args.out.write_text(json.dumps(report(args.results, args.run_id, args.run_url, args.cli_version)))


if __name__ == "__main__":
    main()
