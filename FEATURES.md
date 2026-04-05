# Mnemosyne - Feature Roadmap

## Current Features (v0.3.1)

### CLI Commands

| Command | Description |
|---|---|
| `mnemo init` | Scaffold a new workspace |
| `mnemo ingest` | Atomic ingest: source -> wiki + Memvid + schema |
| `mnemo ingest-dir` | Batch ingest all files from a directory |
| `mnemo search` | Dual-layer search with `--client` scoping |
| `mnemo lint` | Health check: orphan pages, dead links, stale pages, citations, schema drift, coverage |
| `mnemo sync` | Sync wiki pages to Memvid |
| `mnemo drift` | Detect layer desynchronization |
| `mnemo stats` | Health overview |
| `mnemo repair` | Fix corrupted archives and schema |
| `mnemo status` | Quick summary of pending work |
| `mnemo recent` | Show last N activity log entries |
| `mnemo tags list` | List all tags with page counts |
| `mnemo tags merge` | Merge one tag into another across all pages |
| `mnemo diff` | Git-aware diff for a wiki page |
| `mnemo snapshot` | Versioned zip archive of a client + git tag |
| `mnemo dedupe` | Detect near-duplicate wiki pages |
| `mnemo export` | Export client knowledge as JSON or markdown |
| `mnemo profile list` | List available writing style profiles |
| `mnemo profile set` | Set active profile (e.g. eu-mdr, iso-13485) |
| `mnemo profile show` | Show active profile details |
| `mnemo trace add` | Add a traceability link between pages |
| `mnemo trace show` | Walk trace chain forward or backward |
| `mnemo trace matrix` | Generate traceability matrix for a client |
| `mnemo trace gaps` | Find incomplete trace chains |
| `mnemo harmonize` | Vocabulary harmonization against active profile |
| `mnemo validate structure` | Check page sections against profile requirements |
| `mnemo validate consistency` | Cross-document consistency check |
| `mnemo scan-repo` | Scan code repo, compare against QMS docs, find gaps |
| `mnemo tornado` | Inbox processor: auto-detect type/client, ingest, archive to sources |

### Web UI (localhost:3141)

- Dashboard with stats cards and activity log
- Dual-layer search with source badges
- Wiki browser with rendered markdown and frontmatter
- Entity table with filtering
- Health tab with drift and sync status

### Bundled Profiles

| Profile | Description |
|---|---|
| `eu-mdr` | EU Medical Device Regulation (2017/745) -- 15 vocabulary rules, 6 section templates |
| `iso-13485` | ISO 13485:2016 QMS for Medical Devices -- 13 vocabulary rules, 6 section templates |

### Traceability Chains Supported

```
User Need -> Requirement -> Design Input -> DDS -> Verification -> Validation

Hazard -> Risk Analysis -> RMA -> Requirement -> DDS -> Test
```

Relationship types: `derived-from`, `implemented-by`, `detailed-in`, `mitigated-by`, `verified-by`, `validated-by`, `referenced-in`, `supersedes`

### Security (v0.2.0+)

- Path traversal protection using `pathlib.Path.resolve()` with bounds checking
- File serving restricted to WIKI_DIR only
- CORS locked to `localhost:3141`

---

## Planned Features

### High Impact

- [ ] **Query-and-file command** - Search wiki, synthesize answer, file as a new deliverable page
- [ ] **Audit trail** - Per-page immutable change history with timestamps (ISO 9001 document control)
- [ ] **More source formats** - .docx, .csv, .json ingestion
- [ ] **Profile-aware ingest** - Auto-apply vocabulary and section rules during ingest, not just at lint time

### Medium Impact

- [ ] **Watch mode** - Monitor `sources/` for new files and auto-ingest
- [ ] **Entity graph visualization** - Render graph.json as Mermaid/DOT
- [ ] **Confidence auto-upgrade** - Promote confidence when multiple sources confirm a claim
- [ ] **Interactive search UI** - Live search, clickable entities
- [ ] **Trace visualization** - Render traceability chains as diagrams in the web UI
- [ ] **Client archival** - Freeze an engagement and remove from active indexes
- [ ] **Merge clients** - Consolidate two client directories

### Nice to Have

- [ ] **Web UI auth** - Basic auth for LAN sharing
- [ ] **Custom profile builder** - Interactive CLI to create profiles from scratch
- [ ] **Regulatory report generator** - Auto-generate compliance checklists from profile + trace data
