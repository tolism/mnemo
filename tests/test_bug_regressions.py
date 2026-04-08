"""
Regression tests for bugs found by the test-coverage sweep on 2026-04-08.

Each test class anchors to a specific bug fix in mneme/core.py. If any of
these tests start failing, the corresponding bug has been reintroduced.

Bug index:
  #1  dual_search NameError when memvid is unavailable
  #2  ingest_csv silently dropped trace links
  #3  _detect_csv_mapping substring scoring picked the wrong mapping
  #4  trace_gaps used backslash slugs on Windows (cross-platform)
  #5  _print_drift_report crashed on string summary
  #6  lint() Windows path bug broke orphan / dead-link / coverage checks
"""
import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mneme.core import (
    _apply_workspace_override,
    _detect_csv_mapping,
    _load_traceability,
    _print_drift_report,
    check_drift,
    dual_search,
    ingest_csv,
    lint,
    trace_gaps,
)


# ---------------------------------------------------------------------------
# Local fixture (mirrors tests/test_core.py temp_workspace pattern)
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_workspace():
    td = tempfile.mkdtemp(prefix='mneme-regression-')
    for sub in ('wiki', 'sources', 'schema', 'memvid',
                os.path.join('memvid', 'per-client')):
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


def _make_page(workspace, client, slug, body='', title=None,
               page_type='source-summary', tags=None):
    d = os.path.join(workspace, 'wiki', client)
    os.makedirs(d, exist_ok=True)
    title = title or slug
    tags = tags or [client]
    fm = (
        '---\n'
        f'title: {title}\n'
        f'type: {page_type}\n'
        f'client: {client}\n'
        f'tags: [{", ".join(tags)}]\n'
        '---\n\n'
        f'{body}\n'
    )
    path = os.path.join(d, f'{slug}.md')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(fm)
    return path


# ---------------------------------------------------------------------------
# Bug #1 - dual_search NameError when memvid unavailable
# ---------------------------------------------------------------------------

class TestBug1DualSearchNameError:
    """
    Before the fix, dual_search() crashed with UnboundLocalError because the
    no-memvid path returned `wiki_results + []` and there was no variable
    named `wiki_results` (the local was named `results`).
    """

    def test_returns_list_without_memvid(self, temp_workspace):
        # Should not raise UnboundLocalError
        out = dual_search('anything')
        assert isinstance(out, list)

    def test_returns_wiki_hits(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'budget', 'quarterly budget review')
        out = dual_search('quarterly')
        assert any('budget' in r.get('wiki_path', '') for r in out)

    def test_client_filter(self, temp_workspace):
        _make_page(temp_workspace, 'alpha', 'p1', 'shared keyword')
        _make_page(temp_workspace, 'beta', 'p1', 'shared keyword')
        out = dual_search('shared', client='alpha')
        assert all('alpha' in r.get('wiki_path', '') for r in out)


# ---------------------------------------------------------------------------
# Bug #2 - ingest_csv silently dropped trace links
# ---------------------------------------------------------------------------

class TestBug2IngestCsvTracePersistence:
    """
    Before the fix, trace_add() returned {'error': ...} (didn't raise) when
    the target page didn't exist yet, the except branch never fired,
    trace_links_created was incremented anyway, and traceability.json was
    never written for CSV-derived links - silent data loss for the QMS
    V-model workflow.
    """

    CSV = (
        'ID,Title,Description,Linked Requirement\n'
        'UN-001,Patient Safety,No shocks,REQ-003\n'
        'UN-002,Battery Life,8 hours,REQ-007\n'
    )

    def _write_csv(self, workspace):
        path = os.path.join(workspace, 'sources', 'user-needs.csv')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(self.CSV)
        return path

    def test_traceability_file_actually_written(self, temp_workspace):
        csv_path = self._write_csv(temp_workspace)
        ingest_csv(csv_path, 'demo', mapping_name='user-needs')
        trace_file = os.path.join(temp_workspace, 'schema', 'traceability.json')
        assert os.path.exists(trace_file), \
            'traceability.json should be created when CSV ingest produces trace links'

    def test_links_match_csv_rows(self, temp_workspace):
        csv_path = self._write_csv(temp_workspace)
        ingest_csv(csv_path, 'demo', mapping_name='user-needs')
        data = _load_traceability()
        links = data['links']
        assert len(links) == 2
        from_pages = sorted(link['from'] for link in links)
        to_pages = sorted(link['to'] for link in links)
        assert from_pages == ['demo/un-001', 'demo/un-002']
        assert to_pages == ['demo/req-003', 'demo/req-007']
        # All links use the user-needs mapping's traces.implemented-by relation
        assert all(link['type'] == 'implemented-by' for link in links)

    def test_trace_links_persist_even_when_target_pages_missing(self, temp_workspace):
        # The whole point: target REQ pages don't exist yet at the time of
        # CSV ingest, but the links MUST still be persisted so a later
        # `mneme ingest-csv requirements.csv` can complete the chain.
        csv_path = self._write_csv(temp_workspace)
        # Confirm targets do not exist
        for slug in ('req-003', 'req-007'):
            assert not os.path.exists(
                os.path.join(temp_workspace, 'wiki', 'demo', f'{slug}.md')
            )
        result = ingest_csv(csv_path, 'demo', mapping_name='user-needs')
        # Counter and disk both reflect the link
        assert result['trace_links_created'] == 2
        data = _load_traceability()
        assert len(data['links']) == 2


# ---------------------------------------------------------------------------
# Bug #3 - _detect_csv_mapping substring scoring picked the wrong mapping
# ---------------------------------------------------------------------------

class TestBug3DetectCsvMapping:
    """
    Before the fix, _detect_csv_mapping used substring matching against the
    joined header string, so a column called 'Linked Requirement' caused the
    `requirements` mapping's `requirement` keyword to score, outranking
    user-needs even on a clearly user-needs CSV.
    """

    def test_user_needs_csv_picks_user_needs(self):
        headers = ['ID', 'Title', 'Description', 'Priority',
                   'Acceptance Criteria', 'Linked Requirement']
        assert _detect_csv_mapping(headers) == 'user-needs'

    def test_risk_register_csv_picks_risk_register(self):
        headers = ['ID', 'Hazard', 'Harm', 'Severity', 'Probability',
                   'Risk Level', 'Risk Control', 'RMA', 'Verification']
        assert _detect_csv_mapping(headers) == 'risk-register'

    def test_unknown_headers_returns_none(self):
        assert _detect_csv_mapping(['Foo', 'Bar', 'Baz']) is None

    def test_ambiguous_below_threshold_returns_none(self):
        # Only one column matches anything; below the min-2 threshold.
        assert _detect_csv_mapping(['ID']) is None

    def test_substring_alone_does_not_trigger_match(self):
        # 'requirementoid' is not equal to 'requirement', so it must NOT
        # contribute to the requirements mapping's detect score.
        # Two unrelated columns -> total below threshold -> None.
        assert _detect_csv_mapping(['Requirementoid', 'Severityness']) is None


# ---------------------------------------------------------------------------
# Bug #4 - trace_gaps Windows backslash slug
# ---------------------------------------------------------------------------

class TestBug4TraceGapsWindowsPath:
    """
    Before the fix, trace_gaps used os.path.relpath which returns backslash
    paths on Windows, but trace_add / _store_trace_link store and compare
    forward-slash slugs. The set membership check `slug not in verified_pages`
    was always true on Windows, so EVERY hazard / requirement / user-need was
    misclassified as a gap.
    """

    def test_gap_slugs_use_forward_slashes(self, temp_workspace):
        _make_page(
            temp_workspace, 'demo', 'haz-001',
            page_type='entity',
            title='Hazard 001',
            tags=['hazard'],
        )
        result = trace_gaps('demo')
        # All emitted slugs must use forward slashes regardless of platform
        for slug in result['unmitigated']:
            assert '\\' not in slug, f'gap slug contains backslash: {slug}'
            assert slug.count('/') == 1, f'gap slug must be client/page: {slug}'

    def test_mitigated_hazard_recognized_on_any_platform(self, temp_workspace):
        from mneme.core import _store_trace_link
        _make_page(
            temp_workspace, 'demo', 'haz-001',
            page_type='entity',
            title='Hazard 001',
            tags=['hazard'],
        )
        _make_page(temp_workspace, 'demo', 'rma-003', page_type='entity')
        _store_trace_link('demo/haz-001', 'demo/rma-003', 'mitigated-by', '2026-04-08')
        result = trace_gaps('demo')
        assert 'demo/haz-001' not in result['unmitigated'], \
            'mitigated hazard should not appear in gap list (was failing on Windows)'


# ---------------------------------------------------------------------------
# Bug #5 - _print_drift_report TypeError on string summary
# ---------------------------------------------------------------------------

class TestBug5PrintDriftReport:
    """
    Before the fix, _print_drift_report indexed `s["total_wiki_pages"]` but
    `s` was a string when memvid was unavailable, raising TypeError.
    """

    def test_string_summary_does_not_crash(self, capsys):
        report = {
            'missing_from_memvid': [],
            'orphan_frames': [],
            'stale': [],
            'summary': 'Memvid not installed.',
        }
        _print_drift_report(report)  # must not raise
        out = capsys.readouterr().out
        assert 'Memvid not installed' in out

    def test_dict_summary_still_works(self, capsys):
        report = {
            'missing_from_memvid': [],
            'orphan_frames': [],
            'stale': [],
            'summary': {
                'total_wiki_pages': 5,
                'synced': 5,
                'sync_pct': 100.0,
                'missing_from_memvid': 0,
                'orphan_frames': 0,
                'recently_modified_may_be_stale': 0,
                'memvid_frame_count': 12,
            },
        }
        _print_drift_report(report)
        out = capsys.readouterr().out
        assert 'Wiki pages total' in out
        assert '5' in out

    def test_check_drift_report_renders_without_memvid(self, temp_workspace, capsys):
        # End-to-end: real check_drift output (string summary path) should
        # render without raising.
        report = check_drift()
        _print_drift_report(report)


# ---------------------------------------------------------------------------
# Bug #6 - lint() Windows path bug
# ---------------------------------------------------------------------------

class TestBug6LintWindowsPaths:
    """
    Before the fix, lint() used `os.path.splitext(rel)[0]` to build slugs,
    which returned `demo\\alpha` on Windows. Then:
      - `slug.count('/') > 0` was always 0 -> orphan check skipped EVERY page
      - `target in page_map` for wikilinks like `[[demo/alpha]]` never matched
        -> dead-link / coverage / schema-drift checks were silently broken
    """

    def test_orphan_pages_are_flagged(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'alpha', 'Hello.')
        _make_page(temp_workspace, 'demo', 'beta', 'World.')
        result = lint()
        assert len(result['issues']['orphan_pages']) >= 1

    def test_orphan_slugs_use_forward_slashes(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'alpha', 'Hello.')
        _make_page(temp_workspace, 'demo', 'beta', 'World.')
        result = lint()
        for slug in result['issues']['orphan_pages']:
            assert '\\' not in slug, f'orphan slug contains backslash: {slug}'

    def test_valid_wikilink_does_not_appear_as_dead(self, temp_workspace):
        # Page A links to existing page B - this should NOT show up in dead_links.
        _make_page(temp_workspace, 'demo', 'alpha', 'See [[demo/beta]] for context.')
        _make_page(temp_workspace, 'demo', 'beta', 'Body.')
        result = lint()
        for d in result['issues']['dead_links']:
            assert 'demo/beta' not in d.get('broken_link', ''), \
                'valid wikilink incorrectly reported as dead (Windows path bug)'

    def test_dead_wikilink_still_flagged(self, temp_workspace):
        # Sanity: a truly broken link must still be reported.
        _make_page(temp_workspace, 'demo', 'alpha', 'See [[demo/never-existed]].')
        result = lint()
        assert any('never-existed' in d['broken_link']
                   for d in result['issues']['dead_links'])

    def test_pages_linked_from_other_pages_not_orphans(self, temp_workspace):
        _make_page(temp_workspace, 'demo', 'alpha', 'See [[demo/beta]].')
        _make_page(temp_workspace, 'demo', 'beta', 'I am referenced.')
        result = lint()
        # beta has an incoming link, so it should NOT be an orphan
        assert 'demo/beta' not in result['issues']['orphan_pages']
