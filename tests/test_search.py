"""Tests for mneme.search FTS5 backend."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mneme import search


@pytest.fixture
def conn():
    c = search.open_db(':memory:')
    yield c
    c.close()


def test_open_db_creates_schema(conn):
    # Verify pages and pages_fts tables exist
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    ).fetchall()
    names = {r['name'] for r in rows}
    assert 'pages' in names


def test_upsert_and_search(conn):
    indexed = search.upsert_page(
        conn, 'client/page1.md', 'client', 'My Title',
        'tag1, tag2', 'The body text contains keywords', 'hash1',
    )
    assert indexed is True
    results = search.search(conn, 'keywords', k=10)
    assert len(results) == 1
    assert results[0]['title'] == 'My Title'
    assert results[0]['wiki_path'] == 'client/page1.md'
    assert results[0]['layer'] == 'fts5'


def test_upsert_idempotent(conn):
    search.upsert_page(conn, 'p.md', 'c', 't', '', 'body', 'hash1')
    indexed = search.upsert_page(conn, 'p.md', 'c', 't', '', 'body', 'hash1')
    assert indexed is False  # same hash, skipped


def test_upsert_changes(conn):
    search.upsert_page(conn, 'p.md', 'c', 't', '', 'body1', 'hash1')
    indexed = search.upsert_page(conn, 'p.md', 'c', 't', '', 'body2', 'hash2')
    assert indexed is True


def test_stemming(conn):
    search.upsert_page(conn, 'p.md', 'c', 'Title', '', 'They were running fast', 'h')
    results = search.search(conn, 'run', k=10)
    assert len(results) == 1


def test_client_filter(conn):
    search.upsert_page(conn, 'a.md', 'client_a', 'A', '', 'shared keyword', 'h1')
    search.upsert_page(conn, 'b.md', 'client_b', 'B', '', 'shared keyword', 'h2')
    results = search.search(conn, 'keyword', k=10, client='client_a')
    assert len(results) == 1
    assert results[0]['client'] == 'client_a'


def test_delete_page(conn):
    search.upsert_page(conn, 'p.md', 'c', 't', '', 'unique_word', 'h')
    assert len(search.search(conn, 'unique_word', k=10)) == 1
    search.delete_page(conn, 'p.md')
    assert len(search.search(conn, 'unique_word', k=10)) == 0


def test_get_indexed_pages(conn):
    search.upsert_page(conn, 'a.md', 'c', 't', '', 'b', 'hash_a')
    search.upsert_page(conn, 'b.md', 'c', 't', '', 'b', 'hash_b')
    pages = search.get_indexed_pages(conn)
    assert pages == {'a.md': 'hash_a', 'b.md': 'hash_b'}


def test_sanitize_empty_query():
    assert search._sanitize_fts_query('') == '""'
    assert search._sanitize_fts_query('   ') == '""'


def test_sanitize_special_chars():
    # Should not raise
    sanitized = search._sanitize_fts_query('foo (bar) "baz"')
    assert sanitized  # non-empty
    # Should be runnable
    conn = search.open_db(':memory:')
    try:
        search.upsert_page(conn, 'p.md', 'c', 't', '', 'foo bar baz body', 'h')
        # Just verify search doesn't crash
        search.search(conn, 'foo (bar) "baz"', k=10)
    finally:
        conn.close()


def test_bm25_title_outranks_body(conn):
    # Title match should rank higher than body match
    search.upsert_page(conn, 'a.md', 'c', 'Body Title', '', 'unrelated content', 'h1')
    search.upsert_page(conn, 'b.md', 'c', 'Other Title', '', 'body keyword body', 'h2')
    results = search.search(conn, 'body', k=10)
    # Both match - but the one with 'Body' in title should rank higher
    assert len(results) == 2
    assert results[0]['title'] == 'Body Title'


def test_get_stats(conn):
    search.upsert_page(conn, 'a.md', 'c', 't', '', 'b', 'h1')
    search.upsert_page(conn, 'b.md', 'c', 't', '', 'b', 'h2')
    stats = search.get_stats(conn)
    assert stats['page_count'] == 2


def test_rebuild_index(tmp_path):
    # Create some wiki pages
    wiki_dir = tmp_path / 'wiki'
    client_dir = wiki_dir / 'client_a'
    client_dir.mkdir(parents=True)
    (client_dir / 'page1.md').write_text(
        "---\ntitle: Page One\nclient: client_a\ntags: [foo]\n---\n\nBody one keyword"
    )
    (client_dir / 'page2.md').write_text(
        "---\ntitle: Page Two\nclient: client_a\n---\n\nBody two"
    )

    db_path = str(tmp_path / 'search.db')
    conn = search.open_db(db_path)
    try:
        result = search.rebuild_index(
            conn, str(wiki_dir), str(tmp_path),
            excluded_dirs=['_templates'], excluded_files=[],
        )
        assert result['pages_indexed'] == 2
        assert result['errors'] == 0
        results = search.search(conn, 'keyword', k=10)
        assert len(results) == 1
        assert results[0]['title'] == 'Page One'
    finally:
        conn.close()
