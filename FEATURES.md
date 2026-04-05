# Mnemosyne - Feature Roadmap

## Current Features (v0.1.0)

### CLI Commands

| Command | Description |
|---|---|
| `init` | Scaffold a new workspace (dirs, schema, index, log, CLAUDE.md) |
| `ingest` | Atomic ingest: source file -> wiki page + Memvid frames + schema |
| `search` | Dual-layer search (wiki text + Memvid semantic) |
| `sync` | Push wiki pages into Memvid with content-hash dedup |
| `drift` | Detect when wiki and Memvid layers are out of sync |
| `stats` | Health overview across all layers |
| `repair` | Fix corrupted .mv2 archives and schema JSON |

### Web UI (localhost:3141)

- Dashboard with stats cards and activity log
- Dual-layer search with source badges
- Wiki browser with rendered markdown and frontmatter
- Entity table with filtering
- Health tab with drift and sync status

### Core Capabilities

- Dual-layer architecture: human-readable wiki + machine-searchable Memvid
- Automatic entity extraction during ingest
- Content hashing for dedup and drift detection
- Cross-platform file locking (portalocker)
- PDF support via PyMuPDF (optional)
- Obsidian-compatible wikilinks and frontmatter
- Schema tracking (entities.json, graph.json, tags.json)

---

## Planned Features

### High Impact

- [ ] **Lint command** - Orphan pages, dead links, stale pages, missing citations, schema drift, coverage gaps. Protocol defined in CLAUDE.md but not yet implemented in CLI.
- [ ] **Query-and-file command** - Search wiki, synthesize answer, optionally file it as a new deliverable or comparison page. Turns repeated questions into permanent wiki entries.
- [ ] **Batch ingest** - `mnemo ingest-dir sources/pdfs/ my-client` to walk a directory and ingest all files in one pass.
- [ ] **Export / report generation** - Pull knowledge out as a combined brief per client. Markdown or PDF output for handoff.

### Medium Impact

- [ ] **Watch mode** - Monitor `sources/` for new files and auto-ingest on drop.
- [ ] **Tag management CLI** - `mnemo tags list`, `mnemo tags merge old-tag new-tag`.
- [ ] **Entity graph visualization** - Render graph.json as Mermaid or DOT. Viewable in web UI or exported as image.
- [ ] **Confidence auto-upgrade** - When a second source confirms a low-confidence claim, promote to medium. Cross-reference counting drives the upgrade.
- [ ] **Interactive search UI** - Live-as-you-type search, clickable entity links, richer result cards.

### Nice to Have

- [ ] **Page diff** - `mnemo diff <page>` to show what changed since last ingest (git-aware).
- [ ] **More source formats** - .docx, .csv, .json ingestion support.
- [ ] **Web UI auth** - Basic authentication for LAN sharing.
- [ ] **Client archival** - `mnemo archive <client>` to freeze a client engagement and remove from active indexes.
- [ ] **Merge clients** - Consolidate two client directories when engagements overlap.

---

## Security Fixes

- [ ] **Path traversal in server.py** - Replace `str.replace('..',  '')` with `pathlib.Path.resolve()` + bounds checking in `handle_wiki_page()`.
- [ ] **Restrict file serving scope** - Remove BASE_DIR from wiki page candidate paths, limit to WIKI_DIR only.
- [ ] **CORS policy** - Replace wildcard `Access-Control-Allow-Origin: *` with explicit origin.
