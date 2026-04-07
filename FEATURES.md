# Mnemosyne - Feature Roadmap

## Current Features (v0.3.2)

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
| `mnemo ingest-csv` | CSV ingest: one row = one wiki page, with column-to-frontmatter mapping and auto trace links |
| `mnemo demo clean` | Remove all demo content: demo-retail client, demo/ folder, schema entries, memvid manifest, index/log entries |

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

#### Memory Share (`mnemo share`)

Selective knowledge sharing between team members or workspaces. You don't dump the whole repo -- you share curated slices with controlled scope, provenance tracking, and smart merge on import.

**Sub-commands:**

| Command | Purpose |
|---|---|
| `mnemo share slice` | Export selected pages by tag, type, trace chain, or explicit list |
| `mnemo share onboard` | Auto-generate starter package for new team members |
| `mnemo share inspect` | Preview package contents without importing |
| `mnemo share diff` | Compare package against local workspace before importing |
| `mnemo share receive` | Import package with merge strategy (skip/overwrite/append/ask) |
| `mnemo share provenance` | Show import history and origin for any page |

**Slice modes:**

```bash
# By tag
mnemo share slice --tag electrical-safety --output electrical-safety.mnemo

# By trace chain (follow links from a hazard to its tests)
mnemo share slice --trace-from haz-001 --output haz-001-chain.mnemo

# By trace chain with depth/scope controls
mnemo share slice --trace-from haz-001 --depth 3 --relations "mitigated-by,verified-by"

# By page type
mnemo share slice --type hazard --client cardio-monitor --output risk-register.mnemo

# By explicit page list
mnemo share slice --pages "un-001,req-003,dds-015,test-042" --output v-model.mnemo

# Exclude restricted pages by default
mnemo share slice --tag electrical-safety --output public.mnemo
# Pages with confidentiality: restricted are excluded unless --include-restricted
```

**Onboarding package (auto-generated):**

```bash
mnemo share onboard --client cardio-monitor --output onboarding.mnemo
# Auto-selects:
#   - overview page
#   - all entity pages (key players, products, technologies)
#   - open questions from all pages
#   - active profile (vocabulary + section rules)
#   - trace gap report (so they know what needs work)
#   - recent activity summary
```

**Inspect and diff before importing:**

```bash
mnemo share inspect package.mnemo
#   Package: electrical-safety
#   Origin: sarah@cardio-team, 2026-04-05
#   Pages: 12, Trace links: 8, Profile: eu-mdr
#   New pages (not in your workspace): 7
#   Conflicting pages (different content): 3
#   Identical pages (already up to date): 2

mnemo share diff package.mnemo --client my-project
#   NEW: cardio-monitor/rma-003.md
#   CONFLICT: cardio-monitor/req-007.md
#     Your version: updated 2026-04-01
#     Package version: updated 2026-04-05 (newer)
#     Diff: 3 lines changed
```

**Import with merge strategies:**

```bash
mnemo share receive package.mnemo --client my-project                     # default: skip existing
mnemo share receive package.mnemo --client my-project --strategy overwrite # replace with package version
mnemo share receive package.mnemo --client my-project --strategy append   # add as new section
mnemo share receive package.mnemo --client my-project --strategy ask      # interactive per conflict
```

**Package format (`.mnemo` = ZIP):**

```
package.mnemo
  manifest.json          <- package_id (UUID), origin_workspace, origin_user,
                            exported_at, base_snapshot, page_count, trace_count, profile
  profile.json           <- active profile at time of export
  wiki/{client}/*.md     <- selected pages only
  schema/
    entities.json        <- filtered to included pages
    tags.json            <- filtered to included pages
    traceability.json    <- filtered to included pages
```

**New frontmatter fields:**

```yaml
confidentiality: public|internal|restricted    # controls share visibility
provenance:                                     # set on import
  imported_from: "package-uuid"
  imported_at: "2026-04-06"
  origin_user: "sarah"
```

**Provenance tracking:**

```bash
mnemo share provenance cardio-monitor/req-007
#   Origin: created locally on 2026-03-15
#   Import 1: package abc-123 from sarah@cardio-team on 2026-04-01 (appended)
#   Import 2: package def-456 from raj@firmware-team on 2026-04-05 (overwritten)
```

**Delta sharing (incremental updates):**

```bash
# First export
mnemo share slice --tag electrical-safety --output v1.mnemo

# Later, after updates, export only changes
mnemo share slice --tag electrical-safety --output v2.mnemo --since v1.mnemo
#   Delta: 3 pages changed, 1 new, 0 removed
```

Checklist:
- [ ] `.mnemo` package format (ZIP with manifest, wiki, schema, profile)
- [ ] `mnemo share slice` with tag, type, trace-chain, page-list, and depth filters
- [ ] `mnemo share onboard` auto-generated starter package
- [ ] `mnemo share inspect` package preview
- [ ] `mnemo share diff` comparison against local workspace
- [ ] `mnemo share receive` with merge strategies (skip/overwrite/append/ask)
- [ ] `mnemo share provenance` import history per page
- [ ] `confidentiality` frontmatter field (public/internal/restricted)
- [ ] `provenance` frontmatter field (auto-set on import)
- [ ] Delta sharing via `--since` flag
- [ ] Manifest with UUID, origin, snapshot hash for dedup

Future (v2):
- [ ] `mnemo share publish` / `mnemo share subscribe` -- shared folder sync channel for team workflows

---

#### Other High Impact

- [ ] **Query-and-file command** - Search wiki, synthesize answer, file as a new deliverable page
- [ ] **Audit trail** - Per-page immutable change history with timestamps (ISO 9001 document control)
- [ ] **More source formats** - .docx, .json ingestion
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
