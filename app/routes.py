"""
CNV Health Dashboard - Flask Routes

Multi-user with concurrent builds, role-based access, and audit logging.
"""

import os
import sys
import json
import glob
import re
import subprocess
import threading
import time
import signal
from datetime import datetime
from flask import Blueprint, render_template, jsonify, request, send_from_directory, redirect, url_for
from flask_login import login_required, current_user
from functools import wraps
from app.models import db, Host, Template

# Import configuration
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import Config, AVAILABLE_CHECKS, CATEGORY_ICONS, CNV_SCENARIOS, CNV_SCENARIO_CATEGORIES, CNV_CATEGORY_ORDER, CNV_GLOBAL_VARIABLES
from healthchecks.cnv_report import parse_cnv_results, generate_cnv_report_html, generate_cnv_email_html

# Create Blueprint
dashboard_bp = Blueprint('dashboard', __name__)

# Configuration
BASE_DIR = Config.BASE_DIR
REPORTS_DIR = Config.REPORTS_DIR
SCRIPT_PATH = os.path.join(BASE_DIR, "healthchecks", "hybrid_health_check.py")
CNV_SCRIPT_PATH = os.path.join(BASE_DIR, "healthchecks", "cnv_scenarios.py")
BUILDS_FILE = Config.BUILDS_FILE
SCHEDULES_FILE = os.path.join(BASE_DIR, "schedules.json")
SETTINGS_FILE = os.path.join(BASE_DIR, ".settings.json")

# ── Concurrent build queue ──────────────────────────────────────────────────
MAX_CONCURRENT = Config.MAX_CONCURRENT_BUILDS
running_jobs = {}          # job_id -> job dict
queued_jobs = []           # list of (job_id, checks, options, user_id) waiting
_jobs_lock = threading.Lock()

# Legacy JSON storage (still used for settings; builds migrated to DB)
builds = []
schedules = []

# Default thresholds
DEFAULT_THRESHOLDS = {
    'cpu_warning': 85,
    'memory_warning': 80,
    'disk_latency': 100,
    'etcd_latency': 100,
    'pod_density': 50,
    'restart_count': 5,
    'virt_handler_memory': 500
}

# Available AI Agents (from CrewAI)
AVAILABLE_AGENTS = {
    'infra_agent': {
        'name': 'Infrastructure SRE',
        'icon': '🏗️',
        'description': 'Verifies node health and ClusterOperator status',
        'category': 'Infrastructure',
    },
    'cnv_agent': {
        'name': 'Virtualization Specialist',
        'icon': '💻',
        'description': 'Audits CNV/KubeVirt subsystem, checks VMs and operators',
        'category': 'Virtualization',
    },
    'perf_agent': {
        'name': 'Performance Auditor',
        'icon': '📈',
        'description': 'Identifies CPU/RAM bottlenecks via oc adm top',
        'category': 'Performance',
    },
    'storage_agent': {
        'name': 'Storage Inspector',
        'icon': '💿',
        'description': 'Checks ODF, Ceph, PVCs, CSI drivers and volume health',
        'category': 'Storage',
    },
    'network_agent': {
        'name': 'Network Analyst',
        'icon': '🌐',
        'description': 'Inspects network policies, multus, and connectivity',
        'category': 'Network',
    },
    'security_agent': {
        'name': 'Security Auditor',
        'icon': '🔒',
        'description': 'Checks certificates, RBAC, and security configurations',
        'category': 'Security',
    },
}

# Default CNV Scenarios settings (built from config)
_DEFAULT_CNV_SETTINGS = {
    'cnv_path': '/home/kni/git/cnv-scenarios',
    'mode': 'sanity',
    'parallel': False,
    'kb_log_level': '',
    'kb_timeout': '',
    'global_vars': {},
    'scenario_vars': {},
}

# Default settings
DEFAULT_SETTINGS = {
    'thresholds': DEFAULT_THRESHOLDS,
    'ssh': {'host': '', 'user': 'root'},
    'ai': {'model': 'ollama/llama3.2:3b', 'url': 'http://localhost:11434'},
    'jira': {'projects': ['CNV', 'OCPBUGS', 'ODF'], 'scan_days': 30, 'bug_limit': 50},
    'cnv': _DEFAULT_CNV_SETTINGS,
}


# ── Role decorators ─────────────────────────────────────────────────────────
def operator_required(f):
    """Route requires operator or admin role."""
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not current_user.is_operator:
            return "Access denied. Operator role required.", 403
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Route requires admin role."""
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not current_user.is_admin:
            return "Access denied. Admin role required.", 403
        return f(*args, **kwargs)
    return decorated


# ── Audit helper ─────────────────────────────────────────────────────────────
def log_audit(action, target=None, details=None, user_id=None, username=None):
    """Record an audit log entry."""
    from app.models import db, AuditLog
    try:
        if user_id is None and current_user and current_user.is_authenticated:
            user_id = current_user.id
            username = current_user.username
        entry = AuditLog(
            user_id=user_id,
            username=username or 'system',
            action=action,
            target=target,
            details=details,
            ip_address=request.remote_addr if request else None,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        pass  # Audit should never break the app


# ── Settings helpers ─────────────────────────────────────────────────────────
def load_settings():
    """Load user settings from file"""
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                settings = json.load(f)
                merged = DEFAULT_SETTINGS.copy()
                for key in settings:
                    if isinstance(settings[key], dict):
                        merged[key] = {**DEFAULT_SETTINGS.get(key, {}), **settings[key]}
                    else:
                        merged[key] = settings[key]
                return merged
        except:
            pass
    return DEFAULT_SETTINGS.copy()


def save_settings(settings):
    """Save user settings to file"""
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(settings, f, indent=2)


def _collect_scenario_var_defaults(form):
    """Collect per-scenario variable defaults from a settings form POST."""
    result = {}
    for sid, scenario in CNV_SCENARIOS.items():
        svars = scenario.get('variables', {})
        if not svars:
            continue
        saved = {}
        for var_name, var_info in svars.items():
            key = f'cnv_var_{sid}_{var_name}'
            if var_info['type'] == 'bool':
                saved[var_name] = form.get(key) == 'on'
            elif var_info['type'] == 'int':
                try:
                    saved[var_name] = int(form.get(key, var_info.get('default', 0)))
                except (ValueError, TypeError):
                    saved[var_name] = var_info.get('default', 0)
            else:
                saved[var_name] = form.get(key, str(var_info.get('default', ''))).strip()
        result[sid] = saved
    return result


def _send_cnv_email_report(recipient, build_num, build_name, status, status_text,
                            duration, checks, options, output, cnv_results=None):
    """Send a CNV scenario results email with per-test pass/fail details."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    smtp_server = os.getenv('SMTP_SERVER', 'smtp.corp.redhat.com')
    smtp_port = int(os.getenv('SMTP_PORT', '25'))
    email_from = os.getenv('EMAIL_FROM', 'cnv-healthcrew@redhat.com')

    mode = options.get('scenario_mode', 'sanity')

    # Parse results from output if not provided
    if cnv_results is None:
        cnv_results = parse_cnv_results(output)

    # Use the rich email generator
    subject, html = generate_cnv_email_html(
        results=cnv_results,
        build_num=build_num,
        build_name=build_name,
        status=status,
        status_text=status_text,
        duration=duration,
        mode=mode,
        checks=checks,
        output=output,
    )

    # Plain text fallback
    from healthchecks.cnv_report import strip_ansi
    tests = cnv_results.get("tests", [])
    passed = cnv_results.get("passed", 0)
    failed = cnv_results.get("failed", 0)

    test_lines = []
    for t in tests:
        test_lines.append(f"  {'PASS' if t['status'] == 'PASS' else 'FAIL'}  {t['name']:<25}  {t.get('duration_str', 'N/A')}")

    plain = f"""CNV Scenarios Report — Build #{build_num}
Status: {status_text}
Duration: {duration}
Mode: {mode}
Passed: {passed} | Failed: {failed} | Total: {len(tests)}

--- Scenario Results ---
{chr(10).join(test_lines)}
"""

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = email_from
    msg['To'] = recipient
    msg.attach(MIMEText(plain, 'plain'))
    msg.attach(MIMEText(html, 'html'))

    with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
        server.sendmail(email_from, [recipient], msg.as_string())


def get_thresholds():
    """Get current threshold settings"""
    settings = load_settings()
    return settings.get('thresholds', DEFAULT_THRESHOLDS)


def get_hosts_for_user(user, **_kwargs):
    """Get all hosts — everyone can see all hosts."""
    return Host.query.order_by(Host.created_at).all()


def _setup_passwordless_ssh(host, user, password):
    """Setup passwordless SSH to a host. Returns (success, message)."""
    import paramiko
    home = os.path.expanduser("~")
    ssh_dir = os.path.join(home, ".ssh")
    key_path = os.path.join(ssh_dir, "id_ed25519")
    pub_path = key_path + ".pub"

    try:
        os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
        if not os.path.exists(key_path):
            key = paramiko.Ed25519Key.generate()
            key.write_private_key_file(key_path)
            os.chmod(key_path, 0o600)
            pub_key_str = f"{key.get_name()} {key.get_base64()} cnv-healthcrew"
            with open(pub_path, 'w') as f:
                f.write(pub_key_str + "\n")
            os.chmod(pub_path, 0o644)
        else:
            key = paramiko.Ed25519Key(filename=key_path)
            pub_key_str = f"{key.get_name()} {key.get_base64()} cnv-healthcrew"

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(host, username=user, password=password, timeout=15)

        commands = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"grep -qxF '{pub_key_str}' ~/.ssh/authorized_keys 2>/dev/null || "
            f"echo '{pub_key_str}' >> ~/.ssh/authorized_keys && "
            "chmod 600 ~/.ssh/authorized_keys"
        )
        stdin, stdout, stderr = client.exec_command(commands)
        exit_status = stdout.channel.recv_exit_status()
        err_output = stderr.read().decode().strip()
        client.close()

        if exit_status != 0:
            return False, f'Failed to install key: {err_output}'

        # Verify key-based login works
        verify_client = paramiko.SSHClient()
        verify_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        verify_client.connect(host, username=user, key_filename=key_path, timeout=15)
        verify_client.close()
        return True, 'OK'
    except Exception as e:
        return False, str(e)


def sync_hosts_from_form(host_ids, host_names, host_addrs, host_users, host_passwords, user):
    """
    Sync the host list from form submission for the current user.
    - Existing hosts (with id) are updated.
    - New hosts (no id) are created.
    - If a password is provided for a new host, passwordless SSH is set up first.
    Returns (first_host, first_user, ssh_messages).
    """
    first_host = ''
    first_user = 'root'
    ssh_messages = []
    submitted_ids = set()

    # First pass: collect IDs of existing hosts still in the form
    for hid in host_ids:
        hid = hid.strip()
        if hid:
            submitted_ids.add(int(hid))

    # Delete hosts that were removed from the form (before adding new ones)
    if user.is_admin:
        all_hosts = Host.query.all()
    else:
        all_hosts = Host.query.filter_by(created_by=user.id).all()
    for h in all_hosts:
        if h.id not in submitted_ids:
            db.session.delete(h)
    db.session.flush()

    # Second pass: update existing and create new hosts
    for hid, name, addr, usr, pwd in zip(host_ids, host_names, host_addrs, host_users, host_passwords):
        addr = addr.strip()
        if not addr:
            continue
        name = name.strip() or addr
        usr = usr.strip() or 'root'
        pwd = pwd.strip() if pwd else ''

        if not first_host:
            first_host = addr
            first_user = usr

        hid = hid.strip()
        if hid:
            # Update existing host
            host_obj = Host.query.get(int(hid))
            if host_obj and (host_obj.created_by == user.id or user.is_admin):
                host_obj.name = name
                host_obj.host = addr
                host_obj.user = usr
        else:
            # New host — setup passwordless SSH if password provided
            if pwd:
                ok, msg = _setup_passwordless_ssh(addr, usr, pwd)
                if ok:
                    ssh_messages.append(f'SSH key installed on {usr}@{addr}')
                else:
                    ssh_messages.append(f'SSH setup failed for {usr}@{addr}: {msg}')
            label = f'{name} [{user.username}]' if not name.endswith(f'[{user.username}]') else name
            host_obj = Host(name=label, host=addr, user=usr, created_by=user.id)
            db.session.add(host_obj)

    db.session.commit()
    return first_host, first_user, ssh_messages


# ── Build helpers (DB-backed) ────────────────────────────────────────────────
def load_builds():
    """Load builds from database, return as list of dicts."""
    global builds
    from app.models import Build
    try:
        db_builds = Build.query.order_by(Build.build_number.desc()).limit(Config.MAX_BUILDS_HISTORY).all()
        builds = [b.to_dict() for b in db_builds]
    except Exception:
        builds = []
    return builds


def save_build_to_db(build_record, user_id=None):
    """Save a build record to the database."""
    from app.models import db, Build
    build = Build(
        build_number=build_record['number'],
        name=build_record.get('name', ''),
        triggered_by=user_id,
        status=build_record['status'],
        status_text=build_record['status_text'],
        checks=build_record.get('checks', []),
        checks_count=build_record.get('checks_count', 0),
        options=build_record.get('options', {}),
        output=build_record.get('output', ''),
        report_file=build_record.get('report_file'),
        duration=build_record.get('duration', ''),
        scheduled=build_record.get('options', {}).get('scheduled', False),
    )
    db.session.add(build)
    db.session.commit()
    return build


def get_next_build_number():
    """Get next build number from DB."""
    from app.models import Build
    try:
        last = Build.query.order_by(Build.build_number.desc()).first()
        return (last.build_number + 1) if last else 1
    except Exception:
        return 1


# ── Schedule helpers (still JSON for now) ────────────────────────────────────
def load_schedules():
    """Load schedules from file"""
    global schedules
    if os.path.exists(SCHEDULES_FILE):
        try:
            with open(SCHEDULES_FILE, 'r') as f:
                schedules = json.load(f)
        except:
            schedules = []
    return schedules


def save_schedules():
    """Save schedules to file"""
    with open(SCHEDULES_FILE, 'w') as f:
        json.dump(schedules, f, indent=2)


def get_next_run_time(schedule):
    """Calculate the next run time for a schedule"""
    from datetime import timedelta
    now = datetime.now()

    if schedule['type'] == 'once':
        scheduled_time = datetime.strptime(schedule['scheduled_time'], '%Y-%m-%d %H:%M')
        if scheduled_time > now:
            return scheduled_time.strftime('%Y-%m-%d %H:%M')
        return None

    frequency = schedule.get('frequency', 'daily')
    time_str = schedule.get('time', '06:00')

    if frequency == 'hourly':
        from datetime import timedelta
        next_run = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return next_run.strftime('%Y-%m-%d %H:%M')

    hour, minute = map(int, time_str.split(':'))

    if frequency == 'daily':
        from datetime import timedelta
        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        return next_run.strftime('%Y-%m-%d %H:%M')

    if frequency == 'weekly':
        from datetime import timedelta
        days = schedule.get('days', ['mon'])
        day_map = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}
        target_days = [day_map.get(d, 0) for d in days]
        for i in range(7):
            check_date = now + timedelta(days=i)
            if check_date.weekday() in target_days:
                next_run = check_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if next_run > now:
                    return next_run.strftime('%Y-%m-%d %H:%M')
        return None

    if frequency == 'monthly':
        day_of_month = schedule.get('day_of_month', 1)
        next_run = now.replace(day=day_of_month, hour=hour, minute=minute, second=0, microsecond=0)
        if next_run <= now:
            if now.month == 12:
                next_run = next_run.replace(year=now.year + 1, month=1)
            else:
                next_run = next_run.replace(month=now.month + 1)
        return next_run.strftime('%Y-%m-%d %H:%M')

    return None


def get_cron_display(schedule):
    """Get human-readable cron display"""
    if schedule['type'] == 'once':
        return schedule.get('scheduled_time', 'N/A')

    frequency = schedule.get('frequency', 'daily')
    time_str = schedule.get('time', '06:00')

    if frequency == 'hourly':
        return 'Every hour'
    elif frequency == 'daily':
        return f'Daily at {time_str}'
    elif frequency == 'weekly':
        days = schedule.get('days', ['mon'])
        day_names = {'mon': 'Mon', 'tue': 'Tue', 'wed': 'Wed', 'thu': 'Thu', 'fri': 'Fri', 'sat': 'Sat', 'sun': 'Sun'}
        day_list = ', '.join(day_names.get(d, d) for d in days)
        return f'{day_list} at {time_str}'
    elif frequency == 'monthly':
        day_of_month = schedule.get('day_of_month', 1)
        return f'Day {day_of_month} at {time_str}'
    elif frequency == 'custom':
        return schedule.get('cron', '* * * * *')
    return 'Unknown'


# Load schedules on startup
load_schedules()


# =============================================================================
# ROUTES
# =============================================================================

@dashboard_bp.route('/help')
@login_required
def help_page():
    """Help and documentation page"""
    categories = sorted(set(c['category'] for c in AVAILABLE_CHECKS.values()))
    return render_template('help.html',
                           active_page='help',
                           checks=AVAILABLE_CHECKS,
                           categories=categories,
                           category_icons=CATEGORY_ICONS)


@dashboard_bp.route('/')
@login_required
def dashboard():
    """Main dashboard"""
    load_builds()

    # Get all running builds
    with _jobs_lock:
        running_list = list(running_jobs.values())

    # Filter for "my builds" if requested
    view = request.args.get('view', 'all')
    display_builds = builds
    if view == 'mine' and current_user.is_authenticated:
        display_builds = [b for b in builds if b.get('triggered_by') == current_user.username]

    # Calculate stats
    stats = {
        'total': len(builds) + len(running_list),
        'running': len(running_list),
        'success': sum(1 for b in builds if b.get('status') == 'success'),
        'unstable': sum(1 for b in builds if b.get('status') == 'unstable'),
        'failed': sum(1 for b in builds if b.get('status') == 'failed')
    }

    # Load user templates for sidebar
    from sqlalchemy import or_
    user_templates = [t.to_dict() for t in
                      Template.query.filter(
                          or_(Template.created_by == current_user.id, Template.shared == True)
                      ).order_by(Template.name).all()] if current_user.is_authenticated else []

    return render_template('dashboard.html',
                           builds=display_builds[:10],
                           recent_builds=display_builds[:10],
                           stats=stats,
                           running_builds=running_list,
                           running_build=running_list[0] if running_list else None,
                           queued_count=len(queued_jobs),
                           current_view=view,
                           user_templates=user_templates,
                           active_page='dashboard')


@dashboard_bp.route('/job/configure')
@operator_required
def configure():
    """Build configuration page"""
    categories = sorted(set(c['category'] for c in AVAILABLE_CHECKS.values()))
    preset = request.args.get('preset', '')
    settings = load_settings()
    thresholds = settings.get('thresholds', DEFAULT_THRESHOLDS)
    ssh_config = settings.get('ssh', DEFAULT_SETTINGS['ssh'])

    host_objects = get_hosts_for_user(current_user)
    saved_hosts = [h.to_dict() for h in host_objects]

    cnv_config = settings.get('cnv', _DEFAULT_CNV_SETTINGS)

    from app.models import CustomCheck
    custom_checks = [c.to_dict() for c in
                     CustomCheck.query.filter_by(created_by=current_user.id, enabled=True).order_by(CustomCheck.name).all()]

    # Load user templates (own + shared by others)
    from sqlalchemy import or_
    user_templates = [t.to_dict() for t in
                      Template.query.filter(
                          or_(Template.created_by == current_user.id, Template.shared == True)
                      ).order_by(Template.name).all()]

    # If loading a specific template
    load_template = None
    template_id = request.args.get('template', type=int)
    if template_id:
        tmpl = Template.query.get(template_id)
        if tmpl and (tmpl.created_by == current_user.id or tmpl.shared or current_user.is_admin):
            load_template = tmpl.to_dict()

    return render_template('configure.html',
                           checks=AVAILABLE_CHECKS,
                           categories=categories,
                           category_icons=CATEGORY_ICONS,
                           preset=preset,
                           thresholds=thresholds,
                           agents=AVAILABLE_AGENTS,
                           ssh_config=ssh_config,
                           saved_hosts=saved_hosts,
                           server_host=ssh_config.get('host', ''),
                           cnv_scenarios=CNV_SCENARIOS,
                           cnv_categories=CNV_SCENARIO_CATEGORIES,
                           cnv_category_order=CNV_CATEGORY_ORDER,
                           cnv_global_vars=CNV_GLOBAL_VARIABLES,
                           cnv_config=cnv_config,
                           custom_checks=custom_checks,
                           user_templates=user_templates,
                           load_template=load_template,
                           active_page='configure')


@dashboard_bp.route('/job/run', methods=['POST'])
@operator_required
def run_build():
    """Start a new build or schedule one"""
    import uuid

    task_type = request.form.get('task_type', 'health_check')
    run_name = request.form.get('run_name', '').strip()
    server_host = request.form.get('server_host', '').strip()

    # ── CNV Scenarios task ───────────────────────────────────────────────
    if task_type in ('cnv_scenarios', 'cnv_combined'):
        selected_tests = request.form.getlist('scenario_tests')
        if not selected_tests:
            selected_tests = [s['remote_name'] for s in CNV_SCENARIOS.values() if s.get('default')]

        scenario_mode = request.form.get('scenario_mode', 'sanity')
        scenario_parallel = 'scenario_parallel' in request.form
        cnv_path = request.form.get('cnv_path', '/home/kni/git/cnv-scenarios').strip()

        # Collect env-var overrides from the form
        env_overrides = []
        seen_vars = set()
        for key in request.form:
            if key.startswith('cnv_var_'):
                var_name = key[len('cnv_var_'):]
                if var_name in seen_vars:
                    continue
                seen_vars.add(var_name)
                # For checkboxes (bool), getlist returns ['false','true'] when checked
                values = request.form.getlist(key)
                value = values[-1].strip() if values else ''
                if value:
                    env_overrides.append(f"{var_name}={value}")

        kb_log_level = request.form.get('kb_log_level', '').strip()
        kb_timeout = request.form.get('kb_timeout', '').strip()

        # For combined runs: force cleanup=false in env vars so resources
        # stay on the cluster for the health check, then cleanup later.
        combined_cleanup = False
        if task_type == 'cnv_combined':
            combined_cleanup = 'combined_cleanup' in request.form
            # Strip any existing cleanup override and force false
            env_overrides = [e for e in env_overrides if not e.startswith('cleanup=')]
            env_overrides.append('cleanup=false')

        options = {
            'task_type': task_type,
            'server_host': server_host,
            'run_name': run_name,
            'scenario_tests': selected_tests,
            'scenario_mode': scenario_mode,
            'scenario_parallel': scenario_parallel,
            'cnv_path': cnv_path,
            'env_vars': ','.join(env_overrides) if env_overrides else '',
            'kb_log_level': kb_log_level,
            'kb_timeout': kb_timeout,
            'email': 'cnv_send_email' in request.form,
            'email_to': request.form.get('cnv_email_to', Config.DEFAULT_EMAIL),
            'scenario_custom_checks': [int(x) for x in request.form.getlist('scenario_custom_checks')],
        }

        if task_type == 'cnv_combined':
            options['combined_cleanup'] = combined_cleanup

            # ── Collect health-check options for the combined run ─────────
            options['rca_level'] = request.form.get('rca_level', 'none')
            options['rca_jira'] = 'rca_jira' in request.form
            options['rca_email'] = 'rca_email' in request.form
            options['rca_web'] = 'rca_web' in request.form
            options['jira'] = 'check_jira' in request.form

            # Health-check email (separate from CNV email)
            if 'send_email' in request.form:
                options['email'] = True
                options['email_to'] = request.form.get('email_to', Config.DEFAULT_EMAIL)

            options['hc_checks'] = request.form.getlist('checks')
            options['hc_custom_checks'] = [int(x) for x in request.form.getlist('custom_checks')]

            # Thresholds
            current_thresholds = get_thresholds()
            use_custom = 'use_custom_thresholds' in request.form
            options['thresholds'] = {
                'cpu_warning': int(request.form.get('cpu_threshold', current_thresholds['cpu_warning'])) if use_custom else current_thresholds['cpu_warning'],
                'memory_warning': int(request.form.get('memory_threshold', current_thresholds['memory_warning'])) if use_custom else current_thresholds['memory_warning'],
                'disk_latency': int(request.form.get('disk_latency_threshold', current_thresholds['disk_latency'])) if use_custom else current_thresholds['disk_latency'],
                'etcd_latency': int(request.form.get('etcd_latency_threshold', current_thresholds['etcd_latency'])) if use_custom else current_thresholds['etcd_latency'],
                'pod_density': int(request.form.get('pod_density_threshold', current_thresholds['pod_density'])) if use_custom else current_thresholds['pod_density'],
                'restart_count': int(request.form.get('restart_threshold', current_thresholds['restart_count'])) if use_custom else current_thresholds['restart_count'],
            }

        schedule_type = request.form.get('schedule_type', 'now')
        if schedule_type == 'now':
            user_id = current_user.id if current_user.is_authenticated else None
            build_num = start_build(selected_tests, options, user_id=user_id)
            return redirect(url_for('dashboard.console_output', build_num=build_num))

        # Fall through to scheduling code below (reuses same schedule logic)
        selected_checks = selected_tests

    # ── Health Check task (default) ──────────────────────────────────────
    else:
        selected_checks = request.form.getlist('checks')
        if not selected_checks:
            selected_checks = list(AVAILABLE_CHECKS.keys())

        rca_level = request.form.get('rca_level', 'none')

        current_thresholds = get_thresholds()
        use_custom = 'use_custom_thresholds' in request.form

        thresholds = {
            'cpu_warning': int(request.form.get('cpu_threshold', current_thresholds['cpu_warning'])) if use_custom else current_thresholds['cpu_warning'],
            'memory_warning': int(request.form.get('memory_threshold', current_thresholds['memory_warning'])) if use_custom else current_thresholds['memory_warning'],
            'disk_latency': int(request.form.get('disk_latency_threshold', current_thresholds['disk_latency'])) if use_custom else current_thresholds['disk_latency'],
            'etcd_latency': int(request.form.get('etcd_latency_threshold', current_thresholds['etcd_latency'])) if use_custom else current_thresholds['etcd_latency'],
            'pod_density': int(request.form.get('pod_density_threshold', current_thresholds['pod_density'])) if use_custom else current_thresholds['pod_density'],
            'restart_count': int(request.form.get('restart_threshold', current_thresholds['restart_count'])) if use_custom else current_thresholds['restart_count'],
        }

        selected_agent = request.form.get('agent', 'all')

        options = {
            'task_type': 'health_check',
            'server_host': server_host,
            'rca_level': rca_level,
            'rca_jira': 'rca_jira' in request.form,
            'rca_email': 'rca_email' in request.form,
            'rca_web': 'rca_web' in request.form,
            'jira': 'check_jira' in request.form,
            'email': 'send_email' in request.form,
            'email_to': request.form.get('email_to', Config.DEFAULT_EMAIL),
            'run_name': run_name,
            'thresholds': thresholds,
            'agent': selected_agent,
            'custom_checks': [int(x) for x in request.form.getlist('custom_checks')],
        }

    schedule_type = request.form.get('schedule_type', 'now')

    if schedule_type == 'now':
        user_id = current_user.id if current_user.is_authenticated else None
        build_num = start_build(selected_checks, options, user_id=user_id)
        return redirect(url_for('dashboard.console_output', build_num=build_num))

    elif schedule_type == 'once':
        schedule_date = request.form.get('schedule_date', '')
        schedule_time = request.form.get('schedule_time', '')
        if schedule_date and schedule_time:
            scheduled_time = f"{schedule_date} {schedule_time}"
            schedule = {
                'id': str(uuid.uuid4())[:8],
                'name': f"Scheduled Check ({scheduled_time})",
                'type': 'once',
                'scheduled_time': scheduled_time,
                'checks': selected_checks,
                'checks_count': len(selected_checks),
                'options': options,
                'status': 'active',
                'created': datetime.now().strftime('%Y-%m-%d %H:%M'),
                'created_by': current_user.username if current_user.is_authenticated else 'system',
                'last_run': None
            }
            schedules.append(schedule)
            save_schedules()
            return redirect(url_for('dashboard.schedules_page'))

    elif schedule_type == 'recurring':
        frequency = request.form.get('recurring_frequency', 'daily')
        schedule_name = request.form.get('schedule_name', '').strip() or f"Recurring Health Check ({frequency})"
        recurring_time = request.form.get('recurring_time', '06:00')

        schedule = {
            'id': str(uuid.uuid4())[:8],
            'name': schedule_name,
            'type': 'recurring',
            'frequency': frequency,
            'time': recurring_time,
            'checks': selected_checks,
            'checks_count': len(selected_checks),
            'options': options,
            'status': 'active',
            'created': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'created_by': current_user.username if current_user.is_authenticated else 'system',
            'last_run': None
        }

        if frequency == 'weekly':
            days = request.form.getlist('recurring_days')
            schedule['days'] = days if days else ['mon']
        elif frequency == 'monthly':
            day_of_month = request.form.get('recurring_dayofmonth', '1')
            schedule['day_of_month'] = int(day_of_month) if day_of_month.isdigit() else 1
        elif frequency == 'custom':
            cron_expr = request.form.get('recurring_cron', '0 6 * * *')
            schedule['cron'] = cron_expr

        schedules.append(schedule)
        save_schedules()
        return redirect(url_for('dashboard.schedules_page'))

    user_id = current_user.id if current_user.is_authenticated else None
    build_num = start_build(selected_checks, options, user_id=user_id)
    return redirect(url_for('dashboard.console_output', build_num=build_num))


@dashboard_bp.route('/job/quick-run')
@operator_required
def quick_run():
    """Quick build - redirect to configure with all checks selected"""
    return redirect(url_for('dashboard.configure') + '?preset=all')


@dashboard_bp.route('/job/quick-sanity')
@operator_required
def quick_sanity():
    """Quick sanity - redirect to configure with CNV sanity mode pre-selected"""
    return redirect(url_for('dashboard.configure') + '?preset=cnv_sanity')


@dashboard_bp.route('/job/quick-full')
@operator_required
def quick_full():
    """Full CNV scenarios - redirect to configure with full mode pre-selected and 4.21.0 defaults"""
    return redirect(url_for('dashboard.configure') + '?preset=cnv_full')


@dashboard_bp.route('/job/quick-10k')
@operator_required
def quick_10k():
    """Create 10K VMs - per-host density preset optimized for create-only at scale"""
    return redirect(url_for('dashboard.configure') + '?preset=10k_density')


# ── Template CRUD API ────────────────────────────────────────────────────

@dashboard_bp.route('/api/templates', methods=['GET'])
@login_required
def api_templates_list():
    """List templates visible to current user (own + shared)."""
    from sqlalchemy import or_
    templates = Template.query.filter(
        or_(Template.created_by == current_user.id, Template.shared == True)
    ).order_by(Template.updated_at.desc()).all()
    return jsonify([t.to_dict() for t in templates])


@dashboard_bp.route('/api/templates', methods=['POST'])
@operator_required
def api_templates_create():
    """Create a new template from JSON body."""
    data = request.get_json(silent=True)
    if not data or not data.get('name') or not data.get('config'):
        return jsonify({'error': 'name and config are required'}), 400

    tmpl = Template(
        name=data['name'][:200],
        description=(data.get('description') or '')[:500],
        icon=data.get('icon', '📋')[:10],
        created_by=current_user.id,
        shared=bool(data.get('shared', False)),
        config=data['config'],
    )
    db.session.add(tmpl)
    db.session.commit()
    return jsonify(tmpl.to_dict()), 201


@dashboard_bp.route('/api/templates/<int:tmpl_id>', methods=['PUT'])
@operator_required
def api_templates_update(tmpl_id):
    """Update an existing template (owner or admin only)."""
    tmpl = Template.query.get_or_404(tmpl_id)
    if tmpl.created_by != current_user.id and not current_user.is_admin:
        return jsonify({'error': 'forbidden'}), 403

    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'invalid JSON'}), 400

    if 'name' in data:
        tmpl.name = data['name'][:200]
    if 'description' in data:
        tmpl.description = (data['description'] or '')[:500]
    if 'icon' in data:
        tmpl.icon = data['icon'][:10]
    if 'shared' in data:
        tmpl.shared = bool(data['shared'])
    if 'config' in data:
        tmpl.config = data['config']

    db.session.commit()
    return jsonify(tmpl.to_dict())


@dashboard_bp.route('/api/templates/<int:tmpl_id>', methods=['DELETE'])
@operator_required
def api_templates_delete(tmpl_id):
    """Delete a template (owner or admin only)."""
    tmpl = Template.query.get_or_404(tmpl_id)
    if tmpl.created_by != current_user.id and not current_user.is_admin:
        return jsonify({'error': 'forbidden'}), 403

    db.session.delete(tmpl)
    db.session.commit()
    return jsonify({'ok': True})


@dashboard_bp.route('/api/templates/from-build/<int:build_num>', methods=['POST'])
@operator_required
def api_templates_from_build(build_num):
    """Create a template from a past build's options."""
    from app.models import Build
    build = Build.query.filter_by(build_number=build_num).first_or_404()

    data = request.get_json(silent=True) or {}
    name = data.get('name', f'From Build #{build_num}')[:200]
    description = data.get('description', f'Saved from build #{build_num}')[:500]
    icon = data.get('icon', '📋')[:10]
    shared = bool(data.get('shared', False))

    config = build.options or {}
    # Also store the checks/tests list in config for full reproducibility
    if build.checks:
        config['_checks'] = build.checks

    tmpl = Template(
        name=name,
        description=description,
        icon=icon,
        created_by=current_user.id,
        shared=shared,
        config=config,
    )
    db.session.add(tmpl)
    db.session.commit()
    return jsonify(tmpl.to_dict()), 201


@dashboard_bp.route('/job/history')
@login_required
def history():
    """Build history page"""
    load_builds()
    status_filter = request.args.get('status')
    view = request.args.get('view', 'all')

    filtered_builds = builds
    if view == 'mine' and current_user.is_authenticated:
        filtered_builds = [b for b in filtered_builds if b.get('triggered_by') == current_user.username]
    if status_filter:
        filtered_builds = [b for b in filtered_builds if b.get('status') == status_filter]

    return render_template('history.html',
                           builds=filtered_builds,
                           current_view=view,
                           active_page='history')


@dashboard_bp.route('/schedules')
@login_required
def schedules_page():
    """Scheduled tasks page"""
    load_schedules()
    status_filter = request.args.get('status')

    for schedule in schedules:
        schedule['next_run'] = get_next_run_time(schedule)
        schedule['cron_display'] = get_cron_display(schedule)

    filtered_schedules = schedules
    if status_filter:
        filtered_schedules = [s for s in schedules if s.get('status') == status_filter]

    scheduler_status = {
        'active_schedules': sum(1 for s in schedules if s.get('status') == 'active'),
        'runs_today': 0,
        'next_run': min((s.get('next_run') for s in schedules if s.get('status') == 'active' and s.get('next_run')), default=None)
    }

    return render_template('schedules.html',
                           schedules=filtered_schedules,
                           scheduler_status=scheduler_status,
                           active_page='schedules')


@dashboard_bp.route('/job/<int:build_num>')
@login_required
def build_detail(build_num):
    """Build detail page"""
    load_builds()
    build = next((b for b in builds if b.get('number') == build_num), None)

    if not build:
        with _jobs_lock:
            for job_id, job in running_jobs.items():
                if job.get('number') == build_num:
                    build = job
                    break

    if not build:
        return "Build not found", 404

    # Build CNV scenario metadata lookup (remote_name -> display info)
    cnv_meta = {}
    for sid, sc in CNV_SCENARIOS.items():
        cnv_meta[sc['remote_name']] = {
            'name': sc['name'],
            'icon': sc['icon'],
            'category': sc.get('category', ''),
            'description': sc.get('description', ''),
        }

    return render_template('build_detail.html',
                           build=build,
                           checks=AVAILABLE_CHECKS,
                           cnv_meta=cnv_meta,
                           user_templates=[],
                           active_page='history')


@dashboard_bp.route('/job/<int:build_num>/console')
@login_required
def console_output(build_num):
    """Console output page"""
    load_builds()
    build = next((b for b in builds if b.get('number') == build_num), None)

    if not build:
        with _jobs_lock:
            for job_id, job in running_jobs.items():
                if job.get('number') == build_num:
                    build = job
                    break

    if not build:
        return "Build not found", 404

    return render_template('console.html', build=build, active_page='history')


@dashboard_bp.route('/job/rebuild/<int:build_num>')
@operator_required
def rebuild(build_num):
    """Rebuild with same parameters"""
    load_builds()
    build = next((b for b in builds if b.get('number') == build_num), None)

    if build:
        checks = build.get('checks', list(AVAILABLE_CHECKS.keys()))
        options = build.get('options', {'rca_level': 'none', 'jira': False, 'email': False})
        user_id = current_user.id if current_user.is_authenticated else None
        new_build_num = start_build(checks, options, user_id=user_id)
        return redirect(url_for('dashboard.console_output', build_num=new_build_num))

    return redirect(url_for('dashboard.dashboard'))


@dashboard_bp.route('/report/<filename>')
@login_required
def serve_report(filename):
    """Serve report files"""
    return send_from_directory(REPORTS_DIR, filename)


# =============================================================================
# API ENDPOINTS
# =============================================================================

@dashboard_bp.route('/api/status')
@login_required
def api_status():
    """API endpoint for build status - returns all running builds."""
    with _jobs_lock:
        if running_jobs:
            # Return info about all running builds
            all_running = []
            for job_id, job in running_jobs.items():
                all_running.append({
                    'job_id': job_id,
                    'number': job.get('number'),
                    'name': job.get('name', ''),
                    'output': job.get('output', ''),
                    'progress': job.get('progress', 0),
                    'phases': job.get('phases', []),
                    'current_phase': job.get('current_phase', ''),
                    'start_time': job.get('start_time', 0),
                    'triggered_by': job.get('triggered_by', 'system'),
                })

            # For backward compatibility, also return first build's data at top level
            first = all_running[0] if all_running else {}
            return jsonify({
                'running': True,
                'builds': all_running,
                'queued': len(queued_jobs),
                'output': first.get('output', ''),
                'progress': first.get('progress', 0),
                'phases': first.get('phases', []),
                'current_phase': first.get('current_phase', ''),
                'start_time': first.get('start_time', 0),
            })
    return jsonify({'running': False, 'queued': len(queued_jobs)})


@dashboard_bp.route('/api/test-progress/<int:build_num>')
@login_required
def api_test_progress(build_num):
    """API endpoint for per-test live progress of a running build."""
    with _jobs_lock:
        for job_id, job in running_jobs.items():
            if job.get('number') == build_num:
                tp = job.get('test_progress', {})
                # For running tests, compute elapsed time
                now = time.time()
                result = {}
                for tname, info in tp.items():
                    entry = dict(info)
                    if entry['status'] == 'running' and entry.get('start_time'):
                        elapsed = int(now - entry['start_time'])
                        entry['elapsed'] = f"{elapsed // 60}m {elapsed % 60}s"
                    result[tname] = entry
                return jsonify({
                    'running': True,
                    'build_num': build_num,
                    'test_progress': result,
                    'current_phase': job.get('current_phase', ''),
                    'progress': job.get('progress', 0),
                })
    # Not running — check completed builds
    load_builds()
    build = next((b for b in builds if b.get('number') == build_num), None)
    if build:
        return jsonify({'running': False, 'build_num': build_num, 'status': build.get('status', 'unknown')})
    return jsonify({'running': False, 'build_num': build_num, 'status': 'not_found'}), 404


@dashboard_bp.route('/api/stop', methods=['POST'])
@operator_required
def api_stop():
    """API endpoint to stop a running build."""
    data = request.get_json(silent=True) or {}
    target_job_id = data.get('job_id')

    with _jobs_lock:
        if not running_jobs:
            return jsonify({'success': False, 'error': 'No running build'})

        # If no specific job_id, stop the first one (backward compat)
        if not target_job_id:
            target_job_id = list(running_jobs.keys())[0]

        job = running_jobs.get(target_job_id)
        if not job:
            return jsonify({'success': False, 'error': 'Build not found'})

        # Only owner or admin can stop
        if not current_user.is_admin and job.get('user_id') != current_user.id:
            return jsonify({'success': False, 'error': 'You can only stop your own builds.'})

    try:
        process = job.get('process')
        if process and process.poll() is None:
            try:
                pgid = os.getpgid(process.pid)
                os.killpg(pgid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(pgid, signal.SIGKILL)
                    process.wait(timeout=2)
            except (ProcessLookupError, OSError):
                pass

        job['output'] += f'\n[{datetime.now().strftime("%H:%M:%S")}] ⛔ Build stopped by {current_user.username}\n'
        job['current_phase'] = f'Stopped by {current_user.username}'

        for phase in job.get('phases', []):
            if phase['status'] == 'running':
                phase['status'] = 'error'

        duration_secs = int(time.time() - job['start_time'])
        duration = f"{duration_secs // 60}m {duration_secs % 60}s"

        build_record = {
            'number': job['number'],
            'name': job.get('name', ''),
            'status': 'failed',
            'status_text': 'Stopped',
            'checks': job.get('checks', []),
            'checks_count': job.get('checks_count', 0),
            'options': job.get('options', {}),
            'timestamp': job['timestamp'],
            'duration': duration,
            'output': job['output'],
            'report_file': None
        }

        save_build_to_db(build_record, user_id=job.get('user_id'))

        with _jobs_lock:
            if target_job_id in running_jobs:
                del running_jobs[target_job_id]

        log_audit('build_stop', target=f'Build #{job["number"]}',
                  details=f'Stopped by {current_user.username}')

        # Start next queued build if any
        _start_next_queued()

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@dashboard_bp.route('/api/delete/<int:build_num>', methods=['POST'])
@operator_required
def api_delete(build_num):
    """API endpoint to delete a build and its report"""
    from app.models import db, Build
    try:
        build = Build.query.filter_by(build_number=build_num).first()
        if not build:
            return jsonify({'success': False, 'error': 'Build not found'})

        # Only owner or admin can delete
        if not current_user.is_admin and build.triggered_by != current_user.id:
            return jsonify({'success': False, 'error': 'You can only delete your own builds.'})

        report_file = build.report_file
        if report_file:
            report_path = os.path.join(REPORTS_DIR, report_file)
            if os.path.exists(report_path):
                os.remove(report_path)
            md_file = report_file.replace('.html', '.md')
            md_path = os.path.join(REPORTS_DIR, md_file)
            if os.path.exists(md_path):
                os.remove(md_path)

        db.session.delete(build)
        db.session.commit()

        log_audit('build_delete', target=f'Build #{build_num}')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@dashboard_bp.route('/api/delete-bulk', methods=['POST'])
@admin_required
def api_delete_bulk():
    """API endpoint to delete multiple builds by status filter"""
    from app.models import db, Build
    try:
        data = request.get_json() or {}
        filter_type = data.get('filter', 'all')

        if filter_type == 'all':
            query = Build.query
        elif filter_type == 'failed':
            query = Build.query.filter_by(status='failed')
        elif filter_type == 'stopped':
            query = Build.query.filter_by(status_text='Stopped')
        else:
            return jsonify({'success': False, 'error': 'Invalid filter type'})

        builds_to_delete = query.all()
        deleted_count = 0

        for build in builds_to_delete:
            report_file = build.report_file
            if report_file:
                report_path = os.path.join(REPORTS_DIR, report_file)
                if os.path.exists(report_path):
                    os.remove(report_path)
                md_file = report_file.replace('.html', '.md')
                md_path = os.path.join(REPORTS_DIR, md_file)
                if os.path.exists(md_path):
                    os.remove(md_path)
            db.session.delete(build)
            deleted_count += 1

        db.session.commit()
        log_audit('build_bulk_delete', details=f'Deleted {deleted_count} builds (filter: {filter_type})')
        return jsonify({'success': True, 'deleted': deleted_count})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# =============================================================================
# SCHEDULE API ENDPOINTS
# =============================================================================

@dashboard_bp.route('/api/schedules')
@login_required
def api_get_schedules():
    """API endpoint to get all schedules"""
    load_schedules()
    for schedule in schedules:
        schedule['next_run'] = get_next_run_time(schedule)
        schedule['cron_display'] = get_cron_display(schedule)
    return jsonify({'success': True, 'schedules': schedules})


@dashboard_bp.route('/api/schedule', methods=['POST'])
@operator_required
def api_create_schedule():
    """API endpoint to create a new schedule"""
    import uuid
    try:
        data = request.get_json() or {}
        schedule = {
            'id': str(uuid.uuid4())[:8],
            'name': data.get('name', 'Unnamed Schedule'),
            'type': data.get('type', 'recurring'),
            'frequency': data.get('frequency', 'daily'),
            'time': data.get('time', '06:00'),
            'checks': data.get('checks', list(AVAILABLE_CHECKS.keys())),
            'checks_count': len(data.get('checks', AVAILABLE_CHECKS)),
            'options': data.get('options', {'rca_level': 'none'}),
            'status': 'active',
            'created': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'created_by': current_user.username if current_user.is_authenticated else 'system',
            'last_run': None
        }
        if schedule['type'] == 'once':
            schedule['scheduled_time'] = data.get('scheduled_time', '')
        elif schedule['frequency'] == 'weekly':
            schedule['days'] = data.get('days', ['mon'])
        elif schedule['frequency'] == 'monthly':
            schedule['day_of_month'] = data.get('day_of_month', 1)
        elif schedule['frequency'] == 'custom':
            schedule['cron'] = data.get('cron', '0 6 * * *')

        schedules.append(schedule)
        save_schedules()
        return jsonify({'success': True, 'schedule': schedule})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@dashboard_bp.route('/api/schedule/<schedule_id>/<action>', methods=['POST'])
@operator_required
def api_schedule_action(schedule_id, action):
    """API endpoint to pause/resume a schedule"""
    load_schedules()
    try:
        schedule = next((s for s in schedules if s.get('id') == schedule_id), None)
        if not schedule:
            return jsonify({'success': False, 'error': 'Schedule not found'})
        if action == 'pause':
            schedule['status'] = 'paused'
        elif action == 'resume':
            schedule['status'] = 'active'
        else:
            return jsonify({'success': False, 'error': 'Invalid action'})
        save_schedules()
        return jsonify({'success': True, 'status': schedule['status']})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@dashboard_bp.route('/api/schedule/<schedule_id>/run', methods=['POST'])
@operator_required
def api_schedule_run(schedule_id):
    """API endpoint to run a schedule immediately"""
    load_schedules()
    try:
        schedule = next((s for s in schedules if s.get('id') == schedule_id), None)
        if not schedule:
            return jsonify({'success': False, 'error': 'Schedule not found'})

        checks = schedule.get('checks', list(AVAILABLE_CHECKS.keys()))
        options = schedule.get('options', {'rca_level': 'none'})
        options['scheduled'] = True
        options['schedule_id'] = schedule_id
        options['schedule_name'] = schedule.get('name', 'Scheduled')

        user_id = current_user.id if current_user.is_authenticated else None
        start_build(checks, options, user_id=user_id)

        schedule['last_run'] = datetime.now().strftime('%Y-%m-%d %H:%M')
        save_schedules()

        return jsonify({'success': True, 'message': 'Build started'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@dashboard_bp.route('/api/schedule/<schedule_id>', methods=['DELETE'])
@operator_required
def api_schedule_delete(schedule_id):
    """API endpoint to delete a schedule"""
    global schedules
    load_schedules()
    try:
        schedule = next((s for s in schedules if s.get('id') == schedule_id), None)
        if not schedule:
            return jsonify({'success': False, 'error': 'Schedule not found'})
        schedules = [s for s in schedules if s.get('id') != schedule_id]
        save_schedules()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# =============================================================================
# JIRA INTEGRATION API ENDPOINTS
# =============================================================================

SUGGESTED_CHECKS_FILE = os.path.join(BASE_DIR, ".suggested_checks.json")
suggested_checks = []


def load_suggested_checks():
    global suggested_checks
    if os.path.exists(SUGGESTED_CHECKS_FILE):
        try:
            with open(SUGGESTED_CHECKS_FILE, 'r') as f:
                suggested_checks = json.load(f)
        except Exception:
            suggested_checks = []
    return suggested_checks


def _restore_accepted_checks():
    """Re-add previously accepted Jira suggestions to AVAILABLE_CHECKS.

    Called once at import time so accepted checks survive server restarts.
    """
    checks = load_suggested_checks()
    restored = 0
    for sc in checks:
        if sc.get('status') != 'accepted':
            continue
        name = sc.get('name', '')
        if not name or name in AVAILABLE_CHECKS:
            continue
        AVAILABLE_CHECKS[name] = {
            'name': name.replace('_', ' ').title(),
            'description': sc.get('description', ''),
            'category': sc.get('category', 'Custom'),
            'default': True,
            'jira': sc.get('jira_key', ''),
            'custom': True,
        }
        restored += 1
    if restored:
        print(f"  [Knowledge] Restored {restored} accepted Jira check(s) into AVAILABLE_CHECKS")


_restore_accepted_checks()


def save_suggested_checks():
    with open(SUGGESTED_CHECKS_FILE, 'w') as f:
        json.dump(suggested_checks, f, indent=2)


@dashboard_bp.route('/api/jira/suggestions')
@login_required
def api_jira_suggestions():
    """API endpoint to get Jira-based test suggestions"""
    try:
        sys.path.insert(0, BASE_DIR)
        from healthchecks.hybrid_health_check import (
            get_known_recent_bugs,
            get_existing_check_names,
            analyze_bugs_for_new_checks,
            search_jira_for_new_bugs
        )
        existing_checks = get_existing_check_names()
        load_suggested_checks()
        accepted_checks = {s['name'] for s in suggested_checks if s.get('status') == 'accepted'}
        existing_checks.extend(list(accepted_checks))

        try:
            bugs = search_jira_for_new_bugs(days=30, limit=50)
        except:
            bugs = None
        if not bugs:
            bugs = get_known_recent_bugs()

        suggestions = analyze_bugs_for_new_checks(bugs, existing_checks)
        rejected_recently = {
            s['name'] for s in suggested_checks
            if s.get('status') == 'rejected' and s.get('rejected_at')
        }
        suggestions = [s for s in suggestions if s['suggested_check'] not in rejected_recently]

        # Enrich suggestions with command info
        from healthchecks.hybrid_health_check import generate_check_code
        for s in suggestions:
            check_code = generate_check_code(s)
            s['command'] = check_code.get('command', '')

        return jsonify({
            'success': True,
            'suggestions': suggestions,
            'count': len(suggestions),
            'bugs_analyzed': len(bugs)
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'suggestions': []})


@dashboard_bp.route('/api/jira/accept-check', methods=['POST'])
@operator_required
def api_jira_accept_check():
    global suggested_checks
    load_suggested_checks()
    try:
        data = request.get_json() or {}
        check_name = data.get('name', '')
        jira_key = data.get('jira_key', '')
        description = data.get('description', '')
        category = data.get('category', 'Custom')
        if not check_name:
            return jsonify({'success': False, 'error': 'Check name is required'})

        check_record = {
            'name': check_name, 'jira_key': jira_key, 'description': description,
            'category': category, 'status': 'accepted',
            'accepted_at': datetime.now().strftime('%Y-%m-%d %H:%M')
        }
        existing = next((s for s in suggested_checks if s['name'] == check_name), None)
        if existing:
            existing.update(check_record)
        else:
            suggested_checks.append(check_record)
        save_suggested_checks()

        AVAILABLE_CHECKS[check_name] = {
            'name': check_name.replace('_', ' ').title(),
            'description': description, 'category': category,
            'default': True, 'jira': jira_key, 'custom': True
        }

        # Also write into the dynamic knowledge base so the RCA pattern
        # engine matches this issue on subsequent runs.
        try:
            from healthchecks.knowledge_base import save_known_issue, pattern_exists
            keywords = [w for w in check_name.replace('_', ' ').lower().split() if len(w) > 2]
            if not pattern_exists(keywords):
                kb_key = f"jira-{check_name}"
                save_known_issue(kb_key, {
                    'pattern': keywords,
                    'jira': [jira_key] if jira_key else [],
                    'title': check_name.replace('_', ' ').title(),
                    'description': description,
                    'root_cause': [f'Related to {jira_key}'] if jira_key else [],
                    'suggestions': [f'See {jira_key} for details'] if jira_key else [],
                    'verify_cmd': '',
                    'source': 'jira-scan',
                    'confidence': 0.7,
                    'created': datetime.now().isoformat(),
                    'last_matched': None,
                    'investigation_commands': [],
                })
        except Exception:
            pass

        return jsonify({'success': True, 'message': f'Check "{check_name}" added successfully', 'check': check_record})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@dashboard_bp.route('/api/jira/reject-check', methods=['POST'])
@operator_required
def api_jira_reject_check():
    global suggested_checks
    load_suggested_checks()
    try:
        data = request.get_json() or {}
        check_name = data.get('name', '')
        if not check_name:
            return jsonify({'success': False, 'error': 'Check name is required'})

        check_record = {'name': check_name, 'status': 'rejected', 'rejected_at': datetime.now().strftime('%Y-%m-%d %H:%M')}
        existing = next((s for s in suggested_checks if s['name'] == check_name), None)
        if existing:
            existing.update(check_record)
        else:
            suggested_checks.append(check_record)
        save_suggested_checks()
        return jsonify({'success': True, 'message': f'Check "{check_name}" rejected'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@dashboard_bp.route('/api/jira/accepted-checks')
@login_required
def api_jira_accepted_checks():
    load_suggested_checks()
    accepted = [s for s in suggested_checks if s.get('status') == 'accepted']
    return jsonify({'success': True, 'checks': accepted, 'count': len(accepted)})


# =============================================================================
# LEARNING & PATTERNS API ENDPOINTS
# =============================================================================

@dashboard_bp.route('/api/learning/stats')
@login_required
def api_learning_stats():
    try:
        from app.learning import get_learning_stats, get_issue_trends, get_recurring_issues
        stats = get_learning_stats()
        trends = get_issue_trends(days=7)
        recurring = get_recurring_issues(min_count=2)
        return jsonify({'success': True, 'stats': stats, 'trends': trends, 'recurring_count': len(recurring)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@dashboard_bp.route('/api/learning/patterns')
@login_required
def api_learning_patterns():
    try:
        from app.learning import get_learned_patterns
        patterns = get_learned_patterns()
        return jsonify({'success': True, 'patterns': patterns, 'count': len(patterns)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@dashboard_bp.route('/api/learning/recurring')
@login_required
def api_learning_recurring():
    try:
        from app.learning import get_recurring_issues
        min_count = request.args.get('min_count', 2, type=int)
        recurring = get_recurring_issues(min_count=min_count)
        sorted_recurring = dict(sorted(recurring.items(), key=lambda x: -x[1]['count']))
        return jsonify({'success': True, 'recurring_issues': sorted_recurring, 'count': len(sorted_recurring)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@dashboard_bp.route('/api/learning/trends')
@login_required
def api_learning_trends():
    try:
        from app.learning import get_issue_trends
        days = request.args.get('days', 7, type=int)
        trends = get_issue_trends(days=days)
        return jsonify({'success': True, 'trends': trends})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# =============================================================================
# BUILD EXECUTION (with concurrent build support)
# =============================================================================

def extract_issues_from_output(output):
    """Extract detected issues from health check output for learning."""
    import re
    issues = []
    pod_pattern = r'[❌⚠️]\s*(\S+)/(\S+)\s+(\S+.*?)(?:\n|$)'
    for match in re.finditer(pod_pattern, output):
        issues.append({'type': 'pod', 'namespace': match.group(1), 'name': match.group(2), 'status': match.group(3).strip()})
    operator_pattern = r'[❌⚠️]\s*([\w-]+)\s+(Degraded|Unavailable|Not Available)'
    for match in re.finditer(operator_pattern, output, re.IGNORECASE):
        issues.append({'type': 'operator', 'name': match.group(1), 'status': match.group(2)})
    migration_pattern = r'migration.*?(failed|stuck|error)'
    for match in re.finditer(migration_pattern, output, re.IGNORECASE):
        issues.append({'type': 'migration', 'name': 'vm-migration', 'status': match.group(1)})
    storage_pattern = r'(pvc|volume|storage|odf).*?(pending|failed|error|not ready)'
    for match in re.finditer(storage_pattern, output, re.IGNORECASE):
        issues.append({'type': 'storage', 'name': match.group(1), 'status': match.group(2)})
    node_pattern = r'node[s]?\s+(\S+)\s+(NotReady|SchedulingDisabled)'
    for match in re.finditer(node_pattern, output, re.IGNORECASE):
        issues.append({'type': 'node', 'name': match.group(1), 'status': match.group(2)})
    if 'OOMKilled' in output or 'oom' in output.lower():
        issues.append({'type': 'resource', 'name': 'oom-event', 'status': 'OOMKilled'})

    seen = set()
    unique_issues = []
    for issue in issues:
        key = (issue['type'], issue.get('name', ''), issue.get('namespace', ''))
        if key not in seen:
            seen.add(key)
            unique_issues.append(issue)
    return unique_issues


def _start_next_queued():
    """Start the next queued build if a slot is available. Must NOT hold _jobs_lock."""
    with _jobs_lock:
        if len(running_jobs) >= MAX_CONCURRENT or not queued_jobs:
            return
        job_id, checks, options, user_id = queued_jobs.pop(0)

    _execute_build(job_id, checks, options, user_id=user_id)


def start_build(checks, options, user_id=None):
    """Start a new build (or queue it if at capacity)."""
    build_num = get_next_build_number()
    job_id = f"build_{build_num}"

    # Resolve username for display
    username = 'system'
    if user_id:
        from app.models import User
        user = User.query.get(user_id)
        if user:
            username = user.username

    with _jobs_lock:
        if len(running_jobs) >= MAX_CONCURRENT:
            queued_jobs.append((job_id, checks, options, user_id))
            return build_num

    _execute_build(job_id, checks, options, user_id=user_id)
    return build_num


def _execute_build(job_id, checks, options, user_id=None):
    """Actually run the build in a background thread."""
    build_num = int(job_id.split('_')[1])

    # Resolve username
    username = 'system'
    if user_id:
        try:
            from app.models import User
            user = User.query.get(user_id)
            if user:
                username = user.username
        except Exception:
            pass

    is_cnv = options.get('task_type') == 'cnv_scenarios'
    is_combined = options.get('task_type') == 'cnv_combined'

    # ── Build the command and phase list based on task type ───────────────
    if is_cnv or is_combined:
        cmd = [sys.executable, CNV_SCRIPT_PATH]
        server_host = options.get('server_host', '')
        if server_host:
            cmd.extend(['--server', server_host])
            host_obj = Host.query.filter_by(host=server_host).first()
            if host_obj and host_obj.name:
                clean_name = re.sub(r'\s*\[.*?\]\s*$', '', host_obj.name).strip() or host_obj.host
                cmd.extend(['--lab-name', clean_name])

        scenario_tests = options.get('scenario_tests', [])
        tests_str = ','.join(scenario_tests) if scenario_tests else 'all'
        cmd.extend(['--tests', tests_str])
        cmd.extend(['--mode', options.get('scenario_mode', 'sanity')])
        if options.get('scenario_parallel'):
            cmd.append('--parallel')
        if options.get('cnv_path'):
            cmd.extend(['--cnv-path', options['cnv_path']])
        if options.get('env_vars'):
            cmd.extend(['--env-vars', options['env_vars']])
        if options.get('kb_log_level'):
            cmd.extend(['--log-level', options['kb_log_level']])
        if options.get('kb_timeout'):
            cmd.extend(['--timeout', options['kb_timeout']])

        if is_combined:
            rca_level = options.get('rca_level', 'none')
            phases = [
                {'name': 'Initialize', 'status': 'pending', 'start_time': None, 'duration': None},
                {'name': 'Connect', 'status': 'pending', 'start_time': None, 'duration': None},
                {'name': 'Verify Setup', 'status': 'pending', 'start_time': None, 'duration': None},
                {'name': 'Run Scenarios', 'status': 'pending', 'start_time': None, 'duration': None},
                {'name': 'Collect Results', 'status': 'pending', 'start_time': None, 'duration': None},
                {'name': 'Scenario Summary', 'status': 'pending', 'start_time': None, 'duration': None},
                {'name': 'Health Check', 'status': 'pending', 'start_time': None, 'duration': None},
                {'name': 'Health Report', 'status': 'pending', 'start_time': None, 'duration': None},
            ]
            # RCA-related phases (inserted after Health Report)
            if rca_level != 'none':
                if options.get('rca_jira') or rca_level == 'full':
                    phases.append({'name': 'Search Jira', 'status': 'pending', 'start_time': None, 'duration': None})
                if options.get('rca_email') or rca_level == 'full':
                    phases.append({'name': 'Search Email', 'status': 'pending', 'start_time': None, 'duration': None})
                if options.get('rca_web'):
                    phases.append({'name': 'Search Web', 'status': 'pending', 'start_time': None, 'duration': None})
                if rca_level == 'full':
                    phases.append({'name': 'Deep RCA', 'status': 'pending', 'start_time': None, 'duration': None})
            if options.get('combined_cleanup'):
                phases.append({'name': 'Cleanup', 'status': 'pending', 'start_time': None, 'duration': None})
            phases.append({'name': 'Generate Report', 'status': 'pending', 'start_time': None, 'duration': None})
            if options.get('email'):
                phases.append({'name': 'Send Email', 'status': 'pending', 'start_time': None, 'duration': None})
        else:
            phases = [
                {'name': 'Initialize', 'status': 'pending', 'start_time': None, 'duration': None},
                {'name': 'Connect', 'status': 'pending', 'start_time': None, 'duration': None},
                {'name': 'Verify Setup', 'status': 'pending', 'start_time': None, 'duration': None},
                {'name': 'Run Scenarios', 'status': 'pending', 'start_time': None, 'duration': None},
                {'name': 'Collect Results', 'status': 'pending', 'start_time': None, 'duration': None},
                {'name': 'Summary', 'status': 'pending', 'start_time': None, 'duration': None},
            ]
            if options.get('email'):
                phases.append({'name': 'Send Email', 'status': 'pending', 'start_time': None, 'duration': None})

    else:
        cmd = [sys.executable, SCRIPT_PATH]

        server_host = options.get('server_host', '')
        if server_host:
            cmd.extend(['--server', server_host])
            host_obj = Host.query.filter_by(host=server_host).first()
            if host_obj and host_obj.name:
                clean_name = re.sub(r'\s*\[.*?\]\s*$', '', host_obj.name).strip() or host_obj.host
                cmd.extend(['--lab-name', clean_name])

        rca_level = options.get('rca_level', 'none')
        if rca_level == 'bugs':
            cmd.append('--rca-bugs')
        elif rca_level == 'full':
            cmd.append('--ai')

        if options.get('rca_jira'):
            cmd.append('--rca-jira')
        if options.get('rca_email'):
            cmd.append('--rca-email')
        if options.get('jira'):
            cmd.append('--check-jira')
        if options.get('email'):
            cmd.append('--email')
            if options.get('email_to'):
                cmd.extend(['--email-to', options.get('email_to')])

        phases = [
            {'name': 'Initialize', 'status': 'pending', 'start_time': None, 'duration': None},
        ]
        if options.get('jira'):
            phases.append({'name': 'Scan Jira', 'status': 'pending', 'start_time': None, 'duration': None})

        phases.extend([
            {'name': 'Connect', 'status': 'pending', 'start_time': None, 'duration': None},
            {'name': 'Collect Data', 'status': 'pending', 'start_time': None, 'duration': None},
            {'name': 'Console Report', 'status': 'pending', 'start_time': None, 'duration': None},
            {'name': 'Analyze', 'status': 'pending', 'start_time': None, 'duration': None},
            {'name': 'Generate Report', 'status': 'pending', 'start_time': None, 'duration': None},
        ])

        rca_phase_idx = len(phases) - 1
        if rca_level != 'none':
            if options.get('rca_jira') or rca_level == 'full':
                phases.insert(rca_phase_idx, {'name': 'Search Jira', 'status': 'pending', 'start_time': None, 'duration': None})
                rca_phase_idx += 1
            if options.get('rca_email') or rca_level == 'full':
                phases.insert(rca_phase_idx, {'name': 'Search Email', 'status': 'pending', 'start_time': None, 'duration': None})
                rca_phase_idx += 1
            if options.get('rca_web'):
                phases.insert(rca_phase_idx, {'name': 'Search Web', 'status': 'pending', 'start_time': None, 'duration': None})
                rca_phase_idx += 1
            if rca_level == 'full':
                phases.insert(rca_phase_idx, {'name': 'Deep RCA', 'status': 'pending', 'start_time': None, 'duration': None})

        if options.get('email'):
            phases.append({'name': 'Send Email', 'status': 'pending', 'start_time': None, 'duration': None})

    run_name = options.get('run_name', '')
    # Include lab name (jumphost label) in the build name
    server_host = options.get('server_host', '')
    lab_name = ''
    if server_host:
        host_obj = Host.query.filter_by(host=server_host).first()
        if host_obj and host_obj.name:
            lab_name = re.sub(r'\s*\[.*?\]\s*$', '', host_obj.name).strip()
    if run_name and lab_name:
        display_name = f'{run_name} ({lab_name})'
    elif lab_name:
        display_name = lab_name
    else:
        display_name = run_name

    job = {
        'number': build_num,
        'name': display_name,
        'status': 'running',
        'status_text': 'Running',
        'output': f'[{datetime.now().strftime("%H:%M:%S")}] Starting build #{build_num}' + (f' "{run_name}"' if run_name else '') + f' (by {username})...\n',
        'checks': checks,
        'checks_count': len(checks),
        'options': options,
        'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M"),
        'started_at_iso': datetime.utcnow().isoformat() + 'Z',
        'start_time': time.time(),
        'progress': 5,
        'phases': phases,
        'current_phase': 'Initializing...',
        'triggered_by': username,
        'user_id': user_id,
        'test_progress': {},   # per-test live progress: {test_name: {status, duration, ...}}
    }

    with _jobs_lock:
        running_jobs[job_id] = job

    def set_phase(job, index, status, phase_name=None):
        if index < len(job['phases']):
            phase = job['phases'][index]
            now = time.time()
            if status == 'running' and phase['start_time'] is None:
                phase['start_time'] = now
            elif status == 'done' and phase['start_time'] is not None:
                phase['duration'] = round(now - phase['start_time'], 1)
            phase['status'] = status
        if phase_name:
            job['current_phase'] = phase_name
            job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] ▶ {phase_name}\n'

    def run_job():
        from app import create_app
        app = create_app()
        report_file = None

        try:
            set_phase(job, 0, 'running', 'Initializing build environment...')
            if is_cnv or is_combined:
                tests_list = options.get('scenario_tests', [])
                task_label = 'CNV Combined' if is_combined else 'CNV Scenarios'
                job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] Task: {task_label} ({options.get("scenario_mode", "sanity")} mode)\n'
                job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] Tests: {len(tests_list)} selected\n'
                if is_combined:
                    job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] Pipeline: Scenarios → Health Check → {"Cleanup" if options.get("combined_cleanup") else "No Cleanup"}\n'
            else:
                job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] Options: RCA={options.get("rca_level")}, Jira={options.get("jira")}, Email={options.get("email")}\n'
                job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] Checks: {len(checks)} selected\n'
            job['output'] += '-' * 60 + '\n'
            job['progress'] = 5
            set_phase(job, 0, 'done')

            set_phase(job, 1, 'running', 'Connecting to cluster...')
            job['progress'] = 10

            current_phase_idx = 1

            def find_phase_idx(name):
                for i, p in enumerate(job['phases']):
                    if p['name'] == name:
                        return i
                return -1

            # ── Helper: stream a subprocess and match phase keywords ────────
            def stream_subprocess(sub_cmd, sub_keywords, start_phase_idx=1):
                """Run a subprocess, stream output, match keywords to phases.
                Returns (return_code, lines_list, last_phase_idx)."""
                nonlocal current_phase_idx
                sub_process = subprocess.Popen(
                    sub_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE,
                    text=True,
                    cwd=BASE_DIR,
                    bufsize=1,
                    start_new_session=True
                )
                job['process'] = sub_process
                # Regex patterns for per-test progress tracking
                _re_test_start = re.compile(r'\[(\S+)\]\s+Starting test')
                _re_test_complete = re.compile(r'\[(\S+)\]\s+Completed:\s+exit_code=(\d+),\s+duration=(.*)')
                _re_test_queued = re.compile(r'\[(\S+)\]\s+Queued for')

                sub_lines = []
                while True:
                    line = sub_process.stdout.readline()
                    if not line and sub_process.poll() is not None:
                        break
                    if line:
                        sub_lines.append(line)
                        timestamp = datetime.now().strftime("%H:%M:%S")
                        job['output'] += f'[{timestamp}] {line}'

                        # ── Per-test progress tracking ──
                        m_queued = _re_test_queued.search(line)
                        if m_queued:
                            tname = m_queued.group(1)
                            if tname not in job['test_progress']:
                                job['test_progress'][tname] = {
                                    'status': 'queued', 'start_time': None,
                                    'duration': None, 'exit_code': None,
                                }

                        m_start = _re_test_start.search(line)
                        if m_start:
                            tname = m_start.group(1)
                            job['test_progress'][tname] = {
                                'status': 'running', 'start_time': time.time(),
                                'duration': None, 'exit_code': None,
                            }

                        m_done = _re_test_complete.search(line)
                        if m_done:
                            tname = m_done.group(1)
                            ec = int(m_done.group(2))
                            dur_str = m_done.group(3).strip()
                            tp = job['test_progress'].get(tname, {})
                            tp['status'] = 'passed' if ec == 0 else 'failed'
                            tp['exit_code'] = ec
                            tp['duration'] = dur_str
                            job['test_progress'][tname] = tp

                        # ── Phase keyword matching ──
                        for keyword, (phase_idx, phase_msg, progress) in sub_keywords.items():
                            if keyword in line and phase_idx >= 0:
                                if phase_idx > current_phase_idx:
                                    set_phase(job, current_phase_idx, 'done')
                                    for skip_idx in range(current_phase_idx + 1, phase_idx):
                                        if job['phases'][skip_idx]['status'] == 'pending':
                                            job['phases'][skip_idx]['status'] = 'skipped'
                                    current_phase_idx = phase_idx
                                    set_phase(job, phase_idx, 'running', phase_msg)
                                job['progress'] = progress
                                job['current_phase'] = phase_msg
                                break
                rc = sub_process.wait()
                return rc, sub_lines

            # ── Helper: run custom checks via SSH on the jump host ─────────
            def run_custom_checks(check_ids, label='Custom Checks'):
                """Execute custom health checks remotely and return results list.
                Supports both single-command and script-upload checks.
                Each result: {name, command, check_type, expected, match_type, actual, passed, error}"""
                from app.models import CustomCheck
                results = []
                if not check_ids:
                    return results

                checks_list = CustomCheck.query.filter(CustomCheck.id.in_(check_ids)).all()
                if not checks_list:
                    return results

                job['output'] += f'\n[{datetime.now().strftime("%H:%M:%S")}] {"─"*50}\n'
                job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] Running {label} ({len(checks_list)} checks)\n'
                job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] {"─"*50}\n'

                server_host = options.get('server_host', '')
                if not server_host:
                    job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] ⚠ No jump host configured — skipping custom checks\n'
                    return results

                import paramiko
                ssh_key_path = os.path.expanduser('~/.ssh/id_rsa')
                host_obj = Host.query.filter_by(host=server_host).first()
                ssh_user = host_obj.user if host_obj and host_obj.user else 'root'

                try:
                    ssh = paramiko.SSHClient()
                    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    ssh.connect(server_host, username=ssh_user, key_filename=ssh_key_path, timeout=15)
                except Exception as e:
                    job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] ✗ SSH connection failed to {ssh_user}@{server_host}\n'
                    job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}]   Error: {e}\n'
                    job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}]   Key: {ssh_key_path}\n'
                    job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}]   Verify: ssh {ssh_user}@{server_host}\n'
                    return results

                kubeconfig_prefix = 'export KUBECONFIG=/home/kni/clusterconfigs/auth/kubeconfig 2>/dev/null; '

                for cc in checks_list:
                    is_script = (cc.check_type == 'script' and cc.script_content)
                    result = {
                        'name': cc.name,
                        'command': cc.command if not is_script else (cc.script_filename or 'script.sh'),
                        'check_type': cc.check_type or 'command',
                        'expected': cc.expected_value,
                        'match_type': cc.match_type,
                        'actual': '',
                        'passed': False,
                        'error': None,
                    }
                    try:
                        if is_script:
                            # ── Script mode: upload script, execute, cleanup ──
                            import uuid as _uuid
                            remote_script = f'/tmp/healthcrew_custom_{_uuid.uuid4().hex[:8]}.sh'
                            job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] ▸ {cc.name}: 📜 uploading script → {remote_script}\n'

                            # Upload script via SFTP
                            sftp = ssh.open_sftp()
                            with sftp.file(remote_script, 'w') as rf:
                                rf.write(cc.script_content)
                            sftp.close()

                            # Make executable and run
                            wrapped_cmd = f'{kubeconfig_prefix}chmod +x {remote_script} && {remote_script}; _ec=$?; rm -f {remote_script}; exit $_ec'
                            stdin, stdout, stderr = ssh.exec_command(wrapped_cmd, timeout=300)
                            exit_code = stdout.channel.recv_exit_status()
                            actual_output = stdout.read().decode('utf-8', errors='replace').strip()
                            error_output = stderr.read().decode('utf-8', errors='replace').strip()
                        else:
                            # ── Command mode: run single command ──
                            job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] ▸ {cc.name}: {cc.command}\n'
                            wrapped_cmd = f'{kubeconfig_prefix}{cc.command}'
                            stdin, stdout, stderr = ssh.exec_command(wrapped_cmd, timeout=120)
                            exit_code = stdout.channel.recv_exit_status()
                            actual_output = stdout.read().decode('utf-8', errors='replace').strip()
                            error_output = stderr.read().decode('utf-8', errors='replace').strip()

                        result['actual'] = actual_output

                        # Match logic (same for both command and script)
                        if cc.match_type == 'exit_code':
                            expected_ec = int(cc.expected_value) if cc.expected_value else 0
                            result['passed'] = (exit_code == expected_ec)
                        elif cc.match_type == 'exact':
                            result['passed'] = (actual_output == cc.expected_value)
                        elif cc.match_type == 'regex':
                            result['passed'] = bool(re.search(cc.expected_value, actual_output))
                        else:  # contains
                            if cc.expected_value:
                                result['passed'] = (cc.expected_value in actual_output)
                            else:
                                result['passed'] = (exit_code == 0)

                        status_icon = '✓' if result['passed'] else '✗'
                        status_color = 'PASS' if result['passed'] else 'FAIL'
                        job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}]   {status_icon} [{status_color}] '
                        if actual_output:
                            first_line = actual_output.split('\n')[0][:120]
                            job['output'] += f'{first_line}\n'
                        else:
                            job['output'] += f'exit_code={exit_code}\n'
                        if error_output and not result['passed']:
                            job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}]   stderr: {error_output[:200]}\n'

                    except Exception as e:
                        result['error'] = str(e)
                        job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}]   ✗ Error: {e}\n'

                    results.append(result)

                try:
                    ssh.close()
                except Exception:
                    pass

                passed = sum(1 for r in results if r['passed'])
                total = len(results)
                job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] Custom Checks: {passed}/{total} passed\n'
                return results

            # ── Build phase keyword maps based on task type ───────────────
            cnv_scenario_keywords = {}
            health_check_keywords = {}

            if is_cnv or is_combined:
                connect_idx = find_phase_idx('Connect')
                verify_idx = find_phase_idx('Verify Setup')
                run_idx = find_phase_idx('Run Scenarios')
                results_idx = find_phase_idx('Collect Results')
                summary_idx = find_phase_idx('Summary') if not is_combined else find_phase_idx('Scenario Summary')

                cnv_scenario_keywords = {
                    'Connecting to': (connect_idx, 'Connecting to jump host...', 10),
                    'Connected to': (connect_idx, 'Connected to jump host', 15),
                    'SSH connection established': (connect_idx, 'Connected to jump host', 15),
                    'CONNECTION ERROR': (connect_idx, '❌ Connection failed!', 15),
                    'SSH connection failed': (connect_idx, '❌ Connection failed!', 15),
                    'Connection refused': (connect_idx, '❌ Connection refused!', 15),
                    'Verifying cnv-scenarios': (verify_idx, 'Verifying cnv-scenarios setup...', 20),
                    'KUBECONFIG': (verify_idx, 'Setting up environment...', 22),
                    'kubeconfig': (verify_idx, 'Setting up environment...', 22),
                    'Running command': (run_idx, 'Running workload scenarios...', 30),
                    'run-workloads.sh': (run_idx, 'Running workload scenarios...', 30),
                    'Running test': (run_idx, 'Running test scenarios...', 35),
                    'RUNNING': (run_idx, 'Running scenarios...', 40),
                    'kube-burner': (run_idx, 'Running kube-burner workloads...', 50),
                    'Waiting for': (run_idx, 'Waiting for workloads...', 55),
                    'PASS': (run_idx, 'Tests progressing...', 60 if not is_combined else 30),
                    'FAIL': (run_idx, 'Tests progressing...', 60 if not is_combined else 30),
                    'Collecting results': (results_idx, 'Collecting results...', 75 if not is_combined else 35),
                    'summary.json': (results_idx, 'Parsing summary...', 80 if not is_combined else 38),
                    'Results:': (summary_idx, 'Generating summary...', 85 if not is_combined else 40),
                    'Summary:': (summary_idx, 'Generating summary...', 85 if not is_combined else 40),
                    'SUMMARY': (summary_idx, 'Generating summary...', 85 if not is_combined else 40),
                    'scenarios complete': (summary_idx, 'Scenarios done!', 95 if not is_combined else 42),
                    'All tests': (summary_idx, 'Scenarios done!', 95 if not is_combined else 42),
                    'CNV Scenarios finished': (summary_idx, 'Scenarios done!', 95 if not is_combined else 42),
                }

            if not is_cnv and not is_combined:
                scan_jira_idx = find_phase_idx('Scan Jira')
                connect_idx = find_phase_idx('Connect')
                collect_idx = find_phase_idx('Collect Data')
                console_idx = find_phase_idx('Console Report')
                analyze_idx = find_phase_idx('Analyze')
                jira_rca_idx = find_phase_idx('Search Jira')
                email_rca_idx = find_phase_idx('Search Email')
                web_rca_idx = find_phase_idx('Search Web')
                deep_rca_idx = find_phase_idx('Deep RCA')
                report_idx = find_phase_idx('Generate Report')
                email_idx = find_phase_idx('Send Email')

                health_check_keywords = {
                    'Checking Jira for new test suggestions': (scan_jira_idx, 'Scanning Jira for new tests...', 3),
                    'Checking Jira for recent bugs': (scan_jira_idx, 'Checking Jira for bugs...', 4),
                    'Analyzed': (scan_jira_idx, 'Analyzing Jira bugs...', 5),
                    'new checks will be included': (scan_jira_idx, 'Jira scan complete', 6),
                    'HealthCrew AI Starting': (connect_idx, 'Initializing...', 8),
                    'Connecting to cluster': (connect_idx, 'Connecting to cluster...', 10),
                    'Connected to': (connect_idx, 'Connected to cluster', 15),
                    'CONNECTION ERROR': (connect_idx, '❌ Connection failed!', 15),
                    'SSH connection failed': (connect_idx, '❌ Connection failed!', 15),
                    'host unreachable': (connect_idx, '❌ Host unreachable!', 15),
                    'Authentication failed': (connect_idx, '❌ Authentication failed!', 15),
                    'oc.*not responding': (connect_idx, '❌ oc CLI not configured!', 15),
                    'cluster is not configured': (connect_idx, '❌ Cluster not configured!', 15),
                    'Collecting cluster data': (collect_idx, 'Collecting cluster data...', 18),
                    'Checking nodes': (collect_idx, 'Checking nodes...', 22),
                    'Checking node resources': (collect_idx, 'Checking node resources...', 25),
                    'Getting cluster version': (collect_idx, 'Getting cluster version...', 28),
                    'Checking etcd': (collect_idx, 'Checking etcd health...', 30),
                    'Checking certificates': (collect_idx, 'Checking certificates...', 32),
                    'Checking PVC': (collect_idx, 'Checking PVC status...', 35),
                    'Checking VM migrations': (collect_idx, 'Checking VM migrations...', 38),
                    'Checking alerts': (collect_idx, 'Checking alerts...', 40),
                    'Checking CSI': (collect_idx, 'Checking CSI drivers...', 42),
                    'Checking OOM': (collect_idx, 'Checking OOM events...', 44),
                    'Checking virt-handler': (collect_idx, 'Checking virt-handler pods...', 46),
                    'Checking virt-launcher': (collect_idx, 'Checking virt-launcher pods...', 48),
                    'Checking DataVolumes': (collect_idx, 'Checking DataVolumes...', 50),
                    'Checking HyperConverged': (collect_idx, 'Checking HyperConverged...', 52),
                    'Data collection complete': (collect_idx, 'Data collection complete', 54),
                    'Generating console report': (console_idx, 'Generating console report...', 56),
                    'HEALTH REPORT': (console_idx, 'Displaying health report...', 58),
                    'Starting Root Cause Analysis': (analyze_idx, 'Starting root cause analysis...', 60),
                    '🔬 Starting Root Cause Analysis': (analyze_idx, 'Starting root cause analysis...', 60),
                    'Matching failures to known issues': (analyze_idx, 'Matching failures to known issues...', 62),
                    'issue(s) to analyze': (analyze_idx, 'Analyzing issues...', 64),
                    '→ Searching Jira': (jira_rca_idx, 'Searching Jira for bugs...', 66),
                    'Searching Jira for related bugs': (jira_rca_idx, 'Searching Jira for bugs...', 66),
                    '→ Searching emails': (email_rca_idx, 'Searching emails...', 70),
                    'Searching emails for related': (email_rca_idx, 'Searching emails...', 70),
                    '→ Searching web': (web_rca_idx, 'Searching web docs...', 74),
                    'Running deep investigation': (deep_rca_idx, 'Running deep investigation...', 78),
                    'Deep investigation complete': (deep_rca_idx, 'Deep investigation complete', 82),
                    'Saving HTML report': (report_idx, 'Saving HTML report...', 85),
                    'Saved:': (report_idx, 'Report saved', 88),
                    'Reports saved': (report_idx, 'Reports saved', 90),
                    'Health check complete': (report_idx, 'Complete!', 95),
                    'Sending email report': (email_idx, 'Sending email...', 96),
                    'Email sent successfully': (email_idx, 'Email sent!', 99),
                }

            # ==============================================================
            # COMBINED RUN: Scenarios → Health Check → Cleanup
            # ==============================================================
            if is_combined:
                from healthchecks.cnv_report import generate_combined_report_html

                # ── Step 1: Run CNV scenarios (cleanup=false) ─────────────
                job['output'] += f'\n[{datetime.now().strftime("%H:%M:%S")}] {"="*60}\n'
                job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] PHASE 1: Running CNV Scenarios (cleanup=false)\n'
                job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] {"="*60}\n'

                scenario_rc, scenario_lines = stream_subprocess(cmd, cnv_scenario_keywords)
                scenario_output = ''.join(scenario_lines)

                # Mark scenario phases done
                s_summary_idx = find_phase_idx('Scenario Summary')
                if s_summary_idx >= 0:
                    set_phase(job, s_summary_idx, 'done')

                # Parse scenario results
                cnv_results = None
                try:
                    cnv_results = parse_cnv_results(scenario_output)
                except Exception:
                    pass

                # ── Step 2: Run Health Check ──────────────────────────────
                hc_phase_idx = find_phase_idx('Health Check')
                hr_phase_idx = find_phase_idx('Health Report')
                set_phase(job, hc_phase_idx, 'running', 'Running health check...')
                current_phase_idx = hc_phase_idx
                job['progress'] = 50

                job['output'] += f'\n[{datetime.now().strftime("%H:%M:%S")}] {"="*60}\n'
                job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] PHASE 2: Running Health Check\n'
                job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] {"="*60}\n'

                hc_cmd = [sys.executable, SCRIPT_PATH]
                server_host = options.get('server_host', '')
                if server_host:
                    hc_cmd.extend(['--server', server_host])
                    # Add --lab-name from Host DB
                    host_obj = Host.query.filter_by(host=server_host).first()
                    if host_obj and host_obj.name:
                        clean_name = re.sub(r'\s*\[.*?\]\s*$', '', host_obj.name).strip() or host_obj.host
                        hc_cmd.extend(['--lab-name', clean_name])

                # RCA flags
                rca_level = options.get('rca_level', 'none')
                if rca_level == 'bugs':
                    hc_cmd.append('--rca-bugs')
                elif rca_level == 'full':
                    hc_cmd.append('--ai')
                if options.get('rca_jira'):
                    hc_cmd.append('--rca-jira')
                if options.get('rca_email'):
                    hc_cmd.append('--rca-email')

                # Jira integration
                if options.get('jira'):
                    hc_cmd.append('--check-jira')

                # Email (health check script handles its own emailing)
                if options.get('email'):
                    hc_cmd.append('--email')
                    if options.get('email_to'):
                        hc_cmd.extend(['--email-to', options.get('email_to')])

                # Health check phase keywords (mapped to combined phases)
                hc_keywords = {
                    'HealthCrew AI Starting': (hc_phase_idx, 'Health check initializing...', 52),
                    'Connecting to cluster': (hc_phase_idx, 'Health check connecting...', 54),
                    'Connected to': (hc_phase_idx, 'Health check connected', 55),
                    'Collecting cluster data': (hc_phase_idx, 'Collecting cluster data...', 58),
                    'Checking nodes': (hc_phase_idx, 'Checking nodes...', 60),
                    'Checking node resources': (hc_phase_idx, 'Checking resources...', 62),
                    'Data collection complete': (hc_phase_idx, 'Data collection done', 65),
                    'Generating console report': (hc_phase_idx, 'Generating console report...', 67),
                    'HEALTH REPORT': (hc_phase_idx, 'Displaying health report...', 70),
                    'Saving HTML report': (hr_phase_idx, 'Saving health report...', 72),
                    'Saved:': (hr_phase_idx, 'Health report saved', 74),
                    'Reports saved': (hr_phase_idx, 'Health reports saved', 75),
                    'Health check complete': (hr_phase_idx, 'Health check done!', 78),
                }

                # Add RCA-related keywords when RCA is enabled
                rca_level = options.get('rca_level', 'none')
                if rca_level != 'none':
                    jira_rca_idx = find_phase_idx('Search Jira')
                    email_rca_idx = find_phase_idx('Search Email')
                    web_rca_idx = find_phase_idx('Search Web')
                    deep_rca_idx = find_phase_idx('Deep RCA')

                    hc_keywords.update({
                        'Starting Root Cause Analysis': (hr_phase_idx, 'Starting root cause analysis...', 73),
                        '🔬 Starting Root Cause Analysis': (hr_phase_idx, 'Starting root cause analysis...', 73),
                        'Matching failures to known issues': (hr_phase_idx, 'Matching failures...', 74),
                        'issue(s) to analyze': (hr_phase_idx, 'Analyzing issues...', 75),
                        '→ Searching Jira': (jira_rca_idx, 'Searching Jira for bugs...', 76),
                        'Searching Jira for related bugs': (jira_rca_idx, 'Searching Jira for bugs...', 76),
                        '→ Searching emails': (email_rca_idx, 'Searching emails...', 77),
                        'Searching emails for related': (email_rca_idx, 'Searching emails...', 77),
                        '→ Searching web': (web_rca_idx, 'Searching web docs...', 78),
                        'Running deep investigation': (deep_rca_idx, 'Running deep investigation...', 79),
                        'Deep investigation complete': (deep_rca_idx, 'Deep investigation complete', 80),
                    })

                hc_rc, hc_lines = stream_subprocess(hc_cmd, hc_keywords)
                health_output = ''.join(hc_lines)
                set_phase(job, hr_phase_idx, 'done')

                # Mark RCA phases done (if they exist)
                for rca_name in ('Search Jira', 'Search Email', 'Search Web', 'Deep RCA'):
                    rca_idx = find_phase_idx(rca_name)
                    if rca_idx >= 0:
                        set_phase(job, rca_idx, 'done')

                # Capture health report filename
                health_report_file = None
                for hl in hc_lines:
                    match = re.search(r'(health_report_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.html)', hl)
                    if match:
                        health_report_file = match.group(1)

                # ── Step 3: Cleanup (optional) ────────────────────────────
                cleanup_rc = 0
                cleanup_output = ''
                if options.get('combined_cleanup'):
                    cleanup_phase_idx = find_phase_idx('Cleanup')
                    set_phase(job, cleanup_phase_idx, 'running', 'Cleaning up test resources...')
                    current_phase_idx = cleanup_phase_idx
                    job['progress'] = 80

                    job['output'] += f'\n[{datetime.now().strftime("%H:%M:%S")}] {"="*60}\n'
                    job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] PHASE 3: Cleanup (cleanup=true)\n'
                    job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] {"="*60}\n'

                    cleanup_cmd = list(cmd) + ['--cleanup-only']
                    cleanup_keywords = {
                        'Cleanup Starting': (cleanup_phase_idx, 'Cleanup running...', 82),
                        'Connecting to': (cleanup_phase_idx, 'Cleanup connecting...', 83),
                        'Connected to': (cleanup_phase_idx, 'Cleanup connected', 84),
                        'Running': (cleanup_phase_idx, 'Cleanup in progress...', 86),
                        'kube-burner': (cleanup_phase_idx, 'Cleanup running kube-burner...', 88),
                        'CLEANUP COMPLETE': (cleanup_phase_idx, 'Cleanup done!', 90),
                        'CLEANUP FAILED': (cleanup_phase_idx, 'Cleanup failed!', 90),
                    }

                    cleanup_rc, cleanup_lines = stream_subprocess(cleanup_cmd, cleanup_keywords)
                    cleanup_output = ''.join(cleanup_lines)
                    set_phase(job, cleanup_phase_idx, 'done')

                # ── Generate Combined Report ──────────────────────────────
                gen_phase_idx = find_phase_idx('Generate Report')
                set_phase(job, gen_phase_idx, 'running', 'Generating combined report...')
                current_phase_idx = gen_phase_idx
                job['progress'] = 92

                full_output = scenario_output + '\n' + health_output + '\n' + cleanup_output

                # Determine overall status
                has_scenario_fail = scenario_rc != 0 or ('FAIL' in scenario_output and 'PASS' not in scenario_output)
                has_scenario_partial = 'FAIL' in scenario_output and 'PASS' in scenario_output
                has_hc_issues = 'WARNING' in health_output or 'Issues:' in health_output or '⚠️' in health_output
                has_hc_errors = 'ERROR' in health_output or 'CRITICAL' in health_output or '❌' in health_output

                if scenario_rc != 0 and hc_rc != 0:
                    status = 'failed'
                    status_text = 'Failed'
                elif has_scenario_fail or has_hc_errors:
                    status = 'failed'
                    status_text = 'Failed'
                elif has_scenario_partial or has_hc_issues:
                    status = 'unstable'
                    status_text = 'Issues Found'
                else:
                    status = 'success'
                    status_text = 'All Passed'

                try:
                    ts_str = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
                    report_filename = f'combined_report_{ts_str}.html'

                    duration_secs = int(time.time() - job['start_time'])
                    duration = f"{duration_secs // 60}m {duration_secs % 60}s"

                    report_html = generate_combined_report_html(
                        cnv_results=cnv_results,
                        health_output=health_output,
                        health_report_file=health_report_file,
                        cleanup_status='success' if cleanup_rc == 0 and options.get('combined_cleanup') else ('failed' if cleanup_rc != 0 else 'skipped'),
                        build_num=build_num,
                        build_name=job.get('name', run_name),
                        status=status,
                        status_text=status_text,
                        duration=duration,
                        mode=options.get('scenario_mode', 'sanity'),
                        server=options.get('server_host', ''),
                        checks=checks,
                        scenario_output=scenario_output,
                        health_check_output=health_output,
                        cleanup_output=cleanup_output,
                    )
                    os.makedirs(REPORTS_DIR, exist_ok=True)
                    report_path = os.path.join(REPORTS_DIR, report_filename)
                    with open(report_path, 'w', encoding='utf-8') as f:
                        f.write(report_html)
                    report_file = report_filename
                    job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] Reports saved: {report_filename}\n'
                except Exception as e:
                    job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] Report generation failed: {e}\n'

                set_phase(job, gen_phase_idx, 'done')
                job['progress'] = 95

                # Finalize
                duration_secs = int(time.time() - job['start_time'])
                duration = f"{duration_secs // 60}m {duration_secs % 60}s"
                return_code = max(scenario_rc, hc_rc, cleanup_rc)

            # ==============================================================
            # SINGLE TASK: CNV Scenarios only OR Health Check only
            # ==============================================================
            else:
                active_keywords = cnv_scenario_keywords if is_cnv else health_check_keywords

                return_code, stdout_lines = stream_subprocess(cmd, active_keywords)

                if not is_cnv:
                    for sl in stdout_lines:
                        if 'Report saved' in sl or 'health_report_' in sl:
                            match = re.search(r'(health_report_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.html)', sl)
                            if match:
                                report_file = match.group(1)

                for i in range(current_phase_idx, len(phases)):
                    set_phase(job, i, 'done')

                job['progress'] = 100

                duration_secs = int(time.time() - job['start_time'])
                duration = f"{duration_secs // 60}m {duration_secs % 60}s"

                full_output = ''.join(stdout_lines)

            cnv_results_final = None

            if is_cnv:
                # CNV scenario status detection — use the results summary line,
                # not the entire raw output (which contains log noise like
                # "Empty document list" warnings that include "failed" etc.)
                summary_lines = [l for l in full_output.split('\n')
                                 if 'PASSED:' in l and 'FAILED:' in l and 'TOTAL:' in l]
                if summary_lines:
                    import re as _re
                    m = _re.search(r'PASSED:\s*(\d+)\s*\|\s*FAILED:\s*(\d+)\s*\|\s*TOTAL:\s*(\d+)', summary_lines[-1])
                    if m:
                        n_passed, n_failed = int(m.group(1)), int(m.group(2))
                        if return_code != 0 and n_passed == 0:
                            status, status_text = 'failed', 'Failed'
                        elif n_failed > 0 and n_passed > 0:
                            status, status_text = 'unstable', 'Partial Pass'
                        elif n_failed > 0:
                            status, status_text = 'failed', 'Failed'
                        else:
                            status, status_text = 'success', 'All Passed'
                    else:
                        status = 'failed' if return_code != 0 else 'success'
                        status_text = 'Failed' if return_code != 0 else 'All Passed'
                else:
                    # Fallback: no summary line found
                    status = 'failed' if return_code != 0 else 'success'
                    status_text = 'Failed' if return_code != 0 else 'All Passed'

                # Parse structured per-test results and generate HTML report
                try:
                    cnv_results_final = parse_cnv_results(full_output)
                    ts_str = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
                    report_filename = f'cnv_report_{ts_str}.html'
                    report_html = generate_cnv_report_html(
                        results=cnv_results_final,
                        build_num=build_num,
                        build_name=job.get('name', run_name),
                        status=status,
                        status_text=status_text,
                        duration=duration,
                        mode=options.get('scenario_mode', 'sanity'),
                        server=options.get('server_host', ''),
                        checks=checks,
                        output=full_output,
                    )
                    os.makedirs(REPORTS_DIR, exist_ok=True)
                    report_path = os.path.join(REPORTS_DIR, report_filename)
                    with open(report_path, 'w', encoding='utf-8') as f:
                        f.write(report_html)
                    report_file = report_filename
                    job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] Reports saved: {report_filename}\n'
                except Exception as e:
                    job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] Report generation failed: {e}\n'

            elif not is_combined:
                has_issues = 'WARNING' in full_output or 'Issues:' in full_output or 'ISSUES' in full_output or '⚠️' in full_output
                has_errors = 'ERROR' in full_output or 'CRITICAL' in full_output or '❌' in full_output
                if return_code != 0 or has_errors:
                    status = 'failed'
                    status_text = 'Failed'
                elif has_issues:
                    status = 'unstable'
                    status_text = 'Issues Found'
                else:
                    status = 'success'
                    status_text = 'Healthy'

            # is_combined already set status/status_text above

            # ── Run Custom Checks (if any selected) ──────────────────────
            custom_check_results = []
            try:
                if is_cnv:
                    cc_ids = options.get('scenario_custom_checks', [])
                elif is_combined:
                    # Combined: run both health-check and scenario custom checks
                    cc_ids = list(set(
                        options.get('hc_custom_checks', []) +
                        options.get('scenario_custom_checks', [])
                    ))
                else:
                    cc_ids = options.get('custom_checks', [])

                if cc_ids:
                    custom_check_results = run_custom_checks(cc_ids, label='Custom Health Checks')
                    # If any custom check failed, downgrade status
                    cc_failed = [r for r in custom_check_results if not r['passed']]
                    if cc_failed and status == 'success':
                        status = 'unstable'
                        status_text = status_text + ' (custom check issues)'
            except Exception as e:
                job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] Custom check execution error: {e}\n'

            for i in range(current_phase_idx, len(phases)):
                set_phase(job, i, 'done')
            job['progress'] = 100

            build_record = {
                'number': build_num,
                'name': job.get('name', run_name),
                'status': status,
                'status_text': status_text,
                'checks': checks,
                'checks_count': len(checks),
                'options': options,
                'timestamp': job['timestamp'],
                'duration': duration,
                'output': job['output'],
                'report_file': report_file,
                'custom_check_results': custom_check_results,
            }

            with app.app_context():
                save_build_to_db(build_record, user_id=user_id)

                # Record issues for learning (health checks only)
                if not is_cnv and not is_combined:
                    try:
                        from app.learning import record_health_check_run
                        detected_issues = extract_issues_from_output(full_output)
                        if detected_issues:
                            record_health_check_run(detected_issues)
                    except Exception:
                        pass

                # Send email report if requested (CNV/combined — health checks send their own email)
                if (is_cnv or is_combined) and options.get('email') and options.get('email_to'):
                    email_phase_idx = find_phase_idx('Send Email')
                    if email_phase_idx is not None and email_phase_idx >= 0:
                        set_phase(job, email_phase_idx, 'running', 'Sending email report...')
                    try:
                        _send_cnv_email_report(
                            recipient=options['email_to'],
                            build_num=build_num,
                            build_name=job.get('name', run_name),
                            status=status,
                            status_text=status_text,
                            duration=duration,
                            checks=checks,
                            options=options,
                            output=full_output,
                            cnv_results=cnv_results_final if is_cnv else (cnv_results if is_combined else None),
                        )
                        job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] Email sent to {options["email_to"]}\n'
                        if email_phase_idx is not None and email_phase_idx >= 0:
                            set_phase(job, email_phase_idx, 'done', 'Email sent!')
                    except Exception as e:
                        job['output'] += f'[{datetime.now().strftime("%H:%M:%S")}] Email failed: {e}\n'
                        if email_phase_idx is not None and email_phase_idx >= 0:
                            set_phase(job, email_phase_idx, 'done', f'Email failed: {e}')

        except Exception as e:
            job['output'] += f'\n[{datetime.now().strftime("%H:%M:%S")}] ❌ Error: {str(e)}\n'
            duration_secs = int(time.time() - job['start_time'])
            duration = f"{duration_secs // 60}m {duration_secs % 60}s"

            build_record = {
                'number': build_num,
                'name': run_name,
                'status': 'failed',
                'status_text': 'Error',
                'checks': checks,
                'checks_count': len(checks),
                'options': options,
                'timestamp': job['timestamp'],
                'duration': duration,
                'output': job['output'],
                'report_file': None
            }
            with app.app_context():
                save_build_to_db(build_record, user_id=user_id)

        finally:
            with _jobs_lock:
                if job_id in running_jobs:
                    del running_jobs[job_id]
            # Start next queued build
            _start_next_queued()

    thread = threading.Thread(target=run_job)
    thread.daemon = True
    thread.start()


# =============================================================================
# Settings Routes
# =============================================================================

@dashboard_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings_page():
    """Settings page for configuring defaults"""
    message = None

    # Only admin and operator can change settings
    if request.method == 'POST':
        if not current_user.is_operator:
            return "Access denied. Operator role required.", 403

        # Sync hosts to DB (per-user)
        host_ids = request.form.getlist('host_id[]')
        host_names = request.form.getlist('host_name[]')
        host_addrs = request.form.getlist('host_addr[]')
        host_users = request.form.getlist('host_user[]')
        host_passwords = request.form.getlist('host_password[]')
        # Pad passwords list to match hosts (existing hosts don't have password fields)
        while len(host_passwords) < len(host_ids):
            host_passwords.append('')
        first_host, first_user, ssh_messages = sync_hosts_from_form(
            host_ids, host_names, host_addrs, host_users, host_passwords, current_user
        )

        new_settings = {
            'thresholds': {
                'cpu_warning': int(request.form.get('cpu_warning', 85)),
                'memory_warning': int(request.form.get('memory_warning', 80)),
                'disk_latency': int(request.form.get('disk_latency', 100)),
                'etcd_latency': int(request.form.get('etcd_latency', 100)),
                'pod_density': int(request.form.get('pod_density', 50)),
                'restart_count': int(request.form.get('restart_count', 5)),
                'virt_handler_memory': int(request.form.get('virt_handler_memory', 500))
            },
            'ssh': {
                'host': first_host,
                'user': first_user,
            },
            'ai': {
                'model': request.form.get('ollama_model', 'ollama/llama3.2:3b').strip(),
                'url': request.form.get('ollama_url', 'http://localhost:11434').strip()
            },
            'jira': {
                'projects': [p.strip() for p in request.form.get('jira_projects', 'CNV, OCPBUGS, ODF').split(',')],
                'scan_days': int(request.form.get('jira_scan_days', 30)),
                'bug_limit': int(request.form.get('jira_bug_limit', 50))
            },
            'cnv': {
                'cnv_path': request.form.get('cnv_path', '/home/kni/git/cnv-scenarios').strip(),
                'mode': request.form.get('cnv_mode', 'sanity').strip(),
                'parallel': 'cnv_parallel' in request.form,
                'kb_log_level': request.form.get('cnv_kb_log_level', '').strip(),
                'kb_timeout': request.form.get('cnv_kb_timeout', '').strip(),
                'global_vars': {
                    'storageClassName': request.form.get('cnv_default_storageClassName', '').strip(),
                    'nodeSelector': request.form.get('cnv_default_nodeSelector', '').strip(),
                    'maxWaitTimeout': request.form.get('cnv_default_maxWaitTimeout', '').strip(),
                    'jobPause': request.form.get('cnv_default_jobPause', '').strip(),
                },
                'scenario_vars': _collect_scenario_var_defaults(request.form),
            }
        }

        save_settings(new_settings)

        if first_host:
            _update_env_var('RH_LAB_HOST', first_host)
            _update_env_var('RH_LAB_USER', first_user)

        log_audit('settings_update', details='Settings updated')
        message = "Your settings have been saved successfully."
        if ssh_messages:
            message += " " + " | ".join(ssh_messages)

    settings = load_settings()
    ssh_config = settings.get('ssh', {'host': '', 'user': 'root'})

    # Load hosts from DB (user's own + admin sees all)
    host_objects = get_hosts_for_user(current_user)
    saved_hosts = [h.to_dict() for h in host_objects]

    cnv_config = settings.get('cnv', _DEFAULT_CNV_SETTINGS)

    # Load custom checks for this user
    from app.models import CustomCheck
    custom_checks = [c.to_dict() for c in
                     CustomCheck.query.filter_by(created_by=current_user.id).order_by(CustomCheck.created_at.desc()).all()]

    return render_template('settings.html',
                           thresholds=settings.get('thresholds', DEFAULT_THRESHOLDS),
                           ssh_config=ssh_config,
                           saved_hosts=saved_hosts,
                           ai_config=settings.get('ai', {'model': 'ollama/llama3.2:3b', 'url': 'http://localhost:11434'}),
                           jira_config=settings.get('jira', {'projects': ['CNV', 'OCPBUGS', 'ODF'], 'scan_days': 30, 'bug_limit': 50}),
                           cnv_config=cnv_config,
                           cnv_global_vars=CNV_GLOBAL_VARIABLES,
                           cnv_scenarios=CNV_SCENARIOS,
                           custom_checks=custom_checks,
                           message=message,
                           active_page='settings')


@dashboard_bp.route('/api/settings', methods=['GET'])
@login_required
def api_get_settings():
    return jsonify(load_settings())


@dashboard_bp.route('/api/settings/thresholds', methods=['GET'])
@login_required
def api_get_thresholds():
    return jsonify(get_thresholds())


# =============================================================================
# Host Management API Routes
# =============================================================================

@dashboard_bp.route('/api/hosts', methods=['POST'])
@operator_required
def api_add_host():
    """Add a new jump host (persisted to DB immediately)."""
    data = request.get_json(force=True)
    addr = data.get('host', '').strip()
    name = data.get('name', '').strip() or addr
    user = data.get('user', '').strip() or 'root'

    if not addr:
        return jsonify({'success': False, 'error': 'Host address is required.'})

    label = f'{name} [{current_user.username}]' if not name.endswith(f'[{current_user.username}]') else name
    host_obj = Host(name=label, host=addr, user=user, created_by=current_user.id)
    db.session.add(host_obj)
    db.session.commit()
    log_audit('host_add', target=f'{user}@{addr}', details=f'Added host {label}')
    return jsonify({'success': True, 'host': host_obj.to_dict()})


@dashboard_bp.route('/api/hosts/<int:host_id>', methods=['DELETE'])
@operator_required
def api_delete_host(host_id):
    """Delete a jump host from the DB."""
    host_obj = Host.query.get(host_id)
    if not host_obj:
        return jsonify({'success': False, 'error': 'Host not found.'}), 404
    # Only owner or admin can delete
    if host_obj.created_by != current_user.id and not current_user.is_admin:
        return jsonify({'success': False, 'error': 'Permission denied.'}), 403
    log_audit('host_delete', target=f'{host_obj.user}@{host_obj.host}', details=f'Deleted host {host_obj.name}')
    db.session.delete(host_obj)
    db.session.commit()
    return jsonify({'success': True})


# =============================================================================
# SSH Setup Routes
# =============================================================================

@dashboard_bp.route('/api/ssh/setup', methods=['POST'])
@operator_required
def api_ssh_setup():
    import paramiko
    data = request.get_json(force=True)
    host = data.get('host', '').strip()
    user = data.get('user', '').strip()
    password = data.get('password', '')

    if not host or not user or not password:
        return jsonify({'success': False, 'error': 'Host, user, and password are all required.'})

    home = os.path.expanduser("~")
    ssh_dir = os.path.join(home, ".ssh")
    key_path = os.path.join(ssh_dir, "id_ed25519")
    pub_path = key_path + ".pub"

    try:
        os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
        if not os.path.exists(key_path):
            key = paramiko.Ed25519Key.generate()
            key.write_private_key_file(key_path)
            os.chmod(key_path, 0o600)
            pub_key_str = f"{key.get_name()} {key.get_base64()} cnv-healthcrew"
            with open(pub_path, 'w') as f:
                f.write(pub_key_str + "\n")
            os.chmod(pub_path, 0o644)
        else:
            key = paramiko.Ed25519Key(filename=key_path)
            pub_key_str = f"{key.get_name()} {key.get_base64()} cnv-healthcrew"

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(host, username=user, password=password, timeout=15)

        commands = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"grep -qxF '{pub_key_str}' ~/.ssh/authorized_keys 2>/dev/null || "
            f"echo '{pub_key_str}' >> ~/.ssh/authorized_keys && "
            "chmod 600 ~/.ssh/authorized_keys"
        )
        stdin, stdout, stderr = client.exec_command(commands)
        exit_status = stdout.channel.recv_exit_status()
        err_output = stderr.read().decode().strip()
        client.close()

        if exit_status != 0:
            return jsonify({'success': False, 'error': f'Failed to install public key: {err_output}'})

        verify_client = paramiko.SSHClient()
        verify_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        verify_client.connect(host, username=user, key_filename=key_path, timeout=15)
        verify_client.close()

        settings = load_settings()
        settings.setdefault('ssh', {})
        settings['ssh']['host'] = host
        settings['ssh']['user'] = user
        save_settings(settings)

        _update_env_var('RH_LAB_HOST', host)
        _update_env_var('RH_LAB_USER', user)
        _update_env_var('SSH_KEY_PATH', key_path)

        # Also save the host to DB if requested (from the combined add-host flow)
        save_host = data.get('save_host', False)
        host_dict = None
        if save_host:
            host_name = data.get('name', '').strip() or host
            label = f'{host_name} [{current_user.username}]' if not host_name.endswith(f'[{current_user.username}]') else host_name
            host_obj = Host(name=label, host=host, user=user, created_by=current_user.id)
            db.session.add(host_obj)
            db.session.commit()
            host_dict = host_obj.to_dict()

        log_audit('ssh_setup', target=f'{user}@{host}', details='SSH key setup completed')

        result = {'success': True, 'message': f'Passwordless SSH to {user}@{host} is now configured.', 'key_path': key_path}
        if host_dict:
            result['host'] = host_dict
        return jsonify(result)

    except paramiko.AuthenticationException:
        return jsonify({'success': False, 'error': 'Authentication failed — wrong password or user.'})
    except paramiko.SSHException as e:
        return jsonify({'success': False, 'error': f'SSH error: {str(e)}'})
    except Exception as e:
        return jsonify({'success': False, 'error': f'Unexpected error: {str(e)}'})




# =============================================================================
# Custom Health Checks CRUD
# =============================================================================

@dashboard_bp.route('/api/custom-checks', methods=['GET'])
@login_required
def api_get_custom_checks():
    """List all custom checks (user's own)."""
    from app.models import CustomCheck
    checks = CustomCheck.query.filter_by(created_by=current_user.id).order_by(CustomCheck.created_at.desc()).all()
    return jsonify([c.to_dict() for c in checks])


@dashboard_bp.route('/api/custom-checks', methods=['POST'])
@operator_required
def api_create_custom_check():
    """Create a new custom check (command or script)."""
    from app.models import CustomCheck

    # Support both JSON and multipart/form-data (for file upload)
    if request.content_type and 'multipart/form-data' in request.content_type:
        data = request.form.to_dict()
        script_file = request.files.get('script_file')
    else:
        data = request.get_json(silent=True) or {}
        script_file = None

    name = data.get('name', '').strip()
    check_type = data.get('check_type', 'command')

    # Validate: must have a name, and either a command or a script
    command = data.get('command', '').strip()
    script_content = data.get('script_content', '').strip()
    script_filename = ''

    if script_file and script_file.filename:
        script_content = script_file.read().decode('utf-8', errors='replace')
        script_filename = script_file.filename
        check_type = 'script'

    if not name:
        return jsonify({'success': False, 'error': 'Name is required.'}), 400
    if check_type == 'command' and not command:
        return jsonify({'success': False, 'error': 'Command is required.'}), 400
    if check_type == 'script' and not script_content:
        return jsonify({'success': False, 'error': 'Script content is required (paste or upload a file).'}), 400

    check = CustomCheck(
        name=name,
        check_type=check_type,
        command=command,
        script_content=script_content if check_type == 'script' else None,
        script_filename=script_filename or data.get('script_filename', ''),
        expected_value=data.get('expected_value', '').strip(),
        match_type=data.get('match_type', 'contains'),
        description=data.get('description', '').strip(),
        run_with=data.get('run_with', 'health_check'),
        linked_scenario=data.get('linked_scenario', '').strip() or None,
        enabled=data.get('enabled', True) if isinstance(data.get('enabled'), bool) else data.get('enabled', 'true').lower() != 'false',
        created_by=current_user.id,
    )
    db.session.add(check)
    db.session.commit()
    detail = f'Script: {script_filename}' if check_type == 'script' else f'Command: {command}'
    log_audit('custom_check_create', target=name, details=detail)
    return jsonify({'success': True, 'check': check.to_dict()})


@dashboard_bp.route('/api/custom-checks/<int:check_id>', methods=['PUT'])
@operator_required
def api_update_custom_check(check_id):
    """Update an existing custom check."""
    from app.models import CustomCheck
    check = CustomCheck.query.get(check_id)
    if not check:
        return jsonify({'success': False, 'error': 'Check not found.'}), 404
    if check.created_by != current_user.id and not current_user.is_admin:
        return jsonify({'success': False, 'error': 'Permission denied.'}), 403

    # Support both JSON and multipart/form-data
    if request.content_type and 'multipart/form-data' in request.content_type:
        data = request.form.to_dict()
        script_file = request.files.get('script_file')
    else:
        data = request.get_json(silent=True) or {}
        script_file = None

    if 'name' in data:
        check.name = data['name'].strip()
    if 'check_type' in data:
        check.check_type = data['check_type']
    if 'command' in data:
        check.command = data['command'].strip()
    if 'script_content' in data:
        check.script_content = data['script_content'].strip() or None
    if script_file and script_file.filename:
        check.script_content = script_file.read().decode('utf-8', errors='replace')
        check.script_filename = script_file.filename
        check.check_type = 'script'
    if 'script_filename' in data:
        check.script_filename = data['script_filename'].strip()
    if 'expected_value' in data:
        check.expected_value = data['expected_value'].strip()
    if 'match_type' in data:
        check.match_type = data['match_type']
    if 'description' in data:
        check.description = data['description'].strip()
    if 'run_with' in data:
        check.run_with = data['run_with']
    if 'linked_scenario' in data:
        check.linked_scenario = data['linked_scenario'].strip() or None
    if 'enabled' in data:
        val = data['enabled']
        check.enabled = val if isinstance(val, bool) else str(val).lower() != 'false'

    db.session.commit()
    log_audit('custom_check_update', target=check.name)
    return jsonify({'success': True, 'check': check.to_dict()})


@dashboard_bp.route('/api/custom-checks/<int:check_id>', methods=['DELETE'])
@operator_required
def api_delete_custom_check(check_id):
    """Delete a custom check."""
    from app.models import CustomCheck
    check = CustomCheck.query.get(check_id)
    if not check:
        return jsonify({'success': False, 'error': 'Check not found.'}), 404
    if check.created_by != current_user.id and not current_user.is_admin:
        return jsonify({'success': False, 'error': 'Permission denied.'}), 403

    name = check.name
    db.session.delete(check)
    db.session.commit()
    log_audit('custom_check_delete', target=name)
    return jsonify({'success': True})


@dashboard_bp.route('/api/custom-checks/export', methods=['GET'])
@login_required
def api_export_custom_checks():
    """Export all custom checks for this user as a JSON file."""
    from app.models import CustomCheck
    checks = CustomCheck.query.filter_by(created_by=current_user.id).order_by(CustomCheck.name).all()
    export_data = {
        'version': 1,
        'exported_by': current_user.username,
        'exported_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'checks': [],
    }
    for cc in checks:
        export_data['checks'].append({
            'name': cc.name,
            'check_type': cc.check_type or 'command',
            'command': cc.command or '',
            'script_content': cc.script_content or '',
            'script_filename': cc.script_filename or '',
            'expected_value': cc.expected_value or '',
            'match_type': cc.match_type or 'contains',
            'description': cc.description or '',
            'run_with': cc.run_with or 'health_check',
            'linked_scenario': cc.linked_scenario or '',
            'enabled': cc.enabled,
        })

    from flask import Response
    import json as _json
    payload = _json.dumps(export_data, indent=2)
    filename = f'custom_checks_{current_user.username}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    log_audit('custom_check_export', details=f'{len(checks)} checks exported')
    return Response(
        payload,
        mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


@dashboard_bp.route('/api/custom-checks/import', methods=['POST'])
@operator_required
def api_import_custom_checks():
    """Import custom checks from a JSON file."""
    from app.models import CustomCheck
    import json as _json

    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({'success': False, 'error': 'No file uploaded.'}), 400

    try:
        raw = file.read().decode('utf-8', errors='replace')
        data = _json.loads(raw)
    except Exception as e:
        return jsonify({'success': False, 'error': f'Invalid JSON file: {e}'}), 400

    checks_data = data.get('checks', [])
    if not checks_data:
        return jsonify({'success': False, 'error': 'No checks found in the file.'}), 400

    mode = request.form.get('mode', 'merge')  # merge | replace

    if mode == 'replace':
        # Delete existing checks for this user before importing
        CustomCheck.query.filter_by(created_by=current_user.id).delete()
        db.session.flush()

    imported = 0
    skipped = 0
    for item in checks_data:
        name = item.get('name', '').strip()
        if not name:
            skipped += 1
            continue

        # In merge mode, skip if same name already exists
        if mode == 'merge':
            existing = CustomCheck.query.filter_by(created_by=current_user.id, name=name).first()
            if existing:
                skipped += 1
                continue

        check_type = item.get('check_type', 'command')
        command = item.get('command', '').strip()
        script_content = item.get('script_content', '').strip()

        if check_type == 'command' and not command:
            skipped += 1
            continue
        if check_type == 'script' and not script_content:
            skipped += 1
            continue

        cc = CustomCheck(
            name=name,
            check_type=check_type,
            command=command,
            script_content=script_content or None,
            script_filename=item.get('script_filename', ''),
            expected_value=item.get('expected_value', ''),
            match_type=item.get('match_type', 'contains'),
            description=item.get('description', ''),
            run_with=item.get('run_with', 'health_check'),
            linked_scenario=item.get('linked_scenario', '').strip() or None,
            enabled=item.get('enabled', True),
            created_by=current_user.id,
        )
        db.session.add(cc)
        imported += 1

    db.session.commit()
    log_audit('custom_check_import', details=f'{imported} imported, {skipped} skipped (mode={mode})')
    return jsonify({
        'success': True,
        'imported': imported,
        'skipped': skipped,
        'total': len(checks_data),
    })


def _update_env_var(key, value):
    from pathlib import Path
    installed_cfg = Path.home() / ".config" / "cnv-healthcrew" / "config.env"
    if installed_cfg.exists():
        env_file = str(installed_cfg)
    else:
        env_file = os.path.join(BASE_DIR, ".env")

    lines = []
    found = False
    if os.path.exists(env_file):
        with open(env_file, 'r') as f:
            for line in f:
                if line.strip().startswith(f'{key}='):
                    lines.append(f'{key}={value}\n')
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f'{key}={value}\n')
    with open(env_file, 'w') as f:
        f.writelines(lines)
