# CODER.md - Developer Guide for Mnemosyne

This is the engineering reference for anyone building on or extending mneme. Read this before writing code.

---

## Architecture Overview

Mneme is a CLI tool that creates a dual-layer knowledge base from source documents:
- **Wiki layer** -- human-readable markdown pages in `wiki/`
- **Memvid layer** -- machine-searchable `.mv2` archive in `memvid/`
- **Schema layer** -- structured JSON in `schema/` (entities, graph, tags, traceability)

The downstream consumer is a QMS (Quality Management System) application for medical device documentation.

### File Map

```
mneme/
  core.py             <- ALL business logic (4200+ lines, needs splitting -- see Architecture Debt)
  config.py           <- Constants: paths, chunk sizes, stopwords
  server.py           <- Web UI server (localhost:3141)
  profiles/           <- Writing style profiles (eu-mdr.json, iso-13485.json)
    mappings/         <- CSV column-to-frontmatter mapping templates
  wiki/               <- Generated markdown pages (Obsidian-compatible)
  schema/             <- Machine-readable JSON (entities, graph, tags, traceability)
  sources/            <- Archived source files (IMMUTABLE after ingest)
  memvid/             <- .mv2 archives
  inbox/              <- Tornado drop zone (files are processed and moved to sources/)
  exports/            <- Export output (gitignored)
  snapshots/          <- Versioned archives (gitignored)
  tests/test_core.py  <- Pytest suite
```

### Data Flow

```
Source File
    |
    v
ingest_source_to_both()
    |
    +---> wiki/{client}/{slug}.md        (parse_frontmatter, _build_wiki_page)
    +---> memvid/master.mv2              (sync_page_to_memvid, chunk_body)
    +---> schema/entities.json           (_update_entities_schema)
    +---> schema/tags.json               (_update_tags_schema)
    +---> index.md                       (_update_index)
    +---> log.md                         (_append_log)
```

CSV files take a different path:
```
CSV File
    |
    v
ingest_csv()
    |
    +---> one wiki page per row           (_csv_row_to_wiki_page)
    +---> schema/traceability.json        (_store_trace_link)
    +---> schema/entities.json            (_update_entities_schema)
    +---> schema/tags.json                (_update_tags_schema)
    +---> index.md                        (_update_index)
    +---> memvid/master.mv2              (sync_page_to_memvid)
```

---

## How to Add a New CLI Command

Every command follows the same pattern. Here's the checklist:

### Step 1: Write the function in core.py

```python
def my_feature(arg1: str, arg2: bool = False) -> dict:
    """
    One-line description.

    Detailed explanation of what this does.
    Returns a dict with results.
    """
    today = datetime.now().strftime('%Y-%m-%d')

    # ... your logic ...

    # Log the operation (if it modifies state)
    _append_log(
        operation='MY_FEATURE',
        description=f'What happened',
        details=['Detail 1', 'Detail 2'],
        date=today,
    )

    return {'key': 'value', 'count': 42}
```

### Step 2: Register the subparser in main()

Find the subparser section (search for `subparsers.add_parser`):

```python
# my-feature
my_parser = subparsers.add_parser('my-feature', help='Short description')
my_parser.add_argument('required_arg', help='What this is')
my_parser.add_argument('--optional', type=str, default=None, help='Optional flag')
my_parser.add_argument('--flag', action='store_true', help='Boolean flag')
```

### Step 3: Add the handler in main()

Find the handler section (search for `elif args.command`):

```python
elif args.command == 'my-feature':
    try:
        result = my_feature(args.required_arg, flag=args.flag)
        print(f'Done: {result["count"]} items processed.')
    except (FileNotFoundError, ValueError) as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)
```

### Step 4: Add a test

In `tests/test_core.py`:

```python
class TestMyFeature:
    def test_basic(self):
        result = my_feature('test-input')
        assert result['count'] >= 0

    def test_edge_case(self):
        result = my_feature('', flag=True)
        assert 'error' not in result
```

### Step 5: Update FEATURES.md

Add the command to the CLI table.

---

## How to Add a New CSV Mapping

### Step 1: Create the mapping file

Create `profiles/mappings/{name}.json`:

```json
{
  "name": "Human-Readable Name",
  "description": "What this mapping is for",
  "page_type": "wiki-page-type",
  "id_column": "Column name for unique ID",
  "title_column": "Column name for page title",
  "detect_headers": ["keyword1", "keyword2"],
  "mapping": {
    "CSV Column Name": "frontmatter.field_name",
    "Another Column": "body.section-name",
    "Link Column": "traces.relationship-type"
  }
}
```

### Mapping targets

| Target prefix | What it does | Example |
|---|---|---|
| `frontmatter.title` | Sets the page title | `"Title": "frontmatter.title"` |
| `frontmatter.id` | Sets the page ID | `"ID": "frontmatter.id"` |
| `frontmatter.tags` | Adds to tags list | `"Priority": "frontmatter.tags"` |
| `frontmatter.sources` | Adds to sources list | `"Source": "frontmatter.sources"` |
| `body.summary` | Writes to Summary section | `"Description": "body.summary"` |
| `body.{section}` | Creates a named section | `"Rationale": "body.rationale"` |
| `traces.{type}` | Creates trace link | `"Requirement": "traces.derived-from"` |

### Auto-detection

`detect_headers` is a list of keywords. When `mneme ingest-csv` is called without `--mapping`, it scores each mapping by how many `detect_headers` keywords appear in the CSV column names. The mapping with the highest score (minimum 2 matches) wins.

### Step 2: Test it

```bash
mneme ingest-csv my-data.csv my-client --mapping my-mapping --dry-run
```

---

## How to Add a New Profile

Create `profiles/{name}.json`:

```json
{
  "name": "Display Name",
  "description": "What regulatory framework this covers",
  "version": "1.0",
  "vocabulary": {
    "preferred": [
      {"term": "correct term", "reject": ["wrong1", "wrong2"]}
    ],
    "requirement_levels": {
      "shall": "mandatory",
      "should": "recommended",
      "may": "permitted"
    }
  },
  "sections": {
    "document-type": {
      "required": ["section-1", "section-2"],
      "description": "What this document type is"
    }
  },
  "trace_types": ["derived-from", "mitigated-by", "verified-by"],
  "tone": "formal",
  "voice": "passive-for-procedures",
  "citation_style": "section-reference"
}
```

Then activate it:

```bash
mneme profile set my-profile
mneme profile show
```

The profile is used by:
- `mneme harmonize` -- checks vocabulary rules
- `mneme validate structure` -- checks required sections
- `mneme lint` -- can integrate with profile checks

---

## How to Add a New Trace Relationship Type

1. Add the type to the profile's `trace_types` array
2. Add it to the CSV mapping as a `traces.{type}` target
3. The trace system will accept any string as a relationship type -- there's no validation against a fixed list

Current standard types:
`derived-from`, `implemented-by`, `detailed-in`, `mitigated-by`, `verified-by`, `validated-by`, `referenced-in`, `supersedes`

---

## How to Add a New Lint Check

Find the `lint()` function in core.py. Each check follows the pattern:

```python
# N. Your check description
for slug, content in page_content.items():
    # Check logic
    if problem_detected:
        issues['your_check_name'].append({
            'page': slug,
            'detail': 'what went wrong',
        })
```

Then add `'your_check_name': []` to the `issues` dict at the top of lint(), and add a report section at the bottom.

---

## Key Functions Reference

### Core Operations

| Function | Purpose | Returns |
|---|---|---|
| `ingest_source_to_both()` | Atomic ingest: source -> wiki + memvid + schema | `{wiki_page, action, frames_added, entities_updated}` |
| `ingest_csv()` | CSV ingest: one row -> one wiki page | `{pages_created, pages_updated, trace_links_created}` |
| `ingest_dir()` | Batch ingest directory | `{ingested, skipped, errors}` |
| `tornado()` | Inbox processor | `{processed, created, updated, archived}` |
| `dual_search()` | Wiki text + Memvid semantic search | `[{text, title, source, score, layer}]` |
| `lint()` | 6 health checks | `{issues, total_issues, report_path}` |
| `check_drift()` | Wiki vs Memvid sync status | `{missing_from_memvid, orphan_frames, summary}` |
| `get_stats()` | Full system health | `{wiki, memvid, schema, drift}` |

### Schema Operations

| Function | Purpose |
|---|---|
| `_update_entities_schema()` | Extract entities from content, add to entities.json |
| `_update_tags_schema()` | Extract tags from frontmatter, add to tags.json |
| `_update_index()` | Add/update entry in index.md |
| `_append_log()` | Append to log.md (newest first) |
| `_store_trace_link()` | Add trace link to traceability.json |

### Traceability

| Function | Purpose |
|---|---|
| `trace_add()` | Create a trace link between two pages |
| `trace_show()` | BFS walk of trace chain (forward/backward) |
| `trace_matrix()` | Generate traceability matrix for a client |
| `trace_gaps()` | Find incomplete trace chains |

### Profile/QMS

| Function | Purpose |
|---|---|
| `load_profile()` | Read profile JSON from profiles/ |
| `get_active_profile()` | Get currently active profile |
| `set_active_profile()` | Set active profile name |
| `harmonize()` | Check/fix vocabulary against profile |
| `validate_structure()` | Check page sections against profile |
| `validate_consistency()` | Cross-document consistency check |
| `scan_repo()` | Code repo vs wiki coverage analysis |

### Utilities

| Function | Purpose |
|---|---|
| `parse_frontmatter()` | Parse YAML-like frontmatter from markdown |
| `chunk_body()` | Split content into Memvid-sized chunks |
| `_content_hash()` | MD5 hash of content (dedup, drift detection) |
| `_locked_read_modify_write()` | File-locked read-modify-write cycle |
| `_build_wiki_page()` | Construct wiki page string with frontmatter |

---

## File Locking

All schema file modifications use `_locked_read_modify_write()` which:
1. Creates the file if missing
2. Acquires exclusive file lock (portalocker > fcntl > no-lock fallback)
3. Reads current content
4. Calls your modifier function
5. Writes result
6. Releases lock

Use it for any shared state:

```python
def modifier(content: str) -> str:
    data = json.loads(content) if content.strip() else {}
    data['new_key'] = 'new_value'
    return json.dumps(data, indent=2)

_locked_read_modify_write('schema/my-file.json', modifier)
```

---

## Memvid Integration

Memvid is optional. The pattern:

```python
if not MEMVID_AVAILABLE:
    # Skip memvid operations, wiki-only mode
    return

try:
    with _memvid_locked(MASTER_MV2, mode='open') as master:
        result = master.find(query, k=k)
except Exception as e:
    print(f'[mneme] memvid error: {e}', file=sys.stderr)
```

Never assume memvid is installed. Always check `MEMVID_AVAILABLE` first.

---

## Immutability Rules

These are absolute:

1. **`sources/`** is NEVER modified after archival. Files move in, never change.
2. **`index.md`** and **`log.md`** are ALWAYS updated after any wiki modification.
3. **Schema files** are ALWAYS kept in sync with wiki content.
4. **Existing wiki pages** are updated (not replaced) when new information arrives.
5. **Page deletion** requires explicit user approval.
6. **`_templates/`** files are NEVER modified in place.

---

## Known Gaps (from gap analysis)

### Critical for QMS

| Gap | Impact | Where |
|---|---|---|
| `graph.json` never populated | Entity relationships not tracked | No `_update_graph_schema()` function exists |
| QUERY-FILED operation missing | Core CLAUDE.md operation not implemented | Described in CLAUDE.md lines 219-230 |
| No Python logging framework | Audit trail relies on print() | Throughout core.py |
| 11 v0.3.x functions untested | trace, harmonize, validate, scan-repo, tornado, ingest_csv | tests/test_core.py |
| Broad exception handling | Silent failures | 31 instances of `except Exception` |

### Architecture Debt

| Debt | Impact |
|---|---|
| core.py is 4200+ lines | Hard to navigate, test, review |
| Error handling inconsistent | Some functions return `{error}`, others raise, others print |
| No logging framework | `print()` statements everywhere |
| Entity types always "unknown" | `_update_entities_schema()` doesn't classify |

### Recommended Module Split

When core.py is split, the target structure is:

```
mneme/
  __init__.py
  cli.py          <- main(), argparse, print functions
  ingest.py       <- ingest_source_to_both, ingest_dir, ingest_csv, tornado
  search.py       <- dual_search, _search_wiki_text
  memvid.py       <- sync_page_to_memvid, sync_all_pages, check_drift
  schema.py       <- all _update_* functions, _locked_read_modify_write
  lint.py         <- lint()
  trace.py        <- trace_add, trace_show, trace_matrix, trace_gaps
  profile.py      <- load_profile, harmonize, validate_*, scan_repo
  parsing.py      <- parse_frontmatter, chunk_body, _content_hash
  config.py       <- constants (already separate)
```

---

## Testing

Run tests:

```bash
source .venv/bin/activate
pytest tests/ -v
```

Test patterns used:
- **Unit tests** -- pure function input/output (parse_frontmatter, chunk_body)
- **CLI integration tests** -- subprocess calls to `python3 core.py <command>`
- **Ingest integration tests** -- create temp files, ingest, verify wiki + schema, cleanup

When adding tests for new functions, follow the existing pattern in `tests/test_core.py`. Each function group gets its own test class.

---

## Environment

- **Python:** 3.9+
- **Dependencies:** memvid-sdk (optional), portalocker, pymupdf (optional for PDF)
- **Install:** `uv venv && source .venv/bin/activate && uv pip install -e .`
- **CLI entry point:** `mneme` (via pyproject.toml `[project.scripts]`)
- **Web UI:** `python3 server.py` on port 3141
