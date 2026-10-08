#!/usr/bin/env python3
"""Native macOS menu-bar monitor for local and SSH agentmon targets."""
import os
import plistlib
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agentmon as am  # noqa: E402

try:
    from AppKit import (
        NSAlert, NSApplication, NSApplicationActivationPolicyAccessory,
        NSMenu, NSMenuItem, NSStatusBar, NSVariableStatusItemLength,
    )
    from Foundation import NSObject, NSTimer
except ImportError:
    NSApplication = None
    NSObject = object


FLASH_FINISHED = 20
LAUNCH_AGENT = os.path.expanduser("~/Library/LaunchAgents/dev.agentmon.tray.plist")


def overall_state(sessions):
    live = [s.status for s in sessions if s.status != am.EXITED]
    if am.WAITING in live:
        return am.WAITING
    if am.PROCESSING in live or am.WAITING_GUESS in live:
        return am.PROCESSING
    return am.IDLE if live else None


def status_title(sessions, blink=False):
    if blink:
        return "◯ agentmon"
    state = overall_state(sessions)
    dot = {am.WAITING: "🔴", am.PROCESSING: "🟡", am.IDLE: "🟢", None: "⚪"}[state]
    waiting = sum(s.status == am.WAITING for s in sessions)
    busy = sum(s.status in (am.PROCESSING, am.WAITING_GUESS) for s in sessions)
    parts = []
    if waiting:
        parts.append("%d waiting" % waiting)
    if busy:
        parts.append("%d busy" % busy)
    return "%s %s" % (dot, " ".join(parts) or "agentmon")


class Attention:
    def __init__(self):
        self.prev = {}
        self.until = {}

    def update(self, sessions, now):
        events = []
        live_keys = set()
        for s in sessions:
            key = (s.target, s.agent, s.id)
            live_keys.add(key)
            old = self.prev.get(key)
            self.prev[key] = s.status
            if old is None or old == s.status:
                continue
            if s.status == am.WAITING:
                self.until[key] = float("inf")
                events.append(("waiting", s))
            elif s.status == am.IDLE and old in (am.PROCESSING, am.WAITING,
                                                  am.WAITING_GUESS):
                self.until[key] = now + FLASH_FINISHED
                events.append(("finished", s))
            elif old == am.WAITING:
                self.until.pop(key, None)
        for key in list(self.until):
            if key not in live_keys or self.until[key] < now:
                del self.until[key]
        for key in list(self.prev):
            if key not in live_keys:
                del self.prev[key]
        return events

    def blinking(self):
        return bool(self.until)

    def dismiss(self):
        self.until.clear()


def launch_arguments():
    if getattr(sys, "frozen", False):
        return [os.path.abspath(sys.executable)]
    return [os.path.abspath(sys.executable), os.path.abspath(__file__)]


def set_start_at_login(enabled):
    if enabled:
        os.makedirs(os.path.dirname(LAUNCH_AGENT), exist_ok=True)
        data = {
            "Label": "dev.agentmon.tray",
            "ProgramArguments": launch_arguments(),
            "RunAtLoad": True,
            "KeepAlive": False,
        }
        with open(LAUNCH_AGENT, "wb") as f:
            plistlib.dump(data, f)
    elif os.path.exists(LAUNCH_AGENT):
        os.remove(LAUNCH_AGENT)


class MacTrayDelegate(NSObject):
    def initWithTargets_(self, targets):
        self = super(MacTrayDelegate, self).init()
        if self is None:
            return None
        self.targets = targets
        self.monitor = am.monitor_for_targets(targets)
        self.attention = Attention()
        self.sessions = []
        self.status_item = None
        self.menu_sessions = {}
        self.blink_on = False
        self.tick_count = 0
        self.timer = None
        return self

    def applicationDidFinishLaunching_(self, _notification):
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory)
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength)
        self.refresh()
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.5, self, "tick:", None, True)

    def applicationWillTerminate_(self, _notification):
        self.monitor.close()

    def tick_(self, _timer):
        self.tick_count += 1
        if self.tick_count % 2 == 0:
            self.refresh()
        if self.attention.blinking():
            self.blink_on = not self.blink_on
        else:
            self.blink_on = False
        self.status_item.button().setTitle_(status_title(self.sessions, self.blink_on))

    def refresh(self):
        try:
            self.sessions = self.monitor.refresh(time.time())
            self.attention.update(self.sessions, time.time())
            self.status_item.button().setTitle_(status_title(self.sessions, self.blink_on))
            self.rebuild_menu()
        except Exception as e:
            self.status_item.button().setTitle_("⚠ agentmon")
            self.rebuild_menu(str(e))

    def menu_item(self, title, action=None, enabled=True):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, action, "")
        item.setEnabled_(enabled)
        if action:
            item.setTarget_(self)
        return item

    def rebuild_menu(self, fatal_error=None):
        menu = NSMenu.alloc().init()
        header = self.menu_item(status_title(self.sessions), enabled=False)
        menu.addItem_(header)
        menu.addItem_(NSMenuItem.separatorItem())
        self.menu_sessions = {}
        for index, session in enumerate(sorted(self.sessions, key=am.SORTS[0][1])):
            age = am.fmt_age(time.time() - session.status_since) if session.status_since else ""
            title = "%s · %s · %s — %s %s" % (
                session.target, session.agent, session.name or session.id[:8],
                session.status, age)
            item = self.menu_item(title, "showSession:")
            key = str(index)
            item.setRepresentedObject_(key)
            self.menu_sessions[key] = session
            menu.addItem_(item)
        if not self.sessions:
            menu.addItem_(self.menu_item("No live Claude Code or Codex sessions",
                                         enabled=False))
        errors = self.monitor.remote_errors()
        if fatal_error:
            errors["monitor"] = fatal_error
        for target, error in errors.items():
            menu.addItem_(self.menu_item("%s: %s" % (target, am.short(error, 100)),
                                         enabled=False))
        menu.addItem_(NSMenuItem.separatorItem())
        menu.addItem_(self.menu_item("Show summary…", "showSummary:"))
        menu.addItem_(self.menu_item("Stop blinking", "stopBlinking:"))
        login = self.menu_item("Start at Login", "toggleStartAtLogin:")
        login.setState_(1 if os.path.exists(LAUNCH_AGENT) else 0)
        menu.addItem_(login)
        menu.addItem_(NSMenuItem.separatorItem())
        menu.addItem_(self.menu_item("Quit agentmon", "quit:"))
        self.status_item.setMenu_(menu)

    def showSession_(self, sender):
        session = self.menu_sessions.get(str(sender.representedObject()))
        if not session:
            return
        now = time.time()
        text = "\n".join([
            "Target: %s" % session.target,
            "Agent: %s" % session.agent,
            "Status: %s for %s" % (
                session.status,
                am.fmt_age(now - session.status_since) if session.status_since else "-"),
            "Model: %s" % (session.model or "-"),
            "Directory: %s" % (session.cwd or "-"),
            "Tokens: in %s · cached %s · out %s" % (
                am.fmt_tokens(session.tokens_in), am.fmt_tokens(session.tokens_cached),
                am.fmt_tokens(session.tokens_out)),
            "", "Last prompt:", am.short(session.last_prompt, 600) or "-",
            "", "Last reply:", am.short(session.last_message, 1000) or "-",
        ])
        self.show_alert(session.name or session.id[:8], text)

    def showSummary_(self, _sender):
        lines = am.summary_lines(self.sessions, self.monitor.rate_limits)
        for target, error in self.monitor.remote_errors().items():
            lines.append("%s: %s" % (target, error))
        self.show_alert("agentmon", "\n".join(lines))

    def show_alert(self, title, text):
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(text)
        alert.addButtonWithTitle_("OK")
        alert.runModal()

    def stopBlinking_(self, _sender):
        self.attention.dismiss()
        self.blink_on = False
        self.status_item.button().setTitle_(status_title(self.sessions))

    def toggleStartAtLogin_(self, _sender):
        try:
            set_start_at_login(not os.path.exists(LAUNCH_AGENT))
            self.rebuild_menu()
        except OSError as e:
            self.show_alert("Could not update Login Items", str(e))

    def quit_(self, _sender):
        NSApplication.sharedApplication().terminate_(self)


def main(targets=None):
    if sys.platform != "darwin" or NSApplication is None:
        raise SystemExit("agentmon_tray_macos.py requires macOS and PyObjC")
    app = NSApplication.sharedApplication()
    delegate = MacTrayDelegate.alloc().initWithTargets_(targets)
    app.setDelegate_(delegate)
    app.run()
    return 0


if __name__ == "__main__":
    main()
