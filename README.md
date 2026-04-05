# claude-code-agent-for-codex

Expose Claude Code's full agent loop to Codex over MCP.

This project is the reverse bridge of `codex-mcp-server`: instead of letting
Claude Code call Codex, it lets Codex delegate work to `claude -p` as a real
agent. It wraps the agent loop, not Claude Code's low-level tools, so the
handoff is "do this task" rather than "call Read/Edit/Bash one tool at a
time."

## Why This Exists

Codex already knows how to read files, edit code, and run commands. What a
plain tool mirror does not provide is Claude Code's end-to-end agent behavior:
planning, deciding when to inspect more context, resuming prior sessions, and
stopping when the task is done.

That is why this server wraps `claude -p`, not `claude mcp serve`.

```text
Codex -> MCP -> this server -> claude -p -> Claude Code agent loop
```

## Quick Start

### Prerequisites

1. Install Claude Code and confirm `claude -p` works locally.
2. Use a Claude Code version whose `claude --help` includes
   `--output-format stream-json`, `--verbose`, and
   `--include-partial-messages`.
3. Make sure Codex can run local stdio MCP servers.

### Install

```bash
codex mcp add claude-code-agent -- \
  python3 ~/.codex/mcp-servers/claude-code-agent-for-codex/server.py
```

With explicit defaults:

```bash
codex mcp add claude-code-agent \
  --env CC_AGENT_MODEL=opus \
  --env CC_AGENT_DEFAULT_TIER=edit \
  --env CC_AGENT_RUNTIME_PROFILE=simple \
  -- python3 ~/.codex/mcp-servers/claude-code-agent-for-codex/server.py
```

Verify:

```bash
codex mcp list
```

## Execution Model

There are two ways to use the server.

`claude` and `claude_reply` are synchronous and best for short tasks. They use
an internal sync timeout so the caller gets a structured `sync_timeout` error
before the outer MCP client times out.

`claude_start`, `claude_reply_start`, and `claude_status` are asynchronous and
best for real coding tasks. They return a `jobId`, persist job state on disk,
and let the caller poll or wait with bounded progress updates.

The server runs Claude in `stream-json` mode. When the client supplies
`_meta.progressToken`, Claude's intermediate events are translated into
`notifications/progress`. That improves visibility, but it does not turn a
sync call into an unbounded task. If the work might exceed the client's tool
timeout budget, use the async path.

## Tools

| Tool | Purpose |
|------|---------|
| `claude` | Run Claude Code synchronously for short tasks |
| `claude_reply` | Continue a previous session synchronously |
| `claude_start` | Start a background job and return a `jobId` immediately |
| `claude_reply_start` | Start a background follow-up in an existing session |
| `claude_status` | Poll a background job, optionally waiting with progress notifications |
| `claude_list_jobs` | List recent background jobs |
| `tiers` | Show available permission tiers |
| `ping` | Health check |
| `help` | Return `claude --help` output |

## Runtime Profiles

The server supports three runtime profiles.

| Profile | Behavior |
|---------|----------|
| `simple` | Uses normal local auth, but disables inherited slash commands and inherited MCP servers |
| `integrated` | Inherits the full local Claude environment, including plugins, skills, and MCP configuration |
| `isolated` | Runs with `--bare` for a clean, reproducible environment with no inherited local state |

`simple` is the default and the recommended choice for Codex-to-Claude
delegation. It keeps the useful part of "normal Claude" while avoiding the
local Claude ecosystem from leaking into every task.

`integrated` is for cases where you explicitly want local Claude plugins,
skills, or MCP configuration to participate.

`isolated` is the cleanest mode, but it does not use local OAuth or keychain
state. In practice it usually needs explicit auth such as `ANTHROPIC_API_KEY`
or an `apiKeyHelper`.

## Permission Tiers

The server adds a coarse-grained permission layer on top of Claude Code's own
safety system.

| Tier | Permission Mode | Typical Use |
|------|------------------|-------------|
| `readonly` | `plan` | Review, analysis, architecture questions |
| `explore` | `auto` | Investigation, logs, repo inspection |
| `edit` | `auto` | Default coding mode with extra deny rules |
| `full` | `auto` | Maximum Claude flexibility, no extra server deny list |
| `unrestricted` | `bypassPermissions` | Sandbox-only bypass mode |

`edit` is the default because it keeps Claude productive while blocking obvious
destructive patterns such as force-push, hard reset, and recursive delete.

## Parameters

Common execution parameters:

| Parameter | Type | Notes |
|-----------|------|-------|
| `prompt` | string | Required task or follow-up prompt |
| `tier` | enum | `readonly`, `explore`, `edit`, `full`, `unrestricted` |
| `model` | string | Claude model override |
| `effort` | enum | `low`, `medium`, `high`, `max` |
| `systemPrompt` | string | Custom system prompt |
| `permissionMode` | enum | Override the tier's permission mode |
| `allowedTools` | string | Explicit Claude tool auto-approve override |
| `disallowedTools` | string[] | Explicit Claude deny override |
| `workingDirectory` | string | Working directory for the agent |
| `addDirs` | string[] | Extra directories granted to Claude |
| `maxBudgetUsd` | number | Claude Code budget cap |
| `runtimeProfile` | enum | `simple`, `integrated`, or `isolated` |

Sync-only:

| Parameter | Type | Notes |
|-----------|------|-------|
| `syncTimeoutSec` | integer | Override the sync timeout for `claude` and `claude_reply` |

## Async Job Fields

`claude_status` and `claude_list_jobs` expose more than the final Claude
response:

| Field | Meaning |
|-------|---------|
| `phase` | `queued`, `launching`, `starting_claude`, `running`, `parsing_output`, `completed`, `failed` |
| `lastHeartbeatAt` | Last heartbeat timestamp while Claude is still running |
| `lastProgressMessage` | Most recent stream-derived progress summary |
| `childPid` | Active Claude subprocess PID when known |
| `logPath` | Lifecycle log for the job |
| `stdoutPath` / `stderrPath` | Persisted Claude stdout/stderr logs |
| `latestLogLine` | Newest lifecycle log entry |
| `startedCommand` | Shell-escaped Claude command |

These fields make it straightforward to distinguish "still running," "failed to
launch," "timed out," and "Claude itself returned an error."

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `CLAUDE_BIN` | `claude` | Claude Code CLI path |
| `CC_AGENT_MODEL` | Claude default | Default model override |
| `CC_AGENT_EFFORT` | Claude default | Default effort override |
| `CC_AGENT_SYSTEM_PROMPT` | unset | Default system prompt |
| `CC_AGENT_DEFAULT_TIER` | `edit` | Default tier when none is provided |
| `CC_AGENT_TIMEOUT_SEC` | `900` | Hard timeout for the underlying Claude subprocess |
| `CC_AGENT_SYNC_TIMEOUT_SEC` | `90` | Timeout used by sync MCP calls |
| `CC_AGENT_RUNTIME_PROFILE` | `simple` | Default runtime profile |
| `CC_AGENT_HEARTBEAT_SEC` | `5` | Async heartbeat interval |
| `CC_AGENT_STATUS_PROGRESS_SEC` | `2` | Minimum interval between progress notifications in `claude_status` |
| `CC_AGENT_STREAM_TEXT_PROGRESS_SEC` | `1` | Minimum delay before another assistant-text progress summary |
| `CC_AGENT_STREAM_TEXT_PROGRESS_MIN_CHARS` | `48` | Minimum buffered assistant text before early progress emission |
| `CC_AGENT_MAX_BUDGET_USD` | unset | Default budget cap |
| `CC_AGENT_DEBUG_LOG` | `/tmp/claude-code-agent-for-codex-debug.log` | Server debug log |
| `CC_AGENT_STATE_DIR` | `~/.codex/state/claude-code-agent-for-codex/` | Async job state directory |

## Examples

Short readonly review:

```json
{
  "name": "claude",
  "arguments": {
    "prompt": "Review src/parser.ts for edge cases.",
    "tier": "readonly",
    "syncTimeoutSec": 60
  }
}
```

Background refactor:

```json
{
  "name": "claude_start",
  "arguments": {
    "prompt": "Refactor the CSV importer and run tests.",
    "tier": "edit",
    "workingDirectory": "/path/to/repo"
  }
}
```

Background polling:

```json
{
  "name": "claude_status",
  "arguments": {
    "jobId": "abc123",
    "waitSeconds": 30
  }
}
```

Simple delegated review:

```json
{
  "name": "claude_start",
  "arguments": {
    "prompt": "Review the parser refactor for correctness regressions.",
    "tier": "readonly",
    "runtimeProfile": "simple",
    "workingDirectory": "/path/to/repo"
  }
}
```

Clean isolated diagnostic run:

```json
{
  "name": "claude_start",
  "arguments": {
    "prompt": "Reply with exactly: smoke-ok",
    "tier": "readonly",
    "runtimeProfile": "isolated"
  }
}
```

## Troubleshooting

### `sync_timeout`
Use `claude_start` plus `claude_status`. Sync calls intentionally fail before
the MCP client's outer timeout so callers receive a structured error instead of
a silent transport failure.

### Long-Running Task Still Feels Stuck
Progress notifications improve visibility, but they do not remove the sync
timeout boundary. For genuinely long work, use the async path. If the task also
feels too coupled to the local Claude environment, switch from `integrated` to
`simple` before reaching for `isolated`.

### Async Job Stays In `running`
Inspect `lastHeartbeatAt`, `lastProgressMessage`, `logPath`,
`latestLogLine`, and the sibling `*.stdout.log` / `*.stderr.log` files. If
`lastHeartbeatAt` is still advancing, the worker is alive. If
`lastProgressMessage` is changing, Claude is still producing stream events.

### `API Error: Unable to connect to API (ECONNREFUSED)`
This error comes from Claude Code itself, not the MCP transport. Run
`claude -p` directly to confirm local CLI and API health, then retry.

### Isolated Mode Says `Not logged in`
`--bare` does not use implicit local auth. Provide `ANTHROPIC_API_KEY` or an
`apiKeyHelper`, or switch back to `simple` or `integrated`.

### Claude Is Seeing Local Skills Or MCP Servers
You are using `integrated`. Switch to `runtimeProfile: "simple"` to keep normal
auth while disabling inherited slash commands and inherited MCP servers.

## Development

Run the test suite:

```bash
python3 -m unittest -v tests.test_server
```

Quick syntax check:

```bash
python3 -m py_compile server.py tests/test_server.py
```

The project currently has 19 deterministic unit tests covering runtime
profiles, sync and async paths, structured failures, session continuation,
progress streaming, and async job observability.

For design notes and rationale, see [INTRODUCTION.md](./INTRODUCTION.md).

## Related Projects

| Project | Direction |
|---------|-----------|
| `codex-mcp-server` | Claude Code -> Codex |
| `claude mcp serve` | Claude Code tool primitives |
| `claude-code-agent-for-codex` | Codex -> Claude Code agent delegation |
