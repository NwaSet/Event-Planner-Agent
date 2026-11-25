
# Event-Planner-Agent


## Table of Contents

- [Description](#description)
- [1. Installation](#1-installation)
  - [A) Clone the project](#a-clone-the-project)
  - [B) Create the virtual environment](#b-create-the-virtual-environment)
  - [C) Activate the virtual environment](#c-activate-the-virtual-environment)
  - [D) Install requirements](#d-install-requirements)
  - [E) Create the .env file](#e-create-the-env-file)
- [2. Run the Application](#2-run-the-application)
  - [Example request](#example-request)
  - [Expected output](#expected-output)
- [3. Project Architecture](#3-project-architecture)
- [4. Multi-Agent System](#4-multi-agent-system)
  - [A) Event Orchestrator](#a-event-orchestrator)
  - [B) Specialized AI Agents](#b-specialized-ai-agents)
  - [C) Multi-Agent Workflow](#c-multi-agent-workflow)
- [5. Agent Tools](#5-agent-tools)
- [6. Data Management](#6-data-management)
  - [providers.json](#providersjson)
- [7. Memory and Database](#7-memory-and-database)
- [8. Streaming and User Interface](#8-streaming-and-user-interface)
  - [Expected communication flow](#expected-communication-flow)
- [9. Logging and Tracing](#9-logging-and-tracing)
  - [Purpose](#purpose)
- [10. Human Validation](#10-human-validation)
- [11. Technologies](#11-technologies)
- [12. Project Limitations](#12-project-limitations)
- [13. Project Status](#13-project-status)
- [14. Authors](#14-authors)
- [14. License](#15-license)

---


## Description

**Event-Planner-Agent** is a multi-agent AI system designed to help users plan and organize events in Wallonia, Belgium.

The goal of this project is to simplify event planning by coordinating several specialized AI agents through a central orchestrator.

Users only need to describe the event they want to organize and specify their requirements, such as:

- Event type (birthday, corporate event, festival, etc.)
- Location
- Date and duration
- Number of participants
- Maximum budget
- Specific requirements or preferences

The system analyzes one independent request, searches for suitable venues and service providers, allocates a limited budget when needed, and proposes a schedule. Reports and email drafts are produced only when requested; email drafts require user approval and are never sent automatically.

The system uses fictional event-provider data for demonstration purposes and public weather information.

> **Note:** This project is an academic prototype. It does not automatically book venues or contact service providers.

---

## 1. Installation

### A) Clone the project

```bash
git clone https://github.com/NwaSet/Event-Planner-Agent.git
cd Event-Planner-Agent
```

### B) Create the virtual environment

**Windows:**
```bash
python -m venv .venv
```

**Linux:**
```bash
python3 -m venv .venv
```

### C) Activate the virtual environment

**Windows (CMD):**
```bat
.venv\Scripts\activate.bat
```

**Windows (PowerShell):**
```powershell
.\.venv\Scripts\Activate.ps1
```

**Linux:**
```bash
source .venv/bin/activate
```

### D) Install requirements

```bash
pip install -r requirements.txt
```

### E) Create the .env file

Copy the environment configuration template.

**Windows (CMD):**
```bat
copy .env.example .env
```

**Linux / PowerShell:**
```bash
cp .env.example .env
```

Open `.env` and configure your LLM provider:

```env
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_API_KEY=your-api-key-here
LLM_MODEL=qwen/qwen3.8-27b
```

Make sure that the selected model is available from your LLM provider.

**Important:** Never commit your `.env` file or API keys to GitHub.

---

## 2. Run the Application

After activating the virtual environment and installing all dependencies, start the application:

```bash
python main.py
```

The application is intended to accept a natural-language event request and coordinate the appropriate AI agents.

### Example request

```text
Organize an electronic music event in Namur for
200 participants with a maximum budget of €5,000.

The event should take place on a Saturday evening,
from 8 PM to 3 AM.

Find a suitable venue, select service providers,
prepare a budget, and create an event schedule.
```

### Expected output

The system aims to generate:

- A proposed venue and suitable service providers
- A detailed event budget
- An event schedule
- A summary of the selected options
- Email drafts for contacting service providers
- A final event planning report

The user must validate the proposed event plan before the email drafts are finalized.

---

## 3. Project Architecture

```text
Event-Planner-Agent/
│
├── .venv/                 
│
├── controller/
│   └── event_controller.py       # UI extension placeholder
│
├── data/
│   ├── providers.json
│   ├── caterers.json
│   ├── venues.json
│   └── logs/logs.jsonl
│
├── model/
│   ├── agent.py                 # ReAct loop and as_tool delegation
│   ├── event_orchestrator.py    # LLM Supervisor
│   ├── event_state.py           # Per-request data and objective checks
│   ├── agents/                  # Four specialists, prompts, public tools and compact search
│   ├── llm.py                   # API, counters and usage logs
│   └── trace.py
│
├── tests/                       # Mocked LLM/API tests; network disabled
│
├── view/
│   └── view.py               
│
├── .env.example             
├── .gitignore                 
├── LICENSE                  
├── main.py                  
├── README.md                 
└── requirements.txt            
```

---

## 4. Multi-Agent System

The application is based on a **multi-agent architecture**.

A central orchestrator coordinates several specialized AI agents, each responsible for a specific aspect of event planning.

### A) Event Orchestrator

**File:** `model/event_orchestrator.py`

The orchestrator is responsible for:

- Analyzing the user's request
- Identifying the tasks required to organize the event
- Selecting the appropriate agents
- Coordinating communication between agents
- Collecting and combining agent results
- Managing conflicts between agent recommendations
- Handling errors and retry limits
- Producing the final event planning result

The orchestrator does not need to perform every task itself. Instead, it delegates tasks to specialized agents.

### B) Specialized AI Agents

The system is designed around four specialized agents.

| Agent | Responsibilities |
|---|---|
| **Budget Agent** | Allocates the budget, estimates expenses, and checks financial constraints. |
| **Decision Agent** | Selects suitable venues and service providers according to the user's requirements. |
| **Planning Agent** | Creates the event schedule and considers logistical and weather constraints. |
| **Writer Agent** | Generates the final report and email drafts for service providers. |

Each agent can have its own:

- System prompt
- Specialized tools
- Local conversation during one delegation
- Responsibilities
- Execution logic

### C) Multi-Agent Workflow

```text
             USER REQUEST
                  |
                  v
          EVENT CONTROLLER
                  |
                  v
          EVENT ORCHESTRATOR
                  |
          Analyze requirements
                  |
                  v
          Create execution plan
                  |
       +----------+----------+
       |          |          |
       v          v          v
   BUDGET     DECISION    PLANNING
    AGENT       AGENT       AGENT
       |          |          |
       v          v          v
   Budget      Venues      Schedule
   analysis    Providers   Weather
       |          |          |
       +----------+----------+
                  |
                  v
          EVENT ORCHESTRATOR
                  |
          Check consistency
                  |
                  v
             WRITER AGENT
                  |
                  v
             FINAL REPORT
                  |
                  v
           USER VALIDATION
                  |
                  v
             EMAIL DRAFTS
```

The diagram is illustrative: the Supervisor chooses useful tasks and their order. A fixed schedule skips Planning; an unlimited budget skips Budgetary; a simple answer skips Writer. Planning failure does not prevent catalogue recommendations, whose availability remains unverified. Python checks objective compatibility, including when a venue precedes Planning or follows a supplier selection. Selection returns exact IDs, including the formula or kit ID. Conflicts remain explicit instead of silently relaxing constraints.

Before presenting venue candidates, Python checks that the requested catering and technical services have at least one compatible offer under that venue's restrictions. This prevents avoidable dead ends while leaving all qualitative choices to the specialists.

---

## 5. Agent Tools

`model/agents/tools.py` contains the public catalogue searches, weekday calculation, Open-Meteo lookup and `ask_user` approval tool. Public `get_venues`, `get_caterers` and `get_providers` retain their original complete-catalogue fallback for absent, invalid or unmatched filters.

`model/agents/search.py` supplies the separate LLM view: `matched`, `no_match`, `invalid_filters` or `error`, with at most 5 candidates by default (10 maximum per observation). Only compatible formulas/kits are exposed, with exact IDs, prices, descriptions and constraints. Mandatory constraints are also applied when the specialist requests another search. No complete fallback catalogue is injected into an agent conversation.

---

## 6. Data Management

The `data/` directory stores fictional information used by the agents.

To Do

### providers.json

Contains information about event service providers, such as:

- Company name
- Service category
- Location
- Pricing
- Capacity
- Available services

The data is fictional and intended for testing and demonstration.

---

## 7. Memory and Database

Each `Supervisor.run(request)` creates a fresh `EventRun` containing an `event_data` dictionary: request, requirements, planning, budget, venue, catering, providers, documents and task statuses. Python alone updates validated results. Detailed catalogues stay local and are loaded once per run. Validated results and permanent failures are reused. A repairable failure permits one further attempt when relevant structured parameters or earlier decisions change; identical parameters or a paraphrased instruction do not rerun a specialist. A small per-task register stores only the attempt count and last relevant input. There is no result cache or dependency graph.

The Supervisor directly calls `planning_agent(demande=...)`, `budgetary_agent(demande=...)`, `decision_making_agent(demande=..., category=...)` or `writer_agent(demande=...)`. The mandatory `configure_event` tool has been removed. Each delegation can optionally supply `requirements` with only new known constraints, and `tasks` to announce the requested scope. Neither is required to start. Python preserves the original request and injects only relevant earlier decisions. Without prepared candidates, Decision-Making uses its category's search tool before returning IDs.

Preferences use a text format; dictionary input is normalized to compact JSON text without losing information. They remain soft wishes, separate from mandatory event type, headcount, budget and schedule. Explicit student-party wording is preserved as `soiree_etudiante`. Searches apply mandatory constraints before limiting results and expose the applied filters and truncation. Catalogue order is not a qualitative ranking. A refined search replaces earlier selectable candidates. In the current data, all seven Namur venues forbid student parties: the correct result is an explicit venue conflict, while independent catering/provider research can continue.

An announced task list allows automatic stopping when all requested tasks are terminal; without it, the Supervisor can keep delegating and then finish naturally. Empty results never count as a successful event. A known limited budget requires Budgetary's allocation before selections; unknown or unlimited budgets need no allocation. A missing catering headcount can fail that task without blocking a venue recommendation. Errors do not cascade to independent tasks.

There is no conversation persistence, event reload or resumption. `supervisor.event_data` and `supervisor.counters` expose the last result for diagnostics only and are replaced on the next run. Catalogue references are released at the end of the run. Technical usage logs remain persistent.

Limits are configurable without adding dependencies:

```python
from model.event_orchestrator import create_orchestrator

supervisor = create_orchestrator(
    max_iterations=6,       # Supervisor ReAct turns
    max_llm_calls=24,        # Shared by all agents, HTTP retries and finalization
    max_delegations=8,       # Actual specialist executions; reused results do not count
)
result = supervisor.run("Organise une soirée à Namur sans limite de budget.")
```

One correction is allowed for an invalid structured answer. Provider errors, `content_filter` and explicit refusals are distinguished from invalid JSON and are not repaired by reprompting. Identical `tool_use_failed` requests are not retried. Once announced tasks are finished, or a limit stops progress, the Supervisor returns the verified Python summary without a redundant LLM synthesis. If it finishes naturally without another tool call, its final text is also replaced by the verified summary. Standalone specialists retain their final turn when necessary, within the shared budget. CLI exit code `0` means success, `2` a partial result, and `1` a configuration/execution failure.

The user summary displays local dates and times, venue and supplier names, offer descriptions, calculated amounts and unresolved tasks. Venue exclusivities are shown as authorized supplier names instead of a JSON object. Internal IDs remain in `event_data`; missing catalogue references are explicitly marked for verification. Availability and incomplete pricing remain conditional.

The existing positional calls `Agent.run(request)` and `Agent.as_tool(name, description)` remain valid. Optional keyword arguments connect validation, shared counters and completion checks. Decision-Making delegation now requires `category` (`venue`, `catering`, `provider`) alongside `demande`; all public catalogue function signatures are unchanged.

---

## 8. Streaming and User Interface

**File:** `view/view.py`

The application is designed to display agent execution progressively through streaming.

Instead of waiting for the entire event planning process to finish, the interface can display:

- Active agent
- Agent status
- Generated text
- Tool calls
- Progress information
- Errors
- Final results

The Controller connects the user interface to the orchestrator.

The View is responsible for displaying information, while the agents and orchestrator remain independent of the presentation layer.

### Expected communication flow

```text
USER
  |
  v
VIEW
  |
  v
CONTROLLER
  |
  v
ORCHESTRATOR
  |
  v
AI AGENTS
  |
  v
STREAMING EVENTS
  |
  v
CONTROLLER
  |
  v
VIEW
```

This separation makes it easier to replace the interface without changing the agent logic.

---

## 9. Logging and Tracing

**File:** `model/trace.py`

The tracing system is intended to monitor the execution of the multi-agent application.

It records successful LLM usage and exposes runtime traces:

- Agent execution history
- LLM requests and responses
- Tool calls
- Execution duration
- Errors and retries
- Input tokens
- Output tokens
- Total token consumption
- Estimated API costs (using configurable prices, not billing data)

Successful usage is stored in `data/logs/logs.jsonl`, including the returned model and finish reason. Per-run counters include all attempted LLM requests and rate-limit retries; the trace callback receives the final counters and status. Failed requests do not provide token usage, so the successful usage journal and attempt counter intentionally differ.

### Purpose

Tracing makes it possible to analyze:

1. Which agents were executed
2. Which tools were used
3. How long each operation took
4. How many tokens were consumed
5. Where execution errors occurred

This information is useful for debugging, optimization, and evaluating the cost of the multi-agent architecture.

---

## 10. Human Validation

Human validation is an important part of the system.

After the agents generate the event plan, the user can review:

- Selected venues
- Service providers
- Total estimated cost
- Event schedule
- Planning constraints

The user can then approve the plan or request modifications.

Email drafts are generated or finalized after validation.

The system does not automatically send emails or confirm reservations without authorization.

---

## 11. Technologies

The project is designed to use the following technologies:

| Technology | Purpose |
|---|---|
| Python | Main programming language |
| llm API | LLM inference |
| JSON | Fictional venue and provider datasets |
| Open-Meteo | Weather information |
| Python virtual environment | Dependency isolation |
| Git / GitHub | Version control and collaboration |

---

## 12. Project Limitations

This project is an academic prototype.

Current design limitations include:

- Venue and provider information is fictional.
- Weather forecasts depend on API availability and forecast horizon.
- AI-generated recommendations may contain errors.
- Event costs are estimates and must be verified.
- Reservations and email sending are not automated.
- Human validation is required before finalizing email drafts.

The catalogue cannot confirm availability. Provider kit prices are HT, while venue and catering prices are TTC; travel, VAT, drinks and extra staff may remain unpriced. The system reports known amounts and never certifies a complete final cost from those partial data. Unsupported constraints and the interpretation of natural language still rely on the Supervisor; qualitative selection remains an LLM task.

Run all tests without paid calls:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests block network access and mock LLM/API responses. They cover direct delegations without configuration, Namur, partial Planning failure, limited budgets, venue exclusivities, reversed ordering, deadlines/logistics, invalid outputs, duplicate work, limits, presentation, successive independent runs and email refusal. The mocked Namur scenario completes in 7 LLM calls and 4 delegations after removing the redundant synthesis call; this is a deterministic regression check, not a real-provider token benchmark. See `To Do.md` for executed checks and remaining real-LLM validation.

---

## 13. Project Status

**Status: In development**

This project is being developed as part of an academic course on AI agents and multi-agent orchestration.

The architecture, agent responsibilities and tools may evolve during development. Current event handling is strictly one-shot.

---

## 14. Authors

- [Florian Samain](https://github.com/NwaSet)
- [Yohann Ducourtieux](https://github.com/Nhanyo)

---

## 15. License

See the [LICENSE](LICENSE) file for licensing information.
