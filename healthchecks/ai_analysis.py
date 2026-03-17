"""
AI-powered Root Cause Analysis using Google Gemini.

Analyzes health check data collected from the OCP cluster and produces
a structured RCA with correlated failures, root causes, and remediation steps.
"""
import json
import logging
import os
import re
from datetime import datetime

logger = logging.getLogger(__name__)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")

SYSTEM_PROMPT = """\
You are an expert OpenShift and CNV (Container-Native Virtualization) cluster \
health analyst at Red Hat. You receive structured health check data from a \
production OCP cluster and must produce a root cause analysis.

Your analysis should:
1. Correlate failures across different subsystems (nodes, operators, pods, VMs, \
storage, network, etcd).
2. Identify the most likely root cause chain - what failed first and what are \
downstream effects.
3. Rank issues by severity (Critical / Warning / Info).
4. Provide specific remediation steps (oc commands, config changes, or escalation).
5. Flag anything that looks like a known CNV/OCP bug pattern.

When rule-based analysis findings are provided, use them as a starting point:
- Confirm or challenge the pattern-matched root causes with deeper reasoning.
- Identify correlations the rule engine missed (cross-subsystem cascading failures).
- Fill gaps where the rule engine returned "Unknown Issue".
- Add context or nuance the static patterns cannot provide.
- Do NOT simply repeat the rule-based findings verbatim.

Be concise and actionable. Use markdown formatting with headers and bullet points.\
"""


def _build_health_summary(data):
    """Distill the full health data dict into a concise text summary for the prompt."""
    lines = []

    lines.append(f"Cluster: {data.get('cluster', 'unknown')}")
    lines.append(f"OCP Version: {data.get('version', 'unknown')}")
    ts = data.get("timestamp")
    if isinstance(ts, datetime):
        lines.append(f"Collected: {ts.strftime('%Y-%m-%d %H:%M:%S')}")

    nodes = data.get("nodes", {})
    healthy_count = len(nodes.get("healthy", []))
    unhealthy = nodes.get("unhealthy", [])
    lines.append(f"\n## Nodes ({healthy_count} healthy, {len(unhealthy)} unhealthy)")
    for n in unhealthy:
        if isinstance(n, dict):
            lines.append(f"  - {n.get('name', '?')}: {n.get('status', '?')} roles={n.get('roles', '?')}")
        else:
            lines.append(f"  - {n}")

    ops = data.get("operators", {})
    degraded = ops.get("degraded", [])
    unavailable = ops.get("unavailable", [])
    healthy_ops = len(ops.get("healthy", []))
    lines.append(f"\n## Cluster Operators ({healthy_ops} healthy, {len(degraded)} degraded, {len(unavailable)} unavailable)")
    for op in degraded:
        lines.append(f"  - {op}: DEGRADED")
    for op in unavailable:
        lines.append(f"  - {op}: UNAVAILABLE")

    pods = data.get("pods", {})
    unhealthy_pods = pods.get("unhealthy", [])
    lines.append(f"\n## Pods ({pods.get('healthy', 0)} running, {len(unhealthy_pods)} unhealthy)")
    for p in unhealthy_pods[:20]:
        if isinstance(p, dict):
            lines.append(f"  - {p.get('ns', '?')}/{p.get('name', '?')}: {p.get('status', '?')} restarts={p.get('restarts', '?')}")
        else:
            lines.append(f"  - {p}")
    if len(unhealthy_pods) > 20:
        lines.append(f"  ... +{len(unhealthy_pods) - 20} more")

    kv = data.get("kubevirt", {})
    lines.append(f"\n## KubeVirt/CNV (VMs running: {kv.get('vms_running', 0)})")
    for vmi in kv.get("failed_vmis", []):
        if isinstance(vmi, dict):
            lines.append(f"  - {vmi.get('ns', '?')}/{vmi.get('name', '?')}: {vmi.get('phase', '?')}")
        else:
            lines.append(f"  - {vmi}")

    vh = data.get("virt_handler", {})
    if vh.get("unhealthy"):
        lines.append(f"\n  virt-handler unhealthy: {vh['unhealthy']}")
    if vh.get("high_memory"):
        lines.append(f"  virt-handler high memory: {vh['high_memory']}")

    vc = data.get("virt_ctrl", {})
    if vc.get("unhealthy"):
        lines.append(f"  virt-controller unhealthy: {vc['unhealthy']}")

    if data.get("virt_launcher_bad"):
        lines.append(f"  Bad virt-launchers: {data['virt_launcher_bad']}")

    etcd = data.get("etcd", {})
    if etcd.get("unhealthy"):
        lines.append(f"\n## ETCD (unhealthy members: {etcd['unhealthy']})")

    res = data.get("resources", {})
    if res.get("high_cpu") or res.get("high_memory"):
        lines.append("\n## Resource Pressure")
        for n in res.get("high_cpu", []):
            lines.append(f"  - {n}: HIGH CPU")
        for n in res.get("high_memory", []):
            lines.append(f"  - {n}: HIGH MEMORY")

    pvcs = data.get("pvcs", {})
    if pvcs.get("pending"):
        lines.append(f"\n## Storage ({len(pvcs['pending'])} pending PVCs)")
        for pvc in pvcs["pending"][:10]:
            lines.append(f"  - {pvc}")

    if data.get("csi_issues"):
        lines.append(f"  CSI issues: {data['csi_issues']}")
    if data.get("dv_issues"):
        lines.append(f"  DataVolume issues: {data['dv_issues']}")
    if data.get("snapshot_issues"):
        lines.append(f"  Snapshot issues: {data['snapshot_issues']}")

    mig = data.get("migrations", {})
    if mig.get("failed") or mig.get("failed_count", 0) > 0:
        lines.append(f"\n## Migrations (failed: {mig.get('failed_count', len(mig.get('failed', [])))})")
        for m in mig.get("failed", [])[:10]:
            lines.append(f"  - {m}")
    if data.get("stuck_migrations"):
        lines.append(f"  Stuck migrations: {data['stuck_migrations']}")
    if data.get("cordoned_vms"):
        lines.append(f"  VMs on cordoned nodes: {data['cordoned_vms']}")

    if data.get("oom_events"):
        lines.append(f"\n## OOM Events ({len(data['oom_events'])})")
        for ev in data["oom_events"][:10]:
            lines.append(f"  - {ev}")

    alerts = data.get("alerts", [])
    if alerts:
        lines.append(f"\n## Firing Alerts ({len(alerts)})")
        for a in alerts[:15]:
            if isinstance(a, dict):
                lines.append(f"  - {a.get('name', '?')} severity={a.get('severity', '?')} ns={a.get('namespace', '?')}")
            else:
                lines.append(f"  - {a}")

    if not data.get("hco_healthy", True):
        lines.append("\n## HCO Status: UNHEALTHY")

    return "\n".join(lines)


def _build_rule_analysis_summary(analysis):
    """Summarize the rule-based RCA results for inclusion in the Gemini prompt."""
    if not analysis:
        return ""

    lines = ["\n## Rule-Based Analysis Findings"]
    lines.append(f"The pattern engine matched {len(analysis)} issue(s):\n")

    for i, item in enumerate(analysis, 1):
        failure = item.get("failure", {})
        matched = item.get("matched_issue", {})
        cause = item.get("determined_cause")

        lines.append(f"### Issue {i}: {failure.get('name', 'Unknown')} ({failure.get('status', '')})")
        lines.append(f"  Type: {failure.get('type', '?')}")
        lines.append(f"  Matched pattern: {matched.get('title', 'No match')}")

        jira_refs = matched.get("jira", [])
        if jira_refs:
            bug_entries = _get_bug_context(jira_refs)
            if bug_entries:
                lines.append("  Related Jira bugs:")
                lines.extend(bug_entries)
            else:
                lines.append(f"  Related Jira bugs: {', '.join(jira_refs[:5])}")

        root_causes = matched.get("root_cause", [])
        if root_causes:
            lines.append(f"  Suspected root causes: {'; '.join(root_causes[:3])}")

        if cause:
            lines.append(f"  Investigation result: {cause}")

        suggestions = matched.get("suggestions", [])
        if suggestions:
            lines.append(f"  Suggested remediation: {suggestions[0]}")

        lines.append("")

    return "\n".join(lines)


def analyze_with_gemini(data, rule_analysis=None):
    """Send health data to Gemini and return AI-generated RCA markdown.

    Args:
        data: The health check data dict from collect_data().
        rule_analysis: Optional list of dicts from analyze_failures() / 
            run_deep_investigation(). When provided, Gemini uses these
            pattern-matched findings as a starting point.

    Returns None if the API key is missing, the SDK is unavailable, or the
    call fails for any reason (never breaks the pipeline).
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set - skipping AI analysis")
        return None

    try:
        from google import genai
    except ImportError:
        logger.warning("google-genai package not installed - skipping AI analysis")
        return None

    summary = _build_health_summary(data)
    rule_summary = _build_rule_analysis_summary(rule_analysis) if rule_analysis else ""

    if rule_summary:
        user_prompt = (
            "Analyze the following OpenShift cluster health data and provide a "
            "root cause analysis. A rule-based pattern engine has already produced "
            "initial findings (included below). Use those as a starting point: "
            "confirm or challenge them, identify cross-subsystem correlations the "
            "rules missed, and fill any gaps.\n\n"
            f"{summary}\n\n{rule_summary}"
        )
    else:
        user_prompt = (
            "Analyze the following OpenShift cluster health data and provide a "
            "root cause analysis. Correlate failures, identify the primary root "
            "cause, rank by severity, and give remediation steps.\n\n"
            f"{summary}"
        )

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_prompt,
            config=genai.types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.3,
                max_output_tokens=4096,
            ),
        )
        return response.text
    except Exception as e:
        logger.warning("Gemini API call failed: %s", e)
        return None


PATTERN_SUGGESTION_PROMPT = """\
Based on the health check data and your analysis above, suggest any NEW \
issue patterns that the rule-based knowledge base does not already cover.

Return ONLY a JSON array (no markdown fencing). Each element:
{
  "key": "short-kebab-key",
  "pattern": ["keyword1", "keyword2"],
  "title": "Human-readable title",
  "root_cause": ["Possible root cause"],
  "suggestions": ["Remediation step"]
}

If there are no new patterns to suggest, return an empty array: []
"""


def suggest_new_patterns(data, ai_rca_text, rule_analysis=None):
    """Ask Gemini to suggest new patterns based on its RCA.

    Parses the JSON response, deduplicates against existing patterns,
    and saves new ones to the knowledge base with source="gemini".
    Returns the list of newly added pattern keys, or [].
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or not ai_rca_text:
        return []

    try:
        from google import genai
    except ImportError:
        return []

    summary = _build_health_summary(data)
    rule_summary = _build_rule_analysis_summary(rule_analysis) if rule_analysis else ""

    context = (
        f"Health data:\n{summary}\n\n"
        f"Rule-based findings:\n{rule_summary}\n\n"
        f"Your AI RCA:\n{ai_rca_text}\n\n"
        f"{PATTERN_SUGGESTION_PROMPT}"
    )

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=context,
            config=genai.types.GenerateContentConfig(
                system_instruction="You are a pattern extraction assistant. Return ONLY valid JSON.",
                temperature=0.2,
                max_output_tokens=1024,
            ),
        )
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        suggestions = json.loads(raw)
    except Exception as exc:
        logger.warning("Gemini pattern suggestion failed: %s", exc)
        return []

    if not isinstance(suggestions, list):
        return []

    from healthchecks.knowledge_base import save_known_issue, pattern_exists
    from datetime import datetime as _dt

    added = []
    now = _dt.now().isoformat()
    for s in suggestions[:10]:
        if not isinstance(s, dict) or "key" not in s or "pattern" not in s:
            continue
        keywords = s.get("pattern", [])
        if pattern_exists(keywords):
            continue
        key = f"gemini-{s['key']}"
        entry = {
            "pattern": keywords,
            "jira": [],
            "title": s.get("title", key),
            "description": f"AI-suggested pattern from Gemini analysis",
            "root_cause": s.get("root_cause", []),
            "suggestions": s.get("suggestions", []),
            "verify_cmd": "",
            "source": "gemini",
            "confidence": 0.5,
            "created": now,
            "last_matched": None,
            "investigation_commands": [],
        }
        save_known_issue(key, entry)
        added.append(key)
        logger.info("Gemini suggested new pattern: %s", key)

    return added


RC_RULE_SUGGESTION_PROMPT = """\
Based on the investigation output and root cause analysis above, suggest any \
NEW root cause determination rules that are not already in the rule set.

A root cause rule maps keywords found in `oc` command output to a specific \
root cause diagnosis. Return ONLY a JSON array (no markdown fencing). Each element:
{
  "key": "issuetype-short-kebab-key",
  "issue_types": ["issue-type-this-applies-to"],
  "keywords_all": ["keyword1", "keyword2"],
  "keywords_any": ["keyword3", "keyword4"],
  "cause": "Root Cause Name",
  "confidence": "high or medium",
  "explanation": "What this means and suggested action"
}

keywords_all: ALL must appear in investigation output (AND logic).
keywords_any: at least ONE must appear (OR logic).
Use keywords_all for compound conditions (e.g. "disk" AND "pressure").
Use keywords_any for alternative signals (e.g. "timeout" OR "stuck").

If there are no new rules to suggest, return an empty array: []
"""


def suggest_root_cause_rules(data, ai_rca_text, rule_analysis=None):
    """Ask Gemini to suggest new root cause determination rules.

    Saves new rules to root_cause_rules.json with source="gemini".
    Returns list of newly added rule keys, or [].
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or not ai_rca_text:
        return []

    try:
        from google import genai
    except ImportError:
        return []

    summary = _build_health_summary(data)
    rule_summary = _build_rule_analysis_summary(rule_analysis) if rule_analysis else ""

    context = (
        f"Health data:\n{summary}\n\n"
        f"Rule-based findings:\n{rule_summary}\n\n"
        f"Your AI RCA:\n{ai_rca_text}\n\n"
        f"{RC_RULE_SUGGESTION_PROMPT}"
    )

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=context,
            config=genai.types.GenerateContentConfig(
                system_instruction="You are a root cause rule extraction assistant. Return ONLY valid JSON.",
                temperature=0.2,
                max_output_tokens=1024,
            ),
        )
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        suggestions = json.loads(raw)
    except Exception as exc:
        logger.warning("Gemini root cause rule suggestion failed: %s", exc)
        return []

    if not isinstance(suggestions, list):
        return []

    from healthchecks.knowledge_base import load_root_cause_rules, save_root_cause_rule
    from datetime import datetime as _dt

    existing = load_root_cause_rules()
    added = []
    now = _dt.now().isoformat()
    for s in suggestions[:10]:
        if not isinstance(s, dict) or "key" not in s:
            continue
        key = f"gemini-{s['key']}"
        if key in existing:
            continue
        existing_causes = {r.get("cause", "").lower() for r in existing.values()}
        if s.get("cause", "").lower() in existing_causes:
            continue
        entry = {
            "issue_types": s.get("issue_types", []),
            "keywords_all": s.get("keywords_all", []),
            "keywords_any": s.get("keywords_any", []),
            "cause": s.get("cause", key),
            "confidence": s.get("confidence", "medium"),
            "explanation": s.get("explanation", "AI-suggested root cause rule"),
            "source": "gemini",
            "created": now,
            "last_matched": None,
        }
        save_root_cause_rule(key, entry)
        added.append(key)
        logger.info("Gemini suggested new root cause rule: %s", key)

    return added


def _md_to_html(md_text):
    """Convert markdown to HTML with dark-theme styling.

    Handles fenced code blocks, headers, bold, inline code, bullet/numbered
    lists, and horizontal rules. Operates line-by-line with a state machine
    for code fences.
    """
    import re
    from html import escape

    lines = md_text.split("\n")
    out = []
    in_code_block = False

    for line in lines:
        stripped = line.strip()

        # Fenced code block toggle (handles indented fences too)
        if stripped.startswith("```"):
            if not in_code_block:
                in_code_block = True
                out.append(
                    '<pre style="background:#1a1a2e;border:1px solid #333;'
                    "border-radius:6px;padding:12px 16px;margin:10px 0;"
                    "overflow-x:auto;font-size:0.88em;color:#d4d4d4;"
                    'line-height:1.5">'
                )
                continue
            else:
                in_code_block = False
                out.append("</pre>")
                continue

        if in_code_block:
            out.append(escape(line))
            out.append("\n")
            continue

        # Horizontal rule
        if re.match(r"^-{3,}$", stripped):
            out.append('<hr style="border:none;border-top:1px solid #333;margin:16px 0">')
            continue

        # Empty line
        if not stripped:
            out.append("<br>")
            continue

        # Escape HTML entities in the line
        safe = escape(stripped)

        # Headers (process before inline formatting)
        m = re.match(r"^(#{1,5})\s+(.+)$", stripped)
        if m:
            level = len(m.group(1))
            title = escape(m.group(2))
            title = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", title)
            title = re.sub(
                r"`([^`]+)`",
                r'<code style="background:#2a2a3d;padding:2px 6px;'
                r'border-radius:3px;color:#FF9830">\1</code>',
                title,
            )
            colors = {1: "#5794F2", 2: "#5794F2", 3: "#73BF69", 4: "#FF9830", 5: "#FF9830"}
            sizes = {1: "1.4em", 2: "1.25em", 3: "1.1em", 4: "1.0em", 5: "0.95em"}
            tag = f"h{min(level + 1, 6)}"
            out.append(
                f'<{tag} style="color:{colors.get(level, "#ccc")};'
                f"font-size:{sizes.get(level, '1em')};"
                f'margin:20px 0 8px 0">{title}</{tag}>'
            )
            continue

        # Inline formatting: bold, inline code
        safe = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", safe)
        safe = re.sub(
            r"`([^`]+)`",
            r'<code style="background:#2a2a3d;padding:2px 6px;'
            r'border-radius:3px;color:#FF9830">\1</code>',
            safe,
        )

        # Bullet lists (* or -)
        m = re.match(r"^[\*\-]\s+(.+)$", stripped)
        if m:
            content = escape(m.group(1))
            content = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", content)
            content = re.sub(
                r"`([^`]+)`",
                r'<code style="background:#2a2a3d;padding:2px 6px;'
                r'border-radius:3px;color:#FF9830">\1</code>',
                content,
            )
            out.append(
                f'<div style="padding:3px 0 3px 20px">'
                f'<span style="color:#5794F2;margin-right:8px">&#x2022;</span>{content}</div>'
            )
            continue

        # Numbered lists
        m = re.match(r"^(\d+)\.\s+(.+)$", stripped)
        if m:
            num = m.group(1)
            content = escape(m.group(2))
            content = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", content)
            content = re.sub(
                r"`([^`]+)`",
                r'<code style="background:#2a2a3d;padding:2px 6px;'
                r'border-radius:3px;color:#FF9830">\1</code>',
                content,
            )
            out.append(
                f'<div style="padding:3px 0 3px 20px">'
                f'<span style="color:#73BF69;margin-right:8px;font-weight:bold">{num}.</span>{content}</div>'
            )
            continue

        # Indented sub-items (4+ spaces then * or - or digit)
        m = re.match(r"^\s{2,}[\*\-]\s+(.+)$", line)
        if m:
            content = escape(m.group(1))
            content = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", content)
            content = re.sub(
                r"`([^`]+)`",
                r'<code style="background:#2a2a3d;padding:2px 6px;'
                r'border-radius:3px;color:#FF9830">\1</code>',
                content,
            )
            out.append(
                f'<div style="padding:2px 0 2px 40px">'
                f'<span style="color:#888;margin-right:6px">&#x25E6;</span>{content}</div>'
            )
            continue

        m = re.match(r"^\s{2,}(\d+)\.\s+(.+)$", line)
        if m:
            num = m.group(1)
            content = escape(m.group(2))
            content = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", content)
            content = re.sub(
                r"`([^`]+)`",
                r'<code style="background:#2a2a3d;padding:2px 6px;'
                r'border-radius:3px;color:#FF9830">\1</code>',
                content,
            )
            out.append(
                f'<div style="padding:2px 0 2px 40px">'
                f'<span style="color:#888;margin-right:6px">{num}.</span>{content}</div>'
            )
            continue

        # Regular paragraph line
        out.append(f"<div>{safe}</div>")

    # Close unclosed code block
    if in_code_block:
        out.append("</pre>")

    return "\n".join(out)


SAFE_CMD_PREFIXES = (
    "oc get", "oc describe", "oc logs", "oc adm top", "oc adm node-logs",
    "oc status", "oc whoami", "oc version", "oc api-resources",
    "oc explain", "oc events", "oc exec",
    "kubectl get", "kubectl describe", "kubectl logs", "kubectl top",
    "ping ", "ping6 ",
    "ssh ",
    "cat ", "head ", "tail ", "ls ", "df ", "du ", "free ",
    "ps ", "top ", "uptime", "uname", "hostname",
    "dmesg", "journalctl", "systemctl status", "systemctl is-active",
    "systemctl list-units", "systemctl show",
    "crictl ps", "crictl images", "crictl stats", "crictl info",
    "ip addr", "ip route", "ip link", "ss -", "netstat -",
)

BLOCKED_PATTERNS = (
    "delete", "remove", "rm ", "rm -", "rmdir",
    "apply", "create", "patch", "replace", "edit",
    "scale", "rollout", "drain", "cordon", "uncordon", "taint",
    "reboot", "shutdown", "poweroff", "halt", "init ",
    "systemctl restart", "systemctl stop", "systemctl start",
    "systemctl enable", "systemctl disable",
    "kill", "pkill", "killall",
    "mv ", "cp ", "chmod", "chown", "chgrp",
    "curl -X POST", "curl -X PUT", "curl -X DELETE", "curl -X PATCH",
    "oc debug",
    "mkfs", "fdisk", "mount", "umount",
    "yum ", "dnf ", "rpm ", "pip ",
    "export ", "unset ",
    "--force", "--grace-period=0",
    "> /", ">> /", "tee ",
)


def is_safe_command(cmd):
    """Check if a command is read-only and safe to auto-execute."""
    cmd_stripped = cmd.strip().lstrip("$ ")
    cmd_lower = cmd_stripped.lower()

    for blocked in BLOCKED_PATTERNS:
        if blocked in cmd_lower:
            return False

    if cmd_lower.startswith(SAFE_CMD_PREFIXES):
        return True

    if cmd_lower.startswith("ssh "):
        inner = cmd_lower.split("'", 1)[-1] if "'" in cmd_lower else cmd_lower.split('"', 1)[-1]
        for blocked in BLOCKED_PATTERNS:
            if blocked in inner:
                return False
        return True

    return False


AI_INVESTIGATE_SYSTEM = """\
You are an expert OpenShift/Kubernetes SRE. You have SSH access to a bastion that runs `oc` commands
and can SSH to cluster nodes as: ssh core@<node-InternalIP> '<command>'

YOUR GOAL: Find the specific component, workload, or configuration that CAUSED the issue.
A root cause MUST name: the responsible pod/workload/namespace/component, WHAT it did wrong, and WHY.

is_final=true means: "An SRE can take DIRECT ACTION from this sentence alone, without asking 'but what caused that?'"
is_final=false means: "There is still a layer to uncover."

NOT FINAL (keep digging):
  "disk full" -> WHAT filled it? is_final=false
  "/var/lib/kubelet/pods consuming 425G" -> WHICH pods? is_final=false
  "ephemeral storage consumption by pod data" -> WHICH pods specifically? is_final=false
  "container images consuming 300G" -> WHICH images? How many? Why not GC'd? is_final=false
  "kubelet crash-looping" -> WHY is it crashing? is_final=false
  "OOMKilled" -> WHICH container and WHY is it exceeding its limit? is_final=false

FINAL (specific component identified):
  "virt-launcher pods in openshift-cnv namespace consuming 380G ephemeral storage across 45 pods on node X" -> is_final=true
  "847 cached container images (380G) not garbage-collected because imageGCHighThresholdPercent=85 but disk was at 82%" -> is_final=true
  "csi-addons-controller-manager OOMKilled at 512Mi limit while watching 2000+ PVC resources" -> is_final=true

DISK INVESTIGATION - BREADTH FIRST (critical for DiskPressure):
  IMPORTANT: When using du/ls with glob (*) in SSH, use: sudo sh -c "du -sh /path/* | sort -rh"
  DO NOT use: sudo du -sh /path/* (glob won't expand in single quotes through SSH chain)

  When /var is full, do NOT fixate on the first large consumer you find. Map ALL consumers first:
  Step 1: sudo du -sh /var/lib/containers /var/lib/kubelet /var/log /var/lib/etcd 2>/dev/null | sort -rh
  Step 2: For the LARGEST consumer, drill in. For ALL consumers over 10G, note them.
  Step 3: If /var/lib/kubelet is large -> sudo sh -c "du -sh /var/lib/kubelet/pods/* 2>/dev/null | sort -rh | head -10"
  Step 4: Identify each large pod's workload: sudo ls /var/lib/kubelet/pods/<uuid>/volumes/kubernetes.io~empty-dir/ 2>/dev/null
          Volume names reveal the workload (e.g. "prometheus-k8s-db" = Prometheus TSDB, "data" = app data)
          Cross-reference: oc get pods --field-selector spec.nodeName=<node> -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,UID:.metadata.uid
  Step 5: If /var/lib/containers is large -> sudo crictl images | wc -l; sudo sh -c "du -sh /var/lib/containers/storage/overlay/* 2>/dev/null | sort -rh | head -5"
  Step 6: If /var/log is large -> sudo sh -c "du -sh /var/log/pods/* 2>/dev/null | sort -rh | head -10"
  
  BAD: "journal consuming 4G" is_final=true  (what about the other 440G??)
  GOOD: "containers: 300G, kubelet/pods: 120G (top pod: virt-launcher-xyz 45G), logs: 4G" is_final=false -> drill into containers + pods

DIRECTORY DRILL-DOWN (critical - never stop at a large directory):
  Finding "443G /sysroot/ostree" is NOT an answer. Run: sudo du -sh /sysroot/ostree/* | sort -rh | head -10
  Finding "425G /sysroot/ostree/deploy" is NOT an answer. Keep drilling: sudo du -sh /sysroot/ostree/deploy/* | sort -rh
  Keep going until you find the specific component: pods, container images, logs, or specific files.
  Every large directory deserves a `du -sh <dir>/* | sort -rh | head -10` to find what's inside.

TRACING TO THE OWNER (critical - ALWAYS do this for kubelet/pods):
  GLOB IN SSH: Use sudo sh -c "du -sh /path/*" NOT sudo du -sh /path/* (glob won't expand in single quotes through SSH)
  Large kubelet/pods dir? -> sudo sh -c "du -sh /var/lib/kubelet/pods/* 2>/dev/null | sort -rh | head -10"
  Identify the pod: look inside the pod dir for volume names that reveal the workload:
    sudo ls /var/lib/kubelet/pods/<uuid>/volumes/kubernetes.io~empty-dir/ 2>/dev/null
    sudo ls /var/lib/kubelet/pods/<uuid>/volumes/kubernetes.io~configmap/ 2>/dev/null
    Volume names like "prometheus-k8s-db" -> Prometheus, "data" in configmap "alertmanager-config" -> Alertmanager
  Also check the pod's etc-hosts for hostname clues:
    sudo cat /var/lib/kubelet/pods/<uuid>/etc-hosts 2>/dev/null
  Cross-reference with running pods on the node:
    oc get pods --field-selector spec.nodeName=<node-name> -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,UID:.metadata.uid --no-headers | head -20
  Large containers dir? -> sudo crictl images | wc -l, sudo crictl ps -a | wc -l
  Logs filling disk? -> sudo sh -c "ls -lhS /var/log/pods/* 2>/dev/null | head -10"
  YOU MUST NAME THE PODS/WORKLOADS. "kubelet/pods consuming 120G" without naming which pods is NOT a root cause.

COMMAND FAILED? If `du` or `ls` returns "No such file or directory", the path might be different:
  Try with /sysroot prefix: /sysroot/ostree/deploy/rhcos/var/lib/... instead of /var/lib/...
  Try with sudo: permission errors often just need sudo
  Try without glob: `du -sh /dir/*` fails if empty, use `ls -la /dir/` instead

NODE SSH: Use InternalIP, never hostnames. To find a node's IP:
  oc get node <name> -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}'

KNOWN RULES: The context may include "Known Root Cause Rules" for this issue type. These are
hypotheses from the rule engine. Use them to:
- Skip already-proven symptoms (marked [SYMPTOM]) and go straight to the underlying cause.
- Use the "hint" commands as a starting point, but verify and drill deeper.
- If a rule says "DiskPressure" is a symptom, don't re-discover disk pressure - find WHAT filled the disk.

INVESTIGATION PLAYBOOK: The context may include a structured playbook with ordered stages.
Follow the stages in order: identify -> drill_down -> trace_owner -> verify_config.
Each stage builds on the previous one. Use the playbook commands as starting points, adapt
based on what you find. The playbook accelerates your investigation - don't ignore it.

EVIDENCE CHAIN (collect ALL before claiming is_final=true):
1. SYMPTOM: What is broken? (node NotReady, pod CrashLoop, operator degraded, etc.)
2. COMPONENT: Which specific pod/workload/service is the culprit?
3. RESOURCE STATE: What are the limits vs actual usage? (oc describe for limits + oc top for actual)
4. LOGS/EVENTS: What do the logs and events say? (oc logs, oc get events)
5. CONFIG: What configuration drives this behavior? (resource limits, GC settings, retention policy)
6. TIMELINE: When did it start? What changed? (events sorted by time)
If you haven't collected evidence for steps 2-5, you are NOT ready to claim is_final=true.

CAPACITY ANALYSIS (when resource exhaustion is involved):
- Always compare limits vs actual: oc describe pod X | grep Resources + oc adm top pod X
- Calculate rates: if N items use Xmi total, that's X/N mi per item
- Estimate headroom: actual_usage / limit * 100 = percent utilized
- Example: 468 concurrent PVC clones used 988Mi with a 1Gi limit -> ~2.1Mi per clone, 96% utilized

RULES:
- ONLY read-only commands. FORBIDDEN: delete, apply, patch, reboot, restart, rm, any writes.
- Max 5 commands per round. Single line each. Use real names from context.

EXCEPTION: If commands consistently fail with the same error (e.g., "Permission denied"), that IS the finding.

Return JSON:
{"commands":[{"cmd":"...","desc":"..."}],"root_cause":"string or null","confidence":"high/medium/low","is_final":false,"fix":"string or null","needs_manual":"string or null"}\
"""

AI_ANALYZE_SYSTEM = """\
You are an expert OpenShift/Kubernetes SRE analyzing diagnostic command output.

CRITICAL: is_final=true means "the root cause names the specific responsible component/workload/namespace."
If your conclusion says "pod data", "container images", "ephemeral storage" without naming WHICH pods/workloads,
that is NOT final. Set is_final=false and suggest commands to identify the specific owner.

KNOWN RULES: The context may include "Known Root Cause Rules". Rules marked [SYMPTOM] are already
known to be symptoms, not root causes. Skip past them and investigate what's underneath.
Use the "hint" commands as starting points but always verify and dig deeper.

INVESTIGATION PLAYBOOK: If a playbook is in the context, follow its stages. If you've completed
a stage, move to the next one. The playbook is a roadmap - don't skip stages.

EVIDENCE CHAIN: Before is_final=true, ensure you have:
1. The specific component/pod/workload responsible
2. Resource limits vs actual usage comparison
3. Log or event evidence confirming the cause
4. Configuration that drives the behavior
If any of these are missing, suggest commands to collect them.

CAPACITY ANALYSIS: When resources are involved, compare limits vs actual and calculate rates.
Example: "csi-rbdplugin at 988Mi/1Gi (96%) with 468 concurrent clones = ~2.1Mi/clone"

Disk investigation - breadth first:
- Do NOT fixate on the first large consumer. Map ALL /var consumers first, THEN drill into the largest.
- If kubelet/pods is large, you MUST identify the pod UUIDs and map them to names.
- "journal 4G" when disk is 440G full means journal is 1% of the problem - look elsewhere!

Tracing to the owner:
- Found a large directory? -> ALWAYS run sudo sh -c "du -sh <dir>/* | sort -rh | head -10" (use sudo sh -c for glob expansion!)
- Pod UUID in path? -> Identify by checking volume names inside the pod dir:
    sudo ls /var/lib/kubelet/pods/<uuid>/volumes/kubernetes.io~empty-dir/
    Volume names reveal the workload (e.g. "prometheus-k8s-db" = Prometheus)
  Or cross-reference: oc get pods --field-selector spec.nodeName=<node> -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,UID:.metadata.uid
- Large containers dir? -> sudo crictl images | wc -l, check image count/size
- Logs consuming space? -> sudo sh -c "ls -lhS /var/log/pods/* | head -10"
- Command got "No such file or directory"? -> try with /sysroot prefix or sudo sh -c for glob
- YOU MUST NAME THE PODS/WORKLOADS. Generic "pod data" or "ephemeral storage" is NOT a root cause.

Set is_final=false + suggest commands when there's another layer to uncover.

EXCEPTION: If commands consistently fail with the same error, that IS the finding. Set is_final=true.

Max 4 follow-up commands. Keep desc under 15 words.

Return JSON:
{"root_cause":"string or null","confidence":"high/medium/low","is_final":false,"fix":"string or null","needs_more_commands":[{"cmd":"...","desc":"..."}],"needs_manual":"string or null"}\
"""


INVESTIGATE_MODEL = os.getenv("GEMINI_INVESTIGATE_MODEL", "gemini-2.5-pro")


def _try_repair_json(raw):
    """Attempt to repair truncated JSON from the model (e.g. when max_tokens cuts off mid-response)."""
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    open_braces = s.count("{") - s.count("}")
    open_brackets = s.count("[") - s.count("]")
    if open_braces > 0 or open_brackets > 0:
        in_string = False
        escape = False
        for i, c in enumerate(s):
            if escape:
                escape = False
                continue
            if c == '\\':
                escape = True
                continue
            if c == '"':
                in_string = not in_string
        if in_string:
            s += '"'
        s += "]" * max(0, open_brackets) + "}" * max(0, open_braces)
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass
    try:
        last_brace = s.rfind("}")
        if last_brace > 0:
            return json.loads(s[:last_brace + 1])
    except json.JSONDecodeError:
        pass
    return None


def _call_gemini_json(system_prompt, user_prompt, max_tokens=4096, timeout_sec=90):
    """Call Gemini with JSON response mode. Times out after timeout_sec."""
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return None

    try:
        from google import genai
    except ImportError:
        return None

    import concurrent.futures

    def _do_call():
        client = genai.Client(api_key=api_key)
        config_kwargs = {
            "response_mime_type": "application/json",
            "temperature": 0.2,
            "max_output_tokens": max_tokens,
        }
        return client.models.generate_content(
            model=INVESTIGATE_MODEL,
            contents=system_prompt + "\n\n" + user_prompt,
            config=genai.types.GenerateContentConfig(**config_kwargs),
        )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_do_call)
            response = future.result(timeout=timeout_sec)

        raw = getattr(response, "text", None)
        if not raw:
            candidates = getattr(response, "candidates", None)
            if candidates and len(candidates) > 0:
                parts = getattr(candidates[0], "content", None)
                if parts and getattr(parts, "parts", None):
                    for part in parts.parts:
                        t = getattr(part, "text", None)
                        if t and t.strip():
                            raw = t
                            break
            if not raw:
                logger.warning("Gemini returned empty response")
                return None
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except concurrent.futures.TimeoutError:
        logger.warning("Gemini call timed out after %ds", timeout_sec)
        return None
    except json.JSONDecodeError:
        if raw:
            repaired = _try_repair_json(raw)
            if repaired:
                return repaired
        logger.warning("Gemini JSON parse failed: %.300s", raw[:300] if raw else "empty")
        return None
    except Exception as exc:
        logger.warning("Gemini call failed: %s", exc)
        return None


def _get_bug_context(jira_refs):
    """Look up enriched bug descriptions from known_bugs.json for the given Jira keys."""
    if not jira_refs:
        return []
    try:
        from healthchecks.knowledge_base import load_known_bugs
        bugs = load_known_bugs()
    except Exception:
        return []
    entries = []
    for key in jira_refs[:8]:
        bug = bugs.get(key)
        if not bug or not bug.get("summary"):
            continue
        status = bug.get("status", "?")
        fix = bug.get("fix_versions", [])
        fix_str = f" (fix: {', '.join(fix)})" if fix else ""
        snippet = bug.get("description_snippet", "")
        comp = bug.get("components", [])
        comp_str = f" [{', '.join(comp)}]" if comp else ""
        entries.append(
            f"  {key}{comp_str} ({status}{fix_str}): {bug['summary']}"
            + (f"\n    Detail: {snippet}" if snippet else "")
        )
    return entries


def _get_relevant_rules(failure_type):
    """Load root_cause_rules.json and return rules matching the failure type."""
    try:
        from healthchecks.knowledge_base import load_root_cause_rules
        all_rules = load_root_cause_rules()
    except Exception:
        return []
    relevant = []
    for key, rule in all_rules.items():
        issue_types = rule.get("issue_types", [])
        if failure_type in issue_types or not issue_types:
            relevant.append({
                "key": key,
                "cause": rule.get("cause", ""),
                "is_symptom": rule.get("is_symptom", False),
                "explanation": rule.get("explanation", ""),
                "next_steps": rule.get("next_steps", []),
                "investigation_playbook": rule.get("investigation_playbook", []),
            })
    return relevant


def _build_investigation_context(issue_title, issue_desc, failure, investigation_results,
                                  drilldown_results=None, drilldown_conclusion=None,
                                  previous_followup=None,
                                  matched_inv_commands=None,
                                  jira_refs=None):
    """Build a concise context string for the AI investigation prompt."""
    lines = []
    lines.append(f"Issue: {issue_title}")
    lines.append(f"Description: {issue_desc}")

    if _node_ip_cache:
        lines.append("\nNode name -> IP mapping (use IPs for SSH, never hostnames):")
        for name, ip in _node_ip_cache.items():
            lines.append(f"  {name} = {ip}")

    f_type = failure.get("type", "")
    f_name = failure.get("name", "")
    f_status = failure.get("status", "")
    details = failure.get("details", {})
    lines.append(f"Failure: type={f_type} name={f_name} status={f_status}")
    if details:
        if isinstance(details, dict):
            lines.append(f"Details: {json.dumps(details, default=str)[:500]}")
        elif isinstance(details, list):
            lines.append(f"Details ({len(details)} items): {json.dumps(details[:3], default=str)[:500]}")

    rules = _get_relevant_rules(f_type)
    if rules:
        lines.append("\n--- Known Root Cause Rules for this issue type ---")
        lines.append("These are patterns the system already recognizes. Use them as starting")
        lines.append("hypotheses, but dig DEEPER than the rule's conclusion:")
        for r in rules:
            sym = " [SYMPTOM - dig deeper]" if r["is_symptom"] else ""
            lines.append(f"  - {r['cause']}{sym}: {r['explanation']}")
            for step in r.get("next_steps", [])[:3]:
                lines.append(f"    hint: {step}")

        playbooks = [r for r in rules if r.get("investigation_playbook")]
        if playbooks:
            pb = playbooks[0]
            lines.append(f"\n--- Investigation Playbook: {pb['cause']} ---")
            lines.append("Follow these stages in order. Each stage builds on the previous one.")
            lines.append("Use the commands as starting points; adapt based on what you find:")
            for stage in pb["investigation_playbook"]:
                lines.append(f"  Stage '{stage['stage']}': {stage['goal']}")
                for cmd in stage.get("commands", [])[:3]:
                    lines.append(f"    $ {cmd}")

    bug_entries = _get_bug_context(jira_refs)
    if bug_entries:
        lines.append("\n--- Related Known Jira Bugs ---")
        lines.append("These are real Jira bugs filed for similar symptoms. Compare the")
        lines.append("descriptions against what you observe. If symptoms match, reference")
        lines.append("the bug in your conclusion:")
        lines.extend(bug_entries)

    if matched_inv_commands:
        lines.append("\n--- Pattern-Matched Investigation Commands ---")
        lines.append("These commands were identified for this specific issue pattern:")
        for ic in matched_inv_commands[:8]:
            lines.append(f"  $ {ic.get('cmd', '')}  # {ic.get('desc', '')}")

    if investigation_results:
        lines.append("\n--- Investigation Commands Output ---")
        for r in investigation_results[:6]:
            out = r.get("output", "")[:600]
            if out.strip() in ("(no output)", "(error: )", ""):
                continue
            lines.append(f"[{r.get('description', '')}]")
            lines.append(f"$ {r.get('command', '')}")
            lines.append(out)

    if drilldown_results:
        lines.append("\n--- Drill-Down Commands Output ---")
        for r in drilldown_results[:6]:
            out = r.get("output", "")[:600]
            if out.strip() in ("(no output)", "(error: )", ""):
                continue
            lines.append(f"[{r.get('description', '')}]")
            lines.append(f"$ {r.get('command', '')}")
            lines.append(out)

    if drilldown_conclusion:
        lines.append(f"\nDrill-down conclusion: {drilldown_conclusion.get('conclusion', '')}")
        lines.append(f"Confidence: {drilldown_conclusion.get('confidence', '')}")
        if drilldown_conclusion.get("fix"):
            lines.append(f"Suggested fix: {drilldown_conclusion['fix']}")

    if previous_followup:
        lines.append("\n--- Previous AI Investigation Output ---")
        for r in previous_followup[:10]:
            out = r.get("output", "")[:800]
            if out.strip() in ("(no output)", "(error: )", ""):
                continue
            lines.append(f"[{r.get('description', '')}]")
            lines.append(f"$ {r.get('command', '')}")
            lines.append(out)

    return "\n".join(lines)


_node_ip_cache = {}


def _resolve_node_name_to_ip(hostname, ssh_command_func):
    """Resolve an OCP node name to its InternalIP, with caching."""
    if hostname in _node_ip_cache:
        return _node_ip_cache[hostname]
    try:
        out = ssh_command_func(
            f"oc get node {hostname} -o jsonpath='{{.status.addresses[?(@.type==\"InternalIP\")].address}}'",
            timeout=10,
        )
        ip = (out or "").strip().strip("'")
        if re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
            _node_ip_cache[hostname] = ip
            return ip
    except Exception:
        pass
    return None


def _fix_unbounded_commands(cmd, ssh_command_func=None):
    """Add bounds/timeouts to commands that could run forever or hang.
    Resolves node hostnames to IPs in SSH commands when ssh_command_func is provided."""
    stripped = cmd.strip()

    if stripped.startswith("ping ") and " -c " not in stripped:
        target = stripped.split()[-1]
        return f"ping -c 3 -W 3 {target}"
    if stripped.startswith("ping6 ") and " -c " not in stripped:
        target = stripped.split()[-1]
        return f"ping6 -c 3 -W 3 {target}"

    if stripped.startswith("ssh ") and "-o ConnectTimeout" not in stripped:
        stripped = stripped.replace("ssh ", "ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=no ", 1)

    if "ssh " in stripped:
        node_ips = set(_node_ip_cache.values())
        if node_ips and "@" not in stripped.split("'")[0] and "@" not in stripped.split('"')[0]:
            for ip in node_ips:
                if ip in stripped:
                    stripped = stripped.replace(ip, f"core@{ip}", 1)
                    break

        if ssh_command_func and "core@" in stripped:
            host_match = re.search(r'core@([a-zA-Z][a-zA-Z0-9._-]+)', stripped)
            if host_match:
                hostname = host_match.group(1)
                if not re.match(r'^\d+\.\d+\.\d+\.\d+$', hostname):
                    ip = _resolve_node_name_to_ip(hostname, ssh_command_func)
                    if ip:
                        stripped = stripped.replace(f"core@{hostname}", f"core@{ip}")

    return stripped


def _shell_quote(s):
    """Quote a string for safe use as a single shell argument."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _ssh_with_stderr(ssh_command_func, cmd, timeout=15, max_retries=2):
    """Wrapper that captures stderr too, so SSH errors are visible to the AI.
    Appends 2>&1 to the command and wraps with timeout to prevent hanging.
    Retries transient SSH failures with exponential backoff."""
    import time as _time

    cmd = _fix_unbounded_commands(cmd, ssh_command_func=ssh_command_func)
    merged_cmd = f"timeout {timeout} sh -c {_shell_quote(cmd + ' 2>&1')}"
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            output = ssh_command_func(merged_cmd, timeout=timeout + 5)
            if output and output.strip():
                return output.strip()
            return "(no output)"
        except Exception as e:
            last_error = e
            err_str = str(e).lower()
            is_transient = any(k in err_str for k in (
                "timeout", "timed out", "connection reset",
                "broken pipe", "connection refused", "no route",
            ))
            if is_transient and attempt < max_retries:
                delay = 2 * (2 ** attempt)
                _time.sleep(delay)
                continue
            return f"(error: {str(e)[:300]})"
    return f"(error: {str(last_error)[:300]})" if last_error else "(no output)"


def _is_vague_disk_conclusion(conclusion):
    """Return True if the conclusion mentions disk/full/pressure but doesn't name specific pods or workloads."""
    cl = conclusion.lower()
    disk_keywords = ("disk", "full", "pressure", "/var", "filesystem", "partition", "100%", "99%", "98%")
    if not any(kw in cl for kw in disk_keywords):
        return False
    specific_markers = (
        "virt-launcher", "virt-handler", "prometheus", "alertmanager",
        "csi-", "noobaa", "odf-", "ceph", "etcd", "elasticsearch",
        "fluentd", "kibana", "registry", "image-registry",
        "openshift-", "namespace", " ns:", " ns ",
        "crictl", "images not garbage", "imageGC",
    )
    if any(m in cl for m in specific_markers):
        return False
    if re.search(r'pod[s]?\s+\S+', cl) and "kubelet/pods" not in cl:
        return False
    return True


def _suggest_disk_drilldown_commands(all_results):
    """Generate follow-up commands when the AI's disk conclusion is too vague."""
    all_output = " ".join(r.get("output", "") for r in all_results)
    cmds = []
    if "kubelet/pods" in all_output.lower() or "kubelet" in all_output.lower():
        uuids = re.findall(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', all_output)
        seen = set()
        for uid in uuids:
            if uid in seen:
                continue
            seen.add(uid)
            cmds.append({"cmd": f"ssh core@<node-ip> 'sudo ls /var/lib/kubelet/pods/{uid}/volumes/kubernetes.io~empty-dir/ 2>/dev/null; sudo ls /var/lib/kubelet/pods/{uid}/volumes/kubernetes.io~configmap/ 2>/dev/null'",
                         "desc": f"Identify workload for pod UUID {uid[:8]}... via volume names"})
            if len(cmds) >= 3:
                break
    if not cmds:
        ips = set(_node_ip_cache.values())
        if ips:
            ip = next(iter(ips))
            cmds.append({"cmd": f"ssh core@{ip} 'sudo sh -c \"du -sh /var/lib/kubelet/pods/* 2>/dev/null | sort -rh | head -5\"'",
                          "desc": "Find largest pod directories under kubelet"})
    return cmds


def ai_investigate(issue_title, issue_desc, failure, investigation_results,
                   drilldown_results, drilldown_conclusion, ssh_command_func,
                   max_rounds=5, matched_inv_commands=None, jira_refs=None):
    """AI-driven recursive investigation loop. The AI self-evaluates via is_final
    and keeps digging until the root cause identifies the responsible component,
    or max_rounds is exhausted.

    Returns (all_followup_results, final_conclusion_dict_or_none).
    """
    if not _node_ip_cache:
        try:
            wide = ssh_command_func("oc get nodes -o wide --no-headers 2>/dev/null", timeout=10)
            for line in (wide or "").strip().splitlines():
                parts = line.split()
                if len(parts) >= 6 and re.match(r'\d+\.\d+\.\d+\.\d+', parts[5]):
                    _node_ip_cache[parts[0]] = parts[5]
        except Exception:
            pass

    all_results = []
    previous_followup = None
    min_rounds = 3

    for round_num in range(max_rounds):
        print(f"              AI round {round_num+1}/{max_rounds}...", flush=True)
        context = _build_investigation_context(
            issue_title, issue_desc, failure, investigation_results,
            drilldown_results, drilldown_conclusion,
            previous_followup=previous_followup,
            matched_inv_commands=matched_inv_commands,
            jira_refs=jira_refs,
        )

        if round_num == 0:
            prompt = (
                "Investigate the issue below. Suggest diagnostic commands to find the root cause. "
                "Do NOT claim is_final=true yet - gather evidence first.\n\n" + context
            )
            ai_response = _call_gemini_json(AI_INVESTIGATE_SYSTEM, prompt)
        else:
            depth_hint = ""
            if round_num < min_rounds - 1:
                depth_hint = (
                    "You MUST suggest more commands - it is too early to claim is_final=true. "
                    "If you found a large directory or resource hog, trace it to the specific "
                    "pod/workload/namespace responsible. "
                    "For disk issues: if you identified ONE consumer (e.g. journal 4G) but the disk "
                    "is much fuller (e.g. 440G), you've only found a small piece - check OTHER "
                    "directories too. Map ALL major consumers before concluding. "
                    "For kubelet/pods: you MUST identify the workload by checking volume names "
                    "inside the pod UUID dir (ls /var/lib/kubelet/pods/<uuid>/volumes/kubernetes.io~empty-dir/) "
                    "and cross-referencing with oc get pods on the node. "
                )
            prompt = (
                "Round %d. Analyze the new command output. %s"
                "Identify the specific component/workload responsible.\n\n"
                % (round_num + 1, depth_hint) + context
            )
            ai_response = _call_gemini_json(AI_ANALYZE_SYSTEM, prompt)

        if not ai_response:
            print("              AI: no response, stopping", flush=True)
            break

        rc = ai_response.get("root_cause")
        conf = ai_response.get("confidence", "low")
        is_final = ai_response.get("is_final", False)
        commands = ai_response.get("commands") or ai_response.get("needs_more_commands") or []

        if rc:
            label = "FINAL" if is_final else "INTERIM"
            print(f"              AI says ({conf}, {label}): {rc[:80]}", flush=True)

        if rc and is_final and conf in ("high", "medium") and round_num >= min_rounds - 1:
            if _is_vague_disk_conclusion(rc) and round_num < max_rounds - 1:
                print(f"              Depth check: DIG DEEPER - disk conclusion lacks specific pod/workload names", flush=True)
                is_final = False
                commands = commands or _suggest_disk_drilldown_commands(all_results)
            else:
                return all_results, {
                    "conclusion": rc,
                    "confidence": conf,
                    "fix": ai_response.get("fix", ""),
                    "needs_manual": ai_response.get("needs_manual", ""),
                }
        elif rc and is_final and round_num < min_rounds - 1:
            print(f"              Overriding is_final (round {round_num+1} < {min_rounds}), digging deeper", flush=True)

        if not commands:
            if rc:
                return all_results, {
                    "conclusion": rc,
                    "confidence": conf,
                    "fix": ai_response.get("fix", ""),
                    "needs_manual": ai_response.get("needs_manual", ""),
                }
            break

        round_results = []
        executed = 0
        print(f"              Running {len(commands[:5])} commands...", flush=True)
        for cmd_info in commands[:5]:
            cmd = cmd_info.get("cmd", "")
            if not cmd:
                continue
            cmd = _fix_unbounded_commands(cmd, ssh_command_func=ssh_command_func)
            if not is_safe_command(cmd):
                logger.info("Skipping unsafe AI command: %s", cmd[:80])
                continue
            desc = cmd_info.get("desc", "diagnostic")[:50]
            print(f"              $ {cmd[:70]} ({desc})", flush=True)
            output = _ssh_with_stderr(ssh_command_func, cmd, timeout=20)
            output = output[:4000]
            round_results.append({
                "description": cmd_info.get("desc", "AI-suggested diagnostic"),
                "command": cmd,
                "output": output,
            })
            executed += 1

        all_results.extend(round_results)
        previous_followup = all_results

        if executed == 0:
            break

    if all_results:
        context = _build_investigation_context(
            issue_title, issue_desc, failure, investigation_results,
            drilldown_results, drilldown_conclusion,
            previous_followup=all_results,
            jira_refs=jira_refs,
        )
        final_prompt = (
            "Based on ALL diagnostic data collected, provide your FINAL root cause. "
            "You MUST identify the specific component/workload/pod responsible. "
            "Include concrete evidence (paths, sizes, log lines, pod names, namespaces). "
            "If symptoms match a known Jira bug, reference it. "
            "Set is_final=true.\n\n" + context
        )
        final = _call_gemini_json(AI_ANALYZE_SYSTEM, final_prompt, max_tokens=4096)
        if final and final.get("root_cause"):
            return all_results, {
                "conclusion": final["root_cause"],
                "confidence": final.get("confidence", "medium"),
                "fix": final.get("fix", ""),
                "needs_manual": final.get("needs_manual", ""),
            }

    return all_results, None


def generate_ai_rca_html(ai_markdown):
    """Convert AI-generated markdown RCA into an HTML section for the report."""
    if not ai_markdown:
        return ""

    try:
        html_content = _md_to_html(ai_markdown)
    except Exception:
        from html import escape
        html_content = f"<pre>{escape(ai_markdown)}</pre>"

    return f"""
    <div class="dash-section" style="margin-top:24px;border:1px solid #5794F2;border-radius:8px;">
        <div class="dash-section-title" style="background:linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);border-bottom:2px solid #5794F2;">
            <span style="font-size:1.3em;">🤖 AI Root Cause Analysis</span>
            <span style="font-size:0.75em;color:#888;margin-left:12px;">Powered by Gemini</span>
        </div>
        <div style="padding:20px;background:#111217;font-size:0.95em;line-height:1.6;">
            <div style="background:#1a1a2e;border-left:3px solid #FF9830;padding:10px 14px;margin-bottom:16px;border-radius:4px;font-size:0.85em;color:#888;">
                This analysis was generated by AI and should be verified by an engineer.
            </div>
            {html_content}
        </div>
    </div>
    """
