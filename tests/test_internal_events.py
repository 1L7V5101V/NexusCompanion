from bus.internal_events import (
    SPAWN_COMPLETED,
    SpawnCompletionEvent,
    is_spawn_completion_message,
    make_spawn_completion_message,
    parse_spawn_completion,
)


def test_spawn_completion_helpers_roundtrip():
    event = SpawnCompletionEvent(
        job_id="abcd1234",
        label="job",
        task="do work",
        status="incomplete",
        exit_reason="forced_summary",
        result="partial",
    )
    msg = make_spawn_completion_message(
        channel="telegram",
        chat_id="123",
        event=event,
    )

    assert msg.channel == "telegram"
    assert msg.chat_id == "123"
    assert msg.sender == "spawn"
    assert msg.metadata["internal_event"] == SPAWN_COMPLETED
    assert is_spawn_completion_message(msg) is True
    assert parse_spawn_completion(msg) == event
