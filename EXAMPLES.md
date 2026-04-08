# EXAMPLES.md - Mnemosyne Usage Guide

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

#   === Mnemosyne Tornado ===
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

# Validate document structure
mneme validate structure cardio-monitor/risk-management-file
#   Missing sections: residual-risk-evaluation, risk-management-review

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

Mnemo ships two bundled profiles -- `eu-mdr` and `iso-13485` -- but those are just two examples. You're not stuck with what mneme ships. Any workspace can define its own profiles by dropping a JSON file into `<workspace>/profiles/`. **No reinstall, no rebuild, no PR upstream.**

The shadowing rule: when you ask for a profile by name, mneme checks the workspace folder first, then the bundled set. Workspace files win.

### Scenario

You're building Parkiwatch -- an internal product line for parking enforcement. You don't need EU MDR or ISO 13485, but you do need your own QMS framework with parkiwatch-specific terminology and document structure rules. You want `mneme harmonize` to flag "parking ticket" and rewrite it to "parking violation", and `mneme validate structure` to enforce that every incident report has the right sections.

### Step 1 -- Scaffold a workspace (creates `profiles/` for you)

```bash
mneme new ~/projects/parkiwatch --name Parkiwatch --client parkiwatch
cd ~/projects/parkiwatch

ls profiles/
#   README.md       <- explains the format
#   mappings/       <- workspace-local CSV column mappings live here
```

The `profiles/` directory is part of the bundled workspace template, so every fresh `mneme new` workspace has it ready as an obvious extension point.

### Step 2 -- Author your profile

Create `profiles/parkiwatch-qms.json`. The schema matches the bundled `eu-mdr.json` exactly -- you can copy a bundled profile from the installed package as a starting point if you prefer.

```bash
cat > profiles/parkiwatch-qms.json <<'EOF'
{
  "name": "Parkiwatch QMS",
  "description": "Internal quality management framework for the Parkiwatch product line",
  "version": "1.0",
  "vocabulary": {
    "preferred": [
      { "term": "parking violation", "reject": ["parking ticket", "parking offence", "infraction"] },
      { "term": "enforcement officer", "reject": ["meter maid", "warden", "ticket inspector"] },
      { "term": "vehicle owner", "reject": ["driver", "car owner", "license holder"] }
    ],
    "requirement_levels": {
      "shall": "mandatory",
      "should": "recommended",
      "may": "permitted"
    }
  },
  "sections": {
    "incident-report": {
      "required": [
        "incident-id",
        "location",
        "timestamp",
        "vehicle-details",
        "evidence",
        "officer-signature"
      ],
      "description": "Standard parking incident structure"
    },
    "appeal-record": {
      "required": [
        "appeal-id",
        "original-incident",
        "submitted-date",
        "grounds",
        "decision",
        "decision-rationale"
      ],
      "description": "Appeal handling per Parkiwatch policy 3.2"
    }
  },
  "trace_types": [
    "derived-from",
    "implemented-by",
    "verified-by",
    "supersedes"
  ],
  "tone": "formal",
  "voice": "passive-for-procedures",
  "citation_style": "policy-section-reference"
}
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
mneme harmonize parkiwatch
#   Found 3 vocabulary issues:
#     "parking ticket" (2 pages)  -> should be "parking violation"
#     "meter maid" (1 page)        -> should be "enforcement officer"

# Auto-fix vocabulary
mneme harmonize parkiwatch --fix
#   Pages fixed: 3

# Validate structure of a specific incident report
mneme validate structure parkiwatch/incident-001
#   OK -- all required sections present (incident-id, location, timestamp,
#         vehicle-details, evidence, officer-signature)

# Cross-document consistency
mneme validate consistency --client parkiwatch
```

### Where mneme actually looks

When you run `mneme profile set parkiwatch-qms`, mneme resolves the name in this order:

1. **First:** `~/projects/parkiwatch/profiles/parkiwatch-qms.json` -- your local profile
2. **Then:** `<wherever pip installed mneme>/mneme/profiles/parkiwatch-qms.json` -- the bundled set (only `eu-mdr` and `iso-13485` ship)

The first one wins. If neither exists, you get a clear error listing both paths it checked.

### Overriding a bundled profile

The same shadowing rule lets you override `eu-mdr` for one specific project. Suppose you're doing an EU MDR submission but your company has stricter internal vocabulary rules. Drop your stricter version at `~/projects/cardio-monitor/profiles/eu-mdr.json` and that project gets your stricter profile. Other projects that also use `eu-mdr` still get the bundled one.

```bash
cd ~/projects/cardio-monitor
cp ~/qms-templates/strict-eu-mdr.json profiles/eu-mdr.json
mneme profile show
#   Active profile: EU MDR (strict company variant)
```

### CSV column mappings work the same way

If you have a parkiwatch-specific CSV format (e.g. an incidents export from your ticketing system), drop a mapping at `profiles/mappings/parkiwatch-incidents.json`:

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
| `Profile not found` after activate | The JSON file isn't in `<workspace>/profiles/` | `ls profiles/` to verify the filename matches |
| `mneme profile set` works but `harmonize` does nothing | Profile loaded but `vocabulary.preferred` is empty | Add at least one entry under `preferred` |
| Edits to the JSON don't take effect | Wrong workspace, or cached `MNEME_HOME` env var | `mneme profile show` re-reads on every call; verify `MNEME_HOME` is unset |
| Profile from one project leaks into another | You exported `MNEME_HOME` and forgot to unset it | `unset MNEME_HOME` (POSIX) / `Remove-Item Env:MNEME_HOME` (PowerShell) |

### What you do NOT have to do

- You **do not** edit the installed mneme package
- You **do not** rebuild the wheel
- You **do not** open a PR against the mneme repo
- You **do not** restart anything -- mneme reads the JSON on every command

The profile is just a file in your project. Treat it like any other project asset: commit it to git, version it alongside your wiki, and ship it as part of your workspace.

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
