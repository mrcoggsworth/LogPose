# CloudTrail Enrichers Testing Walkthrough

This document covers how to test the Phase D surface of the
`logpose.enrichers.cloud.aws.cloudtrail` package:

- `PrincipalIdentityEnricher` — pure logic
- `WriteCallFilterEnricher` — pure logic
- `PrincipalHistoryEnricher` — calls CloudTrail (mocked or `moto`-backed)
- `ObjectInspectionEnricher` — calls S3/IAM/EC2 (`moto`-backed)
- `CloudTrailEnrichment` — typed Pydantic schema for the enrichers' output

Phase D is **the enrichers, not the runbook**. Wiring into
`CloudTrailRunbook` lands in Phase E. Tests run with no Docker, no
RabbitMQ — just `pytest` and `moto`.

---

## Background: how the enrichers fit together

```
Stage 0: PrincipalIdentityEnricher
  reads alert.raw_payload['userIdentity'] → ctx.principal

Stage 1 (parallel):
  PrincipalHistoryEnricher
    cloudtrail.lookup_events(Username=...) → ctx.extracted['cloudtrail']['principal_recent_events']
    (cached per principal, namespace='history')
  WriteCallFilterEnricher
    parse each CloudTrailEvent JSON → ctx.extracted['cloudtrail']['successful_writes']
    keep only readOnly=False AND no errorCode

Stage 2: ObjectInspectionEnricher
  for each successful_write, dispatch by (eventSource, eventName) →
    S3 head_object / IAM get_user / IAM get_role / EC2 describe_instances
  → ctx.extracted['cloudtrail']['inspected_objects']
  (cached per resource, namespace='objects')
```

See [`docs/enrichers/README.md`](../../enrichers/README.md) for the
package overview, including the dispatch table for `ObjectInspectionEnricher`.

---

## Part 1: Unit-Testing the Pure-Logic Enrichers

`PrincipalIdentityEnricher` and `WriteCallFilterEnricher` make no
network calls and need no mocking — tests just construct an
`EnricherContext`, call `run`, and assert on the result.

### `tests/unit/enrichers/cloud/aws/cloudtrail/test_principal_identity.py`

7 tests. Highlights:

| Test | What it verifies |
|---|---|
| `test_extracts_iam_user_principal` | Standard IAMUser path: `ctx.principal.normalized_id` matches the canonical ARN; `display_name` is the userName |
| `test_extracts_assumed_role_principal` | AssumedRole prefers `sessionContext.sessionIssuer.arn` — sessions collapse to the underlying role |
| `test_extracts_aws_service_principal` | AWSService events build a `service:<invokedBy>` principal (no real ARN) |
| `test_missing_user_identity_records_precondition_error` | A payload without `userIdentity` records a `PreconditionError` rather than crashing |
| `test_invalid_user_identity_records_value_error` | Underlying `ValueError` from `from_aws_user_identity` is captured as a typed entry in `ctx.errors` |
| `test_run_never_raises_even_on_unexpected_input` | A pathological alert (synthetic via `__new__` + `object.__setattr__`) still results in a recorded error, never an exception |

### `tests/unit/enrichers/cloud/aws/cloudtrail/test_write_filter.py`

7 tests. Highlights:

| Test | What it verifies |
|---|---|
| `test_filters_to_successful_writes_only` | A mixed history of read calls, errored writes, and successful writes filters down to the successful writes only |
| `test_already_parsed_cloudtrail_event_is_handled` | Pre-parsed `CloudTrailEvent` dicts are accepted alongside raw JSON strings — both are valid input shapes |
| `test_unparseable_event_records_partial_parse_failure` | A bogus `CloudTrailEvent` body produces a `PartialParseFailure` entry without dropping the well-formed events |
| `test_missing_readonly_field_treated_as_not_write` | Defensive: an event with no `readOnly` field is not treated as a write (pessimistic default) |
| `test_run_never_raises_with_garbage_input` | Items of mixed types (`None`, `int`, `str`) in `principal_recent_events` are silently filtered, no raise |

### Running them

```sh
pytest tests/unit/enrichers/cloud/aws/cloudtrail/test_principal_identity.py \
       tests/unit/enrichers/cloud/aws/cloudtrail/test_write_filter.py -v
```

Both files run in well under 1 second.

---

## Part 2: Testing `PrincipalHistoryEnricher`

This enricher calls `cloudtrail.lookup_events`. Two complementary
strategies:

1. **`unittest.mock.MagicMock`** for behavioural tests — cache hit/miss,
   precondition checks, error capture. Fast (no moto setup).
2. **`@moto.mock_aws` + real `boto3.client('cloudtrail', ...)`** for the
   end-to-end boto3 path.

### `tests/unit/enrichers/cloud/aws/cloudtrail/test_principal_history.py`

10 tests:

| Test | Strategy | What it verifies |
|---|---|---|
| `test_invalid_construction_raises` | none | Non-positive `lookback_minutes` / `max_results` raise at construction |
| `test_missing_principal_records_precondition_error` | mock | Running without `ctx.principal` records a `PreconditionError` |
| `test_aws_service_principal_is_skipped` | mock | `service:` principals are skipped with a `PrincipalSkipped` note; `lookup_events` is NOT called |
| `test_root_account_is_skipped` | mock | Root principals are skipped likewise |
| `test_cache_miss_calls_lookup_events_and_caches_result` | mock | First call populates the cache; the response shape is preserved into `principal_recent_events` |
| `test_cache_hit_skips_api_call` | mock | A pre-seeded cache short-circuits `lookup_events` entirely |
| `test_lookup_events_raise_records_error_does_not_raise` | mock | A boto3 `RuntimeError` becomes a typed entry in `ctx.errors`; an empty list is written so downstream enrichers still find the key |
| `test_real_boto3_call_against_moto_fails_gracefully` | moto | A real boto3 client against `moto`'s CloudTrail (which doesn't implement `lookup_events`) raises `NotImplementedError` — the enricher captures it, writes an empty list, and importantly does **not** cache the failure |

### Why moto's missing implementation is useful

`moto` 5.x does not implement `cloudtrail.lookup_events` (as of writing).
Rather than skipping the boto3 path entirely, the test
`test_real_boto3_call_against_moto_fails_gracefully` exercises **the
exact failure mode that production will hit on transient AWS errors**:
the boto3 client raises, the enricher catches, errors are appended,
the cache stays clean. That's a more valuable assertion than a
contrived happy-path mock.

If `moto` adds CloudTrail support in the future, swap this test for a
positive happy-path assertion and keep a separate test that injects a
boto3 client raising `botocore.exceptions.ClientError` to cover the
failure path.

---

## Part 3: Testing `ObjectInspectionEnricher` against `moto`

This enricher integrates with three boto3 clients (S3, IAM, EC2).
`moto`'s `mock_aws` decorator gives every test its own in-memory AWS
account; tests seed the resources they expect to inspect, build a
`successful_writes` list referencing those resources, and assert on
`ctx.extracted["cloudtrail"]["inspected_objects"]`.

### `tests/unit/enrichers/cloud/aws/cloudtrail/test_object_inspection.py`

10 tests:

| Test | What it verifies |
|---|---|
| `test_unknown_event_silently_skipped` | An event with no registered handler is skipped without an error and without touching any client |
| `test_no_writes_produces_empty_inspected_objects` | Running with no `successful_writes` produces an empty `inspected_objects` and no error |
| `test_s3_event_without_bucket_or_key_silently_skipped` | A handler whose required params are absent silently skips that one event |
| `test_inspects_s3_object_against_moto` | End-to-end: bucket+object created in `moto`, PutObject event inspected, response has `ContentLength` and no `ResponseMetadata` (stripped) |
| `test_inspects_iam_user_against_moto` | End-to-end: IAM user created, CreateUser event inspected, response has the user's ARN |
| `test_inspects_iam_role_against_moto` | End-to-end equivalent for IAM roles |
| `test_inspects_ec2_run_instances_against_moto` | RunInstances response includes `responseElements.instancesSet.items[*].instanceId`; the enricher looks those instances up |
| `test_iam_get_user_failure_records_error_continues_on_others` | When inspecting two users in one batch and one is missing, the error is recorded for the missing one and the other still completes — partial success is normal |
| `test_repeat_inspections_hit_the_cache` | Running the same event twice produces one cache miss + one cache hit; both `ctx`s see the same payload |

### Adding a new dispatch case

When you wire up a new event source (e.g. `lambda.amazonaws.com`,
`UpdateFunctionConfiguration`):

1. Add a handler `_inspect_lambda_function` in
   `logpose/enrichers/cloud/aws/cloudtrail/object_inspection.py` that
   returns `list[(cache_key, payload)]`.
2. Add a row to `_dispatch` matching the new `(eventSource, eventName)`.
3. Add a moto-backed test that creates the Lambda function, builds a
   `successful_writes` event referencing it, and asserts the new
   `inspected_objects` key.

Always include both:

- A happy-path assertion (resource present, payload shape correct).
- A failure-path assertion (resource missing → error recorded, no
  raise, sibling events unaffected).

---

## Part 4: Verification loop

```sh
# 1. Tests
pytest tests/unit/enrichers/cloud/ -v

# 2. Type checking
python -m mypy logpose/enrichers/

# 3. Format check
python -m black --check logpose/enrichers/ tests/unit/enrichers/

# 4. Lint
python -m flake8 logpose/enrichers/ tests/unit/enrichers/
```

All four must be clean. The full unit suite (`pytest tests/unit/`)
should also stay green — Phase D is purely additive.

---

## Why no integration test in Phase D?

The enrichers are tested in isolation. The first test that actually
runs them through `CloudTrailRunbook.enrich()` end-to-end lands in
Phase E
(`tests/integration/test_cloudtrail_runbook_pipeline.py`). That test
will exercise the runbook's RabbitMQ wiring, the orchestrator's basic
field extraction, and the full enricher pipeline against a `moto`
account — all of which are out of scope here.

---

## Relevant Files

| File | Purpose |
|---|---|
| [`logpose/enrichers/cloud/aws/cloudtrail/schema.py`](../../../logpose/enrichers/cloud/aws/cloudtrail/schema.py) | `CloudTrailEnrichment` Pydantic model |
| [`logpose/enrichers/cloud/aws/cloudtrail/principal_identity.py`](../../../logpose/enrichers/cloud/aws/cloudtrail/principal_identity.py) | Sets `ctx.principal` from the alert's `userIdentity` |
| [`logpose/enrichers/cloud/aws/cloudtrail/principal_history.py`](../../../logpose/enrichers/cloud/aws/cloudtrail/principal_history.py) | CloudTrail `LookupEvents` per principal, cached |
| [`logpose/enrichers/cloud/aws/cloudtrail/write_filter.py`](../../../logpose/enrichers/cloud/aws/cloudtrail/write_filter.py) | Filters principal history to successful writes |
| [`logpose/enrichers/cloud/aws/cloudtrail/object_inspection.py`](../../../logpose/enrichers/cloud/aws/cloudtrail/object_inspection.py) | Dispatches by `(eventSource, eventName)` to S3/IAM/EC2 describes |
| [`tests/unit/enrichers/cloud/aws/cloudtrail/`](../../../tests/unit/enrichers/cloud/aws/cloudtrail) | All Phase D tests (one file per enricher) |
| [`requirements-dev.txt`](../../../requirements-dev.txt) | `moto[cloudtrail,ec2,iam,s3]` added by Phase D |
