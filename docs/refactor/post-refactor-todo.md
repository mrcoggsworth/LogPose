# Post-Refactor TODO — Items Outside the Automated Refactor's Reach

This is the follow-up punch list for the N8N + UDM refactor (PR #2, plan in
[n8n-udm-refactor-plan.md](n8n-udm-refactor-plan.md)). Everything in this
document is something the automated session **could not do** — either because
tool permissions blocked the file, because the resource lives outside the git
repository (broker state, cluster manifests, N8N, Splunk), or because it was a
judgment call that belongs to the project owner.

Each item states **what** to do, **why** it matters (so you don't have to
re-derive the reasoning from the code), **how** (concrete commands/content),
and **done-when** (so you can check it off without second-guessing).

Ordered by priority: items 1–5 block a working deployment; 6–8 block a clean
operational picture; 9–13 are housekeeping.

---

## 1. Update `.env.example` with the workflow-worker variables

**Status:** Blocked — the session's permission settings denied both reading and
writing `.env.example`, so it was never seen or touched. It almost certainly
still documents the runbook-era environment.

**Why:** `.env.example` is the template developers copy to `.env` for local
runs. The new worker entry point (`logpose/workflows/worker_main.py`) does
`os.environ["LOGPOSE_ROUTE"]` and `os.environ["N8N_WEBHOOK_URL"]` — it
**crashes at startup** if they're unset. Anyone bootstrapping from the example
file will hit a `KeyError` with no hint of what the new variables are.

**How:** Open `.env.example`, remove any runbook-specific entries (anything
referencing `logpose.runbooks` or enricher knobs — `LOGPOSE_ENRICHER_TOTAL_BUDGET_SECONDS`
and `LOGPOSE_CACHE_STATS_INTERVAL` are gone from the code), and add:

```sh
# --- Workflow worker (one worker process per route; Phase II enrichment) ---
# Route this worker serves. Must be a registered route name:
#   cloud.aws.cloudtrail | cloud.aws.guardduty | cloud.aws.eks
#   | cloud.gcp.event_audit | test
LOGPOSE_ROUTE=test

# The route's N8N webhook URL. For local dev, point at the mock:
#   python docker/n8n_workflow_mock.py   (serves on :5678)
N8N_WEBHOOK_URL=http://localhost:5678/webhook/test

# Optional static auth header sent on every webhook call
# (configure the matching Header Auth credential on the N8N Webhook node)
#N8N_AUTH_HEADER_NAME=Authorization
#N8N_AUTH_HEADER_VALUE=Bearer change-me

# Optional tuning (defaults shown)
#N8N_TIMEOUT_SECONDS=30
#N8N_MAX_ATTEMPTS=3
#N8N_RETRY_BACKOFF_SECONDS=2
```

**Done when:** a fresh clone + `cp .env.example .env` +
`python -m logpose.workflows.worker_main` starts (or fails only on RabbitMQ
being down, not on missing variables).

---

## 2. Update `CLAUDE.md` Phase II to describe the new architecture

**Status:** Deliberately left alone — it's your project-instruction file and
describes your original phase plan ("route … to the correct runbook as code",
"Each runbook as code will run as a separate pod"). Rewriting your own charter
document felt like an owner decision, not a refactor step.

**Why:** `CLAUDE.md` is loaded into every future Claude Code session as
authoritative project context. As written, it instructs future sessions that
Phase II is runbooks-as-code inside this repo. A future session taking it at
face value could "helpfully" rebuild the runbooks package or route new work the
old way.

**How:** In the Phase II section, replace the runbook language with the current
reality, e.g.:

> Route registered events from RabbitMQ to the correct **N8N workflow**. Each
> route has a dedicated workflow-worker pod (`logpose/workflows/worker_main.py`,
> configured via `LOGPOSE_ROUTE` + `N8N_WEBHOOK_URL`) that POSTs the UDM-shaped
> alert to the route's N8N webhook and publishes the JSON response to the
> `enriched` queue. Enrichment logic lives in N8N, not in this repository.
> Alerts are normalized to a Chronicle-style UDM view
> (`logpose/models/udm.py`, mappers in `logpose/udm/`) by the router after
> route matching. Failed invocations go to `alerts.dlq`
> (`workflow_failed` / `workflow_bad_response`).

Also worth adding under "Things Claude Should NOT Do": *"Don't add enrichment
logic to this repo — enrichment belongs in N8N workflows."*

**Done when:** `grep -i runbook CLAUDE.md` returns nothing that describes the
*current* architecture (historical narrative is fine if labeled as such).

---

## 3. Deployed RabbitMQ brokers: delete the orphaned `runbook.*` queues

**Status:** Out of reach — broker state lives on your OpenShift/compose
environments, not in git.

**Why:** The queue constants were renamed (`runbook.cloudtrail` →
`workflow.cloudtrail`, etc.). Queues are **durable**, so any broker that ever
ran the old code still has the old queues. Two consequences if you skip this:
(a) any messages still sitting in `runbook.*` will never be consumed by
anything again — silent alert loss from before the cutover; (b) the dashboard
and Management UI will show ghost queues forever, which will confuse every
future debugging session.

**How:** After deploying the new code (order matters — drain first, then
delete):

```sh
# 1. Check for stranded messages (Management API, or the UI at :15672)
curl -su guest:guest http://localhost:15672/api/queues | \
  python3 -c "import json,sys; [print(q['name'], q['messages']) for q in json.load(sys.stdin) if q['name'].startswith('runbook.')]"

# 2. If any old queue has messages, decide: replay them by re-publishing to the
#    new workflow.* queue name (same Alert JSON — the worker will still accept
#    an alert without a udm section), or accept the loss (pre-prod).

# 3. Delete each old queue
for q in runbook.cloudtrail runbook.guardduty runbook.eks runbook.gcp.event_audit runbook.test; do
  curl -su guest:guest -X DELETE "http://localhost:15672/api/queues/%2F/$q"
done
```

**Done when:** the Management UI lists only `alerts`, `alerts.dlq`, `enriched`,
`logpose.metrics`, and the five `workflow.*` queues.

---

## 4. OpenShift: replace runbook Deployments with workflow-worker Deployments

**Status:** Out of reach — your cluster manifests are not checked into this
repo, so only the compose file could be updated as a reference.

**Why:** The old Deployments run `python -m logpose.runbooks.cloud.aws` etc. —
those modules **no longer exist in the image**, so after you build/deploy the
new image those pods will CrashLoopBackOff. The replacement model is: one
Deployment per route, all using the *same* command, differentiated only by env.
This is why the refactor kept one-pod-per-route — your isolation story on
OpenShift is preserved, just with configuration instead of per-route code.

**How:** For each route, a Deployment shaped like this (CloudTrail shown;
`docker/docker-compose.yml` has the equivalent compose services as reference):

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: logpose-worker-cloudtrail
spec:
  replicas: 1            # scale horizontally per route as volume demands
  template:
    spec:
      containers:
        - name: worker
          image: <your-registry>/logpose/logpose:latest
          command: ["python", "-m", "logpose.workflows.worker_main"]
          env:
            - name: LOGPOSE_ROUTE
              value: cloud.aws.cloudtrail
            - name: N8N_WEBHOOK_URL
              value: https://n8n.example.com/webhook/cloudtrail
          envFrom:
            - secretRef: {name: logpose-rabbitmq}   # RABBITMQ_URL
            - secretRef: {name: logpose-n8n}        # auth header (item 5)
```

Delete the old `logpose-runbook-*` Deployments. Note the AWS credentials
secret is **no longer needed by Phase II pods** — boto3 is only used by the
SQS consumer now; the workers make no AWS calls. Rebuild/push the image first
(`make build-push HOST=<registry>`).

**Done when:** `oc get deploy` shows `logpose-worker-<route>` per route, no
`logpose-runbook-*`, and each worker pod logs
`WorkflowWorker '<route>' started, consuming from queue='workflow.<route>'`.

---

## 5. Stand up N8N and build the actual workflows

**Status:** Out of reach — N8N is an external system; the repo only ships the
demo mock (`docker/n8n_workflow_mock.py`).

**Why:** All enrichment now happens in N8N. Until a real workflow exists for a
route, that route's worker will either DLQ everything (webhook 404s →
`workflow_failed`, no retries since 404 is 4xx) or, against the mock, produce
trivially-enriched events. **Important:** the deleted CloudTrail enricher logic
(principal identity/history via CloudTrail lookups, write-call filtering,
S3/IAM/EC2 object inspection with boto3, TTL principal cache) exists only in
git history now — recover it from the commit before
`refactor: replace runbooks with N8N workflow workers and add UDM normalization`
(`git log --diff-filter=D -- logpose/enrichers/` shows the deletion) if you
want to rebuild it as N8N HTTP-request/code nodes.

**How, per route:**

1. Create a workflow with a **Webhook** node: method POST, path per route
   (e.g. `/webhook/cloudtrail`), *Respond: Using 'Respond to Webhook' node*.
2. Add enrichment nodes. The incoming JSON is the full `Alert` **including
   `udm`** — prefer reading `udm.principal.user.userid`,
   `udm.metadata.event_type`, etc. over vendor-specific `raw_payload` paths, so
   workflows stay portable across sources.
3. End with a **Respond to Webhook** node returning the contract
   (`docs/workflows/README.md` has the full table):

   ```json
   {
     "extracted": { "your": "fields" },
     "udm": { "...optional richer UDM to replace the router's..." },
     "destination": "splunk",
     "error": null
   }
   ```

4. Enable **Header Auth** on the webhook and mirror it in the worker env
   (`N8N_AUTH_HEADER_NAME` / `N8N_AUTH_HEADER_VALUE`, from the `logpose-n8n`
   secret). Why: the worker POSTs full alert payloads — an unauthenticated
   webhook would let anyone on the network inject or read alert-shaped traffic.
5. Keep median workflow runtime well under `N8N_TIMEOUT_SECONDS` (30s default).
   Workers process one alert at a time (prefetch=1), so workflow latency is
   the pipeline's throughput ceiling per replica — scale worker replicas if a
   workflow is inherently slow.

**Done when:** posting a test alert (`_logpose_test: true` via the universal
consumer's `POST /ingest`) lands in Splunk with `extracted` populated by your
real N8N workflow, and an alert POSTed to the webhook without the auth header
is rejected by N8N.

---

## 6. Splunk: update saved searches/dashboards for the renamed fields

**Status:** Out of reach — Splunk content lives in your Splunk instance.

**Why:** The HEC `sourcetype`s did **not** change (`logpose:enriched_alert`,
`logpose:dlq_alert`), but the event JSON inside them did. Any saved search,
dashboard panel, or alert in Splunk referencing the old field names silently
returns zero results after cutover — the worst kind of breakage for a SOC.

**What changed in the event schema:**

| Old | New | Notes |
|-----|-----|-------|
| `runbook` | `workflow` | same dot-path values (`cloud.aws.cloudtrail`, …) |
| `runbook_error` | `workflow_error` | now also set from a workflow's `error` response field |
| — | `alert.udm.*` | new: full UDM section (`alert.udm.metadata.event_type`, `alert.udm.principal.user.userid`, `alert.udm.security_result{}.severity`, …) |
| `dlq_reason` values | + `workflow_failed`, `workflow_bad_response` | DLQ events; `no_route_matched` / `publish_failed` unchanged |
| `extracted.*` | schema now defined by each N8N workflow | old enricher keys (`extracted.cloudtrail.*`, `extracted.principal.*`, `extracted.enricher_errors`) no longer produced |

**How:** `grep` your Splunk app/savedsearches.conf (or search in the UI) for
`runbook` and `enricher`, swap per the table. Consider new searches keyed on
UDM — e.g. `sourcetype=logpose:enriched_alert alert.udm.metadata.event_type=USER_LOGIN`
— that's the payoff of the UDM work: one search shape across AWS/GCP/EKS.

**Done when:** no Splunk knowledge object references `runbook*` fields, and a
smoke-test alert appears in your dashboards post-cutover.

---

## 7. Run the integration suite against the compose stack

**Status:** Not run — the refactor session had no Docker daemon, so only the
224 unit tests were executed. The integration tests were *updated* for the new
architecture but have never been executed.

**Why:** The integration tests are the only automated proof of the real
wiring: router → UDM attach → `workflow.*` queue → worker → (stub N8N HTTP
server) → `enriched` → forwarder. The new
`test_workflow_worker_invokes_n8n_and_publishes_to_enriched_queue` test in
`tests/integration/test_routing_flow.py` spins up a local `http.server` as the
N8N stand-in, so no N8N install is needed.

**How:**

```sh
docker compose -f docker/docker-compose.yml up -d
pytest tests/integration/ -v -m integration
```

Heads-up on a pre-existing environment quirk (not introduced by the refactor):
on Debian/Ubuntu-patched setuptools, `pip install splunk-sdk` fails with
`AttributeError: install_layout`. Workaround that worked in the session:

```sh
SETUPTOOLS_USE_DISTUTILS=local pip install --use-pep517 splunk-sdk
```

**Done when:** `pytest tests/integration -m integration` is green, and the
compose demo round-trip works (`curl POST :8090/ingest` with a CloudTrail
payload → enriched event visible in `docker logs logpose-splunk-hec-mock`).

---

## 8. Add a `LICENSE` file (or fix the README badge)

**Status:** Pre-existing gap, flagged during the docs pass — creating a
license file is a legal decision, so it was left to you.

**Why:** The README carries an MIT badge linking to `LICENSE`, and
`pyproject.toml` declares `license = {text = "MIT"}` — but no `LICENSE` file
exists. The badge link 404s, and legally the repo is "all rights reserved"
until license text actually ships, regardless of the badge.

**How:** Add the standard MIT text as `/LICENSE` with your copyright line
(GitHub → *Add file → Choose a license template* does this in one click), or
remove the badge if you don't intend MIT.

**Done when:** the badge link resolves.

---

## 9. Reconcile `pyproject.toml` dependencies with `requirements.txt`

**Status:** Pre-existing inconsistency, noticed but not changed — packaging
metadata affects how the project installs everywhere, so it was out of scope
for the refactor commit.

**Why:** `pyproject.toml` `[project].dependencies` lists **`kafka-python`**,
but the code imports **`confluent_kafka`**; it's missing `requests` (now
load-bearing — the N8N client is built on it), `splunk-sdk`, `fastapi`, and
`uvicorn`; and it lists `pytest`/`pytest-asyncio` as *runtime* deps. Anyone who
does `pip install .` (instead of `-r requirements.txt`) gets a broken install
with the wrong Kafka client. The Dockerfile presumably uses requirements.txt,
which is why this never bit in containers.

**How:** Replace the dependencies block with (versions pinned to taste):

```toml
dependencies = [
    "pika>=1.3.1",
    "confluent-kafka>=2.3",
    "boto3>=1.28.0",
    "google-cloud-pubsub>=2.18.0",
    "pydantic>=2.0.0",
    "python-dotenv>=1.0.0",
    "requests>=2.31",
    "splunk-sdk>=1.7",
    "fastapi>=0.111",
    "uvicorn[standard]>=0.29",
]
```

and move `pytest`, `pytest-asyncio` into `[project.optional-dependencies].dev`.

**Done when:** `pip install .` in a clean venv can run
`python -c "import logpose.workflows.worker_main"`.

---

## 10. Clean up stale generated artifacts: `graphify-out/`, `debug-logs/`

**Status:** Untouched — they're generated outputs, and deleting things the
refactor didn't create felt like owner's-choice housekeeping.

**Why:** `graphify-out/` (graph.json/graph.html/GRAPH_REPORT.md) is a code
graph generated **before** the refactor — it maps the deleted
`logpose/enrichers/` and `logpose/runbooks/` packages, and it even contains a
`needs_update` marker file. Anyone (human or Claude session) using it to
navigate the codebase will be navigating a codebase that no longer exists.
`debug-logs/sqs-consumer.json` is a captured debug artifact with no docs
referencing it.

**How:** Either regenerate the graph with your graphify tooling against the
new tree, or delete both directories and gitignore them:

```sh
git rm -r graphify-out debug-logs
printf "graphify-out/\ndebug-logs/\n" >> .gitignore
```

**Done when:** the repo contains no generated artifacts describing pre-refactor
code.

---

## 11. Decide the fate of `tls.crt` at the repo root

**Status:** Untouched — couldn't determine what consumes it (nothing in the
Python code or compose file references it), and removing an unexplained cert
from someone's repo is not a call an automated refactor should make.

**Why:** A certificate checked into git at the repo root is either (a) dead
weight from an experiment, or (b) a real deployment artifact that belongs in an
OpenShift Secret / cert-manager, not source control. Even a public cert in the
repo rots silently at expiry. (If a private key was *ever* committed alongside
it, rotate — git history remembers.)

**How:** `git log --follow tls.crt` to see where it came from;
`openssl x509 -in tls.crt -noout -subject -dates` to see what it is and when it
expires. Then delete it, or move it to the secret store your Route/Ingress
actually reads from.

**Done when:** the repo root has no certificate, or a comment/README note
explains exactly what consumes it.

---

## 12. Optional: fix the black/flake8 line-length mismatch

**Status:** Pre-existing, deliberately not fixed — reformatting ~40 untouched
files would have drowned the PR #2 diff in noise.

**Why:** `pyproject.toml` sets black/ruff to `line-length = 100`, but `.flake8`
sets `max-line-length = 88`. Black at 100 *produces* lines flake8 rejects, so
"format then lint" can never be fully green — the repo carried 28 flake8
violations before the refactor for exactly this reason. Your CLAUDE.md
verification loop (black → flake8 → tests) will always show noise until the
two agree.

**How:** Pick one number. If 88: change `line-length = 88` in both
`[tool.black]` and `[tool.ruff]`, run `black .`, commit the reformat as a
standalone no-logic-change commit. If 100: change `.flake8` to
`max-line-length = 100`. Do it in a dedicated PR, not mixed with feature work.

**Done when:** `black --check . && flake8` exits 0.

---

## 13. Optional: write the missing Kafka/Pub-Sub consumer unit tests

**Status:** Pre-existing gap discovered during the docs link-check — the
consumer walkthroughs claimed `tests/unit/test_kafka_consumer.py` and
`tests/unit/test_pubsub_consumer.py` exist; they never did. The docs now say
"*(planned)*" instead of dead-linking.

**Why:** Kafka and Pub/Sub consumers are the only pipeline components with no
unit coverage (SQS, universal, and Splunk ES consumers all have test modules).
Their `_handle_message` decode/normalize paths are exactly where malformed
producer payloads bite.

**How:** Mirror `tests/unit/test_sqs_consumer.py`'s structure: mock
`confluent_kafka.Consumer` / `pubsub_v1.SubscriberClient`, feed valid JSON,
invalid JSON, and null-body messages, assert an `Alert` is produced (with
correct `source` and `metadata`) or the message is skipped with a log — never
an exception. Then flip the "(planned)" notes in
`docs/tests/consumers/*-walkthrough.md` back into links.

**Done when:** both test files exist and the walkthrough links resolve again.
