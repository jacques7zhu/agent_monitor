#!/usr/bin/env python3
"""agentmon tray - Ubuntu top-bar indicator for Claude Code and Codex sessions.

The icon in the top-right panel shows the overall state (red = an agent is waiting
for approval, amber = processing, green = idle) and blinks when a session needs
attention: it starts waiting for approval, finishes a turn, or exits. The menu lists
every session; clicking one opens a details window.

    agentmon_tray.py                   run the indicator (single instance)
    agentmon_tray.py install-desktop   add an app-menu launcher + start on login
    agentmon_tray.py uninstall-desktop

Needs: sudo apt install gir1.2-appindicator3-0.1   (GNOME's AppIndicator extension
is enabled by default on Ubuntu).
"""
import os
import subprocess
import sys
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Notify", "0.7")
from gi.repository import GLib  # noqa: E402

GLib.set_prgname("agentmon")  # before Gtk initialises, so the window class is "agentmon"
GLib.set_application_name("agentmon")
from gi.repository import Gio, Gtk, Notify  # noqa: E402

AppIndicator = None
for _ns in ("AyatanaAppIndicator3", "AppIndicator3"):
    try:
        gi.require_version(_ns, "0.1")
        AppIndicator = getattr(__import__("gi.repository", fromlist=[_ns]), _ns)
        break
    except (ValueError, ImportError):
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agentmon as am  # noqa: E402

APP_ID = "dev.agentmon.Tray"
ICON_DIR = os.path.join(am.STATE_DIR, "icons")
REFRESH_MS = 1000
BLINK_MS = 500
FLASH_FINISHED = 20  # seconds to blink after a session finishes a turn
FLASH_EXITED = 8

COLORS = {
    am.WAITING: "#e5484d",
    am.WAITING_GUESS: "#e5484d",
    am.PROCESSING: "#f5a524",
    am.IDLE: "#30a46c",
    am.EXITED: "#8b8d98",
}
ICON_FOR = {am.WAITING: "wait", am.PROCESSING: "busy", am.IDLE: "idle", None: "none"}
DOT = {am.WAITING: "🔴", am.WAITING_GUESS: "🟠", am.PROCESSING: "🟡", am.IDLE: "🟢", am.EXITED: "⚪"}


def write_icons():
    """Coloured dots for each overall state plus a hollow ring used for blinking."""
    os.makedirs(ICON_DIR, exist_ok=True)
    dot = ('<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22">'
           '<circle cx="11" cy="11" r="7" fill="{fill}" stroke="#ffffff" stroke-width="1.5"/>'
           '<circle cx="11" cy="11" r="2.5" fill="#ffffff" fill-opacity="{core}"/></svg>')
    icons = {
        "agentmon-wait": dot.format(fill=COLORS[am.WAITING], core=0.9),
        "agentmon-busy": dot.format(fill=COLORS[am.PROCESSING], core=0.9),
        "agentmon-idle": dot.format(fill=COLORS[am.IDLE], core=0),
        "agentmon-none": dot.format(fill=COLORS[am.EXITED], core=0),
        "agentmon-blink": ('<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22">'
                           '<circle cx="11" cy="11" r="7" fill="none" stroke="#ffffff" '
                           'stroke-width="1.5"/></svg>'),
    }
    for name, svg in icons.items():
        path = os.path.join(ICON_DIR, name + ".svg")
        try:
            with open(path) as f:
                if f.read() == svg:
                    continue
        except OSError:
            pass
        with open(path, "w") as f:
            f.write(svg)
    return os.path.join(ICON_DIR, "agentmon-busy.svg")


def session_key(s):
    return (s.agent, s.id)


def session_label(s, now):
    age = am.fmt_age(now - s.status_since) if s.status_since else ""
    return "%s %s · %s — %s %s" % (DOT.get(s.status, "•"), s.agent, s.name or s.id[:8], s.status, age)


def jump_to_tmux(target):
    """target is 'session:@window.%pane' or a bare pane id '%pane'; switch the client to it."""
    if not target:
        return False
    if ":" in target:
        sess, rest = target.split(":", 1)
        window, _, pane = rest.partition(".")
    else:
        sess = window = pane = target
    cmds = [["tmux", "switch-client", "-t", sess], ["tmux", "select-window", "-t", window]]
    if pane:
        cmds.append(["tmux", "select-pane", "-t", pane])
    ok = True
    for c in cmds:
        try:
            ok = subprocess.run(c, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=2).returncode == 0 and ok
        except (OSError, subprocess.TimeoutExpired):
            return False
    return ok


class Attention:
    """Decides which sessions should make the tray icon blink.

    Blink while a session waits for approval, and for a short while after it
    finishes a turn or exits. Starting work (-> processing) never blinks: the
    user just typed something, so they already know.
    """

    def __init__(self):
        self.prev = {}
        self.until = {}  # key -> blink deadline (inf while waiting)

    def update(self, sessions, now):
        events = []
        for s in sessions:
            key = session_key(s)
            old = self.prev.get(key)
            self.prev[key] = s.status
            if old is None or old == s.status:
                if s.status != am.WAITING and self.until.get(key) == float("inf"):
                    self.until.pop(key, None)
                continue
            if s.status == am.WAITING:
                self.until[key] = float("inf")
                events.append(("waiting", s))
            elif s.status == am.IDLE and old in (am.PROCESSING, am.WAITING, am.WAITING_GUESS):
                self.until[key] = now + FLASH_FINISHED
                events.append(("finished", s))
            elif s.status == am.EXITED:
                self.until[key] = now + FLASH_EXITED
            else:
                self.until.pop(key, None)
        live = {session_key(s) for s in sessions}
        for key in list(self.until):
            if key not in live or self.until[key] < now:
                del self.until[key]
        for key in list(self.prev):
            if key not in live:
                del self.prev[key]
        return events

    def blinking(self):
        return bool(self.until)

    def dismiss(self):
        self.until.clear()


class DetailsWindow(Gtk.Window):
    COLS = ["Agent", "Name", "Model", "Status", "For", "In", "Cached", "Out", "Ctx", "PID", "tmux"]

    def __init__(self, app):
        super().__init__(title="agentmon")
        self.app = app
        self.set_default_size(980, 520)
        self.set_icon_name("utilities-system-monitor")
        self.connect("delete-event", lambda *a: self.hide() or True)
        self.sessions = {}
        self.wanted_key = None

        # columns: visible text..., status color, key (agent/id)
        self.store = Gtk.ListStore(*([str] * len(self.COLS)), str, str)
        self.view = Gtk.TreeView(model=self.store)
        for i, title in enumerate(self.COLS):
            cell = Gtk.CellRendererText()
            col = Gtk.TreeViewColumn(title, cell, text=i)
            if title == "Status":
                col.add_attribute(cell, "foreground", len(self.COLS))
                cell.set_property("weight", 700)
            if title == "Name":
                col.set_expand(True)
            col.set_resizable(True)
            self.view.append_column(col)
        self.view.get_selection().connect("changed", lambda *_: self.show_detail())
        self.view.connect("row-activated", lambda *_: self.on_jump())

        scroll_top = Gtk.ScrolledWindow()
        scroll_top.add(self.view)

        self.detail = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                   left_margin=10, right_margin=10, top_margin=8, bottom_margin=8)
        self.bold = self.detail.get_buffer().create_tag("bold", weight=700)
        scroll_bottom = Gtk.ScrolledWindow()
        scroll_bottom.add(self.detail)

        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        paned.pack1(scroll_top, True, False)
        paned.pack2(scroll_bottom, True, False)
        paned.set_position(230)

        self.summary = Gtk.Label(xalign=0)
        self.jump_btn = Gtk.Button(label="Go to tmux pane")
        self.jump_btn.connect("clicked", lambda *_: self.on_jump())
        copy_btn = Gtk.Button(label="Copy session id")
        copy_btn.connect("clicked", lambda *_: self.on_copy())

        bar = Gtk.Box(spacing=8, margin=8)
        bar.pack_start(self.summary, True, True, 0)
        bar.pack_end(copy_btn, False, False, 0)
        bar.pack_end(self.jump_btn, False, False, 0)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.pack_start(paned, True, True, 0)
        box.pack_start(bar, False, False, 0)
        self.add(box)

    def selected(self):
        model, it = self.view.get_selection().get_selected()
        if it is None:
            return None
        return self.sessions.get(model[it][len(self.COLS) + 1])

    def update(self, sessions, now, codex_limits):
        sel = self.selected()
        keep = self.wanted_key or (("%s/%s" % session_key(sel)) if sel else None)
        self.sessions = {"%s/%s" % session_key(s): s for s in sessions}
        rows = []
        for s in sorted(sessions, key=am.SORTS[0][1]):
            vals = [str(get(s, now)) for _, _, get in am.COLUMNS]
            rows.append(vals + [COLORS.get(s.status, "#888888"), "%s/%s" % session_key(s)])
        # Update in place so the selection and scroll position survive refreshes.
        if len(self.store) != len(rows) or any(
                self.store[i][len(self.COLS) + 1] != r[-1] for i, r in enumerate(rows)):
            self.store.clear()
            for r in rows:
                self.store.append(r)
        else:
            for i, r in enumerate(rows):
                if list(self.store[i]) != r:
                    self.store[i] = r
        if keep is None and len(self.store):
            keep = self.store[0][len(self.COLS) + 1]
        if keep:
            for row in self.store:
                if row[len(self.COLS) + 1] == keep:
                    self.view.get_selection().select_iter(row.iter)
                    break
        self.wanted_key = None
        self.summary.set_text("   ".join(am.summary_lines(sessions, codex_limits)))
        self.show_detail()

    def select(self, key):
        self.wanted_key = "%s/%s" % key

    def show_detail(self):
        s = self.selected()
        buf = self.detail.get_buffer()
        self.jump_btn.set_sensitive(bool(s and s.tmux))
        text_now = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        if s is None:
            if text_now:
                buf.set_text("")
            return
        now = time.time()
        rows = [
            ("Session", s.id), ("Directory", s.cwd), ("Model", s.model or "-"),
            ("Status", "%s for %s" % (s.status, am.fmt_age(now - s.status_since))),
            ("Tokens", "in %s · cached %s · out %s · context %s / %s" % (
                am.fmt_tokens(s.tokens_in), am.fmt_tokens(s.tokens_cached), am.fmt_tokens(s.tokens_out),
                am.fmt_tokens(s.ctx_used), am.fmt_tokens(s.ctx_window))),
            ("PID / tmux", "%s · %s" % (s.pid or "-", s.tmux or "-")),
            ("Last prompt", s.last_prompt.strip() or "-"),
            ("Last reply", s.last_message.strip() or "-"),
        ]
        new_text = "".join("%s\n%s\n\n" % r for r in rows)
        if new_text == text_now:  # avoid resetting the user's scroll/selection
            return
        buf.set_text("")
        for label, val in rows:
            buf.insert_with_tags(buf.get_end_iter(), label + "\n", self.bold)
            buf.insert(buf.get_end_iter(), val + "\n\n")

    def on_jump(self):
        s = self.selected()
        if s and s.tmux and not jump_to_tmux(s.tmux):
            self.summary.set_text("could not switch to tmux pane %s" % s.tmux)

    def on_copy(self):
        s = self.selected()
        if s:
            Gtk.Clipboard.get_default(self.get_display()).set_text(s.id, -1)


class TrayApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.monitor = am.Monitor()
        self.attention = Attention()
        self.sessions = []
        self.window = None
        self.indicator = None
        self.menu_keys = None
        self.menu_items = {}
        self.blink_on = False
        self.notify_enabled = True
        self.started = False

    # -- lifecycle
    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()  # keep running with no windows open
        Notify.init("agentmon")
        write_icons()
        self.indicator = AppIndicator.Indicator.new(
            "agentmon", "agentmon-none", AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self.indicator.set_icon_theme_path(ICON_DIR)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title("agentmon")
        self.build_menu([])
        self.refresh()
        GLib.timeout_add(REFRESH_MS, self.refresh)
        GLib.timeout_add(BLINK_MS, self.blink)

    def do_activate(self):
        # A second launch (e.g. from the app menu) opens the details window.
        if self.started:
            self.show_details()
        self.started = True

    # -- data
    def refresh(self):
        now = time.time()
        try:
            self.sessions = self.monitor.refresh(now)
        except Exception as e:  # keep the indicator alive whatever the files contain
            print("agentmon: refresh failed: %r" % e, file=sys.stderr)
            return True
        for kind, s in self.attention.update(self.sessions, now):
            self.notify(kind, s)
        self.update_menu(now)
        self.update_icon()
        if self.window is not None and self.window.get_visible():
            self.window.update(self.sessions, now, self.monitor.codex.rate_limits)
        return True

    # -- top bar icon + label
    def overall_state(self):
        live = [s.status for s in self.sessions if s.status != am.EXITED]
        if am.WAITING in live:
            return am.WAITING
        if am.PROCESSING in live or am.WAITING_GUESS in live:
            return am.PROCESSING
        return am.IDLE if live else None

    def update_icon(self):
        icon = "agentmon-" + ICON_FOR[self.overall_state()]
        if self.attention.blinking() and self.blink_on:
            icon = "agentmon-blink"
        self.indicator.set_icon_full(icon, "agentmon")
        waiting = sum(s.status == am.WAITING for s in self.sessions)
        busy = sum(s.status in (am.PROCESSING, am.WAITING_GUESS) for s in self.sessions)
        parts = []
        if waiting:
            parts.append("%d waiting" % waiting)
        if busy:
            parts.append("%d busy" % busy)
        self.indicator.set_label(" ".join(parts), "00 waiting 00 busy")

    def blink(self):
        if self.attention.blinking():
            self.blink_on = not self.blink_on
            self.update_icon()
        elif self.blink_on:
            self.blink_on = False
            self.update_icon()
        return True

    # -- menu (AppIndicator only supports menus; items open the details window)
    def build_menu(self, sessions):
        menu = Gtk.Menu()
        self.menu_items = {}
        self.header_item = Gtk.MenuItem(label="agentmon")
        self.header_item.set_sensitive(False)
        menu.append(self.header_item)
        menu.append(Gtk.SeparatorMenuItem())
        if not sessions:
            empty = Gtk.MenuItem(label="No live Claude Code or Codex sessions")
            empty.set_sensitive(False)
            menu.append(empty)
        for s in sessions:
            item = Gtk.MenuItem(label=s.name)
            item.connect("activate", self.on_session_item, session_key(s))
            menu.append(item)
            self.menu_items[session_key(s)] = item
        menu.append(Gtk.SeparatorMenuItem())
        for label, cb in [("Open details…", lambda *_: self.show_details()),
                          ("Stop blinking", lambda *_: self.on_dismiss())]:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", cb)
            menu.append(item)
        notif = Gtk.CheckMenuItem(label="Desktop notifications")
        notif.set_active(self.notify_enabled)
        notif.connect("toggled", lambda w: setattr(self, "notify_enabled", w.get_active()))
        menu.append(notif)
        quit_item = Gtk.MenuItem(label="Quit agentmon")
        quit_item.connect("activate", lambda *_: self.quit())
        menu.append(quit_item)
        menu.show_all()
        self.indicator.set_menu(menu)
        self.menu = menu

    def update_menu(self, now):
        ordered = sorted(self.sessions, key=am.SORTS[0][1])
        keys = [session_key(s) for s in ordered]
        if keys != self.menu_keys:
            self.build_menu(ordered)
            self.menu_keys = keys
        for s in ordered:
            label = session_label(s, now)
            item = self.menu_items.get(session_key(s))
            if item is not None and item.get_label() != label:
                item.set_label(label)
        header = " · ".join(l.split("  tokens")[0] for l in am.summary_lines(self.sessions, None))
        if self.header_item.get_label() != header:
            self.header_item.set_label(header)

    def on_session_item(self, _item, key):
        self.attention.until.pop(key, None)
        self.show_details(key)

    def on_dismiss(self):
        self.attention.dismiss()
        self.update_icon()

    def show_details(self, key=None):
        if self.window is None:
            self.window = DetailsWindow(self)
            self.add_window(self.window)
        if key:
            self.window.select(key)
        self.window.update(self.sessions, time.time(), self.monitor.codex.rate_limits)
        self.window.show_all()
        self.window.present()

    # -- desktop notifications
    def notify(self, kind, s):
        if not self.notify_enabled:
            return
        if kind == "waiting":
            title = "%s needs approval" % s.agent.capitalize()
            body = "%s — %s" % (s.name, am.short(s.cwd, 80))
            urgency = Notify.Urgency.CRITICAL
        else:
            title = "%s finished · %s" % (s.agent.capitalize(), s.name)
            body = am.short(s.last_message or s.cwd, 160)
            urgency = Notify.Urgency.NORMAL
        n = Notify.Notification.new(title, body, "utilities-system-monitor")
        n.set_urgency(urgency)
        n.add_action("default", "Show details", lambda *_: self.on_session_item(None, session_key(s)))
        n.add_action("details", "Show details", lambda *_: self.on_session_item(None, session_key(s)))
        if s.tmux:
            n.add_action("tmux", "Go to pane", lambda *_: jump_to_tmux(s.tmux))
        try:
            n.show()
        except GLib.Error:
            pass
        self._last_notification = n  # keep a reference so actions stay connected


# --------------------------------------------------------------------------- desktop integration

DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=agentmon
Comment=Top-bar monitor for Claude Code and Codex sessions
Exec={python} {script}
Icon={icon}
Terminal=false
Categories=Development;Monitor;
StartupNotify=false
X-GNOME-Autostart-enabled=true
"""


def desktop_paths():
    data = os.environ.get("XDG_DATA_HOME", os.path.join(am.HOME, ".local", "share"))
    config = os.environ.get("XDG_CONFIG_HOME", os.path.join(am.HOME, ".config"))
    return (os.path.join(data, "applications", "agentmon.desktop"),
            os.path.join(config, "autostart", "agentmon.desktop"))


def install_desktop():
    icon = write_icons()
    entry = DESKTOP_ENTRY.format(python=sys.executable, script=os.path.abspath(__file__), icon=icon)
    for path in desktop_paths():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(entry)
        print("wrote %s" % path)
    print("agentmon will start on login; launch it now from the app menu or run this script.")


def uninstall_desktop():
    for path in desktop_paths():
        if os.path.exists(path):
            os.remove(path)
            print("removed %s" % path)


def main():
    if sys.argv[1:2] == ["install-desktop"]:
        return install_desktop()
    if sys.argv[1:2] == ["uninstall-desktop"]:
        return uninstall_desktop()
    if AppIndicator is None:
        sys.exit("agentmon: the AppIndicator library is missing. Install it with:\n"
                 "    sudo apt install gir1.2-appindicator3-0.1")
    return TrayApp().run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
