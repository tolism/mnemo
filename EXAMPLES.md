# EXAMPLES.md - Mnemosyne Usage Guide

Real-world workflows, core concepts, and entity types used in mnemo.

---

## Core Concepts

### What is a "Second Brain"?

A second brain is a persistent, searchable knowledge layer that sits between you and your documents. You feed it raw information once. From that point, you query it instead of re-reading source files. Knowledge compounds instead of decaying.

Mnemo is the CLI tool that builds and maintains this second brain.

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
# Step 1: Initialize
mnemo init --project "CardioMonitor" --clients "cardio-monitor"

# Step 2: Activate the EU MDR profile
mnemo profile set eu-mdr
mnemo profile show
#   Active profile: EU MDR
#   Vocabulary rules: 15
#   Section templates: 6

# Step 3: Check what you have
mnemo status
#   Sources: 0
#   Wiki pages: 0
```

---

## Example 2: Ingesting Your First Documents

You have meeting notes, a risk analysis, and a product specification.

```bash
# Ingest one at a time
mnemo ingest meeting-notes-2026-03-15.md cardio-monitor
mnemo ingest risk-analysis-v1.pdf cardio-monitor
mnemo ingest product-spec.md cardio-monitor

# Or batch ingest a folder
mnemo ingest-dir documents/ cardio-monitor

# Check results
mnemo stats
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
mnemo ingest-csv user-needs.csv cardio-monitor --mapping user-needs

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

# Or let mnemo auto-detect the mapping from column headers
mnemo ingest-csv user-needs.csv cardio-monitor
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
mnemo ingest-csv user-needs.csv cardio-monitor --mapping user-needs
mnemo ingest-csv requirements.csv cardio-monitor --mapping requirements
mnemo ingest-csv design-specs.csv cardio-monitor --mapping dds
mnemo ingest-csv test-cases.csv cardio-monitor --mapping test-cases
mnemo ingest-csv risk-register.csv cardio-monitor --mapping risk-register

# Check the trace chain from a user need to its test
mnemo trace show cardio-monitor/un-001 --direction forward
#   Root: cardio-monitor/un-001
#     implemented-by -> cardio-monitor/req-003
#       detailed-in -> cardio-monitor/dds-015
#         verified-by -> cardio-monitor/test-042

# Check backward from a test to its origin
mnemo trace show cardio-monitor/test-042 --direction backward
#   Root: cardio-monitor/test-042
#     verifies -> cardio-monitor/dds-015
#       details -> cardio-monitor/req-003
#         implements -> cardio-monitor/un-001

# Find gaps: requirements with no tests, hazards with no mitigations
mnemo trace gaps cardio-monitor
#   Requirements with no verification: req-011, req-023
#   Hazards with no mitigation: haz-009
#   User needs with no requirements: un-005

# Generate full traceability matrix
mnemo trace matrix cardio-monitor
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
mnemo tornado --client cardio-monitor

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
mnemo tornado --client cardio-monitor --dry-run

# Apply vocabulary harmonization during ingest
mnemo tornado --client cardio-monitor --profile
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
mnemo lint
#   Found 14 issues:
#     Dead links: 3
#     Orphan pages: 5
#     Missing citations: 6
#   Report: wiki/lint-report-2026-04-06.md

# Check vocabulary against EU MDR profile
mnemo harmonize --client cardio-monitor
#   Found 8 vocabulary issues:
#     "product" (3 pages) -> should be "medical device"
#     "intended use" (2 pages) -> should be "intended purpose"
#     "vendor" (3 pages) -> should be "manufacturer"

# Auto-fix vocabulary
mnemo harmonize --client cardio-monitor --fix
#   Pages fixed: 7

# Validate document structure
mnemo validate structure cardio-monitor/risk-management-file
#   Missing sections: residual-risk-evaluation, risk-management-review

# Cross-document consistency
mnemo validate consistency --client cardio-monitor
#   WARNING: "ISO 14971:2019" cited in 3 pages, "ISO 14971:2007" in 2 pages
```

---

## Example 7: Searching and Exporting

```bash
# Search across everything
mnemo search "electrical safety"

# Search within one client only
mnemo search "battery life" --client cardio-monitor

# Export a client's knowledge base as JSON (for QMS app)
mnemo export cardio-monitor --format json
#   Exported to: exports/cardio-monitor-2026-04-06.json

# Export as combined markdown (for review)
mnemo export cardio-monitor --format md
#   Exported to: exports/cardio-monitor-2026-04-06.md

# Create a versioned snapshot for audit
mnemo snapshot cardio-monitor
#   Path: snapshots/cardio-monitor-2026-04-06.zip
#   Pages: 127
#   Git tag: snapshot/cardio-monitor/2026-04-06
```

---

## Example 8: Code Repository Scanning

Your firmware team has a repo. You need to check if QMS docs cover the codebase:

```bash
mnemo scan-repo /path/to/cardio-firmware cardio-monitor

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
mnemo status
#   Sources: 5
#   Un-ingested: 2
#   Wiki pages: 127
#   Orphan pages: 3

# What happened recently?
mnemo recent -n 5
#   [2026-04-06 09:15] INGEST | firmware-spec.pdf -> cardio-monitor
#   [2026-04-06 09:10] LINT | 3 issues found
#   ...

# Check for drift between wiki and memvid
mnemo drift
#   Sync: 95% (120/127 pages)
#   Missing from memvid: 7

# Fix it
mnemo sync

# Check for duplicates
mnemo dedupe

# See what changed in a specific page
mnemo diff cardio-monitor/risk-analysis

# View all tags
mnemo tags list
#   cardio-monitor: 127 pages
#   critical: 12 pages
#   electrical-safety: 8 pages

# Merge tags
mnemo tags merge electrical electrical-safety
```

---

## Example 10: QMS Integration Pattern

The QMS application consumes mnemo's output:

```python
import json
import subprocess

# Call mnemo from your app
result = subprocess.run(
    ['mnemo', 'search', 'electrical safety', '--client', 'cardio-monitor'],
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

The QMS app doesn't need to understand how mnemo works internally. It just reads:
- `wiki/{client}/*.md` -- structured markdown pages
- `schema/entities.json` -- all known entities
- `schema/tags.json` -- tag taxonomy
- `schema/traceability.json` -- trace links between pages
- `exports/{client}.json` -- pre-parsed JSON export

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
