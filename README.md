# claude-code-agent-for-codex

Expose Claude Code's full autonomous agent loop to Codex over MCP.

This server is the reverse bridge of `codex-mcp-server`: instead of letting
Claude Code call Codex, it lets Codex delegate work to `claude -p` as a real
agent. The goal is not to mirror Claude Code's low-level tools one by one, but
to hand off multi-step reasoning, exploration, editing, and verification as a
single MCP capability.

## Status

- Current version: `v2.1.2`
- Local validation: 16 deterministic unit tests covering sync and async paths,
  structured failures, session resume, `claude_reply_start`,
  `claude_list_jobs`, progress notifications, parse fallback, zero-budget
  handling, and invalid configuration guards.
- Live smoke on 2026-04-05: a sync review request returned a structured
  `sync_timeout`, and a later async review surfaced a structured
  `api_connection_refused` error with heartbeat and log metadata intact.

## Use Cases

- Delegate deep codebase analysis from Codex to Claude Code.
- Run longer background jobs without getting trapped by MCP client timeouts.
- Keep a session alive across follow-up turns with `threadId`.
- Choose between safer constrained modes and higher-capability modes with
  explicit runtime and permission controls.

## Quick Start

### Prerequisites

1. Install Claude Code and confirm `claude -p` works on the machine.
2. Make sure Codex can run local stdio MCP servers.

### Install in Codex

```bash
codex mcp add claude-code-agent -- \
  python3 ~/.codex/mcp-servers/claude-code-agent-for-codex/server.py
```

Optional defaults:

```bash
codex mcp add claude-code-agent \
  --env CC_AGENT_MODEL=opus \
  --env CC_AGENT_DEFAULT_TIER=edit \
  --env CC_AGENT_RUNTIME_PROFILE=integrated \
  -- python3 ~/.codex/mcp-servers/claude-code-agent-for-codex/server.py
```

Verify registration:

```bash
codex mcp list
```

## How It Works

```text
Codex (client) -> MCP -> this server -> claude -p -> Claude Code agent loop
```

The server supports two execution styles:

- Sync: `claude`, `claude_reply`
  Best for short tasks. These calls use a shorter sync timeout by default so
  they return a structured `sync_timeout` error before the MCP client itself
  times out. When the client provides `_meta.progressToken` (Codex does), the
  server emits `notifications/progress` during the run.
- Async: `claude_start`, `claude_reply_start`, `claude_status`
  Best for long-running work. Async jobs persist state on disk, emit heartbeat
  timestamps, expose per-job logs, and `claude_status` can emit
  `notifications/progress` during bounded waits.

Important boundary:

- Progress notifications improve visibility.
- They do not turn a single synchronous MCP tool call into an unbounded task.
- If the work might outlive the client's tool timeout budget, the correct path
  is still `claude_start` or `claude_reply_start`, followed by `claude_status`.

## Tools

| Tool | Purpose |
|------|---------|
| `claude` | Run Claude Code synchronously for short tasks |
| `claude_reply` | Continue a prior Claude Code session synchronously |
| `claude_start` | Start a background job and return a `jobId` immediately |
| `claude_reply_start` | Background follow-up for an existing session |
| `claude_status` | Poll a background job, optionally waiting with progress notifications |
| `claude_list_jobs` | List recent background jobs |
| `tiers` | Inspect available permission tiers |
| `ping` | Health check |
| `help` | Return `claude --help` output |

## Permission Tiers

The server exposes a higher-level tier model on top of Claude Code's own
permission system.

| Tier | Permission Mode | Typical Use |
|------|------------------|-------------|
| `readonly` | `plan` | Review, analysis, architecture questions |
| `explore` | `auto` | Investigation, logs, repo inspection |
| `edit` | `auto` | Default coding mode with extra deny rules |
| `full` | `auto` | Maximum Claude flexibility, no extra server deny list |
| `unrestricted` | `bypassPermissions` | Sandbox environments only |

`edit` remains the default because it keeps Claude Code productive while still
blocking obviously destructive shell patterns such as force-push, hard reset,
and recursive delete.

## Runtime Profiles

The server supports two runtime profiles:

- `integrated` (default)
  Inherits the local Claude Code environment, including local auth state,
  plugins, skills, and MCP configuration. This is the most compatible mode.
- `isolated`
  Runs Claude Code with `--bare` for a cleaner and more predictable execution
  environment.

Important auth note:

- `isolated` does not rely on local OAuth or keychain state.
- In practice, it usually requires explicit auth such as `ANTHROPIC_API_KEY`
  or an `apiKeyHelper` setting.
- If you see `Not logged in · Please run /login` in isolated mode, that is
  expected unless explicit auth is configured for `--bare`.

## Parameters

Common parameters accepted by the execution tools:

| Parameter | Type | Notes |
|-----------|------|-------|
| `prompt` | string | Required task or follow-up prompt |
| `tier` | enum | `readonly`, `explore`, `edit`, `full`, `unrestricted` |
| `model` | string | Claude model override |
| `effort` | enum | `low`, `medium`, `high`, `max` |
| `systemPrompt` | string | Custom system prompt |
| `permissionMode` | enum | Override the tier's permission mode |
| `allowedTools` | string | Explicit Claude tool allow override |
| `disallowedTools` | string[] | Explicit Claude deny override |
| `workingDirectory` | string | Working directory for the agent |
| `addDirs` | string[] | Extra directories granted to Claude |
| `maxBudgetUsd` | number | Claude Code budget cap |
| `runtimeProfile` | enum | `integrated` or `isolated` |

Sync-only:

| Parameter | Type | Notes |
|-----------|------|-------|
| `syncTimeoutSec` | integer | Override the sync timeout for `claude` and `claude_reply` |

## Async Job Fields

`claude_status` and `claude_list_jobs` return extra job-state fields beyond the
final Claude response:

- `phase`: `queued`, `launching`, `starting_claude`, `running`,
  `parsing_output`, `completed`, `failed`
- `lastHeartbeatAt`: last heartbeat timestamp while Claude is still running
- `childPid`: active Claude subprocess PID when known
- `logPath`: lifecycle log for the job
- `stdoutPath` / `stderrPath`: persisted Claude stdout/stderr log files
- `latestLogLine`: newest lifecycle log entry
- `startedCommand`: shell-escaped Claude command

This makes it easier to distinguish "still running", "timed out", "failed to
launch", and "Claude itself returned an error".

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
| `CC_AGENT_RUNTIME_PROFILE` | `integrated` | Default runtime profile |
| `CC_AGENT_HEARTBEAT_SEC` | `5` | Async heartbeat interval |
| `CC_AGENT_STATUS_PROGRESS_SEC` | `2` | Minimum interval between progress notifications while waiting in `claude_status` |
| `CC_AGENT_MAX_BUDGET_USD` | unset | Default budget cap |
| `CC_AGENT_DEBUG_LOG` | `/tmp/claude-code-agent-for-codex-debug.log` | Server debug log |
| `CC_AGENT_STATE_DIR` | `~/.codex/state/claude-code-agent-for-codex/` | Async job state directory |

## Examples

Short readonly analysis:

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

Clean diagnostic run:

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

### Sync call returns `sync_timeout`

Use `claude_start` plus `claude_status`. Sync calls intentionally fail earlier
than the MCP client's outer timeout so callers get a structured error.

### Long-running task still times out

If the client keeps using `claude` or `claude_reply`, progress notifications
will improve visibility but will not remove the synchronous timeout boundary.
For genuinely long work, use `claude_start` or `claude_reply_start`, then poll
with `claude_status`.

### Async job stays in `running`

Inspect:

- `lastHeartbeatAt`
- `logPath`
- `latestLogLine`
- the sibling `*.stdout.log` and `*.stderr.log` files in the job state directory

If `lastHeartbeatAt` is still moving, the server is healthy and Claude is still
working.

### `API Error: Unable to connect to API (ECONNREFUSED)`

This is coming from Claude Code itself, not from the MCP transport layer. Run
`claude -p` directly to confirm local Claude CLI and API health, then retry.

### Isolated mode says `Not logged in`

That usually means `--bare` does not have explicit auth configured. Provide
`ANTHROPIC_API_KEY` or an `apiKeyHelper`, or switch back to `integrated`.

## Development

Run the stub-based test suite:

```bash
python3 -m unittest -v tests.test_server
```

Quick syntax check:

```bash
python3 -m py_compile server.py tests/test_server.py
```

The current test suite covers:

- sync timeout handling before MCP transport timeout
- invalid tier and invalid sync-timeout validation
- `claude_reply` and `claude_reply_start` session continuation
- async phase transitions, heartbeat updates, and persisted error payloads
- progress notification emission for sync calls and async status waits
- `claude_list_jobs` output
- `latestLogLine` / stdout-stderr path exposure in async status
- raw stdout fallback for non-JSON success output
- structured handling for stderr-only nonzero exits
- isolated-mode auth guidance
- `maxBudgetUsd=0` parsing and command generation
- rejection of partial stdout/stderr log path configuration

For deeper design notes and rationale, see [INTRODUCTION.md](./INTRODUCTION.md).

## Related Projects

- `codex-mcp-server`: Claude Code -> Codex
- `claude mcp serve`: low-level Claude Code tool exposure
- this project: Codex -> Claude Code agent delegation
