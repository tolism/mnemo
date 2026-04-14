"""Tests for schema helpers, search, stats, tags, dedupe, export, snapshot, scan_repo."""
import json
import os
import shutil
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mneme.core import (
    _update_entities_schema,
    _update_tags_schema,
    _update_index,
    _locked_read_modify_write,
    _search_wiki_text,
    dual_search,
    check_drift,
    get_stats,
    status,
    recent,
    sync_all_pages,
    tags_list,
    tags_merge,
    dedupe,
    export_client,
    snapshot,
    diff_page,
    scan_repo,
    _apply_workspace_override,
    parse_frontmatter,
)


@pytest.fixture
def temp_workspace(monkeypatch):
    """Build a clean temp workspace and rebind mneme path constants at it."""
    td = tempfile.mkdtemp(prefix='mneme-schtest-')
    for sub in ('wiki', 'sources', 'schema'):
        os.makedirs(os.path.join(td, sub), exist_ok=True)
    with open(os.path.join(td, 'index.md'), 'w') as f:
        f.write('# Index\nLast updated: 2026-01-01\n\n')
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


def _make_page(workspace, client, slug, body='', title=None, tags=None, page_type='source-summary'):
    d = os.path.join(workspace, 'wiki', client)
    os.makedirs(d, exist_ok=True)
    title = title or slug
    tags = tags or [client]
    fm = (
        '---\n'
        f'title: {title}\n'
        f'type: {page_type}\n'
        f'client: {client}\n'
        'sources: []\n'
        f'tags: [{", ".join(tags)}]\n'
        'related: []\n'
        'created: 2026-04-08\n'
        'updated: 2026-04-08\n'
        'confidence: medium\n'
        '---\n\n'
        f'{body}\n'
    )
    path = os.path.join(d, f'{slug}.md')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(fm)
    return path


class TestSchemaHelpers:
    def test_update_entities_extracts(self, temp_workspace):
        p = _make_page(temp_workspace, 'demo', 'p1', 'Acme Corp ships the Cardio Monitor')
        n = _update_entities_schema('demo', p, 'Acme Corp ships the Cardio Monitor', '2026-04-08')
        assert n >= 1
        with open(os.path.join(temp_workspace, 'schema', 'entities.json')) as f:
            data = json.load(f)
        names = [e['name'] for e in data['entities']]
        assert 'Acme Corp' in names or 'Cardio Monitor' in names

    def test_update_entities_idempotent(self, temp_workspace):
        p = _make_page(temp_workspace, 'demo', 'p1', 'Acme Corp')
        _update_entities_schema('demo', p, 'Acme Corp ships the Cardio Monitor', '2026-04-08')
        _update_entities_schema('demo', p, 'Acme Corp ships the Cardio Monitor', '2026-04-08')
        with open(os.path.join(temp_workspace, 'schema', 'entities.json')) as f:
            data = json.load(f)
        ids = [e['id'] for e in data['entities']]
        assert len(ids) == len(set(ids))

    def test_update_entities_stopwords(self, temp_workspace):
        p = _make_page(temp_workspace, 'demo', 'p1', '')
        _update_entities_schema('demo', p, 'Key Facts is boring. Open Questions here.', '2026-04-08')
        with open(os.path.join(temp_workspace, 'schema', 'entities.json')) as f:
            data = json.load(f)
        names = [e['name'] for e in data['entities']]
        assert 'Key Facts' not in names
        assert 'Open Questions' not in names

    def test_update_tags_schema_basic(self, temp_workspace):
        p = _make_page(temp_workspace, 'demo', 'p1')
        _update_tags_schema(p, {'tags': ['urgent', 'q2']})
        with open(os.path.join(temp_workspace, 'schema', 'tags.json')) as f:
            data = json.load(f)
        assert 'urgent' in data['tags']
        assert 'q2' in data['tags']
        assert data['tags']['urgent']['count'] == 1

    def test_update_tags_schema_additive(self, temp_workspace):
        p1 = _make_page(temp_workspace, 'demo', 'p1')
        p2 = _make_page(temp_workspace, 'demo', 'p2')
        _update_tags_schema(p1, {'tags': ['urgent']})
        _update_tags_schema(p2, {'tags': ['urgent', 'extra']})
        with open(os.path.join(temp_workspace, 'schema', 'tags.json')) as f:
            data = json.load(f)
        assert data['tags']['urgent']['count'] == 2
        assert data['tags']['extra']['count'] == 1

    def test_update_index_adds_entry(self, temp_workspace):
        _update_index('demo', 'page-1', 'wiki/demo/page-1.md', '2026-04-08')
        with open(os.path.join(temp_workspace, 'index.md')) as f:
            c = f.read()
        assert '[[demo/page-1]]' in c

    def test_update_index_idempotent(self, temp_workspace):
        _update_index('demo', 'page-1', 'wiki/demo/page-1.md', '2026-04-08')
        _update_index('demo', 'page-1', 'wiki/demo/page-1.md', '2026-04-08')
        with open(os.path.join(temp_workspace, 'index.md')) as f:
            c = f.read()
        assert c.count('[[demo/page-1]]') == 1


class TestLockedReadModifyWrite:
    def test_unchanged(self, temp_workspace):
        fp = os.path.join(temp_workspace, 'a.txt')
        with open(fp, 'w') as f:
            f.write('hello')
        _locked_read_modify_write(fp, lambda c: c)
        with open(fp) as f:
            assert f.read() == 'hello'

    def test_uppercase(self, temp_workspace):
        fp = os.path.join(temp_workspace, 'a.txt')
        with open(fp, 'w') as f:
            f.write('hello')
        _locked_read_modify_write(fp, lambda c: c.upper())
        with open(fp) as f:
            assert f.read() == 'HELLO'

    def test_creates_nonexistent(self, temp_workspace):
        fp = os.path.join(temp_workspace, 'new.txt')
        _locked_read_modify_write(fp, lambda c: 'written')
        assert os.path.exists(fp)
        with open(fp) as f:
            assert f.read() == 'written'

    def test_raising_modifier_preserves_file(self, temp_workspace):
        fp = os.path.join(temp_workspace, 'a.txt')
        with open(fp, 'w') as f:
            f.write('original')

        def bad(c):
            raise RuntimeError('boom')

        with pytest.raises(RuntimeError):
            _locked_read_modify_write(fp, bad)
        with open(fp) as f:
            assert f.read() == 'original'


class TestSearch:
    def test_empty_workspace(self, temp_workspace):
        assert _search_wiki_text('quarterly') == []

    def test_finds_match(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'p1', 'the quarterly budget report')
        _make_page(temp_workspace, 'demo', 'p2', 'the annual review')
        sync_all_pages()
        results = _search_wiki_text('quarterly')
        assert len(results) == 1
        assert 'p1' in results[0]['wiki_path']

    def test_k_cap(self, temp_workspace):
        for i in range(5):
            _make_page(temp_workspace, 'demo', f'p{i}', 'quarterly budget')
        sync_all_pages()
        results = _search_wiki_text('quarterly', k=2)
        assert len(results) == 2

    def test_dual_search_client_filter(self, temp_workspace):
        _make_page(temp_workspace, 'demoA', 'p1', 'quarterly data')
        _make_page(temp_workspace, 'demoB', 'p1', 'quarterly data')
        sync_all_pages()
        results = dual_search('quarterly', client='demoA')
        assert len(results) >= 1
        for r in results:
            assert 'demoA' in r.get('wiki_path', '')

    def test_dual_search_returns_list(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'p1', 'quarterly')
        sync_all_pages()
        out = dual_search('quarterly')
        assert isinstance(out, list)


class TestCheckDrift:
    def test_empty(self, temp_workspace):
        r = check_drift()
        assert isinstance(r, dict)
        assert 'unindexed' in r
        assert 'orphaned' in r
        assert 'stale' in r
        assert 'summary' in r

    def test_unindexed_page(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'p1', 'body')
        r = check_drift()
        assert isinstance(r, dict)
        # Page exists on disk but is not yet indexed
        assert any('p1' in p for p in r['unindexed'])
        assert r['is_drifted'] is True

    def test_synced_page_is_not_drifted(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'p1', 'body')
        sync_all_pages()
        r = check_drift()
        assert r['unindexed'] == []
        assert r['stale'] == []
        assert r['is_drifted'] is False


class TestGetStats:
    def test_empty(self, temp_workspace):
        s = get_stats()
        assert 'wiki' in s
        assert 'schema' in s
        assert s['wiki']['total_pages'] == 0

    def test_populated(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'p1')
        _make_page(temp_workspace, 'demo', 'p2')
        _make_page(temp_workspace, 'demo', 'p3')
        with open(os.path.join(temp_workspace, 'schema', 'entities.json'), 'w') as f:
            json.dump({'version': 1, 'updated': '2026-04-08', 'entities': [
                {'id': 'a', 'name': 'A', 'type': 'company', 'client': 'demo', 'wiki_page': 'demo/p1', 'tags': []},
                {'id': 'b', 'name': 'B', 'type': 'company', 'client': 'demo', 'wiki_page': 'demo/p2', 'tags': []},
            ]}, f)
        s = get_stats()
        assert s['wiki']['total_pages'] == 3
        assert s['schema']['entity_count'] == 2


class TestStatusRecent:
    def test_status(self, temp_workspace):
        s = status()
        assert isinstance(s, dict)
        assert 'wiki_pages' in s

    def test_recent_empty(self, temp_workspace):
        r = recent(5)
        assert r == []

    def test_recent_order(self, temp_workspace):
        log = os.path.join(temp_workspace, 'log.md')
        with open(log, 'w') as f:
            f.write(
                '# Log\n\n'
                '## [2026-04-08] INGEST | file3.md\n- info\n\n'
                '## [2026-04-07] INGEST | file2.md\n- info\n\n'
                '## [2026-04-06] INGEST | file1.md\n- info\n\n'
            )
        r = recent(2)
        assert len(r) == 2
        assert r[0]['description'].startswith('file3')
        assert r[1]['description'].startswith('file2')


class TestTags:
    def test_empty(self, temp_workspace):
        assert tags_list() == {}

    def test_populated(self, temp_workspace):
        with open(os.path.join(temp_workspace, 'schema', 'tags.json'), 'w') as f:
            json.dump({'version': 1, 'updated': '2026-04-08',
                       'tags': {'urgent': {'count': 2, 'pages': ['demo/p1', 'demo/p2']}}}, f)
        t = tags_list()
        assert 'urgent' in t
        assert t['urgent']['count'] == 2

    def test_merge(self, temp_workspace):
        # Seed pages with old-tag in tags list (one per line so regex matches)
        for slug in ('p1', 'p2'):
            d = os.path.join(temp_workspace, 'wiki', 'demo')
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f'{slug}.md'), 'w') as f:
                f.write(
                    '---\n'
                    f'title: {slug}\n'
                    'type: source-summary\n'
                    'client: demo\n'
                    'sources: []\n'
                    'tags:\n'
                    '  - old-tag\n'
                    'related: []\n'
                    'created: 2026-04-08\n'
                    'updated: 2026-04-08\n'
                    'confidence: medium\n'
                    '---\n\nbody\n'
                )
        with open(os.path.join(temp_workspace, 'schema', 'tags.json'), 'w') as f:
            json.dump({'version': 1, 'updated': '2026-04-08',
                       'tags': {'old-tag': {'count': 2, 'pages': ['demo/p1', 'demo/p2']}}}, f)
        res = tags_merge('old-tag', 'new-tag')
        assert res['pages_updated'] == 2
        with open(os.path.join(temp_workspace, 'schema', 'tags.json')) as f:
            data = json.load(f)
        assert 'old-tag' not in data['tags']
        assert 'new-tag' in data['tags']
        for slug in ('p1', 'p2'):
            with open(os.path.join(temp_workspace, 'wiki', 'demo', f'{slug}.md')) as f:
                c = f.read()
            assert 'new-tag' in c
            assert 'old-tag' not in c

    def test_merge_missing(self, temp_workspace):
        res = tags_merge('does-not-exist', 'new')
        assert res['pages_updated'] == 0


class TestDedupe:
    def test_empty(self, temp_workspace):
        r = dedupe()
        assert r['total_groups'] == 0

    def test_duplicates(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'p1', 'identical body content here')
        _make_page(temp_workspace, 'demo', 'p2', 'identical body content here')
        r = dedupe()
        assert r['total_groups'] >= 1

    def test_unique(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'p1', 'something unique alpha')
        _make_page(temp_workspace, 'demo', 'p2', 'something different beta')
        r = dedupe()
        assert r['total_groups'] == 0


class TestExport:
    def test_json(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'p1', 'body1')
        _make_page(temp_workspace, 'demo', 'p2', 'body2')
        path = export_client('demo', format='json')
        assert os.path.exists(path)
        with open(path) as f:
            data = json.load(f)
        assert len(data) == 2

    def test_md(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'p1', 'body1')
        _make_page(temp_workspace, 'demo', 'p2', 'body2')
        path = export_client('demo', format='md')
        assert os.path.exists(path)
        with open(path) as f:
            c = f.read()
        assert 'body1' in c and 'body2' in c

    def test_nonexistent_client(self, temp_workspace):
        path = export_client('nope', format='json')
        assert os.path.exists(path)
        with open(path) as f:
            data = json.load(f)
        assert data == []


@pytest.mark.skipif(shutil.which('git') is None, reason='git required')
class TestSnapshotAndDiff:
    def test_snapshot_creates_zip(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'p1', 'body')
        r = snapshot('demo')
        assert os.path.exists(r['path'])
        assert r['path'].endswith('.zip')
        assert r['pages_count'] == 1

    def test_diff_page_non_git(self, temp_workspace):
        out = diff_page('demo/p1')
        assert isinstance(out, str)


class TestScanRepo:
    def test_tiny_repo(self, temp_workspace):
        repo = tempfile.mkdtemp(prefix='fake-repo-')
        try:
            with open(os.path.join(repo, 'requirements.txt'), 'w') as f:
                f.write('requests==2.0\n')
            os.makedirs(os.path.join(repo, 'src'))
            with open(os.path.join(repo, 'src', 'main.py'), 'w') as f:
                f.write('print(1)\n')
            r = scan_repo(repo, 'demo')
            assert 'dependencies_found' in r
            assert 'modules_found' in r
            assert 'suggestions' in r
            assert any(d['name'] == 'requests' for d in r['dependencies_found'])
            assert 'src' in r['modules_found']
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_nonexistent_path(self, temp_workspace):
        r = scan_repo(os.path.join(temp_workspace, 'does-not-exist'), 'demo')
        assert 'error' in r
