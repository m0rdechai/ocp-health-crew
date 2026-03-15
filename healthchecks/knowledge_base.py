"""
Dynamic knowledge base for the RCA pattern engine.

Loads known issues, investigation commands, and bug data from JSON files
in the knowledge/ directory. Supports multiple sources: built-in patterns
shipped with the code, user-added patterns, auto-learned patterns,
Gemini AI suggestions, and Jira scan results.

Each pattern has a 'source' field: "built-in", "user", "learned", "gemini",
or "jira-scan".
"""

import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KNOWLEDGE_DIR = os.path.join(_PROJECT_ROOT, "knowledge")
KNOWN_ISSUES_FILE = os.path.join(KNOWLEDGE_DIR, "known_issues.json")
KNOWN_BUGS_FILE = os.path.join(KNOWLEDGE_DIR, "known_bugs.json")


def _ensure_dir():
    os.makedirs(KNOWLEDGE_DIR, exist_ok=True)


def _write_json(path, data):
    _ensure_dir()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_known_issues():
    """Load all known issue patterns from the JSON knowledge base.

    Returns a dict keyed by issue ID. Each entry has the original fields
    (pattern, jira, title, description, root_cause, suggestions, verify_cmd)
    plus: source, confidence, created, last_matched, investigation_commands.

    If the JSON file doesn't exist yet, it is seeded from the hardcoded
    dicts in hybrid_health_check.py (backward compatibility).
    """
    if not os.path.exists(KNOWN_ISSUES_FILE):
        _seed_known_issues()
    try:
        with open(KNOWN_ISSUES_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load %s: %s", KNOWN_ISSUES_FILE, exc)
        return {}


def load_known_bugs():
    """Load the Jira bug cache from JSON.

    Returns a dict keyed by Jira key (e.g. "CNV-66551").
    """
    if not os.path.exists(KNOWN_BUGS_FILE):
        _seed_known_bugs()
    try:
        with open(KNOWN_BUGS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load %s: %s", KNOWN_BUGS_FILE, exc)
        return {}


def load_investigation_commands():
    """Return a dict mapping issue-type keys to investigation command lists.

    Built from known_issues.json: each pattern that has an
    investigation_commands field is indexed by the issue key *and* by
    the inv_type value it maps to.
    """
    issues = load_known_issues()
    inv = {}
    for key, entry in issues.items():
        cmds = entry.get("investigation_commands")
        if cmds:
            inv[key] = cmds
            inv_type = entry.get("inv_type")
            if inv_type and inv_type != key:
                inv[inv_type] = cmds
    return inv


# ---------------------------------------------------------------------------
# Saving / mutating
# ---------------------------------------------------------------------------

def save_known_issue(key, entry):
    """Add or update a single known-issue pattern and persist to disk."""
    issues = load_known_issues()
    if key in issues and issues[key].get("source") == "built-in" and entry.get("source") != "built-in":
        entry["overrides_built_in"] = True
    issues[key] = entry
    _write_json(KNOWN_ISSUES_FILE, issues)
    logger.info("Saved known issue '%s' (source=%s)", key, entry.get("source"))


def save_known_bug(jira_key, bug_data):
    """Add or update a single Jira bug entry and persist to disk."""
    bugs = load_known_bugs()
    bug_data["last_updated"] = datetime.now().isoformat()
    bugs[jira_key] = bug_data
    _write_json(KNOWN_BUGS_FILE, bugs)


def delete_known_issue(key):
    """Remove a known-issue pattern. Returns True if deleted."""
    issues = load_known_issues()
    if key in issues:
        del issues[key]
        _write_json(KNOWN_ISSUES_FILE, issues)
        return True
    return False


def delete_known_bug(jira_key):
    """Remove a bug entry. Returns True if deleted."""
    bugs = load_known_bugs()
    if jira_key in bugs:
        del bugs[jira_key]
        _write_json(KNOWN_BUGS_FILE, bugs)
        return True
    return False


def update_last_matched(key):
    """Update the last_matched timestamp for a pattern."""
    issues = load_known_issues()
    if key in issues:
        issues[key]["last_matched"] = datetime.now().isoformat()
        _write_json(KNOWN_ISSUES_FILE, issues)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def get_stats():
    """Return summary stats for the knowledge base."""
    issues = load_known_issues()
    bugs = load_known_bugs()
    by_source = {}
    for entry in issues.values():
        src = entry.get("source", "unknown")
        by_source[src] = by_source.get(src, 0) + 1
    return {
        "total_patterns": len(issues),
        "total_bugs": len(bugs),
        "by_source": by_source,
    }


# ---------------------------------------------------------------------------
# Duplicate detection (for Gemini / learning suggestions)
# ---------------------------------------------------------------------------

def pattern_exists(keywords):
    """Check if a pattern with similar keywords already exists.

    Returns the key of the existing pattern if >= 60% keyword overlap,
    else None.
    """
    if not keywords:
        return None
    kw_set = set(k.lower() for k in keywords)
    issues = load_known_issues()
    for key, entry in issues.items():
        existing_kw = set(k.lower() for k in entry.get("pattern", []))
        if not existing_kw:
            continue
        overlap = len(kw_set & existing_kw) / max(len(kw_set), len(existing_kw))
        if overlap >= 0.6:
            return key
    return None


# ---------------------------------------------------------------------------
# Seeding from hardcoded dicts (first-run only)
# ---------------------------------------------------------------------------

# Mapping from KNOWN_ISSUES key -> INVESTIGATION_COMMANDS key
_INV_TYPE_MAP = {
    "virt-handler-memory": "virt-handler-memory",
    "virt-handler-error": "virt-handler-memory",
    "noobaa-endpoint": "noobaa",
    "metal3-crashloop": "metal3",
    "container-status-unknown": "pod-unknown",
    "volumesnapshot-not-ready": "volumesnapshot",
    "datavolume-stuck": "csi",
    "migration-failed": "migration",
    "stuck-migration": "migration",
    "cordoned-node-vms": "node",
    "etcd-unhealthy": "etcd",
    "oom-events": "oom",
    "csi-issues": "csi",
    "mco-degraded": "operator-degraded",
    "operator-degraded-generic": "operator-degraded",
    "operator-unavailable": "operator-unavailable",
    "node-not-ready": "node",
    "alerts-firing": "alert",
}


def _seed_known_issues():
    """Generate known_issues.json from the hardcoded dicts on first run."""
    try:
        from healthchecks.hybrid_health_check import KNOWN_ISSUES, INVESTIGATION_COMMANDS
    except ImportError:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from hybrid_health_check import KNOWN_ISSUES, INVESTIGATION_COMMANDS

    now = datetime.now().isoformat()
    merged = {}
    for key, entry in KNOWN_ISSUES.items():
        inv_type = _INV_TYPE_MAP.get(key, "")
        inv_cmds = INVESTIGATION_COMMANDS.get(inv_type, [])
        merged[key] = {
            **entry,
            "source": "built-in",
            "confidence": 1.0,
            "created": now,
            "last_matched": None,
            "inv_type": inv_type,
            "investigation_commands": inv_cmds,
        }

    _write_json(KNOWN_ISSUES_FILE, merged)
    logger.info("Seeded %s with %d built-in patterns", KNOWN_ISSUES_FILE, len(merged))


def _seed_known_bugs():
    """Generate known_bugs.json from the hardcoded KNOWN_BUGS dict."""
    try:
        from healthchecks.hybrid_health_check import get_known_bug_info  # noqa: F401
    except ImportError:
        pass

    # The KNOWN_BUGS dict is local to get_known_bug_info(). We replicate it
    # here to avoid reaching into a function-local scope.
    builtin_bugs = {
        "CNV-66551": {"status": "Closed", "resolution": "Done", "fix_versions": ["CNV 4.17.0"], "affects": ["CNV 4.16"]},
        "CNV-71448": {"status": "In Progress", "resolution": None, "fix_versions": [], "affects": ["CNV 4.17", "CNV 4.18"]},
        "CNV-30274": {"status": "Closed", "resolution": "Done", "fix_versions": ["CNV 4.15.0"], "affects": ["CNV 4.14"]},
        "CNV-68292": {"status": "Closed", "resolution": "Done", "fix_versions": ["CNV 4.17.1"], "affects": ["CNV 4.17.0"]},
        "CNV-70607": {"status": "In Progress", "resolution": None, "fix_versions": [], "affects": ["CNV 4.17"]},
        "CNV-74568": {"status": "Open", "resolution": None, "fix_versions": [], "affects": ["CNV 4.18"]},
        "CNV-71962": {"status": "Closed", "resolution": "Done", "fix_versions": ["CNV 4.17.2"], "affects": ["CNV 4.17"]},
        "CNV-74856": {"status": "Open", "resolution": None, "fix_versions": [], "affects": ["CNV 4.18"]},
        "CNV-76280": {"status": "Open", "resolution": None, "fix_versions": [], "affects": ["CNV 4.18"]},
        "CNV-74866": {"status": "In Progress", "resolution": None, "fix_versions": [], "affects": ["CNV 4.18"]},
        "CNV-69281": {"status": "Closed", "resolution": "Done", "fix_versions": ["CNV 4.17.0"], "affects": ["CNV 4.16"]},
        "CNV-45516": {"status": "Closed", "resolution": "Done", "fix_versions": ["CNV 4.16.0"], "affects": ["CNV 4.15"]},
        "CNV-52369": {"status": "Closed", "resolution": "Done", "fix_versions": ["CNV 4.16.1"], "affects": ["CNV 4.16.0"]},
        "CNV-74930": {"status": "Open", "resolution": None, "fix_versions": [], "affects": ["CNV 4.18"]},
        "CNV-20450": {"status": "Closed", "resolution": "Done", "fix_versions": ["CNV 4.14.0"], "affects": ["CNV 4.13"]},
        "CNV-75962": {"status": "In Progress", "resolution": None, "fix_versions": [], "affects": ["CNV 4.18"]},
        "CNV-63538": {"status": "Closed", "resolution": "Done", "fix_versions": ["CNV 4.16.0"], "affects": ["CNV 4.15"]},
        "CNV-70889": {"status": "Closed", "resolution": "Done", "fix_versions": ["CNV 4.17.0"], "affects": ["CNV 4.16"]},
        "OCPBUGS-48789": {"status": "Closed", "resolution": "Done", "fix_versions": ["OCP 4.17.0"], "affects": ["OCP 4.16"]},
        "OCPBUGS-74962": {"status": "Open", "resolution": None, "fix_versions": [], "affects": ["OCP 4.18"]},
        "OCPBUGS-70140": {"status": "In Progress", "resolution": None, "fix_versions": [], "affects": ["OCP 4.17"]},
        "OCPBUGS-69390": {"status": "Closed", "resolution": "Done", "fix_versions": ["OCP 4.17.1"], "affects": ["OCP 4.17.0"]},
        "OCPBUGS-47041": {"status": "Closed", "resolution": "Done", "fix_versions": ["OCP 4.16.0"], "affects": ["OCP 4.15"]},
        "OCPBUGS-38553": {"status": "Closed", "resolution": "Done", "fix_versions": ["OCP 4.15.0"], "affects": ["OCP 4.14"]},
        "OCPBUGS-41786": {"status": "Closed", "resolution": "Done", "fix_versions": ["OCP 4.16.0"], "affects": ["OCP 4.15"]},
        "OCPBUGS-42135": {"status": "Closed", "resolution": "Done", "fix_versions": ["OCP 4.16.0"], "affects": ["OCP 4.15"]},
    }

    now = datetime.now().isoformat()
    bugs = {}
    for jira_key, data in builtin_bugs.items():
        bugs[jira_key] = {**data, "source": "built-in", "last_updated": now}
    _write_json(KNOWN_BUGS_FILE, bugs)
    logger.info("Seeded %s with %d built-in bugs", KNOWN_BUGS_FILE, len(bugs))
