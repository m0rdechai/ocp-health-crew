<p align="center">
  <img src="https://img.shields.io/badge/OpenShift-EE0000?style=for-the-badge&logo=redhatopenshift&logoColor=white" alt="OpenShift"/>
  <img src="https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python"/>
  <img src="https://img.shields.io/badge/Flask-000000?style=for-the-badge&logo=flask&logoColor=white" alt="Flask"/>
  <img src="https://img.shields.io/badge/KubeVirt-326CE5?style=for-the-badge&logo=kubernetes&logoColor=white" alt="KubeVirt"/>
  <img src="https://img.shields.io/badge/AI_Powered-FF6F00?style=for-the-badge&logo=tensorflow&logoColor=white" alt="AI"/>
</p>

<h1 align="center">CNV HealthCrew AI</h1>

<p align="center">
  <strong>AI-Powered Performance Engineering & Health Monitoring for OpenShift + CNV</strong>
</p>

<p align="center">
  <em>Health Monitoring & Root Cause Analysis for OpenShift + CNV</em>
</p>

<p align="center">
  <a href="#-key-innovations">Key Innovations</a> &bull;
  <a href="#-features">Features</a> &bull;
  <a href="#-architecture">Architecture</a> &bull;
  <a href="#-project-structure">Project Structure</a> &bull;
  <a href="#-quick-start">Quick Start</a>
</p>

---

## Key Innovations

<table>
<tr>
<td align="left" width="50%">

### Intelligent RCA

**3-level root cause analysis that digs until the specific component is identified:**

- **Level 1** - Pattern matching + rule-based investigation commands
- **Level 2** - Symptom drill-down (deeper commands when L1 finds a symptom)
- **Level 3** - AI investigation loop (Gemini suggests/executes/analyzes commands iteratively)
- **Jira Context** - Enriched bug descriptions fed to AI during investigation (29 bugs with full summaries, components, fix versions)
- **Parallel** - All issue types investigated concurrently (ThreadPoolExecutor)

</td>
<td align="left" width="50%">

### Performance Engineering

**Built for Performance Engineers:**

- **Resource profiling** - CPU, Memory, I/O per node
- **Bottleneck detection** - Find hotspots instantly
- **Trend analysis** - Track performance over time
- **Threshold alerts** - Proactive warnings at 85%+
- **Root cause analysis** - AI-powered deep investigation

</td>
</tr>
</table>

---

## How the AI Evolves

<table>
<tr>
<td align="center" colspan="5">
<h3>CONTINUOUS LEARNING CYCLE</h3>
<sub>The system automatically improves with every run</sub>
</td>
</tr>
<tr>
<td align="center" width="20%">
<h3>1</h3>
<strong>Gather Intel</strong><br>
<sub>Jira bugs<br>Emails<br>Web docs</sub>
</td>
<td align="center" width="20%">
<h3>2</h3>
<strong>Analyze</strong><br>
<sub>AI identifies patterns<br>& recurring issues</sub>
</td>
<td align="center" width="20%">
<h3>3</h3>
<strong>Suggest Tests</strong><br>
<sub>Proposes new health<br>checks to add</sub>
</td>
<td align="center" width="20%">
<h3>4</h3>
<strong>Auto-Add</strong><br>
<sub>Approved tests join<br>the suite</sub>
</td>
<td align="center" width="20%">
<h3>5</h3>
<strong>Evolve</strong><br>
<sub>Knowledge grows<br>continuously</sub>
</td>
</tr>
</table>

### Real Example of AI Evolution

```
Jira Bug: CNV-75962 "kubevirt-migration-controller OOMKilled at scale"

AI Analysis:
   ├─ Pattern detected: "OOMKilled" + "migration" + "scale"
   ├─ Component: kubevirt-migration-controller
   └─ Priority: Critical

AI Suggestion:
   "Add new health check: migration_controller_memory"
   - Monitor memory usage of migration controller pods
   - Alert when approaching limits
   - Track during large-scale migrations

Result: New test automatically added to suite!
```

---

## Features

<table>
<tr>
<td width="33%">

### Health Monitoring
- Node & Operator status
- Pod health detection
- KubeVirt/CNV components
- VM migrations & status
- Storage health (ODF/CSI)
- etcd cluster health
- Certificate expiration

</td>
<td width="33%">

### Performance Engineering
- CPU utilization per node
- Memory pressure detection
- I/O bottleneck analysis
- Network throughput monitoring
- Resource quota tracking
- Capacity planning insights
- Historical trend comparison

</td>
<td width="33%">

### AI Capabilities
- **AI Investigation Loop** - Gemini 2.5 Flash iteratively runs diagnostic commands (3-5 rounds)
- **AI Summary** - Gemini 2.5 Pro correlates findings across subsystems
- **Component Tracing** - AI traces resource hogs to specific pods/workloads/namespaces
- **Enriched Jira Context** - AI receives full bug descriptions, symptoms, and fix versions during investigation
- Self-evolving test suite
- Jira bug correlation
- Pattern recognition
- Knowledge base learning

</td>
</tr>
</table>

### CNV Scenario Testing

Built-in support for **kube-burner** workload scenarios via the `cnv-scenarios` repository:

| Category | Scenarios |
|:---------|:----------|
| **Scale Testing** | per-host-density, virt-capacity-benchmark |
| **Resource Limits** | cpu-limits, memory-limits, disk-limits |
| **Performance** | high-memory, large-disk, minimal-resources |
| **Hot Plug** | disk-hotplug, nic-hotplug |

Each scenario runs in **sanity** (quick validation) or **full** (production-scale) mode with configurable variables (storage class, volume mode, pause intervals, VM counts).

### Custom Health Checks

Users can define their own health checks with:
- **Command mode** - Single shell commands run via SSH
- **Script mode** - Multi-line scripts uploaded and executed on the target host
- Match types: `contains`, `not_contains`, `regex`, `exit_code`, `numeric_gt/lt`
- Linked to specific scenarios or the global health check

---

## Architecture

<table>
<tr>
<td align="center" colspan="4">
<h3>WEB DASHBOARD (Flask :5000)</h3>
<sub>Role-based access: Admin | Operator | Viewer</sub>
</td>
</tr>
<tr>
<td align="center" width="25%"><strong>Dashboard</strong><br><sub>Stats, quick actions,<br>live build status</sub></td>
<td align="center" width="25%"><strong>Configure</strong><br><sub>Health checks, CNV<br>scenarios, presets</sub></td>
<td align="center" width="25%"><strong>History</strong><br><sub>Past builds, filtering,<br>reports</sub></td>
<td align="center" width="25%"><strong>Admin</strong><br><sub>Users, roles,<br>audit log</sub></td>
</tr>
<tr><td align="center" colspan="4">&darr;</td></tr>
<tr>
<td align="center" colspan="2">
<h3>Health Check Engine</h3>
<sub>hybrid_health_check.py</sub><br>
<sub>15 check categories &bull; HTML reports &bull; email</sub>
</td>
<td align="center" colspan="2">
<h3>CNV Scenario Engine</h3>
<sub>cnv_scenarios.py + cnv_report.py</sub><br>
<sub>kube-burner workloads &bull; sanity/full modes</sub>
</td>
</tr>
<tr><td align="center" colspan="4">&darr;</td></tr>
<tr>
<td align="center" colspan="4">
<h3>AI / RCA Layer (3-level pipeline)</h3>
<sub>L1: Pattern matching &bull; L2: Symptom drill-down (SSH to nodes) &bull; L3: AI investigation loop (Gemini Pro) &bull; Parallel execution</sub>
</td>
</tr>
<tr><td align="center" colspan="4">&darr;</td></tr>
<tr>
<td align="center" colspan="4">
<h3>SSH Layer (Paramiko)</h3>
<sub>Persistent connection &bull; Auto KUBECONFIG &bull; Auto oc-login on expired auth &bull; Connection validation</sub>
</td>
</tr>
<tr><td align="center" colspan="4">&darr;</td></tr>
<tr>
<td align="center" colspan="4">
<h3>OpenShift Cluster</h3>
</td>
</tr>
<tr>
<td align="center">Nodes</td>
<td align="center">Pods &amp; Operators</td>
<td align="center">VMs &amp; Migrations</td>
<td align="center">Storage &amp; Network</td>
</tr>
</table>

### Connection Flow

```
Dashboard → SSH to jump host → oc/kubectl commands → Cluster API
                │
                ├─ Validates SSH key, host, user
                ├─ Validates oc CLI is available
                ├─ Validates oc whoami (auth check)
                │     └─ Auto-login with kubeadmin if auth expired
                └─ On failure: generates error report with diagnostics
```

### Build Execution

```
Configure → Start Build → Background Thread
                              │
                ┌─────────────┼──────────────┐
                │             │              │
          Health Check   CNV Scenarios   Combined
                │             │              │
          SSH + oc cmds   SSH + kube-burner  Both sequential
                │             │              │
          HTML Report    Scenario Report   Combined Report
                │             │              │
                └─────────────┼──────────────┘
                              │
                    Custom Checks (if any)
                              │
                    Save to DB + Email
```

---

## Project Structure

```
ocp-health-crew/
├── run.py                              # Entry point — starts the Flask web server
├── config.env.example                  # Example configuration file
├── requirements.txt                    # Python dependencies
│
├── app/                                # Flask web application
│   ├── __init__.py                     #   App factory, blueprints, extensions
│   ├── models.py                       #   DB models: User, Build, Schedule, Host, CustomCheck, AuditLog
│   ├── routes.py                       #   Dashboard routes, build execution, APIs
│   ├── auth.py                         #   Authentication: login, register, profile
│   ├── admin.py                        #   Admin panel: user CRUD, roles, audit log, knowledge base
│   ├── scheduler.py                    #   Background scheduler for timed builds
│   ├── learning.py                     #   Pattern recognition & recurring issue tracking
│   ├── checks/                         #   Health check metadata (re-exports AVAILABLE_CHECKS)
│   ├── integrations/                   #   Integration stubs (Jira, email, SSH — future modules)
│   ├── templates/                      #   Jinja2 HTML templates
│   │   ├── base.html                   #     Base layout with sidebar
│   │   ├── dashboard.html              #     Main dashboard with stats & quick actions
│   │   ├── configure.html              #     Build configuration form
│   │   ├── build_detail.html           #     Build detail with live console & duration
│   │   ├── console.html                #     Real-time build output streaming
│   │   ├── history.html                #     Build history with filtering
│   │   ├── settings.html               #     Host management & app settings
│   │   ├── schedules.html              #     Scheduled builds management
│   │   ├── login.html / register.html  #     Authentication pages
│   │   ├── admin_users.html            #     User management (admin)
│   │   ├── admin_audit.html            #     Audit log (admin)
│   │   ├── admin_knowledge.html        #     Knowledge base CRUD (admin)
│   │   └── help.html                   #     Help & documentation
│   └── static/
│       ├── css/style.css               #   Dashboard styles
│       └── img/                        #   Red Hat logos
│
├── config/                             # Configuration
│   ├── __init__.py                     #   Re-exports Config
│   ├── settings.py                     #   App config: paths, DB, SSH, checks, Flask settings
│   └── cnv_scenarios.py                #   CNV scenario definitions & variables for the dashboard
│
├── knowledge/                          # Dynamic knowledge base (JSON)
│   ├── known_issues.json               #   Pattern definitions (built-in, user, learned, gemini, jira-scan)
│   ├── known_bugs.json                 #   Enriched Jira bug cache (29 bugs with summary, description, components)
│   └── root_cause_rules.json           #   Root cause rules (keyword matching, symptom/drill-down flags)
│
├── healthchecks/                       # Health check engines
│   ├── __init__.py                     #   Package overview
│   ├── knowledge_base.py               #   Load/save patterns from knowledge/; built-in seed data
│   ├── hybrid_health_check.py          #   Core engine: SSH, 15 check categories, 3-level RCA,
│   │                                   #     HTML reports, email, Jira, parallel investigation
│   ├── ai_analysis.py                  #   AI investigation loop, SSH safety, Gemini API, AI summary
│   ├── cnv_scenarios.py                #   CNV scenario runner: SSH to jump host, runs kube-burner
│   │                                   #     workloads via run-workloads.sh
│   ├── cnv_report.py                   #   CNV report generator: parses scenario output, builds
│   │                                   #     HTML reports (single + combined)
│   ├── simple_health_check.py          #   Minimal SSH health check (no AI, no web)
│   └── crewai_agents.py                #   CrewAI multi-agent system (experimental)
│
├── tools/                              # Shared tools
│   ├── __init__.py                     #   Package overview
│   └── ssh_tool.py                     #   CrewAI BaseTool for remote oc/kubectl over SSH
│
├── scripts/                            # Shell scripts & utilities
│   ├── install.sh                      #   One-command installer for RHEL/Fedora
│   ├── uninstall.sh                    #   Clean removal script
│   ├── start_dashboard.sh              #   Start server & open browser
│   └── migrate_json_to_db.py           #   One-time JSON → SQLite migration
│
├── docs/                               # Documentation
│   └── DESIGN.md                       #   This file — architecture & design
│
├── legacy/                             # Deprecated code
│   └── web_dashboard.py                #   Old standalone Flask app (replaced by app/)
│
├── reports/                            # Generated reports (gitignored)
│   └── health_report_*.html / .md
│
└── tests/                              # Test suite (placeholder)
```

---

## Performance Monitoring

<table>
<tr>
<td width="50%">

### What We Monitor

| Metric | Threshold | Action |
|:-------|:----------|:-------|
| **CPU Usage** | >85% | Alert + Analysis |
| **Memory Pressure** | >80% | Alert + OOM Risk |
| **Disk I/O** | Latency >100ms | Storage bottleneck |
| **Network** | Packet loss >1% | Network issues |
| **etcd Latency** | >100ms | Critical alert |
| **Pod Density** | >50/node | Capacity warning |

</td>
<td width="50%">

### Performance Insights

**AI-Powered Analysis:**
- Identifies resource hogs
- Predicts capacity issues
- Recommends optimizations
- Tracks degradation trends

**Actionable Reports:**
- "Node X is 92% CPU - consider spreading VMs"
- "Migration controller needs more memory"
- "etcd on slow disk - SSD recommended"

</td>
</tr>
</table>

---

## AI Integration Details

### AI-Driven Root Cause Analysis

The `--ai` flag activates the full 3-level investigation pipeline. Each level digs deeper than the last:

**How it works:**

```
Health Data ──→ L1: Pattern Match ──→ L2: Symptom Drill-down ──→ L3: AI Investigation Loop ──→ Report
                     │                       │                          │
                     └── known_issues.json   └── root_cause_rules.json  └── Gemini 2.5 Flash
                                                  (is_symptom=true)          (3-5 rounds)
```

**Level 1 -- Rule-based.** Pattern matching against `known_issues.json`, investigation commands, root cause determination via `root_cause_rules.json`.

**Level 2 -- Symptom drill-down.** When a root cause rule has `is_symptom: true`, deeper diagnostic commands run automatically. Drill-downs can chain.

**Level 3 -- AI investigation loop.** `ai_investigate()` runs an iterative loop where Gemini 2.5 Flash suggests diagnostic commands, the system executes them via SSH, and the AI analyzes the results. Key properties:
- **3-5 rounds** of investigation (minimum 3 before accepting `is_final=true`)
- **Self-evaluating** - the AI decides when the root cause is specific enough
- **Component tracing** - traces resource consumption to specific pods, workloads, namespaces
- **Directory drill-down** - never stops at "disk full"; drills into subdirectories to find the actual consumer
- **Enriched Jira context** - AI receives full bug descriptions (summary, symptoms, components, fix versions) from `known_bugs.json` during investigation, enabling it to match live symptoms against known bugs
- **Node SSH automation** - auto-resolves hostnames to IPs, adds `core@` prefix, timeout wrapping
- **Read-only safety** - blocks any write/destructive commands
- **Parallel execution** - all issue types investigated concurrently (ThreadPoolExecutor, 4 workers)

**AI summary layer** (`--ai-rca`): After the investigation pipeline, Gemini 2.5 Pro produces a high-level correlation analysis across all findings.

**Usage:**

```bash
python3 healthchecks/hybrid_health_check.py --ai                       # Full 3-level investigation
python3 healthchecks/hybrid_health_check.py --ai --ai-rca              # Investigation + AI summary
python3 healthchecks/hybrid_health_check.py --ai --ai-rca --rca-jira   # Full stack
```

**Performance:** ~75-120s for deep investigation of 5 issue types (parallel). ~$0.03-0.08 per run (Gemini Pro for investigation and summary).

**Requirements:** `GEMINI_API_KEY` or `GOOGLE_API_KEY` environment variable set on the host.

> **Note:** Jira integration works with a static fallback. Email and web search are stubs.

### Learning System

The `app/learning.py` module tracks recurring issues from health check runs. When a pattern reaches **3+ occurrences**, it is automatically promoted into the dynamic knowledge base (`knowledge/known_issues.json`) with `source="learned"`. The RCA pattern engine loads these at runtime, so promoted patterns are picked up automatically on subsequent runs without code changes.

### Learning Sources

<table>
<tr>
<td align="center" width="33%">

**Jira Bugs**

Scans CNV/OCP/ODF projects:
- Analyzes bug summaries
- Extracts error patterns
- Maps to components
- Tracks resolutions

</td>
<td align="center" width="33%">

**Email**

Searches team communications:
- Alert notifications
- Incident discussions
- Troubleshooting threads
- Solution sharing

</td>
<td align="center" width="33%">

**Web**

Searches external sources:
- Red Hat documentation
- Knowledge base articles
- Community forums
- Release notes

</td>
</tr>
</table>

### Evolution Statistics

| Source | What It Learns |
|:-------|:---------------|
| **Jira** | CNV, OCPBUGS, ODF bug reports |
| **Email** | Team alerts, incident threads |
| **Web** | Docs, forums, knowledge bases |

| Metric | Value |
|:-------|:------|
| Knowledge base entries | 50+ known issues |
| Auto-suggested checks | 10+ per scan |
| Current health checks | 17 categories |
| Learning frequency | Every build |

---

## Dynamic Knowledge Base

All knowledge (patterns, investigation commands, bug data) lives in JSON files in `knowledge/`. The pattern engine loads patterns at runtime from `known_issues.json` via `healthchecks/knowledge_base.py`. There are no hardcoded pattern dicts in the engine code.

**Five sources feed the knowledge store:**

| Source | Description |
|:-------|:-------------|
| **built-in** | 18 patterns with investigation commands, seeded on first run from `knowledge_base.py` |
| **user** | Added via admin UI at `/admin/knowledge` |
| **learned** | Auto-promoted from the learning system at 3+ occurrences |
| **gemini** | AI-suggested patterns saved after Gemini RCA analysis |
| **jira-scan** | Accepted suggestions from Jira API scans |

**Enriched Jira Bug Cache:** `known_bugs.json` stores 29 bugs with rich context pulled from Jira: `summary`, `description_snippet` (300-char extract of problem description), `components` (Jira component names), plus status/fix_versions/affects. This context is injected into the AI investigation prompt so the AI can compare live cluster symptoms against known bug descriptions.

**Admin UI:** `/admin/knowledge` provides CRUD operations for patterns and bugs. Bugs can be refreshed from the Jira API (the refresh endpoint now pulls summary, description, and components alongside status fields).

**Seeding:** On first run, if `known_issues.json` does not exist, it is generated from `_BUILTIN_SEED` in `knowledge_base.py` (18 patterns with investigation commands baked in). After seeding, all pattern data lives exclusively in JSON.

---

## Components

### 1. Web Dashboard (`app/`)

Flask-based Jenkins-like UI with role-based access control.

| Page | Description |
|:-----|:------------|
| Dashboard | Stats, recent builds, live status, quick actions |
| Configure | Select checks, pick scenarios, set options, presets |
| History | Past builds with filtering and search |
| Console | Real-time output streaming with phase progress |
| Build Detail | Status, duration (live timer), report, parameters |
| Reports | View generated HTML reports |
| Settings | Host management, app configuration |
| Schedules | Cron-like scheduled builds |
| Admin | User management, role assignment, audit log, knowledge base CRUD |
| Custom Checks | User-defined commands & scripts |

**User Roles:**
| Role | Capabilities |
|:-----|:-------------|
| **admin** | Full access: manage users, roles, all features, Jira scan |
| **operator** | Run builds, manage own runs, view reports |
| **viewer** | View dashboard, history, and reports (read-only) |

### 2. Health Check Engine (`healthchecks/hybrid_health_check.py`)

Core diagnostic system. Connects via SSH, runs `oc` commands, generates reports.

| Category | Checks |
|:---------|:-------|
| **Infrastructure** | Nodes, Cluster Operators, etcd, MachineConfigPools |
| **Workloads** | Pods (CrashLoop, Pending, OOM, Unknown) |
| **Virtualization** | KubeVirt, VMs, VMIs, Migrations, virt-handler, virt-controller, HCO |
| **Storage** | PVCs, CSI drivers, DataVolumes, VolumeSnapshots |
| **Performance** | CPU, Memory utilization per node |
| **Security** | Certificate expiration |
| **Monitoring** | Prometheus alerts, OOM events |

**Connection Resilience:**
- Validates host, SSH key, and user before connecting
- Verifies `oc` binary exists on target
- Checks `oc whoami` to confirm auth
- **Auto-login**: if auth is expired, automatically runs `oc login -u kubeadmin -p $(cat kubeadmin-password)`
- On failure: generates a styled error report HTML with diagnostics and troubleshooting steps

### 3. CNV Scenario Engine (`healthchecks/cnv_scenarios.py`)

Runs kube-burner performance workloads against the cluster via `cnv-scenarios/run-workloads.sh`.

- Configurable scenarios with per-test variables
- Sanity mode (quick, 10s pause) and full mode (production scale)
- Parallel test execution with live progress tracking
- Results parsed and displayed in dedicated HTML reports

### 4. AI Agent System (`healthchecks/crewai_agents.py`)

CrewAI-based multi-agent system (experimental, **CLI only** -- not integrated into the dashboard).

| Agent | Focus |
|:------|:------|
| **Infra SRE** | Node health, Operators, etcd perf |
| **Virt Expert** | KubeVirt, VM perf, Migrations |
| **Perf Engineer** | CPU analysis, Memory profiling, Bottlenecks |

Uses local LLM (Ollama llama3.2:3b) for analysis.

---

## Configuration

### Environment Variables

| Variable | Required | Description |
|:---------|:--------:|:------------|
| `RH_LAB_HOST` | Yes | Remote host with `oc` access (SSH target) |
| `RH_LAB_USER` | Yes | SSH username (default: `root`) |
| `SSH_KEY_PATH` | Yes | Path to SSH private key |
| `KUBECONFIG_REMOTE` | Yes | KUBECONFIG path on the remote host |
| `EMAIL_TO` | No | Email recipient for reports |
| `SMTP_SERVER` | No | SMTP server for email delivery |
| `GEMINI_API_KEY` | No | Google Gemini API key for AI investigation and summary |
| `GOOGLE_API_KEY` | No | Alternative to GEMINI_API_KEY (either works) |
| `GEMINI_MODEL` | No | Model for AI summary (default: `gemini-2.5-pro`) |
| `GEMINI_INVESTIGATE_MODEL` | No | Model for AI investigation loop (default: `gemini-2.5-pro`) |
| `FLASK_HOST` | No | Dashboard bind address (default: `0.0.0.0`) |
| `FLASK_PORT` | No | Dashboard port (default: `5000`) |

### Command Line Flags

| Flag | Description |
|:-----|:------------|
| `--server <host>` | Override SSH target |
| `--ai` | Enable full rule-based root cause analysis |
| `--ai-rca` | Enable Gemini AI-powered root cause analysis |
| `--rca-bugs` | Bug matching only (faster) |
| `--rca-jira` | Search Jira for related bugs |
| `--check-jira` | Enable AI evolution — scan for new tests |
| `--email` | Send report via email |

---

## Build Process

```
Init (5%) → Connect (15%) → Collect Data (50%) → Analyze (75%) → Report (100%)
```

**Detailed phases for health check builds:**

| Phase | What Happens |
|:------|:-------------|
| Scan Jira | Check Jira for new test suggestions (if enabled) |
| Connect | SSH to target host, validate oc access, auto-login if needed |
| Collect Data | Run 17+ oc commands across all check categories |
| Console Report | Print summary to build console |
| Analyze (optional) | 3-level RCA pipeline: L1 pattern matching, L2 symptom drill-down, L3 AI investigation loop (parallel per issue type). Optional Gemini AI summary. |
| Generate Report | Create HTML + Markdown reports |
| Send Email | Deliver report to configured recipients (if enabled) |

**Detailed phases for CNV scenario builds:**

| Phase | What Happens |
|:------|:-------------|
| Connect | SSH to jump host |
| Verify Setup | Check cnv-scenarios repo, KUBECONFIG, kube-burner |
| Run Scenarios | Execute selected tests via run-workloads.sh |
| Collect Results | Parse kube-burner output and summary.json |
| Summary | Generate scenario pass/fail summary |

---

## Security

| Aspect | Implementation |
|:-------|:---------------|
| SSH Keys | Stored locally, never committed to git |
| Command Validation | Only `oc`/`kubectl` commands allowed |
| KUBECONFIG | Injected per-command via environment variable |
| Process Isolation | Builds run in separate process groups |
| Config | `.env` is gitignored; installed config at `~/.config/` |
| Role-Based Access | Admin, Operator, Viewer with enforced permissions |
| Audit Logging | All admin actions logged with timestamp and user |

---

## Roadmap

| Planned | Ideas |
|:--------|:------|
| Performance trend graphs | Slack/Teams alerts |
| Multi-cluster support | Prometheus metrics export |
| Custom check plugins | Auto-remediation actions |
| Scheduled evolution scans | ML-based anomaly detection |

---

<p align="center">
  <strong>AI-Powered &bull; Performance Focused &bull; Pattern Learning</strong>
</p>

<p align="center">
  <strong>Built with care for Performance Engineers & SRE Teams</strong>
</p>

<p align="center">
  <sub>Document Version 3.1 &bull; March 2026</sub>
</p>
