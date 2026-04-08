# Workspace-local profiles

Drop a `<name>.md` file here to define a profile local to this workspace.
Workspace profiles **shadow** any bundled profile with the same name, so you
can either add a brand-new framework or override a bundled one with
project-specific tweaks.

## Format

A profile is a markdown file with YAML frontmatter for the structured fields
and recognized H1 headings for the writing-style prose. The simplest possible
profile:

```markdown
---
name: My Profile
description: A short description
version: 1.0
tone: formal
voice: passive-for-procedures
trace_types: [derived-from, implemented-by, verified-by]
requirement_levels:
  shall: mandatory
  should: recommended
vocabulary:
  - use: medical device
    reject: [product, unit, item]
  - use: nonconformity
    reject: [bug, defect]
---

# Principles

- Be specific.
- Cite everything.

# General Rules

- Avoid editorial language.
- Define every term at first use.

# Terminology

| Use | Instead of | Why |
|---|---|---|
| medical device | product, unit | The term is reserved by EU MDR. |

# Framing: Reporting a result

**Wrong:**

> The algorithm is highly accurate.

**Correct:**

> The algorithm achieved 0.91 MCC against the reference standard.

**Why:** state the number, not the editorial judgement.

# Document Type: design-validation-report

A description of this document type goes here as plain prose.

## Section: context

Per-section guidance for the `context` section of a design-validation-report
goes here. The agent will pull this in when reviewing a page whose
frontmatter says `type: design-validation-report`.

# Submission Checklist

- All references include ID and version
- No clinical claims
```

## Recognized H1 headings

| Heading | What it becomes |
|---|---|
| `# Principles` | `writing_style.principles` (each `-` bullet is one entry) |
| `# General Rules` | `writing_style.general_rules` (bullets) |
| `# Terminology` | `writing_style.terminology_guidance` (parsed from a 3-column table: Use / Instead of / Why) |
| `# Framing: <context>` | one entry in `writing_style.framing_examples` (parses **Wrong:**, **Correct:**, **Why:** blocks) |
| `# Document Type: <slug>` | `sections[<slug>]`. Body before any `## Section:` becomes the description |
| `## Section: <slug>` (under a `# Document Type:`) | `sections[<doc-type>].section_notes[<section-slug>]` |
| `# Submission Checklist` | `submission_checklist` (bullets) |

Unrecognized H1 headings are silently ignored - you can use them for
authoring notes that should not affect mneme's behavior.

## Activate it

```bash
mneme profile set my-profile        # name without the .md extension
mneme profile show
```

## Use it

```bash
mneme harmonize my-client                          # vocabulary check
mneme harmonize my-client --fix                    # auto-fix vocabulary
mneme validate writing-style my-client/some-page   # build LLM review packet
```

## CSV mappings

Workspace-local CSV column mappings (used by `mneme ingest-csv`) live in
`profiles/mappings/<name>.json`. The same shadowing rules apply.
