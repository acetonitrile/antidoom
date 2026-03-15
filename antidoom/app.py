"""Main application — wires everything together."""

import logging
import sys
import signal
from pathlib import Path

from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PyQt6.QtGui import QAction, QIcon, QPixmap, QPainter, QColor, QFont
from PyQt6.QtCore import QTimer, Qt

from .watcher import Watcher, Activity
from .buddy import Buddy, SIGNAL_CLOSING, SIGNAL_MINIMIZE
from .memory import Memory
from .triggers import TriggerEngine, TriggerConfig
from .chat_window import ChatWindow

log = logging.getLogger(__name__)

LOG_DIR = Path(__file__).resolve().parent.parent / ".antidoom"

# Emoji per activity type for the status label
_ACTIVITY_EMOJI = {
    Activity.PRODUCTIVE: "\U0001f7e2",     # green circle
    Activity.DOOM_SCROLLING: "\U0001f534",  # red circle
    Activity.AMBIGUOUS: "\U0001f7e1",       # yellow circle
}


def _setup_logging():
    """Configure logging to both console and file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "antidoom.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # Console handler — DEBUG level so you see everything
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG)
    console.setFormatter(fmt)
    root.addHandler(console)

    # File handler — DEBUG level, appends
    fh = logging.FileHandler(log_file, mode="a")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Silence noisy third-party loggers (httpx, anthropic dump entire base64 payloads)
    for noisy in ("httpx", "httpcore", "anthropic", "urllib3", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.info("Logging to console and %s", log_file)


def _make_tray_icon() -> QIcon:
    """Create a simple colored icon for the menu bar."""
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))  # transparent background

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Draw a circle with a gradient-ish look
    painter.setBrush(QColor(100, 140, 255))  # blue
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(4, 4, size - 8, size - 8)

    # Draw "AD" text
    painter.setPen(QColor(255, 255, 255))
    font = QFont("Helvetica", 22, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "Z")
    painter.end()

    return QIcon(pixmap)


class AntidoomApp:
    def __init__(self, debug: bool = False):
        self.memory = Memory()
        self._debug = debug

        if debug:
            log.info("DEBUG MODE: compressed timings for testing")
            watcher_interval = 10        # 10s between screenshots (vs 30s)
            trigger_config = TriggerConfig(
                doom_nudge_threshold=2,   # 2 snapshots (~20s vs ~2min)
                doom_extended_threshold=4, # 4 snapshots (~40s vs ~5min)
                doom_we_need_to_talk=5.0, # 5 min (vs 3 hours)
                grind_threshold=6,        # ~1 min (vs 90 min)
                nudge_cooldown=30,        # 30s initial (→ 15s → 8s → floor)
                nudge_cooldown_floor=5,   # 5s floor in debug
                absence_threshold=120,    # 2 min (vs 4 hours)
            )
        else:
            watcher_interval = 30
            trigger_config = TriggerConfig()

        self.watcher = Watcher(interval_seconds=watcher_interval, memory=self.memory)
        self.buddy = Buddy(memory=self.memory, watcher_state=self.watcher.state)
        self.triggers = TriggerEngine(config=trigger_config)
        self._onboarding = False  # True while onboarding conversation is active
        self._conversation_active = False  # True while any conversation window is open
        self._user_engaged = False  # True if user replied during current conversation

        # Wire up watcher -> triggers
        self.watcher.on_snapshot(
            lambda snap: self.triggers.evaluate(snap, self.watcher.state)
        )

        self.qt_app = QApplication(sys.argv)
        self.qt_app.setQuitOnLastWindowClosed(False)

        self.window = ChatWindow()
        self.window.set_on_message(self._handle_user_message)
        self.window.set_on_dismissed(self._handle_dismissed)
        self.window.set_on_trigger(self._handle_trigger)

        # Wire signal for showing conversation on main thread (safe from any thread)
        self.window.signal_bridge.show_conversation.connect(self._show_conversation)

        # Wire up activity status updates
        self.watcher.on_snapshot(self._on_snapshot_for_status)
        self.window.signal_bridge.update_activity.connect(self._update_tray_tooltip)
        self._pending_tray_tooltip = "Zerei"

        # Triggers fire into the window via signal bridge — but gate on active state
        self.triggers.on_trigger(self._on_trigger_fired)

        self._setup_tray()

    def _on_snapshot_for_status(self, snapshot):
        """Update the status label and tray tooltip with latest classification."""
        emoji = _ACTIVITY_EMOJI.get(snapshot.activity, "")
        # Short form for status label, full form for tray tooltip
        short = f"\U0001f441 {snapshot.app_name} {emoji}"
        full = f"\U0001f441 {snapshot.app_name} \u2014 {snapshot.description} {emoji}"
        # Both updates must happen on Qt main thread — use signal for label,
        # and pack tray tooltip into the same signal handler
        self._pending_tray_tooltip = f"Antidoom Buddy\n{full}"
        self.window.signal_bridge.update_activity.emit(short)

    def _update_tray_tooltip(self, _text: str):
        """Update tray tooltip — runs on Qt main thread via signal."""
        if hasattr(self, 'tray'):
            self.tray.setToolTip(self._pending_tray_tooltip)

    def _setup_tray(self):
        """System tray icon with menu."""
        self.tray = QSystemTrayIcon()
        self.tray.setIcon(_make_tray_icon())
        self.tray.setToolTip("Zerei")

        menu = QMenu()

        open_action = QAction("Open Zerei", self.qt_app)
        open_action.triggered.connect(lambda: self._open_from_tray("user_initiated"))
        menu.addAction(open_action)

        reflect_action = QAction("Reflect", self.qt_app)
        reflect_action.triggered.connect(lambda: self._open_from_tray("reflection"))
        menu.addAction(reflect_action)

        menu.addSeparator()

        profile_action = QAction("Update Profile", self.qt_app)
        profile_action.triggered.connect(self._start_onboarding)
        menu.addAction(profile_action)

        export_action = QAction("Export Chat History", self.qt_app)
        export_action.triggered.connect(self._export_chat)
        menu.addAction(export_action)

        menu.addSeparator()

        quit_action = QAction("Quit", self.qt_app)
        quit_action.triggered.connect(self.qt_app.quit)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.show()

    def _on_trigger_fired(self, trigger: str):
        """Called from trigger engine (background thread). Gates on active conversation."""
        if self._onboarding:
            log.info("Trigger %s suppressed — onboarding in progress", trigger)
            return
        if self._conversation_active:
            if self.window._conversation_done:
                # Stale window — preempt it with the new trigger
                log.info("Trigger %s preempting stale (done) window", trigger)
                self._cleanup_stale_conversation()
                self.window.signal_bridge.preempt_conversation.emit(trigger)
            else:
                log.info("Trigger %s suppressed — conversation already active", trigger)
            return
        self.window.signal_bridge.show_window.emit(trigger)

    def _cleanup_stale_conversation(self):
        """Clean up a stale conversation before preempting with a new one."""
        self.triggers.reset_cooldown()
        if not self._user_engaged and self.buddy.current_convo and self.buddy.current_convo.trigger in (
            "nudge", "extended_nudge", "we_need_to_talk",
        ):
            self.triggers.dismiss_nudge(self.buddy.current_convo.trigger)
        # Snapshot the convo before start_conversation overwrites it
        stale_convo = self.buddy.current_convo
        import threading
        threading.Thread(target=self.buddy.extract_memories_from, args=(stale_convo,), daemon=True).start()

    def _open_from_tray(self, trigger: str):
        """Open buddy from tray menu — show typing immediately, API call in background."""
        if self._conversation_active:
            return
        self._conversation_active = True
        self._user_engaged = False
        self.window.popup_with_typing(trigger=trigger)
        import threading
        threading.Thread(target=self._handle_trigger, args=(trigger,), daemon=True).start()

    def _handle_trigger(self, trigger: str):
        """Start a buddy conversation for the given trigger.

        When called from _handle_show_trigger, the window is already visible with
        typing indicator. We just need to get the message and deliver it.
        """
        log.info("Handling trigger: %s", trigger)
        self._conversation_active = True
        self._user_engaged = False
        message, sig = self.buddy.start_conversation(trigger)
        log.info("Conversation started, showing message (signal=%s)", sig)

        # Emit signal to show conversation on main Qt thread
        self.window.signal_bridge.show_conversation.emit(message, sig, trigger)

    def _show_conversation(self, message: str, signal: str, trigger: str):
        """Called on main thread when conversation message is ready."""
        # If window is already showing (from _handle_show_trigger with typing indicator),
        # just replace the typing indicator with the real message.
        if self.window.isVisible() and self.window._typing_visible:
            self.window.show_initial_message(message, signal=signal)
        else:
            # Direct open (e.g. tray menu click, onboarding) — show popup normally
            self._conversation_active = True
            self._user_engaged = False
            self.window.popup(buddy_message=message, trigger=trigger, signal=signal)

    def _handle_user_message(self, text: str) -> tuple[str, str]:
        """Called from chat window when user sends a message."""
        if self._onboarding:
            message, sig = self.buddy.reply_onboarding(text)
            if sig == SIGNAL_CLOSING:
                self._onboarding = False
                log.info("Onboarding complete")
                self._start_background_services()
            return message, sig
        self._user_engaged = True
        # Only reset doom escalation for doom-related triggers
        if self.buddy.current_convo and self.buddy.current_convo.trigger in (
            "nudge", "extended_nudge", "we_need_to_talk",
        ):
            self.triggers.engaged()
        return self.buddy.reply(text)

    def _handle_dismissed(self):
        """User closed the window."""
        self._conversation_active = False
        # Reset cooldown so triggers don't fire immediately after a conversation
        self.triggers.reset_cooldown()
        if self._onboarding:
            self._onboarding = False
            log.info("Onboarding dismissed")
            self._start_background_services()
        elif not self._user_engaged and self.buddy.current_convo and self.buddy.current_convo.trigger in (
            "nudge", "extended_nudge", "we_need_to_talk",
        ):
            # Only count as dismissed if user never replied
            self.triggers.dismiss_nudge(self.buddy.current_convo.trigger)

        # Extract memories from the conversation in background
        import threading
        threading.Thread(target=self.buddy.extract_memories, daemon=True).start()

    def _start_onboarding(self):
        """Start or redo the onboarding conversation."""
        import threading
        def _do():
            self._onboarding = True
            message, sig = self.buddy.start_onboarding()
            self.window.signal_bridge.show_conversation.emit(message, sig, "onboarding")
        threading.Thread(target=_do, daemon=True).start()

    def _debug_shutdown(self):
        log.info("Debug mode: 15-minute timeout reached, shutting down")
        self.watcher.stop()
        self.triggers.stop()
        self.qt_app.quit()

    def _export_chat(self):
        """Export all conversations to a text file."""
        path = self.memory.export_conversations_text()
        log.info("Chat history exported to %s", path)
        # Show notification via tray
        self.tray.showMessage(
            "Chat Exported",
            f"Saved to {path}",
            QSystemTrayIcon.MessageIcon.Information,
            3000,
        )

    def _start_background_services(self):
        """Start watcher."""
        if not self.watcher._running:
            log.info("Starting background services (watcher)")
            self.watcher.start()

    def run(self):
        # If no profile yet, start onboarding first — background services start after
        if self.buddy.needs_onboarding():
            log.info("No profile found — starting onboarding (watcher paused until complete)")
            QTimer.singleShot(500, self._start_onboarding)
        else:
            # Profile exists, start services immediately
            # goal_setting will fire automatically on first snapshot (absence detection)
            self._start_background_services()

        # Allow Ctrl+C to work
        signal.signal(signal.SIGINT, signal.SIG_DFL)

        # Periodic timer to keep Python's signal handling alive
        timer = QTimer()
        timer.timeout.connect(lambda: None)
        timer.start(500)

        # Auto-kill in debug mode after 15 minutes
        if self._debug:
            debug_timeout_ms = 15 * 60 * 1000
            log.info("Debug mode: will auto-quit in 15 minutes")
            QTimer.singleShot(debug_timeout_ms, self._debug_shutdown)

        log.info("Antidoom Buddy is running. Check the menu bar.")
        log.info("Right-click the tray icon to open manually or quit.")
        log.info("Snapshots log: ~/.antidoom/snapshots.log")
        log.info("Full log: ~/.antidoom/antidoom.log")

        sys.exit(self.qt_app.exec())


def main():
    _setup_logging()
    debug = "--debug" in sys.argv
    app = AntidoomApp(debug=debug)
    app.run()
