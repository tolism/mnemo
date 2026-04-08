# Workspace-local profiles

Drop a `<name>.json` file here to define a profile that's local to this
workspace. Workspace profiles **shadow** any bundled profile with the same
name, so you can:

- Add a private framework that mneme doesn't ship (e.g. an internal QMS variant)
- Override a bundled profile with project-specific tweaks (e.g. extra vocabulary)

## Format

Same shape as the bundled `eu-mdr.json` / `iso-13485.json`. See `CODER.md` in
the mneme repo for the full schema, or copy a bundled profile as a starting
point:

```bash
python -c "import mneme.config as c, shutil; shutil.copy(c.PROFILES_DIR + '/eu-mdr.json', c.WORKSPACE_PROFILES_DIR + '/my-profile.json')"
```

Then edit `profiles/my-profile.json` and activate it:

```bash
mneme profile set my-profile
mneme profile show
```

## CSV mappings

Workspace-local CSV column mappings (used by `mneme ingest-csv`) live in
`profiles/mappings/<name>.json`. Same shadowing rules apply.
