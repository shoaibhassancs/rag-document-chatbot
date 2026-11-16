"""Short conversation memory in Postgres, keyed by session id.

- `state()` returns the last N exchanges of a session (oldest first) and the status of the previous
  exchange. A session idle longer than the TTL reads as empty, so expiry takes effect at once.
- `save_turn()` stores one exchange after the reply is built. Text is MASKED first (emails and
  phone numbers), so personal data is not kept at rest. Cost: a follow-up cannot refer back to an
  exact email address or phone number. Reversible: remove the two `mask_text` calls below.
- `purge_expired()` deletes idle sessions (their turns go with them). The chat service calls it
  lazily, at most every ten minutes; there is no background thread.

Turns with status `error` are stored (so `last_status` is right) but never given to the model as
history.
"""

from dataclasses import dataclass, field

from app.db import connect
from app.masking import mask_text


@dataclass
class Turn:
    user: str
    assistant: str
    status: str


@dataclass
class SessionState:
    turns: list[Turn] = field(default_factory=list)  # oldest first, errors left out
    last_status: str | None = None  # status of the previous exchange, errors included


class MemoryStore:
    def __init__(self, database_url: str, history_turns: int = 4, ttl_hours: int = 24):
        self._url = database_url
        self._turns = history_turns
        self._ttl_hours = ttl_hours

    def state(self, session_id: str) -> SessionState:
        with connect(self._url) as conn:
            row = conn.execute(
                "SELECT last_status FROM chat_sessions WHERE session_id = %s "
                "AND last_active_at > now() - make_interval(hours => %s)",
                [session_id, self._ttl_hours],
            ).fetchone()
            if row is None:
                return SessionState()
            turns = conn.execute(
                "SELECT user_message, assistant_message, status FROM chat_turns "
                "WHERE session_id = %s AND status <> 'error' ORDER BY id DESC LIMIT %s",
                [session_id, self._turns],
            ).fetchall()
        return SessionState([Turn(*t) for t in reversed(turns)], row[0])

    def save_turn(self, session_id: str, user_message: str, assistant_message: str, status: str):
        with connect(self._url) as conn:
            with conn.transaction():
                conn.execute(
                    "INSERT INTO chat_sessions (session_id, last_status) VALUES (%s, %s) "
                    "ON CONFLICT (session_id) DO UPDATE SET last_active_at = now(), "
                    "last_status = EXCLUDED.last_status",
                    [session_id, status],
                )
                # A session that had expired but was not purged yet starts afresh.
                conn.execute(
                    "DELETE FROM chat_turns WHERE session_id = %s AND created_at < "
                    "now() - make_interval(hours => %s)",
                    [session_id, self._ttl_hours],
                )
                conn.execute(
                    "INSERT INTO chat_turns (session_id, user_message, assistant_message, status) "
                    "VALUES (%s, %s, %s, %s)",
                    [session_id, mask_text(user_message), mask_text(assistant_message), status],
                )

    def purge_expired(self) -> int:
        """Delete sessions idle longer than the TTL. Returns how many were removed."""
        with connect(self._url) as conn:
            cursor = conn.execute(
                "DELETE FROM chat_sessions "
                "WHERE last_active_at < now() - make_interval(hours => %s)",
                [self._ttl_hours],
            )
            return cursor.rowcount
