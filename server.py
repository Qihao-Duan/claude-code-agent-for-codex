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
import queue
import shutil
import signal
import shlex
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# ── Configuration via environment ────────────────────────────────────────
SERVER_NAME = "claude-code-agent-for-codex"
SERVER_VERSION = "2.2.0"

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
DEFAULT_MODEL = os.environ.get("CC_AGENT_MODEL", "")
DEFAULT_EFFORT = os.environ.get("CC_AGENT_EFFORT", "")
DEFAULT_SYSTEM_PROMPT = os.environ.get("CC_AGENT_SYSTEM_PROMPT", "")
DEFAULT_TIER = os.environ.get("CC_AGENT_DEFAULT_TIER", "edit")
DEFAULT_TIMEOUT_SEC = int(os.environ.get("CC_AGENT_TIMEOUT_SEC", "900"))
DEFAULT_SYNC_TIMEOUT_SEC = int(os.environ.get("CC_AGENT_SYNC_TIMEOUT_SEC", "90"))
DEFAULT_MAX_BUDGET = os.environ.get("CC_AGENT_MAX_BUDGET_USD", "")
DEFAULT_RUNTIME_PROFILE = os.environ.get("CC_AGENT_RUNTIME_PROFILE", "simple")
HEARTBEAT_INTERVAL_SEC = float(os.environ.get("CC_AGENT_HEARTBEAT_SEC", "5"))
STATUS_PROGRESS_INTERVAL_SEC = float(
    os.environ.get("CC_AGENT_STATUS_PROGRESS_SEC", "2")
)
STREAM_TEXT_PROGRESS_INTERVAL_SEC = float(
    os.environ.get("CC_AGENT_STREAM_TEXT_PROGRESS_SEC", "1")
)
STREAM_TEXT_PROGRESS_MIN_CHARS = int(
    os.environ.get("CC_AGENT_STREAM_TEXT_PROGRESS_MIN_CHARS", "48")
)
EMPTY_MCP_CONFIG = json.dumps({"mcpServers": {}})

# ── Permission Tiers ────────────────────────────────────────────────────
#
# Modeled on CC's auto-mode classifier (8 allow rules, 25 block rules).
# Each tier combines --permission-mode, --tools (whitelist), and
# --disallowedTools (blacklist) for defense-in-depth.
#
#   readonly < explore < edit < full < unrestricted
#
# In -p (non-interactive) mode, anything the auto classifier blocks
# silently fails — the agent adapts.  The tiers add an OUTER ring of
# protection so the agent never even *sees* the dangerous tools.

PERMISSION_TIERS: dict[str, dict[str, Any]] = {
    "readonly": {
        "description": (
            "Read-only analysis. No file edits, no shell commands. "
            "Use for: code review, architecture analysis, questions."
        ),
        "permission_mode": "plan",
        "tools": "Read,Grep,Glob",
        "disallowed_tools": [],
    },
    "explore": {
        "description": (
            "Read + safe shell commands. No file modifications. "
            "Use for: investigation, debugging, log analysis, git status."
        ),
        "permission_mode": "auto",
        "tools": "Read,Grep,Glob,Bash",
        "disallowed_tools": [
            # No destructive shell commands
            "Bash(rm -rf *)",
            "Bash(rm -r *)",
            "Bash(rmdir *)",
            "Bash(sudo *)",
            "Bash(kill -9 *)",
            "Bash(chmod 777 *)",
        ],
    },
    "edit": {
        "description": (
            "Full coding with safety guardrails. DEFAULT tier. "
            "All tools enabled, destructive patterns denied. "
            "CC auto-mode classifier + deny list = two-layer defense."
        ),
        "permission_mode": "auto",
        "tools": None,  # all tools (CC default)
        "disallowed_tools": [
            # ── Irreversible file destruction ─────────────────────
            "Bash(rm -rf *)",
            "Bash(rm -r *)",
            # ── Privilege escalation ──────────────────────────────
            "Bash(sudo *)",
            # ── Destructive git (Codex should own git workflow) ───
            "Bash(git push --force *)",
            "Bash(git push -f *)",
            "Bash(git reset --hard *)",
            "Bash(git clean -f *)",
            "Bash(git branch -D *)",
            # ── Security weakening ────────────────────────────────
            "Bash(chmod 777 *)",
            # ── Disk-level / system-level ─────────────────────────
            "Bash(mkfs *)",
            "Bash(dd *)",
            "Bash(kill -9 *)",
        ],
    },
    "full": {
        "description": (
            "CC auto-mode classifier is the ONLY safety layer. "
            "No additional tool restrictions from this server. "
            "Use for: complex tasks that need maximum flexibility."
        ),
        "permission_mode": "auto",
        "tools": None,
        "disallowed_tools": [],
    },
    "unrestricted": {
        "description": (
            "Bypass ALL permission checks. SANDBOX ENVIRONMENTS ONLY. "
            "Equivalent to --dangerously-skip-permissions."
        ),
        "permission_mode": "bypassPermissions",
        "tools": None,
        "disallowed_tools": [],
    },
}

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
MCP_STDIN = sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin
MCP_STDOUT = sys.stdout.buffer if hasattr(sys.stdout, "buffer") else sys.stdout

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


def configure_binary_stdio() -> None:
    global MCP_STDIN, MCP_STDOUT
    MCP_STDOUT = os.fdopen(sys.stdout.fileno(), "wb", buffering=0)
    MCP_STDIN = os.fdopen(sys.stdin.fileno(), "rb", buffering=0)


SEND_LOCK = threading.Lock()


def clip_text(text: str, limit: int = 500) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit]


def build_error(
    kind: str,
    message: str,
    *,
    exit_code: int | None = None,
    stderr: str = "",
    thread_id: str | None = None,
    suggestion: str | None = None,
    started_command: str | None = None,
) -> dict[str, Any]:
    error = {
        "kind": kind,
        "message": message,
        "exitCode": exit_code,
        "stderrSnippet": clip_text(stderr),
        "threadId": thread_id,
    }
    if suggestion:
        error["suggestion"] = suggestion
    if started_command:
        error["startedCommand"] = started_command
    return error


def normalize_error(error: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(error, dict):
        payload = dict(error)
        payload.setdefault("kind", "tool_error")
        payload.setdefault("message", "Tool call failed")
        payload.setdefault("exitCode", None)
        payload.setdefault("stderrSnippet", None)
        payload.setdefault("threadId", None)
        return payload
    return build_error("tool_error", str(error))


def classify_error_kind(message: str, *, exit_code: int | None = None) -> str:
    upper_message = message.upper()
    lower_message = message.lower()
    if "not logged in" in lower_message or "please run /login" in lower_message:
        return "auth_required"
    if "ECONNREFUSED" in upper_message:
        return "api_connection_refused"
    if "timed out" in lower_message or "timeout" in lower_message:
        return "timeout"
    if "not found" in lower_message and "claude" in lower_message:
        return "claude_cli_not_found"
    if exit_code not in (None, 0):
        return "claude_exit_nonzero"
    return "claude_error"


def error_suggestion(
    kind: str,
    *,
    runtime_profile: str | None = None,
) -> str | None:
    if kind == "sync_timeout":
        return "Retry with claude_start/claude_status for long-running work."
    if kind == "stdin_delivery_failed":
        return (
            "Claude CLI exited before it consumed the prompt. Inspect stderr/logs "
            "and try `claude -p` directly if the failure is persistent."
        )
    if kind == "api_connection_refused":
        return "Retry later or run `claude -p` directly to verify Claude CLI/API health."
    if kind == "auth_required":
        # Suggestion helpers should never raise while formatting an existing
        # error payload for the caller.
        try:
            resolved_runtime_profile = validate_runtime_profile(runtime_profile)
        except ValueError:
            resolved_runtime_profile = None
        if resolved_runtime_profile == "isolated":
            return (
                "Isolated mode uses --bare and does not read local OAuth/keychain state. "
                "Provide ANTHROPIC_API_KEY or apiKeyHelper, or switch back to simple "
                "or integrated."
            )
        return "Authenticate Claude Code locally before retrying."
    return None


# ── MCP transport (Content-Length framing + NDJSON fallback) ─────────────

def send_response(response: dict[str, Any]) -> None:
    global _use_ndjson
    payload = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    debug_log(f"SEND {payload.decode('utf-8', errors='replace')[:2000]}")
    with SEND_LOCK:
        if _use_ndjson:
            MCP_STDOUT.write(payload + b"\n")
        else:
            header = f"Content-Length: {len(payload)}\r\n\r\n".encode("utf-8")
            MCP_STDOUT.write(header + payload)
        MCP_STDOUT.flush()


def send_notification(method: str, params: dict[str, Any]) -> None:
    send_response(
        {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
    )


def extract_progress_token(params: dict[str, Any]) -> str | int | None:
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None
    token = meta.get("progressToken")
    if isinstance(token, bool):
        return None
    if isinstance(token, (str, int)):
        return token
    return None


def make_progress_notifier(
    progress_token: str | int | None,
) -> Callable[[str], None] | None:
    if progress_token is None:
        return None

    progress_value = 0

    def notify(message: str) -> None:
        nonlocal progress_value
        send_notification(
            "notifications/progress",
            {
                "progressToken": progress_token,
                "progress": progress_value,
                "message": clip_text(message, limit=400),
            },
        )
        progress_value += 1

    return notify


def read_message() -> dict[str, Any] | None:
    global _use_ndjson
    line = MCP_STDIN.readline()
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
            header_line = MCP_STDIN.readline()
            if not header_line:
                return None
            if header_line in {b"\r\n", b"\n"}:
                break
        body = MCP_STDIN.read(content_length)
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


def parse_json_line(raw_line: str) -> dict[str, Any] | None:
    candidate = raw_line.strip()
    if not candidate:
        return None
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def parse_claude_output(raw_stdout: str) -> tuple[dict[str, Any] | None, str | None]:
    """Extract the terminal payload from Claude stdout.

    Supports both the legacy single-JSON-object mode and the newer
    ``--output-format stream-json`` event stream.
    """
    lines = [ln.strip() for ln in raw_stdout.splitlines() if ln.strip()]
    if not lines:
        return None, "Claude CLI returned empty output"
    final_result: dict[str, Any] | None = None
    for candidate in reversed(lines):
        payload = parse_json_line(candidate)
        if payload is None:
            continue
        if payload.get("type") == "result":
            final_result = payload
            break
        if any(key in payload for key in ("result", "error", "is_error")):
            final_result = payload
            break
    if final_result is not None:
        return final_result, None
    return None, "Claude CLI did not return valid JSON"


def extract_message_text(message: dict[str, Any] | None) -> str | None:
    if not isinstance(message, dict):
        return None
    text_parts: list[str] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text_value = block.get("text")
        if isinstance(text_value, str):
            text_parts.append(text_value)
    joined = "".join(text_parts)
    return clip_text(" ".join(joined.split()), limit=160)


def flush_text_progress(stream_state: dict[str, Any]) -> str | None:
    buffered = str(stream_state.get("text_buffer") or "")
    snippet = clip_text(" ".join(buffered.split()), limit=160)
    stream_state["text_buffer"] = ""
    if snippet:
        stream_state["last_text_emit_at"] = time.monotonic()
        return f"Assistant: {snippet}"
    return None


def summarize_stream_payload(
    payload: dict[str, Any],
    stream_state: dict[str, Any],
) -> str | None:
    payload_type = payload.get("type")
    if payload_type == "system":
        if payload.get("subtype") == "init":
            model = payload.get("model") or "unknown-model"
            permission_mode = payload.get("permissionMode") or "unknown"
            return (
                "Claude session initialized "
                f"(model={model}, permission={permission_mode})"
            )
        return None
    if payload_type == "assistant":
        assistant_text = extract_message_text(payload.get("message"))
        if assistant_text:
            stream_state["text_buffer"] = ""
            return f"Assistant: {assistant_text}"
        return None
    if payload_type != "stream_event":
        return None

    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    event_type = event.get("type")
    if event_type == "content_block_start":
        content_block = event.get("content_block")
        if not isinstance(content_block, dict):
            return None
        if content_block.get("type") == "tool_use":
            tool_name = content_block.get("name") or "tool"
            return f"Claude started tool {tool_name}"
        return None
    if event_type == "content_block_delta":
        delta = event.get("delta")
        if not isinstance(delta, dict):
            return None
        if delta.get("type") != "text_delta":
            return None
        text_delta = str(delta.get("text") or "")
        if not text_delta:
            return None
        stream_state["text_buffer"] = f"{stream_state.get('text_buffer', '')}{text_delta}"
        normalized = " ".join(str(stream_state.get("text_buffer") or "").split())
        if not normalized:
            return None
        now = time.monotonic()
        if (
            "\n" not in text_delta
            and len(normalized) < STREAM_TEXT_PROGRESS_MIN_CHARS
            and now - float(stream_state.get("last_text_emit_at") or 0.0)
            < STREAM_TEXT_PROGRESS_INTERVAL_SEC
        ):
            return None
        stream_state["text_buffer"] = ""
        stream_state["last_text_emit_at"] = now
        return f"Assistant: {clip_text(normalized, limit=160)}"
    if event_type in {"content_block_stop", "message_stop"}:
        return flush_text_progress(stream_state)
    return None


def validate_runtime_profile(runtime_profile: str | None) -> str:
    profile = runtime_profile or DEFAULT_RUNTIME_PROFILE
    if profile not in {"simple", "integrated", "isolated"}:
        raise ValueError(
            f"Unknown runtimeProfile '{profile}'. "
            "Valid: simple, integrated, isolated"
        )
    return profile


def prepare_run_stream_paths(run_id: str | None = None) -> tuple[Path, Path]:
    run_token = run_id or uuid.uuid4().hex
    runs_dir = STATE_DIR / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    return (
        runs_dir / f"{run_token}.stdout.log",
        runs_dir / f"{run_token}.stderr.log",
    )


def remove_path_if_exists(path: Path | None) -> None:
    if not path:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        debug_log(f"Failed to remove temp file: {path}")


def terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        proc.terminate()
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        debug_log(f"Failed to reap Claude process pid={proc.pid}")


def execute_claude_command(
    cmd: list[str],
    *,
    prompt: str,
    working_directory: str | None,
    env: dict[str, str],
    timeout_sec: int,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    keep_logs: bool = False,
    on_start: Any = None,
    on_heartbeat: Any = None,
    on_stdout_line: Callable[[str], None] | None = None,
    on_stderr_line: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if (stdout_path is None) != (stderr_path is None):
        raise ValueError("stdout_path and stderr_path must be provided together")

    started_command = shlex.join(cmd)
    owned_paths = stdout_path is None and stderr_path is None
    stdout_file_path = stdout_path
    stderr_file_path = stderr_path
    if stdout_file_path is None or stderr_file_path is None:
        stdout_file_path, stderr_file_path = prepare_run_stream_paths()

    stdin_write_error = False
    timed_out = False
    returncode: int | None = None
    proc: subprocess.Popen[str] | None = None
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stream_events: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
    stdout_thread: threading.Thread | None = None
    stderr_thread: threading.Thread | None = None

    def reader_thread(
        pipe: Any,
        output_handle: Any,
        sink: list[str],
        stream_name: str,
    ) -> None:
        if pipe is None:
            return
        try:
            while True:
                line = pipe.readline()
                if line == "":
                    break
                sink.append(line)
                output_handle.write(line)
                output_handle.flush()
                stream_events.put((stream_name, line))
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    def drain_stream_events() -> None:
        while True:
            try:
                stream_name, line = stream_events.get_nowait()
            except queue.Empty:
                break
            stripped = line.rstrip("\r\n")
            if stream_name == "stdout" and on_stdout_line:
                on_stdout_line(stripped)
            elif stream_name == "stderr" and on_stderr_line:
                on_stderr_line(stripped)

    try:
        with (
            stdout_file_path.open("w", encoding="utf-8") as stdout_file,
            stderr_file_path.open("w", encoding="utf-8") as stderr_file,
        ):
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=working_directory or None,
                env=env,
                start_new_session=True,
            )
            if on_start:
                on_start(proc.pid, started_command)

            stdout_thread = threading.Thread(
                target=reader_thread,
                args=(proc.stdout, stdout_file, stdout_chunks, "stdout"),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=reader_thread,
                args=(proc.stderr, stderr_file, stderr_chunks, "stderr"),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()

            if proc.stdin is not None:
                try:
                    proc.stdin.write(prompt)
                except BrokenPipeError:
                    stdin_write_error = True
                finally:
                    try:
                        proc.stdin.close()
                    except BrokenPipeError:
                        stdin_write_error = True

            started_monotonic = time.monotonic()
            last_heartbeat = started_monotonic
            while True:
                drain_stream_events()
                returncode = proc.poll()
                now = time.monotonic()
                if returncode is not None:
                    break
                if timeout_sec > 0 and now - started_monotonic >= timeout_sec:
                    timed_out = True
                    terminate_process(proc)
                    break
                if on_heartbeat and now - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
                    on_heartbeat(proc.pid)
                    last_heartbeat = now
                time.sleep(0.1)

            if returncode is None:
                try:
                    returncode = proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    debug_log(
                        f"Claude process pid={proc.pid} did not exit cleanly "
                        f"after timeout handling"
                    )
                    returncode = proc.poll()
            if stdout_thread is not None:
                stdout_thread.join(timeout=1)
            if stderr_thread is not None:
                stderr_thread.join(timeout=1)
            drain_stream_events()
    finally:
        stdout_text = "".join(stdout_chunks)
        stderr_text = "".join(stderr_chunks)
        if not keep_logs and owned_paths:
            remove_path_if_exists(stdout_file_path)
            remove_path_if_exists(stderr_file_path)

    return {
        "returncode": returncode,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "timed_out": timed_out,
        "stdin_write_error": stdin_write_error,
        "child_pid": proc.pid if proc else None,
        "started_command": started_command,
        "stdout_path": str(stdout_file_path),
        "stderr_path": str(stderr_file_path),
    }


def resolve_tier(
    tier: str | None,
    *,
    permission_mode_override: str | None = None,
    allowed_tools_override: str | None = None,
    disallowed_tools_override: list[str] | None = None,
) -> dict[str, Any]:
    """Resolve a tier name + optional overrides into CLI flags.

    Priority: explicit parameter overrides > tier defaults > global defaults.
    """
    tier_name = tier or DEFAULT_TIER
    if tier_name not in PERMISSION_TIERS:
        raise ValueError(
            f"Unknown tier '{tier_name}'. "
            f"Valid: {', '.join(PERMISSION_TIERS)}"
        )
    tier_cfg = PERMISSION_TIERS[tier_name]

    return {
        "permission_mode": permission_mode_override or tier_cfg["permission_mode"],
        "tools": tier_cfg.get("tools"),  # whitelist (None = all)
        "disallowed_tools": (
            disallowed_tools_override
            if disallowed_tools_override is not None
            else tier_cfg.get("disallowed_tools", [])
        ),
        "allowed_tools": allowed_tools_override,
        "tier_name": tier_name,
    }


def build_command(
    prompt: str,
    *,
    session_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    system_prompt: str | None = None,
    tier: str | None = None,
    permission_mode: str | None = None,
    allowed_tools: str | None = None,
    disallowed_tools: list[str] | None = None,
    working_directory: str | None = None,
    add_dirs: list[str] | None = None,
    max_budget_usd: float | None = None,
    runtime_profile: str | None = None,
) -> list[str]:
    bin_path = find_claude_bin()
    if not bin_path:
        raise FileNotFoundError(f"Claude CLI not found: {CLAUDE_BIN}")

    # ── Resolve tier into concrete CLI flags ─────────────────────────
    resolved = resolve_tier(
        tier,
        permission_mode_override=permission_mode,
        allowed_tools_override=allowed_tools,
        disallowed_tools_override=disallowed_tools,
    )
    selected_runtime_profile = validate_runtime_profile(runtime_profile)

    cmd: list[str] = [
        bin_path,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
    ]
    if selected_runtime_profile == "isolated":
        cmd.append("--bare")
    elif selected_runtime_profile == "simple":
        cmd.extend(
            [
                "--disable-slash-commands",
                "--strict-mcp-config",
                "--mcp-config",
                EMPTY_MCP_CONFIG,
            ]
        )

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

    # Permission mode (from tier resolution)
    if resolved["permission_mode"]:
        cmd.extend(["--permission-mode", resolved["permission_mode"]])

    # Tools whitelist (from tier — restricts which built-in tools exist)
    if resolved["tools"]:
        cmd.extend(["--tools", resolved["tools"]])

    # Allowed tools (explicit override — auto-approve these without prompt)
    if resolved["allowed_tools"]:
        cmd.extend(["--allowedTools", resolved["allowed_tools"]])

    # Disallowed tools (from tier — removes dangerous patterns entirely)
    # Passed via --settings JSON because --disallowedTools is variadic and
    # would consume the trailing prompt argument.  Complex patterns like
    # "Bash(rm -rf *)" also contain spaces that break CLI space-splitting.
    deny_patterns = resolved.get("disallowed_tools") or []
    if deny_patterns:
        settings_obj = {"permissions": {"deny": deny_patterns}}
        cmd.extend(["--settings", json.dumps(settings_obj)])

    # Working directory
    if working_directory:
        cmd.extend(["--add-dir", working_directory])

    # Additional directories
    if add_dirs:
        for d in add_dirs:
            cmd.extend(["--add-dir", d])

    # Budget
    default_budget = (
        float(DEFAULT_MAX_BUDGET)
        if DEFAULT_MAX_BUDGET not in (None, "")
        else None
    )
    budget = max_budget_usd if max_budget_usd is not None else default_budget
    if budget is not None:
        cmd.extend(["--max-budget-usd", str(budget)])

    # NOTE: prompt is NOT appended here — it is passed via stdin in
    # run_claude_agent() to avoid being consumed by variadic CLI flags
    # like --tools <tools...> or --disallowedTools <tools...>.
    return cmd


def run_claude_agent(
    prompt: str,
    *,
    session_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    system_prompt: str | None = None,
    tier: str | None = None,
    permission_mode: str | None = None,
    allowed_tools: str | None = None,
    disallowed_tools: list[str] | None = None,
    working_directory: str | None = None,
    add_dirs: list[str] | None = None,
    max_budget_usd: float | None = None,
    runtime_profile: str | None = None,
    timeout_sec: int | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    keep_logs: bool = False,
    on_start: Any = None,
    on_heartbeat: Any = None,
    progress_callback: Callable[[str], None] | None = None,
    on_stream_summary: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Run ``claude -p`` synchronously and return structured result."""
    try:
        cmd = build_command(
            prompt,
            session_id=session_id,
            model=model,
            effort=effort,
            system_prompt=system_prompt,
            tier=tier,
            permission_mode=permission_mode,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            working_directory=working_directory,
            add_dirs=add_dirs,
            max_budget_usd=max_budget_usd,
            runtime_profile=runtime_profile,
        )
    except (FileNotFoundError, ValueError) as exc:
        return None, build_error("invalid_configuration", str(exc))

    started_command = shlex.join(cmd)
    debug_log(f"RUN {started_command}")

    env = os.environ.copy()
    if working_directory:
        env["PWD"] = working_directory

    timeout_value = timeout_sec if timeout_sec is not None else DEFAULT_TIMEOUT_SEC
    started_monotonic = time.monotonic()
    stream_state: dict[str, Any] = {
        "text_buffer": "",
        "last_text_emit_at": 0.0,
    }

    def emit_progress(message: str) -> None:
        if progress_callback:
            progress_callback(message)

    def emit_stream_summary(message: str | None) -> None:
        if not message:
            return
        if on_stream_summary:
            on_stream_summary(message)
        emit_progress(message)

    def handle_start(child_pid: int, child_command: str) -> None:
        if on_start:
            on_start(child_pid, child_command)
        emit_progress(f"Claude started (pid={child_pid})")

    def handle_heartbeat(child_pid: int) -> None:
        if on_heartbeat:
            on_heartbeat(child_pid)
        elapsed = max(int(time.monotonic() - started_monotonic), 0)
        emit_progress(f"Claude still running after {elapsed}s (pid={child_pid})")

    def handle_stdout_line(raw_line: str) -> None:
        payload = parse_json_line(raw_line)
        if payload is None:
            return
        emit_stream_summary(summarize_stream_payload(payload, stream_state))

    emit_progress("Launching Claude CLI")

    try:
        result = execute_claude_command(
            cmd,
            prompt=prompt,
            working_directory=working_directory,
            env=env,
            timeout_sec=timeout_value,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            keep_logs=keep_logs,
            on_start=handle_start,
            on_heartbeat=handle_heartbeat,
            on_stdout_line=handle_stdout_line,
        )
    except ValueError as exc:
        return None, build_error(
            "invalid_configuration",
            str(exc),
            started_command=started_command,
        )
    except OSError as exc:
        return None, build_error(
            "claude_launch_failed",
            f"Failed to launch Claude CLI: {exc}",
            started_command=started_command,
        )

    if result["timed_out"]:
        emit_progress(
            f"Claude timed out after {timeout_value}s; switching to error response"
        )
        return None, build_error(
            "sync_timeout",
            f"Claude agent timed out after {timeout_value}s",
            stderr=result["stderr"],
            started_command=started_command,
            suggestion=error_suggestion("sync_timeout"),
        )

    emit_stream_summary(flush_text_progress(stream_state))

    payload, parse_error = parse_claude_output(result["stdout"])
    if parse_error:
        emit_progress("Claude process exited; parsing output")
        stderr = result["stderr"].strip()
        raw_stdout = result["stdout"].strip()
        raw_text = raw_stdout or stderr
        if result["returncode"] not in (None, 0):
            if result["stdin_write_error"] and not raw_text:
                kind = "stdin_delivery_failed"
                message = "Claude CLI exited before reading the prompt"
            else:
                message = raw_text or parse_error
                kind = classify_error_kind(message, exit_code=result["returncode"])
            return None, build_error(
                kind,
                message,
                exit_code=result["returncode"],
                stderr=result["stderr"],
                started_command=started_command,
                suggestion=error_suggestion(kind, runtime_profile=runtime_profile),
            )
        if result["stdin_write_error"]:
            return None, build_error(
                "stdin_delivery_failed",
                raw_text or "Claude CLI exited before reading the prompt",
                exit_code=result["returncode"],
                stderr=result["stderr"],
                started_command=started_command,
                suggestion=error_suggestion("stdin_delivery_failed"),
            )
        if raw_stdout:
            return {
                "threadId": None,
                "response": raw_stdout,
                "model": model or DEFAULT_MODEL,
                "tier": tier or DEFAULT_TIER,
                "runtimeProfile": validate_runtime_profile(runtime_profile),
                "duration_ms": None,
                "stop_reason": "parse_fallback",
            }, None
        msg = parse_error if not stderr else f"{parse_error}. stderr: {stderr}"
        return None, build_error(
            "invalid_claude_output",
            msg,
            exit_code=result["returncode"],
            stderr=result["stderr"],
            started_command=started_command,
        )

    assert payload is not None

    if result["returncode"] != 0 or payload.get("is_error"):
        emit_progress("Claude returned an error payload")
        message = str(
            payload.get("result")
            or payload.get("error")
            or result["stderr"].strip()
            or "Claude agent failed"
        )
        kind = classify_error_kind(message, exit_code=result["returncode"])
        return None, build_error(
            kind,
            message,
            exit_code=result["returncode"],
            stderr=result["stderr"],
            thread_id=payload.get("session_id"),
            started_command=started_command,
            suggestion=error_suggestion(kind, runtime_profile=runtime_profile),
        )

    thread_id = payload.get("session_id")
    response_text = str(payload.get("result", "")).strip()
    model_name = payload.get("model", "") or model or DEFAULT_MODEL
    cost = payload.get("cost_usd") or payload.get("total_cost_usd")

    emit_progress("Claude completed successfully")

    resolved_tier = tier or DEFAULT_TIER
    return {
        "threadId": thread_id,
        "response": response_text,
        "model": model_name,
        "tier": resolved_tier,
        "runtimeProfile": validate_runtime_profile(runtime_profile),
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


def job_log_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.log"


def job_stdout_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.stdout.log"


def job_stderr_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.stderr.log"


def read_last_nonempty_line(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if line.strip():
            return line.strip()
    return None


def append_job_log(job_id: str, message: str) -> None:
    log_path = job_log_path(job_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"[{utc_now()}] {message}\n")


def update_job_file(job_id: str, **changes: Any) -> dict[str, Any]:
    job_path = job_state_path(job_id)
    job = read_json(job_path)
    job.update(changes)
    job["updatedAt"] = utc_now()
    write_json(job_path, job)
    return job


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
    request = job.get("request") or {}
    log_path_value = job.get("logPath")
    latest_log_line = (
        read_last_nonempty_line(Path(log_path_value))
        if isinstance(log_path_value, str) and log_path_value
        else None
    )
    return {
        "jobId": job.get("jobId"),
        "status": job.get("status"),
        "phase": job.get("phase"),
        "done": job.get("status") in TERMINAL_JOB_STATES,
        "threadId": result.get("threadId"),
        "response": result.get("response"),
        "model": result.get("model") or request.get("model"),
        "runtimeProfile": result.get("runtimeProfile") or request.get("runtimeProfile"),
        "duration_ms": result.get("duration_ms"),
        "stop_reason": result.get("stop_reason"),
        "cost_usd": result.get("cost_usd"),
        "num_turns": result.get("num_turns"),
        "error": job.get("error"),
        "createdAt": job.get("createdAt"),
        "startedAt": job.get("startedAt"),
        "completedAt": job.get("completedAt"),
        "lastHeartbeatAt": job.get("lastHeartbeatAt"),
        "lastProgressMessage": job.get("lastProgressMessage"),
        "childPid": job.get("childPid"),
        "logPath": job.get("logPath"),
        "stdoutPath": str(job_stdout_path(job.get("jobId"))),
        "stderrPath": str(job_stderr_path(job.get("jobId"))),
        "latestLogLine": latest_log_line,
        "startedCommand": job.get("startedCommand"),
        "resumeHint": "Call claude_status with this jobId until done=true.",
    }


def format_job_progress_message(job: dict[str, Any]) -> str:
    job_id = str(job.get("jobId") or "unknown")
    phase = job.get("phase") or job.get("status") or "unknown"
    parts = [f"Job {job_id} is {phase}"]
    latest_progress_message = job.get("lastProgressMessage")
    latest_log_line = read_last_nonempty_line(job_log_path(job_id))
    if latest_progress_message:
        parts.append(str(latest_progress_message))
    elif latest_log_line:
        parts.append(latest_log_line)
    return " · ".join(parts)


def start_async_agent(
    prompt: str,
    *,
    session_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    system_prompt: str | None = None,
    tier: str | None = None,
    permission_mode: str | None = None,
    allowed_tools: str | None = None,
    disallowed_tools: list[str] | None = None,
    working_directory: str | None = None,
    add_dirs: list[str] | None = None,
    max_budget_usd: float | None = None,
    runtime_profile: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        resolved_runtime_profile = validate_runtime_profile(runtime_profile)
    except ValueError as exc:
        return None, build_error("invalid_configuration", str(exc))

    job_id = uuid.uuid4().hex
    created_at = utc_now()
    job: dict[str, Any] = {
        "jobId": job_id,
        "status": "queued",
        "phase": "queued",
        "createdAt": created_at,
        "startedAt": None,
        "completedAt": None,
        "updatedAt": created_at,
        "lastHeartbeatAt": None,
        "lastProgressMessage": None,
        "error": None,
        "result": None,
        "workerPid": None,
        "childPid": None,
        "logPath": str(job_log_path(job_id)),
        "startedCommand": None,
        "request": {
            "prompt": prompt,
            "threadId": session_id,
            "model": model,
            "effort": effort,
            "systemPrompt": system_prompt,
            "tier": tier,
            "permissionMode": permission_mode,
            "allowedTools": allowed_tools,
            "disallowedTools": disallowed_tools,
            "workingDirectory": working_directory,
            "addDirs": add_dirs,
            "maxBudgetUsd": max_budget_usd,
            "runtimeProfile": resolved_runtime_profile,
        },
    }

    job_path = job_state_path(job_id)
    write_json(job_path, job)
    append_job_log(job_id, "JOB_QUEUED")

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
        finished_at = utc_now()
        job["status"] = "failed"
        job["phase"] = "failed"
        job["completedAt"] = finished_at
        job["updatedAt"] = finished_at
        job["error"] = build_error(
            "worker_launch_failed",
            f"Failed to launch background worker: {exc}",
        )
        write_json(job_path, job)
        append_job_log(job_id, f"JOB_FAILED error={job['error']['message']}")
        return None, job["error"]

    threading.Thread(target=worker.wait, daemon=True).start()
    job["workerPid"] = worker.pid
    job["updatedAt"] = utc_now()
    write_json(job_path, job)
    append_job_log(job_id, f"JOB_START worker_pid={worker.pid}")
    debug_log(f"JOB_START job_id={job_id} worker_pid={worker.pid}")
    return serialize_job(job), None


def get_job_status(
    job_id: str,
    *,
    wait_seconds: int = 0,
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    job_path = job_state_path(job_id)
    if not job_path.exists():
        return None, build_error("unknown_job", f"Unknown jobId: {job_id}")

    deadline = time.monotonic() + max(wait_seconds, 0)
    last_progress_signature: tuple[Any, ...] | None = None
    last_progress_at = 0.0
    while True:
        job = read_json(job_path)
        if job.get("status") in {
            "queued",
            "running",
        } and not is_pid_alive(job.get("workerPid")):
            job["status"] = "failed"
            job["phase"] = "failed"
            job["error"] = build_error(
                "worker_exited",
                "Background worker exited before completing",
            )
            job["completedAt"] = utc_now()
            job["updatedAt"] = job["completedAt"]
            write_json(job_path, job)
            append_job_log(job_id, "JOB_FAILED error=Background worker exited before completing")
        if progress_callback:
            progress_signature = (
                job.get("status"),
                job.get("phase"),
                job.get("lastHeartbeatAt"),
                job.get("lastProgressMessage"),
                job.get("completedAt"),
                read_last_nonempty_line(job_log_path(job_id)),
            )
            now = time.monotonic()
            if (
                progress_signature != last_progress_signature
                or now - last_progress_at >= STATUS_PROGRESS_INTERVAL_SEC
            ):
                progress_callback(format_job_progress_message(job))
                last_progress_signature = progress_signature
                last_progress_at = now
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
    job["phase"] = "launching"
    job["startedAt"] = utc_now()
    job["updatedAt"] = job["startedAt"]
    job["workerPid"] = os.getpid()
    write_json(job_path, job)
    append_job_log(job_id, f"JOB_RUNNING worker_pid={os.getpid()}")
    debug_log(f"JOB_RUNNING job_id={job_id} pid={os.getpid()}")

    req = job.get("request") or {}
    try:
        add_dirs = req.get("addDirs")
        if add_dirs and not isinstance(add_dirs, list):
            add_dirs = None
        budget_raw = req.get("maxBudgetUsd")
        budget = float(budget_raw) if budget_raw not in (None, "") else None

        disallowed = req.get("disallowedTools")
        if disallowed and not isinstance(disallowed, list):
            disallowed = None
        job_update_lock = threading.Lock()

        def mark_start(child_pid: int, started_command: str) -> None:
            with job_update_lock:
                update_job_file(
                    job_id,
                    phase="running",
                    childPid=child_pid,
                    lastHeartbeatAt=utc_now(),
                    startedCommand=started_command,
                )
                append_job_log(
                    job_id,
                    f"CLAUDE_STARTED child_pid={child_pid} command={started_command}",
                )

        def mark_heartbeat(child_pid: int) -> None:
            with job_update_lock:
                update_job_file(
                    job_id,
                    phase="running",
                    childPid=child_pid,
                    lastHeartbeatAt=utc_now(),
                )
                append_job_log(job_id, f"HEARTBEAT child_pid={child_pid}")

        def record_stream_summary(message: str) -> None:
            with job_update_lock:
                update_job_file(job_id, lastProgressMessage=message)
                append_job_log(job_id, f"PROGRESS {message}")

        update_job_file(job_id, phase="starting_claude")
        append_job_log(job_id, "PHASE starting_claude")

        payload, error = run_claude_agent(
            str(req.get("prompt", "")),
            session_id=req.get("threadId"),
            model=req.get("model"),
            effort=req.get("effort"),
            system_prompt=req.get("systemPrompt"),
            tier=req.get("tier"),
            permission_mode=req.get("permissionMode"),
            allowed_tools=req.get("allowedTools"),
            disallowed_tools=disallowed,
            working_directory=req.get("workingDirectory"),
            add_dirs=add_dirs,
            max_budget_usd=budget,
            runtime_profile=req.get("runtimeProfile"),
            timeout_sec=DEFAULT_TIMEOUT_SEC,
            stdout_path=job_stdout_path(job_id),
            stderr_path=job_stderr_path(job_id),
            keep_logs=True,
            on_start=mark_start,
            on_heartbeat=mark_heartbeat,
            on_stream_summary=record_stream_summary,
        )
        if read_json(job_path).get("childPid"):
            update_job_file(job_id, phase="parsing_output")
            append_job_log(job_id, "PHASE parsing_output")
    except Exception as exc:
        payload = None
        error = build_error("background_crash", f"Background agent crashed: {exc}")
        debug_log(traceback.format_exc())

    finished_at = utc_now()
    job = read_json(job_path)
    job["updatedAt"] = finished_at
    job["completedAt"] = finished_at
    if error:
        job["status"] = "failed"
        job["phase"] = "failed"
        job["error"] = error
        job["result"] = None
        append_job_log(job_id, f"JOB_FAILED error={error['message']}")
        debug_log(f"JOB_FAILED job_id={job_id} error={error}")
    else:
        job["status"] = "completed"
        job["phase"] = "completed"
        job["error"] = None
        job["result"] = payload
        append_job_log(
            job_id,
            f"JOB_COMPLETED thread_id={(payload or {}).get('threadId')}",
        )
        debug_log(
            f"JOB_COMPLETED job_id={job_id} "
            f"thread_id={(payload or {}).get('threadId')}"
        )
    write_json(job_path, job)
    return 0 if not error else 1


# ── MCP tool definitions ─────────────────────────────────────────────────

COMMON_PARAMS: dict[str, dict[str, Any]] = {
    "tier": {
        "type": "string",
        "enum": ["readonly", "explore", "edit", "full", "unrestricted"],
        "description": (
            "Permission tier controlling what CC can do. "
            "readonly: read-only analysis (no edits, no shell). "
            "explore: read + safe shell (no file modifications). "
            "edit: full coding with safety guardrails (DEFAULT). "
            "full: CC auto-mode classifier only, no extra restrictions. "
            "unrestricted: bypass all checks (SANDBOX ONLY). "
            "Explicit permissionMode/allowedTools override tier settings."
        ),
    },
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
            "Override the tier's permission mode. "
            "'plan' = read-only, 'auto' = classifier-based, "
            "'bypassPermissions' = no checks (dangerous)."
        ),
    },
    "allowedTools": {
        "type": "string",
        "description": (
            "Override: space-separated CC tools to auto-approve "
            '(e.g. "Bash(git *) Edit Read").'
        ),
    },
    "disallowedTools": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Override: list of tool patterns to deny entirely "
            '(e.g. ["Bash(rm -rf *)", "Bash(sudo *)"]). '
            "Replaces the tier's deny list."
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
    "runtimeProfile": {
        "type": "string",
        "enum": ["simple", "integrated", "isolated"],
        "description": (
            "Runtime profile. simple (default) keeps local auth but disables "
            "slash commands and inherited MCP servers. integrated inherits "
            "the full local Claude environment. isolated runs with --bare "
            "for a cleaner, more predictable agent environment."
        ),
    },
}

SYNC_ONLY_PARAMS: dict[str, dict[str, Any]] = {
    "syncTimeoutSec": {
        "type": "integer",
        "description": (
            "Timeout for synchronous calls before returning a structured "
            "sync_timeout error. Defaults to CC_AGENT_SYNC_TIMEOUT_SEC."
        ),
    },
}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "claude",
        "description": (
            "Run Claude Code synchronously for short tasks. This path can emit "
            "notifications/progress when the client provides _meta.progressToken, "
            "but it is still bounded by syncTimeoutSec. For long-running work, "
            "prefer claude_start plus claude_status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The task, question, or instruction for Claude Code.",
                },
                **COMMON_PARAMS,
                **SYNC_ONLY_PARAMS,
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "claude_reply",
        "description": (
            "Continue a previous Claude Code session synchronously. Use only "
            "for short follow-ups; long follow-ups should use "
            "claude_reply_start plus claude_status."
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
                **SYNC_ONLY_PARAMS,
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
            "Returns the full result when complete, and can emit "
            "notifications/progress during bounded waits when the client "
            "provides _meta.progressToken."
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
        "name": "tiers",
        "description": (
            "List all available permission tiers with descriptions. "
            "Call this to understand what each tier allows before "
            "choosing one for a task."
        ),
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


def tool_error(request_id: Any, error: str | dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"error": normalize_error(error)},
                        ensure_ascii=False,
                    ),
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
    budget = float(budget_raw) if budget_raw not in (None, "") else None
    disallowed = args.get("disallowedTools")
    if disallowed and not isinstance(disallowed, list):
        disallowed = None

    return {
        "tier": args.get("tier"),
        "model": args.get("model"),
        "effort": args.get("effort"),
        "system_prompt": args.get("systemPrompt"),
        "permission_mode": args.get("permissionMode"),
        "allowed_tools": args.get("allowedTools"),
        "disallowed_tools": disallowed,
        "working_directory": args.get("workingDirectory"),
        "add_dirs": add_dirs,
        "max_budget_usd": budget,
        "runtime_profile": args.get("runtimeProfile"),
    }


def extract_sync_timeout(args: dict[str, Any]) -> int | None:
    raw_value = args.get("syncTimeoutSec")
    if raw_value in (None, ""):
        return None
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("syncTimeoutSec must be a positive integer") from exc
    if value <= 0:
        raise ValueError("syncTimeoutSec must be a positive integer")
    return value


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
        progress_notifier = make_progress_notifier(extract_progress_token(params))

        # ── claude (sync) ────────────────────────────────────────────
        if name == "claude":
            prompt = str(args.get("prompt", ""))
            if not prompt:
                return tool_error(request_id, "prompt is required")
            common = extract_common_args(args)
            try:
                sync_timeout = extract_sync_timeout(args)
            except ValueError as exc:
                return tool_error(request_id, str(exc))
            payload, error = run_claude_agent(
                prompt,
                timeout_sec=sync_timeout or DEFAULT_SYNC_TIMEOUT_SEC,
                progress_callback=progress_notifier,
                **common,
            )
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
            try:
                sync_timeout = extract_sync_timeout(args)
            except ValueError as exc:
                return tool_error(request_id, str(exc))
            payload, error = run_claude_agent(
                prompt,
                session_id=str(thread_id),
                timeout_sec=sync_timeout or DEFAULT_SYNC_TIMEOUT_SEC,
                progress_callback=progress_notifier,
                **common,
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
            if progress_notifier:
                progress_notifier(
                    f"Queued background Claude job {payload.get('jobId')}."
                )
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
            if progress_notifier:
                progress_notifier(
                    f"Queued background Claude follow-up job {payload.get('jobId')}."
                )
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
                str(job_id),
                wait_seconds=max(wait_seconds, 0),
                progress_callback=progress_notifier,
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

        # ── tiers ────────────────────────────────────────────────────
        if name == "tiers":
            tier_info = {
                tier_name: {
                    "description": cfg["description"],
                    "permissionMode": cfg["permission_mode"],
                    "tools": cfg.get("tools") or "all (default)",
                    "denyPatterns": cfg.get("disallowed_tools", []),
                    "isDefault": tier_name == DEFAULT_TIER,
                }
                for tier_name, cfg in PERMISSION_TIERS.items()
            }
            return tool_success(
                request_id,
                {
                    "tiers": tier_info,
                    "defaultTier": DEFAULT_TIER,
                    "hierarchy": "readonly < explore < edit < full < unrestricted",
                },
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

    configure_binary_stdio()
    debug_log(f"=== {SERVER_NAME} v{SERVER_VERSION} starting ===")
    debug_log(f"claude_bin={CLAUDE_BIN} model={DEFAULT_MODEL or '(default)'}")

    while True:
        request: dict[str, Any] | None = None
        try:
            request = read_message()
            if request is None:
                debug_log("EOF — shutting down")
                break
            response = handle_request(request)
            if response is not None:
                send_response(response)
        except Exception as exc:
            debug_log(traceback.format_exc())
            if request and request.get("id") is not None:
                send_response(
                    tool_error(
                        request["id"],
                        build_error("internal_error", f"Unhandled server error: {exc}"),
                    )
                )
                continue
            break


if __name__ == "__main__":
    main()
