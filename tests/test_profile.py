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
