# Antidoom Buddy — How It Works

## The Big Picture

Antidoom Buddy is a macOS menu bar app that watches what you're doing on your computer and intervenes when you're doom scrolling. It's a cowork buddy, not a productivity cop — it knows your goals, notices when you've fallen into a scroll hole, and pops up a chat window to check in.

```
┌─────────────┐     ┌────────────────┐     ┌────────────────┐     ┌─────────────┐
│  Screenshot  │────▶│  Claude Vision  │────▶│  Trigger       │────▶│  Chat Window │
│  (every 30s) │     │  Classification │     │  Engine        │     │  (Buddy)     │
└─────────────┘     └────────────────┘     └────────────────┘     └─────────────┘
```

## Step-by-Step Flow

### 1. Screenshot Capture (watcher.py)

A background thread takes a screenshot every **30 seconds** using macOS `screencapture`. The screenshot is captured as a temporary PNG, read into memory, then deleted.

### 2. Classification (watcher.py)

Each screenshot is sent to **Claude Sonnet** as a vision request. Claude classifies it into one of four categories:

| Category | Examples |
|---|---|
| `productive` | IDE, writing docs, spreadsheets, focused reading |
| `doom_scrolling` | Twitter/X feed, Reddit, TikTok, Instagram, YouTube shorts, news feeds |
| `intentional_leisure` | Watching a specific video, playing a game |
| `ambiguous` | Slack, email, unclear context |

The result is a **Snapshot**: timestamp + activity + app name + short description.

Each snapshot is also appended to `~/.antidoom/snapshots.log` so you can review what was classified and how.

### 3. State Tracking (watcher.py — WatcherState)

Snapshots are kept in a sliding window of the last **120 entries** (~1 hour). The state tracks:

- **Consecutive doom count**: How many of the most recent snapshots in a row are `doom_scrolling`. Breaks the streak the moment one screenshot is something else.
- **Consecutive productive count**: Same idea, but for `productive`.
- **Doom scroll minutes**: Total minutes of doom scrolling in a time window (e.g. last 30 min, last 8 hours). Each snapshot = 30 seconds.

### 4. Trigger Evaluation (triggers.py)

After every snapshot, the **Trigger Engine** checks whether the buddy should pop up. It runs through these rules in priority order:

#### Doom Scrolling Triggers (escalating)

**Nudge** — You've been doom scrolling for ~2 minutes straight.
- Fires when: **4 consecutive** doom_scrolling snapshots
- Buddy tone: Short and specific. References your goal. "Hey, weren't you working on X?"

**Extended Nudge** — You've been doom scrolling for ~5 minutes AND you already dismissed a previous nudge.
- Fires when: **10 consecutive** doom_scrolling snapshots + at least 1 dismissed nudge
- Buddy tone: More direct. Acknowledges you've been at it a while.

**"We Need to Talk"** — You've spent 3 hours doom scrolling today.
- Fires when: **180 minutes** of total doom scrolling in the last 8 hours
- Buddy tone: Longer conversation. Open-ended questions. Won't auto-close — actually talks it through with you.

#### Grind Break

**What it is**: If you've been grinding productively for a very long time without a break, the buddy suggests you take one.

- Fires when: **180 consecutive** productive snapshots (~90 minutes straight)
- Buddy tone: "Hey, you've been locked in for a while. Stretch break?"

This exists because the buddy isn't just anti-doom — it's pro-sustainability. Working 3 hours without standing up isn't healthy either.

#### Cooldown Between Triggers

**What it is**: A minimum gap between any two trigger-fired popups, to prevent the buddy from nagging you repeatedly.

- Default: **600 seconds (10 minutes)**
- After any trigger fires, the engine won't fire another one for this duration, even if thresholds are met.

**Why it exists**: Without it, if you dismiss a nudge and keep scrolling, the buddy would fire again 30 seconds later on the next snapshot. The cooldown gives you space — if you acknowledged the nudge and chose to keep scrolling, that's your call for the next 10 minutes.

The cooldown resets every time a trigger fires (not when it's dismissed).

### 5. Scheduled Check-ins (triggers.py)

Independent of doom scrolling, the buddy has three daily check-ins:

| Check-in | Time | Purpose |
|---|---|---|
| Morning | 9:00 AM | "What do you want to accomplish today?" — sets daily goal |
| Midday | 1:00 PM | Quick temperature check — "How's it going?" |
| Evening | 5:30 PM | Reflect — "What went well today?" |

These fire within a 5-minute window after the scheduled time. Each fires at most once per day.

### 6. Chat Window (chat_window.py, buddy.py)

When a trigger fires, the buddy:
1. Builds **context** for Claude: your profile, goals, current activity, doom scroll stats, recent conversations
2. Sends it to Claude with a system prompt tailored to the trigger type
3. Pops up a floating chat window (top-right corner, dark theme, always-on-top)

You can chat back. The buddy uses **signals** to control the window:
- `keep_open` — conversation ongoing (buddy asked a question)
- `closing` — wrapping up (auto-minimizes after 2.5s)
- `minimize` — immediate minimize (you said you're going back to work)

### 7. Escalation Logic

The trigger engine tracks whether you **engaged** (typed a reply) or **dismissed** (closed the window without replying):

- **Engaged**: Resets the dismissed counter. The buddy assumes the nudge worked.
- **Dismissed**: Increments a counter. If you dismiss and keep doom scrolling, the next trigger escalates to `extended_nudge` instead of a regular `nudge`.

This creates a natural escalation: gentle nudge → firmer nudge → "we need to talk", with the user's own behavior driving the progression.

## Onboarding

On first launch (no `~/.antidoom/profile.json`), the buddy opens immediately and introduces itself. It asks about:
- What you do / your role
- What you're working on
- What distractions look like for you

The answers are extracted into a profile that's included in all future conversations, so the buddy actually knows who it's talking to.

You can redo this anytime via the **"Update Profile"** tray menu item.

## Data Storage

Everything lives in `~/.antidoom/`:

| File | Contents |
|---|---|
| `profile.json` | User profile (role, projects, distractions) |
| `goals.json` | Weekly and daily goals |
| `conversations/*.json` | Every conversation (messages + metadata) |
| `snapshots.log` | Every screenshot classification result |
| `antidoom.log` | Full debug log |

## Debug Mode

Run with `python run.py --debug` to compress all timings:

| Setting | Normal | Debug |
|---|---|---|
| Screenshot interval | 30s | 10s |
| Nudge threshold | 4 snapshots (~2 min) | 2 snapshots (~20s) |
| Extended nudge | 10 snapshots (~5 min) | 4 snapshots (~40s) |
| "We need to talk" | 3 hours | 5 min |
| Grind break | 90 min | ~1 min |
| Cooldown | 10 min | 30s |
