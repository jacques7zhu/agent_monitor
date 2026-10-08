#!/usr/bin/env python3
"""Native Windows notification-area monitor for agentmon.

Uses only Python's standard library and the Win32 API. It monitors the same
local/WSL/SSH targets as agentmon.py and stays in the system tray without a
terminal window when launched with pythonw.exe.

    py agentmon_tray_windows.py --target wsl:Ubuntu-24.04
    py agentmon_tray_windows.py install-startup --target wsl:Ubuntu-24.04
    py agentmon_tray_windows.py uninstall-startup
"""
import argparse
import math
import os
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agentmon as am  # noqa: E402


REFRESH_MS = 1000
BLINK_MS = 500
FLASH_FINISHED = 20
FLASH_TEST = 8
WAITING_STATES = (am.WAITING, am.WAITING_GUESS)

COLORS = {
    am.WAITING: (229, 72, 77),
    am.WAITING_GUESS: (238, 130, 48),
    am.PROCESSING: (245, 165, 36),
    am.IDLE: (48, 164, 108),
    None: (139, 141, 152),
}


def overall_state(sessions):
    live = [s.status for s in sessions if s.status != am.EXITED]
    if am.WAITING in live:
        return am.WAITING
    if am.WAITING_GUESS in live:
        return am.WAITING_GUESS
    if am.PROCESSING in live:
        return am.PROCESSING
    return am.IDLE if live else None


def tray_tooltip(sessions):
    waiting = sum(s.status == am.WAITING for s in sessions)
    waiting_guess = sum(s.status == am.WAITING_GUESS for s in sessions)
    busy = sum(s.status == am.PROCESSING for s in sessions)
    idle = sum(s.status == am.IDLE for s in sessions)
    parts = []
    if waiting:
        parts.append("%d waiting" % waiting)
    if waiting_guess:
        parts.append("%d waiting?" % waiting_guess)
    if busy:
        parts.append("%d busy" % busy)
    if idle:
        parts.append("%d idle" % idle)
    return "agentmon · " + (" · ".join(parts) if parts else "no live agents")


class Attention:
    """Tracks transitions that should blink the icon or show a notification."""

    def __init__(self):
        self.prev = {}
        self.until = {}
        self.manual_until = 0

    def update(self, sessions, now):
        events = []
        live_keys = set()
        for s in sessions:
            key = (s.target, s.agent, s.id)
            live_keys.add(key)
            old = self.prev.get(key)
            self.prev[key] = s.status
            if old is None:
                # Remote collectors connect asynchronously. If the first WSL/SSH
                # snapshot is already waiting, it is a real attention state,
                # not tray initialization noise.
                if s.status in WAITING_STATES:
                    self.until[key] = float("inf")
                    events.append(("waiting", s))
                continue
            if old == s.status:
                continue
            if s.status in WAITING_STATES:
                if old in WAITING_STATES:
                    continue
                self.until[key] = float("inf")
                events.append(("waiting", s))
            elif s.status == am.IDLE and old in (am.PROCESSING, am.WAITING,
                                                  am.WAITING_GUESS):
                self.until[key] = now + FLASH_FINISHED
                events.append(("finished", s))
            elif s.status == am.EXITED and old != am.IDLE:
                self.until[key] = now + FLASH_FINISHED
                events.append(("finished", s))
            else:
                self.until.pop(key, None)
        for key in list(self.until):
            if key not in live_keys or self.until[key] < now:
                del self.until[key]
        for key in list(self.prev):
            if key not in live_keys:
                del self.prev[key]
        if self.manual_until < now:
            self.manual_until = 0
        return events

    def blinking(self, now=None):
        now = time.time() if now is None else now
        return bool(self.until) or self.manual_until > now

    def flash(self, now, seconds):
        self.manual_until = max(self.manual_until, now + seconds)

    def dismiss(self):
        self.until.clear()
        self.manual_until = 0


def _ico_bytes(rgb, outline=False, size=32):
    """Build a small 32-bit RGBA .ico containing a coloured status dot."""
    pixels = bytearray()
    mask = bytearray()
    center = (size - 1) / 2.0
    for y in reversed(range(size)):  # DIB pixel rows are bottom-up
        mask_row = bytearray((size + 31) // 32 * 4)
        for x in range(size):
            distance = math.hypot(x - center, y - center)
            visible = (8.0 <= distance <= 11.5) if outline else distance <= 11.5
            if not visible:
                mask_row[x // 8] |= 1 << (7 - x % 8)
                pixels += b"\x00\x00\x00\x00"
                continue
            r, g, b = (255, 255, 255) if outline else rgb
            if not outline and distance <= 3.5:
                r, g, b = (255, 255, 255)
            pixels += bytes((b, g, r, 255))
        mask += mask_row
    bitmap_size = 40 + len(pixels) + len(mask)
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, bitmap_size, 22)
    dib = struct.pack("<IIIHHIIIIII", 40, size, size * 2, 1, 32, 0,
                      len(pixels), 0, 0, 0, 0)
    return header + entry + dib + pixels + mask


def write_icons():
    icon_dir = os.path.join(am.STATE_DIR, "windows-icons")
    os.makedirs(icon_dir, exist_ok=True)
    paths = {}
    for name, color in (("wait", COLORS[am.WAITING]),
                        ("guess", COLORS[am.WAITING_GUESS]),
                        ("busy", COLORS[am.PROCESSING]),
                        ("idle", COLORS[am.IDLE]),
                        ("none", COLORS[None])):
        paths[name] = os.path.join(icon_dir, "agentmon-%s.ico" % name)
        data = _ico_bytes(color)
        try:
            with open(paths[name], "rb") as f:
                unchanged = f.read() == data
        except OSError:
            unchanged = False
        if not unchanged:
            with open(paths[name], "wb") as f:
                f.write(data)
    paths["blink"] = os.path.join(icon_dir, "agentmon-blink.ico")
    data = _ico_bytes((255, 255, 255), outline=True)
    try:
        with open(paths["blink"], "rb") as f:
            unchanged = f.read() == data
    except OSError:
        unchanged = False
    if not unchanged:
        with open(paths["blink"], "wb") as f:
            f.write(data)
    return paths


def configured_tray_targets(targets):
    """A first Windows tray launch watches default WSL unless config says otherwise."""
    if targets or os.environ.get("AGENTMON_TARGETS") or os.path.exists(am.TARGETS_FILE):
        return targets
    return ["wsl"]


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(
        LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND), ("message", wintypes.UINT),
            ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
            ("time", wintypes.DWORD), ("pt", POINT), ("lPrivate", wintypes.DWORD),
        ]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
        ]

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutOrVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON),
        ]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]
    user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT,
                                ctypes.c_void_p]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterWindowMessageW.restype = wintypes.UINT
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                  wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  wintypes.UINT]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                                   ctypes.c_size_t, wintypes.LPCWSTR]
    user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, ctypes.c_void_p,
    ]
    user32.TrackPopupMenu.restype = wintypes.UINT
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR,
                                   wintypes.LPCWSTR, wintypes.UINT]
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                          ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL


class WindowsTray:
    WM_TRAY = 0x8000 + 20
    TIMER_REFRESH = 1
    TIMER_BLINK = 2
    CMD_DETAILS = 10
    CMD_DISMISS = 11
    CMD_QUIT = 12
    CMD_TEST = 13
    CMD_SESSION_BASE = 1000

    def __init__(self, targets=None):
        self.monitor = am.monitor_for_targets(configured_tray_targets(targets))
        self.attention = Attention()
        self.sessions = []
        self.state = None
        self.blink_on = False
        self.added = False
        self.hwnd = None
        self.icons = {}
        self.session_commands = {}
        self.mutex = None
        self.class_name = "agentmon.WindowsTray"
        self.taskbar_created = 0
        self._callback = None

    def run(self):
        self._ensure_single_instance()
        self._create_window()
        self._load_icons()
        self._add_icon()
        self._refresh()
        user32.SetTimer(self.hwnd, self.TIMER_REFRESH, REFRESH_MS, None)
        user32.SetTimer(self.hwnd, self.TIMER_BLINK, BLINK_MS, None)
        msg = MSG()
        while True:
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self._cleanup()
        return 0

    def _ensure_single_instance(self):
        self.mutex = kernel32.CreateMutexW(None, False, "Local\\agentmon.WindowsTray")
        if not self.mutex:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            message_box("agentmon is already running in the notification area.",
                        "agentmon")
            raise SystemExit(0)

    def _create_window(self):
        self._callback = WNDPROC(self._window_proc)
        instance = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._callback
        wc.hInstance = instance
        wc.lpszClassName = self.class_name
        if not user32.RegisterClassW(ctypes.byref(wc)):
            error = ctypes.get_last_error()
            if error != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                raise OSError(error, "RegisterClassW failed")
        self.hwnd = user32.CreateWindowExW(
            0, self.class_name, "agentmon", 0, 0, 0, 0, 0,
            None, None, instance, None)
        if not self.hwnd:
            raise OSError(ctypes.get_last_error(), "CreateWindowExW failed")
        self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

    def _load_icons(self):
        for name, path in write_icons().items():
            icon = user32.LoadImageW(None, path, 1, 32, 32, 0x0010)
            if not icon:
                raise OSError(ctypes.get_last_error(), "could not load %s" % path)
            self.icons[name] = icon

    def _nid(self, flags=0):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        nid.uCallbackMessage = self.WM_TRAY
        return nid

    def _icon_name(self):
        if self.attention.blinking() and self.blink_on:
            return "blink"
        return {am.WAITING: "wait", am.PROCESSING: "busy",
                am.WAITING_GUESS: "guess", am.IDLE: "idle",
                None: "none"}[self.state]

    def _add_icon(self):
        nid = self._nid(0x1 | 0x2 | 0x4 | 0x80)  # MESSAGE | ICON | TIP | SHOWTIP
        nid.hIcon = self.icons[self._icon_name()]
        nid.szTip = tray_tooltip(self.sessions)[:127]
        self.added = bool(shell32.Shell_NotifyIconW(0, ctypes.byref(nid)))  # NIM_ADD
        if self.added:
            # Microsoft requires setting the desired behavior version after
            # every NIM_ADD; Explorer forgets it when the taskbar is recreated.
            version = self._nid()
            version.uTimeoutOrVersion = 4  # NOTIFYICON_VERSION_4
            shell32.Shell_NotifyIconW(4, ctypes.byref(version))  # NIM_SETVERSION

    def _modify_icon(self, nid):
        if self.added and shell32.Shell_NotifyIconW(1, ctypes.byref(nid)):
            return True
        # Explorer can recreate its notification area without the registered
        # TaskbarCreated message reaching us. Remove any stale registration,
        # re-add once, and retry.
        stale = self._nid()
        shell32.Shell_NotifyIconW(2, ctypes.byref(stale))  # NIM_DELETE
        self.added = False
        self._add_icon()
        return bool(self.added and shell32.Shell_NotifyIconW(
            1, ctypes.byref(nid)))

    def _update_icon(self):
        if not self.added:
            self._add_icon()
            if not self.added:
                return
        nid = self._nid(0x2 | 0x4 | 0x80)  # NIF_ICON | NIF_TIP | NIF_SHOWTIP
        nid.hIcon = self.icons[self._icon_name()]
        nid.szTip = tray_tooltip(self.sessions)[:127]
        self._modify_icon(nid)

    def _refresh(self):
        now = time.time()
        self.sessions = self.monitor.refresh(now)
        events = self.attention.update(self.sessions, now)
        self.state = overall_state(self.sessions)
        self._update_icon()
        for kind, session in events:
            self._notify(kind, session)

    def _blink(self):
        if self.attention.blinking():
            self.blink_on = not self.blink_on
        else:
            self.blink_on = False
        self._update_icon()

    def _notify(self, kind, session):
        if kind == "waiting":
            if session.status == am.WAITING_GUESS:
                title = "%s may need attention" % session.agent.capitalize()
            else:
                title = "%s needs approval" % session.agent.capitalize()
            body = "%s · %s\n%s" % (
                session.target, session.name, am.short(session.cwd, 160))
            info_flag = 2  # NIIF_WARNING
        else:
            title = "%s finished · %s" % (session.agent.capitalize(), session.name)
            body = "%s\n%s" % (
                session.target, am.short(session.last_message or session.cwd, 180))
            info_flag = 1  # NIIF_INFO
        self._show_balloon(title, body, info_flag)

    def _show_balloon(self, title, body, info_flag):
        if not self.added:
            self._add_icon()
        if not self.added:
            return False
        nid = self._nid(0x10)  # NIF_INFO
        nid.szInfoTitle = title[:63]
        nid.szInfo = body[:255]
        nid.dwInfoFlags = info_flag
        return self._modify_icon(nid)

    def _window_proc(self, hwnd, message, wparam, lparam):
        if message == self.taskbar_created:
            self.added = False
            self._add_icon()
            return 0
        if message == self.WM_TRAY:
            mouse_message = int(lparam) & 0xFFFF
            if mouse_message in (0x0202, 0x0400):  # WM_LBUTTONUP / NIN_SELECT
                self._show_details()
            elif mouse_message in (0x0205, 0x007B):  # WM_RBUTTONUP / CONTEXTMENU
                self._show_menu()
            return 0
        if message == 0x0113:  # WM_TIMER
            if int(wparam) == self.TIMER_REFRESH:
                try:
                    self._refresh()
                except Exception as e:
                    self._show_error_once(e)
            elif int(wparam) == self.TIMER_BLINK:
                self._blink()
            return 0
        if message == 0x0111:  # WM_COMMAND
            self._run_command(int(wparam) & 0xFFFF)
            return 0
        if message == 0x0002:  # WM_DESTROY
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _show_error_once(self, error):
        text = str(error)
        if getattr(self, "_last_error", None) != text:
            self._last_error = text
            self._balloon_error("Refresh failed", text)

    def _balloon_error(self, title, text):
        self._show_balloon(title, text, 3)  # NIIF_ERROR

    def _show_menu(self):
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        self.session_commands = {}
        user32.AppendMenuW(menu, 0x0001, 0, tray_tooltip(self.sessions))  # MF_GRAYED
        user32.AppendMenuW(menu, 0x0800, 0, None)  # MF_SEPARATOR
        ordered = sorted(self.sessions, key=am.SORTS[0][1])
        for index, session in enumerate(ordered[:80]):
            command = self.CMD_SESSION_BASE + index
            self.session_commands[command] = session
            age = am.fmt_age(time.time() - session.status_since) if session.status_since else ""
            label = "%s · %s · %s — %s %s" % (
                session.target, session.agent, session.name or session.id[:8],
                session.status, age)
            user32.AppendMenuW(menu, 0, command, label[:120])
        if not ordered:
            user32.AppendMenuW(menu, 0x0001, 0, "No live Claude Code or Codex sessions")
        user32.AppendMenuW(menu, 0x0800, 0, None)
        user32.AppendMenuW(menu, 0, self.CMD_DETAILS, "Show details…")
        user32.AppendMenuW(menu, 0, self.CMD_DISMISS, "Stop blinking")
        user32.AppendMenuW(menu, 0, self.CMD_TEST, "Test notification and blinking")
        user32.AppendMenuW(menu, 0, self.CMD_QUIT, "Quit agentmon")
        point = POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(self.hwnd)
        command = user32.TrackPopupMenu(
            menu, 0x0100 | 0x0002, point.x, point.y, 0, self.hwnd, None)
        user32.DestroyMenu(menu)
        user32.PostMessageW(self.hwnd, 0, 0, 0)  # required after TrackPopupMenu
        if command:
            self._run_command(command)

    def _run_command(self, command):
        if command == self.CMD_DETAILS:
            self._show_details()
        elif command == self.CMD_DISMISS:
            self.attention.dismiss()
            self.blink_on = False
            self._update_icon()
        elif command == self.CMD_TEST:
            self.attention.flash(time.time(), FLASH_TEST)
            self.blink_on = True
            self._update_icon()
            self._show_balloon(
                "agentmon notification test",
                "Notifications and tray-icon blinking are working.", 1)
        elif command == self.CMD_QUIT:
            user32.DestroyWindow(self.hwnd)
        elif command in self.session_commands:
            self._show_details(self.session_commands[command])

    def _show_details(self, session=None):
        now = time.time()
        if session is not None:
            text = "\r\n".join([
                "Target: %s" % session.target,
                "Agent: %s" % session.agent,
                "Name: %s" % (session.name or "-"),
                "Status: %s for %s" % (
                    session.status,
                    am.fmt_age(now - session.status_since) if session.status_since else "-"),
                "Model: %s" % (session.model or "-"),
                "Directory: %s" % (session.cwd or "-"),
                "Tokens: in %s · cached %s · out %s" % (
                    am.fmt_tokens(session.tokens_in), am.fmt_tokens(session.tokens_cached),
                    am.fmt_tokens(session.tokens_out)),
                "",
                "Last prompt:\r\n%s" % (am.short(session.last_prompt, 600) or "-"),
                "",
                "Last reply:\r\n%s" % (am.short(session.last_message, 1000) or "-"),
            ])
            message_box(text, "agentmon · %s" % (session.name or session.id[:8]), self.hwnd)
            return

        lines = am.summary_lines(self.sessions, self.monitor.rate_limits)
        errors = self.monitor.remote_errors()
        if errors:
            lines += ["", "Target issues:"]
            lines += ["%s: %s" % item for item in errors.items()]
        if self.sessions:
            lines += [""]
            for s in sorted(self.sessions, key=am.SORTS[0][1]):
                age = am.fmt_age(now - s.status_since) if s.status_since else "-"
                lines.append("%s · %s · %s — %s (%s)" % (
                    s.target, s.agent, s.name or s.id[:8], s.status, age))
        message_box("\r\n".join(lines), "agentmon", self.hwnd)

    def _cleanup(self):
        if self.added:
            nid = self._nid()
            shell32.Shell_NotifyIconW(2, ctypes.byref(nid))  # NIM_DELETE
            self.added = False
        self.monitor.close()
        for icon in self.icons.values():
            user32.DestroyIcon(icon)
        if self.mutex:
            kernel32.CloseHandle(self.mutex)
            self.mutex = None


def message_box(text, title="agentmon", owner=None):
    if os.name != "nt":
        return
    user32.MessageBoxW(owner, str(text), title, 0x00000040)  # MB_ICONINFORMATION


def pythonw_executable():
    executable = os.path.abspath(sys.executable)
    base = os.path.basename(executable).lower()
    if base in ("python.exe", "python_d.exe"):
        candidate = os.path.join(os.path.dirname(executable), base.replace("python", "pythonw", 1))
        if os.path.exists(candidate):
            return candidate
    return executable


def startup_parts(targets=None):
    if getattr(sys, "frozen", False):
        parts = [os.path.abspath(sys.executable)]
    else:
        parts = [pythonw_executable(), os.path.abspath(__file__)]
    for target in targets or []:
        parts += ["--target", target]
    return parts


def startup_command(targets=None):
    return subprocess.list2cmdline(startup_parts(targets))


def install_startup(targets=None):
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, "agentmon", 0, winreg.REG_SZ, startup_command(targets))
    subprocess.Popen(startup_parts(targets), close_fds=True,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    message_box("agentmon will start in the notification area when you sign in.\n\n"
                "It has also been started now.", "agentmon installed")


def uninstall_startup():
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, "agentmon")
    except FileNotFoundError:
        pass
    message_box("agentmon was removed from startup. A running tray icon must be quit separately.",
                "agentmon uninstalled")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Windows system-tray monitor for agentmon.")
    ap.add_argument("command", nargs="?", default="run",
                    choices=["run", "install-startup", "uninstall-startup"])
    ap.add_argument("--target", action="append", dest="targets", metavar="TARGET",
                    help="local, wsl[:DISTRO], or ssh:HOST; may be repeated")
    args = ap.parse_args(argv)
    if os.name != "nt":
        raise SystemExit("agentmon_tray_windows.py only runs on Windows")
    try:
        if args.command == "install-startup":
            install_startup(args.targets)
            return 0
        if args.command == "uninstall-startup":
            return uninstall_startup()
        return WindowsTray(args.targets).run()
    except SystemExit:
        raise
    except Exception as e:
        message_box("agentmon could not start:\n\n%s" % e, "agentmon error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
