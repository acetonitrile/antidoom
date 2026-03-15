"""Local persistence for goals, conversation history, and state."""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parent.parent / ".antidoom"


@dataclass
class Goal:
    text: str
    scope: str  # "weekly" or "daily"
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


@dataclass
class Message:
    role: str  # "user" or "assistant"
    content: str
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


@dataclass
class Conversation:
    id: str
    trigger: str  # "morning_checkin", "nudge", "user_initiated", "we_need_to_talk", etc.
    messages: list[Message] = field(default_factory=list)
    started_at: str = ""

    def __post_init__(self):
        if not self.started_at:
            self.started_at = datetime.now().isoformat()


class Memory:
    """Simple JSON-file backed persistence."""

    def __init__(self, data_dir: Path = DATA_DIR):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._goals_file = data_dir / "goals.json"
        self._conversations_dir = data_dir / "conversations"
        self._conversations_dir.mkdir(exist_ok=True)

    # --- Profile ---

    def get_profile(self) -> dict | None:
        """Load user profile, or None if not yet created."""
        path = self.data_dir / "profile.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def save_profile(self, profile: dict):
        """Save user profile."""
        path = self.data_dir / "profile.json"
        path.write_text(json.dumps(profile, indent=2))

    def has_profile(self) -> bool:
        return (self.data_dir / "profile.json").exists()

    # --- Goals ---

    def get_goals(self) -> list[Goal]:
        if not self._goals_file.exists():
            return []
        data = json.loads(self._goals_file.read_text())
        return [Goal(**g) for g in data]

    def save_goals(self, goals: list[Goal]):
        self._goals_file.write_text(json.dumps([asdict(g) for g in goals], indent=2))

    def set_daily_goal(self, text: str):
        goals = [g for g in self.get_goals() if g.scope != "daily"]
        goals.append(Goal(text=text, scope="daily"))
        self.save_goals(goals)

    def set_weekly_goals(self, texts: list[str]):
        goals = [g for g in self.get_goals() if g.scope != "weekly"]
        for t in texts:
            goals.append(Goal(text=t, scope="weekly"))
        self.save_goals(goals)

    def get_active_goals(self) -> dict[str, list[str]]:
        goals = self.get_goals()
        return {
            "weekly": [g.text for g in goals if g.scope == "weekly"],
            "daily": [g.text for g in goals if g.scope == "daily"],
        }

    # --- Buddy memories (learnings from conversations) ---

    def get_memories(self) -> list[dict]:
        """Load buddy memory notes."""
        path = self.data_dir / "memories.json"
        if not path.exists():
            return []
        return json.loads(path.read_text())

    def add_memories(self, notes: list[str]):
        """Append new memory notes."""
        memories = self.get_memories()
        for note in notes:
            memories.append({
                "text": note,
                "created_at": datetime.now().isoformat(),
            })
        path = self.data_dir / "memories.json"
        path.write_text(json.dumps(memories, indent=2))

    def update_profile_fields(self, updates: dict):
        """Merge updates into the existing profile."""
        profile = self.get_profile() or {}
        for k, v in updates.items():
            if v is not None:
                profile[k] = v
        self.save_profile(profile)

    # --- Conversations ---

    def save_conversation(self, convo: Conversation):
        path = self._conversations_dir / f"{convo.id}.json"
        path.write_text(json.dumps(asdict(convo), indent=2))

    def load_conversation(self, convo_id: str) -> Conversation | None:
        path = self._conversations_dir / f"{convo_id}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        data["messages"] = [Message(**m) for m in data["messages"]]
        return Conversation(**data)

    def recent_conversations(self, n: int = 5) -> list[Conversation]:
        """Load the N most recent conversations."""
        files = sorted(self._conversations_dir.glob("*.json"), reverse=True)
        convos = []
        for f in files[:n]:
            data = json.loads(f.read_text())
            data["messages"] = [Message(**m) for m in data["messages"]]
            convos.append(Conversation(**data))
        return convos

    def today_conversations(self) -> list[Conversation]:
        today = date.today().isoformat()
        convos = []
        for f in self._conversations_dir.glob("*.json"):
            data = json.loads(f.read_text())
            if data.get("started_at", "").startswith(today):
                data["messages"] = [Message(**m) for m in data["messages"]]
                convos.append(Conversation(**data))
        return convos

    def export_conversations_text(self, output_path: Path | None = None) -> Path:
        """Export all conversations to a human-readable text file."""
        if output_path is None:
            output_path = self.data_dir / "chat_history.txt"
        convos = sorted(self.recent_conversations(n=9999), key=lambda c: c.started_at)
        lines = []
        for convo in convos:
            lines.append(f"{'=' * 60}")
            lines.append(f"Conversation: {convo.id}")
            lines.append(f"Trigger: {convo.trigger}")
            lines.append(f"Started: {convo.started_at}")
            lines.append(f"{'=' * 60}")
            for msg in convo.messages:
                role = "BUDDY" if msg.role == "assistant" else "YOU"
                lines.append(f"[{msg.timestamp}] {role}: {msg.content}")
            lines.append("")
        output_path.write_text("\n".join(lines))
        return output_path
