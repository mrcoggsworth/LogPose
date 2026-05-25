# CLAUDE.md - Project Instructions for Claude Code

This file provides project-specific guidance for Claude Code. Update this file whenever Claude does something incorrectly so it learns not to repeat mistakes.

## Project Overview

The purpose of this project is to create a headless Security Orchestration Automation and Response platform utilizing modern infrastructure on OpenShift.

### Phase I. 
Create the ingestion stage. The soar platform will only be a consumer of kafka, aws sns, gcp pub/sub and possibly other related event driven notification systems that will send alerts into the soar-lite project (codename LogPose) for consumption. From here in phase 1 the platform will need to queue each alert into a self manageing queue that can live through pod failures or restarts. The queue system I would prefer to use is the Rebbit MQ system as the code seems to be more easily understandable over Redis and should perform at the level I am trying to achieve.
- Items I want to achieve before moving on to phase 2
  - Build out the ingestion side of the platform to intake from multiple subscription platforms
    - kafka
    - aws sns
    - gcp pub/sub
  - Be able to create a suitable test to see an incoming message and what it looks like and what possibilities are possible for creating a router solution in Phase 2.
  - I would like to be able to run tests prior to phase 2 execution planning so I can visually understand what is happening as my prior knowledge stems from building out backend api's without a queueing system.

### Phase II.
Create the routing stage of the project. Now that the ingestion part is done it is now time to route registered events in the queue that are consumed by different consumers (kafka, aws sqs, and gcp pub/sub) to the correct runbook as code. Each runbook as code will run as a separate pod so that it has segregation between different pieces of the headless soar project. This way if an error happens it can report it back to the router section which can then either reprocess the event or send it potentially to a dlq to process it later. Use rabbitmq to send to a dlq like area if possible. Once the data is enriched by the runbook it will need to send it back from the pod it was run in back to the routing infrastructure so that it can be sent out to its proper logging destination. We will stop there though as that will be apart of Phase IV. and we are chunking the project into separate phases.
- Items I want to achieve before moving on to phase 2
  - routing from rabbitmq queued events to proper runbooks
    - routes can have a parent route with sub routes
      - example: parent route could be "cloud" and a child route could be "aws" or "gcp" and each child route can be different runbooks
      - example: parent route could be "crowdstrike" and a child route could be "malware execution" or "confirmed downloaded malicious file"
      - these are not specific routes currently but could be in the future
  - routing code needs to be modular so that you can easily add more routes in the future for more use cases
  - routing needs to be simple enough for a junior developer to understand but sophisticated enough to handle efficient route handling and saftey to not send events to the incorrect location.
  - include a test route in the initial phase II to be able to use for tests as well as an operational example.
  - build out routes for cloud that lead to aws and gcp then route again to the logging type.
    - cloud -> aws -> cloudtrail
    - cloud -> aws -> guardduty
    - cloud -> aws -> eks (kubernetes)
    - cloud -> gcp -> event_audit
  - at least one very small test aws cloudtrail runbook and one very small gcp runbook to use as tests. Additional code for data enrichment will be added later.

### Phase III.
Now that we have an eriched data alert and we send it back to the main base, its time to ship out the enriched alert to a splunk index that ingests all the alerts for review.
- if the alert is enriched or not it needs to be able to forward to splunk
- the splunk event should be sent via the splunk sdk using industry best practices when it comes to sending the alerts.

### Phase IV. Agentic Platform Transformation
Transform the platform's processing logic into an orchestrated agentic stream using the **Google ADK (Agent Development Kit)** framework. The RabbitMQ queue backbone, Pydantic models, enricher logic, and Splunk client are all preserved — only the *orchestration layer* changes. Each discrete processing step (routing, enrichment, forwarding) becomes an ADK agent that can be reasoned about, composed, and extended independently.

- **OrchestratorAgent** (`SequentialAgent`): top-level entry point; consumes from RabbitMQ `alerts`, sequences the Router → Runbook → Forwarder agents.
- **RouterAgent** (`LlmAgent` or `SequentialAgent`): wraps `RouteRegistry` matcher functions as ADK tools; produces a routing decision (queue name or DLQ); falls back to rule-based routing when no LLM is configured.
- **RunbookAgents** (custom `BaseAgent` subclasses, one per runbook type): consume from runbook-specific queues; call enrichers via MCP tool calls; publish `EnrichedAlert` back to the enriched queue.
- **EnricherMCPServer**: a lightweight MCP server (`logpose/agents/mcp/enrichers.py`) that exposes each `Enricher.run()` as a named tool. Keeps enrichers stateless and independently testable.
- **ForwarderAgent** (custom agent): consumes from `enriched` queue; calls `send_to_splunk` or `send_to_universal` MCP tools; acks on success.

Items to achieve before moving to Phase V:
  - ADK project scaffold under `logpose/agents/` with clear separation between MCP servers and agents
  - OrchestratorAgent wires the full pipeline end-to-end using existing RabbitMQ queues
  - RouterAgent preserves all existing route matchers as tools (no regression in routing logic)
  - EnricherMCPServer exposes all four CloudTrail enrichers and the GCP enricher as MCP tools
  - ForwarderAgent replaces `EnrichedAlertForwarder` process
  - All existing unit and integration tests continue to pass; add agent-level integration tests
  - Local developer demo: `python -m logpose.agents.orchestrator` starts the full agentic pipeline


## Google ADK Framework

LogPose uses [Google ADK](https://google.github.io/adk-docs/) for agent orchestration starting in Phase IV. ADK is a Python framework for building multi-agent AI systems with composable agent types, MCP tool integration, and built-in session management.

### Agent Types Used

| ADK Type | LogPose Component | File |
|---|---|---|
| `SequentialAgent` | OrchestratorAgent (router → runbook → forwarder) | `logpose/agents/orchestrator.py` |
| `LlmAgent` or `SequentialAgent` | RouterAgent | `logpose/agents/router_agent.py` |
| Custom `BaseAgent` subclass | RunbookAgent (one per runbook type) | `logpose/agents/runbooks/` |
| MCP Server | EnricherMCPServer (enrichers as tools) | `logpose/agents/mcp/enrichers.py` |
| MCP Server | ForwarderMCPServer (Splunk/universal as tools) | `logpose/agents/mcp/forwarder.py` |

### Component Mapping (Current → ADK)

| Current Component | ADK Equivalent |
|---|---|
| `logpose/routing/router.py` Router | `RouterAgent` wrapping `RouteRegistry.match()` as a tool |
| `logpose/routing/registry.py` RouteRegistry | Unchanged; its `match()` is called by the RouterAgent tool |
| `logpose/routing/routes/**` route files | Unchanged matcher functions — wrapped as ADK tools |
| `logpose/runbooks/base.py` BaseRunbook | `BaseRunbookAgent` in `logpose/agents/runbooks/base.py` |
| `logpose/enrichers/**` Enricher classes | MCP tools in `logpose/agents/mcp/enrichers.py` |
| `logpose/forwarder/enriched_forwarder.py` | `ForwarderAgent` calling Splunk MCP tool |
| `logpose/forwarder/splunk_client.py` | Unchanged; wrapped as `send_to_splunk` MCP tool |
| `logpose/models/alert.py` | Unchanged — passed through ADK session state as JSON string |
| `logpose/models/enriched_alert.py` | Unchanged — returned by RunbookAgent |
| `logpose/queue/queues.py` | Unchanged — queue constants reused by all agents |
| `logpose/consumers/**` | Unchanged — consumer layer is not agentic; feeds RabbitMQ as before |

### Architecture Flow

```
Phase I-III (stays):
  Kafka/SQS/Pub/Sub/HTTP → Consumer → RabbitMQ alerts queue

Phase IV (new agentic layer):
  RabbitMQ alerts queue → OrchestratorAgent (SequentialAgent)
                              ├─ RouterAgent  →  RabbitMQ runbook queues
                              ├─ RunbookAgent →  EnricherMCPServer tools
                              │                  RabbitMQ enriched queue
                              └─ ForwarderAgent → ForwarderMCPServer tools
                                                  Splunk HEC
```

### MCP Server Pattern

Each MCP server in `logpose/agents/mcp/` is a stateless FastMCP server. Tools accept and return JSON-serializable dicts (Pydantic models are serialized at the boundary).

```python
# Example pattern — enricher as MCP tool
from mcp.server.fastmcp import FastMCP
from logpose.enrichers.cloud.aws.cloudtrail import PrincipalIdentityEnricher

mcp = FastMCP("logpose-enrichers")

@mcp.tool()
def principal_identity(payload: dict) -> dict:
    """Extract AWS principal identity from a CloudTrail payload."""
    # wrap EnricherContext, run enricher, return ctx.extracted
    ...
```

### Agent Coding Rules

- Every agent must handle its own errors — never propagate exceptions to the runner
- Tools must be pure functions: same input → same output, no shared mutable state
- MCP servers are stateless; boto3 clients are injected as constructor args (testable)
- RunbookAgents must publish to the appropriate RabbitMQ queue even on partial enrichment failure
- When routing fails, RouterAgent publishes to DLQ (same behavior as current Router)
- Agent session state carries the `Alert` as a JSON-serialized string; deserialize with `Alert.model_validate_json()`

### Directory Structure for Phase IV

```
logpose/
  agents/
    __init__.py
    orchestrator.py        # OrchestratorAgent (SequentialAgent) + entry point
    router_agent.py        # RouterAgent wrapping RouteRegistry
    runbooks/
      __init__.py
      base.py              # BaseRunbookAgent
      cloud/
        aws/
          cloudtrail.py    # CloudTrailRunbookAgent
        gcp/
          event_audit.py   # GcpEventAuditRunbookAgent
    mcp/
      __init__.py
      enrichers.py         # FastMCP server exposing all enrichers as tools
      forwarder.py         # FastMCP server: send_to_splunk, send_to_universal
```

### New Dependencies (Phase IV)

Add to `requirements.txt` and `pyproject.toml` when starting Phase IV:
```
google-adk>=0.5.0        # ADK core (agents, runners, sessions)
mcp>=1.0.0               # MCP protocol support
fastmcp>=0.1.0           # Lightweight MCP server builder
```


## Development Workflow

Give Claude verification loops for 2-3x quality improvement:

1. Make changes
2. Run type checking
3. Run tests
4. Lint before committing
5. Before creating PR: run full lint and test suite

## Code Style & Conventions

- Use descriptive variable names
- Keep functions small and focused
- Write tests for new functionality
- Handle errors explicitly, don't swallow them

## Commands Reference

```sh
# Verification loop commands
python -m mypy .        # Type checking
pytest                  # Run tests
flake8                  # Lint all files
black .                 # Format code

# Git workflow
git status              # Check current state
git diff                # Review changes before commit

# Phase IV — ADK agent commands
python -m logpose.agents.orchestrator      # Start full agentic pipeline
python -m logpose.agents.mcp.enrichers    # Start enricher MCP server standalone
python -m logpose.agents.mcp.forwarder    # Start forwarder MCP server standalone
pytest tests/agents/                       # Run agent-level tests
```

## Self-Improvement

After every correction or mistake, update this CLAUDE.md with a rule to prevent repeating it. Claude is good at writing rules for itself.

End corrections with: "Now update CLAUDE.md so you don't make that mistake again."

Keep iterating until the mistake rate measurably drops.

## Working with Plan Mode

- Start every complex task in plan mode (shift+tab to cycle)
- Pour energy into the plan so Claude can 1-shot the implementation
- When something goes sideways, switch back to plan mode and re-plan. Don't keep pushing.
- Use plan mode for verification steps too, not just for the build

## Parallel Work

- For tasks that need more compute, use subagents to work in parallel
- Offload individual tasks to subagents to keep the main context window clean and focused
- When working in parallel, only one agent should edit a given file at a time
- For fully parallel workstreams, use git worktrees:
  `git worktree add .claude/worktrees/<name> origin/main`

## Things Claude Should NOT Do

- Don't use dynamic/untyped variables in Python without explicit approval
- Don't skip error handling
- Don't commit without running tests first
- Don't make breaking API changes without discussion
- Don't bypass the existing RouteRegistry when building RouterAgent — wrap it, don't replace it
- Don't serialize Alert/EnrichedAlert with `dict()` — always use `.model_dump_json()` / `.model_validate_json()`
- Don't make ADK agents stateful across different alert invocations — each alert is a fresh session

## Project-Specific Patterns

- When adding a new enricher, register it as both an `Enricher` (existing protocol in `logpose/enrichers/protocol.py`) AND an MCP tool in `logpose/agents/mcp/enrichers.py`
- RouterAgent must call all existing `RouteRegistry` matcher functions — never bypass the registry for new routes
- RunbookAgents inherit from `BaseRunbookAgent` (`logpose/agents/runbooks/base.py`), not `BaseRunbook` directly; `BaseRunbookAgent` wraps the MCP tool calls
- Do not add LLM calls to the RouterAgent until the rule-based routing is confirmed working via agent integration tests
- Keep `logpose/routing/` and `logpose/runbooks/` intact — the agents layer wraps them, it does not replace them
- Queue name constants always come from `logpose/queue/queues.py` — never use bare string literals for queue names

---

_Update this file continuously. Every mistake Claude makes is a learning opportunity._
