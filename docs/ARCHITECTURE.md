# Architecture

This document describes how the system works at a technical level. For user-facing setup and usage, see [README.md](../README.md). For feature descriptions and roadmap, see [DESIGN.md](DESIGN.md).

---

## System Overview

```mermaid
graph TB
    subgraph browser [Browser]
        WebUI[Dashboard UI]
    end
    subgraph jumphost ["Jumphost (Flask Host)"]
        Flask[Flask App<br>run.py]
        SQLite[(SQLite DB<br>healthcrew.db)]
        Scheduler[Background Scheduler<br>daemon thread]
        Reports[reports/ directory]
    end
    subgraph subprocess [Subprocess]
        HybridHC[hybrid_health_check.py]
        CnvScenarios[cnv_scenarios.py]
    end
    subgraph cluster [OCP Cluster]
        APIServer[API Server]
        Workers[Worker Nodes]
        KubeVirt[KubeVirt / VMs]
    end
    subgraph external [External]
        Jira[Jira MCP]
        SMTP[SMTP Server]
        Ollama[Ollama LLM]
    end

    WebUI -->|HTTP| Flask
    Flask --> SQLite
    Flask -->|"subprocess.Popen()"| HybridHC
    Flask -->|"subprocess.Popen()"| CnvScenarios
    Scheduler -->|"start_build()"| Flask
    HybridHC -->|"Paramiko SSH"| APIServer
    CnvScenarios -->|"Paramiko SSH"| APIServer
    HybridHC -->|HTML file| Reports
    HybridHC -.->|"mcp-proxy (optional)"| Jira
    HybridHC -.->|"smtplib (optional)"| SMTP
    Ollama -.->|"CLI only (crewai_agents.py)"| APIServer
```

The system runs as a Flask web app on a jumphost that has SSH access to the OCP cluster. Health checks execute as **separate Python subprocesses** -- the Flask app spawns them, streams their stdout, and stores results in SQLite. This subprocess isolation means a crashing health check can't take down the web server.

---

## Build Execution Lifecycle

A "build" is a single health check run. This is the core flow from UI click to HTML report.

```mermaid
sequenceDiagram
    actor User
    participant UI as Browser
    participant Flask as Flask (routes.py)
    participant DB as SQLite
    participant Thread as Background Thread
    participant HC as hybrid_health_check.py
    participant Cluster as OCP Cluster (SSH)

    User->>UI: Click "Run" on configure page
    UI->>Flask: POST /job/run
    Flask->>DB: Create Build record (status=running)
    Flask->>Thread: threading.Thread(_execute_build)
    Flask-->>UI: Return build_number

    loop Poll progress
        UI->>Flask: GET /api/test-progress/{id}
        Flask-->>UI: {status, phases, output, progress%}
    end

    Thread->>HC: subprocess.Popen(python hybrid_health_check.py --checks ... --flags ...)
    HC->>Cluster: Paramiko SSH + oc commands
    Cluster-->>HC: Command output
    HC->>HC: Parse output, match patterns, generate HTML
    HC-->>Thread: stdout stream + exit code
    Thread->>DB: Update Build (status, report_file, duration)
    UI->>Flask: GET /report/{filename}
    Flask-->>UI: HTML report file
```

### Key details

- **Concurrency**: `MAX_CONCURRENT_BUILDS` (default 3). Excess builds are queued in memory and dequeued when a slot opens.
- **Phase tracking**: `_execute_build()` matches keywords in the subprocess stdout to advance phase indicators in the UI (e.g., "Connecting to host", "Running checks", "Generating report").
- **Task types**: Three execution paths share this lifecycle:
  - `health_check` -- runs `hybrid_health_check.py`
  - `cnv_scenarios` -- runs `cnv_scenarios.py` (kube-burner workloads)
  - `cnv_combined` -- runs scenarios first, then health check sequentially

---

## SSH Connection Model

All cluster interaction happens over SSH. The Flask host has an SSH key that grants access to a host with `oc` / `kubectl` configured.

```mermaid
graph LR
    subgraph flaskHost [Flask Host]
        HC[Health Check Subprocess]
    end
    subgraph target [Target Host]
        OC["oc / kubectl<br>(KUBECONFIG)"]
    end
    subgraph cluster [OCP Cluster]
        API[API Server]
    end

    HC -->|"Paramiko SSH<br>KEY_PATH"| OC
    OC -->|"kubeconfig auth"| API
```

### Connection flow

1. `get_ssh_client()` creates a global `paramiko.SSHClient` (one per subprocess, no connection pool).
2. `ssh_command(cmd)` prepends `export KUBECONFIG=/path/to/kubeconfig && ` to every command, then runs it via `exec_command()`.
3. The connection is reused for all checks within a single build run.
4. On connection failure, `SSHConnectionError` is raised with host, user, key path, and original error for debugging.

### Credentials

| Variable | Purpose |
|----------|---------|
| `RH_LAB_HOST` | SSH target hostname/IP |
| `RH_LAB_USER` | SSH username (default: `root`) |
| `SSH_KEY_PATH` | Path to private key |
| `KUBECONFIG_REMOTE` | Path to kubeconfig on the target host |

### Multiple SSH implementations

Four files implement SSH connections. Only `hybrid_health_check.py` is used by the dashboard:

| File | Used by | Notes |
|------|---------|-------|
| `healthchecks/hybrid_health_check.py` | Dashboard builds | Canonical. Global client, `ssh_command()` |
| `healthchecks/cnv_scenarios.py` | Dashboard (CNV scenarios) | Similar pattern, separate client |
| `healthchecks/simple_health_check.py` | CLI only | Minimal checks, standalone |
| `tools/ssh_tool.py` | CrewAI agents (CLI only) | CrewAI `BaseTool` wrapper |

---

## Health Check Engine

`healthchecks/hybrid_health_check.py` (~4300 lines) is the main engine. It runs as a subprocess and writes to stdout (streamed by the Flask thread) and generates an HTML report file.

### Check registry

15 check types are defined in `config/settings.py` as `AVAILABLE_CHECKS`. Each entry specifies:
- `name`, `icon`, `description`, `category`
- `commands` -- list of `oc` commands and what they validate
- `default` -- whether enabled by default

Categories: Infrastructure, Workloads, Virtualization, Storage, Network, Resources, Security, Monitoring.

### Execution pipeline

```mermaid
graph TD
    Start[Start] --> Connect[SSH Connect]
    Connect --> RunChecks[Run Selected Checks]
    RunChecks --> Parse[Parse oc Output]
    Parse --> Classify{Failures Found?}
    Classify -->|No| Report[Generate HTML Report]
    Classify -->|Yes| MatchIssues[Match KNOWN_ISSUES<br>18 patterns]
    MatchIssues --> Investigate[Run INVESTIGATION_COMMANDS<br>13 command sets]
    Investigate --> RootCause["determine_root_cause()<br>keyword matching"]
    RootCause --> JiraBugs["check_jira_bugs()<br>(optional)"]
    JiraBugs --> Report
    Report --> Email["send_email_report()<br>(optional)"]
    Email --> Done[Exit]
```

### RCA pipeline (pattern matching, no LLM)

The RCA system is entirely rule-based:

1. **Pattern matching** -- Each failure is compared against `KNOWN_ISSUES`, a dictionary of 18 issue types. Each entry has keyword patterns, Jira bug references, root cause descriptions, and remediation suggestions.

2. **Investigation** -- When an issue matches, `INVESTIGATION_COMMANDS` provides targeted `oc` commands to gather evidence (e.g., pod logs, node conditions, resource usage). 13 command sets cover: pod-crashloop, pod-unknown, virt-handler-memory, volumesnapshot, noobaa, metal3, etcd, migration, csi, oom, operator-degraded, operator-unavailable, node, alert.

3. **Root cause determination** -- `determine_root_cause()` scans investigation output for keywords (e.g., `oomkilled`, `crashloopbackoff`, `image pull`, `disk pressure`) and returns the highest-confidence match.

4. **Jira integration** (optional) -- `check_jira_bugs()` attempts to query Jira via `mcp-proxy`. On failure, it falls back to a hardcoded `KNOWN_BUGS` dictionary. Compares bug fix versions against the cluster version to assess if a bug is fixed, open, or a regression.

---

## Data Model

SQLite database (`healthcrew.db`) managed by SQLAlchemy. Defined in `app/models.py`.

```mermaid
erDiagram
    User ||--o{ Build : triggers
    User ||--o{ Schedule : creates
    User ||--o{ Host : owns
    User ||--o{ CustomCheck : creates
    User ||--o{ Template : creates
    User ||--o{ AuditLog : generates

    User {
        int id PK
        string username UK
        string email UK
        string password_hash
        string role
        datetime created_at
        datetime last_login
    }
    Build {
        int id PK
        int build_number UK
        string name
        int triggered_by FK
        string status
        json checks
        json options
        text output
        string report_file
        datetime started_at
        datetime finished_at
        string duration
        boolean scheduled
    }
    Schedule {
        int id PK
        string schedule_id UK
        string name
        int created_by FK
        string schedule_type
        string frequency
        string time_of_day
        json checks
        json options
        string status
    }
    Host {
        int id PK
        string name
        string host
        string user
        int created_by FK
    }
    CustomCheck {
        int id PK
        string name
        string check_type
        text command
        string match_type
        string run_with
        boolean enabled
        int created_by FK
    }
    Template {
        int id PK
        string name
        text description
        boolean shared
        json config
        int created_by FK
    }
    AuditLog {
        int id PK
        int user_id FK
        string action
        string target
        text details
        string ip_address
        datetime timestamp
    }
```

### Storage inconsistency (known)

The scheduler has a `Schedule` DB model but still reads from `schedules.json` at runtime. Both must be considered the source of truth until the migration is complete.

| Data | Primary storage | Notes |
|------|----------------|-------|
| Users, Builds, Hosts, Templates, CustomChecks, AuditLog | SQLite | Fully migrated |
| Schedules | `schedules.json` | DB model exists but scheduler reads JSON |
| App settings (Ollama config, thresholds) | `.settings.json` | Written by Settings UI, read by routes |

---

## Background Scheduler

`app/scheduler.py` runs a daemon thread that wakes every 60 seconds, reads `schedules.json`, and triggers builds for any schedule whose time has come.

- **Schedule types**: `once` (one-shot, marks `completed` after run), `recurring`
- **Frequencies**: `hourly`, `daily`, `weekly` (with day selection), `monthly` (with day-of-month), `custom` (cron-like)
- **Execution**: Calls `start_build()` from `app/routes.py` with `user_id=None` (system-triggered)
- **Dedup**: Skips a schedule if `last_run` was within the check interval
- **Start condition**: Only starts in the main Werkzeug process (`WERKZEUG_RUN_MAIN=true`) or when not in debug mode, to avoid duplicate schedulers from the reloader

---

## Authentication and Authorization

Session-based auth via Flask-Login. Passwords hashed with bcrypt.

| Role | Permissions |
|------|------------|
| `admin` | Everything: user management, builds, schedules, settings, audit log |
| `operator` | Run builds, create schedules, manage own templates and hosts |
| `viewer` | Read-only: view dashboard, builds, reports |

- First registered user is auto-promoted to `admin`
- `OPEN_REGISTRATION` (default `true`) controls whether new users can self-register
- Decorators: `@login_required`, `@admin_required`, `@operator_required`

---

## Configuration Hierarchy

Five configuration sources, listed by precedence (highest to lowest):

| Priority | Source | What it controls | Set by |
|----------|--------|-----------------|--------|
| 1 | CLI flags on `hybrid_health_check.py` | Per-run overrides: checks, RCA level, email, server | Subprocess args from Flask |
| 2 | `.settings.json` | Runtime UI settings: AI model/URL, thresholds | Settings page |
| 3 | Environment variables | SSH, Flask, database, email | `.env` or `~/.config/cnv-healthcrew/config.env` |
| 4 | `config/settings.py` (`Config` class) | Defaults for all settings | Code |
| 5 | `schedules.json` | Scheduler state | Schedules UI |

### Environment loading

`config/settings.py` checks for `~/.config/cnv-healthcrew/config.env` first (installed mode). If not found, falls back to `.env` in the project root (dev mode). Installed mode also uses XDG directories (`~/.local/share/cnv-healthcrew/`) for data, reports, and the database.

---

## AI Integration Status

| Component | Status | Details |
|-----------|--------|---------|
| CrewAI multi-agent health check | CLI only | `healthchecks/crewai_agents.py` -- not integrated into dashboard |
| Ollama (local LLM) | Config stored, not wired | Settings UI saves model/URL but `hybrid_health_check.py` does not use them |
| Google Gemini | Declared, not implemented | `google-genai` in requirements, `GOOGLE_API_KEY` documented, zero usage in code |
| RCA engine | Fully functional | Pattern matching, no LLM -- works well for known issue types |
| Jira integration | Functional with fallback | Tries MCP, falls back to static `KNOWN_BUGS` |
| Email search | Stub | `search_emails_for_issues()` builds keywords but calls nothing |

---

## Project Structure

```
ocp-health-crew/
├── run.py                         # Entry point: creates Flask app and starts server
├── config/
│   ├── settings.py                # Config class, AVAILABLE_CHECKS registry
│   └── cnv_scenarios.py           # CNV scenario definitions (kube-burner)
├── app/
│   ├── __init__.py                # App factory: create_app(), blueprint registration
│   ├── models.py                  # SQLAlchemy models (User, Build, Schedule, ...)
│   ├── routes.py                  # Dashboard blueprint: UI routes, build execution
│   ├── auth.py                    # Auth blueprint: login, register, profile
│   ├── admin.py                   # Admin blueprint: user CRUD, audit log
│   ├── scheduler.py               # Background scheduler (daemon thread + schedules.json)
│   ├── learning.py                # Pattern recognition from historical runs
│   ├── checks/                    # Re-exports AVAILABLE_CHECKS
│   ├── integrations/              # Stubs for future SSH, Jira, email integrations
│   ├── templates/                 # Jinja2 HTML templates
│   └── static/                    # CSS, images
├── healthchecks/
│   ├── hybrid_health_check.py     # Main engine: SSH checks, RCA, HTML reports
│   ├── cnv_scenarios.py           # kube-burner scenario runner
│   ├── cnv_report.py              # CNV scenario HTML report generator
│   ├── simple_health_check.py     # Minimal CLI health check
│   └── crewai_agents.py           # CrewAI agents (standalone, CLI only)
├── tools/
│   └── ssh_tool.py                # CrewAI BaseTool for SSH commands
├── scripts/
│   ├── install.sh                 # One-line installer
│   ├── uninstall.sh               # Uninstaller
│   ├── start_dashboard.sh         # Start script with browser open
│   └── migrate_json_to_db.py      # Legacy JSON to SQLite migration
├── docs/
│   ├── ARCHITECTURE.md            # This file
│   └── DESIGN.md                  # Feature descriptions and roadmap
├── reports/                       # Generated HTML reports (gitignored)
└── legacy/
    └── web_dashboard.py           # Old standalone Flask app (deprecated)
```
