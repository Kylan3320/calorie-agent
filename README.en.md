# Calorie Agent

A Feishu-native calorie tracking Agent with low-friction logging, tool-grounded facts, and a product validation loop.

`Calorie Agent` is a natural-language food logging Agent for a Feishu bot. A user can send messages such as "chicken breast 50g" or "eggs 100g for breakfast, rice 200g for lunch, how much do I have left today?", and the system turns the message into tool actions for logging, querying, undoing, asking follow-up questions, and replying in Feishu.

It is not a general health app, and it does not let an LLM freely calculate calories.

It is closer to a verifiable AI Agent product pipeline: Feishu provides the low-friction entry point, AgentRuntime plans and orchestrates, backend tools and storage own factual reads and writes, and Reply Guard plus validation materials control AI risk.

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

This GitHub folder is a portfolio and deployment-oriented release package, not a full development repository snapshot. It keeps two runnable Tencent Cloud Function packages, plus PRD, architecture diagrams, test data, and metrics dashboard materials.

The package contains three main parts:

- `calorie-agent-apiV2/`: a V2 cloud function package with Feishu entry, AgentRuntime, tool execution, Bitable-compatible path, reporting, reminders, memory, and Reply Guard.
- `calorie-agent-apiV3-Beta-simulation/`: a V3 Beta simulation package that adds MySQL storage, Feishu multi-tenant identity, and product metrics modules on top of V2 capabilities. It also includes the PyMySQL runtime dependency.
- `PRD及测试数据、数据看板/`: product documentation, system interaction diagrams, state machine, exception recovery diagram, test dataset notes, gray-test metrics dashboard, and MySQL gray-test screenshots.

The core goal is not to make a model answer nutrition questions directly. The goal is to split a high-frequency food logging workflow into an auditable, replayable, and testable product pipeline.

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
- Engineers studying AI Agent tool use, state management, exception recovery, and validation gates.
- Teams connecting chat interfaces to real business data instead of demo-only conversations.
- Anyone who needs to understand Feishu bots, Tencent Cloud Functions, Bitable/MySQL, and LLM orchestration boundaries.

It is not a good fit for:

- Photo recognition, marketplace/community features, a full health app, or a commercial admin console.
- Medical diagnosis, treatment advice, or high-stakes medical conclusions.
- Designs where an LLM directly calculates nutrition, writes to storage, or bypasses the tool layer.
- Lightweight chatbots that do not need idempotency, permission isolation, exception recovery, or release gates.

## What It Can Do

Capabilities represented in the current release package include:

| Capability | Description | Key Material |
|---|---|---|
| Feishu text entry | Receives Feishu bot text events, returns `status=accepted` quickly, and continues processing inside the cloud function | [V2 SCF package](calorie-agent-apiV2/README.md), [V3 Beta package](calorie-agent-apiV3-Beta-simulation/README.md) |
| Natural-language food logging | Supports known foods, multi-food messages, compound messages, and today summaries | [V2 source package](calorie-agent-apiV2/calorie_agent/), [V3 source package](calorie-agent-apiV3-Beta-simulation/calorie_agent/) |
| Query and undo | Supports today queries, stored intake nutrition queries, undo latest, and semantic undo | [domain modules](calorie-agent-apiV3-Beta-simulation/calorie_agent/domain/) |
| Missing-info follow-up | Missing grams, unknown foods, and low-confidence candidates enter pending flows | [pending.py](calorie-agent-apiV3-Beta-simulation/calorie_agent/domain/pending.py) |
| Agent tool orchestration | DeepSeek emits an action plan, while the backend executes only whitelisted tools | [agent modules](calorie-agent-apiV3-Beta-simulation/calorie_agent/agent/) |
| Fact-grounded reply validation | Reply Guard validates final replies against execution facts and blocks fabricated numbers or incorrect statuses | [reply_guard.py](calorie-agent-apiV3-Beta-simulation/calorie_agent/agent/reply_guard.py) |
| V3 MySQL data layer | V3 Beta adds MySQL schema, repository, connection layer, and idempotent write strategy | [schema.sql](calorie-agent-apiV3-Beta-simulation/calorie_agent/storage/schema.sql) |
| Feishu multi-tenant identity | Extracts app/open_id/chat/message fields from Feishu events and maps them to tenant/user context | [identity modules](calorie-agent-apiV3-Beta-simulation/calorie_agent/identity/) |
| Product metrics loop | Usage, feedback, activation, retention, and gray-test dashboard materials | [product_metrics modules](calorie-agent-apiV3-Beta-simulation/calorie_agent/product_metrics/), [metrics dashboard](PRD及测试数据、数据看板/v3灰度测试指标看板.docx) |
| PRD and diagrams | Includes overall chain, sequence diagram, state machine, and exception recovery diagram | [PRD directory](PRD及测试数据、数据看板/PRD/) |

## Example Flow

User input:

> eggs 100g for breakfast, rice 200g and chicken breast 150g for lunch, how much do I have left today?

The system should not ask the model to "calculate an answer" directly. It should process the request through an auditable chain:

1. The Feishu event reaches `/webhook/feishu/events`.
2. The webhook verifies, parses, handles idempotency, and returns `status=accepted` quickly.
3. AgentRuntime asks DeepSeek for a structured action plan.
4. Tool Executor runs multiple `record_food` actions, then runs `query_today`.
5. Food matching, grams, kcal, carbs, protein, and fat come from tools and data tables.
6. Reply Guard checks the final reply, and every number must trace back to execution facts.
7. The final result is sent back to the Feishu conversation.
8. Metrics, diagnostics, and optional trace data record the processing chain.

Core diagrams:

| Diagram | Link |
|---|---|
| Overall chain | [calorie-agent整体链路图.png](PRD及测试数据、数据看板/PRD/calorie-agent整体链路图.png) |
| Async processing sequence | [时序图.png](PRD及测试数据、数据看板/PRD/时序图.png) |
| Job state machine | [状态机图.png](PRD及测试数据、数据看板/PRD/状态机图%20%281%29.png) |
| Exception recovery overview | [06_exception_recovery.png](PRD及测试数据、数据看板/PRD/06_exception_recovery.png) |
| V3 Beta PRD | [飞书饮食记录Agent_V3_Beta_PRD.docx](PRD及测试数据、数据看板/PRD/飞书饮食记录Agent_V3_Beta_PRD.docx) |

![Calorie Agent overall chain](PRD及测试数据、数据看板/PRD/calorie-agent整体链路图.png)

## Core Capabilities

`Calorie Agent` is not mainly about making responses sound more human. It is about making AI Agent risk explicit and controllable.

- **Low-friction entry**: users log, query, undo, and complete follow-ups inside Feishu.
- **Tool boundary**: the LLM only understands, plans, and phrases. It does not write storage, query storage, or calculate nutrition facts.
- **Idempotent writes**: `message_id`, `idempotency_key`, and per-action keys prevent duplicate intake records on retries.
- **Missing-info closure**: missing grams, unknown foods, and ambiguous candidates enter pending flows instead of model guesses.
- **Soft-delete undo**: undo marks intake records as `deleted`, excluding them from today summaries while preserving auditability.
- **Multi-user isolation**: V3 uses `tenant_id + user_id` as the data boundary, isolating the same Feishu open_id across different apps.
- **Fact-grounded replies**: Reply Guard blocks fabricated numbers, incorrect completion wording, permission leaks, and medical diagnosis-style claims.
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
| Verification | Functional tests | Conversation samples | PRD plus test data plus gray-test dashboard |

## Current Setup

The `calorie-agent-apiV2/` and `calorie-agent-apiV3-Beta-simulation/` folders are expanded Tencent Cloud Function packages. They are not full development project directories, so this GitHub package does not include the original `backend/tests`, `scripts/build_scf_zip.py`, or `.env.example` files.

### 1. Choose a deployment package

| Package | Best for | Notes |
|---|---|---|
| [calorie-agent-apiV2](calorie-agent-apiV2/) | Bitable-compatible path and AgentRuntime capability showcase | Includes Feishu entry, tool execution, reporting, reminders, memory, Reply Guard, and V2 Runtime modules |
| [calorie-agent-apiV3-Beta-simulation](calorie-agent-apiV3-Beta-simulation/) | V3 Beta simulation and MySQL direction showcase | Adds `identity`, `storage`, and `product_metrics` on top of V2, and includes the PyMySQL runtime dependency |

### 2. Configure Tencent Cloud Function

Recommended Tencent Cloud Function settings:

- Runtime: Python 3.10
- Function type: HTTP function / Web function
- Listen port: `9000`
- Bootstrap file: `scf_bootstrap`

Core endpoints:

```text
GET  /health
POST /webhook/feishu/events
POST /tasks/daily-cleanup?token=<ROLLOVER_TASK_TOKEN>
POST /tasks/reminders?token=<ROLLOVER_TASK_TOKEN>
```

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

V3 Beta MySQL-related variables are reflected in [schema.sql](calorie-agent-apiV3-Beta-simulation/calorie_agent/storage/schema.sql) and [mysql_client.py](calorie-agent-apiV3-Beta-simulation/calorie_agent/storage/mysql_client.py).

### 3. Zip and upload

When deploying from this GitHub folder, the selected subdirectory content should become the zip root. Do not zip the outer repository directory as the package root.

The zip root should directly contain:

```text
app.py
calorie_agent/
README.md
requirements.txt
scf_bootstrap
```

The V3 Beta package also contains:

```text
pymysql/
pymysql-1.2.0.dist-info/
```

`scf_bootstrap` must remain executable. If Windows compression drops the executable bit, fix it in the cloud function deployment process or with a build script.

## How It Works

The central rule is: **the model plans, tools own facts, and metrics plus validation materials gate product decisions.**

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
  -> metrics / diagnostics / product feedback
```

### Agent fact boundary

- DeepSeek does not calculate `kcal`, `carbs`, `protein`, or `fat`.
- DeepSeek does not directly write storage, query storage, or undo records.
- Fuzzy food matching can only return candidates. Low-confidence or multi-candidate cases must ask follow-up questions.
- Missing grams or missing nutrition fields must become pending states or follow-up questions.
- Factual numbers in replies must come from tool results or intake snapshots.

### V3 data direction

V3 uses MySQL as the primary business database direction. Key tables are defined in [schema.sql](calorie-agent-apiV3-Beta-simulation/calorie_agent/storage/schema.sql), including:

- `tenants`
- `users`
- `foods`
- `food_aliases`
- `intake_records`
- `standards`
- `memories`
- `usage_events`
- `feedback_samples`

`intake_records` stores food names, grams, and nutrition snapshots. Historical queries read the stored snapshot from the original intake row instead of recalculating from the current food database.

### Validation and data materials

The current release package does not include the original development repository's offline test scripts, but it includes portfolio-facing test and metrics materials:

- [测试集与数据指标.docx](PRD及测试数据、数据看板/测试集与数据指标.docx)
- [v3灰度测试指标看板.docx](PRD及测试数据、数据看板/v3灰度测试指标看板.docx)
- [MySQL gray-test screenshots](PRD及测试数据、数据看板/灰度测试数据/MYSQL/)

These materials should be read as high-fidelity simulation or pre-beta validation artifacts unless a separate real-user data source is explicitly provided.

## Why It Is Credible

The credibility of this project comes from readable, runnable, and reviewable repository artifacts, not from a verbal claim.

| File or directory | Description |
|---|---|
| [calorie-agent-apiV2/README.md](calorie-agent-apiV2/README.md) | V2 Tencent Cloud Function package deployment, environment variables, Feishu setup, and troubleshooting |
| [calorie-agent-apiV2/calorie_agent/agent/](calorie-agent-apiV2/calorie_agent/agent/) | V2 AgentRuntime, planner, tool executor, reply guard, and related modules |
| [calorie-agent-apiV3-Beta-simulation/README.md](calorie-agent-apiV3-Beta-simulation/README.md) | V3 Beta simulation package notes |
| [calorie-agent-apiV3-Beta-simulation/calorie_agent/storage/schema.sql](calorie-agent-apiV3-Beta-simulation/calorie_agent/storage/schema.sql) | V3 MySQL schema, idempotency, and isolation table structure |
| [calorie-agent-apiV3-Beta-simulation/calorie_agent/identity/](calorie-agent-apiV3-Beta-simulation/calorie_agent/identity/) | Feishu multi-tenant identity resolution and UserContext |
| [calorie-agent-apiV3-Beta-simulation/calorie_agent/product_metrics/](calorie-agent-apiV3-Beta-simulation/calorie_agent/product_metrics/) | Usage, feedback, activation, and retention metrics modules |
| [PRD及测试数据、数据看板/PRD/飞书饮食记录Agent_V3_Beta_PRD.docx](PRD及测试数据、数据看板/PRD/飞书饮食记录Agent_V3_Beta_PRD.docx) | V3 Beta PRD |
| [PRD及测试数据、数据看板/测试集与数据指标.docx](PRD及测试数据、数据看板/测试集与数据指标.docx) | Test dataset and metric definition material |
| [PRD及测试数据、数据看板/v3灰度测试指标看板.docx](PRD及测试数据、数据看板/v3灰度测试指标看板.docx) | V3 gray-test metrics dashboard material |

## Repository Structure

The actual GitHub project folder structure is:

```text
.
├── README.md
├── README.en.md
├── calorie-agent-apiV2/
│   ├── README.md
│   ├── app.py
│   ├── requirements.txt
│   ├── scf_bootstrap
│   └── calorie_agent/
│       ├── agent/
│       ├── context/
│       ├── domain/
│       ├── memory/
│       ├── skills/
│       ├── tools/
│       ├── legacy_app.py
│       └── __init__.py
├── calorie-agent-apiV3-Beta-simulation/
│   ├── README.md
│   ├── app.py
│   ├── requirements.txt
│   ├── scf_bootstrap
│   ├── pymysql/
│   ├── pymysql-1.2.0.dist-info/
│   └── calorie_agent/
│       ├── agent/
│       ├── context/
│       ├── domain/
│       ├── identity/
│       ├── memory/
│       ├── product_metrics/
│       ├── skills/
│       ├── storage/
│       ├── tools/
│       ├── legacy_app.py
│       └── __init__.py
└── PRD及测试数据、数据看板/
    ├── PRD/
    │   ├── 飞书饮食记录Agent_V3_Beta_PRD.docx
    │   ├── calorie-agent整体链路图.png
    │   ├── 时序图.png
    │   ├── 状态机图 (1).png
    │   └── 06_exception_recovery.png
    ├── 灰度测试数据/
    │   └── MYSQL/
    ├── 测试集与数据指标.docx
    └── v3灰度测试指标看板.docx
```

Key directories:

- `calorie-agent-apiV2/calorie_agent/agent/`: AgentRuntime, planner, tool executor, Reply Guard, and V2 runtime modules.
- `calorie-agent-apiV2/calorie_agent/domain/`: undo, pending, reporting, reminders, anomaly, and food matching business logic.
- `calorie-agent-apiV2/calorie_agent/skills/`: markdown skill descriptions used by the planner.
- `calorie-agent-apiV3-Beta-simulation/calorie_agent/storage/`: MySQL schema, repository, and connection layer.
- `calorie-agent-apiV3-Beta-simulation/calorie_agent/identity/`: Feishu identity resolution and tenant/user context.
- `calorie-agent-apiV3-Beta-simulation/calorie_agent/product_metrics/`: usage, feedback, and retention metrics modules.
- `PRD及测试数据、数据看板/`: product design, system diagrams, test dataset material, gray-test metrics, and MySQL screenshots.

## Boundaries

This project is best understood as an AI Agent product validation and backend pipeline portfolio project, not as a complete commercial application.

Known boundaries:

- The current primary entry point is a Feishu bot, not a standalone app or general health platform.
- This GitHub folder is a release package plus documentation material, not the full development repository. Offline test scripts and build scripts are not included in this package.
- V3 MySQL is the primary storage direction, while the V2/Bitable-compatible path remains available.
- Some V2/V3 capabilities are behind feature flags, shadow mode, read-only mode, or opt-in integration. They should not be assumed to fully take over production by default.
- The project does not include photo recognition, marketplace/community features, a full admin console, coach-side workflows, or a complex commercial permission system.
- The project does not provide medical diagnosis or treatment advice. Nutrition information should be treated as logging and management support.
- Gray-test and eval materials should be read as high-fidelity simulation or pre-beta validation artifacts unless a separate real-user data source is explicitly provided.

If the goal is only a one-off chatbot answer, a lightweight bot would be simpler. This project's value is in designing the Agent pipeline, data boundary, exception recovery, and validation loop together.

## License

No root-level open-source license has been declared yet. Add an explicit `LICENSE` file before public distribution, commercial reuse, or derivative publication.
