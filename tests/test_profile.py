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
    validate_structure,
    validate_consistency,
    _apply_workspace_override,
)


@pytest.fixture
def temp_workspace(monkeypatch):
    """Build a clean temp workspace and rebind mneme path constants at it."""
    td = tempfile.mkdtemp(prefix='mneme-profile-test-')
    for sub in ('wiki', 'sources', 'schema', 'memvid', os.path.join('memvid', 'per-client')):
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


EU_MDR_PATH = os.path.join(os.path.dirname(__file__), '..', 'mneme', 'profiles', 'eu-mdr.json')
with open(EU_MDR_PATH) as _f:
    EU_MDR = json.load(_f)


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


class TestValidateStructure:
    def test_conforming_page(self, temp_workspace):
        set_active_profile('eu-mdr')
        # Use 'risk-management' template
        required = EU_MDR['sections']['risk-management']['required']
        body = '\n\n'.join(f'## {s.replace("-", " ")}\n\nContent.' for s in required)
        _make_page(temp_workspace, 'demo', 'risk-management-file', body,
                   title='Risk Management File')
        result = validate_structure('demo/risk-management-file')
        assert 'error' not in result
        assert result['missing_sections'] == []
        assert result['template'] == 'risk-management'

    def test_missing_sections(self, temp_workspace):
        set_active_profile('eu-mdr')
        _make_page(temp_workspace, 'demo', 'risk-management-doc',
                   '## scope\n\nOnly scope here.',
                   title='Risk Management Doc')
        result = validate_structure('demo/risk-management-doc')
        assert 'error' not in result
        assert result['template'] == 'risk-management'
        assert len(result['missing_sections']) > 0
        assert 'scope' not in result['missing_sections']

    def test_nonexistent_page(self, temp_workspace):
        set_active_profile('eu-mdr')
        result = validate_structure('demo/does-not-exist')
        assert 'error' in result


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
    Profiles dropped at {workspace}/profiles/{name}.json should be loadable
    by name AND should shadow bundled profiles with the same name.
    """

    def _write_profile(self, workspace, name, data):
        """Drop a JSON profile into the workspace's profiles/ directory."""
        d = os.path.join(workspace, 'profiles')
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f'{name}.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        return path

    def test_load_workspace_only_profile(self, temp_workspace):
        # A profile that doesn't exist in the bundled set
        self._write_profile(temp_workspace, 'parkiwatch', {
            'name': 'ParkiWatch QMS',
            'description': 'Internal parkiwatch QMS profile',
            'version': '1.0',
            'vocabulary': {'preferred': [], 'requirement_levels': {}},
            'sections': {},
        })
        profile = load_profile('parkiwatch')
        assert profile['name'] == 'ParkiWatch QMS'

    def test_workspace_profile_shadows_bundled(self, temp_workspace):
        # Override the bundled eu-mdr profile with a workspace variant
        self._write_profile(temp_workspace, 'eu-mdr', {
            'name': 'CUSTOM EU MDR',
            'description': 'workspace override for testing',
            'version': '999',
            'vocabulary': {'preferred': [], 'requirement_levels': {}},
            'sections': {},
        })
        profile = load_profile('eu-mdr')
        assert profile['name'] == 'CUSTOM EU MDR'
        assert profile['version'] == '999'

    def test_bundled_still_loads_when_no_workspace_override(self, temp_workspace):
        # No workspace profile -> bundled is returned
        profile = load_profile('eu-mdr')
        assert profile['name'] != 'CUSTOM EU MDR'
        assert 'EU MDR' in profile['name']

    def test_set_active_accepts_workspace_profile(self, temp_workspace):
        self._write_profile(temp_workspace, 'parkiwatch', {
            'name': 'ParkiWatch QMS',
            'description': '',
            'version': '1.0',
            'vocabulary': {'preferred': [], 'requirement_levels': {}},
            'sections': {},
        })
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
