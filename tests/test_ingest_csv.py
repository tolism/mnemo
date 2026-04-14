"""Tests for the CSV ingest subsystem."""
import json
import os
import shutil
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mneme.core import (
    _load_csv_mapping,
    _detect_csv_mapping,
    _csv_row_to_wiki_page,
    ingest_csv,
    _apply_workspace_override,
)


@pytest.fixture
def temp_workspace(monkeypatch):
    """Build a clean temp workspace and rebind mneme path constants at it."""
    td = tempfile.mkdtemp(prefix='mneme-csv-test-')
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


# ---------------------------------------------------------------------------
# TestLoadCsvMapping
# ---------------------------------------------------------------------------

class TestLoadCsvMapping:
    def test_load_user_needs(self):
        m = _load_csv_mapping('user-needs')
        assert isinstance(m, dict)
        for key in ('mapping', 'detect_headers', 'page_type', 'id_column', 'title_column'):
            assert key in m

    def test_load_risk_register(self):
        m = _load_csv_mapping('risk-register')
        assert isinstance(m, dict)
        assert m['page_type'] == 'hazard'
        assert 'mapping' in m

    def test_load_missing_raises(self):
        with pytest.raises(FileNotFoundError):
            _load_csv_mapping('does-not-exist')

    def test_loaded_mapping_is_dict(self):
        m = _load_csv_mapping('user-needs')
        assert isinstance(m, dict)
        assert isinstance(m['mapping'], dict)


# ---------------------------------------------------------------------------
# TestDetectCsvMapping
# ---------------------------------------------------------------------------

class TestDetectCsvMapping:
    def test_detect_user_needs(self):
        headers = ['ID', 'Title', 'Description', 'Priority',
                   'Acceptance Criteria', 'Linked Requirement']
        assert _detect_csv_mapping(headers) == 'user-needs'

    def test_detect_risk_register(self):
        headers = ['ID', 'Hazard', 'Harm', 'Severity', 'Probability',
                   'Risk Level', 'Risk Control', 'RMA', 'Verification']
        assert _detect_csv_mapping(headers) == 'risk-register'

    def test_no_match_returns_none(self):
        assert _detect_csv_mapping(['Foo', 'Bar', 'Baz']) is None

    def test_empty_headers(self):
        assert _detect_csv_mapping([]) is None

    def test_single_keyword_below_threshold(self):
        # "stakeholder need" is unique to user-needs detect_headers and is
        # not a mapping column name, so it scores exactly 1 => below min-2.
        assert _detect_csv_mapping(['stakeholder need']) is None


# ---------------------------------------------------------------------------
# TestCsvRowToWikiPage
# ---------------------------------------------------------------------------

class TestCsvRowToWikiPage:
    def test_basic_row(self):
        mapping = _load_csv_mapping('user-needs')
        row = {
            'ID': 'UN-001',
            'Title': 'Patient Safety',
            'Description': 'No shocks',
            'Linked Requirement': 'REQ-003',
        }
        slug, page_text, traces = _csv_row_to_wiki_page(row, mapping, 'demo', '2026-04-08')
        assert slug == 'un-001'
        assert 'Patient Safety' in page_text
        assert 'No shocks' in page_text
        # user-needs.json maps "Linked Requirement" -> "traces.implemented-by"
        assert 'implemented-by' in traces
        assert 'REQ-003' in traces['implemented-by']

    def test_row_missing_id(self):
        mapping = _load_csv_mapping('user-needs')
        row = {'Title': 'No ID Here', 'Description': 'foo'}
        # Function does not raise; it falls back to title-based slug.
        slug, page_text, traces = _csv_row_to_wiki_page(row, mapping, 'demo', '2026-04-08')
        assert slug  # non-empty
        assert 'No ID Here' in page_text

    def test_extra_columns_ignored(self):
        mapping = _load_csv_mapping('user-needs')
        row = {
            'ID': 'UN-099',
            'Title': 'Thing',
            'Description': 'Body',
            'UnmappedCol': 'should be ignored',
            'AnotherExtra': 'also ignored',
        }
        slug, page_text, traces = _csv_row_to_wiki_page(row, mapping, 'demo', '2026-04-08')
        assert slug == 'un-099'
        assert 'should be ignored' not in page_text
        assert 'also ignored' not in page_text


# ---------------------------------------------------------------------------
# TestIngestCsvIntegration
# ---------------------------------------------------------------------------

CSV_CONTENT = (
    'ID,Title,Description,Priority,Acceptance Criteria,Linked Requirement\n'
    'UN-001,Patient Safety,No unintended shocks,High,Device rejects bad input,REQ-003\n'
    'UN-002,Battery Life,Operates 24 hours,Medium,Lasts 24h on one charge,REQ-010\n'
    'UN-003,Waterproof,IP67 rated,Low,Survives submersion,REQ-020\n'
)


def _write_csv(workspace, content=CSV_CONTENT, name='user-needs.csv'):
    path = os.path.join(workspace, 'sources', name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return path


class TestIngestCsvIntegration:
    def test_end_to_end_explicit_mapping(self, temp_workspace):
        csv_path = _write_csv(temp_workspace)
        result = ingest_csv(csv_path, 'demo', mapping_name='user-needs')
        assert result['pages_created'] == 3
        for slug in ('un-001', 'un-002', 'un-003'):
            p = os.path.join(temp_workspace, 'wiki', 'demo', f'{slug}.md')
            assert os.path.exists(p), f'missing {p}'
            with open(p, 'r', encoding='utf-8') as f:
                content = f.read()
            from mneme.core import parse_frontmatter
            fm, body = parse_frontmatter(content)
            assert fm.get('title')
            assert fm.get('client') == 'demo'
        # traceability.json should exist and have entries
        trace_path = os.path.join(temp_workspace, 'schema', 'traceability.json')
        assert os.path.exists(trace_path)
        with open(trace_path) as f:
            tr = json.load(f)
        # There should be at least one link. Structure may vary; just check
        # the file is non-trivial.
        assert tr  # not None/empty

    def test_autodetect_mapping(self, temp_workspace):
        csv_path = _write_csv(temp_workspace)
        result = ingest_csv(csv_path, 'demo')
        assert result['pages_created'] == 3
        assert result.get('mapping_used') == 'user-needs'

    def test_dry_run_creates_no_files(self, temp_workspace):
        csv_path = _write_csv(temp_workspace)
        result = ingest_csv(csv_path, 'demo', mapping_name='user-needs', dry_run=True)
        # Dry run should not create wiki files
        demo_dir = os.path.join(temp_workspace, 'wiki', 'demo')
        if os.path.exists(demo_dir):
            files = [f for f in os.listdir(demo_dir) if f.endswith('.md')]
            assert files == []
        # pages_created should be 0 since nothing written
        assert result['pages_created'] == 0

    def test_empty_csv_header_only(self, temp_workspace):
        csv_path = _write_csv(
            temp_workspace,
            content='ID,Title,Description,Priority,Acceptance Criteria,Linked Requirement\n',
            name='empty.csv',
        )
        result = ingest_csv(csv_path, 'demo', mapping_name='user-needs')
        assert result.get('pages_created', 0) == 0

    def test_invalid_mapping_name(self, temp_workspace):
        csv_path = _write_csv(temp_workspace)
        # Current implementation returns {'error': ...} dict instead of
        # raising. Accept either behavior.
        try:
            result = ingest_csv(csv_path, 'demo', mapping_name='totally-fake-mapping')
        except (FileNotFoundError, ValueError):
            return
        assert 'error' in result
        assert result.get('pages_created', 0) == 0

    def test_idempotent_rerun(self, temp_workspace):
        csv_path = _write_csv(temp_workspace)
        r1 = ingest_csv(csv_path, 'demo', mapping_name='user-needs')
        assert r1['pages_created'] == 3
        r2 = ingest_csv(csv_path, 'demo', mapping_name='user-needs')
        # Second run: nothing newly created; all updated instead.
        assert r2['pages_created'] == 0
        assert r2['pages_updated'] == 3
        # Still only 3 files on disk
        demo_dir = os.path.join(temp_workspace, 'wiki', 'demo')
        files = sorted(f for f in os.listdir(demo_dir) if f.endswith('.md'))
        assert files == ['un-001.md', 'un-002.md', 'un-003.md']

    def test_client_slug_not_validated(self, temp_workspace):
        # The function does not validate client slugs; it just uses them as
        # directory names. A slug with spaces would create a directory with
        # spaces. We don't assert on fs behavior here; we just verify the
        # call does not crash on a weird but filesystem-legal slug.
        csv_path = _write_csv(temp_workspace)
        result = ingest_csv(csv_path, 'DemoClient', mapping_name='user-needs')
        assert result['pages_created'] == 3
