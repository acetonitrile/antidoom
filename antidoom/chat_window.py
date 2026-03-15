"""Floating chat window — PyQt6-based UI for the cowork buddy."""

import logging
import subprocess

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QLineEdit, QPushButton, QLabel, QScrollArea,
    QSystemTrayIcon, QMenu, QApplication,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QFont, QKeySequence, QShortcut, QIcon, QAction, QTextCursor

from .buddy import SIGNAL_CLOSING, SIGNAL_MINIMIZE, SIGNAL_KEEP_OPEN

log = logging.getLogger(__name__)

# macOS system sounds
_SOUNDS = {
    "attention": "/System/Library/Sounds/Sosumi.aiff",
    "gentle": "/System/Library/Sounds/Glass.aiff",
}

# Theme colors
_THEME_BLUE = {
    "accent": "rgba(120, 160, 255, {a})",
    "border": "rgba(255, 255, 255, 0.1)",
    "buddy_color": "rgba(120,160,255,0.9)",
}
_THEME_RED = {
    "accent": "rgba(255, 90, 70, {a})",
    "border": "rgba(255, 90, 70, 0.3)",
    "buddy_color": "rgba(255,90,70,0.9)",
}


class SignalBridge(QObject):
    """Thread-safe bridge to trigger window from non-Qt threads."""
    show_window = pyqtSignal(str)  # trigger type
    show_conversation = pyqtSignal(str, str, str)  # (message, signal, trigger)
    show_reply = pyqtSignal(str, str)  # (reply_text, signal) — for inline reply display
    update_activity = pyqtSignal(str)  # activity status text
    preempt_conversation = pyqtSignal(str)  # trigger type — replaces stale window


class ChatWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._on_user_message = None  # callback(str) -> (str, str)
        self._on_window_dismissed = None  # callback()
        self.signal_bridge = SignalBridge()
        self.signal_bridge.show_window.connect(self._handle_show_trigger)
        self.signal_bridge.show_reply.connect(self._handle_reply)
        self.signal_bridge.update_activity.connect(self._update_status_label)
        self.signal_bridge.preempt_conversation.connect(self._preempt_for_new_trigger)
        self._pending_trigger: str | None = None
        self._conversation_done = False  # True when buddy signals closing/minimize
        self._current_theme = _THEME_BLUE
        self._typing_timer: QTimer | None = None
        self._typing_dot_count = 0
        self._typing_visible = False
        self._awaiting_initial = False  # True while waiting for first buddy message
        self._queued_message: str | None = None  # user typed while awaiting initial
        self._html_before_typing = ""  # snapshot of chat HTML before typing indicator
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("Zerei")
        self.setFixedSize(420, 520)

        # Frameless, always on top, translucent
        # Note: Do NOT use Qt.WindowType.Tool — on macOS it hides the window on focus loss
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Central widget with rounded corners
        self.central = QWidget()
        self.central.setObjectName("central")
        self.setCentralWidget(self.central)

        layout = QVBoxLayout(self.central)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        # Header
        header = QHBoxLayout()
        title = QLabel("zerei")
        title.setStyleSheet("color: rgba(255,255,255,0.6); font-size: 12px; font-weight: 600;")
        header.addWidget(title)

        # Status label — shows what AI sees (compact, single-line)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: rgba(255,255,255,0.35); font-size: 10px;")
        self.status_label.setMaximumWidth(220)
        self.status_label.setWordWrap(False)
        header.addWidget(self.status_label)

        header.addStretch()

        close_btn = QPushButton("x")
        close_btn.setFixedSize(24, 24)
        close_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: rgba(255,255,255,0.4);
                border: none;
                font-size: 14px;
            }
            QPushButton:hover { color: rgba(255,255,255,0.8); }
        """)
        close_btn.clicked.connect(self._dismiss)
        header.addWidget(close_btn)
        layout.addLayout(header)

        # Chat area
        self.chat_area = QTextEdit()
        self.chat_area.setReadOnly(True)
        self.chat_area.setStyleSheet("""
            QTextEdit {
                background: transparent;
                color: rgba(255, 255, 255, 0.9);
                border: none;
                font-size: 14px;
                font-family: -apple-system, 'SF Pro Text', sans-serif;
                line-height: 1.5;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 6px;
            }
            QScrollBar::handle:vertical {
                background: rgba(255,255,255,0.2);
                border-radius: 3px;
            }
        """)
        layout.addWidget(self.chat_area, stretch=1)

        # Input area
        input_layout = QHBoxLayout()
        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("Type here...")
        self.input_field.returnPressed.connect(self._send_message)
        input_layout.addWidget(self.input_field)

        self.send_btn = QPushButton("->")
        self.send_btn.setFixedSize(40, 40)
        self.send_btn.clicked.connect(self._send_message)
        input_layout.addWidget(self.send_btn)

        layout.addLayout(input_layout)

        # Apply default blue theme
        self._apply_theme(None)

    def _apply_theme(self, trigger: str | None):
        """Swap accent colors based on trigger type."""
        if trigger == "we_need_to_talk":
            theme = _THEME_RED
        else:
            theme = _THEME_BLUE
        self._current_theme = theme

        self.central.setStyleSheet(f"""
            #central {{
                background-color: rgba(30, 30, 35, 245);
                border-radius: 16px;
                border: 1px solid {theme['border']};
            }}
        """)
        self.input_field.setStyleSheet(f"""
            QLineEdit {{
                background: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(255, 255, 255, 0.15);
                border-radius: 8px;
                padding: 10px 14px;
                color: white;
                font-size: 14px;
                font-family: -apple-system, 'SF Pro Text', sans-serif;
            }}
            QLineEdit:focus {{
                border: 1px solid {theme['accent'].format(a='0.5')};
            }}
        """)
        self.send_btn.setStyleSheet(f"""
            QPushButton {{
                background: {theme['accent'].format(a='0.3')};
                border: none;
                border-radius: 8px;
                color: white;
                font-size: 16px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background: {theme['accent'].format(a='0.5')};
            }}
        """)

    def _play_sound(self, sound_name: str):
        """Play a macOS system sound in the background."""
        path = _SOUNDS.get(sound_name)
        if path:
            try:
                subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                log.debug("Could not play sound %s", sound_name)

    def _show_typing_indicator(self):
        """Show animated typing dots from buddy."""
        self._typing_dot_count = 0
        self._typing_visible = True
        # Snapshot the current HTML so we can restore it cleanly
        self._html_before_typing = self.chat_area.toHtml()
        self._update_typing_dots("...")
        # Animate dots
        self._typing_timer = QTimer()
        self._typing_timer.timeout.connect(self._animate_typing)
        self._typing_timer.start(400)

    def _update_typing_dots(self, dots: str):
        """Rewrite chat HTML with typing indicator appended to the snapshot."""
        color = self._current_theme["buddy_color"]
        typing_html = (
            f'<div style="margin: 8px 0;"><span style="color: {color}; '
            f'font-weight: 600;">zerei:</span> {dots}</div>'
        )
        # Restore snapshot + typing line
        self.chat_area.setHtml(self._html_before_typing)
        self.chat_area.append(typing_html)
        scrollbar = self.chat_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _animate_typing(self):
        """Cycle through dot animation."""
        if not self._typing_visible:
            return
        self._typing_dot_count = (self._typing_dot_count + 1) % 4
        dots = "." * (self._typing_dot_count + 1)
        self._update_typing_dots(dots)

    def _remove_typing_indicator(self):
        """Remove the typing indicator block by restoring the pre-typing snapshot."""
        if not self._typing_visible:
            return
        self._typing_visible = False
        if self._typing_timer:
            self._typing_timer.stop()
            self._typing_timer = None
        # Restore the HTML from before typing was added
        self.chat_area.setHtml(self._html_before_typing)

    def _update_status_label(self, text: str):
        """Update the activity status label, truncating if needed."""
        max_len = 35
        display = text if len(text) <= max_len else text[:max_len - 1] + "\u2026"
        self.status_label.setText(display)
        self.status_label.setToolTip(text)  # full text on hover

    def set_on_message(self, callback):
        """Set callback(user_text: str) -> (buddy_reply: str, signal: str)."""
        self._on_user_message = callback

    def set_on_dismissed(self, callback):
        """Set callback() called when window is dismissed without user engaging."""
        self._on_window_dismissed = callback

    def show_buddy_message(self, text: str):
        """Append a buddy message to the chat."""
        color = self._current_theme["buddy_color"]
        self.chat_area.append(
            f'<div style="margin: 8px 0;"><span style="color: {color}; '
            f'font-weight: 600;">zerei:</span> {text}</div>'
        )
        # Scroll to bottom
        scrollbar = self.chat_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def show_user_message(self, text: str):
        """Append a user message to the chat."""
        self.chat_area.append(
            f'<div style="margin: 8px 0; text-align: right;"><span style="color: rgba(180,255,180,0.9); '
            f'font-weight: 600;">you:</span> {text}</div>'
        )
        scrollbar = self.chat_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def popup(self, buddy_message: str | None = None, trigger: str | None = None,
             signal: str = SIGNAL_KEEP_OPEN):
        """Show the window, optionally with an initial buddy message."""
        log.info("Popup window shown (message=%s, trigger=%s)", bool(buddy_message), trigger)
        self._conversation_done = False
        self.input_field.setPlaceholderText("Type here...")
        self.chat_area.clear()
        self._apply_theme(trigger)

        if buddy_message:
            self.show_buddy_message(buddy_message)

        self._center_on_screen()
        self.show()
        self.raise_()
        self.activateWindow()
        self.input_field.setFocus()

        # Play sound
        if trigger == "we_need_to_talk":
            self._play_sound("attention")
        else:
            self._play_sound("gentle")

        # Honor buddy signal (e.g. grind_break with signal=closing)
        self._check_signal(signal)

    def popup_with_typing(self, trigger: str | None = None):
        """Show the window immediately with typing indicator (no message yet)."""
        log.info("Popup with typing (trigger=%s)", trigger)
        self._conversation_done = False
        self._awaiting_initial = True
        self._queued_message = None
        self.input_field.setPlaceholderText("Type here...")
        self.input_field.setEnabled(True)
        self.chat_area.clear()
        self._apply_theme(trigger)

        self._show_typing_indicator()

        self._center_on_screen()
        self.show()
        self.raise_()
        self.activateWindow()
        self.input_field.setFocus()

        # Play sound
        if trigger == "we_need_to_talk":
            self._play_sound("attention")
        else:
            self._play_sound("gentle")

    def show_initial_message(self, message: str, signal: str = SIGNAL_KEEP_OPEN):
        """Remove typing indicator and show the real first message."""
        self._awaiting_initial = False
        self._remove_typing_indicator()
        self.show_buddy_message(message)
        self._check_signal(signal)

        # If user typed while we were waiting, send their queued message now
        if self._queued_message and not self._conversation_done:
            queued = self._queued_message
            self._queued_message = None
            self._show_typing_indicator()
            if self._on_user_message:
                import threading
                def _do():
                    reply, sig = self._on_user_message(queued)
                    self.signal_bridge.show_reply.emit(reply, sig)
                threading.Thread(target=_do, daemon=True).start()
        else:
            self._queued_message = None
            self.input_field.setFocus()

    def _check_signal(self, signal: str):
        """If buddy signals closing/minimize, enter 'press enter to close' mode."""
        if signal in (SIGNAL_CLOSING, SIGNAL_MINIMIZE):
            self._conversation_done = True
            self.input_field.setPlaceholderText("Press Enter to close")
            self.input_field.setFocus()
            # Auto-close after 15s if user doesn't interact
            self._start_auto_close_timer()

    def _start_auto_close_timer(self):
        """Start a 15s timer that auto-dismisses if conversation is done."""
        if hasattr(self, '_auto_close_timer') and self._auto_close_timer is not None:
            self._auto_close_timer.stop()
        self._auto_close_timer = QTimer()
        self._auto_close_timer.setSingleShot(True)
        self._auto_close_timer.timeout.connect(self._auto_close_if_done)
        self._auto_close_timer.start(30000)

    def _auto_close_if_done(self):
        """Auto-dismiss if still in 'press enter to close' state."""
        if self._conversation_done and self.isVisible():
            log.info("Auto-closing window after 30s idle in close-ready state")
            self._dismiss()

    def auto_minimize(self, delay_ms: int = 2000):
        """Auto-hide after a delay. Currently unused — window requires explicit dismiss."""
        # Kept for future use if we want to re-enable auto-minimize
        QTimer.singleShot(delay_ms, self.hide)

    @staticmethod
    def _is_farewell(text: str) -> bool:
        """Check if the message is a clear farewell/acknowledgment."""
        normalized = text.lower().strip().rstrip("!.,~")
        farewells = {
            "bye", "cya", "later", "peace", "thanks", "thx", "ty",
            "cool", "ok", "k", "kk", "got it", "sounds good", "word",
            "cool cya", "ok bye", "ok thanks", "alright", "aight",
            "ttyl", "gotta go", "going back to work", "back to work",
        }
        return normalized in farewells

    def _send_message(self):
        text = self.input_field.text().strip()
        if not text:
            if self._conversation_done:
                self._dismiss()
            return

        # If still waiting for buddy's first message, queue the user's message
        if self._awaiting_initial:
            self._queued_message = text
            self.input_field.clear()
            self.show_user_message(text)
            log.info("Queued user message while awaiting initial: %s", text[:50])
            return

        # If conversation is wrapping up and user sends a farewell, just close
        if self._conversation_done and self._is_farewell(text):
            self.input_field.clear()
            self.show_user_message(text)
            # Brief pause so user sees their message, then dismiss
            QTimer.singleShot(500, self._dismiss)
            return

        # User typed something — conversation continues even if buddy signaled closing
        self._conversation_done = False
        self.input_field.setPlaceholderText("Type here...")
        if hasattr(self, '_auto_close_timer') and self._auto_close_timer is not None:
            self._auto_close_timer.stop()

        self.input_field.clear()
        self.show_user_message(text)

        # Show typing indicator while waiting for reply
        self._show_typing_indicator()

        if self._on_user_message:
            # Run in thread to not block UI, use signal bridge for thread-safe UI update
            import threading
            def _do():
                reply, sig = self._on_user_message(text)
                self.signal_bridge.show_reply.emit(reply, sig)
            threading.Thread(target=_do, daemon=True).start()

    def _handle_reply(self, reply: str, signal: str):
        self._remove_typing_indicator()
        self.show_buddy_message(reply)
        if signal in (SIGNAL_CLOSING, SIGNAL_MINIMIZE):
            self._conversation_done = True
            self.input_field.setPlaceholderText("Press Enter to close")
            self.input_field.setFocus()
            # Auto-close after 3s — user already engaged, buddy is wrapping up
            QTimer.singleShot(3000, self._auto_close_if_done)

    def _handle_show_trigger(self, trigger: str):
        """Called via signal bridge from trigger engine thread."""
        log.info("Show trigger received: %s", trigger)
        self._pending_trigger = trigger

        # Show window immediately with typing indicator
        self.popup_with_typing(trigger=trigger)

        # The main app will handle starting the conversation
        if hasattr(self, '_on_trigger_callback') and self._on_trigger_callback:
            import threading
            def _do():
                self._on_trigger_callback(trigger)
            threading.Thread(target=_do, daemon=True).start()
        else:
            log.warning("No trigger callback registered on ChatWindow!")

    def _preempt_for_new_trigger(self, trigger: str):
        """Replace stale window content with new trigger conversation (no hide/show flicker)."""
        log.info("Preempting stale window for trigger: %s", trigger)
        # Stop auto-close timer
        if hasattr(self, '_auto_close_timer') and self._auto_close_timer is not None:
            self._auto_close_timer.stop()
        # Clean up typing if active
        if self._typing_timer:
            self._typing_timer.stop()
            self._typing_timer = None
        self._typing_visible = False
        # Reset state
        self._conversation_done = False
        self._awaiting_initial = True
        self._queued_message = None
        self.input_field.setPlaceholderText("Type here...")
        self.input_field.setEnabled(True)
        self.chat_area.clear()
        self._apply_theme(trigger)
        self._show_typing_indicator()
        # Play sound
        if trigger == "we_need_to_talk":
            self._play_sound("attention")
        else:
            self._play_sound("gentle")
        # Start conversation in background
        if hasattr(self, '_on_trigger_callback') and self._on_trigger_callback:
            import threading
            threading.Thread(target=self._on_trigger_callback, args=(trigger,), daemon=True).start()

    def set_on_trigger(self, callback):
        """Set callback(trigger_type: str) called when a trigger wants to open the window."""
        self._on_trigger_callback = callback

    def _dismiss(self):
        log.info("Window dismissed by user")
        # Clean up typing indicator if active
        if self._typing_timer:
            self._typing_timer.stop()
            self._typing_timer = None
        self._typing_visible = False
        self.hide()
        if self._on_window_dismissed:
            self._on_window_dismissed()

    def _center_on_screen(self):
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            x = geo.right() - self.width() - 20
            y = geo.top() + 60
            self.move(x, y)
