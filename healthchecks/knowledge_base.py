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
ROOT_CAUSE_RULES_FILE = os.path.join(KNOWLEDGE_DIR, "root_cause_rules.json")


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


def load_root_cause_rules():
    """Load root cause determination rules from JSON.

    Returns a dict keyed by rule ID. Each entry has: issue_types,
    keywords_all, keywords_any, cause, confidence, explanation,
    source, created, last_matched. Optional: extra_required,
    extra_required_any, special.
    """
    if not os.path.exists(ROOT_CAUSE_RULES_FILE):
        logger.warning("No %s found - determine_root_cause() will use empty ruleset", ROOT_CAUSE_RULES_FILE)
        return {}
    try:
        with open(ROOT_CAUSE_RULES_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load %s: %s", ROOT_CAUSE_RULES_FILE, exc)
        return {}


def save_root_cause_rule(key, entry):
    """Add or update a single root cause rule and persist to disk."""
    rules = load_root_cause_rules()
    rules[key] = entry
    _write_json(ROOT_CAUSE_RULES_FILE, rules)
    logger.info("Saved root cause rule '%s' (source=%s)", key, entry.get("source"))


def delete_root_cause_rule(key):
    """Remove a root cause rule. Returns True if deleted."""
    rules = load_root_cause_rules()
    if key in rules:
        del rules[key]
        _write_json(ROOT_CAUSE_RULES_FILE, rules)
        return True
    return False


def update_root_cause_rule_matched(key):
    """Update the last_matched timestamp for a root cause rule."""
    rules = load_root_cause_rules()
    if key in rules:
        rules[key]["last_matched"] = datetime.now().isoformat()
        _write_json(ROOT_CAUSE_RULES_FILE, rules)


def load_investigation_commands():
    """Return a dict mapping issue-type keys to investigation command lists.

    Built from known_issues.json: each pattern that has an
    investigation_commands field is indexed by the issue key *and* by
    the inv_type value it maps to. Also includes built-in commands for
    issue types that are not knowledge-base entries (e.g. pod-crashloop).
    """
    inv = dict(_BUILTIN_INV_COMMANDS)
    issues = load_known_issues()
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
    rc_rules = load_root_cause_rules()
    by_source = {}
    for entry in issues.values():
        src = entry.get("source", "unknown")
        by_source[src] = by_source.get(src, 0) + 1
    rc_by_source = {}
    for entry in rc_rules.values():
        src = entry.get("source", "unknown")
        rc_by_source[src] = rc_by_source.get(src, 0) + 1
    return {
        "total_patterns": len(issues),
        "total_bugs": len(bugs),
        "total_root_cause_rules": len(rc_rules),
        "by_source": by_source,
        "rc_rules_by_source": rc_by_source,
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
# Seeding (first-run only)
# ---------------------------------------------------------------------------

# All built-in patterns with investigation commands baked in.
# Previously these lived as KNOWN_ISSUES + INVESTIGATION_COMMANDS dicts in
# hybrid_health_check.py. They were merged here so the seed is self-contained
# and the engine has no hardcoded fallback dicts at runtime.
_BUILTIN_SEED = {
    "virt-handler-memory": {
        "pattern": ["virt-handler", "high_memory", "memory"],
        "jira": ["CNV-66551", "CNV-71448", "CNV-30274"],
        "title": "virt-handler High Memory Usage",
        "description": "virt-handler pods using more memory than expected. Common at scale (>50 VMs per node).",
        "root_cause": [
            "Memory requests are hardcoded and set too low for large scale deployments",
            "Goroutine leaks after EUS upgrades (CNV-71448)",
            "Object cache not properly cleaned up at high VM density",
        ],
        "suggestions": [
            "Check if running >50 VMs per node - consider spreading workload",
            "Review virt-handler resource requests in HyperConverged CR",
            "If after upgrade, consider rolling restart of virt-handler pods",
            "Monitor with: oc adm top pods -n openshift-cnv -l kubevirt.io=virt-handler",
        ],
        "verify_cmd": "oc adm top pods -n openshift-cnv -l kubevirt.io=virt-handler --no-headers",
        "inv_type": "virt-handler-memory",
        "investigation_commands": [
            {"cmd": "oc adm top pods -n openshift-cnv -l kubevirt.io=virt-handler --no-headers 2>&1", "desc": "virt-handler resource usage"},
            {"cmd": "oc get pods -n openshift-cnv -l kubevirt.io=virt-handler -o wide --no-headers 2>&1 | awk '{print $1, $7}' | head -20", "desc": "virt-handler pod locations"},
            {"cmd": "oc exec -n openshift-cnv $(oc get pods -n openshift-cnv -l kubevirt.io=virt-handler -o name | head -1) -- cat /proc/meminfo 2>&1 | grep -E 'MemTotal|MemFree|MemAvailable' | head -3", "desc": "Node memory info"},
            {"cmd": "oc get vmi -A --no-headers 2>&1 | wc -l", "desc": "Total VMI count"},
            {"cmd": "oc logs -n openshift-cnv $(oc get pods -n openshift-cnv -l kubevirt.io=virt-handler -o name | head -1) --tail=20 2>&1 | grep -i 'memory\\|oom\\|error' | head -10", "desc": "Memory-related logs"},
        ],
    },
    "virt-handler-error": {
        "pattern": ["virt-handler", "error", "crash", "restart"],
        "jira": ["CNV-68292", "CNV-70607"],
        "title": "virt-handler Pod Errors",
        "description": "virt-handler pods in error state, often during high-scale VM operations.",
        "root_cause": [
            "Deleting large number of VMs at once (>6k) can lock virt-handler",
            "Tight loop on uncompleted migrations blocks node drain",
        ],
        "suggestions": [
            "Delete VMs in smaller batches (100-200 at a time)",
            "Check for stuck migrations: oc get vmim -A | grep Running",
            "Force delete stuck pods if necessary: oc delete pod -n openshift-cnv <pod> --force",
        ],
        "verify_cmd": "oc get pods -n openshift-cnv -l kubevirt.io=virt-handler --no-headers",
        "inv_type": "virt-handler-memory",
        "investigation_commands": [
            {"cmd": "oc adm top pods -n openshift-cnv -l kubevirt.io=virt-handler --no-headers 2>&1", "desc": "virt-handler resource usage"},
            {"cmd": "oc get pods -n openshift-cnv -l kubevirt.io=virt-handler -o wide --no-headers 2>&1 | awk '{print $1, $7}' | head -20", "desc": "virt-handler pod locations"},
            {"cmd": "oc exec -n openshift-cnv $(oc get pods -n openshift-cnv -l kubevirt.io=virt-handler -o name | head -1) -- cat /proc/meminfo 2>&1 | grep -E 'MemTotal|MemFree|MemAvailable' | head -3", "desc": "Node memory info"},
            {"cmd": "oc get vmi -A --no-headers 2>&1 | wc -l", "desc": "Total VMI count"},
            {"cmd": "oc logs -n openshift-cnv $(oc get pods -n openshift-cnv -l kubevirt.io=virt-handler -o name | head -1) --tail=20 2>&1 | grep -i 'memory\\|oom\\|error' | head -10", "desc": "Memory-related logs"},
        ],
    },
    "noobaa-endpoint": {
        "pattern": ["noobaa-endpoint", "ContainerStatusUnknown", "openshift-storage"],
        "jira": ["OCPBUGS-storage"],
        "title": "NooBaa Endpoint Issues",
        "description": "NooBaa endpoint pods in ContainerStatusUnknown state.",
        "root_cause": [
            "Node failure or network partition caused container state to become unknown",
            "ODF/NooBaa components not properly reconciled after node issues",
        ],
        "suggestions": [
            "Check node health where pods were scheduled",
            "Delete the stuck pods to trigger rescheduling: oc delete pod -n openshift-storage <pod>",
            "Verify ODF operator health: oc get csv -n openshift-storage",
        ],
        "verify_cmd": "oc get pods -n openshift-storage -l noobaa-core=noobaa --no-headers",
        "inv_type": "noobaa",
        "investigation_commands": [
            {"cmd": "oc get pods -n openshift-storage -l noobaa-core=noobaa 2>&1", "desc": "NooBaa pod status"},
            {"cmd": "oc describe pod {pod} -n openshift-storage 2>&1 | grep -A15 'Events:'", "desc": "Pod events"},
            {"cmd": "oc get storagecluster -n openshift-storage 2>&1", "desc": "Storage cluster status"},
            {"cmd": "oc get noobaa -n openshift-storage -o yaml 2>&1 | grep -A5 'status:'", "desc": "NooBaa status"},
            {"cmd": "oc logs {pod} -n openshift-storage --tail=30 2>&1 | head -20", "desc": "Pod logs"},
        ],
    },
    "metal3-crashloop": {
        "pattern": ["metal3-image-customization", "CrashLoopBackOff", "Init"],
        "jira": ["OCPBUGS-48789"],
        "title": "Metal3 Image Customization CrashLoop",
        "description": "metal3-image-customization pod failing to start.",
        "root_cause": [
            "Service validation fails when workers are taken offline for servicing",
            "Network connectivity issues to metal3-image-customization-service",
        ],
        "suggestions": [
            "Check metal3 service: oc get svc -n openshift-machine-api",
            "Review pod logs: oc logs -n openshift-machine-api -l app=metal3-image-customization",
            "Ensure at least one worker is available during servicing operations",
        ],
        "verify_cmd": "oc get pods -n openshift-machine-api -l app=metal3-image-customization --no-headers",
        "inv_type": "metal3",
        "investigation_commands": [
            {"cmd": "oc get pods -n openshift-machine-api -l app=metal3-image-customization 2>&1", "desc": "Metal3 pods"},
            {"cmd": "oc logs -n openshift-machine-api -l app=metal3-image-customization --tail=50 2>&1 | head -30", "desc": "Pod logs"},
            {"cmd": "oc describe pod {pod} -n openshift-machine-api 2>&1 | grep -A20 'Events:'", "desc": "Pod events"},
            {"cmd": "oc get svc -n openshift-machine-api | grep metal3 2>&1", "desc": "Metal3 services"},
            {"cmd": "oc get bmh -A 2>&1 | head -10", "desc": "BareMetalHost status"},
        ],
    },
    "container-status-unknown": {
        "pattern": ["ContainerStatusUnknown"],
        "jira": ["OCPBUGS-general"],
        "title": "Container Status Unknown",
        "description": "Pods stuck in ContainerStatusUnknown state.",
        "root_cause": [
            "Node became unreachable or was rebooted unexpectedly",
            "Kubelet lost connection to container runtime",
            "Node was cordoned/drained but pods weren't properly evicted",
        ],
        "suggestions": [
            "Check node status: oc get nodes",
            "Force delete stuck pods: oc delete pod <pod> -n <ns> --force --grace-period=0",
            "Check kubelet logs on affected node",
            "Verify node network connectivity",
        ],
        "verify_cmd": "oc get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded | grep -i unknown",
        "inv_type": "pod-unknown",
        "investigation_commands": [
            {"cmd": "oc get pod {pod} -n {ns} -o wide 2>&1", "desc": "Pod details with node"},
            {"cmd": "oc describe pod {pod} -n {ns} 2>&1 | grep -A5 'Conditions:'", "desc": "Pod conditions"},
            {"cmd": "oc get node $(oc get pod {pod} -n {ns} -o jsonpath='{{.spec.nodeName}}' 2>/dev/null) 2>&1 | tail -1", "desc": "Node status"},
            {"cmd": "oc get events -n {ns} --field-selector involvedObject.name={pod} 2>&1 | tail -5", "desc": "Related events"},
        ],
    },
    "volumesnapshot-not-ready": {
        "pattern": ["volumesnapshot", "snapshot_issues", "not ready"],
        "jira": ["CNV-45516", "CNV-52369", "CNV-74930"],
        "title": "VolumeSnapshot Not Ready",
        "description": "VolumeSnapshots stuck in non-ready state.",
        "root_cause": [
            "LVM Storage with Filesystem mode has size mismatch issues (CNV-52369)",
            "Dangling snapshots from previous operations (CNV-45516)",
            "Default storage class changes can delete snapshots (CNV-74930)",
        ],
        "suggestions": [
            "Check snapshot status: oc get volumesnapshot -A -o wide",
            "For LVM: ensure using Block volume mode for snapshots",
            "Delete orphaned snapshots if source PVC no longer exists",
            "Verify VolumeSnapshotClass exists: oc get volumesnapshotclass",
        ],
        "verify_cmd": "oc get volumesnapshot -A --no-headers | grep -v true",
        "inv_type": "volumesnapshot",
        "investigation_commands": [
            {"cmd": "oc get volumesnapshot -A -o wide 2>&1 | grep -v 'true' | head -10", "desc": "Unhealthy snapshots"},
            {"cmd": "oc describe volumesnapshot {name} -n {ns} 2>&1 | grep -A10 'Status:'", "desc": "Snapshot status details"},
            {"cmd": "oc get volumesnapshotclass 2>&1", "desc": "VolumeSnapshot classes"},
            {"cmd": "oc get volumesnapshotcontent 2>&1 | grep -v 'true' | head -5", "desc": "Snapshot content status"},
            {"cmd": "oc get pvc -A 2>&1 | head -10", "desc": "PVC status"},
        ],
    },
    "datavolume-stuck": {
        "pattern": ["dv_issues", "datavolume", "ImportInProgress", "Pending"],
        "jira": ["CNV-storage"],
        "title": "DataVolume Import Stuck",
        "description": "DataVolumes stuck in import or pending state.",
        "root_cause": [
            "CDI importer pod failed or is slow",
            "Source image URL unreachable",
            "Insufficient storage space",
        ],
        "suggestions": [
            "Check CDI pods: oc get pods -n openshift-cnv -l app=containerized-data-importer",
            "Check importer pod logs: oc logs -n <ns> importer-<dv-name>",
            "Verify source URL accessibility",
            "Check PVC events: oc describe pvc <pvc-name> -n <ns>",
        ],
        "verify_cmd": "oc get dv -A --no-headers | grep -v Succeeded",
        "inv_type": "csi",
        "investigation_commands": [
            {"cmd": "oc get pods -A --no-headers 2>&1 | grep -iE 'csi|ceph|storage' | grep -ivE '^openshift-storage.*Running.*[1-9]/' | head -10", "desc": "Non-healthy storage/CSI pods"},
            {"cmd": "oc get pod {pod} -n {ns} -o wide 2>&1", "desc": "Specific CSI pod status"},
            {"cmd": "oc describe pod {pod} -n {ns} 2>&1 | grep -A20 'Events:' | head -25", "desc": "CSI pod events"},
            {"cmd": "oc logs {pod} -n {ns} --previous --tail=40 2>&1 | tail -25", "desc": "Previous logs of CSI pod"},
            {"cmd": "oc get csidrivers 2>&1", "desc": "CSI drivers"},
            {"cmd": "oc get sc 2>&1", "desc": "Storage classes"},
        ],
    },
    "migration-failed": {
        "pattern": ["migration", "failed", "vmim"],
        "jira": ["CNV-74568", "CNV-71962", "CNV-74856", "CNV-76280"],
        "title": "VM Live Migration Failed",
        "description": "Virtual machine migrations failing.",
        "root_cause": [
            "CPU feature mismatch between source and target nodes (CNV-74856)",
            "Migration between different CPU architectures AMD/Intel (CNV-71957)",
            "Migration breaks after cluster upgrade (CNV-74568)",
            "Storage migration between different backends fails (CNV-76280)",
        ],
        "suggestions": [
            "Check VMI migration status: oc get vmim -A -o wide",
            "Ensure homogeneous CPU types across cluster or use CPU passthrough",
            "After upgrades, restart virt-handler: oc rollout restart ds/virt-handler -n openshift-cnv",
            "For storage migration, ensure same storage class capabilities",
        ],
        "verify_cmd": "oc get vmim -A --no-headers | grep -i failed",
        "inv_type": "migration",
        "investigation_commands": [
            {"cmd": "oc get vmim -A -o wide 2>&1 | head -10", "desc": "Migration status"},
            {"cmd": "oc describe vmim {name} -n {ns} 2>&1 | grep -A20 'Status:'", "desc": "Migration details"},
            {"cmd": "oc get vmi {vm} -n {ns} -o yaml 2>&1 | grep -A10 'migrationState:'", "desc": "VMI migration state"},
            {"cmd": "oc logs -n openshift-cnv -l kubevirt.io=virt-handler --tail=30 2>&1 | grep -i migration | head -10", "desc": "Migration logs"},
        ],
    },
    "stuck-migration": {
        "pattern": ["stuck_migrations", "migration", "Running"],
        "jira": ["CNV-74866", "CNV-70607", "CNV-69281"],
        "title": "VM Migration Stuck",
        "description": "Live migrations stuck in Running state for extended periods.",
        "root_cause": [
            "virt-handler tight loop on uncompleted migration (CNV-74866)",
            "Network bandwidth saturation during large VM migrations",
            "parallelMigrationsPerCluster limit not working properly (CNV-69281)",
        ],
        "suggestions": [
            "Check migration details: oc describe vmim <name> -n <ns>",
            "Cancel stuck migration: oc delete vmim <name> -n <ns>",
            "Reduce parallel migrations in HyperConverged spec",
            "Check network bandwidth between nodes",
        ],
        "verify_cmd": "oc get vmim -A --no-headers | grep Running",
        "inv_type": "migration",
        "investigation_commands": [
            {"cmd": "oc get vmim -A -o wide 2>&1 | head -10", "desc": "Migration status"},
            {"cmd": "oc describe vmim {name} -n {ns} 2>&1 | grep -A20 'Status:'", "desc": "Migration details"},
            {"cmd": "oc get vmi {vm} -n {ns} -o yaml 2>&1 | grep -A10 'migrationState:'", "desc": "VMI migration state"},
            {"cmd": "oc logs -n openshift-cnv -l kubevirt.io=virt-handler --tail=30 2>&1 | grep -i migration | head -10", "desc": "Migration logs"},
        ],
    },
    "cordoned-node-vms": {
        "pattern": ["cordoned_vms", "SchedulingDisabled"],
        "jira": ["CNV-20450"],
        "title": "VMs on Cordoned Nodes",
        "description": "VMs running on nodes marked as SchedulingDisabled.",
        "root_cause": [
            "Node was cordoned but VMs weren't migrated (CNV-20450)",
            "Migrations to cordoned nodes during testing",
        ],
        "suggestions": [
            "Migrate VMs off cordoned nodes: virtctl migrate <vm-name>",
            "Check why node is cordoned: oc describe node <node>",
            "Drain node properly: oc adm drain <node> --ignore-daemonsets --delete-emptydir-data",
        ],
        "verify_cmd": "oc get nodes | grep SchedulingDisabled && oc get vmi -A -o wide",
        "inv_type": "node",
        "investigation_commands": [
            {"cmd": "oc get nodes -o wide 2>&1", "desc": "All node status"},
            {"cmd": "oc describe node {name} 2>&1 | grep -A20 'Conditions:'", "desc": "Node conditions"},
            {"cmd": "oc adm top node {name} 2>&1", "desc": "Node resource usage"},
            {"cmd": "oc get events --field-selector involvedObject.name={name} --sort-by='.lastTimestamp' 2>&1 | tail -10", "desc": "Node events"},
        ],
    },
    "etcd-unhealthy": {
        "pattern": ["etcd", "unhealthy"],
        "jira": ["OCPBUGS-74962", "OCPBUGS-70140"],
        "title": "etcd Cluster Issues",
        "description": "etcd members unhealthy or high latency.",
        "root_cause": [
            "High etcd latency under load (OCPBUGS-74962)",
            "Database size growing due to large operators (OCPBUGS-70140)",
            "Disk I/O saturation on control plane nodes",
        ],
        "suggestions": [
            "Check etcd status: oc get pods -n openshift-etcd",
            "Monitor etcd metrics for latency spikes",
            "Check disk I/O on control plane nodes",
            "Consider defragmentation if DB size is large",
        ],
        "verify_cmd": "oc get pods -n openshift-etcd -l app=etcd --no-headers",
        "inv_type": "etcd",
        "investigation_commands": [
            {"cmd": "oc get pods -n openshift-etcd -l app=etcd 2>&1", "desc": "etcd pod status"},
            {"cmd": "oc logs -n openshift-etcd -l app=etcd --tail=30 2>&1 | grep -i 'error\\|warn\\|slow' | head -15", "desc": "etcd error logs"},
            {"cmd": "oc get etcd cluster -o yaml 2>&1 | grep -A10 'status:'", "desc": "etcd cluster status"},
            {"cmd": "oc rsh -n openshift-etcd $(oc get pods -n openshift-etcd -l app=etcd -o name | head -1) etcdctl endpoint health 2>&1", "desc": "etcd health check"},
        ],
    },
    "oom-events": {
        "pattern": ["oom_events", "OOMKilled"],
        "jira": ["CNV-75962", "CNV-63538"],
        "title": "OOMKilled Pods",
        "description": "Pods being killed due to Out of Memory.",
        "root_cause": [
            "kubevirt-migration-controller OOMKilled at scale (CNV-75962)",
            "virt-launcher consuming more memory than assigned (CNV-63538)",
            "Memory limits set too low for workload",
        ],
        "suggestions": [
            "Check which pods are OOMKilled: oc get events -A --field-selector reason=OOMKilled",
            "Review memory requests/limits in pod spec",
            "For CNV components, check HyperConverged resource settings",
            "Monitor memory usage: oc adm top pods -n <namespace>",
        ],
        "verify_cmd": "oc get events -A --field-selector reason=OOMKilled --no-headers",
        "inv_type": "oom",
        "investigation_commands": [
            {"cmd": "oc get events -A --field-selector reason=OOMKilled --sort-by='.lastTimestamp' 2>&1 | tail -10", "desc": "Recent OOM events"},
            {"cmd": "oc describe pod {pod} -n {ns} 2>&1 | grep -A5 'Resources:'", "desc": "Pod resource limits"},
            {"cmd": "oc adm top pods -n {ns} --no-headers 2>&1 | head -10", "desc": "Namespace resource usage"},
        ],
    },
    "csi-issues": {
        "pattern": ["csi_issues", "csi", "driver"],
        "jira": ["OCPBUGS-69390", "CNV-70889"],
        "title": "CSI Driver Issues",
        "description": "CSI driver pods not running properly.",
        "root_cause": [
            "CSI driver crash on specific cloud providers (OCPBUGS-69390)",
            "kubevirt-csi-controller crash when resize not supported (CNV-70889)",
        ],
        "suggestions": [
            "Check CSI pods: oc get pods -A | grep csi",
            "Review CSI driver logs: oc logs -n <ns> <csi-pod>",
            "Verify storage class configuration",
            "Check if storage backend supports required features",
        ],
        "verify_cmd": "oc get pods -A --no-headers | grep csi | grep -v Running",
        "inv_type": "csi",
        "investigation_commands": [
            {"cmd": "oc get pods -A --no-headers 2>&1 | grep -iE 'csi|ceph|storage' | grep -ivE '^openshift-storage.*Running.*[1-9]/' | head -10", "desc": "Non-healthy storage/CSI pods"},
            {"cmd": "oc get pod {pod} -n {ns} -o wide 2>&1", "desc": "Specific CSI pod status"},
            {"cmd": "oc describe pod {pod} -n {ns} 2>&1 | grep -A20 'Events:' | head -25", "desc": "CSI pod events"},
            {"cmd": "oc logs {pod} -n {ns} --previous --tail=40 2>&1 | tail -25", "desc": "Previous logs of CSI pod"},
            {"cmd": "oc get csidrivers 2>&1", "desc": "CSI drivers"},
            {"cmd": "oc get sc 2>&1", "desc": "Storage classes"},
        ],
    },
    "mco-degraded": {
        "pattern": ["machine-config", "operator-degraded", "machineconfigpool", "syncRequiredMachineConfigPools"],
        "jira": ["OCPBUGS-47041", "OCPBUGS-38553", "OCPBUGS-41786"],
        "title": "Machine Config Operator Degraded",
        "description": "Machine Config Operator is degraded. MachineConfigPool workers may be failing to render or apply updated MachineConfigs.",
        "root_cause": [
            "MachineConfigPool 'worker' is degraded - nodes failed to apply the desired MachineConfig (context deadline exceeded)",
            "Nodes stuck in NotReady or SchedulingDisabled after a failed config render or drain timeout",
            "Post-upgrade MC render failure due to incompatible custom MachineConfigs (OCPBUGS-47041)",
            "MCD drain timeout when pods have long terminationGracePeriod or PodDisruptionBudgets blocking eviction",
        ],
        "suggestions": [
            "Check MachineConfigPool status: oc get mcp",
            "Identify degraded nodes: oc get nodes -o wide | grep -v ' Ready '",
            "Check MCD logs on degraded node: oc logs -n openshift-machine-config-operator machine-config-daemon-<id>",
            "Review rendered MC diff: oc describe mc <rendered-mc>",
            "If stuck after upgrade, approve pending CSRs: oc get csr | grep Pending",
            "Force reboot stuck node: oc debug node/<node> -- chroot /host systemctl reboot",
        ],
        "verify_cmd": "oc get mcp && oc get co machine-config",
        "inv_type": "operator-degraded",
        "investigation_commands": [
            {"cmd": "oc get co --no-headers 2>&1 | grep -vE 'True.*False.*False'", "desc": "Unhealthy cluster operators"},
            {"cmd": "oc get co {name} -o yaml 2>&1 | grep -A5 'message:' | head -30", "desc": "Operator error messages (full)"},
            {"cmd": "oc describe co {name} 2>&1 | grep -A25 'Conditions:' | head -30", "desc": "Operator conditions"},
            {"cmd": "oc get pods -n openshift-{name} --no-headers 2>&1 | head -20", "desc": "All pods in operator namespace"},
            {"cmd": "oc get pods -A --no-headers 2>&1 | grep -E 'openshift-.*{name}' | grep -v Running | head -10", "desc": "Non-running pods in operator namespace"},
            {"cmd": "oc logs -n openshift-{name} $(oc get pods -n openshift-{name} --no-headers -o name 2>/dev/null | head -1) --tail=40 2>&1 | grep -iE 'error|fail|warn|timeout|degrade' | tail -15", "desc": "Recent error/warning logs"},
            {"cmd": "oc get events -n openshift-{name} --sort-by='.lastTimestamp' 2>&1 | tail -15", "desc": "Recent events in operator namespace"},
            {"cmd": "oc get mcp 2>&1", "desc": "MachineConfigPool status (if MCO)"},
            {"cmd": "oc get nodes --no-headers 2>&1 | grep -v ' Ready ' | head -10", "desc": "Nodes not in Ready state"},
            {"cmd": "oc get csr 2>&1 | grep -i pending | head -5", "desc": "Pending CSRs"},
        ],
    },
    "operator-degraded-generic": {
        "pattern": ["operator-degraded", "degraded"],
        "jira": [],
        "title": "Cluster Operator Degraded",
        "description": "One or more cluster operators are in a Degraded state, indicating an issue within the operator's managed components.",
        "root_cause": [
            "Underlying pods managed by the operator are crashing or failing health checks",
            "Resource constraints (CPU/memory) preventing operator pods from functioning",
            "Configuration drift or invalid custom resource changes",
            "Post-upgrade reconciliation failure",
        ],
        "suggestions": [
            "Get operator details: oc describe co <operator-name>",
            "Check operator namespace pods: oc get pods -n openshift-<operator-name>",
            "Review operator logs: oc logs -n openshift-<operator-name> deployment/<operator-name>",
            "Check recent events: oc get events -n openshift-<operator-name> --sort-by='.lastTimestamp'",
            "If post-upgrade, wait for reconciliation or check for pending CSRs",
        ],
        "verify_cmd": "oc get co --no-headers | grep -vE 'True.*False.*False'",
        "inv_type": "operator-degraded",
        "investigation_commands": [
            {"cmd": "oc get co --no-headers 2>&1 | grep -vE 'True.*False.*False'", "desc": "Unhealthy cluster operators"},
            {"cmd": "oc get co {name} -o yaml 2>&1 | grep -A5 'message:' | head -30", "desc": "Operator error messages (full)"},
            {"cmd": "oc describe co {name} 2>&1 | grep -A25 'Conditions:' | head -30", "desc": "Operator conditions"},
            {"cmd": "oc get pods -n openshift-{name} --no-headers 2>&1 | head -20", "desc": "All pods in operator namespace"},
            {"cmd": "oc get pods -A --no-headers 2>&1 | grep -E 'openshift-.*{name}' | grep -v Running | head -10", "desc": "Non-running pods in operator namespace"},
            {"cmd": "oc logs -n openshift-{name} $(oc get pods -n openshift-{name} --no-headers -o name 2>/dev/null | head -1) --tail=40 2>&1 | grep -iE 'error|fail|warn|timeout|degrade' | tail -15", "desc": "Recent error/warning logs"},
            {"cmd": "oc get events -n openshift-{name} --sort-by='.lastTimestamp' 2>&1 | tail -15", "desc": "Recent events in operator namespace"},
            {"cmd": "oc get mcp 2>&1", "desc": "MachineConfigPool status (if MCO)"},
            {"cmd": "oc get nodes --no-headers 2>&1 | grep -v ' Ready ' | head -10", "desc": "Nodes not in Ready state"},
            {"cmd": "oc get csr 2>&1 | grep -i pending | head -5", "desc": "Pending CSRs"},
        ],
    },
    "operator-unavailable": {
        "pattern": ["operator-unavailable", "unavailable"],
        "jira": [],
        "title": "Cluster Operator Unavailable",
        "description": "One or more cluster operators are unavailable, which means the operator's core functionality is not working.",
        "root_cause": [
            "Operator pods are not running or in CrashLoopBackOff",
            "Critical dependency (e.g., etcd, API server) is down",
            "Node hosting the operator pod went offline",
            "Webhook or admission controller blocking operator reconciliation",
        ],
        "suggestions": [
            "Immediately check: oc get co <operator-name> -o yaml",
            "Check operator pods: oc get pods -n openshift-<operator-name> -o wide",
            "Look for crashloop: oc get pods -A | grep -E 'CrashLoop|Error'",
            "Check node health: oc get nodes -o wide",
            "Review API server availability: oc get pods -n openshift-kube-apiserver",
        ],
        "verify_cmd": "oc get co --no-headers | grep -v 'True'",
        "inv_type": "operator-unavailable",
        "investigation_commands": [
            {"cmd": "oc get co --no-headers 2>&1 | grep -vE 'True.*False.*False'", "desc": "Unhealthy cluster operators"},
            {"cmd": "oc get co {name} -o yaml 2>&1 | grep -A5 'message:' | head -30", "desc": "Operator error messages (full)"},
            {"cmd": "oc describe co {name} 2>&1 | grep -A25 'Conditions:' | head -30", "desc": "Operator conditions"},
            {"cmd": "oc get pods -n openshift-{name} --no-headers 2>&1 | head -20", "desc": "All pods in operator namespace"},
            {"cmd": "oc get pods -A --no-headers 2>&1 | grep -E 'openshift-.*{name}' | grep -v Running | head -10", "desc": "Non-running pods in operator namespace"},
            {"cmd": "oc logs -n openshift-{name} $(oc get pods -n openshift-{name} --no-headers -o name 2>/dev/null | head -1) --tail=40 2>&1 | grep -iE 'error|fail|warn|timeout' | tail -15", "desc": "Recent error/warning logs"},
            {"cmd": "oc get events -n openshift-{name} --sort-by='.lastTimestamp' 2>&1 | tail -15", "desc": "Recent events in operator namespace"},
            {"cmd": "oc get nodes --no-headers 2>&1 | grep -v ' Ready ' | head -10", "desc": "Nodes not in Ready state"},
        ],
    },
    "node-not-ready": {
        "pattern": ["node", "not ready", "unhealthy"],
        "jira": ["OCPBUGS-42135"],
        "title": "Node Not Ready",
        "description": "One or more nodes are in NotReady state, meaning workloads cannot be scheduled there.",
        "root_cause": [
            "Kubelet crashed or stopped on the affected node",
            "Network partition between node and control plane",
            "Disk pressure, memory pressure, or PID pressure conditions",
            "Node kernel panic or hardware failure",
        ],
        "suggestions": [
            "Check node conditions: oc describe node <node-name> | grep -A20 Conditions",
            "Check kubelet on node: oc debug node/<node-name> -- chroot /host journalctl -u kubelet --since '30m ago'",
            "Check system resources: oc adm top node <node-name>",
            "If unrecoverable, drain and replace: oc adm drain <node-name> --ignore-daemonsets --delete-emptydir-data",
        ],
        "verify_cmd": "oc get nodes -o wide",
        "inv_type": "node",
        "investigation_commands": [
            {"cmd": "oc get nodes -o wide 2>&1", "desc": "All node status"},
            {"cmd": "oc describe node {name} 2>&1 | grep -A20 'Conditions:'", "desc": "Node conditions"},
            {"cmd": "oc adm top node {name} 2>&1", "desc": "Node resource usage"},
            {"cmd": "oc get events --field-selector involvedObject.name={name} --sort-by='.lastTimestamp' 2>&1 | tail -10", "desc": "Node events"},
        ],
    },
    "alerts-firing": {
        "pattern": ["alert", "firing"],
        "jira": [],
        "title": "Cluster Alerts Firing",
        "description": "Active alerts indicate components that need attention. Critical alerts may require immediate action.",
        "root_cause": [
            "Alerts are symptom indicators - the root cause depends on the specific alert",
            "Common: resource exhaustion, component failures, certificate expiry, etcd issues",
        ],
        "suggestions": [
            "View active alerts in console: Observe > Alerting > Alerts",
            "Check Prometheus: oc -n openshift-monitoring exec -c prometheus prometheus-k8s-0 -- promtool query instant http://localhost:9090 'ALERTS{alertstate=\"firing\"}'",
            "Silence non-critical alerts during maintenance windows",
            "Address critical alerts first, then warnings",
        ],
        "verify_cmd": "oc get pods -n openshift-monitoring",
        "inv_type": "alert",
        "investigation_commands": [
            {"cmd": "oc get pods -A --no-headers 2>&1 | grep -v Running | grep -v Completed | head -15", "desc": "Non-running pods"},
            {"cmd": "oc get co --no-headers 2>&1 | grep -vE 'True.*False.*False'", "desc": "Unhealthy cluster operators"},
            {"cmd": "oc get nodes --no-headers 2>&1 | grep -v ' Ready' | head -10", "desc": "Unhealthy nodes"},
            {"cmd": "oc get events -A --sort-by='.lastTimestamp' 2>&1 | grep -i 'warning' | tail -15", "desc": "Recent warning events"},
        ],
    },
}

# Investigation commands indexed by inv_type (for pod-crashloop and pod-unknown
# which are not KNOWN_ISSUES entries but are referenced by the investigation
# runner). These are also baked into known_issues.json during seeding.
_BUILTIN_INV_COMMANDS = {
    "pod-crashloop": [
        {"cmd": "oc logs {pod} -n {ns} --tail=50 2>&1 | head -30", "desc": "Recent pod logs"},
        {"cmd": "oc logs {pod} -n {ns} --previous --tail=30 2>&1 | head -20", "desc": "Previous container logs"},
        {"cmd": "oc describe pod {pod} -n {ns} 2>&1 | grep -A20 'Events:'", "desc": "Pod events"},
        {"cmd": "oc get pod {pod} -n {ns} -o jsonpath='{{.status.containerStatuses[*].state}}' 2>&1", "desc": "Container state"},
    ],
    "pod-unknown": [
        {"cmd": "oc get pod {pod} -n {ns} -o wide 2>&1", "desc": "Pod details with node"},
        {"cmd": "oc describe pod {pod} -n {ns} 2>&1 | grep -A5 'Conditions:'", "desc": "Pod conditions"},
        {"cmd": "oc get node $(oc get pod {pod} -n {ns} -o jsonpath='{{.spec.nodeName}}' 2>/dev/null) 2>&1 | tail -1", "desc": "Node status"},
        {"cmd": "oc get events -n {ns} --field-selector involvedObject.name={pod} 2>&1 | tail -5", "desc": "Related events"},
    ],
}


def _seed_known_issues():
    """Generate known_issues.json from the built-in seed data on first run."""
    now = datetime.now().isoformat()
    merged = {}
    for key, entry in _BUILTIN_SEED.items():
        merged[key] = {
            **entry,
            "source": "built-in",
            "confidence": 1.0,
            "created": now,
            "last_matched": None,
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
