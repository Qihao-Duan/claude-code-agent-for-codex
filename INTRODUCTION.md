# claude-code-agent-for-codex

> *Let Codex call Claude Code as a full autonomous agent, not just a bag of tools.*

---

## The Problem

You have **codex-mcp-server** (CC -> Codex). But the bridge is one-way. When
Codex needs Claude Code's deep codebase reasoning, there's no way to call back.

`claude mcp serve` exists but exposes CC's *individual tools* (Read, Edit,
Bash). That's a screwdriver when you asked for a carpenter. Codex needs the
*agent* — the full loop that plans, explores, edits, tests, and iterates.

## The Solution

```
                           MCP (stdio)
  Codex  ─────────────────────────────>  this server
                                              │
                                         claude -p (stdin)
                                              │
                                     ┌────────┴────────┐
                                     │  CC Agent Loop   │
                                     │                  │
                                     │  Read  Edit Bash │
                                     │  Grep  Glob Write│
                                     │  WebSearch  ...  │
                                     └─────────────────-┘
```

## Permission Tiers

The core design: **how much power should Codex give CC?**

Inspired by CC's auto-mode classifier (8 allow rules, 25 block rules), the
server provides 5 tiers. Each tier layers defense-in-depth: an outer ring of
tool restrictions from this server, plus CC's inner auto-mode classifier.

```
  Safety ████████████████████░░░░░░░░░░░░░░░░░░░░ Capability
         readonly   explore   edit    full   unrestricted
```

| Tier | Permission Mode | Tools Available | Deny Patterns | Use Case |
|------|----------------|-----------------|---------------|----------|
| **`readonly`** | `plan` | Read, Grep, Glob | 0 | Code review, analysis, questions |
| **`explore`** | `auto` | Read, Grep, Glob, Bash | 6 | Investigation, debugging, log analysis |
| **`edit`** | `auto` | All (default) | 12 | **DEFAULT.** Most coding tasks |
| **`full`** | `auto` | All | 0 | Complex tasks needing max flexibility |
| **`unrestricted`** | `bypassPermissions` | All | 0 | Sandbox environments ONLY |

### What does each tier block?

**`edit` tier** (default) denies these patterns via `--settings` JSON:
```
Bash(rm -rf *)        Bash(rm -r *)           # irreversible deletion
Bash(sudo *)                                  # privilege escalation
Bash(git push --force *)  Bash(git push -f *) # destructive git
Bash(git reset --hard *)  Bash(git clean -f *) Bash(git branch -D *)
Bash(chmod 777 *)                             # security weakening
Bash(mkfs *)  Bash(dd *)  Bash(kill -9 *)     # system-level damage
```

CC's auto-mode classifier (25 block rules) **still runs underneath**, catching
production deploys, data exfiltration, credential leaks, and more. The deny
list is defense-in-depth — two independent safety layers.

### How Codex chooses a tier

```json
// "Just analyze this, don't touch anything"
{ "name": "claude", "arguments": { "prompt": "...", "tier": "readonly" } }

// "Fix this bug" (default tier=edit, safe for most work)
{ "name": "claude", "arguments": { "prompt": "..." } }

// "This is complex, I trust CC's own safety classifier"
{ "name": "claude", "arguments": { "prompt": "...", "tier": "full" } }
```

Codex can also call the `tiers` tool to discover available tiers at runtime.

## Tools at a Glance

```
 Sync (immediate return)          Async (background + poll)
 ──────────────────────           ─────────────────────────
 claude          ──────────────>  claude_start
 claude_reply    ──────────────>  claude_reply_start
                                  claude_status
                                  claude_list_jobs

 Discovery / Utility
 ───────────────────
 tiers            list available permission tiers
 ping             health check
 help             claude --help output
```

**Sync vs async?** Tasks under ~2 min: `claude`. Longer work: `claude_start` +
`claude_status` (avoids MCP's ~120s timeout). Jobs persist to disk.

## Quick Start

```bash
# Register with Codex (one command)
codex mcp add claude-code-agent -- \
    python3 ~/.codex/mcp-servers/claude-code-agent-for-codex/server.py

# With a preferred model and default tier
codex mcp add claude-code-agent \
    --env CC_AGENT_MODEL=opus \
    --env CC_AGENT_DEFAULT_TIER=edit \
    -- python3 ~/.codex/mcp-servers/claude-code-agent-for-codex/server.py

# Verify
codex mcp list
```

## Examples

**Safe code analysis** (readonly tier)
```json
{
  "name": "claude",
  "arguments": {
    "prompt": "Review src/parser.ts for edge cases. What am I missing?",
    "tier": "readonly"
  }
}
```

**Fix a bug** (default edit tier)
```json
{
  "name": "claude",
  "arguments": {
    "prompt": "The CSV parser crashes on empty lines. Fix it and add a test.",
    "workingDirectory": "/home/user/myproject",
    "effort": "high"
  }
}
```

**Long refactor** (async, full tier)
```json
{
  "name": "claude_start",
  "arguments": {
    "prompt": "Migrate test suite from Jest to Vitest. Run tests to verify.",
    "tier": "full",
    "workingDirectory": "/home/user/myproject"
  }
}
// Returns: { "jobId": "abc...", "status": "queued", "done": false }

// Poll:
{ "name": "claude_status", "arguments": { "jobId": "abc...", "waitSeconds": 30 } }
```

**Multi-turn** (session resume)
```json
{ "name": "claude", "arguments": { "prompt": "Analyze the DB schema in schema.sql" } }
// Returns: { "threadId": "sess-xyz", "response": "12 tables..." }

{ "name": "claude_reply", "arguments": { "threadId": "sess-xyz", "prompt": "Add an index on users.email" } }
```

## Parameters

| Parameter | Values | Default |
|-----------|--------|---------|
| **`tier`** | `readonly`, `explore`, `edit`, `full`, `unrestricted` | `edit` |
| `model` | `opus`, `sonnet`, `haiku`, or full model ID | CC default |
| `effort` | `low`, `medium`, `high`, `max` | CC default |
| `permissionMode` | overrides tier's mode | set by tier |
| `allowedTools` | overrides tier's tool whitelist | set by tier |
| `disallowedTools` | overrides tier's deny list | set by tier |
| `workingDirectory` | absolute path | server's cwd |
| `addDirs` | `["/path/a", "/path/b"]` | none |
| `maxBudgetUsd` | number | no limit |
| `systemPrompt` | string | none |

Override priority: **explicit param > tier defaults > env defaults**.

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `CLAUDE_BIN` | `claude` | CLI binary path |
| `CC_AGENT_MODEL` | -- | Default model |
| `CC_AGENT_EFFORT` | -- | Default effort |
| `CC_AGENT_DEFAULT_TIER` | `edit` | Default permission tier |
| `CC_AGENT_TIMEOUT_SEC` | `900` | Max seconds per invocation |
| `CC_AGENT_MAX_BUDGET_USD` | -- | Default budget cap |
| `CC_AGENT_SYSTEM_PROMPT` | -- | Default system prompt |

## Design Decisions

### Why tiers instead of raw permission flags?

Raw flags (`permissionMode`, `allowedTools`) require Codex to understand CC's
permission model. Tiers reduce this to a single dial: *"how much do I trust CC
for this task?"* The answer maps to a tested, safe configuration.

### Why two safety layers?

Layer 1 (this server): `--settings` JSON with deny patterns removes dangerous
tools from CC's context entirely — the model can't even attempt them.

Layer 2 (CC auto-mode): 25 block rules catch intent-level threats (production
deploys, data exfiltration, credential scanning) that pattern matching can't.

Neither layer alone is sufficient. Pattern deny lists miss creative workarounds.
Auto-mode classifiers have false negatives. Together: defense-in-depth.

### Why `edit` as the default tier?

- `readonly` / `explore`: too restrictive for the common case (Codex delegates
  coding tasks, which require Edit/Write)
- `full`: safe in practice (CC's auto-mode catches real dangers), but the deny
  list provides a visible, auditable safety boundary
- `edit`: all tools enabled, 12 known-dangerous patterns removed, auto-mode
  classifier active underneath. The right balance for unsupervised delegation.

### Why stdin for prompt delivery?

CC's CLI uses variadic flags (`--tools <tools...>`, `--disallowedTools <tools...>`)
that greedily consume trailing arguments — including the prompt. Passing the
prompt via stdin avoids this entirely and works reliably with all flag
combinations.

## Test Results (v2.0.0, 2026-04-05)

```
=== Tier Tests (all sync) ===
  readonly  (5+3=8)  ................ PASS  4.0s
  explore   (9-4=5)  ................ PASS  3.4s
  edit      (6*7=42) ................ PASS  4.0s
  full      (3*3=9)  ................ PASS  3.5s

=== Async Test ===
  claude_start + claude_status ...... PASS  (10+10=20)

=== Protocol Tests ===
  Initialize, tools/list, ping,
  help, tiers, validation, errors ... 9/9 PASS
```

## Symmetry

| Direction | Package | Wraps | Transport |
|-----------|---------|-------|-----------|
| CC -> Codex | `codex-mcp-server` | `codex exec` | stdio (Node.js) |
| Codex -> CC | `claude-code-agent-for-codex` | `claude -p` | stdio (Python) |

Together: bidirectional bridge. Either agent can delegate to the other.

## References

- [MCP Specification (2025-03-26)](https://modelcontextprotocol.io/specification/2025-03-26) -- Protocol standard
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) -- The wrapped agent
- [codex-mcp-server](https://github.com/tuannvm/codex-mcp-server) -- Symmetric counterpart
- [CC Auto-Mode Classifier](https://docs.anthropic.com/en/docs/claude-code/security) -- 8 allow + 25 block rules
- [Multi-Agent Task Delegation](https://arxiv.org/abs/2402.01680) -- Architecture pattern
