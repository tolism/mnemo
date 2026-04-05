<p align="center">
  <img src="assets/logo.png" alt="Mnemosyne" width="400">
</p>

<h1 align="center"></h1>



A CLI tool that turns your documents into a searchable second brain. Drop files in, get a structured knowledge layer out -- browsable by humans in Obsidian, queryable by machines in under 5ms.

```bash
pip install -e .
mnemo init --project "My Research" --clients "acme-corp"
mnemo ingest proposal.pdf acme-corp
mnemo search "delivery timeline"
```

That's it. Your knowledge compounds instead of decaying.

---

## Why

You have 200 documents across 5 projects. Someone asks "what did we agree on pricing?" and you spend 20 minutes digging through folders.

Mnemo fixes this:

1. **You ingest** -- drop a PDF, markdown, or text file
2. **Mnemo builds** -- a wiki page (human-readable) + a memory frame (machine-searchable) + entity extraction, all in one atomic operation
3. **You query** -- `mnemo search "pricing agreement"` returns results in milliseconds with citations back to the source

No databases. No servers. No infrastructure. Plain markdown files + a single `.mv2` archive you can copy anywhere.

---

## Install

```bash
git clone https://github.com/tolism/mnemo.git
cd mnemo
pip install -e .
```

You now have the `mnemo` command globally. Verify with `mnemo stats`.

**Optional:** For PDF support, install with `pip install -e ".[pdf]"`. For everything, `pip install -e ".[all]"`.

**Requirements:** Python 3.9+. Works on macOS, Linux, Windows.

---

## Quick Start

```bash
# Initialize a knowledge base
mnemo init --project "My Project" --clients "client-a,client-b"

# Ingest some documents
mnemo ingest report.pdf client-a
mnemo ingest meeting-notes.md client-a
mnemo ingest market-research.txt client-b

# Search across everything
mnemo search "quarterly budget"

# Check health
mnemo stats

# Launch the web dashboard
python3 server.py    # http://localhost:3141
```

### Try the demo

```bash
mnemo ingest demo/sample-proposal.md demo-retail
mnemo ingest demo/sample-meeting-notes.md demo-retail
mnemo ingest demo/sample-research.md demo-retail
mnemo search "RetailCorp budget"
```

---

## CLI

| Command | What It Does |
|---|---|
| `mnemo init` | Scaffold a new knowledge base |
| `mnemo ingest <file> <client>` | Ingest a source document |
| `mnemo search "<query>"` | Search across all layers |
| `mnemo sync` | Sync wiki to Memvid memory |
| `mnemo drift` | Detect layer desynchronization |
| `mnemo stats` | Health overview |
| `mnemo repair` | Fix corrupted archives |

**Formats:** `.md`, `.txt`, `.pdf`

---

## How It Works

```
    Your Document
         |
         v
    mnemo ingest
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

Every `mnemo ingest` writes both layers atomically. `mnemo drift` catches desync. `mnemo repair` fixes it.

**Memvid is optional.** Without it, mnemo runs as a wiki-only knowledge base with text search. Add `memvid-sdk` when you outgrow grep.

---

## Web Dashboard

`python3 server.py` -- opens at `http://localhost:3141`

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
mnemo/
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

Mnemo outputs plain files -- markdown and JSON. Any system can read them. The CLI is designed to be called programmatically by other applications.

**Next up:** Mnemo as the knowledge backend for a QMS (Quality Management System) -- quality documentation, audit trails, compliance evidence, all searchable.

---

## Credits

This project builds on two foundational ideas:

- **LLM Wiki pattern** by [Andrej Karpathy](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) -- the insight that LLMs should build and maintain a persistent, compounding wiki instead of re-deriving answers from raw documents on every query
- **Memvid** by [Olow304/memvid](https://github.com/Olow304/memvid) -- single-file AI memory with sub-millisecond retrieval, no vector DB required
- **Original implementation** -- [tashisleepy/knowledge-engine](https://github.com/tashisleepy/knowledge-engine) -- the first version that fused both patterns into a dual-layer bridge

---

## License

MIT
