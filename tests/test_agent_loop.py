"""Tests for the draft + agent loop subsystems."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mneme.core import (
    draft_document,
    _format_write_packet,
    agent_plan,
    agent_show_plan,
    agent_next_task,
    agent_task_done,
    agent_list_plans,
    set_active_profile,
    _apply_workspace_override,
    _plan_dir,
    _plan_path,
    _plan_state_path,
    _load_plan_state,
)


@pytest.fixture
def temp_workspace():
    td = tempfile.mkdtemp(prefix='mneme-agent-test-')
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
    # Reset active profile for each test
    try:
        set_active_profile('')
    except Exception:
        pass
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


# ---------------------------------------------------------------------------
# draft_document
# ---------------------------------------------------------------------------

class TestDraftDocument:
    def test_no_active_profile_returns_error(self, temp_workspace):
        result = draft_document('design-validation-report', 'context', 'demo')
        assert 'error' in result

    def test_unknown_doc_type_returns_error(self, temp_workspace):
        set_active_profile('eu-mdr')
        result = draft_document('nonsense', 'context', 'demo')
        assert 'error' in result
        assert 'Unknown doc-type' in result['error']

    def test_unknown_section_returns_error(self, temp_workspace):
        set_active_profile('eu-mdr')
        result = draft_document('design-validation-report', 'nonsense', 'demo')
        assert 'error' in result
        assert 'Unknown section' in result['error']

    def test_happy_path_returns_packet(self, temp_workspace):
        set_active_profile('eu-mdr')
        result = draft_document('design-validation-report', 'context', 'demo')
        assert 'error' not in result
        for key in ('profile_name', 'doc_type', 'section', 'section_notes',
                    'writing_style', 'submission_checklist', 'evidence', 'write_prompt'):
            assert key in result
        assert result['doc_type'] == 'design-validation-report'
        assert result['section'] == 'context'
        assert 'EU MDR' in result['profile_name']
        assert isinstance(result['writing_style'], dict)
        assert 'principles' in result['writing_style']

    def test_section_notes_pulled_from_profile(self, temp_workspace):
        set_active_profile('eu-mdr')
        result = draft_document('design-validation-report', 'context', 'demo')
        assert isinstance(result['section_notes'], str)
        assert result['section_notes']
        assert 'literature review' in result['section_notes'].lower()

    def test_explicit_source_included_as_evidence(self, temp_workspace):
        set_active_profile('eu-mdr')
        src_path = os.path.join(temp_workspace, 'sources', 'mysrc.txt')
        with open(src_path, 'w', encoding='utf-8') as f:
            f.write('the quick brown fox')
        result = draft_document('design-validation-report', 'context', 'demo',
                                source_path=src_path)
        assert 'error' not in result
        explicit = [e for e in result['evidence'] if e.get('kind') == 'explicit-source']
        assert len(explicit) == 1
        assert 'the quick brown fox' in explicit[0]['content']

    def test_query_includes_search_hits(self, temp_workspace):
        set_active_profile('eu-mdr')
        _make_page(temp_workspace, 'demo', 'q-page',
                   'This page discusses the quarterly benchmarks in detail.')
        _make_page(temp_workspace, 'demo', 'a-page',
                   'This page discusses the annual benchmarks in detail.')
        result = draft_document('design-validation-report', 'test-results', 'demo',
                                query='quarterly')
        assert 'error' not in result
        hits = [e for e in result['evidence'] if e.get('kind') == 'wiki-search-hit']
        assert any('q-page' in h.get('path', '') for h in hits)
        assert not any('a-page' in h.get('path', '') for h in hits)

    def test_format_write_packet_renders_markdown(self, temp_workspace):
        set_active_profile('eu-mdr')
        packet = draft_document('design-validation-report', 'context', 'demo')
        md = _format_write_packet(packet)
        assert isinstance(md, str)
        assert md.startswith('# Write packet')
        assert 'context' in md
        assert '## Write prompt' in md


# ---------------------------------------------------------------------------
# agent_plan
# ---------------------------------------------------------------------------

class TestAgentPlan:
    def test_no_active_profile_returns_error(self, temp_workspace):
        result = agent_plan('goal', 'design-validation-report', 'demo')
        assert 'error' in result

    def test_unknown_doc_type_returns_error(self, temp_workspace):
        set_active_profile('eu-mdr')
        result = agent_plan('goal', 'nonsense', 'demo')
        assert 'error' in result
        assert 'Unknown doc-type' in result['error']

    def test_plan_has_one_task_per_section(self, temp_workspace):
        set_active_profile('eu-mdr')
        plan = agent_plan('produce DVR', 'design-validation-report', 'demo')
        assert 'error' not in plan
        assert len(plan['tasks']) == 15
        section_tasks = [t for t in plan['tasks'] if t['id'].startswith('section-')]
        assert len(section_tasks) == 11
        for t in section_tasks:
            assert t['kind'] == 'draft-section'
            assert t['depends_on'] == []

    def test_section_kind_is_review_when_page_exists(self, temp_workspace):
        set_active_profile('eu-mdr')
        _make_page(temp_workspace, 'demo', 'design-validation-report',
                   'existing content', page_type='deliverable')
        plan = agent_plan('produce DVR', 'design-validation-report', 'demo')
        section_tasks = [t for t in plan['tasks'] if t['id'].startswith('section-')]
        for t in section_tasks:
            assert t['kind'] == 'review-section'

    def test_assemble_depends_on_all_sections(self, temp_workspace):
        set_active_profile('eu-mdr')
        plan = agent_plan('produce DVR', 'design-validation-report', 'demo')
        assemble = next(t for t in plan['tasks'] if t['id'] == 'assemble-document')
        section_ids = [t['id'] for t in plan['tasks'] if t['id'].startswith('section-')]
        assert set(assemble['depends_on']) == set(section_ids)

    def test_dependency_chain_is_correct(self, temp_workspace):
        set_active_profile('eu-mdr')
        plan = agent_plan('produce DVR', 'design-validation-report', 'demo')
        by_id = {t['id']: t for t in plan['tasks']}
        assert by_id['harmonize']['depends_on'] == ['assemble-document']
        assert by_id['review-page']['depends_on'] == ['harmonize']
        assert by_id['submission-check']['depends_on'] == ['review-page']

    def test_plan_persisted_to_disk(self, temp_workspace):
        set_active_profile('eu-mdr')
        plan = agent_plan('produce DVR', 'design-validation-report', 'demo')
        pid = plan['plan_id']
        assert os.path.exists(_plan_path(pid))
        assert os.path.exists(_plan_state_path(pid))
        with open(_plan_path(pid), 'r', encoding='utf-8') as f:
            loaded = json.load(f)
        assert loaded == plan


# ---------------------------------------------------------------------------
# agent_next_task
# ---------------------------------------------------------------------------

class TestAgentNextTask:
    def test_no_plans_returns_error(self, temp_workspace):
        result = agent_next_task()
        assert 'error' in result

    def test_first_call_returns_first_ready_task(self, temp_workspace):
        set_active_profile('eu-mdr')
        agent_plan('g', 'design-validation-report', 'demo')
        result = agent_next_task()
        assert 'task' in result
        assert result['task']['kind'] == 'draft-section'
        assert result['task']['depends_on'] == []

    def test_after_task_done_advances(self, temp_workspace):
        set_active_profile('eu-mdr')
        agent_plan('g', 'design-validation-report', 'demo')
        first = agent_next_task()['task']
        agent_task_done(first['id'])
        second = agent_next_task()
        assert 'task' in second
        assert second['task']['id'] != first['id']
        assert second['task']['kind'] == 'draft-section'

    def test_dependency_blocks_task(self, temp_workspace):
        set_active_profile('eu-mdr')
        plan = agent_plan('g', 'design-validation-report', 'demo')
        section_ids = [t['id'] for t in plan['tasks'] if t['id'].startswith('section-')]
        # Mark all but one section done; assemble should still be blocked.
        for sid in section_ids[:-1]:
            agent_task_done(sid)
        nxt = agent_next_task()
        assert 'task' in nxt
        assert nxt['task']['id'] == section_ids[-1]
        # Now finish the last one; assemble should be next.
        agent_task_done(section_ids[-1])
        nxt2 = agent_next_task()
        assert 'task' in nxt2
        assert nxt2['task']['id'] == 'assemble-document'

    def test_all_done_returns_done_true(self, temp_workspace):
        set_active_profile('eu-mdr')
        plan = agent_plan('g', 'design-validation-report', 'demo')
        for t in plan['tasks']:
            agent_task_done(t['id'])
        result = agent_next_task()
        assert result.get('done') is True


# ---------------------------------------------------------------------------
# agent_task_done
# ---------------------------------------------------------------------------

class TestAgentTaskDone:
    def test_marks_task_done_in_state(self, temp_workspace):
        set_active_profile('eu-mdr')
        plan = agent_plan('g', 'design-validation-report', 'demo')
        agent_task_done('section-purpose-and-scope')
        state = _load_plan_state(plan['plan_id'])
        assert state['task_status']['section-purpose-and-scope'] == 'done'

    def test_invalid_task_id_returns_error(self, temp_workspace):
        set_active_profile('eu-mdr')
        agent_plan('g', 'design-validation-report', 'demo')
        result = agent_task_done('nonexistent-task')
        assert 'error' in result

    def test_idempotent(self, temp_workspace):
        set_active_profile('eu-mdr')
        plan = agent_plan('g', 'design-validation-report', 'demo')
        r1 = agent_task_done('section-context')
        r2 = agent_task_done('section-context')
        assert 'error' not in r1
        assert 'error' not in r2
        state = _load_plan_state(plan['plan_id'])
        assert state['task_status']['section-context'] == 'done'


# ---------------------------------------------------------------------------
# agent_show_plan / agent_list_plans
# ---------------------------------------------------------------------------

class TestAgentShowAndList:
    def test_show_returns_plan_and_state(self, temp_workspace):
        set_active_profile('eu-mdr')
        plan = agent_plan('g', 'design-validation-report', 'demo')
        result = agent_show_plan()
        assert 'plan' in result
        assert 'state' in result
        assert result['plan']['plan_id'] == plan['plan_id']

    def test_list_plans(self, temp_workspace):
        set_active_profile('eu-mdr')
        agent_plan('g1', 'design-validation-report', 'demo', plan_id='alpha-2026-04-09')
        agent_plan('g2', 'design-validation-report', 'demo', plan_id='beta-2026-04-09')
        plans = agent_list_plans()
        ids = [p['plan_id'] for p in plans]
        assert 'alpha-2026-04-09' in ids
        assert 'beta-2026-04-09' in ids
        for p in plans:
            assert '/' in p['progress']


# ---------------------------------------------------------------------------
# End to end CLI smoke
# ---------------------------------------------------------------------------

class TestEndToEndCLI:
    def test_draft_cli_help(self):
        repo_root = os.path.join(os.path.dirname(__file__), '..')
        env = os.environ.copy()
        env['PYTHONPATH'] = os.path.abspath(repo_root)
        r = subprocess.run(
            [sys.executable, '-m', 'mneme', 'draft', '--help'],
            cwd=repo_root, env=env, capture_output=True, text=True,
        )
        assert r.returncode == 0
        assert '--doc-type' in r.stdout
        assert '--section' in r.stdout

    def test_agent_plan_cli_smoke(self):
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        td = tempfile.mkdtemp(prefix='mneme-agent-cli-')
        try:
            ws = os.path.join(td, 'ws')
            env = os.environ.copy()
            env['PYTHONPATH'] = repo_root
            r1 = subprocess.run(
                [sys.executable, '-m', 'mneme', 'new', ws, '--client', 'demo'],
                cwd=repo_root, env=env, capture_output=True, text=True,
            )
            assert r1.returncode == 0, r1.stderr + r1.stdout
            r2 = subprocess.run(
                [sys.executable, '-m', 'mneme', '--workspace', ws,
                 'profile', 'set', 'eu-mdr'],
                cwd=repo_root, env=env, capture_output=True, text=True,
            )
            assert r2.returncode == 0, r2.stderr + r2.stdout
            r3 = subprocess.run(
                [sys.executable, '-m', 'mneme', '--workspace', ws,
                 'agent', 'plan', '--goal', 'test',
                 '--doc-type', 'design-validation-report',
                 '--client', 'demo'],
                cwd=repo_root, env=env, capture_output=True, text=True,
            )
            assert r3.returncode == 0, r3.stderr + r3.stdout
            assert 'Plan:' in r3.stdout
            assert 'Tasks: 15' in r3.stdout
        finally:
            shutil.rmtree(td, ignore_errors=True)
