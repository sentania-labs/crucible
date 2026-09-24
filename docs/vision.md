# Hades Roadmap and Bootstrap Directive

## Current Crucible Status Snapshot

As of **September 23, 2026 around 10:20 AM Central**, `sentania-labs/crucible` had advanced substantially beyond its top-level README.

- `main` was at commit `48649c1` from PR #90.
- The newest `main` CI run had just started and was still in progress at the time of review; the preceding `main` CI run was green.
- The newest published release was **v0.5.2**.
- Both the service image and worker image were published to GHCR.
- There were **no open pull requests**.
- There were **40 open issues**.
- The README still described the implementation as **C7a**, while the repository had implementation notes through **C14**.
- C11 consolidated Claude Code, Codex, AGY, and Hermes into one worker image.
- C12 consolidated the client/CLI surface.
- Kubernetes deployment support existed and had been exercised against the real lab.
- The real Foundry bootstrap-ledger authority handoff had still not been performed.

### Open issues that matter most for the next milestone

Not all open issues should block progress. The most relevant are:

- **#91** — Cilium/kube-proxy-replacement breaks worker DNS and local LiteLLM access. This is a real Kubernetes/Spark blocker.
- **#93** — worker/canary resource requests are too large for the small lab cluster. Real Kubernetes blocker.
- **#95** — PID readiness canary is measuring the wrong cgroup and can report a false safe state.
- **#92** — Kubernetes credential login is documented but not implemented. This can be worked around by manually provisioning/sealing credentials if necessary.
- **#89** — empty Hermes credential Secret handling. Not blocking once the real key exists.
- **#94** — NFS recursive ownership changes on mounts. Important if applicable to the deployed storage classes.
- **#85** — per-attempt test-service sidecars plus branch CI. This becomes important when Hades-on-Kubernetes is expected to build Hades itself.
- **#96** — release `latest` correctness on re-runs. Explicitly not a deploy blocker, but worth resolving before the first Hades-era release.
- **#42** — Codex credential refresh compatibility remains unproven. Relevant if all subscription-backed harnesses must be proven in the first milestone.

---

# Hades Product Direction

## Core idea

**Hades is a personal agent control center.**

It provides:

- a persistent principal/orchestrator agent for brainstorming, scoping, and delegation;
- curated specialized agents;
- scheduling for recurring agent work;
- lightweight work tracking;
- a focused daily view of what needs the operator's attention;
- deterministic execution through the existing Crucible capabilities;
- optional tools and transports such as Coppermind, Chronicle, GitHub, Discord, and others.

The existing Crucible implementation becomes Hades' proven execution subsystem rather than the complete product.

A useful philosophy for the project is:

> **Keep promises. Stay in your lane. Get shit done.**

That means agents have explicit roles and tools, external services retain their own authority, and Hades coordinates rather than absorbing everything.

---

# Product Shape

```text
Hades
Personal Agent Control Center

├── principal/orchestrator
├── agent definitions
├── scheduler
├── work tracking
├── daily experience
├── web/chat
│
└── execution
      current Crucible implementation
      ├── workers
      ├── harnesses
      ├── evidence
      ├── GitHub delivery
      └── Docker/Kubernetes providers

External tools:
├── Coppermind
├── Chronicle
├── GitHub
├── Discord
└── future tools
```

## Naming rule

Rebrand the **product and repository** as Hades, but do **not** immediately perform a flag-day rename of stable Crucible implementation identifiers.

Existing CLI/API/environment/database/image names may remain as compatibility names through the bootstrap milestone.

Treat the current deterministic supervisor as the **execution service/subsystem**.

A later decision can determine whether that internal service:

- continues to be called Crucible,
- becomes simply `execution`,
- or is fully renamed.

Keep service/component names descriptive. Avoid giving every internal component another mythology name.

---

# Roadmap

## M0 — Hades Bootstrap

This is the transition from "Crucible project" to "Hades project" without destabilizing the execution engine.

### Goals

- Rebrand the product/repository as Hades.
- Reset stale documentation around what the project actually is now.
- Describe the existing Crucible implementation as Hades' execution subsystem.
- Preserve current API/CLI/image compatibility initially.
- Triage the current issue backlog into:
  - **bootstrap blocker**
  - **execution hardening**
  - **later**
- Resolve enough of the real lab Kubernetes issues to make the selected deployment genuinely usable.
- Perform the real Foundry ledger handoff.

### Important near-term issue focus

For the Kubernetes path, pay particular attention to:

- #91
- #93
- #95

Resolve #96 before the next release.

Decide whether #92 is required now or whether manually provisioned credentials are acceptable during bootstrap.

### Real Foundry ledger handoff

Perform the actual authority transition:

- export the current Foundry ledger;
- submit/import it into Hades/Crucible;
- verify it;
- commit the import;
- mark the Foundry bootstrap ledger migrated/frozen.

### M0 completion condition

Hades has:

- a truthful README and roadmap;
- a stable execution service;
- a usable lab deployment;
- and the current Foundry can use Hades as its authoritative execution backend.

---

# M1 — Hades Builds Hades

This is the **first major milestone** and should take precedence over dashboard, scheduler, persistent chat, and agent-management UI work.

The existing Foundry remains the temporary human-facing orchestrator.

Hades runs as a real service through Docker Compose and/or Kubernetes.

Foundry uses Hades to implement Hades.

```text
Current Foundry
running locally
inside Claude/Codex/AGY
        │
        │ judgment / task contracts
        ▼
     Hades API
        │
        ▼
 execution service
        │
 ┌──────┼───────────┐
 │      │           │
Hermes Claude Code Codex/AGY
 │
LiteLLM
 │
Spark
```

## M1 end-to-end acceptance test

Give local Foundry a real Hades feature request.

Foundry must:

1. inspect Hades state/capabilities;
2. create a bounded task;
3. choose an appropriate worker class;
4. submit the task to Hades;
5. have Hades launch the worker;
6. execute in the supported isolated provider;
7. capture evidence;
8. apply completion gates;
9. perform independent review where policy requires it;
10. drive the verified branch/PR and CI lifecycle through the existing delivery path;
11. return sufficient evidence/state for Foundry to judge the result;
12. allow Foundry to accept, correct, or escalate.

Prefer proving:

- one **Hermes → Spark** path;
- and one subscription-backed path.

Do not let proving every harness block the self-hosting milestone.

## Issue #85 and self-hosting

Kubernetes workers cannot run all of the same heavy test tiers that host-based workers can.

Issue #85 proposes the right general shape:

```text
worker
  → unit/integration it can run
  → push branch
  → branch CI runs privileged/heavy tiers
  → Hades observes branch CI
  → only then continue toward PR
```

Use sidecar services for normal integration dependencies such as PostgreSQL.

Do **not** give workers a Docker socket just to make self-hosting easier.

### M1 completion condition

> **Foundry uses Hades to develop Hades.**

---

# M2 — Persistent Principal Agent

Replace the current "start Foundry inside a CLI harness" interaction model.

Build a persistent **principal/orchestrator service** inside Hades.

It owns conversation continuity and borrows a harness for reasoning.

```text
You
 │
 ▼
Hades principal
 │
 ├─ Claude Code
 ├─ Codex
 ├─ AGY
 └─ Hermes/Spark
```

A long brainstorming session belongs to Hades, not to the lifetime of one Claude Code process.

The first UI can be extremely simple:

- persistent chat;
- durable conversation history;
- delegation into Hades execution.

### M2 completion condition

You can close your desktop, return later/from another client, continue the same conversation, and tell the principal to delegate work through Hades.

---

# M3 — Agent Identity and Curation

Agent identity and curation are core product capabilities.

Initial examples:

- Principal / CEO / Chief of Staff
- Secretary
- Researcher
- Ghostwriter
- Engineer

Avoid building an elaborate HR or org-chart system.

An agent needs to answer:

- Who are you?
- What are you responsible for?
- What tools can you use?
- What skills/instructions do you follow?
- What type of reasoning backend should you normally use?
- Are you enabled?

A basic experience might look like:

```text
Agents

Principal        active
Secretary        active
Researcher       active
Ghostwriter      paused
Engineer         active

[Create agent]
```

Clicking an agent should allow the operator to:

- edit identity;
- edit role/responsibility;
- attach skills;
- attach tools;
- select routing preference;
- test the agent;
- enable/disable it.

The principal should eventually itself be represented as an agent definition, with special orchestration authority.

Examples:

- Ghostwriter → Chronicle tool + ghostwriting skill.
- Secretary → Coppermind-read + calendar tools.
- Engineer → Hades execution/Crucible capabilities.

---

# M4 — Work and Focus

Only after the self-hosting and agent foundation should Hades own a lightweight work model.

The goal is **not** to build a Jira clone.

The goal is:

> When Scott or an agent decides something deserves durable attention, Hades can remember and track it.

Cards may originate from:

- chat;
- scheduled agent work;
- GitHub issues;
- agent output;
- explicitly captured external events.

GitHub issues are sources/references, not Hades' work database.

The Kanban view is useful but secondary.

The primary experience should answer:

> **What needs me?**

Hades should distinguish between:

- real work commitments;
- things waiting on agents;
- optional lab/pet-project work;
- content ideas;
- deferred/someday items.

Lab experiments should not impersonate work obligations merely because both exist as cards.

### M4 completion condition

The system helps protect Scott's attention rather than creating another mixed dashboard that gets ignored.

---

# M5 — Scheduling and Daily Experience

Add recurring agent routines.

Examples:

- Researcher: investigate selected topics every morning.
- Ghostwriter: periodically review blog ideas.
- Secretary: summarize real work commitments each morning.
- Weekly activity summary.
- Recurring customer or project research.

Build the daily experience from **Hades state plus agent summaries**, not from a firehose of connected-service data.

A useful daily view may contain:

```text
Needs You

Work Focus

Agents Working / Waiting

Scheduled Today

If You Have Time
```

External systems should contribute concise signals, not noise.

For example, Coppermind may contribute:

> `7 notes awaiting review`

rather than displaying seven unrelated note titles.

### M5 completion condition

The dashboard becomes useful enough that Scott actually opens it.

---

# M6 — Tools and Transports

Add these when they support real flows rather than as architecture projects of their own.

## Chronicle tool

Examples:

- "Submit this as a blog idea."
- "Turn this into a Chronicle draft."

## Coppermind tool

Examples:

- "Save this brainstorming session to my review pile."
- "How many notes are waiting for review?"
- "Search my notes for context about this customer/project."

Coppermind remains Scott's personal knowledge system, not Hades' operational database.

## Discord transport

Talk to the same principal agent from a phone or remote location.

Discord is a transport into Hades, not a separate agent.

## GitHub tool

Use GitHub as a source/reference/work delivery system without making GitHub Issues the Hades work database.

---

# Explicitly Deferred

Do **not** make these prerequisites for the early Hades roadmap:

- elaborate org charts;
- multi-company support;
- full Paperclip clone;
- universal knowledge store;
- vector search;
- replacing Vault/n8n ingestion;
- migrating Chronicle into Hades;
- migrating Coppermind into Hades;
- fancy Discord bot before persistent chat works;
- perfect Kanban UI;
- clearing all existing Crucible technical debt;
- renaming every `crucible` symbol, secret, image, environment variable, or database object.

---

# Operator Directive for the Current Foundry

## Directive: Evolve Crucible into Hades

We are changing the scope of the current Crucible project.

**Hades** is the new product: a personal agent control center.

The current Crucible implementation becomes Hades' proven execution subsystem rather than the complete product.

The existing Foundry remains the bootstrap orchestrator during this transition.

Do **not** attempt to replace yourself immediately.

Your first responsibility is to get Hades to the point where you can use it as your authoritative execution backend to build the remaining Hades services.

---

## Product Intent

Hades should eventually provide:

1. a persistent principal/orchestrator agent Scott can converse with for brainstorming, scoping, and delegation;
2. curated agent definitions for specialists such as Secretary, Researcher, Ghostwriter, and Engineer;
3. scheduled/recurring agent activity;
4. lightweight Hades-owned work tracking independent of GitHub issues while allowing GitHub issues and other systems to be sources/references;
5. a focused daily experience showing what actually needs Scott's attention;
6. the existing deterministic execution capability currently called Crucible;
7. optional tools/transports such as Chronicle, Coppermind, GitHub, Discord, and others.

Hades is **not** intended to absorb Coppermind, Chronicle, Vault/n8n, or every other service.

Agents may use those systems through tools.

---

## Naming and Rebrand Rule

Rebrand the **product and repository** as Hades, but do not perform a flag-day rename of stable Crucible implementation identifiers.

Existing CLI/API/environment/database/image names may remain as compatibility names through the bootstrap milestone.

In particular, do not destabilize the newly proven release and worker-image paths merely to replace `crucible` with `hades`.

Treat the current deterministic supervisor as the **execution service/subsystem**.

A later decision can determine whether that internal service:

- continues to be called Crucible;
- becomes simply `execution`;
- or is fully renamed.

Keep service/component names descriptive.

Do not introduce additional mythology names for every subsystem.

---

## Immediate Repository Reconciliation

Before planning new product work:

- inspect current `main`;
- inspect releases;
- inspect readiness evidence;
- inspect all open issues;
- inspect current deployment documentation;
- reconcile stale operator-facing documentation.

As of the operator's September 23, 2026 review:

- current `main` had advanced through the C11/C12/C14-era work even though the README still claimed C7a;
- release v0.5.2 existed;
- the worker image contained Claude Code, Codex, AGY, and Hermes;
- the unified CLI had landed;
- Kubernetes deployment had been exercised against the real lab;
- there were 40 open issues and no open pull requests;
- the real Foundry bootstrap-ledger authority handoff remained unperformed.

Reconcile these statements against live GitHub before changing anything.

Update stale operator-facing documentation.

---

## Open-Issue Triage

Do not treat all open issues as blockers.

Categorize them as:

- **bootstrap blocker**
- **execution hardening**
- **later**

Pay particular attention to:

- #91
- #93
- #95
- #96
- #92
- #89
- #94
- #85

Codex issue #42 is required only if Codex daily-session compatibility is part of the selected bootstrap acceptance criteria.

---

# Milestone 0 — Bootstrap / Rebrand

Produce the minimum product/repository changes needed to establish Hades as the umbrella while preserving the proven execution service.

Update roadmap and product documentation.

Resolve only the execution/deployment issues necessary to make the selected lab deployment honest and usable.

Perform the real Foundry ledger export/import/handoff with explicit operator approval.

After the handoff:

- Hades' execution state is authoritative;
- Foundry's bootstrap ledger is frozen.

---

# Milestone 1 — Hades Builds Hades

This is the first major milestone.

It takes precedence over:

- dashboard;
- scheduler;
- persistent principal service;
- agent-management UI.

The existing Foundry must run locally as the orchestration/judgment agent while an MVP Hades deployment runs as a real service through Docker Compose and/or Kubernetes.

Foundry must use Hades APIs/CLI to delegate implementation work.

Prove this with a real change to Hades itself.

## End-to-End Acceptance Test

1. Scott gives Foundry a bounded Hades feature request.
2. Foundry scopes it and creates an execution contract.
3. Foundry submits it to Hades.
4. Hades selects/launches a supported worker.
5. The worker executes in the supported isolated provider.
6. Hades captures evidence and applies completion gates.
7. Independent review occurs where policy requires it.
8. The verified branch/PR and CI lifecycle are managed through the existing delivery path.
9. Hades returns sufficient evidence/state for Foundry to judge the result.
10. Foundry accepts, corrects, or escalates based on that evidence.

Prefer proving both:

- a local Hermes/Spark worker;
- at least one subscription-backed harness.

Do not let proving every harness block the self-hosting milestone.

If Kubernetes workers cannot satisfy Hades' own test gates because they lack Docker/testcontainers/kind, use issue #85's model:

- declared test sidecars for ordinary integration dependencies;
- branch CI for privileged/heavy pre-PR gates.

Do **not** grant workers a Docker socket merely to make self-hosting easier.

**Milestone 1 is complete when the operator can truthfully say:**

> **"Foundry uses Hades to develop Hades."**

---

# Post-Bootstrap Roadmap

After Milestone 1, develop in this order unless real usage demonstrates a better order:

## Persistent Principal Agent

Durable conversations, harness-backed reasoning, delegation through Hades.

## Agent Identity and Curation

Create/edit/test/enable/disable specialized agents with:

- identity;
- role;
- skills;
- tools;
- routing preferences.

## Work / Focus Tracking

Lightweight Hades cards with source/reference links and strong separation between:

- things requiring Scott's attention;
- things waiting on agents;
- optional lab/pet-project activity.

## Scheduling / Routines

Recurring work such as:

- daily research;
- secretary summaries;
- ghostwriting review;
- weekly activity summaries.

## Daily Experience

A useful:

- needs me;
- work focus;
- agents;
- optional work;

briefing rather than a raw activity dashboard.

## Tools / Transports

Chronicle, Coppermind, Discord, GitHub, and others as optional capabilities driven by actual workflows.

---

# Product Constraints

Preserve the existing Crucible principle that execution does not invent its own intent.

The principal/orchestrator and specialist agents may exercise judgment.

The execution subsystem remains deterministic where it is deterministic today.

Operational state has one authoritative owner.

Coppermind remains Scott's knowledge system.

Chronicle remains the publishing system.

Hades may use them through tools.

Do not make external integrations prerequisites for Hades to operate.

Favor one memorable product name and descriptive internal component names.

Most importantly:

> **Do not attempt to build the whole roadmap in the rebrand. Get to the self-hosting milestone first, then use Hades to build the rest.**

---

# Recommended Transition Sequence

Before renaming the repository:

1. let the newest `main` CI complete green;
2. resolve release issue #96;
3. optionally cut one final known-good Crucible release;
4. use that as the clean rollback point;
5. then begin the Hades repository/product transition.

That creates a clean historical boundary:

> **Crucible v0.x = proven execution MVP before Hades expansion.**

Hades development then begins from a known-good artifact.

The strategic goal is simple:

> **Do not build the control center entirely by hand if the purpose of the control center is to help build things. Finish the execution substrate, hand it to the current Foundry, and make Hades participate in its own construction.**
[[]]