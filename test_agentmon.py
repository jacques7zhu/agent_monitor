import json
import os
import tempfile
import unittest

import agentmon as am


def write_jsonl(path, rows, mode="w"):
    with open(path, mode) as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class TmpDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()


class ClaudeTranscriptTest(TmpDirTest):
    def test_usage_dedup_by_request_and_pending_tools(self):
        path = os.path.join(self.tmp, "t.jsonl")
        usage1 = {"input_tokens": 1, "output_tokens": 5, "cache_read_input_tokens": 100,
                  "cache_creation_input_tokens": 10}
        usage2 = dict(usage1, output_tokens=50)  # later line for the same request
        write_jsonl(path, [
            {"type": "user", "message": {"content": "fix the bug"}},
            {"type": "assistant", "requestId": "r1",
             "message": {"model": "claude-x", "usage": usage1,
                         "content": [{"type": "text", "text": "on it"}]}},
            {"type": "assistant", "requestId": "r1",
             "message": {"model": "claude-x", "usage": usage2,
                         "content": [{"type": "tool_use", "id": "tu1"}]}},
        ])
        tr = am.ClaudeTranscript(path)
        tr.refresh()
        self.assertEqual(tr.totals(), (11, 100, 50))
        self.assertEqual(tr.model, "claude-x")
        self.assertEqual(tr.pending_tools, {"tu1"})
        self.assertEqual(tr.last_prompt, "fix the bug")
        self.assertEqual(tr.ctx_used, 111)

        write_jsonl(path, [{"type": "user", "message": {
            "content": [{"type": "tool_result", "tool_use_id": "tu1"}]}}], mode="a")
        tr.refresh()
        self.assertEqual(tr.pending_tools, set())

    def test_partial_line_is_buffered(self):
        path = os.path.join(self.tmp, "t.jsonl")
        with open(path, "w") as f:
            f.write('{"type": "user", "message": {"content": "hel')
        tail = am.JsonlTail(path)
        self.assertEqual(tail.read(), [])
        with open(path, "a") as f:
            f.write('lo"}}\n')
        self.assertEqual(tail.read()[0]["message"]["content"], "hello")


class CodexRolloutTest(TmpDirTest):
    def test_tokens_and_task_state(self):
        path = os.path.join(self.tmp, "rollout-x-abc.jsonl")
        write_jsonl(path, [
            {"type": "session_meta", "payload": {"id": "abc", "cwd": "/w/proj"}},
            {"type": "turn_context", "payload": {"model": "gpt-x"}},
            {"type": "event_msg", "timestamp": "2026-10-02T13:14:29.920Z",
             "payload": {"type": "task_started", "model_context_window": 1000}},
            {"type": "response_item", "payload": {"type": "function_call", "call_id": "c1"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 500, "cached_input_tokens": 300,
                                      "output_tokens": 20},
                "last_token_usage": {"input_tokens": 250}, "model_context_window": 1000},
                "rate_limits": {"primary": {"used_percent": 7, "window_minutes": 300}}}},
        ])
        ro = am.CodexRollout(path)
        ro.refresh()
        self.assertEqual((ro.id, ro.cwd, ro.model), ("abc", "/w/proj", "gpt-x"))
        self.assertTrue(ro.busy)
        self.assertEqual(ro.pending_calls, {"c1"})
        self.assertEqual(ro.ctx_used, 250)
        self.assertGreater(ro.state_ts, 0)

        write_jsonl(path, [
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1"}},
            {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "done"}},
        ], mode="a")
        ro.refresh()
        self.assertFalse(ro.busy)
        self.assertEqual(ro.pending_calls, set())
        self.assertEqual(ro.last_message, "done")


class CodexSubagentTest(TmpDirTest):
    def test_guardian_thread_is_subagent(self):
        path = os.path.join(self.tmp, "rollout-g.jsonl")
        write_jsonl(path, [{"type": "session_meta", "payload": {
            "id": "g", "source": {"subagent": {"other": "guardian"}}, "thread_source": "guardian_review"}}])
        ro = am.CodexRollout(path)
        ro.refresh()
        self.assertTrue(ro.subagent)


class MonitorSessionSwapTest(unittest.TestCase):
    def test_new_session_id_in_same_process_is_not_an_exit(self):
        class Fake:
            rate_limits = None

            def __init__(self):
                self.rows = []

            def sessions(self, hooks, now):
                return list(self.rows)

        claude, codex = Fake(), Fake()
        mon = am.Monitor(claude=claude, codex=codex, hooks=am.HookEvents(os.devnull))
        codex.rows = [am.Session(agent="codex", id="placeholder", pid=42)]
        mon.refresh(1)
        codex.rows = [am.Session(agent="codex", id="real", pid=42)]
        self.assertEqual([s.id for s in mon.refresh(2)], ["real"])
        codex.rows = []
        self.assertEqual([(s.id, s.status) for s in mon.refresh(3)], [("real", am.EXITED)])


class ResolveStatusTest(unittest.TestCase):
    def test_no_hooks_uses_file_state(self):
        self.assertEqual(am.resolve_status(None, am.PROCESSING, 10), (am.PROCESSING, 10))

    def test_hook_waiting_beats_busy_file(self):
        ev = {"event": "PermissionRequest", "ts": 100}
        self.assertEqual(am.resolve_status(ev, am.PROCESSING, 90), (am.WAITING, 100))

    def test_notification_permission_prompt(self):
        ev = {"event": "Notification", "notification_type": "permission_prompt", "ts": 100}
        self.assertEqual(am.resolve_status(ev, am.PROCESSING, 90)[0], am.WAITING)

    def test_newer_idle_file_overrides_stale_hook(self):
        ev = {"event": "PermissionRequest", "ts": 100}  # user denied -> no Stop hook
        self.assertEqual(am.resolve_status(ev, am.IDLE, 120), (am.IDLE, 120))

    def test_stop_hook_idle(self):
        ev = {"event": "Stop", "ts": 100}
        self.assertEqual(am.resolve_status(ev, am.PROCESSING, 99)[0], am.IDLE)


class HookInstallTest(unittest.TestCase):
    def test_install_is_idempotent_and_keeps_existing(self):
        other = {"type": "command", "command": "/bin/redock-hook agent-event"}
        cfg = {"theme": "dark", "hooks": {"Stop": [{"hooks": [other]}]}}
        cmd = "python3 /x/agentmon.py hook --agent claude"
        self.assertTrue(am.add_hooks(cfg, ["Stop", "PreToolUse"], cmd, 3))
        snapshot = json.dumps(cfg, sort_keys=True)
        self.assertFalse(am.add_hooks(cfg, ["Stop", "PreToolUse"], cmd, 3))
        self.assertEqual(json.dumps(cfg, sort_keys=True), snapshot)
        self.assertEqual(len(cfg["hooks"]["Stop"]), 2)

        self.assertTrue(am.remove_hooks(cfg))
        self.assertEqual(cfg, {"theme": "dark", "hooks": {"Stop": [{"hooks": [other]}]}})


class HookEventsTest(TmpDirTest):
    def test_latest_relevant_event_per_session(self):
        path = os.path.join(self.tmp, "events.jsonl")
        write_jsonl(path, [
            {"ts": 1, "agent": "claude", "session_id": "s", "event": "PreToolUse"},
            {"ts": 2, "agent": "claude", "session_id": "s", "event": "PermissionRequest"},
            {"ts": 3, "agent": "claude", "session_id": "s", "event": "Notification",
             "notification_type": "auth_success"},  # irrelevant, ignored
        ])
        he = am.HookEvents(path)
        he.refresh()
        self.assertEqual(he.get("claude", "s")["event"], "PermissionRequest")


try:
    import agentmon_tray as tray
except (ImportError, ValueError):  # no GTK on this machine
    tray = None


@unittest.skipIf(tray is None, "GTK not available")
class AttentionTest(unittest.TestCase):
    def sess(self, status):
        return am.Session(agent="claude", id="s", status=status)

    def test_blink_rules(self):
        att = tray.Attention()
        self.assertEqual(att.update([self.sess(am.PROCESSING)], 0), [])  # first sight: quiet
        self.assertFalse(att.blinking())

        events = att.update([self.sess(am.WAITING)], 1)
        self.assertEqual([k for k, _ in events], ["waiting"])
        self.assertTrue(att.blinking())
        att.update([self.sess(am.WAITING)], 999)  # keeps blinking while waiting
        self.assertTrue(att.blinking())

        att.update([self.sess(am.PROCESSING)], 1000)  # approved -> stop
        self.assertFalse(att.blinking())

        events = att.update([self.sess(am.IDLE)], 1001)
        self.assertEqual([k for k, _ in events], ["finished"])
        self.assertTrue(att.blinking())
        att.update([self.sess(am.IDLE)], 1001 + tray.FLASH_FINISHED + 1)
        self.assertFalse(att.blinking())

    @unittest.skipUnless(os.environ.get("DISPLAY"), "needs a display")
    def test_details_window_selects_requested_session(self):
        w = tray.DetailsWindow(None)
        rows = [am.Session(agent="claude", id="a", name="one", tmux="%1"),
                am.Session(agent="codex", id="b", name="two")]
        w.select(("codex", "b"))
        w.update(rows, 0, None)
        self.assertEqual(w.selected().name, "two")
        w.update(rows, 0, None)  # selection survives a refresh
        self.assertEqual(w.selected().name, "two")
        w.destroy()

    def test_dismiss_silences_current_wait(self):
        att = tray.Attention()
        att.update([self.sess(am.PROCESSING)], 0)
        att.update([self.sess(am.WAITING)], 1)
        att.dismiss()
        att.update([self.sess(am.WAITING)], 2)
        self.assertFalse(att.blinking())


if __name__ == "__main__":
    unittest.main()
