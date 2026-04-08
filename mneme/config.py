"""
Mnemosyne configuration and path resolution.

Two distinct roots:

* PACKAGE_DIR - where the installed mneme source lives. Bundled, read-only
  assets (profile JSONs, the web UI HTML, workspace templates) are loaded
  from here.

* WORKSPACE_DIR - where the user's data lives (wiki/, sources/, schema/,
  memvid/, index.md, log.md). Resolved in this order:
      1. The MNEME_HOME environment variable, if set.
      2. The current working directory.

  This means a single installed mneme CLI can serve many independent
  workspaces. Each project (e.g. parkiwatch, cardio-monitor) is just a
  directory; switch between them by `cd`-ing or by exporting MNEME_HOME.

`BASE_DIR` is preserved as an alias of WORKSPACE_DIR for backwards
compatibility with the rest of the codebase.
"""

import os

# ---------------------------------------------------------------------------
# Package root (bundled assets)
# ---------------------------------------------------------------------------

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))

# Bundled assets shipped with the package (read-only).
PROFILES_DIR = os.path.join(PACKAGE_DIR, 'profiles')
TEMPLATE_WORKSPACE_DIR = os.path.join(PACKAGE_DIR, 'templates', 'workspace')
UI_FILE = os.path.join(PACKAGE_DIR, 'ui.html')


# ---------------------------------------------------------------------------
# Workspace root (user data)
# ---------------------------------------------------------------------------

def _resolve_workspace() -> str:
    home = os.environ.get('MNEME_HOME')
    if home:
        return os.path.abspath(os.path.expanduser(home))
    return os.path.abspath(os.getcwd())


WORKSPACE_DIR = _resolve_workspace()

# Backwards-compatible alias. Older code (and most of core.py) still uses
# BASE_DIR; we treat it as the workspace root from now on.
BASE_DIR = WORKSPACE_DIR

WIKI_DIR = os.path.join(WORKSPACE_DIR, 'wiki')
SOURCES_DIR = os.path.join(WORKSPACE_DIR, 'sources')
SCHEMA_DIR = os.path.join(WORKSPACE_DIR, 'schema')
MEMVID_DIR = os.path.join(WORKSPACE_DIR, 'memvid')
MASTER_MV2 = os.path.join(MEMVID_DIR, 'master.mv2')
PER_CLIENT_DIR = os.path.join(MEMVID_DIR, 'per-client')
INDEX_FILE = os.path.join(WORKSPACE_DIR, 'index.md')
LOG_FILE = os.path.join(WORKSPACE_DIR, 'log.md')
TEMPLATES_DIR = os.path.join(WIKI_DIR, '_templates')
TRACEABILITY_FILE = os.path.join(SCHEMA_DIR, 'traceability.json')
ACTIVE_PROFILE_FILE = os.path.join(WORKSPACE_DIR, '.mneme-profile')

# Workspace-local profile overrides. Profiles dropped here shadow the bundled
# ones with the same name. Per-project frameworks (e.g. an internal QMS variant)
# go here so they don't need to be packaged with mneme.
WORKSPACE_PROFILES_DIR = os.path.join(WORKSPACE_DIR, 'profiles')
WORKSPACE_MAPPINGS_DIR = os.path.join(WORKSPACE_PROFILES_DIR, 'mappings')


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Excluded from sync
EXCLUDED_DIRS = ['_templates', '.baselines']
EXCLUDED_FILES = ['_meta.yaml']

# Chunk settings for memvid
MAX_CHUNK_SIZE = 500  # characters per Smart Frame
MIN_CHUNK_SIZE = 50   # don't create tiny frames

# Ingest limits to prevent hangs on huge files
MAX_CHUNKS_PER_INGEST = 200   # hard cap on chunks sent to memvid per page
CHUNK_COMMIT_BATCH = 50       # commit to memvid every N chunks

# Entity extraction stopwords
ENTITY_STOPWORDS = {
    'key facts', 'open questions', 'page title', 'page types', 'special files',
    'client directories', 'cross references', 'source summary', 'detail section',
    'wiki page', 'wiki pages', 'wiki protocol', 'knowledge engine', 'mnemosyne', 'mneme', 'summary section',
    'how to', 'last updated', 'activity log', 'health report', 'action plan',
    'executive summary', 'final verdict', 'risk scorecard', 'prior art',
    'patent strategy', 'filing strategy', 'negotiation strategy',
    'competitive landscape', 'technology stack', 'bill of materials',
    'system architecture', 'operating principle', 'performance analysis',
    'technical specifications', 'environmental conditions', 'safety compliance',
    'target market', 'intended use', 'design rationale', 'component photographs',
    'novelty and inventive step', 'background and technical', 'thermal and electrical',
    'prefer anglo', 'total pages', 'recent activity',
}
