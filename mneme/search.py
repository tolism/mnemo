"""
SQLite FTS5 search backend for mneme.

Replaces memvid-sdk with a zero-dependency full-text search index.
BM25 ranking with Porter stemming, sub-millisecond at wiki scale.

The wiki markdown files remain the source of truth. This module
maintains a search index that is rebuildable from wiki pages at
any time via ``rebuild_index()``.
"""

import glob
import hashlib
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Database lifecycle
# ---------------------------------------------------------------------------

def open_db(db_path: str) -> sqlite3.Connection:
    """
    Open or create the search database at *db_path*.

    Sets WAL mode for concurrent-read performance and returns a
    connection with row_factory = sqlite3.Row.

    Raises RuntimeError if FTS5 is not compiled into the bundled SQLite.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Verify FTS5 support
    try:
        conn.execute("SELECT fts5()")
    except sqlite3.OperationalError:
        # fts5() as a bare call always errors, but with a *different*
        # message when FTS5 is missing vs present.  A safer check:
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_check "
                "USING fts5(x)"
            )
            conn.execute("DROP TABLE IF EXISTS _fts5_check")
        except sqlite3.OperationalError as e:
            if 'fts5' in str(e).lower():
                raise RuntimeError(
                    'Your Python sqlite3 does not include FTS5.  '
                    'Upgrade Python or rebuild sqlite3 with '
                    '-DSQLITE_ENABLE_FTS5.'
                ) from e

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables, virtual table, and triggers if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pages (
            wiki_path    TEXT PRIMARY KEY,
            client       TEXT NOT NULL,
            title        TEXT NOT NULL,
            tags         TEXT,
            body         TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            indexed_at   TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
            title,
            body,
            tags,
            content='pages',
            content_rowid='rowid',
            tokenize='porter unicode61'
        );

        -- Content-sync triggers: keep FTS in sync with pages table.
        CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
            INSERT INTO pages_fts(rowid, title, body, tags)
            VALUES (new.rowid, new.title, new.body, new.tags);
        END;

        CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, title, body, tags)
            VALUES ('delete', old.rowid, old.title, old.body, old.tags);
        END;

        CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, title, body, tags)
            VALUES ('delete', old.rowid, old.title, old.body, old.tags);
            INSERT INTO pages_fts(rowid, title, body, tags)
            VALUES (new.rowid, new.title, new.body, new.tags);
        END;
    """)


# ---------------------------------------------------------------------------
# Index operations
# ---------------------------------------------------------------------------

def upsert_page(conn: sqlite3.Connection, wiki_path: str, client: str,
                title: str, tags: str, body: str, content_hash: str) -> bool:
    """
    Insert or replace a page in the index.

    Returns True if the page was actually written (hash changed or new),
    False if skipped because *content_hash* matches the existing row.
    """
    row = conn.execute(
        "SELECT content_hash FROM pages WHERE wiki_path = ?",
        (wiki_path,),
    ).fetchone()

    if row and row['content_hash'] == content_hash:
        return False  # unchanged

    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    conn.execute(
        "INSERT OR REPLACE INTO pages "
        "(wiki_path, client, title, tags, body, content_hash, indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (wiki_path, client, title, tags, body, content_hash, now),
    )
    conn.commit()
    return True


def delete_page(conn: sqlite3.Connection, wiki_path: str) -> None:
    """Remove a page from the index (triggers handle FTS cleanup)."""
    conn.execute("DELETE FROM pages WHERE wiki_path = ?", (wiki_path,))
    conn.commit()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

_FTS_SPECIAL = re.compile(r'["\*\(\)\{\}\[\]:^~]')
_BOOL_OPERATORS = re.compile(r'\b(AND|OR|NOT|NEAR)\b', re.IGNORECASE)


def _sanitize_fts_query(query: str) -> str:
    """
    Escape FTS5 special syntax for safe querying.

    - Remove boolean operators the user didn't intend
    - Strip special characters
    - Handle empty/whitespace queries
    """
    query = query.strip()
    if not query:
        return '""'

    # Remove boolean operators (users rarely intend them)
    query = _BOOL_OPERATORS.sub('', query)

    # Remove FTS5 special characters
    query = _FTS_SPECIAL.sub(' ', query)

    # Collapse whitespace
    query = ' '.join(query.split())

    if not query.strip():
        return '""'

    # Quote each token so punctuation and hyphens are safe
    tokens = query.split()
    quoted = ' '.join(f'"{t}"' for t in tokens if t)
    return quoted


def search(conn: sqlite3.Connection, query: str, k: int = 10,
           client: str = None) -> list[dict]:
    """
    FTS5 MATCH with BM25 ranking.

    Returns up to *k* results ordered by relevance (best first).
    BM25 weights: title=10.0, body=1.0, tags=5.0.

    Each result is a dict with keys:
        text, title, wiki_path, score, tags, client, layer
    """
    safe_q = _sanitize_fts_query(query)
    if safe_q == '""':
        return []

    if client:
        sql = """
            SELECT p.wiki_path, p.client, p.title, p.tags,
                   snippet(pages_fts, 1, '<b>', '</b>', '...', 32) AS snip,
                   bm25(pages_fts, 10.0, 1.0, 5.0) AS rank
            FROM pages_fts
            JOIN pages p ON p.rowid = pages_fts.rowid
            WHERE pages_fts MATCH ?
              AND p.client = ?
            ORDER BY rank
            LIMIT ?
        """
        rows = conn.execute(sql, (safe_q, client, k)).fetchall()
    else:
        sql = """
            SELECT p.wiki_path, p.client, p.title, p.tags,
                   snippet(pages_fts, 1, '<b>', '</b>', '...', 32) AS snip,
                   bm25(pages_fts, 10.0, 1.0, 5.0) AS rank
            FROM pages_fts
            JOIN pages p ON p.rowid = pages_fts.rowid
            WHERE pages_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """
        rows = conn.execute(sql, (safe_q, k)).fetchall()

    results = []
    for row in rows:
        tags_str = row['tags'] or ''
        tags_list = [t.strip() for t in tags_str.split(',') if t.strip()]
        results.append({
            'text': row['snip'] or '',
            'title': row['title'],
            'wiki_path': row['wiki_path'],
            'score': row['rank'],
            'tags': tags_list,
            'client': row['client'],
            'layer': 'fts5',
        })
    return results


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------

def get_indexed_pages(conn: sqlite3.Connection) -> dict[str, str]:
    """Return ``{wiki_path: content_hash}`` for every indexed page."""
    rows = conn.execute("SELECT wiki_path, content_hash FROM pages").fetchall()
    return {r['wiki_path']: r['content_hash'] for r in rows}


def get_stats(conn: sqlite3.Connection, db_path: str = None) -> dict:
    """
    Return index statistics.

    Keys: page_count, db_size_bytes (if *db_path* given).
    """
    row = conn.execute("SELECT count(*) AS cnt FROM pages").fetchone()
    stats = {'page_count': row['cnt']}
    if db_path and os.path.exists(db_path):
        stats['db_size_bytes'] = os.path.getsize(db_path)
    return stats


# ---------------------------------------------------------------------------
# Bulk operations
# ---------------------------------------------------------------------------

def rebuild_index(conn: sqlite3.Connection, wiki_dir: str, base_dir: str,
                  excluded_dirs: list[str],
                  excluded_files: list[str]) -> dict:
    """
    Full reindex: clear the index, walk *wiki_dir*, re-insert every page.

    Returns ``{pages_indexed: int, errors: int}``.
    """
    # Import here to avoid circular dependency
    from .core import parse_frontmatter, _content_hash

    conn.execute("DELETE FROM pages")
    conn.commit()

    pattern = os.path.join(wiki_dir, '**', '*.md')
    all_pages = glob.glob(pattern, recursive=True)

    pages_indexed = 0
    errors = 0

    for page_path in all_pages:
        rel = os.path.relpath(page_path, wiki_dir)
        parts = Path(rel).parts

        # Skip excluded dirs and files
        if any(p in excluded_dirs for p in parts[:-1]):
            continue
        if parts[-1] in excluded_files:
            continue

        try:
            with open(page_path, 'r', encoding='utf-8') as f:
                content = f.read()

            content_hash = _content_hash(content)
            frontmatter, body = parse_frontmatter(content)

            title = frontmatter.get('title', os.path.basename(page_path))
            client = parts[0] if len(parts) > 1 else '_root'

            tags_raw = frontmatter.get('tags', [])
            if isinstance(tags_raw, str):
                tags_raw = [t.strip() for t in tags_raw.split(',') if t.strip()]
            tags_str = ', '.join(tags_raw)

            wiki_path = rel.replace(os.sep, '/')

            upsert_page(conn, wiki_path, client, title, tags_str,
                        body, content_hash)
            pages_indexed += 1

        except Exception:
            errors += 1

    return {'pages_indexed': pages_indexed, 'errors': errors}
