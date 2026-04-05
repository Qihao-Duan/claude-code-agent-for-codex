# Introduction

This document explains why `claude-code-agent-for-codex` exists, what it wraps,
and which design tradeoffs shaped the current implementation.

## The Core Idea

There are two very different ways to expose Claude Code over MCP:

1. Expose Claude Code's individual tools.
2. Expose Claude Code's full autonomous agent loop.

This project chooses the second approach.

Codex already knows how to read files, run commands, edit code, and inspect a
workspace. What it does not get from a generic tool mirror is Claude Code's
end-to-end agent behavior: planning, choosing when to inspect more context,
resuming a session, and deciding when a task is finished.

So the server wraps `claude -p`, not `claude mcp serve`.

```text
Codex -> MCP -> this server -> claude -p -> Claude Code agent loop
```

## Why Not Just Use `claude mcp serve`?

`claude mcp serve` is useful when another client wants direct access to Claude
Code primitives such as Read, Edit, Grep, or Bash.

That is not the need here.

Codex already has equivalent primitives. The missing value is delegated agent
judgment. This server is intentionally higher-level:

- give Claude a task
- optionally continue the same session later
- for long-running work, move the task into a durable async job
- return enough state to debug failures without losing the agent context

## The Two Main Axes

### 1. Sync vs Async

The server exposes both sync and async entry points because MCP clients usually
have an outer timeout of roughly two minutes, while real coding tasks often
take much longer.

- Sync calls are convenient for short analysis and small follow-ups.
- Async calls are the default safe path for anything non-trivial.

The implementation intentionally uses a shorter sync timeout than the MCP
client's outer timeout, so the caller gets a structured `sync_timeout` error
instead of a generic transport failure.

### 2. Integrated vs Isolated Runtime

The server now exposes two runtime profiles:

- `integrated`
  Inherits the local Claude Code environment, including local auth state,
  plugins, skills, and MCP configuration.
- `isolated`
  Runs Claude Code with `--bare` for a cleaner and more reproducible runtime.

This tradeoff matters in practice.

`integrated` is usually the most compatible option because it can reuse the
machine's existing Claude login state. It is therefore the default.

`isolated` is better when you want a cleaner execution path, fewer inherited
side effects, and more predictable behavior. But `--bare` does not read local
OAuth or keychain state, so it commonly requires explicit auth such as
`ANTHROPIC_API_KEY` or an `apiKeyHelper`.

In other words:

- `integrated` optimizes for "it works on my machine".
- `isolated` optimizes for "I know exactly what environment Claude saw".

## Safety Model

The server adds a tiered permission model on top of Claude Code's own
permission system.

| Tier | Intent |
|------|--------|
| `readonly` | Read-only review and analysis |
| `explore` | Investigation with limited shell access |
| `edit` | Default coding mode with extra deny rules |
| `full` | Claude Code decides within its own safety model |
| `unrestricted` | Sandbox-only bypass mode |

The important principle is defense in depth.

Layer 1 is this server:

- tool whitelists
- deny patterns for known-dangerous shell commands
- an explicit runtime profile

Layer 2 is Claude Code itself:

- its own permission mode
- its classifier-based safety behavior

The server does not try to replace Claude Code's safety system. It narrows the
execution envelope before the request even reaches Claude.

## Async Job Design

Long-running jobs are persisted under `CC_AGENT_STATE_DIR`.

Each job now records:

- `status`
- `phase`
- `lastHeartbeatAt`
- `childPid`
- `logPath`
- `startedCommand`

This is important because "still running", "stuck", "timed out", "Claude
failed", and "the worker process died" are different failure modes. The async
status model exists to make those states visible to the caller.

The lifecycle is intentionally explicit:

```text
queued -> launching -> starting_claude -> running -> parsing_output -> completed|failed
```

## What Changed in v2.1.1

The current implementation adds five practical improvements:

1. Structured sync failures.
   Sync calls now fail with a typed error payload before the outer MCP client
   timeout.
2. Runtime profiles.
   `integrated` and `isolated` make environment inheritance explicit.
3. Async observability.
   Jobs now expose phase, heartbeat, command, and per-job logs.
4. Better server resilience.
   Invalid parameters and unexpected handler exceptions no longer have to take
   down the whole MCP process.
5. Edge-case hardening.
   Zero-budget requests, stderr-only failures, partial log-path configuration,
   and early-stdin-close cases now resolve to explicit and test-covered
   behavior.

## Validation Snapshot

### Local stub tests

The repo includes deterministic unit tests for:

- sync timeout handling
- sync-timeout argument validation
- invalid tier handling without crashing the server
- `claude_reply` session continuation
- `claude_reply_start` background continuation
- `claude_list_jobs` enumeration
- async heartbeat and phase transitions
- persisted async failure payloads
- raw stdout fallback when Claude returns non-JSON success output
- structured nonzero handling when Claude writes only to stderr
- isolated-mode auth error guidance
- `maxBudgetUsd=0` handling
- invalid partial log-path configuration rejection

Run them with:

```bash
python3 -m unittest -v tests.test_server
```

### Live behavior observed on 2026-04-05

On the current machine:

- the live MCP server successfully moved from the earlier `v2.0.0` baseline
  into the current `v2.1.x` line
- async jobs exposed the new `phase`, `lastHeartbeatAt`, `logPath`, and
  `startedCommand` fields as expected
- a sync review call hit the configured 60-second limit and returned a
  structured `sync_timeout` instead of vanishing behind the client transport
- integrated mode could still fail with Claude-side `ECONNREFUSED`, which the
  server now preserves as a structured async error
- isolated mode returned quickly but surfaced the expected bare-mode auth issue
  (`Not logged in · Please run /login`) until explicit auth is provided

These observations matter because they confirm the main architectural goal:
even when Claude itself is unhealthy or unauthenticated, the MCP layer now
reports the failure clearly instead of disappearing behind a generic timeout.

## Relationship to Other Projects

| Direction | Project | What it wraps |
|-----------|---------|---------------|
| Claude Code -> Codex | `codex-mcp-server` | `codex` |
| Codex -> Claude Code | `claude-code-agent-for-codex` | `claude -p` |
| Generic Claude tool exposure | `claude mcp serve` | Claude tool primitives |

Together, these represent three distinct integration shapes:

- tool mirror
- Codex-as-agent
- Claude-as-agent

This project is specifically the third one.

## References

- [GitHub MCP Server README](https://github.com/github/github-mcp-server)
- [Codex Bridge README](https://github.com/eLyiN/codex-bridge)
- [MCP specification](https://modelcontextprotocol.io/specification/2025-03-26)
- [Claude Code documentation](https://docs.anthropic.com/en/docs/claude-code)
- [codex-mcp-server](https://github.com/tuannvm/codex-mcp-server)
