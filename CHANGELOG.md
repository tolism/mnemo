# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.2] - 2026-04-14

### Changed

- **`ingest-dir --preserve-structure` is now the default.** The wiki now
  mirrors the source directory layout unless you pass `--flat`. This avoids
  silent same-basename collisions (e.g. multiple `INSTRUCTIONS.md` files from
  different source directories overwriting each other). Closes suggestion #15.
- **`mneme ingest` (single-file) also mirrors by default.** When the source
  lives under `sources/<client>/`, its relative position becomes a wiki
  subpath automatically. Pass `--flat` to opt out.

### Fixed

- **`mneme profile list`** now discovers profiles correctly. Previously it
  filtered files by `.json` (wrong extension — profiles are markdown) and
  only checked the bundled directory, which meant the shipped `eu-mdr.md`
  and `iso-13485.md` profiles appeared as "No profiles found". Now unions
  workspace + bundled, marks origin, and flags shadowed bundled profiles.
  Closes suggestion #25 discovery bug.

### Added

- **`ingest-dir --flat`** — explicit opt-out for the new preserve-structure
  default.
- **`ingest --flat`** — opt-out for the single-file command.
- **xlsx support is now built-in.** `openpyxl` moved from
  `[project.optional-dependencies].xlsx` to `dependencies`. The `[xlsx]`
  extra is kept for backwards compatibility but is no longer required.

### Documentation

- **README**: expanded the agent end-to-end example. Step 3 now covers
  bulk tagging (`tags bulk-suggest` + `bulk-apply`), Step 3b adds entity
  typing (`entity suggest` + `bulk-apply`), and Step 3c walks the full
  V-model trace chain (UN→REQ→DDS and RMA→REQ→DDS, terminating at code
  and tests).
- **AGENTS.md**: new section 3.9 "TRACE — linking the full V-model
  chain" documents the `implemented-in` / `verified-by` relationships
  and the DDS-to-codebase linking agents must perform when the user
  passes repositories. New task template 6.6 "Close the V-model by
  linking DDS to codebase and tests" gives the exact procedure, stop
  conditions, and hard rules (no fabricated paths, trace targets are
  opaque strings, never embed code links in page bodies).
## [0.5.3] - 2026-04-15

### Documentation

- **AGENTS.md**: new task template 6.7 "Ingest a code repo into the
  wiki as searchable module summaries" — the foundation for any
  code-aware agent work. One wiki page per logical module, chunked
  reading for large files, explicit tagging for partial/unclear pages,
  and `mneme ingest-dir --flat` for the bulk write.
- **AGENTS.md**: new task template 6.8 "Augment a wiki page with
  knowledge from ingested code summaries" — selective enrichment of a
  target page using evidence drawn from the code summaries produced by
  6.7. Existing prose is sacred; agent only adds new sections, in the
  page's local style, with every claim cited.
- **AGENTS.md**: new task template 6.9 "Validate a claim against the
  literature wiki" — the discipline an agent applies before any
  factual claim ships to a notified body. Three buckets (authority /
  non-authority / no support), four resolutions (cite / soften / drop /
  mark `[TO ADD REF]`), zero tolerance for non-authority dressed as
  authoritative.

## [0.5.0] - 2026-04-13

### Breaking Changes

- **Replaced memvid-sdk with SQLite FTS5.** The `memvid/` directory and `.mv2`
  archives are no longer used. Search is now powered by a local `search.db`
  file using BM25 ranking with Porter stemming. **Zero external dependencies**
  for search — `sqlite3` is in the Python stdlib.
- `mneme repair` now rebuilds the FTS5 index instead of memvid archives.
- `mneme drift` reports `unindexed` / `orphaned` / `stale` instead of
  `missing_from_memvid` / `orphan_frames`.
- `get_stats()` returns a `search` key (page_count, db_size_bytes,
  search_latency_ms) instead of `memvid`.
- `sync_page_to_memvid()` renamed to `sync_page_to_index()`. Returns
  `bool` (indexed) instead of `int` (frame count).
- Removed `chunk_body()`, `_sanitize_memvid_query()`, and all chunking
  config (`MAX_CHUNK_SIZE`, `MIN_CHUNK_SIZE`, `MAX_CHUNKS_PER_INGEST`,
  `CHUNK_COMMIT_BATCH`).
- Removed `MEMVID_DIR`, `MASTER_MV2`, `PER_CLIENT_DIR` config constants.
  Replaced by `SEARCH_DB`.

### Added

- **`mneme reindex`** command — rebuild search index from wiki pages.
- **`ingest-dir --recursive` / `-r`** — recurse into subdirectories.
- **`ingest-dir --preserve-structure`** — mirror source directory structure
  in wiki subdirectories (avoids dedup collisions between same-basename files
  in different directories).
- **`ingest-csv --delimiter`** flag with auto-detection via `csv.Sniffer`.
- **`.xlsx` ingest support** — install with `pip install "mneme-cli[xlsx]"`.
  Sheets are rendered as markdown tables.
- **`mneme trace matrix --csv [--out FILE]`** — export the trace matrix as
  CSV for QMS audits and DHF inclusion.
- **`graph.json` auto-populated** during ingest from wiki page wikilinks
  and `related` frontmatter.
- **`stats` relationship count** now includes traceability.json links, not
  just graph.json edges.
- **log.md rotation** — entries beyond `LOG_MAX_ENTRIES` (default 500) are
  archived to `log-archive-YYYY-MM-DD.md`.

### Fixed

- `mneme status` crash (UnboundLocalError on `log_content`).
- CSV ingest crash on `None` cells (`row.get()` returning None).
- Duplicate ingest detection now uses full source path, not just filename
  (two `INSTRUCTIONS.md` files in different directories now both ingest).

### Removed

- `memvid-sdk` dependency.
- `MNEME_NO_MEMVID` env var (no longer needed — FTS5 is always available).
- Chunking logic (`chunk_body`, `MAX_CHUNK_SIZE`, frame management).
- Tantivy-reserved-word query sanitizer (FTS5 has different syntax).

## [0.5.1] - 2026-04-14

### Added
- **`mneme entity suggest` / `entity apply` / `entity bulk-apply`** — agent-driven
  entity classification (same packet pattern as `tags suggest`). Mneme builds a
  packet of unclassified entities + the workspace type taxonomy + example pages;
  the LLM agent classifies; mneme writes the types back atomically.
- **`mneme tags bulk-suggest` / `tags bulk-apply`** — operate on many pages at
  once. `bulk-suggest --client X --filter req- --limit 50` packets up to 50
  matching pages; agent returns one JSON file; `bulk-apply` runs all the changes
  with per-page error tolerance. Critical for tagging workspaces of hundreds of
  pages.
- **`mneme home --client <slug>` / `--all-clients`** — generates a `HOME.md`
  navigation hub with Obsidian Dataview queries (group by type, by ID prefix
  like REQ-*/DDS-*, top tags) plus a plain-markdown `<details>` fallback for
  non-Obsidian viewers.
- **`mneme ingest-dir --preserve-structure`** — mirrors source directory
  hierarchy into wiki subdirectories. `sources/client/REQUIREMENTS/req-001.md`
  becomes `wiki/client/requirements/req-001.md` instead of flattening. Also
  resolves same-basename-different-directory collisions naturally.
- **`mneme resync` auto-detects subpath** from a source's location under
  `sources/<client>/`, so resyncs of preserve-structure ingests target the
  correct nested wiki page instead of creating a duplicate flat one.
- **Progress bar** for `ingest-dir` and `ingest-csv` long loops. TTY-aware
  (in-place updates) with non-TTY fallback (periodic line output) so CI logs
  stay readable.

### Fixed
- `mneme status` crashed with `UnboundLocalError` because a local `status` in
  the `agent show` branch shadowed the function name throughout `main()`.
- `wiki/HOME.md` and `wiki/<client>/HOME.md` are now skipped during HOME
  generation so re-running is idempotent.

## [Unreleased]

### Added
- **`mneme draft`** -- the symmetric counterpart to `mneme validate
  writing-style`. Where validate produces a *review* packet (grade existing
  prose), draft produces a *write* packet (produce new prose). Takes
  `--doc-type`, `--section`, `--client`, optional `--source` (include a file
  verbatim) or `--query` (text-search the wiki for evidence), `--json` and
  `--out` for output control. Mneme assembles the contract; the LLM agent
  consuming the packet does the writing.
- **`mneme agent` namespace** -- the structured agent loop:
  - `mneme agent plan --goal "..." --doc-type <t> --client <c>` generates a
    deterministic TODO plan from the active profile's section_notes. For a
    `design-validation-report` against the eu-mdr profile, this produces 15
    tasks: one `draft-section` (or `review-section` if the page already
    exists) per section in the profile, then `assemble-document`,
    `harmonize`, `review-page`, `submission-check`. Tasks have a
    `depends_on` graph and a `next_command` field that tells the agent
    exactly what to run.
  - `mneme agent next-task` returns the next ready task whose dependencies
    are satisfied.
  - `mneme agent task-done <id>` marks a task complete and unblocks the next.
  - `mneme agent show` displays the plan + per-task statuses.
  - `mneme agent list` lists all plans in the workspace.
  - Plans and state are persisted under `<workspace>/.mneme/agent-plans/`
    (gitignored automatically by the bundled workspace template).
- **`AGENTS.md`** at the repo root -- the canonical agent protocol document.
  Describes the agent loop, six standard task templates (DVR, CER, risk
  file, resync workflow, migration, pre-submission), four sub-agent
  spawning patterns (section-writer, reviewer, vocabulary-fixer,
  evidence-finder), the rituals an agent must read on every operation, and
  the hard rules an agent must never violate. Shipped at the repo root and
  scaffolded into every new workspace by `mneme new`.
- New log operations: `DRAFT-PACKET`, `AGENT-PLAN`, `AGENT-TASK-DONE`.
- Bundled workspace template `.gitignore` now includes `.mneme/`.

### Changed (BREAKING)
- **Profiles are now markdown files.** The previous `.json` profile format
  has been removed entirely. Profiles live in `<workspace>/profiles/<name>.md`
  (workspace-local) or `<package>/mneme/profiles/<name>.md` (bundled). The
  YAML frontmatter carries the structured fields (`vocabulary`, `trace_types`,
  `requirement_levels`, `tone`, `voice`, `citation_style`,
  `placeholder_for_missing_refs`) and the body carries the writing-style
  prose under recognised H1 headings: `# Principles`, `# General Rules`,
  `# Terminology` (3-column markdown table), `# Framing: <context>` (parses
  `**Wrong:**` / `**Correct:**` / `**Why:**` blocks), `# Document Type:
  <slug>` (with nested `## Section: <slug>` blocks for per-section
  guidance), and `# Submission Checklist`. Unrecognised H1 headings are
  silently ignored (use them for authoring notes).
- **Bundled `eu-mdr.md` and `iso-13485.md` rewritten** under the new format,
  v2.0. The eu-mdr profile now ships a full `design-validation-report` style
  guide derived from real reviewer comments on a Tremor Detection Algorithm
  DVR (technical-not-clinical framing, terminology mapping, framing examples,
  submission checklist, per-section notes for context, datasets, methodology,
  equipment, sample-size justification, acceptance criteria, results,
  conclusion).
- **Profile schema rewritten as pure style guidance.** The
  `sections.<doc-type>.required` array (a list of mandatory heading slugs)
  has been removed from the in-memory profile shape. Profiles now carry
  free-form `section_notes` (per-section prose guidance) plus a top-level
  `writing_style` block (`principles`, `general_rules`, `terminology_guidance`,
  `framing_examples`, `placeholder_for_missing_refs`) and a
  `submission_checklist`.
- **Removed:** `mneme validate structure` and the underlying
  `validate_structure()` function. Mechanical heading-list checks didn't
  reflect what regulatory reviewers actually care about; the work belongs to
  an LLM agent reading the full style guide.

### Added
- **`mneme validate writing-style <page>`** assembles a "review packet" for an
  LLM agent: page content + active profile's writing-style block + matched
  section notes + submission checklist + ready-to-paste review prompt.
  Default output is human-readable markdown; `--json` gives raw structured
  output; `--out <file>` writes to a file.
- Document type is resolved from the page's frontmatter `type:` field. If the
  type matches a profile section, that section's `section_notes` are pulled
  in; otherwise the general writing_style block applies on its own.
- 8 new tests in `tests/test_profile.py::TestValidateWritingStyle`.

## [0.4.0] - 2026-04-07

### Added
- **`mneme resync <source-file> <client> [--dry-run]`** — diff-aware
  re-ingest. Performs a 3-way merge between the last clean-ingest baseline
  (ancestor), the current wiki page on disk (ours), and a fresh ingest of
  the updated source (theirs) using `git merge-file -p` with marker size
  7. Clean merges are written back, the baseline is advanced, and the
  schema is re-derived; identical outcomes are a no-op; missing baselines
  fall through to a standard ingest so resync is safe on never-ingested
  files. `--dry-run` previews the merged content and shows
  ancestor/ours/theirs/merged hashes without touching disk. For
  `ingest-csv`-derived pages, per-row updates are merged row-by-row and
  new rows become new pages.
- **`mneme resync-resolve <client/page>`** — follow-up command for
  conflicted resyncs. After the user edits out the
  `<<<<<<< current (ours)` / `======= baseline (ancestor)` /
  `>>>>>>> incoming (theirs)` markers in the wiki page, this command
  re-derives the schema, advances the baseline, and logs the resolution.
- **Automatic baseline snapshots** at `wiki/{client}/.baselines/{slug}.md`,
  written by every clean ingest. These sidecar files are the ancestor
  input for `mneme resync` and are hidden from `lint`, `drift`, `sync`,
  `search`, and repo scans via `EXCLUDED_DIRS`.
- New log operations: `RESYNC`, `RESYNC-CONFLICT`, `RESYNC-RESOLVED`.
- **Engine / workspace split** — `mneme` is now an installable Python package
  whose data lives in independent workspace directories.
- **`MNEME_HOME` environment variable** and **`--workspace` / `-w` global flag**
  so a single installed CLI can serve many projects.
- **`mneme new <dir>`** — scaffold a fresh workspace from a bundled template
  (`--name`, `--client`, `--profile`, `--description`, `--force`).
- **Bundled workspace template** at `mneme/templates/workspace/` with
  placeholder substitution.
- **`mneme demo clean`** — remove all demo content (default client
  `demo-retail`, demo folders, schema entries, memvid manifest, index/log).
- **`mneme --version` / `-V`** flag.
- `python -m mneme` entry point alongside the `mneme` console script.

### Changed
- `ingest_source_to_both()` now also writes a baseline sidecar to
  `wiki/{client}/.baselines/{slug}.md` on every successful ingest, so
  future `mneme resync` calls have an ancestor to merge against.
- Repository restructured into a real Python package (`mneme/` with
  `__init__.py`, `__main__.py`, `core.py`, `config.py`, `server.py`,
  `ui.html`, `profiles/`, `templates/`).
- Distribution name on PyPI is `mneme-cli`; the import package and CLI
  command remain `mneme`.
- Single source of truth for the version: `mneme/__init__.py`
  (`pyproject.toml` reads it dynamically).
- README install instructions updated for `pip install mneme-cli`.
- Stronger PyPI classifiers (MIT license, OS-independent, healthcare,
  scientific/engineering, office/business).
- Web UI server is now invoked as `python -m mneme.server`.

### Removed
- Bundled demo content (`demo/`, `demo-retail/`) and stale workspace data
  from the repository root.
- Redundant `requirements.txt` (dependencies live in `pyproject.toml`).

### Migration notes
- Old workspaces (anything created by `mneme init` before 0.4.0) still work
  unchanged — the project root is treated as a workspace by default.
- To run mneme against a workspace from anywhere:
  ```bash
  mneme --workspace /path/to/workspace stats
  # or
  export MNEME_HOME=/path/to/workspace
  mneme stats
  ```
- New projects should prefer `mneme new` over `mneme init`.

## [0.3.3] - 2026-04-06
- CODER.md, EXAMPLES.md, and QMS-focused README.
- Added memory-share feature spec to FEATURES.md.

## [0.3.2] - 2026-04-05
- CSV ingest with column mapping, auto-detection, and tornado integration.

## [0.3.1] - 2026-04-04
- 22 CLI commands, QMS features, profiles, traceability, and tornado inbox.
