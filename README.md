<p align="center">
  <img src="https://img.shields.io/badge/OpenShift-EE0000?style=for-the-badge&logo=redhatopenshift&logoColor=white" alt="OpenShift"/>
  <img src="https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python"/>
  <img src="https://img.shields.io/badge/Flask-000000?style=for-the-badge&logo=flask&logoColor=white" alt="Flask"/>
  <img src="https://img.shields.io/badge/KubeVirt-326CE5?style=for-the-badge&logo=kubernetes&logoColor=white" alt="KubeVirt"/>
  <img src="https://img.shields.io/badge/AI_Powered-FF6F00?style=for-the-badge&logo=tensorflow&logoColor=white" alt="AI"/>
</p>

<h1 align="center">CNV HealthCrew AI</h1>

<p align="center">
  <strong>AI-Powered Health Monitoring & Root Cause Analysis for OpenShift + CNV</strong><br>
  <em>Self-evolving test suite that learns from Jira bugs, emails, and the web</em>
</p>

---

## What It Does

CNV HealthCrew AI connects to your OpenShift cluster via SSH and runs **15+ health checks** across infrastructure, virtualization, storage, and networking. It generates beautiful HTML reports, sends email notifications, and uses AI to perform root cause analysis on detected issues.

**Key capabilities:**
- **Health Checks** -- Nodes, operators, pods, etcd, KubeVirt/CNV, VMs, migrations, storage (ODF/CSI), network, certificates, alerts
- **Web Dashboard** -- Jenkins-like UI to configure, run, schedule, and review health checks
- **AI Root Cause Analysis** -- Pattern matching + Gemini AI for deep failure correlation and remediation
- **Pattern Learning** -- Discovers patterns, matches known Jira bugs, suggests new checks
- **Reports & Notifications** -- Professional HTML reports with email delivery

---

## Quick Install (RHEL / Fedora)

One command:

```bash
curl -sL https://raw.githubusercontent.com/guchen11/ocp-health-crew/main/scripts/install.sh | bash
```

This will:
1. Check your system (Python 3.11+, git)
2. Clone the repo to `~/cnv-healthcrew/`
3. Create a Python virtual environment and install dependencies
4. Set up configuration at `~/.config/cnv-healthcrew/config.env`
5. Install a systemd user service

Then configure and start:

```bash
# 1. Edit config with your cluster details
vi ~/.config/cnv-healthcrew/config.env

# 2. Start the service
systemctl --user start cnv-healthcrew
systemctl --user enable cnv-healthcrew  # auto-start on boot

# 3. Open the dashboard
xdg-open http://localhost:5000
```

### Update

```bash
cd ~/cnv-healthcrew && git pull && systemctl --user restart cnv-healthcrew
```

### Uninstall

```bash
bash ~/cnv-healthcrew/scripts/uninstall.sh
```

---

## Manual Setup (Development)

```bash
git clone https://github.com/guchen11/ocp-health-crew.git
cd ocp-health-crew

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp config.env.example .env
vi .env   # set RH_LAB_HOST, SSH_KEY_PATH, etc.

# Run
python run.py
```

Open http://localhost:5000 in your browser.

---

## Configuration

Edit `~/.config/cnv-healthcrew/config.env` (installed) or `.env` (dev mode):

| Variable | Required | Description |
|:---------|:--------:|:------------|
| `RH_LAB_HOST` | Yes | Remote host with `oc` access (SSH target) |
| `RH_LAB_USER` | Yes | SSH username (default: `root`) |
| `SSH_KEY_PATH` | Yes | Path to your SSH private key |
| `KUBECONFIG_REMOTE` | Yes | KUBECONFIG path on the remote host |
| `EMAIL_TO` | No | Email recipient for reports |
| `SMTP_SERVER` | No | SMTP server for email delivery |
| `GEMINI_API_KEY` | No | Google Gemini API key for AI-powered RCA (`--ai-rca`) |
| `GEMINI_MODEL` | No | Gemini model name (default: `gemini-2.5-pro`) |
| `FLASK_HOST` | No | Dashboard bind address (default: `0.0.0.0`) |
| `FLASK_PORT` | No | Dashboard port (default: `5000`) |

---

## Command Line Usage

Run health checks directly without the dashboard:

```bash
# Basic health check
python healthchecks/hybrid_health_check.py

# Gemini AI RCA (pattern matching runs first, then Gemini builds on the findings)
python healthchecks/hybrid_health_check.py --ai-rca

# Rule-based RCA with deep investigation + Gemini AI
python healthchecks/hybrid_health_check.py --ai --ai-rca

# Rule-based RCA only (pattern matching + deep investigation, no AI)
python healthchecks/hybrid_health_check.py --ai

# Bug matching only (faster, no deep investigation)
python healthchecks/hybrid_health_check.py --rca-bugs

# Search Jira for related bugs during RCA
python healthchecks/hybrid_health_check.py --rca-jira

# Scan Jira for new test suggestions
python healthchecks/hybrid_health_check.py --check-jira

# Send report via email
python healthchecks/hybrid_health_check.py --email --email-to user@example.com

# Override SSH target
python healthchecks/hybrid_health_check.py --server my-other-host.example.com

# Simple health check (no AI, no web dependencies)
python healthchecks/simple_health_check.py

# CrewAI multi-agent health check (standalone CLI, not integrated into dashboard)
python healthchecks/crewai_agents.py
```

---

## Health Checks

| Category | Checks |
|:---------|:-------|
| **Infrastructure** | Node status, Cluster Operators, etcd health, MachineConfigPools |
| **Workloads** | Pod health (CrashLoop, Pending, OOM, Unknown) |
| **Virtualization** | KubeVirt operator, VMs, VMIs, migrations, virt-handler, CDI, HCO |
| **Storage** | PVCs, CSI drivers, DataVolumes, VolumeSnapshots, ODF |
| **Performance** | CPU/Memory utilization per node, resource thresholds |
| **Security** | Certificate expiration checks |
| **Monitoring** | Active Prometheus alerts |

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
│   ├── admin.py                        #   Admin panel: user CRUD, roles, audit log
│   ├── scheduler.py                    #   Background scheduler for timed builds
│   ├── learning.py                     #   Pattern recognition & recurring issue tracking
│   ├── checks/                         #   Health check metadata (re-exports AVAILABLE_CHECKS)
│   ├── integrations/                   #   Integration stubs (Jira, email, SSH)
│   ├── templates/                      #   Jinja2 HTML templates
│   └── static/                         #   CSS & images
│
├── config/                             # Configuration
│   ├── settings.py                     #   App config: paths, DB, SSH, checks, Flask settings
│   └── cnv_scenarios.py                #   CNV scenario definitions & variables for the dashboard
│
├── healthchecks/                       # Health check engines
│   ├── hybrid_health_check.py          #   Core engine: SSH, reports, rule-based RCA, auto oc-login
│   ├── ai_analysis.py                  #   Gemini AI RCA: API call, prompt builder, markdown-to-HTML
│   ├── cnv_scenarios.py                #   CNV scenario runner: SSH + kube-burner workloads
│   ├── cnv_report.py                   #   CNV report generator: parses output, builds HTML
│   ├── simple_health_check.py          #   Minimal SSH health check (no AI, no web)
│   └── crewai_agents.py                #   CrewAI multi-agent system (experimental)
│
├── tools/                              # Shared tools
│   └── ssh_tool.py                     #   CrewAI SSH tool for remote oc commands
│
├── scripts/                            # Shell scripts & utilities
│   ├── install.sh                      #   One-command installer for RHEL/Fedora
│   ├── uninstall.sh                    #   Clean removal script
│   ├── start_dashboard.sh              #   Start server & open browser
│   └── migrate_json_to_db.py           #   One-time JSON → SQLite migration
│
├── docs/                               # Documentation
│   ├── ARCHITECTURE.md                 #   Technical architecture & data flows
│   └── DESIGN.md                       #   Feature descriptions & roadmap
│
├── legacy/                             # Deprecated code
│   └── web_dashboard.py                #   Old standalone Flask app (replaced by app/)
│
└── reports/                            # Generated reports (gitignored)
```

---

## How the AI Evolves

```
Jira Bug: CNV-75962 "kubevirt-migration-controller OOMKilled at scale"
     |
     v
AI Analysis:
  - Pattern detected: "OOMKilled" + "migration" + "scale"
  - Component: kubevirt-migration-controller
  - Priority: Critical
     |
     v
AI Suggestion: "Add health check: migration_controller_memory"
  - Monitor memory usage of migration controller pods
  - Alert when approaching limits
     |
     v
Result: New test added to the suite automatically
```

The system learns from:
- **Jira** -- Scans CNV, OCPBUGS, ODF bug reports for patterns
- **Email** -- Searches team discussions and alert notifications
- **Web** -- Queries Red Hat docs, knowledge bases, and forums
- **History** -- Tracks recurring issues across runs and discovers patterns

See [docs/DESIGN.md](docs/DESIGN.md) for the full architecture and design details.

---

## Security

| Aspect | Implementation |
|:-------|:---------------|
| SSH Keys | Stored locally, never committed to git |
| Command Validation | Only `oc`/`kubectl` commands are allowed |
| KUBECONFIG | Injected per-command via environment variable |
| Process Isolation | Health check builds run in separate process groups |
| Config | `.env` is gitignored; installed config at `~/.config/` |

---

## Useful Commands

```bash
# Service management
systemctl --user status cnv-healthcrew    # Check status
systemctl --user restart cnv-healthcrew   # Restart
systemctl --user stop cnv-healthcrew      # Stop
journalctl --user -u cnv-healthcrew -f    # View logs

# Quick update
cd ~/cnv-healthcrew && git pull && systemctl --user restart cnv-healthcrew
```

---

<p align="center">
  Built with care for Performance Engineers & SRE Teams at Red Hat
</p>
