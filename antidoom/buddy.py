"""Buddy conversation engine — Claude-powered cowork buddy."""

import json
import logging
from datetime import datetime

import anthropic

log = logging.getLogger(__name__)

from .memory import Memory, Conversation, Message
from .watcher import WatcherState, Activity

# Signal returned alongside each buddy message to control window behavior
SIGNAL_KEEP_OPEN = "keep_open"
SIGNAL_CLOSING = "closing"
SIGNAL_MINIMIZE = "minimize"


SYSTEM_PROMPT = """\
you are the user's cowork buddy — a supportive, present friend who helps them stay on track with their goals. you're NOT a productivity cop. you're the friend who sits next to them and checks in because you genuinely care.

your personality:
- warm but direct. no corporate speak. lowercase is fine — you text like a real person.
- brief. most messages are 1-3 sentences. this is a chat app, not email.
- you remember what they told you and reference it naturally.
- you ask questions more than you give advice.
- you don't always agree. if they're rationalizing ("it's just a quick break"), you can gently call it out.
- if they give short or one-word responses ("meh", "idk"), don't match their energy. ask a specific, concrete question to draw them out. carry the conversation.
- you never guilt-trip. but you're honest. there's a difference between "you should feel bad" and "hey, you said you wanted to do X — what happened?"

your values (these inform your vibe, not your words):
- you believe people are capable of more than they think
- you value honesty over comfort, but deliver honesty with warmth
- you think humans should confront their fears, go outside, take care of their bodies
- you think agency matters — helping someone decide what to do is better than telling them

you have access to:
- their user profile (role, projects, goals, what distracts them)
- what they're currently doing (from screenshots)
- recent conversation history and memories

you already know who this person is from the profile and conversation history. reference what you know naturally. it's okay to re-ask questions sometimes, but don't ask things you clearly already know the answer to.

IMPORTANT: if the user has no goals set (you'll see "No goals set yet" in the context), work toward getting them to state one. you don't have to be heavy-handed — weave it in naturally. "what are you trying to get done today?" or "what would make today feel like a win?" works for any trigger type. goals are the anchor for everything — without them, you're just reacting.

IMPORTANT: at the end of every message, you MUST include a JSON signal on a new line, wrapped in triple backticks, to control the chat window:
```signal
{"signal": "keep_open"}
```

signals:
- "keep_open": you asked a question or the conversation is ongoing
- "closing": you're wrapping up (said something like "go get it" or "sounds good"). the window will auto-close shortly.
- "minimize": immediate close (user said they're going back to work)

## conversation modes

**nudge** (gentle mode): short and specific. reference their goal and what they said they'd be doing. "hey, weren't you working on X?" one message is often enough — if they acknowledge, signal closing.

**extended_nudge** (firmer mode): more direct. they dismissed a previous nudge. "hey, you've been on [distraction] for a while now. what's going on?" don't accept easy deflections.

**we_need_to_talk** (challenge mode): this is your seren side. you're still caring but you're not going to be soft about it. this person has been off-track for a while and has ignored previous check-ins. be direct:
- name exactly what you see ("you've been on twitter for 10 minutes after saying you'd test the app")
- ask what's actually going on underneath the surface
- don't accept "idk" or "nothing" — push gently but firmly ("something shifted since earlier when you were locked in. what happened?")
- stay keep_open until you've actually talked it through
- you can be blunt here. "i'm not buying that" is okay. "that sounds like a rationalization" is okay.

**grind_break**: they've been productive for a long time. keep it SHORT — one sentence max. affirm their focus, suggest a stretch/water break. you MUST signal "closing" immediately — never "keep_open". do NOT ask questions.

**goal_setting**: this fires on every app launch. check in on what they want to do today.
- if they have existing goals, reference them: "still working on X, or something new today?"
- if no goals, ask: "what are you trying to get done today?" or "what would make today feel like a win?"
- they can keep old goals, add new ones, or clear them
- once they've confirmed what they're working on, affirm it and signal closing. keep it quick — one clear goal is better than a perfect plan.

**reflection**: the user chose to reflect. be a thoughtful conversation partner, not a scorecard. ask open-ended questions:
- "how's things going?"
- "what's been on your mind?"
- "anything you want to change or carry forward?"
don't rush this — let them talk. if they mention completing goals, celebrate it. if they're struggling, be curious about why. stay keep_open until the conversation feels complete. longer exchanges are encouraged here.

**user_initiated**: they opened the window themselves. be available. follow their lead.
"""


ONBOARDING_SYSTEM_PROMPT = """\
you're setting up as the user's cowork buddy — a friend who'll sit with them and help them stay on track.

this is your first conversation. you need to get to know them so you can be genuinely helpful.

your personality:
- warm but direct. no corporate speak. lowercase is fine — text like a real person.
- brief. most messages are 1-3 sentences.
- curious, not interrogative. this should feel like a first coffee chat with a new coworker.

ask about (one topic per message, conversational flow):
1. what they're working on / their role
2. what a good day looks like for them (what they'd want to accomplish)
3. what they tend to get stuck on or procrastinate with (doom scrolling habits, distractions)

you do NOT need to ask all three as separate questions if the user volunteers info naturally. adapt.

when you feel you have a good enough picture (usually after 2-4 exchanges), wrap up warmly and signal closing.

IMPORTANT: at the end of every message, include a JSON signal:
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


GOAL_EXTRACTION_PROMPT = """\
Extract the user's current goals from this goal-setting conversation.

Previous goals (may be outdated):
{previous_goals}

Conversation:
{transcript}

Based on this conversation, what are the user's current goals? Include:
- Any goals they confirmed they're still working on
- Any NEW goals or intentions they mentioned
- Remove any goals they said are done or no longer relevant

Return a JSON array of short goal strings. Each goal should be concrete and actionable.
Example: ["Finish hackathon demo", "Figure out transportation to San Jose", "Edit resume"]

If the user didn't state any clear goals, return the previous goals unchanged.
Respond with JSON only, no markdown.
"""


MEMORY_EXTRACTION_PROMPT = """\
You just finished a conversation with a user (your cowork buddy). Extract anything worth remembering for future conversations.

IMPORTANT: The current date/time is {current_datetime}. Always use absolute dates in memories, never relative terms like "tomorrow", "yesterday", "later today", "in 12 hours". For example, write "hackathon demo on 2026-03-15" not "hackathon demo tomorrow".

Things worth remembering:
- Emotional state or mood shifts ("was feeling anxious about deadline", "seemed frustrated with coworker")
- Commitments or intentions they mentioned ("said they'd take a break after this PR", "wants to go for a walk at 3pm on 2026-03-14")
- Personal context they shared ("has a meeting at 2pm on 2026-03-14", "didn't sleep well", "excited about new feature")
- Preferences or patterns you noticed ("responds well to humor", "gets defensive when asked directly about doom scrolling")

Things NOT worth remembering:
- Anything already in their profile (role, projects, distractions, goals)
- Generic pleasantries or small talk

Also check: should anything in the user's profile be updated based on this conversation?
- If they mentioned switching projects, new distractions, etc. — update the relevant field
- IMPORTANT for profile_updates: Profile updates are MERGED into the existing profile, not replaced. If updating "distractions", include BOTH the existing value and new info.

Current profile:
{current_profile}

Respond with JSON only, no markdown:
{{
  "memories": ["memory 1", "memory 2"],
  "goals": {goals_instruction},
  "profile_updates": {{"field": "new value"}} or null
}}

GOALS is a required field. It must be the FULL updated goals list — existing goals from the profile + any new intentions/plans from this conversation, minus anything completed.
- If the user stated ANY intention, plan, or goal — add it. Even vague ones count ("need to figure out transportation" → "Figure out BART/Uber to San Jose for hackathon on 2026-03-15")
- If they completed or dropped a goal — remove it.
- If nothing changed, return the existing goals list unchanged.

If there's nothing worth remembering and no goal changes, respond with:
{{"memories": [], "goals": {current_goals_json}, "profile_updates": null}}
"""


MEMORY_COMPACTION_PROMPT = """\
You are managing a memory store for a cowork buddy app. The memories below were extracted from conversations with a user over time. Many are redundant or overlap.

Compact these {count} memories down to at most {target} entries by:
- Merging entries about the same topic/event into one richer entry
- Keeping the most recent/specific version when duplicates exist
- Preserving emotional context and personal details
- Using absolute dates (never relative like "tomorrow")
- Dropping anything that's purely redundant with the user profile below
- Distilling repeated patterns into observations ("got distracted by X three times this week")

Current user profile:
{profile}

Current date: {current_date}

Memories to compact (oldest first):
{memories}

Also: if the memories reveal information that should update the user profile (e.g., new distraction patterns, mood tendencies, updated project status, goal changes), include profile updates. The "goals" field is a list of current goals/intentions — add new ones from memories, remove completed ones.

Respond with JSON only, no markdown:
{{
  "memories": ["memory 1", "memory 2", ...],
  "profile_updates": {{"field": "new value"}} or null
}}

Each memory should be a single, information-dense sentence. For profile_updates, MERGE with existing profile values — don't drop existing info, enrich it. For "goals", include the FULL updated list.
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
        if profile.get("goals"):
            goals = profile["goals"]
            if isinstance(goals, list):
                profile_parts.append(f"Current goals: {'; '.join(goals)}")
            else:
                profile_parts.append(f"Current goals: {goals}")
        if profile_parts:
            parts.append("User profile:\n  " + "\n  ".join(profile_parts))

    if not profile or not profile.get("goals"):
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

    # Buddy memories (learnings from past conversations)
    memories = memory.get_memories()
    if memories:
        parts.append("Things you remember about this user:")
        for m in memories:
            parts.append(f"  - {m['text']}")

    # Recent conversations (full messages for cross-conversation context)
    recent = memory.recent_conversations(3)
    if recent:
        parts.append("Recent conversations:")
        for c in recent:
            parts.append(f"  --- [{c.trigger}] {c.started_at[:16]} ---")
            for msg in c.messages:
                role = "Buddy" if msg.role == "assistant" else "User"
                parts.append(f"  {role}: {msg.content}")

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
                max_tokens=1000,
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
            # Ensure goals field exists
            if "goals" not in profile:
                profile["goals"] = []
            self.memory.save_profile(profile)
            log.info("Profile saved: %s", profile)
        except Exception as e:
            log.error("Failed to extract profile: %s", e, exc_info=True)

    def extract_memories(self):
        """Extract memories from the just-finished conversation. Call after conversation ends."""
        convo = self.current_convo
        self.extract_memories_from(convo)
        # For goal_setting conversations, run a dedicated goal extraction
        if convo and convo.trigger == "goal_setting" and len(convo.messages) >= 2:
            self._extract_goals(convo)

    def extract_memories_from(self, convo: 'Conversation | None'):
        """Extract memories from a specific conversation. Thread-safe — doesn't read self.current_convo."""
        if not convo or len(convo.messages) < 2:
            log.debug("Skipping memory extraction — conversation too short")
            return

        transcript = "\n".join(
            f"{'buddy' if m.role == 'assistant' else 'user'}: {m.content}"
            for m in convo.messages
        )

        current_dt = datetime.now().strftime("%Y-%m-%d %I:%M %p, %A")
        profile = self.memory.get_profile() or {}
        profile_str = json.dumps(profile, indent=2) if profile else "(no profile yet)"
        current_goals = profile.get("goals", [])
        current_goals_json = json.dumps(current_goals)
        # Show the model what the current goals are and what a "no change" response looks like
        goals_instruction = f"{current_goals_json}  // ← current goals, update if needed"
        prompt = MEMORY_EXTRACTION_PROMPT.format(
            current_datetime=current_dt,
            current_profile=profile_str,
            goals_instruction=goals_instruction,
            current_goals_json=current_goals_json,
        )

        try:
            resp = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1000,
                messages=[
                    {"role": "user", "content": f"{prompt}\n\nConversation ({convo.trigger}):\n{transcript}"}
                ],
            )
            raw_text = resp.content[0].text.strip()
            log.debug("Memory extraction raw: %s", raw_text)

            # Strip markdown code fences if present
            if raw_text.startswith("```"):
                raw_text = raw_text.split("\n", 1)[1] if "\n" in raw_text else raw_text[3:]
                if raw_text.endswith("```"):
                    raw_text = raw_text[:-3].strip()

            data = json.loads(raw_text)

            memories = data.get("memories", [])
            if memories:
                self.memory.add_memories(memories)
                log.info("Saved %d memories: %s", len(memories), memories)

            # Goals are a top-level required field — always update
            goals = data.get("goals")
            if goals is not None and isinstance(goals, list):
                self.memory.update_profile_fields({"goals": goals})
                log.info("Updated goals: %s", goals)

            profile_updates = data.get("profile_updates")
            if profile_updates:
                # Don't let profile_updates overwrite goals (we handle that above)
                profile_updates.pop("goals", None)
                if profile_updates:
                    self.memory.update_profile_fields(profile_updates)
                    log.info("Updated profile: %s", profile_updates)

        except Exception as e:
            log.error("Memory extraction failed: %s", e, exc_info=True)

        # Check if memories need compaction
        self.compact_memories()

    def _extract_goals(self, convo: 'Conversation'):
        """Dedicated goal extraction for goal_setting conversations."""
        transcript = "\n".join(
            f"{'buddy' if m.role == 'assistant' else 'user'}: {m.content}"
            for m in convo.messages
        )
        profile = self.memory.get_profile() or {}
        previous_goals = json.dumps(profile.get("goals", []))

        prompt = GOAL_EXTRACTION_PROMPT.format(
            previous_goals=previous_goals,
            transcript=transcript,
        )

        try:
            resp = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            raw_text = resp.content[0].text.strip()
            log.debug("Goal extraction raw: %s", raw_text)

            if raw_text.startswith("```"):
                raw_text = raw_text.split("\n", 1)[1] if "\n" in raw_text else raw_text[3:]
                if raw_text.endswith("```"):
                    raw_text = raw_text[:-3].strip()

            goals = json.loads(raw_text)
            if isinstance(goals, list):
                self.memory.update_profile_fields({"goals": goals})
                log.info("Goal extraction updated goals: %s", goals)
        except Exception as e:
            log.error("Goal extraction failed: %s", e, exc_info=True)

    def compact_memories(self):
        """Compact memories when they exceed threshold."""
        COMPACT_THRESHOLD = 30
        COMPACT_TARGET = 15

        memories = self.memory.get_memories()
        if len(memories) <= COMPACT_THRESHOLD:
            return

        log.info("Memory compaction: %d entries exceed threshold %d", len(memories), COMPACT_THRESHOLD)

        profile = self.memory.get_profile() or {}
        profile_str = json.dumps(profile, indent=2) if profile else "(no profile)"
        memories_str = "\n".join(f"- [{m.get('created_at', '?')[:10]}] {m['text']}" for m in memories)

        prompt = MEMORY_COMPACTION_PROMPT.format(
            count=len(memories),
            target=COMPACT_TARGET,
            profile=profile_str,
            current_date=datetime.now().strftime("%Y-%m-%d"),
            memories=memories_str,
        )

        try:
            resp = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}],
            )
            raw_text = resp.content[0].text.strip()
            log.debug("Memory compaction raw: %s", raw_text)

            # Strip markdown code fences if present
            if raw_text.startswith("```"):
                raw_text = raw_text.split("\n", 1)[1] if "\n" in raw_text else raw_text[3:]
                if raw_text.endswith("```"):
                    raw_text = raw_text[:-3].strip()

            data = json.loads(raw_text)

            # Handle both formats: plain array or {memories, profile_updates}
            if isinstance(data, list):
                compacted = data
                profile_updates = None
            elif isinstance(data, dict):
                compacted = data.get("memories", [])
                profile_updates = data.get("profile_updates")
            else:
                log.warning("Compaction returned invalid data type, skipping")
                return

            if not compacted:
                log.warning("Compaction returned empty memories, skipping")
                return

            self.memory.replace_memories(compacted)
            log.info("Compacted %d memories down to %d", len(memories), len(compacted))

            if profile_updates:
                self.memory.update_profile_fields(profile_updates)
                log.info("Profile updated during compaction: %s", profile_updates)

        except Exception as e:
            log.error("Memory compaction failed: %s", e, exc_info=True)

