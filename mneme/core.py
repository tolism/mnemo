"""
core.py - mneme core engine.

Structured markdown wiki with SQLite FTS5 search index.
Wiki pages are the source of truth; the search DB is a rebuildable index.

Usage:
    mneme sync
    mneme search "query here"
    mneme drift
    mneme stats
    mneme ingest path/to/file.md client-slug
"""

import argparse
import csv
from contextlib import contextmanager
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Cross-platform file locking
try:
    import portalocker
    _USE_PORTALOCKER = True
except ImportError:
    _USE_PORTALOCKER = False
    try:
        import fcntl
        _USE_FCNTL = True
    except ImportError:
        _USE_FCNTL = False


def _lock_file(fd, exclusive=True):
    """Acquire a file lock (cross-platform)."""
    if _USE_PORTALOCKER:
        portalocker.lock(fd, portalocker.LOCK_EX if exclusive else portalocker.LOCK_SH)
    elif _USE_FCNTL:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    # else: no locking available - proceed without (single-user mode)


def _unlock_file(fd):
    """Release a file lock (cross-platform)."""
    try:
        if _USE_PORTALOCKER:
            portalocker.unlock(fd)
        elif _USE_FCNTL:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass

from . import search as _search

from .config import (
    ACTIVE_PROFILE_FILE,
    BASE_DIR,
    ENTITY_STOPWORDS,
    EXCLUDED_DIRS,
    EXCLUDED_FILES,
    INDEX_FILE,
    LOG_FILE,
    LOG_MAX_ENTRIES,
    PROFILES_DIR,
    SCHEMA_DIR,
    SEARCH_DB,
    SOURCES_DIR,
    TEMPLATES_DIR,
    TRACEABILITY_FILE,
    WIKI_DIR,
    WORKSPACE_MAPPINGS_DIR,
    WORKSPACE_PROFILES_DIR,
)

# Lazy search DB connection (opened on first use)
_search_conn = None


def _get_search_db():
    """Return a connection to the FTS5 search index, opening it lazily."""
    global _search_conn
    if _search_conn is None:
        _search_conn = _search.open_db(SEARCH_DB)
    return _search_conn


# ---------------------------------------------------------------------------
# Progress indicator (zero-dep, TTY-aware)
# ---------------------------------------------------------------------------


class _ProgressBar:
    """
    Lightweight progress bar for long loops.

    TTY mode: single-line in-place update with percentage, count, ETA, and
    the current item label.

    Non-TTY mode: emits one line every max(1, total // 50) steps, preserving
    the pre-existing `[N/M] filename` log format so anything parsing stdout
    in CI keeps working.
    """

    def __init__(self, total: int, label: str = '',
                 stream=None, enabled: 'bool | None' = None,
                 width: int = 30):
        self.total = max(total, 0)
        self.done = 0
        self.label = label
        self.start = time.monotonic()
        self.stream = stream or sys.stdout
        if enabled is None:
            self.is_tty = bool(getattr(self.stream, 'isatty', lambda: False)())
        else:
            self.is_tty = enabled
        self.width = width
        self._last_tty_draw = 0.0
        # Step cadence for non-TTY mode.
        self._step = max(1, self.total // 50) if self.total else 1

    @staticmethod
    def _fmt_eta(seconds: float) -> str:
        if seconds < 0 or seconds == float('inf'):
            return '--:--'
        m, s = divmod(int(seconds), 60)
        return f'{m:02d}:{s:02d}'

    def _render_tty(self, current: str) -> None:
        pct = (self.done / self.total * 100) if self.total else 100.0
        filled = int(self.width * self.done / self.total) if self.total else self.width
        bar = '=' * filled + ('>' if filled < self.width else '') + ' ' * max(0, self.width - filled - 1)
        elapsed = time.monotonic() - self.start
        eta = (elapsed / self.done * (self.total - self.done)) if self.done else float('inf')
        line = f'\r[{bar}] {pct:5.1f}% ({self.done}/{self.total}) ETA {self._fmt_eta(eta)}'
        if current:
            # Truncate current-item label to fit terminal width
            try:
                term_cols = shutil.get_terminal_size((80, 20)).columns
            except Exception:
                term_cols = 80
            remaining = max(10, term_cols - len(line) - 3)
            cur = current
            if len(cur) > remaining:
                cur = '...' + cur[-(remaining - 3):]
            line += f' | {cur}'
        # Pad to avoid leftover chars from prior longer lines
        try:
            term_cols = shutil.get_terminal_size((80, 20)).columns
            if len(line) < term_cols:
                line += ' ' * (term_cols - len(line))
        except Exception:
            pass
        self.stream.write(line)
        self.stream.flush()

    def update(self, n: int = 1, current: str = '') -> None:
        self.done += n
        if self.is_tty:
            now = time.monotonic()
            # Rate-limit redraws to ~10 Hz for cheap loops
            if now - self._last_tty_draw >= 0.1 or self.done >= self.total:
                self._render_tty(current)
                self._last_tty_draw = now
        else:
            # Periodic line-mode output
            if self.total == 0 or self.done % self._step == 0 or self.done >= self.total:
                label = f' {self.label}' if self.label else ''
                if current:
                    print(f'  [{self.done}/{self.total}]{label} {current}', file=self.stream)
                else:
                    print(f'  [{self.done}/{self.total}]{label}', file=self.stream)

    def log(self, msg: str) -> None:
        """Print a message without corrupting the bar."""
        if self.is_tty:
            # Clear current line, print, redraw on next update
            self.stream.write('\r' + ' ' * 100 + '\r')
            self.stream.flush()
            print(msg, file=self.stream)
            # Redraw bar below
            self._render_tty('')
        else:
            print(msg, file=self.stream)

    def finish(self) -> None:
        if self.is_tty:
            self._render_tty('')
            self.stream.write('\n')
            self.stream.flush()


# ---------------------------------------------------------------------------
# Frontmatter parsing (no PyYAML dependency)
# ---------------------------------------------------------------------------

def parse_frontmatter(content: str) -> tuple[dict, str]:
    """
    Split markdown content into (frontmatter_dict, body).
    Returns ({}, content) if no frontmatter markers found.
    Simple regex split on '---' markers. Handles basic YAML scalars and lists.
    """
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', content, re.DOTALL)
    if not match:
        return {}, content

    raw_yaml = match.group(1)
    body = match.group(2)
    frontmatter = _parse_simple_yaml(raw_yaml)
    return frontmatter, body


def _parse_simple_yaml(text: str) -> dict:
    """
    Parse a limited subset of YAML: scalar key-value pairs and simple lists.
    Handles:
        key: value
        key:
          - item1
          - item2
    Does not handle nested dicts or multiline scalars (not needed for wiki frontmatter).
    """
    result = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        # Skip blank lines and comments
        if not line.strip() or line.strip().startswith('#'):
            i += 1
            continue
        # key: value or key: (start of list)
        # Use split(':', 1) so values containing colons are preserved intact.
        if ':' not in line:
            i += 1
            continue
        raw_key, raw_val = line.split(':', 1)
        raw_key = raw_key.strip()
        # Key must be a valid YAML identifier (word chars + hyphens)
        if not re.match(r'^\w[\w\-]*$', raw_key):
            i += 1
            continue
        key = raw_key
        val = raw_val.strip()
        if val == '':
            # Possibly a list on following lines
            items = []
            i += 1
            while i < len(lines) and re.match(r'^\s+-\s+', lines[i]):
                item = re.sub(r'^\s+-\s+', '', lines[i]).strip().strip('"').strip("'")
                items.append(item)
                i += 1
            result[key] = items
        else:
            # Strip inline quotes
            val = val.strip('"').strip("'")
            result[key] = val
            i += 1
    return result


# ---------------------------------------------------------------------------
# (Chunking removed — FTS5 indexes whole pages, no frame splitting needed)
# ---------------------------------------------------------------------------


def _content_hash(content: str) -> str:
    """Return MD5 hex digest of content string."""
    return hashlib.md5(content.encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# Core engine functions
# ---------------------------------------------------------------------------

def sync_page_to_index(wiki_page_path: str, client_slug: str = None) -> bool:
    """
    Read a wiki page and upsert it into the FTS5 search index.

    Returns True if the page was indexed (content changed), False if skipped.
    """
    with open(wiki_page_path, 'r', encoding='utf-8') as f:
        content = f.read()

    content_hash = _content_hash(content)
    frontmatter, body = parse_frontmatter(content)

    title = frontmatter.get('title', os.path.basename(wiki_page_path))
    client = client_slug or frontmatter.get('client', '_unknown')

    tags_raw = frontmatter.get('tags', [])
    if isinstance(tags_raw, str):
        tags_raw = [t.strip() for t in tags_raw.split(',') if t.strip()]
    else:
        tags_raw = list(tags_raw)
    if client and client not in tags_raw:
        tags_raw.append(client)
    tags_str = ', '.join(tags_raw)

    try:
        rel_path = os.path.relpath(wiki_page_path, WIKI_DIR)
    except ValueError:
        rel_path = wiki_page_path
    wiki_path = rel_path.replace(os.sep, '/')

    conn = _get_search_db()
    indexed = _search.upsert_page(conn, wiki_path, client, title, tags_str,
                                  body, content_hash)

    if indexed:
        _update_tags_schema(wiki_page_path, frontmatter)

    return indexed


def sync_all_pages() -> dict:
    """
    Glob all wiki pages (excluding _templates/), sync each to the FTS5 index.
    Returns a summary dict with total_pages, total_indexed, per_client breakdown.
    """
    pattern = os.path.join(WIKI_DIR, '**', '*.md')
    all_pages = glob.glob(pattern, recursive=True)

    pages_to_sync = []
    for page in all_pages:
        rel = os.path.relpath(page, WIKI_DIR)
        parts = Path(rel).parts
        excluded = False
        for part in parts[:-1]:
            if part in EXCLUDED_DIRS:
                excluded = True
                break
        if os.path.basename(page) in EXCLUDED_FILES:
            excluded = True
        if not excluded:
            pages_to_sync.append(page)

    total_pages = 0
    total_indexed = 0
    per_client: dict[str, int] = {}
    errors: list[str] = []

    for page in pages_to_sync:
        rel = os.path.relpath(page, WIKI_DIR)
        parts = Path(rel).parts
        client_slug = parts[0] if len(parts) > 1 else '_unknown'

        try:
            indexed = sync_page_to_index(page, client_slug=client_slug)
            total_pages += 1
            if indexed:
                total_indexed += 1
                per_client[client_slug] = per_client.get(client_slug, 0) + 1
        except Exception as e:
            errors.append(f'{page}: {e}')

    return {
        'total_pages': total_pages,
        'total_indexed': total_indexed,
        'per_client': per_client,
        'errors': errors,
    }
def _search_wiki_text(query: str, k: int = 10, client: str = None) -> list[dict]:
    """
    Raw FTS5 search over wiki pages.

    Returns a list of result dicts (text, title, wiki_path, score, tags,
    client, layer). Used internally by dual_search and by callers that
    want wiki-layer hits without the 'source' annotation.
    """
    conn = _get_search_db()
    return _search.search(conn, query, k=k, client=client)


def dual_search(query: str, k: int = 10, client: str = None) -> list[dict]:
    """
    Search the wiki via FTS5 with BM25 ranking.

    Returns up to *k* results as dicts with:
        text, title, source, score, tags, wiki_path, layer
    """
    results = _search_wiki_text(query, k=k, client=client)

    for r in results:
        r['source'] = f'wiki: {r["wiki_path"]}'

    return results


def check_drift() -> dict:
    """
    Compare wiki pages on disk against the FTS5 search index.

    Reports: unindexed (on disk but not in DB), orphaned (in DB but not on
    disk), stale (hash mismatch).
    """
    # Build set of wiki pages on disk with their hashes
    pattern = os.path.join(WIKI_DIR, '**', '*.md')
    wiki_pages = glob.glob(pattern, recursive=True)

    disk_pages: dict[str, str] = {}  # wiki_path -> content_hash
    for page in wiki_pages:
        rel = os.path.relpath(page, WIKI_DIR)
        parts = Path(rel).parts
        if any(p in EXCLUDED_DIRS for p in parts[:-1]):
            continue
        if os.path.basename(page) in EXCLUDED_FILES:
            continue
        wiki_path = rel.replace(os.sep, '/')
        try:
            with open(page, 'r', encoding='utf-8') as f:
                content = f.read()
            disk_pages[wiki_path] = _content_hash(content)
        except Exception:
            continue

    # Get indexed pages from the DB
    conn = _get_search_db()
    indexed_pages = _search.get_indexed_pages(conn)

    disk_set = set(disk_pages.keys())
    index_set = set(indexed_pages.keys())

    unindexed = sorted(disk_set - index_set)
    orphaned = sorted(index_set - disk_set)
    stale = sorted(
        p for p in disk_set & index_set
        if disk_pages[p] != indexed_pages[p]
    )

    total_wiki = len(disk_pages)
    total_indexed = len(indexed_pages)
    synced = len(disk_set & index_set) - len(stale)
    sync_pct = round(100 * synced / total_wiki, 1) if total_wiki else 0.0
    is_drifted = bool(unindexed or orphaned or stale)

    return {
        'unindexed': unindexed,
        'orphaned': orphaned,
        'stale': stale,
        'is_drifted': is_drifted,
        'summary': {
            'total_wiki_pages': total_wiki,
            'total_indexed': total_indexed,
            'synced': synced,
            'sync_pct': sync_pct,
            'unindexed': len(unindexed),
            'orphaned': len(orphaned),
            'stale': len(stale),
        },
    }


def _slugify_subpath_segment(seg: str) -> str:
    """Normalise a directory name into a wiki-safe segment."""
    seg = re.sub(r'[^\w\-]', '-', seg).lower().strip('-')
    seg = re.sub(r'-+', '-', seg)
    return seg


def _auto_detect_subpath(source_path: str, client_slug: str) -> str:
    """
    Derive a wiki subpath automatically from a source file's location under
    sources/<client>/. Used by resync so nested pages can be located without
    the caller passing --preserve-structure again.

    Returns '' if the source is not under sources/<client>/, or directly in it.
    """
    src_client_root = os.path.join(SOURCES_DIR, client_slug)
    try:
        abs_src = os.path.abspath(source_path)
        abs_root = os.path.abspath(src_client_root)
        if not abs_src.startswith(abs_root + os.sep):
            return ''
        rel = os.path.relpath(os.path.dirname(abs_src), abs_root)
        if rel in ('', '.'):
            return ''
        segs = [_slugify_subpath_segment(s) for s in rel.split(os.sep) if s]
        return os.path.join(*segs) if segs else ''
    except ValueError:
        return ''


def ingest_source_to_both(source_path: str, client_slug: str, force: bool = False,
                          subpath: str = '') -> dict:
    """
    Atomic ingest operation. Takes a raw source file, writes it to the wiki,
    syncs to search index, updates schema/entities.json and index.md, appends to log.md.

    Handles .md and .txt. PDF requires pymupdf.
    Returns a summary of what was created.

    ``subpath`` (optional): wiki subdirectory under ``wiki/<client>/`` for the
    new page. Used by ``ingest-dir --preserve-structure`` and by ``resync``
    (auto-detected from the source's location under ``sources/<client>/``).
    """
    if not os.path.exists(source_path):
        raise FileNotFoundError(f'Source not found: {source_path}')

    # Check for duplicate ingest (matches INGEST_STARTED and INGEST_COMPLETE)
    source_filename = os.path.basename(source_path)
    # Use relative path for dedup so files with the same basename in different
    # directories are ingested independently (suggestion #3).
    try:
        source_rel_path = os.path.relpath(source_path, BASE_DIR)
    except ValueError:
        source_rel_path = source_path
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r') as f:
            log_text = f.read()
            # Check both relative path (new) and basename (backwards compat)
            if f'INGEST | {source_rel_path}' in log_text or f'Source: {source_path}' in log_text:
                # BUG-005 fix: only print "Skipping" when force is False
                if not force:
                    print(f'Warning: {source_filename} was previously ingested. Skipping. Use --force to re-ingest.')
                    return {}
                else:
                    print(f'[mneme] Re-ingesting {source_filename} (--force)')

    _, ext = os.path.splitext(source_path)
    ext = ext.lower()

    # Read content
    if ext in ('.md', '.txt'):
        with open(source_path, 'r', encoding='utf-8') as f:
            raw_content = f.read()
    elif ext == '.pdf':
        try:
            import fitz  # pymupdf
            doc = fitz.open(source_path)
            raw_content = '\n\n'.join(page.get_text() for page in doc)
            doc.close()
        except ImportError:
            raise ValueError(
                'PDF extraction requires pymupdf. Install: pip install pymupdf'
            )
    elif ext == '.xlsx':
        try:
            import openpyxl
            wb = openpyxl.load_workbook(source_path, read_only=True, data_only=True)
            sheets = []
            for ws in wb.worksheets:
                rows = list(ws.iter_rows(values_only=True))
                if not rows:
                    continue
                headers = [str(h or '').strip() for h in rows[0]]
                sheet_lines = [f'## {ws.title}\n']
                sheet_lines.append('| ' + ' | '.join(headers) + ' |')
                sheet_lines.append('| ' + ' | '.join(['---'] * len(headers)) + ' |')
                for row in rows[1:]:
                    cells = [str(c or '').strip() for c in row]
                    sheet_lines.append('| ' + ' | '.join(cells) + ' |')
                sheets.append('\n'.join(sheet_lines))
            wb.close()
            raw_content = '\n\n'.join(sheets)
        except ImportError:
            raise ValueError(
                'Excel extraction requires openpyxl. Install: pip install "mneme-cli[xlsx]"'
            )
    else:
        # Generic text fallback
        try:
            with open(source_path, 'r', encoding='utf-8', errors='replace') as f:
                raw_content = f.read()
        except Exception as e:
            raise ValueError(f'Could not read file: {e}')

    source_filename = os.path.basename(source_path)
    today = datetime.now().strftime('%Y-%m-%d')
    page_slug = re.sub(r'[^\w\-]', '-', os.path.splitext(source_filename)[0]).lower()
    page_slug = re.sub(r'-+', '-', page_slug).strip('-')

    # Target wiki directory (optionally mirroring source sub-structure)
    if subpath:
        safe_segs = [_slugify_subpath_segment(s) for s in subpath.split(os.sep) if s]
        safe_subpath = os.path.join(*safe_segs) if safe_segs else ''
    else:
        safe_subpath = ''
    client_wiki_dir = os.path.join(WIKI_DIR, client_slug, safe_subpath) if safe_subpath else os.path.join(WIKI_DIR, client_slug)
    os.makedirs(client_wiki_dir, exist_ok=True)

    wiki_page_path = os.path.join(client_wiki_dir, f'{page_slug}.md')

    # Check if a page already exists - update vs create
    if os.path.exists(wiki_page_path):
        # Update: read existing, append new content under a new section
        with open(wiki_page_path, 'r', encoding='utf-8') as f:
            existing = f.read()
        existing_fm, existing_body = parse_frontmatter(existing)

        # Update frontmatter dates and sources
        sources = existing_fm.get('sources', [])
        if isinstance(sources, str):
            sources = [sources]
        rel_source = os.path.relpath(source_path, BASE_DIR)
        if rel_source not in sources:
            sources.append(rel_source)

        new_page = _build_wiki_page(
            title=existing_fm.get('title', page_slug),
            client=client_slug,
            sources=sources,
            tags=existing_fm.get('tags', [client_slug]),
            created=existing_fm.get('created', today),
            updated=today,
            confidence=existing_fm.get('confidence', 'medium'),
            body=existing_body + f'\n\n## Update - {today}\n\n{raw_content}',
        )
        action = 'updated'
    else:
        # Create new page
        rel_source = os.path.relpath(source_path, BASE_DIR)
        new_page = _build_wiki_page(
            title=_title_from_slug(page_slug),
            client=client_slug,
            sources=[rel_source],
            tags=[client_slug],
            created=today,
            updated=today,
            confidence='medium',
            body=_build_default_body(raw_content),
        )
        action = 'created'

    with open(wiki_page_path, 'w', encoding='utf-8') as f:
        f.write(new_page)

    # Snapshot the just-written wiki page as the baseline for future resyncs.
    # This is the "ancestor" for a 3-way merge in resync_source().
    _write_baseline(wiki_page_path, new_page)

    rel_wiki_path = os.path.relpath(wiki_page_path, BASE_DIR)

    # Write INGEST_STARTED log immediately after wiki page write.
    # The duplicate-ingest guard reads log.md, so logging first ensures a crash
    # between wiki write and search index sync is detectable on subsequent runs.
    _append_log(
        operation='INGEST',
        description=f'{source_rel_path} -> {client_slug}/{page_slug}.md ({action}) [INGEST_STARTED]',
        details=[
            f'Source: {source_path}',
            f'Wiki page: {action} at {rel_wiki_path}',
        ],
        date=today,
    )

    # Sync the wiki page to search index
    indexed = sync_page_to_index(wiki_page_path, client_slug=client_slug)

    # Update schema/entities.json with any capitalized entity mentions
    entities_updated = _update_entities_schema(client_slug, wiki_page_path, raw_content, today)

    # Update schema/tags.json from the wiki page frontmatter (BUG-006 fix)
    with open(wiki_page_path, 'r', encoding='utf-8') as _f:
        _page_content = _f.read()
        _page_fm, _ = parse_frontmatter(_page_content)
    _update_tags_schema(wiki_page_path, _page_fm)

    # Update schema/graph.json with nodes and edges (suggestion #18)
    _update_graph_schema(client_slug, wiki_page_path, _page_content, _page_fm, today)

    # Update index.md
    _update_index(client_slug, page_slug, rel_wiki_path, today)

    # Append completion log entry
    _append_log(
        operation='INGEST',
        description=f'{source_rel_path} -> {client_slug}/{page_slug}.md ({action}) [INGEST_COMPLETE]',
        details=[
            f'Source: {source_path}',
            f'Wiki page: {action} at {rel_wiki_path}',
            f'Indexed: {indexed}',
            f'Entities updated: {entities_updated}',
        ],
        date=today,
    )

    return {
        'wiki_page': rel_wiki_path,
        'action': action,
        'indexed': indexed,
        'entities_updated': entities_updated,
        'client': client_slug,
        'source': source_path,
    }


# ---------------------------------------------------------------------------
# Resync (3-way merge of an updated source against the wiki + baseline)
# ---------------------------------------------------------------------------

def _baseline_dir_for(client_wiki_dir: str) -> str:
    """Return the .baselines/ directory for a client wiki directory."""
    return os.path.join(client_wiki_dir, '.baselines')


def _baseline_path(wiki_page_path: str) -> str:
    """
    Return the baseline sidecar path for a wiki page.

    wiki/{client}/page.md  ->  wiki/{client}/.baselines/page.md
    """
    parent = os.path.dirname(wiki_page_path)
    fname = os.path.basename(wiki_page_path)
    return os.path.join(_baseline_dir_for(parent), fname)


def _write_baseline(wiki_page_path: str, content: str) -> None:
    """Save the baseline (ancestor) snapshot of a wiki page after a clean ingest."""
    baseline_path = _baseline_path(wiki_page_path)
    os.makedirs(os.path.dirname(baseline_path), exist_ok=True)
    with open(baseline_path, 'w', encoding='utf-8') as f:
        f.write(content)


def _read_baseline(wiki_page_path: str) -> Optional[str]:
    """Read the baseline content for a wiki page, or None if no baseline exists."""
    baseline_path = _baseline_path(wiki_page_path)
    if not os.path.exists(baseline_path):
        return None
    with open(baseline_path, 'r', encoding='utf-8') as f:
        return f.read()


def _git_merge_file(ours: str, ancestor: str, theirs: str) -> tuple[str, bool]:
    """
    Run a 3-way merge using `git merge-file -p`.

    `git merge-file` is part of git core; it operates on three files passed
    by path and writes the merged result to stdout. It does not require the
    files to be inside a git repo.

    Args:
        ours:     content of the current wiki page (possibly hand-edited)
        ancestor: content of the wiki page right after the last clean ingest
        theirs:   content of the wiki page that a fresh ingest of the new
                  source would produce

    Returns:
        (merged_text, had_conflicts)
        - merged_text contains <<<<<<< / ======= / >>>>>>> markers if conflicts
        - had_conflicts is True iff git merge-file exited with status 1

    Raises:
        FileNotFoundError if `git` is not on PATH.
        RuntimeError      if git merge-file exits with an unexpected error.
    """
    import subprocess
    import tempfile

    fds = []
    try:
        ours_fd, ours_path = tempfile.mkstemp(suffix='.ours.md', text=True)
        ancestor_fd, ancestor_path = tempfile.mkstemp(suffix='.base.md', text=True)
        theirs_fd, theirs_path = tempfile.mkstemp(suffix='.theirs.md', text=True)
        fds = [(ours_fd, ours_path), (ancestor_fd, ancestor_path), (theirs_fd, theirs_path)]

        for fd, _ in fds:
            os.close(fd)
        with open(ours_path, 'w', encoding='utf-8', newline='') as f:
            f.write(ours)
        with open(ancestor_path, 'w', encoding='utf-8', newline='') as f:
            f.write(ancestor)
        with open(theirs_path, 'w', encoding='utf-8', newline='') as f:
            f.write(theirs)

        result = subprocess.run(
            ['git', 'merge-file', '-p',
             '--marker-size=7',
             '-L', 'current (ours)',
             '-L', 'baseline (ancestor)',
             '-L', 'incoming (theirs)',
             ours_path, ancestor_path, theirs_path],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout, False
        if result.returncode == 1:
            return result.stdout, True
        raise RuntimeError(
            f'git merge-file failed (exit {result.returncode}): {result.stderr.strip()}'
        )
    finally:
        for _, path in fds:
            try:
                os.remove(path)
            except OSError:
                pass


def _build_wiki_page_from_source(source_path: str, client_slug: str, today: str) -> tuple[str, str, str]:
    """
    Read a source file and produce the wiki page string a fresh ingest would
    create. Used by resync_source() to compute "theirs" without touching disk.

    Returns (page_slug, raw_content, wiki_page_text).
    """
    _, ext = os.path.splitext(source_path)
    ext = ext.lower()
    if ext in ('.md', '.txt'):
        with open(source_path, 'r', encoding='utf-8') as f:
            raw_content = f.read()
    elif ext == '.pdf':
        try:
            import fitz
            doc = fitz.open(source_path)
            raw_content = '\n\n'.join(page.get_text() for page in doc)
            doc.close()
        except ImportError:
            raise ValueError('PDF extraction requires pymupdf. Install: pip install pymupdf')
    else:
        with open(source_path, 'r', encoding='utf-8', errors='replace') as f:
            raw_content = f.read()

    source_filename = os.path.basename(source_path)
    page_slug = re.sub(r'[^\w\-]', '-', os.path.splitext(source_filename)[0]).lower()
    page_slug = re.sub(r'-+', '-', page_slug).strip('-')

    rel_source = os.path.relpath(source_path, BASE_DIR)
    wiki_page_text = _build_wiki_page(
        title=_title_from_slug(page_slug),
        client=client_slug,
        sources=[rel_source],
        tags=[client_slug],
        created=today,
        updated=today,
        confidence='medium',
        body=_build_default_body(raw_content),
    )
    return page_slug, raw_content, wiki_page_text


def resync_source(source_path: str, client_slug: str, dry_run: bool = False) -> dict:
    """
    Diff-aware re-ingest. Treats the supplied file as an updated version of a
    previously ingested source and merges it into the existing wiki page using
    `git merge-file` (3-way merge: ours = current page, ancestor = baseline,
    theirs = what a fresh ingest of the new file would produce).

    If no baseline exists for the page, falls through to a fresh ingest.

    Returns a dict describing what happened, including a 'conflicts' flag.
    """
    if not os.path.exists(source_path):
        raise FileNotFoundError(f'Source not found: {source_path}')

    today = datetime.now().strftime('%Y-%m-%d')

    # Auto-detect subpath from the source's location under sources/<client>/
    # so a source ingested with --preserve-structure resyncs to the correct
    # nested wiki page rather than creating a duplicate flat page.
    subpath = _auto_detect_subpath(source_path, client_slug)
    if subpath:
        client_wiki_dir = os.path.join(WIKI_DIR, client_slug, subpath)
    else:
        client_wiki_dir = os.path.join(WIKI_DIR, client_slug)
    page_slug, raw_content, theirs_text = _build_wiki_page_from_source(
        source_path, client_slug, today
    )
    wiki_page_path = os.path.join(client_wiki_dir, f'{page_slug}.md')

    # No prior page or baseline -> fall through to a regular ingest.
    baseline = _read_baseline(wiki_page_path) if os.path.exists(wiki_page_path) else None
    if baseline is None:
        if dry_run:
            return {
                'action': 'would-ingest-fresh',
                'wiki_page': os.path.relpath(wiki_page_path, BASE_DIR),
                'reason': 'no baseline found - resync would perform a fresh ingest',
                'conflicts': False,
            }
        result = ingest_source_to_both(source_path, client_slug, force=True, subpath=subpath)
        result['action'] = f'fresh-{result.get("action", "created")}'
        result['conflicts'] = False
        return result

    with open(wiki_page_path, 'r', encoding='utf-8') as f:
        ours_text = f.read()

    # Fast paths
    if ours_text == theirs_text:
        return {
            'action': 'noop',
            'wiki_page': os.path.relpath(wiki_page_path, BASE_DIR),
            'reason': 'incoming source produces an identical wiki page',
            'conflicts': False,
        }

    try:
        merged_text, had_conflicts = _git_merge_file(ours_text, baseline, theirs_text)
    except FileNotFoundError:
        raise RuntimeError(
            'git is not on PATH. `mneme resync` requires git for 3-way merge. '
            'Install git or use `mneme ingest --force` to overwrite the page.'
        )

    rel_wiki_path = os.path.relpath(wiki_page_path, BASE_DIR)
    rel_source = os.path.relpath(source_path, BASE_DIR)

    if dry_run:
        return {
            'action': 'would-merge-conflict' if had_conflicts else 'would-merge-clean',
            'wiki_page': rel_wiki_path,
            'baseline_hash': _content_hash(baseline),
            'ours_hash': _content_hash(ours_text),
            'theirs_hash': _content_hash(theirs_text),
            'merged_hash': _content_hash(merged_text),
            'conflicts': had_conflicts,
            'preview': merged_text[:1200],
        }

    # Persist merged content
    with open(wiki_page_path, 'w', encoding='utf-8') as f:
        f.write(merged_text)

    # Update baseline to "theirs" so the next resync diffs against the new
    # source, not the original. Conflict regions are part of the merged file
    # but the baseline always reflects the latest clean source ingestion.
    _write_baseline(wiki_page_path, theirs_text)

    # Re-derive schema from the merged content (only if no conflicts; with
    # conflicts the file contains markers and entity extraction would be noisy)
    indexed = False
    entities_updated = 0
    if not had_conflicts:
        indexed = sync_page_to_index(wiki_page_path, client_slug=client_slug)
        entities_updated = _update_entities_schema(client_slug, wiki_page_path, merged_text, today)
        with open(wiki_page_path, 'r', encoding='utf-8') as _f:
            _page_fm, _ = parse_frontmatter(_f.read())
        _update_tags_schema(wiki_page_path, _page_fm)

    _update_index(client_slug, page_slug, rel_wiki_path, today)

    op = 'RESYNC-CONFLICT' if had_conflicts else 'RESYNC'
    _append_log(
        operation=op,
        description=f'{os.path.basename(source_path)} -> {client_slug}/{page_slug}.md',
        details=[
            f'Source: {rel_source}',
            f'Wiki page: merged at {rel_wiki_path}',
            f'Conflicts: {"yes" if had_conflicts else "no"}',
            f'Indexed: {indexed}',
            f'Entities updated: {entities_updated}',
        ],
        date=today,
    )

    return {
        'action': 'merge-conflict' if had_conflicts else 'merge-clean',
        'wiki_page': rel_wiki_path,
        'conflicts': had_conflicts,
        'indexed': indexed,
        'entities_updated': entities_updated,
        'client': client_slug,
        'source': source_path,
    }


def resync_resolve(page_ref: str) -> dict:
    """
    Mark a previously-conflicted resync as resolved.

    The user has hand-edited the conflict markers out of the wiki page.
    This function:
      1. Verifies no merge markers remain.
      2. Re-extracts schema (entities, tags) from the cleaned page.
      3. Updates search index.
      4. Logs RESYNC-RESOLVED.

    Args:
        page_ref: a path like "cardio-monitor/risk-register" (no .md extension)
                  or a full path to a wiki page file.
    """
    if os.path.isabs(page_ref) and os.path.exists(page_ref):
        wiki_page_path = page_ref
    else:
        ref = page_ref
        if not ref.endswith('.md'):
            ref += '.md'
        wiki_page_path = os.path.join(WIKI_DIR, ref)

    if not os.path.exists(wiki_page_path):
        raise FileNotFoundError(f'Wiki page not found: {wiki_page_path}')

    with open(wiki_page_path, 'r', encoding='utf-8') as f:
        content = f.read()

    if '<<<<<<<' in content or '>>>>>>>' in content or '=======' in content:
        raise ValueError(
            'Wiki page still contains merge conflict markers. '
            'Edit the file to remove all <<<<<<<, =======, and >>>>>>> lines first.'
        )

    today = datetime.now().strftime('%Y-%m-%d')
    parts = os.path.relpath(wiki_page_path, WIKI_DIR).replace('\\', '/').split('/')
    client_slug = parts[0]
    page_slug = os.path.splitext(parts[-1])[0]

    indexed = sync_page_to_index(wiki_page_path, client_slug=client_slug)
    entities_updated = _update_entities_schema(client_slug, wiki_page_path, content, today)
    fm, _ = parse_frontmatter(content)
    _update_tags_schema(wiki_page_path, fm)
    _update_index(client_slug, page_slug, os.path.relpath(wiki_page_path, BASE_DIR), today)

    # Bless this state as the new baseline so the next resync starts fresh.
    _write_baseline(wiki_page_path, content)

    _append_log(
        operation='RESYNC-RESOLVED',
        description=f'{client_slug}/{page_slug}.md - merge conflicts resolved by user',
        details=[
            f'Wiki page: {os.path.relpath(wiki_page_path, BASE_DIR)}',
            f'Indexed: {indexed}',
            f'Entities updated: {entities_updated}',
        ],
        date=today,
    )

    return {
        'action': 'resolved',
        'wiki_page': os.path.relpath(wiki_page_path, BASE_DIR),
        'indexed': indexed,
        'entities_updated': entities_updated,
    }


def get_stats() -> dict:
    """
    Gather stats from wiki, search index, and schema layers.
    Returns a structured dict with counts, sizes, and drift status.
    """
    # --- Wiki stats ---
    pattern = os.path.join(WIKI_DIR, '**', '*.md')
    all_wiki = glob.glob(pattern, recursive=True)
    wiki_by_client: dict[str, int] = {}
    total_cross_refs = 0

    for page in all_wiki:
        rel = os.path.relpath(page, WIKI_DIR)
        parts = Path(rel).parts
        if any(p in EXCLUDED_DIRS for p in parts[:-1]):
            continue
        client = parts[0] if len(parts) > 1 else '_root'
        wiki_by_client[client] = wiki_by_client.get(client, 0) + 1
        try:
            with open(page, 'r', encoding='utf-8') as f:
                content = f.read()
            total_cross_refs += len(re.findall(r'\[\[.+?\]\]', content))
        except Exception as e:
            print(f'[mneme] Warning: failed to read schema file: {e}', file=sys.stderr)

    total_wiki_pages = sum(wiki_by_client.values())

    # --- Search index stats ---
    search_stats: dict = {}
    try:
        conn = _get_search_db()
        search_stats = _search.get_stats(conn, db_path=SEARCH_DB)
        t0 = time.time()
        _search.search(conn, 'test', k=1)
        search_stats['search_latency_ms'] = round((time.time() - t0) * 1000, 1)
    except Exception as e:
        search_stats = {'error': str(e)}

    # --- Schema stats ---
    schema_stats: dict = {}
    try:
        entities_path = os.path.join(SCHEMA_DIR, 'entities.json')
        graph_path = os.path.join(SCHEMA_DIR, 'graph.json')
        tags_path = os.path.join(SCHEMA_DIR, 'tags.json')

        entity_count = 0
        if os.path.exists(entities_path):
            with open(entities_path, 'r') as f:
                data = json.load(f)
            entities = data.get('entities', data) if isinstance(data, dict) else data
            entity_count = len(entities) if isinstance(entities, list) else 0

        rel_count = 0
        if os.path.exists(graph_path):
            with open(graph_path, 'r') as f:
                data = json.load(f)
            edges = data.get('edges', []) if isinstance(data, dict) else data
            rel_count = len(edges)

        # Also count trace links from traceability.json (suggestion #21)
        trace_count = 0
        if os.path.exists(TRACEABILITY_FILE):
            with open(TRACEABILITY_FILE, 'r') as f:
                tdata = json.load(f)
            trace_links = tdata.get('links', []) if isinstance(tdata, dict) else tdata
            trace_count = len(trace_links) if isinstance(trace_links, list) else 0
        rel_count += trace_count

        tag_count = 0
        if os.path.exists(tags_path):
            with open(tags_path, 'r') as f:
                data = json.load(f)
            tags = data.get('tags', data) if isinstance(data, dict) else data
            tag_count = len(tags)

        schema_stats = {
            'entity_count': entity_count,
            'relationship_count': rel_count,
            'tag_count': tag_count,
        }
    except Exception as e:
        schema_stats = {'error': str(e)}

    # --- Drift quick check ---
    # Run after search index section completes.
    drift_synced = 'unknown'
    try:
        drift = check_drift()
        summary = drift.get('summary', {})
        drift_synced = f"{summary.get('sync_pct', 0)}% ({summary.get('synced', 0)}/{summary.get('total_wiki_pages', 0)} pages)"
    except Exception as e:
        drift_synced = f'unavailable ({e})'

    return {
        'wiki': {
            'total_pages': total_wiki_pages,
            'by_client': wiki_by_client,
            'total_cross_references': total_cross_refs,
        },
        'search': search_stats,
        'schema': schema_stats,
        'drift': {
            'sync_status': drift_synced,
        },
    }


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _locked_read_modify_write(filepath: str, modifier_fn) -> None:
    """Read-modify-write with exclusive file lock to prevent concurrent corruption."""
    os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else '.', exist_ok=True)

    # Create file if it doesn't exist
    if not os.path.exists(filepath):
        with open(filepath, 'w') as f:
            f.write('')

    with open(filepath, 'r+') as f:
        _lock_file(f, exclusive=True)
        try:
            content = f.read()
            new_content = modifier_fn(content)
            f.seek(0)
            f.truncate()
            f.write(new_content)
        finally:
            _unlock_file(f)


def _build_wiki_page(
    title: str,
    client: str,
    sources: list,
    tags: list,
    created: str,
    updated: str,
    confidence: str,
    body: str,
) -> str:
    """Build a wiki page string with proper frontmatter."""
    sources_yaml = '\n'.join(f'  - {s}' for s in sources) if sources else '  []'
    tags_yaml = '\n'.join(f'  - {t}' for t in tags) if tags else '  []'

    return f"""---
title: {title}
type: source-summary
client: {client}
sources:
{sources_yaml}
tags:
{tags_yaml}
related: []
created: {created}
updated: {updated}
confidence: {confidence}
---

{body}
"""


def _build_default_body(raw_content: str) -> str:
    """Build a basic wiki page body from raw source content."""
    stripped = raw_content.strip()
    if not stripped:
        # Nothing to put in the body - return empty.
        return ''

    return f"""## Summary

Source ingested via mneme. Review and expand with citations.

## Content

{stripped}

## Open Questions

- What entities need pages?
- What relationships should be added to schema/graph.json?

## Cross-References

"""


def _title_from_slug(slug: str) -> str:
    """Convert a kebab-case or snake_case slug to a Title Case string."""
    # Normalise both hyphens and underscores as word separators
    normalised = re.sub(r'[-_]+', ' ', slug)
    return ' '.join(w.capitalize() for w in normalised.split())


def _update_entities_schema(client_slug: str, wiki_page_path: str, content: str, today: str) -> int:
    """
    Scan content for capitalized multi-word phrases (rough entity detection).
    Add any new entities to schema/entities.json.
    Returns count of new entities added.

    Note: entity type classification and acronym extraction are left to the
    LLM agent, which has the context to classify correctly. This function
    only does mechanical proper-noun detection.
    """
    entities_path = os.path.join(SCHEMA_DIR, 'entities.json')
    os.makedirs(SCHEMA_DIR, exist_ok=True)

    # Detect capitalized proper nouns (2+ consecutive capitalized words on the same line).
    # Restrict to single lines so heading fragments like "Title\n\nNext" are never captured.
    # Strip markdown heading markers before matching so headings are not captured as entities.
    found = []
    for line in content.splitlines():
        stripped_line = re.sub(r'^#+\s*', '', line)
        found.extend(re.findall(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b', stripped_line))
    unique_names = list(dict.fromkeys(found))  # preserve order, deduplicate

    rel_wiki = os.path.relpath(wiki_page_path, os.path.join(BASE_DIR, 'wiki'))
    rel_wiki_no_ext = os.path.splitext(rel_wiki)[0]

    added_count = 0

    def modifier(raw: str) -> str:
        nonlocal added_count
        # Parse existing data
        if raw.strip():
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                print(f'[mneme] Warning: entities.json was corrupt. Resetting to empty. Prior entities lost.', file=sys.stderr)
                data = {'version': 1, 'updated': today, 'entities': []}
        else:
            data = {'version': 1, 'updated': today, 'entities': []}

        # Always use nested format; migrate list format if encountered
        if isinstance(data, list):
            data = {'version': 1, 'updated': today, 'entities': data}

        entities = data.get('entities', [])
        if not isinstance(entities, list):
            entities = []
        existing_ids: set[str] = {e.get('id', '') for e in entities if isinstance(e, dict)}

        for name in unique_names[:20]:  # cap at 20 new entities per ingest
            if name.lower() in ENTITY_STOPWORDS:
                continue
            entity_id = re.sub(r'\s+', '-', name.lower())
            entity_id = re.sub(r'[^\w\-]', '', entity_id)
            if entity_id in existing_ids:
                continue
            entities.append({
                'id': entity_id,
                'name': name,
                'type': 'unknown',
                'client': client_slug,
                'wiki_page': rel_wiki_no_ext,
                'tags': [client_slug],
            })
            existing_ids.add(entity_id)
            added_count += 1

        data['entities'] = entities
        data['updated'] = today
        return json.dumps(data, indent=2)

    _locked_read_modify_write(entities_path, modifier)
    return added_count


def _update_tags_schema(wiki_page_path: str, frontmatter: dict) -> None:
    """
    Extract tags from wiki page frontmatter and update schema/tags.json.

    BUG-006 fix: tags.json was never populated because no code path called
    into it. This function is called from ingest_source_to_both() and
    sync_page_to_index() after the wiki page is written.
    """
    tags = frontmatter.get('tags', [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(',') if t.strip()]
    if not tags:
        return

    try:
        rel_path = os.path.relpath(wiki_page_path, WIKI_DIR)
    except ValueError:
        rel_path = wiki_page_path

    today = datetime.now().strftime('%Y-%m-%d')
    tags_file = os.path.join(SCHEMA_DIR, 'tags.json')
    os.makedirs(SCHEMA_DIR, exist_ok=True)

    def modifier(raw: str) -> str:
        if raw.strip():
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {'version': 1, 'updated': today, 'tags': {}}
        else:
            data = {'version': 1, 'updated': today, 'tags': {}}

        if 'tags' not in data or not isinstance(data['tags'], dict):
            data['tags'] = {}

        for tag in tags:
            if not isinstance(tag, str):
                continue
            tag = tag.strip().lower()
            if not tag:
                continue
            if tag not in data['tags']:
                data['tags'][tag] = {'count': 0, 'pages': []}
            entry = data['tags'][tag]
            if rel_path not in entry.get('pages', []):
                entry.setdefault('pages', []).append(rel_path)
            entry['count'] = len(entry['pages'])

        data['updated'] = today
        return json.dumps(data, indent=2)

    _locked_read_modify_write(tags_file, modifier)


def _update_graph_schema(client_slug: str, wiki_page_path: str, content: str,
                         frontmatter: dict, today: str) -> None:
    """
    Build graph.json nodes and edges from wiki pages and their wikilinks.

    Creates a node for the current page and an edge for every [[wikilink]]
    found in the body. Also adds edges for trace links in frontmatter's
    ``related`` field.
    """
    graph_path = os.path.join(SCHEMA_DIR, 'graph.json')
    os.makedirs(SCHEMA_DIR, exist_ok=True)

    try:
        rel_path = os.path.relpath(wiki_page_path, WIKI_DIR)
    except ValueError:
        rel_path = wiki_page_path
    page_id = rel_path.replace(os.sep, '/').replace('.md', '')
    page_type = frontmatter.get('type', 'source-summary')

    # Extract wikilinks from body
    wikilinks = re.findall(r'\[\[([^\]]+)\]\]', content)

    # Extract related from frontmatter
    related = frontmatter.get('related', [])
    if isinstance(related, str):
        related = [related]
    for r in related:
        # Strip [[ ]] if present
        cleaned = r.strip().strip('[').strip(']')
        if cleaned and cleaned not in wikilinks:
            wikilinks.append(cleaned)

    def modifier(raw: str) -> str:
        if raw.strip():
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {'version': 1, 'updated': today, 'nodes': [], 'edges': []}
        else:
            data = {'version': 1, 'updated': today, 'nodes': [], 'edges': []}

        nodes = data.get('nodes', [])
        edges = data.get('edges', [])

        # Add/update node for this page
        existing_node_ids = {n.get('id') for n in nodes}
        if page_id not in existing_node_ids:
            nodes.append({
                'id': page_id,
                'type': page_type,
                'client': client_slug,
            })

        # Add edges for wikilinks (dedup)
        existing_edges = {(e.get('from'), e.get('to')) for e in edges}
        for link in wikilinks:
            link = link.strip()
            if not link:
                continue
            if (page_id, link) not in existing_edges:
                edges.append({
                    'from': page_id,
                    'to': link,
                    'label': 'references',
                })
                existing_edges.add((page_id, link))

        data['nodes'] = nodes
        data['edges'] = edges
        data['updated'] = today
        return json.dumps(data, indent=2)

    _locked_read_modify_write(graph_path, modifier)


def _update_index(client_slug: str, page_slug: str, rel_wiki_path: str, today: str) -> None:
    """
    Add or update an entry in index.md for the given wiki page.
    Entry format: | [[client/page-slug]] | type | description | date | confidence |

    When the wiki page lives in a subdirectory (preserve-structure), derive
    the full wikilink path from ``rel_wiki_path`` so nested pages resolve.
    """
    # Ensure file exists with header before locking
    if not os.path.exists(INDEX_FILE):
        with open(INDEX_FILE, 'w') as f:
            f.write(f'# mneme Index\nLast updated: {today}\n\n')

    # Build the wikilink from the page's actual location under wiki/
    # rel_wiki_path is relative to BASE_DIR, e.g. "wiki/demo/sub/page.md"
    link_path = rel_wiki_path
    wiki_prefix = 'wiki' + os.sep
    if link_path.startswith(wiki_prefix):
        link_path = link_path[len(wiki_prefix):]
    link_path = link_path.replace(os.sep, '/')
    if link_path.endswith('.md'):
        link_path = link_path[:-3]
    wikilink = f'[[{link_path}]]'
    new_entry = f'| {wikilink} | source-summary | Ingested via mneme on {today} | {today} | medium |\n'

    def modifier(index_content: str) -> str:
        # If entry already exists, skip (wiki agent handles detailed updates)
        if wikilink in index_content:
            return index_content

        # Find or create client section
        client_header = f'## {client_slug}'
        if client_header in index_content:
            index_content = index_content.replace(
                client_header + '\n',
                client_header + '\n' + new_entry,
            )
        else:
            index_content += f'\n{client_header}\n{new_entry}'

        # Update last updated date
        index_content = re.sub(
            r'Last updated: \d{4}-\d{2}-\d{2}',
            f'Last updated: {today}',
            index_content,
        )

        # Update stats table: count all wikilink entries (lines starting with "| [[")
        total_pages = len(re.findall(r'^\| \[\[', index_content, re.MULTILINE))
        index_content = re.sub(
            r'(\| Total pages\s*\|)\s*\d+',
            f'\\g<1> {total_pages}',
            index_content,
        )
        return index_content

    _locked_read_modify_write(INDEX_FILE, modifier)


def _rotate_log_if_needed() -> None:
    """Archive old log entries when log.md exceeds LOG_MAX_ENTRIES."""
    if not os.path.exists(LOG_FILE):
        return
    with open(LOG_FILE, 'r', encoding='utf-8') as f:
        content = f.read()
    # Find all entry markers (## [YYYY-MM-DD ...])
    entry_starts = [m.start() for m in re.finditer(r'^## \[', content, re.MULTILINE)]
    if len(entry_starts) <= LOG_MAX_ENTRIES:
        return
    # Keep the newest LOG_MAX_ENTRIES entries (entries are in reverse chronological order)
    # The cut point is the start of the (LOG_MAX_ENTRIES+1)th entry
    cut_point = entry_starts[LOG_MAX_ENTRIES]
    keep_content = content[:cut_point]
    archive_content = content[cut_point:]
    # Find the header section (everything before the first entry)
    if entry_starts:
        header = content[:entry_starts[0]]
    else:
        header = '# Mneme Activity Log\n\n'
    # Write archive
    today = datetime.now().strftime('%Y-%m-%d')
    archive_path = os.path.join(BASE_DIR, f'log-archive-{today}.md')
    # Append to existing archive if it exists
    mode = 'a' if os.path.exists(archive_path) else 'w'
    with open(archive_path, mode, encoding='utf-8') as f:
        if mode == 'w':
            f.write(header)
        f.write(archive_content)
    # Write trimmed log
    with open(LOG_FILE, 'w', encoding='utf-8') as f:
        f.write(keep_content)


def _append_log(operation: str, description: str, details: list[str], date: str) -> None:
    """Append a log entry to log.md (newest first)."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    entry_lines = [f'## [{now}] {operation} | {description}']
    for d in details:
        entry_lines.append(f'- {d}')
    entry_lines.append('')
    entry = '\n'.join(entry_lines) + '\n'

    # Ensure file exists with header before locking
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'w') as f:
            f.write('# mneme Log\n\n')

    def modifier(existing: str) -> str:
        if not existing:
            existing = '# mneme Log\n\n'
        # Insert after the header (first blank line after the first heading)
        header_end = existing.find('\n\n')
        if header_end == -1:
            return existing + '\n' + entry
        return existing[:header_end + 2] + entry + existing[header_end + 2:]

    _locked_read_modify_write(LOG_FILE, modifier)
    _rotate_log_if_needed()


# ---------------------------------------------------------------------------
# Init workspace
# ---------------------------------------------------------------------------

def init_workspace(project_name=None, clients=None):
    """
    Create a clean mneme workspace in the current directory.

    Creates the full directory structure, empty schema files, index.md, log.md,
    and CLAUDE.md protocol. Client directories are created under wiki/.
    """
    if project_name is None:
        project_name = os.path.basename(os.getcwd())

    if not clients:
        clients = ['default']

    today = datetime.now().strftime('%Y-%m-%d')

    dirs = [
        'sources/pdfs',
        'sources/emails',
        'sources/conversations',
        'sources/web-captures',
        'sources/images',
        'wiki/_shared',
        'wiki/_templates',
        'schema',
    ]
    for d in dirs:
        os.makedirs(d, exist_ok=True)

    for client in clients:
        os.makedirs('wiki/' + client, exist_ok=True)

    template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wiki', '_templates')
    for tpl in ('page.md', 'client-overview.md'):
        src = os.path.join(template_dir, tpl)
        dst = os.path.join('wiki', '_templates', tpl)
        if os.path.exists(src) and not os.path.exists(dst):
            with open(src, 'r', encoding='utf-8') as f:
                tpl_content = f.read()
            with open(dst, 'w', encoding='utf-8') as fw:
                fw.write(tpl_content)

    entities_path = os.path.join('schema', 'entities.json')
    if not os.path.exists(entities_path):
        with open(entities_path, 'w') as f:
            json.dump({'version': 1, 'updated': today, 'entities': []}, f, indent=2)

    graph_path = os.path.join('schema', 'graph.json')
    if not os.path.exists(graph_path):
        with open(graph_path, 'w') as f:
            json.dump({'version': 1, 'updated': today, 'nodes': [], 'edges': []}, f, indent=2)

    tags_path = os.path.join('schema', 'tags.json')
    if not os.path.exists(tags_path):
        with open(tags_path, 'w') as f:
            json.dump({'version': 1, 'updated': today, 'tags': {}}, f, indent=2)

    index_path = 'index.md'
    if not os.path.exists(index_path):
        client_sections = ''.join('\n## ' + c + '\n\n' for c in clients)
        with open(index_path, 'w', encoding='utf-8') as f:
            lines = [
                '# ' + project_name + ' - mneme Index',
                '',
                'Last updated: ' + today,
                '',
                '| Page | Type | Description | Updated | Confidence |',
                '|---|---|---|---|---|',
                client_sections,
            ]
            f.write('\n'.join(lines))

    log_path = 'log.md'
    if not os.path.exists(log_path):
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write(
                '# mneme Log\n\n'
                '## [' + today + '] INIT | ' + project_name + ' workspace created\n'
                '- Clients: ' + ', '.join(clients) + '\n'
                '- Structure: sources/, wiki/, schema/\n\n'
            )

    claude_md_path = 'CLAUDE.md'
    if not os.path.exists(claude_md_path):
        client_table_rows = '\n'.join(
            '| `' + c + '` | ' + c.replace('-', ' ').title() + ' | Your domain | Active |'
            for c in clients
        )
        claude_content = '\n'.join([
            '# mneme - Wiki Protocol',
            '',
            '## Purpose',
            '',
            'This is a persistent knowledge base. It replaces re-deriving answers from raw documents',
            'with a living, compounding intelligence layer maintained by LLM agents.',
            '',
            'Every question answered here stays answered. Every source ingested compounds into',
            'structured knowledge. Agents read from this layer instead of starting from scratch.',
            '',
            '---',
            '',
            '## Architecture',
            '',
            '### Layer 1: Sources (Read-Only)',
            '',
            'Location: `sources/`',
            '',
            'Rule: IMMUTABLE. Never modify source files. They are evidence.',
            '',
            '### Layer 2: Wiki (LLM-Owned)',
            '',
            'Location: `wiki/`',
            '',
            'Rule: LLM agents create and maintain all content. Humans browse and curate.',
            '',
            '### Layer 3: Schema (Machine-Readable)',
            '',
            'Location: `schema/`',
            '',
            'Files: `entities.json`, `graph.json`, `tags.json`',
            '',
            '---',
            '',
            '## Client Directories',
            '',
            '| Slug | Client | Domain | Status |',
            '|---|---|---|---|',
            client_table_rows,
            '| `_shared` | Shared cross-client knowledge | - | Always active |',
            '',
            'Add your own clients by creating directories under wiki/ and running `mneme init --clients your-client-name`',
            '',
            '---',
            '',
            '## Page Format',
            '',
            'All wiki pages use YAML frontmatter. See `wiki/_templates/page.md` for the full template.',
            '',
            'Required fields: `title`, `type`, `client`, `sources`, `tags`, `created`, `updated`, `confidence`',
            '',
            'Page types: `overview`, `entity`, `concept`, `source-summary`, `comparison`, `deliverable`',
            '',
            '---',
            '',
            '## Quality Standards',
            '',
            '**Claims and Citations**',
            '- Every factual claim must cite a source: `(source: filename)` or `(wiki: [[page]])`',
            '- If no source exists for a claim, mark it: `(inferred)`',
            '',
            '**Structure**',
            '- Use tables for any comparison of 2+ items',
            '- Lead with the answer, then explain. Never bury the conclusion.',
            '',
            '**Confidence**',
            '- `confidence: high` - cross-referenced from multiple sources or verified',
            '- `confidence: medium` - likely correct but based on one source or contains inferences',
            '- `confidence: low` - uncertain; explain why in the Summary section',
            '',
            '---',
            '',
            '## Operations',
            '',
            '### INGEST',
            'Trigger: New source file added to `sources/`',
            'Steps: Read source, identify entities/facts, create or update wiki pages, update schema, update index.md, append to log.md.',
            '',
            '### QUERY',
            'Trigger: User asks a question',
            'Steps: Search index.md, read relevant wiki pages, synthesize answer with citations. File reusable answers as new pages.',
            '',
            '### LINT',
            'Trigger: Manual or after every 10 ingest operations',
            'Checks: Orphan pages, dead links, stale pages, missing citations, schema drift, coverage gaps.',
            '',
        ])
        with open(claude_md_path, 'w', encoding='utf-8') as f:
            f.write(claude_content)

    print('mneme initialized.')
    print('  Project:  ' + project_name)
    print('  Clients:  ' + ', '.join(clients))
    print('')
    print('Run `mneme ingest <file> <client>` to add your first source.')




# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_sync_result(result: dict) -> None:
    print(f'Sync complete.')
    print(f'  Pages synced:  {result["total_pages"]}')
    print(f'  Pages indexed: {result["total_indexed"]}')
    if result['per_client']:
        print('  Per client:')
        for client, count in sorted(result['per_client'].items()):
            print(f'    {client}: {count} pages indexed')
    if result['errors']:
        print(f'  Errors ({len(result["errors"])}):')
        for err in result['errors']:
            print(f'    - {err}')


def _print_search_results(results: list[dict]) -> None:
    if not results:
        print('No results found.')
        return
    for i, r in enumerate(results, 1):
        layer_tag = '[wiki]' if r['layer'] == 'wiki' else '[fts5]'
        print(f'\n{i}. {layer_tag} {r["title"]}')
        print(f'   Source: {r["source"]}')
        if r['tags']:
            print(f'   Tags:   {", ".join(r["tags"][:5])}')
        text = r['text']
        if len(text) > 200:
            text = text[:200].rstrip() + '...'
        print(f'   {text}')


def _print_drift_report(report: dict) -> None:
    if 'error' in report:
        print(f'Drift check error: {report["error"]}')
        return
    s = report.get('summary')
    print('Drift report:')
    if isinstance(s, str):
        print(f'  {s}')
        return
    if not isinstance(s, dict):
        print('  (no summary available)')
        return
    print(f'  Wiki pages total:      {s["total_wiki_pages"]}')
    print(f'  Indexed:               {s.get("total_indexed", s.get("synced", 0))} ({s["sync_pct"]}%)')
    print(f'  Unindexed:             {s["unindexed"]}')
    print(f'  Orphaned:              {s.get("orphaned", 0)}')
    print(f'  Stale:                 {s.get("stale", 0)}')
    print(f'  Drifted:               {report.get("is_drifted", False)}')

    if report.get('unindexed'):
        print('\nUnindexed pages:')
        for p in report['unindexed'][:10]:
            print(f'  - {p}')
        if len(report['unindexed']) > 10:
            print(f'  ... and {len(report["unindexed"]) - 10} more')

    if report.get('orphaned'):
        print('\nOrphaned index entries (source page gone):')
        for p in report['orphaned'][:10]:
            print(f'  - {p}')

    if report.get('stale'):
        print('\nStale pages (may need re-sync):')
        for p in report['stale'][:10]:
            print(f'  - {p}')


def _print_stats(stats: dict) -> None:
    w = stats['wiki']
    m = stats['search']
    sc = stats['schema']
    d = stats['drift']

    print('=== mneme Stats ===\n')
    print('WIKI')
    print(f'  Total pages:       {w["total_pages"]}')
    print(f'  Cross-references:  {w["total_cross_references"]}')
    if w['by_client']:
        print('  By client:')
        for client, count in sorted(w['by_client'].items()):
            print(f'    {client}: {count} pages')

    print('\nSEARCH INDEX')
    if 'error' in m:
        print(f'  Error: {m["error"]}')
    else:
        print(f'  Indexed pages:     {m.get("page_count", "?")}')
        size_kb = round(m.get("db_size_bytes", 0) / 1024, 1)
        print(f'  DB size:           {size_kb} KB')
        if m.get('search_latency_ms') is not None:
            print(f'  Search latency:    {m["search_latency_ms"]} ms')

    print('\nSCHEMA')
    if 'error' in sc:
        print(f'  Error: {sc["error"]}')
    else:
        print(f'  Entities:          {sc["entity_count"]}')
        print(f'  Relationships:     {sc["relationship_count"]}')
        print(f'  Tags:              {sc["tag_count"]}')

    print('\nDRIFT')
    print(f'  Sync status:       {d["sync_status"]}')



def lint() -> dict:
    """
    Health check across the entire knowledge base.

    Checks:
    1. Orphan pages - wiki pages with no incoming wikilinks from other pages
    2. Dead links - [[wikilinks]] pointing to pages that don't exist
    3. Stale pages - pages not updated in 90+ days
    4. Missing citations - factual claims without (source:) or (inferred) markers
    5. Schema drift - entities referenced in wiki but not in entities.json
    6. Coverage gaps - source files with no corresponding wiki pages

    Returns a dict with all issues found and a summary.
    Writes a lint report to wiki/lint-report-YYYY-MM-DD.md.
    """
    today = datetime.now().strftime('%Y-%m-%d')
    issues: dict[str, list] = {
        'orphan_pages': [],
        'dead_links': [],
        'stale_pages': [],
        'missing_citations': [],
        'schema_drift': [],
        'coverage_gaps': [],
    }

    # Collect all wiki pages
    pattern = os.path.join(WIKI_DIR, '**', '*.md')
    all_pages = glob.glob(pattern, recursive=True)
    page_map: dict[str, str] = {}  # slug -> abs path
    page_content: dict[str, str] = {}  # slug -> content
    page_frontmatter: dict[str, dict] = {}  # slug -> frontmatter
    incoming_links: dict[str, set] = {}  # slug -> set of slugs linking to it

    for page in all_pages:
        rel = os.path.relpath(page, WIKI_DIR)
        parts = Path(rel).parts
        if any(p.startswith('_') for p in parts):
            continue
        if rel.startswith('lint-report'):
            continue
        # Normalize slugs to forward-slash form so they match wikilink targets
        # like `[[client/page]]` even on Windows where os.path.relpath returns
        # backslashes. Without this, dead-link / orphan / coverage checks
        # silently misclassify every page on Windows.
        slug = os.path.splitext(rel)[0].replace(os.sep, '/')
        page_map[slug] = page
        incoming_links[slug] = set()
        try:
            with open(page, 'r', encoding='utf-8') as f:
                content = f.read()
            page_content[slug] = content
            fm, _ = parse_frontmatter(content)
            page_frontmatter[slug] = fm
        except Exception:
            page_content[slug] = ''
            page_frontmatter[slug] = {}

    # 1. Dead links + build incoming link map
    for slug, content in page_content.items():
        links = re.findall(r'\[\[(.+?)\]\]', content)
        for link in links:
            target = link.strip()
            if target in page_map:
                incoming_links[target].add(slug)
            else:
                issues['dead_links'].append({
                    'page': slug,
                    'broken_link': target,
                })

    # 2. Orphan pages - no incoming links from other pages
    for slug in page_map:
        if not incoming_links[slug]:
            # overview pages are expected roots
            if not slug.endswith('/overview') and slug.count('/') > 0:
                issues['orphan_pages'].append(slug)

    # 3. Stale pages - not updated in 90+ days
    for slug, fm in page_frontmatter.items():
        updated = fm.get('updated', '')
        if updated:
            try:
                updated_date = datetime.strptime(updated, '%Y-%m-%d')
                days_old = (datetime.now() - updated_date).days
                if days_old > 90:
                    issues['stale_pages'].append({
                        'page': slug,
                        'last_updated': updated,
                        'days_old': days_old,
                    })
            except ValueError:
                pass

    # 4. Missing citations - lines with factual claims but no source marker
    citation_pattern = re.compile(r'\(source:|\(inferred\)|\(wiki:')
    for slug, content in page_content.items():
        _, body = parse_frontmatter(content)
        lines = body.strip().splitlines()
        uncited_count = 0
        for line in lines:
            line = line.strip()
            # Skip headings, empty lines, list markers, links, metadata
            if not line or line.startswith('#') or line.startswith('---'):
                continue
            if line.startswith('- [') or line.startswith('- What') or line.startswith('- '):
                # Check bullet points for citations
                if len(line) > 40 and not citation_pattern.search(line):
                    uncited_count += 1
        if uncited_count > 3:
            issues['missing_citations'].append({
                'page': slug,
                'uncited_claims': uncited_count,
            })

    # 5. Schema drift - entities in wiki pages not in entities.json
    entities_path = os.path.join(SCHEMA_DIR, 'entities.json')
    registered_ids: set[str] = set()
    if os.path.exists(entities_path):
        try:
            with open(entities_path, 'r') as f:
                data = json.load(f)
            for e in data.get('entities', []):
                if isinstance(e, dict):
                    registered_ids.add(e.get('id', ''))
        except Exception:
            pass

    for slug, content in page_content.items():
        # Find capitalized multi-word phrases (same logic as entity extraction)
        found = []
        for line in content.splitlines():
            stripped_line = re.sub(r'^#+\s*', '', line)
            found.extend(re.findall(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b', stripped_line))
        for name in set(found):
            if name.lower() in ENTITY_STOPWORDS:
                continue
            entity_id = re.sub(r'\s+', '-', name.lower())
            entity_id = re.sub(r'[^\w\-]', '', entity_id)
            if entity_id not in registered_ids:
                issues['schema_drift'].append({
                    'page': slug,
                    'entity': name,
                    'entity_id': entity_id,
                })

    # 6. Coverage gaps - source files with no wiki pages
    if os.path.exists(SOURCES_DIR):
        for root, _dirs, files in os.walk(SOURCES_DIR):
            for fname in files:
                if fname.startswith('.'):
                    continue
                source_rel = os.path.relpath(os.path.join(root, fname), BASE_DIR)
                # Check if any wiki page references this source
                found_in_wiki = False
                for content in page_content.values():
                    if source_rel in content or fname in content:
                        found_in_wiki = True
                        break
                if not found_in_wiki:
                    issues['coverage_gaps'].append(source_rel)

    # Count total issues
    total_issues = sum(len(v) for v in issues.values())

    # Write lint report
    report_path = os.path.join(WIKI_DIR, f'lint-report-{today}.md')
    report_lines = [
        f'# Lint Report - {today}',
        '',
        f'Total issues found: {total_issues}',
        '',
    ]

    if issues['dead_links']:
        report_lines.append('## Dead Links')
        report_lines.append('')
        for item in issues['dead_links']:
            report_lines.append(f'- `{item["page"]}` links to `[[{item["broken_link"]}]]` which does not exist')
        report_lines.append('')

    if issues['orphan_pages']:
        report_lines.append('## Orphan Pages (no incoming links)')
        report_lines.append('')
        for slug in issues['orphan_pages']:
            report_lines.append(f'- `{slug}`')
        report_lines.append('')

    if issues['stale_pages']:
        report_lines.append('## Stale Pages (90+ days without update)')
        report_lines.append('')
        for item in issues['stale_pages']:
            report_lines.append(f'- `{item["page"]}` - last updated {item["last_updated"]} ({item["days_old"]} days ago)')
        report_lines.append('')

    if issues['missing_citations']:
        report_lines.append('## Missing Citations')
        report_lines.append('')
        for item in issues['missing_citations']:
            report_lines.append(f'- `{item["page"]}` has {item["uncited_claims"]} uncited claims')
        report_lines.append('')

    if issues['schema_drift']:
        report_lines.append('## Schema Drift (entities not in entities.json)')
        report_lines.append('')
        seen = set()
        for item in issues['schema_drift']:
            key = item['entity_id']
            if key not in seen:
                report_lines.append(f'- `{item["entity"]}` found in `{item["page"]}` but not registered')
                seen.add(key)
        report_lines.append('')

    if issues['coverage_gaps']:
        report_lines.append('## Coverage Gaps (sources with no wiki pages)')
        report_lines.append('')
        for src in issues['coverage_gaps']:
            report_lines.append(f'- `{src}`')
        report_lines.append('')

    if total_issues == 0:
        report_lines.append('All checks passed. Knowledge base is healthy.')
        report_lines.append('')

    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(report_lines))

    # Log the lint
    _append_log(
        operation='LINT',
        description=f'{total_issues} issues found | report: wiki/lint-report-{today}.md',
        details=[
            f'Dead links: {len(issues["dead_links"])}',
            f'Orphan pages: {len(issues["orphan_pages"])}',
            f'Stale pages: {len(issues["stale_pages"])}',
            f'Missing citations: {len(issues["missing_citations"])}',
            f'Schema drift: {len(issues["schema_drift"])}',
            f'Coverage gaps: {len(issues["coverage_gaps"])}',
        ],
        date=today,
    )

    return {
        'issues': issues,
        'total_issues': total_issues,
        'report_path': report_path,
    }


def ingest_dir(directory: str, client_slug: str, force: bool = False,
               recursive: bool = False, preserve_structure: bool = False) -> dict:
    """
    Batch ingest all supported files from a directory.

    Walks the directory (non-recursive by default for safety), ingests each
    supported file (.md, .txt, .pdf) into the given client.

    When recursive=True, walks subdirectories as well.

    When preserve_structure=True, each file's directory position relative to
    ``directory`` becomes a wiki subdirectory under ``wiki/<client>/``. Also
    naturally resolves same-basename collisions (suggestion #15).

    Returns a summary of all ingestions.
    """
    if not os.path.isdir(directory):
        raise FileNotFoundError(f'Directory not found: {directory}')

    supported_exts = {'.md', '.txt', '.pdf', '.xlsx'}
    files = []
    if recursive:
        for root, dirs, filenames in os.walk(directory):
            # Skip hidden directories
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for fname in sorted(filenames):
                _, ext = os.path.splitext(fname)
                if ext.lower() in supported_exts:
                    files.append(os.path.join(root, fname))
    else:
        for fname in sorted(os.listdir(directory)):
            fpath = os.path.join(directory, fname)
            if not os.path.isfile(fpath):
                continue
            _, ext = os.path.splitext(fname)
            if ext.lower() in supported_exts:
                files.append(fpath)

    if not files:
        print(f'No supported files found in {directory}')
        return {'ingested': 0, 'skipped': 0, 'errors': 0, 'results': []}

    print(f'Found {len(files)} files to ingest into client "{client_slug}"')

    ingested = 0
    skipped = 0
    errors = 0
    results = []
    bar = _ProgressBar(len(files), label='ingest')

    for fpath in files:
        fname = os.path.basename(fpath)
        # Compute subpath relative to the input directory when preserving structure
        if preserve_structure:
            sub_rel = os.path.relpath(os.path.dirname(fpath), directory)
            subpath = '' if sub_rel in ('', '.') else sub_rel
        else:
            subpath = ''
        try:
            result = ingest_source_to_both(fpath, client_slug, force=force, subpath=subpath)
            if not result:
                skipped += 1
            else:
                ingested += 1
                results.append(result)
        except Exception as e:
            bar.log(f'ERROR: {fname}: {e}')
            errors += 1
        bar.update(1, current=fname)
    bar.finish()

    print(f'\nIngested: {ingested}  Skipped: {skipped}  Errors: {errors}')

    today = datetime.now().strftime('%Y-%m-%d')
    _append_log(
        operation='INGEST',
        description=f'Batch ingest from {directory} into {client_slug}',
        details=[
            f'Files found: {len(files)}',
            f'Ingested: {ingested}',
            f'Skipped: {skipped}',
            f'Errors: {errors}',
        ],
        date=today,
    )

    return {
        'ingested': ingested,
        'skipped': skipped,
        'errors': errors,
        'results': results,
    }


# ---------------------------------------------------------------------------
# Tornado - inbox processor
# ---------------------------------------------------------------------------

# Type detection keywords - maps content patterns to wiki page types
_TYPE_KEYWORDS = {
    'source-summary': ['meeting notes', 'attendees', 'action items', 'minutes', 'transcript'],
    'entity': ['hazard', 'risk estimation', 'risk evaluation', 'product specification', 'device description'],
    'concept': ['procedure', 'sop', 'work instruction', 'methodology', 'process description'],
    'deliverable': ['report', 'audit', 'assessment', 'evaluation report', 'review report'],
    'comparison': ['comparison', 'versus', 'alternatives', 'trade-off', 'benchmarking'],
    'overview': ['overview', 'executive summary', 'project summary', 'engagement summary'],
}

INBOX_DIR = os.path.join(BASE_DIR, 'inbox')


def _detect_page_type(content: str, frontmatter: dict) -> str:
    """Detect wiki page type from frontmatter or content keywords."""
    # Trust frontmatter first
    fm_type = frontmatter.get('type', '')
    if fm_type in ('overview', 'entity', 'concept', 'source-summary', 'comparison', 'deliverable'):
        return fm_type

    content_lower = content.lower()
    best_type = 'source-summary'
    best_score = 0

    for page_type, keywords in _TYPE_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in content_lower)
        if score > best_score:
            best_score = score
            best_type = page_type

    return best_type


def _detect_client(content: str, frontmatter: dict, filename: str) -> str | None:
    """Detect client slug from frontmatter, filename pattern, or content."""
    # 1. Frontmatter client field
    fm_client = frontmatter.get('client', '')
    if fm_client and fm_client != '_shared':
        return fm_client

    # 2. Filename pattern: client--filename.md or client_filename.md
    if '--' in filename:
        candidate = filename.split('--')[0].lower().strip()
        if re.match(r'^[a-z0-9][a-z0-9\-]*$', candidate):
            return candidate

    # 3. Check if any existing client slug appears in the content
    existing_clients = []
    if os.path.exists(WIKI_DIR):
        for d in os.listdir(WIKI_DIR):
            if os.path.isdir(os.path.join(WIKI_DIR, d)) and not d.startswith('_'):
                existing_clients.append(d)

    content_lower = content.lower()
    for client in existing_clients:
        # Match client slug or its title-case form
        if client in content_lower or client.replace('-', ' ') in content_lower:
            return client

    return None


def tornado(client_slug: str = None, dry_run: bool = False, apply_profile: bool = False) -> dict:
    """
    Process all files in the inbox/ directory.

    For each file:
    1. Read content and parse frontmatter
    2. Detect page type from content keywords
    3. Detect or use specified client
    4. Ingest via ingest_source_to_both() (existing)
    5. Archive original to sources/{client}/
    6. Optionally apply vocabulary harmonization from active profile

    Args:
        client_slug: Force all files into this client. If None, auto-detect per file.
        dry_run: If True, show what would happen without doing it.
        apply_profile: If True, run harmonize(fix=True) on each ingested page.

    Returns a summary dict.
    """
    os.makedirs(INBOX_DIR, exist_ok=True)

    supported_exts = {'.md', '.txt', '.pdf', '.csv'}
    files = []
    for fname in sorted(os.listdir(INBOX_DIR)):
        fpath = os.path.join(INBOX_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        _, ext = os.path.splitext(fname)
        if ext.lower() in supported_exts:
            files.append(fpath)

    if not files:
        print('Inbox is empty. Drop files into inbox/ and run again.')
        return {'processed': 0, 'created': 0, 'updated': 0, 'archived': 0, 'skipped': 0, 'errors': 0}

    print(f'=== mneme Tornado ===\n')
    print(f'Scanning inbox/... found {len(files)} files\n')

    today = datetime.now().strftime('%Y-%m-%d')
    processed = 0
    created = 0
    updated = 0
    archived = 0
    skipped = 0
    errors = 0
    details = []

    for i, fpath in enumerate(files, 1):
        fname = os.path.basename(fpath)
        print(f'[{i}/{len(files)}]  {fname}')

        # Read and analyze
        try:
            with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except Exception as e:
            print(f'        ERROR: Could not read file: {e}\n')
            errors += 1
            continue

        fm, body = parse_frontmatter(content)
        detected_type = _detect_page_type(content, fm)
        detected_client = client_slug or _detect_client(content, fm, fname)

        if not detected_client:
            print(f'        SKIP: Could not detect client. Use --client flag.')
            print()
            skipped += 1
            continue

        # Validate client slug
        if not re.match(r'^[a-z0-9][a-z0-9\-]*$', detected_client):
            print(f'        SKIP: Invalid client slug "{detected_client}"')
            print()
            skipped += 1
            continue

        print(f'        Type: {detected_type}')
        print(f'        Client: {detected_client}')

        if dry_run:
            page_slug = re.sub(r'[^\w\-]', '-', os.path.splitext(fname)[0]).lower()
            page_slug = re.sub(r'-+', '-', page_slug).strip('-')
            print(f'        -> wiki/{detected_client}/{page_slug}.md (dry run)')
            print(f'        -> sources/{detected_client}/{fname} (dry run)')
            print()
            processed += 1
            continue

        # Route CSV files through ingest_csv, everything else through ingest_source_to_both
        _, file_ext = os.path.splitext(fname)
        try:
            if file_ext.lower() == '.csv':
                csv_result = ingest_csv(fpath, detected_client)
                csv_created = csv_result.get('pages_created', 0)
                csv_updated = csv_result.get('pages_updated', 0)
                created += csv_created
                updated += csv_updated
            else:
                result = ingest_source_to_both(fpath, detected_client, force=True)
                if not result:
                    print(f'        Skipped (duplicate)')
                    skipped += 1
                else:
                    action = result.get('action', 'created')
                    print(f'        -> {result["wiki_page"]} ({action})')
                    if action == 'created':
                        created += 1
                    else:
                        updated += 1

                    # Apply profile harmonization if requested
                    if apply_profile:
                        profile = get_active_profile()
                        if profile:
                            harm_result = harmonize(detected_client, fix=True)
                            fixed = harm_result.get('pages_fixed', 0)
                            if fixed:
                                print(f'        Profile applied: {fixed} pages harmonized')
        except Exception as e:
            print(f'        ERROR: {e}')
            errors += 1
            print()
            continue

        # Archive: move original to sources/{client}/
        sources_client_dir = os.path.join(SOURCES_DIR, detected_client)
        os.makedirs(sources_client_dir, exist_ok=True)
        archive_path = os.path.join(sources_client_dir, fname)
        try:
            shutil.move(fpath, archive_path)
            print(f'        -> sources/{detected_client}/{fname} (archived)')
            archived += 1
        except Exception as e:
            print(f'        WARNING: Could not archive: {e}')

        processed += 1
        print()

    # Log the tornado run
    if not dry_run:
        _append_log(
            operation='TORNADO',
            description=f'Inbox processed: {processed} files',
            details=[
                f'Created: {created}',
                f'Updated: {updated}',
                f'Archived: {archived}',
                f'Skipped: {skipped}',
                f'Errors: {errors}',
            ],
            date=today,
        )

    # Summary
    print(f'Tornado {"(dry run) " if dry_run else ""}complete.')
    print(f'  Processed:  {processed}')
    print(f'  Created:    {created} wiki pages')
    print(f'  Updated:    {updated} wiki pages')
    print(f'  Archived:   {archived} sources')
    print(f'  Skipped:    {skipped}')
    print(f'  Errors:     {errors}')
    remaining = len(os.listdir(INBOX_DIR)) if os.path.exists(INBOX_DIR) else 0
    remaining = len([f for f in os.listdir(INBOX_DIR) if os.path.isfile(os.path.join(INBOX_DIR, f))]) if remaining else 0
    print(f'  Inbox:      {"empty" if remaining == 0 else f"{remaining} files remaining"}')

    return {
        'processed': processed,
        'created': created,
        'updated': updated,
        'archived': archived,
        'skipped': skipped,
        'errors': errors,
    }


# ---------------------------------------------------------------------------
# CSV Ingest
# ---------------------------------------------------------------------------

MAPPINGS_DIR = os.path.join(PROFILES_DIR, 'mappings')


def _resolve_mapping_path(name: str) -> Optional[str]:
    """
    Resolve a CSV mapping name to a JSON path.

    Workspace mappings (WORKSPACE_DIR/profiles/mappings/) shadow bundled
    mappings (PACKAGE_DIR/profiles/mappings/) with the same name, mirroring
    profile resolution.
    """
    workspace_path = os.path.join(WORKSPACE_MAPPINGS_DIR, f'{name}.json')
    if os.path.exists(workspace_path):
        return workspace_path
    bundled_path = os.path.join(MAPPINGS_DIR, f'{name}.json')
    if os.path.exists(bundled_path):
        return bundled_path
    return None


def _load_csv_mapping(name: str) -> dict:
    """Load a CSV mapping template, workspace shadows bundled."""
    path = _resolve_mapping_path(name)
    if path is None:
        raise FileNotFoundError(
            f'Mapping not found: "{name}". Looked in '
            f'{WORKSPACE_MAPPINGS_DIR} (workspace) and {MAPPINGS_DIR} (bundled).'
        )
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _detect_csv_mapping(headers: list[str]) -> str | None:
    """
    Auto-detect which mapping template matches a CSV's column headers.

    Scoring (designed to avoid substring traps like "Linked Requirement"
    spuriously matching the `requirements` mapping):

      column_score: how many of the mapping's declared columns appear in
                    the CSV headers, by exact (lowercased) header equality.
      detect_score: how many of the mapping's `detect_headers` tokens appear
                    in the CSV headers, also by exact equality (NOT substring
                    of the joined header string - that produced false positives
                    when one mapping's keyword was a substring of another
                    mapping's column name).

    The winner is chosen by the lexicographic tuple (column_score, detect_score),
    so column matches dominate and detect_headers act as a tiebreaker.

    Threshold: at least 2 total matches across columns + detect_headers.
    Returns None if no mapping reaches the threshold (caller should fall back
    to requiring an explicit --mapping argument).
    """
    headers_lower = {h.lower().strip() for h in headers}

    # Build a name -> path map. Workspace mappings take precedence over
    # bundled mappings with the same name.
    candidates: dict[str, str] = {}
    if os.path.exists(MAPPINGS_DIR):
        for fname in os.listdir(MAPPINGS_DIR):
            if fname.endswith('.json'):
                candidates[fname[:-5]] = os.path.join(MAPPINGS_DIR, fname)
    if os.path.exists(WORKSPACE_MAPPINGS_DIR):
        for fname in os.listdir(WORKSPACE_MAPPINGS_DIR):
            if fname.endswith('.json'):
                candidates[fname[:-5]] = os.path.join(WORKSPACE_MAPPINGS_DIR, fname)

    if not candidates:
        return None

    best_name = None
    best_score = (-1, -1)

    for name, path in candidates.items():
        try:
            with open(path, 'r', encoding='utf-8') as f:
                mapping = json.load(f)
        except Exception:
            continue

        column_score = sum(
            1 for col_name in mapping.get('mapping', {}).keys()
            if col_name.lower().strip() in headers_lower
        )
        detect_keys = mapping.get('detect_headers', [])
        detect_score = sum(
            1 for dk in detect_keys if dk.lower().strip() in headers_lower
        )

        score = (column_score, detect_score)
        if score > best_score:
            best_score = score
            best_name = name

    if best_score[0] + best_score[1] >= 2:
        return best_name
    return None


def _csv_row_to_wiki_page(row: dict, mapping: dict, client_slug: str, today: str) -> tuple[str, str, dict]:
    """
    Convert a single CSV row to a wiki page using the mapping template.

    Returns (page_slug, page_content, trace_links) where trace_links is
    {relationship_type: [target_page_slug, ...]}.
    """
    col_map = mapping.get('mapping', {})
    page_type = mapping.get('page_type', 'source-summary')

    # Extract frontmatter fields
    fm_fields = {}
    body_sections = {}
    trace_links = {}

    for csv_col, target in col_map.items():
        value = (row.get(csv_col) or '').strip()
        if not value:
            continue

        if target.startswith('frontmatter.'):
            field = target.split('.', 1)[1]
            if field == 'tags':
                # Accumulate tags
                existing = fm_fields.get('tags', [])
                existing.append(value.lower().replace(' ', '-'))
                fm_fields['tags'] = existing
            else:
                fm_fields[field] = value
        elif target.startswith('body.'):
            section = target.split('.', 1)[1]
            body_sections[section] = value
        elif target.startswith('traces.'):
            rel_type = target.split('.', 1)[1]
            # Value might be a comma-separated list of IDs
            targets = [v.strip() for v in value.split(',') if v.strip()]
            if targets:
                trace_links[rel_type] = targets

    # Build page slug from ID or title
    title = fm_fields.get('title', fm_fields.get('id', f'row-{hash(str(row)) % 10000}'))
    item_id = fm_fields.get('id', '')

    if item_id:
        page_slug = re.sub(r'[^\w\-]', '-', item_id).lower().strip('-')
    else:
        page_slug = re.sub(r'[^\w\-]', '-', title).lower().strip('-')
    page_slug = re.sub(r'-+', '-', page_slug)

    # Build tags
    tags = fm_fields.get('tags', [])
    if client_slug not in tags:
        tags.insert(0, client_slug)

    # Build body
    body_parts = []
    body_parts.append('## Summary\n')
    if 'summary' in body_sections:
        body_parts.append(body_sections.pop('summary'))
    elif item_id and title != item_id:
        body_parts.append(f'{item_id}: {title}')
    else:
        body_parts.append(title)
    body_parts.append('')

    for section_name, section_content in body_sections.items():
        heading = section_name.replace('-', ' ').title()
        body_parts.append(f'## {heading}\n')
        body_parts.append(section_content)
        body_parts.append('')

    if trace_links:
        body_parts.append('## Trace Links\n')
        for rel_type, targets in trace_links.items():
            for t in targets:
                t_slug = re.sub(r'[^\w\-]', '-', t).lower().strip('-')
                t_slug = re.sub(r'-+', '-', t_slug)
                body_parts.append(f'- {rel_type}: [[{client_slug}/{t_slug}]]')
        body_parts.append('')

    body = '\n'.join(body_parts)

    # Build frontmatter
    sources_yaml = f'  - csv-import-{today}'
    tags_yaml = '\n'.join(f'  - {t}' for t in tags) if tags else '  []'

    page_content = f"""---
title: {title}
type: {page_type}
client: {client_slug}
sources:
{sources_yaml}
tags:
{tags_yaml}
related: []
created: {today}
updated: {today}
confidence: medium
---

{body}
"""
    return page_slug, page_content, trace_links


def ingest_csv(csv_path: str, client_slug: str, mapping_name: str = None, dry_run: bool = False, delimiter: str = None) -> dict:
    """
    Ingest a CSV file, creating one wiki page per row.

    Uses column mapping templates to determine how CSV columns map to
    wiki page frontmatter, body sections, and trace links.

    Args:
        csv_path: Path to the CSV file
        client_slug: Target client
        mapping_name: Explicit mapping template name. If None, auto-detect from headers.
        dry_run: Show what would happen without creating pages.

    Returns summary dict.
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f'CSV file not found: {csv_path}')

    # Read CSV
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        sample = f.read(4096)
        f.seek(0)
        if delimiter:
            delim = delimiter
        else:
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
                delim = dialect.delimiter
            except csv.Error:
                delim = ','
        reader = csv.DictReader(f, delimiter=delim)
        headers = reader.fieldnames or []
        rows = list(reader)

    if not headers or not rows:
        return {'error': 'CSV is empty or has no headers', 'pages_created': 0}

    # Detect or load mapping
    if mapping_name:
        try:
            mapping = _load_csv_mapping(mapping_name)
        except FileNotFoundError:
            return {'error': f'Mapping "{mapping_name}" not found in profiles/mappings/'}
    else:
        detected = _detect_csv_mapping(headers)
        if detected:
            mapping = _load_csv_mapping(detected)
            mapping_name = detected
        else:
            # Fallback: create a generic mapping from headers
            mapping = {
                'name': 'Auto-generated',
                'page_type': 'source-summary',
                'id_column': headers[0],
                'title_column': headers[1] if len(headers) > 1 else headers[0],
                'mapping': {},
            }
            # Map first column to ID, second to title, rest to body
            mapping['mapping'][headers[0]] = 'frontmatter.id'
            if len(headers) > 1:
                mapping['mapping'][headers[1]] = 'frontmatter.title'
            for h in headers[2:]:
                section = re.sub(r'[^\w\-]', '-', h).lower().strip('-')
                mapping['mapping'][h] = f'body.{section}'
            mapping_name = 'auto'

    today = datetime.now().strftime('%Y-%m-%d')
    fname = os.path.basename(csv_path)

    print(f'=== CSV Ingest ===\n')
    print(f'File: {fname} ({len(rows)} rows, {len(headers)} columns)')
    print(f'Mapping: {mapping_name} ({mapping.get("name", "")})')
    print(f'Type: {mapping.get("page_type", "source-summary")}')
    print()

    pages_created = 0
    pages_updated = 0
    trace_links_created = 0
    errors = 0

    client_wiki_dir = os.path.join(WIKI_DIR, client_slug)
    os.makedirs(client_wiki_dir, exist_ok=True)

    # Progress bar for long CSV ingests. dry_run keeps the verbose per-row output.
    bar = None if dry_run else _ProgressBar(len(rows), label='csv')

    for i, row in enumerate(rows, 1):
        # Skip empty rows
        if not any(v.strip() for v in row.values() if v):
            continue

        try:
            page_slug, page_content, traces = _csv_row_to_wiki_page(row, mapping, client_slug, today)
        except Exception as e:
            print(f'[{i}/{len(rows)}]  ERROR: {e}')
            errors += 1
            continue

        # Get a display title
        id_col = mapping.get('id_column', '')
        title_col = mapping.get('title_column', '')
        display_id = row.get(id_col, '').strip() or page_slug
        display_title = row.get(title_col, '').strip() or ''
        display = f'{display_id}: {display_title}' if display_title and display_title != display_id else display_id

        wiki_path = os.path.join(client_wiki_dir, f'{page_slug}.md')
        action = 'update' if os.path.exists(wiki_path) else 'create'

        if dry_run:
            print(f'[{i}/{len(rows)}]  {display}')
            print(f'        -> wiki/{client_slug}/{page_slug}.md ({action}, dry run)')
            if traces:
                for rel, targets in traces.items():
                    for t in targets:
                        print(f'        -> trace: {rel} -> {t}')
            continue

        # Write wiki page
        with open(wiki_path, 'w', encoding='utf-8') as f:
            f.write(page_content)

        if action == 'create':
            pages_created += 1
        else:
            pages_updated += 1

        # Create trace links.
        #
        # NOTE: trace_add returns {'error': ...} when either page doesn't exist
        # yet (it does NOT raise), so we cannot rely on a try/except. CSV ingest
        # frequently runs before the target pages exist (e.g. user-needs imported
        # before requirements). We always go through _store_trace_link directly
        # so the link is persisted regardless of target existence; trace_add's
        # frontmatter side-effect is best-effort and only relevant when the
        # source page is on disk (it always is by this point in ingest_csv).
        for rel_type, targets in traces.items():
            for target_id in targets:
                target_slug = re.sub(r'[^\w\-]', '-', target_id).lower().strip('-')
                target_slug = re.sub(r'-+', '-', target_slug)
                target_page = f'{client_slug}/{target_slug}'
                from_page = f'{client_slug}/{page_slug}'
                _store_trace_link(from_page, target_page, rel_type, today)
                trace_links_created += 1

        # Update index
        _update_index(client_slug, page_slug, os.path.relpath(wiki_path, BASE_DIR), today)

        # Sync to search index
        try:
            sync_page_to_index(wiki_path, client_slug=client_slug)
        except Exception:
            pass

        if bar:
            bar.update(1, current=f'{page_slug} ({action})')
        else:
            print(f'[{i}/{len(rows)}]  {display}')
            print(f'        -> wiki/{client_slug}/{page_slug}.md ({action}d)')
            if traces:
                for rel, targets in traces.items():
                    for t in targets:
                        print(f'        -> trace: {rel} -> {t}')

    # Update entities schema for all new pages
    if not dry_run:
        try:
            for fname_wiki in os.listdir(client_wiki_dir):
                if fname_wiki.endswith('.md'):
                    wp = os.path.join(client_wiki_dir, fname_wiki)
                    with open(wp, 'r', encoding='utf-8') as f:
                        content = f.read()
                    _update_entities_schema(client_slug, wp, content, today)
                    fm, _ = parse_frontmatter(content)
                    _update_tags_schema(wp, fm)
        except Exception:
            pass

        _append_log(
            operation='INGEST',
            description=f'CSV ingest: {os.path.basename(csv_path)} -> {client_slug} ({pages_created} created, {pages_updated} updated)',
            details=[
                f'Source: {csv_path}',
                f'Mapping: {mapping_name}',
                f'Rows: {len(rows)}',
                f'Pages created: {pages_created}',
                f'Pages updated: {pages_updated}',
                f'Trace links: {trace_links_created}',
                f'Errors: {errors}',
            ],
            date=today,
        )

    if bar:
        bar.finish()
    print(f'\nCSV ingest {"(dry run) " if dry_run else ""}complete.')
    print(f'  Pages created:  {pages_created}')
    print(f'  Pages updated:  {pages_updated}')
    print(f'  Trace links:    {trace_links_created}')
    print(f'  Errors:         {errors}')

    return {
        'pages_created': pages_created,
        'pages_updated': pages_updated,
        'trace_links_created': trace_links_created,
        'errors': errors,
        'mapping_used': mapping_name,
    }


def _store_trace_link(from_page: str, to_page: str, relationship: str, today: str) -> None:
    """Store a trace link in traceability.json without validating page existence."""
    trace_file = TRACEABILITY_FILE
    os.makedirs(os.path.dirname(trace_file), exist_ok=True)

    def modifier(raw: str) -> str:
        if raw.strip():
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {'version': 1, 'updated': today, 'links': []}
        else:
            data = {'version': 1, 'updated': today, 'links': []}

        if 'links' not in data:
            data['links'] = []

        # Check for duplicate
        for link in data['links']:
            if link.get('from') == from_page and link.get('to') == to_page and link.get('type') == relationship:
                return json.dumps(data, indent=2)

        data['links'].append({
            'from': from_page,
            'to': to_page,
            'type': relationship,
            'created': today,
        })
        data['updated'] = today
        return json.dumps(data, indent=2)

    _locked_read_modify_write(trace_file, modifier)


# ---------------------------------------------------------------------------
# Status, recent, tags, diff, snapshot, dedupe, export, profile functions
# ---------------------------------------------------------------------------

def status() -> dict:
    """
    Quick summary of the knowledge base health.

    Returns counts of un-ingested sources, recently modified wiki pages,
    and a rough pending-lint issue count (orphan pages).
    """
    # Find all source files
    source_files = []
    if os.path.isdir(SOURCES_DIR):
        for root, dirs, files in os.walk(SOURCES_DIR):
            for fname in files:
                if not fname.startswith('.'):
                    source_files.append(os.path.join(root, fname))

    # Read log.md to find which sources have been ingested
    log_content = ''
    ingested_sources = set()
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r') as f:
            log_content = f.read()
        # Match filenames mentioned in INGEST log entries
        for match in re.finditer(r'INGEST\s*\|\s*(.+)', log_content):
            desc = match.group(1)
            ingested_sources.add(desc.strip())

    # Count un-ingested: sources whose basename is not mentioned in any ingest log line
    uningest_count = 0
    un_ingested_files = []
    for sf in source_files:
        basename = os.path.basename(sf)
        if basename not in log_content:
            uningest_count += 1
            un_ingested_files.append(sf)

    # Count wiki pages
    wiki_pages = []
    if os.path.isdir(WIKI_DIR):
        for root, dirs, files in os.walk(WIKI_DIR):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
            for fname in files:
                if fname.endswith('.md') and fname not in EXCLUDED_FILES:
                    wiki_pages.append(os.path.join(root, fname))

    # Count orphan pages (pages with no incoming links from other pages)
    all_slugs = set()
    incoming = set()
    for wp in wiki_pages:
        rel = os.path.relpath(wp, WIKI_DIR)
        slug = rel.replace(os.sep, '/').replace('.md', '')
        all_slugs.add(slug)

    for wp in wiki_pages:
        with open(wp, 'r') as f:
            content = f.read()
        for link_match in re.finditer(r'\[\[([^\]]+)\]\]', content):
            incoming.add(link_match.group(1))

    orphan_count = len(all_slugs - incoming)

    return {
        'source_files': len(source_files),
        'total_sources': len(source_files),
        'un_ingested': uningest_count,
        'un_ingested_files': un_ingested_files,
        'wiki_pages': len(wiki_pages),
        'total_wiki_pages': len(wiki_pages),
        'orphan_pages': orphan_count,
    }


def recent(n: int = 10) -> list:
    """
    Parse log.md and return the last N log entries.

    Each entry is a dict with {date, operation, description}.
    """
    if not os.path.exists(LOG_FILE):
        return []

    with open(LOG_FILE, 'r') as f:
        log_content = f.read()

    entries = []
    for match in re.finditer(
        r'## \[(\d{4}-\d{2}-\d{2}[\s\d:]*)\]\s+(\w[\w-]*)\s*\|\s*(.+)',
        log_content,
    ):
        entries.append({
            'date': match.group(1).strip(),
            'operation': match.group(2).strip(),
            'description': match.group(3).strip(),
        })

    return entries[:n]


def tags_list() -> dict:
    """
    Read schema/tags.json and return the tags dict with counts and pages.
    """
    tags_file = os.path.join(SCHEMA_DIR, 'tags.json')
    if not os.path.exists(tags_file):
        return {}

    with open(tags_file, 'r') as f:
        data = json.load(f)

    return data.get('tags', {})


def tags_merge(old_tag: str, new_tag: str) -> dict:
    """
    Merge old_tag into new_tag in schema/tags.json and all wiki page frontmatter.

    Combines page lists and updates tag counts in schema. Scans all wiki pages
    and replaces the old tag with the new tag in their frontmatter tags lists.

    Returns {pages_updated, old_tag, new_tag}.
    """
    tags_file = os.path.join(SCHEMA_DIR, 'tags.json')
    pages_updated = 0

    # Update tags.json
    if os.path.exists(tags_file):
        def merge_tags_json(content):
            if not content.strip():
                return content
            data = json.loads(content)
            tags = data.get('tags', {})
            if old_tag in tags:
                old_entry = tags.pop(old_tag)
                if new_tag not in tags:
                    tags[new_tag] = old_entry
                else:
                    # Merge pages lists, deduplicate
                    merged_pages = list(set(
                        tags[new_tag].get('pages', []) + old_entry.get('pages', [])
                    ))
                    tags[new_tag]['pages'] = merged_pages
                    tags[new_tag]['count'] = len(merged_pages)
                data['tags'] = tags
                data['updated'] = datetime.now().strftime('%Y-%m-%d')
            return json.dumps(data, indent=2)

        _locked_read_modify_write(tags_file, merge_tags_json)

    # Scan and update all wiki pages
    if os.path.isdir(WIKI_DIR):
        for root, dirs, files in os.walk(WIKI_DIR):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
            for fname in files:
                if not fname.endswith('.md') or fname in EXCLUDED_FILES:
                    continue
                fpath = os.path.join(root, fname)

                def replace_tag(content, _old=old_tag, _new=new_tag):
                    # Replace tag in frontmatter tags list
                    updated = re.sub(
                        r'(^\s+-\s+)' + re.escape(_old) + r'\s*$',
                        r'\g<1>' + _new,
                        content,
                        flags=re.MULTILINE,
                    )
                    return updated

                with open(fpath, 'r') as f:
                    original = f.read()

                new_content = replace_tag(original)
                if new_content != original:
                    _locked_read_modify_write(fpath, replace_tag)
                    pages_updated += 1

    today = datetime.now().strftime('%Y-%m-%d')
    _append_log(
        operation='UPDATE',
        description=f'Merged tag "{old_tag}" into "{new_tag}"',
        details=[f'Pages updated: {pages_updated}'],
        date=today,
    )

    return {
        'pages_updated': pages_updated,
        'old_tag': old_tag,
        'new_tag': new_tag,
    }


def _format_tag_packet(packet: dict) -> str:
    """Render a tag packet as markdown for piping to an LLM agent."""
    lines: list[str] = []
    page = packet['page']
    lines.append('# Tag packet')
    lines.append('')
    lines.append(f'**Page:** `{page["wiki_path"]}`')
    lines.append(f'**Title:** {page["title"]}')
    lines.append(f'**Client:** {page["client"]}')
    current = page.get('current_tags') or []
    lines.append(f'**Current tags:** {", ".join(current) if current else "(none)"}')
    if packet.get('profile_guidance'):
        lines.append(f'**Profile:** {packet["profile_guidance"]}')
    lines.append('')

    taxonomy = packet.get('tag_taxonomy') or []
    lines.append('## Existing tag taxonomy (sorted by usage)')
    lines.append('')
    if not taxonomy:
        lines.append('_(no tags exist yet in this workspace)_')
    else:
        lines.append('| Tag | Pages | Description |')
        lines.append('|---|---:|---|')
        for t in taxonomy[:50]:
            desc = t.get('description', '') or ''
            lines.append(f'| `{t["name"]}` | {t["count"]} | {desc} |')
        if len(taxonomy) > 50:
            lines.append(f'\n_(... and {len(taxonomy) - 50} more)_')
    lines.append('')

    lines.append('## Page content')
    lines.append('')
    lines.append('```markdown')
    lines.append(page['body'])
    lines.append('```')
    lines.append('')

    lines.append('## Instruction')
    lines.append('')
    lines.append(packet['tag_prompt'])
    return '\n'.join(lines)


def tags_suggest(page_slug: str) -> dict:
    """
    Build a *tag packet* for an LLM agent to read.

    The packet contains the page content, current tags, the workspace tag
    taxonomy (existing tags with usage counts), and a ready-to-paste prompt
    instructing the agent to propose tags. The agent reads the packet,
    decides on tags, and calls ``mneme tags apply`` to write them.

    page_slug format: "client/page" (with or without .md extension).
    """
    if page_slug.endswith('.md'):
        page_slug = page_slug[:-3]
    rel_page = page_slug.replace('/', os.sep) + '.md'
    page_path = os.path.join(WIKI_DIR, rel_page)
    if not os.path.exists(page_path):
        raise FileNotFoundError(f'Page not found: {page_slug}')

    with open(page_path, 'r', encoding='utf-8') as f:
        content = f.read()
    frontmatter, body = parse_frontmatter(content)

    title = frontmatter.get('title', os.path.basename(page_path))
    client = frontmatter.get('client', page_slug.split('/')[0])
    current_tags = frontmatter.get('tags', [])
    if isinstance(current_tags, str):
        current_tags = [t.strip() for t in current_tags.split(',') if t.strip()]

    # Collect the workspace tag taxonomy (existing tags + counts).
    taxonomy = []
    tags_data = tags_list()
    for tag_name, info in sorted(tags_data.items(), key=lambda kv: -kv[1].get('count', 0)):
        taxonomy.append({
            'name': tag_name,
            'count': info.get('count', 0),
            'description': info.get('description', ''),
        })

    # Profile tag guidance, if any.
    profile_guidance = ''
    try:
        profile = get_active_profile()
        if profile:
            profile_guidance = (
                f"Active profile: {profile.get('name', 'unknown')}. "
                "Prefer profile vocabulary terms when they describe the topic."
            )
    except Exception:
        pass

    tag_prompt = (
        "You are tagging a wiki page in a knowledge workspace.\n\n"
        "Read the page content below. Propose 3-7 tags that describe the "
        "topic, domain, and any standards/regulations mentioned.\n\n"
        "Rules:\n"
        "1. PREFER existing tags from the taxonomy when they fit -- consistency "
        "matters more than novelty.\n"
        "2. Add NEW tags only when no existing tag captures the concept.\n"
        "3. Tag format: lowercase, hyphenated (e.g. `iso-13485`, "
        "`risk-management`, `cardiac-monitoring`).\n"
        "4. Do NOT add the client slug -- it is auto-applied.\n"
        "5. Do NOT propose generic tags like `summary`, `overview`, `report`.\n\n"
        "Output a single JSON object with two keys:\n"
        '  {"tags": ["existing-tag-a", "existing-tag-b"], '
        '"new_tags": ["proposed-new-tag"]}\n\n'
        "After deciding, the operator will run:\n"
        f"  mneme tags apply {page_slug} --add tag1,tag2,tag3"
    )

    return {
        'page': {
            'wiki_path': page_slug + '.md',
            'title': title,
            'client': client,
            'current_tags': current_tags,
            'body': body.strip(),
        },
        'tag_taxonomy': taxonomy,
        'profile_guidance': profile_guidance,
        'tag_prompt': tag_prompt,
    }


def tags_apply(page_slug: str, add: list = None,
               remove: list = None) -> dict:
    """
    Apply tag changes to a wiki page atomically.

    1. Read the page frontmatter.
    2. Add / remove tags (deduplicated, lowercase, hyphenated).
    3. Write the page back.
    4. Update schema/tags.json via _update_tags_schema().
    5. Re-sync the page to the FTS5 index so search reflects the new tags.

    Returns ``{wiki_path, tags_before, tags_after, added, removed}``.
    """
    if page_slug.endswith('.md'):
        page_slug = page_slug[:-3]
    rel_page = page_slug.replace('/', os.sep) + '.md'
    page_path = os.path.join(WIKI_DIR, rel_page)
    if not os.path.exists(page_path):
        raise FileNotFoundError(f'Page not found: {page_slug}')

    add = [t.strip().lower() for t in (add or []) if t and t.strip()]
    remove = [t.strip().lower() for t in (remove or []) if t and t.strip()]

    with open(page_path, 'r', encoding='utf-8') as f:
        content = f.read()
    frontmatter, body = parse_frontmatter(content)

    current = frontmatter.get('tags', [])
    if isinstance(current, str):
        current = [t.strip() for t in current.split(',') if t.strip()]
    current = [t.lower() for t in current]
    tags_before = list(current)

    # Apply removals first, then additions, dedup while preserving order.
    new_tags = [t for t in current if t not in remove]
    for t in add:
        if t not in new_tags:
            new_tags.append(t)
    actually_added = [t for t in add if t not in tags_before]
    actually_removed = [t for t in remove if t in tags_before]

    # Rewrite the page frontmatter.
    today = datetime.now().strftime('%Y-%m-%d')
    client = frontmatter.get('client', page_slug.split('/')[0])
    new_page = _build_wiki_page(
        title=frontmatter.get('title', os.path.basename(page_path)),
        client=client,
        sources=frontmatter.get('sources', []),
        tags=new_tags,
        created=frontmatter.get('created', today),
        updated=today,
        confidence=frontmatter.get('confidence', 'medium'),
        body=body.strip(),
    )
    with open(page_path, 'w', encoding='utf-8') as f:
        f.write(new_page)

    # Update schema/tags.json (handles add). For removals, we also need to
    # drop the page from the removed tags' page lists.
    new_fm, _ = parse_frontmatter(new_page)
    _update_tags_schema(page_path, new_fm)

    if actually_removed:
        tags_file = os.path.join(SCHEMA_DIR, 'tags.json')
        wiki_rel = os.path.relpath(page_path, WIKI_DIR)

        def drop_from_removed(raw: str) -> str:
            if not raw.strip():
                return raw
            data = json.loads(raw)
            tags_dict = data.get('tags', {})
            for t in actually_removed:
                if t in tags_dict:
                    pages = [p for p in tags_dict[t].get('pages', []) if p != wiki_rel]
                    tags_dict[t]['pages'] = pages
                    tags_dict[t]['count'] = len(pages)
                    if not pages:
                        del tags_dict[t]
            data['tags'] = tags_dict
            data['updated'] = today
            return json.dumps(data, indent=2)

        if os.path.exists(tags_file):
            _locked_read_modify_write(tags_file, drop_from_removed)

    # Re-sync the page to the FTS5 index so search picks up the new tags.
    sync_page_to_index(page_path, client_slug=client)

    _append_log(
        operation='UPDATE',
        description=f'Tagged {page_slug}',
        details=[
            f'Added: {", ".join(actually_added) or "none"}',
            f'Removed: {", ".join(actually_removed) or "none"}',
            f'Tags after: {", ".join(new_tags)}',
        ],
        date=today,
    )

    return {
        'wiki_path': rel_page.replace(os.sep, '/'),
        'tags_before': tags_before,
        'tags_after': new_tags,
        'added': actually_added,
        'removed': actually_removed,
    }


def tags_bulk_suggest(client: str = None, filter_substr: str = None,
                      limit: int = 20, include_tagged: bool = False,
                      body_preview: int = 2000) -> dict:
    """
    Build a *bulk tag packet* covering up to N pages at once.

    By default, only pages whose tag list is empty or only contains the
    client slug are included. Pass ``include_tagged=True`` to re-tag pages
    that already have non-auto tags.

    ``body_preview`` caps each page's body in the packet to keep the packet
    small enough for agent context windows.
    """
    pattern = os.path.join(WIKI_DIR, '**', '*.md')
    all_pages = sorted(glob.glob(pattern, recursive=True))
    candidates: list[dict] = []

    for page_path in all_pages:
        rel = os.path.relpath(page_path, WIKI_DIR)
        parts = Path(rel).parts
        if any(p in EXCLUDED_DIRS for p in parts[:-1]):
            continue
        if os.path.basename(page_path) in EXCLUDED_FILES:
            continue
        wiki_path = rel.replace(os.sep, '/')
        if filter_substr and filter_substr not in wiki_path:
            continue
        page_client = parts[0] if len(parts) > 1 else '_root'
        if client and page_client != client:
            continue

        try:
            with open(page_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception:
            continue
        frontmatter, body = parse_frontmatter(content)

        current_tags = frontmatter.get('tags', [])
        if isinstance(current_tags, str):
            # Inline "[]" is common when frontmatter is written as `tags: []`
            stripped = current_tags.strip().strip('[]').strip()
            current_tags = [t.strip() for t in stripped.split(',') if t.strip()] if stripped else []
        non_auto = [t for t in current_tags if t != page_client]

        if not include_tagged and non_auto:
            continue

        candidates.append({
            'wiki_path': wiki_path,
            'title': frontmatter.get('title', os.path.basename(page_path)),
            'client': page_client,
            'current_tags': current_tags,
            'body': body.strip()[:body_preview],
        })
        if len(candidates) >= limit:
            break

    # Shared taxonomy across all pages in the packet.
    taxonomy = []
    tags_data = tags_list()
    for tag_name, info in sorted(tags_data.items(), key=lambda kv: -kv[1].get('count', 0)):
        taxonomy.append({
            'name': tag_name,
            'count': info.get('count', 0),
            'description': info.get('description', ''),
        })

    profile_guidance = ''
    try:
        profile = get_active_profile()
        if profile:
            profile_guidance = (
                f"Active profile: {profile.get('name', 'unknown')}. "
                "Prefer profile vocabulary terms when they describe the topic."
            )
    except Exception:
        pass

    tag_prompt = (
        "You are bulk-tagging a batch of wiki pages.\n\n"
        "For each page below, propose 3-7 tags that describe its topic, "
        "domain, and any standards/regulations mentioned.\n\n"
        "Rules:\n"
        "1. PREFER existing taxonomy tags over inventing new ones.\n"
        "2. Tag format: lowercase, hyphenated (e.g. `iso-13485`).\n"
        "3. Do NOT add the client slug -- it is auto-applied.\n"
        "4. Skip generic tags (`summary`, `overview`, `report`).\n\n"
        "Output a single JSON object with a `pages` array:\n"
        '  {"pages": [\n'
        '    {"wiki_path": "client/page.md", "add": ["tag1"], "remove": []},\n'
        '    ...\n'
        '  ]}\n\n'
        "Then run: mneme tags bulk-apply response.json"
    )

    return {
        'pages': candidates,
        'tag_taxonomy': taxonomy,
        'profile_guidance': profile_guidance,
        'tag_prompt': tag_prompt,
    }


def _format_bulk_tag_packet(packet: dict) -> str:
    """Render a bulk tag packet as markdown for an LLM agent."""
    lines: list[str] = []
    lines.append('# Bulk tag packet')
    lines.append('')
    lines.append(f'**Pages in this batch:** {len(packet["pages"])}')
    if packet.get('profile_guidance'):
        lines.append(f'**Profile:** {packet["profile_guidance"]}')
    lines.append('')

    taxonomy = packet.get('tag_taxonomy') or []
    lines.append('## Existing tag taxonomy (sorted by usage)')
    lines.append('')
    if not taxonomy:
        lines.append('_(no tags exist yet in this workspace)_')
    else:
        lines.append('| Tag | Pages |')
        lines.append('|---|---:|')
        for t in taxonomy[:50]:
            lines.append(f'| `{t["name"]}` | {t["count"]} |')
    lines.append('')

    for i, page in enumerate(packet['pages'], 1):
        lines.append(f'## Page {i}/{len(packet["pages"])}: `{page["wiki_path"]}`')
        lines.append('')
        lines.append(f'**Title:** {page["title"]}')
        current = page.get('current_tags') or []
        lines.append(f'**Current tags:** {", ".join(current) if current else "(none)"}')
        lines.append('')
        lines.append('```markdown')
        lines.append(page['body'])
        lines.append('```')
        lines.append('')

    lines.append('## Instruction')
    lines.append('')
    lines.append(packet['tag_prompt'])
    return '\n'.join(lines)


def tags_bulk_apply(response) -> dict:
    """
    Apply tag changes from an agent's bulk response.

    ``response`` may be a path to a JSON file OR a dict.
    Expected shape: ``{"pages": [{"wiki_path": "client/page.md",
    "add": [...], "remove": [...]}, ...]}``.

    Tolerates per-page failures; continues on error.
    """
    if isinstance(response, str):
        with open(response, 'r', encoding='utf-8') as f:
            response = json.load(f)
    if not isinstance(response, dict) or 'pages' not in response:
        raise ValueError('Expected {"pages": [...]} structure')
    entries = response.get('pages', [])
    if not isinstance(entries, list):
        raise ValueError('"pages" must be a list')

    applied = 0
    failed: list[dict] = []
    total_added = 0
    total_removed = 0

    for entry in entries:
        wiki_path = entry.get('wiki_path') if isinstance(entry, dict) else None
        if not wiki_path:
            failed.append({'entry': entry, 'error': 'missing wiki_path'})
            continue
        add_list = entry.get('add') or []
        remove_list = entry.get('remove') or []
        # Strip .md if present for tags_apply
        slug = wiki_path[:-3] if wiki_path.endswith('.md') else wiki_path
        try:
            result = tags_apply(slug, add=add_list, remove=remove_list)
            applied += 1
            total_added += len(result['added'])
            total_removed += len(result['removed'])
        except (FileNotFoundError, ValueError) as e:
            failed.append({'wiki_path': wiki_path, 'error': str(e)})

    return {
        'applied': applied,
        'failed': failed,
        'total_tags_added': total_added,
        'total_tags_removed': total_removed,
    }


VALID_ENTITY_TYPES = {
    'standard', 'company', 'person', 'product',
    'technology', 'concept', 'brand', 'unknown',
}


def _load_entities() -> dict:
    """Load schema/entities.json (nested form). Returns a fresh skeleton on error."""
    path = os.path.join(SCHEMA_DIR, 'entities.json')
    if not os.path.exists(path):
        return {'version': 1, 'updated': '', 'entities': []}
    try:
        with open(path, 'r') as f:
            data = json.load(f)
    except Exception:
        return {'version': 1, 'updated': '', 'entities': []}
    if isinstance(data, list):
        data = {'version': 1, 'updated': '', 'entities': data}
    if 'entities' not in data or not isinstance(data['entities'], list):
        data['entities'] = []
    return data


def _format_entity_packet(packet: dict) -> str:
    """Render an entity-classification packet as markdown for an LLM agent."""
    lines: list[str] = []
    lines.append('# Entity classification packet')
    lines.append('')
    lines.append(f'**Entities needing classification:** {len(packet["entities"])}')
    types_summary = ', '.join(f'{t}={c}' for t, c in sorted(packet["existing_types"].items()))
    lines.append(f'**Current type distribution:** {types_summary or "(none)"}')
    lines.append(f'**Valid types:** {", ".join(packet["valid_types"])}')
    lines.append('')

    lines.append('## Entities')
    lines.append('')
    lines.append('| ID | Name | Client | Current type | Example page |')
    lines.append('|---|---|---|---|---|')
    for e in packet['entities']:
        example = (e.get('example_pages') or [''])[0]
        lines.append(f'| `{e["id"]}` | {e["name"]} | {e.get("client", "")} | {e.get("current_type", "unknown")} | {example} |')
    lines.append('')

    lines.append('## Instruction')
    lines.append('')
    lines.append(packet['entity_prompt'])
    return '\n'.join(lines)


def entity_suggest(client: str = None, limit: int = 50,
                   only_unknown: bool = True) -> dict:
    """
    Build a packet of entities for an LLM agent to classify.

    By default, only entities typed `unknown` are included. Pass
    only_unknown=False to review all entities.
    """
    data = _load_entities()
    entities = data.get('entities', [])

    candidates = []
    for e in entities:
        if not isinstance(e, dict):
            continue
        if client and e.get('client') != client:
            continue
        if only_unknown and e.get('type') != 'unknown':
            continue
        candidates.append(e)
        if len(candidates) >= limit:
            break

    # Build a reverse index: entity name -> list of wiki paths that mention it.
    # One pass over the wiki to keep O(N) for large workspaces.
    example_pages: dict[str, list[str]] = {}
    if candidates:
        names = [e['name'] for e in candidates if e.get('name')]
        if names:
            pattern = os.path.join(WIKI_DIR, '**', '*.md')
            for page_path in glob.glob(pattern, recursive=True):
                rel = os.path.relpath(page_path, WIKI_DIR)
                parts = Path(rel).parts
                if any(p in EXCLUDED_DIRS for p in parts[:-1]):
                    continue
                try:
                    with open(page_path, 'r', encoding='utf-8') as f:
                        page_body = f.read()
                except Exception:
                    continue
                for name in names:
                    if len(example_pages.get(name, [])) >= 3:
                        continue
                    if name in page_body:
                        example_pages.setdefault(name, []).append(rel.replace(os.sep, '/'))

    out_entities = []
    for e in candidates:
        out_entities.append({
            'id': e.get('id'),
            'name': e.get('name'),
            'client': e.get('client'),
            'current_type': e.get('type', 'unknown'),
            'wiki_page': e.get('wiki_page'),
            'example_pages': example_pages.get(e.get('name', ''), []),
        })

    # Global type distribution (across all entities, not just candidates)
    existing_types: dict[str, int] = {}
    for e in entities:
        if isinstance(e, dict):
            t = e.get('type', 'unknown')
            existing_types[t] = existing_types.get(t, 0) + 1

    entity_prompt = (
        "You are classifying named entities extracted from a wiki.\n\n"
        "For each entity in the table, decide which type it belongs to from "
        "the valid types list. Use the example page for context when the "
        "name alone is ambiguous.\n\n"
        "Rules:\n"
        "1. Pick one of the valid types; do NOT invent new types.\n"
        "2. Examples:\n"
        "   - `iso-13485`, `iec-62304`, `eu-mdr`, `gdpr` -> standard\n"
        "   - `acme-corp`, `siemens-healthineers` -> company\n"
        "   - `john-smith` (a person's name) -> person\n"
        "   - `cardiac-monitor-x200` (a named product) -> product\n"
        "   - `imu`, `bda`, `rbac` (technical concepts/acronyms) -> technology\n"
        "   - `risk-management`, `design-validation` (abstract ideas) -> concept\n"
        "3. If genuinely unclear, leave as `unknown` -- better than a wrong guess.\n\n"
        "Output a single JSON array:\n"
        '  [{"id": "iso-13485", "type": "standard"}, ...]\n\n'
        "Then run one of:\n"
        "  mneme entity apply --id <id> --type <type>        # one at a time\n"
        "  mneme entity bulk-apply classifications.json      # batch"
    )

    return {
        'entities': out_entities,
        'existing_types': existing_types,
        'valid_types': sorted(VALID_ENTITY_TYPES),
        'entity_prompt': entity_prompt,
    }


def entity_apply(entity_id: str, type_: str) -> dict:
    """Atomic: set a single entity's `type` in schema/entities.json."""
    if type_ not in VALID_ENTITY_TYPES:
        raise ValueError(
            f'Invalid entity type "{type_}". Must be one of: '
            f'{", ".join(sorted(VALID_ENTITY_TYPES))}'
        )

    entities_path = os.path.join(SCHEMA_DIR, 'entities.json')
    if not os.path.exists(entities_path):
        raise FileNotFoundError(f'entities.json not found at {entities_path}')

    today = datetime.now().strftime('%Y-%m-%d')
    result = {'id': entity_id, 'old_type': None, 'new_type': type_}

    def modifier(raw: str) -> str:
        if raw.strip():
            data = json.loads(raw)
        else:
            data = {'version': 1, 'updated': today, 'entities': []}
        if isinstance(data, list):
            data = {'version': 1, 'updated': today, 'entities': data}
        entities = data.get('entities', [])
        found = False
        for e in entities:
            if isinstance(e, dict) and e.get('id') == entity_id:
                result['old_type'] = e.get('type', 'unknown')
                e['type'] = type_
                found = True
                break
        if not found:
            raise KeyError(f'Entity id not found: {entity_id}')
        data['entities'] = entities
        data['updated'] = today
        return json.dumps(data, indent=2)

    _locked_read_modify_write(entities_path, modifier)

    _append_log(
        operation='UPDATE',
        description=f'Classified entity "{entity_id}"',
        details=[f'{result["old_type"]} -> {type_}'],
        date=today,
    )

    return result


def entity_bulk_apply(classifications) -> dict:
    """
    Apply a batch of entity classifications.

    `classifications` may be either:
      - a list of {id, type} dicts, or
      - a path to a JSON file containing such a list.

    Tolerates per-entity failures; returns a summary.
    """
    if isinstance(classifications, str):
        with open(classifications, 'r', encoding='utf-8') as f:
            classifications = json.load(f)
    if not isinstance(classifications, list):
        raise ValueError('Expected a list of {id, type} classifications')

    applied = 0
    errors: list[dict] = []
    updated_ids: list[str] = []

    for item in classifications:
        if not isinstance(item, dict) or 'id' not in item or 'type' not in item:
            errors.append({'item': item, 'error': 'missing id or type'})
            continue
        try:
            entity_apply(item['id'], item['type'])
            applied += 1
            updated_ids.append(item['id'])
        except (ValueError, KeyError, FileNotFoundError) as e:
            errors.append({'id': item.get('id'), 'error': str(e)})

    return {
        'applied': applied,
        'errors': errors,
        'updated_ids': updated_ids,
    }


def _detect_id_prefixes(slugs: list[str], min_count: int = 2) -> dict[str, int]:
    """Return {PREFIX: count} for slugs matching `^[A-Z]{2,8}-\\d+`."""
    prefixes: dict[str, int] = {}
    pat = re.compile(r'^([A-Z]{2,8})[-_]?\d+', re.IGNORECASE)
    for slug in slugs:
        m = pat.match(slug)
        if m:
            p = m.group(1).upper()
            prefixes[p] = prefixes.get(p, 0) + 1
    return {p: c for p, c in prefixes.items() if c >= min_count}


def generate_home(client_slug: str = None, workspace_wide: bool = False) -> dict:
    """
    Generate a HOME.md navigation hub for a client (or workspace-wide).

    Uses Obsidian Dataview queries for the rich-rendering case and a
    plain-markdown fallback listing so the file is still useful outside
    Obsidian. Always overwrites the target HOME.md.

    Returns ``{path, pages_total, types_detected, prefixes_detected, top_tags}``.
    """
    if workspace_wide:
        scope_dir = WIKI_DIR
        out_path = os.path.join(WIKI_DIR, 'HOME.md')
        scope_name = 'Workspace'
        dv_from = '"wiki"'
    else:
        if not client_slug:
            raise ValueError('Provide client_slug or use workspace_wide=True')
        scope_dir = os.path.join(WIKI_DIR, client_slug)
        out_path = os.path.join(scope_dir, 'HOME.md')
        scope_name = client_slug
        dv_from = f'"wiki/{client_slug}"'

    pages: list[dict] = []
    if os.path.isdir(scope_dir):
        pattern = os.path.join(scope_dir, '**', '*.md')
        for page_path in sorted(glob.glob(pattern, recursive=True)):
            rel = os.path.relpath(page_path, scope_dir)
            parts = Path(rel).parts
            if any(p in EXCLUDED_DIRS for p in parts[:-1]):
                continue
            if os.path.basename(page_path) in EXCLUDED_FILES:
                continue
            if os.path.basename(page_path).upper() == 'HOME.MD':
                continue
            try:
                with open(page_path, 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception:
                continue
            fm, _ = parse_frontmatter(content)
            slug = os.path.splitext(os.path.basename(page_path))[0]
            pages.append({
                'slug': slug,
                'title': fm.get('title', slug),
                'type': fm.get('type', ''),
                'rel': rel.replace(os.sep, '/'),
            })

    # Group by type
    by_type: dict[str, list[dict]] = {}
    for p in pages:
        by_type.setdefault(p['type'] or 'unclassified', []).append(p)

    # Detect ID prefixes
    prefixes = _detect_id_prefixes([p['slug'] for p in pages])
    by_prefix: dict[str, list[dict]] = {}
    for p in pages:
        m = re.match(r'^([A-Z]{2,8})[-_]?\d+', p['slug'], re.IGNORECASE)
        if m:
            key = m.group(1).upper()
            if key in prefixes:
                by_prefix.setdefault(key, []).append(p)

    # Top tags (scoped)
    top_tags: list[tuple[str, int]] = []
    tags_data = tags_list()
    if tags_data:
        scoped: list[tuple[str, int]] = []
        for tag_name, info in tags_data.items():
            if client_slug and not workspace_wide:
                tag_pages = info.get('pages', [])
                count = sum(1 for tp in tag_pages if tp.startswith(client_slug + '/'))
            else:
                count = info.get('count', 0)
            if count > 0:
                scoped.append((tag_name, count))
        scoped.sort(key=lambda kv: -kv[1])
        top_tags = scoped[:10]

    # Render
    lines: list[str] = []
    lines.append(f'# {scope_name} — Home')
    lines.append('')
    lines.append(f'_Auto-generated by `mneme home generate`. Last updated: {datetime.now().strftime("%Y-%m-%d")}._')
    lines.append('')
    lines.append(f'Total pages: **{len(pages)}**')
    lines.append('')

    # By type
    lines.append('## By page type')
    lines.append('')
    lines.append('```dataview')
    lines.append(f'TABLE WITHOUT ID file.link AS Page, type, updated')
    lines.append(f'FROM {dv_from}')
    lines.append('WHERE type')
    lines.append('SORT type ASC, file.name ASC')
    lines.append('```')
    lines.append('')
    lines.append('<details><summary>Plain-markdown fallback (no Dataview)</summary>')
    lines.append('')
    for t in sorted(by_type.keys()):
        lines.append(f'### {t} ({len(by_type[t])})')
        lines.append('')
        for p in by_type[t][:50]:
            link = p['rel'][:-3] if p['rel'].endswith('.md') else p['rel']
            lines.append(f'- [[{link}|{p["title"]}]]')
        if len(by_type[t]) > 50:
            lines.append(f'- … and {len(by_type[t]) - 50} more')
        lines.append('')
    lines.append('</details>')
    lines.append('')

    # By ID prefix
    if by_prefix:
        lines.append('## By ID prefix')
        lines.append('')
        for prefix in sorted(by_prefix.keys()):
            lines.append(f'### {prefix}-* ({len(by_prefix[prefix])})')
            lines.append('')
            lines.append('```dataview')
            lines.append('TABLE WITHOUT ID file.link AS Page, updated')
            lines.append(f'FROM {dv_from}')
            lines.append(f'WHERE startswith(lower(file.name), "{prefix.lower()}")')
            lines.append('SORT file.name ASC')
            lines.append('```')
            lines.append('')
            lines.append('<details><summary>Plain-markdown fallback</summary>')
            lines.append('')
            for p in by_prefix[prefix][:50]:
                link = p['rel'][:-3] if p['rel'].endswith('.md') else p['rel']
                lines.append(f'- [[{link}|{p["title"]}]]')
            if len(by_prefix[prefix]) > 50:
                lines.append(f'- … and {len(by_prefix[prefix]) - 50} more')
            lines.append('')
            lines.append('</details>')
            lines.append('')

    # Top tags
    if top_tags:
        lines.append('## Top tags')
        lines.append('')
        lines.append('| Tag | Pages |')
        lines.append('|---|---:|')
        for tag_name, count in top_tags:
            lines.append(f'| `{tag_name}` | {count} |')
        lines.append('')

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    return {
        'path': out_path,
        'pages_total': len(pages),
        'types_detected': sorted(by_type.keys()),
        'prefixes_detected': sorted(prefixes.keys()),
        'top_tags': [t for t, _ in top_tags],
    }


def diff_page(page_slug: str) -> str:
    """
    Show git diff for a wiki page.

    Uses subprocess to run git diff HEAD on the wiki page file.
    If the slug has no .md extension, one is appended.
    Returns the diff output string, or an error message if not in a git repo.
    """
    if not page_slug.endswith('.md'):
        page_slug = page_slug + '.md'

    page_path = os.path.join('wiki', page_slug)

    try:
        result = subprocess.run(
            ['git', 'diff', 'HEAD', '--', page_path],
            capture_output=True,
            text=True,
            cwd=BASE_DIR,
        )
        if result.returncode != 0 and result.stderr:
            return f'Error: {result.stderr.strip()}'
        return result.stdout if result.stdout else '(no changes)'
    except FileNotFoundError:
        return 'Error: git is not installed or not found in PATH'
    except Exception as e:
        return f'Error: {e}'


def snapshot(client_slug: str) -> dict:
    """
    Create a zip archive of a client's wiki pages and their schema entries.

    Saves to snapshots/{client_slug}-{date}.zip. Creates snapshots/ dir if needed.
    Also attempts to create a git tag snapshot/{client_slug}/{date}.

    Returns {path, pages_count, tag}.
    """
    today = datetime.now().strftime('%Y-%m-%d')
    snapshots_dir = os.path.join(BASE_DIR, 'snapshots')
    os.makedirs(snapshots_dir, exist_ok=True)

    zip_name = f'{client_slug}-{today}.zip'
    zip_path = os.path.join(snapshots_dir, zip_name)

    client_wiki_dir = os.path.join(WIKI_DIR, client_slug)
    pages_count = 0

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # Add wiki pages
        if os.path.isdir(client_wiki_dir):
            for root, dirs, files in os.walk(client_wiki_dir):
                for fname in files:
                    if fname.endswith('.md'):
                        fpath = os.path.join(root, fname)
                        arcname = os.path.relpath(fpath, BASE_DIR)
                        zf.write(fpath, arcname)
                        pages_count += 1

        # Add schema files with client-relevant entries
        for schema_name in ('entities.json', 'graph.json', 'tags.json'):
            schema_path = os.path.join(SCHEMA_DIR, schema_name)
            if os.path.exists(schema_path):
                zf.write(schema_path, os.path.relpath(schema_path, BASE_DIR))

    # Try to create a git tag
    tag_name = f'snapshot/{client_slug}/{today}'
    try:
        subprocess.run(
            ['git', 'tag', tag_name, '-m', f'Snapshot of {client_slug} on {today}'],
            capture_output=True,
            text=True,
            cwd=BASE_DIR,
        )
    except Exception:
        pass

    _append_log(
        operation='UPDATE',
        description=f'Snapshot created for {client_slug}',
        details=[f'Path: {zip_path}', f'Pages: {pages_count}', f'Tag: {tag_name}'],
        date=today,
    )

    return {
        'path': zip_path,
        'pages_count': pages_count,
        'tag': tag_name,
    }


def dedupe() -> dict:
    """
    Scan all wiki pages and find duplicates by content hash.

    Computes a hash of the body text (after frontmatter) for each page,
    then groups pages with identical hashes.

    Returns {duplicates: list of groups, total_groups: int}.
    """
    hash_map = {}  # hash -> list of page slugs

    if os.path.isdir(WIKI_DIR):
        for root, dirs, files in os.walk(WIKI_DIR):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
            for fname in files:
                if not fname.endswith('.md') or fname in EXCLUDED_FILES:
                    continue
                fpath = os.path.join(root, fname)
                with open(fpath, 'r') as f:
                    content = f.read()

                _, body = parse_frontmatter(content)
                body_stripped = body.strip()
                if not body_stripped:
                    continue

                h = _content_hash(body_stripped)
                rel = os.path.relpath(fpath, WIKI_DIR).replace(os.sep, '/')
                slug = rel.replace('.md', '')
                hash_map.setdefault(h, []).append(slug)

    duplicates = [group for group in hash_map.values() if len(group) > 1]

    return {
        'duplicates': duplicates,
        'total_groups': len(duplicates),
    }


def export_client(client_slug: str, format: str = 'json') -> str:
    """
    Export all wiki pages for a client to a single file.

    For 'json' format: array of {slug, frontmatter, body} objects.
    For 'md' format: concatenated pages with separators.

    Writes output to exports/{client_slug}-{date}.{format}.
    Creates exports/ dir if needed. Returns the output file path.
    """
    today = datetime.now().strftime('%Y-%m-%d')
    exports_dir = os.path.join(BASE_DIR, 'exports')
    os.makedirs(exports_dir, exist_ok=True)

    client_wiki_dir = os.path.join(WIKI_DIR, client_slug)
    pages = []

    if os.path.isdir(client_wiki_dir):
        for root, dirs, files in os.walk(client_wiki_dir):
            for fname in sorted(files):
                if not fname.endswith('.md'):
                    continue
                fpath = os.path.join(root, fname)
                with open(fpath, 'r') as f:
                    content = f.read()

                fm, body = parse_frontmatter(content)
                rel = os.path.relpath(fpath, WIKI_DIR).replace(os.sep, '/')
                slug = rel.replace('.md', '')
                pages.append({
                    'slug': slug,
                    'frontmatter': fm,
                    'body': body,
                })

    ext = 'json' if format == 'json' else 'md'
    out_file = os.path.join(exports_dir, f'{client_slug}-{today}.{ext}')

    if format == 'json':
        with open(out_file, 'w') as f:
            json.dump(pages, f, indent=2, default=str)
    else:
        with open(out_file, 'w') as f:
            for page in pages:
                f.write(f'<!-- {page["slug"]} -->\n')
                # Reconstruct frontmatter
                if page['frontmatter']:
                    f.write('---\n')
                    for k, v in page['frontmatter'].items():
                        if isinstance(v, list):
                            f.write(f'{k}:\n')
                            for item in v:
                                f.write(f'  - {item}\n')
                        else:
                            f.write(f'{k}: {v}\n')
                    f.write('---\n')
                f.write(page['body'])
                f.write('\n\n---\n\n')

    return out_file


def _resolve_profile_path(name: str) -> Optional[str]:
    """
    Resolve a profile name to a `.md` path.

    Profiles are markdown files. The lookup order is:
      1. {WORKSPACE_DIR}/profiles/{name}.md   (per-project, not packaged)
      2. {PACKAGE_DIR}/profiles/{name}.md     (bundled with mneme)

    Workspace profiles shadow bundled profiles with the same name, so a
    project can override an industry profile with a project-specific tweak.

    Returns the absolute path to the .md file, or None if neither exists.
    """
    workspace_path = os.path.join(WORKSPACE_PROFILES_DIR, f'{name}.md')
    if os.path.exists(workspace_path):
        return workspace_path
    bundled_path = os.path.join(PROFILES_DIR, f'{name}.md')
    if os.path.exists(bundled_path):
        return bundled_path
    return None


# ---------------------------------------------------------------------------
# Markdown profile parser
# ---------------------------------------------------------------------------
#
# A profile is a markdown file with YAML frontmatter. The frontmatter carries
# the structured fields (vocabulary, trace_types, tone, ...) and the body
# carries the writing-style prose under recognized H1 headings.
#
# Recognized H1 headings (case-insensitive on the prefix):
#   # Principles                       -> writing_style.principles (- bullets)
#   # General Rules                    -> writing_style.general_rules (- bullets)
#   # Terminology                      -> writing_style.terminology_guidance
#                                         (parsed from a 3-column markdown table:
#                                          Use | Instead of | Why)
#   # Framing: <context label>         -> one entry in writing_style.framing_examples
#                                         body parses **Wrong:** **Correct:** **Why:**
#   # Document Type: <slug>            -> sections[<slug>]
#                                         body before any "## Section: ..." -> description
#   ## Section: <slug>                 -> sections[<doc-type>].section_notes[<section-slug>]
#                                         (must appear under a # Document Type heading)
#   # Submission Checklist             -> submission_checklist (- bullets)
#
# Frontmatter conventions (note these differ slightly from the in-memory dict
# shape so the .md format reads naturally):
#   vocabulary:                          -> profile['vocabulary']['preferred']
#     - use: medical device                (each `use:` becomes the in-memory `term:`)
#       reject: [product, unit]
#   requirement_levels:                  -> profile['vocabulary']['requirement_levels']
#     shall: mandatory


def _parse_md_profile_frontmatter_value(text: str):
    """
    Parse a YAML-ish value from a profile frontmatter line.
    Handles plain strings, [a, b, c] inline lists, quoted strings, and
    `${"true", "false", null}`. Multi-line nested structures are handled
    by the block parser, not here.
    """
    text = text.strip()
    if not text:
        return ''
    if text.startswith('[') and text.endswith(']'):
        inner = text[1:-1].strip()
        if not inner:
            return []
        items = []
        for raw in re.split(r',\s*', inner):
            s = raw.strip()
            if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
                s = s[1:-1]
            items.append(s)
        return items
    if text.lower() in ('true', 'false'):
        return text.lower() == 'true'
    if text.lower() in ('null', 'none', '~'):
        return None
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        return text[1:-1]
    return text


def _parse_md_profile_frontmatter(fm_text: str) -> dict:
    """
    Parse YAML frontmatter for an .md profile. Supports:
      - scalar key: value
      - inline list `key: [a, b]`
      - block list:
            key:
              - item1
              - item2
      - nested mapping under a key:
            requirement_levels:
              shall: mandatory
              should: recommended
      - list of inline mappings (the vocabulary case):
            vocabulary:
              - use: medical device
                reject: [product, unit]
              - use: intended purpose
                reject: [intended use]

    Returns a plain dict. This is intentionally tiny - we don't pull in
    PyYAML because mneme's existing parse_frontmatter is already a hand-
    rolled mini-YAML and we want to stay dependency-free.
    """
    lines = fm_text.split('\n')
    result: dict = {}
    i = 0
    n = len(lines)

    def line_indent(s: str) -> int:
        return len(s) - len(s.lstrip(' '))

    while i < n:
        line = lines[i]
        if not line.strip() or line.lstrip().startswith('#'):
            i += 1
            continue
        if ':' not in line:
            i += 1
            continue
        indent = line_indent(line)
        if indent != 0:
            i += 1
            continue
        key, _, rest = line.strip().partition(':')
        key = key.strip()
        rest = rest.strip()

        if rest:
            # Scalar or inline list on the same line
            result[key] = _parse_md_profile_frontmatter_value(rest)
            i += 1
            continue

        # Block follow: peek ahead to decide list-of-dicts vs list-of-scalars vs nested map
        i += 1
        block_lines = []
        while i < n and (not lines[i].strip() or line_indent(lines[i]) > 0):
            block_lines.append(lines[i])
            i += 1

        if not block_lines:
            result[key] = ''
            continue

        # Strip leading common indent
        nonblank = [l for l in block_lines if l.strip()]
        if not nonblank:
            result[key] = ''
            continue

        first = nonblank[0]
        if first.lstrip().startswith('-'):
            # List under this key. Each "- foo" or "- key: value" starts a new item.
            items = []
            current_item = None
            for bl in block_lines:
                if not bl.strip():
                    continue
                stripped = bl.lstrip()
                if stripped.startswith('-'):
                    if current_item is not None:
                        items.append(current_item)
                    after_dash = stripped[1:].strip()
                    if ':' in after_dash and not after_dash.startswith('['):
                        # First field of a mapping item
                        sub_k, _, sub_v = after_dash.partition(':')
                        current_item = {sub_k.strip(): _parse_md_profile_frontmatter_value(sub_v)}
                    else:
                        # Plain scalar item like "- foo"
                        items.append(_parse_md_profile_frontmatter_value(after_dash))
                        current_item = None
                else:
                    # Continuation of the previous mapping item
                    if current_item is None:
                        continue
                    if ':' in stripped:
                        sub_k, _, sub_v = stripped.partition(':')
                        current_item[sub_k.strip()] = _parse_md_profile_frontmatter_value(sub_v)
            if current_item is not None:
                items.append(current_item)
            result[key] = items
        else:
            # Nested mapping. Each "key: value" indented under the parent.
            nested: dict = {}
            for bl in block_lines:
                if not bl.strip():
                    continue
                stripped = bl.strip()
                if ':' in stripped:
                    sub_k, _, sub_v = stripped.partition(':')
                    nested[sub_k.strip()] = _parse_md_profile_frontmatter_value(sub_v)
            result[key] = nested

    return result


def _split_md_profile_body_by_h1(body: str) -> list[tuple[str, str]]:
    """
    Split a markdown body into a list of (heading_text, section_body) pairs,
    one per top-level (#) heading. Lines before the first H1 are dropped.
    """
    sections = []
    current_heading = None
    current_lines: list[str] = []
    for line in body.split('\n'):
        m = re.match(r'^#\s+(.+?)\s*$', line)
        if m:
            if current_heading is not None:
                sections.append((current_heading, '\n'.join(current_lines).strip('\n')))
            current_heading = m.group(1).strip()
            current_lines = []
        else:
            if current_heading is not None:
                current_lines.append(line)
    if current_heading is not None:
        sections.append((current_heading, '\n'.join(current_lines).strip('\n')))
    return sections


def _parse_md_profile_bullets(text: str) -> list[str]:
    """Extract `- foo` bullets from a block of text."""
    items = []
    for line in text.split('\n'):
        stripped = line.lstrip()
        if stripped.startswith('- '):
            items.append(stripped[2:].strip())
    return items


def _parse_md_profile_terminology_table(text: str) -> list[dict]:
    """
    Parse a 3-column markdown table:
        | Use | Instead of | Why |
        |---|---|---|
        | foo | bar, baz | rationale |
    Returns a list of {use, instead_of: [...], rationale} dicts.
    """
    rows = []
    in_table = False
    for line in text.split('\n'):
        s = line.strip()
        if not s.startswith('|'):
            continue
        cells = [c.strip() for c in s.strip('|').split('|')]
        if all(set(c) <= {'-', ':', ' '} for c in cells):
            in_table = True
            continue
        if not in_table and any('use' in c.lower() and 'instead' not in c.lower() for c in cells):
            # The header row (Use | Instead of | Why) - skip it
            continue
        if in_table and len(cells) >= 3:
            use = cells[0]
            instead = [s.strip() for s in cells[1].split(',') if s.strip()]
            rationale = cells[2]
            rows.append({'use': use, 'instead_of': instead, 'rationale': rationale})
    return rows


def _parse_md_profile_framing_block(text: str) -> dict:
    """
    Parse a Framing block. The body looks like:

        **Wrong:**

        > offending text...

        **Correct:**

        > replacement text...

        **Why:** rationale...

    Returns {wrong, correct, why}.
    """
    out = {'wrong': '', 'correct': '', 'why': ''}
    # Find the three labeled blocks. Each label may be followed by a paragraph
    # or a blockquote. We capture everything until the next label or EOF.
    labels = [('wrong', r'\*\*Wrong:\*\*'),
              ('correct', r'\*\*Correct:\*\*'),
              ('why', r'\*\*Why:\*\*')]
    label_pattern = '|'.join(p for _, p in labels)
    parts = re.split(f'({label_pattern})', text)
    # parts is alternating: [pre, label1, body1, label2, body2, ...]
    current_key = None
    for chunk in parts:
        if not chunk:
            continue
        matched_key = None
        for key, pat in labels:
            if re.fullmatch(pat, chunk.strip()):
                matched_key = key
                break
        if matched_key:
            current_key = matched_key
            continue
        if current_key is not None:
            # Strip blockquote markers and collapse whitespace
            cleaned_lines = []
            for line in chunk.split('\n'):
                line = line.lstrip()
                if line.startswith('>'):
                    line = line[1:].lstrip()
                cleaned_lines.append(line)
            text_value = ' '.join(l for l in cleaned_lines if l).strip()
            if text_value:
                out[current_key] = (out[current_key] + ' ' + text_value).strip() if out[current_key] else text_value
    return out


def _parse_md_profile_doctype_body(body: str) -> tuple[str, dict]:
    """
    For a `# Document Type: <slug>` body, separate the description (everything
    before the first `## Section:` heading) from the section_notes (everything
    after, keyed by section slug).
    """
    description_lines = []
    section_notes: dict = {}
    current_section = None
    current_lines: list[str] = []

    for line in body.split('\n'):
        m = re.match(r'^##\s+Section:\s*(.+?)\s*$', line, re.IGNORECASE)
        if m:
            if current_section is not None:
                section_notes[current_section] = '\n'.join(current_lines).strip()
            current_section = m.group(1).strip()
            current_lines = []
        elif current_section is not None:
            current_lines.append(line)
        else:
            description_lines.append(line)

    if current_section is not None:
        section_notes[current_section] = '\n'.join(current_lines).strip()

    description = '\n'.join(description_lines).strip()
    return description, section_notes


def _load_profile_from_md(path: str) -> dict:
    """
    Parse an .md profile file into the in-memory dict shape that the rest of
    mneme already consumes (the same shape `harmonize`, `validate_writing_style`
    etc. expect).

    The .md format is documented above _parse_md_profile_frontmatter.
    """
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Use the existing simple parser to split frontmatter from body
    fm_match = re.match(r'^---\s*\n(.*?)\n---\s*\n?(.*)$', content, re.DOTALL)
    if not fm_match:
        raise ValueError(
            f'Profile {path} is missing a YAML frontmatter block (--- ... ---).'
        )
    fm_text = fm_match.group(1)
    body = fm_match.group(2)

    fm = _parse_md_profile_frontmatter(fm_text)

    # Build the in-memory profile dict
    profile: dict = {
        'name': fm.get('name', ''),
        'description': fm.get('description', ''),
        'version': fm.get('version', ''),
        'tone': fm.get('tone', ''),
        'voice': fm.get('voice', ''),
        'citation_style': fm.get('citation_style', ''),
        'trace_types': fm.get('trace_types', []) or [],
        'vocabulary': {
            'preferred': [],
            'requirement_levels': fm.get('requirement_levels', {}) or {},
        },
        'sections': {},
        'writing_style': {},
        'submission_checklist': [],
    }

    # Vocabulary: rename `use` -> `term` so harmonize sees the existing shape
    raw_vocab = fm.get('vocabulary', []) or []
    for entry in raw_vocab:
        if not isinstance(entry, dict):
            continue
        term = entry.get('use') or entry.get('term') or ''
        reject = entry.get('reject') or []
        if isinstance(reject, str):
            reject = [r.strip() for r in reject.split(',') if r.strip()]
        if term:
            profile['vocabulary']['preferred'].append({'term': term, 'reject': reject})

    if 'placeholder_for_missing_refs' in fm:
        profile.setdefault('writing_style', {})
        profile['writing_style']['placeholder_for_missing_refs'] = fm['placeholder_for_missing_refs']

    # Body sections
    h1_sections = _split_md_profile_body_by_h1(body)
    framing_examples: list[dict] = []

    for heading, section_body in h1_sections:
        h_lower = heading.lower().strip()

        if h_lower == 'principles':
            profile['writing_style']['principles'] = _parse_md_profile_bullets(section_body)
        elif h_lower in ('general rules', 'general writing rules'):
            profile['writing_style']['general_rules'] = _parse_md_profile_bullets(section_body)
        elif h_lower in ('terminology', 'terminology guidance'):
            profile['writing_style']['terminology_guidance'] = _parse_md_profile_terminology_table(section_body)
        elif h_lower.startswith('framing:') or h_lower.startswith('framing -'):
            context_label = heading.split(':', 1)[1].strip() if ':' in heading else heading.split('-', 1)[1].strip()
            block = _parse_md_profile_framing_block(section_body)
            entry = {'context': context_label}
            entry.update(block)
            framing_examples.append(entry)
        elif h_lower.startswith('document type:') or h_lower.startswith('document type -'):
            doc_type = heading.split(':', 1)[1].strip() if ':' in heading else heading.split('-', 1)[1].strip()
            description, section_notes = _parse_md_profile_doctype_body(section_body)
            profile['sections'][doc_type] = {
                'description': description,
                'section_notes': section_notes,
            }
        elif h_lower in ('submission checklist', 'checklist'):
            profile['submission_checklist'] = _parse_md_profile_bullets(section_body)
        # Unknown headings are silently ignored - they may be authoring notes

    if framing_examples:
        profile['writing_style']['framing_examples'] = framing_examples

    return profile


def load_profile(name: str) -> dict:
    """
    Load a profile by name. Workspace profiles shadow bundled profiles.
    Profiles are markdown files; see `_load_profile_from_md` for the format.
    """
    path = _resolve_profile_path(name)
    if path is None:
        raise FileNotFoundError(
            f'Profile not found: "{name}.md". Looked in '
            f'{WORKSPACE_PROFILES_DIR} (workspace) and {PROFILES_DIR} (bundled).'
        )
    return _load_profile_from_md(path)


def get_active_profile() -> Optional[dict]:
    """
    Read .mneme-profile to get the active profile name, then load it.

    Returns the profile dict, or None if no active profile is set.
    """
    if not os.path.exists(ACTIVE_PROFILE_FILE):
        return None

    with open(ACTIVE_PROFILE_FILE, 'r') as f:
        name = f.read().strip()

    if not name:
        return None

    try:
        profile = load_profile(name)
        profile['_profile_name'] = name
        return profile
    except FileNotFoundError:
        return None


def set_active_profile(name: str) -> None:
    """
    Set the active profile for this workspace.

    Accepts either a workspace-local profile (in {workspace}/profiles/)
    or a bundled profile (in the package). The profile must exist before
    it can be activated.
    """
    if _resolve_profile_path(name) is None:
        raise FileNotFoundError(
            f'Profile not found: "{name}". Looked in '
            f'{WORKSPACE_PROFILES_DIR} (workspace) and {PROFILES_DIR} (bundled).'
        )
    with open(ACTIVE_PROFILE_FILE, 'w', encoding='utf-8') as f:
        f.write(name + '\n')


# ---------------------------------------------------------------------------
# QMS: Traceability
# ---------------------------------------------------------------------------

def _load_traceability() -> dict:
    """Load traceability.json, creating empty structure if missing."""
    if os.path.exists(TRACEABILITY_FILE):
        try:
            with open(TRACEABILITY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict) and 'links' in data:
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {'version': 1, 'updated': '', 'links': []}


def trace_add(from_page: str, to_page: str, relationship: str) -> dict:
    """
    Add a trace link between two wiki pages.

    Stores the link in schema/traceability.json and updates the from_page's
    frontmatter to include a 'traces' field if not already present.
    Validates both pages exist under wiki/.
    Returns {from, to, type, created}.
    """
    today = datetime.now().strftime('%Y-%m-%d')

    # Validate both pages exist
    from_path = os.path.join(WIKI_DIR, from_page + '.md')
    to_path = os.path.join(WIKI_DIR, to_page + '.md')
    if not os.path.exists(from_path):
        return {'error': f'Page not found: {from_page}'}
    if not os.path.exists(to_path):
        return {'error': f'Page not found: {to_page}'}

    link_entry = {
        'from': from_page,
        'to': to_page,
        'type': relationship,
        'created': today,
    }

    os.makedirs(SCHEMA_DIR, exist_ok=True)

    def modifier(raw: str) -> str:
        if raw.strip():
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {'version': 1, 'updated': today, 'links': []}
        else:
            data = {'version': 1, 'updated': today, 'links': []}

        if not isinstance(data.get('links'), list):
            data['links'] = []

        # Avoid duplicate links
        for existing in data['links']:
            if (existing.get('from') == from_page and
                    existing.get('to') == to_page and
                    existing.get('type') == relationship):
                return json.dumps(data, indent=2)

        data['links'].append(link_entry)
        data['updated'] = today
        return json.dumps(data, indent=2)

    _locked_read_modify_write(TRACEABILITY_FILE, modifier)

    # Update from_page frontmatter to include traces field
    with open(from_path, 'r', encoding='utf-8') as f:
        content = f.read()

    fm, body = parse_frontmatter(content)
    traces = fm.get('traces', [])
    if isinstance(traces, str):
        traces = [traces]
    trace_ref = f'[[{to_page}]] ({relationship})'
    if trace_ref not in traces:
        traces.append(trace_ref)

    # Rebuild frontmatter with traces field
    if 'traces' not in content.split('---')[1] if content.startswith('---') else True:
        # Insert traces field into frontmatter
        match = re.match(r'^(---\s*\n)(.*?)(\n---\s*\n)', content, re.DOTALL)
        if match:
            fm_text = match.group(2)
            traces_yaml = '\n'.join(f'  - "{t}"' for t in traces)
            new_fm = fm_text + f'\ntraces:\n{traces_yaml}'
            content = match.group(1) + new_fm + match.group(3) + body
            with open(from_path, 'w', encoding='utf-8') as f:
                f.write(content)
    else:
        # Update existing traces field
        match = re.match(r'^(---\s*\n)(.*?)(\n---\s*\n)', content, re.DOTALL)
        if match:
            fm_text = match.group(2)
            # Remove old traces block
            fm_text = re.sub(r'traces:\n(?:\s+-\s+.*\n)*', '', fm_text)
            traces_yaml = '\n'.join(f'  - "{t}"' for t in traces)
            new_fm = fm_text.rstrip() + f'\ntraces:\n{traces_yaml}'
            content = match.group(1) + new_fm + match.group(3) + body
            with open(from_path, 'w', encoding='utf-8') as f:
                f.write(content)

    _append_log(
        operation='TRACE',
        description=f'Added trace: {from_page} --{relationship}--> {to_page}',
        details=[f'Type: {relationship}', f'From: {from_page}', f'To: {to_page}'],
        date=today,
    )

    return {'from': from_page, 'to': to_page, 'type': relationship, 'created': today}


def trace_show(page_slug: str, direction: str = 'forward') -> dict:
    """
    Walk the trace chain from a page using BFS.

    direction='forward': follow from-links outward (page -> what it links to -> ...).
    direction='backward': follow to-links inward (what links to page -> ...).
    Returns {root, direction, chain: [{page, relationship, depth}]}.
    """
    data = _load_traceability()
    links = data.get('links', [])

    # Build adjacency lists
    forward: dict[str, list] = {}  # from -> [(to, type)]
    backward: dict[str, list] = {}  # to -> [(from, type)]
    for link in links:
        f = link.get('from', '')
        t = link.get('to', '')
        rel = link.get('type', '')
        forward.setdefault(f, []).append((t, rel))
        backward.setdefault(t, []).append((f, rel))

    adj = forward if direction == 'forward' else backward

    # BFS
    visited = set()
    queue = [(page_slug, 0)]
    visited.add(page_slug)
    chain = []

    while queue:
        current, depth = queue.pop(0)
        for neighbor, rel in adj.get(current, []):
            if neighbor not in visited:
                visited.add(neighbor)
                chain.append({
                    'page': neighbor,
                    'relationship': rel,
                    'depth': depth + 1,
                })
                queue.append((neighbor, depth + 1))

    return {'root': page_slug, 'direction': direction, 'chain': chain}


def trace_matrix(client_slug: str) -> dict:
    """
    Generate a traceability matrix for a client.

    Reads all trace links, filters to those involving client pages.
    Returns {rows, columns, cells, gaps}.
    """
    data = _load_traceability()
    links = data.get('links', [])

    # Filter links to client pages
    client_links = [
        link for link in links
        if link.get('from', '').startswith(client_slug + '/') or
           link.get('to', '').startswith(client_slug + '/')
    ]

    # Collect all client pages involved in traces
    all_pages = set()
    for link in client_links:
        f = link.get('from', '')
        t = link.get('to', '')
        if f.startswith(client_slug + '/'):
            all_pages.add(f)
        if t.startswith(client_slug + '/'):
            all_pages.add(t)

    rows = sorted(all_pages)
    columns = sorted(all_pages)

    # Build cells: (from, to) -> relationship
    cells = {}
    has_outgoing = set()
    has_incoming = set()
    for link in client_links:
        f = link.get('from', '')
        t = link.get('to', '')
        rel = link.get('type', '')
        cells[f'{f}|{t}'] = rel
        has_outgoing.add(f)
        has_incoming.add(t)

    # Gaps: pages with no outgoing or no incoming traces
    gaps = [p for p in all_pages if p not in has_outgoing or p not in has_incoming]

    return {
        'rows': rows,
        'columns': columns,
        'cells': cells,
        'gaps': sorted(gaps),
    }


def trace_gaps(client_slug: str) -> dict:
    """
    Find items with broken or incomplete trace chains for a client.

    Checks for:
    - Requirements with no verification trace
    - Hazards with no mitigation trace
    - User needs with no requirements trace
    Returns {unverified, unmitigated, unlinked_needs, total_gaps}.
    """
    data = _load_traceability()
    links = data.get('links', [])

    # Build sets of pages that are targets of specific relationship types
    verified_pages = set()    # pages that have a 'verified-by' outgoing link
    mitigated_pages = set()   # pages that have a 'mitigated-by' outgoing link
    linked_needs = set()      # user-need pages that have a 'derived-from' incoming or outgoing link

    for link in links:
        f = link.get('from', '')
        t = link.get('to', '')
        rel = link.get('type', '')
        if rel == 'verified-by':
            verified_pages.add(f)
        if rel == 'mitigated-by':
            mitigated_pages.add(f)
        if rel in ('derived-from', 'implemented-by', 'detailed-in'):
            linked_needs.add(f)
            linked_needs.add(t)

    # Scan wiki pages for the client to classify by type from frontmatter
    pattern = os.path.join(WIKI_DIR, client_slug, '**', '*.md')
    pages = glob.glob(pattern, recursive=True)

    unverified = []
    unmitigated = []
    unlinked_needs = []

    for page_path in pages:
        rel = os.path.relpath(page_path, WIKI_DIR)
        # Normalize to forward-slash slugs so they match the format used by
        # trace_add / _store_trace_link (which always store "client/page").
        # Without this, on Windows os.path.relpath returns backslashes and the
        # `slug not in verified_pages` check below would always be true.
        slug = os.path.splitext(rel)[0].replace(os.sep, '/')
        try:
            with open(page_path, 'r', encoding='utf-8') as f:
                content = f.read()
            fm, body = parse_frontmatter(content)
        except Exception:
            continue

        page_type = fm.get('type', '')
        tags = fm.get('tags', [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(',')]
        title_lower = fm.get('title', '').lower()

        # Heuristic classification
        is_requirement = (
            page_type in ('entity', 'concept') and
            any(kw in title_lower for kw in ('requirement', 'req', 'specification', 'spec'))
        ) or 'requirement' in tags

        is_hazard = (
            any(kw in title_lower for kw in ('hazard', 'risk', 'failure'))
        ) or 'hazard' in tags or 'risk' in tags

        is_user_need = (
            any(kw in title_lower for kw in ('user need', 'user-need', 'intended use', 'intended purpose'))
        ) or 'user-need' in tags

        if is_requirement and slug not in verified_pages:
            unverified.append(slug)
        if is_hazard and slug not in mitigated_pages:
            unmitigated.append(slug)
        if is_user_need and slug not in linked_needs:
            unlinked_needs.append(slug)

    total_gaps = len(unverified) + len(unmitigated) + len(unlinked_needs)
    return {
        'unverified': sorted(unverified),
        'unmitigated': sorted(unmitigated),
        'unlinked_needs': sorted(unlinked_needs),
        'total_gaps': total_gaps,
    }


# ---------------------------------------------------------------------------
# QMS: Vocabulary and structure validation
# ---------------------------------------------------------------------------

def harmonize(client_slug: str, fix: bool = False) -> dict:
    """
    Scan wiki pages for vocabulary inconsistencies against the active profile.

    Loads the active profile and checks each client wiki page for rejected
    synonyms of preferred terms. If fix=True, replaces rejected terms with
    preferred terms in all affected pages.
    Returns {issues, total_issues, pages_fixed (if fix)}.
    """
    profile = get_active_profile()
    if profile is None:
        return {'error': 'No active profile. Set one with: mneme profile activate <name>'}

    vocabulary = profile.get('vocabulary', {})
    preferred_terms = vocabulary.get('preferred', [])
    if not preferred_terms:
        return {'issues': [], 'total_issues': 0}

    # Collect client wiki pages
    pattern = os.path.join(WIKI_DIR, client_slug, '**', '*.md')
    pages = glob.glob(pattern, recursive=True)

    issues = []
    pages_fixed = 0
    today = datetime.now().strftime('%Y-%m-%d')

    for page_path in pages:
        rel = os.path.relpath(page_path, WIKI_DIR)
        slug = os.path.splitext(rel)[0]

        try:
            with open(page_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception:
            continue

        page_modified = False
        new_content = content

        for entry in preferred_terms:
            preferred = entry.get('term', '')
            rejects = entry.get('reject', [])
            for rejected in rejects:
                # Case-insensitive search for rejected term
                matches = re.findall(re.escape(rejected), content, re.IGNORECASE)
                if matches:
                    issues.append({
                        'page': slug,
                        'found_term': rejected,
                        'preferred_term': preferred,
                        'count': len(matches),
                    })
                    if fix:
                        new_content = re.sub(
                            re.escape(rejected), preferred, new_content, flags=re.IGNORECASE
                        )
                        page_modified = True

        if fix and page_modified:
            with open(page_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            pages_fixed += 1

    result = {
        'issues': issues,
        'total_issues': len(issues),
    }

    if fix:
        result['pages_fixed'] = pages_fixed
        if pages_fixed > 0:
            _append_log(
                operation='HARMONIZE',
                description=f'Vocabulary harmonization for {client_slug}',
                details=[
                    f'Profile: {profile.get("_profile_name", "unknown")}',
                    f'Issues found: {len(issues)}',
                    f'Pages fixed: {pages_fixed}',
                ],
                date=today,
            )

    return result


def validate_writing_style(page_slug: str) -> dict:
    """
    Build a "writing-style review packet" for an LLM agent.

    Mneme does not grade prose itself - that requires reasoning. Instead this
    function assembles everything an agent needs to grade the page against
    the active profile's writing style:

      - the page content
      - the active profile's writing_style block (principles, general_rules,
        terminology_guidance, framing_examples)
      - the section_notes for the document type that matches the page (resolved
        from the page's frontmatter `type:` field; falls back to no notes if
        there is no match)
      - the profile's submission_checklist
      - a ready-to-paste review prompt the user can hand to any LLM

    Returns a dict containing all of the above. CLI rendering is handled by
    the caller; see `--json` for raw output.

    The page's frontmatter `type:` field is the canonical way to associate a
    page with a profile section. If a profile defines `sections.foo`, the
    user should set `type: foo` in the page frontmatter to opt into the
    `section_notes` for `foo`.
    """
    profile = get_active_profile()
    if profile is None:
        return {'error': 'No active profile. Set one with: mneme profile set <name>'}

    page_path = os.path.join(WIKI_DIR, page_slug + '.md')
    if not os.path.exists(page_path):
        return {'error': f'Page not found: {page_slug}'}

    with open(page_path, 'r', encoding='utf-8') as f:
        content = f.read()

    fm, body = parse_frontmatter(content)
    page_type = (fm.get('type') or '').strip()

    sections_config = profile.get('sections', {})
    matched_section = None
    section_notes: dict = {}
    section_description = ''
    if page_type and page_type in sections_config:
        matched_section = page_type
        sec_def = sections_config[page_type]
        section_notes = sec_def.get('section_notes', {}) or {}
        section_description = sec_def.get('description', '')

    writing_style = profile.get('writing_style', {}) or {}
    submission_checklist = profile.get('submission_checklist', []) or []

    # Build a copy-pasteable review prompt for any LLM agent.
    review_prompt = (
        f'You are reviewing the wiki page below against the writing style of the '
        f'"{profile.get("name", "active")}" profile. Use the principles, general '
        f'rules, terminology guidance, framing examples, and any section-specific '
        f'notes provided. For each issue you find, quote the offending text, '
        f'explain why it violates the style, and propose a concrete rewrite. '
        f'Then walk the submission checklist and report pass/fail per item with '
        f'a one-line justification. Be specific. Do not hedge.'
    )

    today = datetime.now().strftime('%Y-%m-%d')
    _append_log(
        operation='VALIDATE-STYLE',
        description=f'Writing-style review packet built for {page_slug}',
        details=[
            f'Profile: {profile.get("name", "?")}',
            f'Document type: {matched_section or "(none matched)"}',
            f'Section notes available: {len(section_notes)}',
        ],
        date=today,
    )

    return {
        'page': page_slug,
        'page_path': os.path.relpath(page_path, BASE_DIR).replace(os.sep, '/'),
        'page_content': content,
        'frontmatter': fm,
        'profile_name': profile.get('name', ''),
        'profile_description': profile.get('description', ''),
        'document_type': matched_section,
        'section_description': section_description,
        'section_notes': section_notes,
        'writing_style': writing_style,
        'submission_checklist': submission_checklist,
        'review_prompt': review_prompt,
    }


def _format_writing_style_packet(packet: dict) -> str:
    """Render a validate_writing_style packet as a markdown text packet
    suitable for piping into Claude Code or pasting into an LLM chat."""
    lines: list[str] = []
    lines.append(f'# Writing-style review packet')
    lines.append('')
    lines.append(f'- **Page:** `{packet["page_path"]}`')
    lines.append(f'- **Profile:** {packet["profile_name"]}')
    if packet['document_type']:
        lines.append(f'- **Document type:** `{packet["document_type"]}`')
        if packet['section_description']:
            lines.append(f'  - {packet["section_description"]}')
    else:
        lines.append('- **Document type:** (none matched - frontmatter `type` did '
                     'not match any profile section; only the general writing style applies)')
    lines.append('')
    lines.append('---')
    lines.append('')

    lines.append('## Review prompt')
    lines.append('')
    lines.append(packet['review_prompt'])
    lines.append('')

    style = packet.get('writing_style') or {}

    principles = style.get('principles') or []
    if principles:
        lines.append('## Principles')
        lines.append('')
        for p in principles:
            lines.append(f'- {p}')
        lines.append('')

    general_rules = style.get('general_rules') or []
    if general_rules:
        lines.append('## General writing rules')
        lines.append('')
        for r in general_rules:
            lines.append(f'- {r}')
        lines.append('')

    terminology = style.get('terminology_guidance') or []
    if terminology:
        lines.append('## Terminology guidance')
        lines.append('')
        lines.append('| Use | Instead of | Why |')
        lines.append('|---|---|---|')
        for entry in terminology:
            instead = ', '.join(entry.get('instead_of', []))
            lines.append(f'| {entry.get("use", "")} | {instead} | {entry.get("rationale", "")} |')
        lines.append('')

    framing = style.get('framing_examples') or []
    if framing:
        lines.append('## Framing examples')
        lines.append('')
        for ex in framing:
            lines.append(f'**Context:** {ex.get("context", "")}')
            lines.append('')
            lines.append('**Wrong:**')
            lines.append('')
            lines.append(f'> {ex.get("wrong", "")}')
            lines.append('')
            lines.append('**Correct:**')
            lines.append('')
            lines.append(f'> {ex.get("correct", "")}')
            lines.append('')
            if ex.get('why'):
                lines.append(f'*Why:* {ex["why"]}')
                lines.append('')

    if style.get('placeholder_for_missing_refs'):
        lines.append(f'**Missing-reference placeholder:** `{style["placeholder_for_missing_refs"]}`')
        lines.append('')

    section_notes = packet.get('section_notes') or {}
    if section_notes:
        lines.append(f'## Section-specific notes for `{packet["document_type"]}`')
        lines.append('')
        for slug, note in section_notes.items():
            lines.append(f'### `{slug}`')
            lines.append('')
            lines.append(note)
            lines.append('')

    checklist = packet.get('submission_checklist') or []
    if checklist:
        lines.append('## Submission checklist')
        lines.append('')
        for item in checklist:
            lines.append(f'- [ ] {item}')
        lines.append('')

    lines.append('---')
    lines.append('')
    lines.append('## Page content')
    lines.append('')
    lines.append(packet['page_content'])
    lines.append('')

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Draft (write packet for an LLM agent)
# ---------------------------------------------------------------------------
#
# `mneme draft` is the symmetric counterpart to `mneme validate writing-style`:
#
#     validate writing-style  -> review packet (grade existing prose)
#     draft                   -> write packet  (produce new prose)
#
# Mneme does not call any LLM. It assembles a "write packet" the agent
# consumes to produce a single section of a document. The packet contains:
#
#   * The section's name and description
#   * The section's specific notes from the active profile
#   * The full writing-style block (principles, rules, terminology, framing)
#   * The submission checklist (so the agent knows what "done" looks like)
#   * Candidate evidence drawn either from a specific source file (--source)
#     or from a wiki text search (--query, defaults to the section name)
#   * A ready-to-paste write prompt
#
# Like the review packet, the agent receives this and writes the section.

def draft_document(
    doc_type: str,
    section: str,
    client_slug: str,
    source_path: Optional[str] = None,
    query: Optional[str] = None,
    k: int = 10,
) -> dict:
    """
    Build a write packet for an LLM agent that will produce one section of a
    document of type `doc_type` for client `client_slug`.

    Returns the packet as a dict. CLI rendering is the caller's job.
    """
    profile = get_active_profile()
    if profile is None:
        return {'error': 'No active profile. Set one with: mneme profile set <name>'}

    sections_config = profile.get('sections', {}) or {}
    if doc_type not in sections_config:
        available = ', '.join(sorted(sections_config.keys())) or '(none)'
        return {'error': f'Unknown doc-type "{doc_type}" for profile "{profile.get("name", "?")}". '
                         f'Available doc-types: {available}'}

    doc_def = sections_config[doc_type]
    section_notes = doc_def.get('section_notes', {}) or {}
    if section not in section_notes:
        available = ', '.join(sorted(section_notes.keys())) or '(none)'
        return {'error': f'Unknown section "{section}" for doc-type "{doc_type}". '
                         f'Available sections: {available}'}

    # Gather candidate evidence
    evidence: list[dict] = []

    if source_path:
        if not os.path.exists(source_path):
            return {'error': f'Source not found: {source_path}'}
        with open(source_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        evidence.append({
            'kind': 'explicit-source',
            'path': os.path.relpath(source_path, BASE_DIR).replace(os.sep, '/'),
            'content': content,
        })

    # Run a text search if asked, OR if no explicit source was given.
    # If neither --query nor --source was given, fall back to the section
    # name as a query so the agent always sees something.
    effective_query = query
    if effective_query is None and source_path is None:
        effective_query = section.replace('-', ' ')

    if effective_query:
        try:
            hits = _search_wiki_text(effective_query, k=k)
        except Exception:
            hits = []
        for hit in hits:
            wiki_path = hit.get('wiki_path', '')
            # Filter to the requested client only
            normalized = wiki_path.replace('\\', '/')
            if client_slug and not normalized.replace('wiki/', '').startswith(client_slug + '/'):
                continue
            evidence.append({
                'kind': 'wiki-search-hit',
                'path': wiki_path,
                'title': hit.get('title', ''),
                'score': hit.get('score', 0),
                'excerpt': hit.get('text', ''),
            })

    writing_style = profile.get('writing_style', {}) or {}
    submission_checklist = profile.get('submission_checklist', []) or []

    write_prompt = (
        f'You are writing the "{section}" section of a {doc_type} document for the '
        f'"{client_slug}" client, against the "{profile.get("name", "active")}" profile. '
        f'Use ONLY the evidence in this packet. Cite each non-trivial claim with a '
        f'source path. Where you cannot find a citation, insert the placeholder '
        f'{writing_style.get("placeholder_for_missing_refs", "[TO ADD REF]")} at '
        f'the exact spot. Follow the writing style block strictly: principles, '
        f'general rules, terminology guidance, and framing examples are all '
        f'normative. Do not invent facts. Do not use editorial language. Output '
        f'a single markdown section starting with `## {section}` followed by the '
        f'body. No frontmatter, no surrounding prose.'
    )

    today = datetime.now().strftime('%Y-%m-%d')
    _append_log(
        operation='DRAFT-PACKET',
        description=f'Write packet built for {client_slug}/{doc_type}/{section}',
        details=[
            f'Profile: {profile.get("name", "?")}',
            f'Doc type: {doc_type}',
            f'Section: {section}',
            f'Evidence pieces: {len(evidence)}',
        ],
        date=today,
    )

    return {
        'profile_name': profile.get('name', ''),
        'doc_type': doc_type,
        'doc_description': doc_def.get('description', ''),
        'section': section,
        'section_notes': section_notes[section],
        'all_section_notes': section_notes,  # so the agent sees the full doc structure
        'writing_style': writing_style,
        'submission_checklist': submission_checklist,
        'client_slug': client_slug,
        'evidence': evidence,
        'write_prompt': write_prompt,
    }


def _format_write_packet(packet: dict) -> str:
    """Render a write packet as markdown for piping to an LLM agent."""
    lines: list[str] = []
    lines.append('# Write packet')
    lines.append('')
    lines.append(f'- **Profile:** {packet["profile_name"]}')
    lines.append(f'- **Document type:** `{packet["doc_type"]}`')
    if packet.get('doc_description'):
        lines.append(f'  - {packet["doc_description"]}')
    lines.append(f'- **Section:** `{packet["section"]}`')
    lines.append(f'- **Client:** `{packet["client_slug"]}`')
    lines.append('')
    lines.append('---')
    lines.append('')

    lines.append('## Write prompt')
    lines.append('')
    lines.append(packet['write_prompt'])
    lines.append('')

    lines.append(f'## Section notes for `{packet["section"]}`')
    lines.append('')
    lines.append(packet['section_notes'])
    lines.append('')

    style = packet.get('writing_style', {}) or {}
    if style.get('principles'):
        lines.append('## Principles')
        lines.append('')
        for p in style['principles']:
            lines.append(f'- {p}')
        lines.append('')
    if style.get('general_rules'):
        lines.append('## General writing rules')
        lines.append('')
        for r in style['general_rules']:
            lines.append(f'- {r}')
        lines.append('')
    if style.get('terminology_guidance'):
        lines.append('## Terminology guidance')
        lines.append('')
        lines.append('| Use | Instead of | Why |')
        lines.append('|---|---|---|')
        for entry in style['terminology_guidance']:
            instead = ', '.join(entry.get('instead_of', []))
            lines.append(f'| {entry.get("use", "")} | {instead} | {entry.get("rationale", "")} |')
        lines.append('')
    if style.get('framing_examples'):
        lines.append('## Framing examples')
        lines.append('')
        for ex in style['framing_examples']:
            lines.append(f'**Context:** {ex.get("context", "")}')
            lines.append('')
            lines.append('**Wrong:**')
            lines.append('')
            lines.append(f'> {ex.get("wrong", "")}')
            lines.append('')
            lines.append('**Correct:**')
            lines.append('')
            lines.append(f'> {ex.get("correct", "")}')
            lines.append('')
            if ex.get('why'):
                lines.append(f'*Why:* {ex["why"]}')
                lines.append('')

    if packet.get('submission_checklist'):
        lines.append('## Submission checklist (for the final document)')
        lines.append('')
        for item in packet['submission_checklist']:
            lines.append(f'- [ ] {item}')
        lines.append('')

    evidence = packet.get('evidence', []) or []
    lines.append(f'## Evidence ({len(evidence)} piece(s))')
    lines.append('')
    if not evidence:
        lines.append('_No evidence found. The agent will need to ask the user for '
                     'a source file or run additional ingest commands first._')
        lines.append('')
    else:
        for i, ev in enumerate(evidence, 1):
            lines.append(f'### Evidence #{i}: `{ev.get("path", "?")}`')
            lines.append(f'- Kind: `{ev.get("kind", "?")}`')
            if ev.get('title'):
                lines.append(f'- Title: {ev["title"]}')
            if ev.get('score') is not None and ev.get('kind') != 'explicit-source':
                lines.append(f'- Score: {ev["score"]}')
            lines.append('')
            content = ev.get('content') or ev.get('excerpt') or ''
            lines.append('```markdown')
            lines.append(content)
            lines.append('```')
            lines.append('')

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Agent loop (plan, next-task, task-done, show, list)
# ---------------------------------------------------------------------------
#
# The agent loop turns "produce a Design Validation Report" into a structured
# TODO list the agent can walk one task at a time. Mneme generates the plan
# deterministically from the active profile - the agent does the intelligent
# work (writing, finding evidence, judging quality), mneme does the mechanical
# work (knowing which sections exist, what command runs next, what depends on
# what).
#
# Plans are persisted under <workspace>/.mneme/agent-plans/<id>.json (the
# plan document) and <id>.state.json (per-task statuses). The .mneme/
# directory is workspace-internal and gitignored.

def _plan_dir() -> str:
    return os.path.join(BASE_DIR, '.mneme', 'agent-plans')


def _plan_path(plan_id: str) -> str:
    return os.path.join(_plan_dir(), f'{plan_id}.json')


def _plan_state_path(plan_id: str) -> str:
    return os.path.join(_plan_dir(), f'{plan_id}.state.json')


def _ensure_plan_dir() -> None:
    os.makedirs(_plan_dir(), exist_ok=True)


def _save_plan(plan: dict) -> None:
    _ensure_plan_dir()
    with open(_plan_path(plan['plan_id']), 'w', encoding='utf-8') as f:
        json.dump(plan, f, indent=2)


def _load_plan(plan_id: str) -> Optional[dict]:
    path = _plan_path(plan_id)
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _save_plan_state(plan_id: str, state: dict) -> None:
    _ensure_plan_dir()
    with open(_plan_state_path(plan_id), 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)


def _load_plan_state(plan_id: str) -> dict:
    path = _plan_state_path(plan_id)
    if not os.path.exists(path):
        return {'plan_id': plan_id, 'task_status': {}}
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _resolve_plan_id(plan_id: Optional[str]) -> Optional[str]:
    """If plan_id is None, return the most-recently-modified plan id, else
    return plan_id verbatim. Returns None if no plans exist."""
    if plan_id:
        return plan_id
    d = _plan_dir()
    if not os.path.isdir(d):
        return None
    candidates = []
    for fn in os.listdir(d):
        if fn.endswith('.json') and not fn.endswith('.state.json'):
            full = os.path.join(d, fn)
            candidates.append((os.path.getmtime(full), fn[:-5]))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def agent_plan(goal: str, doc_type: str, client_slug: str, plan_id: Optional[str] = None) -> dict:
    """
    Generate a deterministic TODO list for a goal like "produce a Design
    Validation Report". Persists the plan to disk and returns it.

    The plan is built from the active profile's section_notes for the given
    doc_type. Each section becomes either a draft-section task (if the
    document doesn't exist yet) or a review-page task (if it does). An
    assemble-document task depends on all the section tasks. A harmonize
    task depends on assembly. A review task depends on harmonize. A
    submission-check task depends on review.
    """
    profile = get_active_profile()
    if profile is None:
        return {'error': 'No active profile. Set one with: mneme profile set <name>'}

    sections_config = profile.get('sections', {}) or {}
    if doc_type not in sections_config:
        available = ', '.join(sorted(sections_config.keys())) or '(none)'
        return {'error': f'Unknown doc-type "{doc_type}" for profile "{profile.get("name", "?")}". '
                         f'Available: {available}'}

    section_notes = sections_config[doc_type].get('section_notes', {}) or {}
    if not section_notes:
        return {'error': f'Profile "{profile.get("name", "?")}" doc-type "{doc_type}" '
                         f'has no section_notes; nothing to plan.'}

    today = datetime.now().strftime('%Y-%m-%d')
    if plan_id is None:
        # Stable id derived from doc_type + client + date
        slug_doc = re.sub(r'[^a-z0-9-]+', '-', doc_type.lower()).strip('-')
        slug_client = re.sub(r'[^a-z0-9-]+', '-', client_slug.lower()).strip('-')
        plan_id = f'{slug_doc}-{slug_client}-{today}'

    # Stable assembled-document filename: <doc_type>.md under the client dir
    page_path = os.path.join(WIKI_DIR, client_slug, f'{doc_type}.md')
    page_exists = os.path.exists(page_path)

    tasks: list[dict] = []
    section_task_ids: list[str] = []

    for section_slug in section_notes.keys():
        task_id = f'section-{section_slug}'
        section_task_ids.append(task_id)

        if page_exists:
            kind = 'review-section'
            instructions = (
                f'The page already exists. Read the existing `{section_slug}` section '
                f'and grade it against the section notes below. If issues are found, '
                f'rewrite the section in place.'
            )
            next_command = (
                f'mneme validate writing-style {client_slug}/{doc_type}'
            )
        else:
            kind = 'draft-section'
            instructions = (
                f'Run the next_command to assemble a write packet for the '
                f'`{section_slug}` section. Then write the section as a single '
                f'markdown block starting with `## {section_slug}` and following '
                f'the section notes, principles, general rules, and terminology '
                f'guidance in the packet. Use only the evidence provided. Cite '
                f'each non-trivial claim. Use [TO ADD REF] for missing refs.'
            )
            next_command = (
                f'mneme draft --doc-type {doc_type} --section {section_slug} '
                f'--client {client_slug}'
            )

        tasks.append({
            'id': task_id,
            'kind': kind,
            'goal': f'{"Review" if page_exists else "Draft"} the `{section_slug}` section of the {doc_type}',
            'instructions': instructions,
            'preconditions': [
                f'Active profile must be "{profile.get("name", "?")}"',
            ],
            'deliverable': {
                'kind': 'markdown-section',
                'target_page': os.path.relpath(page_path, BASE_DIR).replace(os.sep, '/'),
                'section_slug': section_slug,
            },
            'next_command': next_command,
            'after_done': f'mneme agent task-done {task_id} --plan {plan_id}',
            'depends_on': [],
            'blocks': ['assemble-document'],
            'status': 'pending',
        })

    tasks.append({
        'id': 'assemble-document',
        'kind': 'assemble-document',
        'goal': f'Assemble all section drafts into wiki/{client_slug}/{doc_type}.md',
        'instructions': (
            f'Combine the drafted sections (in the order listed in the active '
            f'profile) into a single wiki page at the deliverable target_page. '
            f'Add proper frontmatter: title, type: {doc_type}, client: {client_slug}, '
            f'created/updated dates, sources from the evidence used, '
            f'confidence: medium. Then run the after_done command.'
        ),
        'preconditions': ['All section tasks must be done'],
        'deliverable': {
            'kind': 'wiki-page',
            'target_page': os.path.relpath(page_path, BASE_DIR).replace(os.sep, '/'),
        },
        'next_command': f'# manual: write {os.path.relpath(page_path, BASE_DIR).replace(os.sep, "/")}',
        'after_done': f'mneme agent task-done assemble-document --plan {plan_id}',
        'depends_on': list(section_task_ids),
        'blocks': ['harmonize'],
        'status': 'pending',
    })

    tasks.append({
        'id': 'harmonize',
        'kind': 'harmonize',
        'goal': f'Apply vocabulary harmonization to {client_slug}',
        'instructions': (
            'Run the next_command to mechanically replace rejected vocabulary '
            'with preferred terms across the entire client.'
        ),
        'preconditions': ['assemble-document must be done'],
        'deliverable': {'kind': 'harmonized-pages'},
        'next_command': f'mneme harmonize --client {client_slug} --fix',
        'after_done': f'mneme agent task-done harmonize --plan {plan_id}',
        'depends_on': ['assemble-document'],
        'blocks': ['review-page'],
        'status': 'pending',
    })

    tasks.append({
        'id': 'review-page',
        'kind': 'review-page',
        'goal': f'Run the writing-style review on the assembled document',
        'instructions': (
            'Run the next_command to build the review packet, then grade the '
            'document against the writing style. For each issue found, quote '
            'the offending text, explain why, and propose a concrete rewrite. '
            'Apply your own corrections. The user will read your final report.'
        ),
        'preconditions': ['harmonize must be done'],
        'deliverable': {'kind': 'review-report'},
        'next_command': f'mneme validate writing-style {client_slug}/{doc_type}',
        'after_done': f'mneme agent task-done review-page --plan {plan_id}',
        'depends_on': ['harmonize'],
        'blocks': ['submission-check'],
        'status': 'pending',
    })

    tasks.append({
        'id': 'submission-check',
        'kind': 'submission-check',
        'goal': 'Walk the submission checklist from the active profile and report pass/fail per item',
        'instructions': (
            'Open the active profile (run `mneme profile show` for a summary, '
            'or read the .md file directly). For each item in the submission '
            'checklist, walk the assembled document and report pass/fail with '
            'a one-line justification. Stop. Do NOT mark the document final '
            'until the user has reviewed.'
        ),
        'preconditions': ['review-page must be done'],
        'deliverable': {'kind': 'submission-checklist-report'},
        'next_command': f'mneme profile show',
        'after_done': f'mneme agent task-done submission-check --plan {plan_id}',
        'depends_on': ['review-page'],
        'blocks': [],
        'status': 'pending',
    })

    plan = {
        'plan_id': plan_id,
        'goal': goal,
        'doc_type': doc_type,
        'client_slug': client_slug,
        'profile': profile.get('name', ''),
        'created': today,
        'tasks': tasks,
    }

    _save_plan(plan)
    # Initialize empty state file
    state = {'plan_id': plan_id, 'task_status': {t['id']: 'pending' for t in tasks}}
    _save_plan_state(plan_id, state)

    _append_log(
        operation='AGENT-PLAN',
        description=f'Generated plan {plan_id} ({len(tasks)} tasks)',
        details=[
            f'Goal: {goal}',
            f'Doc type: {doc_type}',
            f'Client: {client_slug}',
        ],
        date=today,
    )

    return plan


def agent_show_plan(plan_id: Optional[str] = None) -> dict:
    """Return the plan + state. Picks the most recent plan if id is omitted."""
    resolved = _resolve_plan_id(plan_id)
    if resolved is None:
        return {'error': 'No plans found in this workspace.'}
    plan = _load_plan(resolved)
    if plan is None:
        return {'error': f'Plan not found: {resolved}'}
    state = _load_plan_state(resolved)
    return {'plan': plan, 'state': state}


def agent_next_task(plan_id: Optional[str] = None) -> dict:
    """
    Return the next ready task: the first task whose status is `pending` and
    whose dependencies are all `done`. Returns {done: True} if every task is
    done. Returns {error: ...} if no plan exists.
    """
    resolved = _resolve_plan_id(plan_id)
    if resolved is None:
        return {'error': 'No plans found in this workspace.'}
    plan = _load_plan(resolved)
    if plan is None:
        return {'error': f'Plan not found: {resolved}'}
    state = _load_plan_state(resolved)
    statuses = state.get('task_status', {})

    all_done = True
    for task in plan['tasks']:
        if statuses.get(task['id'], 'pending') != 'done':
            all_done = False
            break
    if all_done:
        return {'plan_id': resolved, 'done': True}

    for task in plan['tasks']:
        tid = task['id']
        if statuses.get(tid, 'pending') != 'pending':
            continue
        deps = task.get('depends_on', []) or []
        if all(statuses.get(d, 'pending') == 'done' for d in deps):
            return {'plan_id': resolved, 'task': task}

    return {'plan_id': resolved, 'blocked': True,
            'message': 'No ready tasks (all remaining tasks are blocked on dependencies).'}


def agent_task_done(task_id: str, plan_id: Optional[str] = None) -> dict:
    """Mark a task as done. Returns the updated state."""
    resolved = _resolve_plan_id(plan_id)
    if resolved is None:
        return {'error': 'No plans found in this workspace.'}
    plan = _load_plan(resolved)
    if plan is None:
        return {'error': f'Plan not found: {resolved}'}
    if not any(t['id'] == task_id for t in plan['tasks']):
        valid = ', '.join(t['id'] for t in plan['tasks'])
        return {'error': f'Task "{task_id}" not in plan {resolved}. Valid tasks: {valid}'}
    state = _load_plan_state(resolved)
    state.setdefault('task_status', {})[task_id] = 'done'
    _save_plan_state(resolved, state)
    today = datetime.now().strftime('%Y-%m-%d')
    _append_log(
        operation='AGENT-TASK-DONE',
        description=f'Plan {resolved}: task {task_id} marked done',
        details=[],
        date=today,
    )
    return {'plan_id': resolved, 'task_id': task_id, 'state': state}


def agent_list_plans() -> list[dict]:
    """List all plans in this workspace, newest first."""
    d = _plan_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for fn in os.listdir(d):
        if not fn.endswith('.json') or fn.endswith('.state.json'):
            continue
        plan_id = fn[:-5]
        plan = _load_plan(plan_id)
        if plan is None:
            continue
        state = _load_plan_state(plan_id)
        statuses = state.get('task_status', {})
        total = len(plan['tasks'])
        done = sum(1 for s in statuses.values() if s == 'done')
        out.append({
            'plan_id': plan_id,
            'goal': plan.get('goal', ''),
            'doc_type': plan.get('doc_type', ''),
            'client_slug': plan.get('client_slug', ''),
            'profile': plan.get('profile', ''),
            'created': plan.get('created', ''),
            'progress': f'{done}/{total}',
            'mtime': os.path.getmtime(os.path.join(d, fn)),
        })
    out.sort(key=lambda p: p['mtime'], reverse=True)
    for entry in out:
        entry.pop('mtime', None)
    return out


def validate_consistency(client_slug: str) -> dict:
    """
    Cross-document consistency check for a client.

    Reads all pages for the client and checks for:
    - Inconsistent standard references (e.g. ISO 14971:2019 vs ISO 14971:2007)
    - Conflicting version references for the same standard
    Returns {conflicts, standard_inconsistencies, total_issues}.
    """
    pattern = os.path.join(WIKI_DIR, client_slug, '**', '*.md')
    pages = glob.glob(pattern, recursive=True)

    # Collect all standard references across pages
    # Pattern: ISO NNNNN:YYYY, IEC NNNNN:YYYY, EN NNNNN:YYYY, etc.
    std_pattern = re.compile(
        r'\b((?:ISO|IEC|EN|ANSI|IEEE|BS|DIN|ASTM|FDA)\s*[\d\-]+)\s*:\s*(\d{4})\b',
        re.IGNORECASE,
    )

    # standard_name -> {version -> [pages]}
    std_refs: dict[str, dict[str, list]] = {}
    conflicts = []

    for page_path in pages:
        rel = os.path.relpath(page_path, WIKI_DIR)
        slug = os.path.splitext(rel)[0]

        try:
            with open(page_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception:
            continue

        matches = std_pattern.findall(content)
        for std_name, version in matches:
            normalized = re.sub(r'\s+', ' ', std_name.strip().upper())
            std_refs.setdefault(normalized, {})
            std_refs[normalized].setdefault(version, [])
            if slug not in std_refs[normalized][version]:
                std_refs[normalized][version].append(slug)

    # Find standards with multiple versions cited
    standard_inconsistencies = []
    for std_name, versions in std_refs.items():
        if len(versions) > 1:
            version_details = []
            for ver, page_list in sorted(versions.items()):
                version_details.append({
                    'version': ver,
                    'pages': page_list,
                })
            standard_inconsistencies.append({
                'standard': std_name,
                'versions_found': list(versions.keys()),
                'details': version_details,
            })

    total_issues = len(conflicts) + len(standard_inconsistencies)
    return {
        'conflicts': conflicts,
        'standard_inconsistencies': standard_inconsistencies,
        'total_issues': total_issues,
    }


# ---------------------------------------------------------------------------
# QMS: Repository scanning
# ---------------------------------------------------------------------------

def scan_repo(repo_path: str, client_slug: str) -> dict:
    """
    Scan a code repository and compare against wiki documentation.

    Scans for dependency files (requirements.txt, package.json, Cargo.toml,
    CMakeLists.txt, go.mod), parses dependencies, lists top-level directories
    as architecture modules, and compares findings against wiki page content.
    Returns {dependencies_found, dependencies_documented, dependencies_missing,
             modules_found, modules_documented, modules_missing, suggestions}.
    """
    if not os.path.isdir(repo_path):
        return {'error': f'Repository path not found: {repo_path}'}

    # --- Parse dependencies ---
    deps_found = []

    # requirements.txt
    req_txt = os.path.join(repo_path, 'requirements.txt')
    if os.path.exists(req_txt):
        with open(req_txt, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                match = re.match(r'^([a-zA-Z0-9_\-\.]+)\s*([>=<~!]+\s*[\d\.\*]+)?', line)
                if match:
                    name = match.group(1)
                    version = (match.group(2) or '').strip()
                    deps_found.append({'name': name, 'version': version, 'source': 'requirements.txt'})

    # package.json
    pkg_json = os.path.join(repo_path, 'package.json')
    if os.path.exists(pkg_json):
        try:
            with open(pkg_json, 'r', encoding='utf-8') as f:
                pkg = json.load(f)
            for section in ('dependencies', 'devDependencies'):
                for name, version in pkg.get(section, {}).items():
                    deps_found.append({'name': name, 'version': version, 'source': 'package.json'})
        except (json.JSONDecodeError, OSError):
            pass

    # Cargo.toml
    cargo_toml = os.path.join(repo_path, 'Cargo.toml')
    if os.path.exists(cargo_toml):
        try:
            with open(cargo_toml, 'r', encoding='utf-8') as f:
                cargo_content = f.read()
            in_deps = False
            for line in cargo_content.splitlines():
                if re.match(r'^\[dependencies\]', line):
                    in_deps = True
                    continue
                if re.match(r'^\[', line):
                    in_deps = False
                    continue
                if in_deps:
                    match = re.match(r'^(\w[\w\-]*)\s*=\s*"([^"]*)"', line)
                    if match:
                        deps_found.append({'name': match.group(1), 'version': match.group(2), 'source': 'Cargo.toml'})
        except OSError:
            pass

    # go.mod
    go_mod = os.path.join(repo_path, 'go.mod')
    if os.path.exists(go_mod):
        try:
            with open(go_mod, 'r', encoding='utf-8') as f:
                go_content = f.read()
            for match in re.finditer(r'^\s+(\S+)\s+(v[\d\.]+)', go_content, re.MULTILINE):
                module_name = match.group(1).split('/')[-1]
                deps_found.append({'name': module_name, 'version': match.group(2), 'source': 'go.mod'})
        except OSError:
            pass

    # CMakeLists.txt
    cmake = os.path.join(repo_path, 'CMakeLists.txt')
    if os.path.exists(cmake):
        try:
            with open(cmake, 'r', encoding='utf-8') as f:
                cmake_content = f.read()
            for match in re.finditer(r'find_package\s*\(\s*(\w+)', cmake_content):
                deps_found.append({'name': match.group(1), 'version': '', 'source': 'CMakeLists.txt'})
        except OSError:
            pass

    # --- Scan top-level directories as modules ---
    modules_found = []
    try:
        for entry in os.listdir(repo_path):
            entry_path = os.path.join(repo_path, entry)
            if os.path.isdir(entry_path) and not entry.startswith('.'):
                modules_found.append(entry)
    except OSError:
        pass

    # --- Compare against wiki pages ---
    wiki_pattern = os.path.join(WIKI_DIR, client_slug, '**', '*.md')
    wiki_pages = glob.glob(wiki_pattern, recursive=True)

    # Build a combined text corpus of all client wiki content
    wiki_corpus = ''
    for page_path in wiki_pages:
        try:
            with open(page_path, 'r', encoding='utf-8') as f:
                wiki_corpus += f.read().lower() + '\n'
        except Exception:
            pass

    dep_names = [d['name'] for d in deps_found]
    deps_documented = [name for name in dep_names if name.lower() in wiki_corpus]
    deps_missing = [name for name in dep_names if name.lower() not in wiki_corpus]

    modules_documented = [m for m in modules_found if m.lower() in wiki_corpus]
    modules_missing = [m for m in modules_found if m.lower() not in wiki_corpus]

    # Generate suggestions
    suggestions = []
    for dep in deps_missing:
        suggestions.append({
            'action': 'CREATE',
            'page': f'{client_slug}/{dep.lower().replace(".", "-")}-dependency',
            'reason': f'Dependency "{dep}" found in repo but not documented in wiki',
        })
    for mod in modules_missing:
        suggestions.append({
            'action': 'CREATE',
            'page': f'{client_slug}/{mod.lower()}-module',
            'reason': f'Module "{mod}" found in repo but not documented in wiki',
        })
    # Suggest updates for documented items that might need version info
    for dep in deps_found:
        if dep['name'] in deps_documented and dep['version']:
            suggestions.append({
                'action': 'UPDATE',
                'page': f'{client_slug} (containing page)',
                'reason': f'Verify version {dep["version"]} for "{dep["name"]}" matches wiki documentation',
            })

    return {
        'dependencies_found': deps_found,
        'dependencies_documented': deps_documented,
        'dependencies_missing': deps_missing,
        'modules_found': modules_found,
        'modules_documented': modules_documented,
        'modules_missing': modules_missing,
        'suggestions': suggestions,
    }


def repair() -> dict:
    """
    Repair corrupted mneme archives and schema files.

    Checks:
    - search.db: exists and is usable; if missing/corrupt, deletes and rebuilds via rebuild_index
    - entities.json, graph.json, tags.json: valid JSON; if corrupt, resets to empty structure
    - index.md: exists

    Returns a dict summarising what was repaired.
    """
    repaired = []
    warnings = []

    # --- search.db ---
    db_ok = False
    if os.path.exists(SEARCH_DB):
        try:
            conn = _get_search_db()
            _search.get_stats(conn, db_path=SEARCH_DB)
            db_ok = True
        except Exception as e:
            warnings.append(f'search.db unreadable: {e}')
            try:
                os.remove(SEARCH_DB)
            except OSError:
                pass
            global _search_conn
            _search_conn = None
    if not db_ok:
        print('[mneme] repair: search.db missing or corrupt - rebuilding index...', file=sys.stderr)
        conn = _get_search_db()
        rebuild_result = _search.rebuild_index(conn, WIKI_DIR, BASE_DIR, EXCLUDED_DIRS, EXCLUDED_FILES)
        pages_reindexed = rebuild_result if isinstance(rebuild_result, int) else rebuild_result.get('total_pages', 0)
        repaired.append(f'search.db rebuilt ({pages_reindexed} pages reindexed)')

    # --- Schema files ---
    today = datetime.now().strftime('%Y-%m-%d')
    empty_structures = {
        'entities.json': {'version': 1, 'updated': today, 'entities': []},
        'graph.json': {'version': 1, 'updated': today, 'nodes': [], 'edges': []},
        'tags.json': {'version': 1, 'updated': today, 'tags': {}},
    }
    os.makedirs(SCHEMA_DIR, exist_ok=True)
    for fname, empty in empty_structures.items():
        fpath = os.path.join(SCHEMA_DIR, fname)
        if os.path.exists(fpath):
            try:
                with open(fpath, 'r') as f:
                    json.loads(f.read())
            except (json.JSONDecodeError, OSError) as e:
                print(f'[mneme] repair: {fname} corrupt ({e}) - resetting to empty structure.', file=sys.stderr)
                with open(fpath, 'w') as f:
                    json.dump(empty, f, indent=2)
                repaired.append(f'{fname} reset to empty structure')

    # --- index.md ---
    if not os.path.exists(INDEX_FILE):
        with open(INDEX_FILE, 'w') as f:
            f.write(f'# mneme Index\nLast updated: {today}\n\n')
        repaired.append('index.md created (was missing)')

    return {
        'repaired': repaired,
        'warnings': warnings,
        'ok': len(repaired) == 0 and len(warnings) == 0,
    }


def new_workspace(
    target: str,
    project_name: str = None,
    default_client: str = 'default',
    profile: str = None,
    description: str = '',
    force: bool = False,
) -> dict:
    """
    Scaffold a new mneme workspace at `target` from the bundled template.

    Substitutes placeholders ({{PROJECT_NAME}}, {{DEFAULT_CLIENT}}, etc.)
    in template files, creates the default client directory, optionally
    sets the active profile, and writes a CLAUDE.md protocol stub.
    """
    import shutil
    from . import config as _cfg

    target_abs = os.path.abspath(os.path.expanduser(target))
    if os.path.exists(target_abs):
        if not force and os.listdir(target_abs):
            raise FileExistsError(
                f'Target "{target_abs}" exists and is not empty. Use --force to overwrite.'
            )
    if not project_name:
        project_name = os.path.basename(target_abs.rstrip(os.sep)) or 'mneme-workspace'
    project_slug = re.sub(r'[^a-z0-9\-]+', '-', project_name.lower()).strip('-') or 'workspace'
    if not re.match(r'^[a-z0-9][a-z0-9\-]*$', default_client):
        raise ValueError(f'Invalid client slug: "{default_client}"')

    today = datetime.now().strftime('%Y-%m-%d')
    template_dir = _cfg.TEMPLATE_WORKSPACE_DIR
    if not os.path.isdir(template_dir):
        raise FileNotFoundError(f'Workspace template not found at {template_dir}')

    substitutions = {
        '{{PROJECT_NAME}}': project_name,
        '{{PROJECT_SLUG}}': project_slug,
        '{{DEFAULT_CLIENT}}': default_client,
        '{{PROFILE}}': profile or 'none',
        '{{DESCRIPTION}}': description,
        '{{CREATED_DATE}}': today,
    }

    text_extensions = {'.md', '.json', '.txt', '.yml', '.yaml', '.gitignore', ''}
    files_written = 0
    os.makedirs(target_abs, exist_ok=True)
    for root, dirs, files in os.walk(template_dir):
        rel = os.path.relpath(root, template_dir)
        out_dir = target_abs if rel == '.' else os.path.join(target_abs, rel)
        os.makedirs(out_dir, exist_ok=True)
        for fname in files:
            if fname == '.gitkeep':
                continue
            src = os.path.join(root, fname)
            dst = os.path.join(out_dir, fname)
            ext = os.path.splitext(fname)[1].lower()
            if ext in text_extensions:
                with open(src, 'r', encoding='utf-8') as f:
                    content = f.read()
                for k, v in substitutions.items():
                    content = content.replace(k, v)
                with open(dst, 'w', encoding='utf-8') as f:
                    f.write(content)
            else:
                shutil.copy2(src, dst)
            files_written += 1

    # Default client directories
    os.makedirs(os.path.join(target_abs, 'wiki', default_client), exist_ok=True)
    os.makedirs(os.path.join(target_abs, 'sources', default_client), exist_ok=True)
    # Search DB is created lazily on first use; no directory needed.

    # Active profile
    if profile and profile != 'none':
        with open(os.path.join(target_abs, '.mneme-profile'), 'w', encoding='utf-8') as f:
            f.write(profile.strip() + '\n')

    # Workspace version stamp
    with open(os.path.join(target_abs, '.mneme-version'), 'w', encoding='utf-8') as f:
        from . import __version__ as _v
        f.write(_v + '\n')

    return {
        'target': target_abs,
        'project_name': project_name,
        'project_slug': project_slug,
        'default_client': default_client,
        'profile': profile or 'none',
        'files_written': files_written,
    }


def _apply_workspace_override(workspace: str) -> None:
    """
    Re-point all path constants in this module at a different workspace.

    Called after --workspace is parsed (or MNEME_HOME is set) so the rest
    of the CLI operates against the chosen workspace instead of cwd.
    """
    import importlib
    from . import config as _cfg
    os.environ['MNEME_HOME'] = os.path.abspath(os.path.expanduser(workspace))
    importlib.reload(_cfg)
    # Re-bind every path constant we imported at module load time.
    g = globals()
    for name in (
        'ACTIVE_PROFILE_FILE', 'BASE_DIR', 'INDEX_FILE', 'LOG_FILE',
        'SEARCH_DB', 'PROFILES_DIR',
        'SCHEMA_DIR', 'SOURCES_DIR', 'TEMPLATES_DIR', 'TRACEABILITY_FILE',
        'WIKI_DIR', 'WORKSPACE_PROFILES_DIR', 'WORKSPACE_MAPPINGS_DIR',
    ):
        if hasattr(_cfg, name):
            g[name] = getattr(_cfg, name)
    # INBOX_DIR is derived from BASE_DIR in this module.
    g['INBOX_DIR'] = os.path.join(_cfg.BASE_DIR, 'inbox')
    # Reset lazy search connection so it reopens against the new workspace.
    global _search_conn
    _search_conn = None


def main() -> None:
    from . import __version__ as _mnemo_version

    parser = argparse.ArgumentParser(
        prog='mneme',
        description='mneme - your second brain. LLM Wiki + FTS5 search layer.',
    )
    parser.add_argument(
        '--version', '-V',
        action='version',
        version=f'mneme {_mnemo_version}',
    )
    parser.add_argument(
        '--workspace', '-w',
        type=str,
        default=None,
        help='Path to a mneme workspace (overrides MNEME_HOME and cwd)',
    )
    subparsers = parser.add_subparsers(dest='command', required=True)

    # sync
    subparsers.add_parser('sync', help='Sync all wiki pages to search index')

    # search
    search_parser = subparsers.add_parser('search', help='Dual-layer search (wiki + FTS5)')
    search_parser.add_argument('query', help='Search query')
    search_parser.add_argument('-k', type=int, default=10, help='Max results (default: 10)')
    search_parser.add_argument('--client', type=str, default=None, help='Scope search to a specific client')

    # drift
    subparsers.add_parser('drift', help='Check sync drift between wiki and search index')

    # stats
    subparsers.add_parser('stats', help='Show stats for all layers')

    # ingest
    ingest_parser = subparsers.add_parser('ingest', help='Atomic ingest: source -> wiki + search index')
    ingest_parser.add_argument('file', help='Path to source file (.md, .txt, .pdf)')
    ingest_parser.add_argument('client_slug', help='Client slug (e.g. demo-retail, my-client)')
    ingest_parser.add_argument('--force', action='store_true', help='Re-ingest even if source was previously ingested')

    # init
    init_parser = subparsers.add_parser('init', help='Initialize a new mneme workspace')
    init_parser.add_argument('--project', type=str, default=None, help='Project name (default: current directory name)')
    init_parser.add_argument('--clients', type=str, default=None, help='Comma-separated client slugs (default: default)')

    # lint
    subparsers.add_parser('lint', help='Health check: orphan pages, dead links, stale pages, missing citations')

    # ingest-dir
    ingest_dir_parser = subparsers.add_parser('ingest-dir', help='Batch ingest all files from a directory')
    ingest_dir_parser.add_argument('directory', help='Path to directory containing source files')
    ingest_dir_parser.add_argument('client_slug', help='Client slug (e.g. demo-retail, my-client)')
    ingest_dir_parser.add_argument('--force', action='store_true', help='Re-ingest even if sources were previously ingested')
    ingest_dir_parser.add_argument('--recursive', '-r', action='store_true', help='Recurse into subdirectories')
    ingest_dir_parser.add_argument('--preserve-structure', dest='preserve_structure', action='store_true',
                                   help='Mirror source directory structure into wiki/<client>/ subdirectories')

    # tornado
    tornado_parser = subparsers.add_parser('tornado', help='Process inbox: auto-detect, ingest, archive')
    tornado_parser.add_argument('--client', type=str, default=None, help='Force all files into this client')
    tornado_parser.add_argument('--dry-run', action='store_true', help='Show what would happen without doing it')
    tornado_parser.add_argument('--profile', action='store_true', help='Apply active profile vocabulary after ingest')

    # ingest-csv
    csv_parser = subparsers.add_parser('ingest-csv', help='Ingest CSV file: one row = one wiki page')
    csv_parser.add_argument('file', help='Path to CSV file')
    csv_parser.add_argument('client_slug', help='Client slug')
    csv_parser.add_argument('--mapping', type=str, default=None, help='Mapping template name (e.g. user-needs, requirements, risk-register, dds, test-cases)')
    csv_parser.add_argument('--dry-run', action='store_true', help='Show what would happen without creating pages')
    csv_parser.add_argument('--delimiter', help='CSV delimiter character (auto-detected if omitted)')

    # status
    subparsers.add_parser('status', help='Quick summary of pending work')

    # recent
    recent_parser = subparsers.add_parser('recent', help='Show recent activity')
    recent_parser.add_argument('-n', type=int, default=10, help='Number of entries (default: 10)')

    # tags
    tags_parser = subparsers.add_parser('tags', help='Tag management')
    tags_sub = tags_parser.add_subparsers(dest='tags_command')
    tags_sub.add_parser('list', help='List all tags with counts')
    tags_merge_parser = tags_sub.add_parser('merge', help='Merge one tag into another')
    tags_merge_parser.add_argument('old_tag', help='Tag to merge from (will be removed)')
    tags_merge_parser.add_argument('new_tag', help='Tag to merge into')

    # tags suggest -- build a tag packet for an LLM agent
    tags_suggest_parser = tags_sub.add_parser(
        'suggest',
        help='Build a tag-suggestion packet for an LLM agent (the agent decides the tags)',
    )
    tags_suggest_parser.add_argument('page', help='Page slug (e.g. client-a/proposal)')
    tags_suggest_parser.add_argument('--json', action='store_true', help='Output raw JSON instead of formatted markdown')
    tags_suggest_parser.add_argument('--out', help='Write packet to a file instead of stdout')

    # tags apply -- atomic add/remove of tags on a page
    tags_apply_parser = tags_sub.add_parser(
        'apply',
        help='Apply tag changes to a page (writes frontmatter, updates schema and search index)',
    )
    tags_apply_parser.add_argument('page', help='Page slug (e.g. client-a/proposal)')
    tags_apply_parser.add_argument('--add', help='Comma-separated tags to add')
    tags_apply_parser.add_argument('--remove', help='Comma-separated tags to remove')

    # tags bulk-suggest / bulk-apply -- operate on many pages at once
    tags_bulk_suggest_parser = tags_sub.add_parser(
        'bulk-suggest',
        help='Build a tag packet covering many pages at once',
    )
    tags_bulk_suggest_parser.add_argument('--client', help='Limit to one client')
    tags_bulk_suggest_parser.add_argument('--filter', dest='filter_substr', help='Substring filter on wiki_path (e.g. req-)')
    tags_bulk_suggest_parser.add_argument('--limit', type=int, default=20, help='Max pages in the packet (default 20)')
    tags_bulk_suggest_parser.add_argument('--include-tagged', action='store_true', help='Include pages that already have non-auto tags')
    tags_bulk_suggest_parser.add_argument('--json', action='store_true', help='Output raw JSON')
    tags_bulk_suggest_parser.add_argument('--out', help='Write packet to a file instead of stdout')

    tags_bulk_apply_parser = tags_sub.add_parser(
        'bulk-apply',
        help='Apply tag changes from an agent response JSON file',
    )
    tags_bulk_apply_parser.add_argument('file', help='JSON file: {"pages": [{wiki_path, add, remove}, ...]}')

    # entity - agent-driven classification (same pattern as tags suggest/apply)
    entity_parser = subparsers.add_parser('entity', help='Entity classification (agent-driven)')
    entity_sub = entity_parser.add_subparsers(dest='entity_command')
    entity_suggest_parser = entity_sub.add_parser(
        'suggest',
        help='Build an entity-classification packet for an LLM agent',
    )
    entity_suggest_parser.add_argument('--client', help='Limit to one client')
    entity_suggest_parser.add_argument('--limit', type=int, default=50, help='Max entities in the packet (default 50)')
    entity_suggest_parser.add_argument('--all', action='store_true', help='Include already-classified entities too')
    entity_suggest_parser.add_argument('--json', action='store_true', help='Output raw JSON')
    entity_suggest_parser.add_argument('--out', help='Write packet to a file instead of stdout')

    entity_apply_parser = entity_sub.add_parser(
        'apply',
        help='Classify a single entity by id',
    )
    entity_apply_parser.add_argument('--id', required=True, help='Entity id (e.g. iso-13485)')
    entity_apply_parser.add_argument('--type', required=True, help='Entity type (standard|company|person|product|technology|concept|brand|unknown)')

    entity_bulk_parser = entity_sub.add_parser(
        'bulk-apply',
        help='Classify many entities from a JSON file',
    )
    entity_bulk_parser.add_argument('file', help='JSON file: list of {id, type} objects')

    # home -- generate a navigation landing page for Obsidian
    home_parser = subparsers.add_parser('home', help='Generate a HOME.md landing page (Dataview + fallback)')
    home_parser.add_argument('--client', help='Generate wiki/<client>/HOME.md')
    home_parser.add_argument('--all-clients', dest='workspace_wide', action='store_true',
                             help='Generate cross-client wiki/HOME.md')

    # diff
    diff_parser = subparsers.add_parser('diff', help='Show git diff for a wiki page')
    diff_parser.add_argument('page', help='Page slug (e.g. demo-retail/sample-proposal)')

    # snapshot
    snap_parser = subparsers.add_parser('snapshot', help='Create versioned archive of a client')
    snap_parser.add_argument('client_slug', help='Client slug')

    # dedupe
    subparsers.add_parser('dedupe', help='Detect near-duplicate wiki pages')

    # export
    export_parser = subparsers.add_parser('export', help='Export client knowledge base')
    export_parser.add_argument('client_slug', help='Client slug')
    export_parser.add_argument('--format', choices=['json', 'md'], default='json', help='Output format (default: json)')

    # profile
    profile_parser = subparsers.add_parser('profile', help='Manage writing style profiles')
    profile_sub = profile_parser.add_subparsers(dest='profile_command')
    profile_sub.add_parser('list', help='List available profiles')
    profile_set_parser = profile_sub.add_parser('set', help='Set active profile')
    profile_set_parser.add_argument('name', help='Profile name (e.g. eu-mdr, iso-13485)')
    profile_sub.add_parser('show', help='Show active profile details')

    # trace
    trace_parser = subparsers.add_parser('trace', help='Traceability management')
    trace_sub = trace_parser.add_subparsers(dest='trace_command')
    trace_add_parser = trace_sub.add_parser('add', help='Add a trace link')
    trace_add_parser.add_argument('from_page', help='Source page slug')
    trace_add_parser.add_argument('to_page', help='Target page slug')
    trace_add_parser.add_argument('relationship', help='Relationship type (e.g. mitigated-by, verified-by)')
    trace_show_parser = trace_sub.add_parser('show', help='Show trace chain for a page')
    trace_show_parser.add_argument('page', help='Page slug')
    trace_show_parser.add_argument('--direction', choices=['forward', 'backward'], default='forward', help='Chain direction')
    trace_matrix_parser = trace_sub.add_parser('matrix', help='Generate traceability matrix')
    trace_matrix_parser.add_argument('client_slug', help='Client slug')
    trace_matrix_parser.add_argument('--csv', action='store_true', help='Export trace matrix as CSV')
    trace_matrix_parser.add_argument('--out', help='Output file path (default: stdout)')
    trace_gaps_parser = trace_sub.add_parser('gaps', help='Find incomplete trace chains')
    trace_gaps_parser.add_argument('client_slug', help='Client slug')

    # harmonize
    harmonize_parser = subparsers.add_parser('harmonize', help='Vocabulary harmonization against active profile')
    harmonize_parser.add_argument('--client', required=True, help='Client slug')
    harmonize_parser.add_argument('--fix', action='store_true', help='Auto-fix inconsistencies')

    # validate
    validate_parser = subparsers.add_parser(
        'validate',
        help='Validate writing style (LLM agent packet) and cross-doc consistency',
    )
    validate_sub = validate_parser.add_subparsers(dest='validate_command')
    validate_style_parser = validate_sub.add_parser(
        'writing-style',
        help='Build a writing-style review packet for an LLM agent',
    )
    validate_style_parser.add_argument('page', help='Page slug (e.g. cardio-monitor/dvr-tda)')
    validate_style_parser.add_argument('--json', action='store_true',
                                       help='Emit JSON instead of human-readable markdown')
    validate_style_parser.add_argument('--out', type=str, default=None,
                                       help='Write packet to a file instead of stdout')
    validate_consist_parser = validate_sub.add_parser('consistency', help='Cross-document consistency check')
    validate_consist_parser.add_argument('--client', required=True, help='Client slug')

    # draft (write packet for an LLM agent)
    draft_parser = subparsers.add_parser(
        'draft',
        help='Build a write packet for an LLM agent (the symmetric counterpart to validate writing-style)',
    )
    draft_parser.add_argument('--doc-type', required=True, dest='doc_type',
                              help='Document type (must match a section in the active profile)')
    draft_parser.add_argument('--section', required=True,
                              help='Section slug within the doc-type')
    draft_parser.add_argument('--client', required=True, help='Client slug')
    draft_parser.add_argument('--source', type=str, default=None,
                              help='Path to a source file to include as evidence verbatim')
    draft_parser.add_argument('--query', type=str, default=None,
                              help='Wiki text search query to gather evidence (defaults to the section name)')
    draft_parser.add_argument('-k', type=int, default=10,
                              help='Max evidence pieces from the wiki search (default: 10)')
    draft_parser.add_argument('--json', action='store_true',
                              help='Emit JSON instead of human-readable markdown')
    draft_parser.add_argument('--out', type=str, default=None,
                              help='Write packet to a file instead of stdout')

    # agent (the structured agent loop)
    agent_parser = subparsers.add_parser(
        'agent',
        help='Structured agent loop: plan a goal, walk tasks, mark them done',
    )
    agent_sub = agent_parser.add_subparsers(dest='agent_command')

    agent_plan_parser = agent_sub.add_parser('plan', help='Generate a TODO plan for a goal')
    agent_plan_parser.add_argument('--goal', required=True, help='High-level goal in plain English')
    agent_plan_parser.add_argument('--doc-type', required=True, dest='doc_type',
                                   help='Document type (must match a section in the active profile)')
    agent_plan_parser.add_argument('--client', required=True, help='Client slug')
    agent_plan_parser.add_argument('--id', type=str, default=None, dest='plan_id',
                                   help='Optional explicit plan id (default: auto-derived)')
    agent_plan_parser.add_argument('--json', action='store_true', help='Emit JSON instead of markdown')

    agent_show_parser = agent_sub.add_parser('show', help='Show a plan and its task statuses')
    agent_show_parser.add_argument('--plan', type=str, default=None, dest='plan_id',
                                   help='Plan id (default: most recently modified)')
    agent_show_parser.add_argument('--json', action='store_true', help='Emit JSON instead of markdown')

    agent_next_parser = agent_sub.add_parser('next-task', help='Return the next ready task')
    agent_next_parser.add_argument('--plan', type=str, default=None, dest='plan_id',
                                   help='Plan id (default: most recently modified)')
    agent_next_parser.add_argument('--json', action='store_true', help='Emit JSON instead of markdown')

    agent_done_parser = agent_sub.add_parser('task-done', help='Mark a task as done')
    agent_done_parser.add_argument('task_id', help='Task id')
    agent_done_parser.add_argument('--plan', type=str, default=None, dest='plan_id',
                                   help='Plan id (default: most recently modified)')

    agent_list_parser = agent_sub.add_parser('list', help='List all plans in this workspace')
    agent_list_parser.add_argument('--json', action='store_true', help='Emit JSON instead of a table')

    # scan-repo
    scan_parser = subparsers.add_parser('scan-repo', help='Scan code repo and compare against QMS docs')
    scan_parser.add_argument('repo_path', help='Path to code repository')
    scan_parser.add_argument('client_slug', help='Client slug')

    # repair
    subparsers.add_parser('repair', help='Repair corrupted archives and schema')
    subparsers.add_parser('reindex', help='Rebuild the FTS5 search index from wiki pages')

    # resync
    resync_parser = subparsers.add_parser(
        'resync',
        help='Diff-aware re-ingest: 3-way merge an updated source against the existing wiki page',
    )
    resync_parser.add_argument('source', help='Path to the updated source file')
    resync_parser.add_argument('client_slug', help='Client slug')
    resync_parser.add_argument('--dry-run', action='store_true', help='Preview the merge without writing')

    # resync-resolve
    resync_resolve_parser = subparsers.add_parser(
        'resync-resolve',
        help='Mark a conflicted resync as resolved (after editing the conflict markers out)',
    )
    resync_resolve_parser.add_argument('page', help='Wiki page reference (e.g. "client/page" or full path)')

    # new
    new_parser = subparsers.add_parser('new', help='Scaffold a new mneme workspace from the bundled template')
    new_parser.add_argument('target', help='Target directory for the new workspace')
    new_parser.add_argument('--name', type=str, default=None, help='Project name (defaults to target dir name)')
    new_parser.add_argument('--client', type=str, default='default', help='Default client slug (default: "default")')
    new_parser.add_argument('--profile', type=str, default=None, help='Active profile (eu-mdr, iso-13485, ...)')
    new_parser.add_argument('--description', type=str, default='', help='One-line project description')
    new_parser.add_argument('--force', action='store_true', help='Allow scaffolding into a non-empty directory')

    # demo
    demo_parser = subparsers.add_parser('demo', help='Demo content management')
    demo_sub = demo_parser.add_subparsers(dest='demo_action')
    demo_clean = demo_sub.add_parser('clean', help='Remove all demo content (files, wiki, schema, search index, log/index entries)')
    demo_clean.add_argument('--client', default='demo-retail', help='Client slug to remove (default: demo-retail)')
    demo_clean.add_argument('--dry-run', action='store_true', help='Preview without deleting')
    demo_clean.add_argument('--yes', action='store_true', help='Skip confirmation prompt')

    args = parser.parse_args()

    if args.workspace:
        _apply_workspace_override(args.workspace)

    if args.command == 'sync':
        result = sync_all_pages()
        _print_sync_result(result)

    elif args.command == 'search':
        if not args.query.strip():
            print('Error: search query cannot be empty.', file=sys.stderr)
            sys.exit(1)
        results = dual_search(args.query, k=args.k, client=args.client)
        _print_search_results(results)

    elif args.command == 'drift':
        report = check_drift()
        _print_drift_report(report)

    elif args.command == 'stats':
        stats = get_stats()
        _print_stats(stats)

    elif args.command == 'ingest':
        if not re.match(r'^[a-z0-9][a-z0-9\-]*$', args.client_slug):
            print(f'Error: invalid client slug "{args.client_slug}". Use lowercase letters, numbers, hyphens only.', file=sys.stderr)
            sys.exit(1)
        try:
            result = ingest_source_to_both(args.file, args.client_slug, force=args.force)
            if not result:
                # Skipped due to duplicate detection
                sys.exit(0)
            print(f'Ingest complete.')
            print(f'  Action:          {result["action"]}')
            print(f'  Wiki page:       {result["wiki_page"]}')
            print(f'  Indexed:         {result["indexed"]}')
            print(f'  Entities added:  {result["entities_updated"]}')
        except (FileNotFoundError, ValueError, OSError) as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)


    elif args.command == 'init':
        client_list = [c.strip() for c in args.clients.split(',')] if args.clients else None
        init_workspace(project_name=args.project, clients=client_list)

    elif args.command == 'lint':
        result = lint()
        total = result['total_issues']
        issues = result['issues']
        print(f'=== mneme Lint ===\n')
        if total == 0:
            print('All checks passed. Knowledge base is healthy.')
        else:
            print(f'Found {total} issue(s):\n')
            if issues['dead_links']:
                print(f'  Dead links:          {len(issues["dead_links"])}')
            if issues['orphan_pages']:
                print(f'  Orphan pages:        {len(issues["orphan_pages"])}')
            if issues['stale_pages']:
                print(f'  Stale pages:         {len(issues["stale_pages"])}')
            if issues['missing_citations']:
                print(f'  Missing citations:   {len(issues["missing_citations"])}')
            if issues['schema_drift']:
                # Deduplicate by entity_id for count
                unique_drift = len({item['entity_id'] for item in issues['schema_drift']})
                print(f'  Schema drift:        {unique_drift}')
            if issues['coverage_gaps']:
                print(f'  Coverage gaps:       {len(issues["coverage_gaps"])}')
        print(f'\nReport: {result["report_path"]}')

    elif args.command == 'ingest-dir':
        if not re.match(r'^[a-z0-9][a-z0-9\-]*$', args.client_slug):
            print(f'Error: invalid client slug "{args.client_slug}". Use lowercase letters, numbers, hyphens only.', file=sys.stderr)
            sys.exit(1)
        try:
            result = ingest_dir(args.directory, args.client_slug, force=args.force,
                               recursive=getattr(args, 'recursive', False),
                               preserve_structure=getattr(args, 'preserve_structure', False))
            print(f'\nBatch ingest complete.')
            print(f'  Ingested:  {result["ingested"]}')
            print(f'  Skipped:   {result["skipped"]}')
            print(f'  Errors:    {result["errors"]}')
        except (FileNotFoundError, ValueError, OSError) as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)

    elif args.command == 'tornado':
        if args.client and not re.match(r'^[a-z0-9][a-z0-9\-]*$', args.client):
            print(f'Error: invalid client slug "{args.client}".', file=sys.stderr)
            sys.exit(1)
        tornado(client_slug=args.client, dry_run=args.dry_run, apply_profile=args.profile)

    elif args.command == 'ingest-csv':
        if not re.match(r'^[a-z0-9][a-z0-9\-]*$', args.client_slug):
            print(f'Error: invalid client slug "{args.client_slug}".', file=sys.stderr)
            sys.exit(1)
        try:
            result = ingest_csv(args.file, args.client_slug, mapping_name=args.mapping, dry_run=args.dry_run, delimiter=args.delimiter)
            if 'error' in result:
                print(f'Error: {result["error"]}', file=sys.stderr)
                sys.exit(1)
        except (FileNotFoundError, ValueError) as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)

    elif args.command == 'status':
        result = status()
        print('=== mneme Status ===\n')
        print(f'  Sources:           {result.get("total_sources", 0)}')
        print(f'  Un-ingested:       {result.get("un_ingested", 0)}')
        print(f'  Wiki pages:        {result.get("total_wiki_pages", 0)}')
        print(f'  Orphan pages:      {result.get("orphan_pages", 0)}')
        if result.get('un_ingested_files'):
            print('\n  Pending ingest:')
            for f in result['un_ingested_files'][:10]:
                print(f'    - {f}')

    elif args.command == 'recent':
        entries = recent(n=args.n)
        if not entries:
            print('No recent activity.')
        else:
            print('=== Recent Activity ===\n')
            for e in entries:
                print(f'  [{e["date"]}] {e["operation"]} | {e["description"]}')

    elif args.command == 'tags':
        if args.tags_command == 'list':
            result = tags_list()
            if not result:
                print('No tags found.')
            else:
                print('=== Tags ===\n')
                for tag, info in sorted(result.items()):
                    count = info.get('count', 0) if isinstance(info, dict) else 0
                    print(f'  {tag}: {count} pages')
        elif args.tags_command == 'merge':
            result = tags_merge(args.old_tag, args.new_tag)
            print(f'Merged "{result["old_tag"]}" into "{result["new_tag"]}"')
            print(f'  Pages updated: {result["pages_updated"]}')
        elif args.tags_command == 'suggest':
            try:
                packet = tags_suggest(args.page)
            except FileNotFoundError as e:
                print(f'Error: {e}', file=sys.stderr)
                sys.exit(1)
            if args.json:
                output = json.dumps(packet, indent=2)
            else:
                output = _format_tag_packet(packet)
            if args.out:
                with open(args.out, 'w', encoding='utf-8') as f:
                    f.write(output)
                print(f'Tag packet written to {args.out}')
            else:
                print(output)
        elif args.tags_command == 'apply':
            add_list = [t for t in (args.add or '').split(',') if t.strip()]
            remove_list = [t for t in (args.remove or '').split(',') if t.strip()]
            if not add_list and not remove_list:
                print('Error: provide --add and/or --remove with comma-separated tags', file=sys.stderr)
                sys.exit(1)
            try:
                result = tags_apply(args.page, add=add_list, remove=remove_list)
            except FileNotFoundError as e:
                print(f'Error: {e}', file=sys.stderr)
                sys.exit(1)
            print(f'Updated {result["wiki_path"]}')
            if result['added']:
                print(f'  Added:   {", ".join(result["added"])}')
            if result['removed']:
                print(f'  Removed: {", ".join(result["removed"])}')
            print(f'  Tags now: {", ".join(result["tags_after"])}')
        elif args.tags_command == 'bulk-suggest':
            packet = tags_bulk_suggest(
                client=args.client,
                filter_substr=args.filter_substr,
                limit=args.limit,
                include_tagged=args.include_tagged,
            )
            if args.json:
                output = json.dumps(packet, indent=2)
            else:
                output = _format_bulk_tag_packet(packet)
            if args.out:
                with open(args.out, 'w', encoding='utf-8') as f:
                    f.write(output)
                print(f'Bulk tag packet written to {args.out} ({len(packet["pages"])} pages)')
            else:
                print(output)
        elif args.tags_command == 'bulk-apply':
            try:
                result = tags_bulk_apply(args.file)
            except (ValueError, FileNotFoundError) as e:
                print(f'Error: {e}', file=sys.stderr)
                sys.exit(1)
            print(f'Bulk tag apply complete.')
            print(f'  Pages updated:    {result["applied"]}')
            print(f'  Tags added:       {result["total_tags_added"]}')
            print(f'  Tags removed:     {result["total_tags_removed"]}')
            if result['failed']:
                print(f'  Failures:         {len(result["failed"])}')
                for f in result['failed'][:10]:
                    print(f'    - {f}')
        else:
            print('Usage: mneme tags {list|merge|suggest|apply|bulk-suggest|bulk-apply}', file=sys.stderr)

    elif args.command == 'entity':
        if args.entity_command == 'suggest':
            packet = entity_suggest(
                client=args.client,
                limit=args.limit,
                only_unknown=not args.all,
            )
            if args.json:
                output = json.dumps(packet, indent=2)
            else:
                output = _format_entity_packet(packet)
            if args.out:
                with open(args.out, 'w', encoding='utf-8') as f:
                    f.write(output)
                print(f'Entity packet written to {args.out}')
            else:
                print(output)
        elif args.entity_command == 'apply':
            try:
                result = entity_apply(args.id, args.type)
            except (KeyError, ValueError, FileNotFoundError) as e:
                print(f'Error: {e}', file=sys.stderr)
                sys.exit(1)
            print(f'Classified entity "{result["id"]}": {result["old_type"]} -> {result["new_type"]}')
        elif args.entity_command == 'bulk-apply':
            try:
                result = entity_bulk_apply(args.file)
            except (ValueError, FileNotFoundError) as e:
                print(f'Error: {e}', file=sys.stderr)
                sys.exit(1)
            print(f'Bulk classification complete.')
            print(f'  Applied: {result["applied"]}')
            if result['errors']:
                print(f'  Errors:  {len(result["errors"])}')
                for err in result['errors'][:10]:
                    print(f'    - {err}')
        else:
            print('Usage: mneme entity {suggest|apply|bulk-apply}', file=sys.stderr)

    elif args.command == 'home':
        if not args.client and not args.workspace_wide:
            print('Error: provide --client <slug> or --all-clients', file=sys.stderr)
            sys.exit(1)
        try:
            result = generate_home(
                client_slug=args.client,
                workspace_wide=args.workspace_wide,
            )
        except (ValueError, FileNotFoundError) as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)
        print(f'HOME.md generated: {result["path"]}')
        print(f'  Pages:      {result["pages_total"]}')
        print(f'  Types:      {", ".join(result["types_detected"]) or "(none)"}')
        print(f'  Prefixes:   {", ".join(result["prefixes_detected"]) or "(none)"}')
        print(f'  Top tags:   {", ".join(result["top_tags"][:5]) or "(none)"}')

    elif args.command == 'diff':
        output = diff_page(args.page)
        if output:
            print(output)
        else:
            print('No changes detected.')

    elif args.command == 'snapshot':
        result = snapshot(args.client_slug)
        print(f'Snapshot created.')
        print(f'  Path:   {result["path"]}')
        print(f'  Pages:  {result["pages_count"]}')
        if result.get('tag'):
            print(f'  Git tag: {result["tag"]}')

    elif args.command == 'dedupe':
        result = dedupe()
        groups = result.get('duplicates', [])
        if not groups:
            print('No duplicates found.')
        else:
            print(f'Found {result["total_groups"]} duplicate group(s):\n')
            for group in groups:
                print(f'  Hash: {group["hash"][:12]}...')
                for page in group['pages']:
                    print(f'    - {page}')
                print()

    elif args.command == 'export':
        try:
            path = export_client(args.client_slug, format=args.format)
            print(f'Exported to: {path}')
        except Exception as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)

    elif args.command == 'profile':
        if args.profile_command == 'list':
            if os.path.exists(PROFILES_DIR):
                profiles = [f[:-5] for f in os.listdir(PROFILES_DIR) if f.endswith('.json')]
                active = None
                if os.path.exists(ACTIVE_PROFILE_FILE):
                    with open(ACTIVE_PROFILE_FILE, 'r') as f:
                        active = f.read().strip()
                if profiles:
                    print('Available profiles:\n')
                    for p in sorted(profiles):
                        marker = ' (active)' if p == active else ''
                        print(f'  - {p}{marker}')
                else:
                    print('No profiles found in profiles/ directory.')
            else:
                print('No profiles/ directory found.')
        elif args.profile_command == 'set':
            try:
                set_active_profile(args.name)
                print(f'Active profile set to: {args.name}')
            except (FileNotFoundError, ValueError) as e:
                print(f'Error: {e}', file=sys.stderr)
                sys.exit(1)
        elif args.profile_command == 'show':
            profile = get_active_profile()
            if not profile:
                print('No active profile. Set one with: mneme profile set <name>')
            else:
                print(f'Active profile: {profile.get("name", "unknown")}\n')
                print(f'  Description: {profile.get("description", "")}')
                vocab = profile.get('vocabulary', {}).get('preferred', [])
                print(f'  Vocabulary rules: {len(vocab)}')
                sections = profile.get('sections', {})
                print(f'  Section templates: {len(sections)}')
                print(f'  Tone: {profile.get("tone", "not set")}')
                print(f'  Voice: {profile.get("voice", "not set")}')
        else:
            print('Usage: mneme profile {list|set|show}', file=sys.stderr)

    elif args.command == 'trace':
        if args.trace_command == 'add':
            try:
                result = trace_add(args.from_page, args.to_page, args.relationship)
                print(f'Trace link added: {result["from"]} --[{result["type"]}]--> {result["to"]}')
            except (FileNotFoundError, ValueError) as e:
                print(f'Error: {e}', file=sys.stderr)
                sys.exit(1)
        elif args.trace_command == 'show':
            result = trace_show(args.page, direction=args.direction)
            chain = result.get('chain', [])
            if not chain:
                print(f'No trace links found for {args.page} ({args.direction}).')
            else:
                print(f'=== Trace Chain ({result["direction"]}) ===\n')
                print(f'  Root: {result["root"]}')
                for item in chain:
                    indent = '    ' * item['depth']
                    print(f'  {indent}{item["relationship"]} -> {item["page"]}')
        elif args.trace_command == 'matrix':
            result = trace_matrix(args.client_slug)
            if getattr(args, 'csv', False):
                import io
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(['Source'] + result['columns'])
                for row_slug in result['rows']:
                    row_data = [row_slug]
                    for col_slug in result['columns']:
                        cell = result['cells'].get((row_slug, col_slug), '')
                        row_data.append(cell)
                    writer.writerow(row_data)
                csv_text = output.getvalue()
                if getattr(args, 'out', None):
                    with open(args.out, 'w', encoding='utf-8') as f:
                        f.write(csv_text)
                    print(f'Trace matrix exported to {args.out}')
                else:
                    print(csv_text)
            else:
                rows = result.get('rows', [])
                if not rows:
                    print('No trace links found for this client.')
                else:
                    print(f'=== Traceability Matrix: {args.client_slug} ===\n')
                    print(f'  Items traced: {len(rows)}')
                    gaps = result.get('gaps', [])
                    if gaps:
                        print(f'  Gaps (no links): {len(gaps)}')
                        for g in gaps[:10]:
                            print(f'    - {g}')
        elif args.trace_command == 'gaps':
            result = trace_gaps(args.client_slug)
            total = result.get('total_gaps', 0)
            if total == 0:
                print('No trace gaps found. All chains are complete.')
            else:
                print(f'=== Trace Gaps: {args.client_slug} ===\n')
                print(f'  Total gaps: {total}\n')
                if result.get('unverified'):
                    print(f'  Requirements with no verification:')
                    for item in result['unverified']:
                        print(f'    - {item}')
                if result.get('unmitigated'):
                    print(f'  Hazards with no mitigation:')
                    for item in result['unmitigated']:
                        print(f'    - {item}')
                if result.get('unlinked_needs'):
                    print(f'  User needs with no requirements:')
                    for item in result['unlinked_needs']:
                        print(f'    - {item}')
        else:
            print('Usage: mneme trace {add|show|matrix|gaps}', file=sys.stderr)

    elif args.command == 'harmonize':
        result = harmonize(args.client, fix=args.fix)
        if 'error' in result:
            print(f'Error: {result["error"]}', file=sys.stderr)
            sys.exit(1)
        total = result.get('total_issues', 0)
        if total == 0:
            print('No vocabulary issues found. Documents are harmonized.')
        else:
            print(f'Found {total} vocabulary issue(s):\n')
            seen = set()
            for item in result.get('issues', []):
                key = (item['found_term'], item['preferred_term'])
                if key not in seen:
                    print(f'  "{item["found_term"]}" -> should be "{item["preferred_term"]}"')
                    seen.add(key)
            if args.fix:
                print(f'\n  Pages fixed: {result.get("pages_fixed", 0)}')
            else:
                print(f'\n  Run with --fix to auto-replace.')

    elif args.command == 'validate':
        if args.validate_command == 'writing-style':
            packet = validate_writing_style(args.page)
            if 'error' in packet:
                print(f'Error: {packet["error"]}', file=sys.stderr)
                sys.exit(1)
            if args.json:
                output = json.dumps(packet, indent=2, default=str)
            else:
                output = _format_writing_style_packet(packet)
            if args.out:
                with open(args.out, 'w', encoding='utf-8') as f:
                    f.write(output)
                print(f'Wrote review packet to {args.out}')
            else:
                print(output)
        elif args.validate_command == 'consistency':
            result = validate_consistency(args.client)
            total = result.get('total_issues', 0)
            if total == 0:
                print('No consistency issues found.')
            else:
                print(f'=== Consistency Check ===\n')
                print(f'  Total issues: {total}\n')
                for conflict in result.get('conflicts', []):
                    print(f'  CONFLICT: {conflict}')
                for incon in result.get('standard_inconsistencies', []):
                    print(f'  WARNING: Standard "{incon.get("standard", "")}" cited as versions: {", ".join(incon.get("versions", []))}')
        else:
            print('Usage: mneme validate {writing-style|consistency}', file=sys.stderr)

    elif args.command == 'draft':
        if not re.match(r'^[a-z0-9][a-z0-9\-]*$', args.client):
            print(f'Error: invalid client slug "{args.client}".', file=sys.stderr)
            sys.exit(1)
        packet = draft_document(
            doc_type=args.doc_type,
            section=args.section,
            client_slug=args.client,
            source_path=args.source,
            query=args.query,
            k=args.k,
        )
        if 'error' in packet:
            print(f'Error: {packet["error"]}', file=sys.stderr)
            sys.exit(1)
        if args.json:
            output = json.dumps(packet, indent=2, default=str)
        else:
            output = _format_write_packet(packet)
        if args.out:
            with open(args.out, 'w', encoding='utf-8') as f:
                f.write(output)
            print(f'Wrote write packet to {args.out}')
        else:
            print(output)

    elif args.command == 'agent':
        if args.agent_command == 'plan':
            if not re.match(r'^[a-z0-9][a-z0-9\-]*$', args.client):
                print(f'Error: invalid client slug "{args.client}".', file=sys.stderr)
                sys.exit(1)
            plan = agent_plan(args.goal, args.doc_type, args.client, plan_id=args.plan_id)
            if 'error' in plan:
                print(f'Error: {plan["error"]}', file=sys.stderr)
                sys.exit(1)
            if args.json:
                print(json.dumps(plan, indent=2, default=str))
            else:
                print(f'Plan: {plan["plan_id"]}')
                print(f'  Goal: {plan["goal"]}')
                print(f'  Doc type: {plan["doc_type"]}')
                print(f'  Client: {plan["client_slug"]}')
                print(f'  Profile: {plan["profile"]}')
                print(f'  Tasks: {len(plan["tasks"])}')
                print()
                print('Tasks:')
                for t in plan['tasks']:
                    deps = ', '.join(t['depends_on']) if t['depends_on'] else '(none)'
                    print(f'  - [{t["kind"]}] {t["id"]}')
                    print(f'      goal: {t["goal"]}')
                    print(f'      depends on: {deps}')
                print()
                print(f'Next: mneme agent next-task --plan {plan["plan_id"]}')

        elif args.agent_command == 'show':
            result = agent_show_plan(args.plan_id)
            if 'error' in result:
                print(f'Error: {result["error"]}', file=sys.stderr)
                sys.exit(1)
            if args.json:
                print(json.dumps(result, indent=2, default=str))
            else:
                plan = result['plan']
                state = result['state']
                statuses = state.get('task_status', {})
                print(f'Plan: {plan["plan_id"]}')
                print(f'  Goal: {plan.get("goal", "?")}')
                print(f'  Doc type: {plan.get("doc_type", "?")}')
                print(f'  Client: {plan.get("client_slug", "?")}')
                print()
                done = sum(1 for s in statuses.values() if s == 'done')
                print(f'Progress: {done}/{len(plan["tasks"])}')
                print()
                for t in plan['tasks']:
                    task_status = statuses.get(t['id'], 'pending')
                    marker = '[x]' if task_status == 'done' else '[ ]'
                    print(f'  {marker} {t["id"]}  ({t["kind"]})')
                    print(f'      {t["goal"]}')

        elif args.agent_command == 'next-task':
            result = agent_next_task(args.plan_id)
            if 'error' in result:
                print(f'Error: {result["error"]}', file=sys.stderr)
                sys.exit(1)
            if args.json:
                print(json.dumps(result, indent=2, default=str))
            else:
                if result.get('done'):
                    print('All tasks done.')
                elif result.get('blocked'):
                    print(result.get('message', 'Blocked.'))
                else:
                    t = result['task']
                    print(f'Next task: {t["id"]}')
                    print(f'  Kind:    {t["kind"]}')
                    print(f'  Goal:    {t["goal"]}')
                    print()
                    print('  Instructions:')
                    print(f'    {t["instructions"]}')
                    print()
                    if t.get('preconditions'):
                        print('  Preconditions:')
                        for p in t['preconditions']:
                            print(f'    - {p}')
                        print()
                    print(f'  Run:        {t["next_command"]}')
                    print(f'  After done: {t["after_done"]}')

        elif args.agent_command == 'task-done':
            result = agent_task_done(args.task_id, args.plan_id)
            if 'error' in result:
                print(f'Error: {result["error"]}', file=sys.stderr)
                sys.exit(1)
            print(f'Marked task "{result["task_id"]}" as done in plan {result["plan_id"]}.')
            print()
            print('Run `mneme agent next-task` to get the next ready task.')

        elif args.agent_command == 'list':
            plans = agent_list_plans()
            if args.json:
                print(json.dumps(plans, indent=2, default=str))
            else:
                if not plans:
                    print('No plans in this workspace.')
                else:
                    print(f'{"PLAN":<40}  {"PROGRESS":<10}  {"DOC TYPE":<25}  GOAL')
                    for p in plans:
                        print(f'{p["plan_id"]:<40}  {p["progress"]:<10}  {p["doc_type"]:<25}  {p["goal"]}')

        else:
            print('Usage: mneme agent {plan|show|next-task|task-done|list}', file=sys.stderr)
            sys.exit(1)

    elif args.command == 'scan-repo':
        try:
            result = scan_repo(args.repo_path, args.client_slug)
            deps_found = len(result.get('dependencies_found', []))
            deps_missing = len(result.get('dependencies_missing', []))
            mods_found = len(result.get('modules_found', []))
            mods_missing = len(result.get('modules_missing', []))
            suggestions = result.get('suggestions', [])
            print(f'=== Repo Scan: {args.repo_path} ===\n')
            print(f'  Dependencies found:      {deps_found}')
            print(f'  Dependencies documented:  {len(result.get("dependencies_documented", []))}')
            print(f'  Dependencies missing:     {deps_missing}')
            print(f'  Modules found:           {mods_found}')
            print(f'  Modules documented:       {len(result.get("modules_documented", []))}')
            print(f'  Modules missing:          {mods_missing}')
            if suggestions:
                print(f'\n  Suggestions:')
                for s in suggestions:
                    print(f'    {s["action"]}: {s.get("page", "")} - {s.get("reason", "")}')
        except (FileNotFoundError, ValueError) as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)

    elif args.command == 'resync':
        if not re.match(r'^[a-z0-9][a-z0-9\-]*$', args.client_slug):
            print(f'Error: invalid client slug "{args.client_slug}".', file=sys.stderr)
            sys.exit(1)
        try:
            result = resync_source(args.source, args.client_slug, dry_run=args.dry_run)
        except (FileNotFoundError, ValueError, RuntimeError) as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)
        action = result.get('action', 'unknown')
        print(f'mneme resync: {result.get("wiki_page", "?")}')
        print(f'  Action:    {action}')
        if 'reason' in result:
            print(f'  Reason:    {result["reason"]}')
        if result.get('conflicts'):
            print()
            print('  CONFLICTS detected. The wiki page now contains <<<<<<< / >>>>>>> markers.')
            print('  To resolve:')
            print('    1. Open the page and edit out the conflict markers.')
            print(f'    2. Run: mneme resync-resolve {args.client_slug}/{os.path.splitext(os.path.basename(args.source))[0]}')
        elif action.startswith('would-'):
            print(f'  (dry-run; nothing was written)')
            if 'baseline_hash' in result:
                print(f'  Baseline hash: {result["baseline_hash"][:12]}')
                print(f'  Ours hash:     {result["ours_hash"][:12]}')
                print(f'  Theirs hash:   {result["theirs_hash"][:12]}')
                print(f'  Merged hash:   {result["merged_hash"][:12]}')
        else:
            print(f'  Indexed:         {result.get("indexed", False)}')
            print(f'  Entities updated:{result.get("entities_updated", 0)}')

    elif args.command == 'resync-resolve':
        try:
            result = resync_resolve(args.page)
        except (FileNotFoundError, ValueError) as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)
        print(f'mneme resync-resolve: {result["wiki_page"]}')
        print(f'  Indexed:         {result["indexed"]}')
        print(f'  Entities updated:{result["entities_updated"]}')
        print('  Baseline updated. Page is clean.')

    elif args.command == 'new':
        try:
            result = new_workspace(
                target=args.target,
                project_name=args.name,
                default_client=args.client,
                profile=args.profile,
                description=args.description,
                force=args.force,
            )
        except (FileExistsError, FileNotFoundError, ValueError) as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)
        print(f'Created new mneme workspace:')
        print(f'  Path:           {result["target"]}')
        print(f'  Project:        {result["project_name"]}')
        print(f'  Default client: {result["default_client"]}')
        print(f'  Profile:        {result["profile"]}')
        print(f'  Files written:  {result["files_written"]}')
        print()
        print('Next steps:')
        print(f'  cd {result["target"]}')
        print(f'  git init')
        print(f'  mneme stats')

    elif args.command == 'demo':
        if args.demo_action != 'clean':
            print('Usage: mneme demo clean [--client SLUG] [--dry-run] [--yes]', file=sys.stderr)
            sys.exit(1)
        if not re.match(r'^[a-z0-9][a-z0-9\-]*$', args.client):
            print(f'Error: invalid client slug "{args.client}".', file=sys.stderr)
            sys.exit(1)
        mode = 'DRY RUN' if args.dry_run else 'DELETE'
        print(f'mneme demo clean [{mode}] -- target client: {args.client}')
        if not args.dry_run and not args.yes:
            try:
                resp = input(f'This will permanently delete all "{args.client}" content and the demo/ folder. Continue? [y/N] ')
            except EOFError:
                resp = ''
            if resp.strip().lower() not in ('y', 'yes'):
                print('Aborted.')
                sys.exit(0)
        result = clean_demo(client_slug=args.client, dry_run=args.dry_run)
        print(f'  Directories removed:    {len(result["directories"])}')
        for d in result['directories']:
            print(f'    - {d}')
        print(f'  Files removed:          {len(result["files"])}')
        for f in result['files']:
            print(f'    - {f}')
        print(f'  Schema entities:        {result["schema_entities_removed"]}')
        print(f'  Schema tag pages:       {result["schema_tag_pages_removed"]}')
        print(f'  Schema tags emptied:    {result["schema_tags_removed"]}')
        print(f'  Graph nodes:            {result["graph_nodes_removed"]}')
        print(f'  Graph edges:            {result["graph_edges_removed"]}')
        print(f'  Trace links:            {result["trace_links_removed"]}')
        print(f'  Search pages removed:   {result["search_pages_removed"]}')
        print(f'  Index lines:            {result["index_lines_removed"]}')
        print(f'  Log entries:            {result["log_entries_removed"]}')
        if args.dry_run:
            print('Dry run -- nothing was modified. Re-run without --dry-run to apply.')
        else:
            print('Done.')

    elif args.command == 'repair':
        result = repair()
        if result['ok']:
            print('repair: all checks passed - nothing needed fixing.')
        else:
            if result['repaired']:
                print('repair: fixed the following issues:')
                for item in result['repaired']:
                    print(f'  - {item}')
            if result['warnings']:
                print('repair: warnings:')
                for w in result['warnings']:
                    print(f'  - {w}')

    elif args.command == 'reindex':
        global _search_conn
        # Drop the existing connection and the DB file, then rebuild.
        if _search_conn is not None:
            try:
                _search_conn.close()
            except Exception:
                pass
            _search_conn = None
        if os.path.exists(SEARCH_DB):
            os.remove(SEARCH_DB)
        conn = _get_search_db()
        result = _search.rebuild_index(
            conn, WIKI_DIR, BASE_DIR,
            EXCLUDED_DIRS, EXCLUDED_FILES,
        )
        print(f'Reindex complete.')
        print(f'  Pages indexed: {result["pages_indexed"]}')
        if result['errors']:
            print(f'  Errors: {result["errors"]}')


def clean_demo(client_slug: str = 'demo-retail', dry_run: bool = False) -> dict:
    """
    Remove all demo content: wiki pages, sources, schema entries, search index
    entries, index.md and log.md entries, and stray top-level demo dirs.

    Returns a dict with what was (or would be) removed.
    """
    import shutil

    today = datetime.now().strftime('%Y-%m-%d')
    removed = {
        'directories': [],
        'files': [],
        'schema_entities_removed': 0,
        'schema_tags_removed': 0,
        'schema_tag_pages_removed': 0,
        'graph_nodes_removed': 0,
        'graph_edges_removed': 0,
        'trace_links_removed': 0,
        'search_pages_removed': 0,
        'index_lines_removed': 0,
        'log_entries_removed': 0,
    }

    page_prefix = f'{client_slug}/'

    # 1. Directories to wipe entirely
    candidate_dirs = [
        os.path.join(WIKI_DIR, client_slug),
        os.path.join(SOURCES_DIR, client_slug),
        os.path.join(BASE_DIR, 'demo'),          # bundled sample files
        os.path.join(BASE_DIR, client_slug),     # stray top-level dir if any
    ]
    for d in candidate_dirs:
        if os.path.isdir(d):
            removed['directories'].append(os.path.relpath(d, BASE_DIR))
            if not dry_run:
                shutil.rmtree(d)

    # 2. Delete pages from search index for this client
    wiki_client_dir = os.path.join(WIKI_DIR, client_slug)
    if os.path.isdir(wiki_client_dir):
        conn = _get_search_db()
        for root, _dirs, files in os.walk(wiki_client_dir):
            for fn in files:
                if fn.endswith('.md'):
                    wiki_path = os.path.relpath(os.path.join(root, fn), BASE_DIR)
                    if not dry_run:
                        _search.delete_page(conn, wiki_path)
                    removed['search_pages_removed'] += 1

    # 3. Lint reports referencing the client
    if os.path.isdir(WIKI_DIR):
        for fn in os.listdir(WIKI_DIR):
            if fn.startswith('lint-report-') and fn.endswith('.md'):
                fp = os.path.join(WIKI_DIR, fn)
                try:
                    with open(fp, 'r', encoding='utf-8') as f:
                        if client_slug in f.read():
                            removed['files'].append(os.path.relpath(fp, BASE_DIR))
                            if not dry_run:
                                os.remove(fp)
                except OSError:
                    pass

    # 4. schema/entities.json — drop entities for the client
    entities_file = os.path.join(SCHEMA_DIR, 'entities.json')
    if os.path.exists(entities_file):
        def ent_mod(content: str) -> str:
            try:
                data = json.loads(content) if content.strip() else {}
            except json.JSONDecodeError:
                return content
            ents = data.get('entities', [])
            kept = [e for e in ents if e.get('client') != client_slug
                    and not str(e.get('wiki_page', '')).startswith(page_prefix)]
            removed['schema_entities_removed'] = len(ents) - len(kept)
            data['entities'] = kept
            data['updated'] = today
            return json.dumps(data, indent=2)
        if dry_run:
            with open(entities_file, 'r', encoding='utf-8') as f:
                ent_mod(f.read())
        else:
            _locked_read_modify_write(entities_file, ent_mod)

    # 5. schema/tags.json — drop tag pages referencing the client
    tags_file = os.path.join(SCHEMA_DIR, 'tags.json')
    if os.path.exists(tags_file):
        def tags_mod(content: str) -> str:
            try:
                data = json.loads(content) if content.strip() else {}
            except json.JSONDecodeError:
                return content
            tags = data.get('tags', {})
            new_tags = {}
            for tag, info in tags.items():
                pages = info.get('pages', [])
                kept_pages = [p for p in pages if not p.startswith(page_prefix)]
                removed['schema_tag_pages_removed'] += len(pages) - len(kept_pages)
                if kept_pages:
                    info['pages'] = kept_pages
                    info['count'] = len(kept_pages)
                    new_tags[tag] = info
                else:
                    removed['schema_tags_removed'] += 1
            data['tags'] = new_tags
            data['updated'] = today
            return json.dumps(data, indent=2)
        if dry_run:
            with open(tags_file, 'r', encoding='utf-8') as f:
                tags_mod(f.read())
        else:
            _locked_read_modify_write(tags_file, tags_mod)

    # 6. schema/graph.json — drop nodes/edges for the client
    graph_file = os.path.join(SCHEMA_DIR, 'graph.json')
    if os.path.exists(graph_file):
        def graph_mod(content: str) -> str:
            try:
                data = json.loads(content) if content.strip() else {}
            except json.JSONDecodeError:
                return content
            nodes = data.get('nodes', [])
            kept_nodes = [n for n in nodes if n.get('client') != client_slug
                          and not str(n.get('id', '')).startswith(page_prefix)]
            removed['graph_nodes_removed'] = len(nodes) - len(kept_nodes)
            data['nodes'] = kept_nodes
            edges = data.get('edges', [])
            kept_edges = [e for e in edges
                          if not str(e.get('from', '')).startswith(page_prefix)
                          and not str(e.get('to', '')).startswith(page_prefix)]
            removed['graph_edges_removed'] = len(edges) - len(kept_edges)
            data['edges'] = kept_edges
            data['updated'] = today
            return json.dumps(data, indent=2)
        if dry_run:
            with open(graph_file, 'r', encoding='utf-8') as f:
                graph_mod(f.read())
        else:
            _locked_read_modify_write(graph_file, graph_mod)

    # 7. schema/traceability.json — drop trace links touching the client
    if os.path.exists(TRACEABILITY_FILE):
        def trace_mod(content: str) -> str:
            try:
                data = json.loads(content) if content.strip() else {}
            except json.JSONDecodeError:
                return content
            links = data.get('links', [])
            kept = [l for l in links
                    if not str(l.get('from', '')).startswith(page_prefix)
                    and not str(l.get('to', '')).startswith(page_prefix)]
            removed['trace_links_removed'] = len(links) - len(kept)
            data['links'] = kept
            data['updated'] = today
            return json.dumps(data, indent=2)
        if dry_run:
            with open(TRACEABILITY_FILE, 'r', encoding='utf-8') as f:
                trace_mod(f.read())
        else:
            _locked_read_modify_write(TRACEABILITY_FILE, trace_mod)

    # 8. (search index pages already removed in step 2)

    # 9. index.md — strip the client section and any line referencing the client
    if os.path.exists(INDEX_FILE):
        def index_mod(content: str) -> str:
            lines = content.split('\n')
            out = []
            skip_section = False
            removed_count = 0
            section_headers = {f'### {client_slug}', f'## {client_slug}'}
            for line in lines:
                stripped = line.strip()
                if stripped in section_headers:
                    skip_section = True
                    removed_count += 1
                    continue
                if skip_section:
                    if stripped.startswith('## ') or stripped.startswith('### ') or stripped.startswith('---'):
                        skip_section = False
                    else:
                        if stripped:
                            removed_count += 1
                        continue
                if f'[[{client_slug}/' in line or f'/{client_slug}/' in line:
                    removed_count += 1
                    continue
                out.append(line)
            new_content = '\n'.join(out)
            # Recount totals
            total_pages = len(re.findall(r'^\| \[\[', new_content, re.MULTILINE))
            new_content = re.sub(
                r'(\| Total pages\s*\|)\s*\d+',
                f'\\g<1> {total_pages}',
                new_content,
            )
            new_content = re.sub(
                r'Last updated: \d{4}-\d{2}-\d{2}',
                f'Last updated: {today}',
                new_content,
            )
            removed['index_lines_removed'] = removed_count
            return new_content
        if dry_run:
            with open(INDEX_FILE, 'r', encoding='utf-8') as f:
                index_mod(f.read())
        else:
            _locked_read_modify_write(INDEX_FILE, index_mod)

    # 10. log.md — strip log entries that mention the client
    if os.path.exists(LOG_FILE):
        def log_mod(content: str) -> str:
            # Entries are blocks separated by "## [date]" headers
            blocks = re.split(r'(?m)(?=^## \[)', content)
            kept = []
            count_removed = 0
            for block in blocks:
                if block.startswith('## [') and client_slug in block:
                    count_removed += 1
                    continue
                kept.append(block)
            removed['log_entries_removed'] = count_removed
            return ''.join(kept)
        if dry_run:
            with open(LOG_FILE, 'r', encoding='utf-8') as f:
                log_mod(f.read())
        else:
            _locked_read_modify_write(LOG_FILE, log_mod)

    if not dry_run:
        _append_log(
            operation='DEMO-CLEAN',
            description=f'Removed demo client "{client_slug}" and all related content',
            details=[
                f'Directories: {len(removed["directories"])}',
                f'Files: {len(removed["files"])}',
                f'Entities: {removed["schema_entities_removed"]}',
                f'Tag pages: {removed["schema_tag_pages_removed"]}',
            ],
            date=today,
        )

    return removed


if __name__ == '__main__':
    main()
