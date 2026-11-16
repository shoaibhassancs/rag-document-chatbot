import psycopg
import pytest

from app.memory import MemoryStore
from app.startup import prepare_database
from tests.helpers import rows, scalar


@pytest.fixture
def store(settings) -> MemoryStore:
    prepare_database(settings)
    return MemoryStore(settings.database_url, history_turns=4, ttl_hours=24)


def age(settings, session_id: str, hours: int) -> None:
    """Make a session and its turns look `hours` old."""
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(
            "UPDATE chat_sessions SET last_active_at = now() - make_interval(hours => %s) "
            "WHERE session_id = %s",
            [hours, session_id],
        )
        conn.execute(
            "UPDATE chat_turns SET created_at = now() - make_interval(hours => %s) "
            "WHERE session_id = %s",
            [hours, session_id],
        )
        conn.commit()


def test_an_unknown_session_is_empty(store):
    state = store.state("nobody")
    assert state.turns == [] and state.last_status is None


def test_turns_come_back_oldest_first_with_the_last_status(store):
    store.save_turn("s1", "opening hours?", "We open at 8.", "answered")
    store.save_turn("s1", "How much is it?", "What treatment?", "clarify")
    state = store.state("s1")
    assert [(t.user, t.status) for t in state.turns] == [
        ("opening hours?", "answered"),
        ("How much is it?", "clarify"),
    ]
    assert state.turns[1].assistant == "What treatment?" and state.last_status == "clarify"


def test_only_the_last_four_turns_are_returned(store):
    for n in range(1, 7):
        store.save_turn("s1", f"question {n}", f"answer {n}", "answered")
    assert [t.user for t in store.state("s1").turns] == [f"question {n}" for n in range(3, 7)]


def test_sessions_are_isolated(store):
    store.save_turn("a", "from a", "ans a", "answered")
    store.save_turn("b", "from b", "ans b", "refused")
    assert [t.user for t in store.state("a").turns] == ["from a"]
    assert [t.user for t in store.state("b").turns] == ["from b"]
    assert store.state("b").last_status == "refused"


def test_stored_text_is_masked_but_other_text_is_kept(store, settings):
    store.save_turn("s1", "mail me at jane@example.com", "Call (555) 014-2299 or $450", "answered")
    user, assistant = rows(settings, "SELECT user_message, assistant_message FROM chat_turns")[0]
    assert user == "mail me at [EMAIL]" and assistant == "Call [PHONE] or $450"


def test_error_turns_are_kept_for_the_status_but_not_given_as_history(store):
    store.save_turn("s1", "good question", "good answer", "answered")
    store.save_turn("s1", "the failing one", "The assistant is busy", "error")
    state = store.state("s1")
    assert [t.user for t in state.turns] == ["good question"] and state.last_status == "error"


def test_an_expired_session_reads_as_empty(store, settings):
    store.save_turn("s1", "old question", "old answer", "clarify")
    age(settings, "s1", hours=25)
    state = store.state("s1")
    assert state.turns == [] and state.last_status is None


def test_saving_into_an_expired_unpurged_session_starts_afresh(store, settings):
    store.save_turn("s1", "old question", "old answer", "clarify")
    age(settings, "s1", hours=25)
    store.save_turn("s1", "new question", "new answer", "answered")
    state = store.state("s1")
    assert [t.user for t in state.turns] == ["new question"] and state.last_status == "answered"


def test_purge_removes_idle_sessions_and_their_turns_only(store, settings):
    store.save_turn("old", "q", "a", "answered")
    store.save_turn("fresh", "q", "a", "answered")
    age(settings, "old", hours=30)
    assert store.purge_expired() == 1
    assert [r[0] for r in rows(settings, "SELECT session_id FROM chat_sessions")] == ["fresh"]
    assert scalar(settings, "SELECT count(*) FROM chat_turns WHERE session_id = 'old'") == 0
    assert scalar(settings, "SELECT count(*) FROM chat_turns WHERE session_id = 'fresh'") == 1
    assert store.purge_expired() == 0


def test_the_window_and_the_ttl_come_from_the_constructor(settings):
    prepare_database(settings)
    short = MemoryStore(settings.database_url, history_turns=2, ttl_hours=1)
    for n in range(1, 4):
        short.save_turn("s1", f"q{n}", "a", "answered")
    assert [t.user for t in short.state("s1").turns] == ["q2", "q3"]
    age(settings, "s1", hours=2)
    assert short.state("s1").turns == []
