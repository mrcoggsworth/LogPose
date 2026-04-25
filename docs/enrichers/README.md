# Enrichers

The `logpose.enrichers` package provides the building blocks runbooks use to
attach contextual information to alerts beyond the fields parseable from the
raw payload — principal lookups, history queries, object inspections, and so
on.

This is the **shared infrastructure** that runbooks compose. The
runbook-specific enrichers (CloudTrail, GuardDuty, GCP audit) live in
`logpose.enrichers.cloud.<provider>.<service>` and ship phase by phase.

> **Status — Phase A (skeleton).** This package currently exposes:
> the `Enricher` Protocol, the `EnricherContext` dataclass, and the
> `Principal` model with provider-aware normalizers. The TTL cache
> (Phase B), async pipeline runner (Phase C), and concrete CloudTrail
> enrichers (Phase D) land in subsequent commits.

---

## Why a separate package?

Today, `CloudTrailRunbook.enrich()` is a single function that pulls fields
out of the raw payload. As soon as the runbook needs to call AWS APIs (look
up the principal's recent activity, describe the object that was modified,
fetch role policies), one function becomes hostile to test, slow because of
sequential network I/O, and impossible to reuse from `GuardDutyRunbook`
which needs the same principal-history lookup.

The enricher package splits that work into small, typed, independently
testable units that the runbook composes:

```
Alert
  ↓
[Runbook orchestrator]
  ↓
[basic field extraction]
  ↓
[EnricherPipeline]
   ├── stage 0: PrincipalIdentity   (sets ctx.principal)
   ├── stage 1: PrincipalHistory  ⨯  WriteCallFilter   (parallel)
   └── stage 2: ObjectInspection                       (depends on stage 1)
  ↓
EnrichedAlert
```

Each stage runs in parallel via `asyncio.to_thread` over a shared
`ThreadPoolExecutor` — boto3 stays sync, the orchestrator gets the
parallelism for free.

---

## Public surface (Phase A)

### `Enricher` — the contract

```python
from logpose.enrichers import Enricher

@runtime_checkable
class Enricher(Protocol):
    name: str            # short id used in metrics and error records
    cache_ttl: int | None  # None ⇒ use the cache's default TTL
    timeout: float       # seconds; per-call wall-clock cap

    def run(self, ctx: EnricherContext) -> None: ...
```

A new enricher is just a class that implements `run`. The Protocol is
`@runtime_checkable` so tests can `isinstance(my_enricher, Enricher)`.

**Contract:** `run` mutates `ctx` in place and **must not raise**. Catch
internally and append to `ctx.errors`. The runner has a defensive top-level
catch but relying on it loses per-enricher error attribution. This honours
the project's "no silent drops" rule (see `CLAUDE.md`) — every failure is
recorded, never swallowed.

### `EnricherContext` — the per-alert state

```python
@dataclass
class EnricherContext:
    alert: Alert
    extracted: dict[str, Any]             # merged into EnrichedAlert.extracted
    principal: Principal | None           # set by the principal-identity enricher
    errors: list[dict[str, str]]          # {"enricher", "error", "type"} per failure
    timings: list[dict[str, Any]]         # populated in Phase F
```

The orchestrator constructs one context per alert, hands it to the
pipeline, then promotes `principal`, `errors`, and `timings` into
`EnrichedAlert.extracted` under reserved keys.

### `Principal` — the cache key

```python
class Principal(BaseModel):
    provider: Literal["aws", "gcp", "ad"]
    normalized_id: str
    raw_id: str
    display_name: str | None
    account_or_project: str | None

    def cache_key(self) -> str:
        return f"{self.provider}::{self.normalized_id}"
```

`Principal` is the **canonical actor identity** referenced by an alert. It
is *not* a full identity record — just enough that two alerts referencing
the same actor produce the same `cache_key()`, so per-principal lookups
(history, group memberships, etc.) cache cleanly.

The provider prefix (`"aws::"`, `"gcp::"`, `"ad::"`) prevents cross-provider
collisions: an AWS user named `alice@example.com` cannot collide with a GCP
user with the same email.

**Provider normalizers** in `logpose.enrichers.principal`:

| Function | Input | Notes |
|---|---|---|
| `from_aws_user_identity(user_identity)` | CloudTrail `userIdentity` block | Handles `IAMUser`, `Root`, `AssumedRole` (collapses session suffix), `FederatedUser`, `AWSService`, `AWSAccount`. `AssumedRole` prefers `sessionContext.sessionIssuer.arn`. |
| `from_gcp_audit_authentication(auth_info)` | GCP audit log `authenticationInfo` | Service accounts (`*.iam.gserviceaccount.com`) keep their email as the id; users get a `user:` prefix to avoid colliding with service-account-shaped emails. Project parsed from the SA host. |
| `from_ad_event(event)` | AD event payload | Accepts `domain_name`/`sam_account_name` or `domain`/`sam_account`. Domain uppercased, SAM lowercased so case variants collapse to one cache key. |

Each normalizer raises `ValueError` when the input is unusable. Callers
wrap this and append a structured entry to `ctx.errors` — the contract is
"the orchestrator never sees an exception".

### Cache key examples

```python
Principal(provider="aws", normalized_id="arn:aws:iam::123:role/MyRole", ...).cache_key()
# → "aws::arn:aws:iam::123:role/MyRole"

Principal(provider="gcp", normalized_id="svc-acct@proj.iam.gserviceaccount.com", ...).cache_key()
# → "gcp::svc-acct@proj.iam.gserviceaccount.com"

Principal(provider="ad", normalized_id="CORP\\alice", ...).cache_key()
# → "ad::CORP\\alice"
```

### AssumedRole collapsing — why

Two CloudTrail events from sessions of the same IAM role would produce
*different* assumed-role ARNs (the session suffix differs each time):

```
arn:aws:sts::123:assumed-role/MyRole/i-abc123
arn:aws:sts::123:assumed-role/MyRole/i-def456
```

If we cached on the raw ARN every session would be a cache miss. The
normalizer collapses these to the underlying role:

```
arn:aws:iam::123:role/MyRole
```

…so all sessions assumed from one role share a cache key and one
`LookupEvents` call covers them all.

---

## Adding a new enricher

```python
from logpose.enrichers import Enricher, EnricherContext

class MyEnricher:
    name = "my_enricher"
    cache_ttl: int | None = None  # use cache default
    timeout: float = 3.0

    def run(self, ctx: EnricherContext) -> None:
        try:
            # do work, mutate ctx.extracted
            pass
        except Exception as exc:
            ctx.errors.append(
                {"enricher": self.name, "error": str(exc), "type": type(exc).__name__}
            )
```

That's the whole contract — no base class, no decorators, no registration.

---

## Roadmap

| Phase | Adds | Status |
|---|---|---|
| A | `Enricher` protocol, `EnricherContext`, `Principal` + normalizers | ✅ shipped |
| B | `PrincipalCache` ABC + `InProcessTTLCache` | pending |
| C | `EnricherPipeline` (async runner with timeout + error capture) | pending |
| D | CloudTrail enrichers: principal identity, history, write filter, object inspection | pending |
| E | Wire `CloudTrailRunbook` into the pipeline (orchestrator only) | pending |
| F | Metrics: cache hit rate, per-enricher duration, error counts | pending |

Each phase is independently shippable and revertable.

---

## See also

- [`docs/tests/enrichers/principal-testing-walkthrough.md`](../tests/enrichers/principal-testing-walkthrough.md) — how to test the Phase A surface
- [`logpose/enrichers/principal.py`](../../logpose/enrichers/principal.py) — `Principal` + provider normalizers
- [`logpose/enrichers/protocol.py`](../../logpose/enrichers/protocol.py) — `Enricher` Protocol
- [`logpose/enrichers/context.py`](../../logpose/enrichers/context.py) — `EnricherContext` dataclass
