"""
CNV Health Dashboard - Learning & Pattern Recognition Module
This module tracks recurring issues and learns from each health check run.
"""

import os
import json
from datetime import datetime, timedelta
from collections import defaultdict

# Learning data file
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEARNING_FILE = os.path.join(BASE_DIR, ".learning_data.json")

# Default learning data structure
DEFAULT_LEARNING_DATA = {
    "version": "1.0",
    "created": None,
    "last_updated": None,
    "total_runs": 0,
    "patterns": {},          # Discovered patterns from recurring issues
    "issue_history": [],     # Recent issues for trend analysis
    "recurring_issues": {},  # Issues that appear frequently
    "learned_fixes": {},     # Fixes that worked
    "suggested_checks": []   # Checks suggested by AI and accepted
}


def load_learning_data():
    """Load learning data from file"""
    if os.path.exists(LEARNING_FILE):
        try:
            with open(LEARNING_FILE, 'r') as f:
                data = json.load(f)
                return data
        except:
            pass
    
    # Return default structure
    data = DEFAULT_LEARNING_DATA.copy()
    data["created"] = datetime.now().isoformat()
    return data


def save_learning_data(data):
    """Save learning data to file"""
    data["last_updated"] = datetime.now().isoformat()
    with open(LEARNING_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def record_health_check_run(issues, cluster_info=None):
    """
    Record results from a health check run for learning.
    
    Args:
        issues: List of detected issues from the health check
        cluster_info: Optional cluster metadata (version, node count, etc.)
    """
    data = load_learning_data()
    data["total_runs"] += 1
    
    timestamp = datetime.now().isoformat()
    
    # Record each issue
    for issue in issues:
        issue_key = generate_issue_key(issue)
        
        # Add to history
        history_entry = {
            "timestamp": timestamp,
            "key": issue_key,
            "type": issue.get("type", "unknown"),
            "name": issue.get("name", ""),
            "status": issue.get("status", ""),
            "namespace": issue.get("namespace", ""),
            "cluster_version": cluster_info.get("version") if cluster_info else None
        }
        data["issue_history"].append(history_entry)
        
        # Track recurring issues
        if issue_key not in data["recurring_issues"]:
            data["recurring_issues"][issue_key] = {
                "first_seen": timestamp,
                "last_seen": timestamp,
                "count": 0,
                "type": issue.get("type", "unknown"),
                "name": issue.get("name", ""),
                "sample_status": issue.get("status", ""),
                "pattern_keywords": extract_keywords(issue)
            }
        
        data["recurring_issues"][issue_key]["count"] += 1
        data["recurring_issues"][issue_key]["last_seen"] = timestamp
        
        # Auto-discover patterns from recurring issues
        if data["recurring_issues"][issue_key]["count"] >= 3:
            discover_pattern(data, issue_key, issue)
    
    # Trim old history (keep last 30 days)
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()
    data["issue_history"] = [h for h in data["issue_history"] if h["timestamp"] > cutoff]
    
    save_learning_data(data)
    return data


def generate_issue_key(issue):
    """Generate a unique key for an issue based on its characteristics"""
    parts = [
        issue.get("type", ""),
        issue.get("name", "").split("-")[0] if issue.get("name") else "",  # Base name without random suffix
        issue.get("namespace", ""),
        issue.get("status", "").split()[0] if issue.get("status") else ""  # First word of status
    ]
    return ":".join(filter(None, parts)).lower()


def extract_keywords(issue):
    """Extract keywords from an issue for pattern matching"""
    keywords = set()
    
    # From type
    if issue.get("type"):
        keywords.add(issue["type"].lower())
    
    # From name (split by common separators)
    if issue.get("name"):
        name = issue["name"].lower()
        for sep in ["-", "_", "."]:
            keywords.update(name.split(sep)[:3])  # First 3 parts
    
    # From status
    if issue.get("status"):
        status_keywords = ["crashloop", "error", "failed", "pending", "unknown", 
                         "oom", "evicted", "terminated", "notready", "degraded"]
        status_lower = issue["status"].lower()
        for kw in status_keywords:
            if kw in status_lower:
                keywords.add(kw)
    
    # From namespace
    if issue.get("namespace"):
        ns = issue["namespace"].lower()
        if "cnv" in ns or "kubevirt" in ns:
            keywords.add("kubevirt")
        elif "storage" in ns or "odf" in ns:
            keywords.add("storage")
        elif "machine" in ns:
            keywords.add("machine")
    
    return list(keywords)


def discover_pattern(data, issue_key, issue):
    """
    Automatically discover and record a pattern from a recurring issue.
    This is the core of the "learns from every run" feature.

    When confidence reaches the promotion threshold (3), the pattern is
    also written into the dynamic knowledge base so the RCA pattern engine
    picks it up on subsequent runs.
    """
    now = datetime.now().isoformat()

    if issue_key in data["patterns"]:
        data["patterns"][issue_key]["confidence"] += 1
        data["patterns"][issue_key]["last_matched"] = now
        _maybe_promote_to_knowledge_base(issue_key, data["patterns"][issue_key])
        return
    
    keywords = extract_keywords(issue)
    
    data["patterns"][issue_key] = {
        "discovered": now,
        "last_matched": now,
        "confidence": 1,
        "keywords": keywords,
        "type": issue.get("type", "unknown"),
        "suggested_check": f"check_{issue_key.replace(':', '_')}",
        "description": f"Auto-discovered pattern for recurring {issue.get('type', 'issue')}: {issue.get('name', 'unknown')}",
        "sample_issue": {
            "name": issue.get("name", ""),
            "status": issue.get("status", ""),
            "namespace": issue.get("namespace", "")
        }
    }
    
    print(f"  [Learning] Discovered new pattern: {issue_key}")


PROMOTION_THRESHOLD = 3


def _maybe_promote_to_knowledge_base(issue_key, pattern):
    """Promote a learned pattern into knowledge/known_issues.json once it
    has been seen enough times. Skips if a similar pattern already exists."""
    if pattern.get("confidence", 0) < PROMOTION_THRESHOLD:
        return
    if pattern.get("promoted"):
        return

    try:
        from healthchecks.knowledge_base import save_known_issue, pattern_exists

        keywords = pattern.get("keywords", [])
        if pattern_exists(keywords):
            return

        kb_key = f"learned-{issue_key.replace(':', '-')}"
        entry = {
            "pattern": keywords,
            "jira": [],
            "title": pattern.get("description", f"Learned: {issue_key}"),
            "description": pattern.get("description", ""),
            "root_cause": [f"Recurring issue detected {pattern['confidence']} times"],
            "suggestions": [
                f"This issue recurs frequently - investigate root cause",
                f"First seen: {pattern.get('discovered', 'unknown')}",
            ],
            "verify_cmd": "",
            "source": "learned",
            "confidence": min(pattern["confidence"] / 10.0, 1.0),
            "created": pattern.get("discovered", datetime.now().isoformat()),
            "last_matched": pattern.get("last_matched"),
            "investigation_commands": [],
        }
        save_known_issue(kb_key, entry)
        pattern["promoted"] = True
        print(f"  [Learning] Promoted pattern to knowledge base: {kb_key}")
    except Exception as exc:
        print(f"  [Learning] Failed to promote pattern: {exc}")


def get_learned_patterns():
    """Get all learned patterns for use in health checks"""
    data = load_learning_data()
    return data.get("patterns", {})


def get_recurring_issues(min_count=2):
    """Get issues that have occurred multiple times"""
    data = load_learning_data()
    recurring = {}
    
    for key, issue in data.get("recurring_issues", {}).items():
        if issue["count"] >= min_count:
            recurring[key] = issue
    
    return recurring


def get_issue_trends(days=7):
    """Analyze issue trends over recent period"""
    data = load_learning_data()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    
    recent = [h for h in data.get("issue_history", []) if h["timestamp"] > cutoff]
    
    # Count by type
    by_type = defaultdict(int)
    by_name = defaultdict(int)
    
    for h in recent:
        by_type[h.get("type", "unknown")] += 1
        by_name[h.get("name", "unknown").split("-")[0]] += 1
    
    return {
        "total_issues": len(recent),
        "by_type": dict(by_type),
        "by_name": dict(sorted(by_name.items(), key=lambda x: -x[1])[:10]),
        "period_days": days
    }


def record_fix_applied(issue_key, fix_description, success=True):
    """Record when a fix is applied and whether it worked"""
    data = load_learning_data()
    
    if issue_key not in data["learned_fixes"]:
        data["learned_fixes"][issue_key] = []
    
    data["learned_fixes"][issue_key].append({
        "timestamp": datetime.now().isoformat(),
        "fix": fix_description,
        "success": success
    })
    
    save_learning_data(data)


def get_suggested_fix(issue_key):
    """Get the most successful fix for an issue based on learning"""
    data = load_learning_data()
    fixes = data.get("learned_fixes", {}).get(issue_key, [])
    
    if not fixes:
        return None
    
    # Find most successful fix
    fix_success = defaultdict(lambda: {"success": 0, "total": 0})
    for f in fixes:
        fix_success[f["fix"]]["total"] += 1
        if f["success"]:
            fix_success[f["fix"]]["success"] += 1
    
    # Sort by success rate
    best = max(fix_success.items(), key=lambda x: x[1]["success"] / max(x[1]["total"], 1))
    return {
        "fix": best[0],
        "success_rate": best[1]["success"] / best[1]["total"],
        "times_tried": best[1]["total"]
    }


def get_learning_stats():
    """Get statistics about the learning system"""
    data = load_learning_data()
    
    return {
        "total_runs": data.get("total_runs", 0),
        "patterns_discovered": len(data.get("patterns", {})),
        "recurring_issues_tracked": len(data.get("recurring_issues", {})),
        "fixes_recorded": sum(len(f) for f in data.get("learned_fixes", {}).values()),
        "history_entries": len(data.get("issue_history", [])),
        "created": data.get("created"),
        "last_updated": data.get("last_updated")
    }


def match_learned_patterns(issue):
    """
    Match an issue against learned patterns.
    Returns matching patterns sorted by confidence.
    """
    data = load_learning_data()
    patterns = data.get("patterns", {})
    
    if not patterns:
        return []
    
    issue_keywords = set(extract_keywords(issue))
    matches = []
    
    for pattern_key, pattern in patterns.items():
        pattern_keywords = set(pattern.get("keywords", []))
        
        # Calculate match score
        if pattern_keywords:
            overlap = issue_keywords & pattern_keywords
            score = len(overlap) / len(pattern_keywords)
            
            if score >= 0.5:  # At least 50% keyword match
                matches.append({
                    "pattern_key": pattern_key,
                    "confidence": pattern.get("confidence", 1),
                    "match_score": score,
                    "description": pattern.get("description", ""),
                    "suggested_check": pattern.get("suggested_check", "")
                })
    
    # Sort by confidence * match_score
    matches.sort(key=lambda x: x["confidence"] * x["match_score"], reverse=True)
    return matches
