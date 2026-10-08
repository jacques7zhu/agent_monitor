import json
import os
import plistlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import agentmon as am
import agentmon_tray_macos as mactray
import agentmon_tray_windows as wintray


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

    def test_same_session_id_on_two_targets_stays_distinct(self):
        class Fake:
            rate_limits = None

            def __init__(self, rows):
                self.rows = rows

            def sessions(self, *args):
                return [am.Session(**am.asdict(s)) for s in self.rows]

        local = Fake([am.Session(agent="codex", id="same", pid=7)])
        empty = Fake([])
        remote = Fake([am.Session(agent="codex", id="same", target="ssh/prod", pid=7)])
        mon = am.Monitor(claude=empty, codex=local, hooks=am.HookEvents(os.devnull),
                         remotes=[remote])
        rows = mon.refresh(1)
        self.assertEqual({s.target for s in rows}, {"local", "ssh/prod"})


class TargetTest(unittest.TestCase):
    def test_background_helpers_are_hidden_on_windows(self):
        kwargs = am.hidden_subprocess_kwargs()
        if os.name == "nt":
            self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        else:
            self.assertEqual(kwargs, {})

    def test_macos_pid_and_codex_process_detection(self):
        with mock.patch.object(am.sys, "platform", "darwin"), \
                mock.patch.object(am.os, "kill") as kill:
            self.assertTrue(am.pid_alive(123))
            kill.assert_called_once_with(123, 0)

        ps = types.SimpleNamespace(stdout=(
            " 101 /opt/homebrew/bin/codex\n"
            " 202 /usr/local/bin/codex-aarch64-apple-darwin\n"
            " 303 /bin/bash\n"))
        with mock.patch.object(am.sys, "platform", "darwin"), \
                mock.patch.object(am.subprocess, "run", return_value=ps):
            self.assertEqual(am.codex_pids(), [101, 202])

    def test_parse_and_commands(self):
        self.assertEqual(am.parse_target("local").label, "local")
        wsl = am.parse_target("wsl:Ubuntu-24.04")
        self.assertEqual(wsl.label, "wsl/Ubuntu-24.04")
        self.assertEqual(wsl.command()[:4], ["wsl.exe", "-d", "Ubuntu-24.04", "--"])
        ssh = am.parse_target("ssh:prod")
        self.assertEqual(ssh.command()[-7:],
                         ["prod", "python3", "-u", "-", "snapshot", "--watch", "1.0"])
        with self.assertRaises(ValueError):
            am.parse_target("ssh:-oProxyCommand=bad")

    def test_remote_snapshot_is_tagged_and_unknown_fields_are_ignored(self):
        remote = am.RemoteTarget(am.parse_target("ssh:prod"))
        try:
            ok = remote._accept_snapshot({
                "agentmon_snapshot": 1,
                "sessions": [{"agent": "claude", "id": "abc", "target": "wrong",
                              "status": "processing", "future_field": 123}],
                "rate_limits": {"primary": {"used_percent": 4}},
            })
            self.assertTrue(ok)
            rows = [am.Session(**am.asdict(s)) for s in remote._sessions]
            self.assertEqual([(s.target, s.agent, s.id) for s in rows],
                             [("ssh/prod", "claude", "abc")])
            self.assertEqual(remote.rate_limits["primary"]["used_percent"], 4)
        finally:
            remote.close()

    def test_collector_can_be_streamed_over_stdin(self):
        class LocalPipe:
            kind = "ssh"
            label = "test/pipe"

            def command(self):
                return [sys.executable, "-u", "-", "snapshot", "--watch", "0.05"]

        remote = am.RemoteTarget(LocalPipe(), source_path=am.__file__)
        try:
            self.assertTrue(remote.wait_ready(3))
            self.assertGreater(remote._received_at, 0)
            self.assertEqual(remote.error(), "")
        finally:
            remote.close()

    def test_bundled_collector_source_is_preferred(self):
        with tempfile.TemporaryDirectory() as bundle:
            bundled = os.path.join(bundle, "agentmon.py")
            with open(bundled, "w") as f:
                f.write("# bundled collector\n")
            with mock.patch.object(am.sys, "_MEIPASS", bundle, create=True):
                self.assertEqual(am.collector_source_path(), bundled)


class WindowsTrayLogicTest(unittest.TestCase):
    def session(self, status, sid="s"):
        return am.Session(agent="codex", id=sid, target="wsl/Ubuntu", status=status)

    def test_overall_state_and_tooltip(self):
        rows = [self.session(am.IDLE, "idle"), self.session(am.PROCESSING, "busy")]
        self.assertEqual(wintray.overall_state(rows), am.PROCESSING)
        self.assertEqual(wintray.tray_tooltip(rows), "agentmon · 1 busy · 1 idle")
        rows.append(self.session(am.WAITING, "wait"))
        self.assertEqual(wintray.overall_state(rows), am.WAITING)

    def test_attention_transition(self):
        attention = wintray.Attention()
        attention.update([self.session(am.PROCESSING)], 1)
        events = attention.update([self.session(am.WAITING)], 2)
        self.assertEqual([event for event, _ in events], ["waiting"])
        self.assertTrue(attention.blinking())
        events = attention.update([self.session(am.IDLE)], 3)
        self.assertEqual([event for event, _ in events], ["finished"])

    def test_generated_icon_is_valid_ico_container(self):
        data = wintray._ico_bytes((255, 0, 0))
        self.assertEqual(data[:6], b"\x00\x00\x01\x00\x01\x00")
        self.assertGreater(len(data), 4000)

    def test_first_tray_launch_defaults_to_wsl(self):
        with mock.patch.dict(os.environ, {"AGENTMON_TARGETS": ""}), \
                mock.patch.object(am, "TARGETS_FILE", os.path.join("missing", "targets.json")):
            self.assertEqual(wintray.configured_tray_targets(None), ["wsl"])

    def test_frozen_startup_runs_the_executable_itself(self):
        with mock.patch.object(wintray.sys, "frozen", True, create=True), \
                mock.patch.object(wintray.sys, "executable", os.path.join("C:\\", "agentmon.exe")):
            parts = wintray.startup_parts(["wsl"])
        self.assertEqual(parts, [os.path.abspath(os.path.join("C:\\", "agentmon.exe")),
                                 "--target", "wsl"])


class MacTrayLogicTest(unittest.TestCase):
    def session(self, status, sid="s"):
        return am.Session(agent="codex", id=sid, status=status)

    def test_overall_state_and_status_title(self):
        rows = [self.session(am.IDLE, "idle"), self.session(am.PROCESSING, "busy")]
        self.assertEqual(mactray.overall_state(rows), am.PROCESSING)
        self.assertEqual(mactray.status_title(rows), "🟡 1 busy")
        rows.append(self.session(am.WAITING, "wait"))
        self.assertEqual(mactray.overall_state(rows), am.WAITING)
        self.assertEqual(mactray.status_title(rows), "🔴 1 waiting 1 busy")

    def test_attention_transition(self):
        attention = mactray.Attention()
        attention.update([self.session(am.PROCESSING)], 1)
        events = attention.update([self.session(am.WAITING)], 2)
        self.assertEqual([event for event, _ in events], ["waiting"])
        self.assertTrue(attention.blinking())
        events = attention.update([self.session(am.IDLE)], 3)
        self.assertEqual([event for event, _ in events], ["finished"])

    def test_start_at_login_uses_frozen_application(self):
        with tempfile.TemporaryDirectory() as tmp:
            launch_agent = os.path.join(tmp, "dev.agentmon.tray.plist")
            executable = "/Applications/agentmon.app/Contents/MacOS/agentmon"
            with mock.patch.object(mactray, "LAUNCH_AGENT", launch_agent), \
                    mock.patch.object(mactray.sys, "frozen", True, create=True), \
                    mock.patch.object(mactray.sys, "executable", executable):
                mactray.set_start_at_login(True)
                with open(launch_agent, "rb") as f:
                    data = plistlib.load(f)
                self.assertEqual(data["ProgramArguments"], [executable])
                self.assertTrue(data["RunAtLoad"])
                mactray.set_start_at_login(False)
                self.assertFalse(os.path.exists(launch_agent))


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
    def test_frozen_executable_is_used_as_hook_command(self):
        executable = os.path.join(os.sep, "opt", "agentmon")
        with mock.patch.object(am.sys, "frozen", True, create=True), \
                mock.patch.object(am.sys, "executable", executable):
            cmd = am.hook_command("codex")
        self.assertIn(executable, cmd)
        self.assertIn("hook --agent codex", cmd)
        self.assertTrue(am.is_agentmon_command(cmd))

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
