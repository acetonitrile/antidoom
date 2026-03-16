# Zerei — Design & Decisions

## Vision

An AI companion that lives on your desktop, watches how you spend your time, and actually remembers. A cowork-lite rubber duck that holds context across sessions.

The core insight: for people who struggle with executive function, the difference between "tool you open" and "entity who checks in" is everything. The activation energy of self-reflection drops to near zero when a window pops up and asks you a simple question.

## Core Loop

**Screenshot → Classify → Decide → Intervene (conversationally) → Extract memories/goals → Loop**

A background watcher takes screenshots every 15s, classifies activity via Claude vision against the user's stated goals, and triggers a floating chat window when something is worth talking about. After every conversation, memories and goals are extracted and persisted.

## Key Design Decisions

### Conversation window behavior

- **The chat window IS the product.** Everything flows through a single conversational interface. No dashboards, no settings pages.
- **One conversation at a time.** If a trigger fires while a conversation is active, refocus the existing window instead of opening a new one. The user must explicitly dismiss a conversation to end it.
- **Refocus, don't interrupt.** If the user drifts (doom scrolls) while a conversation is open, bring the window back to front. Don't open a second window or silently suppress the trigger.
- **Stale windows can be preempted.** If a conversation is "done" (buddy signaled closing, user hasn't interacted), a new trigger can replace it.
- **Auto-close is context-dependent.** Most conversations auto-close after buddy signals closing (3s after reply, 30s idle). Onboarding never auto-closes — let the user read and close manually.
- **Window signals:** Each buddy message returns a signal:
  - `keep_open` — conversation ongoing
  - `closing` — buddy wrapping up, window enters "press Enter to close" state
  - `minimize` — immediate close

### Classification

- **Classify by TOPIC, not app name.** Discord, Slack, email can be work or distraction. The classifier reads actual screen content (tweet text, thread titles, code context, article headlines) and judges relevance against the user's profile/goals.
- **Adaptive detail in descriptions.** Generic scrolling = 10-15 words. Interesting content (specific article, conversation, code) = up to 80 words. Don't waste tokens on "Scrolling Twitter feed" × 50, but capture the good stuff for the daily journal.
- **Be skeptical.** If it doesn't clearly connect to the user's stated projects, it's doom_scrolling. "Ambiguous" is a last resort, not a safe default.
- **Screenshots resized to 1920px** before sending to API. Retina displays produce 3MB+ PNGs; resizing keeps them under 1MB.

### Triggers & escalation

- **Escalating nudges:** gentle nudge (2min doom) → extended nudge (5min / dismissed previous) → "we need to talk" (3hr total in a day)
- **Exponential backoff on dismissals.** Cooldown starts at 2min, halves each time user dismisses without engaging, floors at 30s.
- **Grind breaks.** After 90min of continuous productive work, suggest a break. One-shot, doesn't repeat for the same streak.
- **Ambiguous check-ins.** After 5min of consecutive ambiguous activity, ask the user to clarify what they're doing.
- **Goal setting on every app launch.** Don't wait for the first screenshot — show the goal-setting window immediately. Also fires after 4h+ absence.

### Idle detection

- **Skip screenshots when user is AFK.** Uses macOS `CGEventSourceSecondsSinceLastEventType` to check seconds since last mouse/keyboard input. If idle > threshold, skip capture and API call entirely.
- **Thresholds:** 120s in production, 30s in debug.
- **Idle markers in snapshots log.** `idle_start` and `idle_end` lines are written to `snapshots.log` so the daily journal knows the user was away (vs the app being off).
- **Graceful fallback.** If the Quartz API is unavailable, `get_idle_seconds()` returns 0 (assume active, never skip).
- **Interacts with absence trigger naturally.** Skipped screenshots create a time gap that the existing 4h absence trigger detects for welcome-back check-ins.

### Goals & memory

- **Goals are extracted from every conversation**, not just goal-setting ones. Max 5, priority-ordered, aggressively cleaned.
- **Remove completed/abandoned goals aggressively.** If the user says "I'm switching to taxes," remove the old focus.
- **Memories persist across sessions.** Extracted after every conversation. Compacted when they exceed 30 entries (down to 15).
- **Profile evolves.** Profile fields are updated based on conversation content — new projects, new distractions, changed circumstances.

### "Review My Day" (daily journal)

- **Narrative, not dashboard.** Claude generates a personal journal entry from the day's screenshots + memories. Flowing prose, not bullet points or time charts.
- **The journal is Zerei's voice.** Presented in the reflection conversation as "here's what I saw today." Then the user can react and discuss.
- **Only deduplicates identical consecutive snapshots.** If you were on 5 different Reddit threads, each gets its own line. Only truly identical screenshots (same app + same description) are collapsed.

### Data storage

- **All data in project-local `.antidoom/` directory.** Profile, memories, conversations, snapshots log, app log. Nothing in `~/.antidoom/`.
- **Append-only snapshots log.** Every classification is appended to `snapshots.log` for the daily journal.
- **Conversations saved as JSON** in `.antidoom/conversations/`.

### Personality

- **Warm but firm.** Cares about you AND will call you out.
- **Affirms agency.** Reminds you of your own goals/aspirations — doesn't set the direction.
- **Names rationalizations.** "That sounds like a rationalization" is fair game. Guilt-tripping is not.
- **Carries the conversation.** If user gives one-word answers, Zerei doesn't match their energy.
- **Escalates on behavior, not request.** The personality shifts based on what Zerei observes, not what the user asks for.

## Architecture

```
Watcher (15s) → Screenshot → Resize (sips 1920px) → Claude Vision → Snapshot
     ↓
TriggerEngine.evaluate() → nudge / extended_nudge / we_need_to_talk / grind_break / ambiguous_checkin
     ↓
SignalBridge (Qt signal) → ChatWindow popup with typing indicator
     ↓
Zerei.start_conversation() → Claude Sonnet with context (profile + goals + memories + activity)
     ↓
Conversation loop (user ↔ Zerei) → signals control window lifecycle
     ↓
On dismiss: extract_memories() → update goals, profile, memories
```

For "Review My Day": `parse_today_snapshots()` → `generate_daily_journal()` (separate Claude call) → journal text fed into reflection conversation context.

## Tech Stack

- **Claude Sonnet 4.6** — vision (classification) + conversation (nudges, reflection, journal)
- **Python** + **PyQt6** — desktop app with system tray
- **macOS `screencapture` + `sips`** — screenshot capture and resize
- **Local JSON** — persistence (profile, memories, conversations)

## Known Limitations

- **"Productive on the wrong thing"** — classifier can't tell if coding is aligned with top goal. Need a separate trigger.
- **Classification inconsistency** — same screen content sometimes classified differently across consecutive snapshots. One "productive" blip breaks a doom streak.
- **Reply history bug** — every turn re-sends the "[START CONVERSATION]" instruction, confusing multi-turn conversations.
- **No retry on API failures** — network blips kill the conversation or classification cycle.
- **Window steals focus** — `activateWindow()` is aggressive. Nudges should be gentler.
