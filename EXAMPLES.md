# EXAMPLES.md - mneme Usage Guide

Real-world workflows, core concepts, and entity types used in mneme.

---

## Core Concepts

### What is a "Second Brain"?

A second brain is a persistent, searchable knowledge layer that sits between you and your documents. You feed it raw information once. From that point, you query it instead of re-reading source files. Knowledge compounds instead of decaying.

Mneme is the CLI tool that builds and maintains this second brain.

### The Three Layers

| Layer | Location | Who reads it | Purpose |
|---|---|---|---|
| **Sources** | `sources/` | Nobody (archive) | Immutable evidence. Raw PDFs, CSVs, meeting notes. Never modified. |
| **Wiki** | `wiki/` | Humans | Structured markdown pages. Browse in Obsidian, VS Code, or any editor. |
| **Memory** | `memvid/` | Machines | Semantic search in <5ms. Embeddings in a single `.mv2` file. |

### Page Types

| Type | What it is | Example |
|---|---|---|
| `overview` | Client or project summary | `my-device/overview.md` |
| `entity` | Company, person, product, or technology | `my-device/supplier-acme-corp.md` |
| `concept` | Technical concept or methodology | `_shared/risk-management.md` |
| `source-summary` | Summary of one ingested document | `my-device/meeting-2026-03-15.md` |
| `comparison` | Side-by-side analysis | `_shared/vendor-comparison.md` |
| `deliverable` | Work product or output | `my-device/clinical-evaluation-report.md` |
| `user-need` | Stakeholder need (from CSV) | `my-device/un-001.md` |
| `requirement` | Design requirement (from CSV) | `my-device/req-003.md` |
| `hazard` | Risk register entry (from CSV) | `my-device/haz-001.md` |
| `design-spec` | Detailed design specification | `my-device/dds-015.md` |
| `verification` | Test case or verification record | `my-device/test-042.md` |

### Traceability Chains

In QMS, every item must trace forward and backward through the lifecycle:

```
User Need (UN-001)
  |
  derived-from
  v
Requirement (REQ-003)
  |
  detailed-in
  v
Design Specification (DDS-015)
  |
  verified-by
  v
Test Case (TEST-042)
  |
  validated-by
  v
Validation Record (VAL-008)
```

Risk-based trace:

```
Hazard (HAZ-001)
  |
  mitigated-by
  v
Risk Mitigation Activity (RMA-003)
  |
  implemented-by
  v
Requirement (REQ-003)
  |
  detailed-in
  v
Design Specification (DDS-015)
  |
  verified-by
  v
Test Case (TEST-042)
```

### Writing Style Profiles

Profiles enforce terminology and structure rules for regulatory frameworks:

| Profile | Use when | Vocabulary rules | Section templates |
|---|---|---|---|
| `eu-mdr` | EU Medical Device Regulation (2017/745) | 15 rules | 6 templates |
| `iso-13485` | ISO 13485:2016 QMS | 13 rules | 6 templates |

Example vocabulary enforcement:
- "product" -> should be "medical device"
- "intended use" -> should be "intended purpose"
- "company" -> should be "manufacturer"

---

## Example 1: Starting a New QMS Project from Scratch

You're building a medical device. You need to set up documentation that satisfies EU MDR.

```bash
# Step 1: Scaffold a fresh workspace with the profile baked in
mneme new ~/projects/cardio-monitor \
  --name "CardioMonitor" \
  --client cardio-monitor \
  --profile eu-mdr \
  --description "Implantable cardiac monitor - EU MDR technical file"

cd ~/projects/cardio-monitor

# Step 2: Verify the profile is active
mneme profile show
#   Active profile: EU MDR
#   Vocabulary rules: 15
#   Section templates: 6

# Step 3: Check what you have
mneme status
#   Sources: 0
#   Wiki pages: 0

# Step 4 (optional): put it under version control
git init && git add -A && git commit -m "scaffold cardio-monitor workspace"
```

One installed `mneme` CLI can serve any number of workspaces like this. To switch:

```bash
mneme --workspace ~/projects/parkiwatch stats
# or
export MNEME_HOME=~/projects/parkiwatch
mneme stats
```

---

## Example 2: Ingesting Your First Documents

You have meeting notes, a risk analysis, and a product specification.

```bash
# Ingest one at a time
mneme ingest meeting-notes-2026-03-15.md cardio-monitor
mneme ingest risk-analysis-v1.pdf cardio-monitor
mneme ingest product-spec.md cardio-monitor

# Or batch ingest a folder
mneme ingest-dir documents/ cardio-monitor

# Check results
mneme stats
#   Wiki pages: 3
#   Entities: 12
#   Tags: 4
```

Each ingest creates:
- A wiki page in `wiki/cardio-monitor/`
- Memvid frames for semantic search
- Entity entries in `schema/entities.json`
- Tag entries in `schema/tags.json`
- An index entry in `index.md`
- A log entry in `log.md`

---

## Example 3: Importing a Requirements Spreadsheet

Your systems engineer hands you a CSV with 47 user needs.

```csv
ID,Title,Description,Priority,Acceptance Criteria,Linked Requirement
UN-001,Patient Safety from Electrical Shock,Device shall not expose patient to harmful electrical currents,Critical,No current >10uA reaches patient,REQ-003
UN-002,Battery Life for Full Shift,Device shall operate 8+ hours on single charge,High,8+ hours under typical clinical load,REQ-007
...
```

```bash
# Ingest with explicit mapping
mneme ingest-csv user-needs.csv cardio-monitor --mapping user-needs

#   File: user-needs.csv (47 rows, 6 columns)
#   Mapping: user-needs
#   Type: user-need
#
#   [1/47] UN-001: Patient Safety from Electrical Shock
#          -> wiki/cardio-monitor/un-001.md (created)
#          -> trace: implemented-by -> REQ-003
#   ...
#   Pages created: 47
#   Trace links: 34

# Or let mneme auto-detect the mapping from column headers
mneme ingest-csv user-needs.csv cardio-monitor
```

This creates 47 wiki pages, each with:
- Frontmatter (title, type, client, tags, confidence)
- Body sections (summary, acceptance criteria)
- Trace links to requirements

---

## Example 4: Building Full Traceability

Import your requirements, design specs, and test cases from separate CSVs:

```bash
# Import each layer of the V-model
mneme ingest-csv user-needs.csv cardio-monitor --mapping user-needs
mneme ingest-csv requirements.csv cardio-monitor --mapping requirements
mneme ingest-csv design-specs.csv cardio-monitor --mapping dds
mneme ingest-csv test-cases.csv cardio-monitor --mapping test-cases
mneme ingest-csv risk-register.csv cardio-monitor --mapping risk-register

# Check the trace chain from a user need to its test
mneme trace show cardio-monitor/un-001 --direction forward
#   Root: cardio-monitor/un-001
#     implemented-by -> cardio-monitor/req-003
#       detailed-in -> cardio-monitor/dds-015
#         verified-by -> cardio-monitor/test-042

# Check backward from a test to its origin
mneme trace show cardio-monitor/test-042 --direction backward
#   Root: cardio-monitor/test-042
#     verifies -> cardio-monitor/dds-015
#       details -> cardio-monitor/req-003
#         implements -> cardio-monitor/un-001

# Find gaps: requirements with no tests, hazards with no mitigations
mneme trace gaps cardio-monitor
#   Requirements with no verification: req-011, req-023
#   Hazards with no mitigation: haz-009
#   User needs with no requirements: un-005

# Generate full traceability matrix
mneme trace matrix cardio-monitor
```

---

## Example 5: The Tornado Workflow (Quick Migration)

You're migrating from an old QMS system. You have 50 files in various formats -- markdown, PDFs, CSVs. You just want them all in.

```bash
# Dump everything into the inbox
cp ~/old-qms/*.md inbox/
cp ~/old-qms/*.csv inbox/
cp ~/old-qms/*.pdf inbox/

# Let tornado figure it out
mneme tornado --client cardio-monitor

#   === mneme Tornado ===
#
#   Scanning inbox/... found 50 files
#
#   [1/50] sop-design-control.md
#          Type: concept
#          Client: cardio-monitor
#          -> wiki/cardio-monitor/sop-design-control.md (created)
#          -> sources/cardio-monitor/sop-design-control.md (archived)
#
#   [2/50] risk-register.csv
#          Type: entity
#          Client: cardio-monitor
#          -> 15 wiki pages created (CSV, auto-detected risk-register mapping)
#          -> sources/cardio-monitor/risk-register.csv (archived)
#   ...
#
#   Tornado complete.
#     Processed: 50
#     Created: 127 wiki pages
#     Archived: 50 sources
#     Inbox: empty

# Dry run first to preview
mneme tornado --client cardio-monitor --dry-run

# Apply vocabulary harmonization during ingest
mneme tornado --client cardio-monitor --profile
```

Tornado:
- Auto-detects page type from content keywords
- Auto-detects client from frontmatter or filename
- Routes CSVs through `ingest-csv` with auto-detected mapping
- Routes everything else through standard ingest
- Archives originals to `sources/{client}/`
- Empties the inbox when done

---

## Example 6: Quality Checks and Harmonization

After ingesting everything, run quality checks:

```bash
# Full lint: orphan pages, dead links, stale content, missing citations
mneme lint
#   Found 14 issues:
#     Dead links: 3
#     Orphan pages: 5
#     Missing citations: 6
#   Report: wiki/lint-report-2026-04-06.md

# Check vocabulary against EU MDR profile
mneme harmonize --client cardio-monitor
#   Found 8 vocabulary issues:
#     "product" (3 pages) -> should be "medical device"
#     "intended use" (2 pages) -> should be "intended purpose"
#     "vendor" (3 pages) -> should be "manufacturer"

# Auto-fix vocabulary
mneme harmonize --client cardio-monitor --fix
#   Pages fixed: 7

# Build a writing-style review packet for an LLM agent.
# Mneme does not grade prose itself - it hands the agent the page, the
# active profile's writing_style block, and the section_notes for the
# document type matched from the page's frontmatter `type:` field.
mneme validate writing-style cardio-monitor/dvr-tda > review.md
# Then paste review.md into Claude Code (or any LLM) for a critique.

# Or get raw structured output for SDK integration:
mneme validate writing-style cardio-monitor/dvr-tda --json > review.json

# Cross-document consistency
mneme validate consistency --client cardio-monitor
#   WARNING: "ISO 14971:2019" cited in 3 pages, "ISO 14971:2007" in 2 pages
```

---

## Example 7: Searching and Exporting

```bash
# Search across everything
mneme search "electrical safety"

# Search within one client only
mneme search "battery life" --client cardio-monitor

# Export a client's knowledge base as JSON (for QMS app)
mneme export cardio-monitor --format json
#   Exported to: exports/cardio-monitor-2026-04-06.json

# Export as combined markdown (for review)
mneme export cardio-monitor --format md
#   Exported to: exports/cardio-monitor-2026-04-06.md

# Create a versioned snapshot for audit
mneme snapshot cardio-monitor
#   Path: snapshots/cardio-monitor-2026-04-06.zip
#   Pages: 127
#   Git tag: snapshot/cardio-monitor/2026-04-06
```

---

## Example 8: Code Repository Scanning

Your firmware team has a repo. You need to check if QMS docs cover the codebase:

```bash
mneme scan-repo /path/to/cardio-firmware cardio-monitor

#   Dependencies found:      12
#   Dependencies documented:  4
#   Dependencies missing:     8     <- SOUP analysis gap!
#
#   Modules found:           28
#   Modules documented:       15
#   Modules missing:          13    <- Architecture doc gap!
#
#   Suggestions:
#     CREATE: cardio-monitor/openssl-dependency (SOUP)
#     CREATE: cardio-monitor/freertos-dependency (SOUP)
#     UPDATE: cardio-monitor/software-architecture (add: ota-update, sensor-driver)
```

---

## Example 9: Day-to-Day Operations

```bash
# Start of day: what's pending?
mneme status
#   Sources: 5
#   Un-ingested: 2
#   Wiki pages: 127
#   Orphan pages: 3

# What happened recently?
mneme recent -n 5
#   [2026-04-06 09:15] INGEST | firmware-spec.pdf -> cardio-monitor
#   [2026-04-06 09:10] LINT | 3 issues found
#   ...

# Check for drift between wiki and memvid
mneme drift
#   Sync: 95% (120/127 pages)
#   Missing from memvid: 7

# Fix it
mneme sync

# Check for duplicates
mneme dedupe

# See what changed in a specific page
mneme diff cardio-monitor/risk-analysis

# View all tags
mneme tags list
#   cardio-monitor: 127 pages
#   critical: 12 pages
#   electrical-safety: 8 pages

# Merge tags
mneme tags merge electrical electrical-safety
```

---

## Example 10: QMS Integration Pattern

The QMS application consumes mneme's output:

```python
import json
import subprocess

# Call mneme from your app
result = subprocess.run(
    ['mneme', 'search', 'electrical safety', '--client', 'cardio-monitor'],
    capture_output=True, text=True
)

# Or read the export directly
with open('exports/cardio-monitor-2026-04-06.json') as f:
    knowledge_base = json.load(f)

# Each page is a dict with:
# {
#   "slug": "cardio-monitor/un-001",
#   "frontmatter": {"title": "...", "type": "user-need", "confidence": "medium"},
#   "body": "## Summary\n..."
# }

# Or hit the web API
import requests
response = requests.get('http://localhost:3141/api/search?q=electrical+safety')
results = response.json()

# Or read files directly -- it's all plain markdown + JSON
import glob
for page in glob.glob('wiki/cardio-monitor/*.md'):
    with open(page) as f:
        content = f.read()
    # Parse frontmatter, extract what you need
```

The QMS app doesn't need to understand how mneme works internally. It just reads:
- `wiki/{client}/*.md` -- structured markdown pages
- `schema/entities.json` -- all known entities
- `schema/tags.json` -- tag taxonomy
- `schema/traceability.json` -- trace links between pages
- `exports/{client}.json` -- pre-parsed JSON export

---

## Example 11: Using the Workspace as an Obsidian Vault

Mneme wiki pages are already Obsidian-compatible. Every workspace doubles as a vault.

```bash
# 1. Scaffold a workspace
mneme new ~/vaults/cardio-monitor --name "CardioMonitor" --client cardio-monitor --profile eu-mdr

# 2. Ingest some docs via the CLI
cd ~/vaults/cardio-monitor
mneme ingest design-input.pdf cardio-monitor
mneme ingest-csv risk-register.csv cardio-monitor --mapping risk-register
```

**Open in Obsidian:**

1. Launch Obsidian → **Open folder as vault** → select `~/vaults/cardio-monitor`
2. Obsidian creates `.obsidian/` inside the workspace (ignored by mneme)
3. You instantly see:
   - `wiki/cardio-monitor/*.md` as browseable notes
   - `[[wikilinks]]` between pages as clickable links
   - `index.md` as your catalog
   - Tag pane populated from page frontmatter
   - Graph view of entity relationships

**Recommended vault settings:**

```
Settings → Files & Links
  Default location for new notes: wiki/cardio-monitor
  New link format: Relative path to file
  Use [[Wikilinks]]: ON
  Detect all file extensions: OFF
```

**Community plugins that pair well:**

- **Dataview** — query frontmatter like a database:
  ```dataview
  TABLE confidence, updated FROM "wiki/cardio-monitor"
  WHERE type = "hazard" AND confidence = "low"
  ```
- **Templater** — paste mneme page skeletons from snippets
- **Tag Wrangler** — rename tags across the vault and `schema/tags.json` stays in sync after `mneme repair`
- **Graph Analysis** — compare against `schema/graph.json`

**Two-way workflow:**

| You do this... | Mneme sees... | Obsidian sees... |
|---|---|---|
| `mneme ingest report.pdf client` | New wiki page written | Auto-detected, linked, tagged |
| Edit a page in Obsidian | `mneme lint` flags citation / link issues on next run | Your edits persist |
| `mneme tornado` on an inbox | Batch-ingested | Live updates in the file tree |
| Sync the workspace via git | History preserved | Conflicts surface in Obsidian's sync plugin if used |

**Gotchas:**

- Keep `memvid/` and `snapshots/` in `.gitignore` (scaffolder does this already)
- Turn OFF Obsidian's "Detect all file extensions" so `sources/*.pdf` stays out of the graph view
- Don't rename mneme-generated files inside Obsidian — it breaks the `[[wikilinks]]` and `index.md` entries. Rename via `mneme` commands instead.

---

## Example 12: Updating a Source File After External Edits

A colleague on the firmware team edited `risk-register.md` in their own repo -- they added RMA-003 for a newly discovered electrical hazard. Meanwhile, you've been annotating the matching wiki page in Obsidian with open questions from the last design review. A plain re-ingest would wipe your notes. `mneme resync` does a 3-way merge instead.

```bash
# Starting state: the risk register was ingested last week
mneme recent -n 1
#   [2026-04-01 14:22] INGEST | risk-register.md -> cardio-monitor

# Since then, you added an "## Open Questions" section by hand in Obsidian
# to wiki/cardio-monitor/risk-register.md

# Your colleague drops an updated copy in your inbox (new RMA-003 row added)
cp ~/shared/firmware-team/risk-register.md incoming/

# Preview the merge without touching disk
mneme resync incoming/risk-register.md cardio-monitor --dry-run
#   Baseline:  wiki/cardio-monitor/.baselines/risk-register.md  (a1b2c3d4)
#   Ours:      wiki/cardio-monitor/risk-register.md             (9f8e7d6c)
#   Theirs:    <fresh ingest of incoming/risk-register.md>      (4d5e6f70)
#   Merged:                                                     (77aabb11)
#   Result: clean merge (no conflicts)

# Apply the merge
mneme resync incoming/risk-register.md cardio-monitor
#   Ingesting incoming/risk-register.md ...
#   3-way merge: baseline <- ours / theirs
#   Result: clean merge
#     + added: "RMA-003 - Insulation barrier for secondary circuit"
#     = kept:  "## Open Questions" section (your hand edit)
#   wiki/cardio-monitor/risk-register.md updated
#   Baseline advanced
#   Schema re-derived

mneme recent -n 1
#   [2026-04-08 10:03] RESYNC | risk-register.md -> cardio-monitor (clean)
```

The new RMA is now in the wiki page, and your Open Questions section survived untouched.

### Conflict variant

Suppose you *also* edited the severity column for HAZ-002 locally, and your colleague edited the same cell to a different value. Both sides touched the same line, so git can't auto-merge:

```bash
mneme resync incoming/risk-register.md cardio-monitor
#   3-way merge: baseline <- ours / theirs
#   Result: CONFLICT (1 region)
#   wiki/cardio-monitor/risk-register.md now contains merge markers.
#   Edit the file, then run:
#     mneme resync-resolve cardio-monitor/risk-register
```

Open the page and you'll see a block like:

```
<<<<<<< current (ours)
| HAZ-002 | Battery overheat | Burn | High | ...
||||||| baseline (ancestor)
| HAZ-002 | Battery overheat | Burn | Medium | ...
=======
| HAZ-002 | Battery overheat | Burn | Critical | ...
>>>>>>> incoming (theirs)
```

Pick the right value (or write a new one), delete the marker lines, save, then:

```bash
mneme resync-resolve cardio-monitor/risk-register
#   Conflict markers cleared
#   Baseline advanced
#   Schema re-derived

mneme recent -n 2
#   [2026-04-08 10:11] RESYNC-RESOLVED | cardio-monitor/risk-register
#   [2026-04-08 10:07] RESYNC-CONFLICT | risk-register.md -> cardio-monitor
```

`mneme resync` is safe to run on files that were never ingested before -- with no baseline available, it falls through to a regular `mneme ingest`. For CSV-derived workflows where each row is its own page, resync handles per-row updates the same way: added rows become new pages, modified rows trigger a per-page 3-way merge.

---

## Example 13: Adding a Custom Profile (Workspace-Local)

Mnemo ships two bundled profiles -- `eu-mdr` and `iso-13485` -- but those are just two examples. Any workspace can define its own profiles by dropping a markdown file into `<workspace>/profiles/`. **No reinstall, no rebuild, no PR upstream.**

The shadowing rule: when you ask for a profile by name, mneme checks the workspace folder first, then the bundled set. Workspace files win.

### Scenario

You're building Parkiwatch -- an internal product line for parking enforcement. You don't need EU MDR or ISO 13485, but you do need your own quality framework with parkiwatch-specific terminology and writing-style guidance. You want `mneme harmonize` to flag "parking ticket" and rewrite it to "parking violation", and `mneme validate writing-style` to hand a Claude agent the prose guidance for incident reports so it can critique a draft.

### Step 1 -- Scaffold a workspace (creates `profiles/` for you)

```bash
mneme new ~/projects/parkiwatch --name Parkiwatch --client parkiwatch
cd ~/projects/parkiwatch

ls profiles/
#   README.md       <- explains the .md profile format
#   mappings/       <- workspace-local CSV column mappings live here
```

The `profiles/` directory is part of the bundled workspace template, so every fresh `mneme new` workspace has it ready as an obvious extension point.

### Step 2 -- Author your profile

Profiles are markdown files with YAML frontmatter. The frontmatter carries the structured fields and the body carries the writing-style prose under recognized H1 headings. Create `profiles/parkiwatch-qms.md`:

```bash
cat > profiles/parkiwatch-qms.md <<'EOF'
---
name: Parkiwatch QMS
description: Internal quality management framework for the Parkiwatch product line
version: 1.0
tone: formal
voice: passive-for-procedures
citation_style: policy-section-reference
trace_types:
  - derived-from
  - implemented-by
  - verified-by
  - supersedes
requirement_levels:
  shall: mandatory
  should: recommended
  may: permitted
vocabulary:
  - use: parking violation
    reject: [parking ticket, parking offence, infraction]
  - use: enforcement officer
    reject: [meter maid, warden, ticket inspector]
  - use: vehicle owner
    reject: [driver, car owner, license holder]
---

# Principles

- Auditable: every claim about an incident must be backed by a controlled record (photo, witness statement, telemetry log).
- Procedural, not narrative: incident reports describe what happened, when, and against which policy clause. They do not editorialise.

# General Rules

- Reference Parkiwatch policy clauses by ID (e.g. PWP-3.2.1), not by name.
- Use the controlled vocabulary. The terminology table below is normative.
- Never leave a section blank. Every section heading must contain content or be removed.
- Use the placeholder [TO ADD REF] when a reference cannot be located at the time of writing.

# Terminology

| Use | Instead of | Why |
|---|---|---|
| parking violation | parking ticket, parking offence, infraction | Parkiwatch internal convention. |
| enforcement officer | meter maid, warden, ticket inspector | Reflects the formal job title. |
| vehicle owner | driver, car owner, license holder | The legal liability sits with the registered owner. |

# Framing: Reporting an incident

**Wrong:**

> The car was illegally parked and the driver was rude to the warden.

**Correct:**

> Vehicle BD-1234-AA was observed in violation of Parkiwatch policy PWP-3.2.1 (no-parking zone, school hours). Enforcement officer ID-471 issued violation notice PV-2026-0182 at 08:42 local time. Photo evidence is attached as PV-2026-0182-photo-{1,2}.jpg.

**Why:** the wrong version is narrative and editorial. The correct version cites the policy clause, names the controlled records, and uses the controlled vocabulary.

# Document Type: incident-report

Standard parking incident structure used by all enforcement officers.

## Section: incident-id

Format: `PV-YYYY-NNNN`. Auto-generated by the enforcement tablet.

## Section: location

GPS coordinates plus the nearest controlled-zone reference (e.g. PZ-CITY-CENTER-N3).

## Section: evidence

Photo evidence with timestamp and GPS metadata is mandatory. Reference the photo IDs by name.

## Section: officer-signature

Officer ID and digital signature timestamp. Manual signatures are not acceptable.

# Document Type: appeal-record

Appeal handling per Parkiwatch policy 3.2.

## Section: grounds

State the appellant's grounds verbatim where possible. Use a quote block.

## Section: decision

Pass / overturned / reduced. Reference the policy clause that supports the decision.

# Submission Checklist

- All references include policy clause ID (e.g. PWP-3.2.1)
- Controlled vocabulary used throughout
- Photo evidence attached and named consistently
- Officer ID and digital signature present
- No editorial language about the vehicle owner
EOF
```

### Step 3 -- Activate it

```bash
mneme profile set parkiwatch-qms
#   Active profile set to: parkiwatch-qms

mneme profile show
#   Active profile: Parkiwatch QMS
#
#     Description: Internal quality management framework for the Parkiwatch product line
#     Vocabulary rules: 3
#     Section templates: 2
#     Tone: formal
#     Voice: passive-for-procedures
```

### Step 4 -- Use it across the workflow

The active profile drives several commands:

```bash
# Ingest some incidents
mneme ingest incidents/incident-001.md parkiwatch
mneme ingest incidents/incident-002.md parkiwatch

# Vocabulary check: flags pages using "parking ticket" instead of "parking violation"
mneme harmonize --client parkiwatch
#   Found 3 vocabulary issues:
#     "parking ticket" (2 pages)  -> should be "parking violation"
#     "meter maid" (1 page)        -> should be "enforcement officer"

# Auto-fix vocabulary
mneme harmonize --client parkiwatch --fix
#   Pages fixed: 3

# Build a writing-style review packet for an LLM agent.
# The packet contains the page, the writing-style block, and the
# section_notes resolved from the page's frontmatter `type:` field.
mneme validate writing-style parkiwatch/incident-001 > /tmp/review.md
# Then paste /tmp/review.md into Claude or any LLM.

# Cross-document consistency
mneme validate consistency --client parkiwatch
```

### Where mneme actually looks

When you run `mneme profile set parkiwatch-qms`, mneme resolves the name in this order:

1. **First:** `~/projects/parkiwatch/profiles/parkiwatch-qms.md` -- your local profile
2. **Then:** `<wherever pip installed mneme>/mneme/profiles/parkiwatch-qms.md` -- the bundled set (only `eu-mdr` and `iso-13485` ship)

The first one wins. If neither exists, you get a clear error listing both paths it checked.

### Overriding a bundled profile

The same shadowing rule lets you override `eu-mdr` for one specific project. Suppose you're doing an EU MDR submission but your company has stricter internal vocabulary rules. Drop your stricter version at `~/projects/cardio-monitor/profiles/eu-mdr.md` and that project gets your stricter profile. Other projects that also use `eu-mdr` still get the bundled one.

```bash
cd ~/projects/cardio-monitor
cp ~/qms-templates/strict-eu-mdr.md profiles/eu-mdr.md
mneme profile show
#   Active profile: EU MDR (strict company variant)
```

### CSV column mappings work the same way

CSV mappings (used by `mneme ingest-csv`) are still JSON because they are programmatic, not prose. Drop a mapping at `profiles/mappings/parkiwatch-incidents.json`:

```bash
cat > profiles/mappings/parkiwatch-incidents.json <<'EOF'
{
  "name": "Parkiwatch Incidents Import",
  "description": "Maps incident-tracker CSV exports to incident wiki pages",
  "page_type": "incident",
  "id_column": "Ticket",
  "title_column": "Summary",
  "detect_headers": ["parkiwatch incident", "incident-id"],
  "mapping": {
    "Ticket": "frontmatter.id",
    "Summary": "frontmatter.title",
    "Location": "body.location",
    "Severity": "body.severity",
    "Reporter": "body.reporter",
    "Linked Appeal": "traces.referenced-in"
  }
}
EOF

mneme ingest-csv incidents-export.csv parkiwatch --mapping parkiwatch-incidents
#   Pages created: 47
#   Trace links:    12
```

`mneme ingest-csv` will also auto-detect this mapping if you omit `--mapping`, because the auto-detector scans both bundled and workspace mapping directories.

### Common gotchas

| Gotcha | Why | Fix |
|---|---|---|
| `Profile not found` after activate | The .md file isn't in `<workspace>/profiles/` | `ls profiles/` to verify the filename matches |
| `mneme profile set` works but `harmonize` does nothing | Profile loaded but the `vocabulary:` block in frontmatter is empty | Add at least one `- use: ...` entry |
| Edits to the .md don't take effect | Wrong workspace, or cached `MNEME_HOME` env var | `mneme profile show` re-reads on every call; verify `MNEME_HOME` is unset |
| Profile from one project leaks into another | You exported `MNEME_HOME` and forgot to unset it | `unset MNEME_HOME` (POSIX) / `Remove-Item Env:MNEME_HOME` (PowerShell) |
| Unrecognised H1 heading silently ignored | Mneme only parses the documented set of H1 headings | Use the recognised headings, or keep prose under one of them. Authoring notes under unknown headings are intentionally dropped. |

### What you do NOT have to do

- You **do not** edit the installed mneme package
- You **do not** rebuild the wheel
- You **do not** open a PR against the mneme repo
- You **do not** restart anything -- mneme reads the .md on every command

The profile is just a file in your project. Treat it like any other project asset: commit it to git, version it alongside your wiki, and ship it as part of your workspace.

---

## Example 14: Producing a Design Validation Report with the Agent Loop

This example shows the headline workflow of v0.4.0: a user gives an LLM agent (Claude Code, an SDK app, anything) a high-level goal, and the agent walks a deterministic plan generated from the active profile, calling `mneme draft` and `mneme validate writing-style` along the way. Mneme assembles the contracts; the agent does the writing and grading.

If you are an LLM agent reading these examples, also read [AGENTS.md](AGENTS.md) at the repo root for the full protocol.

### Scenario

You're producing a Design Validation Report (DVR) for the Tremor Detection Algorithm (TDA), an accelerometer-based feature on a wrist-worn device. The eu-mdr profile already ships a `design-validation-report` document type with 11 sections of writing-style guidance derived from real reviewer comments.

### Step 1 -- Set up the workspace

```bash
mneme new ~/projects/cardio-monitor --name "CardioMonitor" --client tda --profile eu-mdr
cd ~/projects/cardio-monitor
mneme profile show
#   Active profile: EU MDR
#     Vocabulary rules: 15
#     ...
```

Drop your evidence into `sources/tda/`: the algorithm code (or a notebook export), the validation result CSV, the dataset description, the reference standards. Then ingest:

```bash
mneme ingest sources/tda/algorithm-spec.md tda
mneme ingest sources/tda/validation-results.md tda
mneme ingest-csv sources/tda/datasets.csv tda --mapping datasets
```

### Step 2 -- Generate the plan

The user (you) tells the agent the goal:

> Produce a Design Validation Report for the TDA algorithm.

The agent runs:

```bash
mneme agent plan \
  --goal "Produce a Design Validation Report for the TDA algorithm" \
  --doc-type design-validation-report \
  --client tda
```

Mneme reads the active eu-mdr profile, walks `sections.design-validation-report.section_notes`, and generates a 15-task plan:

```
Plan: design-validation-report-tda-2026-04-09
  Goal: Produce a Design Validation Report for the TDA algorithm
  Doc type: design-validation-report
  Client: tda
  Profile: EU MDR
  Tasks: 15

Tasks:
  - [draft-section] section-purpose-and-scope
      goal: Draft the `purpose-and-scope` section of the design-validation-report
      depends on: (none)
  - [draft-section] section-context
      goal: Draft the `context` section of the design-validation-report
      depends on: (none)
  ... (one per section in the eu-mdr profile)
  - [assemble-document] assemble-document
      depends on: section-purpose-and-scope, section-context, ... (all 11)
  - [harmonize] harmonize
      depends on: assemble-document
  - [review-page] review-page
      depends on: harmonize
  - [submission-check] submission-check
      depends on: review-page

Next: mneme agent next-task --plan design-validation-report-tda-2026-04-09
```

The plan and per-task statuses are persisted under `.mneme/agent-plans/` (gitignored). The agent can resume across sessions by reading the plan file.

### Step 3 -- Walk the plan

The agent loops `next-task` -> do work -> `task-done` until done.

```bash
mneme agent next-task
#   Next task: section-purpose-and-scope
#     Kind:    draft-section
#     Goal:    Draft the `purpose-and-scope` section of the design-validation-report
#
#     Instructions:
#       Run the next_command to assemble a write packet for the
#       `purpose-and-scope` section. Then write the section as a single
#       markdown block starting with `## purpose-and-scope` and following
#       the section notes, principles, general rules, and terminology
#       guidance in the packet. Use only the evidence provided. Cite each
#       non-trivial claim. Use [TO ADD REF] for missing refs.
#
#     Preconditions:
#       - Active profile must be "EU MDR"
#
#     Run:        mneme draft --doc-type design-validation-report --section purpose-and-scope --client tda
#     After done: mneme agent task-done section-purpose-and-scope --plan design-validation-report-tda-2026-04-09
```

The agent runs the `Run:` line:

```bash
mneme draft --doc-type design-validation-report --section purpose-and-scope --client tda
```

Mneme returns a write packet: the section's notes from the profile, the full writing_style block (principles, general_rules, terminology_guidance, framing_examples), the submission_checklist, candidate evidence (drawn from a wiki text search using the section name as the query, scoped to the `tda` client), and a write prompt that tells the agent exactly what to do.

The agent writes the section, saves it (typically into a working draft file or directly into `wiki/tda/design-validation-report.md`), then marks the task done:

```bash
mneme agent task-done section-purpose-and-scope
#   Marked task "section-purpose-and-scope" as done.
#   Run `mneme agent next-task` to get the next ready task.
```

Repeat for each section. **The agent can spawn parallel sub-agents** -- one section-writer per section -- because all 11 section tasks have empty `depends_on` lists. See [AGENTS.md](AGENTS.md) section 7 for the sub-agent patterns.

### Step 4 -- Assemble, harmonize, review, submission check

Once all section tasks are done, `next-task` returns the assemble-document task:

```bash
mneme agent next-task
#   Next task: assemble-document
#     Goal: Assemble all section drafts into wiki/tda/design-validation-report.md
#     Instructions: Combine the drafted sections (in the order listed in
#       the active profile) into a single wiki page at the deliverable
#       target_page. Add proper frontmatter: title, type:
#       design-validation-report, client: tda, created/updated dates,
#       sources from the evidence used, confidence: medium.
```

The agent assembles, marks done, then walks the remaining tasks: `harmonize` (mechanical vocabulary fix), `review-page` (build a review packet via `validate writing-style` and apply the LLM grade), `submission-check` (walk the profile's submission_checklist).

```bash
mneme harmonize --client tda --fix
mneme agent task-done harmonize

mneme validate writing-style tda/design-validation-report > /tmp/review.md
# Agent reads /tmp/review.md, applies the recommendations to the page
mneme agent task-done review-page

# Walk the submission_checklist from the profile
mneme profile show
mneme agent task-done submission-check
```

### Step 5 -- Confirm done

```bash
mneme agent show
#   Plan: design-validation-report-tda-2026-04-09
#     Goal: Produce a Design Validation Report for the TDA algorithm
#
#   Progress: 15/15
#
#     [x] section-purpose-and-scope  (draft-section)
#     [x] section-context            (draft-section)
#     ...
#     [x] submission-check           (submission-check)

mneme agent next-task
#   All tasks done.
```

### Why this matters

The agent didn't have to know the eu-mdr profile structure, the section list for a DVR, the writing style rules, or the order of operations. It only had to:

1. Run `mneme agent plan` once with the goal.
2. Loop `next-task` -> read envelope -> run `next_command` -> mark done.

Mneme generated the plan deterministically from the profile. Every command the agent ran (`mneme draft`, `mneme harmonize`, `mneme validate writing-style`) returns a self-contained packet with no external context required. **The agent's job is to write and grade prose. Mneme's job is to give the agent every piece of context it needs to do that well.**

For the full agent protocol, including the standard task templates for CER (Part A), risk management files, resync workflows, project migration, and pre-submission readiness checks -- read [AGENTS.md](AGENTS.md).

---

## Bundled CSV Mapping Templates

| Mapping | CSV columns it expects | Page type created |
|---|---|---|
| `user-needs` | ID, Title, Description, Priority, Acceptance Criteria, Linked Requirement | `user-need` |
| `requirements` | ID, Title, Description, Acceptance Criteria, Category, Derived From, Verification | `requirement` |
| `risk-register` | ID, Hazard, Harm, Severity, Probability, Risk Level, Risk Control, RMA, Verification | `hazard` |
| `dds` | ID, Title, Description, Module, Requirement, Test Case | `design-spec` |
| `test-cases` | ID, Title, Description, Test Method, Expected Result, Pass/Fail, Requirement | `verification` |

Column names are flexible -- the mapping matches by keyword, not exact name. A column called "Test ID" will match the `test-cases` mapping's detect pattern.
