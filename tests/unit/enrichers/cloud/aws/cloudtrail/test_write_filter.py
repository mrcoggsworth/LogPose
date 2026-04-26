"""Unit tests for ``WriteCallFilterEnricher``."""

from __future__ import annotations

import json
from typing import Any

from logpose.enrichers.cloud.aws.cloudtrail.write_filter import WriteCallFilterEnricher
from logpose.enrichers.context import EnricherContext
from logpose.models.alert import Alert


def _ctx_with_history(events: list[dict[str, Any]]) -> EnricherContext:
    ctx = EnricherContext(alert=Alert(source="sqs", raw_payload={}))
    ctx.extracted["cloudtrail"] = {"principal_recent_events": events}
    return ctx


def _lookup_item(cloudtrail_event: dict[str, Any] | str) -> dict[str, Any]:
    """Build a LookupEvents response item (CloudTrailEvent is a JSON string)."""
    is_dict = isinstance(cloudtrail_event, dict)
    name = cloudtrail_event.get("eventName", "?") if is_dict else "?"
    body = json.dumps(cloudtrail_event) if is_dict else cloudtrail_event
    return {
        "EventId": "abc",
        "EventName": name,
        "CloudTrailEvent": body,
    }


def test_filters_to_successful_writes_only() -> None:
    events = [
        _lookup_item({"eventName": "PutObject", "readOnly": False}),  # ✓ kept
        _lookup_item({"eventName": "GetObject", "readOnly": True}),  # ✗ read
        _lookup_item(
            {"eventName": "CreateUser", "readOnly": False, "errorCode": "AccessDenied"}
        ),  # ✗ failed
        _lookup_item({"eventName": "DeleteObject", "readOnly": False}),  # ✓ kept
    ]
    ctx = _ctx_with_history(events)
    WriteCallFilterEnricher().run(ctx)
    writes = ctx.extracted["cloudtrail"]["successful_writes"]
    names = [w["eventName"] for w in writes]
    assert names == ["PutObject", "DeleteObject"]
    assert ctx.errors == []


def test_no_history_writes_empty_list() -> None:
    ctx = EnricherContext(alert=Alert(source="sqs", raw_payload={}))
    WriteCallFilterEnricher().run(ctx)
    assert ctx.extracted["cloudtrail"]["successful_writes"] == []
    assert ctx.errors == []


def test_history_with_no_writes_yields_empty_list() -> None:
    events = [
        _lookup_item({"eventName": "GetObject", "readOnly": True}),
        _lookup_item({"eventName": "ListBuckets", "readOnly": True}),
    ]
    ctx = _ctx_with_history(events)
    WriteCallFilterEnricher().run(ctx)
    assert ctx.extracted["cloudtrail"]["successful_writes"] == []
    assert ctx.errors == []


def test_already_parsed_cloudtrail_event_is_handled() -> None:
    # Some test fixtures pre-parse CloudTrailEvent into a dict.
    events = [
        {
            "EventId": "1",
            "EventName": "PutObject",
            "CloudTrailEvent": {"eventName": "PutObject", "readOnly": False},
        }
    ]
    ctx = _ctx_with_history(events)
    WriteCallFilterEnricher().run(ctx)
    assert len(ctx.extracted["cloudtrail"]["successful_writes"]) == 1


def test_unparseable_event_records_partial_parse_failure() -> None:
    events = [
        _lookup_item("not-json"),
        _lookup_item({"eventName": "PutObject", "readOnly": False}),
    ]
    ctx = _ctx_with_history(events)
    WriteCallFilterEnricher().run(ctx)
    # The valid event still survives.
    assert len(ctx.extracted["cloudtrail"]["successful_writes"]) == 1
    # And the parse failure is recorded.
    assert any(e["type"] == "PartialParseFailure" for e in ctx.errors)


def test_missing_readonly_field_treated_as_not_write() -> None:
    # Defensive: if readOnly is absent (older event format), don't claim it was a write.
    events = [_lookup_item({"eventName": "WeirdEvent"})]  # no readOnly
    ctx = _ctx_with_history(events)
    WriteCallFilterEnricher().run(ctx)
    assert ctx.extracted["cloudtrail"]["successful_writes"] == []


def test_run_never_raises_with_garbage_input() -> None:
    ctx = EnricherContext(alert=Alert(source="sqs", raw_payload={}))
    ctx.extracted["cloudtrail"] = {"principal_recent_events": [None, 42, "string"]}
    WriteCallFilterEnricher().run(ctx)  # must not raise
    assert ctx.extracted["cloudtrail"]["successful_writes"] == []
