"""Tests for profile / validate / harmonize subsystem."""
import json
import os
import shutil
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mneme.core import (
    load_profile,
    get_active_profile,
    set_active_profile,
    harmonize,
    validate_writing_style,
    validate_consistency,
    _apply_workspace_override,
)


@pytest.fixture
def temp_workspace(monkeypatch):
    """Build a clean temp workspace and rebind mneme path constants at it."""
    td = tempfile.mkdtemp(prefix='mneme-profile-test-')
    for sub in ('wiki', 'sources', 'schema'):
        os.makedirs(os.path.join(td, sub), exist_ok=True)
    with open(os.path.join(td, 'index.md'), 'w') as f:
        f.write('# Index\n')
    with open(os.path.join(td, 'log.md'), 'w') as f:
        f.write('# Log\n')
    with open(os.path.join(td, 'schema', 'entities.json'), 'w') as f:
        json.dump({'version': 1, 'updated': '2026-01-01', 'entities': []}, f)
    with open(os.path.join(td, 'schema', 'tags.json'), 'w') as f:
        json.dump({'version': 1, 'updated': '2026-01-01', 'tags': {}}, f)
    with open(os.path.join(td, 'schema', 'graph.json'), 'w') as f:
        json.dump({'version': 1, 'updated': '2026-01-01', 'nodes': [], 'edges': []}, f)

    prior = os.environ.get('MNEME_HOME')
    _apply_workspace_override(td)
    try:
        yield td
    finally:
        if prior is not None:
            _apply_workspace_override(prior)
        else:
            os.environ.pop('MNEME_HOME', None)
            _apply_workspace_override(os.getcwd())
        shutil.rmtree(td, ignore_errors=True)


def _make_page(workspace, client, slug, body, title=None, page_type='source-summary'):
    d = os.path.join(workspace, 'wiki', client)
    os.makedirs(d, exist_ok=True)
    fm_title = title or slug
    page = (
        '---\n'
        f'title: {fm_title}\n'
        f'type: {page_type}\n'
        f'client: {client}\n'
        'sources: []\n'
        'tags: []\n'
        'related: []\n'
        'created: 2026-04-08\n'
        'updated: 2026-04-08\n'
        'confidence: medium\n'
        '---\n\n'
        f'{body}\n'
    )
    path = os.path.join(d, f'{slug}.md')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(page)
    return path


# Profiles are now markdown files. Load via the public API so the tests
# always exercise the .md parser path.
EU_MDR = load_profile('eu-mdr')


class TestLoadProfile:
    def test_load_eu_mdr(self):
        p = load_profile('eu-mdr')
        assert isinstance(p, dict)
        assert 'vocabulary' in p
        assert 'sections' in p
        assert 'name' in p

    def test_load_iso_13485(self):
        p = load_profile('iso-13485')
        assert isinstance(p, dict)
        assert p != load_profile('eu-mdr')

    def test_load_nonexistent(self):
        with pytest.raises(FileNotFoundError):
            load_profile('nonexistent-profile-xyz')

    def test_profile_has_rules_and_sections(self):
        p = load_profile('eu-mdr')
        assert len(p['vocabulary'].get('preferred', [])) >= 1
        assert len(p.get('sections', {})) >= 1


class TestActiveProfile:
    def test_get_active_none_on_fresh(self, temp_workspace):
        assert get_active_profile() is None

    def test_set_active_writes_file(self, temp_workspace):
        set_active_profile('eu-mdr')
        active_file = os.path.join(temp_workspace, '.mneme-profile')
        assert os.path.exists(active_file)
        with open(active_file) as f:
            assert f.read().strip() == 'eu-mdr'

    def test_get_after_set(self, temp_workspace):
        set_active_profile('eu-mdr')
        p = get_active_profile()
        assert p is not None
        assert p.get('_profile_name') == 'eu-mdr'

    def test_switch_profile(self, temp_workspace):
        set_active_profile('eu-mdr')
        set_active_profile('iso-13485')
        p = get_active_profile()
        assert p.get('_profile_name') == 'iso-13485'

    def test_set_nonexistent_raises(self, temp_workspace):
        with pytest.raises(FileNotFoundError):
            set_active_profile('nonexistent-profile-xyz')


class TestHarmonize:
    def _first_reject(self):
        for entry in EU_MDR['vocabulary']['preferred']:
            if entry.get('reject'):
                return entry['term'], entry['reject'][0]
        raise RuntimeError("no reject term found")

    def test_empty_workspace(self, temp_workspace):
        set_active_profile('eu-mdr')
        result = harmonize('demo', fix=False)
        assert 'error' not in result
        assert result['total_issues'] == 0

    def test_flags_rejected_term(self, temp_workspace):
        set_active_profile('eu-mdr')
        preferred, rejected = self._first_reject()
        _make_page(temp_workspace, 'demo', 'p1',
                   f'This {rejected} is under review.')
        result = harmonize('demo', fix=False)
        assert result['total_issues'] >= 1
        found = [i for i in result['issues'] if i['found_term'].lower() == rejected.lower()]
        assert found
        assert found[0]['preferred_term'] == preferred

    def test_fix_rewrites(self, temp_workspace):
        set_active_profile('eu-mdr')
        preferred, rejected = self._first_reject()
        path = _make_page(temp_workspace, 'demo', 'p2',
                          f'The {rejected} shall be evaluated.')
        result = harmonize('demo', fix=True)
        assert result.get('pages_fixed', 0) >= 1
        with open(path, encoding='utf-8') as f:
            new_content = f.read()
        assert preferred.lower() in new_content.lower()

    def test_fix_idempotent(self, temp_workspace):
        set_active_profile('eu-mdr')
        preferred, rejected = self._first_reject()
        _make_page(temp_workspace, 'demo', 'p3',
                   f'Here is a {rejected} description.')
        harmonize('demo', fix=True)
        result2 = harmonize('demo', fix=True)
        # After fix, the rejected term should no longer appear as an issue
        remaining = [i for i in result2['issues']
                     if i['found_term'].lower() == rejected.lower()]
        assert remaining == []

    def test_no_active_profile(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'p4', 'content')
        result = harmonize('demo', fix=False)
        assert 'error' in result


class TestValidateWritingStyle:
    """
    validate_writing_style assembles a "review packet" for an LLM agent.
    Mneme does NOT grade prose itself; it just hands the agent the page +
    the active profile's writing-style block + section notes for the
    matched document type.
    """

    def test_packet_includes_profile_writing_style(self, temp_workspace):
        set_active_profile('eu-mdr')
        _make_page(temp_workspace, 'demo', 'dvr-tda',
                   'Some draft body content.',
                   title='Design Validation Report - TDA',
                   page_type='design-validation-report')
        packet = validate_writing_style('demo/dvr-tda')
        assert 'error' not in packet
        assert packet['profile_name'] == 'EU MDR'
        # Writing style block must travel with the packet
        ws = packet['writing_style']
        assert 'principles' in ws and ws['principles']
        assert 'general_rules' in ws and ws['general_rules']
        assert 'terminology_guidance' in ws
        assert 'framing_examples' in ws

    def test_packet_resolves_section_notes_via_frontmatter_type(self, temp_workspace):
        set_active_profile('eu-mdr')
        _make_page(temp_workspace, 'demo', 'dvr-tda',
                   'Body.',
                   title='DVR',
                   page_type='design-validation-report')
        packet = validate_writing_style('demo/dvr-tda')
        assert packet['document_type'] == 'design-validation-report'
        notes = packet['section_notes']
        # eu-mdr ships notes for these sections under design-validation-report
        assert 'context' in notes
        assert 'dataset-descriptions' in notes
        # And each note is a non-empty string
        assert all(isinstance(v, str) and v.strip() for v in notes.values())

    def test_no_section_match_when_frontmatter_type_unknown(self, temp_workspace):
        set_active_profile('eu-mdr')
        _make_page(temp_workspace, 'demo', 'random-page',
                   'Body.',
                   title='Random',
                   page_type='source-summary')
        packet = validate_writing_style('demo/random-page')
        # source-summary is not a profile section, so no notes apply
        assert packet['document_type'] is None
        assert packet['section_notes'] == {}
        # But the general writing_style block is still present
        assert packet['writing_style']

    def test_packet_includes_submission_checklist(self, temp_workspace):
        set_active_profile('eu-mdr')
        _make_page(temp_workspace, 'demo', 'dvr-tda', 'Body.',
                   page_type='design-validation-report')
        packet = validate_writing_style('demo/dvr-tda')
        assert isinstance(packet['submission_checklist'], list)
        assert len(packet['submission_checklist']) > 0

    def test_packet_includes_review_prompt(self, temp_workspace):
        set_active_profile('eu-mdr')
        _make_page(temp_workspace, 'demo', 'dvr-tda', 'Body.',
                   page_type='design-validation-report')
        packet = validate_writing_style('demo/dvr-tda')
        assert isinstance(packet['review_prompt'], str)
        assert 'EU MDR' in packet['review_prompt'] or 'profile' in packet['review_prompt']

    def test_packet_includes_full_page_content(self, temp_workspace):
        set_active_profile('eu-mdr')
        body_marker = 'UNIQUE_BODY_TOKEN_42'
        _make_page(temp_workspace, 'demo', 'dvr-tda', body_marker,
                   page_type='design-validation-report')
        packet = validate_writing_style('demo/dvr-tda')
        assert body_marker in packet['page_content']

    def test_no_active_profile_returns_error(self, temp_workspace):
        # Don't set a profile
        _make_page(temp_workspace, 'demo', 'dvr-tda', 'Body.',
                   page_type='design-validation-report')
        packet = validate_writing_style('demo/dvr-tda')
        assert 'error' in packet

    def test_nonexistent_page_returns_error(self, temp_workspace):
        set_active_profile('eu-mdr')
        packet = validate_writing_style('demo/does-not-exist')
        assert 'error' in packet


class TestValidateConsistency:
    def test_inconsistent_standard_versions(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'doc-a',
                   'This complies with ISO 14971:2019 requirements.')
        _make_page(temp_workspace, 'demo', 'doc-b',
                   'Based on ISO 14971:2007 clauses.')
        result = validate_consistency('demo')
        assert result['total_issues'] >= 1
        assert len(result['standard_inconsistencies']) >= 1
        names = [s['standard'] for s in result['standard_inconsistencies']]
        assert any('14971' in n for n in names)

    def test_consistent_pages(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'doc-c',
                   'Complies with ISO 14971:2019.')
        _make_page(temp_workspace, 'demo', 'doc-d',
                   'Follows ISO 14971:2019 throughout.')
        result = validate_consistency('demo')
        assert result['total_issues'] == 0
        assert result['standard_inconsistencies'] == []

    def test_empty_client(self, temp_workspace):
        result = validate_consistency('demo')
        assert result['total_issues'] == 0
        assert result['standard_inconsistencies'] == []


# ---------------------------------------------------------------------------
# Workspace-local profiles (added in v0.4.0+)
# ---------------------------------------------------------------------------

class TestWorkspaceProfiles:
    """
    Profiles dropped at {workspace}/profiles/{name}.md should be loadable
    by name AND should shadow bundled profiles with the same name.
    """

    def _write_profile(self, workspace, name, content):
        """Drop a markdown profile into the workspace's profiles/ directory."""
        d = os.path.join(workspace, 'profiles')
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f'{name}.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return path

    _MINIMAL_PROFILE = """---
name: {name}
description: {description}
version: {version}
tone: formal
voice: passive-for-procedures
trace_types: [derived-from]
requirement_levels:
  shall: mandatory
vocabulary: []
---

# Principles

- Be precise.
"""

    def test_load_workspace_only_profile(self, temp_workspace):
        # A profile that doesn't exist in the bundled set
        self._write_profile(
            temp_workspace, 'parkiwatch',
            self._MINIMAL_PROFILE.format(
                name='ParkiWatch QMS',
                description='Internal parkiwatch QMS profile',
                version='1.0',
            ),
        )
        profile = load_profile('parkiwatch')
        assert profile['name'] == 'ParkiWatch QMS'

    def test_workspace_profile_shadows_bundled(self, temp_workspace):
        # Override the bundled eu-mdr profile with a workspace variant
        self._write_profile(
            temp_workspace, 'eu-mdr',
            self._MINIMAL_PROFILE.format(
                name='CUSTOM EU MDR',
                description='workspace override for testing',
                version='999',
            ),
        )
        profile = load_profile('eu-mdr')
        assert profile['name'] == 'CUSTOM EU MDR'
        assert profile['version'] == '999'

    def test_bundled_still_loads_when_no_workspace_override(self, temp_workspace):
        # No workspace profile -> bundled is returned
        profile = load_profile('eu-mdr')
        assert profile['name'] != 'CUSTOM EU MDR'
        assert 'EU MDR' in profile['name']

    def test_set_active_accepts_workspace_profile(self, temp_workspace):
        self._write_profile(
            temp_workspace, 'parkiwatch',
            self._MINIMAL_PROFILE.format(
                name='ParkiWatch QMS',
                description='',
                version='1.0',
            ),
        )
        # Should not raise
        set_active_profile('parkiwatch')
        active = get_active_profile()
        assert active is not None
        assert active['name'] == 'ParkiWatch QMS'

    def test_set_active_rejects_truly_unknown(self, temp_workspace):
        with pytest.raises(FileNotFoundError):
            set_active_profile('does-not-exist-anywhere')

    def test_load_unknown_profile_error_lists_both_paths(self, temp_workspace):
        # The error should mention both lookup locations so users can debug
        with pytest.raises(FileNotFoundError) as exc_info:
            load_profile('totally-bogus-profile')
        msg = str(exc_info.value)
        assert 'workspace' in msg
        assert 'bundled' in msg


class TestWorkspaceCsvMappings:
    """
    Workspace-local CSV mappings should also resolve from
    {workspace}/profiles/mappings/{name}.json and shadow bundled ones.
    """

    def _write_mapping(self, workspace, name, data):
        d = os.path.join(workspace, 'profiles', 'mappings')
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f'{name}.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        return path

    def test_load_workspace_only_mapping(self, temp_workspace):
        from mneme.core import _load_csv_mapping
        self._write_mapping(temp_workspace, 'parkiwatch-incidents', {
            'name': 'Parkiwatch Incidents Import',
            'description': 'Maps incident-tracker CSVs',
            'page_type': 'incident',
            'id_column': 'Ticket',
            'title_column': 'Summary',
            'detect_headers': ['parkiwatch incident'],
            'mapping': {
                'Ticket': 'frontmatter.id',
                'Summary': 'frontmatter.title',
            },
        })
        mapping = _load_csv_mapping('parkiwatch-incidents')
        assert mapping['name'] == 'Parkiwatch Incidents Import'

    def test_workspace_mapping_shadows_bundled(self, temp_workspace):
        from mneme.core import _load_csv_mapping
        self._write_mapping(temp_workspace, 'user-needs', {
            'name': 'CUSTOM user-needs',
            'description': '',
            'page_type': 'user-need',
            'id_column': 'ID',
            'title_column': 'Title',
            'detect_headers': [],
            'mapping': {},
        })
        mapping = _load_csv_mapping('user-needs')
        assert mapping['name'] == 'CUSTOM user-needs'

    def test_detect_csv_mapping_finds_workspace_mapping(self, temp_workspace):
        from mneme.core import _detect_csv_mapping
        self._write_mapping(temp_workspace, 'parkiwatch-incidents', {
            'name': 'Parkiwatch Incidents',
            'description': '',
            'page_type': 'incident',
            'id_column': 'Ticket',
            'title_column': 'Summary',
            'detect_headers': ['parkiwatch incident', 'incident-id'],
            'mapping': {
                'Ticket': 'frontmatter.id',
                'Summary': 'frontmatter.title',
                'Severity': 'body.severity',
                'Reporter': 'body.reporter',
            },
        })
        result = _detect_csv_mapping(['Ticket', 'Summary', 'Severity', 'Reporter'])
        assert result == 'parkiwatch-incidents'


# ---------------------------------------------------------------------------
# Markdown profile parser
# ---------------------------------------------------------------------------

class TestMarkdownProfileParser:
    """
    The .md profile parser is the source of truth for both bundled and
    workspace profiles. These tests pin the parser's behavior on every
    recognized field so we can iterate on profiles without breaking shape.
    """

    def _write(self, path, content):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)

    def test_parse_minimal(self, temp_workspace):
        from mneme.core import _load_profile_from_md
        path = os.path.join(temp_workspace, 'profiles', 'tiny.md')
        self._write(path, "---\nname: Tiny\nversion: 1.0\n---\n")
        p = _load_profile_from_md(path)
        assert p['name'] == 'Tiny'
        assert p['version'] == '1.0'
        assert p['vocabulary']['preferred'] == []
        assert p['sections'] == {}

    def test_parse_full_eu_mdr_round_trip(self):
        # The bundled eu-mdr.md is the canonical exercise of every recognized
        # heading and field type. If this test passes, the parser handles
        # everything we need for the v0.4.0 release.
        from mneme.core import load_profile
        p = load_profile('eu-mdr')
        assert p['name'] == 'EU MDR'
        assert p['version'] == '2.0'
        assert p['tone'] == 'formal'
        assert len(p['vocabulary']['preferred']) == 15
        # First vocab entry uses the in-memory `term:` shape, not the .md `use:`
        assert p['vocabulary']['preferred'][0]['term'] == 'medical device'
        assert 'product' in p['vocabulary']['preferred'][0]['reject']
        assert p['vocabulary']['requirement_levels']['shall'].startswith('mandatory')
        assert len(p['trace_types']) == 8
        ws = p['writing_style']
        assert len(ws['principles']) == 3
        assert len(ws['general_rules']) == 8
        assert len(ws['terminology_guidance']) == 7
        assert ws['terminology_guidance'][0]['use'].startswith('Kinematic')
        assert len(ws['framing_examples']) == 2
        assert ws['framing_examples'][0]['context'] == 'Describing correlation results'
        assert 'monotonic correlation' in ws['framing_examples'][0]['correct']
        assert ws['placeholder_for_missing_refs'] == '[TO ADD REF]'
        # Document types and their section_notes
        assert 'design-validation-report' in p['sections']
        dvr = p['sections']['design-validation-report']
        assert dvr['description'].startswith('Design Validation Report')
        notes = dvr['section_notes']
        assert 'context' in notes
        assert 'literature review' in notes['context'].lower()
        assert 'dataset-descriptions' in notes
        # Submission checklist
        assert len(p['submission_checklist']) == 15
        assert 'Conclusion' in p['submission_checklist'][-1]

    def test_parse_terminology_table(self, temp_workspace):
        from mneme.core import _load_profile_from_md
        path = os.path.join(temp_workspace, 'profiles', 'term.md')
        self._write(path, """---
name: Term Test
version: 1.0
---

# Terminology

| Use | Instead of | Why |
|---|---|---|
| foo | bar, baz | because |
| alpha | beta | rationale here |
""")
        p = _load_profile_from_md(path)
        terms = p['writing_style']['terminology_guidance']
        assert len(terms) == 2
        assert terms[0]['use'] == 'foo'
        assert terms[0]['instead_of'] == ['bar', 'baz']
        assert terms[0]['rationale'] == 'because'
        assert terms[1]['instead_of'] == ['beta']

    def test_parse_framing_block(self, temp_workspace):
        from mneme.core import _load_profile_from_md
        path = os.path.join(temp_workspace, 'profiles', 'framing.md')
        self._write(path, """---
name: Framing Test
version: 1.0
---

# Framing: Reporting numbers

**Wrong:**

> The result was excellent.

**Correct:**

> MCC = 0.91 against the reference standard.

**Why:** the wrong version is editorial; the correct version reports the value.
""")
        p = _load_profile_from_md(path)
        examples = p['writing_style']['framing_examples']
        assert len(examples) == 1
        ex = examples[0]
        assert ex['context'] == 'Reporting numbers'
        assert 'excellent' in ex['wrong']
        assert 'MCC' in ex['correct']
        assert 'editorial' in ex['why']

    def test_parse_document_type_with_sections(self, temp_workspace):
        from mneme.core import _load_profile_from_md
        path = os.path.join(temp_workspace, 'profiles', 'doctype.md')
        self._write(path, """---
name: DocType Test
version: 1.0
---

# Document Type: my-report

A description of my-report goes here as plain prose.

## Section: introduction

Write the introduction first.

## Section: methods

Describe the methods used.
""")
        p = _load_profile_from_md(path)
        assert 'my-report' in p['sections']
        sec = p['sections']['my-report']
        assert sec['description'].startswith('A description')
        assert sec['section_notes']['introduction'].startswith('Write')
        assert sec['section_notes']['methods'].startswith('Describe')

    def test_parse_vocabulary_block(self, temp_workspace):
        from mneme.core import _load_profile_from_md
        path = os.path.join(temp_workspace, 'profiles', 'vocab.md')
        self._write(path, """---
name: Vocab Test
version: 1.0
vocabulary:
  - use: medical device
    reject: [product, unit]
  - use: nonconformity
    reject: [bug, defect, issue]
---
""")
        p = _load_profile_from_md(path)
        vocab = p['vocabulary']['preferred']
        assert len(vocab) == 2
        assert vocab[0]['term'] == 'medical device'
        assert vocab[0]['reject'] == ['product', 'unit']
        assert vocab[1]['term'] == 'nonconformity'
        assert 'bug' in vocab[1]['reject']
        assert 'defect' in vocab[1]['reject']
        assert 'issue' in vocab[1]['reject']

    def test_parse_submission_checklist(self, temp_workspace):
        from mneme.core import _load_profile_from_md
        path = os.path.join(temp_workspace, 'profiles', 'checklist.md')
        self._write(path, """---
name: Checklist Test
version: 1.0
---

# Submission Checklist

- All references include ID and version
- No clinical claims
- Conclusion is unambiguous
""")
        p = _load_profile_from_md(path)
        assert p['submission_checklist'] == [
            'All references include ID and version',
            'No clinical claims',
            'Conclusion is unambiguous',
        ]

    def test_unknown_h1_heading_silently_ignored(self, temp_workspace):
        from mneme.core import _load_profile_from_md
        path = os.path.join(temp_workspace, 'profiles', 'extra.md')
        self._write(path, """---
name: Extra Test
version: 1.0
---

# Authoring Notes

This is just a note for the author. Should not appear in the parsed output.

# Principles

- Be precise.
""")
        p = _load_profile_from_md(path)
        assert p['writing_style']['principles'] == ['Be precise.']
        # Authoring Notes is not a recognized heading - it just gets ignored

    def test_missing_frontmatter_raises(self, temp_workspace):
        from mneme.core import _load_profile_from_md
        path = os.path.join(temp_workspace, 'profiles', 'broken.md')
        self._write(path, "# Just a heading, no frontmatter\n")
        with pytest.raises(ValueError, match='frontmatter'):
            _load_profile_from_md(path)
