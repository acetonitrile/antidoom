"""Trigger logic — decides when the buddy should pop up and what kind of conversation to start."""

import logging
import threading
from datetime import datetime, time
from dataclasses import dataclass

from .watcher import WatcherState, Snapshot, Activity

log = logging.getLogger(__name__)


@dataclass
class TriggerConfig:
    # Doom scroll thresholds (in consecutive snapshot counts, ~30s each)
    doom_nudge_threshold: int = 4          # ~2 min
    doom_extended_threshold: int = 10      # ~5 min
    doom_we_need_to_talk: float = 180.0    # 3 hours in a day (minutes)

    # Grind threshold
    grind_threshold: int = 180             # ~90 min of consecutive productive

    # Nudge dismissed cooldown (seconds)
    nudge_cooldown: int = 600              # 10 min

    # Scheduled check-in times
    morning_checkin: time = time(9, 0)
    midday_checkin: time = time(13, 0)
    evening_checkin: time = time(17, 30)


class TriggerEngine:
    """Evaluates watcher state and fires triggers."""

    def __init__(self, config: TriggerConfig | None = None):
        self.config = config or TriggerConfig()
        self._last_nudge_time: float = 0
        self._nudges_dismissed: int = 0
        self._on_trigger_callback = None
        self._scheduled_thread: threading.Thread | None = None
        self._running = False
        self._fired_checkins: set[str] = set()  # track which check-ins fired today
        self._grind_break_fired_at: int = 0  # productive count when last grind break fired

    def on_trigger(self, callback):
        """Register callback(trigger_type: str) called when buddy should pop up."""
        self._on_trigger_callback = callback

    def evaluate(self, snapshot: Snapshot, watcher_state: WatcherState):
        """Called after each new snapshot. Decides if a trigger should fire."""
        now = datetime.now().timestamp()
        consec_doom = watcher_state.consecutive_doom_count()
        consec_prod = watcher_state.consecutive_productive_count()
        doom_mins = watcher_state.doom_scroll_minutes(window_minutes=480)

        log.debug(
            "Evaluating: activity=%s consec_doom=%d consec_prod=%d doom_8h=%.1fmin dismissed=%d",
            snapshot.activity.value, consec_doom, consec_prod, doom_mins, self._nudges_dismissed,
        )

        # Reset grind break tracker when productive streak breaks
        if consec_prod == 0:
            self._grind_break_fired_at = 0

        # Cooldown check
        cooldown_remaining = self.config.nudge_cooldown - (now - self._last_nudge_time)
        if cooldown_remaining > 0:
            log.debug("In cooldown: %.0fs remaining", cooldown_remaining)
            return

        trigger = None

        # "We need to talk" — bad day
        if doom_mins >= self.config.doom_we_need_to_talk:
            trigger = "we_need_to_talk"

        # Extended doom scroll
        elif consec_doom >= self.config.doom_extended_threshold:
            if self._nudges_dismissed > 0:
                trigger = "extended_nudge"
            else:
                trigger = "nudge"

        # Initial doom scroll nudge
        elif consec_doom >= self.config.doom_nudge_threshold:
            trigger = "nudge"

        # Long grind — suggest a break (only once per streak)
        elif consec_prod >= self.config.grind_threshold and consec_prod > self._grind_break_fired_at:
            trigger = "grind_break"

        if trigger and self._on_trigger_callback:
            log.info("TRIGGER FIRED: %s", trigger)
            self._last_nudge_time = now
            if trigger == "grind_break":
                self._grind_break_fired_at = consec_prod
            self._on_trigger_callback(trigger)
        elif trigger:
            log.warning("Trigger %s would fire but no callback registered!", trigger)
        else:
            log.debug("No trigger fired")

    def reset_cooldown(self):
        """Reset cooldown timer — called after any conversation ends to prevent immediate re-trigger."""
        self._last_nudge_time = datetime.now().timestamp()
        log.info("Cooldown reset (conversation ended)")

    def dismiss_nudge(self):
        """Called when user dismisses a doom-related nudge without engaging."""
        self._nudges_dismissed += 1
        log.info("Nudge dismissed (total dismissed: %d)", self._nudges_dismissed)

    def engaged(self):
        """Called when user engages with a doom-related nudge."""
        log.info("User engaged, resetting dismissed count")
        self._nudges_dismissed = 0

    def start_scheduled_checkins(self):
        """Background thread that fires scheduled check-ins."""
        self._running = True
        self._scheduled_thread = threading.Thread(target=self._checkin_loop, daemon=True)
        self._scheduled_thread.start()
        log.info("Scheduled check-ins started")

    def stop(self):
        self._running = False

    def _checkin_loop(self):
        import time as time_mod
        while self._running:
            now = datetime.now()
            today_key = now.strftime("%Y-%m-%d")
            current_time = now.time()

            checkins = [
                ("morning_checkin", self.config.morning_checkin),
                ("midday_checkin", self.config.midday_checkin),
                ("evening_checkin", self.config.evening_checkin),
            ]

            for name, scheduled_time in checkins:
                key = f"{today_key}_{name}"
                if key not in self._fired_checkins:
                    # Fire if we're within 5 minutes after scheduled time
                    scheduled_minutes = scheduled_time.hour * 60 + scheduled_time.minute
                    current_minutes = current_time.hour * 60 + current_time.minute
                    if 0 <= (current_minutes - scheduled_minutes) <= 5:
                        self._fired_checkins.add(key)
                        if self._on_trigger_callback:
                            self._on_trigger_callback(name)

            time_mod.sleep(30)
