#!/usr/bin/env python3
"""agentmon - a terminal dashboard for Claude Code and Codex sessions.

Shows every live session with its status (processing / waiting for approval /
idle / exited) and token usage. Pure stdlib; Python 3.8+.

    agentmon.py [--target TARGET] [tui]  interactive dashboard (default)
    agentmon.py once             print the table once and exit
    agentmon.py watch            portable live dashboard (including Windows)
    agentmon.py install-hooks    add agentmon hooks to Claude Code and Codex
    agentmon.py uninstall-hooks  remove them again
    agentmon.py hook --agent X   (called by the hooks; logs one event)
"""
import argparse
import atexit
import glob
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, fields
from typing import Dict, List, Optional

try:
    import curses
except ImportError:  # The Windows stdlib does not bundle curses.
    curses = None

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
CODEX_DIR = os.path.join(HOME, ".codex")
STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.join(HOME, ".local", "state")), "agentmon"
)
EVENTS_FILE = os.path.join(STATE_DIR, "events.jsonl")
CONFIG_HOME = (os.environ.get("XDG_CONFIG_HOME")
               or (os.environ.get("APPDATA") if os.name == "nt" else None)
               or os.path.join(HOME, ".config"))
TARGETS_FILE = os.path.join(CONFIG_HOME, "agentmon", "targets.json")

PROCESSING = "processing"
WAITING = "waiting"
WAITING_GUESS = "waiting?"
IDLE = "idle"
EXITED = "exited"

EXITED_LINGER = 60  # seconds an exited session stays on screen
CODEX_RECENT = 600  # a rollout written this recently counts as live
GUESS_STALL = 5  # pending tool call + no writes for this long => "waiting?"

HOOK_STATE = {
    "UserPromptSubmit": PROCESSING,
    "PreToolUse": PROCESSING,
    "PostToolUse": PROCESSING,
    "PermissionRequest": WAITING,
    "Stop": IDLE,
    "Interrupt": IDLE,
    "SessionEnd": EXITED,
}
CLAUDE_HOOK_EVENTS = [
    "UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest",
    "Notification", "Stop", "SessionEnd",
]
CODEX_HOOK_EVENTS = [
    "UserPromptSubmit", "PostToolUse", "PermissionRequest", "Stop", "Interrupt", "SessionEnd",
]


def hidden_subprocess_kwargs():
    """Prevent helper commands from flashing a console window on Windows."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


@dataclass
class Session:
    agent: str
    id: str
    name: str = ""
    cwd: str = ""
    pid: Optional[int] = None
    model: str = ""
    status: str = IDLE
    status_since: float = 0.0
    tokens_in: int = 0
    tokens_cached: int = 0
    tokens_out: int = 0
    ctx_used: int = 0
    ctx_window: int = 0
    last_activity: float = 0.0
    tmux: str = ""
    last_prompt: str = ""
    last_message: str = ""
    exited_at: Optional[float] = None
    target: str = "local"


def hook_event_state(ev: dict) -> Optional[str]:
    name = ev.get("event", "")
    if name == "Notification":
        nt = ev.get("notification_type") or ""
        if nt == "permission_prompt":
            return WAITING
        if nt == "idle_prompt":
            return IDLE
        return None
    return HOOK_STATE.get(name)


def pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name != "nt":
        return os.path.exists("/proc/%d" % pid)

    # os.kill(pid, 0) is not a portable existence check on Windows. Querying a
    # limited-information handle works without optional packages or admin rights.
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    except (AttributeError, OSError, ValueError):
        return False


def parse_ts(s) -> float:
    """ISO-8601 ('2026-10-02T13:14:29.920Z') -> epoch seconds; 0 on failure."""
    if not s:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def short(text, n=200) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


class JsonlTail:
    """Reads only the lines appended to a JSONL file since the last call."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.buf = b""

    def read(self):
        try:
            size = os.path.getsize(self.path)
            if size < self.offset:  # truncated/rewritten
                self.offset, self.buf = 0, b""
            if size == self.offset:
                return []
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return []
        self.offset += len(data)
        data = self.buf + data
        lines = data.split(b"\n")
        self.buf = lines.pop()  # incomplete trailing line, if any
        out = []
        for line in lines:
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out


class HookEvents:
    """Latest hook event per (agent, session_id) from the events log."""

    def __init__(self, path=EVENTS_FILE):
        self.tail = JsonlTail(path)
        self.latest: Dict[tuple, dict] = {}

    def refresh(self):
        for ev in self.tail.read():
            key = (ev.get("agent"), ev.get("session_id"))
            if hook_event_state(ev) is not None:
                self.latest[key] = ev

    def get(self, agent, sid) -> Optional[dict]:
        return self.latest.get((agent, sid))


# --------------------------------------------------------------------------- Claude


class ClaudeTranscript:
    def __init__(self, path):
        self.tail = JsonlTail(path)
        self.usage: Dict[str, dict] = {}  # requestId -> usage (last write wins)
        self.model = ""
        self.ctx_used = 0
        self.pending_tools = set()
        self.last_prompt = ""
        self.last_message = ""
        self.mtime = 0.0

    def refresh(self):
        try:
            self.mtime = os.path.getmtime(self.tail.path)
        except OSError:
            return
        for d in self.tail.read():
            t = d.get("type")
            msg = d.get("message") or {}
            content = msg.get("content")
            if t == "assistant":
                u = msg.get("usage")
                if u:
                    self.usage[d.get("requestId") or d.get("uuid")] = u
                    self.ctx_used = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                                     + u.get("cache_creation_input_tokens", 0))
                if msg.get("model") and not msg["model"].startswith("<"):
                    self.model = msg["model"]
                for block in content if isinstance(content, list) else []:
                    if block.get("type") == "tool_use":
                        self.pending_tools.add(block.get("id"))
                    elif block.get("type") == "text" and block.get("text", "").strip():
                        self.last_message = block["text"]
            elif t == "user":
                if isinstance(content, str):
                    if not content.startswith("<"):
                        self.last_prompt = content
                    continue
                for block in content if isinstance(content, list) else []:
                    if block.get("type") == "tool_result":
                        self.pending_tools.discard(block.get("tool_use_id"))
                    elif block.get("type") == "text" and not block.get("text", "").startswith("<"):
                        self.last_prompt = block.get("text", "")

    def totals(self):
        tin = cached = out = 0
        for u in self.usage.values():
            tin += u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
            cached += u.get("cache_read_input_tokens", 0)
            out += u.get("output_tokens", 0)
        return tin, cached, out


class ClaudeSource:
    agent = "claude"

    def __init__(self, base=CLAUDE_DIR):
        self.base = base
        self.transcripts: Dict[str, ClaudeTranscript] = {}

    def transcript_path(self, cwd, sid):
        proj = "".join(c if c.isalnum() else "-" for c in cwd)
        path = os.path.join(self.base, "projects", proj, sid + ".jsonl")
        if not os.path.exists(path):  # fall back to searching all projects
            hits = glob.glob(os.path.join(self.base, "projects", "*", sid + ".jsonl"))
            if hits:
                path = hits[0]
        return path

    def sessions(self, hooks: HookEvents, now: float) -> List[Session]:
        out = []
        for f in glob.glob(os.path.join(self.base, "sessions", "*.json")):
            try:
                with open(f) as fh:
                    d = json.load(fh)
            except (OSError, ValueError):
                continue
            pid = d.get("pid")
            sid = d.get("sessionId")
            if not sid or not pid_alive(pid):
                continue
            cwd = d.get("cwd", "")
            tr = self.transcripts.get(sid)
            if tr is None:
                tr = self.transcripts[sid] = ClaudeTranscript(self.transcript_path(cwd, sid))
            tr.refresh()
            s = Session(agent=self.agent, id=sid, name=d.get("name") or os.path.basename(cwd),
                        cwd=cwd, pid=pid, tmux=d.get("tmux", ""), model=tr.model)
            s.tokens_in, s.tokens_cached, s.tokens_out = tr.totals()
            s.ctx_used = tr.ctx_used
            s.ctx_window = 1_000_000 if "[1m]" in tr.model or tr.ctx_used > 200_000 else 200_000
            s.last_prompt, s.last_message = tr.last_prompt, tr.last_message
            s.last_activity = max(tr.mtime, (d.get("updatedAt") or 0) / 1000)

            raw = (d.get("status") or "").lower()
            if raw == "busy":
                file_state = PROCESSING
            elif "wait" in raw or "permission" in raw or "approv" in raw:
                file_state = WAITING
            else:
                file_state = IDLE
            file_ts = (d.get("statusUpdatedAt") or 0) / 1000
            if (file_state == PROCESSING and tr.pending_tools
                    and now - tr.mtime > GUESS_STALL):
                file_state = WAITING_GUESS
            s.status, s.status_since = resolve_status(
                hooks.get(self.agent, sid), file_state, file_ts)
            out.append(s)
        return out


# --------------------------------------------------------------------------- Codex


class CodexRollout:
    def __init__(self, path):
        self.tail = JsonlTail(path)
        self.id = ""
        self.cwd = ""
        self.model = ""
        self.busy = False
        self.state_ts = 0.0
        self.total = {}
        self.ctx_used = 0
        self.ctx_window = 0
        self.rate_limits = None
        self.pending_calls = set()
        self.last_prompt = ""
        self.last_message = ""
        self.mtime = 0.0
        self.subagent = False  # e.g. the guardian thread that reviews approvals

    def refresh(self):
        try:
            self.mtime = os.path.getmtime(self.tail.path)
        except OSError:
            return
        for d in self.tail.read():
            t = d.get("type")
            p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
            pt = p.get("type", "")
            if t == "session_meta":
                self.id = p.get("id") or p.get("session_id") or self.id
                self.cwd = p.get("cwd", self.cwd)
                self.subagent = ("subagent" in json.dumps(p.get("source"))
                                 or p.get("thread_source") not in (None, "user"))
            elif t == "turn_context":
                self.model = p.get("model") or self.model
                self.cwd = p.get("cwd") or self.cwd
            elif t == "event_msg":
                if pt == "task_started":
                    self.busy, self.state_ts = True, parse_ts(d.get("timestamp"))
                    self.ctx_window = p.get("model_context_window") or self.ctx_window
                elif pt in ("task_complete", "turn_aborted"):
                    self.busy, self.state_ts = False, parse_ts(d.get("timestamp"))
                    self.pending_calls.clear()
                    if p.get("last_agent_message"):
                        self.last_message = p["last_agent_message"]
                elif pt == "token_count":
                    info = p.get("info") or {}
                    self.total = info.get("total_token_usage") or self.total
                    last = info.get("last_token_usage") or {}
                    self.ctx_used = last.get("input_tokens", self.ctx_used)
                    self.ctx_window = info.get("model_context_window") or self.ctx_window
                    if p.get("rate_limits"):
                        self.rate_limits = p["rate_limits"]
                elif pt == "user_message" and p.get("message"):
                    self.last_prompt = p["message"]
                elif pt == "agent_message" and p.get("message"):
                    self.last_message = p["message"]
            elif t == "response_item":
                if pt.endswith("_call_output"):
                    self.pending_calls.discard(p.get("call_id"))
                elif pt.endswith("_call"):
                    self.pending_calls.add(p.get("call_id") or p.get("id"))
                elif pt == "message":
                    text = " ".join(c.get("text", "") for c in p.get("content") or []
                                    if isinstance(c, dict))
                    if p.get("role") == "user" and text and not text.lstrip().startswith("<"):
                        self.last_prompt = text
                    elif p.get("role") == "assistant" and text:
                        self.last_message = text


def codex_pids() -> List[int]:
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq codex.exe", "/FO", "CSV", "/NH"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                timeout=3, check=False, **hidden_subprocess_kwargs()
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        pids = []
        for line in proc.stdout.splitlines():
            # tasklist CSV starts with "image.exe","1234",... . Avoid the csv
            # module here because localized error lines are not CSV records.
            cols = [part.strip().strip('"') for part in line.split(",")]
            if len(cols) > 1 and cols[0].lower() == "codex.exe":
                try:
                    pids.append(int(cols[1]))
                except ValueError:
                    pass
        return pids

    pids = []
    for proc in glob.glob("/proc/[0-9]*"):
        try:
            with open(proc + "/cmdline", "rb") as f:
                argv0 = f.read().split(b"\0")[0]
        except OSError:
            continue
        if os.path.basename(argv0) == b"codex":
            pids.append(int(os.path.basename(proc)))
    return pids


def held_files(pid):
    """Thread ids (from thread-writer-locks) and rollout files a codex process holds open."""
    threads, rollouts = set(), set()
    for fd in glob.glob("/proc/%d/fd/*" % pid):
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if "/thread-writer-locks/" in target and target.endswith(".lock"):
            threads.add(os.path.basename(target)[: -len(".lock")])
        elif "/rollout-" in target and target.endswith(".jsonl"):
            rollouts.add(target)
    return threads, rollouts


def proc_tmux_pane(pid):
    """The tmux pane id (e.g. '%9') a process runs in, from its environment."""
    try:
        with open("/proc/%d/environ" % pid, "rb") as f:
            for var in f.read().split(b"\0"):
                if var.startswith(b"TMUX_PANE="):
                    return var.split(b"=", 1)[1].decode()
    except OSError:
        pass
    return ""


def proc_cwd(pid):
    try:
        return os.readlink("/proc/%d/cwd" % pid)
    except OSError:
        return ""


class CodexSource:
    agent = "codex"

    def __init__(self, base=CODEX_DIR):
        self.base = base
        self.rollouts: Dict[str, CodexRollout] = {}
        self.rate_limits = None
        self._paths_by_id: Dict[str, str] = {}

    def default_model(self):
        try:
            with open(os.path.join(self.base, "config.toml")) as f:
                for line in f:
                    if line.startswith("["):
                        break
                    key, _, val = line.partition("=")
                    if key.strip() == "model":
                        return val.strip().strip('"')
        except OSError:
            pass
        return ""

    def recent_rollouts(self, now):
        paths = []
        day_dirs = sorted(glob.glob(os.path.join(self.base, "sessions", "*", "*", "*")))[-2:]
        for d in day_dirs:
            for p in glob.glob(os.path.join(d, "rollout-*.jsonl")):
                try:
                    if now - os.path.getmtime(p) < CODEX_RECENT:
                        paths.append(p)
                except OSError:
                    pass
        return paths

    def path_for_id(self, sid):
        # Only cache hits: a new thread gets its rollout file on the first prompt.
        if sid not in self._paths_by_id:
            hits = glob.glob(os.path.join(self.base, "sessions", "*", "*", "*", "rollout-*%s.jsonl" % sid))
            if not hits:
                return ""
            self._paths_by_id[sid] = hits[0]
        return self._paths_by_id[sid]

    def sessions(self, hooks: HookEvents, now: float) -> List[Session]:
        pids = codex_pids()
        if not pids:
            return []
        out = []
        any_threads = False
        for pid in pids:
            threads, paths = held_files(pid)
            any_threads = any_threads or bool(threads)
            paths |= {p for p in map(self.path_for_id, threads) if p}
            rows = [s for s in (self.from_rollout(p, pid, hooks, now) for p in sorted(paths)) if s]
            if not rows and threads:
                rows = [self.placeholder(pid, threads, hooks)]
            pane = proc_tmux_pane(pid)
            for s in rows:
                s.tmux = pane
            out += rows
        if not any_threads:
            # Older Codex without thread-writer locks: fall back to recently written rollouts.
            for p in self.recent_rollouts(now):
                s = self.from_rollout(p, None, hooks, now)
                if s and all(s.id != o.id for o in out):
                    out.append(s)
        return [s for s in out if s.status != EXITED]

    def placeholder(self, pid, threads, hooks):
        """A session that has no rollout yet (Codex writes it on the first prompt)."""
        with_events = [t for t in threads if hooks.get(self.agent, t)]
        tid = (with_events or sorted(threads))[0]
        cwd = proc_cwd(pid)
        s = Session(agent=self.agent, id=tid, name=os.path.basename(cwd.rstrip("/")) or tid[:8],
                    cwd=cwd, pid=pid, model=self.default_model(), last_prompt="(no prompt yet)")
        s.status, s.status_since = resolve_status(hooks.get(self.agent, tid), IDLE, 0)
        return s

    def from_rollout(self, path, pid, hooks, now) -> Optional[Session]:
        ro = self.rollouts.get(path)
        if ro is None:
            ro = self.rollouts[path] = CodexRollout(path)
        ro.refresh()
        if not ro.id or ro.subagent:
            return None
        if ro.rate_limits:
            self.rate_limits = ro.rate_limits
        tot = ro.total
        cached = tot.get("cached_input_tokens", 0)
        s = Session(
            agent=self.agent, id=ro.id, name=os.path.basename(ro.cwd.rstrip("/")) or ro.id[:8],
            cwd=ro.cwd, pid=pid, model=ro.model or self.default_model(),
            tokens_in=tot.get("input_tokens", 0) - cached, tokens_cached=cached,
            tokens_out=tot.get("output_tokens", 0),
            ctx_used=ro.ctx_used, ctx_window=ro.ctx_window, last_activity=ro.mtime,
            last_prompt=ro.last_prompt, last_message=ro.last_message,
        )
        file_state = PROCESSING if ro.busy else IDLE
        if ro.busy and ro.pending_calls and now - ro.mtime > GUESS_STALL:
            file_state = WAITING_GUESS
        s.status, s.status_since = resolve_status(
            hooks.get(self.agent, ro.id), file_state, ro.state_ts or ro.mtime)
        return s


def resolve_status(ev: Optional[dict], file_state: str, file_ts: float):
    """Pick between the latest hook event and the state read from session files.

    Hooks are exact, so they win - except when the file reports idle more recently
    (e.g. the user denied a permission or interrupted, which fires no Stop hook).
    """
    if ev is not None:
        hstate = hook_event_state(ev)
        hts = ev.get("ts", 0)
        if hstate and not (file_state == IDLE and file_ts > hts + 1):
            if hstate == PROCESSING and file_state == IDLE and file_ts >= hts:
                return IDLE, file_ts
            return hstate, hts
    return file_state, file_ts


# --------------------------------------------------------------------------- targets


@dataclass(frozen=True)
class TargetSpec:
    kind: str
    value: str
    label: str

    def command(self, watch=1.0):
        remote_args = ["python3", "-u", "-", "snapshot", "--watch", str(watch)]
        if self.kind == "wsl":
            cmd = ["wsl.exe"]
            if self.value:
                cmd += ["-d", self.value]
            return cmd + ["--"] + remote_args
        if self.kind == "ssh":
            return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                    self.value] + remote_args
        raise ValueError("local targets do not have a remote command")


def parse_target(spec: str) -> TargetSpec:
    """Parse local, wsl[:distro], or ssh:host without invoking a shell."""
    spec = (spec or "").strip()
    if spec == "local":
        return TargetSpec("local", "", "local")
    if spec == "wsl":
        return TargetSpec("wsl", "", "wsl")
    kind, sep, value = spec.partition(":")
    if kind not in ("wsl", "ssh") or not sep or not value.strip():
        raise ValueError("invalid target %r (use local, wsl[:DISTRO], or ssh:HOST)" % spec)
    value = value.strip()
    if value.startswith("-") or any(c in value for c in "\r\n\0"):
        raise ValueError("unsafe target value %r" % value)
    return TargetSpec(kind, value, "%s/%s" % (kind, value))


def configured_target_specs(cli_targets=None):
    """Return target strings from CLI, environment, config, or the local default."""
    raw = cli_targets
    if not raw and os.environ.get("AGENTMON_TARGETS"):
        raw = [s.strip() for s in os.environ["AGENTMON_TARGETS"].split(",") if s.strip()]
    if not raw and os.path.exists(TARGETS_FILE):
        try:
            with open(TARGETS_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
            raw = cfg if isinstance(cfg, list) else cfg.get("targets")
        except (OSError, ValueError, AttributeError) as e:
            raise ValueError("could not read %s: %s" % (TARGETS_FILE, e))
    raw = raw or ["local"]
    if not isinstance(raw, list) or not all(isinstance(s, str) for s in raw):
        raise ValueError("targets must be a list of strings")
    out = []
    seen = set()
    for item in raw:
        target = parse_target(item)
        key = (target.kind, target.value)
        if key not in seen:
            out.append(target)
            seen.add(key)
    return out


def collector_source_path():
    """Locate source sent to WSL/SSH, including inside a PyInstaller bundle."""
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        bundled = os.path.join(bundle_dir, "agentmon.py")
        if os.path.isfile(bundled):
            return bundled
    return os.path.abspath(__file__)


class RemoteTarget:
    """A persistent WSL/SSH worker that streams JSON snapshots.

    The current agentmon source is sent over stdin and executed with ``python3 -``.
    That keeps setup zero-install and guarantees the collector matches the client.
    """

    PROTOCOL = 1
    STALE_AFTER = 5

    def __init__(self, spec: TargetSpec, source_path=None):
        if spec.kind == "local":
            raise ValueError("RemoteTarget requires wsl or ssh")
        self.spec = spec
        self.source_path = source_path or collector_source_path()
        self.rate_limits = None
        self.last_error = ""
        self._sessions = []
        self._received_at = 0.0
        self._proc = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._retry_at = 0.0
        self._closed = False
        self._stderr_lines = []
        atexit.register(self.close)

    def start(self):
        with self._lock:
            if self._closed or (self._proc is not None and self._proc.poll() is None):
                return
            if time.time() < self._retry_at:
                return
            self._retry_at = time.time() + 2
            self._ready.clear()
            self._stderr_lines = []
            try:
                proc = subprocess.Popen(
                    self.spec.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                    bufsize=1, **hidden_subprocess_kwargs()
                )
                self._proc = proc
            except OSError as e:
                self.last_error = str(e)
                self._ready.set()
                return

        threading.Thread(target=self._read_stdout, args=(proc,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True).start()
        try:
            with open(self.source_path, encoding="utf-8") as f:
                source = f.read()
            proc.stdin.write(source)
            proc.stdin.close()
        except (OSError, BrokenPipeError) as e:
            with self._lock:
                self.last_error = str(e)
            try:
                proc.terminate()
            except OSError:
                pass

    def _read_stdout(self, proc):
        try:
            for line in proc.stdout:
                try:
                    data = json.loads(line)
                except ValueError:
                    continue  # tolerate login banners written to stdout
                self._accept_snapshot(data)
        except (OSError, ValueError) as e:
            with self._lock:
                self.last_error = str(e)
        finally:
            try:
                rc = proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                rc = None
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                    if not self._closed and not self.last_error:
                        detail = " · ".join(self._stderr_lines[-2:])
                        self.last_error = detail or "remote collector exited%s" % (
                            " (%s)" % rc if rc is not None else "")
            self._ready.set()

    def _accept_snapshot(self, data):
        if not isinstance(data, dict) or data.get("agentmon_snapshot") != self.PROTOCOL:
            return False
        valid = {f.name for f in fields(Session)}
        sessions = []
        received_at = time.time()
        generated_at = data.get("generated_at")
        clock_offset = (received_at - generated_at
                        if isinstance(generated_at, (int, float)) else 0)
        for row in data.get("sessions") or []:
            if not isinstance(row, dict):
                continue
            values = {k: v for k, v in row.items() if k in valid}
            if not values.get("agent") or not values.get("id"):
                continue
            values["target"] = self.spec.label
            for key in ("status_since", "last_activity", "exited_at"):
                if isinstance(values.get(key), (int, float)) and values[key]:
                    values[key] += clock_offset
            sessions.append(Session(**values))
        with self._lock:
            self._sessions = sessions
            self.rate_limits = data.get("rate_limits")
            self._received_at = received_at
            self.last_error = ""
        self._ready.set()
        return True

    def _read_stderr(self, proc):
        try:
            for line in proc.stderr:
                line = line.strip()
                if line:
                    with self._lock:
                        self._stderr_lines = (self._stderr_lines + [line])[-5:]
        except OSError:
            pass

    def wait_ready(self, timeout):
        self.start()
        return self._ready.wait(timeout)

    def sessions(self, now):
        self.start()
        with self._lock:
            # Once a dead connection's last result is stale, let Monitor mark its
            # rows exited instead of presenting old state as live indefinitely.
            proc_alive = self._proc is not None and self._proc.poll() is None
            if not proc_alive and self._received_at and now - self._received_at > self.STALE_AFTER:
                return []
            return [Session(**asdict(s)) for s in self._sessions]

    def error(self):
        with self._lock:
            if self.last_error:
                return self.last_error
            if not self._received_at:
                return "connecting"
            return ""

    def close(self):
        with self._lock:
            self._closed = True
            proc = self._proc
            self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass


# --------------------------------------------------------------------------- Monitor


class Monitor:
    def __init__(self, claude=None, codex=None, hooks=None, remotes=None, include_local=True):
        self.hooks = hooks or HookEvents()
        self.claude = claude or ClaudeSource()
        self.codex = codex or CodexSource()
        self.remotes = remotes or []
        self.include_local = include_local
        self.rate_limits = None
        self.known: Dict[tuple, Session] = {}
        self.since: Dict[tuple, tuple] = {}  # key -> (status, first seen at)

    def refresh(self, now=None) -> List[Session]:
        now = now or time.time()
        live = []
        if self.include_local:
            self.hooks.refresh()
            live = self.claude.sessions(self.hooks, now) + self.codex.sessions(self.hooks, now)
            for s in live:
                s.target = "local"
            self.rate_limits = self.codex.rate_limits
        for remote in self.remotes:
            live += remote.sessions(now)
            if remote.rate_limits and self.rate_limits is None:
                self.rate_limits = remote.rate_limits
        live_keys = set()
        for s in live:
            key = (s.target, s.agent, s.id)
            live_keys.add(key)
            prev = self.since.get(key)
            if prev is None or prev[0] != s.status:
                self.since[key] = (s.status, s.status_since or now)
            s.status_since = self.since[key][1]
            self.known[key] = s
        live_pids = {(s.target, s.agent, s.pid) for s in live if s.pid}
        for key, s in list(self.known.items()):
            if key in live_keys:
                continue
            if s.pid and (s.target, s.agent, s.pid) in live_pids:
                # Same process, new session id (Claude /clear, Codex's first prompt): not an exit.
                del self.known[key]
                self.since.pop(key, None)
                continue
            if s.exited_at is None:
                s.exited_at, s.status, s.status_since = now, EXITED, now
            elif now - s.exited_at > EXITED_LINGER:
                del self.known[key]
                self.since.pop(key, None)
        return list(self.known.values())

    def start_remotes(self):
        for remote in self.remotes:
            remote.start()

    def wait_remotes(self, timeout=7):
        deadline = time.time() + timeout
        self.start_remotes()
        for remote in self.remotes:
            remote.wait_ready(max(0, deadline - time.time()))

    def remote_errors(self):
        return {remote.spec.label: remote.error() for remote in self.remotes if remote.error()}

    def close(self):
        for remote in self.remotes:
            remote.close()


def monitor_for_targets(cli_targets=None):
    specs = configured_target_specs(cli_targets)
    remotes = [RemoteTarget(s) for s in specs if s.kind != "local"]
    return Monitor(remotes=remotes, include_local=any(s.kind == "local" for s in specs))


# --------------------------------------------------------------------------- formatting


def fmt_tokens(n) -> str:
    n = n or 0
    if n >= 1_000_000:
        return "%.1fM" % (n / 1e6)
    if n >= 1000:
        return "%.1fk" % (n / 1e3) if n < 10000 else "%dk" % (n // 1000)
    return str(n)


def fmt_age(secs) -> str:
    secs = max(0, int(secs))
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        return "%dm%02ds" % (secs // 60, secs % 60)
    return "%dh%02dm" % (secs // 3600, secs % 3600 // 60)


COLUMNS = [  # header, width, getter(session, now)
    ("TARGET", 16, lambda s, now: s.target),
    ("AGENT", 6, lambda s, now: s.agent),
    ("NAME", 24, lambda s, now: s.name),
    ("MODEL", 16, lambda s, now: s.model),
    ("STATUS", 10, lambda s, now: s.status),
    ("FOR", 7, lambda s, now: fmt_age(now - s.status_since) if s.status_since else "-"),
    ("IN", 7, lambda s, now: fmt_tokens(s.tokens_in)),
    ("CACHED", 7, lambda s, now: fmt_tokens(s.tokens_cached)),
    ("OUT", 7, lambda s, now: fmt_tokens(s.tokens_out)),
    ("CTX", 5, lambda s, now: "%d%%" % (100 * s.ctx_used / s.ctx_window) if s.ctx_window else "-"),
    ("PID", 8, lambda s, now: str(s.pid or "-")),
    ("TMUX", 20, lambda s, now: s.tmux or "-"),
]

STATUS_ORDER = {WAITING: 0, WAITING_GUESS: 1, PROCESSING: 2, IDLE: 3, EXITED: 4}
SORTS = [
    ("status", lambda s: (STATUS_ORDER.get(s.status, 9), s.target, s.agent, s.name)),
    ("activity", lambda s: -s.last_activity),
    ("tokens", lambda s: -(s.tokens_in + s.tokens_cached + s.tokens_out)),
    ("name", lambda s: (s.target, s.agent, s.name)),
]


def format_row(s: Session, now: float) -> str:
    cells = []
    for _, width, get in COLUMNS:
        v = str(get(s, now))
        cells.append((v if len(v) <= width else v[: width - 1] + "…").ljust(width))
    return " ".join(cells)


def header_row() -> str:
    return " ".join(h.ljust(w) for h, w, _ in COLUMNS)


def summary_lines(sessions: List[Session], codex_limits) -> List[str]:
    lines = []
    for agent in ("claude", "codex"):
        ss = [s for s in sessions if s.agent == agent and s.status != EXITED]
        counts = {}
        for s in ss:
            counts[s.status] = counts.get(s.status, 0) + 1
        tot = sum(s.tokens_in + s.tokens_cached + s.tokens_out for s in ss)
        parts = ", ".join("%d %s" % (counts[k], k) for k in sorted(counts, key=lambda k: STATUS_ORDER.get(k, 9)))
        line = "%-6s %d live (%s)  tokens %s" % (agent, len(ss), parts or "none", fmt_tokens(tot))
        # A single unlabeled limits value is only meaningful when the displayed
        # Codex rows come from one machine. Do not imply that one host's quota is
        # an aggregate when several targets are present.
        if agent == "codex" and codex_limits and len({s.target for s in ss}) <= 1:
            lim = []
            for key in ("primary", "secondary"):
                w = codex_limits.get(key) or {}
                if "used_percent" in w:
                    mins = w.get("window_minutes") or 0
                    label = "%dh" % (mins // 60) if mins < 1440 else "%dd" % (mins // 1440)
                    lim.append("%s %d%%" % (label, w["used_percent"]))
            if lim:
                line += "  limits: " + "  ".join(lim)
        lines.append(line)
    return lines


def print_monitor(mon, clear=False):
    now = time.time()
    sessions = sorted(mon.refresh(now), key=SORTS[0][1])
    if clear:
        print("\033[2J\033[H", end="")
    for line in summary_lines(sessions, mon.rate_limits):
        print(line)
    print()
    print(header_row())
    for s in sessions:
        print(format_row(s, now))
    for target, error in mon.remote_errors().items():
        print("\n%s: %s" % (target, error))
    if mon.include_local and not os.path.exists(EVENTS_FILE):
        print("\n(no hook events yet - run `agentmon.py install-hooks` for exact approval detection)")


def cmd_once(args):
    try:
        mon = monitor_for_targets(getattr(args, "targets", None))
    except ValueError as e:
        raise SystemExit("agentmon: %s" % e)
    try:
        mon.wait_remotes()
        print_monitor(mon)
    finally:
        mon.close()


def cmd_watch(args):
    try:
        mon = monitor_for_targets(getattr(args, "targets", None))
    except ValueError as e:
        raise SystemExit("agentmon: %s" % e)
    try:
        while True:
            print_monitor(mon, clear=True)
            print("\nrefreshing every %.1fs · Ctrl-C to quit" % args.interval)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        mon.close()


def cmd_snapshot(args):
    """Machine-readable local collector used by RemoteTarget."""
    mon = Monitor()
    try:
        while True:
            now = time.time()
            payload = {
                "agentmon_snapshot": RemoteTarget.PROTOCOL,
                "generated_at": now,
                "sessions": [asdict(s) for s in mon.refresh(now)],
                "rate_limits": mon.rate_limits,
            }
            try:
                print(json.dumps(payload, separators=(",", ":")), flush=True)
            except BrokenPipeError:
                return 0
            if not args.watch:
                return 0
            time.sleep(args.watch)
    finally:
        mon.close()


# --------------------------------------------------------------------------- TUI


class TUI:
    def __init__(self, scr, monitor=None):
        self.scr = scr
        self.mon = monitor or Monitor()
        self.sel = 0
        self.sort = 0
        self.show_exited = True
        self.bell = True
        self.detail = False
        self.prev_status: Dict[tuple, str] = {}
        curses.curs_set(0)
        curses.use_default_colors()
        for i, c in enumerate([curses.COLOR_YELLOW, curses.COLOR_RED, curses.COLOR_GREEN,
                               curses.COLOR_CYAN, curses.COLOR_MAGENTA], start=1):
            curses.init_pair(i, c, -1)
        self.status_attr = {
            PROCESSING: curses.color_pair(1),
            WAITING: curses.color_pair(2) | curses.A_BOLD,
            WAITING_GUESS: curses.color_pair(2),
            IDLE: curses.color_pair(3),
            EXITED: curses.A_DIM,
        }
        scr.timeout(1000)

    def run(self):
        while True:
            now = time.time()
            sessions = [s for s in self.mon.refresh(now) if self.show_exited or s.status != EXITED]
            sessions.sort(key=SORTS[self.sort][1])
            self.ring_on_new_waiting(sessions)
            self.sel = max(0, min(self.sel, len(sessions) - 1))
            self.draw(sessions, now)
            key = self.scr.getch()
            if key in (ord("q"), ord("Q")):
                return
            if self.detail and key in (27, ord("\n"), curses.KEY_ENTER, 10, 13):
                self.detail = False
            elif key in (curses.KEY_UP, ord("k")):
                self.sel -= 1
            elif key in (curses.KEY_DOWN, ord("j")):
                self.sel += 1
            elif key in (ord("\n"), curses.KEY_ENTER, 10, 13):
                self.detail = bool(sessions)
            elif key == ord("s"):
                self.sort = (self.sort + 1) % len(SORTS)
            elif key == ord("a"):
                self.show_exited = not self.show_exited
            elif key == ord("b"):
                self.bell = not self.bell

    def ring_on_new_waiting(self, sessions):
        ring = False
        for s in sessions:
            key = (s.agent, s.id)
            if s.status == WAITING and self.prev_status.get(key) not in (None, WAITING):
                ring = True
            self.prev_status[key] = s.status
        if ring and self.bell:
            curses.beep()

    def put(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                self.scr.addnstr(y, x, text, w - x - 1, attr)
            except curses.error:
                pass

    def draw(self, sessions, now):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        title = " agentmon  %s  sort:%s  bell:%s " % (
            time.strftime("%H:%M:%S"), SORTS[self.sort][0], "on" if self.bell else "off")
        self.put(0, 0, title.ljust(w), curses.A_REVERSE)
        y = 1
        for line in summary_lines(sessions, self.mon.rate_limits):
            self.put(y, 1, line, curses.color_pair(4))
            y += 1
        for target, error in self.mon.remote_errors().items():
            self.put(y, 1, "%s: %s" % (target, error), curses.A_DIM)
            y += 1
        if self.mon.include_local and not os.path.exists(EVENTS_FILE):
            self.put(y, 1, "hooks not installed: approval state is a guess (run install-hooks)", curses.A_DIM)
            y += 1
        y += 1
        self.put(y, 1, header_row(), curses.A_BOLD | curses.A_UNDERLINE)
        y += 1
        status_index = next(i for i, col in enumerate(COLUMNS) if col[0] == "STATUS")
        status_col = sum(wd + 1 for _, wd, _ in COLUMNS[:status_index]) + 1
        for i, s in enumerate(sessions):
            if y >= h - 1:
                break
            base = curses.A_REVERSE if i == self.sel else 0
            if s.status == EXITED:
                base |= curses.A_DIM
            self.put(y, 1, format_row(s, now), base)
            self.put(y, status_col, s.status.ljust(10), base | self.status_attr.get(s.status, 0))
            y += 1
        if not sessions:
            self.put(y, 1, "no live Claude Code or Codex sessions", curses.A_DIM)
        self.put(h - 1, 0, " ↑/↓ select  enter details  s sort  a exited  b bell  q quit ".ljust(w),
                 curses.A_REVERSE)
        scr.refresh()
        if self.detail and sessions:
            self.draw_detail(sessions[self.sel], now)

    def draw_detail(self, s: Session, now):
        h, w = self.scr.getmaxyx()
        bw, bh = max(20, w - 8), max(8, min(h - 4, 18))
        win = curses.newwin(bh, bw, max(0, (h - bh) // 2), 4)
        win.erase()
        win.box()
        inner = bw - 4
        rows = [
            ("target", s.target), ("session", s.id), ("cwd", s.cwd), ("model", s.model),
            ("status", "%s for %s" % (s.status, fmt_age(now - s.status_since))),
            ("tokens", "in %s  cached %s  out %s  ctx %s/%s" % (
                fmt_tokens(s.tokens_in), fmt_tokens(s.tokens_cached), fmt_tokens(s.tokens_out),
                fmt_tokens(s.ctx_used), fmt_tokens(s.ctx_window))),
            ("pid/tmux", "%s  %s" % (s.pid or "-", s.tmux or "-")),
            ("prompt", short(s.last_prompt, inner * 3)),
            ("reply", short(s.last_message, inner * 6)),
        ]
        y = 1
        for label, val in rows:
            text = "%-9s %s" % (label, val)
            while text and y < bh - 1:
                try:
                    win.addnstr(y, 2, text[:inner], inner)
                except curses.error:
                    pass
                text = " " * 10 + text[inner:] if len(text) > inner else ""
                y += 1
        try:
            win.addnstr(0, 2, " %s / %s " % (s.agent, s.name), inner, curses.A_BOLD)
        except curses.error:
            pass
        win.refresh()


def cmd_tui(args):
    if curses is None:
        # Windows ships no curses module. The ANSI dashboard works in Windows
        # Terminal/PowerShell; installing windows-curses restores key controls.
        if not hasattr(args, "interval"):
            args.interval = 1.0
        return cmd_watch(args)
    try:
        mon = monitor_for_targets(getattr(args, "targets", None))
    except ValueError as e:
        raise SystemExit("agentmon: %s" % e)
    try:
        return curses.wrapper(lambda scr: TUI(scr, mon).run())
    finally:
        mon.close()


# --------------------------------------------------------------------------- hooks


def cmd_hook(args):
    """Called by Claude Code / Codex hooks. Must be fast and never fail the agent."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
        ev = {
            "ts": time.time(),
            "agent": args.agent,
            "session_id": data.get("session_id") or data.get("sessionId") or "",
            "event": data.get("hook_event_name") or data.get("event") or "",
        }
        if data.get("notification_type"):
            ev["notification_type"] = data["notification_type"]
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps(ev) + "\n")
        if os.path.getsize(EVENTS_FILE) > 5_000_000:
            os.replace(EVENTS_FILE, EVENTS_FILE + ".1")
    except Exception:
        pass
    return 0


def hook_command(agent):
    parts = [sys.executable, os.path.abspath(__file__), "hook", "--agent", agent]
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return " ".join(shlex.quote(part) for part in parts)


def is_agentmon_command(cmd):
    cmd = cmd or ""
    return "agentmon.py" in cmd and "hook --agent" in cmd


def _load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def _save_json(path, data):
    if os.path.exists(path) and not os.path.exists(path + ".agentmon.bak"):
        shutil.copy2(path, path + ".agentmon.bak")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".agentmon.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def add_hooks(config: dict, events, command, timeout) -> bool:
    hooks = config.setdefault("hooks", {})
    changed = False
    for event in events:
        groups = hooks.setdefault(event, [])
        if any(is_agentmon_command(h.get("command")) for g in groups for h in g.get("hooks", [])):
            continue
        groups.append({"hooks": [{"type": "command", "command": command, "timeout": timeout}]})
        changed = True
    return changed


def remove_hooks(config: dict) -> bool:
    hooks = config.get("hooks") or {}
    changed = False
    for event in list(hooks):
        kept = []
        for g in hooks[event]:
            hs = [h for h in g.get("hooks", []) if not is_agentmon_command(h.get("command"))]
            if len(hs) != len(g.get("hooks", [])):
                changed = True
            if hs:
                kept.append(dict(g, hooks=hs))
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    return changed


def hook_targets():
    return [
        (os.path.join(CLAUDE_DIR, "settings.json"), CLAUDE_HOOK_EVENTS, "claude"),
        (os.path.join(CODEX_DIR, "hooks.json"), CODEX_HOOK_EVENTS, "codex"),
    ]


def cmd_install_hooks(args):
    for path, events, agent in hook_targets():
        cfg = _load_json(path)
        if add_hooks(cfg, events, hook_command(agent), 3):
            _save_json(path, cfg)
            print("installed agentmon hooks in %s" % path)
        else:
            print("already installed in %s" % path)
    print("Restart running sessions to pick up the hooks. Codex may ask you to trust the new hook.")


def cmd_uninstall_hooks(args):
    for path, _, _ in hook_targets():
        cfg = _load_json(path)
        if remove_hooks(cfg):
            _save_json(path, cfg)
            print("removed agentmon hooks from %s" % path)
        else:
            print("no agentmon hooks in %s" % path)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Monitor local, WSL, and SSH Claude Code/Codex sessions.",
        epilog="TARGET is local, wsl, wsl:DISTRO, or ssh:HOST (repeatable).",
    )
    ap.add_argument("--target", action="append", dest="targets", metavar="TARGET",
                    help="session source; may be repeated (default: config or local)")
    sub = ap.add_subparsers(dest="cmd")
    tui = sub.add_parser("tui", help="interactive dashboard (default)")
    once = sub.add_parser("once", help="print the table once")
    watch = sub.add_parser("watch", help="portable live dashboard; Ctrl-C to quit")
    watch.add_argument("--interval", type=float, default=1.0, metavar="SECONDS")
    for parser in (tui, once, watch):
        parser.add_argument("--target", action="append", dest="targets", metavar="TARGET",
                            default=argparse.SUPPRESS, help="session source; may be repeated")
    sub.add_parser("install-hooks", help="add agentmon hooks to Claude Code and Codex")
    sub.add_parser("uninstall-hooks", help="remove agentmon hooks")
    hp = sub.add_parser("hook", help="log one hook event (used by the hooks)")
    hp.add_argument("--agent", required=True, choices=["claude", "codex"])
    snapshot = sub.add_parser("snapshot", help="internal JSON snapshot collector")
    snapshot.add_argument("--watch", type=float, default=0, metavar="SECONDS",
                          help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if getattr(args, "interval", 1) <= 0 or getattr(args, "watch", 1) < 0:
        ap.error("refresh intervals must be positive")
    handler = {
        None: cmd_tui, "tui": cmd_tui, "once": cmd_once, "watch": cmd_watch,
        "snapshot": cmd_snapshot, "hook": cmd_hook,
        "install-hooks": cmd_install_hooks, "uninstall-hooks": cmd_uninstall_hooks,
    }[args.cmd]
    return handler(args) or 0


if __name__ == "__main__":
    sys.exit(main())
