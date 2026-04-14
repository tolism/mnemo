"""Tests for tornado, lint, init/new workspace, and demo-clean subsystems."""
import json
import os
import shutil
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mneme.core import (
    _detect_page_type,
    _detect_client,
    tornado,
    lint,
    init_workspace,
    new_workspace,
    clean_demo,
    _apply_workspace_override,
    parse_frontmatter,
)


@pytest.fixture
def temp_workspace():
    """Build a clean temp workspace and rebind mneme path constants at it."""
    td = tempfile.mkdtemp(prefix='mneme-tornado-test-')
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


def _make_page(workspace, client, slug, body, frontmatter_extra=None, page_type='source-summary'):
    d = os.path.join(workspace, 'wiki', client)
    os.makedirs(d, exist_ok=True)
    fm_lines = [
        '---',
        f'title: {slug}',
        f'type: {page_type}',
        f'client: {client}',
        'sources: []',
        'tags: []',
        'related: []',
        'created: 2026-04-08',
        'updated: 2026-04-08',
        'confidence: medium',
    ]
    if frontmatter_extra:
        fm_lines += frontmatter_extra
    fm_lines.append('---')
    page = '\n'.join(fm_lines) + '\n\n' + body + '\n'
    path = os.path.join(d, f'{slug}.md')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(page)
    return path


def _drop_in_inbox(workspace, name, content):
    inbox = os.path.join(workspace, 'inbox')
    os.makedirs(inbox, exist_ok=True)
    path = os.path.join(inbox, name)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return path


# ---------------------------------------------------------------------------
# _detect_page_type
# ---------------------------------------------------------------------------

class TestDetectPageType:
    def test_risk_content_returns_entity(self):
        # "hazard" is an entity keyword in _TYPE_KEYWORDS
        t = _detect_page_type('## Hazard\nRisk estimation performed.', {})
        assert t == 'entity'

    def test_frontmatter_type_wins(self):
        # frontmatter type must be one of the six valid types to be trusted
        t = _detect_page_type('random content', {'type': 'concept'})
        assert t == 'concept'

    def test_invalid_frontmatter_type_falls_through(self):
        # "hazard" is NOT in the allowed frontmatter type list; falls through
        t = _detect_page_type('just prose here', {'type': 'hazard'})
        assert t == 'source-summary'

    def test_plain_prose_default(self):
        t = _detect_page_type('Just some plain prose with nothing special.', {})
        assert t == 'source-summary'

    def test_csv_like_content(self):
        t = _detect_page_type('id,name,value\n1,foo,bar\n2,baz,qux\n', {})
        # No keywords match -> default
        assert t == 'source-summary'

    def test_meeting_notes_detected(self):
        t = _detect_page_type('Meeting notes\nAttendees: Alice, Bob\nAction items:', {})
        assert t == 'source-summary'


# ---------------------------------------------------------------------------
# _detect_client
# ---------------------------------------------------------------------------

class TestDetectClient:
    def test_frontmatter_client(self):
        c = _detect_client('body', {'client': 'cardio-monitor'}, 'file.md')
        assert c == 'cardio-monitor'

    def test_shared_frontmatter_ignored(self):
        c = _detect_client('body', {'client': '_shared'}, 'file.md')
        assert c is None

    def test_double_dash_filename_pattern(self):
        c = _detect_client('body', {}, 'cardio-monitor--meeting.md')
        assert c == 'cardio-monitor'

    def test_single_dash_filename_not_detected(self):
        # only "--" triggers filename detection
        c = _detect_client('body', {}, 'cardio-monitor-meeting.md')
        assert c is None

    def test_no_signals(self):
        c = _detect_client('plain content', {}, 'foo.md')
        assert c is None


# ---------------------------------------------------------------------------
# tornado
# ---------------------------------------------------------------------------

class TestTornado:
    def test_empty_inbox(self, temp_workspace):
        result = tornado(client_slug='demo')
        assert result['processed'] == 0

    def test_single_file_ingested(self, temp_workspace):
        _drop_in_inbox(temp_workspace, 'note.md', '# Note\n\nSome content.\n')
        result = tornado(client_slug='demo')
        assert result['processed'] >= 1
        # wiki page created under demo
        wiki_demo = os.path.join(temp_workspace, 'wiki', 'demo')
        assert os.path.isdir(wiki_demo)
        assert any(f.endswith('.md') for f in os.listdir(wiki_demo))
        # original archived
        sources_demo = os.path.join(temp_workspace, 'sources', 'demo')
        assert os.path.isfile(os.path.join(sources_demo, 'note.md'))
        # inbox cleared
        inbox_files = [f for f in os.listdir(os.path.join(temp_workspace, 'inbox'))
                       if os.path.isfile(os.path.join(temp_workspace, 'inbox', f))]
        assert inbox_files == []

    def test_dry_run_is_noop(self, temp_workspace):
        _drop_in_inbox(temp_workspace, 'preview.md', '# Preview\n\nStuff.\n')
        result = tornado(client_slug='demo', dry_run=True)
        assert result['processed'] >= 1
        # file still in inbox
        assert os.path.isfile(os.path.join(temp_workspace, 'inbox', 'preview.md'))
        # nothing in sources or wiki
        assert not os.path.exists(os.path.join(temp_workspace, 'sources', 'demo', 'preview.md'))
        wiki_demo = os.path.join(temp_workspace, 'wiki', 'demo')
        if os.path.isdir(wiki_demo):
            assert os.listdir(wiki_demo) == []

    def test_multi_file(self, temp_workspace):
        for i in range(3):
            _drop_in_inbox(temp_workspace, f'f{i}.md', f'# File {i}\n\nBody {i}.\n')
        result = tornado(client_slug='demo')
        assert result['processed'] == 3

    def test_autodetect_client_from_frontmatter(self, temp_workspace):
        fm = ('---\n'
              'title: Explicit\n'
              'client: explicit-client\n'
              '---\n\n'
              '# Explicit\n\nBody.\n')
        _drop_in_inbox(temp_workspace, 'exp.md', fm)
        result = tornado(client_slug=None)
        assert result['processed'] >= 1
        assert os.path.isdir(os.path.join(temp_workspace, 'wiki', 'explicit-client'))


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------

class TestLint:
    def test_empty_workspace_no_crash(self, temp_workspace):
        r = lint()
        assert r['total_issues'] == 0
        assert os.path.exists(r['report_path'])

    def test_orphan_page_flagged(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'alpha', 'Hello.')
        _make_page(temp_workspace, 'demo', 'beta', 'World.')
        r = lint()
        assert len(r['issues']['orphan_pages']) >= 1

    def test_dead_link_flagged(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'a', 'See [[demo/does-not-exist]] here.')
        r = lint()
        dead = r['issues']['dead_links']
        assert any('does-not-exist' in d['broken_link'] for d in dead)

    def test_missing_citations_flagged(self, temp_workspace):
        body = '\n'.join([
            '- ' + ('This is a long factual claim without any citation marker at all ' * 1),
            '- ' + ('Another factual claim with no source marker anywhere on the line ' * 1),
            '- ' + ('Yet a third factual uncited claim making statements about things ' * 1),
            '- ' + ('And a fourth uncited claim long enough to exceed the threshold len ' * 1),
            '- ' + ('And a fifth uncited claim long enough to exceed the threshold len ' * 1),
        ])
        _make_page(temp_workspace, 'demo', 'uncited', body)
        r = lint()
        mc = r['issues']['missing_citations']
        assert any('uncited' in m['page'] for m in mc)

    def test_report_file_written(self, temp_workspace):
        r = lint()
        assert os.path.isfile(r['report_path'])
        assert r['report_path'].startswith(os.path.join(temp_workspace, 'wiki'))


# ---------------------------------------------------------------------------
# init_workspace
# ---------------------------------------------------------------------------

class TestInitWorkspace:
    def test_creates_structure(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        init_workspace('TestProject', clients=['client-a'])
        assert (tmp_path / 'wiki').is_dir()
        assert (tmp_path / 'sources').is_dir()
        assert (tmp_path / 'schema').is_dir()
        assert (tmp_path / 'index.md').is_file()
        assert (tmp_path / 'log.md').is_file()
        assert (tmp_path / 'wiki' / 'client-a').is_dir()
        assert (tmp_path / 'schema' / 'entities.json').is_file()

    def test_default_project_name(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        init_workspace()
        idx = (tmp_path / 'index.md').read_text(encoding='utf-8')
        assert tmp_path.name in idx

    def test_multiple_clients(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        init_workspace('Multi', clients=['alpha', 'beta', 'gamma'])
        for c in ('alpha', 'beta', 'gamma'):
            assert (tmp_path / 'wiki' / c).is_dir()


# ---------------------------------------------------------------------------
# new_workspace
# ---------------------------------------------------------------------------

class TestNewWorkspace:
    def test_creates_populated_workspace(self, tmp_path):
        target = str(tmp_path / 'ws')
        result = new_workspace(
            target,
            project_name='Test',
            default_client='test',
            profile='iso-13485',
            description='hi',
            force=False,
        )
        assert result['files_written'] > 0
        assert (tmp_path / 'ws' / 'wiki' / 'test').is_dir()
        assert (tmp_path / 'ws' / 'sources' / 'test').is_dir()
        assert (tmp_path / 'ws' / '.mneme-profile').is_file()

    def test_profile_substitution(self, tmp_path):
        target = str(tmp_path / 'ws')
        new_workspace(target, project_name='P', default_client='c',
                      profile='iso-13485', description='', force=False)
        prof = (tmp_path / 'ws' / '.mneme-profile').read_text(encoding='utf-8').strip()
        assert prof == 'iso-13485'

    def test_existing_nonempty_raises(self, tmp_path):
        target = tmp_path / 'ws'
        target.mkdir()
        (target / 'junk.txt').write_text('x')
        with pytest.raises(FileExistsError):
            new_workspace(str(target), project_name='P', default_client='c',
                          profile=None, description='', force=False)

    def test_force_overwrites(self, tmp_path):
        target = tmp_path / 'ws'
        target.mkdir()
        (target / 'junk.txt').write_text('x')
        result = new_workspace(str(target), project_name='P', default_client='c',
                               profile=None, description='', force=True)
        assert result['files_written'] > 0

    def test_invalid_client_slug(self, tmp_path):
        with pytest.raises(ValueError):
            new_workspace(str(tmp_path / 'ws'), project_name='P',
                          default_client='Bad Slug!', profile=None,
                          description='', force=False)


# ---------------------------------------------------------------------------
# clean_demo
# ---------------------------------------------------------------------------

class TestCleanDemo:
    def test_empty_workspace_dry_run(self, temp_workspace):
        r = clean_demo(client_slug='demo-retail', dry_run=True)
        assert r['schema_entities_removed'] == 0
        assert r['directories'] == [] or all('demo' in d for d in r['directories'])

    def test_nonexistent_client_graceful(self, temp_workspace):
        r = clean_demo(client_slug='no-such-client', dry_run=False)
        assert r['schema_entities_removed'] == 0

    def test_full_wipe(self, temp_workspace):
        # seed wiki pages
        _make_page(temp_workspace, 'demo-retail', 'page1', 'hi')
        _make_page(temp_workspace, 'demo-retail', 'page2', 'ho')
        # seed sources
        src_dir = os.path.join(temp_workspace, 'sources', 'demo-retail')
        os.makedirs(src_dir, exist_ok=True)
        with open(os.path.join(src_dir, 'raw.md'), 'w') as f:
            f.write('raw')
        # seed top-level demo dir
        top_demo = os.path.join(temp_workspace, 'demo')
        os.makedirs(top_demo, exist_ok=True)
        with open(os.path.join(top_demo, 'sample.md'), 'w') as f:
            f.write('x')
        # seed schema entities + tags
        with open(os.path.join(temp_workspace, 'schema', 'entities.json'), 'w') as f:
            json.dump({'version': 1, 'updated': '2026-01-01', 'entities': [
                {'id': 'x', 'name': 'X', 'client': 'demo-retail', 'wiki_page': 'demo-retail/page1'},
                {'id': 'y', 'name': 'Y', 'client': 'other', 'wiki_page': 'other/pg'},
            ]}, f)
        with open(os.path.join(temp_workspace, 'schema', 'tags.json'), 'w') as f:
            json.dump({'version': 1, 'updated': '2026-01-01', 'tags': {
                'risk': {'count': 1, 'description': '', 'pages': ['demo-retail/page1']},
                'keep': {'count': 1, 'description': '', 'pages': ['other/pg']},
            }}, f)

        r = clean_demo(client_slug='demo-retail', dry_run=False)

        assert not os.path.isdir(os.path.join(temp_workspace, 'wiki', 'demo-retail'))
        assert not os.path.isdir(os.path.join(temp_workspace, 'sources', 'demo-retail'))
        assert not os.path.isdir(top_demo)

        with open(os.path.join(temp_workspace, 'schema', 'entities.json')) as f:
            ents = json.load(f)
        assert all(e.get('client') != 'demo-retail' for e in ents['entities'])

        with open(os.path.join(temp_workspace, 'schema', 'tags.json')) as f:
            tags = json.load(f)
        assert 'risk' not in tags['tags']
        assert 'keep' in tags['tags']

        assert r['schema_entities_removed'] >= 1

    def test_dry_run_preserves_content(self, temp_workspace):
        _make_page(temp_workspace, 'demo-retail', 'page1', 'hi')
        r = clean_demo(client_slug='demo-retail', dry_run=True)
        assert os.path.isdir(os.path.join(temp_workspace, 'wiki', 'demo-retail'))
        assert r['directories']  # reported what would be removed
