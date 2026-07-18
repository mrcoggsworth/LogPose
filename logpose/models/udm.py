"""Pragmatic subset of Google Chronicle's Unified Data Model (UDM).

Mirrors the shape documented in the UDM overview
(https://cloud.google.com/chronicle/docs/event-processing/udm-overview):
a ``metadata`` section describing the event itself, plus "noun" sections
(``principal``, ``target``, ``src``, ``observer``, ``about``) describing the
entities involved, and ``network`` / ``security_result`` sections.

This is intentionally NOT the full Chronicle field dictionary — only the
fields LogPose mappers populate today. Extend field-by-field as workflows
need them; never rename existing fields (N8N workflows depend on this
contract).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class EventType(str, Enum):
    """Subset of Chronicle's metadata.event_type enum.

    Use the most specific type that applies; GENERIC_EVENT is the fallback
    when nothing better is known.
    """

    GENERIC_EVENT = "GENERIC_EVENT"
    USER_LOGIN = "USER_LOGIN"
    USER_LOGOUT = "USER_LOGOUT"
    USER_RESOURCE_ACCESS = "USER_RESOURCE_ACCESS"
    RESOURCE_CREATION = "RESOURCE_CREATION"
    RESOURCE_DELETION = "RESOURCE_DELETION"
    RESOURCE_READ = "RESOURCE_READ"
    RESOURCE_PERMISSIONS_CHANGE = "RESOURCE_PERMISSIONS_CHANGE"
    NETWORK_CONNECTION = "NETWORK_CONNECTION"
    SCAN_UNCATEGORIZED = "SCAN_UNCATEGORIZED"
    SERVICE_UNSPECIFIED = "SERVICE_UNSPECIFIED"


class SecuritySeverity(str, Enum):
    """Severity levels for security_result, matching Chronicle's enum."""

    UNKNOWN_SEVERITY = "UNKNOWN_SEVERITY"
    INFORMATIONAL = "INFORMATIONAL"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class UdmUser(BaseModel):
    """The user portion of a noun — who acted or was acted upon."""

    userid: str  # canonical identity, e.g. an ARN or "user:alice@example.com"
    user_display_name: str | None = None
    email_addresses: list[str] = Field(default_factory=list)


class UdmResource(BaseModel):
    """A resource referenced by a noun (bucket, instance, table, ...)."""

    name: str
    resource_type: str | None = None


class UdmCloud(BaseModel):
    """Cloud context for a noun."""

    environment: str | None = None  # "AWS" | "GCP" | "AZURE" | ...
    account_id: str | None = None
    project_id: str | None = None
    region: str | None = None


class UdmNoun(BaseModel):
    """An entity involved in the event.

    Chronicle calls these "nouns": the same structure describes principal
    (the actor), target (what was acted on), src, observer, and about.
    """

    user: UdmUser | None = None
    hostname: str | None = None
    ip: list[str] = Field(default_factory=list)
    port: int | None = None
    application: str | None = None
    resource: UdmResource | None = None
    cloud: UdmCloud | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class UdmNetwork(BaseModel):
    """Network details when the event describes network activity."""

    application_protocol: str | None = None
    http_method: str | None = None
    http_response_code: int | None = None


class UdmSecurityResult(BaseModel):
    """A classification or verdict from a security product."""

    severity: SecuritySeverity = SecuritySeverity.UNKNOWN_SEVERITY
    summary: str | None = None
    category_details: str | None = None
    rule_name: str | None = None
    action: str | None = None  # e.g. "ALLOW" | "BLOCK" | "QUARANTINE"


class UdmMetadata(BaseModel):
    """General information about the event itself."""

    event_type: EventType = EventType.GENERIC_EVENT
    event_timestamp: datetime | None = None  # when the event occurred at source
    ingested_timestamp: datetime | None = None  # when LogPose received it
    vendor_name: str | None = None  # e.g. "Amazon Web Services"
    product_name: str | None = None  # e.g. "AWS CloudTrail"
    product_event_type: str | None = None  # vendor-native type, e.g. "PutObject"
    product_log_id: str | None = None  # vendor-native event id
    description: str | None = None


class UdmEvent(BaseModel):
    """A normalized security event, attached to Alert.udm by the Router.

    ``raw_payload`` on the Alert always remains the source of truth for the
    original event — UDM is a normalized view, never a replacement.
    """

    metadata: UdmMetadata = Field(default_factory=UdmMetadata)
    principal: UdmNoun | None = None
    target: UdmNoun | None = None
    src: UdmNoun | None = None
    observer: UdmNoun | None = None
    about: list[UdmNoun] = Field(default_factory=list)
    network: UdmNetwork | None = None
    security_result: list[UdmSecurityResult] = Field(default_factory=list)
    # Escape hatch for mapper fields with no UDM home yet (mirrors Chronicle's
    # "additional" section). Keys should be snake_case strings.
    additional: dict[str, str] = Field(default_factory=dict)
