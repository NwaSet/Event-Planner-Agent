
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

The system analyzes the request, searches for suitable venues and service providers, manages the budget, creates a schedule, and generates a final event report with email drafts ready to be sent to the selected providers.

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
│   └── event_controller.py       
│
├── dao/                        
│
├── data/
│   ├── providers.json           
│   └── venues.json                
│
├── logs/
│   └── logs.json              
│
├── model/
│   ├── __init__.py
│   ├── agent.py                  
│   ├── llm.py                  
│   └── trace.py                
│
├── services/
│   └── event_orchestrator.py     
│
├── tools/                       
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

**File:** `services/event_orchestrator.py`

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
- Memory
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

The exact agent execution order may depend on the user's request.

For example, a venue selected by the Decision Agent may exceed the available budget. In that case, the orchestrator must coordinate another iteration to find a suitable alternative.

---

## 5. Agent Tools

The `tools/` directory contains the functions and external services used by the AI agents.

To Do

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

The `dao/` directory is intended to manage persistent data.

The planned persistence layer will allow the application to:

- Save previous conversations
- Retrieve conversation history
- Store validated event decisions
- Preserve useful agent memory
- Resume previous event planning sessions
- Delete stored conversations and related data

Each agent maintain its own memory, while the orchestrator manages the shared event context.

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

It can collect:

- Agent execution history
- LLM requests and responses
- Tool calls
- Execution duration
- Errors and retries
- Input tokens
- Output tokens
- Total token consumption
- Estimated API costs

Execution information can be stored in `logs/logs.json`.

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
| Database (to be determined) | Conversation history and persistent memory |

---

## 12. Project Limitations

This project is an academic prototype.

Current design limitations include:

- Venue and provider information is fictional.
- Weather forecasts depend on API availability and forecast horizon.
- AI-generated recommendations may contain errors.
- Event costs are estimates and must be verified.
- Reservations and email sending are not automated.
- Human validation is required before finalizing the event plan.

---

## 13. Project Status

**Status: In development**

This project is being developed as part of an academic course on AI agents and multi-agent orchestration.

The architecture, agent responsibilities, tools, and persistence mechanisms may evolve during development.

---

## 14. Authors

- [Florian Samain](https://github.com/NwaSet)
- [Yohann Ducourtieux](https://github.com/Nhanyo)

---

## 15. License

See the [LICENSE](LICENSE) file for licensing information.
