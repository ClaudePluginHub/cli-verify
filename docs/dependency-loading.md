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
[run 40175427695](https://github.com/ClaudePluginHub/cli-verify/actions/runs/40175427695), all
from `codearranger/claude-legal`. Its marketplace allowlists `anthropic-agent-skills`
(`allowCrossMarketplaceDependenciesOn`), as the Claude Code docs require for a dependency from
another marketplace.

## The plugins' real setup, 2026-10-27

Claude Code 2.1.311, the verifier's commands and isolation (fresh `HOME` / `CLAUDE_CONFIG_DIR`, no
session), installing `az-court-docs@claude-legal`:

| Marketplaces added first | `plugin install --json` | `plugin list --json` |
| --- | --- | --- |
| `codearranger/claude-legal` only (what the verifier runs) | `ok`, warning: `dependency "document-skills@anthropic-agent-skills" was not installed (no marketplace you have added lists it)` | `dependency-unsatisfied` |
| `anthropics/skills`, then `codearranger/claude-legal` | `ok`, `(+ 2 dependencies: claude-legal-federal-laws, document-skills)` | no errors on any of the three |

So a missing dependency is a fact about the install steps, not a refusal of the plugin: it loads
once the dependency's marketplace is added. The synthetic probe above could not show this, because
a local marketplace named `anthropic-agent-skills` is reserved. The fix belongs in the install
steps: add the dependency's marketplace first, and have the verifier run the same steps.

Since 2026-10-28 it does (`deps`, the steps the plugin page shows). `dependency-unsatisfied` after
those steps is therefore `dependency_missing`: the page's commands leave the dependency out (its
author states no source we can use), and Claude Code won't load the plugin without it. The site
treats it like a load refusal for alerts, and its owner workspace names the README line that fixes
it. `generic-error` beside it is still `load_refused`.
