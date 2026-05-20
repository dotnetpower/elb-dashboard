from __future__ import annotations

from api.services.blast_events import canonical_job_events


def test_canonical_job_events_parse_history_payloads() -> None:
    rows = [
        {
            "PartitionKey": "job-1",
            "RowKey": "002",
            "event": "running",
            "ts": "2026-05-20T00:00:02+00:00",
            "payload_json": '{"status":"running","phase":"submitting"}',
        },
        {
            "PartitionKey": "job-1",
            "RowKey": "001",
            "event": "created",
            "ts": "2026-05-20T00:00:01+00:00",
            "payload_json": '{"status":"queued"}',
        },
    ]

    events = canonical_job_events(rows)

    assert [event["event"] for event in events] == ["created", "running"]
    assert events[1]["phase"] == "submitting"
    assert events[1]["payload"]["status"] == "running"
