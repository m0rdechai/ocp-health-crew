"""
CNV HealthCrew AI - Configuration Settings

Supports two modes:
  - Dev mode:  Reads .env from project root, stores data locally
  - Installed:  Reads ~/.config/cnv-healthcrew/config.env, stores in XDG dirs
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load config: installed path first, then fall back to local .env
_INSTALLED_CONFIG = Path.home() / ".config" / "cnv-healthcrew" / "config.env"
if _INSTALLED_CONFIG.exists():
    load_dotenv(_INSTALLED_CONFIG)
else:
    load_dotenv()  # loads .env from cwd


def _xdg_data_dir():
    """Get XDG data directory for installed mode"""
    return Path.home() / ".local" / "share" / "cnv-healthcrew"


def _is_installed():
    """Check if running in installed mode (config.env exists in XDG config dir)"""
    return _INSTALLED_CONFIG.exists()


class Config:
    """Application configuration"""
    
    # Base paths
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    # In installed mode, use XDG directories; in dev mode, use project-local paths
    if _is_installed():
        DATA_DIR = str(_xdg_data_dir())
        REPORTS_DIR = str(_xdg_data_dir() / "reports")
        BUILDS_FILE = str(_xdg_data_dir() / "builds.json")
    else:
        DATA_DIR = BASE_DIR
        REPORTS_DIR = os.path.join(BASE_DIR, "reports")
        BUILDS_FILE = os.path.join(BASE_DIR, ".builds.json")
    
    # Database Configuration (SQLite by default, upgrade to PostgreSQL later)
    SQLALCHEMY_DATABASE_URI = os.getenv(
        'DATABASE_URL',
        'sqlite:///' + os.path.join(DATA_DIR, 'healthcrew.db')
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Multi-user / Build Queue Configuration
    MAX_CONCURRENT_BUILDS = int(os.getenv('MAX_CONCURRENT_BUILDS', '3'))
    
    # Secret key for sessions
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-key-change-in-production')
    
    # Open registration: allow new users to self-register (default: True)
    OPEN_REGISTRATION = os.getenv('OPEN_REGISTRATION', 'true').lower() in ('true', '1', 'yes')
    
    TEMPLATES_DIR = os.path.join(BASE_DIR, "app", "templates")
    STATIC_DIR = os.path.join(BASE_DIR, "app", "static")
    
    # SSH Configuration
    SSH_HOST = os.getenv("RH_LAB_HOST")
    SSH_USER = os.getenv("RH_LAB_USER", "root")
    SSH_KEY_PATH = os.getenv("SSH_KEY_PATH")
    KUBECONFIG = os.getenv("KUBECONFIG_REMOTE", "/home/kni/clusterconfigs/auth/kubeconfig")
    
    # Email Configuration
    DEFAULT_EMAIL = os.getenv("EMAIL_TO", "guchen@redhat.com")
    
    # Flask Configuration
    FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")
    FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))
    DASHBOARD_BASE_URL = os.getenv("DASHBOARD_BASE_URL", "http://10.46.254.144:5000")
    FLASK_DEBUG = False
    
    # Build Configuration
    MAX_BUILDS_HISTORY = 100
    
    # Health Check Thresholds
    CPU_WARNING_THRESHOLD = 85
    MEMORY_WARNING_THRESHOLD = 80
    DISK_LATENCY_THRESHOLD_MS = 100
    ETCD_LATENCY_THRESHOLD_MS = 100
    POD_DENSITY_WARNING = 50
    
    # AI Configuration
    OLLAMA_MODEL = "ollama/llama3.2:3b"
    OLLAMA_URL = "http://localhost:11434"
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
    GEMINI_AI_RCA_ENABLED = os.getenv("GEMINI_AI_RCA", "true").lower() in ("true", "1", "yes")
    
    # Jira Configuration
    JIRA_PROJECTS = ["CNV", "OCPBUGS", "ODF"]
    JIRA_BUG_SCAN_DAYS = 30
    JIRA_BUG_LIMIT = 50


# Available health checks configuration
CATEGORY_ICONS = {
    "Infrastructure": "\U0001f3d7\ufe0f",   # 🏗️
    "Workloads":      "\U0001f4e6",          # 📦
    "Virtualization":  "\U0001f4bb",          # 💻
    "Storage":        "\U0001f4be",           # 💾
    "Network":        "\U0001f310",           # 🌐
    "Resources":      "\U0001f4ca",           # 📊
    "Security":       "\U0001f512",           # 🔒
    "Monitoring":     "\U0001f514",           # 🔔
}

AVAILABLE_CHECKS = {
    "node_health": {
        "name": "Node Health",
        "icon": "\U0001f5a5\ufe0f",          # 🖥️
        "description": "Check if all nodes are in Ready state",
        "category": "Infrastructure",
        "default": True,
        "commands": [
            {"cmd": "oc get nodes --no-headers",
             "validates": "All nodes must show 'Ready' status. Flags any node that is NotReady, SchedulingDisabled, or Unknown."}
        ]
    },
    "cluster_operators": {
        "name": "Cluster Operators",
        "icon": "\u2699\ufe0f",               # ⚙️
        "description": "Verify all cluster operators are available and not degraded",
        "category": "Infrastructure",
        "default": True,
        "commands": [
            {"cmd": "oc get co --no-headers",
             "validates": "Every operator must have AVAILABLE=True and DEGRADED=False. Flags operators that are unavailable or degraded."}
        ]
    },
    "pod_health": {
        "name": "Pod Health",
        "icon": "\U0001f4e6",                 # 📦
        "description": "Check for crashed, pending, or unhealthy pods",
        "category": "Workloads",
        "default": True,
        "commands": [
            {"cmd": "oc get pods -A --no-headers --field-selector=status.phase!=Running,status.phase!=Succeeded",
             "validates": "Lists pods NOT in Running or Succeeded state (CrashLoopBackOff, Pending, Error, Unknown, ImagePullBackOff)."},
            {"cmd": "oc get pods -A --no-headers | wc -l",
             "validates": "Total pod count across all namespaces for density calculations."}
        ]
    },
    "etcd_health": {
        "name": "ETCD Health",
        "icon": "\U0001f5c4\ufe0f",           # 🗄️
        "description": "Check etcd cluster status and leader election",
        "category": "Infrastructure",
        "default": True,
        "commands": [
            {"cmd": "oc get pods -n openshift-etcd -l app=etcd --no-headers",
             "validates": "All etcd member pods must be Running. Missing or CrashLooping etcd pods = critical."},
            {"cmd": "oc rsh -n openshift-etcd -c etcdctl <etcd-pod> etcdctl endpoint status --cluster -w table",
             "validates": "Checks cluster-wide etcd endpoint health, leader election, DB size, and raft index lag between members."}
        ]
    },
    "kubevirt": {
        "name": "KubeVirt/CNV",
        "icon": "\U0001f4bb",                 # 💻
        "description": "Check CNV components and virtual machine status",
        "category": "Virtualization",
        "default": True,
        "commands": [
            {"cmd": "oc get kubevirt -A --no-headers",
             "validates": "KubeVirt CR must show 'Deployed' phase. Any other phase (Deploying, Error) = problem."},
            {"cmd": "oc get vmi -A --no-headers",
             "validates": "Lists all VM instances. Counts running VMs and identifies failed/stuck VMIs."},
            {"cmd": "oc get pods -n openshift-cnv -l kubevirt.io=virt-handler --no-headers",
             "validates": "All virt-handler DaemonSet pods must be Running. These manage VMs on each node."},
            {"cmd": "oc adm top pods -n openshift-cnv -l kubevirt.io=virt-handler --no-headers",
             "validates": "Checks memory/CPU usage of virt-handler pods. High memory (>500Mi) indicates possible leak (CNV-66551)."},
            {"cmd": "oc get pods -n openshift-cnv -l 'kubevirt.io in (virt-controller,virt-api)' --no-headers",
             "validates": "virt-controller and virt-api pods must be Running. These are the CNV control plane."},
            {"cmd": "oc get pods -A -l kubevirt.io=virt-launcher --no-headers | grep -v Running",
             "validates": "Finds virt-launcher pods not Running. Each VM has a launcher pod - unhealthy = VM problem."}
        ]
    },
    "vm_migrations": {
        "name": "VM Migrations",
        "icon": "\U0001f504",                 # 🔄
        "description": "Check for stuck or failed VM migrations",
        "category": "Virtualization",
        "default": True,
        "commands": [
            {"cmd": "oc get vmim -A --no-headers | grep -v Succeeded",
             "validates": "Lists active/pending/failed migrations. Only 'Succeeded' is healthy - everything else needs attention."},
            {"cmd": "oc get vmim -A -o json | grep '\"phase\":\"Failed\"' | wc -l",
             "validates": "Counts total failed migrations. High count suggests underlying storage/network issues."},
            {"cmd": "oc get vmim -A --no-headers | grep Running",
             "validates": "Finds migrations stuck in Running state. Long-running migrations may be hung."}
        ]
    },
    "storage_health": {
        "name": "Storage Health",
        "icon": "\U0001f4be",                 # 💾
        "description": "Check PVCs, CSI drivers, and volume snapshots",
        "category": "Storage",
        "default": True,
        "commands": [
            {"cmd": "oc get pvc -A --no-headers | grep -v Bound",
             "validates": "All PVCs should be Bound. Pending PVCs = storage provisioning failure or missing StorageClass."},
            {"cmd": "oc get pods -A --no-headers | grep -E 'csi|driver' | grep -v Running",
             "validates": "CSI driver pods must be Running. Down CSI drivers = storage operations will fail."},
            {"cmd": "oc get volumesnapshot -A --no-headers | grep -v 'true'",
             "validates": "Volume snapshots should show readyToUse=true. Unready snapshots = backup/clone problems."},
            {"cmd": "oc get dv -A --no-headers | grep -vE 'Succeeded|PVCBound'",
             "validates": "DataVolumes should be Succeeded or PVCBound. Stuck DVs = import/clone failures."}
        ]
    },
    "network_health": {
        "name": "Network Health",
        "icon": "\U0001f310",                 # 🌐
        "description": "Check network policies and multus configurations",
        "category": "Network",
        "default": True,
        "commands": [
            {"cmd": "oc get network-attachment-definitions -A --no-headers",
             "validates": "Lists all Multus network attachments. Missing NADs = VMs can't attach to secondary networks."},
            {"cmd": "oc get networkpolicy -A --no-headers | head -20",
             "validates": "Lists active network policies. Misconfigured policies can block pod/VM traffic."}
        ]
    },
    "resource_usage": {
        "name": "Resource Usage",
        "icon": "\U0001f4ca",                 # 📊
        "description": "Check CPU and memory utilization across nodes",
        "category": "Resources",
        "default": True,
        "commands": [
            {"cmd": "oc adm top nodes --no-headers",
             "validates": "Shows CPU/memory usage per node. Flags nodes above threshold (default: CPU >85%, Memory >80%)."}
        ]
    },
    "certificates": {
        "name": "Certificates",
        "icon": "\U0001f512",                 # 🔒
        "description": "Check for expiring or invalid certificates",
        "category": "Security",
        "default": True,
        "commands": [
            {"cmd": "oc get certificates -A --no-headers",
             "validates": "Lists cert-manager certificates. Checks for expired, not-ready, or failed renewal certs."},
            {"cmd": "oc get secret -A -o json | grep -o '\"notAfter\":\"[^\"]*\"' | head -10",
             "validates": "Scans TLS secrets for expiration dates. Certs expiring soon = API/ingress outage risk."}
        ]
    },
    "machine_config": {
        "name": "Machine Config",
        "icon": "\U0001f6e0\ufe0f",           # 🛠️
        "description": "Check MachineConfigPool status",
        "category": "Infrastructure",
        "default": True,
        "commands": [
            {"cmd": "oc get mcp --no-headers",
             "validates": "All MachineConfigPools must show UPDATED=True and DEGRADED=False. Degraded MCP = nodes stuck in config rollout."}
        ]
    },
    "cdi_health": {
        "name": "CDI Health",
        "icon": "\U0001f4bf",                 # 💿
        "description": "Check Containerized Data Importer status",
        "category": "Virtualization",
        "default": True,
        "commands": [
            {"cmd": "oc get cdi -A --no-headers",
             "validates": "CDI CR must show 'Deployed' phase. CDI handles VM disk imports and cloning."},
            {"cmd": "oc get pods -n openshift-cnv -l app=containerized-data-importer --no-headers",
             "validates": "CDI operator/controller pods must be Running. Down CDI = DataVolume imports will fail."}
        ]
    },
    "hco_health": {
        "name": "HCO Health",
        "icon": "\U0001f3db\ufe0f",           # 🏛️
        "description": "Check HyperConverged Operator status",
        "category": "Virtualization",
        "default": True,
        "commands": [
            {"cmd": "oc get hyperconverged -n openshift-cnv kubevirt-hyperconverged -o jsonpath='{.status.conditions}'",
             "validates": "All HCO conditions should be healthy (Available=True, Degraded=False, Progressing=False). HCO manages all CNV sub-operators."}
        ]
    },
    "odf_health": {
        "name": "ODF Health",
        "icon": "\U0001f4e1",                 # 📡
        "description": "Check OpenShift Data Foundation status",
        "category": "Storage",
        "default": True,
        "commands": [
            {"cmd": "oc get storagecluster -n openshift-storage --no-headers",
             "validates": "StorageCluster phase must be 'Ready'. Any other phase = ODF is degraded or not functional."},
            {"cmd": "oc get pods -n openshift-storage --no-headers | grep -v Running | grep -v Completed",
             "validates": "All ODF pods should be Running or Completed. Crashed ODF pods = Ceph storage problems."}
        ]
    },
    "alerts": {
        "name": "Active Alerts",
        "icon": "\U0001f6a8",                 # 🚨
        "description": "Check for firing Prometheus alerts",
        "category": "Monitoring",
        "default": True,
        "commands": [
            {"cmd": "oc exec -n openshift-monitoring -c prometheus prometheus-k8s-0 -- curl -s 'http://localhost:9090/api/v1/alerts' | grep -o '\"alertname\":\"[^\"]*\"' | sort | uniq -c | sort -rn",
             "validates": "Queries Prometheus for all currently firing alerts. Lists alert names with frequency - any firing alert needs investigation."}
        ]
    }
}

# ── CNV Scenarios (kube-burner test suite) ────────────────────────────────────
from config.cnv_scenarios import CNV_SCENARIOS, CNV_SCENARIO_CATEGORIES, CNV_CATEGORY_ORDER, CNV_GLOBAL_VARIABLES
