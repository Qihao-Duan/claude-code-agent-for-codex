#!/usr/bin/env python3
"""Claude Code Agent MCP server for Codex.

Exposes Claude Code's full autonomous agent loop as MCP tools so that
Codex can delegate complex multi-step coding tasks to Claude Code.

Architecture note
-----------------
This is the symmetric counterpart of ``codex-mcp-server`` (which lets
Claude Code call Codex).  The key distinction from ``claude mcp serve``
(CC's built-in MCP server) is that this wraps the *agent loop*
(``claude -p``), not the individual low-level tools.

References
----------
- MCP specification: https://modelcontextprotocol.io/specification/2025-03-26
- Claude Code CLI: https://docs.anthropic.com/en/docs/claude-code
- Multi-agent delegation pattern: https://arxiv.org/abs/2402.01680
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Binary I/O for MCP framing ──────────────────────────────────────────
sys.stdout = os.fdopen(sys.stdout.fileno(), "wb", buffering=0)
sys.stdin = os.fdopen(sys.stdin.fileno(), "rb", buffering=0)

# ── Configuration via environment ────────────────────────────────────────
SERVER_NAME = "claude-code-agent-for-codex"
SERVER_VERSION = "1.0.0"

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
DEFAULT_MODEL = os.environ.get("CC_AGENT_MODEL", "")
DEFAULT_EFFORT = os.environ.get("CC_AGENT_EFFORT", "")
DEFAULT_SYSTEM_PROMPT = os.environ.get("CC_AGENT_SYSTEM_PROMPT", "")
DEFAULT_PERMISSION_MODE = os.environ.get("CC_AGENT_PERMISSION_MODE", "auto")
DEFAULT_TIMEOUT_SEC = int(os.environ.get("CC_AGENT_TIMEOUT_SEC", "900"))
DEFAULT_MAX_BUDGET = os.environ.get("CC_AGENT_MAX_BUDGET_USD", "")

DEBUG_LOG = Path(
    os.environ.get("CC_AGENT_DEBUG_LOG", f"/tmp/{SERVER_NAME}-debug.log")
)
STATE_DIR = Path(
    os.environ.get(
        "CC_AGENT_STATE_DIR",
        str(Path.home() / ".codex" / "state" / SERVER_NAME),
    )
)
JOBS_DIR = STATE_DIR / "jobs"

_use_ndjson = False
TERMINAL_JOB_STATES = {"completed", "failed"}

# ── Helpers ──────────────────────────────────────────────────────────────

def debug_log(message: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a", encoding="utf-8") as fh:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            fh.write(f"[{ts}] {message}\n")
    except OSError:
        pass


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ── MCP transport (Content-Length framing + NDJSON fallback) ─────────────

def send_response(response: dict[str, Any]) -> None:
    global _use_ndjson
    payload = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    debug_log(f"SEND {payload.decode('utf-8', errors='replace')[:2000]}")
    if _use_ndjson:
        sys.stdout.write(payload + b"\n")
    else:
        header = f"Content-Length: {len(payload)}\r\n\r\n".encode("utf-8")
        sys.stdout.write(header + payload)
    sys.stdout.flush()


def read_message() -> dict[str, Any] | None:
    global _use_ndjson
    line = sys.stdin.readline()
    if not line:
        return None
    line_text = line.decode("utf-8").rstrip("\r\n")

    # Content-Length framing
    if line_text.lower().startswith("content-length:"):
        try:
            content_length = int(line_text.split(":", 1)[1].strip())
        except ValueError:
            return None
        while True:
            header_line = sys.stdin.readline()
            if not header_line:
                return None
            if header_line in {b"\r\n", b"\n"}:
                break
        body = sys.stdin.read(content_length)
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return None

    # NDJSON fallback
    if line_text.startswith("{") or line_text.startswith("["):
        _use_ndjson = True
        try:
            return json.loads(line_text)
        except json.JSONDecodeError:
            return None

    return None


# ── Claude Code CLI interaction ──────────────────────────────────────────

def find_claude_bin() -> str | None:
    if Path(CLAUDE_BIN).is_file():
        return CLAUDE_BIN
    return shutil.which(CLAUDE_BIN)


def parse_claude_json(raw_stdout: str) -> tuple[dict[str, Any] | None, str | None]:
    """Extract the JSON object from ``claude -p --output-format json`` output."""
    lines = [ln.strip() for ln in raw_stdout.splitlines() if ln.strip()]
    if not lines:
        return None, "Claude CLI returned empty output"
    # Walk backwards — the JSON payload is typically the last line.
    for candidate in reversed(lines):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload, None
    return None, "Claude CLI did not return valid JSON"


def build_command(
    prompt: str,
    *,
    session_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    system_prompt: str | None = None,
    permission_mode: str | None = None,
    allowed_tools: str | None = None,
    working_directory: str | None = None,
    add_dirs: list[str] | None = None,
    max_budget_usd: float | None = None,
) -> list[str]:
    bin_path = find_claude_bin()
    if not bin_path:
        raise FileNotFoundError(f"Claude CLI not found: {CLAUDE_BIN}")

    cmd: list[str] = [bin_path, "-p", "--output-format", "json"]

    # Session resume
    if session_id:
        cmd.extend(["--resume", session_id])

    # Model
    selected_model = model or DEFAULT_MODEL
    if selected_model:
        cmd.extend(["--model", selected_model])

    # Effort
    selected_effort = effort or DEFAULT_EFFORT
    if selected_effort:
        cmd.extend(["--effort", selected_effort])

    # System prompt
    selected_system = system_prompt or DEFAULT_SYSTEM_PROMPT
    if selected_system:
        cmd.extend(["--system-prompt", selected_system])

    # Permission mode
    selected_perm = permission_mode or DEFAULT_PERMISSION_MODE
    if selected_perm:
        cmd.extend(["--permission-mode", selected_perm])

    # Allowed tools
    if allowed_tools is not None:
        cmd.extend(["--allowedTools", allowed_tools])

    # Working directory
    if working_directory:
        cmd.extend(["--add-dir", working_directory])

    # Additional directories
    if add_dirs:
        for d in add_dirs:
            cmd.extend(["--add-dir", d])

    # Budget
    budget = max_budget_usd or (float(DEFAULT_MAX_BUDGET) if DEFAULT_MAX_BUDGET else None)
    if budget:
        cmd.extend(["--max-budget-usd", str(budget)])

    # Prompt is the last positional argument
    cmd.append(prompt)
    return cmd


def run_claude_agent(
    prompt: str,
    *,
    session_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    system_prompt: str | None = None,
    permission_mode: str | None = None,
    allowed_tools: str | None = None,
    working_directory: str | None = None,
    add_dirs: list[str] | None = None,
    max_budget_usd: float | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Run ``claude -p`` synchronously and return structured result."""
    try:
        cmd = build_command(
            prompt,
            session_id=session_id,
            model=model,
            effort=effort,
            system_prompt=system_prompt,
            permission_mode=permission_mode,
            allowed_tools=allowed_tools,
            working_directory=working_directory,
            add_dirs=add_dirs,
            max_budget_usd=max_budget_usd,
        )
    except FileNotFoundError as exc:
        return None, str(exc)

    debug_log(f"RUN {' '.join(cmd)}")

    env = os.environ.copy()
    if working_directory:
        env["PWD"] = working_directory

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=DEFAULT_TIMEOUT_SEC,
            cwd=working_directory or None,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, f"Claude agent timed out after {DEFAULT_TIMEOUT_SEC}s"

    payload, parse_error = parse_claude_json(result.stdout)
    if parse_error:
        stderr = result.stderr.strip()
        # If JSON parsing failed but we have raw text output, return it
        raw_text = result.stdout.strip() or stderr
        if raw_text:
            return {
                "threadId": None,
                "response": raw_text,
                "model": model or DEFAULT_MODEL,
                "duration_ms": None,
                "stop_reason": "parse_fallback",
            }, None
        msg = parse_error if not stderr else f"{parse_error}. stderr: {stderr}"
        return None, msg

    assert payload is not None

    if result.returncode != 0 or payload.get("is_error"):
        message = str(
            payload.get("result")
            or payload.get("error")
            or result.stderr.strip()
            or "Claude agent failed"
        )
        return None, message

    thread_id = payload.get("session_id")
    response_text = str(payload.get("result", "")).strip()
    model_name = payload.get("model", "") or model or DEFAULT_MODEL
    cost = payload.get("cost_usd") or payload.get("total_cost_usd")

    return {
        "threadId": thread_id,
        "response": response_text,
        "model": model_name,
        "duration_ms": payload.get("duration_ms"),
        "stop_reason": payload.get("stop_reason"),
        "cost_usd": cost,
        "num_turns": payload.get("num_turns"),
    }, None


# ── Async job management ─────────────────────────────────────────────────

def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temp_path.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def job_state_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def is_pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def serialize_job(job: dict[str, Any]) -> dict[str, Any]:
    result = job.get("result") or {}
    return {
        "jobId": job.get("jobId"),
        "status": job.get("status"),
        "done": job.get("status") in TERMINAL_JOB_STATES,
        "threadId": result.get("threadId"),
        "response": result.get("response"),
        "model": result.get("model"),
        "duration_ms": result.get("duration_ms"),
        "stop_reason": result.get("stop_reason"),
        "cost_usd": result.get("cost_usd"),
        "num_turns": result.get("num_turns"),
        "error": job.get("error"),
        "createdAt": job.get("createdAt"),
        "startedAt": job.get("startedAt"),
        "completedAt": job.get("completedAt"),
        "resumeHint": "Call claude_status with this jobId until done=true.",
    }


def start_async_agent(
    prompt: str,
    *,
    session_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    system_prompt: str | None = None,
    permission_mode: str | None = None,
    allowed_tools: str | None = None,
    working_directory: str | None = None,
    add_dirs: list[str] | None = None,
    max_budget_usd: float | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    job_id = uuid.uuid4().hex
    created_at = utc_now()
    job: dict[str, Any] = {
        "jobId": job_id,
        "status": "queued",
        "createdAt": created_at,
        "startedAt": None,
        "completedAt": None,
        "updatedAt": created_at,
        "error": None,
        "result": None,
        "workerPid": None,
        "request": {
            "prompt": prompt,
            "threadId": session_id,
            "model": model,
            "effort": effort,
            "systemPrompt": system_prompt,
            "permissionMode": permission_mode,
            "allowedTools": allowed_tools,
            "workingDirectory": working_directory,
            "addDirs": add_dirs,
            "maxBudgetUsd": max_budget_usd,
        },
    }

    job_path = job_state_path(job_id)
    write_json(job_path, job)

    try:
        worker = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--run-job", job_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            cwd=working_directory or None,
        )
    except OSError as exc:
        job["status"] = "failed"
        job["completedAt"] = utc_now()
        job["updatedAt"] = job["completedAt"]
        job["error"] = f"Failed to launch background worker: {exc}"
        write_json(job_path, job)
        return None, job["error"]

    job["workerPid"] = worker.pid
    job["updatedAt"] = utc_now()
    write_json(job_path, job)
    debug_log(f"JOB_START job_id={job_id} worker_pid={worker.pid}")
    return serialize_job(job), None


def get_job_status(
    job_id: str, *, wait_seconds: int = 0
) -> tuple[dict[str, Any] | None, str | None]:
    job_path = job_state_path(job_id)
    if not job_path.exists():
        return None, f"Unknown jobId: {job_id}"

    deadline = time.monotonic() + max(wait_seconds, 0)
    while True:
        job = read_json(job_path)
        if job.get("status") in {
            "queued",
            "running",
        } and not is_pid_alive(job.get("workerPid")):
            job["status"] = "failed"
            job["error"] = "Background worker exited before completing"
            job["completedAt"] = utc_now()
            job["updatedAt"] = job["completedAt"]
            write_json(job_path, job)
        if job.get("status") in TERMINAL_JOB_STATES:
            return serialize_job(job), None
        if time.monotonic() >= deadline:
            return serialize_job(job), None
        time.sleep(min(0.5, max(deadline - time.monotonic(), 0.0)))


def list_jobs() -> list[dict[str, Any]]:
    """Return all jobs, sorted newest first."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    jobs = []
    for p in JOBS_DIR.glob("*.json"):
        if p.suffix == ".tmp":
            continue
        try:
            job = read_json(p)
            jobs.append(serialize_job(job))
        except (json.JSONDecodeError, OSError):
            continue
    jobs.sort(key=lambda j: j.get("createdAt", ""), reverse=True)
    return jobs


def run_async_job(job_id: str) -> int:
    """Entry point for the background worker subprocess."""
    job_path = job_state_path(job_id)
    if not job_path.exists():
        debug_log(f"JOB_MISSING job_id={job_id}")
        return 1

    job = read_json(job_path)
    job["status"] = "running"
    job["startedAt"] = utc_now()
    job["updatedAt"] = job["startedAt"]
    job["workerPid"] = os.getpid()
    write_json(job_path, job)
    debug_log(f"JOB_RUNNING job_id={job_id} pid={os.getpid()}")

    req = job.get("request") or {}
    try:
        add_dirs = req.get("addDirs")
        if add_dirs and not isinstance(add_dirs, list):
            add_dirs = None
        budget_raw = req.get("maxBudgetUsd")
        budget = float(budget_raw) if budget_raw else None

        payload, error = run_claude_agent(
            str(req.get("prompt", "")),
            session_id=req.get("threadId"),
            model=req.get("model"),
            effort=req.get("effort"),
            system_prompt=req.get("systemPrompt"),
            permission_mode=req.get("permissionMode"),
            allowed_tools=req.get("allowedTools"),
            working_directory=req.get("workingDirectory"),
            add_dirs=add_dirs,
            max_budget_usd=budget,
        )
    except Exception as exc:
        payload = None
        error = f"Background agent crashed: {exc}"
        debug_log(traceback.format_exc())

    finished_at = utc_now()
    job = read_json(job_path)
    job["updatedAt"] = finished_at
    job["completedAt"] = finished_at
    if error:
        job["status"] = "failed"
        job["error"] = error
        job["result"] = None
        debug_log(f"JOB_FAILED job_id={job_id} error={error}")
    else:
        job["status"] = "completed"
        job["error"] = None
        job["result"] = payload
        debug_log(
            f"JOB_COMPLETED job_id={job_id} "
            f"thread_id={(payload or {}).get('threadId')}"
        )
    write_json(job_path, job)
    return 0 if not error else 1


# ── MCP tool definitions ─────────────────────────────────────────────────

COMMON_PARAMS: dict[str, dict[str, Any]] = {
    "model": {
        "type": "string",
        "description": (
            "Claude model to use (e.g. 'opus', 'sonnet', 'haiku', "
            "or full ID like 'claude-opus-4-6'). "
            "Defaults to CC_AGENT_MODEL env var."
        ),
    },
    "effort": {
        "type": "string",
        "enum": ["low", "medium", "high", "max"],
        "description": "Effort/reasoning depth (low < medium < high < max).",
    },
    "systemPrompt": {
        "type": "string",
        "description": "Custom system prompt for this invocation.",
    },
    "permissionMode": {
        "type": "string",
        "enum": ["default", "plan", "auto", "bypassPermissions"],
        "description": (
            "Permission mode. 'plan' = read-only planning, "
            "'auto' = auto-approve safe actions, "
            "'bypassPermissions' = full access (dangerous). "
            "Defaults to 'auto'."
        ),
    },
    "allowedTools": {
        "type": "string",
        "description": (
            "Space-separated list of CC tools to enable "
            '(e.g. "Bash Edit Read Grep Glob Write"). '
            "Omit for all tools."
        ),
    },
    "workingDirectory": {
        "type": "string",
        "description": "Working directory for the agent.",
    },
    "addDirs": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Additional directories to grant tool access to.",
    },
    "maxBudgetUsd": {
        "type": "number",
        "description": "Maximum dollar spend for this invocation.",
    },
}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "claude",
        "description": (
            "Execute Claude Code agent in non-interactive mode for autonomous "
            "coding tasks. The agent can read/edit files, run commands, search "
            "codebases, and perform multi-step reasoning. Returns structured "
            "JSON with threadId (for follow-up) and response."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The task, question, or instruction for Claude Code.",
                },
                **COMMON_PARAMS,
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "claude_reply",
        "description": (
            "Continue a previous Claude Code session using the threadId "
            "returned from a prior call. Maintains full conversation context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "threadId": {
                    "type": "string",
                    "description": "Session ID from a previous claude/claude_start call.",
                },
                "thread_id": {
                    "type": "string",
                    "description": "Alias of threadId.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Follow-up prompt.",
                },
                **COMMON_PARAMS,
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "claude_start",
        "description": (
            "Start a background Claude Code agent task and return a jobId "
            "immediately. Use claude_status to poll for completion. "
            "Ideal for long-running tasks that exceed MCP timeout (~120s)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The task for Claude Code.",
                },
                **COMMON_PARAMS,
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "claude_reply_start",
        "description": (
            "Start a background follow-up in an existing Claude session. "
            "Returns a jobId for polling via claude_status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "threadId": {
                    "type": "string",
                    "description": "Session ID from a previous call.",
                },
                "thread_id": {
                    "type": "string",
                    "description": "Alias of threadId.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Follow-up prompt.",
                },
                **COMMON_PARAMS,
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "claude_status",
        "description": (
            "Check whether a background Claude agent job has finished. "
            "Returns the full result when complete."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "jobId": {
                    "type": "string",
                    "description": "Background job ID from claude_start.",
                },
                "job_id": {
                    "type": "string",
                    "description": "Alias of jobId.",
                },
                "waitSeconds": {
                    "type": "integer",
                    "description": (
                        "Optional bounded wait (seconds) before returning. "
                        "Server will poll internally up to this duration."
                    ),
                },
            },
            "required": ["jobId"],
        },
    },
    {
        "name": "claude_list_jobs",
        "description": "List all background jobs with their status.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "ping",
        "description": "Test MCP server connection.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Message to echo back.",
                },
            },
        },
    },
    {
        "name": "help",
        "description": "Get Claude Code CLI help information.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


# ── MCP response helpers ─────────────────────────────────────────────────

def tool_success(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [
                {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
            ],
        },
    }


def tool_error(request_id: Any, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"error": message}, ensure_ascii=False),
                }
            ],
            "isError": True,
        },
    }


# ── Extract common args from MCP tool arguments ─────────────────────────

def extract_common_args(args: dict[str, Any]) -> dict[str, Any]:
    """Pull the shared parameters out of an MCP ``arguments`` dict."""
    add_dirs = args.get("addDirs")
    if add_dirs and not isinstance(add_dirs, list):
        add_dirs = None
    budget_raw = args.get("maxBudgetUsd")
    budget = float(budget_raw) if budget_raw else None

    return {
        "model": args.get("model"),
        "effort": args.get("effort"),
        "system_prompt": args.get("systemPrompt"),
        "permission_mode": args.get("permissionMode"),
        "allowed_tools": args.get("allowedTools"),
        "working_directory": args.get("workingDirectory"),
        "add_dirs": add_dirs,
        "max_budget_usd": budget,
    }


# ── MCP request handler ─────────────────────────────────────────────────

def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    request_id = request.get("id")
    method = request.get("method", "")
    params = request.get("params", {})

    debug_log(
        f"REQUEST id={request_id!r} method={method} "
        f"params={json.dumps(params, ensure_ascii=False)[:500]}"
    )

    # Notifications (no id) — just acknowledge
    if request_id is None:
        return None

    # ── Protocol methods ─────────────────────────────────────────────
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}

    if method in {"notifications/initialized", "initialized"}:
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}

    if method == "resources/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"resources": []},
        }

    if method == "resources/templates/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"resourceTemplates": []},
        }

    # ── Tool listing ─────────────────────────────────────────────────
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": TOOL_DEFINITIONS},
        }

    # ── Tool execution ───────────────────────────────────────────────
    if method == "tools/call":
        name = params.get("name", "")
        args: dict[str, Any] = params.get("arguments", {}) or {}

        # ── claude (sync) ────────────────────────────────────────────
        if name == "claude":
            prompt = str(args.get("prompt", ""))
            if not prompt:
                return tool_error(request_id, "prompt is required")
            common = extract_common_args(args)
            payload, error = run_claude_agent(prompt, **common)
            if error:
                return tool_error(request_id, error)
            return tool_success(request_id, payload or {})

        # ── claude_reply (sync) ──────────────────────────────────────
        if name == "claude_reply":
            thread_id = args.get("threadId") or args.get("thread_id")
            if not thread_id:
                return tool_error(
                    request_id, "threadId or thread_id is required"
                )
            prompt = str(args.get("prompt", ""))
            if not prompt:
                return tool_error(request_id, "prompt is required")
            common = extract_common_args(args)
            payload, error = run_claude_agent(
                prompt, session_id=str(thread_id), **common
            )
            if error:
                return tool_error(request_id, error)
            return tool_success(request_id, payload or {})

        # ── claude_start (async) ─────────────────────────────────────
        if name == "claude_start":
            prompt = str(args.get("prompt", ""))
            if not prompt:
                return tool_error(request_id, "prompt is required")
            common = extract_common_args(args)
            payload, error = start_async_agent(prompt, **common)
            if error:
                return tool_error(request_id, error)
            return tool_success(request_id, payload or {})

        # ── claude_reply_start (async) ───────────────────────────────
        if name == "claude_reply_start":
            thread_id = args.get("threadId") or args.get("thread_id")
            if not thread_id:
                return tool_error(
                    request_id, "threadId or thread_id is required"
                )
            prompt = str(args.get("prompt", ""))
            if not prompt:
                return tool_error(request_id, "prompt is required")
            common = extract_common_args(args)
            payload, error = start_async_agent(
                prompt, session_id=str(thread_id), **common
            )
            if error:
                return tool_error(request_id, error)
            return tool_success(request_id, payload or {})

        # ── claude_status ────────────────────────────────────────────
        if name == "claude_status":
            job_id = args.get("jobId") or args.get("job_id")
            if not job_id:
                return tool_error(request_id, "jobId or job_id is required")
            wait_raw = args.get("waitSeconds", 0)
            try:
                wait_seconds = int(wait_raw)
            except (TypeError, ValueError):
                return tool_error(
                    request_id, "waitSeconds must be an integer"
                )
            payload, error = get_job_status(
                str(job_id), wait_seconds=max(wait_seconds, 0)
            )
            if error:
                return tool_error(request_id, error)
            return tool_success(request_id, payload or {})

        # ── claude_list_jobs ─────────────────────────────────────────
        if name == "claude_list_jobs":
            jobs = list_jobs()
            return tool_success(
                request_id,
                {"jobs": jobs, "count": len(jobs)},
            )

        # ── ping ─────────────────────────────────────────────────────
        if name == "ping":
            message = args.get("message", "pong")
            return tool_success(
                request_id,
                {
                    "message": message,
                    "server": SERVER_NAME,
                    "version": SERVER_VERSION,
                    "timestamp": utc_now(),
                },
            )

        # ── help ─────────────────────────────────────────────────────
        if name == "help":
            bin_path = find_claude_bin()
            if not bin_path:
                return tool_error(
                    request_id, f"Claude CLI not found: {CLAUDE_BIN}"
                )
            try:
                result = subprocess.run(
                    [bin_path, "--help"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                text = result.stdout or result.stderr or "No help available"
            except Exception as exc:
                text = f"Failed to get help: {exc}"
            return tool_success(request_id, {"help": text})

        # ── Unknown tool ─────────────────────────────────────────────
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Unknown tool: {name}"},
        }

    # ── Unknown method ───────────────────────────────────────────────
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> None:
    # Background worker mode
    if len(sys.argv) == 3 and sys.argv[1] == "--run-job":
        raise SystemExit(run_async_job(sys.argv[2]))

    debug_log(f"=== {SERVER_NAME} v{SERVER_VERSION} starting ===")
    debug_log(f"claude_bin={CLAUDE_BIN} model={DEFAULT_MODEL or '(default)'}")

    while True:
        try:
            request = read_message()
            if request is None:
                debug_log("EOF — shutting down")
                break
            response = handle_request(request)
            if response is not None:
                send_response(response)
        except Exception:
            debug_log(traceback.format_exc())
            break


if __name__ == "__main__":
    main()
