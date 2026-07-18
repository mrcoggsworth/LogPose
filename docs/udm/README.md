# UDM Normalization

The `logpose.udm` package maps vendor-shaped `raw_payload` dicts onto a
Chronicle-style **Unified Data Model** (`UdmEvent`), so every alert leaving the
router carries a consistent, vendor-neutral view for N8N workflows and Splunk.
It is modeled on [Google Chronicle's UDM](https://cloud.google.com/chronicle/docs/event-processing/udm-overview)
— see [Models](../models/README.md#udmevent) for the field-level reference.

---

## Where normalization happens

Normalization lives in **one place**: the Router. After a route matches, the
router runs that route's mapper and attaches the result:

```python
alert = alert.model_copy(update={"udm": normalize_alert(alert, route.name)})
```

This ordering is deliberate:

- **Matching stays on `raw_payload`** — the existing fail-safe matchers are
  untouched, so the UDM layer can never cause a misroute.
- **The mapper is selected by route**, not by re-sniffing the payload — no
  duplicated detection logic, no chance of the mapper disagreeing with the
  matcher.
- **Consumers stay thin** — ingestion sources keep producing minimal `Alert`
  objects and know nothing about UDM.

Unrouted alerts are normalized with the generic mapper before they go to the
DLQ, so even dead-lettered events carry minimal UDM metadata.

---

## Package layout

| Module | Responsibility |
|--------|----------------|
| `normalize.py` | `MAPPERS` (route name → mapper fn) and the `normalize_alert()` dispatcher |
| `identity.py` | `Principal` + provider normalizers (`from_aws_user_identity`, `from_gcp_audit_authentication`, `from_ad_event`) and `Principal.to_udm_user()` |
| `mappers/aws_cloudtrail.py` | eventName verb → `event_type`; `userIdentity` → principal; `sourceIPAddress` → src (real IPs only); `requestParameters` → target resource; `errorCode` → security_result |
| `mappers/aws_guardduty.py` | Findings → `SCAN_UNCATEGORIZED` with severity bands (≥7 HIGH, ≥4 MEDIUM, else LOW) and the affected resource as target |
| `mappers/aws_eks.py` | k8s audit verb → `event_type`; `user.username` → principal; `objectRef` → target with namespace label; `responseStatus` → network |
| `mappers/gcp_event_audit.py` | `methodName` verb → `event_type`; `authenticationInfo` → principal; `resourceName` → target; `callerIp` → src |
| `mappers/generic.py` | Fallback: `GENERIC_EVENT` with ingestion timestamp and source |

---

## The fail-open guarantee

`normalize_alert()` **never raises**:

1. Route has a registered mapper → run it.
2. Mapper raises (malformed payload, missing identity fields) → log with
   traceback, fall back to the generic mapper.
3. Generic mapper somehow fails → return a bare `UdmEvent()`.

Mappers are therefore written *readably* rather than defensively — they may
raise freely on payloads that don't carry the fields they need, and the
dispatcher absorbs it. Normalization can slow an alert down; it can never lose
one or misroute one.

---

## Adding a mapper for a new route

1. Create `logpose/udm/mappers/<name>.py` exposing
   `map_to_udm(alert: Alert) -> UdmEvent`.
2. Register it in `logpose/udm/normalize.py`:

   ```python
   MAPPERS: dict[str, MapperFn] = {
       ...,
       "cloud.aws.securityhub": aws_securityhub.map_to_udm,
   }
   ```

3. Add a test module under `tests/unit/` asserting on `event_type`, principal,
   target, and (where relevant) `security_result` for a realistic payload.

Routes without a mapper are valid — they fall back to `GENERIC_EVENT`. The
test suite asserts every `MAPPERS` key corresponds to a registered route, so a
typo'd route name fails CI rather than silently never running.

### Guidelines

- Populate `metadata.product_event_type` with the vendor-native event name and
  `metadata.product_log_id` with the vendor's event ID — analysts pivot on
  these.
- Use `identity.py` for anything actor-shaped; it already collapses AWS
  assumed-role sessions to their role and namespaces GCP users vs. service
  accounts.
- Only put real IPs in `noun.ip` (CloudTrail's `sourceIPAddress` can be a
  service hostname — the CloudTrail mapper validates with `ipaddress` and uses
  `hostname` otherwise).
- Prefer a specific `event_type` over `GENERIC_EVENT`, but prefer
  `USER_RESOURCE_ACCESS` over guessing wrong.
- Never rename existing UDM fields or enum values — N8N workflows depend on
  the shape.
