"""
AI-powered Root Cause Analysis using Google Gemini.

Analyzes health check data collected from the OCP cluster and produces
a structured RCA with correlated failures, root causes, and remediation steps.
"""
import json
import logging
import os
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
