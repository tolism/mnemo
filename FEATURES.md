# Mnemosyne - Feature Roadmap

## Current Features (v0.4.0)

### Workspace Model

* **Engine / workspace split** -- mneme is an installable package; user data lives in independent workspace directories
* **MNEME_HOME env var** + **`--workspace` global flag** -- one mneme CLI serves many projects
* **`mneme new <dir>`** -- scaffold a fresh workspace from the bundled template (project name, default client, profile, description)
* `python -m mneme` and `mneme` entry points both work


### CLI Commands

| Command | Description |
|---|---|
| `mneme new` | Scaffold a new workspace from the bundled template (preferred over `init`) |
| `mneme init` | Scaffold a workspace in cwd (legacy) |
| `mneme --workspace <dir>` / `MNEME_HOME=<dir>` | Run any command against a specific workspace |
| `mneme ingest` | Atomic ingest: source -> wiki + Memvid + schema |
| `mneme resync` | Diff-aware re-ingest: 3-way merge (baseline / wiki / fresh ingest) via `git merge-file` |
| `mneme resync-resolve` | Mark a conflicted resync page as resolved after editing out markers |
| `mneme ingest-dir` | Batch ingest all files from a directory |
| `mneme search` | Dual-layer search with `--client` scoping |
| `mneme lint` | Health check: orphan pages, dead links, stale pages, citations, schema drift, coverage |
| `mneme sync` | Sync wiki pages to Memvid |
| `mneme drift` | Detect layer desynchronization |
| `mneme stats` | Health overview |
| `mneme repair` | Fix corrupted archives and schema |
| `mneme status` | Quick summary of pending work |
| `mneme recent` | Show last N activity log entries |
| `mneme tags list` | List all tags with page counts |
| `mneme tags merge` | Merge one tag into another across all pages |
| `mneme diff` | Git-aware diff for a wiki page |
| `mneme snapshot` | Versioned zip archive of a client + git tag |
| `mneme dedupe` | Detect near-duplicate wiki pages |
| `mneme export` | Export client knowledge as JSON or markdown |
| `mneme profile list` | List available writing style profiles |
| `mneme profile set` | Set active profile (e.g. eu-mdr, iso-13485) |
| `mneme profile show` | Show active profile details |
| `mneme trace add` | Add a traceability link between pages |
| `mneme trace show` | Walk trace chain forward or backward |
| `mneme trace matrix` | Generate traceability matrix for a client |
| `mneme trace gaps` | Find incomplete trace chains |
| `mneme harmonize` | Vocabulary harmonization against active profile |
| `mneme validate writing-style` | Build a writing-style review packet for an LLM agent (replaces `validate structure`) |
| `mneme validate consistency` | Cross-document consistency check |
| `mneme scan-repo` | Scan code repo, compare against QMS docs, find gaps |
| `mneme tornado` | Inbox processor: auto-detect type/client, ingest, archive to sources |
| `mneme ingest-csv` | CSV ingest: one row = one wiki page, with column-to-frontmatter mapping and auto trace links |
| `mneme demo clean` | Remove all demo content: demo-retail client, demo/ folder, schema entries, memvid manifest, index/log entries |

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

### Workspace-local Profiles (v0.4.0+)

Drop a `<name>.json` file into `{workspace}/profiles/` and it becomes available
to `mneme profile set`, `harmonize`, and `validate writing-style`. Workspace profiles
**shadow** any bundled profile with the same name, so a project can override
an industry profile with project-specific tweaks. The same applies to CSV
column mappings under `{workspace}/profiles/mappings/`. `mneme new` scaffolds
the directory automatically.

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

#### Memory Share (`mneme share`)

Selective knowledge sharing between team members or workspaces. You don't dump the whole repo -- you share curated slices with controlled scope, provenance tracking, and smart merge on import.

**Sub-commands:**

| Command | Purpose |
|---|---|
| `mneme share slice` | Export selected pages by tag, type, trace chain, or explicit list |
| `mneme share onboard` | Auto-generate starter package for new team members |
| `mneme share inspect` | Preview package contents without importing |
| `mneme share diff` | Compare package against local workspace before importing |
| `mneme share receive` | Import package with merge strategy (skip/overwrite/append/ask) |
| `mneme share provenance` | Show import history and origin for any page |

**Slice modes:**

```bash
# By tag
mneme share slice --tag electrical-safety --output electrical-safety.mneme

# By trace chain (follow links from a hazard to its tests)
mneme share slice --trace-from haz-001 --output haz-001-chain.mneme

# By trace chain with depth/scope controls
mneme share slice --trace-from haz-001 --depth 3 --relations "mitigated-by,verified-by"

# By page type
mneme share slice --type hazard --client cardio-monitor --output risk-register.mneme

# By explicit page list
mneme share slice --pages "un-001,req-003,dds-015,test-042" --output v-model.mneme

# Exclude restricted pages by default
mneme share slice --tag electrical-safety --output public.mneme
# Pages with confidentiality: restricted are excluded unless --include-restricted
```

**Onboarding package (auto-generated):**

```bash
mneme share onboard --client cardio-monitor --output onboarding.mneme
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
mneme share inspect package.mneme
#   Package: electrical-safety
#   Origin: sarah@cardio-team, 2026-04-05
#   Pages: 12, Trace links: 8, Profile: eu-mdr
#   New pages (not in your workspace): 7
#   Conflicting pages (different content): 3
#   Identical pages (already up to date): 2

mneme share diff package.mneme --client my-project
#   NEW: cardio-monitor/rma-003.md
#   CONFLICT: cardio-monitor/req-007.md
#     Your version: updated 2026-04-01
#     Package version: updated 2026-04-05 (newer)
#     Diff: 3 lines changed
```

**Import with merge strategies:**

```bash
mneme share receive package.mneme --client my-project                     # default: skip existing
mneme share receive package.mneme --client my-project --strategy overwrite # replace with package version
mneme share receive package.mneme --client my-project --strategy append   # add as new section
mneme share receive package.mneme --client my-project --strategy ask      # interactive per conflict
```

**Package format (`.mneme` = ZIP):**

```
package.mneme
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
mneme share provenance cardio-monitor/req-007
#   Origin: created locally on 2026-03-15
#   Import 1: package abc-123 from sarah@cardio-team on 2026-04-01 (appended)
#   Import 2: package def-456 from raj@firmware-team on 2026-04-05 (overwritten)
```

**Delta sharing (incremental updates):**

```bash
# First export
mneme share slice --tag electrical-safety --output v1.mneme

# Later, after updates, export only changes
mneme share slice --tag electrical-safety --output v2.mneme --since v1.mneme
#   Delta: 3 pages changed, 1 new, 0 removed
```

Checklist:
- [ ] `.mneme` package format (ZIP with manifest, wiki, schema, profile)
- [ ] `mneme share slice` with tag, type, trace-chain, page-list, and depth filters
- [ ] `mneme share onboard` auto-generated starter package
- [ ] `mneme share inspect` package preview
- [ ] `mneme share diff` comparison against local workspace
- [ ] `mneme share receive` with merge strategies (skip/overwrite/append/ask)
- [ ] `mneme share provenance` import history per page
- [ ] `confidentiality` frontmatter field (public/internal/restricted)
- [ ] `provenance` frontmatter field (auto-set on import)
- [ ] Delta sharing via `--since` flag
- [ ] Manifest with UUID, origin, snapshot hash for dedup

Future (v2):
- [ ] `mneme share publish` / `mneme share subscribe` -- shared folder sync channel for team workflows

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
