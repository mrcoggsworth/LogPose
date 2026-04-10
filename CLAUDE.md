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


<!-- Expand more on the project and prompt claude only to build in sections -->

## Development Workflow

Give Claude verification loops for 2-3x quality improvement:

1. Make changes
2. Run type checking
3. Run tests
4. Lint before committing
5. Before creating PR: run full lint and test suite

## Code Style & Conventions

<!-- Customize these for your project's conventions -->

- Use descriptive variable names
- Keep functions small and focused
- Write tests for new functionality
- Handle errors explicitly, don't swallow them

## Commands Reference

```sh
# Verification loop commands (customize for your project)
python -m mypy .        # Type checking
pytest                  # Run tests
flake8                  # Lint all files
black .                 # Format code

# Git workflow
git status              # Check current state
git diff                # Review changes before commit
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

<!-- Add mistakes Claude makes so it learns -->

- Don't use dynamic/untyped variables in Python without explicit approval
- Don't skip error handling
- Don't commit without running tests first
- Don't make breaking API changes without discussion

## Project-Specific Patterns

<!-- Add patterns as they emerge from your codebase -->

---

_Update this file continuously. Every mistake Claude makes is a learning opportunity._
