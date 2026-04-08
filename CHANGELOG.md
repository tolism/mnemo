# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
