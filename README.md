<p align="center">
  <img src="https://raw.githubusercontent.com/tolism/mneme/main/assets/logo.png" alt="Mnemosyne" width="400">
</p>

<h1 align="center"></h1>



A CLI tool that turns your documents into a searchable second brain. Drop files in, get a structured knowledge layer out -- browsable by humans in Obsidian, queryable by machines in under 5ms.

```bash
pip install mneme-cli
mneme new ~/projects/my-research --name "My Research" --client acme-corp
cd ~/projects/my-research
mneme ingest proposal.pdf acme-corp
mneme search "delivery timeline"
```

One installed `mneme` CLI can serve many independent workspaces. Switch between them by `cd`-ing, exporting `MNEME_HOME`, or passing `--workspace /path/to/ws`.

That's it. Your knowledge compounds instead of decaying.

---

## Why

You're building a medical device. You have a risk analysis in a PDF, user needs in a spreadsheet, meeting notes in markdown, and 47 requirements in a CSV. An auditor asks "show me the trace from hazard HAZ-001 to the test that verifies its mitigation." You spend two hours searching folders.

Mneme fixes this:

```bash
# Import everything
mneme ingest risk-analysis.pdf cardio-monitor
mneme ingest-csv user-needs.csv cardio-monitor --mapping user-needs
mneme ingest-csv risk-register.csv cardio-monitor --mapping risk-register

# Answer the auditor in 2 seconds
mneme trace show cardio-monitor/haz-001 --direction forward
#   haz-001 (Electrical Shock)
#     mitigated-by -> rma-003 (Insulation Barrier)
#       implemented-by -> req-007 (Double Insulation)
#         verified-by -> test-042 (Dielectric Strength Test)

# Find gaps before the auditor does
mneme trace gaps cardio-monitor
#   Requirements with no verification: req-011, req-023
#   Hazards with no mitigation: haz-009
```

Every document ingested once. Every trace link tracked. Every vocabulary term harmonized. Every gap found automatically.

No databases. No servers. No infrastructure. Plain markdown files + JSON schemas that any system can read.

---

## Install

```bash
pip install mneme-cli
```

Or from source:

```bash
git clone https://github.com/tolism/mneme.git
cd mneme
pip install -e .
```

You now have the `mneme` command globally. Verify with `mneme --help`.

**Optional:** For PDF support, `pip install "mneme-cli[pdf]"`. For everything, `pip install "mneme-cli[all]"`.

**Requirements:** Python 3.9+. Works on macOS, Linux, Windows.

---

## Quick Start

```bash
# Scaffold a new workspace (from anywhere)
mneme new ~/projects/my-project --name "My Project" --client client-a

cd ~/projects/my-project

# Ingest some documents
mneme ingest report.pdf client-a
mneme ingest meeting-notes.md client-a

# Search across everything
mneme search "quarterly budget"

# Check health
mneme stats

# Launch the web dashboard
python -m mneme.server    # http://localhost:3141
```

### Run mneme against any workspace

```bash
mneme --workspace ~/projects/parkiwatch stats     # one-shot
export MNEME_HOME=~/projects/parkiwatch           # sticky for the shell
mneme stats
```

One installed CLI serves many projects — each workspace is just a directory.

---

## CLI

| Command | What It Does |
|---|---|
| `mneme new <dir>` | Scaffold a new workspace from the bundled template |
| `mneme init` | Scaffold a workspace in cwd (legacy) |
| `mneme --workspace <dir>` | Run any command against a specific workspace |
| `mneme ingest <file> <client>` | Ingest a source document |
| `mneme resync <file> <client>` | Re-ingest an updated source via 3-way merge, preserving hand edits |
| `mneme resync-resolve <client/page>` | Finalize a conflicted resync after editing out markers |
| `mneme search "<query>"` | Search across all layers |
| `mneme sync` | Sync wiki to Memvid memory |
| `mneme drift` | Detect layer desynchronization |
| `mneme stats` | Health overview |
| `mneme repair` | Fix corrupted archives |

**Formats:** `.md`, `.txt`, `.pdf`

---

## How It Works

```
    Your Document
         |
         v
    mneme ingest
         |
         +---> Wiki Layer (markdown, Obsidian-compatible)
         |       Frontmatter, citations, [[wikilinks]]
         |       You read and browse here
         |
         +---> Memory Layer (.mv2 archive)
         |       Smart Frames, semantic embeddings
         |       Machines query here (<5ms)
         |
         +---> Schema Layer (JSON)
                 entities.json - people, companies, products
                 graph.json   - relationships between entities
                 tags.json    - taxonomy
```

Every `mneme ingest` writes both layers atomically. `mneme drift` catches desync. `mneme repair` fixes it.

**Memvid is optional.** Without it, mneme runs as a wiki-only knowledge base with text search. Add `memvid-sdk` when you outgrow grep.

---

## Obsidian Integration

A mneme workspace *is* an Obsidian vault. The wiki pages use YAML frontmatter and `[[wikilinks]]`, so Obsidian indexes everything natively.

**Open a workspace as a vault:**

1. Open Obsidian → *Open folder as vault* → select your workspace directory (e.g. `~/projects/parkiwatch`)
2. Obsidian creates `.obsidian/` inside the workspace on first open — this is safe and mneme ignores it
3. Browse `wiki/` in the file explorer; click any page to render with backlinks, graph view, and tag search

**Recommended Obsidian settings:**

- **Files & Links → Default location for new notes:** `wiki/{default-client}/`
- **Files & Links → New link format:** `Relative path to file`
- **Files & Links → Use [[Wikilinks]]:** ON
- **Files & Links → Detect all file extensions:** OFF (keeps `sources/` archive out of the graph)

**Useful community plugins:**

| Plugin | Why |
|---|---|
| **Dataview** | Query frontmatter: list all pages with `type: hazard`, `confidence: low`, etc. |
| **Templater** | Paste mneme page frontmatter from a snippet |
| **Tag Wrangler** | Visualise the same tags mneme tracks in `schema/tags.json` |
| **Graph Analysis** | See the entity relationships mneme builds in `schema/graph.json` |

**Workflow:**

```bash
# Ingest new docs from the CLI
mneme ingest meeting.pdf parkiwatch

# Obsidian auto-detects the new wiki page
# Read, link, and annotate in Obsidian
# mneme lint catches dead links on your next run
mneme lint
```

Sync the workspace via Dropbox, iCloud, or git and you have multi-device Obsidian + mneme.

---

## Profiles (and custom profiles)

A profile defines the vocabulary and document structure rules for a regulatory framework. mneme ships two bundled profiles:

| Profile | Use when |
|---|---|
| `eu-mdr` | EU Medical Device Regulation (2017/745) -- 15 vocabulary rules, 6 section templates |
| `iso-13485` | ISO 13485:2016 QMS -- 13 vocabulary rules, 6 section templates |

Activate one in any workspace with `mneme profile set eu-mdr`. From then on, `mneme harmonize` enforces vocabulary, `mneme validate writing-style` builds an LLM review packet for prose, and `mneme validate consistency` checks cross-document standard versions.

### Adding your own profile

Profiles are just JSON files in `<workspace>/profiles/`. **No reinstall, no rebuild, no PR to mneme.** Drop a file in, activate it, you're done.

```bash
# 1. mneme new already creates the profiles/ folder for you
mneme new ~/projects/parkiwatch --name Parkiwatch --client parkiwatch
cd ~/projects/parkiwatch

# 2. Drop your profile in (use any text editor or this heredoc)
cat > profiles/parkiwatch-qms.json <<'EOF'
{
  "name": "Parkiwatch QMS",
  "description": "Internal quality framework for the Parkiwatch product line",
  "version": "1.0",
  "vocabulary": {
    "preferred": [
      { "term": "parking violation", "reject": ["parking ticket", "infraction"] },
      { "term": "enforcement officer", "reject": ["meter maid", "warden"] }
    ],
    "requirement_levels": { "shall": "mandatory", "should": "recommended" }
  },
  "sections": {
    "incident-report": {
      "required": ["incident-id", "location", "timestamp", "evidence"],
      "description": "Standard parking incident structure"
    }
  }
}
EOF

# 3. Activate and verify
mneme profile set parkiwatch-qms
mneme profile show
#   Active profile: Parkiwatch QMS
#     Vocabulary rules: 2
#     Section templates: 1

# 4. Use it
mneme harmonize parkiwatch          # flag "parking ticket" -> should be "parking violation"
mneme harmonize parkiwatch --fix    # auto-fix vocabulary
mneme validate writing-style parkiwatch/incident-001 > review.md  # paste into Claude
```

### How resolution works

When you run `mneme profile set <name>`, mneme looks in two places, in order:

1. **First:** `<workspace>/profiles/<name>.json` (your local profile)
2. **Then:** `<installed-mneme>/profiles/<name>.json` (the bundled `eu-mdr` / `iso-13485`)

The first one wins. So you can:

- **Add a brand-new framework** mneme doesn't ship -- just give it a unique name (e.g. `parkiwatch-qms.json`, `acme-internal.json`)
- **Override a bundled framework** with project-specific tweaks -- create your own `eu-mdr.json` in the workspace and it shadows the bundled one for that project only

The same shadowing rule applies to CSV column mappings under `<workspace>/profiles/mappings/`, used by `mneme ingest-csv`.

If neither file exists, you get a clear error listing both paths it checked.

### What goes into a profile

A profile is a JSON file. It carries **vocabulary rules** (mechanically enforced by `mneme harmonize`) and **writing-style guidance** (free-form prose consumed by an LLM agent via `mneme validate writing-style`).

| Field | What it does | Used by |
|---|---|---|
| `name`, `description`, `version` | Display metadata | `mneme profile show` |
| `vocabulary.preferred[].term` / `.reject[]` | Terminology swaps | `mneme harmonize` (mechanical) |
| `vocabulary.requirement_levels` | Reserved words like `shall`, `should`, `may` | Documentation |
| `sections.<doc-type>.description` | Per-document-type description | `mneme profile show` |
| `sections.<doc-type>.section_notes` | Per-section prose guidance for an LLM agent | `mneme validate writing-style` |
| `writing_style.principles` | High-level principles (e.g. "reproducibility", "technical not clinical") | `mneme validate writing-style` |
| `writing_style.general_rules` | Cross-cutting writing rules | `mneme validate writing-style` |
| `writing_style.terminology_guidance` | Phrase-level use-this/instead-of/why | `mneme validate writing-style` |
| `writing_style.framing_examples` | Worked correct/incorrect pairs with rationale | `mneme validate writing-style` |
| `submission_checklist` | Pre-submission go/no-go items | `mneme validate writing-style` + reviewer |
| `trace_types` | Allowed relationship types for trace links | Documentation |
| `tone`, `voice`, `citation_style` | Style hints | `mneme profile show` |

**Important:** profiles do NOT enforce a list of required headings. Mechanical heading checks were removed in v0.4.0 because they don't reflect what regulatory reviewers actually care about. Instead, use `mneme validate writing-style <page>` to build a review packet that an LLM agent grades against the full style guide.

See `EXAMPLES.md` Example 13 for a full walkthrough with a real Parkiwatch scenario, and copy a bundled profile (`eu-mdr.json`, `iso-13485.json`) from the installed package as a starting template.

---

## Web Dashboard

`python -m mneme.server` -- opens at `http://localhost:3141`

- **Dashboard** -- stats, per-client counts, activity log
- **Search** -- dual-layer results with source attribution
- **Wiki** -- browse all pages with rendered markdown
- **Entities** -- filterable table of extracted entities
- **Health** -- drift status, sync state

---

## When You Need This

| Scale | Wiki alone | Wiki + Memvid |
|---|---|---|
| 5 docs | Plenty | Overkill |
| 50 docs | Fine | Starting to help |
| 500 docs | Grep takes 2-3s, misses semantic matches | 2ms, cross-client connections |
| 5,000 docs | Unusable | Still 2ms |

Start wiki-only. Add the memory layer when search gets slow.

---

## Project Structure

```
mneme/
  sources/        Raw documents (immutable, never modified)
  wiki/           Markdown knowledge pages (Obsidian-compatible)
  schema/         entities.json, graph.json, tags.json
  memvid/         .mv2 memory archives
  core.py         Engine (ingest, search, sync, drift, repair)
  config.py       Configuration
  server.py       Web dashboard
  index.md        Master page catalog
  log.md          Activity timeline
```

---

## Downstream Use

Mneme outputs plain files -- markdown and JSON. Any system can read them. The CLI is designed to be called programmatically by other applications.

**Next up:** Mneme as the knowledge backend for a QMS (Quality Management System) -- quality documentation, audit trails, compliance evidence, all searchable.

---

## Releasing (maintainers)

Mneme ships to PyPI as `mneme`. To cut a new release:

```bash
# 1. Bump the version in mneme/__init__.py and pyproject.toml
# 2. Install release tooling
pip install -e ".[release]"

# 3. Dry run to TestPyPI first
scripts/release.sh test              # bash (macOS/Linux/WSL)
scripts\release.ps1 test             # PowerShell (Windows)

pip install --index-url https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple/ mneme

# 4. Production
scripts/release.sh prod              # bash
scripts\release.ps1 prod             # PowerShell
```

The script cleans `dist/`, runs `python -m build`, validates with `twine check`, and uploads.

You'll need a PyPI API token in `~/.pypirc`:

```ini
[distutils]
index-servers =
    pypi
    testpypi

[pypi]
username = __token__
password = pypi-AgEI...           # from https://pypi.org/manage/account/token/

[testpypi]
repository = https://test.pypi.org/legacy/
username = __token__
password = pypi-AgENd...          # from https://test.pypi.org/manage/account/token/
```

---

## Credits

This project builds on two foundational ideas:

- **LLM Wiki pattern** by [Andrej Karpathy](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) -- the insight that LLMs should build and maintain a persistent, compounding wiki instead of re-deriving answers from raw documents on every query
- **Memvid** by [Olow304/memvid](https://github.com/Olow304/memvid) -- single-file AI memory with sub-millisecond retrieval, no vector DB required
- **Original implementation** -- [tashisleepy/knowledge-engine](https://github.com/tashisleepy/knowledge-engine) -- the first version that fused both patterns into a dual-layer bridge

---

## License

MIT
