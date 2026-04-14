<p align="center">
  <img src="https://raw.githubusercontent.com/tolism/mneme/main/assets/logo.png" alt="Mnemosyne" width="400">
</p>

<h1 align="center"></h1>



A CLI tool that turns your documents into a searchable second brain. Drop files in, get a structured knowledge layer out -- browsable by humans in Obsidian, queryable by machines in under 5ms.

```bash
pip install mneme-cli
mneme new ~/projects/my-research --name "My Research" --client acme-corp
cd ~/projects/my-research
mneme ingest proposal.pdf acme-corp
mneme search "delivery timeline"
```

One installed `mneme` CLI can serve many independent workspaces. Switch between them by `cd`-ing, exporting `MNEME_HOME`, or passing `--workspace /path/to/ws`.

That's it. Your knowledge compounds instead of decaying.

---

## Why

You're building a medical device. You have a risk analysis in a PDF, user needs in a spreadsheet, meeting notes in markdown, and 47 requirements in a CSV. An auditor asks "show me the trace from hazard HAZ-001 to the test that verifies its mitigation." You spend two hours searching folders.

Mneme fixes this:

```bash
# Import everything
mneme ingest risk-analysis.pdf cardio-monitor
mneme ingest-csv user-needs.csv cardio-monitor --mapping user-needs
mneme ingest-csv risk-register.csv cardio-monitor --mapping risk-register

# Answer the auditor in 2 seconds
mneme trace show cardio-monitor/haz-001 --direction forward
#   haz-001 (Electrical Shock)
#     mitigated-by -> rma-003 (Insulation Barrier)
#       implemented-by -> req-007 (Double Insulation)
#         verified-by -> test-042 (Dielectric Strength Test)

# Find gaps before the auditor does
mneme trace gaps cardio-monitor
#   Requirements with no verification: req-011, req-023
#   Hazards with no mitigation: haz-009
```

Every document ingested once. Every trace link tracked. Every vocabulary term harmonized. Every gap found automatically.

No databases. No servers. No infrastructure. Plain markdown files + JSON schemas that any system can read.

---

## Install

```bash
pip install mneme-cli
```

Or from source:

```bash
git clone https://github.com/tolism/mneme.git
cd mneme
pip install -e .
```

You now have the `mneme` command globally. Verify with `mneme --help`.

**Optional:** For PDF support, `pip install "mneme-cli[pdf]"`. For everything, `pip install "mneme-cli[all]"`.

**Requirements:** Python 3.9+. Works on macOS, Linux, Windows.

---

## Quick Start

```bash
# Scaffold a new workspace (from anywhere)
mneme new ~/projects/my-project --name "My Project" --client client-a

cd ~/projects/my-project

# Ingest some documents
mneme ingest report.pdf client-a
mneme ingest meeting-notes.md client-a

# Search across everything
mneme search "quarterly budget"

# Check health
mneme stats

# Launch the web dashboard
python -m mneme.server    # http://localhost:3141
```

### Run mneme against any workspace

```bash
mneme --workspace ~/projects/parkiwatch stats     # one-shot
export MNEME_HOME=~/projects/parkiwatch           # sticky for the shell
mneme stats
```

One installed CLI serves many projects — each workspace is just a directory.

---

## CLI

| Command | What It Does |
|---|---|
| `mneme new <dir>` | Scaffold a new workspace from the bundled template |
| `mneme init` | Scaffold a workspace in cwd (legacy) |
| `mneme --workspace <dir>` | Run any command against a specific workspace |
| `mneme ingest <file> <client>` | Ingest a source document |
| `mneme resync <file> <client>` | Re-ingest an updated source via 3-way merge, preserving hand edits |
| `mneme resync-resolve <client/page>` | Finalize a conflicted resync after editing out markers |
| `mneme search "<query>"` | Search across all layers |
| `mneme draft --doc-type <t> --section <s> --client <c>` | Build a *write packet* for an LLM agent to produce one section |
| `mneme validate writing-style <page>` | Build a *review packet* for an LLM agent to grade a page |
| `mneme tags suggest <page>` | Build a *tag packet* for an LLM agent to choose tags |
| `mneme tags apply <page> --add t1,t2 --remove t3` | Atomic tag update (frontmatter + schema + search index) |
| `mneme agent plan --goal "..." --doc-type <t> --client <c>` | Generate a deterministic TODO plan from the active profile |
| `mneme agent next-task` | Return the next ready task in the active plan |
| `mneme agent task-done <id>` | Mark a task as done |
| `mneme sync` | Sync wiki pages to FTS5 search index |
| `mneme reindex` | Rebuild search index from wiki pages |
| `mneme drift` | Detect layer desynchronization |
| `mneme stats` | Health overview |
| `mneme repair` | Fix corrupted archives |

**Formats:** `.md`, `.txt`, `.pdf`, `.xlsx` (with `pip install "mneme-cli[xlsx]"`)

---

## For LLM agents

If you are an LLM agent driving mneme on a user's behalf — read **[AGENTS.md](AGENTS.md)** first. It is the canonical contract for the agent loop, the standard task templates (DVR, CER, risk file, resync, migration, pre-submission), the sub-agent spawning patterns, and the hard rules you must never violate.

The 30-second version of the agent loop:

```bash
# 1. Generate a plan from the active profile
mneme agent plan --goal "Produce a Design Validation Report" \
                 --doc-type design-validation-report \
                 --client tda

# 2. Walk the plan one task at a time
mneme agent next-task        # returns a self-contained task envelope
# (do the work the envelope describes -- usually `mneme draft` or
#  `mneme validate writing-style`, then write or grade prose)
mneme agent task-done section-context

# 3. Repeat until done
mneme agent next-task
# ...

# 4. Inspect progress at any time
mneme agent show
mneme agent list
```

Mneme generates the plan deterministically from the active profile's section_notes. Tasks have a dependency graph; `next-task` only returns ones whose dependencies are satisfied. The plan and per-task state are persisted under `<workspace>/.mneme/agent-plans/` (gitignored). Mneme does not call any LLM — you (the agent) do the writing. Mneme assembles the contracts.

---

## End-to-end example: from raw documents to a tagged, searchable, validated knowledge base

A realistic walkthrough showing how the human, the CLI, and the LLM agent collaborate. Suppose you're building a knowledge base for **Parkiwatch**, a medical device for Parkinson's monitoring.

### Step 1 — Scaffold a workspace (human, one-time)

```bash
mneme new ~/projects/parkiwatch --name Parkiwatch --client parkiwatch --profile eu-mdr
cd ~/projects/parkiwatch
```

Creates the workspace tree, sets the EU MDR writing-style profile, and initializes empty schema files.

### Step 2 — Ingest source material (human)

```bash
# Drop a folder of source documents into inbox/, then bulk-process
cp -r ~/Downloads/parkinson-research/* inbox/
mneme tornado --client parkiwatch

# Or ingest individual files
mneme ingest research-paper.pdf parkiwatch
mneme ingest-csv risk-register.csv parkiwatch --mapping risk-register
mneme ingest spec-table.xlsx parkiwatch          # .xlsx renders sheets as markdown tables
mneme ingest-dir docs/ parkiwatch --recursive    # walk subdirectories
```

What happens per ingest: source file → wiki page in `wiki/parkiwatch/` → frontmatter with auto-extracted entities → entry in `index.md` → row in the FTS5 search DB → log entry.

### Step 3 — Tag the new pages (LLM agent)

The new pages have only the auto-applied `parkiwatch` client tag. The agent now adds meaningful tags:

```bash
# For each new page, the agent runs:
mneme tags suggest parkiwatch/research-paper > /tmp/packet.md
```

The packet contains the page body, the current tag taxonomy (every tag in the workspace + usage counts), and a ready-to-paste prompt. **The LLM reads the packet** — it understands the content and decides on tags, preferring existing taxonomy entries when they fit. The LLM's response is JSON:

```json
{"tags": ["clinical-trial", "iso-13485"], "new_tags": ["bradykinesia-detection"]}
```

The agent then runs:

```bash
mneme tags apply parkiwatch/research-paper \
  --add clinical-trial,iso-13485,bradykinesia-detection
```

Atomic operation: rewrites the wiki page frontmatter, updates `schema/tags.json`, re-indexes the page in FTS5 (so search picks up the new tags immediately), appends a log entry. **Repeat for every page** — the taxonomy grows, and subsequent pages tend to reuse existing tags (consistency).

### Step 4 — Search the knowledge base (anyone)

```bash
mneme search "bradykinesia"                              # BM25 + Porter stemming
mneme search "clinical evaluation" --client parkiwatch   # client-scoped
```

Sub-millisecond. Returns the page title, snippet (with `<b>highlights</b>`), tags, and BM25 score.

### Step 5 — Produce a regulatory deliverable (LLM agent driving the agent loop)

```bash
# Generate a deterministic plan from the active profile
mneme agent plan --goal "produce a Design Validation Report" \
                 --doc-type design-validation-report \
                 --client parkiwatch
# → 15 tasks: 11 section drafts + assemble + harmonize + review + submission-check

# Walk the plan
mneme agent next-task
# → Task: section-purpose-and-scope
#   next_command: mneme draft --doc-type design-validation-report \
#                             --section purpose-and-scope --client parkiwatch

mneme draft --doc-type design-validation-report \
            --section purpose-and-scope --client parkiwatch \
            --query "purpose scope intended use" \
            --out /tmp/write-packet.md

# The LLM reads /tmp/write-packet.md (which includes wiki search hits as evidence,
# the profile's writing-style rules, and a write prompt) and produces the section.
# The agent writes the section to wiki/parkiwatch/design-validation-report.md.

mneme agent task-done section-purpose-and-scope

# ... repeat for each section ...

# After all sections drafted:
mneme harmonize --client parkiwatch --fix       # mechanical vocabulary swap
mneme validate writing-style parkiwatch/design-validation-report > /tmp/review.md
# The LLM reads /tmp/review.md, critiques every section, applies fixes in place
mneme agent task-done review-page

# Submission readiness
mneme validate consistency --client parkiwatch  # cross-doc version checks
mneme trace gaps parkiwatch                     # find broken trace chains
mneme trace matrix parkiwatch --csv --out trace-matrix.csv  # for the DHF
mneme snapshot parkiwatch                       # versioned audit zip
```

### Who does what

| Layer | Responsibility |
|---|---|
| **Human** | Drops sources, runs commands, reviews diffs, ships the deliverable |
| **mneme CLI** | Deterministic infrastructure: parses files, builds packets, indexes, traces, harmonizes vocabulary, generates plans, atomic state updates |
| **LLM agent** | All reasoning: classifying entities, choosing tags, drafting prose, grading writing style, deciding when a chain is complete |

mneme never calls an LLM. The LLM never bypasses mneme's atomic operations. They meet at the packet boundary.

---

## How It Works

```
    Your Document
         |
         v
    mneme ingest
         |
         +---> Wiki Layer (markdown, Obsidian-compatible)
         |       Frontmatter, citations, [[wikilinks]]
         |       You read and browse here
         |
         +---> Search Index (SQLite FTS5)
         |       BM25 ranking, Porter stemming
         |       Sub-millisecond queries, zero dependencies
         |
         +---> Schema Layer (JSON)
                 entities.json - people, companies, products
                 graph.json   - relationships between entities
                 tags.json    - taxonomy
```

Every `mneme ingest` writes the wiki page and updates the search index atomically. `mneme drift` catches desync. `mneme reindex` rebuilds the index from wiki pages.

**Zero external dependencies for search.** SQLite FTS5 is built into Python's stdlib — no install, no API key, no capacity limit.

---

## Obsidian Integration

A mneme workspace *is* an Obsidian vault. The wiki pages use YAML frontmatter and `[[wikilinks]]`, so Obsidian indexes everything natively.

**Open a workspace as a vault:**

1. Open Obsidian → *Open folder as vault* → select your workspace directory (e.g. `~/projects/parkiwatch`)
2. Obsidian creates `.obsidian/` inside the workspace on first open — this is safe and mneme ignores it
3. Browse `wiki/` in the file explorer; click any page to render with backlinks, graph view, and tag search

**Recommended Obsidian settings:**

- **Files & Links → Default location for new notes:** `wiki/{default-client}/`
- **Files & Links → New link format:** `Relative path to file`
- **Files & Links → Use [[Wikilinks]]:** ON
- **Files & Links → Detect all file extensions:** OFF (keeps `sources/` archive out of the graph)

**Useful community plugins:**

| Plugin | Why |
|---|---|
| **Dataview** | Query frontmatter: list all pages with `type: hazard`, `confidence: low`, etc. |
| **Templater** | Paste mneme page frontmatter from a snippet |
| **Tag Wrangler** | Visualise the same tags mneme tracks in `schema/tags.json` |
| **Graph Analysis** | See the entity relationships mneme builds in `schema/graph.json` |

**Workflow:**

```bash
# Ingest new docs from the CLI
mneme ingest meeting.pdf parkiwatch

# Obsidian auto-detects the new wiki page
# Read, link, and annotate in Obsidian
# mneme lint catches dead links on your next run
mneme lint
```

Sync the workspace via Dropbox, iCloud, or git and you have multi-device Obsidian + mneme.

---

## Profiles (and custom profiles)

A profile defines the vocabulary and document structure rules for a regulatory framework. mneme ships two bundled profiles:

| Profile | Use when |
|---|---|
| `eu-mdr` | EU Medical Device Regulation (2017/745) -- 15 vocabulary rules, 6 section templates |
| `iso-13485` | ISO 13485:2016 QMS -- 13 vocabulary rules, 6 section templates |

Activate one in any workspace with `mneme profile set eu-mdr`. From then on, `mneme harmonize` enforces vocabulary, `mneme validate writing-style` builds an LLM review packet for prose, and `mneme validate consistency` checks cross-document standard versions.

### Adding your own profile

Profiles are just JSON files in `<workspace>/profiles/`. **No reinstall, no rebuild, no PR to mneme.** Drop a file in, activate it, you're done.

```bash
# 1. mneme new already creates the profiles/ folder for you
mneme new ~/projects/parkiwatch --name Parkiwatch --client parkiwatch
cd ~/projects/parkiwatch

# 2. Drop your profile in (use any text editor or this heredoc).
#    Profiles are markdown with YAML frontmatter.
cat > profiles/parkiwatch-qms.md <<'EOF'
---
name: Parkiwatch QMS
description: Internal quality framework for the Parkiwatch product line
version: 1.0
tone: formal
voice: passive-for-procedures
trace_types: [derived-from, implemented-by, verified-by]
requirement_levels:
  shall: mandatory
  should: recommended
vocabulary:
  - use: parking violation
    reject: [parking ticket, infraction]
  - use: enforcement officer
    reject: [meter maid, warden]
---

# Principles

- Be specific. Cite the policy clause.
- Auditable: every claim must trace to a controlled record.

# Terminology

| Use | Instead of | Why |
|---|---|---|
| parking violation | parking ticket, infraction | Internal Parkiwatch convention. |

# Document Type: incident-report

Standard parking incident structure used by all enforcement officers.

## Section: evidence

Photo evidence with timestamp and GPS coordinates is mandatory.
EOF

# 3. Activate and verify
mneme profile set parkiwatch-qms
mneme profile show
#   Active profile: Parkiwatch QMS

# 4. Use it
mneme harmonize parkiwatch          # flag "parking ticket" -> should be "parking violation"
mneme harmonize parkiwatch --fix    # auto-fix vocabulary
mneme validate writing-style parkiwatch/incident-001 > review.md  # paste into Claude
```

### How resolution works

When you run `mneme profile set <name>`, mneme looks in two places, in order:

1. **First:** `<workspace>/profiles/<name>.md` (your local profile)
2. **Then:** `<installed-mneme>/profiles/<name>.md` (the bundled `eu-mdr` / `iso-13485`)

The first one wins. So you can:

- **Add a brand-new framework** mneme doesn't ship -- just give it a unique name (e.g. `parkiwatch-qms.md`, `acme-internal.md`)
- **Override a bundled framework** with project-specific tweaks -- create your own `eu-mdr.md` in the workspace and it shadows the bundled one for that project only

The same shadowing rule applies to CSV column mappings under `<workspace>/profiles/mappings/`, used by `mneme ingest-csv`. Mappings are still JSON because they are programmatic, not prose.

If neither file exists, you get a clear error listing both paths it checked.

### What goes into a profile

A profile is a markdown file with YAML frontmatter. The frontmatter carries the structured fields (`vocabulary`, `trace_types`, `tone`, etc.) and the body carries the writing-style prose under recognized H1 headings.

| Frontmatter field | What it does | Used by |
|---|---|---|
| `name`, `description`, `version` | Display metadata | `mneme profile show` |
| `vocabulary[].use` / `.reject[]` | Terminology swaps | `mneme harmonize` (mechanical) |
| `requirement_levels` | Reserved words (`shall`, `should`, `may`) | Documentation |
| `trace_types` | Allowed relationship types for trace links | Documentation |
| `tone`, `voice`, `citation_style` | Style hints | `mneme profile show` |
| `placeholder_for_missing_refs` | Marker token (e.g. `[TO ADD REF]`) | LLM agent |

| Body H1 heading | What it becomes |
|---|---|
| `# Principles` | Top-level principles (bullets) |
| `# General Rules` | Cross-cutting writing rules (bullets) |
| `# Terminology` | A 3-column markdown table: Use / Instead of / Why |
| `# Framing: <context>` | One worked example: **Wrong:** / **Correct:** / **Why:** blocks |
| `# Document Type: <slug>` | A document type description; nested `## Section: <slug>` blocks become per-section guidance |
| `# Submission Checklist` | Pre-submission go/no-go items (bullets) |

**Important:** profiles do NOT enforce a list of required headings. Mechanical heading checks were removed because they don't reflect what regulatory reviewers actually care about. Instead, use `mneme validate writing-style <page>` to build a review packet that an LLM agent grades against the full style guide.

See `EXAMPLES.md` Example 13 for a full walkthrough with a real Parkiwatch scenario. The bundled `eu-mdr.md` and `iso-13485.md` profiles inside the installed package are good starting templates -- copy one and edit it.

---

## Web Dashboard

`python -m mneme.server` -- opens at `http://localhost:3141`

- **Dashboard** -- stats, per-client counts, activity log
- **Search** -- dual-layer results with source attribution
- **Wiki** -- browse all pages with rendered markdown
- **Entities** -- filterable table of extracted entities
- **Health** -- drift status, sync state

---

## When You Need This

| Scale | Search performance |
|---|---|
| 5 docs | Sub-millisecond |
| 50 docs | Sub-millisecond |
| 500 docs | Sub-millisecond, BM25 ranked |
| 5,000 docs | A few ms, still ranked by relevance |
| 50,000 docs | Tens of ms |

SQLite FTS5 scales transparently. No tuning, no capacity limits.

---

## Project Structure

```
mneme/
  sources/        Raw documents (immutable, never modified)
  wiki/           Markdown knowledge pages (Obsidian-compatible)
  schema/         entities.json, graph.json, tags.json
  search.db       SQLite FTS5 search index
  core.py         Engine (ingest, search, sync, drift, repair)
  config.py       Configuration
  server.py       Web dashboard
  index.md        Master page catalog
  log.md          Activity timeline
```

---

## Downstream Use

Mneme outputs plain files -- markdown and JSON. Any system can read them. The CLI is designed to be called programmatically by other applications.

**Next up:** Mneme as the knowledge backend for a QMS (Quality Management System) -- quality documentation, audit trails, compliance evidence, all searchable.

---

## Releasing (maintainers)

Mneme ships to PyPI as `mneme`. To cut a new release:

```bash
# 1. Bump the version in mneme/__init__.py and pyproject.toml
# 2. Install release tooling
pip install -e ".[release]"

# 3. Dry run to TestPyPI first
scripts/release.sh test              # bash (macOS/Linux/WSL)
scripts\release.ps1 test             # PowerShell (Windows)

pip install --index-url https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple/ mneme

# 4. Production
scripts/release.sh prod              # bash
scripts\release.ps1 prod             # PowerShell
```

The script cleans `dist/`, runs `python -m build`, validates with `twine check`, and uploads.

You'll need a PyPI API token in `~/.pypirc`:

```ini
[distutils]
index-servers =
    pypi
    testpypi

[pypi]
username = __token__
password = pypi-AgEI...           # from https://pypi.org/manage/account/token/

[testpypi]
repository = https://test.pypi.org/legacy/
username = __token__
password = pypi-AgENd...          # from https://test.pypi.org/manage/account/token/
```

---

## Credits

This project builds on two foundational ideas:

- **LLM Wiki pattern** by [Andrej Karpathy](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) -- the insight that LLMs should build and maintain a persistent, compounding wiki instead of re-deriving answers from raw documents on every query
- **SQLite FTS5** -- the world's most-deployed embedded database, with built-in BM25 full-text search
- **Original implementation** -- [tashisleepy/knowledge-engine](https://github.com/tashisleepy/knowledge-engine) -- the first version that fused both patterns into a dual-layer bridge

---

## License

MIT
