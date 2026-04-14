# {{PROJECT_NAME}}

A mneme knowledge workspace.

## Quick start

```bash
# from inside this directory
mneme stats
mneme ingest path/to/document.md {{DEFAULT_CLIENT}}
mneme search "your query"
```

Or from anywhere:

```bash
mneme --workspace /path/to/{{PROJECT_SLUG}} stats
```

## Layout

```
{{PROJECT_SLUG}}/
  wiki/{{DEFAULT_CLIENT}}/   structured markdown pages
  sources/{{DEFAULT_CLIENT}}/  immutable source archive
  schema/                    entities, graph, tags, traceability
  search.db                  SQLite FTS5 search index (created on first sync)
  inbox/                     drop files here, run `mneme tornado`
  index.md                   master catalog
  log.md                     activity timeline
```

Created with `mneme new` on {{CREATED_DATE}}.
