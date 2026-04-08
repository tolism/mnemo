"""
core.py - Mnemosyne core engine.

Fuses the LLM Wiki (System A) with Memvid (System B) into a unified knowledge layer.
System A: structured markdown wiki at mneme/wiki/ - curated, cited, Obsidian-compatible
System B: Memvid semantic archive at mneme/memvid/ - fast retrieval via Smart Frames

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

try:
    import memvid_sdk as mv
    # Validate expected API exists
    for attr in ('use', 'create'):
        assert hasattr(mv, attr), f"memvid_sdk missing '{attr}'"
    MEMVID_AVAILABLE = True
except ImportError:
    MEMVID_AVAILABLE = False
    mv = None

from .config import (
    ACTIVE_PROFILE_FILE,
    BASE_DIR,
    CHUNK_COMMIT_BATCH,
    ENTITY_STOPWORDS,
    EXCLUDED_DIRS,
    EXCLUDED_FILES,
    INDEX_FILE,
    LOG_FILE,
    MASTER_MV2,
    MAX_CHUNK_SIZE,
    MAX_CHUNKS_PER_INGEST,
    MEMVID_DIR,
    MIN_CHUNK_SIZE,
    PER_CLIENT_DIR,
    PROFILES_DIR,
    SCHEMA_DIR,
    SOURCES_DIR,
    TEMPLATES_DIR,
    TRACEABILITY_FILE,
    WIKI_DIR,
)


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
# Chunking
# ---------------------------------------------------------------------------

def chunk_body(body: str) -> list[str]:
    """
    Split wiki body text into paragraph-level chunks for Memvid Smart Frames.
    Paragraphs are separated by blank lines. Chunks that exceed MAX_CHUNK_SIZE
    get split further at sentence boundaries. Chunks below MIN_CHUNK_SIZE are dropped.
    """
    # Split on double newlines (paragraph breaks)
    raw_paragraphs = re.split(r'\n\s*\n', body)
    chunks = []
    for para in raw_paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) <= MAX_CHUNK_SIZE:
            if len(para) >= MIN_CHUNK_SIZE:
                chunks.append(para)
        else:
            # Split long paragraph at sentence boundaries
            sentences = re.split(r'(?<=[.!?])\s+', para)
            current = ''
            for sentence in sentences:
                if len(current) + len(sentence) + 1 <= MAX_CHUNK_SIZE:
                    current = (current + ' ' + sentence).strip() if current else sentence
                else:
                    if len(current) >= MIN_CHUNK_SIZE:
                        chunks.append(current)
                    current = sentence
            if len(current) >= MIN_CHUNK_SIZE:
                chunks.append(current)
    return chunks


# ---------------------------------------------------------------------------
# Memvid open helpers
# ---------------------------------------------------------------------------

def _open_master(mode: str = 'auto'):
    """Open or create the master archive. Returns None if memvid not available."""
    if not MEMVID_AVAILABLE:
        return None
    os.makedirs(MEMVID_DIR, exist_ok=True)
    return mv.use('basic', MASTER_MV2, mode=mode)


def _open_client_archive(client_slug: str, mode: str = 'auto'):
    """Open or create a per-client archive. Returns None if memvid not available."""
    if not MEMVID_AVAILABLE:
        return None
    os.makedirs(PER_CLIENT_DIR, exist_ok=True)
    path = os.path.join(PER_CLIENT_DIR, f'{client_slug}.mv2')
    return mv.use('basic', path, mode=mode)



@contextmanager
def _memvid_locked(mv2_path: str, mode: str = 'auto'):
    """Open a memvid archive with file-level locking (shared for reads, exclusive for writes)."""
    lock_path = mv2_path + '.lock'
    os.makedirs(os.path.dirname(mv2_path), exist_ok=True)
    lock_fd = open(lock_path, 'w')
    try:
        _lock_file(lock_fd, exclusive=(mode != 'open'))
        mem = mv.use('basic', mv2_path, mode=mode)
        yield mem
    finally:
        _unlock_file(lock_fd)
        lock_fd.close()


# ---------------------------------------------------------------------------
# Sync manifest (BUG-003 fix: prevent duplicate frames on repeated sync)
# ---------------------------------------------------------------------------

# Path resolved lazily so MEMVID_DIR is available at import time
def _sync_manifest_path() -> str:
    return os.path.join(MEMVID_DIR, '.sync-manifest.json')


def _load_manifest() -> dict:
    """Load the sync manifest, returning an empty structure on any failure."""
    path = _sync_manifest_path()
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {'synced_pages': {}}


def _save_manifest(manifest: dict) -> None:
    """Persist the sync manifest atomically."""
    path = _sync_manifest_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2)


def _content_hash(content: str) -> str:
    """Return MD5 hex digest of content string."""
    return hashlib.md5(content.encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# Core engine functions
# ---------------------------------------------------------------------------

def sync_page_to_memvid(wiki_page_path: str, client_slug: Optional[str] = None) -> int:
    """
    Read a wiki markdown page, extract frontmatter, chunk the body, and add
    each chunk to master.mv2 as a Smart Frame. If client_slug is provided,
    also add frames to that client's per-client archive.

    Returns the number of frames added.
    """
    with open(wiki_page_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # BUG-003 fix: skip sync if content hasn't changed since last sync
    manifest = _load_manifest()
    page_key = wiki_page_path
    content_hash = _content_hash(content)
    if page_key in manifest['synced_pages']:
        if manifest['synced_pages'][page_key].get('hash') == content_hash:
            return 0  # already synced, no changes

    frontmatter, body = parse_frontmatter(content)

    title = frontmatter.get('title', os.path.basename(wiki_page_path))
    client = client_slug or frontmatter.get('client', '_unknown')
    tags_raw = frontmatter.get('tags', [])
    # tags can be a list or a comma-separated string depending on wiki authoring
    if isinstance(tags_raw, str):
        tags = [t.strip() for t in tags_raw.split(',') if t.strip()]
    else:
        tags = list(tags_raw)
    # Always tag with client slug for filtering
    if client and client not in tags:
        tags.append(client)

    sources = frontmatter.get('sources', [])
    confidence = frontmatter.get('confidence', 'medium')

    # Relative path from mneme root - used as the canonical reference
    try:
        rel_path = os.path.relpath(wiki_page_path, BASE_DIR)
    except ValueError:
        rel_path = wiki_page_path

    chunks = chunk_body(body)
    if not chunks:
        return 0

    # BUG-001 fix: cap chunks to prevent hang on huge files
    if len(chunks) > MAX_CHUNKS_PER_INGEST:
        print(
            f'[mneme] Warning: content has {len(chunks)} chunks, capping at {MAX_CHUNKS_PER_INGEST}',
            file=sys.stderr,
        )
        chunks = chunks[:MAX_CHUNKS_PER_INGEST]

    if not MEMVID_AVAILABLE:
        print('[mneme] Memvid not installed. Wiki-only mode. Install with: pip install memvid-sdk')
        return 0

    client_mv2_path = (
        os.path.join(PER_CLIENT_DIR, f'{client}.mv2')
        if client and client not in EXCLUDED_DIRS else None
    )

    frames_added = 0

    def _write_frames(master, client_archive=None):
        nonlocal frames_added
        for i, chunk in enumerate(chunks):
            metadata = {
                'wiki_path': rel_path,
                'client': client,
                'confidence': confidence,
                'chunk_index': str(i),
                'chunk_total': str(len(chunks)),
                'sources': json.dumps(sources) if sources else '[]',
            }
            chunk_title = f'{title} [{i + 1}/{len(chunks)}]' if len(chunks) > 1 else title
            master.put(
                text=chunk,
                title=chunk_title,
                metadata=metadata,
                tags=tags,
            )
            if client_archive:
                client_archive.put(
                    text=chunk,
                    title=chunk_title,
                    metadata=metadata,
                    tags=tags,
                )
            frames_added += 1
            # BUG-001 fix: batch commit every CHUNK_COMMIT_BATCH frames
            if (i + 1) % CHUNK_COMMIT_BATCH == 0:
                master.commit()
                if client_archive:
                    client_archive.commit()
        # Final commit for remaining frames
        master.commit()
        if client_archive:
            client_archive.commit()

    os.makedirs(MEMVID_DIR, exist_ok=True)
    if client_mv2_path:
        os.makedirs(PER_CLIENT_DIR, exist_ok=True)
        with _memvid_locked(MASTER_MV2) as master:
            with _memvid_locked(client_mv2_path) as client_archive:
                _write_frames(master, client_archive)
    else:
        with _memvid_locked(MASTER_MV2) as master:
            _write_frames(master)

    # BUG-003 fix: record sync in manifest so subsequent syncs skip unchanged pages
    manifest['synced_pages'][page_key] = {
        'hash': content_hash,
        'frame_count': frames_added,
        'synced_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
    }
    _save_manifest(manifest)

    # BUG-006 fix: update tags.json from frontmatter tags
    _update_tags_schema(wiki_page_path, frontmatter)

    return frames_added


def sync_all_pages() -> dict:
    """
    Glob all wiki pages (excluding _templates/ only), sync each to memvid.
    Returns a summary dict with total_pages, total_frames, per_client breakdown.
    """
    pattern = os.path.join(WIKI_DIR, '**', '*.md')
    all_pages = glob.glob(pattern, recursive=True)

    # Filter excluded dirs and files
    pages_to_sync = []
    for page in all_pages:
        rel = os.path.relpath(page, WIKI_DIR)
        parts = Path(rel).parts
        excluded = False
        for part in parts[:-1]:  # skip filename in check
            if part in EXCLUDED_DIRS:
                excluded = True
                break
        if os.path.basename(page) in EXCLUDED_FILES:
            excluded = True
        if not excluded:
            pages_to_sync.append(page)

    total_pages = 0
    total_frames = 0
    per_client: dict[str, int] = {}
    errors: list[str] = []

    for page in pages_to_sync:
        # Derive client slug from directory structure
        rel = os.path.relpath(page, WIKI_DIR)
        parts = Path(rel).parts
        client_slug = parts[0] if len(parts) > 1 else '_unknown'

        try:
            frames = sync_page_to_memvid(page, client_slug=client_slug)
            total_pages += 1
            total_frames += frames
            per_client[client_slug] = per_client.get(client_slug, 0) + frames
        except Exception as e:
            errors.append(f'{page}: {e}')

    result = {
        'total_pages': total_pages,
        'total_frames': total_frames,
        'per_client': per_client,
        'errors': errors,
    }
    return result


# Tantivy reserved words that cause MV999 errors in memvid semantic search
_TANTIVY_RESERVED = {'and', 'or', 'not', 'in', 'to', 'the', 'for', 'of', 'is', 'a', 'an'}


def _sanitize_memvid_query(query: str) -> str:
    """Remove Tantivy reserved words from query to prevent MV999 errors.

    BUG-002 fix: boolean keywords ('and', 'or', 'not') and common stop-words
    are reserved in Tantivy's query parser. Passing them raw silently degrades
    results or raises MV999. Strip them; if all words are reserved, keep the
    first original word as a fallback so the search is never empty.
    """
    words = query.split()
    cleaned = [w for w in words if w.lower() not in _TANTIVY_RESERVED]
    if cleaned:
        return ' '.join(cleaned)
    # Fallback: keep first word even if reserved (better than empty string)
    return words[0] if words else ''


def dual_search(query: str, k: int = 10, client: str = None) -> list[dict]:
    """
    Search both wiki (text matching) and memvid master archive (semantic).

    Wiki hits get priority - they are structured, curated, source-cited.
    Memvid fills semantic gaps where text matching misses.

    If client is specified, only return results from that client's pages.

    Returns a unified list of results with source attribution and deduplication.
    Each result: {'text', 'source', 'title', 'score', 'tags', 'wiki_path'}
    """
    results = []
    seen_paths: set[str] = set()
    seen_texts: set[str] = set()  # dedup frames with no wiki_path by content fingerprint

    # --- Layer 1: Wiki text search ---
    wiki_hits = _search_wiki_text(query, k=k)
    for hit in wiki_hits:
        path = hit.get('wiki_path', '')
        # Client filter: skip results not from the requested client
        if client and not path.replace('wiki/', '').startswith(client + '/'):
            continue
        results.append({
            'text': hit['text'],
            'title': hit['title'],
            'source': f"wiki: {hit['wiki_path']}",
            'score': hit['score'],
            'tags': hit.get('tags', []),
            'wiki_path': path,
            'layer': 'wiki',
        })
        if path:
            seen_paths.add(path)

    # --- Layer 2: Memvid semantic search ---
    if not MEMVID_AVAILABLE:
        return wiki_results + []

    try:
        safe_query = _sanitize_memvid_query(query)
        with _memvid_locked(MASTER_MV2, mode='open') as master:
            mv_result = master.find(safe_query, k=k)
        mv_hits = mv_result.get('hits', []) if isinstance(mv_result, dict) else mv_result.hits

        for hit in mv_hits:
            hit_title = hit.get('title', '') if isinstance(hit, dict) else hit.title
            hit_snippet = hit.get('snippet', '') if isinstance(hit, dict) else hit.snippet
            hit_score = hit.get('score', 0.0) if isinstance(hit, dict) else hit.score
            hit_tags = hit.get('tags', []) if isinstance(hit, dict) else hit.tags

            # Extract wiki_path from snippet metadata if present
            path_match = re.search(r'wiki_path:\s*"?([^\s"]+)"?', hit_snippet)
            wiki_path = path_match.group(1) if path_match else ''

            # Skip if the same wiki page already returned from text search or prior memvid hit
            if wiki_path and wiki_path in seen_paths:
                continue

            # Strip injected metadata from the snippet for clean display
            display_text = re.sub(r'(?:wiki_path|client|confidence|chunk_index|chunk_total|sources|extractous_metadata|labels|title|tags):\s*["\[]?[^\n]+', '', hit_snippet).strip()

            # Dedup frames without wiki_path by content fingerprint (first 100 chars)
            if not wiki_path:
                text_key = (display_text or hit_snippet)[:100]
                if text_key in seen_texts:
                    continue
                seen_texts.add(text_key)

            results.append({
                'text': display_text or hit_snippet,
                'title': hit_title,
                'source': f"memvid: {wiki_path}" if wiki_path else f"memvid: frame {hit.get('frame_id', '?') if isinstance(hit, dict) else hit.frame_id}",
                'score': hit_score,
                'tags': hit_tags,
                'wiki_path': wiki_path,
                'layer': 'memvid',
            })
            if wiki_path:
                seen_paths.add(wiki_path)

    except Exception as e:
        # Memvid may reject certain queries (reserved words, disabled index).
        # Log the failure but don't pollute results - wiki layer already ran.
        print(f'[mneme] memvid search skipped: {e}', file=sys.stderr)

    # Sort: wiki hits first by score, then memvid hits by score
    wiki_results = [r for r in results if r['layer'] == 'wiki']
    memvid_results = [r for r in results if r['layer'] == 'memvid']

    wiki_results.sort(key=lambda x: x['score'], reverse=True)
    memvid_results.sort(key=lambda x: x['score'], reverse=True)

    return wiki_results + memvid_results


def _search_wiki_text(query: str, k: int = 10) -> list[dict]:
    """
    Substring search across wiki markdown files. Also checks index.md.
    Returns up to k results ranked by match count.
    """
    pattern = os.path.join(WIKI_DIR, '**', '*.md')
    all_pages = glob.glob(pattern, recursive=True)

    query_lower = query.lower()
    query_terms = query_lower.split()
    scored: list[tuple[int, dict]] = []

    for page in all_pages:
        rel = os.path.relpath(page, WIKI_DIR)
        parts = Path(rel).parts
        # Skip excluded dirs
        excluded = any(p in EXCLUDED_DIRS for p in parts[:-1])
        if excluded:
            continue

        try:
            with open(page, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception:
            continue

        content_lower = content.lower()
        # Score = sum of occurrences of each query term
        score = sum(content_lower.count(term) for term in query_terms)
        if score == 0:
            continue

        frontmatter, body = parse_frontmatter(content)
        title = frontmatter.get('title', os.path.basename(page))
        tags = frontmatter.get('tags', [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(',')]

        # Find the first paragraph containing any query term as snippet.
        # Fall back to the full-content match so frontmatter-only pages still show
        # something useful (the match is in frontmatter, not body).
        snippet = ''
        for para in re.split(r'\n\s*\n', body):
            if any(term in para.lower() for term in query_terms):
                snippet = para.strip()[:300]
                break
        if not snippet:
            body_stripped = body.strip()
            if body_stripped:
                snippet = body_stripped[:300]
            else:
                # Match was in frontmatter; show the raw content up to 300 chars
                snippet = content.strip()[:300]

        rel_page_path = os.path.relpath(page, BASE_DIR)
        scored.append((score, {
            'text': snippet,
            'title': title,
            'wiki_path': rel_page_path,
            'score': float(score),
            'tags': tags if isinstance(tags, list) else [],
        }))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:k]]


def check_drift() -> dict:
    """
    Compare wiki pages against memvid frames to find drift.

    Checks:
    - Wiki pages with no corresponding memvid frames (need sync)
    - Memvid frames whose source wiki page no longer exists
    - Potentially stale frames (wiki page modified after last frame creation)

    Returns a structured drift report.
    """
    # Build set of wiki pages
    pattern = os.path.join(WIKI_DIR, '**', '*.md')
    wiki_pages = glob.glob(pattern, recursive=True)
    wiki_rel_paths: set[str] = set()
    for page in wiki_pages:
        rel = os.path.relpath(page, WIKI_DIR)
        parts = Path(rel).parts
        if any(p in EXCLUDED_DIRS for p in parts[:-1]):
            continue
        wiki_rel_paths.add(os.path.relpath(page, BASE_DIR))

    # Build set of wiki paths referenced in memvid frames
    memvid_wiki_paths: set[str] = set()
    orphan_frames: list[dict] = []

    if not MEMVID_AVAILABLE:
        return {
            'missing_from_memvid': [],
            'orphan_frames': [],
            'stale': [],
            'summary': 'Memvid not installed. Install with: pip install memvid-sdk',
        }

    try:
        with _memvid_locked(MASTER_MV2, mode='open') as master:
            mv_stats = master.stats()
            frame_count = mv_stats['frame_count'] if isinstance(mv_stats, dict) else mv_stats.frame_count
            probe_result = master.find('wiki_path', k=500, snippet_chars=2000)

        # Search specifically for 'wiki_path' metadata key to collect all synced paths.
        # Use large snippet_chars so the metadata block isn't truncated mid-path.
        # Memvid injects metadata at the end of the snippet, so 2000 chars covers most frames.
        sampled_paths: set[str] = set()
        probe_hits = probe_result.get('hits', []) if isinstance(probe_result, dict) else probe_result.hits
        for hit in probe_hits:
            snippet = hit.get('snippet', '') if isinstance(hit, dict) else hit.snippet
            # Match wiki paths ending in .md
            for path_match in re.finditer(r'wiki_path:\s*"?([^\s"]+\.md)"?', snippet):
                sampled_paths.add(path_match.group(1))

        memvid_wiki_paths = sampled_paths

        # Find frames with no backing wiki page
        for path in memvid_wiki_paths:
            abs_path = os.path.join(BASE_DIR, path)
            if not os.path.exists(abs_path):
                orphan_frames.append({'wiki_path': path, 'issue': 'source wiki page does not exist'})

    except Exception as e:
        return {
            'error': str(e),
            'missing_from_memvid': [],
            'orphan_frames': [],
            'stale': [],
            'summary': 'Drift check failed - memvid unavailable.',
        }

    # Pages missing from memvid
    missing_from_memvid = [p for p in wiki_rel_paths if p not in memvid_wiki_paths]

    # Stale check: wiki page modified after its frames were created
    # We can't get per-frame creation time easily, so flag pages where the file
    # was modified recently (last 24h) as candidates for re-sync
    stale: list[str] = []
    now = datetime.now(tz=timezone.utc).timestamp()
    for path in wiki_rel_paths:
        abs_path = os.path.join(BASE_DIR, path)
        if path in memvid_wiki_paths:
            try:
                mtime = os.path.getmtime(abs_path)
                # Flag if modified in the last 24 hours and already in memvid
                # (could have been updated after last sync)
                if now - mtime < 86400:
                    stale.append(path)
            except Exception:
                pass

    total_wiki = len(wiki_rel_paths)
    synced = len(wiki_rel_paths & memvid_wiki_paths)
    sync_pct = round(100 * synced / total_wiki, 1) if total_wiki else 0.0

    return {
        'missing_from_memvid': missing_from_memvid,
        'orphan_frames': orphan_frames,
        'stale': stale,
        'summary': {
            'total_wiki_pages': total_wiki,
            'synced': synced,
            'sync_pct': sync_pct,
            'missing_from_memvid': len(missing_from_memvid),
            'orphan_frames': len(orphan_frames),
            'recently_modified_may_be_stale': len(stale),
            'memvid_frame_count': frame_count,
        },
    }


def ingest_source_to_both(source_path: str, client_slug: str, force: bool = False) -> dict:
    """
    Atomic ingest operation. Takes a raw source file, writes it to the wiki,
    syncs to memvid, updates schema/entities.json and index.md, appends to log.md.

    Handles .md and .txt. PDF requires pymupdf.
    Returns a summary of what was created.
    """
    if not os.path.exists(source_path):
        raise FileNotFoundError(f'Source not found: {source_path}')

    # Check for duplicate ingest (matches INGEST_STARTED and INGEST_COMPLETE)
    source_filename = os.path.basename(source_path)
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r') as f:
            if f'INGEST | {source_filename}' in f.read():
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

    # Target wiki directory
    client_wiki_dir = os.path.join(WIKI_DIR, client_slug)
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

    rel_wiki_path = os.path.relpath(wiki_page_path, BASE_DIR)

    # Write INGEST_STARTED log immediately after wiki page write.
    # The duplicate-ingest guard reads log.md, so logging first ensures a crash
    # between wiki write and memvid sync is detectable on subsequent runs.
    _append_log(
        operation='INGEST',
        description=f'{source_filename} -> {client_slug}/{page_slug}.md ({action}) [INGEST_STARTED]',
        details=[
            f'Source: {source_path}',
            f'Wiki page: {action} at {rel_wiki_path}',
        ],
        date=today,
    )

    # Sync the wiki page to memvid
    frames_added = sync_page_to_memvid(wiki_page_path, client_slug=client_slug)

    # Update schema/entities.json with any capitalized entity mentions
    entities_updated = _update_entities_schema(client_slug, wiki_page_path, raw_content, today)

    # Update schema/tags.json from the wiki page frontmatter (BUG-006 fix)
    with open(wiki_page_path, 'r', encoding='utf-8') as _f:
        _page_fm, _ = parse_frontmatter(_f.read())
    _update_tags_schema(wiki_page_path, _page_fm)

    # Update index.md
    _update_index(client_slug, page_slug, rel_wiki_path, today)

    # Append completion log entry
    _append_log(
        operation='INGEST',
        description=f'{source_filename} -> {client_slug}/{page_slug}.md ({action}) [INGEST_COMPLETE]',
        details=[
            f'Source: {source_path}',
            f'Wiki page: {action} at {rel_wiki_path}',
            f'Memvid frames added: {frames_added}',
            f'Entities updated: {entities_updated}',
        ],
        date=today,
    )

    return {
        'wiki_page': rel_wiki_path,
        'action': action,
        'frames_added': frames_added,
        'entities_updated': entities_updated,
        'client': client_slug,
        'source': source_path,
    }


def get_stats() -> dict:
    """
    Gather stats from wiki, memvid, and schema layers.
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

    # --- Memvid stats ---
    memvid_stats: dict = {}
    search_latency_ms = None
    if not MEMVID_AVAILABLE:
        memvid_stats = {'error': 'Memvid not installed. Install with: pip install memvid-sdk'}
    else:
        try:
            master = _open_master(mode='open')
            raw = master.stats()
            memvid_stats = raw if isinstance(raw, dict) else {}

            # Latency test
            t0 = time.time()
            master.find('test', k=1)
            search_latency_ms = round((time.time() - t0) * 1000, 1)

            # Per-client archive sizes
            client_archive_sizes: dict[str, int] = {}
            if os.path.exists(PER_CLIENT_DIR):
                for fname in os.listdir(PER_CLIENT_DIR):
                    if fname.endswith('.mv2'):
                        slug = fname[:-4]
                        fpath = os.path.join(PER_CLIENT_DIR, fname)
                        client_archive_sizes[slug] = os.path.getsize(fpath)
            memvid_stats['per_client_archive_sizes'] = client_archive_sizes
            memvid_stats['search_latency_ms'] = search_latency_ms
            master.close()  # release exclusive lock before drift check opens it again
        except Exception as e:
            memvid_stats = {'error': str(e)}

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
    # Run after memvid section completes (archive handle released by garbage collection).
    # Use a fresh call without holding any open archive handles.
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
        'memvid': memvid_stats,
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
        # Nothing to put in the body - return empty so chunk_body produces 0 frames.
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
    sync_page_to_memvid() after the wiki page is written.
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


def _update_index(client_slug: str, page_slug: str, rel_wiki_path: str, today: str) -> None:
    """
    Add or update an entry in index.md for the given wiki page.
    Entry format: | [[client/page-slug]] | type | description | date | confidence |
    """
    # Ensure file exists with header before locking
    if not os.path.exists(INDEX_FILE):
        with open(INDEX_FILE, 'w') as f:
            f.write(f'# Mnemosyne Index\nLast updated: {today}\n\n')

    wikilink = f'[[{client_slug}/{page_slug}]]'
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
            f.write('# Mnemosyne Log\n\n')

    def modifier(existing: str) -> str:
        if not existing:
            existing = '# Mnemosyne Log\n\n'
        # Insert after the header (first blank line after the first heading)
        header_end = existing.find('\n\n')
        if header_end == -1:
            return existing + '\n' + entry
        return existing[:header_end + 2] + entry + existing[header_end + 2:]

    _locked_read_modify_write(LOG_FILE, modifier)


# ---------------------------------------------------------------------------
# Init workspace
# ---------------------------------------------------------------------------

def init_workspace(project_name=None, clients=None):
    """
    Create a clean Mnemosyne workspace in the current directory.

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
        'memvid',
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
                '# ' + project_name + ' - Mnemosyne Index',
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
                '# Mnemosyne Log\n\n'
                '## [' + today + '] INIT | ' + project_name + ' workspace created\n'
                '- Clients: ' + ', '.join(clients) + '\n'
                '- Structure: sources/, wiki/, schema/, memvid/\n\n'
            )

    claude_md_path = 'CLAUDE.md'
    if not os.path.exists(claude_md_path):
        client_table_rows = '\n'.join(
            '| `' + c + '` | ' + c.replace('-', ' ').title() + ' | Your domain | Active |'
            for c in clients
        )
        claude_content = '\n'.join([
            '# Mnemosyne - Wiki Protocol',
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

    print('Mnemosyne initialized.')
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
    print(f'  Frames added:  {result["total_frames"]}')
    if result['per_client']:
        print('  Per client:')
        for client, frames in sorted(result['per_client'].items()):
            print(f'    {client}: {frames} frames')
    if result['errors']:
        print(f'  Errors ({len(result["errors"])}):')
        for err in result['errors']:
            print(f'    - {err}')


def _print_search_results(results: list[dict]) -> None:
    if not results:
        print('No results found.')
        return
    for i, r in enumerate(results, 1):
        layer_tag = '[wiki]' if r['layer'] == 'wiki' else '[memvid]'
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
    s = report['summary']
    print('Drift report:')
    print(f'  Wiki pages total:      {s["total_wiki_pages"]}')
    print(f'  Synced to memvid:      {s["synced"]} ({s["sync_pct"]}%)')
    print(f'  Missing from memvid:   {s["missing_from_memvid"]}')
    print(f'  Orphan frames:         {s["orphan_frames"]}')
    print(f'  Recently modified:     {s["recently_modified_may_be_stale"]}')

    if report['missing_from_memvid']:
        print('\nPages missing from memvid:')
        for p in report['missing_from_memvid'][:10]:
            print(f'  - {p}')
        if len(report['missing_from_memvid']) > 10:
            print(f'  ... and {len(report["missing_from_memvid"]) - 10} more')

    if report['orphan_frames']:
        print('\nOrphan frames (source page gone):')
        for f in report['orphan_frames'][:10]:
            print(f'  - {f["wiki_path"]}')

    if report['stale']:
        print('\nRecently modified (may need re-sync):')
        for p in report['stale'][:10]:
            print(f'  - {p}')


def _print_stats(stats: dict) -> None:
    w = stats['wiki']
    m = stats['memvid']
    sc = stats['schema']
    d = stats['drift']

    print('=== Mnemosyne Stats ===\n')
    print('WIKI')
    print(f'  Total pages:       {w["total_pages"]}')
    print(f'  Cross-references:  {w["total_cross_references"]}')
    if w['by_client']:
        print('  By client:')
        for client, count in sorted(w['by_client'].items()):
            print(f'    {client}: {count} pages')

    print('\nMEMVID')
    if 'error' in m:
        print(f'  Error: {m["error"]}')
    else:
        print(f'  Frame count:       {m.get("frame_count", "?")}')
        size_mb = round(m.get("size_bytes", 0) / 1024 / 1024, 2)
        print(f'  Master size:       {size_mb} MB')
        if m.get('search_latency_ms') is not None:
            print(f'  Search latency:    {m["search_latency_ms"]} ms')
        if m.get('per_client_archive_sizes'):
            print('  Per-client archives:')
            for slug, size in sorted(m['per_client_archive_sizes'].items()):
                print(f'    {slug}: {round(size / 1024, 1)} KB')

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
        slug = os.path.splitext(rel)[0]
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


def ingest_dir(directory: str, client_slug: str, force: bool = False) -> dict:
    """
    Batch ingest all supported files from a directory.

    Walks the directory (non-recursive by default for safety), ingests each
    supported file (.md, .txt, .pdf) into the given client.

    Returns a summary of all ingestions.
    """
    if not os.path.isdir(directory):
        raise FileNotFoundError(f'Directory not found: {directory}')

    supported_exts = {'.md', '.txt', '.pdf'}
    files = []
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
    print()

    ingested = 0
    skipped = 0
    errors = 0
    results = []

    for i, fpath in enumerate(files, 1):
        fname = os.path.basename(fpath)
        print(f'[{i}/{len(files)}] {fname}...', end=' ')
        try:
            result = ingest_source_to_both(fpath, client_slug, force=force)
            if not result:
                print('skipped (already ingested)')
                skipped += 1
            else:
                print(f'{result["action"]} -> {result["wiki_page"]}')
                ingested += 1
                results.append(result)
        except Exception as e:
            print(f'ERROR: {e}')
            errors += 1

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

    print(f'=== Mnemosyne Tornado ===\n')
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


def _load_csv_mapping(name: str) -> dict:
    """Load a CSV mapping template from profiles/mappings/{name}.json."""
    path = os.path.join(MAPPINGS_DIR, f'{name}.json')
    if not os.path.exists(path):
        raise FileNotFoundError(f'Mapping not found: {path}')
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _detect_csv_mapping(headers: list[str]) -> str | None:
    """
    Auto-detect which mapping template matches a CSV's column headers.
    Scores each mapping by how many detect_headers keywords appear in the columns.
    """
    if not os.path.exists(MAPPINGS_DIR):
        return None

    headers_lower = [h.lower().strip() for h in headers]
    headers_joined = ' '.join(headers_lower)

    best_name = None
    best_score = 0

    for fname in os.listdir(MAPPINGS_DIR):
        if not fname.endswith('.json'):
            continue
        try:
            with open(os.path.join(MAPPINGS_DIR, fname), 'r') as f:
                mapping = json.load(f)
        except Exception:
            continue

        detect_keys = mapping.get('detect_headers', [])
        score = sum(1 for dk in detect_keys if dk.lower() in headers_joined)

        # Also check if mapping column names match actual headers
        for col_name in mapping.get('mapping', {}).keys():
            if col_name.lower() in headers_lower:
                score += 1

        if score > best_score:
            best_score = score
            best_name = fname[:-5]  # strip .json

    return best_name if best_score >= 2 else None


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
        value = row.get(csv_col, '').strip()
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


def ingest_csv(csv_path: str, client_slug: str, mapping_name: str = None, dry_run: bool = False) -> dict:
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
        reader = csv.DictReader(f)
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

        # Create trace links
        for rel_type, targets in traces.items():
            for target_id in targets:
                target_slug = re.sub(r'[^\w\-]', '-', target_id).lower().strip('-')
                target_slug = re.sub(r'-+', '-', target_slug)
                target_page = f'{client_slug}/{target_slug}'
                from_page = f'{client_slug}/{page_slug}'
                try:
                    trace_add(from_page, target_page, rel_type)
                    trace_links_created += 1
                except Exception:
                    # Target page may not exist yet - that's ok, trace is stored
                    # Try storing just in traceability.json without page validation
                    _store_trace_link(from_page, target_page, rel_type, today)
                    trace_links_created += 1

        # Update index
        _update_index(client_slug, page_slug, os.path.relpath(wiki_path, BASE_DIR), today)

        # Sync to memvid
        try:
            sync_page_to_memvid(wiki_path, client_slug=client_slug)
        except Exception:
            pass

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
    for sf in source_files:
        basename = os.path.basename(sf)
        if basename not in log_content if os.path.exists(LOG_FILE) else True:
            uningest_count += 1

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
        'un_ingested': uningest_count,
        'wiki_pages': len(wiki_pages),
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


def load_profile(name: str) -> dict:
    """
    Load a profile from profiles/{name}.json and return the parsed dict.
    """
    profile_path = os.path.join(PROFILES_DIR, f'{name}.json')
    if not os.path.exists(profile_path):
        raise FileNotFoundError(f'Profile not found: {profile_path}')

    with open(profile_path, 'r') as f:
        return json.load(f)


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
    Write the profile name to .mneme-profile to set it as active.
    """
    profile_path = os.path.join(PROFILES_DIR, f'{name}.json')
    if not os.path.exists(profile_path):
        raise FileNotFoundError(f'Profile not found: {profile_path}')

    with open(ACTIVE_PROFILE_FILE, 'w') as f:
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
        slug = os.path.splitext(rel)[0]
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


def validate_structure(page_slug: str) -> dict:
    """
    Check a wiki page against the active profile's section requirements.

    Loads the profile, determines the page type from frontmatter, checks
    required sections for that type, and scans for ## headings in the page.
    Returns {page, type, required_sections, present_sections, missing_sections, extra_sections}.
    """
    profile = get_active_profile()
    if profile is None:
        return {'error': 'No active profile. Set one with: mneme profile activate <name>'}

    page_path = os.path.join(WIKI_DIR, page_slug + '.md')
    if not os.path.exists(page_path):
        return {'error': f'Page not found: {page_slug}'}

    with open(page_path, 'r', encoding='utf-8') as f:
        content = f.read()

    fm, body = parse_frontmatter(content)
    page_type = fm.get('type', '')

    # Determine which section template applies
    sections_config = profile.get('sections', {})
    # Try to match page type or tags to a section template
    required_sections = []
    matched_template = None

    # Check if page tags or type match a section key
    tags = fm.get('tags', [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(',')]

    # Try matching by page slug suffix, tags, or type
    for section_key, section_def in sections_config.items():
        normalized_key = section_key.replace('-', ' ').lower()
        title_lower = fm.get('title', '').lower().replace('-', ' ')
        slug_lower = page_slug.lower().replace('-', ' ')
        if (normalized_key in title_lower or
                normalized_key in slug_lower or
                section_key in tags):
            required_sections = section_def.get('required', [])
            matched_template = section_key
            break

    # Extract ## headings from page body
    headings = re.findall(r'^##\s+(.+)$', body, re.MULTILINE)
    # Normalize headings to slug form for comparison
    present_normalized = [h.strip().lower().replace(' ', '-') for h in headings]
    present_sections = [h.strip() for h in headings]

    missing_sections = [s for s in required_sections if s not in present_normalized]
    extra_sections = [h for h, norm in zip(present_sections, present_normalized)
                      if norm not in required_sections and required_sections]

    today = datetime.now().strftime('%Y-%m-%d')
    if missing_sections:
        _append_log(
            operation='VALIDATE',
            description=f'Structure validation for {page_slug}',
            details=[
                f'Template: {matched_template or "none"}',
                f'Missing sections: {", ".join(missing_sections)}',
            ],
            date=today,
        )

    return {
        'page': page_slug,
        'type': page_type,
        'template': matched_template,
        'required_sections': required_sections,
        'present_sections': present_sections,
        'missing_sections': missing_sections,
        'extra_sections': extra_sections,
    }


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
    Repair corrupted Mnemosyne archives and schema files.

    Checks:
    - master.mv2: exists and is readable; if missing/corrupt, deletes and recreates via sync_all_pages
    - entities.json, graph.json, tags.json: valid JSON; if corrupt, resets to empty structure
    - index.md: exists

    Returns a dict summarising what was repaired.
    """
    repaired = []
    warnings = []

    # --- master.mv2 ---
    master_ok = False
    if os.path.exists(MASTER_MV2):
        try:
            m = mv.use('basic', MASTER_MV2, mode='open')
            m.stats()
            master_ok = True
        except Exception as e:
            warnings.append(f'master.mv2 unreadable: {e}')
            try:
                os.remove(MASTER_MV2)
            except OSError:
                pass
    if not master_ok:
        print('[mneme] repair: master.mv2 missing or corrupt - rebuilding via sync_all_pages...', file=sys.stderr)
        sync_result = sync_all_pages()
        repaired.append(f'master.mv2 rebuilt ({sync_result["total_frames"]} frames from {sync_result["total_pages"]} pages)')

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
            f.write(f'# Mnemosyne Index\nLast updated: {today}\n\n')
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
    os.makedirs(os.path.join(target_abs, 'memvid'), exist_ok=True)

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
        'MASTER_MV2', 'MEMVID_DIR', 'PER_CLIENT_DIR', 'PROFILES_DIR',
        'SCHEMA_DIR', 'SOURCES_DIR', 'TEMPLATES_DIR', 'TRACEABILITY_FILE',
        'WIKI_DIR',
    ):
        if hasattr(_cfg, name):
            g[name] = getattr(_cfg, name)
    # INBOX_DIR is derived from BASE_DIR in this module.
    g['INBOX_DIR'] = os.path.join(_cfg.BASE_DIR, 'inbox')


def main() -> None:
    from . import __version__ as _mnemo_version

    parser = argparse.ArgumentParser(
        prog='mneme',
        description='Mnemosyne - your second brain. LLM Wiki + Memvid memory layer.',
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
    subparsers.add_parser('sync', help='Sync all wiki pages to memvid')

    # search
    search_parser = subparsers.add_parser('search', help='Dual-layer search (wiki + memvid)')
    search_parser.add_argument('query', help='Search query')
    search_parser.add_argument('-k', type=int, default=10, help='Max results (default: 10)')
    search_parser.add_argument('--client', type=str, default=None, help='Scope search to a specific client')

    # drift
    subparsers.add_parser('drift', help='Check sync drift between wiki and memvid')

    # stats
    subparsers.add_parser('stats', help='Show stats for all layers')

    # ingest
    ingest_parser = subparsers.add_parser('ingest', help='Atomic ingest: source -> wiki + memvid')
    ingest_parser.add_argument('file', help='Path to source file (.md, .txt, .pdf)')
    ingest_parser.add_argument('client_slug', help='Client slug (e.g. demo-retail, my-client)')
    ingest_parser.add_argument('--force', action='store_true', help='Re-ingest even if source was previously ingested')

    # init
    init_parser = subparsers.add_parser('init', help='Initialize a new Mnemosyne workspace')
    init_parser.add_argument('--project', type=str, default=None, help='Project name (default: current directory name)')
    init_parser.add_argument('--clients', type=str, default=None, help='Comma-separated client slugs (default: default)')

    # lint
    subparsers.add_parser('lint', help='Health check: orphan pages, dead links, stale pages, missing citations')

    # ingest-dir
    ingest_dir_parser = subparsers.add_parser('ingest-dir', help='Batch ingest all files from a directory')
    ingest_dir_parser.add_argument('directory', help='Path to directory containing source files')
    ingest_dir_parser.add_argument('client_slug', help='Client slug (e.g. demo-retail, my-client)')
    ingest_dir_parser.add_argument('--force', action='store_true', help='Re-ingest even if sources were previously ingested')

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
    trace_gaps_parser = trace_sub.add_parser('gaps', help='Find incomplete trace chains')
    trace_gaps_parser.add_argument('client_slug', help='Client slug')

    # harmonize
    harmonize_parser = subparsers.add_parser('harmonize', help='Vocabulary harmonization against active profile')
    harmonize_parser.add_argument('--client', required=True, help='Client slug')
    harmonize_parser.add_argument('--fix', action='store_true', help='Auto-fix inconsistencies')

    # validate
    validate_parser = subparsers.add_parser('validate', help='Validate document structure and consistency')
    validate_sub = validate_parser.add_subparsers(dest='validate_command')
    validate_struct_parser = validate_sub.add_parser('structure', help='Check page structure against profile')
    validate_struct_parser.add_argument('page', help='Page slug')
    validate_consist_parser = validate_sub.add_parser('consistency', help='Cross-document consistency check')
    validate_consist_parser.add_argument('--client', required=True, help='Client slug')

    # scan-repo
    scan_parser = subparsers.add_parser('scan-repo', help='Scan code repo and compare against QMS docs')
    scan_parser.add_argument('repo_path', help='Path to code repository')
    scan_parser.add_argument('client_slug', help='Client slug')

    # repair
    subparsers.add_parser('repair', help='Repair corrupted archives and schema')

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
    demo_clean = demo_sub.add_parser('clean', help='Remove all demo content (files, wiki, schema, memvid, log/index entries)')
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
            print(f'  Memvid frames:   {result["frames_added"]}')
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
        print(f'=== Mnemosyne Lint ===\n')
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
            result = ingest_dir(args.directory, args.client_slug, force=args.force)
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
            result = ingest_csv(args.file, args.client_slug, mapping_name=args.mapping, dry_run=args.dry_run)
            if 'error' in result:
                print(f'Error: {result["error"]}', file=sys.stderr)
                sys.exit(1)
        except (FileNotFoundError, ValueError) as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)

    elif args.command == 'status':
        result = status()
        print('=== Mnemosyne Status ===\n')
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
        else:
            print('Usage: mneme tags {list|merge}', file=sys.stderr)

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
        if args.validate_command == 'structure':
            result = validate_structure(args.page)
            if 'error' in result:
                print(f'Error: {result["error"]}', file=sys.stderr)
                sys.exit(1)
            print(f'=== Structure Validation: {result.get("page", args.page)} ===\n')
            print(f'  Type: {result.get("type", "unknown")}')
            missing = result.get('missing_sections', [])
            present = result.get('present_sections', [])
            print(f'  Required sections present: {len(present)}')
            if missing:
                print(f'  Missing sections: {len(missing)}')
                for s in missing:
                    print(f'    - {s}')
            else:
                print('  All required sections present.')
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
            print('Usage: mneme validate {structure|consistency}', file=sys.stderr)

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
        print(f'  Memvid manifest entries:{result["manifest_entries_removed"]}')
        print(f'  Memvid archives:        {result["memvid_archives_removed"]}')
        print(f'  Index lines:            {result["index_lines_removed"]}')
        print(f'  Log entries:            {result["log_entries_removed"]}')
        if args.dry_run:
            print('Dry run -- nothing was modified. Re-run without --dry-run to apply.')
        else:
            print('Done. Note: master.mv2 frames are not removed; run `mneme repair` or rebuild memvid to drop them.')

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


def clean_demo(client_slug: str = 'demo-retail', dry_run: bool = False) -> dict:
    """
    Remove all demo content: wiki pages, sources, schema entries, memvid sync
    manifest entries, index.md and log.md entries, and stray top-level demo dirs.

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
        'manifest_entries_removed': 0,
        'index_lines_removed': 0,
        'log_entries_removed': 0,
        'memvid_archives_removed': 0,
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

    # 2. Per-client memvid archive
    per_client_mv2 = os.path.join(PER_CLIENT_DIR, f'{client_slug}.mv2')
    if os.path.exists(per_client_mv2):
        removed['files'].append(os.path.relpath(per_client_mv2, BASE_DIR))
        removed['memvid_archives_removed'] += 1
        if not dry_run:
            os.remove(per_client_mv2)

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

    # 8. memvid/.sync-manifest.json — drop entries pointing at the client
    manifest_file = os.path.join(MEMVID_DIR, '.sync-manifest.json')
    if os.path.exists(manifest_file):
        def manifest_mod(content: str) -> str:
            try:
                data = json.loads(content) if content.strip() else {}
            except json.JSONDecodeError:
                return content
            synced = data.get('synced_pages', {})
            needle = f'/wiki/{client_slug}/'
            needle_win = f'\\wiki\\{client_slug}\\'
            kept = {k: v for k, v in synced.items()
                    if needle not in k.replace('\\', '/') and needle_win not in k}
            removed['manifest_entries_removed'] = len(synced) - len(kept)
            data['synced_pages'] = kept
            return json.dumps(data, indent=2)
        if dry_run:
            with open(manifest_file, 'r', encoding='utf-8') as f:
                manifest_mod(f.read())
        else:
            _locked_read_modify_write(manifest_file, manifest_mod)

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
                f'Manifest entries: {removed["manifest_entries_removed"]}',
            ],
            date=today,
        )

    return removed


if __name__ == '__main__':
    main()
