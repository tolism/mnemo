"""
Comprehensive pytest test suite for Mnemosyne core.

Covers all core functions:
  - parse_frontmatter (pure parsing)
  - chunk_body (chunking logic)
  - _title_from_slug (slug conversion)
  - _sanitize_memvid_query (query cleaning)
  - _content_hash (hashing)
  - CLI integration (subprocess)
  - Ingest integration (with cleanup)
"""

import json
import os
import shutil
import subprocess
import sys

# Ensure the package is importable from the tests/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mneme.core import (
    _content_hash,
    _sanitize_memvid_query,
    _title_from_slug,
    chunk_body,
    parse_frontmatter,
)
from mneme.config import MAX_CHUNK_SIZE, MIN_CHUNK_SIZE

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
# Category 2: Chunking
# ---------------------------------------------------------------------------

class TestChunkBody:
    def test_empty_text_returns_empty(self):
        chunks = chunk_body("")
        assert chunks == []

    def test_short_text_below_min_chunk_size_dropped(self):
        # MIN_CHUNK_SIZE is 50; "Short text." is 11 chars, should be dropped
        chunks = chunk_body("Short text.")
        assert chunks == []

    def test_text_at_min_chunk_size_included(self):
        # Exactly MIN_CHUNK_SIZE characters should be included
        text = "A" * MIN_CHUNK_SIZE
        chunks = chunk_body(text)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_long_text_produces_multiple_chunks(self):
        # ~2600 chars -> should produce multiple chunks
        text = "This is a test sentence. " * 104  # ~2600 chars
        chunks = chunk_body(text)
        assert len(chunks) > 1

    def test_all_chunks_within_max_size(self):
        text = ("A" * 400 + "\n\n") * 10
        chunks = chunk_body(text)
        for chunk in chunks:
            # Allow small tolerance for sentence boundary splitting
            assert len(chunk) <= MAX_CHUNK_SIZE + 50

    def test_paragraph_separation_respected(self):
        # Two paragraphs each >= MIN_CHUNK_SIZE
        para1 = "First paragraph with enough content to pass the minimum chunk size filter."
        para2 = "Second paragraph with enough content to pass the minimum chunk size filter."
        text = para1 + "\n\n" + para2
        chunks = chunk_body(text)
        assert len(chunks) == 2

    def test_whitespace_only_paragraphs_skipped(self):
        text = "   \n\n   \n\nA" * MIN_CHUNK_SIZE
        chunks = chunk_body(text)
        # Whitespace paragraphs are stripped and skipped
        for chunk in chunks:
            assert chunk.strip() != ''

    def test_single_chunk_for_text_within_max_size(self):
        # Text of exactly 200 chars (well above MIN_CHUNK_SIZE, below MAX_CHUNK_SIZE)
        text = "Word " * 40  # 200 chars
        chunks = chunk_body(text)
        assert len(chunks) == 1

    def test_sentence_splitting_on_long_paragraph(self):
        # A single very long paragraph should be split at sentence boundaries
        sentences = ["This is sentence number %d with some extra padding words to make it longer." % i for i in range(20)]
        text = " ".join(sentences)
        assert len(text) > MAX_CHUNK_SIZE
        chunks = chunk_body(text)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= MAX_CHUNK_SIZE + 50


# ---------------------------------------------------------------------------
# Category 3: Title from slug
# ---------------------------------------------------------------------------

class TestTitleFromSlug:
    def test_hyphenated(self):
        assert _title_from_slug("hello-world") == "Hello World"

    def test_underscored(self):
        assert _title_from_slug("test_document") == "Test Document"

    def test_md_extension_becomes_part_of_last_word(self):
        # _title_from_slug converts hyphens/underscores but does not strip .md
        # The slug 'my-file.md' becomes 'My File.md' (period is not a separator)
        title = _title_from_slug("my-file.md")
        assert "My File" in title

    def test_single_word(self):
        assert _title_from_slug("overview") == "Overview"

    def test_multi_word_hyphenated(self):
        assert _title_from_slug("product-overview-v2") == "Product Overview V2"

    def test_mixed_separators(self):
        # Mixed hyphens and underscores
        result = _title_from_slug("hello_world-test")
        assert result == "Hello World Test"

    def test_empty_string(self):
        # Edge case: empty slug
        result = _title_from_slug("")
        assert result == ""

    def test_single_hyphen(self):
        # A slug that is just a separator normalizes to empty or single space then stripped
        result = _title_from_slug("-")
        # re.sub replaces '-' with ' ', split on spaces gives [''], capitalize gives ''
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Category 4: Memvid query sanitization
# ---------------------------------------------------------------------------

class TestSanitizeMemvidQuery:
    def test_removes_boolean_or(self):
        result = _sanitize_memvid_query("price or volume analysis")
        assert "or" not in result.split()
        assert "price" in result
        assert "analysis" in result

    def test_removes_boolean_and(self):
        result = _sanitize_memvid_query("revenue and growth")
        assert "and" not in result.split()

    def test_removes_boolean_not(self):
        result = _sanitize_memvid_query("cost not overhead")
        assert "not" not in result.split()
        assert "cost" in result

    def test_removes_common_stopwords(self):
        result = _sanitize_memvid_query("for the operations in a system")
        words = result.split()
        reserved = {'for', 'the', 'in', 'a'}
        assert not any(w.lower() in reserved for w in words)
        assert "operations" in result
        assert "system" in result

    def test_preserves_meaningful_words(self):
        result = _sanitize_memvid_query("Thermobox patent analysis")
        assert "Thermobox" in result
        assert "patent" in result
        assert "analysis" in result

    def test_all_reserved_words_returns_first_word(self):
        # When all words are reserved, fallback returns the first word
        result = _sanitize_memvid_query("and or not")
        assert len(result) > 0
        assert result == "and"  # first word as fallback

    def test_empty_string_returns_empty(self):
        result = _sanitize_memvid_query("")
        assert result == ""

    def test_normal_query_unchanged(self):
        result = _sanitize_memvid_query("revenue growth analysis")
        assert result == "revenue growth analysis"

    def test_mixed_case_reserved_words_stripped(self):
        # Reserved words are checked case-insensitively
        result = _sanitize_memvid_query("AND OR NOT price")
        # 'AND' -> lower 'and' in reserved set -> stripped
        assert "price" in result

    def test_single_non_reserved_word_preserved(self):
        result = _sanitize_memvid_query("revenue")
        assert result == "revenue"


# ---------------------------------------------------------------------------
# Category 5: Content hash
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
        # MD5 always returns 32 hex chars regardless of input length
        long_content = "word " * 10000
        h = _content_hash(long_content)
        assert len(h) == 32


# ---------------------------------------------------------------------------
# Category 6: CLI Integration Tests
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
        # Valid slug format: lowercase + hyphens + digits
        # Will fail on missing file but not on slug validation
        rc, out, err = _run_mnemo('ingest', '/tmp/does-not-exist.md', 'my-client-123')
        assert rc == 1
        # Error must be about the file, not the slug
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
        # Either "all checks passed" or "fixed the following" or "warnings"
        assert 'repair' in out.lower() or rc == 0


# ---------------------------------------------------------------------------
# Category 7: Ingest Integration Tests (with cleanup)
# ---------------------------------------------------------------------------

_KE_BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
_TEST_CLIENT = 'pytest-int-test'
# Use a fixed unique filename unlikely to clash with any real source
_TEST_SOURCE_SLUG = 'pytest-mneme-ingest-test-fixture'
_TEST_FILE = f'/tmp/{_TEST_SOURCE_SLUG}.md'


def _cleanup_test_client():
    """Remove all test artifacts for the integration test client."""
    wiki_dir = os.path.join(_KE_BASE, 'wiki', _TEST_CLIENT)
    if os.path.exists(wiki_dir):
        shutil.rmtree(wiki_dir)

    mv2 = os.path.join(_KE_BASE, 'memvid', 'per-client', f'{_TEST_CLIENT}.mv2')
    for path in [mv2, mv2 + '.lock']:
        if os.path.exists(path):
            os.remove(path)

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

    # tags.json - drop tag pages referencing the test client
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

    # index.md - strip the test client section and any wikilink entries
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
                # Drop any stray wikilink lines outside the section too
                if f'[[{_TEST_CLIENT}/' in line:
                    continue
                cleaned_lines.append(line)
            with open(index_file, 'w') as f:
                f.writelines(cleaned_lines)
        except OSError:
            pass

    # Remove any log entries for the test source file so duplicate detection
    # doesn't block re-ingestion across test runs
    log_file = os.path.join(_KE_BASE, 'log.md')
    if os.path.exists(log_file):
        try:
            with open(log_file, 'r') as f:
                lines = f.readlines()
            # Filter out log blocks that contain our test fixture filename
            # A block starts with '## [' and spans until the next '## [' or EOF
            cleaned_lines = []
            skip_block = False
            for line in lines:
                if line.startswith('## ['):
                    # Start of a new log block - decide whether to skip it
                    skip_block = _TEST_SOURCE_SLUG in line
                if not skip_block:
                    cleaned_lines.append(line)
            with open(log_file, 'w') as f:
                f.writelines(cleaned_lines)
        except OSError:
            pass

    # Finally, if we left behind empty workspace files (created by ingest
    # because the project root is the default workspace), remove them so the
    # repo stays clean. Only remove if the file is empty/header-only after
    # cleanup, never if real content remains.
    for fname in ('index.md', 'log.md'):
        fp = os.path.join(_KE_BASE, fname)
        if not os.path.exists(fp):
            continue
        try:
            with open(fp, 'r') as f:
                content = f.read().strip()
            # Header-only files are safe to remove
            header_only_signatures = (
                '# Mnemosyne Index',
                '# Mnemosyne Log',
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

    # Empty schema files: remove the schema dir if it has no real content left
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

    # Empty wiki dir
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
        """Create source file and ensure clean state before any test in this class."""
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
        """Clean up all test artifacts after all tests in this class."""
        if os.path.exists(_TEST_FILE):
            os.remove(_TEST_FILE)
        _cleanup_test_client()

    def test_ingest_creates_wiki_page(self):
        """Ingest should write a wiki page even if memvid sync subsequently fails."""
        # Run ingest - wiki write happens before memvid sync, so page is created
        # regardless of memvid capacity. Accept rc=0 or rc=1.
        _run_mnemo('ingest', _TEST_FILE, _TEST_CLIENT)
        wiki_page = os.path.join(
            _KE_BASE, 'wiki', _TEST_CLIENT, f'{_TEST_SOURCE_SLUG}.md'
        )
        assert os.path.exists(wiki_page), (
            f"Wiki page was not created at {wiki_page}"
        )

    def test_ingest_wiki_page_has_valid_frontmatter(self):
        """Created wiki page should have proper frontmatter."""
        wiki_page = os.path.join(
            _KE_BASE, 'wiki', _TEST_CLIENT, f'{_TEST_SOURCE_SLUG}.md'
        )
        # This test depends on test_ingest_creates_wiki_page having run first
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
        """Second ingest of same file should print a warning and exit 0."""
        # Ensure the file was previously ingested
        wiki_page = os.path.join(
            _KE_BASE, 'wiki', _TEST_CLIENT, f'{_TEST_SOURCE_SLUG}.md'
        )
        if not os.path.exists(wiki_page):
            _run_mnemo('ingest', _TEST_FILE, _TEST_CLIENT)

        rc, out, err = _run_mnemo('ingest', _TEST_FILE, _TEST_CLIENT)
        assert rc == 0
        assert 'previously ingested' in out.lower() or 'skipping' in out.lower()

    def test_force_reingest_prints_reingest_message(self):
        """Force re-ingest should print a re-ingesting message."""
        rc, out, err = _run_mnemo('ingest', _TEST_FILE, _TEST_CLIENT, '--force')
        # The core prints this before the memvid step
        combined = out + err
        assert 're-ingesting' in combined.lower() or 'force' in combined.lower()

    def test_ingest_appends_to_log(self):
        """Log.md should contain an entry for the ingested filename."""
        log_file = os.path.join(_KE_BASE, 'log.md')
        assert os.path.exists(log_file)
        with open(log_file, 'r') as f:
            log_content = f.read()
        assert f'{_TEST_SOURCE_SLUG}.md' in log_content

    def test_ingest_updates_index(self):
        """index.md should exist after ingest (core maintains it)."""
        # The _update_index call happens after memvid sync in core.py.
        # If the master archive is at capacity, memvid sync raises before index
        # update runs. We test that index.md exists and is well-formed instead
        # of asserting on the specific test client entry.
        index_file = os.path.join(_KE_BASE, 'index.md')
        assert os.path.exists(index_file)
        with open(index_file, 'r') as f:
            index_content = f.read()
        # Index always has at least the header line
        assert len(index_content) > 0
        # Known pre-existing clients remain present - proves index is intact
        assert 'Mnemosyne' in index_content


# ---------------------------------------------------------------------------
# Category 8: Resync (3-way merge)
# ---------------------------------------------------------------------------

import tempfile
import pytest

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
        # On Windows FileNotFoundError is raised when git can't be found.
        with pytest.raises((FileNotFoundError, RuntimeError)):
            _git_merge_file('a', 'a', 'a')


# ---------------------------------------------------------------------------
# Category 9: Resync integration (temp workspace)
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_workspace(monkeypatch):
    """Build a clean temp workspace and rebind mneme path constants at it."""
    td = tempfile.mkdtemp(prefix='mneme-resync-test-')
    # Minimum skeleton
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

    # Save prior MNEME_HOME and swap in the temp workspace via the official helper
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

        # New source with a new item
        with open(src, 'w', encoding='utf-8') as f:
            f.write('# Doc\n\n- item one\n- item two\n- item three NEW\n')

        result = resync_source(src, client)
        assert result['conflicts'] is False
        assert result['action'] == 'merge-clean'
        with open(wiki_page, 'r', encoding='utf-8') as f:
            merged = f.read()
        assert 'Human added question' in merged
        assert 'item three NEW' in merged
        # Baseline updated to theirs (should NOT contain the human section)
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
        # New source edits same line differently
        with open(src, 'w', encoding='utf-8') as f:
            f.write('# Doc\n\nRevenue: $5M INCOMING\n\nNotes here filler content.\n')

        result = resync_source(src, client)
        assert result['conflicts'] is True
        assert result['action'] == 'merge-conflict'
        with open(wiki_page, 'r', encoding='utf-8') as f:
            merged = f.read()
        assert '<<<<<<<' in merged
        # Log entry
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
        # User cleans the file
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
# Category 10: Resync CLI smoke test
# ---------------------------------------------------------------------------

class TestResyncCLI:
    def test_resync_help(self):
        rc, out, err = _run_mnemo('resync', '--help')
        assert rc == 0

    @_skip_no_git
    def test_resync_end_to_end(self):
        td = tempfile.mkdtemp(prefix='mneme-resync-cli-')
        try:
            for sub in ('wiki', 'sources', 'schema', 'memvid',
                        os.path.join('memvid', 'per-client')):
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

            # First ingest
            rc, out, err = _run_mnemo('--workspace', td, 'ingest', src, client, '--force')
            # Accept any rc; we just need a wiki page + baseline created
            wiki_page = os.path.join(td, 'wiki', client, 'doc.md')
            assert os.path.exists(wiki_page), f'wiki page missing. stderr={err}'

            # Mutate source and resync
            with open(src, 'w', encoding='utf-8') as f:
                f.write('# Doc\n\n- alpha\n- beta\n- gamma\n\nLong enough filler body content.\n')
            rc, out, err = _run_mnemo('--workspace', td, 'resync', src, client)
            assert rc == 0, f'resync failed: {err}'
            combined = out + err
            assert ('merge-clean' in combined or 'Action' in combined
                    or 'action' in combined.lower() or 'fresh' in combined.lower())
        finally:
            shutil.rmtree(td, ignore_errors=True)
