"""Canonical, provider-aware identity used as the enricher cache key.

A ``Principal`` is the actor referenced by an alert (an AWS IAM role, a GCP
service account, an Active Directory user). It is *not* a full identity record —
just enough information that two alerts referencing the same actor produce the
same ``cache_key()`` so per-principal lookups (history, group memberships, etc.)
can be cached.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

Provider = Literal["aws", "gcp", "ad"]


class Principal(BaseModel):
    """Provider-aware actor identity used as a cache key by enrichers."""

    provider: Provider
    normalized_id: str  # canonical, lowercased where appropriate
    raw_id: str  # original string from the source payload (preserved for audit)
    display_name: str | None = None
    account_or_project: str | None = None  # AWS account id, GCP project, AD domain

    model_config = {"frozen": True}

    def cache_key(self) -> str:
        """Return a globally unique cache key.

        The provider prefix prevents collisions between providers that
        coincidentally share an identifier string.
        """
        return f"{self.provider}::{self.normalized_id}"


# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------


def from_aws_user_identity(user_identity: dict[str, Any]) -> Principal:
    """Build a ``Principal`` from a CloudTrail ``userIdentity`` block.

    Handles the userIdentity ``type`` values that real CloudTrail events emit:
    ``IAMUser``, ``Root``, ``AssumedRole``, ``FederatedUser``, ``AWSService``,
    ``AWSAccount``. For ``AssumedRole`` the principal is the *role*, not the
    session — the session suffix is collapsed so all sessions assumed from the
    same role share a cache key.

    Raises ``ValueError`` when the input lacks the minimum fields needed to
    identify an actor; callers wrap and record the error in
    ``EnricherContext.errors``.
    """
    if not isinstance(user_identity, dict):
        raise ValueError("userIdentity must be a dict")

    identity_type = user_identity.get("type")
    arn = user_identity.get("arn")
    account = user_identity.get("accountId")

    if identity_type == "AWSService":
        invoked_by = user_identity.get("invokedBy")
        if not invoked_by:
            raise ValueError("AWSService userIdentity missing invokedBy")
        return Principal(
            provider="aws",
            normalized_id=f"service:{invoked_by.lower()}",
            raw_id=invoked_by,
            display_name=invoked_by,
        )

    if identity_type == "AssumedRole":
        # Prefer sessionContext.sessionIssuer.arn — it's already the role ARN.
        session_context = user_identity.get("sessionContext")
        session_issuer: dict[str, Any] = {}
        if isinstance(session_context, dict):
            issuer_raw = session_context.get("sessionIssuer")
            if isinstance(issuer_raw, dict):
                session_issuer = issuer_raw

        issuer_arn = session_issuer.get("arn")
        role_name = session_issuer.get("userName")

        if issuer_arn:
            normalized = issuer_arn
        elif arn:
            normalized = _collapse_assumed_role_arn(arn)
        else:
            raise ValueError("AssumedRole missing arn and sessionIssuer.arn")

        return Principal(
            provider="aws",
            normalized_id=normalized,
            raw_id=arn or normalized,
            display_name=role_name or _name_from_arn(normalized),
            account_or_project=account or _account_from_arn(normalized),
        )

    if not arn:
        raise ValueError(f"userIdentity type={identity_type!r} missing arn")

    if identity_type == "Root":
        return Principal(
            provider="aws",
            normalized_id=arn,
            raw_id=arn,
            display_name="root",
            account_or_project=account or _account_from_arn(arn),
        )

    # IAMUser, FederatedUser, AWSAccount, and any other type with a usable ARN.
    return Principal(
        provider="aws",
        normalized_id=arn,
        raw_id=arn,
        display_name=user_identity.get("userName") or _name_from_arn(arn),
        account_or_project=account or _account_from_arn(arn),
    )


def _collapse_assumed_role_arn(arn: str) -> str:
    """``arn:aws:sts::123:assumed-role/MyRole/sess`` → ``arn:aws:iam::123:role/MyRole``.

    Returns the input unchanged if the shape doesn't match; callers should not
    treat that as success — but raising here would lose the raw value, so we
    let the resulting ARN flow through and rely on the test suite to flag it.
    """
    parts = arn.split(":")
    if len(parts) < 6 or parts[2] != "sts":
        return arn
    resource = parts[5]
    if not resource.startswith("assumed-role/"):
        return arn
    role_segments = resource.split("/")
    if len(role_segments) < 2:
        return arn
    role_name = role_segments[1]
    account = parts[4]
    partition = parts[1]
    return f"arn:{partition}:iam::{account}:role/{role_name}"


def _account_from_arn(arn: str) -> str | None:
    parts = arn.split(":")
    return parts[4] if len(parts) >= 5 and parts[4] else None


def _name_from_arn(arn: str) -> str | None:
    parts = arn.split(":")
    if len(parts) < 6:
        return None
    resource = parts[5]
    # resource looks like "user/alice", "role/Foo", "federated-user/Alice", "root", etc.
    if "/" in resource:
        return resource.split("/", 1)[1]
    return resource


# ---------------------------------------------------------------------------
# GCP
# ---------------------------------------------------------------------------


_GCP_SERVICE_ACCOUNT_SUFFIX = ".iam.gserviceaccount.com"


def from_gcp_audit_authentication(auth_info: dict[str, Any]) -> Principal:
    """Build a ``Principal`` from a GCP audit log ``authenticationInfo`` block.

    Service accounts (ending in ``.iam.gserviceaccount.com``) keep their email
    as ``normalized_id`` directly. Human users get a ``user:`` prefix so a
    user named ``svc@x.com`` cannot collide with a service account email.

    Raises ``ValueError`` when ``principalEmail`` is missing.
    """
    if not isinstance(auth_info, dict):
        raise ValueError("authenticationInfo must be a dict")
    email = auth_info.get("principalEmail")
    if not email or not isinstance(email, str):
        raise ValueError("authenticationInfo missing principalEmail")

    email_lower = email.lower()
    is_service_account = email_lower.endswith(_GCP_SERVICE_ACCOUNT_SUFFIX)

    project: str | None = None
    if is_service_account:
        # svc-acct@my-project.iam.gserviceaccount.com → "my-project"
        local_and_host = email_lower.rsplit("@", 1)
        if len(local_and_host) == 2:
            host = local_and_host[1]
            project = host[: -len(_GCP_SERVICE_ACCOUNT_SUFFIX)] or None

    normalized = email_lower if is_service_account else f"user:{email_lower}"

    return Principal(
        provider="gcp",
        normalized_id=normalized,
        raw_id=email,
        display_name=email,
        account_or_project=project,
    )


# ---------------------------------------------------------------------------
# Active Directory
# ---------------------------------------------------------------------------


def from_ad_event(event: dict[str, Any]) -> Principal:
    """Build a ``Principal`` from an Active Directory event payload.

    Accepts either the standard Windows event-log shape
    (``{"domain_name": "DOMAIN", "sam_account_name": "user"}``) or a short
    form (``{"domain": ..., "sam_account": ...}``). Domain is uppercased,
    SAM account is lowercased — Windows treats both case-insensitively and
    uppercase-domain / lowercase-sam is the most common written form.

    Raises ``ValueError`` when neither field pair is present.
    """
    if not isinstance(event, dict):
        raise ValueError("AD event must be a dict")

    domain = event.get("domain_name") or event.get("domain")
    sam = event.get("sam_account_name") or event.get("sam_account")

    if not domain or not sam:
        raise ValueError("AD event missing domain and sam_account fields")

    domain_norm = str(domain).upper()
    sam_norm = str(sam).lower()
    raw = f"{domain}\\{sam}"
    normalized = f"{domain_norm}\\{sam_norm}"

    return Principal(
        provider="ad",
        normalized_id=normalized,
        raw_id=raw,
        display_name=str(sam),
        account_or_project=domain_norm,
    )
