# Windows checkout failures, 2026-10-26

Claude Code 2.1.310 and Git 2.56.0.windows.2 were tested on GitHub's `windows-latest` runner.
Every target retained the normal fresh HOME/config, stripped environment, 120-second command
timeout and process-tree cleanup. Diagnostic runs held no ingest secret and posted no results
to production. Only bounded command output, CLI debug logs, Git Trace2 and descendant process
snapshots were uploaded, before the temporary plugin directories were deleted.

## Long paths

[Run 40383552776](https://github.com/ClaudePluginHub/cli-verify/actions/runs/40383552776)
reproduced `marketplace add sickn33/agentic-awesome-skills` failing during Git checkout. The
captured trace contained 141 `Filename too long` errors per attempt; network fetching succeeded.
The CLI retried the clone, then misleadingly described it as an HTTPS authentication failure.

In the controlled [run 40384719250](https://github.com/ClaudePluginHub/cli-verify/actions/runs/40384719250),
the default isolated config again failed both fresh attempts. Adding only `core.longpaths=true`
to that config restored add, install and list: 39.1 s, 14.2 s and 0.6 s respectively. The plugin
reported `installed`. The unrelated `alirezarezvani/claude-skills` representative also installed
in the serial baseline and long-path control.

The verifier therefore enables long paths only in its isolated Windows Git config. It does not
change the host's Git settings, credentials, symlink behavior or the advertised install route.

## Concurrent checkouts

The same run tested eight distinct affected targets with long paths enabled: both `sickn33`
targets installed, while all six `alirezarezvani` targets timed out in `marketplace add`.
Their Git traces had completed fetching and entered `Updating files`; descendant snapshots
showed the matching `git clone` still running when the deadline expired. The serial control
completed that same add in 79.7 s (96.4 s with long paths enabled).

This identifies concurrent working-tree checkout as the blocking stage, rather than install,
list or an unresolved network request. With the same eight targets and long-path setting,
[run 40389138642](https://github.com/ClaudePluginHub/cli-verify/actions/runs/40389138642) reduced
only the worker count to two. All eight plugins reported `installed`: 24 add/install/list
commands completed, none timed out, and the longest command took 89.4 seconds.

Both workflow lanes therefore use two workers on Windows and eight on Linux/macOS. The
120-second timeout remains unchanged. Timeouts remain `error`; their reports now retain
the command and a bounded output tail so future failures do not discard this evidence.

After merging, use a full-lane run with the still-affected current slugs and normal ingestion.
Read the actual outcomes and current keys, not just the Actions job conclusion. Only usable
results can close error gaps; unrelated plugin refusals and rules disagreements stay visible.
