"""Screenshot watcher — captures screen periodically and classifies activity via Claude vision."""

import base64
import json
import logging
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

import anthropic

log = logging.getLogger(__name__)

VISION_MODEL = "claude-sonnet-4-6"


def get_idle_seconds() -> float:
    """Get seconds since last user input (mouse/keyboard) via macOS CoreGraphics."""
    try:
        import Quartz
        return Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateCombinedSessionState,
            Quartz.kCGAnyInputEventType,
        )
    except Exception:
        return 0.0


class Activity(Enum):
    PRODUCTIVE = "productive"
    DOOM_SCROLLING = "doom_scrolling"
    AMBIGUOUS = "ambiguous"


@dataclass
class Snapshot:
    timestamp: datetime
    activity: Activity
    description: str  # short LLM description of what user is doing
    app_name: str  # best guess at foreground app


@dataclass
class WatcherState:
    """Sliding window of recent snapshots for trend detection."""
    history: list[Snapshot] = field(default_factory=list)
    max_history: int = 120  # ~1 hour at 30s intervals

    def add(self, snapshot: Snapshot):
        self.history.append(snapshot)
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]

    def recent(self, minutes: int = 5) -> list[Snapshot]:
        cutoff = datetime.now().timestamp() - (minutes * 60)
        return [s for s in self.history if s.timestamp.timestamp() >= cutoff]

    def doom_scroll_minutes(self, window_minutes: int = 30) -> float:
        """How many minutes of doom scrolling in the last N minutes."""
        snaps = self.recent(window_minutes)
        doom_count = sum(1 for s in snaps if s.activity == Activity.DOOM_SCROLLING)
        # Each snapshot represents ~15s
        return doom_count * 0.25

    def consecutive_doom_count(self) -> int:
        """How many consecutive recent snapshots are doom scrolling."""
        count = 0
        for s in reversed(self.history):
            if s.activity == Activity.DOOM_SCROLLING:
                count += 1
            else:
                break
        return count

    def recent_ambiguous(self) -> list[Snapshot]:
        """Return deduplicated ambiguous snapshots from the current session."""
        seen = set()
        results = []
        for s in reversed(self.history):
            if s.activity == Activity.AMBIGUOUS:
                # Dedupe by app name to avoid "Discord" x 10
                if s.app_name not in seen:
                    seen.add(s.app_name)
                    results.append(s)
        return results

    def consecutive_ambiguous_count(self) -> int:
        """How many consecutive recent snapshots are ambiguous."""
        count = 0
        for s in reversed(self.history):
            if s.activity == Activity.AMBIGUOUS:
                count += 1
            else:
                break
        return count

    def consecutive_productive_count(self) -> int:
        count = 0
        for s in reversed(self.history):
            if s.activity == Activity.PRODUCTIVE:
                count += 1
            else:
                break
        return count


CLASSIFICATION_PROMPT_BASE = """\
You are classifying a user's computer screenshot for Zerei, a productivity companion app.

Your job is to judge whether the user is doing REAL WORK or not. Be skeptical. If something doesn't look like it's clearly contributing to their work, it's probably not.

Classify the activity into exactly one category:
- "productive": Actively doing work — coding in an IDE, writing docs, spreadsheets, work communication. The activity must be DIRECTLY and OBVIOUSLY related to their current project. Do not assume a technical topic is work-related just because the user is a developer — it must be specifically relevant to what they're building.
- "doom_scrolling": ANY leisure, entertainment, or distraction browsing. This includes social media, news feeds, YouTube, Reddit, torrent sites, shopping, forums, games, or anything else that is clearly not work. Communication apps (Discord, Slack, email) are doom_scrolling if the TOPIC of conversation has no connection to the user's current projects/goals. READ the actual content on screen and judge by topic, not by app name.
- "ambiguous": ONLY use this when you genuinely cannot tell — the topic is plausibly related to their work but you're not sure. Do NOT use this as a safe default. If you can read the content and it's not about their projects, it's doom_scrolling.
"""

CLASSIFICATION_PROMPT_SUFFIX = """
Respond with JSON only, no markdown:
{"activity": "<category>", "description": "<READ THE SCREEN. If the content is generic/unremarkable (scrolling a feed, browsing thumbnails), keep it short — 10-15 words. But if there's something specific and interesting (an article they're reading, a conversation topic, specific code they're debugging, a video topic, a thread they're deep in), describe it in detail — quote headlines, thread titles, tweet content, code context. Up to 80 words when the content is worth capturing.>", "app_name": "<best guess at app name>"}
"""


def build_classification_prompt(profile: dict | None = None) -> str:
    """Build classification prompt, optionally with user context."""
    parts = [CLASSIFICATION_PROMPT_BASE]

    if profile:
        context_lines = []
        if profile.get("projects"):
            context_lines.append(f"Currently working on: {profile['projects']}")
        if profile.get("distractions"):
            context_lines.append(f"Known distractions (always classify as doom_scrolling): {profile['distractions']}")
        if context_lines:
            parts.append("\nUser context:\n" + "\n".join(context_lines))
            parts.append("\nUse the user's project context to judge relevance. If what they're doing has NO clear connection to their current work, classify as \"doom_scrolling\". Browsing entertainment, media, shopping, or anything unrelated to their project is doom_scrolling — you don't need to be told every specific site.")

    parts.append(CLASSIFICATION_PROMPT_SUFFIX)
    return "\n".join(parts)


def capture_screenshot() -> bytes:
    """Capture screenshot using macOS screencapture. Returns resized PNG bytes (<5MB)."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp_path = f.name
    log.debug("Capturing screenshot to %s", tmp_path)
    subprocess.run(
        ["screencapture", "-x", "-C", tmp_path],
        check=True,
        capture_output=True,
    )
    # Resize to max 1920px wide — keeps text readable, stays well under 5MB
    subprocess.run(
        ["sips", "--resampleWidth", "1920", tmp_path],
        check=True,
        capture_output=True,
    )
    data = Path(tmp_path).read_bytes()
    log.debug("Screenshot captured: %d bytes", len(data))
    Path(tmp_path).unlink(missing_ok=True)
    return data


def classify_screenshot(client: anthropic.Anthropic, png_bytes: bytes, profile: dict | None = None) -> Snapshot:
    """Send screenshot to Claude vision and return a Snapshot."""
    b64 = base64.b64encode(png_bytes).decode("utf-8")
    prompt = build_classification_prompt(profile)
    log.debug("Sending screenshot to Claude for classification (%d bytes)", len(png_bytes))

    response = client.messages.create(
        model=VISION_MODEL,
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )

    text = response.content[0].text.strip()
    log.debug("Claude classification raw response: %s", text)
    parsed = json.loads(text)
    snapshot = Snapshot(
        timestamp=datetime.now(),
        activity=Activity(parsed["activity"]),
        description=parsed.get("description", ""),
        app_name=parsed.get("app_name", ""),
    )
    log.info(
        "Snapshot: activity=%s app=%s desc='%s'",
        snapshot.activity.value, snapshot.app_name, snapshot.description,
    )
    return snapshot


class Watcher:
    """Background thread that captures and classifies screenshots."""

    def __init__(self, interval_seconds: int = 30, snapshots_log: Path | None = None, memory=None, idle_threshold: float = 120.0):
        self.interval = interval_seconds
        self.idle_threshold = idle_threshold
        self._was_idle = False
        self.state = WatcherState()
        self.client = anthropic.Anthropic(timeout=60.0)
        self._memory = memory  # Memory instance for profile-aware classification
        self._running = False
        self._thread: threading.Thread | None = None
        self._on_snapshot_callbacks: list = []
        # File where every snapshot classification is appended
        from .memory import DATA_DIR
        self._snapshots_log = snapshots_log or (DATA_DIR / "snapshots.log")
        self._snapshots_log.parent.mkdir(parents=True, exist_ok=True)

    def on_snapshot(self, callback):
        """Register a callback(Snapshot) called after each classification."""
        self._on_snapshot_callbacks.append(callback)

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("Watcher started (interval=%ds)", self.interval)

    def stop(self):
        self._running = False

    def _append_snapshot_to_log(self, snapshot: Snapshot):
        """Append a snapshot result line to the persistent log file."""
        line = (
            f"{snapshot.timestamp.isoformat()} | "
            f"{snapshot.activity.value:<25} | "
            f"{snapshot.app_name:<20} | "
            f"{snapshot.description}\n"
        )
        with open(self._snapshots_log, "a") as f:
            f.write(line)

    def _append_idle_marker(self, marker: str):
        line = f"{datetime.now().isoformat()} | {marker:<25} | {'system':<20} | User {'went idle' if marker == 'idle_start' else 'returned from idle'}\n"
        with open(self._snapshots_log, "a") as f:
            f.write(line)

    def _loop(self):
        log.debug("Watcher loop started")
        while self._running:
            cycle_start = time.time()
            try:
                idle_secs = get_idle_seconds()
                if idle_secs >= self.idle_threshold:
                    if not self._was_idle:
                        log.info("User idle (%.0fs) — pausing screenshots", idle_secs)
                        self._append_idle_marker("idle_start")
                        self._was_idle = True
                    time.sleep(self.interval)
                    continue

                if self._was_idle:
                    log.info("User returned after idle — resuming screenshots")
                    self._append_idle_marker("idle_end")
                    self._was_idle = False

                png = capture_screenshot()
                profile = self._memory.get_profile() if self._memory else None
                snapshot = classify_screenshot(self.client, png, profile=profile)
                self.state.add(snapshot)
                self._append_snapshot_to_log(snapshot)
                log.debug(
                    "State: %d snapshots, %d consecutive doom, %d consecutive productive",
                    len(self.state.history),
                    self.state.consecutive_doom_count(),
                    self.state.consecutive_productive_count(),
                )
                for i, cb in enumerate(self._on_snapshot_callbacks):
                    try:
                        cb(snapshot)
                    except Exception as cb_err:
                        log.error("Watcher callback %d error: %s", i, cb_err, exc_info=True)
            except Exception as e:
                log.error("Watcher error: %s", e, exc_info=True)
            # Sleep for remaining interval time (subtract API call duration)
            elapsed = time.time() - cycle_start
            remaining = max(1, self.interval - elapsed)
            log.debug("Watcher cycle took %.1fs, sleeping %.1fs", elapsed, remaining)
            time.sleep(remaining)
