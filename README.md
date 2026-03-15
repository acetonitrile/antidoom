# Zerei

**An AI companion that lives on your desktop, watches how you spend your time, and actually remembers.**

It uses Claude's vision to read your screen every 15 seconds — not just which app is open, but what you're actually looking at. It knows your goals because you told it. When you drift, it nudges you. When you rationalize, it calls you out (gently). When you ask it to review your day, it gives you an overview of how you spent your time and notable themes and patterns it noticed.

Think of it as a coworker who's always there — not to judge, but to hold context. It remembers what you said you wanted to do last Tuesday, notices you keep ending up on Twitter at 2pm, and asks why. It's a rubber duck that talks back, holds your goals across sessions, and won't let you forget what you said mattered.

## How it works

1. **Screenshots every 15s** — Claude vision reads what's actually on screen (tweet topics, Reddit threads, code being written, article headlines) and classifies it against your stated goals
2. **Escalating nudges** — gentle check-in → firmer nudge → real talk, with exponential backoff if you keep dismissing
3. **Goal tracking** — goals are extracted from every conversation, priority-ordered, and referenced in future nudges
4. **Memory** — learns your patterns, distractions, and rationalizations across sessions
5. **"Review My Day"** — generates a narrative journal entry from your full day's screenshots and memories, then discusses it with you

## Screenshots

### Goal setting on startup
Zerei checks in every time you open the app to ask what you're working on today.

### Doom scrolling nudge
When you've been on Twitter for 2 minutes, Zerei pops up with a gentle nudge referencing your actual goals.

### Review My Day
A personal journal entry written by Zerei based on everything it observed — the deep work sessions, the Twitter rabbit holes, the moments you switched tasks.

## Setup

```bash
# Requires macOS (uses screencapture + sips), Python 3.11+
pip install -r requirements.txt

# Set your Anthropic API key
export ANTHROPIC_API_KEY=sk-...

# Run
python run.py

# Debug mode (faster intervals for testing)
python run.py --debug
```

On first launch, Zerei will ask you a few questions to get to know you (role, projects, what distracts you). After that, it runs in the menu bar and checks in when it notices you drifting.

## Architecture

- **watcher.py** — Screenshot capture → resize to 1920px → Claude Sonnet vision classification
- **zerei.py** — Conversation engine, system prompt, memory/goal extraction, daily journal generation
- **triggers.py** — Activity-based trigger logic (doom escalation, grind breaks, ambiguous check-ins)
- **chat_window.py** — PyQt6 floating chat window with dark translucent UI
- **memory.py** — JSON persistence for profile, goals, memories, conversations
- **app.py** — Wires everything together, manages tray icon and conversation lifecycle

## Built with

- **Claude Sonnet 4.6** — vision (screenshot classification) + conversation (nudges, reflection, journal)
- **Python** + **PyQt6** — desktop app with system tray
- **Claude Code** — the entire app was built with Claude Code over a weekend
