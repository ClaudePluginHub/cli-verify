#!/usr/bin/env python3
"""Install plugins with the real Claude Code CLI and record what it did.

Three steps, one per workflow job:
  plan    targets.json (from claudepluginhub.com/api/cli-checks/targets) -> plan.json: per OS, the
          targets whose check key (commit, marketplace entry, route) or CLI version changed since
          their stored result (`--mode full`), or only whose check key changed (`--mode keys`,
          the fast lane).
  check   plan.json -> results-<os>.json: install each planned target in a throwaway HOME and
          CLAUDE_CONFIG_DIR, 8 at a time; a result other than installed or error is checked once
          more in a fresh one (check_one). Runs third-party code paths; the job holds no secrets.
  report  plan.json + results-*.json -> report.json, the body POSTed to /api/cli-checks/results:
          only the results the plan asked of each OS (accept_results).

Only `claude plugin marketplace add <x>`, `claude plugin install <x> --json`,
`claude plugin list --json` and `claude plugin marketplace list --json` are ever run (see cli_argv),
plus the `claude plugin …` lines of a plugin's README that readme_args accepts (Linux only, see
check_readme). Never a session, `-p` or `claude mcp` (which starts a plugin's MCP servers). stdin
is never a terminal, so Claude Code never runs a marketplace entry's `command` source (it refuses them non-interactively; claudepluginhub c28).
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
# The CLI clones into a randomly named folder (`temp_github_1792291888483_qr227e`). Without the random
# part, the same refusal reads the same on every OS and run. Paths are otherwise left as they are: a
# backslash may be the defect.
TEMP_NAME_RE = re.compile(r"\b(temp_[a-z]+)_\d{10,}_[a-z0-9]+\b")

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


def needs_check(target: dict, os_name: str, cli_version: str, mode: str = "full") -> bool:
    """`full`: a new check key, a new Claude Code version or a last result of `error`. `keys` (the
    fast lane): a new check key only; versions and errors stay the hourly full run's job."""
    last = (target.get("last") or {}).get(os_name)
    if last is None or last.get("checkKey") != target["checkKey"]:
        return True
    return mode == "full" and (last.get("cliVersion") != cli_version or last.get("outcome") == "error")


def readme_needs_check(readme: dict, cli_version: str, mode: str = "full") -> bool:
    """Same rule as needs_check, for the README's stored result (Linux only)."""
    last = readme.get("last")
    if last is None or last.get("checkKey") != readme["checkKey"]:
        return True
    return mode == "full" and (last.get("cliVersion") != cli_version or last.get("outcome") == "error")


def plan(
    targets: list[dict],
    cli_version: str,
    force: bool = False,
    slugs: set[str] | None = None,
    mode: str = "full",
    max_per_os: int | None = None,
) -> dict:
    work: dict[str, list[dict]] = {os_name: [] for os_name in OSES}
    readmes: list[dict] = []
    for target in targets:
        if slugs and target["slug"] not in slugs:
            continue
        readme = target.get("readme")
        if (
            readme
            and (max_per_os is None or len(readmes) < max_per_os)
            and (force or slugs or readme_needs_check(readme, cli_version, mode))
        ):
            readmes.append(
                {
                    "pluginId": target["pluginId"],
                    "slug": target["slug"],
                    "checkKey": readme["checkKey"],
                    "commands": [{k: c[k] for k in ("line", "kind", "argv")} for c in readme["commands"]],
                }
            )
        for os_name in OSES:
            if max_per_os is not None and len(work[os_name]) >= max_per_os:
                continue
            if force or slugs or needs_check(target, os_name, cli_version, mode):
                work[os_name].append(
                    {k: target[k] for k in ("pluginId", "slug", "addArg", "installArg", "checkKey")}
                )
    return {"cliVersion": cli_version, "work": work, "readme": readmes}


# ---------------------------------------------------------------- check


def cli_argv(claude: str, args: list[str]) -> list[str]:
    """The only CLI invocations this runner makes. Anything else is a bug and raises."""
    shapes = (
        len(args) == 4 and args[:3] == ["plugin", "marketplace", "add"],
        len(args) == 4 and args[:2] == ["plugin", "install"] and args[3] == "--json",
        args == ["plugin", "list", "--json"],
        args == ["plugin", "marketplace", "list", "--json"],
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
    text = TEMP_NAME_RE.sub(r"\1_…", text).strip()
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


def list_record(list_stdout: str, install_arg: str) -> dict | None:
    """The installed plugin's `plugin list --json` record, or None when it doesn't show it."""
    try:
        records = json.loads(list_stdout)
    except json.JSONDecodeError:
        return None
    for record in records if isinstance(records, list) else []:
        if isinstance(record, dict) and record.get("id") == install_arg:
            return record
    return None


def load_errors(list_stdout: str, install_arg: str) -> list[str] | None:
    """The installed plugin's load errors, or None when `plugin list` doesn't show it."""
    record = list_record(list_stdout, install_arg)
    return None if record is None else [str(e) for e in record.get("errors") or []]


# `errorDetails[].type` values that leave the rest of the plugin loaded (claudepluginhub c25, c32).
# `generic-error` is the one where Claude Code loads none of it (`plugin details` says "not found").
PARTIAL_LOAD_TYPES = {"hook-load-failed", "path-not-found", "component-load-failed"}


def load_outcome(list_stdout: str, install_arg: str) -> str:
    """installed, load_refused (loads none of it), load_warnings (loads it, drops a part) or
    load_errors (errors of a type c25 hasn't placed: the site treats it as unknown)."""
    record = list_record(list_stdout, install_arg) or {}
    if not record.get("errors"):
        return "installed"
    details = record.get("errorDetails")
    types = {d.get("type") for d in details if isinstance(d, dict)} if isinstance(details, list) else set()
    if "generic-error" in types:
        return "load_refused"
    return "load_warnings" if types and types <= PARTIAL_LOAD_TYPES else "load_errors"


def installed_ref(root: Path, install_arg: str) -> str | None:
    """The commit Claude Code recorded for the install (installed_plugins.json, claudepluginhub c10).
    None for a source that isn't a git clone, or when the file doesn't say."""
    try:
        data = json.loads((root / "cfg" / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
        sha = data["plugins"][install_arg][0].get("gitCommitSha")
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None
    return sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) else None


def mcp_servers(list_stdout: str, install_arg: str) -> list[str]:
    """Names of the MCP servers Claude Code's loader kept for the plugin."""
    servers = (list_record(list_stdout, install_arg) or {}).get("mcpServers")
    return sorted(servers) if isinstance(servers, dict) else []


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


def run_cli(argv: list[str], env: dict[str, str], root: Path) -> subprocess.CompletedProcess:
    """Runs an already validated argv in its own process group; a timeout ends the whole tree."""
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(
        argv,
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


def check_attempt(claude: str, target: dict, os_name: str) -> dict:
    """One install of the target in its own throwaway HOME and CLAUDE_CONFIG_DIR."""
    root = Path(tempfile.mkdtemp(prefix="cliv-"))
    started = time.monotonic()

    def run(args: list[str]) -> subprocess.CompletedProcess:
        return run_cli(cli_argv(claude, args), env, root)

    def done(outcome: str, message: str | None, **listed: object) -> dict:
        return {
            "pluginId": target["pluginId"],
            "os": os_name,
            "checkKey": target["checkKey"],
            "outcome": outcome,
            "message": normalize(message, root) if message else None,
            "checkedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "durationMs": int((time.monotonic() - started) * 1000),
            **listed,
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
        # What the site compares with the page's load warnings and MCP servers (Linux).
        extra = {
            "loadErrors": [normalize(e, root) for e in errors],
            "mcpServers": mcp_servers(listed.stdout, target["installArg"]),
            "installedRef": installed_ref(root, target["installArg"]),
        }
        return done(load_outcome(listed.stdout, target["installArg"]), "\n".join(errors) or None, **extra)
    except subprocess.TimeoutExpired:
        return done("error", f"timed out after {TIMEOUT_S} s")
    except Exception as error:  # noqa: BLE001 — one bad target must not take the OS's batch with it
        return done("error", f"runner: {type(error).__name__}: {error}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# Outcomes final after one attempt: a clean install, and `error` (already retried next run).
NO_RETRY = {"installed", "error"}


def check_one(claude: str, target: dict, os_name: str) -> dict:
    """A refusal or load problem is what owners get alerted on, so it must repeat: the whole check
    runs once more in a fresh HOME and CLAUDE_CONFIG_DIR. The same outcome stands (with the first
    attempt's message: wording that differs, e.g. a path normalization misses, must not turn a
    deterministic refusal into an error); a different one is reported as `error`, which the hourly
    run retries."""
    first = check_attempt(claude, target, os_name)
    if first["outcome"] in NO_RETRY:
        return first
    second = check_attempt(claude, target, os_name)
    duration = first["durationMs"] + second["durationMs"]
    if second["outcome"] == first["outcome"]:
        return {**first, "durationMs": duration}
    message = (
        f"flaky: attempts disagreed: {first['outcome']} then {second['outcome']}\n"
        f"1: {first['message'] or ''}\n2: {second['message'] or ''}"
    )
    return {
        **{k: first[k] for k in ("pluginId", "os", "checkKey", "checkedAt")},
        "outcome": "error",
        "message": message if len(message) <= MESSAGE_MAX else message[: MESSAGE_MAX - 1] + "…",
        "durationMs": duration,
    }


def check(plan_data: dict, os_name: str, concurrency: int, out: Path) -> list[dict]:
    """Installs every planned target; `out` is rewritten as results land, so a job that hits its
    time limit still uploads what it finished (the rest is planned again next run)."""
    claude = shutil.which("claude")
    if not claude:
        raise SystemExit("claude is not on PATH")
    work = plan_data["work"][os_name]
    results: list[dict] = []
    out.write_text("[]")
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(check_one, claude, t, os_name) for t in work]
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            results.append(future.result())
            if i % 10 == 0 or i == len(futures):
                out.write_text(json.dumps(results))
                print(f"{os_name}: {i}/{len(futures)}", file=sys.stderr, flush=True)
    counts: dict[str, int] = {}
    for r in results:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    print(f"{os_name}: {len(results)} checked {json.dumps(counts, sort_keys=True)}", file=sys.stderr)
    return results


# ---------------------------------------------------------------- README commands (Linux)

# The `claude plugin …` lines a README tells people to run, as the site extracted them
# (claudepluginhub readme-commands.ts). The site's list is a proposal: readme_args re-validates every
# argv, and nothing from the README is ever run through a shell.

README_OUTCOMES = frozenset({"ok", "add_refused", "install_refused", "unknown_command", "not_checked", "error"})
README_REFUSALS = frozenset({"add_refused", "install_refused", "unknown_command"})
README_TOKEN_MAX = 300
README_ARGV_MAX = 40
# `claude plugin --help` on 2.1.310: any other word is what the CLI rejects.
README_SUBCOMMANDS = frozenset(
    "configure details disable enable eval help init new install i list marketplace prune autoremove"
    " tag test uninstall remove update validate".split()
)
README_WORD_RE = re.compile(r"[A-Za-z][\w-]*")
README_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# A source this machine can't have or reach: a path, `~`, a drive, a file or ssh URL.
README_LOCAL_RE = re.compile(r"^(\.{1,2}(/|$)|[/~]|[A-Za-z]:[\\/]|file:|ssh:|git@)", re.IGNORECASE)
README_CONFIG_RE = re.compile(r"[\w.-]+=[\w./:@-]*")
README_SCOPES = frozenset({"user", "project", "local"})
UNKNOWN_COMMAND_RE = re.compile(r"unknown command", re.IGNORECASE)
# Needs a credential this runner doesn't have: the README may be right for the people it addresses.
AUTH_RE = re.compile(
    r"could not read username|terminal prompts disabled|permission denied \(publickey\)"
    r"|host key verification failed|authentication failed",
    re.IGNORECASE,
)


def parse_flags(rest: list[str], spec: dict[str, str]) -> tuple[list[str], dict[str, list[str]]]:
    """Positionals and `--flag value…` pairs; ValueError for an unknown flag, one without a value,
    a repeated one or a bad scope. `spec` maps flag -> "one" | "many" | "scope" | "config"."""
    positional: list[str] = []
    flags: dict[str, list[str]] = {}
    i = 0
    while i < len(rest):
        token = rest[i]
        i += 1
        if not token.startswith("-"):
            positional.append(token)
            continue
        kind = spec.get(token)
        if kind is None or (token in flags and kind != "config"):
            raise ValueError(f"flag {token!r}")
        values: list[str] = []
        while i < len(rest) and not rest[i].startswith("-") and (kind == "many" or not values):
            values.append(rest[i])
            i += 1
        if not values or (kind == "scope" and values[0] not in README_SCOPES):
            raise ValueError(f"value of {token!r}")
        if kind == "config" and not README_CONFIG_RE.fullmatch(values[0]):
            raise ValueError(f"value of {token!r}")
        if kind in ("one", "scope", "config") and len(values) != 1:
            raise ValueError(f"value of {token!r}")
        flags[token] = [*flags.get(token, []), *values] if kind == "config" else values
    return positional, flags


def flatten_flags(flags: dict[str, list[str]]) -> list[str]:
    """Back to argv; a repeatable flag (--config) is repeated per value, not given several."""
    return [t for f, v in flags.items() for t in ([x for val in v for x in (f, val)] if f == "--config" else (f, *v))]


def readme_args(argv: object) -> tuple[str, list[str]]:
    """(kind, args after `claude`) for a README command, or ValueError. The only shapes: a marketplace
    add, an install (the runner appends --json), or `plugin <word>` for a word the CLI doesn't know
    (run bare: its arguments are never passed on)."""
    if not isinstance(argv, list) or not 3 <= len(argv) <= README_ARGV_MAX:
        raise ValueError("argv shape")
    if not all(isinstance(t, str) and 0 < len(t) <= README_TOKEN_MAX and not README_CONTROL_RE.search(t) for t in argv):
        raise ValueError("argv token")
    if argv[0] != "claude" or argv[1] not in ("plugin", "plugins"):
        raise ValueError("not a claude plugin command")
    sub = argv[2]
    if sub not in README_SUBCOMMANDS:
        if not README_WORD_RE.fullmatch(sub):
            raise ValueError("subcommand")
        return "unknown_subcommand", ["plugin", sub]
    if sub == "marketplace":
        if argv[3:4] != ["add"]:
            raise ValueError("marketplace subcommand")
        positional, flags = parse_flags(argv[4:], {"--scope": "scope", "--sparse": "many"})
        if len(positional) != 1 or README_LOCAL_RE.match(positional[0]):
            raise ValueError("marketplace source")
        return "add", ["plugin", "marketplace", "add", positional[0], *flatten_flags(flags)]
    if sub in ("install", "i"):
        spec = {"--marketplace": "one", "--scope": "scope", "-s": "scope", "--config": "config"}
        positional, flags = parse_flags(argv[3:], spec)
        if len(positional) != 1 or any(README_LOCAL_RE.match(v) for v in flags.get("--marketplace", [])):
            raise ValueError("install target")
        return "install", ["plugin", "install", positional[0], *flatten_flags(flags)]
    raise ValueError("subcommand")


def install_names_unadded_marketplace(args: list[str], names: set[str]) -> bool:
    """`install p@m` with no --marketplace, where no add of the README produced marketplace `m`."""
    target = args[2]
    return "--marketplace" not in args and "@" in target and target.rsplit("@", 1)[1] not in names


def marketplace_names(listed_stdout: str) -> set[str]:
    try:
        records = json.loads(listed_stdout)
    except json.JSONDecodeError:
        return set()
    return {r["name"] for r in records if isinstance(r, dict) and isinstance(r.get("name"), str)} if isinstance(records, list) else set()


def readme_attempt(claude: str, adds: list[list[str]], command: dict) -> tuple[str, str | None]:
    """One README line in a fresh HOME, after the README's own earlier `marketplace add` lines
    (`adds`, validated args). (outcome, message); not_checked says nothing about the README."""
    root = Path(tempfile.mkdtemp(prefix="cliv-"))
    try:
        env = isolated_env(root)

        def run(args: list[str]) -> tuple[int, str]:
            done = run_cli([claude, *args], env, root)
            return done.returncode, (done.stdout + done.stderr)

        kind, args = readme_args(command["argv"])
        if kind != command["kind"]:
            return "not_checked", "runner: the line is not the kind the site planned"
        if kind == "unknown_subcommand":
            code, output = run(args)
            if code == 0:
                return "ok", normalize(output, root) or None
            return ("unknown_command" if UNKNOWN_COMMAND_RE.search(output) else "error"), normalize(output, root)
        if kind == "add":
            code, output = run(args)
            if code == 0:
                return "ok", None
            return refusal_or_note(output, "add_refused", root)
        for earlier in adds:
            code, output = run(earlier)
            if code != 0:
                outcome, message = refusal_or_note(output, "add_refused", root)
                return ("error" if outcome == "error" else "not_checked"), f"an earlier marketplace add of the README failed: {message}"
        added = run(["plugin", "marketplace", "list", "--json"]) if adds else (0, "[]")
        code, output = run([*args, "--json"])
        result = parse_install(output)
        outcome = classify_install(result, output)
        message = (result or {}).get("message") or output
        if outcome == "installed":
            return "ok", None
        if outcome == "command_source_refused":
            return "not_checked", "the entry installs by running a command on this machine"
        if outcome == "install_refused":
            if install_names_unadded_marketplace(args, marketplace_names(added[1])):
                return "not_checked", "the line names a marketplace the README never adds"
            return refusal_or_note(message, "install_refused", root)
        return "error", normalize(message, root)
    except subprocess.TimeoutExpired:
        return "error", f"timed out after {TIMEOUT_S} s"
    except ValueError as error:
        return "not_checked", f"runner refused the command: {error}"
    except Exception as error:  # noqa: BLE001 — one bad line must not take the batch with it
        return "error", f"runner: {type(error).__name__}: {error}"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def refusal_or_note(output: str, refusal: str, root: Path) -> tuple[str, str]:
    """A failure of the README's own command: the refusal, unless it needs the network to settle
    (error) or a credential the runner lacks (not_checked)."""
    message = normalize(output, root)
    if TRANSIENT_RE.search(output):
        return "error", message
    if AUTH_RE.search(output):
        return "not_checked", f"needs a credential this check does not have: {message}"
    return refusal, message


def readme_check_one(claude: str, item: dict, command: dict, adds: list[list[str]]) -> dict:
    """A refusal is what owners get alerted on, so it must repeat in a fresh HOME (as check_one)."""
    outcome, message = readme_attempt(claude, adds, command)
    if outcome in README_REFUSALS:
        again, other = readme_attempt(claude, adds, command)
        if again != outcome:
            message = f"flaky: attempts disagreed: {outcome} then {again}\n1: {message or ''}\n2: {other or ''}"
            outcome = "error"
    if message and len(message) > MESSAGE_MAX:
        message = message[: MESSAGE_MAX - 1] + "…"
    return {
        "pluginId": item["pluginId"],
        "os": "linux",
        "checkKey": item["checkKey"],
        "line": command["line"],
        "outcome": outcome,
        "message": message,
        "checkedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def check_readme(plan_data: dict, concurrency: int, out: Path) -> list[dict]:
    """Runs every planned README line (Linux job only); `out` is rewritten as results land."""
    claude = shutil.which("claude")
    if not claude:
        raise SystemExit("claude is not on PATH")
    jobs: list[tuple[dict, dict, list[list[str]]]] = []
    for item in plan_data.get("readme", []):
        adds: list[list[str]] = []
        for command in item["commands"]:
            jobs.append((item, command, list(adds)))
            try:
                kind, args = readme_args(command["argv"])
            except ValueError:
                continue
            if kind == "add" and args not in adds:
                adds.append(args)
    results: list[dict] = []
    out.write_text("[]")
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(readme_check_one, claude, *job) for job in jobs]
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            results.append(future.result())
            if i % 10 == 0 or i == len(futures):
                out.write_text(json.dumps(results))
    counts: dict[str, int] = {}
    for r in results:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    print(f"readme: {len(results)} lines checked {json.dumps(counts, sort_keys=True)}", file=sys.stderr)
    return results


def accept_readme_results(plan_data: dict, results: object) -> tuple[list[dict], dict[str, int]]:
    """The README results the Linux job may report: only (pluginId, checkKey, line) the plan asked
    for, one per plugin and line (duplicates all dropped), rebuilt from known fields."""
    dropped: dict[str, int] = {}

    def drop(reason: str, n: int = 1) -> None:
        dropped[reason] = dropped.get(reason, 0) + n

    if not isinstance(results, list):
        return [], {"not a list": 1}
    planned = {(i["pluginId"], i["checkKey"], c["line"]) for i in plan_data.get("readme", []) for c in i["commands"]}
    kept: dict[tuple, dict] = {}
    duplicated: set[tuple] = set()
    for r in results:
        if not isinstance(r, dict):
            drop("not an object")
            continue
        key = (r.get("pluginId"), r.get("checkKey"), r.get("line"))
        if not all(isinstance(part, str) for part in key):
            drop("bad id, key or line")
            continue
        checked_at = iso_or_none(r.get("checkedAt")) if isinstance(r.get("checkedAt"), str) else None
        message = r.get("message")
        if key not in planned:
            drop("not planned")
        elif r.get("outcome") not in README_OUTCOMES or not (message is None or isinstance(message, str)) or checked_at is None:
            drop("malformed")
        elif key in kept or key in duplicated:
            duplicated.add(key)
            drop("duplicate", 1 + (kept.pop(key, None) is not None))
        else:
            kept[key] = {
                "pluginId": key[0],
                "os": "linux",
                "checkKey": key[1],
                "line": key[2],
                "outcome": r["outcome"],
                "message": message[:MESSAGE_MAX] if message else None,
                "checkedAt": checked_at,
            }
    return list(kept.values()), dropped


# ---------------------------------------------------------------- report


def accept_results(plan_data: dict, results_by_os: dict[str, list]) -> tuple[list[dict], dict[str, int]]:
    """The results a check job may report: each OS's file only for that OS, only (pluginId,
    checkKey) pairs the plan job planned for that OS, at most one per plugin. Anything else is
    dropped and counted by reason.

    A check job runs third-party code paths and the check keys are public, so its results file is
    untrusted: without this, a job could claim any plugin's result on any OS. A job can still lie
    about the plugins planned for its own OS (see README, Isolation)."""
    accepted: list[dict] = []
    dropped: dict[str, int] = {}

    def drop(reason: str, n: int = 1) -> None:
        dropped[reason] = dropped.get(reason, 0) + n

    for os_name, results in results_by_os.items():
        planned = {(t["pluginId"], t["checkKey"]) for t in plan_data["work"].get(os_name, [])}
        if not isinstance(results, list):
            drop("not a list")
            continue
        kept: dict[str, dict] = {}
        duplicated: set[str] = set()
        for r in results:
            if not isinstance(r, dict):
                drop("not an object")
            elif not isinstance(r.get("pluginId"), str) or not isinstance(r.get("checkKey"), str):
                drop("bad id or key")
            elif r.get("os") != os_name:
                drop("wrong os")
            elif (r.get("pluginId"), r.get("checkKey")) not in planned:
                drop("not planned")
            elif r["pluginId"] in kept or r["pluginId"] in duplicated:
                # Which one is genuine is unknowable: drop them all (re-checked next run).
                duplicated.add(r["pluginId"])
                drop("duplicate", 1 + (kept.pop(r["pluginId"], None) is not None))
            else:
                kept[r["pluginId"]] = r
        accepted.extend(kept.values())
    return accepted, dropped


def iso_or_none(value: str | None) -> str | None:
    """An ISO 8601 timestamp as UTC `…Z`, or None for empty or unreadable input (a dispatch input is
    free text)."""
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):  # OverflowError: year 1 or 9999 shifted out of range
        return None


def report(
    results_dir: Path,
    plan_data: dict,
    run_id: int,
    run_url: str,
    cli_version: str,
    lane: str = "full",
    run_started_at: str | None = None,
    requested_at: str | None = None,
) -> tuple[dict, dict[str, int]]:
    """`results_dir` holds one folder per check artifact (`results-<os>/results-<os>.json`); the
    OS comes from the artifact name the workflow gave it, never from the file's content."""
    results_by_os: dict[str, list] = {}
    for os_name in OSES:
        path = results_dir / f"results-{os_name}" / f"results-{os_name}.json"
        if path.is_file():
            try:
                results_by_os[os_name] = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                results_by_os[os_name] = None  # counted as "not a list"
    results, dropped = accept_results(plan_data, results_by_os)
    readme_path = results_dir / "results-linux" / "readme-linux.json"
    readme_results: list[dict] = []
    if readme_path.is_file():
        try:
            readme_results, readme_dropped = accept_readme_results(plan_data, json.loads(readme_path.read_text(encoding="utf-8")))
        except ValueError:
            readme_dropped = {"not a list": 1}
        dropped.update({f"readme: {k}": v for k, v in readme_dropped.items()})
    body = {
        "runId": run_id,
        "runUrl": run_url,
        "cliVersion": cli_version,
        "lane": lane,
        "runStartedAt": iso_or_none(run_started_at),
        "requestedAt": iso_or_none(requested_at),
        "results": results,
        "readmeResults": readme_results,
    }
    return body, dropped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--targets", type=Path, required=True)
    p.add_argument("--cli-version", required=True)
    p.add_argument("--force", action="store_true")
    p.add_argument("--slugs", default="")
    p.add_argument("--mode", choices=("full", "keys"), default="full")
    p.add_argument("--max", type=int, default=None, help="at most this many targets per OS")
    p.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("check")
    c.add_argument("--plan", type=Path, required=True)
    c.add_argument("--os", choices=OSES, required=True)
    c.add_argument("--concurrency", type=int, default=8)
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--readme-out", type=Path, default=None, help="Linux: where the README line results go")
    r = sub.add_parser("report")
    r.add_argument("--results-dir", type=Path, required=True)
    r.add_argument("--plan", type=Path, required=True)
    r.add_argument("--run-id", type=int, required=True)
    r.add_argument("--run-url", required=True)
    r.add_argument("--cli-version", required=True)
    r.add_argument("--lane", choices=("fast", "full"), default="full")
    r.add_argument("--run-started-at", default="")
    r.add_argument("--requested-at", default="")
    r.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if args.step == "plan":
        targets = json.loads(args.targets.read_text())["targets"]
        slugs = {s.strip() for s in args.slugs.split(",") if s.strip()} or None
        data = plan(targets, args.cli_version, args.force, slugs, args.mode, args.max)
        args.out.write_text(json.dumps(data))
        oses = [o for o in OSES if data["work"][o] or (o == "linux" and data["readme"])]
        print(json.dumps({"os": oses, "counts": {o: len(data["work"][o]) for o in OSES}, "readme": len(data["readme"])}))
    elif args.step == "check":
        plan_data = json.loads(args.plan.read_text())
        check(plan_data, args.os, args.concurrency, args.out)
        if args.os == "linux" and args.readme_out:
            check_readme(plan_data, args.concurrency, args.readme_out)
    else:
        body, dropped = report(
            args.results_dir,
            json.loads(args.plan.read_text()),
            args.run_id,
            args.run_url,
            args.cli_version,
            args.lane,
            args.run_started_at,
            args.requested_at,
        )
        args.out.write_text(json.dumps(body))
        print(f"Dropped results the plan did not ask for: {json.dumps(dropped, sort_keys=True)}")
        if dropped:
            print(f"::warning::dropped results the plan did not ask for: {json.dumps(dropped, sort_keys=True)}")


if __name__ == "__main__":
    main()
