"""Trigger logic — decides when the buddy should pop up and what kind of conversation to start."""

import logging
from datetime import datetime
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

    # Nudge cooldown — exponential backoff: halved on each dismiss, min=nudge_cooldown_floor
    nudge_cooldown: int = 300              # 5 min initial (→ 2.5min → 1.25min → floor)
    nudge_cooldown_floor: int = 60         # 1 min minimum

    # Ambiguous activity — ask user to clarify
    ambiguous_threshold: int = 10          # ~5 min of consecutive ambiguous

    # Welcome back / goal check-in after absence
    absence_threshold: float = 4 * 3600    # 4 hours (seconds)


class TriggerEngine:
    """Evaluates watcher state and fires triggers."""

    def __init__(self, config: TriggerConfig | None = None):
        self.config = config or TriggerConfig()
        self._last_nudge_time: float = 0
        self._nudges_dismissed: int = 0
        self._on_trigger_callback = None
        self._running = False
        self._grind_break_fired_at: int = 0  # productive count when last grind break fired
        self._last_snapshot_time: float = 0  # timestamp of last snapshot
        self._welcome_back_fired: bool = False  # only fire once per absence

    def on_trigger(self, callback):
        """Register callback(trigger_type: str) called when buddy should pop up."""
        self._on_trigger_callback = callback

    def evaluate(self, snapshot: Snapshot, watcher_state: WatcherState):
        """Called after each new snapshot. Decides if a trigger should fire."""
        now = datetime.now().timestamp()

        # Goal check-in — fires on first snapshot (app launch) and after long absence
        if self._last_snapshot_time == 0:
            # First snapshot since app started — always check in
            self._welcome_back_fired = True
            self._last_snapshot_time = now
            log.info("TRIGGER FIRED: goal_setting (app launch)")
            if self._on_trigger_callback:
                self._on_trigger_callback("goal_setting")
            return
        gap = now - self._last_snapshot_time
        if gap >= self.config.absence_threshold and not self._welcome_back_fired:
            self._welcome_back_fired = True
            log.info("TRIGGER FIRED: goal_setting (absence of %.0f min)", gap / 60)
            self._last_snapshot_time = now
            if self._on_trigger_callback:
                self._on_trigger_callback("goal_setting")
            return  # don't evaluate other triggers this tick
        self._last_snapshot_time = now
        # Reset welcome_back flag once we've had a normal tick
        if self._welcome_back_fired:
            self._welcome_back_fired = False

        consec_doom = watcher_state.consecutive_doom_count()
        consec_prod = watcher_state.consecutive_productive_count()
        consec_ambiguous = watcher_state.consecutive_ambiguous_count()
        doom_mins = watcher_state.doom_scroll_minutes(window_minutes=480)

        log.debug(
            "Evaluating: activity=%s consec_doom=%d consec_prod=%d consec_ambig=%d doom_8h=%.1fmin dismissed=%d",
            snapshot.activity.value, consec_doom, consec_prod, consec_ambiguous, doom_mins, self._nudges_dismissed,
        )

        # Reset grind break tracker when productive streak breaks
        if consec_prod == 0:
            self._grind_break_fired_at = 0

        # Cooldown check — exponential backoff: halve cooldown on each dismiss, floor at 60s
        effective_cooldown = self.config.nudge_cooldown
        for _ in range(self._nudges_dismissed):
            effective_cooldown = max(self.config.nudge_cooldown_floor, effective_cooldown // 2)
        cooldown_remaining = effective_cooldown - (now - self._last_nudge_time)
        if cooldown_remaining > 0:
            log.debug("In cooldown: %.0fs remaining (effective=%ds, dismissed=%d)",
                       cooldown_remaining, effective_cooldown, self._nudges_dismissed)
            return

        trigger = None

        # "We need to talk" — bad day (only when currently doom scrolling)
        if doom_mins >= self.config.doom_we_need_to_talk and consec_doom > 0:
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

        # Ambiguous activity — ask user to clarify what they're doing
        elif consec_ambiguous >= self.config.ambiguous_threshold:
            trigger = "ambiguous_checkin"

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

    def dismiss_nudge(self, trigger: str = "nudge"):
        """Called when user dismisses a doom-related conversation without engaging."""
        self._nudges_dismissed += 1
        log.info("%s dismissed without engagement (total dismissed: %d)", trigger, self._nudges_dismissed)

    def engaged(self):
        """Called when user engages with a doom-related nudge."""
        log.info("User engaged, resetting dismissed count")
        self._nudges_dismissed = 0

    def stop(self):
        self._running = False
