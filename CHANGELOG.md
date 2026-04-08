# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-04-07

### Added
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
