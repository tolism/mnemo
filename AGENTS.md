# AGENTS.md

This document is the contract for any LLM agent driving the `mneme` CLI.
It complements `CLAUDE.md` (which describes the wiki-layer protocol) by
describing **the agent's job and how mneme helps you do it**. Read this
file once at the start of a session and keep it in context.

You are an agent driving mneme. Mneme is the substrate. You are the writer,
the reviewer, and the judge. Mneme gives you structure, evidence, vocabulary
rules, and a task graph. It does not call any LLM. Every intelligent action
is yours.

---

## tl;dr — the 30-second version

```bash
# 1. Understand the workspace
mneme stats
mneme profile show

# 2. Turn the user's goal into a plan
mneme agent plan --goal "produce a DVR for the TDA algorithm" \
                 --doc-type design-validation-report --client tda

# 3. Walk the plan, one task at a time, until done
mneme agent next-task                      # read envelope, run next_command
mneme draft --doc-type ... --section ...   # (or whatever next_command says)
# (do the intelligent work)
mneme agent task-done <task-id>
# repeat
```

When `mneme agent next-task` returns `{done: true}`, you are finished.

---

## 1. What mneme is (and is not)

- **Mneme is a substrate.** It stores evidence (`sources/`), a structured
  wiki (`wiki/`), and machine-readable schema (`schema/`). It knows how to
  ingest, search, trace, harmonize, resync, draft, and plan. It does not
  know how to write.
- **You are the writer.** Mneme assembles contracts — write packets, review
  packets, task envelopes — and hands them to you. You produce the prose.
- **The wiki is the source of truth, not the source files.** Query the wiki.
  Only fall through to `sources/` when the wiki is empty on a topic.
- **Profiles are the style contract.** A profile is a markdown file with
  YAML frontmatter at `<package>/profiles/<name>.md` or
  `<workspace>/profiles/<name>.md`. Workspace profiles shadow bundled ones.
- **Mneme does NOT call any LLM.** It builds prompts and packets; you act
  on them. This is deliberate — mneme stays deterministic and cheap.
- **Sources are immutable.** Never modify anything under `sources/`.

---

## 2. The workspace shape you can rely on

A mneme workspace is a directory. Its shape is stable across versions:

```
<workspace>/
  sources/           immutable evidence, one subdir per client
  wiki/              markdown pages, Obsidian-compatible
    {client}/        one dir per client engagement
    _shared/         cross-client knowledge
  schema/
    entities.json    registered entities
    graph.json       relationship graph
    tags.json        tag registry
    traceability.json  trace links between pages
  search.db          SQLite FTS5 search index (rebuilt from wiki)
  profiles/          workspace-local profiles and CSV mappings
    mappings/        JSON column mappings for ingest-csv
  exports/           JSON / markdown exports
  snapshots/         versioned zip archives
  .mneme/
    agent-plans/     agent plan state (gitignored)
  index.md           master catalog of wiki pages
  log.md             append-only activity timeline
```

State discovery rituals — run these before doing anything destructive:

| Command | What it tells you |
|---|---|
| `mneme stats` | Source count, wiki count, entities, tags, drift |
| `mneme status` | What is pending (un-ingested sources, orphans) |
| `mneme recent -n 10` | What happened recently, who did it |
| `mneme profile show` | Active profile, vocabulary rules, doc types |

If `mneme profile show` prints "no active profile", **stop and set one
before writing any prose**. Prose without a profile has no style contract
and cannot be graded.

---

## 3. The five operations you'll do most

### 3.1 INGEST — bringing evidence into the workspace

```bash
mneme ingest <file> <client>
mneme ingest-dir <directory> <client>
mneme ingest-csv <file> <client> [--mapping <name>]
mneme tornado --client <client>          # batch from inbox/
```

`ingest` is atomic: it writes the wiki page, updates the schema, and
indexes the page in SQLite FTS5 in one operation. `ingest-csv` produces one
wiki page per row, with trace links derived from the mapping. `tornado`
is a bulk inbox processor — it auto-detects page type and routes CSVs
through `ingest-csv`, everything else through `ingest`.

Rule: never re-run `ingest` on a file that has a baseline. Use `resync`.

### 3.2 SEARCH — finding what's already there

```bash
mneme search "<query>"
mneme search "<query>" --client <client>
mneme trace show <client>/<page> --direction forward
mneme trace show <client>/<page> --direction backward
mneme trace gaps <client>
mneme trace matrix <client>
```

Search the wiki first. Read source files only when wiki coverage is zero.
`trace gaps` is the fastest way to find holes in a V-model chain.

### 3.3 DRAFT — writing new prose (NEW in v0.4.0)

```bash
mneme draft --doc-type <type> --section <slug> --client <client> \
            [--source <path>] [--query <text>] [-k N] \
            [--json] [--out <file>]
```

`mneme draft` builds a **write packet** and prints it as markdown (or
JSON with `--json`). The packet is everything you need to write one
section of a document:

| Field | Meaning |
|---|---|
| `profile_name` | Active profile that governs this section |
| `doc_type` | The document type, e.g. `design-validation-report` |
| `section` | The section slug, e.g. `purpose-and-scope` |
| `section_notes` | The normative notes for this section from the profile |
| `all_section_notes` | All sections of this doc type (for context) |
| `writing_style` | principles, general_rules, terminology_guidance, framing_examples |
| `submission_checklist` | Pre-submission go/no-go items |
| `evidence` | Candidate material: the explicit source (if given) plus wiki search hits |
| `write_prompt` | A ready-to-paste instruction telling you what to produce |

Evidence selection:
- If `--source <path>` is given, that file is included verbatim as
  `kind: explicit-source`.
- If `--query <text>` is given, mneme runs a wiki text search and filters
  to the requested client; hits are included as `kind: wiki-search-hit`.
- If neither is given, the section slug is used as an implicit query so
  the packet always has something to work with.

Your contract when consuming a write packet:

1. Use ONLY the evidence in the packet. Do not invent facts.
2. Cite each non-trivial claim with the source path from the packet.
3. For missing refs, insert the profile's
   `placeholder_for_missing_refs` (default `[TO ADD REF]`) at the exact spot.
4. Obey the writing style block: principles, general rules, terminology
   guidance, and framing examples are all normative.
5. Output a single markdown section starting with `## <section-slug>`.
   No frontmatter. No surrounding prose.

### 3.4 REVIEW — grading prose against the writing style

```bash
mneme validate writing-style <client>/<page>           # markdown packet
mneme validate writing-style <client>/<page> --json    # raw dict
mneme validate consistency --client <client>           # cross-doc checks
```

`validate writing-style` builds a **review packet**: the page content,
its frontmatter, the active profile's `writing_style` block, the
`section_notes` resolved from the page's frontmatter `type:` field, the
submission checklist, and a ready-to-paste review prompt.

Your contract when consuming a review packet:

1. For each issue, quote the offending text, explain the violation, and
   propose a concrete rewrite.
2. Walk the submission checklist and report pass/fail per item with a
   one-line justification.
3. Apply your own fixes to the page in place. Do not leave a report and
   walk away — the user expects the document to improve.

### 3.5 RESYNC — merging external edits without losing your work

```bash
mneme resync <file> <client>                           # preview + apply
mneme resync <file> <client> --dry-run                 # preview only
mneme resync-resolve <client>/<page>                   # after manual fix
```

When a colleague sends an updated source file, never re-ingest — that
would clobber hand edits. Use `resync`. It runs a 3-way merge between
baseline, your current wiki page, and a fresh ingest of the new source.
If there are conflicts, the page is left with merge markers. Edit them
out manually, then run `resync-resolve`.

### 3.6 TAG — agent-driven tagging

```bash
mneme tags suggest <client>/<page>                     # build tag packet
mneme tags suggest <client>/<page> --json              # raw dict
mneme tags apply <client>/<page> --add t1,t2 --remove t3

# Bulk variants -- packet up to N pages in one round-trip
mneme tags bulk-suggest --client <c> --filter req- --limit 50 --out packet.md
mneme tags bulk-apply response.json     # response: {"pages": [{wiki_path, add, remove}, ...]}
```

`mneme tags suggest` builds a **tag packet**: the page content, current
tags, the workspace tag taxonomy (every existing tag with usage counts),
active profile guidance, and a ready-to-paste prompt instructing you to
choose 3–7 tags. Mneme does **not** propose tags itself — content
understanding is your job. The packet gives you all the context you need.

Your contract when consuming a tag packet:

1. **Prefer existing tags** from the taxonomy when they fit. Consistency
   matters more than novelty — `iso-13485` should not become `iso13485`
   on the next page.
2. **Add new tags only** when no existing tag captures the concept.
3. Follow the format: lowercase, hyphenated (`risk-management`, not
   `Risk Management`).
4. Do not propose generic tags (`summary`, `overview`, `report`).
5. Do not add the client slug — it is auto-applied.
6. Output JSON: `{"tags": ["existing-a", "existing-b"], "new_tags": ["proposed-c"]}`.

`mneme tags apply` is **atomic**: it rewrites the wiki page frontmatter,
updates `schema/tags.json`, re-syncs the page to the FTS5 index, and
appends a log entry — all in one operation. Search picks up the new tags
immediately. Use `--add` and/or `--remove`, comma-separated.

Existing taxonomy ops:

```bash
mneme tags list                                        # all tags + counts
mneme tags merge <old> <new>                           # rename across all pages
```

### 3.7 ENTITY — agent-driven classification

```bash
mneme entity suggest --client <c> --limit 50          # classification packet
mneme entity apply --id iso-13485 --type standard     # one at a time
mneme entity bulk-apply classifications.json          # batch
```

`mneme entity suggest` builds an **entity packet**: every `unknown`-typed
entity, the workspace's current type distribution, the valid type
vocabulary, an example wiki page per entity, and a prompt. The agent
returns a JSON array of `{id, type}` objects which `bulk-apply` writes
back atomically. Same philosophy as tags: mneme stays deterministic, the
LLM does the classification.

Valid types: `standard`, `company`, `person`, `product`, `technology`,
`concept`, `brand`, `unknown`.

### 3.8 HOME — generated landing page

```bash
mneme home --client <c>          # wiki/<c>/HOME.md
mneme home --all-clients         # wiki/HOME.md (cross-client)
```

Generates an Obsidian-friendly navigation hub with Dataview queries
(group by type, by ID prefix like `REQ-*` / `DDS-*`, top tags) and a
plain-markdown `<details>` fallback so the page is useful outside
Obsidian. Run after a large ingest, or whenever the wiki's shape
changes meaningfully.

### 3.9 TRACE — linking the full V-model chain

The trace chain a notified body expects has two legs that both terminate
at code and tests:

```
UN  ──implemented-by──┐
                      ├──> REQ ──detailed-in──> DDS ──implemented-in──> codebase
RMA ──mitigated-by────┘                             └──verified-by───> tests
```

The first three links (UN→REQ, RMA→REQ, REQ→DDS) are created
automatically by the CSV mappings in `profiles/mappings/` (or by
`mneme trace add` when ingesting structured sources). The last two
links (DDS→codebase, DDS→tests) close the V-model and are the agent's
responsibility when a user passes you one or more repositories.

**When the user passes you a repo path, you must:**

```bash
# 1. Inventory: what code modules / test files exist?
mneme scan-repo <repo-path> <client>
# → reports which wiki pages reference the repo's modules, and which do not.

# 2. For each DDS page that corresponds to a code module, add the link.
#    The target is a git URL or an absolute repo path; mneme treats it
#    as an opaque string (not a wiki slug) — the target may live outside
#    the workspace.
mneme trace add <client>/dds-cyb-001 \
                "github.com/<org>/<repo>/blob/main/src/auth/password_policy.py" \
                implemented-in

# 3. For each DDS page that has a corresponding test, add the link.
#    The test target can be a wiki page (for test-plan docs) or an
#    external path (for a test file in a repo).
mneme trace add <client>/dds-cyb-001 <client>/test-auth-001 verified-by
mneme trace add <client>/dds-cyb-001 \
                "github.com/<org>/<repo>/blob/main/tests/test_password_policy.py" \
                verified-by
```

Do this for every DDS page that has implementing code or a verifying
test. When there are tens or hundreds of links to create (typical for
a real medical-device codebase):

```bash
# Batch approach — the agent parses the repo, maps DDS → files,
# then writes a shell script of `mneme trace add` lines and runs it.
# mneme has no bulk-trace-add subcommand yet; scripting is the way.
for pair in dds-cyb-001:src/auth/password_policy.py \
            dds-cyb-002:src/auth/mfa.py \
            dds-cyb-003:src/auth/rate_limiter.py; do
  dds=${pair%%:*}; file=${pair##*:}
  mneme trace add <client>/$dds "<repo-url>/$file" implemented-in
done
```

**Verify the chain is now complete:**

```bash
mneme trace gaps <client>
# → should report 0 hazards without mitigation, 0 DDS without
#   implementation link, 0 DDS without verification link

mneme trace show <client>/un-001
# → UN.001
#     implemented-by -> REQ.SYS.001
#         detailed-in -> DDS.CYB.001
#             implemented-in -> github.com/.../password_policy.py
#             verified-by    -> github.com/.../test_password_policy.py
```

**Relationship vocabulary — use exactly these strings:**

| Relationship | From → To | Semantics |
|---|---|---|
| `implemented-by` | UN → REQ | The user need is met by this requirement |
| `mitigated-by` | RMA → REQ | The hazard is mitigated by this requirement |
| `derived-from` | REQ → UN / REQ → higher-level REQ | Parent requirement |
| `detailed-in` | REQ → DDS | The requirement is elaborated by this design spec |
| `implemented-in` | DDS → codebase | The design spec is realised by this source file / module |
| `verified-by` | DDS → test / REQ → test | The spec/requirement is verified by this test |
| `validated-by` | DDS → clinical/usability study | Validation (not verification) evidence |

Stick to this vocabulary. Custom relationships confuse downstream
matrix exports and break the default `trace gaps` heuristics.

---

## 4. Profiles and the writing-style contract

Profiles are markdown files with YAML frontmatter. The frontmatter carries
structured fields (`vocabulary`, `trace_types`, `tone`, `requirement_levels`,
etc.). The body carries the writing-style prose under recognised H1 headings
(`# Principles`, `# General Rules`, `# Terminology`, `# Framing: ...`,
`# Document Type: <slug>`, `## Section: <slug>`, `# Submission Checklist`).

Two things you must know before writing:

1. **Active profile lookup.** `mneme profile show` prints the active
   profile's name, doc types, vocabulary rule count, and submission
   checklist length. Run it first. If you want the raw contract, read the
   `.md` file directly — mneme resolves workspace profiles first, then
   bundled profiles.
2. **Section resolution.** When you write a wiki page whose frontmatter
   says `type: design-validation-report`, `mneme validate writing-style`
   will resolve the `section_notes` for `design-validation-report` from
   the active profile and include them in the review packet. If the
   `type:` does not match any doc type in the profile, `matched_section`
   is empty and only the general writing style applies.

Harmonize vs validate:

| Command | What it does | When to run |
|---|---|---|
| `mneme harmonize --client <c>` | Mechanical vocabulary swap (find/replace against `vocabulary[]`) | After drafting, before review |
| `mneme harmonize --client <c> --fix` | Same, but writes fixes back | After drafting |
| `mneme validate writing-style <page>` | Build an LLM review packet for prose quality | After harmonize |
| `mneme validate consistency --client <c>` | Cross-document version/standard checks | Before submission |

Harmonize is deterministic and dumb. Validate writing-style is your job
and requires reasoning.

---

## 5. The agent loop (the headline feature)

The agent loop turns "produce a Design Validation Report" into a TODO list
you can walk one task at a time. Mneme generates the plan deterministically
from the active profile — it knows which sections exist and in what order.

Commands:

| Command | Purpose |
|---|---|
| `mneme agent plan --goal <text> --doc-type <type> --client <c> [--id <id>] [--json]` | Generate a new plan and persist it |
| `mneme agent show [--plan <id>] [--json]` | Show a plan and per-task statuses |
| `mneme agent next-task [--plan <id>] [--json]` | Return the next ready task (respects deps) |
| `mneme agent task-done <task-id> [--plan <id>]` | Mark a task complete |
| `mneme agent list [--json]` | List every plan in the workspace |

Plans live under `<workspace>/.mneme/agent-plans/<id>.json` (the plan
document) and `<id>.state.json` (task statuses). `.mneme/` is gitignored.
If you omit `--plan <id>`, mneme picks the most-recently-modified plan.

Task shape:

```json
{
  "id": "section-purpose-and-scope",
  "kind": "draft-section",
  "goal": "Draft the `purpose-and-scope` section of the design-validation-report",
  "instructions": "Run the next_command, then write a single markdown block...",
  "preconditions": ["Active profile must be \"EU MDR\""],
  "deliverable": {
    "kind": "markdown-section",
    "target_page": "wiki/tda/design-validation-report.md",
    "section_slug": "purpose-and-scope"
  },
  "next_command": "mneme draft --doc-type design-validation-report --section purpose-and-scope --client tda",
  "after_done": "mneme agent task-done section-purpose-and-scope --plan design-validation-report-tda-2026-04-09",
  "depends_on": [],
  "blocks": ["assemble-document"],
  "status": "pending"
}
```

A plan for `design-validation-report` under the bundled `eu-mdr` profile
produces **15 tasks**: one `draft-section` (or `review-section` if the
page already exists) per section in the profile's `section_notes` (11
for `eu-mdr` DVR), then `assemble-document`, `harmonize`, `review-page`,
`submission-check`. Dependencies:

```
section-* (x11)  ->  assemble-document  ->  harmonize  ->  review-page  ->  submission-check
```

### Worked transcript: produce a DVR for the TDA algorithm

```bash
# Step 1: User says "I need a Design Validation Report for the TDA algorithm."

# Step 2: Verify the workspace is set up
mneme stats
mneme profile show
#   Active profile: EU MDR
#   Doc types: design-validation-report, risk-management, clinical-evaluation, ...
#   Vocabulary rules: 15

# Step 3: Generate the plan
mneme agent plan \
  --goal "produce a Design Validation Report for the TDA algorithm" \
  --doc-type design-validation-report \
  --client tda
#   Plan: design-validation-report-tda-2026-04-09
#   Tasks: 15
#     section-purpose-and-scope
#     section-context
#     section-referenced-documents
#     section-execution-metadata
#     section-dataset-descriptions
#     section-methodology-explanations
#     section-test-equipment
#     section-sample-size-justification
#     section-acceptance-criteria
#     section-test-results
#     section-conclusion
#     assemble-document
#     harmonize
#     review-page
#     submission-check

# Step 4: Walk the plan
mneme agent next-task
#   Task: section-purpose-and-scope
#   next_command: mneme draft --doc-type design-validation-report \
#                             --section purpose-and-scope --client tda

# Agent runs the next_command to pull the write packet
mneme draft --doc-type design-validation-report \
            --section purpose-and-scope --client tda \
            --source sources/tda/design-input.md \
            --out /tmp/packet.md

# Agent reads /tmp/packet.md, writes the section to
# wiki/tda/design-validation-report.md as a `## purpose-and-scope` block
# (or a separate section file, per the agent's own staging convention)

mneme agent task-done section-purpose-and-scope

# Repeat for each of the 10 remaining sections
mneme agent next-task
mneme draft --doc-type design-validation-report --section context --client tda \
            --query "technical literature kinematic accelerometer"
mneme agent task-done section-context

# ... and so on for referenced-documents, execution-metadata,
# dataset-descriptions, methodology-explanations, test-equipment,
# sample-size-justification, acceptance-criteria, test-results, conclusion.

# After all 11 section tasks are done, assemble-document is ready
mneme agent next-task
#   Task: assemble-document
#   Combine section drafts into wiki/tda/design-validation-report.md with
#   frontmatter (title, type: design-validation-report, client: tda,
#   created, updated, sources, confidence: medium)

# Agent assembles the page, then
mneme agent task-done assemble-document

# Harmonize the vocabulary
mneme agent next-task
mneme harmonize --client tda --fix
mneme agent task-done harmonize

# Review the prose against the writing style
mneme agent next-task
mneme validate writing-style tda/design-validation-report > /tmp/review.md
# Agent reads /tmp/review.md, critiques every section against principles,
# general rules, terminology, and framing examples. Applies concrete fixes
# in place.
mneme agent task-done review-page

# Submission check
mneme agent next-task
mneme profile show
# Agent walks the submission_checklist item by item, reporting pass/fail
# with a one-line justification per item. Stops. Does NOT mark the
# document "validated" — that is the user's call.
mneme agent task-done submission-check

# Done
mneme agent next-task
#   {done: true}
```

That's the whole loop. Every task has a `next_command`, an `after_done`
command, and an `instructions` field. When in doubt, read the envelope
mneme gives you and do exactly what it says.

---

## 6. Standard task templates

When the user states one of these goals, follow the matching template.
Each template assumes an active profile is set and the workspace is
already scaffolded. Stop conditions are the trigger for "task complete".

### 6.1 Produce a Design Validation Report from a code repo

```
1. mneme profile show                            # confirm EU MDR or equivalent
2. mneme scan-repo <repo-path> <client>          # find SOUP gaps, module gaps
3. Ingest any reports the scan surfaces:
     mneme ingest <file> <client>
4. mneme agent plan --goal "produce a DVR" \
                    --doc-type design-validation-report --client <client>
5. Loop: mneme agent next-task -> mneme draft -> write -> task-done
6. After all sections done, assemble-document, then
     mneme harmonize --client <client> --fix
7. mneme validate writing-style <client>/design-validation-report
   Apply fixes in place.
8. Walk the submission checklist. Report pass/fail per item.
```

Stop conditions: `mneme agent next-task` returns `{done: true}` AND
the submission checklist report is in hand.

### 6.2 Produce a Clinical Evaluation Report (Part A) from existing literature

```
1. mneme profile show                            # expect EU MDR
2. mneme search "clinical evaluation" --client <client>
3. If coverage is thin:
     mneme ingest <literature-pdf> <client>      # for each source
4. mneme agent plan --goal "produce CER Part A" \
                    --doc-type clinical-evaluation --client <client>
5. Walk the plan. For each section, the write packet will include
   any literature hits already in the wiki.
6. assemble-document -> harmonize -> review-page -> submission-check
7. mneme validate consistency --client <client>  # catch version drift on
                                                   cited standards
```

Stop conditions: plan is done AND `validate consistency` reports zero
conflicting standard versions.

### 6.3 Build a Risk Management File from a hazard list and a code repo

```
1. mneme ingest-csv risk-register.csv <client> --mapping risk-register
2. mneme scan-repo <repo-path> <client>
3. mneme trace gaps <client>                     # find hazards without RMAs
4. For each gap, mneme ingest or mneme trace add to close the link
5. mneme agent plan --goal "produce risk management file" \
                    --doc-type risk-management --client <client>
6. Walk the plan (same section/assemble/harmonize/review/submission shape)
7. mneme trace matrix <client>                   # verify full coverage
```

Stop conditions: plan is done AND `mneme trace gaps <client>` reports
zero unmitigated hazards.

### 6.4 Update wiki pages after a teammate sent updated source files

```
1. Drop the new files into incoming/ (or any path outside sources/)
2. For each file:
     mneme resync <file> <client> --dry-run      # preview
     mneme resync <file> <client>                # apply
3. If conflicts are reported:
     a. Open the affected wiki page
     b. Edit merge markers by hand, preserving the correct content
     c. mneme resync-resolve <client>/<page>
4. mneme recent -n 10                            # verify RESYNC entries
5. mneme lint                                    # catch any broken links
```

Stop conditions: every new file is resynced, no merge markers remain,
and `mneme recent` shows a RESYNC (clean or RESYNC-RESOLVED) entry per
file.

### 6.5 Migrate a project from another QMS into a fresh mneme workspace

```
1. mneme new <path> --name <project> --client <client> --profile <profile>
2. cd <path>
3. cp -r <old-qms>/* inbox/
4. mneme tornado --client <client> --dry-run     # preview
5. mneme tornado --client <client>               # apply
6. mneme trace gaps <client>                     # find holes in the v-model
7. mneme harmonize --client <client> --fix       # normalise vocabulary
8. mneme lint                                    # orphans, dead links
9. mneme stats                                   # sanity check totals
```

Stop conditions: inbox is empty, `mneme stats` shows a plausible page
count, and `mneme lint` reports no critical issues.

### 6.6 Close the V-model by linking DDS to codebase and tests

The user has just handed you one or more repositories. Your job is to
connect every DDS page to the implementing source file(s) and the
verifying test file(s) so `mneme trace show` walks end-to-end from a
user need / hazard all the way to the exact line of code and the exact
test that exercises it.

```
1. mneme profile show                            # sanity check
2. mneme trace matrix <client>                   # baseline — which DDS exist?
3. For each repo the user passes:
     a. mneme scan-repo <repo-path> <client>     # surface module gaps
     b. Read the repo tree and README yourself.
        Build a mapping: DDS ID -> [source files]
                         DDS ID -> [test files]
        Prefer explicit evidence (comments referencing the DDS ID,
        module/function names that mirror the DDS title, docstrings
        that cite the requirement). When evidence is weak, flag the
        DDS as ambiguous and surface it — do not guess.
4. For each confident (DDS, file) pair:
     mneme trace add <client>/<dds-slug> "<repo-url-or-path>/<file>" implemented-in
     mneme trace add <client>/<dds-slug> "<repo-url-or-path>/<test-file>" verified-by
   Batch these in a shell loop — there is no bulk-trace-add subcommand.
5. mneme trace gaps <client>                     # should trend to zero
6. mneme trace show <client>/un-001              # spot-check: full chain
                                                   from UN to test file?
7. mneme trace matrix <client> --csv --out trace-matrix.csv
                                                 # DHF-ready export
```

Stop conditions: (a) every DDS page either has both `implemented-in`
and `verified-by` trace links OR is explicitly flagged ambiguous in a
report to the user, AND (b) `trace gaps` reports zero open chains.

Hard rules:
- Do not fabricate file paths. If the repo has no file matching a DDS,
  report the gap and stop — the user must either point you at another
  repo or add the link manually.
- Trace targets for external files are opaque strings. Use a stable
  form the team can resolve later (a git URL with a pinned commit is
  ideal; a bare relative path is fine when the repo lives alongside
  the workspace).
- Never rewrite a DDS page's body to embed the code link. The link
  lives in `schema/traceability.json` only. Wiki pages stay prose.

### 6.7 Ingest a code repo into the wiki as searchable module summaries

The user has handed you a code repo. Your job is to produce one wiki page
per logical module so future agents can answer "how does this codebase do
X?" through `mneme search` instead of re-reading the source.

This is the foundation for any later code-aware work (style-matched
extension, refactor planning, gap analysis). It does not modify the repo —
read-only ingestion.

```
1. Walk <REPO_PATH>. Skip: .git, node_modules, .venv, dist, build,
   __pycache__, anything in .gitignore.

2. Group files into logical modules. Heuristics:
   - A directory containing __init__.py / mod.rs / index.ts / mod.go
     is one module.
   - A standalone script with no siblings is one module.
   - Tests (tests/ or *_test.* alongside) are part of the module they
     test, not separate modules.

3. For each module, write a summary file at
     /tmp/mneme-summaries/<module-path>.md
   with this exact frontmatter and section structure:

   ---
   title: <Module Name>
   type: code-summary
   client: <CLIENT_SLUG>
   sources:
     - <repo-relative path of every file in the module>
   tags:
     - code
     - <language>
     - <one-or-two-domain-tags>
   ---

   ## Purpose
   One paragraph in plain English. No code.

   ## Public API
   List of exported functions / classes / types, one line each.
   Format: `name(args) -> return_type` then a sentence.

   ## Key data structures
   Non-trivial types or schemas this module owns. Skip if none.

   ## Dependencies
   - Internal: which other modules in this repo it imports
   - External: which libraries (with pinned version if any)

   ## Tests
   Path to test file(s) + one sentence on coverage shape.

   ## Conventions observed
   3-5 bullets: error style, async/sync, naming, comment density, etc.

4. For files too large to read in one pass:
   a. Read the first 200 lines.
   b. Read the last 100 lines.
   c. If the file has a clear table-of-contents (a __all__, an exports
      block, a class index near the top), use it to guide which middle
      sections to read in additional 200-line chunks.
   d. State in the summary's Purpose section that this was a partial
      read, and tag the page `partial-read` so a future pass can
      revisit.

5. Ingest the summaries in one pass:
     mneme ingest-dir /tmp/mneme-summaries <CLIENT_SLUG> --recursive --flat

   Use --flat: the summaries already encode their path in the slug, and
   they don't live under sources/<CLIENT_SLUG>/ so subpath auto-detection
   won't help.

6. Smoke-test:
     mneme stats
     mneme search "<a real concept from the repo>" --client <CLIENT_SLUG>
     mneme tags list
```

Stop conditions: every module in the repo (modulo the skip list) has a
wiki summary, and a search for a known concept returns the right module.

Hard rules:
- Do not generate summaries for files you did not actually read. Partial
  reads must be tagged `partial-read` in the page's frontmatter.
- Do not speculate. If a module's purpose is unclear from the code, write
  "unclear, needs human review" and tag the page `needs-review`.
- Do not modify the repo. Read-only.
- Keep summaries under 300 lines. They are pointers, not replacements.
- One module = one wiki page. Do not split a module across pages, and
  do not merge unrelated modules into one page.

Report when done: total modules summarized, count tagged `partial-read`,
count tagged `needs-review`, directories skipped and why, and the three
search queries you used to verify the ingest.

### 6.8 Augment a wiki page with knowledge from ingested code summaries

Pre-condition: 6.7 has run, so the repo is in the wiki as `code-summary`
pages. You now have a target wiki page (sparse, half-finished, or
explicitly marked TBD) and you want to enrich it with sections that
draw on the code knowledge — in the page's existing voice, with every
claim cited.

This is selective augment, not regeneration. Existing prose is sacred.

```
1. Read the target page in full at <WORKSPACE>/wiki/<client>/<page>.md.
   Note: existing tone, sentence length, citation density, heading depth,
   table-of-contents shape. These define the local style you must match.

2. Decide what to add. Two paths:
   a. Human-driven: the user told you "add a Performance Characteristics
      section drawing latency data from the codebase." Skip to step 3.
   b. Agent-driven: gap analysis. Compare the target's actual sections
      against (i) the active profile's expected sections for this
      doc-type (run `mneme profile show`), and (ii) topics covered by
      code-summary pages that the target does not cite. Propose 1-5
      candidate sections to the human and wait for confirmation. Do not
      add sections without confirmation.

3. For each agreed section, gather evidence:
     mneme search "<topic keywords>" --client <client> -k 20
   Prefer hits with the `code` tag for implementation details. Prefer
   regulatory wiki pages for context and definitions. Read the top hits
   in full before writing.

4. Draft the section. Hard requirements:
   - Match the target's local style. Local consistency wins over the
     active profile's global rules within a single page.
   - Every non-trivial claim cites its source as
     `(wiki: <client>/<page>)` or `(source: <repo-relative-path>)`.
   - When evidence is insufficient for a claim, do not invent it.
     Insert `[TO ADD REF]` and continue.

5. Insert at the structurally correct location. Read the target's TOC.
   The new section's heading depth and ordering must follow the
   document's own logic, not your intuition.

6. Update the target's frontmatter:
   - Append every newly cited source to the `sources:` list.
   - Bump `updated:` to today.
   - If the page was previously marked draft / TBD and is now complete,
     update `confidence:` accordingly.

7. Re-ingest the target so search picks up the new content. Two options:
   a. If the page has a corresponding source file in sources/<client>/,
      mirror your wiki edits back to it and run:
        mneme resync sources/<client>/<path-to-source> <client>
   b. Otherwise, edit the wiki page directly and run:
        mneme reindex
```

Stop conditions: every agreed section is either (a) written with full
citations, or (b) explicitly flagged as evidence-insufficient and
reported back to the human. The page passes
`mneme validate writing-style <client>/<page>` against the active
profile.

Hard rules:
- Do NOT rewrite existing prose. Augment only — add new sections, do not
  edit current ones unless explicitly asked.
- Do NOT fabricate citations. Every `(wiki: ...)` and `(source: ...)`
  reference must resolve to an actual page or file.
- Do NOT exceed the human-confirmed scope. If gap analysis surfaced 5
  candidate sections and the human approved 2, write only those 2.
- Do NOT touch the page's frontmatter `created:` or `client:` fields.

Report when done: sections added (with line counts), sources cited
(deduplicated list), any sections you were asked to write but skipped
because evidence was insufficient (with a one-line explanation per skip),
and the result of the post-edit `mneme validate writing-style` run.

### 6.9 Validate a claim against the literature wiki

You are about to write or have already written a factual claim in a
deliverable (DVR, CER, technical documentation, etc.). Before the
claim ships to a notified body, it must be backed by an authoritative
source — or explicitly carry `[TO ADD REF]` so the gap is visible.

Pre-condition: the relevant literature has been ingested into the wiki
(typically under `research-questions/` or similar) and tagged with
`literature` plus an authority marker (`authority` / `non-authority`).
If those tags don't exist, run a one-time `mneme tags bulk-suggest` /
`bulk-apply` pass to add them — see Step 3 in the README.

```
1. Identify the claim. Reduce it to its load-bearing assertion.
   "Parkinsonian tremor manifests primarily in the 4-6 Hz band" is a
   claim. "Tremor is a problem" is not — too vague to validate.

2. Search the literature for evidence. Be specific in the query:
     mneme search "<claim keywords>" --client <client> -k 30
   When `mneme search --tag` is available (planned), prefer:
     mneme search "<claim keywords>" --client <client> --tag authority -k 20

3. Read the top hits in full. Sort the relevant ones into three buckets:
   a. AUTHORITY supports the claim (peer-reviewed, recent, on-topic)
   b. NON-AUTHORITY supports the claim (preprints, blog posts, secondary)
   c. Nothing relevant, or hits contradict the claim

4. Decide based on the bucket:

   a. AUTHORITY support
      -> Write the claim with the citation:
         "...4-6 Hz band (wiki: <client>/research-questions/.../<page>)."
      -> Append the cited page to the deliverable's frontmatter
         `sources:` list if not already present.

   b. NON-AUTHORITY support only
      -> Either soften the claim ("Preliminary reports suggest..."),
         OR keep the strong form with [TO ADD REF] and find an
         authority source separately.
      -> Do NOT cite a non-authority source as if it were authoritative.

   c. No support / contradicting evidence
      -> Three options, in order of preference:
         i.  Drop the claim. The deliverable doesn't need it.
         ii. Find a new authority source. Drop the PDF into
             sources/<client>/<literature-path>/, summarize and ingest
             it (run a single-page version of 6.7), then return to step 2.
         iii. Keep the claim but mark it [TO ADD REF] AND open a
              tracked TODO so the gap doesn't ship by accident.

5. After resolving the claim (or marking it), run:
     mneme validate writing-style <client>/<deliverable-page>
   The review packet flags every remaining [TO ADD REF] and every
   uncited factual claim. Address them or hand the page back to the
   human reviewer with the gaps surfaced.
```

Stop conditions: the claim is either (a) cited with an authority
source, (b) softened to match the strength of the available evidence,
(c) dropped, or (d) explicitly marked `[TO ADD REF]` AND tracked for
follow-up. Never (e) cited with fabricated or non-authoritative
evidence dressed as authoritative.

Hard rules:
- Do NOT cite a wiki page you did not read. Read every page you cite.
- Do NOT cite a non-authority source as `(wiki: ...)` without making
  its non-authority status visible in the surrounding prose.
- Do NOT silently weaken or rewrite the claim to dodge the citation
  requirement. If the evidence is weak, say so.
- Do NOT bulk-clear `[TO ADD REF]` markers without going through this
  procedure for each one. Each marker is a discrete claim that needs
  individual evidence.

Report when done: the original claim, the final form of the claim
(verbatim if changed), the citation added (or the [TO ADD REF] marker
left in place), the wiki pages read, and a one-line note on whether
this gap should be tracked for human follow-up.

### 6.10 Pre-submission readiness check before sending to a notified body

```
1. mneme profile show                            # confirm active profile
2. mneme harmonize --client <client>             # list (do not fix yet)
3. mneme harmonize --client <client> --fix       # apply, review diff
4. mneme validate consistency --client <client>  # standard versions
5. mneme trace gaps <client>                     # open trace chains
6. For each deliverable page (DVR, CER, RMF, etc.):
     mneme validate writing-style <client>/<page>
     Apply fixes in place.
7. Walk the profile's submission_checklist per deliverable.
8. mneme snapshot <client>                       # freeze an audit copy
```

Stop conditions: zero gaps, zero consistency warnings, zero outstanding
writing-style issues, and a snapshot zip is on disk.

---

## 7. Sub-agent patterns (when to spawn parallel sub-agents)

A top-level agent driving mneme can spawn sub-agents to parallelise the
slow parts of a plan. Four patterns are expected.

### 7.1 section-writer

**When to spawn:** during the `draft-section` phase of a plan. Section
tasks are independent (they all depend only on the empty state), so N
section-writer sub-agents can run in parallel.

**Input contract:**
- The task envelope from `mneme agent next-task`
- The write packet from `mneme draft --json` (or markdown)

**Output contract:** a single markdown block starting with
`## <section-slug>`, written to a staging area chosen by the parent. No
frontmatter. Citations in-line. `[TO ADD REF]` for gaps.

**Parallelism:** up to one per section in the plan. The parent collects
the drafts and runs `assemble-document` sequentially.

### 7.2 reviewer

**When to spawn:** during `review-page`, or ad-hoc against any existing
page.

**Input contract:** the review packet from
`mneme validate writing-style <page> --json`.

**Output contract:** an issue list (quote, violation, rewrite) plus a
submission-checklist walk (pass/fail per item, one-line justification).
If authorised, apply fixes in place.

**Parallelism:** one per page. Multiple pages can be reviewed in
parallel.

### 7.3 vocabulary-fixer

**When to spawn:** when `mneme harmonize` reports issues that the
mechanical swap cannot safely auto-fix (e.g. a term is used in two
different senses on the same page, only one of which should be rewritten).

**Input contract:** the harmonize report + the page path.

**Output contract:** a patched page that preserves meaning. Run
`mneme harmonize --client <c>` again after to confirm the count drops.

**Parallelism:** one per page.

### 7.4 evidence-finder

**When to spawn:** when a section-writer's write packet has no evidence,
or the evidence is thin. The parent dispatches evidence-finder to locate
material in `sources/` (or external docs not yet ingested) and either
ingest it or cite what exists.

**Input contract:** the section_notes for the missing section + the
client slug.

**Output contract:** either (a) a list of new `mneme ingest` commands
the parent should run, or (b) a list of existing wiki paths the parent
should pass to `mneme draft --query`.

**Parallelism:** one per thin section.

---

## 8. The contracts you must read on every operation

Before you write anything, read:

1. `mneme profile show` — confirm the style contract
2. `mneme stats` — confirm the workspace is healthy
3. `mneme status` — confirm nothing is pending that would invalidate
   your work
4. The active profile's `.md` file — the normative source of
   principles, general rules, terminology, framing, and section notes

Before you mark a task done, read:

1. The task envelope's `deliverable` field — did you actually produce
   that artefact?
2. The task envelope's `preconditions` — were they all satisfied?
3. `mneme recent -n 5` — does the log reflect what you expected?

---

## 9. The contracts you must NEVER violate

These are absolute. Breaking any of them corrupts the workspace or
misleads the user.

1. **Never modify `sources/`.** It is immutable evidence. If you need
   to update a source, you replace it through `resync`, never by hand.
2. **Never overwrite a wiki page that has a baseline without `resync`.**
   A baseline means a teammate or a prior run owns state on that page;
   blind overwrite destroys history.
3. **Never claim "validated" beyond the profile's definition.** In
   EU MDR, "design validated" means acceptance criteria passed against
   pre-defined reference standards. "Clinically validated" requires a
   separate clinical validation. Do not conflate them.
4. **Never invent citations.** Every claim cites a real source path or
   wiki page, or it is marked `[TO ADD REF]`. No fabricated DOIs, no
   hallucinated standard numbers.
5. **Never silently widen scope.** If the user asked for a DVR for the
   TDA algorithm, do not draft a CER on the side. If the plan says 15
   tasks, do not add a 16th without asking.
6. **Never skip `harmonize` or `validate writing-style` before marking
   a deliverable complete.** They are cheap and catch real issues.
7. **Never mark `submission-check` done without walking the checklist
   item by item.** Pass/fail per item is the whole point.
8. **Never run `--fix` variants destructively without reviewing the
   diff.** `mneme harmonize --fix` writes to disk; run it, then read
   `mneme diff <page>` before committing.
9. **Never call an LLM from inside mneme.** Mneme is deterministic
   infrastructure. All reasoning happens in you, the agent.

---

## 10. Reference cards

### 10.1 Document type to profile section table (bundled `eu-mdr`)

| Doc type | Purpose | Sections |
|---|---|---|
| `risk-management` | ISO 14971 risk management file | inherited from profile |
| `clinical-evaluation` | MEDDEV 2.7/1 rev 4 CER | inherited from profile |
| `design-history-file` | Design and development docs per Annex II | inherited from profile |
| `software-documentation` | IEC 62304 software lifecycle | inherited from profile |
| `post-market-surveillance` | PMS per Articles 83-86 | inherited from profile |
| `technical-documentation` | Technical documentation per Annex II and III | inherited from profile |
| `design-validation-report` | DVR under EU MDR CE marking | 11 sections, see below |

Sections for `design-validation-report` under `eu-mdr`:

1. `purpose-and-scope`
2. `context`
3. `referenced-documents`
4. `execution-metadata`
5. `dataset-descriptions`
6. `methodology-explanations`
7. `test-equipment`
8. `sample-size-justification`
9. `acceptance-criteria`
10. `test-results`
11. `conclusion`

Plan total = 11 section tasks + `assemble-document` + `harmonize` +
`review-page` + `submission-check` = **15 tasks**.

### 10.2 Common errors and what to do

| Error from mneme | What it means | What to do |
|---|---|---|
| `No active profile` | Profile is unset | `mneme profile set <name>` (e.g. `eu-mdr`) |
| `Unknown doc-type "X" for profile "Y"` | Typo or the profile has no `Document Type: X` block | `mneme profile show` then fix the `--doc-type` |
| `Unknown section "X" for doc-type "Y"` | Section slug typo | Read the profile `.md`, pick a real `## Section: <slug>` |
| `Page not found` from `validate writing-style` | Slug is wrong or page was never written | `ls wiki/<client>/` and try again |
| `No plans found in this workspace` | No agent plan has been generated | `mneme agent plan ...` first |
| `CONFLICT (N regions)` from `resync` | 3-way merge couldn't auto-resolve | Edit merge markers by hand, then `mneme resync-resolve <page>` |
| `Profile not found` after `profile set` | File not at `<workspace>/profiles/<name>.md` or bundled | Verify filename matches exactly |
| `Source not found: <path>` from `draft --source` | Path typo | Quote the path, check `ls` |

### 10.3 Where to look for things in a workspace

| You need to... | Look at... |
|---|---|
| See every wiki page mneme knows about | `index.md` |
| See the activity timeline | `log.md` |
| See active profile contents | `mneme profile show`, then read `profiles/<name>.md` or the bundled file |
| See which plans exist | `mneme agent list` |
| See plan internals | `.mneme/agent-plans/<id>.json` and `<id>.state.json` |
| See the entity graph | `schema/graph.json` |
| See the tag taxonomy | `schema/tags.json` |
| See the trace links | `schema/traceability.json` |
| See what changed on a page | `mneme diff <client>/<page>` |
| See cross-doc version conflicts | `mneme validate consistency --client <client>` |
| See trace holes | `mneme trace gaps <client>` |
| Export a client for an external QMS app | `mneme export <client> --format json` |
| Freeze an audit snapshot | `mneme snapshot <client>` |

---

*This document is the contract. When in doubt, re-read sections 5 and 9.*
