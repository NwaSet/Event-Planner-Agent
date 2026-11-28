
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
  - [Catalogues académiques](#champs-conservés-et-supprimés)
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

**Event-Planner-Agent** is exclusively an academic demonstration of AI multi-agent orchestration, using entirely fictional event catalogues across Belgium. It will never be used to organize real events.

The goal of this project is to simplify event planning by coordinating several specialized AI agents through a central orchestrator.

Users only need to describe the event they want to organize and specify their requirements, such as:

- Event type (birthday, corporate event, festival, etc.)
- Location
- Date and duration
- Number of participants
- Maximum budget
- Specific requirements or preferences

The system maintains an event across successive messages, searches for suitable venues and service providers, allocates a limited budget when needed, and proposes a schedule. Reports and email drafts are produced only when requested; the plan and each email draft require separate user approval. Emails are never sent automatically.

The system uses fictional event-provider data for demonstration purposes and public weather information.

Venue searches first consider the requested commune, then the closest commune with compatible
offers, using the Overpass/Haversine function adapted from `WIP/fonction.py` in `model/geography.py`.
Set `OVERPASS_URL` in `.env` (the `/interpreter` suffix is added automatically), and optionally
`OVERPASS_KEY` for the `X-API-Key` header. Distances are straight-line distances between commune
centres, among locations resolved by Overpass; they are not driving distances or delivery costs.
Capacity, budget, schedules and explicitly requested equipment remain mandatory. If no compatible offer
exists, the search returns no match; if geolocation fails, it returns an explicit error.

After a venue is selected, caterer, technical provider and weather searches use its actual commune.
For example, a request for Namur with a venue selected in Dinant searches services from Dinant.
Changing the venue location invalidates earlier service selections. The original requested city
is retained in the event requirements. `location_scope=city` restricts an explicitly mandatory
commune; `province_fallback` restricts the province; the default `nearest` permits nearby communes
across province boundaries. A standalone service search uses the requested event location.

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

On Windows, this opens a separate console window using the same Python interpreter (and virtual environment), with the existing `rich.console.Console` display. The original process waits for that window to close and returns its exit code. To keep the interface inside the current terminal instead, use `python main.py --current-console`. Other platforms use the current terminal by default.

The application is intended to accept a natural-language event request and coordinate the appropriate AI agents.

The terminal now offers:

```text
Event Planner Agent
1. Nouvelle conversation
2. Historique des conversations
3. Quitter
```

Choose **1** and describe the event. After each answer, the `Vous >` prompt accepts another message for the same event. Exchanges and decisions are saved automatically in the same JSON file. Type **Quitter** (or press Enter without a message) to return to the menu, then **3** to close the application. Relaunch `python main.py`, choose **2**, and select a conversation to read its history and continue it. `0` returns from the history list without opening anything.

History works without a configured API key. Opening a saved conversation never runs agents, reloads catalogues or recalculates decisions. Only submitting a new message starts a turn. A simple question can be answered by the Supervisor from saved decisions; it does not require specialist calls. A yes/no draft approval or plan refusal is recorded entirely in Python, even without an API key. Accepting the plan starts Writer and requires model configuration.

```text
Vous > Organise une soirée à Namur pour 100 personnes.
Assistant > [Proposition et décisions sauvegardées]
Vous > Ajoute un traiteur.
Vous > Combien coûte la salle ?
Vous > Rédige un email pour demander un devis à la salle.
Assistant > [Plan consolidé à approuver]
Vous > oui
Assistant > [Brouillon exact à approuver]
Vous > oui
Vous > Finalement nous serons 150.
Vous > Quitter
```

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

The user must validate the proposed event plan before Writer generates any email draft, then approve or refuse the exact draft in a separate exchange.

---

## 3. Project Architecture

```text
Event-Planner-Agent/
│
├── .venv/                 
│
├── controller/
│   └── event_controller.py       # New/resumed turns + automatic conversation saving
│
├── dao/
│   └── conversation_dao.py       # JSON persistence, independent of agents/UI
│
├── data/
│   ├── providers.json
│   ├── caterers.json
│   ├── venues.json
│   ├── conversations/           # Personal local history, ignored by Git
│   └── logs/logs.jsonl
│
├── model/
│   ├── agent.py                 # ReAct loop and as_tool delegation
│   ├── event_orchestrator.py    # LLM Supervisor
│   ├── event_state.py           # Event state, revisions and objective checks per turn
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
          CONSOLIDATED PLAN
                  |
                  v
          USER PLAN APPROVAL
                  |
                  v
             WRITER AGENT
                  |
                  v
             EMAIL DRAFTS
                  |
                  v
          USER DRAFT APPROVAL
```

The diagram is illustrative: the Supervisor chooses useful tasks and their order. A fixed schedule skips Planning; an unlimited budget skips Budgetary; a simple answer skips Writer. Planning failure does not prevent catalogue recommendations, whose availability remains unverified. Python checks objective compatibility, including when a venue precedes Planning or follows a supplier selection. Selection returns exact IDs, including the formula or kit ID. Conflicts remain explicit instead of silently relaxing constraints.

Before presenting venue candidates, Python checks that the requested catering and technical services have at least one compatible offer under that venue's restrictions. This prevents avoidable dead ends while leaving all qualitative choices to the specialists.

---

## 5. Agent Tools

`model/agents/tools.py` contains the public catalogue searches, weekday calculation, Open-Meteo lookup and `ask_user` approval tool. Public `get_venues`, `get_caterers` and `get_providers` retain their complete-catalogue fallback for absent or invalid filters. Valid searches preserve their constraints and return offers in the nearest compatible commune, an empty list if none match, or a geolocation error if proximity cannot be established.

`model/agents/search.py` supplies the separate LLM view: `matched`, `no_match`, `invalid_filters` or `error`, with at most 5 candidates by default (10 maximum per observation). Only compatible formulas/kits are exposed, with exact IDs, prices, descriptions and constraints. Mandatory constraints are also applied when the specialist requests another search. No complete fallback catalogue is injected into an agent conversation.

---

## 6. Data Management

Les catalogues sont exclusivement fictifs. La simplification supplémentaire du **28 novembre 2026** retire les détails commerciaux inutiles, sans ajouter d'entrée ni changer d'identifiant.

| Catalogue | Entrées avant → après | Offres conservées |
| --- | ---: | ---: |
| `venues.json` | 150 → 150 | — |
| `caterers.json` | 100 → 100 | 322 formules |
| `providers.json` | 40 → 40 | 240 kits |

### Champs conservés et supprimés

**Salles :** ID, nom, commune/province, type d'espace, **une capacité entière**, deux tarifs TTC (journée/soirée), types d'événements compatibles, une courte liste d'équipements présents et `heure_fin_musique` directement dans la fiche. La capacité est désormais un plafond générique fictif, sans distinction assis/debout ou calcul de cumul entre espaces.

Les champs **PMR/accessibilité**, les capacités imbriquées, les fournisseurs imposés et le bloc `contraintes` ont disparu. Les équipements absents ne sont plus énumérés avec `false`. Le type de bâtiment, l'ambiance, la météo détaillée, la réservation, la logistique, les cautions et frais séparés ne sont pas utilisés. Les agents ne doivent pas inventer une garantie d'accessibilité ou un nombre de places assises.

**Traiteurs :** ID, nom, commune/province, catégories compatibles, formules avec ID/nom/description/type de service/caractère complet/capacité/prix TTC par personne. Les options alimentaires sont une liste des options disponibles. Pas de délai, livraison, cuisine obligatoire, personnel, supplément ou zone d'intervention. Les formules sont prévues pour au moins 250 personnes ; leurs capacités maximales restent finies. Un cocktail apéritif reste `repas_complet=false` ; une demande de repas complet filtre les formules avant le choix du modèle.

**Prestataires :** ID, nom, implantation(s), province lorsqu'elle est renseignée, kits avec ID/description/**`prix_ttc`** et niveaux `son`, `light`, `video`. Tous utilisent **absent < basique < standard < bon < excellent**. `absent` exige l'absence ; les autres niveaux sont des minimums. Les anciens termes techniques sont normalisés à l'entrée. Aucun fournisseur n'est imposé par une salle ; seuls des IDs explicitement demandés par l'utilisateur limitent la sélection.

### Recherche et choix des agents

144 salles acceptent les six catégories courantes : soirée, soirée étudiante, afterwork (`reception_entreprise`), anniversaire, mariage et fête associative. Six salles restent spécialisées en réceptions/calme. Les prix, capacités, espaces intérieurs/extérieurs, horaires musicaux et niveaux techniques maintiennent des choix intéressants.

Les contraintes utiles sont : capacité suffisante, budget connu, équipement réellement obligatoire, type d'événement compatible, horaire musical et localisation explicitement demandée. Les préférences restent des critères qualitatifs, notamment « extérieur si possible ». Python ne sélectionne pas le premier résultat : Decision-Making compare les candidats et renvoie les IDs choisis.

La fonction Overpass/Haversine de `model/geography.py` est inchangée : les villes viennent des candidats présents dans le JSON, et la commune compatible la plus proche est recherchée avant la limite d'affichage. Les traiteurs et prestataires peuvent venir d'une commune voisine, y compris d'une autre province. Aucune province d'intervention n'est injectée automatiquement ; une province explicitement imposée reste obligatoire. Les implantations multiples des prestataires restent utilisables par la fonction géographique existante.

### Finances, Writer et conversations

Tous les tarifs sont maintenant **TTC tout compris**, sans conversion ni supplément de TVA : location de salle, prix du repas × effectif, prix du kit. Les montants des kits sont des tarifs finaux fictifs ; il ne s'agit pas d'une conversion fiscale de tarifs réels. Le bilan additionne directement les trois catégories. Seuls un effectif de repas manquant ou une période de location non précisée peuvent rendre le chiffrage incomplet.

Les anciens paramètres PMR, assis/debout, fournisseur imposé par la salle, kilomètres, véhicules et TVA sont retirés des exigences à la reprise. Les anciennes formes de montants de caution/livraison restent à zéro pour les lecteurs existants, sans réintroduire de frais. Les filtres `accessible`, `seated`, `provider_id`, `venue_type`, `caterer_id`, `cuisine`, `category` et `setup_size` ne sont plus exposés aux agents. Les anciens filtres non vides sans équivalent, comme un style de cuisine, doivent être révisés explicitement.

Writer reçoit les seuls champs conservés, l'offre exacte et son prix TTC. Les messages, documents approuvés et JSON sauvegardés ne sont pas réécrits. Les IDs de salles, traiteurs, prestataires, formules et kits sont tous conservés ; une reprise utilise les tarifs actuels. Les cinq agents, ReAct, `Agent.as_tool()`, le DAO JSON et les validations distinctes du plan puis du brouillon restent en place. Aucun email n'est envoyé.

### Tests et cohérence des catalogues

**578 tests réussis**, sans réseau ni LLM réel/payant. Les dix scénarios demandés restent couverts : les neuf scénarios courants offrent plusieurs candidats par catégorie, tandis qu'un million de personnes produit un `no_match` réel pour la salle et le repas. Exemples avant la limite d'affichage :

| Scénario | Salles | Traiteurs | Prestataires |
| --- | ---: | ---: | ---: |
| Soirée à Namur, 100 personnes | 7 | 5 | 2 |
| Soirée étudiante à Namur, 200 personnes | 4 | 5 | 2 |
| Afterwork à Anvers, 100 personnes | 7 | 5 | 2 |
| Mariage à Namur, 150 personnes | 6 | 5 | 2 |
| Plafonds salle 2 000 €, repas 3 500 €, kit 1 500 € TTC | 3 | 4 | 2 |
| Repas complet obligatoire à Namur, 100 personnes | 7 | 5 | 2 |
| Son excellent obligatoire à Namur | 7 | 5 | 2 |

`tests/test_catalog_integrity.py` contrôle l'unicité des IDs, les prix positifs au centime, les capacités, les champs autorisés, les listes d'équipements/options, le lien cocktail/repas complet, les niveaux techniques, les descriptions « sans vidéo/son/éclairage », les provinces des communes partagées et la reprise sans anciens paramètres bloquants. Les tests géographiques conservent les recherches voisines. `tests/test_academic_catalogs.py` vérifie les dix scénarios et le parcours complet des cinq agents : budget de 7 000 €, choix autre que le premier candidat, sauvegarde/rechargement, validation du plan puis du brouillon exact.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_catalog_integrity.py tests/test_academic_catalogs.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

Les réponses LLM et le géocodage sont simulés ; ces tests vérifient les données, contrats et interactions, sans mesurer la qualité d'un modèle réel.

---

## 7. Memory and Database

Each turn creates an `EventRun` with fresh call/delegation limits and optionally restores the saved `event_data`: initial request, current requirements, planning, budget, selections, documents and task statuses. The controller uses `Supervisor.run(request, event_data=..., messages=..., interactive=True)`. The direct `Supervisor.run(request)` entry point retains its original independent one-shot behavior. Python alone updates validated results. Catalogues stay local and are loaded once per turn. Existing successful tasks are reused; explicit revisions reopen only affected tasks. Within a turn, the existing bounded retry checks still prevent duplicate work. There is no second orchestrator or generic dependency engine.

The Supervisor receives the initial request (up to 4,000 characters), current constraints, valid decisions, calculated costs, the pending question and at most four recent messages (1,200 characters each). Full user-facing history remains in JSON for consultation; specialist prompts and observations are not replayed. `clarify(question)` saves one pending clarification and ends the turn. Its answer is provided to the Supervisor on the next message.

For explicit changes, the Supervisor calls `revise_event`: updated `requirements`, `replace` for another proposal, `remove` for a cancelled service, or `unset` for an abandoned constraint. Python rechecks capacity, offer suitability, schedules and allocations. Venues impose no supplier. Compatible selections remain; incompatible selections are archived and marked pending for the appropriate specialist. Budgetary revises allocations when the ceiling changes, and Planning receives the previous schedule when moving the event. Each material change invalidates current approvals and archives documents with their original text, approval and revision. Archived documents are not presented as approved for the new plan.

The Supervisor directly calls `planning_agent(demande=...)`, `budgetary_agent(demande=...)`, `decision_making_agent(demande=..., category=...)` or `writer_agent(demande=...)`. The mandatory `configure_event` tool has been removed. Each delegation can optionally supply `requirements` with only new known constraints, and `tasks` to announce the requested scope. Neither is required to start. Python preserves the original request and injects only relevant earlier decisions. Without prepared candidates, Decision-Making uses its category's search tool before returning IDs.

Preferences use a text format; dictionary input is normalized to compact JSON text without losing information. They remain soft wishes, separate from mandatory event type, headcount, budget and schedule. Explicit student-party wording is preserved as `soiree_etudiante`. Searches apply mandatory constraints before limiting results and expose the applied filters and truncation. Catalogue order is not a qualitative ranking. A refined search replaces earlier selectable candidates. The academic catalogues provide several compatible candidates for common events, including student parties in Namur. Impossible strict constraints still produce an explicit conflict, while independent service research can continue.

An announced task list allows automatic stopping when all requested tasks are terminal; without it, the Supervisor can keep delegating and then finish naturally. Empty results never count as a successful event. A known limited budget requires Budgetary's allocation before selections; unknown or unlimited budgets need no allocation. A missing catering headcount can fail that task without blocking a venue recommendation. Errors do not cascade to independent tasks.

The controller persists every turn in the same conversation file, keeping its ID, title and creation date. Opening alone is read-only; submitting a message resumes the saved event. Selected IDs and offer IDs remain in `event_data`; the displayed names and amounts remain in saved messages. The current Supervisor leaves `event_details` empty rather than duplicating catalogue rows. Older snapshots, when present, remain readable. Writer reconstructs only the selected rows and offers from their stable IDs. Technical usage logs remain separate.

### Local conversation format and DAO

One UTF-8 JSON file is stored at `data/conversations/<conversation_id>.json`, relative to the project, independent of the terminal's current directory. IDs are UUID4 hexadecimal strings. Personal records and temporary files are ignored by Git; only `.gitkeep` is tracked. Timestamps use ISO 8601 in UTC with an explicit offset. The title is derived locally from the request (maximum 80 characters); no title-generation LLM call is added.

The version 1 structure is:

```json
{
  "schema_version": 1,
  "conversation_id": "0123456789abcdef0123456789abcdef",
  "title": "Une demande d'événement",
  "created_at": "2026-11-25T15:00:00+00:00",
  "updated_at": "2026-11-25T15:01:00+00:00",
  "status": "completed",
  "messages": [
    {"role": "user", "content": "Une demande d'événement"},
    {"role": "assistant", "content": "Réponse finale exacte"}
  ],
  "event_data": {},
  "event_details": {}
}
```

The empty objects above stand for the actual saved data, without filling in missing decisions:

- `messages`: successive user messages and assistant responses, including whitespace; user-facing history only.
- `event_data`: original request, requirements, dates, budget allocation, selected IDs, documents, task errors, human approvals, weather observations and geographic fallback state, as available from the run. IDs such as `"002"` remain strings; numeric kit IDs remain numbers.
- `event_details`: optional legacy snapshots, preserved when reading older files; current turns leave this object empty. Historical responses remain available in `messages`. Resuming a selection uses the current catalogue prices.
- Status: `running`, `completed`, `partial`, `refused`, `error`, `interrupted` or `waiting`. A human refusal is distinguished from a technical failure; its exact plan/draft and approval status remain in `event_data`.

Optional event fields include `pending_input` (`clarification`, `plan_approval` or `email_approval`), `revision`, `document_archive`, `approval_archive`, `decision_archive` and effective `selection_filters`. The JSON envelope stays at version 1. Older saves remain readable without rewriting; missing event fields receive defaults when the next turn starts.

The DAO uses standard JSON types, with explicit validation of the format and timestamps. Unsupported Python objects and non-finite floats are rejected with a clear error, never hidden with `default=str`. The current event data and financial outputs already use JSON-compatible types.

```python
from dao.conversation_dao import (
    create_conversation, save_conversation, list_conversations, load_conversation,
)
from controller.event_controller import run_conversation, continue_conversation

# New execution, including automatic saving (this calls the configured models):
conversation = run_conversation("Organise une soirée à Namur.")

# Read-only operations; no agents or model configuration required:
history = list_conversations()  # {"conversations": [metadata...], "errors": [diagnostics...]}
saved = load_conversation(conversation["conversation_id"])

# Explicit new message in the same conversation (may call the configured models):
saved = continue_conversation(saved, "Ajoute un traiteur.")

# Lower-level creation and writing, without agents:
draft = create_conversation("Une nouvelle demande")  # in-memory record only
path = save_conversation(draft)
```

`save_conversation`, `list_conversations`, `load_conversation` and `run_conversation` accept an optional `directory` argument, useful for tests or another local frontend. `save_conversation` returns the final `Path`; `load_conversation` returns independent data; `list_conversations` returns metadata sorted by creation date, newest first, and separate errors for unreadable files. `ConversationError` reports invalid IDs, missing files, corrupt/incompatible JSON and storage failures. IDs cannot contain arbitrary paths; symbolic links to conversation files are rejected.

### Saving, errors and resuming

The controller saves each new message with status `running` before any LLM call, then saves the complete, partial, waiting, refused or failed result. A handled interruption preserves decisions already available. Each save writes a temporary file in the same directory, flushes it and atomically replaces the destination. An initial storage failure prevents execution. If the final write fails, the terminal still displays the in-memory result and explicitly reports the storage failure; the previous valid file remains available.

An abrupt process termination may leave only the initial `running` record: there is no checkpoint after every tool call and no automatic resumption. Reopening displays only what was actually saved, including a notice for an unfinished run. It does not modify the file, even its modification timestamp. Corrupt files are reported individually without hiding valid conversations. This local version has no database, locking for concurrent writers, authentication or multi-user support.

No trace listener is used for persistence. Prompts, reasoning, API keys, `.env` contents, model usage and technical logs are not copied into conversations. Existing technical logs remain in `data/logs/`; conversation files contain user-facing text and event decisions. This is an unencrypted local history, not a remote service.

The DAO and controller have no Rich, terminal input or Streamlit dependencies. A future UI can reuse `run_conversation` and `continue_conversation` (both accept `directory`) and submit answers from widgets. Questions and approvals are stored as pending state; no interactive agent remains blocked on terminal `input()`. Streamlit itself is not implemented.

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

One correction is allowed for an invalid structured answer. Provider errors, `content_filter` and explicit refusals are distinguished from invalid JSON and are not repaired by reprompting. Identical `tool_use_failed` requests are not retried. Completed actions use the checked Python summary without a redundant synthesis call. In conversation mode, a direct Supervisor answer without actions is retained. Standalone specialists retain their final turn when necessary, within the shared budget. On leaving the menu, CLI exit codes reflect the last interaction: `0` for success, a pending human answer or consultation only; `2` for a partial/refused result; `1` for configuration/execution/storage failure; and `130` for interruption.

The user summary displays local dates and times, venue and supplier names, offer descriptions, calculated amounts and unresolved tasks. Venues have no supplier exclusivity or accessibility rules. Internal IDs remain in `event_data`; missing catalogue references are explicitly marked for verification. Availability and incomplete pricing remain conditional.

The existing positional calls `Agent.run(request)` and `Agent.as_tool(name, description)` remain valid. Optional keyword arguments connect validation, shared counters and completion checks. Decision-Making delegation now requires `category` (`venue`, `catering`, `provider`) alongside `demande`; catalogue filters now match the simplified fields documented above.

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

Python presents the consolidated plan before any email generation and saves the pending approval. A refusal preserves the plan and stops Writer. Acceptance is recorded in `event_data.plan_approval` and permits Writer to generate the draft. Writer's existing `ask_user` tool saves the exact draft as a separate pending approval, then immediately returns to the interface. On the next yes/no message, Python stores the exact text and approval status in `event_data.documents`, including refused drafts, with no further LLM call.

An edit during validation is handled as an explicit revision, never implicit approval. Material changes clear current approval and archive prior documents and agreements with their revision. A new email requires approval of the new plan. No email sending or booking mechanism is implemented.

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

The catalogue cannot confirm availability. All catalogue prices are fictitious final TTC prices, with no travel or VAT supplement. Totals use the selected venue tariff, meal headcount and technical kit price. Unsupported constraints and the interpretation of natural language still rely on the Supervisor; qualitative selection remains an LLM task.

Run all tests without paid calls:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests block network access and mock LLM/API responses. They cover direct delegations without configuration, Namur, partial Planning failure, limited budgets, venue exclusivities, reversed ordering, deadlines/logistics, invalid outputs, duplicate work, limits, presentation, successive independent runs and email refusal. The mocked Namur scenario completes in 7 LLM calls and 4 delegations after removing the redundant synthesis call; this is a deterministic regression check, not a real-provider token benchmark. See `To Do.md` for executed checks and remaining real-LLM validation.

---

## 13. Project Status

**Status: In development**

This project is being developed as part of an academic course on AI agents and multi-agent orchestration.

Conversations can be continued, reopened and revised through the Rich terminal while keeping the five AI agents, ReAct, `as_tool()` and the existing JSON DAO. Regression tests simulate the twenty requested conversation scenarios with network access blocked. Actual model interpretation, qualitative choices, token consumption and the full interactive journey with a real LLM still require a separate real-provider trial; these changes were tested without paid calls. Multiple simultaneously active email drafts and Streamlit remain outside this implementation.

---

## 14. Authors

- [Florian Samain](https://github.com/NwaSet)
- [Yohann Ducourtieux](https://github.com/Nhanyo)

---

## 15. License

See the [LICENSE](LICENSE) file for licensing information.
