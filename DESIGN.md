# Antidoom — Cowork Buddy

## Vision

A coworking buddy that helps people with low conscientiousness stay on track. Not a productivity cop — a supportive colleague who checks in, listens, and helps you connect what you're doing to what you said you wanted to do.

The core insight: for people who struggle with executive function, the difference between "tool you open" and "buddy who checks in" is everything. The activation energy of self-reflection drops to near zero when a window pops up and asks you a simple question.

## Core Loop

**Screenshot → Classify → Decide → Intervene (conversationally)**

A background watcher takes periodic screenshots, classifies activity via vision LLM, and triggers a floating chat window when something is worth talking about.

## Key Design Decisions

- **Microsoft Recall / Claude Cowork approach** — periodic screenshots fed to vision endpoint. No privacy/security concerns for V1.
- **The chat window IS the product.** Everything flows through a single conversational interface. No dashboards, no settings pages.
- **The chat log IS the journal.** Every conversation is persisted. No separate journal feature needed.
- **Supportive tone, not punitive.** "You said shipping the PR was the priority — want to get back to it?" not "Stop scrolling Twitter."

## Features

### 1. Scheduled Check-ins

Regular interviews to capture and maintain user intentions:

| Interval | Purpose | Example |
|----------|---------|---------|
| Weekly | Big picture goals, priorities | "What are your top 3 goals this week?" |
| Daily (morning) | Today's plan | "What's the one thing that'd make today a win?" |
| Daily (midday) | Progress check | "How's it going? Still on track?" |
| Daily (evening) | Reflection + summary | "How'd today go? Here's what I saw..." |

Check-in times are configurable (buddy learns your schedule from conversation).

### 2. Unscheduled Interventions

Triggered by the watcher based on activity patterns:

| Trigger | Condition | Buddy opens with... |
|---------|-----------|---------------------|
| Doom scroll | 2+ min of classified scrolling | Gentle redirect referencing current goal |
| Extended doom scroll | 5+ min / dismissed first nudge | More direct check-in |
| Stuck | Same app, low activity 10+ min | "Want to talk through it?" |
| Long grind | 90+ min focused work | "Nice streak. Break time?" |
| Context switch storm | 5+ app switches in 2 min | "Feeling scattered?" |

After dismissal without engagement, back off for a cooldown (~10 min).

### 3. "We Need to Talk"

The nuclear option. Triggered by bad trends, not single moments:

- 3+ hours doom scrolling in a day
- Dismissed 3+ nudges without engaging
- Multiple days of missing stated goals
- Morning goal completely abandoned by afternoon

Tone shifts to a mini coaching session — open-ended questions, no guilt trips. Helps user figure out if the problem is the goal or the execution.

### 4. Activity Timeline / Daily Summary

The watcher already classifies every screenshot, so we get a timeline for free. Delivered conversationally during evening check-in:

> "Today: 5.5h productive, 1.5h scrolling, 1h breaks. Most productive time in VS Code. Scrolling was mostly Twitter between 2-3pm."

No charts — the buddy just tells you. For some people, just knowing how time was spent is enough motivation.

### 5. Rubber Duck Mode

User can open the window anytime (hotkey) and talk through what they're working on. The buddy doesn't do the work — it asks clarifying questions and helps you think out loud.

## Chat Window Lifecycle

The window should feel like a natural conversation, not a dialog box you have to dismiss.

**Window state signals:** The buddy engine returns a signal with each message:

- `keep_open` — conversation is ongoing, window stays
- `closing` — buddy is wrapping up (e.g. "Sounds good, go get it!"). Window shows the final message for ~2 seconds, then auto-minimizes.
- `minimize` — immediate minimize (e.g. user said "back to work")

**When the window auto-minimizes:**
- After a completed check-in ("LGTM, let's get started")
- After a successful nudge redirect ("Cool, going back to it")
- After the user explicitly dismisses ("I'm good")

**When the window stays open:**
- User is actively typing / mid-conversation
- Rubber duck mode (user opened it themselves)
- "We need to talk" sessions (buddy keeps it open until resolution)

The user can always reopen via hotkey or menubar. Closing the window mid-conversation is fine — buddy won't be offended, and will remember where you left off.

## Activity Classification

Vision model classifies screenshots into:

- **Productive work** — IDE, docs, spreadsheets, focused reading
- **Intentional leisure** — specific video, game, planned break
- **Doom scrolling** — Twitter/X, Reddit, TikTok, YouTube shorts, news feeds, Instagram
- **Ambiguous** — Slack, email (could be work or procrastination)

Tracked as a sliding window to detect trends, not just point-in-time.

## Architecture

```
┌─────────────────────────────────────┐
│           Cowork Buddy              │
├─────────────────────────────────────┤
│  [Watcher]                          │
│    Screenshots every 30-60s         │
│    Vision API classification        │
│    Activity timeline                │
│                                     │
│  [Buddy Engine]                     │
│    Claude-powered conversation      │
│    Context: activity + goals +      │
│            conversation history     │
│    Modes: check-in, nudge, stuck,   │
│           "we need to talk"         │
│                                     │
│  [Trigger Logic]                    │
│    Scheduled check-ins (cron-like)  │
│    Activity-based triggers          │
│    Escalation ladder                │
│                                     │
│  [Memory / State]                   │
│    Goal hierarchy (weekly → daily)  │
│    Conversation history (journal)   │
│    Activity timeline log            │
│                                     │
├─────────────────────────────────────┤
│  Floating PyQt6 Chat Window         │
│  + macOS Menubar Icon               │
│  + Global Hotkey                    │
└─────────────────────────────────────┘
```

## Tech Stack

- **Python** (backend dev background, fast iteration)
- **PyQt6** — floating chat window + menubar
- **macOS `screencapture`** — screenshot capture
- **Claude API (Sonnet)** — vision classification + conversation
- **Local JSON/SQLite** — persistence

## Hackathon Scope (1.5 days)

### Must have
- Screenshot watcher + vision classification
- Floating chat window (pop-up + hotkey)
- Scheduled check-ins (morning/midday/evening)
- Doom scroll detection → nudge
- Goal setting through conversation
- Conversation history persisted to disk

### Nice to have
- "We need to talk" (trend-based escalation)
- Stuck/grind/scattered detection
- Weekly interview

### Cut for V1
- Activity timeline / daily summary (commoditized, hard to get right)
- Missed check-in handling (user not at computer)
- Analytics dashboard
- Mobile
- Accounts/auth
- Privacy controls
