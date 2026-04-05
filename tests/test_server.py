#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"


def load_server_module():
    spec = importlib.util.spec_from_file_location(
        "claude_code_agent_server",
        SERVER_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ClaudeCodeAgentServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = load_server_module()

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tempdir.name)
        self.state_dir = self.tmp / "state"
        self.debug_log = self.tmp / "debug.log"
        self.fake_claude = self.tmp / "fake_claude.py"
        self._write_fake_claude()

        self.server.CLAUDE_BIN = str(self.fake_claude)
        self.server.STATE_DIR = self.state_dir
        self.server.JOBS_DIR = self.state_dir / "jobs"
        self.server.DEBUG_LOG = self.debug_log
        self.server.DEFAULT_SYNC_TIMEOUT_SEC = 1
        self.server.DEFAULT_TIMEOUT_SEC = 5
        self.server.DEFAULT_RUNTIME_PROFILE = "simple"
        self.server.HEARTBEAT_INTERVAL_SEC = 0.1
        self.server.STATUS_PROGRESS_INTERVAL_SEC = 0.1

        self.env_patch = patch.dict(
            os.environ,
            {
                "CLAUDE_BIN": str(self.fake_claude),
                "CC_AGENT_STATE_DIR": str(self.state_dir),
                "CC_AGENT_DEBUG_LOG": str(self.debug_log),
                "CC_AGENT_SYNC_TIMEOUT_SEC": "1",
                "CC_AGENT_TIMEOUT_SEC": "5",
                "CC_AGENT_RUNTIME_PROFILE": "simple",
                "CC_AGENT_HEARTBEAT_SEC": "0.1",
                "FAKE_CLAUDE_MODE": "success",
                "FAKE_CLAUDE_SLEEP": "0",
                "FAKE_CLAUDE_RESULT": "",
            },
            clear=False,
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.tempdir.cleanup()

    def _write_fake_claude(self) -> None:
        self.fake_claude.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import sys
                import time

                args = sys.argv[1:]
                if "--help" in args:
                    print("fake help")
                    raise SystemExit(0)

                prompt = sys.stdin.read()
                resume_id = None
                output_format = "text"
                permission_mode = "default"
                for index, arg in enumerate(args):
                    if arg == "--resume" and index + 1 < len(args):
                        resume_id = args[index + 1]
                    if arg == "--output-format" and index + 1 < len(args):
                        output_format = args[index + 1]
                    if arg == "--permission-mode" and index + 1 < len(args):
                        permission_mode = args[index + 1]

                stream_mode = output_format == "stream-json"

                def emit(payload):
                    print(json.dumps(payload), flush=True)

                sleep_sec = float(os.environ.get("FAKE_CLAUDE_SLEEP", "0"))
                mode = os.environ.get("FAKE_CLAUDE_MODE", "success")
                if mode == "bad_json":
                    print("not-json")
                    raise SystemExit(0)
                if mode == "stderr_error":
                    print("stderr failure", file=sys.stderr)
                    raise SystemExit(int(os.environ.get("FAKE_CLAUDE_EXIT", "3")))
                if stream_mode:
                    emit({
                        "type": "system",
                        "subtype": "init",
                        "model": "fake-model",
                        "permissionMode": permission_mode,
                    })

                if sleep_sec:
                    time.sleep(sleep_sec)

                if mode == "api_refused":
                    payload = {
                        "is_error": True,
                        "error": "API Error: Unable to connect to API (ECONNREFUSED)",
                    }
                    if stream_mode:
                        payload = {
                            "type": "result",
                            "is_error": True,
                            "result": "API Error: Unable to connect to API (ECONNREFUSED)",
                            "session_id": os.environ.get("FAKE_CLAUDE_SESSION", "fake-session"),
                        }
                        emit(payload)
                    else:
                        print(json.dumps(payload))
                    raise SystemExit(1)
                if mode == "auth_required":
                    payload = {
                        "is_error": True,
                        "error": "Not logged in · Please run /login",
                        "session_id": "auth-session",
                    }
                    if stream_mode:
                        payload = {
                            "type": "result",
                            "is_error": True,
                            "result": "Not logged in · Please run /login",
                            "session_id": "auth-session",
                        }
                        emit(payload)
                    else:
                        print(json.dumps(payload))
                    raise SystemExit(1)

                result_text = os.environ.get("FAKE_CLAUDE_RESULT") or prompt.strip() or "ok"
                if stream_mode:
                    emit({
                        "type": "stream_event",
                        "event": {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "tool_use", "name": "Read"},
                        },
                    })
                    emit({
                        "type": "stream_event",
                        "event": {
                            "type": "content_block_start",
                            "index": 1,
                            "content_block": {"type": "text", "text": ""},
                        },
                    })
                    emit({
                        "type": "stream_event",
                        "event": {
                            "type": "content_block_delta",
                            "index": 1,
                            "delta": {"type": "text_delta", "text": result_text},
                        },
                    })
                    emit({
                        "type": "stream_event",
                        "event": {"type": "message_stop"},
                    })
                    emit({
                        "type": "result",
                        "is_error": False,
                        "result": result_text,
                        "session_id": resume_id or os.environ.get("FAKE_CLAUDE_SESSION", "fake-session"),
                        "duration_ms": 12,
                        "stop_reason": "end_turn",
                        "total_cost_usd": 0.01,
                        "num_turns": 1,
                    })
                else:
                    print(json.dumps({
                        "is_error": False,
                        "result": result_text,
                        "session_id": resume_id or os.environ.get("FAKE_CLAUDE_SESSION", "fake-session"),
                        "duration_ms": 12,
                        "stop_reason": "end_turn",
                        "total_cost_usd": 0.01,
                        "num_turns": 1,
                    }))
                """
            ),
            encoding="utf-8",
        )
        self.fake_claude.chmod(0o755)

    def _tool_payload(self, response: dict) -> dict:
        return json.loads(response["result"]["content"][0]["text"])

    def test_build_command_uses_simple_profile_by_default(self) -> None:
        cmd = self.server.build_command("prompt", tier="readonly")
        self.assertIn("--disable-slash-commands", cmd)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertIn("--mcp-config", cmd)
        self.assertIn('{"mcpServers": {}}', cmd)
        self.assertNotIn("--bare", cmd)

    def test_build_command_adds_bare_for_isolated_profile(self) -> None:
        isolated_cmd = self.server.build_command(
            "prompt",
            tier="readonly",
            runtime_profile="isolated",
        )
        self.assertIn("stream-json", isolated_cmd)
        self.assertIn("--verbose", isolated_cmd)
        self.assertIn("--bare", isolated_cmd)

        integrated_cmd = self.server.build_command(
            "prompt",
            tier="readonly",
            runtime_profile="integrated",
        )
        self.assertNotIn("--bare", integrated_cmd)
        self.assertNotIn("--disable-slash-commands", integrated_cmd)
        self.assertNotIn("--strict-mcp-config", integrated_cmd)
        self.assertNotIn("--mcp-config", integrated_cmd)

        simple_cmd = self.server.build_command(
            "prompt",
            tier="readonly",
            runtime_profile="simple",
        )
        self.assertNotIn("--bare", simple_cmd)
        self.assertIn("--disable-slash-commands", simple_cmd)

        self.assertNotIn("--disable-slash-commands", isolated_cmd)
        self.assertNotIn("--strict-mcp-config", isolated_cmd)
        self.assertNotIn("--mcp-config", isolated_cmd)

    def test_build_command_preserves_zero_budget(self) -> None:
        cmd = self.server.build_command(
            "prompt",
            tier="readonly",
            max_budget_usd=0,
        )
        self.assertIn("--max-budget-usd", cmd)
        self.assertIn("0", cmd)

    def test_extract_common_args_preserves_zero_budget(self) -> None:
        common = self.server.extract_common_args({"maxBudgetUsd": 0})
        self.assertEqual(common["max_budget_usd"], 0.0)

    def test_error_suggestion_ignores_invalid_runtime_profile(self) -> None:
        suggestion = self.server.error_suggestion(
            "auth_required",
            runtime_profile="definitely-invalid",
        )
        self.assertEqual(
            suggestion,
            "Authenticate Claude Code locally before retrying.",
        )

    def test_error_suggestion_preserves_isolated_auth_guidance(self) -> None:
        suggestion = self.server.error_suggestion(
            "auth_required",
            runtime_profile="isolated",
        )
        self.assertIn("ANTHROPIC_API_KEY", suggestion)

    def test_extract_sync_timeout_validates_positive_integer(self) -> None:
        self.assertEqual(
            self.server.extract_sync_timeout({"syncTimeoutSec": "7"}),
            7,
        )
        with self.assertRaisesRegex(ValueError, "positive integer"):
            self.server.extract_sync_timeout({"syncTimeoutSec": 0})
        with self.assertRaisesRegex(ValueError, "positive integer"):
            self.server.extract_sync_timeout({"syncTimeoutSec": "abc"})

    def test_sync_timeout_returns_structured_error(self) -> None:
        os.environ["FAKE_CLAUDE_SLEEP"] = "2"
        response = self.server.handle_request(
            {
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "claude",
                    "arguments": {
                        "prompt": "hello",
                        "tier": "readonly",
                        "syncTimeoutSec": 1,
                    },
                },
            }
        )

        self.assertTrue(response["result"]["isError"])
        payload = self._tool_payload(response)
        self.assertEqual(payload["error"]["kind"], "sync_timeout")
        self.assertIn("claude_start", payload["error"]["suggestion"])

    def test_sync_call_emits_progress_notifications(self) -> None:
        os.environ["FAKE_CLAUDE_SLEEP"] = "0.35"
        notifications: list[tuple[str, dict]] = []

        with patch.object(
            self.server,
            "send_notification",
            side_effect=lambda method, params: notifications.append((method, params)),
        ):
            response = self.server.handle_request(
                {
                    "id": 101,
                    "method": "tools/call",
                    "params": {
                        "_meta": {"progressToken": 77},
                        "name": "claude",
                        "arguments": {
                            "prompt": "hello",
                            "tier": "readonly",
                            "syncTimeoutSec": 1,
                        },
                    },
                }
            )

        payload = self._tool_payload(response)
        self.assertEqual(payload["response"], "hello")
        self.assertGreaterEqual(len(notifications), 3)
        self.assertTrue(
            all(method == "notifications/progress" for method, _ in notifications)
        )
        self.assertTrue(
            all(params["progressToken"] == 77 for _, params in notifications)
        )
        messages = [params["message"] for _, params in notifications]
        self.assertIn("Launching Claude CLI", messages[0])
        self.assertTrue(
            any("Claude session initialized" in message for message in messages)
        )
        self.assertTrue(any("Claude started tool Read" in message for message in messages))
        self.assertTrue(any("Assistant: hello" in message for message in messages))
        self.assertTrue(any("Claude still running" in message for message in messages))
        self.assertIn("Claude completed successfully", messages[-1])

    def test_invalid_tier_does_not_kill_followup_requests(self) -> None:
        bad_response = self.server.handle_request(
            {
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "claude",
                    "arguments": {
                        "prompt": "hello",
                        "tier": "bad-tier",
                    },
                },
            }
        )
        self.assertTrue(bad_response["result"]["isError"])
        bad_payload = self._tool_payload(bad_response)
        self.assertEqual(bad_payload["error"]["kind"], "invalid_configuration")

        ping_response = self.server.handle_request(
            {
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "ping",
                    "arguments": {"message": "still-alive"},
                },
            }
        )
        ping_payload = self._tool_payload(ping_response)
        self.assertEqual(ping_payload["message"], "still-alive")

    def test_claude_reply_resumes_session(self) -> None:
        os.environ["FAKE_CLAUDE_RESULT"] = "resume-ok"
        response = self.server.handle_request(
            {
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "claude_reply",
                    "arguments": {
                        "threadId": "session-123",
                        "prompt": "follow-up",
                        "tier": "readonly",
                        "syncTimeoutSec": 1,
                    },
                },
            }
        )
        payload = self._tool_payload(response)
        self.assertEqual(payload["threadId"], "session-123")
        self.assertEqual(payload["response"], "resume-ok")

    def test_bad_json_falls_back_to_raw_stdout(self) -> None:
        os.environ["FAKE_CLAUDE_MODE"] = "bad_json"
        response = self.server.handle_request(
            {
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "claude",
                    "arguments": {
                        "prompt": "hello",
                        "tier": "readonly",
                    },
                },
            }
        )
        payload = self._tool_payload(response)
        self.assertEqual(payload["response"], "not-json")
        self.assertEqual(payload["stop_reason"], "parse_fallback")

    def test_stderr_error_returns_structured_error(self) -> None:
        os.environ["FAKE_CLAUDE_MODE"] = "stderr_error"
        response = self.server.handle_request(
            {
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "claude",
                    "arguments": {
                        "prompt": "hello",
                        "tier": "readonly",
                    },
                },
            }
        )
        self.assertTrue(response["result"]["isError"])
        payload = self._tool_payload(response)
        self.assertEqual(payload["error"]["kind"], "claude_exit_nonzero")
        self.assertEqual(payload["error"]["exitCode"], 3)
        self.assertIn("stderr failure", payload["error"]["stderrSnippet"])

    def test_async_job_reports_phase_heartbeat_and_log_path(self) -> None:
        os.environ["FAKE_CLAUDE_SLEEP"] = "1"
        payload, error = self.server.start_async_agent(
            "async-ok",
            tier="readonly",
            runtime_profile="isolated",
        )
        self.assertIsNone(error)
        job_id = payload["jobId"]

        time.sleep(0.25)
        running, status_error = self.server.get_job_status(job_id, wait_seconds=0)
        self.assertIsNone(status_error)
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["phase"], "running")
        self.assertIsNotNone(running["childPid"])
        self.assertIsNotNone(running["lastHeartbeatAt"])
        self.assertEqual(running["runtimeProfile"], "isolated")
        self.assertTrue(Path(running["logPath"]).exists())
        self.assertIn("--bare", running["startedCommand"])
        self.assertIn("stream-json", running["startedCommand"])

        completed, status_error = self.server.get_job_status(job_id, wait_seconds=5)
        self.assertIsNone(status_error)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["phase"], "completed")
        self.assertEqual(completed["response"], "async-ok")
        self.assertEqual(completed["runtimeProfile"], "isolated")
        self.assertIsNotNone(completed["lastProgressMessage"])

        job_log = Path(completed["logPath"]).read_text(encoding="utf-8")
        self.assertIn("HEARTBEAT", job_log)
        self.assertIn("PROGRESS Claude session initialized", job_log)
        self.assertIn("JOB_COMPLETED", job_log)

    def test_async_job_failure_persists_structured_error(self) -> None:
        os.environ["FAKE_CLAUDE_MODE"] = "api_refused"
        payload, error = self.server.start_async_agent(
            "async-fail",
            tier="readonly",
        )
        self.assertIsNone(error)

        final_status, status_error = self.server.get_job_status(
            payload["jobId"],
            wait_seconds=5,
        )
        self.assertIsNone(status_error)
        self.assertEqual(final_status["status"], "failed")
        self.assertEqual(final_status["phase"], "failed")
        self.assertEqual(final_status["error"]["kind"], "api_connection_refused")

    def test_claude_status_emits_progress_and_exposes_latest_log_line(self) -> None:
        os.environ["FAKE_CLAUDE_SLEEP"] = "1"
        payload, error = self.server.start_async_agent("async-progress", tier="readonly")
        self.assertIsNone(error)
        job_id = payload["jobId"]
        notifications: list[tuple[str, dict]] = []

        with patch.object(
            self.server,
            "send_notification",
            side_effect=lambda method, params: notifications.append((method, params)),
        ):
            status_response = self.server.handle_request(
                {
                    "id": 102,
                    "method": "tools/call",
                    "params": {
                        "_meta": {"progressToken": "job-progress"},
                        "name": "claude_status",
                        "arguments": {
                            "jobId": job_id,
                            "waitSeconds": 1,
                        },
                    },
                }
            )

        status_payload = self._tool_payload(status_response)
        self.assertIn(status_payload["status"], {"running", "completed"})
        self.assertIsNotNone(status_payload["latestLogLine"])
        self.assertIsNotNone(status_payload["lastProgressMessage"])
        self.assertTrue(status_payload["stdoutPath"].endswith(".stdout.log"))
        self.assertTrue(status_payload["stderrPath"].endswith(".stderr.log"))
        self.assertGreaterEqual(len(notifications), 1)
        self.assertTrue(
            all(params["progressToken"] == "job-progress" for _, params in notifications)
        )
        self.assertTrue(
            any("Job " in params["message"] for _, params in notifications)
        )
        self.assertTrue(
            any("Claude session initialized" in params["message"] for _, params in notifications)
        )

    def test_claude_reply_start_and_list_jobs(self) -> None:
        response = self.server.handle_request(
            {
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "claude_reply_start",
                    "arguments": {
                        "threadId": "session-async-123",
                        "prompt": "follow-up",
                        "tier": "readonly",
                    },
                },
            }
        )
        started = self._tool_payload(response)
        job_id = started["jobId"]

        status_response = self.server.handle_request(
            {
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "claude_status",
                    "arguments": {
                        "jobId": job_id,
                        "waitSeconds": 5,
                    },
                },
            }
        )
        status_payload = self._tool_payload(status_response)
        self.assertTrue(status_payload["done"])
        self.assertEqual(status_payload["threadId"], "session-async-123")
        self.assertEqual(status_payload["runtimeProfile"], "simple")

        list_response = self.server.handle_request(
            {
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": "claude_list_jobs",
                    "arguments": {},
                },
            }
        )
        jobs_payload = self._tool_payload(list_response)
        self.assertGreaterEqual(jobs_payload["count"], 1)
        self.assertEqual(jobs_payload["jobs"][0]["jobId"], job_id)
        self.assertIn("latestLogLine", jobs_payload["jobs"][0])

    def test_partial_log_paths_are_rejected(self) -> None:
        payload, error = self.server.run_claude_agent(
            "hello",
            tier="readonly",
            stdout_path=self.tmp / "stdout.log",
        )
        self.assertIsNone(payload)
        assert error is not None
        self.assertEqual(error["kind"], "invalid_configuration")
        self.assertIn("stdout_path and stderr_path", error["message"])

    def test_isolated_auth_failure_has_specific_guidance(self) -> None:
        os.environ["FAKE_CLAUDE_MODE"] = "auth_required"
        payload, error = self.server.run_claude_agent(
            "needs-auth",
            tier="readonly",
            runtime_profile="isolated",
            timeout_sec=5,
        )
        self.assertIsNone(payload)
        assert error is not None
        self.assertEqual(error["kind"], "auth_required")
        self.assertIn("ANTHROPIC_API_KEY", error["suggestion"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
