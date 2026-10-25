# cli-verify

Installs [ClaudePluginHub](https://www.claudepluginhub.com)'s owned and most-installed plugins with
the real Claude Code CLI on Linux, macOS and Windows, and reports what it did. Owners see the result
in their plugin workspace, and an owner alert about a refused plugin goes out only once Claude Code
itself has confirmed the refusal.

- **What runs:** `claude plugin marketplace add <route>`, `claude plugin install <plugin> --json`
  and `claude plugin list --json`, each plugin in its own throwaway `HOME` and `CLAUDE_CONFIG_DIR`.
  No Claude session, no `-p`, no `claude mcp`. stdin is never a terminal, so Claude Code never runs a
  marketplace entry's `command` source.
- **What it reports:** per plugin and OS, the outcome (`installed`, `load_warnings` when Claude Code
  loads the plugin but drops a part, `load_refused` when it loads none of it, `load_errors` for an
  error type not yet placed, or a refusal at add or install), the CLI's message, and `installedRef`,
  the commit Claude Code recorded in `installed_plugins.json` (null when it records none). The
  load split follows `plugin list --json` `errorDetails[].type` (claudepluginhub cli-oracle c25:
  `generic-error` means nothing loads).
- **README commands (Linux only):** a plugin's README may tell people to run `claude plugin …`
  lines; the site extracts the checkable ones into each target's `readme` (claudepluginhub
  `readme-commands.ts`) and the plan carries them as `readme`. Each line runs in its own fresh
  `HOME`: a `marketplace add` line alone, an `install` line after the README's earlier `marketplace
  add` lines (then `install … --json`), and a `claude plugin <word>` the CLI has no subcommand for
  bare (its arguments are never passed on). The runner re-validates every argv (`readme_args`: only
  those three shapes, no `-y`/`--yes`/`--accept-command`/`--registry`/`--claudeai`, no local path,
  `file:` or ssh source, never a shell) and reports `not_checked` for a line it refuses. Outcomes:
  `ok`, `add_refused`, `install_refused`, `unknown_command` (each repeated once in a fresh `HOME`,
  like the install check), `error` (network, timeout, or attempts that disagree), `not_checked`
  (an earlier add failed, the install names a marketplace the README never adds, the entry runs a
  command, or the clone needs a credential). Results go in `readmeResults` of the report, one per
  plugin and line, only for lines the plan asked for.
- **When:** two lanes run the same jobs (`.github/workflows/run.yml`), each in its own concurrency
  group so neither waits on the other:
  - **Full** (`verify.yml`, group `cli-verify`): hourly, and by hand with `slugs` or `force`. A
    plugin is checked again when its commit, its marketplace entry, its install route or the Claude
    Code version changes, or its last result was `error`.
  - **Fast** (`verify-fast.yml`, group `cli-verify-fast`): dispatched by claudepluginhub.com, input
    `requested_at` (ISO 8601, optional). Checks only plugins whose check key differs from their
    stored result on that OS (or that have none), at most 50 per OS (`verify.py plan --mode keys
    --max 50`); Claude Code versions and earlier errors are left to the full lane. The dispatch
    carries no plugin list: GitHub keeps one pending run per group and replaces it, so the plan is
    recomputed from the live targets.
- **Retry:** a result other than `installed` or `error` is checked once more, from scratch, in a fresh
  `HOME` and `CLAUDE_CONFIG_DIR`. The same outcome stands, with the first attempt's message; a
  different outcome is reported as `error` with a message starting `flaky: attempts disagreed: `, so
  no owner alert rests on one attempt. Network failures (`TRANSIENT_RE`) are `error` as before.
- **Report body:** `{runId, runUrl, cliVersion, lane, runStartedAt, requestedAt, results, readmeResults}`. `lane`
  is `"full"` or `"fast"`, `runStartedAt` is when the plan job started (`…Z`), `requestedAt` is the
  fast lane's `requested_at` normalized to UTC `…Z`, or `null` (full lane, or missing/unreadable).
- **Isolation:** the check jobs hold no secrets, no write permissions and no caches. Only the report
  job, which runs no plugin code, holds the secret that posts results to claudepluginhub.com. It
  reports a result only if the plan job planned that plugin, check key and OS, from the artifact of
  that OS's job, once; anything else is dropped and counted in the job summary. This is defense in
  depth, not isolation: the plugins one OS job installs share its VM and user, so code run while
  installing one could rewrite the results of the others on that OS (or, with the runner's token,
  replace another job's artifact). Full isolation would take one ephemeral job per plugin.

Tests: `python3 -m unittest tests/test_verify.py`.
