"""Tests for the traceability subsystem.

Functions covered:
  _load_traceability
  _store_trace_link
  trace_add
  trace_show (forward + backward BFS)
  trace_matrix
  trace_gaps
"""
import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mneme.core import (
    _load_traceability,
    _store_trace_link,
    trace_add,
    trace_show,
    trace_matrix,
    trace_gaps,
    _apply_workspace_override,
)


# ---------------------------------------------------------------------------
# Local fixture (mirrors tests/test_core.py:674-704 pattern)
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_workspace():
    td = tempfile.mkdtemp(prefix='mneme-trace-test-')
    for sub in ('wiki', 'sources', 'schema', 'memvid', os.path.join('memvid', 'per-client')):
        os.makedirs(os.path.join(td, sub), exist_ok=True)
    with open(os.path.join(td, 'index.md'), 'w') as f:
        f.write('# Index\n')
    with open(os.path.join(td, 'log.md'), 'w') as f:
        f.write('# Log\n')
    for fn, default in [
        ('entities.json', {'version': 1, 'updated': '2026-01-01', 'entities': []}),
        ('tags.json', {'version': 1, 'updated': '2026-01-01', 'tags': {}}),
        ('graph.json', {'version': 1, 'updated': '2026-01-01', 'nodes': [], 'edges': []}),
    ]:
        with open(os.path.join(td, 'schema', fn), 'w') as f:
            json.dump(default, f)

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


def _make_page(workspace, client, slug, page_type='requirement', body='', title=None, tags=None):
    """Seed a minimal wiki page on disk."""
    d = os.path.join(workspace, 'wiki', client)
    os.makedirs(d, exist_ok=True)
    title = title or slug
    tags = tags or [client]
    fm_lines = [
        '---',
        f'title: {title}',
        f'type: {page_type}',
        f'client: {client}',
        f'tags: [{", ".join(tags)}]',
        '---',
    ]
    page = '\n'.join(fm_lines) + '\n\n' + body + '\n'
    path = os.path.join(d, f'{slug}.md')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(page)
    return path


# ---------------------------------------------------------------------------
# _load_traceability
# ---------------------------------------------------------------------------

class TestLoadTraceability:
    def test_fresh_workspace_returns_empty_links(self, temp_workspace):
        data = _load_traceability()
        assert isinstance(data, dict)
        assert 'links' in data
        assert data['links'] == []

    def test_after_store_link_reflected(self, temp_workspace):
        _store_trace_link('demo/un-001', 'demo/req-003', 'derived-from', '2026-04-08')
        data = _load_traceability()
        assert len(data['links']) == 1
        assert data['links'][0]['from'] == 'demo/un-001'
        assert data['links'][0]['to'] == 'demo/req-003'
        assert data['links'][0]['type'] == 'derived-from'


# ---------------------------------------------------------------------------
# _store_trace_link
# ---------------------------------------------------------------------------

class TestStoreTraceLink:
    def test_store_writes_traceability_file(self, temp_workspace):
        _store_trace_link('demo/a', 'demo/b', 'derived-from', '2026-04-08')
        trace_file = os.path.join(temp_workspace, 'schema', 'traceability.json')
        assert os.path.exists(trace_file)
        with open(trace_file) as f:
            data = json.load(f)
        assert data['version'] == 1
        assert data['updated'] == '2026-04-08'
        assert isinstance(data['links'], list)
        assert len(data['links']) == 1

    def test_store_same_link_twice_is_idempotent(self, temp_workspace):
        _store_trace_link('demo/a', 'demo/b', 'derived-from', '2026-04-08')
        _store_trace_link('demo/a', 'demo/b', 'derived-from', '2026-04-08')
        data = _load_traceability()
        assert len(data['links']) == 1

    def test_multiple_distinct_links_accumulate(self, temp_workspace):
        _store_trace_link('demo/un-001', 'demo/req-003', 'derived-from', '2026-04-08')
        _store_trace_link('demo/req-003', 'demo/dds-015', 'detailed-in', '2026-04-08')
        _store_trace_link('demo/dds-015', 'demo/test-042', 'verified-by', '2026-04-08')
        data = _load_traceability()
        assert len(data['links']) == 3

    def test_today_parameter_stored_as_created(self, temp_workspace):
        _store_trace_link('demo/a', 'demo/b', 'supersedes', '2025-12-31')
        data = _load_traceability()
        assert data['links'][0]['created'] == '2025-12-31'
        assert data['updated'] == '2025-12-31'

    def test_different_relationship_same_endpoints_not_duplicate(self, temp_workspace):
        # Same endpoints but different relationship type -> two distinct links
        _store_trace_link('demo/a', 'demo/b', 'derived-from', '2026-04-08')
        _store_trace_link('demo/a', 'demo/b', 'supersedes', '2026-04-08')
        data = _load_traceability()
        assert len(data['links']) == 2


# ---------------------------------------------------------------------------
# trace_add (top-level with validation)
# ---------------------------------------------------------------------------

class TestTraceAdd:
    def test_happy_path(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'un-001', page_type='entity')
        _make_page(temp_workspace, 'demo', 'req-003', page_type='entity')
        result = trace_add('demo/un-001', 'demo/req-003', 'derived-from')
        assert 'error' not in result
        assert result['from'] == 'demo/un-001'
        assert result['to'] == 'demo/req-003'
        assert result['type'] == 'derived-from'
        # Link is stored in traceability.json
        data = _load_traceability()
        assert len(data['links']) == 1

    def test_from_page_missing_returns_error(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'req-003', page_type='entity')
        result = trace_add('demo/missing', 'demo/req-003', 'derived-from')
        assert 'error' in result
        assert 'missing' in result['error'].lower() or 'not found' in result['error'].lower()
        # And no link was stored
        data = _load_traceability()
        assert len(data['links']) == 0

    def test_to_page_missing_returns_error(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'un-001', page_type='entity')
        result = trace_add('demo/un-001', 'demo/never-existed', 'derived-from')
        assert 'error' in result
        data = _load_traceability()
        assert len(data['links']) == 0

    def test_unknown_relationship_type_accepted(self, temp_workspace):
        # The system uses free-form relationship strings (no whitelist)
        _make_page(temp_workspace, 'demo', 'a', page_type='entity')
        _make_page(temp_workspace, 'demo', 'b', page_type='entity')
        result = trace_add('demo/a', 'demo/b', 'cromulent-link')
        assert 'error' not in result
        assert result['type'] == 'cromulent-link'

    def test_idempotent_via_trace_add(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'a', page_type='entity')
        _make_page(temp_workspace, 'demo', 'b', page_type='entity')
        trace_add('demo/a', 'demo/b', 'derived-from')
        trace_add('demo/a', 'demo/b', 'derived-from')
        data = _load_traceability()
        assert len(data['links']) == 1

    def test_self_link_allowed(self, temp_workspace):
        # The implementation does not block self-links
        _make_page(temp_workspace, 'demo', 'a', page_type='entity')
        result = trace_add('demo/a', 'demo/a', 'supersedes')
        assert 'error' not in result
        data = _load_traceability()
        assert len(data['links']) == 1

    def test_trace_add_updates_from_page_frontmatter(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'un-001', page_type='entity')
        _make_page(temp_workspace, 'demo', 'req-003', page_type='entity')
        trace_add('demo/un-001', 'demo/req-003', 'derived-from')
        with open(os.path.join(temp_workspace, 'wiki', 'demo', 'un-001.md'), 'r') as f:
            page = f.read()
        # The link reference should be embedded somewhere in the file
        assert 'req-003' in page
        assert 'derived-from' in page


# ---------------------------------------------------------------------------
# trace_show (BFS forward + backward)
# ---------------------------------------------------------------------------

class TestTraceShow:
    def _seed_chain(self, workspace):
        """Seed: un-001 -> req-003 -> dds-015 -> test-042"""
        for slug in ('un-001', 'req-003', 'dds-015', 'test-042'):
            _make_page(workspace, 'demo', slug, page_type='entity')
        _store_trace_link('demo/un-001', 'demo/req-003', 'derived-from', '2026-04-08')
        _store_trace_link('demo/req-003', 'demo/dds-015', 'detailed-in', '2026-04-08')
        _store_trace_link('demo/dds-015', 'demo/test-042', 'verified-by', '2026-04-08')

    def test_forward_walks_full_chain(self, temp_workspace):
        self._seed_chain(temp_workspace)
        result = trace_show('demo/un-001', direction='forward')
        assert result['root'] == 'demo/un-001'
        assert result['direction'] == 'forward'
        chain_pages = [item['page'] for item in result['chain']]
        assert 'demo/req-003' in chain_pages
        assert 'demo/dds-015' in chain_pages
        assert 'demo/test-042' in chain_pages
        # Depths increase along the chain
        depths = {item['page']: item['depth'] for item in result['chain']}
        assert depths['demo/req-003'] == 1
        assert depths['demo/dds-015'] == 2
        assert depths['demo/test-042'] == 3

    def test_backward_walks_to_root(self, temp_workspace):
        self._seed_chain(temp_workspace)
        result = trace_show('demo/test-042', direction='backward')
        chain_pages = [item['page'] for item in result['chain']]
        assert 'demo/dds-015' in chain_pages
        assert 'demo/req-003' in chain_pages
        assert 'demo/un-001' in chain_pages

    def test_orphan_page_returns_empty_chain(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'orphan', page_type='entity')
        result = trace_show('demo/orphan', direction='forward')
        assert result['chain'] == []
        assert result['root'] == 'demo/orphan'

    def test_cycle_terminates(self, temp_workspace):
        # a -> b -> a (a cycle)
        _make_page(temp_workspace, 'demo', 'a', page_type='entity')
        _make_page(temp_workspace, 'demo', 'b', page_type='entity')
        _store_trace_link('demo/a', 'demo/b', 'derived-from', '2026-04-08')
        _store_trace_link('demo/b', 'demo/a', 'derived-from', '2026-04-08')
        # Should terminate without infinite loop
        result = trace_show('demo/a', direction='forward')
        assert result['root'] == 'demo/a'
        # Each page visited at most once
        chain_pages = [item['page'] for item in result['chain']]
        assert len(chain_pages) == len(set(chain_pages))

    def test_branching_forward(self, temp_workspace):
        # un-001 derives req-003 AND req-007
        for slug in ('un-001', 'req-003', 'req-007'):
            _make_page(temp_workspace, 'demo', slug, page_type='entity')
        _store_trace_link('demo/un-001', 'demo/req-003', 'derived-from', '2026-04-08')
        _store_trace_link('demo/un-001', 'demo/req-007', 'derived-from', '2026-04-08')
        result = trace_show('demo/un-001', direction='forward')
        chain_pages = [item['page'] for item in result['chain']]
        assert 'demo/req-003' in chain_pages
        assert 'demo/req-007' in chain_pages


# ---------------------------------------------------------------------------
# trace_matrix
# ---------------------------------------------------------------------------

class TestTraceMatrix:
    def test_matrix_for_seeded_client(self, temp_workspace):
        for slug in ('un-001', 'req-003', 'dds-015'):
            _make_page(temp_workspace, 'demo', slug, page_type='entity')
        _store_trace_link('demo/un-001', 'demo/req-003', 'derived-from', '2026-04-08')
        _store_trace_link('demo/req-003', 'demo/dds-015', 'detailed-in', '2026-04-08')
        result = trace_matrix('demo')
        assert 'rows' in result
        assert 'columns' in result
        assert 'cells' in result
        assert 'demo/un-001' in result['rows']
        assert 'demo/req-003' in result['rows']
        assert 'demo/dds-015' in result['rows']
        assert result['cells']['demo/un-001|demo/req-003'] == 'derived-from'
        assert result['cells']['demo/req-003|demo/dds-015'] == 'detailed-in'

    def test_matrix_filters_by_client(self, temp_workspace):
        # Only links involving 'demo/' pages should appear in a demo matrix
        _store_trace_link('demo/a', 'demo/b', 'derived-from', '2026-04-08')
        _store_trace_link('other/x', 'other/y', 'derived-from', '2026-04-08')
        result = trace_matrix('demo')
        all_cells_keys = ' '.join(result['cells'].keys())
        assert 'other/x' not in all_cells_keys
        assert 'other/y' not in all_cells_keys
        assert 'demo/a' in all_cells_keys

    def test_empty_client_returns_empty_matrix(self, temp_workspace):
        result = trace_matrix('nothing-here')
        assert result['rows'] == []
        assert result['columns'] == []
        assert result['cells'] == {}


# ---------------------------------------------------------------------------
# trace_gaps
# ---------------------------------------------------------------------------

class TestTraceGaps:
    def test_unmitigated_hazard_flagged(self, temp_workspace):
        _make_page(
            temp_workspace, 'demo', 'haz-001',
            page_type='entity',
            title='Hazard 001 Electrical Shock',
            tags=['hazard'],
        )
        result = trace_gaps('demo')
        assert 'demo/haz-001' in result['unmitigated']

    def test_mitigated_hazard_not_flagged(self, temp_workspace):
        _make_page(
            temp_workspace, 'demo', 'haz-001',
            page_type='entity',
            title='Hazard 001 Electrical Shock',
            tags=['hazard'],
        )
        _make_page(temp_workspace, 'demo', 'rma-003', page_type='entity')
        _store_trace_link('demo/haz-001', 'demo/rma-003', 'mitigated-by', '2026-04-08')
        result = trace_gaps('demo')
        assert 'demo/haz-001' not in result['unmitigated']

    def test_unverified_requirement_flagged(self, temp_workspace):
        _make_page(
            temp_workspace, 'demo', 'req-011',
            page_type='entity',
            title='Requirement 011 - Battery life',
            tags=['requirement'],
        )
        result = trace_gaps('demo')
        assert 'demo/req-011' in result['unverified']

    def test_verified_requirement_not_flagged(self, temp_workspace):
        _make_page(
            temp_workspace, 'demo', 'req-003',
            page_type='entity',
            title='Requirement 003',
            tags=['requirement'],
        )
        _make_page(temp_workspace, 'demo', 'test-042', page_type='entity')
        _store_trace_link('demo/req-003', 'demo/test-042', 'verified-by', '2026-04-08')
        result = trace_gaps('demo')
        assert 'demo/req-003' not in result['unverified']

    def test_unlinked_user_need_flagged(self, temp_workspace):
        _make_page(
            temp_workspace, 'demo', 'un-005',
            page_type='entity',
            title='User Need 005',
            tags=['user-need'],
        )
        result = trace_gaps('demo')
        assert 'demo/un-005' in result['unlinked_needs']

    def test_empty_client_no_gaps(self, temp_workspace):
        result = trace_gaps('client-with-nothing')
        assert result['unverified'] == []
        assert result['unmitigated'] == []
        assert result['unlinked_needs'] == []
        assert result['total_gaps'] == 0

    def test_total_gaps_count(self, temp_workspace):
        _make_page(
            temp_workspace, 'demo', 'req-011',
            page_type='entity',
            title='Requirement 011',
            tags=['requirement'],
        )
        _make_page(
            temp_workspace, 'demo', 'haz-009',
            page_type='entity',
            title='Hazard 009',
            tags=['hazard'],
        )
        result = trace_gaps('demo')
        # At least the seeded gaps are counted
        assert result['total_gaps'] >= 2
