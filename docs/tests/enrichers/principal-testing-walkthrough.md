# Principal & Enricher Skeleton Testing Walkthrough

This document covers how to test the Phase A surface of the
`logpose.enrichers` package:

- The `Principal` model and its provider-aware normalizers (AWS, GCP, AD)
- The `Enricher` Protocol — runtime-checkable, structurally typed
- The `EnricherContext` dataclass

Phase A is **pure data + types**: no AWS calls, no cache, no async runner.
That arrives in Phases B–F. Tests therefore run with no Docker, no
RabbitMQ, no boto3 — just `pytest`.

---

## Background: what the package contributes

`logpose.enrichers` provides the small, typed primitives that every runbook
will use to compose enrichment pipelines. See
[`docs/enrichers/README.md`](../../enrichers/README.md) for the
architecture overview.

Phase A's job is to lock down the shapes that everything else builds on:

```
Phase A surface
  ├─ Principal             ← the cache key
  ├─ from_aws_user_identity
  ├─ from_gcp_audit_authentication
  ├─ from_ad_event
  ├─ Enricher (Protocol)
  └─ EnricherContext (dataclass)
```

Nothing in production wires into this yet. The runbook integration lands in
Phase E.

---

## Part 1: Unit Testing the Principal Normalizers

Unit tests live in `tests/unit/enrichers/test_principal.py`. There is no
runbook to instantiate, no broker to mock — every test calls a normalizer
directly and asserts on the returned `Principal`.

### The mock structure

There isn't one. The Phase A code is a pure function of its inputs:

```python
import pytest

from logpose.enrichers.principal import (
    Principal,
    from_ad_event,
    from_aws_user_identity,
    from_gcp_audit_authentication,
)
```

Each test builds a `dict` representing the relevant slice of an event
(CloudTrail `userIdentity`, GCP `authenticationInfo`, or an AD event),
calls the normalizer, and asserts on the resulting `Principal` and its
`cache_key()`.

```python
def test_aws_iam_user_arn_round_trip() -> None:
    p = from_aws_user_identity(
        {
            "type": "IAMUser",
            "arn": "arn:aws:iam::123456789012:user/alice",
            "userName": "alice",
            "accountId": "123456789012",
        }
    )
    assert p.normalized_id == "arn:aws:iam::123456789012:user/alice"
    assert p.cache_key() == "aws::arn:aws:iam::123456789012:user/alice"
```

### Running the unit tests

No Docker required:

```sh
pytest tests/unit/enrichers/ -v
```

Expected:

```
tests/unit/enrichers/test_principal.py::test_aws_iam_user_arn_round_trip PASSED
tests/unit/enrichers/test_principal.py::test_aws_assumed_role_uses_session_issuer_arn_when_present PASSED
tests/unit/enrichers/test_principal.py::test_aws_assumed_role_collapses_session_suffix_without_session_issuer PASSED
tests/unit/enrichers/test_principal.py::test_aws_two_sessions_of_same_role_share_cache_key PASSED
tests/unit/enrichers/test_principal.py::test_aws_root_account PASSED
tests/unit/enrichers/test_principal.py::test_aws_service_principal PASSED
tests/unit/enrichers/test_principal.py::test_aws_federated_user_uses_arn PASSED
tests/unit/enrichers/test_principal.py::test_aws_invalid_user_identity_raises PASSED
tests/unit/enrichers/test_principal.py::test_gcp_service_account PASSED
tests/unit/enrichers/test_principal.py::test_gcp_service_account_lowercases_email PASSED
tests/unit/enrichers/test_principal.py::test_gcp_user_principal PASSED
tests/unit/enrichers/test_principal.py::test_gcp_user_cannot_collide_with_service_account_email PASSED
tests/unit/enrichers/test_principal.py::test_gcp_missing_email_raises PASSED
tests/unit/enrichers/test_principal.py::test_ad_domain_uppercase_sam_lowercase PASSED
tests/unit/enrichers/test_principal.py::test_ad_short_field_names_accepted PASSED
tests/unit/enrichers/test_principal.py::test_ad_case_insensitive_input_yields_same_key PASSED
tests/unit/enrichers/test_principal.py::test_ad_missing_fields_raises PASSED
tests/unit/enrichers/test_principal.py::test_cross_provider_keys_dont_collide PASSED
tests/unit/enrichers/test_principal.py::test_principal_is_frozen PASSED

19 passed in 0.10s
```

### What each test covers

#### AWS

| Test | What it verifies |
|---|---|
| `test_aws_iam_user_arn_round_trip` | An `IAMUser` event keeps its ARN and lifts `userName` into `display_name` |
| `test_aws_assumed_role_uses_session_issuer_arn_when_present` | When `sessionContext.sessionIssuer.arn` is provided, that role ARN becomes `normalized_id`; the raw assumed-role ARN is preserved as `raw_id` |
| `test_aws_assumed_role_collapses_session_suffix_without_session_issuer` | Without a session issuer, the assumed-role ARN is collapsed to the underlying role ARN |
| `test_aws_two_sessions_of_same_role_share_cache_key` | Two events from different sessions of the *same* role produce identical `cache_key()` values — this is what makes the principal cache useful |
| `test_aws_root_account` | `Root` events get `display_name="root"` and an `iam::ACCT:root` ARN |
| `test_aws_service_principal` | `AWSService` events use `service:<invokedBy>` as the id (no ARN exists for these) |
| `test_aws_federated_user_uses_arn` | `FederatedUser` keeps the federated-user ARN |
| `test_aws_invalid_user_identity_raises` | Missing `arn` or `invokedBy` raises `ValueError` — caller is expected to wrap and record |

#### GCP

| Test | What it verifies |
|---|---|
| `test_gcp_service_account` | A service account email becomes the `normalized_id` directly; project parsed from the host |
| `test_gcp_service_account_lowercases_email` | Mixed-case input collapses to lowercase so case variants share a cache key |
| `test_gcp_user_principal` | A human user gets a `user:` prefix in `normalized_id` and `cache_key()` |
| `test_gcp_user_cannot_collide_with_service_account_email` | A user with an email that *looks* like a service account is still routed under `user:` because the suffix is the discriminator |
| `test_gcp_missing_email_raises` | Missing `principalEmail` raises `ValueError` |

#### Active Directory

| Test | What it verifies |
|---|---|
| `test_ad_domain_uppercase_sam_lowercase` | Domain uppercased, SAM lowercased — produces `CORP\alice` |
| `test_ad_short_field_names_accepted` | Both `domain_name`/`sam_account_name` and `domain`/`sam_account` are accepted |
| `test_ad_case_insensitive_input_yields_same_key` | Case variants of the same identity collapse to one `cache_key()` |
| `test_ad_missing_fields_raises` | Missing either field raises `ValueError` |

#### Cross-provider invariants

| Test | What it verifies |
|---|---|
| `test_cross_provider_keys_dont_collide` | Three `Principal`s with the same `normalized_id` but different providers produce three distinct cache keys — the `aws::`/`gcp::`/`ad::` prefix isolates namespaces |
| `test_principal_is_frozen` | `Principal` is immutable; mutation raises (Pydantic v2 frozen contract) |

### Adding a new normalizer test

To verify a new branch in an existing normalizer (e.g. handling an
additional `userIdentity.type`), follow the existing pattern:

```python
def test_aws_handles_new_identity_type() -> None:
    p = from_aws_user_identity(
        {
            "type": "NewType",
            "arn": "arn:aws:iam::123456789012:something/foo",
            "accountId": "123456789012",
        }
    )
    assert p.provider == "aws"
    assert p.normalized_id == "arn:aws:iam::123456789012:something/foo"
    assert p.cache_key().startswith("aws::")
```

Always assert on **both** the structured fields you care about (`normalized_id`,
`display_name`, `account_or_project`) and `cache_key()` — the cache key is
the load-bearing string for downstream caching, so test it explicitly.

When adding a new error path, prefer `pytest.raises(ValueError)` and let
the message be free-form. Callers don't switch on the message; they just
catch and append to `ctx.errors`.

---

## Part 2: Verifying the package wires together

The package exposes its public surface through `logpose.enrichers`:

```sh
python -c "from logpose.enrichers import Enricher, EnricherContext, Principal, \
    from_aws_user_identity, from_gcp_audit_authentication, from_ad_event; \
    print('imports OK')"
```

Expected: `imports OK`.

To confirm the `Enricher` Protocol is `@runtime_checkable`:

```sh
python -c "
from logpose.enrichers import Enricher

class MyEnricher:
    name = 'my'
    cache_ttl = None
    timeout = 3.0
    def run(self, ctx): pass

print(isinstance(MyEnricher(), Enricher))   # True
"
```

Expected: `True`.

---

## Part 3: Verification loop

Phase A's verification loop is the project standard, restricted to the new
files:

```sh
# 1. Tests
pytest tests/unit/enrichers/ -v

# 2. Type checking
python -m mypy logpose/enrichers/

# 3. Format check (line-length 100 — see pyproject.toml)
python -m black --check logpose/enrichers/ tests/unit/enrichers/

# 4. Lint
python -m flake8 logpose/enrichers/ tests/unit/enrichers/
```

All four must be clean. The full unit suite (`pytest tests/unit/`) should
also pass — Phase A is purely additive and must not regress any existing
test.

> **Note on flake8 vs black:** the project's `.flake8` sets
> `max-line-length = 88` while `pyproject.toml` configures black at 100.
> This is a pre-existing project-wide drift (28 baseline E501 violations
> across the codebase). Phase A files are kept under flake8's 88-char
> limit so they are clean against both tools.

---

## Why no integration test in Phase A?

`Principal` does not interact with RabbitMQ, boto3, or any external
service — there is nothing to integrate. The first integration test
arrives in Phase E (`tests/integration/test_cloudtrail_runbook_pipeline.py`),
which exercises the full pipeline end-to-end against `moto`-backed AWS.

---

## Relevant Files

| File | Purpose |
|---|---|
| [`logpose/enrichers/__init__.py`](../../../logpose/enrichers/__init__.py) | Package public surface |
| [`logpose/enrichers/principal.py`](../../../logpose/enrichers/principal.py) | `Principal` model + AWS/GCP/AD provider normalizers |
| [`logpose/enrichers/protocol.py`](../../../logpose/enrichers/protocol.py) | `@runtime_checkable` `Enricher` Protocol |
| [`logpose/enrichers/context.py`](../../../logpose/enrichers/context.py) | `EnricherContext` dataclass |
| [`tests/unit/enrichers/test_principal.py`](../../../tests/unit/enrichers/test_principal.py) | 19 unit tests covering every normalizer branch + cross-provider invariants |
| [`docs/enrichers/README.md`](../../enrichers/README.md) | Architecture overview, public surface, adding-a-new-enricher recipe |
