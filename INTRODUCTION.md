# claude-code-agent-for-codex

> *Let Codex call Claude Code as a full autonomous agent, not just a bag of tools.*

---

## The Problem

You already have **codex-mcp-server** — it lets Claude Code delegate tasks to
Codex. But the bridge is one-way. When Codex needs Claude Code's deep codebase
reasoning, multi-step editing, or its rich tool ecosystem, there's no
standardized way to call it back.

`claude mcp serve` exists, but it exposes CC's *individual tools* (Read, Edit,
Bash...). That's like handing someone a screwdriver when they asked for a
carpenter. What Codex actually needs is the *agent* — the full autonomous loop
that plans, explores, edits, tests, and iterates.

## The Solution

This MCP server wraps `claude -p` (the non-interactive agent mode) and exposes
it through 8 well-defined tools. Codex sends a task; CC's agent loop handles
the rest.

```
                           MCP (stdio)
  Codex  ─────────────────────────────>  this server
                                              │
                                         claude -p "..."
                                              │
                                     ┌────────┴────────┐
                                     │  CC Agent Loop   │
                                     │                  │
                                     │  Read  Edit Bash │
                                     │  Grep  Glob Write│
                                     │  WebSearch  ...  │
                                     └─────────────────-┘
```

## Tools at a Glance

```
 Sync (immediate return)          Async (background + poll)
 ──────────────────────           ─────────────────────────
 claude          ──────────────>  claude_start
 claude_reply    ──────────────>  claude_reply_start
                                  claude_status
                                  claude_list_jobs

 Utility
 ───────
 ping
 help
```

**When to use sync vs async?** If the task finishes in under ~2 minutes, use
`claude` / `claude_reply`. For deeper work (refactors, multi-file edits, test
suites), use `claude_start` + `claude_status` to avoid MCP timeout.

## Quick Start

```bash
# Register with Codex (one command)
codex mcp add claude-code-agent -- \
    python3 ~/.codex/mcp-servers/claude-code-agent-for-codex/server.py

# With a preferred model
codex mcp add claude-code-agent \
    --env CC_AGENT_MODEL=opus \
    -- python3 ~/.codex/mcp-servers/claude-code-agent-for-codex/server.py

# Verify
codex mcp list
```

That's it. Next time you start Codex, it can call `claude(prompt="...")`.

## What Codex Can Do With It

**Delegate complex coding tasks**
```json
{
  "name": "claude",
  "arguments": {
    "prompt": "Refactor the auth middleware in src/auth/ to use JWT instead of session cookies. Update all tests.",
    "workingDirectory": "/home/user/myproject",
    "effort": "high"
  }
}
```

**Get a second opinion on code**
```json
{
  "name": "claude",
  "arguments": {
    "prompt": "Review the changes in src/parser.ts. Are there edge cases I'm missing?",
    "permissionMode": "plan",
    "effort": "medium"
  }
}
```

**Long-running task (async)**
```json
{
  "name": "claude_start",
  "arguments": {
    "prompt": "Migrate the entire test suite from Jest to Vitest. Run tests after migration to verify.",
    "workingDirectory": "/home/user/myproject",
    "effort": "max"
  }
}
// Returns: { "jobId": "abc123...", "status": "queued", "done": false }

// Later:
{
  "name": "claude_status",
  "arguments": { "jobId": "abc123...", "waitSeconds": 30 }
}
// Returns: { "done": true, "response": "...", "threadId": "..." }
```

**Multi-turn conversation**
```json
// First call
{ "name": "claude", "arguments": { "prompt": "Analyze the database schema in schema.sql" } }
// Returns: { "threadId": "sess-xyz", "response": "The schema has 12 tables..." }

// Follow-up (same session)
{ "name": "claude_reply", "arguments": { "threadId": "sess-xyz", "prompt": "Now add an index for the users.email column" } }
```

## Parameters

Every tool accepts these optional parameters:

| Parameter | Values | Default |
|-----------|--------|---------|
| `model` | `opus`, `sonnet`, `haiku`, or full model ID | CC default |
| `effort` | `low`, `medium`, `high`, `max` | CC default |
| `permissionMode` | `default`, `plan`, `auto`, `bypassPermissions` | `auto` |
| `allowedTools` | `"Bash Edit Read Grep Glob Write"` | all tools |
| `workingDirectory` | absolute path | server's cwd |
| `addDirs` | `["/path/a", "/path/b"]` | none |
| `maxBudgetUsd` | number | no limit |
| `systemPrompt` | string | none |

## Environment Variables

Configure defaults without changing code:

| Variable | Default | Purpose |
|----------|---------|---------|
| `CLAUDE_BIN` | `claude` | CLI binary path |
| `CC_AGENT_MODEL` | — | Default model |
| `CC_AGENT_EFFORT` | — | Default effort |
| `CC_AGENT_PERMISSION_MODE` | `auto` | Default permissions |
| `CC_AGENT_TIMEOUT_SEC` | `900` | Max seconds per invocation |
| `CC_AGENT_MAX_BUDGET_USD` | — | Default budget cap |
| `CC_AGENT_SYSTEM_PROMPT` | — | Default system prompt |

## Design Decisions

### Why `auto` permission mode?

When CC runs as a delegated agent, no human is watching. `plan` mode
(read-only) is too restrictive for real coding tasks. `auto` mode lets CC
approve safe operations (file reads, searches) while still blocking
destructive ones (force push, rm -rf). It's the sweet spot between safety and
autonomy.

### Why the async pattern?

CC agent tasks can take 2--15 minutes for complex work. MCP's tool-call
timeout is ~120 seconds. The async pattern (`claude_start` returns a jobId
immediately; `claude_status` polls) decouples execution from the timeout.
Jobs persist to disk, so they survive MCP server restarts.

### Why Python, not Node.js?

Zero dependencies. The server is a single file using only Python stdlib. No
`npm install`, no `node_modules`, no build step. It follows the same pattern
as the existing `claude-review` MCP server that already works in production
with Codex.

### Why not just `claude mcp serve`?

That exposes CC's *low-level tools* (Read, Edit, Bash). Codex already has
equivalent capabilities. The value of CC-as-agent is the *judgment loop* —
the ability to plan multi-step approaches, try alternatives when something
fails, and iterate until the task is done. That's what `claude -p` provides
and what this server wraps.

## Test Results

All tests pass (tested 2026-04-05):

```
=== Protocol Tests ===
  Initialize .......................... PASS
  Tools List (8 tools) ................ PASS
  Ping ................................ PASS
  Help ................................ PASS
  List Jobs (empty) ................... PASS
  Unknown Tool Error .................. PASS
  Validation (missing prompt) ......... PASS
  Validation (missing threadId) ....... PASS
  Validation (bad jobId) .............. PASS

=== End-to-End Tests ===
  Sync Agent (2+2=4) .................. PASS  (3.9s)
  Async Start + Poll (3*7=21) ......... PASS  (5.5s)
  Session Resume (21*2=42) ............ PASS
  Job Listing ......................... PASS
```

## File Structure

```
~/.codex/mcp-servers/claude-code-agent-for-codex/
    server.py          # The MCP server (single file, zero deps)
    README.md          # Technical reference
    INTRODUCTION.md    # This document
```

## Symmetry

| Direction | Package | Wraps | Transport |
|-----------|---------|-------|-----------|
| CC -> Codex | `codex-mcp-server` | `codex exec` | stdio (Node.js) |
| Codex -> CC | `claude-code-agent-for-codex` | `claude -p` | stdio (Python) |

Together, they complete the bidirectional bridge between the two agent systems.

## References

- [MCP Specification](https://modelcontextprotocol.io/specification/2025-03-26) — The protocol this server implements
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) — The agent being wrapped
- [codex-mcp-server](https://github.com/tuannvm/codex-mcp-server) — The symmetric counterpart (CC -> Codex)
- [Multi-Agent Task Delegation](https://arxiv.org/abs/2402.01680) — Architectural pattern reference
