"""
Comprehensive pytest test suite for mneme core.

Covers:
  - parse_frontmatter (pure parsing)
  - _title_from_slug (slug conversion)
  - _content_hash (hashing)
  - sync_page_to_index (FTS5 upsert)
  - CLI integration (subprocess)
  - Ingest integration (with cleanup)
  - Resync (3-way merge) unit + integration
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

# Ensure the package is importable from the tests/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mneme.core import (
    _content_hash,
    _title_from_slug,
    parse_frontmatter,
)

# ---------------------------------------------------------------------------
# Category 1: Parsing (pure functions, no I/O)
# ---------------------------------------------------------------------------

class TestParseFrontmatter:
    def test_valid_frontmatter(self):
        content = "---\ntitle: Test Page\ntype: entity\nclient: test\n---\n\n## Body content here"
        fm, body = parse_frontmatter(content)
        assert fm['title'] == 'Test Page'
        assert fm['type'] == 'entity'
        assert '## Body content here' in body

    def test_no_frontmatter(self):
        content = "Just plain text"
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert body == "Just plain text"

    def test_empty_string(self):
        fm, body = parse_frontmatter("")
        assert fm == {}
        assert body == ""

    def test_frontmatter_with_list(self):
        content = "---\ntitle: Test\ntags:\n  - tag1\n  - tag2\n---\nBody"
        fm, body = parse_frontmatter(content)
        assert fm['tags'] == ['tag1', 'tag2']

    def test_colon_in_value(self):
        content = "---\ntitle: Meeting: Q1 Review\n---\nBody"
        fm, body = parse_frontmatter(content)
        assert fm['title'] == 'Meeting: Q1 Review'

    def test_quoted_value(self):
        content = '---\ntitle: "Test Title"\n---\nBody'
        fm, body = parse_frontmatter(content)
        assert fm['title'] == 'Test Title'

    def test_frontmatter_only_no_body(self):
        content = "---\ntitle: Test\ntype: entity\n---\n"
        fm, body = parse_frontmatter(content)
        assert fm['title'] == 'Test'
        assert body.strip() == ''

    def test_multiple_fields(self):
        content = "---\ntitle: Full Page\ntype: overview\nclient: acme\nconfidence: high\ncreated: 2026-01-01\n---\nContent here"
        fm, body = parse_frontmatter(content)
        assert fm['title'] == 'Full Page'
        assert fm['type'] == 'overview'
        assert fm['client'] == 'acme'
        assert fm['confidence'] == 'high'
        assert fm['created'] == '2026-01-01'
        assert 'Content here' in body

    def test_body_preserved_after_frontmatter(self):
        content = "---\ntitle: Test\n---\n\n## Section\n\nSome text here."
        fm, body = parse_frontmatter(content)
        assert '## Section' in body
        assert 'Some text here.' in body

    def test_single_quoted_value(self):
        content = "---\ntitle: 'Single Quoted'\n---\nBody"
        fm, body = parse_frontmatter(content)
        assert fm['title'] == 'Single Quoted'

    def test_missing_closing_delimiter_treated_as_no_frontmatter(self):
        # No closing --- means the regex won't match, body is entire content
        content = "---\ntitle: Test\nNo closing delimiter"
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert body == content

    def test_related_field_as_empty_list(self):
        content = "---\ntitle: Test\nrelated:\n---\nBody"
        fm, body = parse_frontmatter(content)
        # Empty list yields []
        assert fm.get('related') == []

    def test_sources_list(self):
        content = "---\ntitle: Test\nsources:\n  - sources/pdfs/doc.pdf\n  - sources/emails/msg.txt\n---\nContent"
        fm, body = parse_frontmatter(content)
        assert isinstance(fm['sources'], list)
        assert 'sources/pdfs/doc.pdf' in fm['sources']
        assert 'sources/emails/msg.txt' in fm['sources']


# ---------------------------------------------------------------------------
# Category 2: Title from slug
# ---------------------------------------------------------------------------

class TestTitleFromSlug:
    def test_hyphenated(self):
        assert _title_from_slug("hello-world") == "Hello World"

    def test_underscored(self):
        assert _title_from_slug("test_document") == "Test Document"

    def test_md_extension_becomes_part_of_last_word(self):
        title = _title_from_slug("my-file.md")
        assert "My File" in title

    def test_single_word(self):
        assert _title_from_slug("overview") == "Overview"

    def test_multi_word_hyphenated(self):
        assert _title_from_slug("product-overview-v2") == "Product Overview V2"

    def test_mixed_separators(self):
        result = _title_from_slug("hello_world-test")
        assert result == "Hello World Test"

    def test_empty_string(self):
        result = _title_from_slug("")
        assert result == ""

    def test_single_hyphen(self):
        result = _title_from_slug("-")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Category 3: Content hash
# ---------------------------------------------------------------------------

class TestContentHash:
    def test_same_content_same_hash(self):
        assert _content_hash("hello world") == _content_hash("hello world")

    def test_different_content_different_hash(self):
        assert _content_hash("hello") != _content_hash("world")

    def test_empty_string_returns_hash(self):
        h = _content_hash("")
        assert isinstance(h, str)
        assert len(h) == 32  # MD5 hex digest length

    def test_hash_is_32_hex_chars(self):
        h = _content_hash("some content here")
        assert len(h) == 32
        assert all(c in '0123456789abcdef' for c in h)

    def test_whitespace_sensitive(self):
        # Leading/trailing whitespace changes the hash
        assert _content_hash("hello") != _content_hash(" hello")
        assert _content_hash("hello") != _content_hash("hello ")

    def test_unicode_content(self):
        h = _content_hash("Enterprise AI consulting solutions deployed globally")
        assert isinstance(h, str)
        assert len(h) == 32

    def test_long_content_returns_fixed_length(self):
        long_content = "word " * 10000
        h = _content_hash(long_content)
        assert len(h) == 32


# ---------------------------------------------------------------------------
# Category 4: sync_page_to_index (FTS5 upsert)
# ---------------------------------------------------------------------------

@pytest.fixture
def sync_workspace():
    """Build a clean temp workspace and rebind mneme path constants."""
    td = tempfile.mkdtemp(prefix='mneme-sync-test-')
    for sub in ('wiki', 'sources', 'schema'):
        os.makedirs(os.path.join(td, sub), exist_ok=True)
    with open(os.path.join(td, 'index.md'), 'w') as f:
        f.write('# Index\n')
    with open(os.path.join(td, 'log.md'), 'w') as f:
        f.write('# Log\n')
    for name, default in [
        ('entities.json', {'version': 1, 'updated': '2026-01-01', 'entities': []}),
        ('tags.json', {'version': 1, 'updated': '2026-01-01', 'tags': {}}),
        ('graph.json', {'version': 1, 'updated': '2026-01-01', 'nodes': [], 'edges': []}),
    ]:
        with open(os.path.join(td, 'schema', name), 'w') as f:
            json.dump(default, f)

    from mneme.core import _apply_workspace_override
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


def _make_simple_page(workspace, client, slug, body='body text'):
    d = os.path.join(workspace, 'wiki', client)
    os.makedirs(d, exist_ok=True)
    fm = (
        '---\n'
        f'title: {slug}\n'
        'type: source-summary\n'
        f'client: {client}\n'
        'tags: []\n'
        '---\n\n'
        f'{body}\n'
    )
    path = os.path.join(d, f'{slug}.md')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(fm)
    return path


class TestSyncPageToIndex:
    def test_returns_bool_when_new(self, sync_workspace):
        from mneme.core import sync_page_to_index
        path = _make_simple_page(sync_workspace, 'demo', 'p1', 'unique_word_xyz body')
        result = sync_page_to_index(path, client_slug='demo')
        assert result is True

    def test_returns_false_when_unchanged(self, sync_workspace):
        from mneme.core import sync_page_to_index
        path = _make_simple_page(sync_workspace, 'demo', 'p1', 'stable body content')
        first = sync_page_to_index(path, client_slug='demo')
        second = sync_page_to_index(path, client_slug='demo')
        assert first is True
        assert second is False

    def test_reindexes_when_content_changes(self, sync_workspace):
        from mneme.core import sync_page_to_index
        path = _make_simple_page(sync_workspace, 'demo', 'p1', 'original body')
        sync_page_to_index(path, client_slug='demo')
        # Modify
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content.replace('original body', 'modified body'))
        result = sync_page_to_index(path, client_slug='demo')
        assert result is True


class TestTagsSuggest:
    """tags_suggest() builds a packet for an LLM agent to read."""

    def test_packet_has_required_fields(self, sync_workspace):
        from mneme.core import tags_suggest
        _make_simple_page(sync_workspace, 'demo', 'p1', 'cardiac monitoring')
        packet = tags_suggest('demo/p1')
        assert 'page' in packet
        assert 'tag_taxonomy' in packet
        assert 'tag_prompt' in packet
        assert packet['page']['wiki_path'] == 'demo/p1.md'
        assert packet['page']['client'] == 'demo'

    def test_includes_current_tags(self, sync_workspace):
        from mneme.core import tags_suggest, sync_page_to_index, tags_apply
        path = _make_simple_page(sync_workspace, 'demo', 'p1', 'body')
        sync_page_to_index(path, client_slug='demo')
        tags_apply('demo/p1', add=['existing-tag'])
        packet = tags_suggest('demo/p1')
        assert 'existing-tag' in packet['page']['current_tags']

    def test_taxonomy_includes_existing_tags(self, sync_workspace):
        from mneme.core import tags_suggest, sync_page_to_index, tags_apply
        # Create two pages with tags so taxonomy is populated
        p1 = _make_simple_page(sync_workspace, 'demo', 'p1', 'b')
        p2 = _make_simple_page(sync_workspace, 'demo', 'p2', 'b')
        sync_page_to_index(p1, client_slug='demo')
        sync_page_to_index(p2, client_slug='demo')
        tags_apply('demo/p1', add=['shared-tag'])
        tags_apply('demo/p2', add=['shared-tag', 'unique-tag'])
        packet = tags_suggest('demo/p1')
        names = {t['name'] for t in packet['tag_taxonomy']}
        assert 'shared-tag' in names
        assert 'unique-tag' in names

    def test_raises_on_missing_page(self, sync_workspace):
        from mneme.core import tags_suggest
        with pytest.raises(FileNotFoundError):
            tags_suggest('demo/no-such-page')

    def test_accepts_slug_with_md_extension(self, sync_workspace):
        from mneme.core import tags_suggest
        _make_simple_page(sync_workspace, 'demo', 'p1', 'body')
        # Both forms should work
        a = tags_suggest('demo/p1')
        b = tags_suggest('demo/p1.md')
        assert a['page']['wiki_path'] == b['page']['wiki_path']


class TestTagsApply:
    """tags_apply() rewrites frontmatter, updates tags.json, re-syncs FTS."""

    def test_add_tags_writes_frontmatter(self, sync_workspace):
        from mneme.core import tags_apply, parse_frontmatter
        path = _make_simple_page(sync_workspace, 'demo', 'p1', 'body')
        result = tags_apply('demo/p1', add=['foo', 'bar'])
        assert 'foo' in result['tags_after']
        assert 'bar' in result['tags_after']
        assert result['added'] == ['foo', 'bar']
        with open(path, 'r') as f:
            fm, _ = parse_frontmatter(f.read())
        assert 'foo' in fm['tags']
        assert 'bar' in fm['tags']

    def test_remove_tag(self, sync_workspace):
        from mneme.core import tags_apply, parse_frontmatter
        path = _make_simple_page(sync_workspace, 'demo', 'p1', 'body')
        tags_apply('demo/p1', add=['foo', 'bar'])
        result = tags_apply('demo/p1', remove=['foo'])
        assert result['removed'] == ['foo']
        assert 'foo' not in result['tags_after']
        assert 'bar' in result['tags_after']
        with open(path, 'r') as f:
            fm, _ = parse_frontmatter(f.read())
        assert 'foo' not in fm['tags']

    def test_dedup_on_add(self, sync_workspace):
        from mneme.core import tags_apply
        _make_simple_page(sync_workspace, 'demo', 'p1', 'body')
        tags_apply('demo/p1', add=['foo'])
        result = tags_apply('demo/p1', add=['foo'])
        # Already there, count should be 1, not 2
        assert result['tags_after'].count('foo') == 1

    def test_updates_tags_json(self, sync_workspace):
        from mneme.core import tags_apply, tags_list
        _make_simple_page(sync_workspace, 'demo', 'p1', 'body')
        tags_apply('demo/p1', add=['foo'])
        tags = tags_list()
        assert 'foo' in tags
        assert tags['foo']['count'] == 1

    def test_remove_drops_from_tags_json(self, sync_workspace):
        from mneme.core import tags_apply, tags_list
        _make_simple_page(sync_workspace, 'demo', 'p1', 'body')
        tags_apply('demo/p1', add=['foo'])
        tags_apply('demo/p1', remove=['foo'])
        tags = tags_list()
        assert 'foo' not in tags

    def test_search_picks_up_new_tag(self, sync_workspace):
        from mneme.core import tags_apply, dual_search
        _make_simple_page(sync_workspace, 'demo', 'p1', 'unrelated body')
        tags_apply('demo/p1', add=['unique-search-tag'])
        results = dual_search('unique-search-tag', k=10)
        assert any(r['wiki_path'] == 'demo/p1.md' for r in results)

    def test_normalizes_tags_to_lowercase(self, sync_workspace):
        from mneme.core import tags_apply
        _make_simple_page(sync_workspace, 'demo', 'p1', 'body')
        result = tags_apply('demo/p1', add=['Foo-BAR'])
        assert 'foo-bar' in result['tags_after']

    def test_raises_on_missing_page(self, sync_workspace):
        from mneme.core import tags_apply
        with pytest.raises(FileNotFoundError):
            tags_apply('demo/no-such-page', add=['x'])


class TestIngestDirPreserveStructure:
    """ingest-dir --preserve-structure mirrors source layout in the wiki."""

    def _make_source_tree(self, workspace, layout):
        """layout: dict of {relpath_from_sources_dir: content}"""
        for rel, content in layout.items():
            full = os.path.join(workspace, 'sources', rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'w') as f:
                f.write(content)

    def test_flat_default_unchanged(self, sync_workspace):
        from mneme.core import ingest_dir
        self._make_source_tree(sync_workspace, {
            'demo/REQUIREMENTS/req-001.md': '# req 1',
            'demo/DESIGN/dds-001.md': '# dds 1',
        })
        ingest_dir(os.path.join(sync_workspace, 'sources', 'demo'),
                   'demo', recursive=True)
        # Without --preserve-structure, both pages flatten
        assert os.path.exists(os.path.join(sync_workspace, 'wiki', 'demo', 'req-001.md'))
        assert os.path.exists(os.path.join(sync_workspace, 'wiki', 'demo', 'dds-001.md'))

    def test_preserve_structure_creates_subdirs(self, sync_workspace):
        from mneme.core import ingest_dir
        self._make_source_tree(sync_workspace, {
            'demo/REQUIREMENTS/req-001.md': '# req 1',
            'demo/DESIGN/dds-001.md': '# dds 1',
        })
        ingest_dir(os.path.join(sync_workspace, 'sources', 'demo'),
                   'demo', recursive=True, preserve_structure=True)
        assert os.path.exists(os.path.join(sync_workspace, 'wiki', 'demo', 'requirements', 'req-001.md'))
        assert os.path.exists(os.path.join(sync_workspace, 'wiki', 'demo', 'design', 'dds-001.md'))

    def test_preserve_avoids_basename_collision(self, sync_workspace):
        from mneme.core import ingest_dir
        self._make_source_tree(sync_workspace, {
            'demo/A/INSTRUCTIONS.md': '# A version',
            'demo/B/INSTRUCTIONS.md': '# B version',
        })
        ingest_dir(os.path.join(sync_workspace, 'sources', 'demo'),
                   'demo', recursive=True, preserve_structure=True)
        # Both should exist under their own subdirectory
        assert os.path.exists(os.path.join(sync_workspace, 'wiki', 'demo', 'a', 'instructions.md'))
        assert os.path.exists(os.path.join(sync_workspace, 'wiki', 'demo', 'b', 'instructions.md'))

    def test_fts5_indexes_nested_pages(self, sync_workspace):
        from mneme.core import ingest_dir, dual_search
        self._make_source_tree(sync_workspace, {
            'demo/REQUIREMENTS/req-001.md': '# Cardiac monitoring requirement',
        })
        ingest_dir(os.path.join(sync_workspace, 'sources', 'demo'),
                   'demo', recursive=True, preserve_structure=True)
        results = dual_search('cardiac', k=5)
        assert any('requirements/req-001' in r['wiki_path'] for r in results)

    def test_auto_detect_subpath(self, sync_workspace):
        from mneme.core import _auto_detect_subpath
        # File under sources/<client>/sub/dir/file.md -> subpath 'sub/dir' (slugified)
        path = os.path.join(sync_workspace, 'sources', 'demo', 'TRACE', 'REQ', 'r1.md')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write('content')
        sub = _auto_detect_subpath(path, 'demo')
        assert sub == os.path.join('trace', 'req')

    def test_resync_picks_up_nested_baseline(self, sync_workspace):
        """A source ingested with --preserve-structure must resync to the nested page."""
        from mneme.core import ingest_dir, resync_source
        self._make_source_tree(sync_workspace, {
            'demo/AREA/a-doc.md': '# Original content',
        })
        ingest_dir(os.path.join(sync_workspace, 'sources', 'demo'),
                   'demo', recursive=True, preserve_structure=True)
        nested_page = os.path.join(sync_workspace, 'wiki', 'demo', 'area', 'a-doc.md')
        flat_page = os.path.join(sync_workspace, 'wiki', 'demo', 'a-doc.md')
        assert os.path.exists(nested_page)
        assert not os.path.exists(flat_page)

        # Modify the source and resync
        src = os.path.join(sync_workspace, 'sources', 'demo', 'AREA', 'a-doc.md')
        with open(src, 'w') as f:
            f.write('# Original content\n\nUpdated paragraph.')
        result = resync_source(src, 'demo')
        # The nested page must have been targeted, not duplicated
        assert os.path.exists(nested_page)
        assert not os.path.exists(flat_page)


class TestGenerateHome:
    """generate_home() writes a navigable landing page."""

    def test_generates_for_client(self, sync_workspace):
        from mneme.core import generate_home
        _make_simple_page(sync_workspace, 'demo', 'p1', 'b1')
        _make_simple_page(sync_workspace, 'demo', 'p2', 'b2')
        result = generate_home(client_slug='demo')
        assert os.path.exists(result['path'])
        assert result['pages_total'] == 2
        assert result['path'].endswith(os.path.join('wiki', 'demo', 'HOME.md'))
        with open(result['path']) as f:
            content = f.read()
        assert '```dataview' in content
        assert 'demo — Home' in content

    def test_detects_id_prefixes(self, sync_workspace):
        from mneme.core import generate_home
        _make_simple_page(sync_workspace, 'demo', 'REQ-001', 'b')
        _make_simple_page(sync_workspace, 'demo', 'REQ-002', 'b')
        _make_simple_page(sync_workspace, 'demo', 'DDS-001', 'b')
        _make_simple_page(sync_workspace, 'demo', 'DDS-002', 'b')
        _make_simple_page(sync_workspace, 'demo', 'one-off', 'b')
        result = generate_home(client_slug='demo')
        assert 'REQ' in result['prefixes_detected']
        assert 'DDS' in result['prefixes_detected']
        with open(result['path']) as f:
            content = f.read()
        assert 'REQ-*' in content
        assert 'DDS-*' in content

    def test_workspace_wide(self, sync_workspace):
        from mneme.core import generate_home
        _make_simple_page(sync_workspace, 'demo', 'p1', 'b')
        result = generate_home(workspace_wide=True)
        assert result['path'] == os.path.join(sync_workspace, 'wiki', 'HOME.md')
        with open(result['path']) as f:
            content = f.read()
        assert 'Workspace — Home' in content

    def test_idempotent(self, sync_workspace):
        from mneme.core import generate_home
        _make_simple_page(sync_workspace, 'demo', 'p1', 'b')
        r1 = generate_home(client_slug='demo')
        r2 = generate_home(client_slug='demo')
        assert r1['path'] == r2['path']
        # Re-run doesn't double page count (HOME.md is excluded from the scan)
        assert r2['pages_total'] == 1

    def test_plain_markdown_fallback_present(self, sync_workspace):
        from mneme.core import generate_home
        _make_simple_page(sync_workspace, 'demo', 'p1', 'b')
        result = generate_home(client_slug='demo')
        with open(result['path']) as f:
            content = f.read()
        # The <details> fallback contains plain markdown links
        assert '<details>' in content
        assert '[[' in content

    def test_missing_args_raises(self, sync_workspace):
        from mneme.core import generate_home
        with pytest.raises(ValueError):
            generate_home()

    def test_detect_id_prefixes_helper(self):
        from mneme.core import _detect_id_prefixes
        result = _detect_id_prefixes(['REQ-001', 'REQ-002', 'DDS-001', 'one-off'])
        assert result == {'REQ': 2}  # DDS has only 1, filtered by min_count


class TestBulkTags:
    """tags_bulk_suggest / tags_bulk_apply."""

    def test_bulk_suggest_only_untagged_by_default(self, sync_workspace):
        from mneme.core import tags_bulk_suggest, tags_apply
        _make_simple_page(sync_workspace, 'demo', 'p1', 'body')
        _make_simple_page(sync_workspace, 'demo', 'p2', 'body')
        tags_apply('demo/p1', add=['already-tagged'])
        packet = tags_bulk_suggest(client='demo')
        paths = {p['wiki_path'] for p in packet['pages']}
        assert 'demo/p2.md' in paths
        assert 'demo/p1.md' not in paths

    def test_bulk_suggest_include_tagged_flag(self, sync_workspace):
        from mneme.core import tags_bulk_suggest, tags_apply
        _make_simple_page(sync_workspace, 'demo', 'p1', 'body')
        tags_apply('demo/p1', add=['already-tagged'])
        packet = tags_bulk_suggest(client='demo', include_tagged=True)
        paths = {p['wiki_path'] for p in packet['pages']}
        assert 'demo/p1.md' in paths

    def test_bulk_suggest_respects_limit(self, sync_workspace):
        from mneme.core import tags_bulk_suggest
        for i in range(10):
            _make_simple_page(sync_workspace, 'demo', f'p{i}', 'body')
        packet = tags_bulk_suggest(client='demo', limit=3)
        assert len(packet['pages']) == 3

    def test_bulk_suggest_filter(self, sync_workspace):
        from mneme.core import tags_bulk_suggest
        _make_simple_page(sync_workspace, 'demo', 'req-001', 'body')
        _make_simple_page(sync_workspace, 'demo', 'dds-001', 'body')
        packet = tags_bulk_suggest(client='demo', filter_substr='req-')
        paths = {p['wiki_path'] for p in packet['pages']}
        assert any('req-001' in p for p in paths)
        assert not any('dds-001' in p for p in paths)

    def test_bulk_suggest_shares_taxonomy_once(self, sync_workspace):
        from mneme.core import tags_bulk_suggest, tags_apply
        _make_simple_page(sync_workspace, 'demo', 'p1', 'body')
        _make_simple_page(sync_workspace, 'demo', 'p2', 'body')
        _make_simple_page(sync_workspace, 'demo', 'p3', 'body')
        tags_apply('demo/p1', add=['shared'])
        # p2, p3 untagged -> in packet
        packet = tags_bulk_suggest(client='demo')
        assert isinstance(packet['tag_taxonomy'], list)
        # Taxonomy is at the packet root, not per-page
        for p in packet['pages']:
            assert 'tag_taxonomy' not in p

    def test_bulk_apply_all_changes(self, sync_workspace):
        from mneme.core import tags_bulk_apply, tags_list
        _make_simple_page(sync_workspace, 'demo', 'p1', 'body')
        _make_simple_page(sync_workspace, 'demo', 'p2', 'body')
        result = tags_bulk_apply({
            'pages': [
                {'wiki_path': 'demo/p1.md', 'add': ['foo', 'bar']},
                {'wiki_path': 'demo/p2.md', 'add': ['baz']},
            ]
        })
        assert result['applied'] == 2
        assert result['total_tags_added'] == 3
        tags = tags_list()
        assert 'foo' in tags and 'bar' in tags and 'baz' in tags

    def test_bulk_apply_continues_on_missing_page(self, sync_workspace):
        from mneme.core import tags_bulk_apply
        _make_simple_page(sync_workspace, 'demo', 'p1', 'body')
        result = tags_bulk_apply({
            'pages': [
                {'wiki_path': 'demo/p1.md', 'add': ['foo']},
                {'wiki_path': 'demo/missing.md', 'add': ['bar']},
            ]
        })
        assert result['applied'] == 1
        assert len(result['failed']) == 1
        assert 'missing' in result['failed'][0]['wiki_path']

    def test_bulk_apply_rejects_bad_shape(self, sync_workspace):
        from mneme.core import tags_bulk_apply
        with pytest.raises(ValueError):
            tags_bulk_apply({'not_pages': []})
        with pytest.raises(ValueError):
            tags_bulk_apply({'pages': 'not a list'})


class TestEntityClassification:
    """entity_suggest / entity_apply / entity_bulk_apply."""

    def _seed_entities(self, workspace, rows):
        entities_path = os.path.join(workspace, 'schema', 'entities.json')
        data = {'version': 1, 'updated': '2026-04-14', 'entities': rows}
        with open(entities_path, 'w') as f:
            json.dump(data, f)

    def test_suggest_default_only_unknown(self, sync_workspace):
        from mneme.core import entity_suggest
        self._seed_entities(sync_workspace, [
            {'id': 'iso-13485', 'name': 'ISO 13485', 'type': 'unknown', 'client': 'demo', 'wiki_page': 'demo/p1', 'tags': []},
            {'id': 'already-classified', 'name': 'Acme Corp', 'type': 'company', 'client': 'demo', 'wiki_page': 'demo/p1', 'tags': []},
        ])
        packet = entity_suggest(client='demo')
        ids = {e['id'] for e in packet['entities']}
        assert 'iso-13485' in ids
        assert 'already-classified' not in ids

    def test_suggest_all_includes_classified(self, sync_workspace):
        from mneme.core import entity_suggest
        self._seed_entities(sync_workspace, [
            {'id': 'iso-13485', 'name': 'ISO 13485', 'type': 'unknown', 'client': 'demo', 'wiki_page': 'demo/p1', 'tags': []},
            {'id': 'acme', 'name': 'Acme', 'type': 'company', 'client': 'demo', 'wiki_page': 'demo/p1', 'tags': []},
        ])
        packet = entity_suggest(client='demo', only_unknown=False)
        ids = {e['id'] for e in packet['entities']}
        assert 'acme' in ids

    def test_suggest_respects_limit(self, sync_workspace):
        from mneme.core import entity_suggest
        self._seed_entities(sync_workspace, [
            {'id': f'e-{i}', 'name': f'Entity {i}', 'type': 'unknown', 'client': 'demo', 'wiki_page': 'demo/p1', 'tags': []}
            for i in range(10)
        ])
        packet = entity_suggest(client='demo', limit=3)
        assert len(packet['entities']) == 3

    def test_suggest_packet_fields(self, sync_workspace):
        from mneme.core import entity_suggest
        self._seed_entities(sync_workspace, [
            {'id': 'foo', 'name': 'Foo', 'type': 'unknown', 'client': 'demo', 'wiki_page': 'demo/p1', 'tags': []},
        ])
        packet = entity_suggest(client='demo')
        assert 'entities' in packet
        assert 'existing_types' in packet
        assert 'valid_types' in packet
        assert 'entity_prompt' in packet
        assert 'standard' in packet['valid_types']

    def test_apply_updates_type(self, sync_workspace):
        from mneme.core import entity_apply
        self._seed_entities(sync_workspace, [
            {'id': 'iso-13485', 'name': 'ISO 13485', 'type': 'unknown', 'client': 'demo', 'wiki_page': 'demo/p1', 'tags': []},
        ])
        result = entity_apply('iso-13485', 'standard')
        assert result['old_type'] == 'unknown'
        assert result['new_type'] == 'standard'
        # Verify persisted
        with open(os.path.join(sync_workspace, 'schema', 'entities.json')) as f:
            data = json.load(f)
        e = next(x for x in data['entities'] if x['id'] == 'iso-13485')
        assert e['type'] == 'standard'

    def test_apply_rejects_invalid_type(self, sync_workspace):
        from mneme.core import entity_apply
        self._seed_entities(sync_workspace, [
            {'id': 'foo', 'name': 'Foo', 'type': 'unknown', 'client': 'demo', 'wiki_page': 'demo/p1', 'tags': []},
        ])
        with pytest.raises(ValueError):
            entity_apply('foo', 'nonsense')

    def test_apply_raises_on_missing_id(self, sync_workspace):
        from mneme.core import entity_apply
        self._seed_entities(sync_workspace, [])
        with pytest.raises(KeyError):
            entity_apply('nonexistent', 'company')

    def test_bulk_apply_partial_failure(self, sync_workspace):
        from mneme.core import entity_bulk_apply
        self._seed_entities(sync_workspace, [
            {'id': 'a', 'name': 'A', 'type': 'unknown', 'client': 'demo', 'wiki_page': 'demo/p1', 'tags': []},
            {'id': 'b', 'name': 'B', 'type': 'unknown', 'client': 'demo', 'wiki_page': 'demo/p1', 'tags': []},
        ])
        result = entity_bulk_apply([
            {'id': 'a', 'type': 'standard'},
            {'id': 'does-not-exist', 'type': 'company'},
            {'id': 'b', 'type': 'technology'},
        ])
        assert result['applied'] == 2
        assert len(result['errors']) == 1
        assert 'a' in result['updated_ids']
        assert 'b' in result['updated_ids']


class TestProgressBar:
    """_ProgressBar behaves correctly in TTY and non-TTY modes."""

    def test_non_tty_emits_periodic_lines(self):
        import io
        from mneme.core import _ProgressBar
        buf = io.StringIO()
        bar = _ProgressBar(10, stream=buf, enabled=False)
        for _ in range(10):
            bar.update(1, current='x')
        bar.finish()
        out = buf.getvalue()
        # No carriage returns in non-TTY mode
        assert '\r' not in out
        # At least the final count should appear
        assert '[10/10]' in out

    def test_fmt_eta(self):
        from mneme.core import _ProgressBar
        assert _ProgressBar._fmt_eta(125) == '02:05'
        assert _ProgressBar._fmt_eta(0) == '00:00'
        assert _ProgressBar._fmt_eta(float('inf')) == '--:--'

    def test_zero_total_does_not_crash(self):
        import io
        from mneme.core import _ProgressBar
        bar = _ProgressBar(0, stream=io.StringIO(), enabled=False)
        bar.update(0)
        bar.finish()

    def test_tty_mode_uses_carriage_return(self):
        import io
        from mneme.core import _ProgressBar
        buf = io.StringIO()
        bar = _ProgressBar(5, stream=buf, enabled=True)
        for _ in range(5):
            bar.update(1, current='a')
        bar.finish()
        assert '\r' in buf.getvalue()


# ---------------------------------------------------------------------------
# Category 5: CLI Integration Tests
# ---------------------------------------------------------------------------

BRIDGE_DIR = os.path.join(os.path.dirname(__file__), '..')


def _run_mnemo(*args):
    """Run the mneme CLI via `python -m mneme` from the project root."""
    cmd = [sys.executable, '-m', 'mneme'] + list(args)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=BRIDGE_DIR,
        timeout=60,
    )
    return result.returncode, result.stdout, result.stderr


class TestCLI:
    def test_help(self):
        rc, out, err = _run_mnemo('--help')
        assert rc == 0
        assert 'stats' in out
        assert 'search' in out
        assert 'ingest' in out
        assert 'sync' in out
        assert 'drift' in out

    def test_stats_exits_zero(self):
        rc, out, err = _run_mnemo('stats')
        assert rc == 0

    def test_stats_output_contains_wiki_section(self):
        rc, out, err = _run_mnemo('stats')
        assert rc == 0
        assert 'WIKI' in out

    def test_stats_output_contains_schema_section(self):
        rc, out, err = _run_mnemo('stats')
        assert rc == 0
        assert 'SCHEMA' in out

    def test_search_empty_query_fails_with_rc1(self):
        rc, out, err = _run_mnemo('search', '')
        assert rc == 1

    def test_search_empty_query_error_message(self):
        rc, out, err = _run_mnemo('search', '')
        assert 'empty' in err.lower() or 'error' in err.lower()

    def test_search_no_results_for_gibberish(self):
        rc, out, err = _run_mnemo('search', 'xyznonexistent12345qqqzzz')
        assert rc == 0
        assert 'No results found' in out

    def test_search_exits_zero_for_valid_query(self):
        rc, out, err = _run_mnemo('search', 'xyznonexistent12345qqqzzz')
        assert rc == 0

    def test_ingest_missing_file_exits_nonzero(self):
        rc, out, err = _run_mnemo('ingest', '/tmp/nonexistent_file_xyz_mnemo.md', 'test')
        assert rc == 1

    def test_ingest_missing_file_error_message(self):
        rc, out, err = _run_mnemo('ingest', '/tmp/nonexistent_file_xyz_mnemo.md', 'test')
        assert 'not found' in err.lower() or 'error' in err.lower()

    def test_ingest_bad_slug_exits_nonzero(self):
        rc, out, err = _run_mnemo('ingest', '/dev/null', 'BAD SLUG!')
        assert rc == 1

    def test_ingest_bad_slug_error_message(self):
        rc, out, err = _run_mnemo('ingest', '/dev/null', 'BAD SLUG!')
        assert 'invalid client slug' in err.lower() or 'invalid' in err.lower()

    def test_ingest_path_traversal_rejected(self):
        rc, out, err = _run_mnemo('ingest', '/dev/null', '../../../etc')
        assert rc == 1

    def test_ingest_uppercase_slug_rejected(self):
        rc, out, err = _run_mnemo('ingest', '/dev/null', 'MyClient')
        assert rc == 1

    def test_ingest_slug_with_spaces_rejected(self):
        rc, out, err = _run_mnemo('ingest', '/dev/null', 'my client')
        assert rc == 1

    def test_ingest_valid_slug_format_accepted(self):
        rc, out, err = _run_mnemo('ingest', '/tmp/does-not-exist.md', 'my-client-123')
        assert rc == 1
        assert 'not found' in err.lower() or 'source not found' in err.lower()

    def test_drift_exits_zero(self):
        rc, out, err = _run_mnemo('drift')
        assert rc == 0

    def test_drift_output_contains_report(self):
        rc, out, err = _run_mnemo('drift')
        assert 'Drift report' in out or 'drift' in out.lower()

    def test_sync_exits_zero(self):
        rc, out, err = _run_mnemo('sync')
        assert rc == 0

    def test_sync_output_contains_complete(self):
        rc, out, err = _run_mnemo('sync')
        assert 'Sync complete' in out or 'sync' in out.lower()

    def test_repair_exits_zero(self):
        rc, out, err = _run_mnemo('repair')
        assert rc == 0

    def test_repair_output_has_status(self):
        rc, out, err = _run_mnemo('repair')
        assert 'repair' in out.lower() or rc == 0


# ---------------------------------------------------------------------------
# Category 6: Ingest Integration Tests (with cleanup)
# ---------------------------------------------------------------------------

_KE_BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
_TEST_CLIENT = 'pytest-int-test'
_TEST_SOURCE_SLUG = 'pytest-mneme-ingest-test-fixture'
_TEST_FILE = f'/tmp/{_TEST_SOURCE_SLUG}.md'


def _cleanup_test_client():
    """Remove all test artifacts for the integration test client."""
    wiki_dir = os.path.join(_KE_BASE, 'wiki', _TEST_CLIENT)
    if os.path.exists(wiki_dir):
        shutil.rmtree(wiki_dir)

    # Remove search.db if tests created one in the project root
    search_db = os.path.join(_KE_BASE, 'search.db')
    for suffix in ('', '-wal', '-shm'):
        p = search_db + suffix
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass

    ent_file = os.path.join(_KE_BASE, 'schema', 'entities.json')
    if os.path.exists(ent_file):
        try:
            with open(ent_file) as f:
                data = json.load(f)
            data['entities'] = [
                e for e in data['entities']
                if e.get('client') != _TEST_CLIENT
            ]
            with open(ent_file, 'w') as f:
                json.dump(data, f, indent=2)
        except (json.JSONDecodeError, OSError):
            pass

    tags_file = os.path.join(_KE_BASE, 'schema', 'tags.json')
    if os.path.exists(tags_file):
        try:
            with open(tags_file) as f:
                data = json.load(f)
            tags = data.get('tags', {})
            cleaned = {}
            for tag, info in tags.items():
                pages = [p for p in info.get('pages', [])
                         if _TEST_CLIENT not in p]
                if pages:
                    info['pages'] = pages
                    info['count'] = len(pages)
                    cleaned[tag] = info
            data['tags'] = cleaned
            with open(tags_file, 'w') as f:
                json.dump(data, f, indent=2)
        except (json.JSONDecodeError, OSError):
            pass

    index_file = os.path.join(_KE_BASE, 'index.md')
    if os.path.exists(index_file):
        try:
            with open(index_file, 'r') as f:
                lines = f.readlines()
            cleaned_lines = []
            skip_section = False
            for line in lines:
                stripped = line.strip()
                if stripped == f'## {_TEST_CLIENT}':
                    skip_section = True
                    continue
                if skip_section:
                    if stripped.startswith('## ') or stripped.startswith('---'):
                        skip_section = False
                    else:
                        if not stripped:
                            continue
                        if stripped.startswith('|') and _TEST_CLIENT in stripped:
                            continue
                if f'[[{_TEST_CLIENT}/' in line:
                    continue
                cleaned_lines.append(line)
            with open(index_file, 'w') as f:
                f.writelines(cleaned_lines)
        except OSError:
            pass

    log_file = os.path.join(_KE_BASE, 'log.md')
    if os.path.exists(log_file):
        try:
            with open(log_file, 'r') as f:
                lines = f.readlines()
            cleaned_lines = []
            skip_block = False
            for line in lines:
                if line.startswith('## ['):
                    skip_block = _TEST_SOURCE_SLUG in line
                if not skip_block:
                    cleaned_lines.append(line)
            with open(log_file, 'w') as f:
                f.writelines(cleaned_lines)
        except OSError:
            pass

    for fname in ('index.md', 'log.md'):
        fp = os.path.join(_KE_BASE, fname)
        if not os.path.exists(fp):
            continue
        try:
            with open(fp, 'r') as f:
                content = f.read().strip()
            header_only_signatures = (
                '# mneme Index',
                '# mneme Log',
                '# Index',
                '# Log',
            )
            stripped_lines = [l for l in content.split('\n') if l.strip()]
            if (len(stripped_lines) <= 2
                    and (not stripped_lines or stripped_lines[0] in header_only_signatures
                         or stripped_lines[0].startswith('Last updated'))):
                os.remove(fp)
        except OSError:
            pass

    schema_dir = os.path.join(_KE_BASE, 'schema')
    if os.path.isdir(schema_dir):
        try:
            entities_empty = True
            tags_empty = True
            ef = os.path.join(schema_dir, 'entities.json')
            tf = os.path.join(schema_dir, 'tags.json')
            if os.path.exists(ef):
                with open(ef) as f:
                    entities_empty = not json.load(f).get('entities')
            if os.path.exists(tf):
                with open(tf) as f:
                    tags_empty = not json.load(f).get('tags')
            if entities_empty and tags_empty:
                shutil.rmtree(schema_dir, ignore_errors=True)
        except (json.JSONDecodeError, OSError):
            pass

    wd = os.path.join(_KE_BASE, 'wiki')
    if os.path.isdir(wd):
        try:
            if not os.listdir(wd):
                os.rmdir(wd)
        except OSError:
            pass


class TestIngestIntegration:
    @classmethod
    def setup_class(cls):
        _cleanup_test_client()
        with open(_TEST_FILE, 'w') as f:
            f.write(
                "# Pytest Test Document\n\n"
                "This document tests the full ingest pipeline.\n\n"
                "## Facts\n"
                "- Company: PytestCorp\n"
                "- Revenue: $1M\n"
            )

    @classmethod
    def teardown_class(cls):
        if os.path.exists(_TEST_FILE):
            os.remove(_TEST_FILE)
        _cleanup_test_client()

    def test_ingest_creates_wiki_page(self):
        _run_mnemo('ingest', _TEST_FILE, _TEST_CLIENT)
        wiki_page = os.path.join(
            _KE_BASE, 'wiki', _TEST_CLIENT, f'{_TEST_SOURCE_SLUG}.md'
        )
        assert os.path.exists(wiki_page), (
            f"Wiki page was not created at {wiki_page}"
        )

    def test_ingest_wiki_page_has_valid_frontmatter(self):
        wiki_page = os.path.join(
            _KE_BASE, 'wiki', _TEST_CLIENT, f'{_TEST_SOURCE_SLUG}.md'
        )
        if not os.path.exists(wiki_page):
            _run_mnemo('ingest', _TEST_FILE, _TEST_CLIENT)

        with open(wiki_page, 'r') as f:
            content = f.read()
        fm, body = parse_frontmatter(content)
        assert fm.get('client') == _TEST_CLIENT
        assert fm.get('type') == 'source-summary'
        assert 'created' in fm
        assert 'updated' in fm

    def test_duplicate_ingest_warns(self):
        wiki_page = os.path.join(
            _KE_BASE, 'wiki', _TEST_CLIENT, f'{_TEST_SOURCE_SLUG}.md'
        )
        if not os.path.exists(wiki_page):
            _run_mnemo('ingest', _TEST_FILE, _TEST_CLIENT)

        rc, out, err = _run_mnemo('ingest', _TEST_FILE, _TEST_CLIENT)
        assert rc == 0
        assert 'previously ingested' in out.lower() or 'skipping' in out.lower()

    def test_force_reingest_prints_reingest_message(self):
        rc, out, err = _run_mnemo('ingest', _TEST_FILE, _TEST_CLIENT, '--force')
        combined = out + err
        assert 're-ingesting' in combined.lower() or 'force' in combined.lower()

    def test_ingest_appends_to_log(self):
        log_file = os.path.join(_KE_BASE, 'log.md')
        assert os.path.exists(log_file)
        with open(log_file, 'r') as f:
            log_content = f.read()
        assert f'{_TEST_SOURCE_SLUG}.md' in log_content

    def test_ingest_updates_index(self):
        index_file = os.path.join(_KE_BASE, 'index.md')
        assert os.path.exists(index_file)
        with open(index_file, 'r') as f:
            index_content = f.read()
        assert len(index_content) > 0
        assert 'mneme' in index_content


# ---------------------------------------------------------------------------
# Category 7: Resync (3-way merge)
# ---------------------------------------------------------------------------

from mneme.core import (
    _baseline_path,
    _write_baseline,
    _read_baseline,
    _git_merge_file,
)

_GIT_AVAILABLE = shutil.which('git') is not None
_skip_no_git = pytest.mark.skipif(not _GIT_AVAILABLE, reason='git required')


class TestResyncUnits:
    def test_baseline_path_computation(self):
        p = os.path.join('wiki', 'acme', 'overview.md')
        bp = _baseline_path(p)
        assert bp == os.path.join('wiki', 'acme', '.baselines', 'overview.md')

    def test_write_read_baseline_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            wiki_page = os.path.join(td, 'wiki', 'c1', 'page.md')
            os.makedirs(os.path.dirname(wiki_page), exist_ok=True)
            _write_baseline(wiki_page, 'hello baseline\n')
            assert os.path.isdir(os.path.join(td, 'wiki', 'c1', '.baselines'))
            assert _read_baseline(wiki_page) == 'hello baseline\n'

    def test_read_baseline_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            wiki_page = os.path.join(td, 'wiki', 'c1', 'nope.md')
            assert _read_baseline(wiki_page) is None

    @_skip_no_git
    def test_merge_clean_ours_unchanged(self):
        ancestor = "line1\nline2\nline3\n"
        ours = ancestor
        theirs = "line1\nlineTWO\nline3\n"
        merged, conflicts = _git_merge_file(ours, ancestor, theirs)
        assert conflicts is False
        assert merged == theirs

    @_skip_no_git
    def test_merge_clean_non_overlapping_edits(self):
        ancestor = "A\nB\nC\nD\nE\nF\nG\nH\nI\nJ\n"
        ours = "A\nB\nC\nD\nE\nF\nG\nH\nI\nJ\nOURS-ADDED\n"
        theirs = "THEIRS-ADDED\nA\nB\nC\nD\nE\nF\nG\nH\nI\nJ\n"
        merged, conflicts = _git_merge_file(ours, ancestor, theirs)
        assert conflicts is False
        assert 'OURS-ADDED' in merged
        assert 'THEIRS-ADDED' in merged

    @_skip_no_git
    def test_merge_conflict_same_line(self):
        ancestor = "line1\nORIGINAL\nline3\n"
        ours = "line1\nOURS-EDIT\nline3\n"
        theirs = "line1\nTHEIRS-EDIT\nline3\n"
        merged, conflicts = _git_merge_file(ours, ancestor, theirs)
        assert conflicts is True
        assert '<<<<<<<' in merged
        assert '>>>>>>>' in merged

    def test_git_merge_file_missing_git(self, monkeypatch):
        monkeypatch.setenv('PATH', '')
        with pytest.raises((FileNotFoundError, RuntimeError)):
            _git_merge_file('a', 'a', 'a')


# ---------------------------------------------------------------------------
# Category 8: Resync integration (temp workspace)
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_workspace(monkeypatch):
    """Build a clean temp workspace and rebind mneme path constants at it."""
    td = tempfile.mkdtemp(prefix='mneme-resync-test-')
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

    from mneme.core import _apply_workspace_override
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


@_skip_no_git
class TestResyncIntegration:
    def _write_source(self, workspace, client, name, body):
        src_dir = os.path.join(workspace, 'sources', client)
        os.makedirs(src_dir, exist_ok=True)
        path = os.path.join(src_dir, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(body)
        return path

    def test_baseline_created_by_ingest(self, temp_workspace):
        from mneme.core import ingest_source_to_both, _baseline_path
        client = 'c-baseline'
        src = self._write_source(
            temp_workspace, client, 'doc.md',
            '# Doc\n\nSome content that is long enough to ingest.\n'
        )
        result = ingest_source_to_both(src, client, force=True)
        wiki_page = os.path.join(temp_workspace, result['wiki_page'])
        assert os.path.exists(wiki_page)
        bp = _baseline_path(wiki_page)
        assert os.path.exists(bp)
        with open(wiki_page) as f:
            wp = f.read()
        with open(bp) as f:
            bc = f.read()
        assert wp == bc

    def test_fresh_resync_no_baseline(self, temp_workspace):
        from mneme.core import resync_source, _baseline_path
        client = 'c-fresh'
        src = self._write_source(
            temp_workspace, client, 'brand-new.md',
            '# Brand New\n\nFresh content body for ingest.\n'
        )
        result = resync_source(src, client)
        assert result['action'].startswith('fresh-')
        wiki_page = os.path.join(temp_workspace, 'wiki', client, 'brand-new.md')
        assert os.path.exists(wiki_page)
        assert os.path.exists(_baseline_path(wiki_page))

    def test_clean_merge_preserves_human_edits(self, temp_workspace):
        from mneme.core import ingest_source_to_both, resync_source, _read_baseline
        client = 'c-clean'
        src = self._write_source(
            temp_workspace, client, 'doc.md',
            '# Doc\n\n- item one\n- item two\n'
        )
        r = ingest_source_to_both(src, client, force=True)
        wiki_page = os.path.join(temp_workspace, r['wiki_page'])
        with open(wiki_page, 'r', encoding='utf-8') as f:
            original = f.read()
        human_edited = original + '\n## Open Questions\n\n- Human added question\n'
        with open(wiki_page, 'w', encoding='utf-8') as f:
            f.write(human_edited)

        with open(src, 'w', encoding='utf-8') as f:
            f.write('# Doc\n\n- item one\n- item two\n- item three NEW\n')

        result = resync_source(src, client)
        assert result['conflicts'] is False
        assert result['action'] == 'merge-clean'
        with open(wiki_page, 'r', encoding='utf-8') as f:
            merged = f.read()
        assert 'Human added question' in merged
        assert 'item three NEW' in merged
        baseline = _read_baseline(wiki_page)
        assert 'Human added question' not in baseline
        assert 'item three NEW' in baseline

    def test_conflict_path(self, temp_workspace):
        from mneme.core import ingest_source_to_both, resync_source
        client = 'c-conflict'
        src = self._write_source(
            temp_workspace, client, 'doc.md',
            '# Doc\n\nRevenue: $1M\n\nNotes here filler content.\n'
        )
        r = ingest_source_to_both(src, client, force=True)
        wiki_page = os.path.join(temp_workspace, r['wiki_page'])
        with open(wiki_page, 'r', encoding='utf-8') as f:
            content = f.read()
        content = content.replace('Revenue: $1M', 'Revenue: $2M HUMAN')
        with open(wiki_page, 'w', encoding='utf-8') as f:
            f.write(content)
        with open(src, 'w', encoding='utf-8') as f:
            f.write('# Doc\n\nRevenue: $5M INCOMING\n\nNotes here filler content.\n')

        result = resync_source(src, client)
        assert result['conflicts'] is True
        assert result['action'] == 'merge-conflict'
        with open(wiki_page, 'r', encoding='utf-8') as f:
            merged = f.read()
        assert '<<<<<<<' in merged
        with open(os.path.join(temp_workspace, 'log.md')) as f:
            log_content = f.read()
        assert 'RESYNC-CONFLICT' in log_content

    def test_noop_fast_path(self, temp_workspace):
        from mneme.core import ingest_source_to_both, resync_source
        client = 'c-noop'
        src = self._write_source(
            temp_workspace, client, 'doc.md',
            '# Doc\n\nStable body content that will not change.\n'
        )
        ingest_source_to_both(src, client, force=True)
        result = resync_source(src, client)
        assert result['action'] == 'noop'
        assert result['conflicts'] is False

    def test_dry_run_no_writes(self, temp_workspace):
        from mneme.core import ingest_source_to_both, resync_source
        client = 'c-dry'
        src = self._write_source(
            temp_workspace, client, 'doc.md',
            '# Doc\n\n- item one\n- item two\n'
        )
        r = ingest_source_to_both(src, client, force=True)
        wiki_page = os.path.join(temp_workspace, r['wiki_page'])
        with open(wiki_page, 'r', encoding='utf-8') as f:
            before = f.read()
        with open(src, 'w', encoding='utf-8') as f:
            f.write('# Doc\n\n- item one\n- item two\n- item three\n')
        result = resync_source(src, client, dry_run=True)
        assert result['action'] in ('would-merge-clean', 'would-merge-conflict')
        assert 'merged_hash' in result
        with open(wiki_page, 'r', encoding='utf-8') as f:
            after = f.read()
        assert before == after


@_skip_no_git
class TestResyncResolve:
    def _seed_conflict(self, workspace, client='c-res'):
        from mneme.core import ingest_source_to_both, resync_source
        src_dir = os.path.join(workspace, 'sources', client)
        os.makedirs(src_dir, exist_ok=True)
        src = os.path.join(src_dir, 'doc.md')
        with open(src, 'w', encoding='utf-8') as f:
            f.write('# Doc\n\nRevenue: $1M\n\nLong enough filler content here to ingest.\n')
        r = ingest_source_to_both(src, client, force=True)
        wiki_page = os.path.join(workspace, r['wiki_page'])
        with open(wiki_page, 'r', encoding='utf-8') as f:
            c = f.read().replace('Revenue: $1M', 'Revenue: $2M HUMAN')
        with open(wiki_page, 'w', encoding='utf-8') as f:
            f.write(c)
        with open(src, 'w', encoding='utf-8') as f:
            f.write('# Doc\n\nRevenue: $5M INCOMING\n\nLong enough filler content here to ingest.\n')
        resync_source(src, client)
        return wiki_page, client

    def test_resolve_rejects_unresolved_markers(self, temp_workspace):
        from mneme.core import resync_resolve
        wiki_page, client = self._seed_conflict(temp_workspace)
        with pytest.raises(ValueError):
            resync_resolve(f'{client}/doc')

    def test_resolve_happy_path(self, temp_workspace):
        from mneme.core import resync_resolve, _read_baseline
        wiki_page, client = self._seed_conflict(temp_workspace)
        cleaned = (
            '---\ntitle: Doc\ntype: source-summary\nclient: ' + client + '\n---\n\n'
            '## Summary\n\nResolved content here that is long enough.\n\n'
            'Revenue: $5M RESOLVED\n'
        )
        with open(wiki_page, 'w', encoding='utf-8') as f:
            f.write(cleaned)
        result = resync_resolve(f'{client}/doc')
        assert result['action'] == 'resolved'
        assert _read_baseline(wiki_page) == cleaned
        with open(os.path.join(temp_workspace, 'log.md')) as f:
            assert 'RESYNC-RESOLVED' in f.read()


# ---------------------------------------------------------------------------
# Category 9: Resync CLI smoke test
# ---------------------------------------------------------------------------

class TestResyncCLI:
    def test_resync_help(self):
        rc, out, err = _run_mnemo('resync', '--help')
        assert rc == 0

    @_skip_no_git
    def test_resync_end_to_end(self):
        td = tempfile.mkdtemp(prefix='mneme-resync-cli-')
        try:
            for sub in ('wiki', 'sources', 'schema'):
                os.makedirs(os.path.join(td, sub), exist_ok=True)
            with open(os.path.join(td, 'index.md'), 'w') as f:
                f.write('# Index\n')
            with open(os.path.join(td, 'log.md'), 'w') as f:
                f.write('# Log\n')
            for name, empty in (('entities.json', {'version': 1, 'updated': '2026-01-01', 'entities': []}),
                                ('tags.json', {'version': 1, 'updated': '2026-01-01', 'tags': {}}),
                                ('graph.json', {'version': 1, 'updated': '2026-01-01', 'nodes': [], 'edges': []})):
                with open(os.path.join(td, 'schema', name), 'w') as f:
                    json.dump(empty, f)

            client = 'cli-client'
            src_dir = os.path.join(td, 'sources', client)
            os.makedirs(src_dir, exist_ok=True)
            src = os.path.join(src_dir, 'doc.md')
            with open(src, 'w', encoding='utf-8') as f:
                f.write('# Doc\n\n- alpha\n- beta\n\nLong enough filler body content.\n')

            rc, out, err = _run_mnemo('--workspace', td, 'ingest', src, client, '--force')
            wiki_page = os.path.join(td, 'wiki', client, 'doc.md')
            assert os.path.exists(wiki_page), f'wiki page missing. stderr={err}'

            with open(src, 'w', encoding='utf-8') as f:
                f.write('# Doc\n\n- alpha\n- beta\n- gamma\n\nLong enough filler body content.\n')
            rc, out, err = _run_mnemo('--workspace', td, 'resync', src, client)
            assert rc == 0, f'resync failed: {err}'
            combined = out + err
            assert ('merge-clean' in combined or 'Action' in combined
                    or 'action' in combined.lower() or 'fresh' in combined.lower())
        finally:
            shutil.rmtree(td, ignore_errors=True)
