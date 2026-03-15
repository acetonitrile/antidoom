"""Buddy conversation engine — Claude-powered cowork buddy."""

import json
import logging
from datetime import datetime

import anthropic

log = logging.getLogger(__name__)

from .memory import Memory, Conversation, Message, Goal
from .watcher import WatcherState, Activity

# Signal returned alongside each buddy message to control window behavior
SIGNAL_KEEP_OPEN = "keep_open"
SIGNAL_CLOSING = "closing"
SIGNAL_MINIMIZE = "minimize"


SYSTEM_PROMPT = """\
You are the user's cowork buddy — a supportive, present colleague who helps them stay on track with their goals. You are NOT a productivity cop. You're a friend who sits next to them and occasionally checks in.

Your personality:
- Warm but direct. No corporate speak.
- Brief. Most messages are 1-3 sentences.
- You remember what they told you and reference it naturally.
- You ask questions more than you give advice.
- You never guilt-trip. If they're struggling, you're curious about why, not judgmental.

You have access to:
- Their user profile (role, projects, what distracts them)
- Their stated goals (weekly and daily)
- What they're currently doing (from screenshots)
- Recent conversation history

CRITICAL: You already know who this person is and what they're working on from the profile and conversation history. NEVER ask questions you already have the answer to. Reference what you know naturally — "how's the antidoom app coming?" not "what are you building?"

IMPORTANT: At the end of every message, you MUST include a JSON signal on a new line, wrapped in triple backticks, to control the chat window:
```signal
{"signal": "keep_open"}
```

Use these signals:
- "keep_open": You asked a question or the conversation is ongoing
- "closing": You're wrapping up (said something like "go get it" or "sounds good"). The window will close when user presses Enter.
- "minimize": Immediate close (user said they're going back to work)

Guidelines per conversation type:
- **morning_checkin**: Ask what they want to accomplish today. Reference their profile/projects. Once they set a goal, affirm it and signal closing.
- **midday_checkin**: Ask how it's going with their stated goal. Quick temperature check. Signal closing once they respond.
- **evening_checkin**: Reflect on the day. What went well? Signal closing once done.
- **nudge**: Short and specific. Reference their goal and what they said they'd be doing. "Hey, weren't you working on X?" One message is often enough — if they acknowledge, signal closing.
- **extended_nudge**: More direct. They dismissed a previous nudge. "Hey, you've been on [distraction] for a while now. What's going on?"
- **we_need_to_talk**: Longer conversation. This is a bad day. Ask open-ended questions about what's going on. Stay keep_open until you've actually talked it through.
- **grind_break**: They've been productive for a long time. Keep it SHORT — one sentence. Affirm their focus, suggest a stretch/water break. Signal closing immediately. Do NOT ask what they're working on — you already know.
- **user_initiated**: They opened the window themselves. Be available. Follow their lead.
"""


ONBOARDING_SYSTEM_PROMPT = """\
You are setting up as the user's cowork buddy — a supportive colleague who'll sit with them and help them stay on track.

This is your first conversation. You need to get to know them so you can be genuinely helpful.

Your personality:
- Warm but direct. No corporate speak.
- Brief. Most messages are 1-3 sentences.
- Curious, not interrogative. This should feel like a first coffee chat with a new coworker.

Ask about (one topic per message, conversational flow):
1. What they're working on / their role
2. What a good day looks like for them (what they'd want to accomplish)
3. What they tend to get stuck on or procrastinate with (doom scrolling habits, distractions)

You do NOT need to ask all three as separate questions if the user volunteers info naturally. Adapt.

When you feel you have a good enough picture (usually after 2-4 exchanges), wrap up warmly and signal closing.

IMPORTANT: At the end of every message, include a JSON signal:
```signal
{"signal": "keep_open"}
```

Use "keep_open" while still getting to know them. Use "closing" when you've got enough to work with.
"""


PROFILE_EXTRACTION_PROMPT = """\
Extract a user profile from this onboarding conversation. Return JSON only, no markdown:
{
  "role": "what they do (brief)",
  "projects": "what they're currently working on",
  "good_day": "what a productive day looks like for them",
  "distractions": "what they tend to doom scroll or procrastinate with",
  "notes": "any other relevant context they shared"
}

Omit any field where the user didn't provide info (use null).
"""


def _build_context(
    memory: Memory,
    watcher_state: WatcherState | None,
    trigger: str,
) -> str:
    """Build context string for the buddy from current state."""
    parts = []

    # User profile
    profile = memory.get_profile()
    if profile:
        profile_parts = []
        if profile.get("role"):
            profile_parts.append(f"Role: {profile['role']}")
        if profile.get("projects"):
            profile_parts.append(f"Working on: {profile['projects']}")
        if profile.get("good_day"):
            profile_parts.append(f"Good day: {profile['good_day']}")
        if profile.get("distractions"):
            profile_parts.append(f"Distractions: {profile['distractions']}")
        if profile.get("notes"):
            profile_parts.append(f"Notes: {profile['notes']}")
        if profile_parts:
            parts.append("User profile:\n  " + "\n  ".join(profile_parts))

    # Goals
    goals = memory.get_active_goals()
    if goals["weekly"]:
        parts.append(f"Weekly goals: {', '.join(goals['weekly'])}")
    if goals["daily"]:
        parts.append(f"Today's goal: {', '.join(goals['daily'])}")
    if not goals["weekly"] and not goals["daily"]:
        parts.append("No goals set yet.")

    # Current activity
    if watcher_state and watcher_state.history:
        latest = watcher_state.history[-1]
        parts.append(f"Current activity: {latest.description} ({latest.app_name}) — classified as {latest.activity.value}")

        doom_mins = watcher_state.doom_scroll_minutes(window_minutes=30)
        if doom_mins > 0:
            parts.append(f"Doom scrolling in last 30 min: {doom_mins:.0f} minutes")

        consec_doom = watcher_state.consecutive_doom_count()
        if consec_doom > 0:
            parts.append(f"Consecutive doom scroll snapshots: {consec_doom} (~{consec_doom * 0.5:.0f} min)")

        consec_prod = watcher_state.consecutive_productive_count()
        if consec_prod > 4:  # >2 min
            parts.append(f"Been productive for ~{consec_prod * 0.5:.0f} min straight")

    # Recent conversations (summaries)
    recent = memory.recent_conversations(3)
    if recent:
        parts.append("Recent conversations:")
        for c in recent:
            first_msg = c.messages[0].content[:100] if c.messages else "(empty)"
            parts.append(f"  [{c.trigger}] {c.started_at[:16]}: {first_msg}...")

    parts.append(f"Conversation trigger: {trigger}")
    parts.append(f"Current time: {datetime.now().strftime('%I:%M %p, %A')}")

    return "\n".join(parts)


def parse_signal(text: str) -> tuple[str, str]:
    """Extract signal from buddy message. Returns (clean_message, signal)."""
    signal = SIGNAL_KEEP_OPEN
    clean = text

    if "```signal" in text:
        before, _, after = text.partition("```signal")
        signal_block, _, remaining = after.partition("```")
        try:
            data = json.loads(signal_block.strip())
            signal = data.get("signal", SIGNAL_KEEP_OPEN)
        except json.JSONDecodeError:
            pass
        clean = before.strip()
        if remaining.strip():
            clean += "\n" + remaining.strip()

    return clean, signal


class Buddy:
    """Manages conversations with the cowork buddy."""

    def __init__(self, memory: Memory, watcher_state: WatcherState | None = None):
        self.memory = memory
        self.watcher_state = watcher_state
        self.client = anthropic.Anthropic()
        self.current_convo: Conversation | None = None

    def start_conversation(self, trigger: str) -> tuple[str, str]:
        """Start a new conversation. Returns (buddy_message, signal)."""
        convo_id = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{trigger}"
        self.current_convo = Conversation(id=convo_id, trigger=trigger)
        log.info("Starting conversation: %s (trigger=%s)", convo_id, trigger)

        context = _build_context(self.memory, self.watcher_state, trigger)
        log.debug("Buddy context:\n%s", context)

        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": f"[CONTEXT]\n{context}\n\n[START CONVERSATION]\nOpen the conversation based on the trigger type."}
            ],
        )

        raw = response.content[0].text
        log.debug("Buddy raw response: %s", raw)
        message, signal = parse_signal(raw)
        log.info("Buddy message (signal=%s): %s", signal, message[:80])

        self.current_convo.messages.append(Message(role="assistant", content=message))
        self.memory.save_conversation(self.current_convo)

        return message, signal

    def reply(self, user_message: str) -> tuple[str, str]:
        """User replies to the buddy. Returns (buddy_message, signal)."""
        log.info("User reply: %s", user_message[:80])
        if not self.current_convo:
            return self.start_conversation("user_initiated")

        self.current_convo.messages.append(Message(role="user", content=user_message))

        # Build message history for Claude
        context = _build_context(self.memory, self.watcher_state, self.current_convo.trigger)
        messages = [{"role": "user", "content": f"[CONTEXT]\n{context}\n\n[START CONVERSATION]\nOpen the conversation based on the trigger type."}]

        for msg in self.current_convo.messages:
            messages.append({"role": msg.role, "content": msg.content})

        # Ensure alternating roles — Claude API requires it
        # The first assistant message is already in history, user just replied
        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=messages,
        )

        raw = response.content[0].text
        message, signal = parse_signal(raw)

        self.current_convo.messages.append(Message(role="assistant", content=message))
        self.memory.save_conversation(self.current_convo)

        # Try to extract goals from conversation
        self._maybe_extract_goals(user_message)

        return message, signal

    def needs_onboarding(self) -> bool:
        return not self.memory.has_profile()

    def start_onboarding(self) -> tuple[str, str]:
        """Start the onboarding conversation. Returns (buddy_message, signal)."""
        convo_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_onboarding"
        self.current_convo = Conversation(id=convo_id, trigger="onboarding")
        log.info("Starting onboarding conversation: %s", convo_id)

        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=ONBOARDING_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": "[START] This is the first time the app has launched. Introduce yourself and start getting to know the user."}
            ],
        )

        raw = response.content[0].text
        message, signal = parse_signal(raw)
        log.info("Onboarding opening (signal=%s): %s", signal, message[:80])

        self.current_convo.messages.append(Message(role="assistant", content=message))
        self.memory.save_conversation(self.current_convo)

        return message, signal

    def reply_onboarding(self, user_message: str) -> tuple[str, str]:
        """Handle a reply during onboarding. Returns (buddy_message, signal)."""
        log.info("Onboarding reply: %s", user_message[:80])
        if not self.current_convo:
            return self.start_onboarding()

        self.current_convo.messages.append(Message(role="user", content=user_message))

        messages = [
            {"role": "user", "content": "[START] This is the first time the app has launched. Introduce yourself and start getting to know the user."}
        ]
        for msg in self.current_convo.messages:
            messages.append({"role": msg.role, "content": msg.content})

        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=ONBOARDING_SYSTEM_PROMPT,
            messages=messages,
        )

        raw = response.content[0].text
        message, signal = parse_signal(raw)
        log.info("Onboarding response (signal=%s): %s", signal, message[:80])

        self.current_convo.messages.append(Message(role="assistant", content=message))
        self.memory.save_conversation(self.current_convo)

        # If closing, extract profile from the conversation
        if signal == SIGNAL_CLOSING:
            self._extract_profile()

        return message, signal

    def _extract_profile(self):
        """Extract user profile from the onboarding conversation and save it."""
        if not self.current_convo:
            return
        log.info("Extracting profile from onboarding conversation")
        transcript = "\n".join(
            f"{'buddy' if m.role == 'assistant' else 'user'}: {m.content}"
            for m in self.current_convo.messages
        )
        try:
            resp = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=300,
                messages=[
                    {"role": "user", "content": f"{PROFILE_EXTRACTION_PROMPT}\n\nConversation:\n{transcript}"}
                ],
            )
            raw_text = resp.content[0].text.strip()
            log.debug("Profile extraction raw response: %s", raw_text)

            # Strip markdown code fences if present (```json ... ```)
            if raw_text.startswith("```"):
                # Remove opening fence (with optional language tag)
                raw_text = raw_text.split("\n", 1)[1] if "\n" in raw_text else raw_text[3:]
                # Remove closing fence
                if raw_text.endswith("```"):
                    raw_text = raw_text[:-3].strip()

            profile = json.loads(raw_text)
            # Remove null values
            profile = {k: v for k, v in profile.items() if v is not None}
            self.memory.save_profile(profile)
            log.info("Profile saved: %s", profile)
        except Exception as e:
            log.error("Failed to extract profile: %s", e, exc_info=True)

    def _maybe_extract_goals(self, user_message: str):
        """Simple heuristic: if this is a morning check-in and user stated a goal, save it."""
        if self.current_convo and self.current_convo.trigger == "morning_checkin":
            # Let Claude extract the goal
            try:
                resp = self.client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=100,
                    messages=[
                        {
                            "role": "user",
                            "content": f'The user said: "{user_message}"\n\nIf this contains a goal or plan for the day, extract it as a short phrase. If not, respond with just "none".\nRespond with just the goal text or "none".',
                        }
                    ],
                )
                goal_text = resp.content[0].text.strip().strip('"')
                if goal_text.lower() != "none" and len(goal_text) < 200:
                    self.memory.set_daily_goal(goal_text)
            except Exception:
                pass
