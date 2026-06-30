# Calorie Agent

A Feishu-native calorie tracking Agent with low-friction logging, tool-grounded facts, and an evaluation-driven product loop.

`Calorie Agent` is a natural-language food logging backend for a Feishu bot. A user can send messages such as "chicken breast 50g" or "eggs 100g for breakfast, rice 200g for lunch, how much do I have left today?", and the system turns the message into tool actions for logging, querying, undoing, asking follow-up questions, and replying in Feishu.

It is not a general health app, and it does not let an LLM freely calculate calories.

It is closer to a verifiable AI Agent product pipeline: Feishu provides the low-friction entry point, AgentRuntime plans and orchestrates, backend tools and storage own factual reads and writes, and Reply Guard plus offline evals control AI risk.

<p align="center">
  <strong>Language</strong>:
  <a href="./README.md">简体中文</a> |
  <a href="./README.en.md">English</a>
</p>

## Quick Navigation

- [Overview](#overview)
- [Why It Exists](#why-it-exists)
- [Who It Is For](#who-it-is-for)
- [What It Can Do](#what-it-can-do)
- [Example Flow](#example-flow)
- [Core Capabilities](#core-capabilities)
- [How It Differs From Calorie Apps Or Chatbots](#how-it-differs-from-calorie-apps-or-chatbots)
- [Current Setup](#current-setup)
- [How It Works](#how-it-works)
- [Why It Is Credible](#why-it-is-credible)
- [Repository Structure](#repository-structure)
- [Boundaries](#boundaries)
- [License](#license)

## Overview

This repository contains the backend, Agent Runtime, tool layer, evaluation suites, deployment scripts, and product documentation for a Feishu food logging Agent.

The main goal is not to make a model answer nutrition questions directly. The goal is to split a high-frequency food logging workflow into an auditable, replayable, and testable system:

- `Feishu Webhook`: receives messages, sends a fast ACK, and reduces callback retry or cloud timeout risk.
- `AgentRuntime`: lets DeepSeek handle only understanding, planning, and response composition.
- `Tools`: perform logging, querying, undo, reporting, reminders, memory, and other backend actions.
- `Storage`: keeps Bitable compatibility for the legacy path, while V3 moves toward MySQL as the primary business database.
- `Reply Guard`: validates numbers, statuses, and permission-sensitive details in the final reply.
- `Eval`: uses offline regression and Agent behavior tests to cover the main path and high-risk exceptions.

The V3 Beta product stance is: Feishu is the entry point, MySQL is the primary storage direction, Bitable remains for compatibility and rollback, and DeepSeek does not own nutrition facts.

## Why It Exists

The hard part of food logging is usually not whether logging is possible. The hard part is whether users will keep logging.

Traditional calorie apps often require users to open an app or form, search for food, estimate grams, confirm nutrition data, and then return to their original workflow. For weight loss, fitness, glucose control, and everyday health management users, this creates too much friction in a high-frequency task.

`Calorie Agent` puts the entry point back into Feishu conversations and solves concrete product problems:

- Users can log food in natural language without switching to a standalone app.
- A compound message can become multiple tool actions, such as logging several meals and then querying today's remaining target.
- Missing grams, ambiguous foods, and unknown foods become pending follow-ups instead of model guesses.
- Feishu retries, cloud timeouts, and duplicate pushes are controlled through idempotency.
- Nutrition numbers come from food tables, intake snapshots, and backend calculations, not free-form model output.
- Product investment can be evaluated through usage, feedback, retention, and eval metrics.

## Who It Is For

This project is useful for:

- Product managers or builders validating a low-friction food logging experience.
- Engineers studying AI Agent tool use, state management, exception recovery, and eval gates.
- Teams connecting chat interfaces to real business data instead of demo-only conversations.
- Anyone who needs to understand Feishu bots, Tencent Cloud Functions, Bitable/MySQL, and LLM orchestration boundaries.

It is not a good fit for:

- Photo recognition, marketplace/community features, a full health app, or a commercial admin console.
- Medical diagnosis, treatment advice, or high-stakes medical conclusions.
- Designs where an LLM directly calculates nutrition, writes to storage, or bypasses the tool layer.
- Lightweight chatbots that do not need idempotency, permission isolation, exception recovery, or release gates.

## What It Can Do

Capabilities already represented in this repository include:

| Capability | Description | Key Material |
|---|---|---|
| Feishu text entry | Receives Feishu bot text events, returns accepted/queued quickly, and continues processing | [Tencent SCF README](backend/tencent_scf/README.md) |
| Natural-language food logging | Supports known foods, multi-food messages, compound messages, and today summaries | [Backend README](backend/README.md) |
| Query and undo | Supports today queries, stored intake nutrition queries, undo latest, and semantic undo | [Test cases](backend/docs/test-cases.md) |
| Missing-info follow-up | Missing grams, unknown foods, and low-confidence candidates enter pending flows | [Test cases](backend/docs/test-cases.md) |
| Agent tool orchestration | DeepSeek emits an action plan, while the backend executes only whitelisted tools | [Agent Runtime v2](backend/docs/agent-runtime-v2.md) |
| Fact-grounded reply validation | Reply Guard validates the final reply against execution facts | [Test cases](backend/docs/test-cases.md) |
| Multi-user and MySQL direction | V3 identity resolves tenant/user, and the MySQL repository provides the primary storage direction | [Feishu tenant](backend/docs/v3-feishu-tenant.md), [MySQL Schema](backend/docs/v3-mysql-schema.md) |
| Product metrics loop | Usage, feedback, activation, retention, and P0/P1/P2 prioritization rules | [Product validation](backend/docs/v3-product-validation.md) |
| Offline eval gates | 11 validation scripts cover regression, Agent Eval, Reply Guard, Planner, Memory, and more | [Test cases](backend/docs/test-cases.md) |

## Example Flow

User input:

> eggs 100g for breakfast, rice 200g and chicken breast 150g for lunch, how much do I have left today?

The system should not ask the model to "calculate an answer" directly. It should process the request through an auditable chain:

1. The Feishu event reaches `/webhook/feishu/events`.
2. The webhook verifies, parses, handles idempotency, and returns accepted/queued quickly.
3. AgentRuntime asks DeepSeek for a structured action plan.
4. Tool Executor runs multiple `record_food` actions, then runs `query_today`.
5. Food matching, grams, kcal, carbs, protein, and fat come from tools and data tables.
6. Reply Guard checks the final reply, and every number must trace back to execution facts.
7. The final result is sent back to the Feishu conversation.
8. Metrics, diagnostics, and optional trace data record the processing chain.

Core diagrams:

| Diagram | Link |
|---|---|
| Overall architecture | [fig_2_1_architecture.png](dv_document/prd_work/figures/fig_2_1_architecture.png) |
| End-to-end swimlane | [fig_4_1_swimlane.png](dv_document/prd_work/figures/fig_4_1_swimlane.png) |
| Async processing sequence | [fig_5_1_sequence.png](dv_document/prd_work/figures/fig_5_1_sequence.png) |
| Job state machine | [fig_5_2_job_state.png](dv_document/prd_work/figures/fig_5_2_job_state.png) |
| MySQL data relationship | [fig_6_1_er.png](dv_document/prd_work/figures/fig_6_1_er.png) |

![Calorie Agent architecture](dv_document/prd_work/figures/fig_2_1_architecture.png)

## Core Capabilities

`Calorie Agent` is not mainly about making responses sound more human. It is about making AI Agent risk explicit and controllable.

- **Low-friction entry**: users log, query, undo, and complete follow-ups inside Feishu.
- **Tool boundary**: the LLM only understands, plans, and phrases. It does not write storage, query storage, or calculate nutrition facts.
- **Idempotent writes**: `message_id`, `idempotency_key`, and per-action keys prevent duplicate intake records on retries.
- **Missing-info closure**: missing grams, unknown foods, and ambiguous candidates enter pending flows instead of model guesses.
- **Soft-delete undo**: undo marks intake records as `deleted`, excluding them from today summaries while preserving auditability.
- **Multi-user isolation**: V3 uses `tenant_id + user_id` as the data boundary, isolating the same Feishu open_id across different apps.
- **Fact-grounded replies**: Reply Guard blocks fabricated numbers, incorrect completion wording, permission leaks, and medical diagnosis-style claims.
- **Evaluation gates**: local regression and Agent Eval cover happy paths, duplicate messages, mistaken deletes, pending, hallucinated numbers, and tool failures.
- **Product validation**: activation, retention, actionable feedback, failed users, and usage events drive prioritization.

## How It Differs From Calorie Apps Or Chatbots

| Area | Typical calorie app | Typical chatbot | Calorie Agent |
|---|---|---|---|
| Entry | Standalone app or form | Chat window | Natural language inside Feishu |
| Fact source | User input or app database | Mostly model output | Tool and database facts |
| Multi-action handling | Often requires multiple UI steps | Often free-form | Action plan plus whitelisted tools |
| Error handling | Mostly form validation | Often lacks state closure | Pending, retry, dead letter, diagnostics |
| Duplicate messages | Usually not central | Rarely handled | Idempotency keys and duplicate skip |
| Undo | UI operation | Semantically unstable | Soft delete plus candidate targeting and permission boundary |
| AI risk | Limited AI involvement | Easy to hallucinate facts | Reply Guard plus execution facts |
| Verification | Functional tests | Conversation samples | Regression plus Agent Eval plus metrics |

## Current Setup

The project currently runs primarily as a Feishu bot backed by Tencent Cloud Functions. Local development is most useful for offline verification, regression testing, and SCF package builds.

### 1. Install backend dependencies

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The V3 MySQL repository is opt-in. If you need to package Tencent SCF dependencies, also install `backend/tencent_scf/requirements.txt` into the SCF dependency directory, or use the existing packaging workflow.

### 2. Run offline verification

```powershell
cd backend
.\.venv\Scripts\python.exe tests\run_regression.py
.\.venv\Scripts\python.exe tests\run_agent_eval.py
.\.venv\Scripts\python.exe tests\run_reply_guard_v2_eval.py
.\.venv\Scripts\python.exe tests\run_v2_agent_eval.py
```

The full verification list is in [backend/docs/test-cases.md](backend/docs/test-cases.md). These offline tests use fake tables, fixtures, or fake planner responses. They do not call real Feishu, DeepSeek, Tencent Cloud, or Bitable APIs.

### 3. Configure Tencent Cloud Function

Core endpoints:

```text
GET  /health
POST /webhook/feishu/events
POST /tasks/daily-cleanup?token=<ROLLOVER_TASK_TOKEN>
POST /tasks/reminders?token=<ROLLOVER_TASK_TOKEN>
```

Core environment variables are documented in [backend/.env.example](backend/.env.example) and [Tencent SCF README](backend/tencent_scf/README.md).

Required variables include:

```env
PORT=9000
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_VERIFICATION_TOKEN=
DEEPSEEK_API_KEY=
ROLLOVER_TASK_TOKEN=
BITABLE_APP_TOKEN=
BITABLE_FOOD_TABLE_ID=
BITABLE_INTAKE_TABLE_ID=
BITABLE_PENDING_TABLE_ID=
```

### 4. Build the Tencent SCF package

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\build_scf_zip.py
```

Output package:

```text
backend/dist/calorie-agent-api-tencent-scf.zip
```

Do not use Windows right-click compression or PowerShell `Compress-Archive` directly for the SCF package. The SCF zip root and Linux-style path rules are documented in the [release checklist](backend/docs/release-checklist.md).

## How It Works

The central rule is: **the model plans, tools own facts, and eval gates releases.**

Main flow:

```text
Feishu message
  -> Webhook verification / parse / idempotency
  -> quick ACK
  -> AgentRuntime planner
  -> whitelisted tool execution
  -> storage read/write
  -> Reply Guard
  -> Feishu reply
  -> metrics / diagnostics / eval feedback
```

### Agent fact boundary

- DeepSeek does not calculate `kcal`, `carbs`, `protein`, or `fat`.
- DeepSeek does not directly write storage, query storage, or undo records.
- Fuzzy food matching can only return candidates. Low-confidence or multi-candidate cases must ask follow-up questions.
- Missing grams or missing nutrition fields must become pending states or follow-up questions.
- Factual numbers in replies must come from tool results or intake snapshots.

### V3 data direction

V3 uses MySQL as the primary business database direction. Key tables include:

- `tenants`
- `users`
- `foods`
- `food_aliases`
- `intake_records`
- `standards`
- `memories`
- `usage_events`
- `feedback_samples`

`intake_records` stores food names, grams, and nutrition snapshots. Historical queries read the stored snapshot from the original intake row instead of recalculating from the current food database. See [V3 MySQL Schema](backend/docs/v3-mysql-schema.md).

### Evaluation and release gates

This project breaks Agent risk into testable scenarios instead of relying on chat examples only. Current documentation records offline validation coverage for:

- 63 regression scenarios
- 22 agent eval cases
- 12 reminder eval cases
- 10 skill registry eval cases
- 10 context loader eval cases
- 14 planner profile eval cases
- 12 memory learner eval cases
- 12 reply guard v2 eval cases
- 15 v2 agent eval cases
- 13 dynamic planner eval cases
- 10 intake nutrition query eval cases

These are offline validation figures from the repository's eval suites. They should not be described as real production user metrics.

## Why It Is Credible

The credibility of this project comes from readable, runnable, and reviewable repository artifacts, not from a verbal claim.

| File | Description |
|---|---|
| [backend/README.md](backend/README.md) | Backend capabilities, important files, regression checks, and deployment entry points |
| [backend/tencent_scf/README.md](backend/tencent_scf/README.md) | Tencent SCF deployment, environment variables, Feishu setup, and troubleshooting |
| [backend/docs/test-cases.md](backend/docs/test-cases.md) | Regression, Agent Eval, Reply Guard, Memory, Reminder, and other validation suites |
| [backend/docs/agent-runtime-v2.md](backend/docs/agent-runtime-v2.md) | Agent Runtime v2 goals, boundaries, module responsibilities, and migration strategy |
| [backend/docs/v3-mysql-schema.md](backend/docs/v3-mysql-schema.md) | V3 MySQL layer, idempotency, isolation, and environment variables |
| [backend/docs/v3-feishu-tenant.md](backend/docs/v3-feishu-tenant.md) | Feishu integration, multi-tenant identity resolution, and user onboarding flow |
| [backend/docs/v3-product-validation.md](backend/docs/v3-product-validation.md) | Beta user goals, activation/retention/feedback definitions, and prioritization rules |
| [backend/docs/release-checklist.md](backend/docs/release-checklist.md) | Release freeze, regression gate, cloud smoke tests, and rollback steps |
| [dv_document/prd_work/飞书饮食记录Agent_V3_Beta_PRD.md](dv_document/prd_work/飞书饮食记录Agent_V3_Beta_PRD.md) | V3 Beta PRD source index and fixed product stance |

## Repository Structure

The most relevant project structure is:

```text
.
├── backend/
│   ├── app/
│   │   ├── config.py
│   │   ├── feishu/
│   │   └── services/
│   ├── docs/
│   │   ├── agent-runtime-v2.md
│   │   ├── test-cases.md
│   │   ├── v3-feishu-tenant.md
│   │   ├── v3-mysql-schema.md
│   │   └── v3-product-validation.md
│   ├── scripts/
│   │   └── build_scf_zip.py
│   ├── tencent_scf/
│   │   ├── app.py
│   │   ├── scf_bootstrap
│   │   ├── requirements.txt
│   │   └── calorie_agent/
│   │       ├── agent/
│   │       ├── context/
│   │       ├── domain/
│   │       ├── identity/
│   │       ├── memory/
│   │       ├── product_metrics/
│   │       ├── skills/
│   │       ├── storage/
│   │       └── tools/
│   ├── tests/
│   ├── requirements.txt
│   └── README.md
├── dv_document/
│   └── prd_work/
│       ├── figures/
│       └── 飞书饮食记录Agent_V3_Beta_PRD.md
├── v2-plan/
├── v3-plan/
├── README.md
└── README.en.md
```

Key directories:

- `backend/tencent_scf/calorie_agent/agent/`: AgentRuntime, planner, tool executor, and reply guard.
- `backend/tencent_scf/calorie_agent/domain/`: undo, pending, reporting, reminders, anomaly, and food matching logic.
- `backend/tencent_scf/calorie_agent/storage/`: V3 MySQL schema, repository, and connection layer.
- `backend/tencent_scf/calorie_agent/identity/`: V3 Feishu identity resolution and tenant/user context.
- `backend/tests/`: offline regression, Agent Eval, Reply Guard, Memory, Planner, and related validation scripts.
- `dv_document/prd_work/`: PRD, architecture diagram, swimlane, sequence diagram, state machine, and ER diagram.

## Boundaries

This project is best understood as an AI Agent product validation and backend pipeline project, not as a complete commercial application.

Known boundaries:

- The current primary entry point is a Feishu bot, not a standalone app or general health platform.
- V3 MySQL is the primary storage direction, while the legacy Bitable path remains for compatibility and rollback.
- Some V2/V3 capabilities are behind feature flags, shadow mode, read-only mode, or opt-in integration. They should not be assumed to fully take over production by default.
- The project does not include photo recognition, marketplace/community features, a full admin console, coach-side workflows, or a complex commercial permission system.
- The project does not provide medical diagnosis or treatment advice. Nutrition information should be treated as logging and management support.

If the goal is only a one-off chatbot answer, a lightweight bot would be simpler. This project's value is in designing the Agent pipeline, data boundary, exception recovery, and evaluation loop together.

## License

