# Introduction

This project exposes Claude Code as an autonomous agent over MCP.

The distinction matters. A client like Codex already knows how to read files,
run commands, and edit code. What it does not get from a plain tool mirror is
Claude Code's end-to-end agent behavior: planning, deciding when to inspect
more context, resuming prior sessions, and stopping when the task is done.

That is why this server wraps `claude -p` rather than `claude mcp serve`.

```text
Codex -> MCP -> this server -> claude -p -> Claude Code agent loop
```

## Why `claude -p` Instead of `claude mcp serve`

`claude mcp serve` is useful when another client wants direct access to Claude
Code primitives such as Read, Edit, Grep, or Bash.

That is not the need here.

Codex already has equivalent primitives. The missing value is delegated agent
judgment: giving Claude a task, letting it decide how much context it needs,
resuming the same session later, and getting a coherent answer or failure
state back.

This server is therefore higher-level by design. It exposes Claude Code as an
agent, not as a bag of tools.

## Sync vs Async

MCP clients usually enforce a transport timeout of around two minutes, while
real coding tasks often take longer. The server exposes both sync and async
entry points to handle that gap cleanly.

Sync calls are convenient for short analysis or small follow-ups. The server
uses a sync timeout that is shorter than the client's outer timeout, so callers
receive a structured `sync_timeout` error instead of a generic transport
failure.

Async calls are the safe default for anything substantial. The caller gets a
`jobId` immediately and can poll for status, wait with a bounded timeout, or
inspect the persisted logs afterward.

Progress is a visibility mechanism, not a way to extend a sync call's lifetime.
Even when the client provides a progress token and the server emits
`notifications/progress`, synchronous calls remain bounded.

## Streamed Progress

The server runs Claude in `stream-json` mode and translates Claude's own
intermediate events into MCP-visible progress.

That matters because observability is better when it comes from Claude's real
output rather than heartbeat polling alone. Tool-use events, assistant text,
and terminal results can all contribute to the caller's view of what is
happening.

For async jobs, that same stream is also reflected into persisted job state so
the caller can inspect progress after the fact, not only while waiting live.

## Runtime Profiles

The server exposes three runtime profiles, each answering the same question:
how much of the local Claude environment should be inherited by this delegated
task?

| Profile | Behavior |
|---------|----------|
| `simple` | Uses local auth, but disables inherited slash commands and inherited MCP servers |
| `integrated` | Inherits the full local Claude environment, including plugins, skills, and MCP configuration |
| `isolated` | Runs with `--bare` for a clean, reproducible environment with no inherited local state |

`simple` is the default because it keeps the useful part of normal local Claude
execution without dragging the full local Claude ecosystem into every task.

`integrated` is the compatibility-first mode. It is the right choice when you
explicitly want local plugins, skills, or MCP configuration to participate in
the delegated task.

`isolated` is the reproducibility-first mode. Because `--bare` does not use
local OAuth or keychain state, it usually requires explicit auth such as
`ANTHROPIC_API_KEY` or an `apiKeyHelper`.

In practice:

- `simple` optimizes for normal Claude with minimal inherited baggage
- `integrated` optimizes for full local Claude behavior
- `isolated` optimizes for strict control over the execution environment

## Permission Model

The server adds a tiered permission layer on top of Claude Code's own safety
system.

| Tier | Intent |
|------|--------|
| `readonly` | Read-only review and analysis |
| `explore` | Investigation with limited shell access |
| `edit` | Default coding mode with additional deny rules |
| `full` | Claude Code decides within its own safety model |
| `unrestricted` | Sandbox-only bypass mode |

The underlying principle is defense in depth.

The server narrows the execution envelope before the request reaches Claude:
tool whitelists, explicit shell deny patterns, and an explicit runtime profile.
Claude Code then applies its own permission mode and safety behavior inside
that narrower envelope.

The server does not replace Claude Code's safety model. It constrains the task
boundary around it.

## Async Job Observability

Long-running jobs are persisted under `CC_AGENT_STATE_DIR`. Each job exposes
enough information to separate the failure modes that matter in practice:

- `status` and `phase` show where the job is in its lifecycle
- `lastHeartbeatAt` shows whether the worker is still alive
- `lastProgressMessage` shows the most recent stream-derived event summary
- `childPid`, `logPath`, `stdoutPath`, and `stderrPath` make live debugging
  possible without re-running the task
- `startedCommand` and `latestLogLine` make it easier to distinguish server
  failures from Claude failures

The lifecycle is explicit:

```text
queued -> launching -> starting_claude -> running -> parsing_output -> completed | failed
```

That explicit state model exists so "still running," "worker died," "Claude
failed," and "timed out" do not collapse into the same opaque timeout.

## Relationship to Other Projects

| Direction | Project | What It Wraps |
|-----------|---------|---------------|
| Claude Code -> Codex | `codex-mcp-server` | `codex` |
| Codex -> Claude Code | `claude-code-agent-for-codex` | `claude -p` |
| Generic Claude tool exposure | `claude mcp serve` | Claude Code tool primitives |

These are three different integration shapes:

- tool mirror
- Codex-as-agent
- Claude-as-agent

This project is specifically the third.

## References

- [MCP specification](https://modelcontextprotocol.io/specification/2025-03-26)
- [Claude Code documentation](https://docs.anthropic.com/en/docs/claude-code)
- [codex-mcp-server](https://github.com/tuannvm/codex-mcp-server)
