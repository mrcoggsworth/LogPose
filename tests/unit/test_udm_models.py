"""Unit tests for the UDM pydantic models."""

from __future__ import annotations

import json

from logpose.models.alert import Alert
from logpose.models.udm import (
    EventType,
    SecuritySeverity,
    UdmEvent,
    UdmMetadata,
    UdmNoun,
    UdmSecurityResult,
    UdmUser,
)


def test_udm_event_defaults_are_minimal_but_valid() -> None:
    event = UdmEvent()
    assert event.metadata.event_type == EventType.GENERIC_EVENT
    assert event.principal is None
    assert event.security_result == []
    assert event.about == []
    assert event.additional == {}


def test_udm_event_json_round_trip() -> None:
    event = UdmEvent(
        metadata=UdmMetadata(
            event_type=EventType.USER_LOGIN,
            vendor_name="Amazon Web Services",
            product_name="AWS CloudTrail",
        ),
        principal=UdmNoun(
            user=UdmUser(
                userid="arn:aws:iam::123:user/alice", user_display_name="alice"
            )
        ),
        security_result=[
            UdmSecurityResult(severity=SecuritySeverity.HIGH, summary="failed login")
        ],
        additional={"raw_event_name": "ConsoleLogin"},
    )

    restored = UdmEvent.model_validate_json(event.model_dump_json())

    assert restored == event
    assert restored.metadata.event_type == EventType.USER_LOGIN
    assert restored.principal is not None
    assert restored.principal.user is not None
    assert restored.principal.user.userid == "arn:aws:iam::123:user/alice"


def test_alert_udm_defaults_to_none_and_serializes() -> None:
    alert = Alert(source="kafka", raw_payload={"a": 1})
    assert alert.udm is None
    assert json.loads(alert.model_dump_json())["udm"] is None


def test_alert_round_trips_with_udm_attached() -> None:
    alert = Alert(source="kafka", raw_payload={"a": 1})
    with_udm = alert.model_copy(
        update={
            "udm": UdmEvent(
                metadata=UdmMetadata(event_type=EventType.RESOURCE_READ)
            )
        }
    )

    restored = Alert.model_validate_json(with_udm.model_dump_json())

    assert restored.udm is not None
    assert restored.udm.metadata.event_type == EventType.RESOURCE_READ
    # raw_payload survives untouched next to the UDM view
    assert restored.raw_payload == {"a": 1}


def test_event_type_serializes_as_string() -> None:
    event = UdmEvent(metadata=UdmMetadata(event_type=EventType.NETWORK_CONNECTION))
    data = json.loads(event.model_dump_json())
    assert data["metadata"]["event_type"] == "NETWORK_CONNECTION"
