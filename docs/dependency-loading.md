# Missing dependencies prevent session loading

On 2026-10-25, Claude Code **2.1.310** was tested with synthetic local marketplaces and fresh
`HOME` / `CLAUDE_CONFIG_DIR` directories. Each plugin contained a skill, command and agent.
Session initialization was captured with fake API credentials pointed at a loopback server;
no real model request or third-party plugin session ran.

| Case | `plugin install --json` | Session initialization |
| --- | --- | --- |
| No dependency | `ok` | Plugin, skill, command and agent registered |
| Missing dependency, string declaration | `ok` | Plugin and all three components absent |
| Missing dependency, production object declaration | `ok` | Plugin and all three components absent |
| Synthetic dependency installed afterward | `ok` | Plugin and all three components registered again |

The production declaration was `{ "name": "document-skills", "marketplace": "anthropic-agent-skills" }`.
`plugin list --json` returned `errors` explaining the missing dependency and an `errorDetails`
entry with `"type": "dependency-unsatisfied"`. Session debug output said:

```text
Plugin not available for MCP: exact-object-missing@dependency-probe2 - error type: dependency-unsatisfied
Total plugin commands loaded: 0
Total plugin skills loaded: 0 (0 duplicate/user-owned entries skipped)
Total plugin agents loaded: 0
```

`plugin details` could still inventory component files while the session disabled the plugin.
Neither installation success nor that inventory proves loading. A local marketplace named
`anthropic-agent-skills` is reserved, so the recovery control used `synthetic-deps` and the
otherwise equivalent dependency declaration instead.

The same missing-dependency message affected 15 plugins on each OS in
[run 40175427695](https://github.com/ClaudePluginHub/cli-verify/actions/runs/40175427695).
`dependency-unsatisfied` therefore maps to `load_refused`, including when other error types
accompany it. Unknown types without a known whole-plugin blocker remain `load_errors` and
are retried by the full lane. Refusals still require two matching attempts in separate fresh configs.

After merging a classifier fix, select the affected slugs in `verify.yml` to obtain current
results on all three OSes through normal ingestion. Full-lane reports refresh coverage gaps.
Closing an unknown gap is not a plugin recovery: missing dependencies remain real refusals,
and any Linux disagreement with the site's rules remains visible.
