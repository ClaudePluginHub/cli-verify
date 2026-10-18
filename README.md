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
- **When:** hourly (`.github/workflows/verify.yml`). A plugin is checked again only when its
  commit, its marketplace entry, its install route or the Claude Code version changes.
- **Isolation:** the check jobs hold no secrets, no write permissions and no caches. Only the report
  job, which runs no plugin code, holds the secret that posts results to claudepluginhub.com.

Tests: `python3 -m unittest tests/test_verify.py`.
