from __future__ import annotations

import json

from api.services.job_logs import event_bus


def test_publish_job_log_event_writes_bounded_sanitised_stream(monkeypatch) -> None:
    calls = []

    class FakeRedis:
        def xadd(self, key, fields, *, maxlen, approximate):
            calls.append(
                {
                    "key": key,
                    "fields": fields,
                    "maxlen": maxlen,
                    "approximate": approximate,
                }
            )

    monkeypatch.setattr(event_bus, "_redis_client", lambda: FakeRedis())

    event_bus.publish_job_log_event(
        "job/unsafe value",
        source="terminal_exec",
        phase="submitting",
        stream="stderr",
        line="hello sig=abc",
    )

    assert calls[0]["key"] == "joblogs:job_unsafe_value"
    assert calls[0]["maxlen"] == 5000
    assert calls[0]["approximate"] is True
    payload = json.loads(calls[0]["fields"]["event"])
    assert payload["job_id"] == "job/unsafe value"
    assert payload["source"] == "terminal_exec"
    assert payload["phase"] == "submitting"
    assert payload["stream"] == "stderr"
    assert payload["line"] == "hello sig=abc"


def test_read_job_log_events_decodes_redis_stream_rows(monkeypatch) -> None:
    class FakeRedis:
        def xread(self, streams, *, count, block):
            assert streams == {"joblogs:job-1": "0-0"}
            assert count == 100
            assert block == 5000
            return [
                (
                    b"joblogs:job-1",
                    [
                        (
                            b"1700000000000-0",
                            {b"event": b'{"job_id":"job-1","phase":"running","line":"ok"}'},
                        )
                    ],
                )
            ]

    monkeypatch.setattr(event_bus, "_redis_client", lambda: FakeRedis())

    events = event_bus.read_job_log_events("job-1")

    assert events == [
        {
            "id": "1700000000000-0",
            "job_id": "job-1",
            "phase": "running",
            "line": "ok",
        }
    ]
