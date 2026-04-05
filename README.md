# claude-code-agent-for-codex

MCP server that exposes Claude Code's **full autonomous agent loop** to Codex.

This is the symmetric counterpart of
[codex-mcp-server](https://github.com/tuannvm/codex-mcp-server) (which lets
Claude Code call Codex). The key distinction from `claude mcp serve` (CC's
built-in MCP server) is that this wraps the **agent loop** (`claude -p`), not
the individual low-level tools (Read/Edit/Bash).

## Architecture

```
Codex (client) ──MCP──> this server ──subprocess──> claude -p (agent loop)
                                                        │
                                                   Read, Edit, Bash,
                                                   Grep, Glob, Write, ...
```

## Tools

| Tool | Mode | Description |
|------|------|-------------|
| `claude` | sync | Execute CC agent, return structured result with threadId |
| `claude_reply` | sync | Continue a previous CC session via threadId |
| `claude_start` | async | Start background CC task, return jobId immediately |
| `claude_reply_start` | async | Background follow-up in existing session |
| `claude_status` | poll | Check/wait for async job completion |
| `claude_list_jobs` | — | List all background jobs |
| `ping` | — | Health check |
| `help` | — | Return `claude --help` output |

### Sync vs Async

- **Sync** (`claude`, `claude_reply`): for tasks that finish within MCP
  timeout (~120s). Simpler, returns result directly.
- **Async** (`claude_start` + `claude_status`): for long-running tasks (deep
  codebase refactors, multi-file edits, test runs). Returns jobId immediately;
  poll `claude_status` with optional `waitSeconds` for bounded blocking.

## Install

```bash
# Register with Codex
codex mcp add claude-code-agent -- python3 ~/.codex/mcp-servers/claude-code-agent-for-codex/server.py
```

With custom model:
```bash
codex mcp add claude-code-agent \
  --env CC_AGENT_MODEL=opus \
  -- python3 ~/.codex/mcp-servers/claude-code-agent-for-codex/server.py
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_BIN` | `claude` | Path to Claude Code CLI |
| `CC_AGENT_MODEL` | *(CC default)* | Default model (`opus`, `sonnet`, `haiku`, or full ID) |
| `CC_AGENT_EFFORT` | *(CC default)* | Default effort level (`low`/`medium`/`high`/`max`) |
| `CC_AGENT_SYSTEM_PROMPT` | *(none)* | Default system prompt |
| `CC_AGENT_PERMISSION_MODE` | `auto` | Default permission mode |
| `CC_AGENT_TIMEOUT_SEC` | `900` | Subprocess timeout (seconds) |
| `CC_AGENT_MAX_BUDGET_USD` | *(none)* | Default budget cap per invocation |
| `CC_AGENT_DEBUG_LOG` | `/tmp/claude-code-agent-for-codex-debug.log` | Debug log path |
| `CC_AGENT_STATE_DIR` | `~/.codex/state/claude-code-agent-for-codex/` | Async job state directory |

## Common Parameters (all tools)

| Parameter | Type | Description |
|-----------|------|-------------|
| `prompt` | string | Task/instruction (required) |
| `model` | string | Model override |
| `effort` | enum | `low`/`medium`/`high`/`max` |
| `systemPrompt` | string | Custom system prompt |
| `permissionMode` | enum | `default`/`plan`/`auto`/`bypassPermissions` |
| `allowedTools` | string | Space-separated tool names |
| `workingDirectory` | string | Working directory |
| `addDirs` | string[] | Additional directories for tool access |
| `maxBudgetUsd` | number | Budget cap |

## Design Rationale

### Why not `claude mcp serve`?

`claude mcp serve` exposes CC's individual tools (Read, Edit, Bash...) as MCP
tools. Codex already has equivalent capabilities. What Codex needs from CC is
the **agent judgment** — the ability to autonomously plan, explore, edit, and
verify across multiple steps.

### Why async jobs?

CC agent tasks can take 2-15 minutes for complex refactors. MCP tool calls
typically timeout around 120 seconds. The async pattern (start + poll)
decouples execution from the MCP timeout.

### Why `--permission-mode auto` by default?

When CC runs as a delegated agent, human-in-the-loop approval creates a
deadlock (no human is watching). `auto` mode lets CC auto-approve safe
operations while still blocking destructive ones. Use `bypassPermissions`
only in trusted sandboxed environments.

## References

- [MCP Specification (2025-03-26)](https://modelcontextprotocol.io/specification/2025-03-26)
- [Claude Code CLI Documentation](https://docs.anthropic.com/en/docs/claude-code)
- [Multi-agent task delegation patterns](https://arxiv.org/abs/2402.01680)
- [codex-mcp-server (symmetric counterpart)](https://github.com/tuannvm/codex-mcp-server)
