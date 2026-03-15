"""Floating chat window — PyQt6-based UI for the cowork buddy."""

import logging

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QLineEdit, QPushButton, QLabel, QScrollArea,
    QSystemTrayIcon, QMenu, QApplication,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QFont, QKeySequence, QShortcut, QIcon, QAction

from .buddy import SIGNAL_CLOSING, SIGNAL_MINIMIZE, SIGNAL_KEEP_OPEN

log = logging.getLogger(__name__)


class SignalBridge(QObject):
    """Thread-safe bridge to trigger window from non-Qt threads."""
    show_window = pyqtSignal(str)  # trigger type
    show_conversation = pyqtSignal(str, str)  # (message, signal) — for displaying result on main thread
    show_reply = pyqtSignal(str, str)  # (reply_text, signal) — for inline reply display


class ChatWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._on_user_message = None  # callback(str) -> (str, str)
        self._on_window_dismissed = None  # callback()
        self.signal_bridge = SignalBridge()
        self.signal_bridge.show_window.connect(self._handle_show_trigger)
        self.signal_bridge.show_reply.connect(self._handle_reply)
        self._pending_trigger: str | None = None
        self._conversation_done = False  # True when buddy signals closing/minimize
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("Antidoom Buddy")
        self.setFixedSize(420, 520)

        # Frameless, always on top, translucent
        # Note: Do NOT use Qt.WindowType.Tool — on macOS it hides the window on focus loss
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Central widget with rounded corners
        central = QWidget()
        central.setObjectName("central")
        central.setStyleSheet("""
            #central {
                background-color: rgba(30, 30, 35, 245);
                border-radius: 16px;
                border: 1px solid rgba(255, 255, 255, 0.1);
            }
        """)
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        # Header
        header = QHBoxLayout()
        title = QLabel("antidoom buddy")
        title.setStyleSheet("color: rgba(255,255,255,0.6); font-size: 12px; font-weight: 600;")
        header.addWidget(title)
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
        self.input_field.setStyleSheet("""
            QLineEdit {
                background: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(255, 255, 255, 0.15);
                border-radius: 8px;
                padding: 10px 14px;
                color: white;
                font-size: 14px;
                font-family: -apple-system, 'SF Pro Text', sans-serif;
            }
            QLineEdit:focus {
                border: 1px solid rgba(120, 160, 255, 0.5);
            }
        """)
        self.input_field.returnPressed.connect(self._send_message)
        input_layout.addWidget(self.input_field)

        send_btn = QPushButton("->")
        send_btn.setFixedSize(40, 40)
        send_btn.setStyleSheet("""
            QPushButton {
                background: rgba(120, 160, 255, 0.3);
                border: none;
                border-radius: 8px;
                color: white;
                font-size: 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: rgba(120, 160, 255, 0.5);
            }
        """)
        send_btn.clicked.connect(self._send_message)
        input_layout.addWidget(send_btn)

        layout.addLayout(input_layout)

    def set_on_message(self, callback):
        """Set callback(user_text: str) -> (buddy_reply: str, signal: str)."""
        self._on_user_message = callback

    def set_on_dismissed(self, callback):
        """Set callback() called when window is dismissed without user engaging."""
        self._on_window_dismissed = callback

    def show_buddy_message(self, text: str):
        """Append a buddy message to the chat."""
        self.chat_area.append(
            f'<div style="margin: 8px 0;"><span style="color: rgba(120,160,255,0.9); '
            f'font-weight: 600;">buddy:</span> {text}</div>'
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

    def popup(self, buddy_message: str | None = None):
        """Show the window, optionally with an initial buddy message."""
        log.info("Popup window shown (message=%s)", bool(buddy_message))
        self._conversation_done = False
        self.input_field.setPlaceholderText("Type here...")
        self.chat_area.clear()
        if buddy_message:
            self.show_buddy_message(buddy_message)
        self._center_on_screen()
        self.show()
        self.raise_()
        self.activateWindow()
        self.input_field.setFocus()

    def auto_minimize(self, delay_ms: int = 2000):
        """Auto-hide after a delay. Currently unused — window requires explicit dismiss."""
        # Kept for future use if we want to re-enable auto-minimize
        QTimer.singleShot(delay_ms, self.hide)

    def _send_message(self):
        text = self.input_field.text().strip()
        if not text:
            if self._conversation_done:
                self._dismiss()
            return

        # User typed something — conversation continues even if buddy signaled closing
        self._conversation_done = False
        self.input_field.setPlaceholderText("Type here...")

        self.input_field.clear()
        self.show_user_message(text)

        if self._on_user_message:
            # Run in thread to not block UI, use signal bridge for thread-safe UI update
            import threading
            def _do():
                reply, sig = self._on_user_message(text)
                self.signal_bridge.show_reply.emit(reply, sig)
            threading.Thread(target=_do, daemon=True).start()

    def _handle_reply(self, reply: str, signal: str):
        self.show_buddy_message(reply)
        if signal in (SIGNAL_CLOSING, SIGNAL_MINIMIZE):
            self._conversation_done = True
            self.input_field.setPlaceholderText("Press Enter to close")
            self.input_field.setFocus()

    def _handle_show_trigger(self, trigger: str):
        """Called via signal bridge from trigger engine thread."""
        log.info("Show trigger received: %s", trigger)
        self._pending_trigger = trigger
        # The main app will handle starting the conversation
        if hasattr(self, '_on_trigger_callback') and self._on_trigger_callback:
            import threading
            def _do():
                self._on_trigger_callback(trigger)
            threading.Thread(target=_do, daemon=True).start()
        else:
            log.warning("No trigger callback registered on ChatWindow!")

    def set_on_trigger(self, callback):
        """Set callback(trigger_type: str) called when a trigger wants to open the window."""
        self._on_trigger_callback = callback

    def _dismiss(self):
        log.info("Window dismissed by user")
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
