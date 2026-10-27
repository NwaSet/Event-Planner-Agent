# Event-Planner-Agent

Academic Python event planner with one LLM supervisor and five LLM specialists.
Python searches local catalogues and validates calculations; agents interpret
requirements, compare trade-offs and select proposals. No bookings or emails are sent.

## Install and run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python main.py
```

Configure `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` in `.env` for your provider.
Never commit credentials. The terminal supports creating, opening, renaming,
archiving and deleting conversations. Chat commands: `/back`, `/history`,
`/usage`, `/help`, `/exit`.

Example: `Organise une conférence à Namur, dans un rayon de 30 km, pour 200
personnes. Budget total : 10000 EUR. Cherche une salle à moins de 2000 EUR TTC.`
Add the date, requested configuration and service needs when known. Missing
information produces clarification questions; a proposal is not a reservation.

## Files and execution

| Module | Responsibility |
| --- | --- |
| `main.py` | Initialize persistence, tracing and the terminal |
| `view/view.py` | Rich menus, conversation, results and usage tables |
| `controller/event_controller.py` | Conversation operations and service calls |
| `services/event_orchestrator.py` | Central event state, automatic ID storage, delegation reuse and saving the turn |
| `agents/*_agent.py` | Five short specialist prompts and their own tool sets |
| `model/agent.py` | One shared, bounded LLM/tool loop and conversation histories |
| `model/llm.py` | Existing API adapter, network errors and token accounting |
| `model/trace.py` | Technical events and credential redaction |
| `tools/search_tools.py` | Offline geographic and catalogue filtering |
| `tools/data_tools.py` | Catalogue loading and individual records; legacy pagination remains a Python API only |
| `tools/budget_tools.py`, `tools/schedule_tools.py` | Monetary and scheduling checks used by the responsible specialists |
| `tools/tool_registry.py` | Role-specific tool schemas |
| `dao/` | Atomic JSON storage, visible conversations, agent memories and traces |

The terminal calls the controller, which calls the supervisor. The supervisor
coordinates five genuine LLM specialists without rechecking their business decisions.
BudgetAgent computes costs, VenueAgent selects venues, ProviderAgent selects technical
kits or catering, ScheduleAgent organizes the timeline and WriterAgent writes summaries.
All six use the same bounded `Agent` loop, with separate prompts, tools and histories.

Search specialists return a common compact contract:

```json
{"status": "selected", "items": [{"id": "002", "option_id": 6}]}
```

`option_id` is optional for venues; it selects a kit/formula for suppliers. Other
statuses are `not_found` and `error`. Ordered alternatives put the preferred option
first. No names, addresses, prices or descriptions are copied into the selection.
Python checks only the format and existence of references, then immediately saves
them. Capacity, suitability and budget decisions belong to the relevant specialist.
Budget and schedule return their own calculated JSON objects; WriterAgent returns
readable text. The supervisor finishes with ordinary text: no `submit_result`,
manual recopying of decisions or extra protocol-repair LLM call.

`get_item(category, item_id, option_id=None)` reuses existing catalogue readers.
Budget, schedule and writing agents can resolve only the details they need. Selecting
an option omits other kits/formulas but retains shared terms such as travel fees.
These agents have no catalogue search tools. `meal=false` prevents catering delegation;
technical tasks do not expose catering search tools. Drinks, bar and artists are
separate categories with no current catalogue, and must not be replaced by meals.

## Local search

```python
from tools.search_tools import search_venues, search_caterers, search_providers

venues = search_venues(city="Namur", radius_km=30, min_capacity=200,
                       max_price=2000, configuration="seated", limit=15)
caterers = search_caterers(city="Gembloux", radius_km=30, attendance=100,
                           max_price_per_person=70, full_meal=True,
                           dietary=["vegetarien"])
providers = search_providers(city="Gembloux", radius_km=30,
                             max_kit_price=2000, services=["son", "light"])
```

Each search reads the local JSON, applies objective filters to all records, sorts
by distance then price and source ID, and returns at most 20 compact candidates
(default 15). Without a city, sorting uses price then ID. With a city and no radius,
the default is explicitly returned as 30 km. Results include the total match count,
actual radius, missing coordinates, price basis and limitations. No automatic
radius expansion occurs; an empty result can suggest a larger radius for discussion.

Venue capacity uses one indoor/outdoor and seated/standing configuration, capped
by the site's maximum. Price is the selected day/evening rental plus mandatory
cleaning, TTC; deposits are separate. Catering filters individual formulas by
attendance, service, full meal and dietary flags. Its ceiling is per person TTC,
excluding extras. Technical suppliers use an HT kit ceiling and son/light/video
presence, without assuming a guest capacity. Equipment levels are not quality
ratings. Unknown mandatory values are not assumed to satisfy a filter.

Only `search_*` and individual `get_venue`, `get_provider`, `get_caterer` retrievals
are exposed to the relevant agents. The old full-record page tools and catalogue
metadata traversal are no longer LLM tools. Fetch a promising record by ID to inspect
logistics, exclusivity, extras and other detailed constraints before selecting it.
Subjective comparisons and the final choice remain the specialist LLM's responsibility.

## Offline geography and data provenance

`data/cities.json` contains 53 selected localities with real source coordinates,
postal codes and French/Dutch aliases. Names are matched without accents, punctuation
or case differences. Postal codes disambiguate Aalst (9300) and Halle (1500).
Coordinates were copied from the [GeoNames Belgian postal-code extract](https://download.geonames.org/export/zip/BE.zip)
on 2026-10-27, under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
Attribution: [GeoNames](https://www.geonames.org/).
The [source documentation](https://download.geonames.org/export/zip/readme.txt)
describes these as estimated WGS84 locality coordinates, not supplier addresses.

Haversine computes distances directly; there is no stored distance matrix and no
runtime geocoding or internet request. These are approximate straight-line distances,
not driving distances, travel times or billable mileage. Multiple supplier branches
are checked separately and the nearest known branch is used. Unknown branches are
reported. Andrimont's supplied coordinate needs verification for the Dison location;
no position is assumed for the merged locality Nazareth-De Pinte. Both are reported
as unresolved. Other unlisted user cities also produce an explicit missing-coordinate
result, rather than guessed positions. Add sourced entries to extend coverage.

The venue and caterer offers are fictional school-project data. Technical supplier
names/locations are source data, but kits and prices are simulated. There are no
live availability calendars, current quotes, weather integration or verified email
contacts. Venue/catering prices are TTC; technical prices are HT and cannot be
silently combined. The existing monetary tools validate full line amounts without
inventing quantity extensions or tax rates.

## Central event state, memory and accounting

Each conversation in `logs/conv.json` now has a persistent `event_state`:

```json
{
  "event": {"city": "Namur", "attendance": 100, "budget": 10000, "meal": false},
  "selections": {
    "venue": [{"id": "salle_054", "option_id": null}],
    "technical": [{"id": "002", "option_id": 6}]
  },
  "budget": null,
  "schedule": null,
  "summary": null,
  "tasks": {}
}
```

`update_event(changes)` merges user requirements only, preserving omitted fields and
explicit false values. Specialist results update `selections`, `budget`, `schedule`
or `summary` automatically, without another LLM call. `tasks` contains small input
fingerprints, result statuses and stale flags for reuse. Changed event constraints
or upstream selections invalidate affected results; older results remain visible
and marked stale. An explicit `refresh_reason` permits a user-requested replacement
with unchanged constraints. Failed replacements retain the previous selection.
`not_found` results are also reused instead of repeating the same failed search.

`EventOrchestrator.get_planning_context(conversation_id)` reads this dictionary
without an LLM call. A new service instance or application restart reloads it.
Conversation IDs isolate all state and histories. A summary uses saved IDs and
WriterAgent, not fresh searches. The first candidate is preferred; alternative
venues/kits are not automatically all added to a budget.

Existing conversations are read without destructive migration: legacy planning
snapshots and recognizable structured specialist selections can initialize the
state. Arbitrary prose is not mined for guessed IDs or decisions. The new field
is saved when the conversation is used. Full agent histories and technical traces
remain in their existing files, with credential redaction and atomic JSON writes.
Writes are individually atomic, not one transaction for the entire interaction;
use one application process per log directory.

The supervisor receives the central state and at most two recent user/final-response
turns; specialists receive a mission, event constraints and relevant IDs/calculations.
Old tool research is not replayed. The message budget remains 32000 serialized
characters (tool schemas are additional). Complete tool exchanges are retained or
removed as units, never sliced JSON. Identical read-only searches in an agent run
reuse their observations. This avoids repeated Python work; it does not manufacture
an LLM answer or bypass the specialist's comparison.

Defaults: 12 supervisor LLM iterations, 8 total specialist delegations, at most 2
per specialist per interaction. Budget/venue/provider allow 8 iterations, schedule
6 and writer 3. No automatic protocol correction or retry of an empty/truncated
answer is added. The existing bounded provider rate-limit retry policy remains.

The API adapter now preserves `finish_reason` in messages and usage metadata.
`length`, filtered/interrupted responses, empty output, malformed tool envelopes
and tool-call JSON delivered as ordinary text cannot be treated as successful tool
execution. Truncated tool calls are not executed. Available text/work can be returned
as partial; saved selections remain available if the supervisor fails. Plain prose
announcing an action never executes a tool, and no deterministic parser can certify
all claims in a model's prose. Live-model recommendation quality is not guaranteed.

The terminal displays reported input/output/total tokens, calls per agent and
actual delegation count. `/usage` shows cumulative conversation/event usage in this
process. Cached delegations make no specialist LLM calls. Usage is counted once
using the existing request-ID journal, including reported usage for truncated
responses. Unknown usage is marked; counters reset after restart and failed requests
can incur unreported charges.

## Observed failures and measurements

The inspected conversation contained usable supervisor prose rejected for missing
`submit_result`, selected venue IDs left only in specialist history, and several
empty responses at exactly the configured 2048 output-token limit. The previous
adapter discarded `finish_reason`, so those logs alone cannot prove why each
response was empty. Future traces retain that reason. The output ceiling was not
silently increased to spend more tokens or mask missing responses.

No paid/live LLM benchmark was run. Measurements are serialized characters and
mocked call counts, not a claim about billed token savings:

- A saved detailed VenueAgent response occupied 5082 characters. Its three IDs
  require 153 characters in the normalized selection contract (about 97% less).
- Ordinary final prose now needs one supervisor call. Previously it triggered a
  protocol correction; a fully submitted final response also needed a tool call
  followed by a final-text call. These extra protocol calls are removed.
- Mocked restart tests show a repeated venue delegation with identical constraints
  invokes the specialist zero times. Summary tests invoke only supervisor/writer.
- Earlier local-search reductions remain: Namur / 30 km / 200 seated / 2000 EUR
  returns 2388 characters for three candidates, versus 214488 for the catalogue.

The supervisor's business validation, submission checks, repair prompt, repair run
and history-exhaustion inference were removed. The generic agent reports execution
status directly. No new state manager, DTO hierarchy, persistence file, framework
or dependency was added. The existing DAO stores a dictionary. The obsolete empty
`model/__inti__.py` was removed during the preceding cleanup.

## Tests

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```

Tests mock LLM calls, use temporary persistence and never read API credentials.
They check delegation, actual search/tool execution, geographic boundaries,
category-specific filters, bounded JSON/context, complete tool exchanges, saved
history, automatic ID persistence, restart reuse, no-meal requests, replacement,
plain final responses, truncation, malformed calls, loop limits and usage accounting.
They cannot establish the quality of choices made by a live model or real supplier
availability. Source coordinate precision and missing localities remain limitations.

## Authors and license

- [Florian Samain](https://github.com/NwaSet)
- [Yohann Ducourtieux](https://github.com/Nhanyo)

See [LICENSE](LICENSE) for project licensing. GeoNames data retains its separate
CC BY 4.0 attribution above and in `data/cities.json`.
