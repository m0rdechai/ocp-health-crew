"""
CNV Health Dashboard - Admin Blueprint

User management, audit log viewing, and knowledge base CRUD for admin users.
"""

from functools import wraps
from flask import Blueprint, render_template, redirect, url_for, request, jsonify, flash
from flask_login import login_required, current_user
from app.models import db, User, AuditLog
from app.auth import log_audit
from healthchecks.knowledge_base import (
    load_known_issues, load_known_bugs, save_known_issue, save_known_bug,
    delete_known_issue, delete_known_bug, get_stats,
)

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')


def admin_required(f):
    """Decorator to restrict access to admin users only."""
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not current_user.is_admin:
            return "Access denied. Admin role required.", 403
        return f(*args, **kwargs)
    return decorated


@admin_bp.route('/users')
@admin_required
def users():
    """User management page."""
    all_users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin_users.html', users=all_users, active_page='admin')


@admin_bp.route('/users/create', methods=['POST'])
@admin_required
def create_user():
    """Create a new user."""
    username = request.form.get('username', '').strip()
    email = request.form.get('email', '').strip()
    password = request.form.get('password', '')
    role = request.form.get('role', 'operator')

    if not username or not email or not password:
        return jsonify({'success': False, 'error': 'All fields are required.'})

    if User.query.filter_by(username=username).first():
        return jsonify({'success': False, 'error': 'Username already taken.'})

    if User.query.filter_by(email=email).first():
        return jsonify({'success': False, 'error': 'Email already registered.'})

    if role not in ('admin', 'operator', 'viewer'):
        return jsonify({'success': False, 'error': 'Invalid role.'})

    user = User(username=username, email=email, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    log_audit('user_create', target=f'User {username}', details=f'Role: {role}')
    return jsonify({'success': True, 'message': f'User {username} created.'})


@admin_bp.route('/users/<int:user_id>/update', methods=['POST'])
@admin_required
def update_user(user_id):
    """Update a user's role."""
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({'success': False, 'error': 'User not found.'})

    # Prevent demoting self
    if user.id == current_user.id:
        return jsonify({'success': False, 'error': 'Cannot change your own role.'})

    new_role = request.form.get('role', user.role)
    if new_role not in ('admin', 'operator', 'viewer'):
        return jsonify({'success': False, 'error': 'Invalid role.'})

    old_role = user.role
    user.role = new_role
    db.session.commit()

    log_audit('user_update', target=f'User {user.username}',
              details=f'Role: {old_role} -> {new_role}')
    return jsonify({'success': True, 'message': f'User {user.username} updated.'})


@admin_bp.route('/users/<int:user_id>/reset-password', methods=['POST'])
@admin_required
def reset_user_password(user_id):
    """Reset a user's password."""
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({'success': False, 'error': 'User not found.'})

    new_password = request.form.get('password', '')
    if len(new_password) < 6:
        return jsonify({'success': False, 'error': 'Password must be at least 6 characters.'})

    user.set_password(new_password)
    db.session.commit()

    log_audit('password_reset', target=f'User {user.username}',
              details='Password reset by admin')
    return jsonify({'success': True, 'message': f'Password reset for {user.username}.'})


@admin_bp.route('/users/<int:user_id>/delete', methods=['POST'])
@admin_required
def delete_user(user_id):
    """Delete a user."""
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({'success': False, 'error': 'User not found.'})

    if user.id == current_user.id:
        return jsonify({'success': False, 'error': 'Cannot delete yourself.'})

    username = user.username
    db.session.delete(user)
    db.session.commit()

    log_audit('user_delete', target=f'User {username}')
    return jsonify({'success': True, 'message': f'User {username} deleted.'})


@admin_bp.route('/audit')
@admin_required
def audit_log():
    """Audit log page."""
    page = request.args.get('page', 1, type=int)
    per_page = 50
    action_filter = request.args.get('action', '')

    query = AuditLog.query.order_by(AuditLog.timestamp.desc())
    if action_filter:
        query = query.filter(AuditLog.action == action_filter)

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    logs = pagination.items

    # Get unique actions for filter dropdown
    actions = db.session.query(AuditLog.action).distinct().all()
    actions = sorted([a[0] for a in actions])

    return render_template('admin_audit.html',
                           logs=logs,
                           pagination=pagination,
                           actions=actions,
                           current_action=action_filter,
                           active_page='admin')


# -----------------------------------------------------------------------
# Knowledge Base management
# -----------------------------------------------------------------------

@admin_bp.route('/knowledge')
@admin_required
def knowledge():
    """Knowledge Base management page."""
    issues = load_known_issues()
    bugs = load_known_bugs()
    stats = get_stats()
    source_filter = request.args.get('source', '')
    if source_filter:
        issues = {k: v for k, v in issues.items() if v.get('source') == source_filter}
    return render_template('admin_knowledge.html',
                           issues=issues, bugs=bugs, stats=stats,
                           source_filter=source_filter, active_page='admin')


@admin_bp.route('/api/knowledge/issues', methods=['GET'])
@admin_required
def api_list_issues():
    return jsonify(load_known_issues())


@admin_bp.route('/api/knowledge/issues', methods=['POST'])
@admin_required
def api_create_issue():
    data = request.get_json(force=True)
    key = data.get('key', '').strip()
    if not key:
        return jsonify({'success': False, 'error': 'Key is required'}), 400
    issues = load_known_issues()
    if key in issues:
        return jsonify({'success': False, 'error': f'Key "{key}" already exists'}), 409

    from datetime import datetime
    entry = {
        'pattern': [k.strip() for k in data.get('pattern', '').split(',') if k.strip()],
        'jira': [j.strip() for j in data.get('jira', '').split(',') if j.strip()],
        'title': data.get('title', key),
        'description': data.get('description', ''),
        'root_cause': [r.strip() for r in data.get('root_cause', '').split('\n') if r.strip()],
        'suggestions': [s.strip() for s in data.get('suggestions', '').split('\n') if s.strip()],
        'verify_cmd': data.get('verify_cmd', ''),
        'source': 'user',
        'confidence': 1.0,
        'created': datetime.now().isoformat(),
        'last_matched': None,
        'investigation_commands': [],
    }
    save_known_issue(key, entry)
    log_audit('kb_create_issue', target=key, details=f'Source: user')
    return jsonify({'success': True, 'message': f'Pattern "{key}" created'})


@admin_bp.route('/api/knowledge/issues/<key>', methods=['PUT'])
@admin_required
def api_update_issue(key):
    issues = load_known_issues()
    if key not in issues:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    data = request.get_json(force=True)
    entry = issues[key]
    if 'pattern' in data:
        entry['pattern'] = [k.strip() for k in data['pattern'].split(',') if k.strip()]
    if 'jira' in data:
        entry['jira'] = [j.strip() for j in data['jira'].split(',') if j.strip()]
    for field in ('title', 'description', 'verify_cmd'):
        if field in data:
            entry[field] = data[field]
    if 'root_cause' in data:
        entry['root_cause'] = [r.strip() for r in data['root_cause'].split('\n') if r.strip()]
    if 'suggestions' in data:
        entry['suggestions'] = [s.strip() for s in data['suggestions'].split('\n') if s.strip()]
    save_known_issue(key, entry)
    log_audit('kb_update_issue', target=key)
    return jsonify({'success': True, 'message': f'Pattern "{key}" updated'})


@admin_bp.route('/api/knowledge/issues/<key>', methods=['DELETE'])
@admin_required
def api_delete_issue(key):
    if delete_known_issue(key):
        log_audit('kb_delete_issue', target=key)
        return jsonify({'success': True, 'message': f'Pattern "{key}" deleted'})
    return jsonify({'success': False, 'error': 'Not found'}), 404


@admin_bp.route('/api/knowledge/bugs', methods=['GET'])
@admin_required
def api_list_bugs():
    return jsonify(load_known_bugs())


@admin_bp.route('/api/knowledge/bugs', methods=['POST'])
@admin_required
def api_create_bug():
    data = request.get_json(force=True)
    jira_key = data.get('jira_key', '').strip()
    if not jira_key:
        return jsonify({'success': False, 'error': 'Jira key is required'}), 400

    from datetime import datetime
    entry = {
        'status': data.get('status', 'Open'),
        'resolution': data.get('resolution') or None,
        'fix_versions': [v.strip() for v in data.get('fix_versions', '').split(',') if v.strip()],
        'affects': [a.strip() for a in data.get('affects', '').split(',') if a.strip()],
        'source': 'user',
        'last_updated': datetime.now().isoformat(),
    }
    save_known_bug(jira_key, entry)
    log_audit('kb_create_bug', target=jira_key)
    return jsonify({'success': True, 'message': f'Bug "{jira_key}" added'})


@admin_bp.route('/api/knowledge/bugs/<jira_key>', methods=['PUT'])
@admin_required
def api_update_bug(jira_key):
    bugs = load_known_bugs()
    if jira_key not in bugs:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    data = request.get_json(force=True)
    entry = bugs[jira_key]
    for field in ('status', 'resolution'):
        if field in data:
            entry[field] = data[field] or None
    if 'fix_versions' in data:
        entry['fix_versions'] = [v.strip() for v in data['fix_versions'].split(',') if v.strip()]
    if 'affects' in data:
        entry['affects'] = [a.strip() for a in data['affects'].split(',') if a.strip()]
    save_known_bug(jira_key, entry)
    log_audit('kb_update_bug', target=jira_key)
    return jsonify({'success': True, 'message': f'Bug "{jira_key}" updated'})


@admin_bp.route('/api/knowledge/bugs/<jira_key>', methods=['DELETE'])
@admin_required
def api_delete_bug(jira_key):
    if delete_known_bug(jira_key):
        log_audit('kb_delete_bug', target=jira_key)
        return jsonify({'success': True, 'message': f'Bug "{jira_key}" deleted'})
    return jsonify({'success': False, 'error': 'Not found'}), 404


@admin_bp.route('/api/knowledge/bugs/refresh', methods=['POST'])
@admin_required
def api_refresh_bugs():
    """Refresh bug statuses from Jira (uses Jira REST API if configured).

    Requires JIRA_URL and JIRA_TOKEN environment variables. Falls back
    to a no-op if not configured.
    """
    import os
    import requests
    from datetime import datetime

    jira_url = os.getenv('JIRA_URL', 'https://issues.redhat.com')
    jira_token = os.getenv('JIRA_TOKEN', '')

    if not jira_token:
        return jsonify({
            'success': False,
            'error': 'JIRA_TOKEN not set. Configure it to enable live refresh.'
        }), 400

    bugs = load_known_bugs()
    updated = 0
    errors = []

    for jira_key in list(bugs.keys()):
        if jira_key.startswith(('OCPBUGS-storage', 'OCPBUGS-general', 'CNV-storage')):
            continue
        try:
            resp = requests.get(
                f'{jira_url}/rest/api/2/issue/{jira_key}',
                headers={'Authorization': f'Bearer {jira_token}'},
                params={'fields': 'status,resolution,fixVersions,versions'},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                fields = data.get('fields', {})
                bugs[jira_key]['status'] = fields.get('status', {}).get('name', bugs[jira_key].get('status'))
                res = fields.get('resolution')
                bugs[jira_key]['resolution'] = res.get('name') if res else None
                bugs[jira_key]['fix_versions'] = [v['name'] for v in fields.get('fixVersions', [])]
                bugs[jira_key]['affects'] = [v['name'] for v in fields.get('versions', [])]
                bugs[jira_key]['last_updated'] = datetime.now().isoformat()
                updated += 1
            elif resp.status_code != 404:
                errors.append(f'{jira_key}: HTTP {resp.status_code}')
        except Exception as exc:
            errors.append(f'{jira_key}: {str(exc)[:80]}')

    if updated:
        from healthchecks.knowledge_base import _write_json, KNOWN_BUGS_FILE
        _write_json(KNOWN_BUGS_FILE, bugs)
        log_audit('kb_refresh_bugs', details=f'Updated {updated} bugs')

    msg = f'Updated {updated}/{len(bugs)} bugs.'
    if errors:
        msg += f' Errors: {len(errors)}'
    return jsonify({'success': True, 'message': msg, 'updated': updated, 'errors': errors})
