#!/usr/bin/env python3
"""
CNV HealthCrew AI - Professional Edition
- Fast single SSH connection
- Beautiful HTML reports
- Email notifications
- Optional AI analysis
- Jira bug status checking
- Automatic new check suggestions from Jira
"""

import os
import sys
import re
import json
import subprocess
import paramiko

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# Jira bug status cache (populated at runtime)
JIRA_BUG_CACHE = {}

# Configuration
HOST = os.getenv("RH_LAB_HOST")
USER = os.getenv("RH_LAB_USER", "root")
KEY_PATH = os.getenv("SSH_KEY_PATH")
KUBECONFIG = "/home/kni/clusterconfigs/auth/kubeconfig"

# Email Configuration
EMAIL_TO = os.getenv("EMAIL_TO", "guchen@redhat.com")
EMAIL_FROM = os.getenv("EMAIL_FROM", "cnv-healthcrew@redhat.com")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.corp.redhat.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "25"))
DASHBOARD_BASE_URL = os.getenv("DASHBOARD_BASE_URL", "http://10.46.254.144:5000")


def send_email_report(html_path, recipient=None, subject=None, cluster_name=None, issue_count=0, report_data=None):
    """
    Send a beautiful HTML email summary matching the dashboard style.
    
    Args:
        html_path: Path to the HTML report file
        recipient: Email recipient (defaults to EMAIL_TO)
        subject: Email subject (auto-generated if not provided)
        cluster_name: Cluster name for the subject line
        issue_count: Number of issues found (for subject line)
        report_data: Dict containing report data for email body
    
    Returns:
        bool: True if email sent successfully, False otherwise
    """
    recipient = recipient or EMAIL_TO
    
    # Generate subject if not provided
    if not subject:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        status = "⚠️ ISSUES FOUND" if issue_count > 0 else "✅ HEALTHY"
        lab_or_cluster = LAB_NAME or cluster_name or ''
        subject = f"[CNV HealthCrew AI] {status} - {lab_or_cluster} ({timestamp})" if lab_or_cluster else f"[CNV HealthCrew AI] {status} ({timestamp})"
    
    try:
        # Extract data for email summary
        data = report_data or {}
        version = data.get('version', 'N/A')
        
        # Node stats
        nodes = data.get('nodes', {})
        healthy_nodes = len(nodes.get('healthy', []))
        unhealthy_nodes = len(nodes.get('unhealthy', []))
        total_nodes = healthy_nodes + unhealthy_nodes
        
        # Operator stats
        operators = data.get('operators', {})
        healthy_ops = len(operators.get('healthy', []))
        degraded_ops = len(operators.get('degraded', []))
        unavailable_ops = len(operators.get('unavailable', []))
        total_ops = healthy_ops + degraded_ops + unavailable_ops
        
        # Pod stats
        pods = data.get('pods', {})
        healthy_pods = pods.get('healthy', 0)
        unhealthy_pods_list = pods.get('unhealthy', [])
        unhealthy_pods = len(unhealthy_pods_list)
        total_pods = healthy_pods + unhealthy_pods
        
        # VM stats
        vms = data.get('vms', {})
        running_vms = len(vms.get('running', []))
        stopped_vms = len(vms.get('stopped', []))
        total_vms = running_vms + stopped_vms
        
        # KubeVirt stats
        kubevirt = data.get('kubevirt', {})
        failed_vmis = kubevirt.get('failed_vmis', [])
        migrations = kubevirt.get('migrations', [])
        running_migrations = len([m for m in migrations if isinstance(m, dict) and m.get('status') == 'Running'])
        
        # ETCD stats
        etcd = data.get('etcd', {})
        etcd_members = etcd.get('member_count', 0) if isinstance(etcd, dict) else 0
        
        # PVC stats
        pvcs = data.get('pvcs', {})
        pending_pvcs = len(pvcs.get('pending', [])) if isinstance(pvcs, dict) else 0
        
        # OOM events
        oom_events = len(data.get('oom_events', []))
        
        # Build report URL for the CTA button
        report_filename = os.path.basename(html_path)
        report_url = f"{DASHBOARD_BASE_URL}/report/{report_filename}"
        
        # Status styling
        if issue_count > 0:
            status_text = "ATTENTION NEEDED"
            status_color = "#ff6b6b"
        else:
            status_text = "ALL SYSTEMS HEALTHY"
            status_color = "#73BF69"
        
        # Helper function to create gauge SVG (email-safe version using arc)
        def create_gauge(value, total, color="#73BF69"):
            if total == 0:
                percent = 100
            else:
                percent = (value / total) * 100
            # Create a simple circular progress indicator using borders
            return f'''<div style="width:80px;height:80px;margin:0 auto;position:relative;">
                <div style="width:80px;height:80px;border-radius:50%;border:8px solid #2a2a3e;box-sizing:border-box;"></div>
                <div style="position:absolute;top:0;left:0;width:80px;height:80px;border-radius:50%;border:8px solid {color};border-color:{color} {color} transparent transparent;box-sizing:border-box;transform:rotate({int(percent * 1.8 - 45)}deg);"></div>
            </div>'''
        
        # Build unhealthy pods HTML
        unhealthy_pods_html = ""
        if unhealthy_pods_list:
            pods_rows = ""
            for pod in unhealthy_pods_list[:6]:  # Show max 6
                if isinstance(pod, dict):
                    pod_name = pod.get('name', 'unknown')
                    pod_ns = pod.get('namespace', '')
                    pod_status = pod.get('status', 'Error')
                    # Truncate long names
                    if len(pod_name) > 40:
                        pod_name = pod_name[:37] + "..."
                    status_bg = "#ff6b6b" if 'Error' in pod_status or 'Crash' in pod_status else "#ffaa00"
                    pods_rows += f'''<tr>
                        <td style="padding:8px 12px;color:#8b8fa3;font-size:11px;border-bottom:1px solid #2a2a3e;">{pod_ns}</td>
                        <td style="padding:8px 12px;color:#e0e0e0;font-size:12px;border-bottom:1px solid #2a2a3e;">{pod_name}</td>
                        <td style="padding:8px 12px;text-align:right;border-bottom:1px solid #2a2a3e;">
                            <span style="background:{status_bg};color:#fff;padding:2px 8px;border-radius:4px;font-size:10px;">{pod_status}</span>
                        </td>
                    </tr>'''
            
            remaining = len(unhealthy_pods_list) - 6
            if remaining > 0:
                pods_rows += f'''<tr><td colspan="3" style="padding:8px 12px;color:#8b8fa3;font-size:11px;text-align:center;">...and {remaining} more in full report</td></tr>'''
            
            unhealthy_pods_html = f'''
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#1e1e2e;border-radius:12px;margin-top:16px;overflow:hidden;">
                <tr>
                    <td style="padding:16px 20px;border-bottom:1px solid #2a2a3e;">
                        <span style="color:#ff6b6b;font-size:13px;font-weight:600;">⚠️ UNHEALTHY PODS ({unhealthy_pods})</span>
                    </td>
                </tr>
                <tr>
                    <td style="padding:0;">
                        <table width="100%" cellpadding="0" cellspacing="0">
                            {pods_rows}
                        </table>
                    </td>
                </tr>
            </table>'''
        
        # Create beautiful dark-themed HTML email matching the dashboard
        html_content = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;background:#0d0d14;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#0d0d14;padding:20px 0;">
        <tr>
            <td align="center">
                <table width="700" cellpadding="0" cellspacing="0" style="background:#13131f;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.4);">
                    
                    <!-- Header Bar -->
                    <tr>
                        <td style="background:linear-gradient(90deg,#1a1a2e 0%,#16213e 100%);padding:16px 24px;">
                            <table width="100%" cellpadding="0" cellspacing="0">
                                <tr>
                                    <td>
                                        <span style="color:#73BF69;font-size:18px;font-weight:700;">CNV</span>
                                        <span style="color:#ffffff;font-size:18px;font-weight:300;"> HealthCrew</span>
                                        <span style="color:#73BF69;font-size:18px;font-weight:700;"> AI</span>
                                    </td>
                                    <td style="text-align:right;">
                                        <span style="background:{status_color};color:#fff;padding:6px 16px;border-radius:6px;font-size:12px;font-weight:600;">{status_text}</span>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
                    
                    <!-- Cluster Info -->
                    <tr>
                        <td style="padding:20px 24px;border-bottom:1px solid #2a2a3e;">
                            <table width="100%" cellpadding="0" cellspacing="0">
                                <tr>
                                    <td style="color:#ffffff;font-size:20px;font-weight:600;padding-bottom:4px;">
                                        {LAB_NAME or cluster_name or 'Cluster Health Report'}
                                    </td>
                                </tr>
                                {'<tr><td style="padding-bottom:8px;"><span style="color:#8b8fa3;font-size:13px;">' + cluster_name + '</span></td></tr>' if LAB_NAME and cluster_name else ''}
                                <tr>
                                    <td>
                                        <table cellpadding="0" cellspacing="0">
                                            <tr>
                                                <td style="padding-right:24px;">
                                                    <span style="color:#73BF69;font-size:12px;">📅</span>
                                                    <span style="color:#8b8fa3;font-size:12px;"> {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</span>
                                                </td>
                                                <td style="padding-right:24px;">
                                                    <span style="color:#73BF69;font-size:12px;">🏷️</span>
                                                    <span style="color:#8b8fa3;font-size:12px;"> Version {version}</span>
                                                </td>
                                                <td>
                                                    <span style="color:#73BF69;font-size:12px;">🔍</span>
                                                    <span style="color:#8b8fa3;font-size:12px;"> 17 Health Checks</span>
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
                    
                    <!-- Main Stats Cards - Row 1 -->
                    <tr>
                        <td style="padding:20px 24px 10px;">
                            <table width="100%" cellpadding="0" cellspacing="0">
                                <tr>
                                    <!-- NODES Card -->
                                    <td width="24%" style="vertical-align:top;">
                                        <table width="100%" cellpadding="0" cellspacing="0" style="background:#1e1e2e;border-radius:12px;overflow:hidden;">
                                            <tr>
                                                <td style="padding:16px;text-align:center;">
                                                    <div style="color:#8b8fa3;font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px;">🖥️ NODES</div>
                                                    <div style="width:70px;height:70px;margin:0 auto 12px;border-radius:50%;border:6px solid #2a2a3e;border-top-color:{'#73BF69' if unhealthy_nodes == 0 else '#ff6b6b'};border-right-color:{'#73BF69' if unhealthy_nodes == 0 else '#ff6b6b'};"></div>
                                                    <div style="color:{'#73BF69' if unhealthy_nodes == 0 else '#ff6b6b'};font-size:28px;font-weight:700;">{healthy_nodes}<span style="color:#8b8fa3;font-size:14px;font-weight:400;">/{total_nodes}</span></div>
                                                    <div style="color:#8b8fa3;font-size:11px;margin-top:4px;">Ready</div>
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                    <td width="2%"></td>
                                    <!-- OPERATORS Card -->
                                    <td width="24%" style="vertical-align:top;">
                                        <table width="100%" cellpadding="0" cellspacing="0" style="background:#1e1e2e;border-radius:12px;overflow:hidden;">
                                            <tr>
                                                <td style="padding:16px;text-align:center;">
                                                    <div style="color:#8b8fa3;font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px;">⚙️ OPERATORS</div>
                                                    <div style="width:70px;height:70px;margin:0 auto 12px;border-radius:50%;border:6px solid #2a2a3e;border-top-color:{'#73BF69' if degraded_ops + unavailable_ops == 0 else '#ff6b6b'};border-right-color:{'#73BF69' if degraded_ops + unavailable_ops == 0 else '#ff6b6b'};"></div>
                                                    <div style="color:{'#73BF69' if degraded_ops + unavailable_ops == 0 else '#ff6b6b'};font-size:28px;font-weight:700;">{healthy_ops}<span style="color:#8b8fa3;font-size:14px;font-weight:400;">/{total_ops}</span></div>
                                                    <div style="color:#8b8fa3;font-size:11px;margin-top:4px;">Available</div>
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                    <td width="2%"></td>
                                    <!-- PODS Card -->
                                    <td width="24%" style="vertical-align:top;">
                                        <table width="100%" cellpadding="0" cellspacing="0" style="background:#1e1e2e;border-radius:12px;overflow:hidden;">
                                            <tr>
                                                <td style="padding:16px;text-align:center;">
                                                    <div style="color:#8b8fa3;font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px;">📦 PODS</div>
                                                    <div style="width:70px;height:70px;margin:0 auto 12px;border-radius:50%;border:6px solid #2a2a3e;border-top-color:{'#73BF69' if unhealthy_pods == 0 else '#ffaa00'};border-right-color:{'#73BF69' if unhealthy_pods == 0 else '#ffaa00'};"></div>
                                                    <div style="color:{'#73BF69' if unhealthy_pods == 0 else '#ffaa00'};font-size:28px;font-weight:700;">{healthy_pods}<span style="color:#8b8fa3;font-size:14px;font-weight:400;">/{total_pods}</span></div>
                                                    <div style="color:#8b8fa3;font-size:11px;margin-top:4px;">Running</div>
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                    <td width="2%"></td>
                                    <!-- VMS Card -->
                                    <td width="24%" style="vertical-align:top;">
                                        <table width="100%" cellpadding="0" cellspacing="0" style="background:#1e1e2e;border-radius:12px;overflow:hidden;">
                                            <tr>
                                                <td style="padding:16px;text-align:center;">
                                                    <div style="color:#8b8fa3;font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px;">🖧 VMS</div>
                                                    <div style="width:70px;height:70px;margin:0 auto 12px;border-radius:50%;border:6px solid #2a2a3e;border-top-color:#73BF69;border-right-color:#73BF69;"></div>
                                                    <div style="color:#73BF69;font-size:28px;font-weight:700;">{running_vms}<span style="color:#8b8fa3;font-size:14px;font-weight:400;">/{total_vms}</span></div>
                                                    <div style="color:#8b8fa3;font-size:11px;margin-top:4px;">Running</div>
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
                    
                    <!-- Stats Cards - Row 2 -->
                    <tr>
                        <td style="padding:10px 24px 20px;">
                            <table width="100%" cellpadding="0" cellspacing="0">
                                <tr>
                                    <!-- ETCD Card -->
                                    <td width="24%" style="vertical-align:top;">
                                        <table width="100%" cellpadding="0" cellspacing="0" style="background:#1e1e2e;border-radius:12px;overflow:hidden;">
                                            <tr>
                                                <td style="padding:16px;text-align:center;">
                                                    <div style="color:#8b8fa3;font-size:10px;font-weight:500;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;">🗄️ ETCD MEMBERS</div>
                                                    <div style="color:#73BF69;font-size:32px;font-weight:700;">{etcd_members}</div>
                                                    <div style="color:#8b8fa3;font-size:11px;margin-top:4px;">Healthy</div>
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                    <td width="2%"></td>
                                    <!-- PVCs Card -->
                                    <td width="24%" style="vertical-align:top;">
                                        <table width="100%" cellpadding="0" cellspacing="0" style="background:#1e1e2e;border-radius:12px;overflow:hidden;">
                                            <tr>
                                                <td style="padding:16px;text-align:center;">
                                                    <div style="color:#8b8fa3;font-size:10px;font-weight:500;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;">💾 PVCS PENDING</div>
                                                    <div style="color:{'#73BF69' if pending_pvcs == 0 else '#ffaa00'};font-size:32px;font-weight:700;">{pending_pvcs}</div>
                                                    <div style="color:#8b8fa3;font-size:11px;margin-top:4px;">&nbsp;</div>
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                    <td width="2%"></td>
                                    <!-- OOM Card -->
                                    <td width="24%" style="vertical-align:top;">
                                        <table width="100%" cellpadding="0" cellspacing="0" style="background:#1e1e2e;border-radius:12px;overflow:hidden;">
                                            <tr>
                                                <td style="padding:16px;text-align:center;">
                                                    <div style="color:#8b8fa3;font-size:10px;font-weight:500;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;">💥 OOM EVENTS</div>
                                                    <div style="color:{'#73BF69' if oom_events == 0 else '#ff6b6b'};font-size:32px;font-weight:700;">{oom_events}</div>
                                                    <div style="color:#8b8fa3;font-size:11px;margin-top:4px;">Recent</div>
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                    <td width="2%"></td>
                                    <!-- Migrations Card -->
                                    <td width="24%" style="vertical-align:top;">
                                        <table width="100%" cellpadding="0" cellspacing="0" style="background:#1e1e2e;border-radius:12px;overflow:hidden;">
                                            <tr>
                                                <td style="padding:16px;text-align:center;">
                                                    <div style="color:#8b8fa3;font-size:10px;font-weight:500;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;">🔄 MIGRATIONS</div>
                                                    <div style="color:#73BF69;font-size:32px;font-weight:700;">{running_migrations}</div>
                                                    <div style="color:#8b8fa3;font-size:11px;margin-top:4px;">Running</div>
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
                    
                    <!-- Unhealthy Pods Section -->
                    <tr>
                        <td style="padding:0 24px 20px;">
                            {unhealthy_pods_html}
                        </td>
                    </tr>
                    
                    <!-- CTA Button -->
                    <tr>
                        <td style="padding:0 24px 24px;">
                            <table width="100%" cellpadding="0" cellspacing="0">
                                <tr>
                                    <td align="center">
                                        <table cellpadding="0" cellspacing="0" style="background:linear-gradient(135deg,#73BF69 0%,#5ba350 100%);border-radius:8px;">
                                            <tr>
                                                <td style="padding:14px 32px;color:#ffffff;font-weight:600;font-size:14px;">
                                                    📎 Full Interactive Report Attached — Open in Browser
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
                    
                    <!-- Footer -->
                    <tr>
                        <td style="background:#1a1a2e;padding:16px 24px;border-top:1px solid #2a2a3e;">
                            <table width="100%" cellpadding="0" cellspacing="0">
                                <tr>
                                    <td style="color:#8b8fa3;font-size:11px;text-align:center;">
                                        <strong style="color:#73BF69;">CNV HealthCrew AI</strong> • Performance Engineering Team<br>
                                        <span style="font-size:10px;color:#5f6368;">Automated health check report • {datetime.now().strftime("%Y-%m-%d")}</span>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
                    
                </table>
            </td>
        </tr>
    </table>
</body>
</html>'''
        
        # Build detailed findings section (email-safe tables)
        findings_html = ""
        
        # Degraded / unavailable operators
        degraded_list = operators.get('degraded', [])
        unavailable_list = operators.get('unavailable', [])
        if degraded_list or unavailable_list:
            op_rows = ""
            for op in degraded_list:
                op_rows += f'<tr><td style="padding:8px 12px;color:#e0e0e0;font-size:12px;font-family:monospace;border-bottom:1px solid #2a2a3e;">{op}</td><td style="padding:8px 12px;text-align:right;border-bottom:1px solid #2a2a3e;"><span style="background:#FF9830;color:#fff;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;">DEGRADED</span></td></tr>'
            for op in unavailable_list:
                op_rows += f'<tr><td style="padding:8px 12px;color:#e0e0e0;font-size:12px;font-family:monospace;border-bottom:1px solid #2a2a3e;">{op}</td><td style="padding:8px 12px;text-align:right;border-bottom:1px solid #2a2a3e;"><span style="background:#F2495C;color:#fff;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;">UNAVAILABLE</span></td></tr>'
            findings_html += f'''
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#1e1e2e;border-radius:12px;margin-bottom:16px;overflow:hidden;">
                <tr><td style="padding:14px 20px;border-bottom:1px solid #2a2a3e;"><span style="color:#FF9830;font-size:13px;font-weight:600;">⚙️ DEGRADED CLUSTER OPERATORS ({len(degraded_list) + len(unavailable_list)})</span></td></tr>
                <tr><td style="padding:0;"><table width="100%" cellpadding="0" cellspacing="0">{op_rows}</table></td></tr>
            </table>'''
        
        # Health check summary table
        check_items = [
            ("🖥️", "Nodes", f"{healthy_nodes}/{total_nodes} Ready", unhealthy_nodes == 0),
            ("⚙️", "Cluster Operators", f"{healthy_ops}/{total_ops} Available", degraded_ops + unavailable_ops == 0),
            ("📦", "Pods", f"{healthy_pods}/{total_pods} Running", unhealthy_pods == 0),
            ("🗄️", "etcd", f"{etcd_members} members healthy", True),
            ("💾", "PVCs", f"{pending_pvcs} pending" if pending_pvcs > 0 else "All Bound", pending_pvcs == 0),
            ("🔄", "VM Migrations", f"{running_migrations} running", True),
            ("💥", "OOM Events", f"{oom_events}" if oom_events > 0 else "None", oom_events == 0),
        ]
        
        # Add CNV checks if available
        virt_handler = data.get('virt_handler', {})
        if isinstance(virt_handler, dict):
            vh_count = len(virt_handler.get('pods', []))
            vh_unhealthy = len(virt_handler.get('unhealthy', []))
            if vh_count > 0:
                check_items.append(("🔧", "virt-handler", f"{vh_count - vh_unhealthy}/{vh_count} healthy", vh_unhealthy == 0))
        
        check_rows = ""
        for icon, name, result, is_ok in check_items:
            status_icon = "✅" if is_ok else "❌"
            result_color = "#73BF69" if is_ok else "#FF9830"
            check_rows += f'''<tr>
                <td style="padding:10px 16px;border-bottom:1px solid #2a2a3e;font-size:14px;width:30px;">{status_icon}</td>
                <td style="padding:10px 8px;border-bottom:1px solid #2a2a3e;color:#e0e0e0;font-size:13px;font-weight:600;">{icon} {name}</td>
                <td style="padding:10px 16px;border-bottom:1px solid #2a2a3e;text-align:right;color:{result_color};font-size:13px;font-weight:600;">{result}</td>
            </tr>'''
        
        findings_html += f'''
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#1e1e2e;border-radius:12px;margin-bottom:16px;overflow:hidden;">
                <tr><td style="padding:14px 20px;border-bottom:1px solid #2a2a3e;"><span style="color:#5794F2;font-size:13px;font-weight:600;">📋 HEALTH CHECK RESULTS</span></td></tr>
                <tr><td style="padding:0;"><table width="100%" cellpadding="0" cellspacing="0">{check_rows}</table></td></tr>
            </table>'''
        
        # Unhealthy nodes details
        unhealthy_node_list = nodes.get('unhealthy', [])
        if unhealthy_node_list:
            node_rows = ""
            for n in unhealthy_node_list[:10]:
                n_name = n.get('name', n) if isinstance(n, dict) else str(n)
                n_status = n.get('status', 'NotReady') if isinstance(n, dict) else 'NotReady'
                node_rows += f'<tr><td style="padding:8px 12px;color:#e0e0e0;font-size:12px;font-family:monospace;border-bottom:1px solid #2a2a3e;">{n_name}</td><td style="padding:8px 12px;text-align:right;border-bottom:1px solid #2a2a3e;"><span style="background:#F2495C;color:#fff;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;">{n_status}</span></td></tr>'
            findings_html += f'''
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#1e1e2e;border-radius:12px;margin-bottom:16px;overflow:hidden;">
                <tr><td style="padding:14px 20px;border-bottom:1px solid #2a2a3e;"><span style="color:#F2495C;font-size:13px;font-weight:600;">🖥️ UNHEALTHY NODES ({len(unhealthy_node_list)})</span></td></tr>
                <tr><td style="padding:0;"><table width="100%" cellpadding="0" cellspacing="0">{node_rows}</table></td></tr>
            </table>'''
        
        # Firing alerts
        alerts = data.get('alerts', [])
        if alerts and isinstance(alerts, list) and len(alerts) > 0:
            alert_rows = ""
            for a in alerts[:15]:
                if isinstance(a, dict):
                    a_name = a.get('name', a.get('alertname', 'Unknown'))
                    a_sev = a.get('severity', 'warning')
                elif isinstance(a, str):
                    a_name = a
                    a_sev = 'warning'
                else:
                    continue
                sev_bg = '#F2495C' if a_sev == 'critical' else '#FF9830'
                alert_rows += f'<tr><td style="padding:8px 12px;color:#e0e0e0;font-size:12px;border-bottom:1px solid #2a2a3e;">{a_name}</td><td style="padding:8px 12px;text-align:right;border-bottom:1px solid #2a2a3e;"><span style="background:{sev_bg};color:#fff;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;">{a_sev.upper()}</span></td></tr>'
            if alert_rows:
                findings_html += f'''
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#1e1e2e;border-radius:12px;margin-bottom:16px;overflow:hidden;">
                <tr><td style="padding:14px 20px;border-bottom:1px solid #2a2a3e;"><span style="color:#FF9830;font-size:13px;font-weight:600;">🔔 FIRING ALERTS ({len(alerts)})</span></td></tr>
                <tr><td style="padding:0;"><table width="100%" cellpadding="0" cellspacing="0">{alert_rows}</table></td></tr>
            </table>'''
        
        # Failed VMIs
        if failed_vmis:
            vmi_rows = ""
            for v in failed_vmis[:10]:
                v_name = v.get('name', v) if isinstance(v, dict) else str(v)
                v_ns = v.get('namespace', '') if isinstance(v, dict) else ''
                v_display = f"{v_ns}/{v_name}" if v_ns else v_name
                vmi_rows += f'<tr><td style="padding:8px 12px;color:#e0e0e0;font-size:12px;font-family:monospace;border-bottom:1px solid #2a2a3e;">{v_display}</td><td style="padding:8px 12px;text-align:right;border-bottom:1px solid #2a2a3e;"><span style="background:#F2495C;color:#fff;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;">FAILED</span></td></tr>'
            findings_html += f'''
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#1e1e2e;border-radius:12px;margin-bottom:16px;overflow:hidden;">
                <tr><td style="padding:14px 20px;border-bottom:1px solid #2a2a3e;"><span style="color:#F2495C;font-size:13px;font-weight:600;">🗄️ FAILED VMIs ({len(failed_vmis)})</span></td></tr>
                <tr><td style="padding:0;"><table width="100%" cellpadding="0" cellspacing="0">{vmi_rows}</table></td></tr>
            </table>'''
        
        # Insert findings into the email HTML before the CTA button
        html_content = html_content.replace(
            '<!-- CTA Button -->',
            f'''<!-- Detailed Findings -->
                    <tr>
                        <td style="padding:0 24px 10px;">
                            {findings_html}
                            {unhealthy_pods_html}
                        </td>
                    </tr>
                    <!-- CTA Button -->'''
        )
        # Remove the old separate unhealthy pods section since it's now inside findings
        html_content = html_content.replace(
            f'''                    <!-- Unhealthy Pods Section -->
                    <tr>
                        <td style="padding:0 24px 20px;">
                            {unhealthy_pods_html}
                        </td>
                    </tr>''',
            ''
        )
        
        # Create message
        msg = MIMEMultipart('mixed')
        msg['Subject'] = subject
        msg['From'] = EMAIL_FROM
        msg['To'] = recipient
        
        # Create alternative part for text/html
        msg_alt = MIMEMultipart('alternative')
        
        # Plain text fallback
        lab_line = f"Lab: {LAB_NAME}\n" if LAB_NAME else ""
        plain_text = f"""CNV HealthCrew AI - Health Check Report

Cluster: {cluster_name or 'N/A'}
{lab_line}Time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
Version: {version}
Status: {status_text}

Nodes:      {healthy_nodes}/{total_nodes} Ready
Operators:  {healthy_ops}/{total_ops} Available
Pods:       {healthy_pods}/{total_pods} Running
VMs:        {running_vms}/{total_vms} Running
ETCD:       {etcd_members} Healthy
PVCs Pending: {pending_pvcs}
OOM Events:   {oom_events}
Migrations:   {running_migrations} Running

{'Issues Found: ' + str(issue_count) if issue_count > 0 else 'No issues detected.'}

Full HTML report attached — open in a browser for the interactive view with RCA details.
        """
        
        # Email body = table-based email-friendly HTML (works in Gmail, Outlook, etc.)
        part1 = MIMEText(plain_text, 'plain')
        part2 = MIMEText(html_content, 'html')
        
        msg_alt.attach(part1)
        msg_alt.attach(part2)
        msg.attach(msg_alt)
        
        # Attach the full HTML report for offline / mobile viewing
        with open(html_path, 'rb') as f:
            attachment = MIMEBase('text', 'html')
            attachment.set_payload(f.read())
            encoders.encode_base64(attachment)
            filename = os.path.basename(html_path)
            attachment.add_header('Content-Disposition', f'attachment; filename="{filename}"')
            msg.attach(attachment)
        
        # Send the email
        print(f"  📧 Connecting to SMTP server ({SMTP_SERVER}:{SMTP_PORT})...", flush=True)
        
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            server.sendmail(EMAIL_FROM, [recipient], msg.as_string())
        
        print(f"  ✅ Email sent successfully to {recipient}", flush=True)
        return True
        
    except FileNotFoundError:
        print(f"  ❌ Email failed: Report file not found: {html_path}", flush=True)
        return False
    except smtplib.SMTPConnectError as e:
        print(f"  ❌ Email failed: Could not connect to SMTP server {SMTP_SERVER}:{SMTP_PORT}", flush=True)
        print(f"     Error: {e}", flush=True)
        print(f"     💡 Tip: Set SMTP_SERVER and SMTP_PORT environment variables", flush=True)
        return False
    except smtplib.SMTPException as e:
        print(f"  ❌ Email failed: SMTP error: {e}", flush=True)
        return False
    except Exception as e:
        print(f"  ❌ Email failed: {e}", flush=True)
        return False


# Parse arguments
USE_AI = "--ai" in sys.argv  # Full RCA with deep investigation
AI_RCA = "--ai-rca" in sys.argv  # Gemini-powered AI root cause analysis
RCA_BUGS = "--rca-bugs" in sys.argv  # Bug matching only (no deep investigation)
RCA_JIRA = "--rca-jira" in sys.argv  # Search Jira for RCA
RCA_EMAIL = "--rca-email" in sys.argv  # Search email for RCA
SEND_EMAIL = "--email" in sys.argv or "-e" in sys.argv
CHECK_JIRA_NEW = "--check-jira" in sys.argv or "--jira" in sys.argv

# Parse --server argument
SERVER_HOST = None
for i, arg in enumerate(sys.argv):
    if arg == '--server' and i + 1 < len(sys.argv):
        SERVER_HOST = sys.argv[i + 1]
        HOST = SERVER_HOST  # Override HOST with command line argument
        break

# Parse --email-to argument
for i, arg in enumerate(sys.argv):
    if arg == '--email-to' and i + 1 < len(sys.argv):
        EMAIL_TO = sys.argv[i + 1]
        break

# Parse --lab-name argument (jumphost label used as lab name in reports)
LAB_NAME = None
for i, arg in enumerate(sys.argv):
    if arg == '--lab-name' and i + 1 < len(sys.argv):
        LAB_NAME = sys.argv[i + 1]
        break

# Keywords that indicate a bug might need a health check
HEALTH_CHECK_KEYWORDS = {
    "crash": "Pod crash detection",
    "oom": "OOM event monitoring",
    "memory leak": "Memory usage check",
    "high latency": "Latency monitoring",
    "not ready": "Readiness check",
    "stuck": "Stuck resource detection",
    "timeout": "Timeout detection",
    "certificate": "Certificate expiry check",
    "expir": "Expiration monitoring",
    "failed": "Failure detection",
    "degraded": "Degraded state check",
    "unavailable": "Availability check",
    "pending": "Pending resource check",
    "node not": "Node health check",
    "kubelet": "Kubelet health check",
    "etcd": "etcd health check",
    "migration": "Migration status check",
    "storage": "Storage health check",
    "pvc": "PVC status check",
    "csi": "CSI driver check",
    "operator": "Operator health check",
    "catalog": "Catalog source check",
    "router": "Router health check",
    "network": "Network connectivity check",
    "dns": "DNS resolution check",
    "api": "API server check",
}

# Components that map to health check categories
COMPONENT_TO_CHECK = {
    "Etcd": "etcd",
    "Machine Config Operator": "mco",
    "Networking": "network",
    "Storage": "storage",
    "OLM": "olm",
    "CNV": "cnv",
    "Virtualization": "cnv",
    "kube-apiserver": "apiserver",
    "oauth": "oauth",
    "Installer": "installer",
}

# Global SSH client (thread-safe: Paramiko multiplexes channels over one transport)
import threading
_ssh_lock = threading.Lock()
ssh_client = None

def call_jira_mcp(tool_name, arguments):
    """Call Jira MCP tool via subprocess"""
    try:
        # Use cursor's mcp-proxy to call the tool
        import urllib.request
        import urllib.error
        
        # Try direct Jira API if MCP not available
        # For now, return mock data structure - will be replaced by actual MCP call
        return None
    except Exception as e:
        print(f"  ⚠️  Jira API error: {e}")
        return None

def search_jira_for_new_bugs(days=30, limit=50):
    """
    Search Jira for recent bugs in CNV, ODF, OCPBUGS projects.
    Returns list of bugs that might suggest new health checks.
    """
    # JQL to find recent bugs
    jql_queries = [
        f'project = CNV AND issuetype = Bug AND status in (Open, "In Progress", New) AND created >= -{days}d ORDER BY priority DESC, created DESC',
        f'project = OCPBUGS AND issuetype = Bug AND status in (Open, "In Progress", New) AND created >= -{days}d ORDER BY priority DESC, created DESC',
    ]
    
    all_bugs = []
    
    # Try to use mcp-proxy for Jira access
    try:
        for jql in jql_queries:
            result = subprocess.run(
                ['mcp-proxy', 'call', 'user-jira', 'jira_search', 
                 '--jql', jql, '--limit', str(limit // 2),
                 '--fields', 'summary,status,priority,components,labels,created'],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if 'issues' in data:
                    all_bugs.extend(data['issues'])
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
        # MCP proxy not available, use fallback recent bugs list
        pass
    
    # If no bugs from Jira, use known recent bugs from our database
    if not all_bugs:
        all_bugs = get_known_recent_bugs()
    
    return all_bugs

def get_known_recent_bugs():
    """Return list of known recent bugs that might need health checks"""
    return [
        {
            "key": "OCPBUGS-74962",
            "summary": "[4.19] Very High etcd Latency",
            "priority": {"name": "Critical"},
            "components": [{"name": "Etcd"}],
            "suggested_check": "etcd_latency",
            "check_description": "Monitor etcd latency and alert on high values"
        },
        {
            "key": "OCPBUGS-74938",
            "summary": "Kubelet and NetworkManager do not start automatically on any node after reboot",
            "priority": {"name": "Critical"},
            "components": [{"name": "Machine Config Operator"}],
            "suggested_check": "kubelet_health",
            "check_description": "Check if kubelet is running on all nodes"
        },
        {
            "key": "OCPBUGS-74926",
            "summary": "In-memory certificate expiration date is too short",
            "priority": {"name": "Major"},
            "components": [{"name": "oauth-apiserver"}],
            "suggested_check": "cert_expiry",
            "check_description": "Check certificate expiration dates"
        },
        {
            "key": "OCPBUGS-74907",
            "summary": "SDN to OVN-Kubernetes migration stuck",
            "priority": {"name": "Critical"},
            "components": [{"name": "Networking / ovn-kubernetes"}],
            "suggested_check": "network_migration",
            "check_description": "Check network migration status"
        },
        {
            "key": "CNV-78575",
            "summary": "kubevirt-hyperconverged operator version disappeared from OLM catalog",
            "priority": {"name": "Major"},
            "components": [{"name": "CNV Install, Upgrade and Operators"}],
            "suggested_check": "catalog_source",
            "check_description": "Verify OLM catalog sources are healthy"
        },
        {
            "key": "OCPBUGS-74894",
            "summary": "Router got flooding connection",
            "priority": {"name": "Major"},
            "components": [{"name": "Networking / router"}],
            "suggested_check": "router_health",
            "check_description": "Monitor router pod health and connection count"
        },
        {
            "key": "CNV-78518",
            "summary": "virt-exportserver image pull issues",
            "priority": {"name": "Major"},
            "components": [{"name": "CNV Install, Upgrade and Operators"}],
            "suggested_check": "image_pull",
            "check_description": "Check for ImagePullBackOff errors"
        },
    ]

def analyze_bugs_for_new_checks(bugs, existing_checks):
    """
    Analyze bugs to determine if new health checks should be added.
    Returns list of suggested new checks.
    """
    suggestions = []
    
    for bug in bugs:
        summary = bug.get("summary", "").lower()
        key = bug.get("key", "")
        priority = bug.get("priority", {}).get("name", "Normal")
        components = [c.get("name", "") if isinstance(c, dict) else c for c in bug.get("components", [])]
        
        # Check if bug already has a suggested check
        if bug.get("suggested_check"):
            check_name = bug["suggested_check"]
            if check_name not in existing_checks:
                suggestions.append({
                    "jira_key": key,
                    "summary": bug.get("summary", ""),
                    "priority": priority,
                    "components": components,
                    "suggested_check": check_name,
                    "check_description": bug.get("check_description", ""),
                    "reason": f"Based on bug {key}"
                })
            continue
        
        # Analyze summary for health check keywords
        matched_keywords = []
        for keyword, check_type in HEALTH_CHECK_KEYWORDS.items():
            if keyword in summary:
                matched_keywords.append((keyword, check_type))
        
        # Analyze components
        matched_components = []
        for comp in components:
            for comp_key, check_cat in COMPONENT_TO_CHECK.items():
                if comp_key.lower() in comp.lower():
                    matched_components.append((comp, check_cat))
        
        # Only suggest if priority is Critical/Major or multiple keywords match
        if (priority in ["Critical", "Blocker", "Major"] or len(matched_keywords) >= 2) and matched_keywords:
            # Generate suggested check name
            check_name = matched_keywords[0][1].lower().replace(" ", "_")
            if matched_components:
                check_name = f"{matched_components[0][1]}_{check_name}"
            
            if check_name not in existing_checks:
                suggestions.append({
                    "jira_key": key,
                    "summary": bug.get("summary", ""),
                    "priority": priority,
                    "components": components,
                    "suggested_check": check_name,
                    "check_description": f"New check based on: {matched_keywords[0][1]}",
                    "matched_keywords": [k[0] for k in matched_keywords],
                    "reason": f"Keywords: {', '.join([k[0] for k in matched_keywords[:3]])}"
                })
    
    # Deduplicate by check name
    seen = set()
    unique_suggestions = []
    for s in suggestions:
        if s["suggested_check"] not in seen:
            seen.add(s["suggested_check"])
            unique_suggestions.append(s)
    
    return unique_suggestions[:10]  # Limit to top 10 suggestions

def get_existing_check_names():
    """Return list of existing health check names"""
    return [
        "nodes", "operators", "pods", "kubevirt", "resources", "etcd",
        "pvcs", "migrations", "oom_events", "csi", "virt_handler",
        "virt_ctrl", "virt_launcher", "datavolumes", "volumesnapshots",
        "cordoned_vms", "stuck_migrations"
    ]

def display_jira_suggestions(suggestions):
    """Display Jira-based health check suggestions to user"""
    if not suggestions:
        print("\n  ✅ No new health checks suggested from recent Jira bugs.\n")
        return []
    
    # ANSI colors
    Y = '\033[93m'
    G = '\033[92m'
    B = '\033[94m'
    C = '\033[96m'
    R = '\033[91m'
    X = '\033[0m'
    BD = '\033[1m'
    
    print(f"\n{B}╔{'═'*72}╗{X}")
    print(f"{B}║{X}  {BD}🔍 NEW HEALTH CHECK SUGGESTIONS FROM JIRA{X}".ljust(83) + f"{B}║{X}")
    print(f"{B}╠{'═'*72}╣{X}")
    print(f"{B}║{X}  Found {Y}{len(suggestions)}{X} potential new checks based on recent Jira bugs:".ljust(88) + f"{B}║{X}")
    print(f"{B}╠{'─'*72}╣{X}")
    
    for i, s in enumerate(suggestions, 1):
        priority_color = R if s['priority'] in ['Critical', 'Blocker'] else Y if s['priority'] == 'Major' else X
        print(f"{B}║{X}  {BD}{i}.{X} {C}{s['suggested_check']}{X}".ljust(85) + f"{B}║{X}")
        print(f"{B}║{X}     {priority_color}[{s['priority']}]{X} {s['jira_key']}: {s['summary'][:45]}...".ljust(85) + f"{B}║{X}")
        print(f"{B}║{X}     {G}→ {s['check_description'][:55]}{X}".ljust(88) + f"{B}║{X}")
        if i < len(suggestions):
            print(f"{B}║{X}" + " "*72 + f"{B}║{X}")
    
    print(f"{B}╠{'═'*72}╣{X}")
    print(f"{B}║{X}  {Y}Enter check numbers to add (comma-separated), 'all', or 'skip':{X}".ljust(88) + f"{B}║{X}")
    print(f"{B}╚{'═'*72}╝{X}")
    
    return suggestions

def prompt_for_new_checks(suggestions):
    """Prompt user to select which checks to add"""
    if not suggestions:
        return []
    
    # Check if running non-interactively (from web UI)
    import sys
    import os
    import json
    
    if not sys.stdin.isatty() or os.environ.get('NON_INTERACTIVE'):
        # Save suggestions to file for web UI review
        suggestions_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.suggested_checks.json')
        try:
            # Load existing suggestions
            existing = []
            if os.path.exists(suggestions_file):
                with open(suggestions_file, 'r') as f:
                    existing = json.load(f)
            
            # Add new suggestions with timestamp
            from datetime import datetime
            for s in suggestions:
                s['timestamp'] = datetime.now().isoformat()
                s['status'] = 'pending'
            
            # Merge (avoid duplicates by jira_key)
            existing_keys = {s.get('jira_key') for s in existing}
            for s in suggestions:
                if s.get('jira_key') not in existing_keys:
                    existing.append(s)
            
            with open(suggestions_file, 'w') as f:
                json.dump(existing, f, indent=2)
            
            print(f"  💾 Saved {len(suggestions)} suggestions for web UI review")
            print(f"     Review at: Dashboard > Jira Suggestions\n")
        except Exception as e:
            print(f"  ⚠️  Could not save suggestions: {e}\n")
        
        return []  # Don't add checks automatically, let user review in web UI
    
    # Interactive mode - prompt user
    try:
        response = input("\n  Your choice: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return []
    
    if response == 'skip' or response == 's' or response == '':
        print("  ⏭️  Skipping new check additions.\n")
        return []
    
    if response == 'all' or response == 'a':
        print(f"  ✅ Adding all {len(suggestions)} suggested checks.\n")
        return suggestions
    
    # Parse comma-separated numbers
    selected = []
    try:
        indices = [int(x.strip()) - 1 for x in response.split(',')]
        for idx in indices:
            if 0 <= idx < len(suggestions):
                selected.append(suggestions[idx])
    except ValueError:
        print("  ⚠️  Invalid input. Skipping.\n")
        return []
    
    if selected:
        print(f"  ✅ Adding {len(selected)} selected checks.\n")
    
    return selected

def generate_check_code(check_info):
    """Generate the code for a new health check"""
    check_name = check_info['suggested_check']
    jira_key = check_info['jira_key']
    description = check_info['check_description']
    
    # Map check types to actual commands (stored as description, actual execution happens in collect_data)
    check_commands = {
        "etcd_latency": "oc exec etcd pod -- etcdctl endpoint health",
        "kubelet_health": "oc get nodes with Ready status",
        "cert_expiry": "oc get secrets with TLS type",
        "network_migration": "oc get network.operator migration status",
        "catalog_source": "oc get catalogsource status",
        "router_health": "oc get router pods",
        "image_pull": "oc get pods with ImagePullBackOff",
    }
    
    cmd = check_commands.get(check_name, "oc get pods")
    
    return {
        "name": check_name,
        "command": cmd,
        "jira": jira_key,
        "description": description
    }

def add_checks_to_script(selected_checks):
    """
    Add new checks to the SUGGESTED_NEW_CHECKS list (runtime only).
    In a real implementation, this could modify the script file.
    """
    global SUGGESTED_NEW_CHECKS
    SUGGESTED_NEW_CHECKS = []
    
    for check in selected_checks:
        check_code = generate_check_code(check)
        SUGGESTED_NEW_CHECKS.append(check_code)
        print(f"  📝 Added check: {check_code['name']} (from {check_code['jira']})")
    
    return SUGGESTED_NEW_CHECKS

def check_jira_for_new_tests():
    """
    Main function to check Jira for new bugs and suggest health checks.
    Called before running the health check if --check-jira flag is set.
    """
    print(f"\n  🔍 Checking Jira for recent bugs that might need new health checks...")
    
    # Get existing check names
    existing_checks = get_existing_check_names()
    
    # Search Jira for recent bugs
    bugs = search_jira_for_new_bugs(days=30, limit=50)
    
    if not bugs:
        print("  ⚠️  Could not fetch bugs from Jira. Using known recent bugs.\n")
        bugs = get_known_recent_bugs()
    
    print(f"  📊 Analyzed {len(bugs)} recent bugs from CNV/OCP/ODF projects")
    
    # Analyze bugs for potential new checks
    suggestions = analyze_bugs_for_new_checks(bugs, existing_checks)
    
    # Display suggestions and prompt user
    display_jira_suggestions(suggestions)
    
    # Get user selection
    selected = prompt_for_new_checks(suggestions)
    
    # Add selected checks
    if selected:
        add_checks_to_script(selected)
        return selected
    
    return []

def search_emails_for_issues(issues, gmail_account="guchen@redhat.com"):
    """
    Search Gmail for emails related to the detected issues.
    Uses the MCP Gmail tool to search for relevant emails.
    Returns dict mapping issue types to related emails.
    """
    import subprocess
    import json
    
    email_results = {}
    
    if not issues:
        return email_results
    
    print(f"  📧 Searching emails for related discussions...")
    
    # Build search queries based on issue types
    search_keywords = []
    for issue in issues:
        if isinstance(issue, dict):
            issue_type = issue.get('type', '')
            resource = issue.get('resource', issue.get('name', ''))
        else:
            issue_type = str(issue)
            resource = ''
        
        # Add keywords based on issue type
        if 'virt-handler' in str(issue_type).lower() or 'virt-handler' in str(resource).lower():
            search_keywords.extend(['virt-handler memory', 'virt-handler high memory'])
        elif 'migration' in str(issue_type).lower():
            search_keywords.extend(['vm migration stuck', 'migration failed'])
        elif 'operator' in str(issue_type).lower():
            search_keywords.extend(['operator degraded', 'cluster operator'])
        elif 'pod' in str(issue_type).lower():
            search_keywords.extend(['pod crashloop', 'pod not ready'])
        elif 'storage' in str(issue_type).lower() or 'odf' in str(issue_type).lower():
            search_keywords.extend(['storage issue', 'ODF degraded', 'ceph'])
        elif 'snapshot' in str(issue_type).lower():
            search_keywords.extend(['snapshot failed', 'volumesnapshot'])
    
    # Also search for general CNV/OCP issues
    search_keywords.extend(['CNV issue', 'OpenShift problem', 'cluster alert'])
    
    # Deduplicate
    search_keywords = list(set(search_keywords))[:5]  # Limit to 5 searches
    
    found_emails = []
    for keyword in search_keywords:
        try:
            # For now, we'll store the search terms - actual email search would be done via MCP
            # This is a placeholder that the web dashboard can use with MCP tools
            found_emails.append({
                'search_term': keyword,
                'status': 'pending',
                'results': []
            })
        except Exception as e:
            pass
    
    email_results['searches'] = found_emails
    email_results['keywords'] = search_keywords
    
    print(f"  📧 Prepared {len(search_keywords)} email search queries")
    
    return email_results

# Storage for dynamically added checks
SUGGESTED_NEW_CHECKS = []

DRILLDOWN_COMMANDS = {
    "node-not-ready": [
        {"cmd": "oc describe node {name} 2>&1 | grep -A30 'Conditions:'", "desc": "Full node conditions (DiskPressure, MemoryPressure, PIDPressure, Ready)"},
        {"cmd": "oc get events --field-selector involvedObject.name={name},reason!=NodeHasSufficientMemory,reason!=NodeHasNoDiskPressure --sort-by='.lastTimestamp' 2>&1 | tail -20", "desc": "Significant node events (excluding routine)"},
        {"cmd": "oc describe node {name} 2>&1 | grep -A5 'Ready\\|MemoryPressure\\|DiskPressure\\|PIDPressure'", "desc": "Node condition reasons and messages"},
        {"cmd": "oc get pods --field-selector spec.nodeName={name} -A --no-headers 2>&1 | grep -ciE 'evict|unknown|pending'", "desc": "Count of Evicted/Unknown/Pending pods on this node"},
        {"cmd": "oc get node {name} -o wide 2>&1 | tail -1 | awk '{print $6}'", "desc": "Node internal IP (for SSH check)"},
        {"cmd": "oc adm top node {name} 2>&1", "desc": "Node resource usage (fails if node is dead)"},
    ],
    "node-disk-pressure": [
        {"cmd": "ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=no core@{node_ip} 'df -h /var /var/lib/kubelet /sysroot 2>/dev/null; echo --- ; df -ih /var 2>/dev/null'", "desc": "Filesystem usage on the node (blocks + inodes)"},
        {"cmd": "ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=no core@{node_ip} 'sudo du -sh /var/lib/containers /var/lib/kubelet /var/log /var/lib/etcd 2>/dev/null | sort -rh'", "desc": "Top /var consumers breakdown (containers, kubelet, logs, etcd)"},
        {"cmd": "ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=no core@{node_ip} 'sudo sh -c \"du -sh /var/lib/kubelet/pods/* 2>/dev/null | sort -rh | head -10\"'", "desc": "Top 10 pods by ephemeral disk usage under kubelet/pods"},
        {"cmd": "ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=no core@{node_ip} 'sudo sh -c \"du -sh /var/lib/containers/storage/overlay/* 2>/dev/null | sort -rh | head -10\"'", "desc": "Top 10 container image layers by disk usage"},
        {"cmd": "ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=no core@{node_ip} 'sudo sh -c \"du -sh /var/log/pods/* 2>/dev/null | sort -rh | head -10\"'", "desc": "Top 10 pods by log disk usage"},
        {"cmd": "ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=no core@{node_ip} 'sudo sh -c \"for d in $(ls /var/lib/kubelet/pods/ 2>/dev/null | head -5); do sz=$(du -sh /var/lib/kubelet/pods/$d 2>/dev/null | cut -f1); vols=$(ls /var/lib/kubelet/pods/$d/volumes/kubernetes.io~empty-dir/ 2>/dev/null | tr \\\\n ,); echo $sz $d emptyDirs=$vols; done\" | sort -rh'", "desc": "Top 5 pod UUIDs with size and emptyDir volume names (identifies workload)"},
        {"cmd": "oc describe node {name} 2>&1 | grep -B2 -A5 'DiskPressure'", "desc": "DiskPressure condition details"},
        {"cmd": "oc get pods --field-selector spec.nodeName={name} -A --no-headers 2>&1 | grep -i evict | head -10", "desc": "Evicted pods on this node"},
        {"cmd": "oc get events -A --sort-by='.lastTimestamp' 2>&1 | grep -i 'evict\\|disk\\|pressure\\|space' | tail -15", "desc": "Cluster events related to disk/eviction"},
    ],
    "node-memory-pressure": [
        {"cmd": "oc describe node {name} 2>&1 | grep -B2 -A5 'MemoryPressure'", "desc": "MemoryPressure condition details"},
        {"cmd": "oc describe node {name} 2>&1 | grep -A5 'Ready\\|MemoryPressure\\|DiskPressure\\|PIDPressure'", "desc": "All node conditions with reasons"},
        {"cmd": "oc adm top pods --field-selector spec.nodeName={name} -A --no-headers 2>&1 | sort -k3 -rh | head -15", "desc": "Top memory consuming pods on this node"},
        {"cmd": "oc adm top node {name} 2>&1", "desc": "Node overall resource usage"},
        {"cmd": "oc get pods --field-selector spec.nodeName={name} -A --no-headers 2>&1 | wc -l", "desc": "Total pod count on this node"},
        {"cmd": "oc get events -A --field-selector reason=OOMKilled --sort-by='.lastTimestamp' 2>&1 | tail -10", "desc": "Recent OOM kill events cluster-wide"},
        {"cmd": "oc get pods --field-selector spec.nodeName={name} -A --no-headers 2>&1 | grep -ciE 'evict|oom'", "desc": "Count of evicted/OOM pods on this node"},
    ],
    "node-cordoned": [
        {"cmd": "oc describe node {name} 2>&1 | grep -A10 'Taints:'", "desc": "Node taints (shows cordon reason)"},
        {"cmd": "oc get mcp 2>&1", "desc": "MachineConfigPool status (node may be updating)"},
        {"cmd": "oc get events --field-selector involvedObject.name={name} --sort-by='.lastTimestamp' 2>&1 | grep -iE 'drain|cordon|taint|schedule' | tail -10", "desc": "Drain/cordon events"},
        {"cmd": "oc get nodes {name} -o yaml 2>&1 | grep -A5 'unschedulable'", "desc": "Unschedulable flag details"},
    ],
    "operator-crashloop": [
        {"cmd": "oc get pods -n openshift-{name} --no-headers 2>&1 | grep -v Running | head -10", "desc": "Non-running pods in operator namespace"},
        {"cmd": "oc logs -n openshift-{name} $(oc get pods -n openshift-{name} --no-headers 2>/dev/null | grep -v Running | head -1 | awk '{print $1}') --previous --tail=50 2>&1 | tail -30", "desc": "Previous container logs (crash reason)"},
        {"cmd": "oc describe pod -n openshift-{name} $(oc get pods -n openshift-{name} --no-headers 2>/dev/null | grep -v Running | head -1 | awk '{print $1}') 2>&1 | grep -A10 'Last State:'", "desc": "Last container state (exit code, reason)"},
        {"cmd": "oc describe pod -n openshift-{name} $(oc get pods -n openshift-{name} --no-headers 2>/dev/null | grep -v Running | head -1 | awk '{print $1}') 2>&1 | grep -A5 'Resources:'", "desc": "Pod resource limits"},
        {"cmd": "oc get events -n openshift-{name} --sort-by='.lastTimestamp' 2>&1 | tail -15", "desc": "Recent events in operator namespace"},
    ],
    "pod-app-crash": [
        {"cmd": "oc logs {pod} -n {ns} --previous --tail=80 2>&1 | tail -40", "desc": "Previous container logs (crash output)"},
        {"cmd": "oc describe pod {pod} -n {ns} 2>&1 | grep -A10 'Last State:'", "desc": "Last container state (exit code, OOM, error)"},
        {"cmd": "oc describe pod {pod} -n {ns} 2>&1 | grep -A5 'Resources:'", "desc": "Resource requests and limits"},
        {"cmd": "oc get events -n {ns} --field-selector involvedObject.name={pod} --sort-by='.lastTimestamp' 2>&1 | tail -10", "desc": "Pod events timeline"},
        {"cmd": "oc describe pod {pod} -n {ns} 2>&1 | grep -A3 'Restart Count:'", "desc": "Restart count and pattern"},
    ],
    "pod-evicted-disk": [
        {"cmd": "oc get pod {pod} -n {ns} -o wide 2>&1 | tail -1", "desc": "Pod details (shows which node)"},
        {"cmd": "oc describe pod {pod} -n {ns} 2>&1 | grep -A5 'Status:\\|Message:\\|Reason:'", "desc": "Pod eviction reason details"},
        {"cmd": "oc get events -n {ns} --field-selector involvedObject.name={pod} --sort-by='.lastTimestamp' 2>&1 | tail -10", "desc": "Pod events"},
        {"cmd": "oc get nodes --no-headers 2>&1 | grep -E 'NotReady|SchedulingDisabled'", "desc": "Nodes with issues"},
        {"cmd": "oc describe node $(oc get pod {pod} -n {ns} -o jsonpath='{{.spec.nodeName}}' 2>/dev/null) 2>&1 | grep -A5 'DiskPressure' | head -8", "desc": "Node DiskPressure condition"},
        {"cmd": "oc get pods --field-selector spec.nodeName=$(oc get pod {pod} -n {ns} -o jsonpath='{{.spec.nodeName}}' 2>/dev/null) -A --no-headers 2>&1 | grep -ci evict", "desc": "Count of evicted pods on same node"},
    ],
    "pod-evicted-generic": [
        {"cmd": "oc describe pod {pod} -n {ns} 2>&1 | grep -A5 'Status:\\|Message:\\|Reason:'", "desc": "Pod eviction details"},
        {"cmd": "oc get events -n {ns} --field-selector involvedObject.name={pod} --sort-by='.lastTimestamp' 2>&1 | tail -10", "desc": "Pod events"},
        {"cmd": "oc describe node $(oc get pod {pod} -n {ns} -o jsonpath='{{.spec.nodeName}}' 2>/dev/null) 2>&1 | grep -A20 'Conditions:'", "desc": "Node conditions of hosting node"},
    ],
    "mco-degraded": [
        {"cmd": "oc get mcp 2>&1", "desc": "MachineConfigPool status"},
        {"cmd": "oc get co machine-config -o yaml 2>&1 | grep -A10 'message:' | head -20", "desc": "MCO error message details"},
        {"cmd": "oc get nodes --no-headers 2>&1 | grep -E 'NotReady|SchedulingDisabled' | head -10", "desc": "Nodes that are NotReady or cordoned"},
        {"cmd": "oc get pods -n openshift-machine-config-operator --no-headers 2>&1 | grep -v Running | head -10", "desc": "Non-running MCD pods"},
        {"cmd": "oc get mcp worker -o yaml 2>&1 | grep -A10 'degradedMachineCount\\|message' | head -20", "desc": "Worker MCP degraded details"},
        {"cmd": "oc get events -n openshift-machine-config-operator --sort-by='.lastTimestamp' 2>&1 | tail -15", "desc": "Recent MCO events"},
        {"cmd": "oc get nodes --no-headers 2>&1 | grep SchedulingDisabled | head -5", "desc": "Nodes stuck in drain (SchedulingDisabled)"},
    ],
    "csi-crashloop": [
        {"cmd": "oc get pods -A --no-headers 2>&1 | grep -i csi | grep -v Running | head -10", "desc": "Non-running CSI pods"},
        {"cmd": "oc logs $(oc get pods -A --no-headers 2>&1 | grep -i csi | grep -i crash | head -1 | awk '{print \"-n \" $1 \" \" $2}') --previous --tail=50 2>&1 | tail -30", "desc": "Previous logs of crashing CSI pod"},
        {"cmd": "oc describe pod $(oc get pods -A --no-headers 2>&1 | grep -i csi | grep -v Running | head -1 | awk '{print $2 \" -n \" $1}') 2>&1 | grep -A10 'Last State:\\|Events:' | head -25", "desc": "Pod last state and events"},
        {"cmd": "oc get events -A --sort-by='.lastTimestamp' 2>&1 | grep -i csi | tail -10", "desc": "CSI-related events"},
        {"cmd": "oc get storagecluster -n openshift-storage 2>&1", "desc": "Storage cluster status"},
    ],
    "csi-pod-issue": [
        {"cmd": "oc get pods -A --no-headers 2>&1 | grep -i csi | grep -v Running | head -10", "desc": "Non-running CSI pods"},
        {"cmd": "oc get events -A --sort-by='.lastTimestamp' 2>&1 | grep -i csi | tail -10", "desc": "CSI-related events"},
        {"cmd": "oc get csidrivers 2>&1", "desc": "Registered CSI drivers"},
    ],
    "virt-handler-memory-deep": [
        {"cmd": "oc adm top pods -n openshift-cnv -l kubevirt.io=virt-handler --no-headers 2>&1 | sort -k3 -rh | head -10", "desc": "Top memory virt-handler pods"},
        {"cmd": "oc get vmi -A --no-headers 2>&1 | wc -l", "desc": "Total VMI count"},
        {"cmd": "oc get vmi -A -o wide --no-headers 2>&1 | awk '{print $4}' | sort | uniq -c | sort -rn | head -10", "desc": "VM count per node (top 10)"},
        {"cmd": "oc get ds virt-handler -n openshift-cnv -o yaml 2>&1 | grep -A10 'resources:' | head -12", "desc": "virt-handler resource requests/limits"},
        {"cmd": "oc get events -n openshift-cnv --field-selector reason=OOMKilled --sort-by='.lastTimestamp' 2>&1 | tail -5", "desc": "Recent OOM kills in CNV namespace"},
        {"cmd": "oc logs -n openshift-cnv $(oc get pods -n openshift-cnv -l kubevirt.io=virt-handler --no-headers -o name | head -1) --tail=30 2>&1 | grep -iE 'memory|oom|error|warn' | head -15", "desc": "virt-handler warning/error logs"},
    ],
}

DRILLDOWN_ANALYSIS_RULES = {
    "node-disk-pressure": [
        {"keywords": ["kubelet stopped posting", "nodestatusunknown"], "conclusion": "Node is DEAD - kubelet stopped posting status; all conditions show Unknown. This is NOT a disk issue, the node itself is unreachable", "confidence": "high",
         "fix": "1) Check if node is powered on via BMC/IPMI. 2) Try SSH: ssh core@<node-ip>. 3) If reachable: systemctl status kubelet, journalctl -u kubelet. 4) If not reachable: check network/switch, reboot via BMC.",
         "doc": "https://access.redhat.com/solutions/5765631",
         "followup": "node-dead-verify"},
        {"keywords": ["prometheus", "/prometheus", "prometheus-k8s"], "conclusion": "Prometheus TSDB is consuming excessive disk space", "confidence": "high",
         "fix": "Configure retention: oc edit configmap cluster-monitoring-config -n openshift-monitoring. Add 'retentionSize: 40GB' under prometheusK8s. Also set 'volumeClaimTemplate' to use a PVC instead of emptyDir.",
         "doc": "https://access.redhat.com/solutions/4976801"},
        {"keywords": ["/var/lib/containers"], "conclusion": "Container images and layers consuming disk", "confidence": "high",
         "fix": "Run garbage collection: crictl rmi --prune, or increase imageGCHighThresholdPercent in kubelet config",
         "doc": "https://docs.openshift.com/container-platform/latest/nodes/nodes/nodes-nodes-garbage-collection.html"},
        {"keywords": ["/var/log", "journal"], "conclusion": "Log files consuming disk space", "confidence": "medium",
         "fix": "Configure journald MaxRetentionSec or SystemMaxUse, check for pods with excessive logging",
         "doc": "https://access.redhat.com/solutions/6963786"},
        {"keywords": ["kubelet/pods", "emptydir", "ephemeral"], "conclusion": "Pod ephemeral storage (emptyDir volumes) consuming disk", "confidence": "medium",
         "fix": "Identify pods with large emptyDir usage and add ephemeral-storage limits"},
        {"keywords": ["95%", "96%", "97%", "98%", "99%", "100%"], "conclusion": "Filesystem is critically full (95%+)", "confidence": "high",
         "fix": "Identify the full filesystem from df -h output, then check its largest consumers with du -sh"},
        {"keywords": ["kubelet/pods", "kubelet\\pods"], "conclusion": "Pod ephemeral storage is a top disk consumer under /var/lib/kubelet/pods", "confidence": "high",
         "fix": "Identify the largest pod directories and their workloads. Check volume names inside each UUID directory to identify the pod (e.g. 'prometheus-k8s-db' = Prometheus). Cross-reference with: oc get pods --field-selector spec.nodeName=<node> -A"},
    ],
    "node-not-ready": [
        {"keywords": ["kubelet stopped posting", "nodestatusunknown"], "conclusion": "Kubelet stopped posting node status - node is unreachable or kubelet crashed", "confidence": "high",
         "fix": "1) Check if node is powered on via BMC/IPMI. 2) Try SSH: ssh core@<node-ip>. 3) If reachable: systemctl status kubelet, journalctl -u kubelet. 4) If not reachable: check network/switch.",
         "doc": "https://access.redhat.com/solutions/5765631",
         "followup": "node-dead-verify"},
        {"keywords": ["diskpressure", "disk pressure"], "conclusion": "Node Not Ready due to DiskPressure condition", "confidence": "high",
         "fix": "See disk pressure drilldown - identify and clean the top disk consumer",
         "follow_drilldown": "node-disk-pressure"},
        {"keywords": ["memorypressure", "memory pressure"], "conclusion": "Node Not Ready due to MemoryPressure condition", "confidence": "high",
         "fix": "See memory pressure drilldown - identify and stop the top memory consumer",
         "follow_drilldown": "node-memory-pressure"},
        {"keywords": ["oom", "out of memory", "killed process"], "conclusion": "Node Not Ready due to kernel OOM killer", "confidence": "high",
         "fix": "Check dmesg for which process was OOM-killed, increase node memory or reduce workload",
         "followup": "node-dead-verify"},
        {"keywords": ["kubelet", "stopped", "not running", "failed"], "conclusion": "Kubelet process is not running on the node", "confidence": "high",
         "fix": "SSH to node and check kubelet: journalctl -u kubelet --no-pager | tail -50",
         "followup": "node-dead-verify"},
        {"keywords": ["connection refused", "unreachable", "timeout"], "conclusion": "Node is network-unreachable or powered off", "confidence": "high",
         "fix": "Check physical/BMC connectivity, ping the node, check switch ports",
         "followup": "node-dead-verify"},
        {"keywords": ["i/o error", "disk error", "hardware"], "conclusion": "Hardware failure (disk I/O or other)", "confidence": "high",
         "fix": "Check BMC/IPMI logs, replace faulty hardware, cordon and drain the node",
         "followup": "node-dead-verify"},
    ],
    "node-memory-pressure": [
        {"keywords": ["oom", "killed"], "conclusion": "Kernel OOM killer is actively killing processes", "confidence": "high",
         "fix": "Identify which process was OOM-killed from dmesg, increase memory limits or reduce pods"},
        {"keywords": ["virt-handler", "virt-launcher"], "conclusion": "KubeVirt components consuming excessive memory (known issue with VM density)", "confidence": "high",
         "fix": "Reduce VM count per node, or increase virt-handler memory limits (CNV-71448)"},
        {"keywords": ["prometheus", "monitoring"], "conclusion": "Monitoring stack consuming excessive memory", "confidence": "medium",
         "fix": "Tune Prometheus memory by reducing retention, scrape interval, or number of targets"},
    ],
    "operator-crashloop": [
        {"keywords": ["oomkilled", "out of memory", "oom"], "conclusion": "Operator pod OOM-killed - needs higher memory limit", "confidence": "high",
         "fix": "Increase memory limit in operator deployment or subscription"},
        {"keywords": ["permission denied", "forbidden", "rbac"], "conclusion": "RBAC/permission error preventing operator from functioning", "confidence": "high",
         "fix": "Check ClusterRoleBindings and ServiceAccount permissions"},
        {"keywords": ["connection refused", "dial tcp", "no such host"], "conclusion": "Operator cannot reach a required service endpoint", "confidence": "high",
         "fix": "Check network policies and service availability in the namespace"},
        {"keywords": ["exit code 1", "exit code 2", "panic", "fatal"], "conclusion": "Application error in operator code (bug or bad config)", "confidence": "medium",
         "fix": "Check operator version, search Jira for matching bugs, consider reinstalling"},
    ],
    "pod-app-crash": [
        {"keywords": ["oomkilled", "exit code 137"], "conclusion": "Container OOM-killed (exit code 137 = SIGKILL from OOM)", "confidence": "high",
         "fix": "Increase memory limit in pod spec"},
        {"keywords": ["exit code 1", "error", "exception"], "conclusion": "Application runtime error", "confidence": "medium",
         "fix": "Check application logs for the specific error"},
        {"keywords": ["exit code 2", "usage", "invalid"], "conclusion": "Invalid arguments or configuration", "confidence": "medium",
         "fix": "Check ConfigMaps and environment variables passed to the pod"},
    ],
    "pod-evicted-disk": [
        {"keywords": ["kubelet stopped posting", "nodestatusunknown"], "conclusion": "Node is dead/unreachable - kubelet stopped. Pods were evicted as a consequence", "confidence": "high",
         "fix": "Check node power/network. SSH to node and check kubelet: systemctl status kubelet",
         "doc": "https://access.redhat.com/solutions/5765631",
         "followup": "node-dead-verify"},
        {"keywords": ["diskpressure", "disk pressure"], "conclusion": "Node has DiskPressure - kubelet is evicting pods to free ephemeral storage", "confidence": "high",
         "fix": "Identify top disk consumers on the node. Common culprits: Prometheus TSDB, container images, pod logs. Run: du -sh /var/lib/containers /var/lib/kubelet /var/log on the node.",
         "doc": "https://access.redhat.com/solutions/4976801",
         "follow_drilldown": "node-disk-pressure"},
    ],
    "pod-evicted-generic": [
        {"keywords": ["diskpressure"], "conclusion": "Pod evicted due to DiskPressure on the node", "confidence": "high",
         "fix": "Free disk space on the node or increase storage capacity",
         "follow_drilldown": "node-disk-pressure"},
        {"keywords": ["memorypressure"], "conclusion": "Pod evicted due to MemoryPressure on the node", "confidence": "high",
         "fix": "Reduce memory usage or add more memory to the node",
         "follow_drilldown": "node-memory-pressure"},
        {"keywords": ["evict"], "conclusion": "Pod evicted by kubelet due to resource pressure", "confidence": "medium",
         "fix": "Check node conditions to identify which resource is under pressure"},
    ],
    "mco-degraded": [
        {"keywords": ["context deadline exceeded", "timeout"], "conclusion": "MachineConfig apply timed out - node drain or reboot took too long. Likely a pod with a PodDisruptionBudget blocking the drain", "confidence": "high",
         "fix": "1) Check stuck nodes: oc get nodes | grep SchedulingDisabled. 2) Check PDBs: oc get pdb -A. 3) Check what's blocking drain: oc get pods --field-selector spec.nodeName=<node> -A | grep -v Completed",
         "doc": "https://access.redhat.com/solutions/5414371",
         "followup": "mco-drain-blocked"},
        {"keywords": ["notready", "unavailable"], "conclusion": "MCP degraded because a node went NotReady during update", "confidence": "high",
         "fix": "Check the NotReady node, fix it first, then MCO will recover automatically",
         "followup": "mco-drain-blocked"},
        {"keywords": ["schedulingdisabled"], "conclusion": "A node is stuck in SchedulingDisabled (drain did not complete)", "confidence": "high",
         "fix": "Check what's blocking drain on the stuck node, then uncordon: oc adm uncordon <node>",
         "followup": "mco-drain-blocked"},
        {"keywords": ["failed to resync", "resync"], "conclusion": "MCO failed to resync to target version - possible incompatible custom MachineConfig", "confidence": "high",
         "fix": "Check for custom MachineConfigs: oc get mc | grep -v rendered | grep -v 00-. Remove or fix incompatible custom configs."},
        {"keywords": ["degraded", "machine-config-daemon"], "conclusion": "MachineConfigDaemon on one or more nodes is reporting degraded", "confidence": "medium",
         "fix": "Check MCD logs on degraded nodes: oc logs -n openshift-machine-config-operator <mcd-pod> --tail=50",
         "followup": "mco-drain-blocked"},
    ],
    "csi-crashloop": [
        {"keywords": ["oomkilled", "oom", "exit code 137"], "conclusion": "CSI controller pod OOM-killed - needs higher memory limit", "confidence": "high",
         "fix": "Increase memory limit in the CSI controller deployment"},
        {"keywords": ["permission denied", "forbidden", "rbac"], "conclusion": "CSI controller lacks required RBAC permissions", "confidence": "high",
         "fix": "Check ClusterRoleBindings for the CSI service account"},
        {"keywords": ["connection refused", "dial tcp", "no such host"], "conclusion": "CSI controller cannot connect to the storage backend", "confidence": "high",
         "fix": "Check storage backend connectivity and network policies"},
        {"keywords": ["needsreinstall", "installcheckfailed", "installwaiting"], "conclusion": "OLM CSV reports CSI operator needs reinstall - deployment not available. The operator installation is broken.", "confidence": "high",
         "fix": "1) Check CSV status: oc get csv -n openshift-storage | grep csi. 2) Try deleting the CSV to trigger reinstall: oc delete csv <name> -n openshift-storage. 3) Check if the subscription is healthy: oc get sub -n openshift-storage.",
         "doc": "https://access.redhat.com/solutions/6005941"},
        {"keywords": ["leader election", "failed to acquire"], "conclusion": "CSI controller leader election failure - possible stale lock", "confidence": "medium",
         "fix": "Delete the leader election ConfigMap/Lease and restart the CSI pods"},
        {"keywords": ["crashloopbackoff", "error", "fail"], "conclusion": "CSI controller is crash-looping - check logs for the specific error", "confidence": "medium",
         "fix": "Check previous container logs: oc logs <pod> -n <ns> --previous"},
    ],
    "csi-pod-issue": [
        {"keywords": ["evicted"], "conclusion": "CSI pod was evicted due to resource pressure on the node", "confidence": "medium",
         "fix": "Check node conditions and consider dedicated nodes for storage pods"},
        {"keywords": ["pending"], "conclusion": "CSI pod is stuck in Pending state", "confidence": "medium",
         "fix": "Check pod events for scheduling issues: oc describe pod <pod> -n <ns>"},
    ],
    "virt-handler-memory-deep": [
        {"keywords": ["oomkilled", "oom"], "conclusion": "virt-handler is being OOM-killed. Memory limit too low for current VM density", "confidence": "high",
         "fix": "Increase virt-handler memory limit via HCO: spec.infra.nodePlacement.tolerations or patch the DaemonSet directly. CNV-71448",
         "doc": "https://issues.redhat.com/browse/CNV-71448"},
        {"keywords": ["error", "fail", "timeout"], "conclusion": "virt-handler experiencing errors in addition to high memory usage", "confidence": "medium",
         "fix": "Check virt-handler logs for root cause errors. High memory may be a side effect.",
         "followup": "virt-handler-errors-deep"},
    ],
}

FOLLOWUP_COMMANDS = {
    "node-dead-verify": [
        {"cmd": "ping -c 3 -W 2 {node_ip} 2>&1 || echo PING_FAILED", "desc": "Network reachability (ICMP ping)"},
        {"cmd": "ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes core@{node_ip} 'uptime' 2>&1 || echo SSH_UNREACHABLE", "desc": "SSH reachability check"},
        {"cmd": "ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes core@{node_ip} 'systemctl is-active kubelet; systemctl status kubelet --no-pager -l 2>&1 | head -15' 2>&1 || echo SSH_CMD_FAILED", "desc": "Kubelet service status on the node"},
        {"cmd": "ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes core@{node_ip} 'journalctl -u kubelet --since \"30 min ago\" --no-pager 2>&1 | tail -25' 2>&1 || echo SSH_CMD_FAILED", "desc": "Recent kubelet journal logs"},
        {"cmd": "ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes core@{node_ip} 'df -h / /var /var/lib/containers 2>&1' 2>&1 || echo SSH_CMD_FAILED", "desc": "Disk usage on the node"},
        {"cmd": "ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes core@{node_ip} 'dmesg -T 2>/dev/null | tail -25 || dmesg | tail -25' 2>&1 || echo SSH_CMD_FAILED", "desc": "Recent kernel messages (hardware errors, OOM, panic)"},
    ],
    "mco-drain-blocked": [
        {"cmd": "oc get pdb -A --no-headers 2>&1", "desc": "PodDisruptionBudgets blocking drain"},
        {"cmd": "oc get nodes --no-headers 2>&1 | grep SchedulingDisabled | awk '{print $1}'", "desc": "Nodes stuck in SchedulingDisabled"},
        {"cmd": "stuck=$(oc get nodes --no-headers 2>&1 | grep SchedulingDisabled | head -1 | awk '{print $1}'); [ -n \"$stuck\" ] && oc get pods --field-selector spec.nodeName=$stuck -A --no-headers 2>&1 | grep -vE 'Completed|Running' | head -15 || echo '(no stuck nodes found)'", "desc": "Non-running pods on stuck node (blocking drain)"},
        {"cmd": "oc get pods -A --no-headers 2>&1 | grep Terminating | head -10", "desc": "Pods stuck in Terminating state"},
        {"cmd": "stuck=$(oc get nodes --no-headers 2>&1 | grep SchedulingDisabled | head -1 | awk '{print $1}'); [ -n \"$stuck\" ] && oc get pods --field-selector spec.nodeName=$stuck -A --no-headers 2>&1 | wc -l || echo '0'", "desc": "Total pods remaining on stuck node"},
        {"cmd": "oc logs -n openshift-machine-config-operator $(oc get pods -n openshift-machine-config-operator --no-headers 2>/dev/null | grep machine-config-daemon | head -1 | awk '{print $1}') --tail=30 2>&1 | grep -iE 'error|fail|drain|timeout|evict' | head -10", "desc": "MCD error logs related to drain"},
    ],
    "virt-handler-errors-deep": [
        {"cmd": "oc logs -n openshift-cnv $(oc get pods -n openshift-cnv -l kubevirt.io=virt-handler --no-headers -o name 2>/dev/null | head -1) --tail=100 2>&1 | grep -iE 'error|warn|fail|timeout|refused|tls|cert|oom' | head -25", "desc": "virt-handler error/warning log lines"},
        {"cmd": "oc get pods -n openshift-cnv 2>&1 | grep -E 'virt-api|virt-controller' | head -5", "desc": "virt-api and virt-controller status"},
        {"cmd": "oc get vmi -A --no-headers 2>&1 | awk '{print $4}' | sort | uniq -c | sort -rn | head -5", "desc": "VM count per node (top 5)"},
        {"cmd": "oc get events -n openshift-cnv --sort-by='.lastTimestamp' 2>&1 | grep -iE 'error|fail|oom|kill' | tail -10", "desc": "Recent error events in CNV namespace"},
        {"cmd": "oc get ds virt-handler -n openshift-cnv -o yaml 2>&1 | grep -A8 'resources:' | head -10", "desc": "virt-handler resource limits"},
    ],
}

FOLLOWUP_ANALYSIS_RULES = {
    "node-dead-verify": [
        {"keywords": ["100% packet loss", "ping_failed", "ssh_unreachable"],
         "conclusion": "Node is completely unreachable - ping and SSH both fail. Machine is powered off, has a network failure, or experienced a kernel panic.",
         "confidence": "high",
         "fix": "Access via BMC/IPMI: 1) ipmitool -I lanplus -H <bmc-ip> power status, 2) If powered on: check console for kernel panic (ipmitool sol activate), 3) If powered off: power on via BMC, 4) Check switch port for the node NIC"},
        {"keywords": ["0 received", "ssh_unreachable"],
         "conclusion": "Node does not respond to ping or SSH. Machine may be powered off or has a complete network failure.",
         "confidence": "high",
         "fix": "Access via BMC/IPMI console. Check if machine is powered on and if NIC link is up."},
        {"keywords": ["ssh_unreachable", "bytes from"],
         "conclusion": "Node responds to ping but SSH is down. OS kernel is running but sshd or userspace may have crashed (possible kernel panic with network stack still alive).",
         "confidence": "high",
         "fix": "Access via BMC/IPMI console (ipmitool sol activate). Check: systemctl status sshd, systemctl status kubelet, free -h, df -h"},
        {"keywords": ["inactive", "dead"],
         "conclusion": "Node is reachable but kubelet service is stopped/dead.",
         "confidence": "high",
         "fix": "Root cause is in kubelet journal logs above. Common causes: certificate expiry, disk full (/var at 100%), OOM-killed. Do NOT blindly restart kubelet until cause is identified."},
        {"keywords": ["100%", "/var"],
         "conclusion": "Node is reachable but /var filesystem is full. This caused kubelet to stop functioning.",
         "confidence": "high",
         "fix": "Free disk on /var: du -sh /var/lib/containers /var/lib/kubelet /var/log on the node. Likely culprits: container images, pod logs, kubelet pods directory."},
        {"keywords": ["oom-killer", "out of memory", "killed process"],
         "conclusion": "Kernel OOM killer fired on the node. Kubelet or another critical process was killed.",
         "confidence": "high",
         "fix": "Check dmesg output above for which process was killed. Reduce memory consumption or increase node RAM."},
        {"keywords": ["panic", "bug:", "rip:", "call trace"],
         "conclusion": "Node experienced a kernel panic or kernel bug.",
         "confidence": "high",
         "fix": "Check BMC event log for timing. If recurring: check for known kernel bugs, update RHCOS, or replace hardware. Single event: node may recover after reboot via BMC."},
        {"keywords": ["hardware error", "mce:", "i/o error"],
         "conclusion": "Node has hardware errors (MCE or I/O). Likely failing disk, memory DIMM, or CPU.",
         "confidence": "high",
         "fix": "Check BMC SEL log for hardware events. Replace faulty component. Cordon node first if it recovers."},
        {"keywords": ["active", "running"],
         "conclusion": "Node is reachable and kubelet reports active/running. The NotReady condition may be transient or caused by API server connectivity issues.",
         "confidence": "medium",
         "fix": "Check kubelet journal for errors related to API server communication. Also check if node certificates have expired: oc get csr | grep Pending"},
    ],
    "mco-drain-blocked": [
        {"keywords": ["poddisruptionbudget", "pdb", "disruptionsallowed"],
         "conclusion": "PodDisruptionBudget is preventing node drain. MCO cannot evict pods protected by PDB, so the MachineConfig update is stuck.",
         "confidence": "high",
         "fix": "Identify the blocking PDB from output above. Options: 1) Scale up the deployment so PDB allows disruption, 2) Temporarily relax the PDB minAvailable, 3) If safe, delete the PDB temporarily."},
        {"keywords": ["terminating"],
         "conclusion": "Pods stuck in Terminating state are blocking the node drain.",
         "confidence": "high",
         "fix": "Force-delete stuck pods: oc delete pod <pod> -n <ns> --grace-period=0 --force. Then drain will complete and MCO proceeds."},
        {"keywords": ["(no stuck nodes found)", "0"],
         "conclusion": "No nodes currently stuck in SchedulingDisabled. The MCO drain may have already completed or the timeout was transient.",
         "confidence": "medium",
         "fix": "Check if MCP is now progressing: oc get mcp. If still degraded, check MCO operator logs for the specific error."},
        {"keywords": ["pending", "containercreating"],
         "conclusion": "Pods on the stuck node cannot be rescheduled - other nodes may lack capacity or have taints/affinity constraints.",
         "confidence": "medium",
         "fix": "Check node capacity: oc adm top nodes. Ensure other nodes have room for the pods being drained."},
    ],
    "virt-handler-errors-deep": [
        {"keywords": ["connection refused", "dial tcp", "connect:"],
         "conclusion": "virt-handler cannot connect to virt-api or other KubeVirt services. Network or service issue.",
         "confidence": "high",
         "fix": "Check virt-api pods above. If not Running, restart: oc delete pod -n openshift-cnv -l kubevirt.io=virt-api. If Running, check network policies."},
        {"keywords": ["tls", "certificate", "x509"],
         "conclusion": "TLS/certificate error in virt-handler. Certificates may have expired or been rotated without virt-handler restart.",
         "confidence": "high",
         "fix": "Restart virt-handler pods: oc delete pods -n openshift-cnv -l kubevirt.io=virt-handler. If persists, check HCO operator and cert-manager."},
        {"keywords": ["oomkilled", "oom", "killed"],
         "conclusion": "virt-handler pods are being OOM-killed. Current memory limit is too low for the VM density on these nodes.",
         "confidence": "high",
         "fix": "Increase memory limit from resource limits shown above. With high VM density, 2-4Gi is recommended. See CNV-71448.",
         "doc": "https://issues.redhat.com/browse/CNV-71448"},
        {"keywords": ["timeout", "context deadline"],
         "conclusion": "virt-handler experiencing timeouts communicating with KubeVirt control plane. Possible overload or network latency.",
         "confidence": "medium",
         "fix": "Check cluster network health and virt-api/virt-controller pod status. May resolve after reducing VM density."},
    ],
}


def _extract_context_from_results(results):
    """Extract additional context (like node_ip) from drilldown/investigation results."""
    extra = {}
    import re as _re
    for r in results:
        desc = r.get("description", "").lower()
        output = r.get("output", "").strip()
        if not output or output in ("(no output)", "(error: )"):
            continue
        if "node internal ip" in desc or "node ip" in desc:
            ip_match = _re.search(r'(\d+\.\d+\.\d+\.\d+)', output)
            if ip_match:
                extra["node_ip"] = ip_match.group(1)
        if "schedulingdisabled" in desc:
            lines = output.strip().split('\n')
            if lines and lines[0].strip():
                extra["stuck_node"] = lines[0].strip().split()[0]
    return extra


def run_followup(followup_key, context, drilldown_results, ssh_command_func):
    """Run level-3 followup commands after drilldown conclusion is known.
    Only executes safe, read-only diagnostic commands.
    Returns (followup_results, refined_conclusion_or_none).
    """
    import re as _re
    commands = FOLLOWUP_COMMANDS.get(followup_key, [])
    if not commands:
        return [], None

    enriched_ctx = dict(context)
    enriched_ctx.update(_extract_context_from_results(drilldown_results))

    if "node_ip" not in enriched_ctx and enriched_ctx.get("name"):
        try:
            ip_out = ssh_command_func(
                f"oc get node {enriched_ctx['name']} -o wide 2>&1 | tail -1 | awk '{{print $6}}'",
                timeout=8
            )
            if ip_out:
                ip_match = _re.search(r'(\d+\.\d+\.\d+\.\d+)', ip_out.strip())
                if ip_match:
                    enriched_ctx["node_ip"] = ip_match.group(1)
        except Exception:
            pass

    results = []
    for cmd_info in commands:
        cmd = cmd_info["cmd"]
        for key, value in enriched_ctx.items():
            cmd = cmd.replace("{" + key + "}", str(value))
        if "{node_ip}" in cmd or "{stuck_node}" in cmd:
            continue
        try:
            output = ssh_command_func(cmd, timeout=12)
            if output:
                output = output.strip()[:3000]
            else:
                output = "(no output)"
        except Exception as e:
            output = f"(error: {str(e)[:100]})"
        results.append({
            "description": cmd_info["desc"],
            "command": cmd,
            "output": output,
        })

    if not results:
        return [], None

    all_output = " ".join(r["output"] for r in results).lower()
    analysis_rules = FOLLOWUP_ANALYSIS_RULES.get(followup_key, [])
    best = None
    for arule in analysis_rules:
        kws = arule["keywords"]
        if any(kw.lower() in all_output for kw in kws):
            best = {
                "conclusion": arule["conclusion"],
                "confidence": arule.get("confidence", "medium"),
                "fix": arule.get("fix", ""),
                "doc": arule.get("doc", ""),
            }
            break

    return results, best


def run_drilldown(drilldown_key, context, ssh_command_func):
    """Run second-level drill-down commands for a symptom-level root cause.
    Returns (drilldown_results, conclusion_dict_or_none).
    """
    import re as _re
    commands = DRILLDOWN_COMMANDS.get(drilldown_key, [])
    if not commands:
        return [], None

    enriched_ctx = dict(context)
    if "node_ip" not in enriched_ctx and enriched_ctx.get("name"):
        try:
            ip_out = ssh_command_func(
                f"oc get node {enriched_ctx['name']} -o wide 2>&1 | tail -1 | awk '{{print $6}}'",
                timeout=8
            )
            if ip_out:
                ip_match = _re.search(r'(\d+\.\d+\.\d+\.\d+)', ip_out.strip())
                if ip_match:
                    enriched_ctx["node_ip"] = ip_match.group(1)
        except Exception:
            pass

    results = []
    for cmd_info in commands:
        cmd = cmd_info["cmd"]
        for key, value in enriched_ctx.items():
            cmd = cmd.replace("{" + key + "}", str(value))
        if "{node_ip}" in cmd:
            continue
        try:
            output = ssh_command_func(cmd, timeout=15)
            if output:
                output = output.strip()[:3000]
            else:
                output = "(no output)"
        except Exception as e:
            output = f"(error: {str(e)[:100]})"
        results.append({
            "description": cmd_info["desc"],
            "command": cmd,
            "output": output,
        })

    all_output = " ".join(r["output"] for r in results).lower()
    analysis_rules = DRILLDOWN_ANALYSIS_RULES.get(drilldown_key, [])
    best_conclusion = None
    for arule in analysis_rules:
        kws = arule["keywords"]
        if any(kw.lower() in all_output for kw in kws):
            best_conclusion = {
                "conclusion": arule["conclusion"],
                "confidence": arule.get("confidence", "medium"),
                "fix": arule.get("fix", ""),
                "doc": arule.get("doc", ""),
                "follow_drilldown": arule.get("follow_drilldown"),
                "followup": arule.get("followup"),
            }
            break

    return results, best_conclusion


def investigate_issue(issue_type, context, ssh_command_func):
    """
    Run investigation commands for a specific issue type.
    Returns list of investigation results.
    """
    try:
        from healthchecks.knowledge_base import load_investigation_commands
    except ImportError:
        from knowledge_base import load_investigation_commands
    inv_commands = load_investigation_commands()
    results = []
    commands = inv_commands.get(issue_type, [])
    
    for cmd_info in commands:
        cmd_template = cmd_info["cmd"]
        desc = cmd_info["desc"]
        
        # Substitute context variables
        cmd = cmd_template
        for key, value in context.items():
            cmd = cmd.replace("{" + key + "}", str(value))
        
        # Run command with shorter timeout for speed
        try:
            output = ssh_command_func(cmd, timeout=8)
            if output:
                output = output.strip()[:2000]  # Limit output size
            else:
                output = "(no output)"
        except Exception as e:
            output = f"(error: {str(e)[:100]})"
        
        results.append({
            "description": desc,
            "command": cmd,
            "output": output
        })
    
    return results

def _extract_vmi_count(investigation_results):
    """Extract VMI count from investigation results for special rules."""
    for r in investigation_results:
        if "Total VMI" in r.get("description", ""):
            try:
                return int(r.get("output", "0").strip())
            except (ValueError, AttributeError):
                pass
    return 0


def _extract_max_memory_mi(investigation_results):
    """Extract the maximum memory value in Mi from investigation output."""
    import re as _re
    max_mem = 0
    for r in investigation_results:
        output = r.get("output", "")
        for match in _re.findall(r'(\d+)Mi', output):
            val = int(match)
            if val > max_mem:
                max_mem = val
    return max_mem


def _evaluate_special(special_key, investigation_results):
    """Evaluate special (non-keyword) conditions."""
    if special_key == "vmi_count_gt_1000":
        return _extract_vmi_count(investigation_results) > 1000
    if special_key == "vmi_count_gt_500":
        count = _extract_vmi_count(investigation_results)
        return 500 < count <= 1000
    if special_key == "virt_handler_memory_gt_800mi":
        return _extract_max_memory_mi(investigation_results) > 800
    return False


def _rule_matches(rule, issue_type, all_output, investigation_results):
    """Check if a single root cause rule matches the given context.

    Rule schema:
      issue_types      - list of issue types this rule applies to
      keywords_all     - ALL must appear in output (AND)
      keywords_any     - at least ONE must appear (OR)
      extra_required   - additional keywords that ALL must appear (AND)
      extra_required_any - at least ONE must appear (OR)
      special          - non-keyword condition key (e.g. vmi_count_gt_1000)
    """
    if issue_type not in rule.get("issue_types", []):
        return False

    special = rule.get("special")
    if special:
        return _evaluate_special(special, investigation_results)

    kw_all = rule.get("keywords_all", [])
    if kw_all and not all(kw.lower() in all_output for kw in kw_all):
        return False

    kw_any = rule.get("keywords_any", [])
    if kw_any and not any(kw.lower() in all_output for kw in kw_any):
        return False

    extra_req = rule.get("extra_required", [])
    if extra_req and not all(kw.lower() in all_output for kw in extra_req):
        return False

    extra_req_any = rule.get("extra_required_any", [])
    if extra_req_any and not any(kw.lower() in all_output for kw in extra_req_any):
        return False

    if not kw_all and not kw_any and not extra_req and not extra_req_any:
        return False

    return True


def determine_root_cause(issue_type, investigation_results, failure_details):
    """Analyze investigation results to determine the most likely root cause.

    Loads rules from knowledge/root_cause_rules.json so the logic is
    extensible without code changes.
    Returns (root_cause, confidence, explanation, rule_key, matched_rule).
    The last two are optional for backward compat - callers using 3-tuple
    unpacking still work because extra values are silently ignored by
    tuple assignment.
    """
    from healthchecks.knowledge_base import (
        load_root_cause_rules, update_root_cause_rule_matched,
    )

    all_output = " ".join(
        [r.get("output", "") for r in investigation_results]
    ).lower()

    if failure_details:
        if isinstance(failure_details, dict):
            all_output += " " + " ".join(str(v) for v in failure_details.values()).lower()
        elif isinstance(failure_details, list):
            for fd in failure_details:
                if isinstance(fd, dict):
                    all_output += " " + " ".join(str(v) for v in fd.values()).lower()
                else:
                    all_output += " " + str(fd).lower()
        else:
            all_output += " " + str(failure_details).lower()

    rules = load_root_cause_rules()
    root_causes = []

    for rule_key, rule in rules.items():
        if _rule_matches(rule, issue_type, all_output, investigation_results):
            root_causes.append((
                rule["cause"],
                rule.get("confidence", "medium"),
                rule.get("explanation", ""),
                rule_key,
                rule,
            ))

    if not root_causes:
        return ("Unknown", "low", "Further manual investigation required", None, None)

    confidence_order = {"high": 0, "medium": 1, "low": 2}
    root_causes.sort(key=lambda x: confidence_order.get(x[1], 3))

    best = root_causes[0]
    try:
        update_root_cause_rule_matched(best[3])
    except Exception:
        pass

    return (best[0], best[1], best[2], best[3], best[4])

def parse_version(version_str):
    """Parse version string to comparable tuple"""
    if not version_str:
        return (0, 0, 0)
    # Handle formats like "4.21.0-ec.3", "4.17", "CNV 4.17.0"
    match = re.search(r'(\d+)\.(\d+)(?:\.(\d+))?', str(version_str))
    if match:
        major = int(match.group(1))
        minor = int(match.group(2))
        patch = int(match.group(3)) if match.group(3) else 0
        return (major, minor, patch)
    return (0, 0, 0)

def compare_versions(v1, v2):
    """Compare two version strings. Returns: -1 if v1 < v2, 0 if equal, 1 if v1 > v2"""
    v1_tuple = parse_version(v1)
    v2_tuple = parse_version(v2)
    if v1_tuple < v2_tuple:
        return -1
    elif v1_tuple > v2_tuple:
        return 1
    return 0

def check_jira_bugs(jira_keys, cluster_version):
    """
    Check Jira bug status and determine if bugs are open, fixed, or regression.
    Uses subprocess to call the Jira MCP tool.
    
    Returns dict with bug info: {
        'CNV-12345': {
            'status': 'Closed',
            'resolution': 'Done',
            'fix_versions': ['CNV 4.17.0'],
            'affects_versions': ['CNV 4.16.0'],
            'assessment': 'fixed'|'open'|'regression'|'unknown',
            'assessment_detail': 'Fixed in CNV 4.17.0, you are on 4.21'
        }
    }
    """
    import subprocess
    
    results = {}
    
    for jira_key in jira_keys:
        if not jira_key or jira_key in ["OCPBUGS-storage", "OCPBUGS-general", "CNV-storage"]:
            # Skip placeholder keys
            continue
            
        if jira_key in JIRA_BUG_CACHE:
            results[jira_key] = JIRA_BUG_CACHE[jira_key]
            continue
        
        try:
            # Call the Jira MCP tool via cursor's mcp-proxy if available, 
            # or use direct Jira API
            # For now, we'll use a cached/known status approach
            
            # Try to get from environment or use known statuses
            bug_info = get_known_bug_info(jira_key, cluster_version)
            results[jira_key] = bug_info
            JIRA_BUG_CACHE[jira_key] = bug_info
            
        except Exception as e:
            results[jira_key] = {
                'status': 'Unknown',
                'resolution': None,
                'fix_versions': [],
                'assessment': 'unknown',
                'assessment_detail': f'Unable to fetch: {str(e)}'
            }
    
    return results

def get_known_bug_info(jira_key, cluster_version):
    """
    Get known bug information from the dynamic knowledge base.
    Falls back to the hardcoded dict for backward compatibility.
    """
    try:
        from healthchecks.knowledge_base import load_known_bugs
    except ImportError:
        from knowledge_base import load_known_bugs
    known_bugs = load_known_bugs()
    
    if jira_key in known_bugs:
        bug = known_bugs[jira_key]
        assessment, detail = assess_bug_status(bug, cluster_version, jira_key)
        return {
            'status': bug['status'],
            'resolution': bug.get('resolution'),
            'fix_versions': bug.get('fix_versions', []),
            'affects_versions': bug.get('affects', []),
            'assessment': assessment,
            'assessment_detail': detail
        }
    
    # Unknown bug - return generic info
    return {
        'status': 'Unknown',
        'resolution': None,
        'fix_versions': [],
        'affects_versions': [],
        'assessment': 'unknown',
        'assessment_detail': f'Bug {jira_key} not in local database'
    }

def assess_bug_status(bug, cluster_version, jira_key):
    """
    Assess if a bug is relevant to current cluster version.
    Returns (assessment, detail) tuple.
    """
    status = bug.get('status', 'Unknown')
    fix_versions = bug.get('fix_versions', [])
    affects = bug.get('affects', [])
    
    # Parse cluster version (e.g., "4.21.0-ec.3" -> (4, 21, 0))
    cluster_ver = parse_version(cluster_version)
    
    # Open/In Progress bugs
    if status in ['Open', 'In Progress', 'New', 'To Do']:
        # Check if affects current version
        for av in affects:
            av_ver = parse_version(av)
            if av_ver[0] == cluster_ver[0] and av_ver[1] <= cluster_ver[1]:
                return ('open', f'🔴 OPEN - Affects your version ({cluster_version})')
        return ('open', f'🟡 OPEN - May affect version {cluster_version}')
    
    # Closed/Done bugs
    if status in ['Closed', 'Done', 'Resolved']:
        if fix_versions:
            # Find the lowest fix version
            fix_ver = min([parse_version(fv) for fv in fix_versions])
            fix_ver_str = fix_versions[0]
            
            # Compare with cluster version
            if cluster_ver >= fix_ver:
                # Bug was fixed in a version <= current
                # This could be a regression!
                return ('regression', f'⚠️ POTENTIAL REGRESSION - Fixed in {fix_ver_str}, you have {cluster_version}')
            else:
                # Bug fixed in newer version
                return ('fixed_newer', f'🟢 Fixed in {fix_ver_str} - Upgrade from {cluster_version} to resolve')
        else:
            return ('fixed', f'🟢 Closed/Resolved')
    
    return ('unknown', f'Status: {status}')

def format_raw_output(details, failure_type):
    """Format raw details into readable output like oc command result"""
    if isinstance(details, list):
        if not details:
            return "(no data)"
        lines = []
        for item in details[:8]:  # Limit to 8 items
            if isinstance(item, dict):
                if "ns" in item and "name" in item:
                    lines.append(f"{item.get('ns', '-'):<30} {item.get('name', '-'):<45} {item.get('status', '-')}")
                elif "name" in item:
                    lines.append(f"{item.get('name', '-'):<45} {item.get('status', item.get('memory', '-'))}")
                else:
                    lines.append(str(item))
            else:
                lines.append(str(item))
        if len(details) > 8:
            lines.append(f"... +{len(details) - 8} more")
        return "\n".join(lines)
    elif isinstance(details, dict):
        return "\n".join([f"{k}: {v}" for k, v in list(details.items())[:5]])
    else:
        return str(details)

def analyze_failures(data):
    """Analyze failures and match to known issues from Jira"""
    analysis = []
    
    # Check each failure type against known issues
    failures = []
    
    # Collect all failures with raw output
    
    # Degraded / unavailable cluster operators
    if data.get("operators", {}).get("degraded"):
        raw_lines = ["NAME" + " " * 40 + "STATUS"]
        for op in data["operators"]["degraded"]:
            raw_lines.append(f"{op:<44} Degraded")
        failures.append({
            "type": "operator-degraded",
            "name": "Cluster Operators",
            "status": f"{len(data['operators']['degraded'])} degraded",
            "details": data["operators"]["degraded"],
            "raw_output": "\n".join(raw_lines)
        })
    
    if data.get("operators", {}).get("unavailable"):
        raw_lines = ["NAME" + " " * 40 + "STATUS"]
        for op in data["operators"]["unavailable"]:
            raw_lines.append(f"{op:<44} Unavailable")
        failures.append({
            "type": "operator-unavailable",
            "name": "Cluster Operators",
            "status": f"{len(data['operators']['unavailable'])} unavailable",
            "details": data["operators"]["unavailable"],
            "raw_output": "\n".join(raw_lines)
        })
    
    # Unhealthy nodes
    if data.get("nodes", {}).get("unhealthy"):
        raw_lines = ["NAME" + " " * 30 + "STATUS" + " " * 10 + "ROLES"]
        for node in data["nodes"]["unhealthy"]:
            if isinstance(node, dict):
                raw_lines.append(f"{node.get('name', '-'):<34} {node.get('status', '-'):<16} {node.get('roles', '-')}")
            else:
                raw_lines.append(str(node))
        failures.append({
            "type": "node",
            "name": "Nodes",
            "status": f"{len(data['nodes']['unhealthy'])} not ready",
            "details": data["nodes"]["unhealthy"],
            "raw_output": "\n".join(raw_lines)
        })
    
    # Firing alerts
    if data.get("alerts"):
        raw_lines = ["ALERT" + " " * 35 + "SEVERITY" + " " * 5 + "NAMESPACE"]
        for alert in data["alerts"][:15]:
            if isinstance(alert, dict):
                raw_lines.append(f"{alert.get('name', '-'):<40} {alert.get('severity', '-'):<13} {alert.get('namespace', '-')}")
            else:
                raw_lines.append(str(alert))
        if len(data["alerts"]) > 15:
            raw_lines.append(f"... +{len(data['alerts']) - 15} more alerts")
        failures.append({
            "type": "alert",
            "name": "Firing Alerts",
            "status": f"{len(data['alerts'])} firing",
            "details": data["alerts"],
            "raw_output": "\n".join(raw_lines)
        })
    
    if data["pods"]["unhealthy"]:
        # Format pod output like oc get pods
        raw_lines = ["NAMESPACE" + " "*22 + "NAME" + " "*41 + "STATUS"]
        for pod in data["pods"]["unhealthy"][:10]:
            raw_lines.append(f"{pod['ns']:<30} {pod['name']:<45} {pod['status']}")
        if len(data["pods"]["unhealthy"]) > 10:
            raw_lines.append(f"... +{len(data['pods']['unhealthy']) - 10} more pods")
        
        for pod in data["pods"]["unhealthy"]:
            failures.append({
                "type": "pod",
                "name": f"{pod['ns']}/{pod['name']}",
                "status": pod["status"],
                "details": pod,
                "raw_output": "\n".join(raw_lines)
            })
    
    if data["virt_handler"]["unhealthy"]:
        raw_out = format_raw_output(data["virt_handler"]["unhealthy"], "virt-handler")
        failures.append({
            "type": "virt-handler",
            "name": "virt-handler pods",
            "status": "unhealthy",
            "details": data["virt_handler"]["unhealthy"],
            "raw_output": raw_out
        })
    
    if data["virt_handler"]["high_memory"]:
        # Format like oc adm top pods output
        raw_lines = ["NAME" + " "*36 + "CPU" + " "*5 + "MEMORY"]
        for pod in data["virt_handler"]["high_memory"][:8]:
            raw_lines.append(f"{pod.get('name', '-'):<40} {pod.get('cpu', '-'):<8} {pod.get('memory', '-')}")
        if len(data["virt_handler"]["high_memory"]) > 8:
            raw_lines.append(f"... +{len(data['virt_handler']['high_memory']) - 8} more")
        
        failures.append({
            "type": "virt-handler-memory",
            "name": "virt-handler memory",
            "status": f"{len(data['virt_handler']['high_memory'])} pods high memory",
            "details": data["virt_handler"]["high_memory"],
            "raw_output": "\n".join(raw_lines)
        })
    
    if data["snapshot_issues"]:
        raw_out = format_raw_output(data["snapshot_issues"], "snapshot")
        failures.append({
            "type": "volumesnapshot",
            "name": "VolumeSnapshots",
            "status": f"{len(data['snapshot_issues'])} not ready",
            "details": data["snapshot_issues"],
            "raw_output": raw_out
        })
    
    if data["dv_issues"]:
        raw_out = format_raw_output(data["dv_issues"], "dv")
        failures.append({
            "type": "datavolume",
            "name": "DataVolumes",
            "status": f"{len(data['dv_issues'])} stuck",
            "details": data["dv_issues"],
            "raw_output": raw_out
        })
    
    if data["migrations"]["failed"] or data["migrations"]["failed_count"] > 0:
        raw_out = format_raw_output(data["migrations"]["failed"], "migration")
        failures.append({
            "type": "migration-failed",
            "name": "VM Migrations",
            "status": "failed",
            "details": data["migrations"]["failed"],
            "raw_output": raw_out
        })
    
    if data["stuck_migrations"]:
        raw_out = format_raw_output(data["stuck_migrations"], "migration")
        failures.append({
            "type": "stuck-migration",
            "name": "Stuck Migrations",
            "status": f"{len(data['stuck_migrations'])} stuck",
            "details": data["stuck_migrations"],
            "raw_output": raw_out
        })
    
    if data["cordoned_vms"]:
        raw_out = format_raw_output(data["cordoned_vms"], "vmi")
        failures.append({
            "type": "cordoned-vms",
            "name": "VMs on cordoned nodes",
            "status": f"{len(data['cordoned_vms'])} at risk",
            "details": data["cordoned_vms"],
            "raw_output": raw_out
        })
    
    if data["etcd"]["unhealthy"]:
        raw_out = format_raw_output(data["etcd"]["unhealthy"], "etcd")
        failures.append({
            "type": "etcd",
            "name": "etcd",
            "status": "unhealthy",
            "details": data["etcd"]["unhealthy"],
            "raw_output": raw_out
        })
    
    if data["oom_events"]:
        raw_out = format_raw_output(data["oom_events"], "events")
        failures.append({
            "type": "oom",
            "name": "OOM Events",
            "status": f"{len(data['oom_events'])} events",
            "details": data["oom_events"],
            "raw_output": raw_out
        })
    
    if data["csi_issues"]:
        raw_out = format_raw_output(data["csi_issues"], "csi")
        failures.append({
            "type": "csi",
            "name": "CSI Drivers",
            "status": f"{len(data['csi_issues'])} issues",
            "details": data["csi_issues"],
            "raw_output": raw_out
        })
    
    # Load patterns from the dynamic knowledge base (falls back to hardcoded on first run)
    try:
        from healthchecks.knowledge_base import load_known_issues, update_last_matched
    except ImportError:
        from knowledge_base import load_known_issues, update_last_matched
    known_issues = load_known_issues()

    # Match failures to known issues (prefer specific matches over generic)
    for failure in failures:
        matched_issues = []
        failure_text = f"{failure['type']} {failure['name']} {failure['status']} {str(failure['details'])}".lower()
        
        for issue_key, issue in known_issues.items():
            match_count = 0
            for pattern in issue["pattern"]:
                if pattern.lower() in failure_text:
                    match_count += 1
            if match_count > 0:
                matched_issues.append((match_count, len(issue.get("jira", [])), issue_key, issue))
        
        if matched_issues:
            # Sort: most pattern matches first, then most Jira refs (= most specific)
            matched_issues.sort(key=lambda x: (-x[0], -x[1]))
            best_key = matched_issues[0][2]
            best_match = matched_issues[0][3]
            all_matches = [m[3] for m in matched_issues]
            try:
                update_last_matched(best_key)
            except Exception:
                pass
            analysis.append({
                "failure": failure,
                "matched_issue": best_match,
                "all_matches": all_matches,
                "investigation": None,
                "determined_cause": None
            })
        else:
            # Generic analysis for unmatched failures
            analysis.append({
                "failure": failure,
                "matched_issue": {
                    "title": f"Unknown Issue: {failure['name']}",
                    "jira": [],
                    "description": f"Issue detected: {failure['status']}",
                    "root_cause": ["Unable to determine root cause from known issues database"],
                    "suggestions": [
                        f"Check pod/resource logs: oc logs <pod> -n <namespace>",
                        f"Describe the resource: oc describe <resource>",
                        "Search Jira for similar issues",
                        "Contact support if issue persists"
                    ]
                },
                "all_matches": [],
                "investigation": None,
                "determined_cause": None
            })
    
    return analysis

def run_deep_investigation(analysis, ssh_command_func, max_unique_types=10):
    """
    Run deep investigation for issues in the analysis.
    OPTIMIZATION: Clusters issues by symptom/type and only investigates ONE
    representative issue per cluster, then applies results to all similar issues.
    """
    import hashlib
    
    # Helper function to get investigation type and context for an item
    def get_inv_info(item):
        failure = item["failure"]
        failure_type = failure.get("type", "")
        details = failure.get("details", {})
        
        # Determine investigation type based on failure
        if failure_type == "pod":
            status = failure.get("status", "").lower()
            if "crashloop" in status or "error" in status or "init:" in status:
                inv_type = "pod-crashloop"
            elif "unknown" in status or "pending" in status:
                inv_type = "pod-unknown"
            else:
                inv_type = "pod-unknown"
            
            # Check for specific pod types
            name = failure.get("name", "").lower()
            if "noobaa" in name:
                inv_type = "noobaa"
            elif "metal3" in name:
                inv_type = "metal3"
            
            # Build context
            if isinstance(details, dict):
                context = {
                    "pod": details.get("name", ""),
                    "ns": details.get("ns", ""),
                    "name": details.get("name", ""),
                }
            else:
                parts = failure.get("name", "").split("/")
                context = {
                    "pod": parts[1] if len(parts) > 1 else parts[0],
                    "ns": parts[0] if len(parts) > 1 else "default",
                    "name": parts[1] if len(parts) > 1 else parts[0],
                }
        
        elif failure_type == "virt-handler-memory":
            inv_type = "virt-handler-memory"
            context = {}
        
        elif failure_type == "volumesnapshot":
            inv_type = "volumesnapshot"
            if isinstance(details, list) and details:
                first = details[0] if isinstance(details[0], dict) else {}
                context = {"name": first.get("name", ""), "ns": first.get("ns", "")}
            else:
                context = {"name": "", "ns": ""}
        
        elif failure_type == "etcd":
            inv_type = "etcd"
            context = {}
        
        elif failure_type in ["migration-failed", "stuck-migration"]:
            inv_type = "migration"
            if isinstance(details, list) and details:
                first = details[0] if isinstance(details[0], dict) else {}
                context = {"name": first.get("name", ""), "ns": first.get("ns", ""), "vm": first.get("vm", "")}
            else:
                context = {"name": "", "ns": "", "vm": ""}
        
        elif failure_type == "csi":
            inv_type = "csi"
            if isinstance(details, dict):
                context = {"pod": details.get("pod", details.get("name", "")), "ns": details.get("ns", "")}
            elif isinstance(details, list) and details:
                first = details[0] if isinstance(details[0], dict) else {}
                context = {"pod": first.get("pod", first.get("name", "")), "ns": first.get("ns", "")}
            else:
                context = {"pod": "", "ns": ""}
        
        elif failure_type == "oom":
            inv_type = "oom"
            if isinstance(details, list) and details:
                first = details[0] if isinstance(details[0], dict) else {}
                context = {"pod": first.get("name", ""), "ns": first.get("ns", "")}
            else:
                context = {"pod": "", "ns": ""}
        
        elif failure_type in ["operator-degraded", "operator-unavailable"]:
            inv_type = failure_type
            if isinstance(details, list) and details:
                context = {"name": details[0] if isinstance(details[0], str) else str(details[0])}
            else:
                context = {"name": ""}
        
        elif failure_type == "node":
            inv_type = "node"
            if isinstance(details, list) and details:
                first = details[0]
                if isinstance(first, dict):
                    context = {"name": first.get("name", "")}
                else:
                    context = {"name": str(first)}
            else:
                context = {"name": ""}
        
        elif failure_type == "alert":
            inv_type = "alert"
            context = {}
        
        else:
            inv_type = "pod-unknown"
            context = {"pod": "", "ns": "", "name": ""}
        
        return inv_type, context, failure_type, details
    
    # Step 1: Group issues by their matched issue title (symptom)
    symptom_groups = {}
    for item in analysis:
        # Use matched issue title as the grouping key
        symptom_key = item.get("matched_issue", {}).get("title", "unknown")
        if symptom_key not in symptom_groups:
            symptom_groups[symptom_key] = []
        symptom_groups[symptom_key].append(item)
    
    unique_symptoms = len(symptom_groups)
    total_issues = len(analysis)
    
    print(f"        Found {unique_symptoms} unique issue types across {total_issues} issues", flush=True)
    print(f"        Investigating ONE representative per type (saves {total_issues - unique_symptoms} duplicate investigations)", flush=True)
    
    # Step 2: Investigate all symptom groups in parallel
    try:
        from healthchecks.ai_analysis import ai_investigate
    except ImportError:
        from ai_analysis import ai_investigate

    import concurrent.futures
    import time as _time

    groups_to_investigate = list(symptom_groups.items())[:max_unique_types]

    def _investigate_one(idx, symptom_key, items):
        """Investigate a single symptom group. Returns (symptom_key, items) with results attached."""
        tag = f"[{idx+1}/{len(groups_to_investigate)}]"
        representative = items[0]
        inv_type, context, failure_type, details = get_inv_info(representative)

        print(f"        {tag} Investigating: {symptom_key[:50]}... ({len(items)} similar)", flush=True)

        investigation_results = investigate_issue(inv_type, context, ssh_command_func)
        if not investigation_results:
            return symptom_key, items

        rc_tuple = determine_root_cause(inv_type, investigation_results, details)
        root_cause, confidence, explanation = rc_tuple[0], rc_tuple[1], rc_tuple[2]
        matched_rule = rc_tuple[4] if len(rc_tuple) > 4 else None

        inv_id = hashlib.md5(f"{symptom_key}".encode()).hexdigest()[:8]
        drilldown_results = []
        drilldown_conclusion = None
        followup_results = []
        followup_conclusion = None
        next_steps = []

        if matched_rule and matched_rule.get("is_symptom") and matched_rule.get("drilldown"):
            drilldown_key = matched_rule["drilldown"]
            print(f"        {tag} ↳ Symptom, drilling down: {drilldown_key}", flush=True)

            dd_results, dd_conclusion = run_drilldown(drilldown_key, context, ssh_command_func)
            drilldown_results = dd_results

            if dd_conclusion:
                drilldown_conclusion = dd_conclusion
                root_cause = dd_conclusion["conclusion"]
                confidence = dd_conclusion["confidence"]
                explanation = dd_conclusion.get("fix", explanation)
                print(f"        {tag} ✓ Drilldown: {root_cause[:60]}", flush=True)

                follow = dd_conclusion.get("follow_drilldown")
                if follow and follow != drilldown_key:
                    dd2_results, dd2_conclusion = run_drilldown(follow, context, ssh_command_func)
                    drilldown_results.extend(dd2_results)
                    if dd2_conclusion:
                        root_cause = dd2_conclusion["conclusion"]
                        confidence = dd2_conclusion["confidence"]
                        explanation = dd2_conclusion.get("fix", explanation)
                        drilldown_conclusion = dd2_conclusion
            else:
                print(f"        {tag} ⚠ Drilldown inconclusive", flush=True)

            next_steps = matched_rule.get("next_steps", [])

        issue_obj = representative.get("matched_issue", {})
        inv_cmds = issue_obj.get("investigation_commands", [])
        jira_refs = issue_obj.get("jira", [])
        print(f"        {tag} ↳ AI investigating...", flush=True)
        fu_results, fu_conclusion = ai_investigate(
            issue_title=issue_obj.get("title", symptom_key),
            issue_desc=issue_obj.get("description", ""),
            failure=representative["failure"],
            investigation_results=investigation_results,
            drilldown_results=drilldown_results,
            drilldown_conclusion=drilldown_conclusion,
            ssh_command_func=ssh_command_func,
            matched_inv_commands=inv_cmds if inv_cmds else None,
            jira_refs=jira_refs if jira_refs else None,
        )
        followup_results = fu_results
        if fu_conclusion:
            followup_conclusion = fu_conclusion
            root_cause = fu_conclusion["conclusion"]
            confidence = fu_conclusion["confidence"]
            explanation = fu_conclusion.get("fix", explanation)
            print(f"        {tag} ✓ AI verified: {root_cause[:70]}", flush=True)
            if fu_conclusion.get("needs_manual"):
                next_steps = [fu_conclusion["needs_manual"]]
        elif fu_results:
            print(f"        {tag} ✓ AI collected {len(fu_results)} checks", flush=True)
        else:
            print(f"        {tag} ⚠ AI skipped (no API key)", flush=True)

        for item in items:
            item["investigation"] = investigation_results
            item["determined_cause"] = {
                "cause": root_cause,
                "confidence": confidence,
                "explanation": explanation,
                "investigation_id": inv_id,
                "shared_with": len(items) - 1,
            }
            if drilldown_results:
                item["drilldown"] = {"results": drilldown_results, "conclusion": drilldown_conclusion}
            if followup_results:
                item["followup"] = {"results": followup_results, "conclusion": followup_conclusion}
            if next_steps:
                item["determined_cause"]["next_steps"] = next_steps
            if followup_conclusion and followup_conclusion.get("doc"):
                item["determined_cause"]["doc_url"] = followup_conclusion["doc"]
            elif drilldown_conclusion and drilldown_conclusion.get("doc"):
                item["determined_cause"]["doc_url"] = drilldown_conclusion["doc"]

        return symptom_key, items

    # Run all investigations in parallel (max 4 concurrent to avoid SSH/API overload)
    t0 = _time.time()
    max_workers = min(4, len(groups_to_investigate))
    print(f"        ⚡ Running {len(groups_to_investigate)} investigations in parallel (max {max_workers} workers)", flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_investigate_one, idx, sk, items): sk
            for idx, (sk, items) in enumerate(groups_to_investigate)
        }
        investigation_count = 0
        for future in concurrent.futures.as_completed(futures):
            sk = futures[future]
            try:
                _, result_items = future.result(timeout=480)
                if result_items and result_items[0].get("investigation"):
                    investigation_count += 1
            except Exception as exc:
                print(f"        ⚠ Investigation failed for {sk[:40]}: {exc}", flush=True)

    elapsed = _time.time() - t0
    if unique_symptoms > max_unique_types:
        print(f"        (Skipped {unique_symptoms - max_unique_types} additional issue types)", flush=True)

    print(f"        Deep investigation complete: {investigation_count} investigations in {elapsed:.0f}s", flush=True)

    return analysis

def generate_rca_html(analysis, cluster_version="", show_investigation=True, email_data=None):
    """Generate HTML for Root Cause Analysis section - grouped by issue type
    
    show_investigation: If False, only show bug matching without deep investigation
    email_data: Dict containing email search results
    """
    if not analysis:
        return ""
    
    # Group by issue title to bundle similar issues
    grouped = {}
    for item in analysis:
        title = item["matched_issue"]["title"]
        if title not in grouped:
            grouped[title] = {
                "issue": item["matched_issue"],
                "failures": [],
                "raw_outputs": [],
                "investigations": [],
                "determined_causes": []
            }
        grouped[title]["failures"].append(item["failure"])
        # Collect raw output (avoid duplicates)
        raw = item["failure"].get("raw_output", "")
        if raw and raw not in grouped[title]["raw_outputs"]:
            grouped[title]["raw_outputs"].append(raw)
        # Collect investigations
        if item.get("investigation"):
            grouped[title]["investigations"].append({
                "failure_name": item["failure"].get("name", ""),
                "results": item["investigation"]
            })
        if item.get("determined_cause"):
            grouped[title]["determined_causes"].append(item["determined_cause"])
        if item.get("drilldown"):
            grouped[title]["drilldown"] = item["drilldown"]
        if item.get("followup"):
            grouped[title]["followup"] = item["followup"]
    
    # Collect all Jira keys and check their status
    all_jira_keys = []
    for data in grouped.values():
        all_jira_keys.extend(data["issue"].get("jira", []))
    
    # Check bug status against cluster version
    bug_status_info = check_jira_bugs(all_jira_keys, cluster_version)
    
    # Count bug categories
    open_bugs = sum(1 for b in bug_status_info.values() if b.get('assessment') == 'open')
    regression_bugs = sum(1 for b in bug_status_info.values() if b.get('assessment') == 'regression')
    fixed_bugs = sum(1 for b in bug_status_info.values() if b.get('assessment') in ['fixed', 'fixed_newer'])
    
    html = '''
    <div class="panel rca-panel" style="border-color:#FF9830;">
        <div class="panel-title" style="background:#2d1f0f;color:#FF9830;">🔍 Root Cause Analysis & Recommendations</div>
        <div style="padding:20px;">
            <p style="color:var(--text-secondary);margin-bottom:12px;font-size:13px;">
                Analysis based on Red Hat Jira bug database (CNV, OCPBUGS projects) • {count} issue categories identified
            </p>
            <div style="display:flex;gap:16px;margin-bottom:20px;">
                <div style="background:#1a0a0a;border:1px solid #F2495C;border-radius:6px;padding:8px 16px;">
                    <span style="color:#F2495C;font-weight:600;">{open_count}</span>
                    <span style="color:#8b949e;font-size:12px;margin-left:4px;">Open Bugs</span>
                </div>
                <div style="background:#1a1a0a;border:1px solid #FF9830;border-radius:6px;padding:8px 16px;">
                    <span style="color:#FF9830;font-weight:600;">{regression_count}</span>
                    <span style="color:#8b949e;font-size:12px;margin-left:4px;">Potential Regressions</span>
                </div>
                <div style="background:#0a1a0a;border:1px solid #73BF69;border-radius:6px;padding:8px 16px;">
                    <span style="color:#73BF69;font-weight:600;">{fixed_count}</span>
                    <span style="color:#8b949e;font-size:12px;margin-left:4px;">Fixed (upgrade available)</span>
                </div>
            </div>
    '''.format(count=len(grouped), open_count=open_bugs, regression_count=regression_bugs, fixed_count=fixed_bugs)
    
    # Add email search results if available
    if email_data and email_data.get('keywords'):
        keywords = email_data.get('keywords', [])
        html += f'''
            <div style="margin-bottom:20px;padding:12px 16px;background:linear-gradient(135deg, #1a1a2e 0%, #0d1117 100%);border:1px solid #30363d;border-radius:8px;">
                <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;">
                    <span style="font-size:16px;">📧</span>
                    <span style="color:#58a6ff;font-weight:600;font-size:13px;">Email Search Keywords</span>
                </div>
                <div style="display:flex;flex-wrap:wrap;gap:8px;">
        '''
        for keyword in keywords[:8]:  # Limit to 8 keywords
            html += f'''
                    <span style="background:#21262d;border:1px solid #30363d;padding:4px 10px;border-radius:12px;font-size:11px;color:#c9d1d9;">
                        🔍 {keyword}
                    </span>
            '''
        html += '''
                </div>
                <p style="color:#8b949e;font-size:11px;margin-top:10px;margin-bottom:0;">
                    💡 Use these keywords to search your inbox for related discussions, alerts, or previous incidents.
                </p>
            </div>
        '''
    
    # Executive Summary Table
    exec_rows = []
    for title, gdata in grouped.items():
        causes = gdata.get("determined_causes", [])
        drilldown = gdata.get("drilldown")
        followup = gdata.get("followup")
        best = causes[0] if causes else None
        num_affected = len(gdata["failures"])

        if best:
            cause_text = best["cause"]
            conf = best["confidence"]
            has_fix = bool(drilldown and drilldown.get("conclusion", {}) and drilldown["conclusion"].get("fix"))
            has_dd = bool(drilldown and drilldown.get("results"))
            has_fu = bool(followup and followup.get("results"))
            has_ns = bool(best.get("next_steps"))
            has_doc = bool(best.get("doc_url") or (drilldown and drilldown.get("conclusion", {}) and drilldown["conclusion"].get("doc")))
        else:
            cause_text = "Investigation pending"
            conf = "low"
            has_fix = False
            has_dd = False
            has_fu = False
            has_ns = False
            has_doc = False

        conf_color = "#73BF69" if conf == "high" else "#FF9830" if conf == "medium" else "#8b949e"
        check = '<span style="color:#73BF69;">&#10003;</span>'
        dash = '<span style="color:#30363d;">-</span>'

        exec_rows.append(f'''
            <tr style="border-bottom:1px solid #21262d;">
                <td style="padding:10px 12px;color:#e6edf3;font-weight:600;font-size:12px;max-width:180px;">{title}</td>
                <td style="padding:10px 8px;text-align:center;"><span style="color:#c9d1d9;font-size:12px;">{num_affected}</span></td>
                <td style="padding:10px 12px;color:#c9d1d9;font-size:11px;max-width:280px;">{cause_text}</td>
                <td style="padding:10px 8px;text-align:center;"><span style="background:{conf_color}22;color:{conf_color};padding:2px 8px;border-radius:8px;font-size:10px;font-weight:600;text-transform:uppercase;">{conf}</span></td>
                <td style="padding:10px 8px;text-align:center;">{check if has_dd else dash}</td>
                <td style="padding:10px 8px;text-align:center;">{check if has_fu else dash}</td>
                <td style="padding:10px 8px;text-align:center;">{check if has_fix else dash}</td>
                <td style="padding:10px 8px;text-align:center;">{check if has_doc else dash}</td>
            </tr>
        ''')

    if exec_rows:
        total_issues = sum(len(g["failures"]) for g in grouped.values())
        total_with_rc = sum(1 for g in grouped.values() if g.get("determined_causes") and g["determined_causes"][0].get("confidence") in ("high", "medium"))
        total_cats = len(grouped)

        html += f'''
            <div style="margin-bottom:24px;padding:18px;background:linear-gradient(135deg, #0d1117 0%, #161b22 100%);border:1px solid #30363d;border-radius:10px;">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
                    <div style="color:#58a6ff;font-weight:700;font-size:14px;">📊 Executive RCA Summary</div>
                    <div style="display:flex;gap:12px;">
                        <span style="color:#8b949e;font-size:11px;">{total_cats} categories</span>
                        <span style="color:#8b949e;font-size:11px;">{total_issues} total issues</span>
                        <span style="color:#73BF69;font-size:11px;font-weight:600;">{total_with_rc}/{total_cats} root-caused</span>
                    </div>
                </div>
                <div style="overflow-x:auto;">
                    <table style="width:100%;border-collapse:collapse;font-family:'JetBrains Mono',Monaco,monospace;">
                        <thead>
                            <tr style="border-bottom:2px solid #30363d;">
                                <th style="padding:8px 12px;text-align:left;color:#8b949e;font-size:10px;text-transform:uppercase;font-weight:600;letter-spacing:0.5px;">Issue</th>
                                <th style="padding:8px 8px;text-align:center;color:#8b949e;font-size:10px;text-transform:uppercase;font-weight:600;">Affected</th>
                                <th style="padding:8px 12px;text-align:left;color:#8b949e;font-size:10px;text-transform:uppercase;font-weight:600;letter-spacing:0.5px;">Root Cause</th>
                                <th style="padding:8px 8px;text-align:center;color:#8b949e;font-size:10px;text-transform:uppercase;font-weight:600;">Confidence</th>
                                <th style="padding:8px 8px;text-align:center;color:#8b949e;font-size:10px;text-transform:uppercase;font-weight:600;" title="Deep Drill-Down Performed">Drill</th>
                                <th style="padding:8px 8px;text-align:center;color:#8b949e;font-size:10px;text-transform:uppercase;font-weight:600;" title="AI Investigation (auto-executed diagnostics)">AI</th>
                                <th style="padding:8px 8px;text-align:center;color:#8b949e;font-size:10px;text-transform:uppercase;font-weight:600;" title="Fix Instructions Provided">Fix</th>
                                <th style="padding:8px 8px;text-align:center;color:#8b949e;font-size:10px;text-transform:uppercase;font-weight:600;" title="Red Hat Documentation Linked">Docs</th>
                            </tr>
                        </thead>
                        <tbody>
                            {"".join(exec_rows)}
                        </tbody>
                    </table>
                </div>
            </div>
        '''

    for title, data in grouped.items():
        issue = data["issue"]
        failures = data["failures"]
        raw_outputs = data["raw_outputs"]
        jira_keys = issue.get("jira", [])
        verify_cmd = issue.get("verify_cmd", "")
        
        # Build Jira links with status badges
        jira_html_parts = []
        for jira_key in jira_keys:
            if jira_key in bug_status_info:
                bug_info = bug_status_info[jira_key]
                status = bug_info.get('status', 'Unknown')
                assessment = bug_info.get('assessment', 'unknown')
                detail = bug_info.get('assessment_detail', '')
                
                # Color based on assessment
                if assessment == 'open':
                    badge_color = "#F2495C"
                    badge_bg = "rgba(242,73,92,0.2)"
                elif assessment == 'regression':
                    badge_color = "#FF9830"
                    badge_bg = "rgba(255,152,48,0.2)"
                elif assessment in ['fixed', 'fixed_newer']:
                    badge_color = "#73BF69"
                    badge_bg = "rgba(115,191,105,0.2)"
                else:
                    badge_color = "#8b949e"
                    badge_bg = "rgba(139,148,158,0.2)"
                
                jira_html_parts.append(
                    f'<div style="display:inline-flex;align-items:center;gap:6px;margin:2px 0;">'
                    f'<a href="https://issues.redhat.com/browse/{jira_key}" style="color:#5794F2;" target="_blank">{jira_key}</a>'
                    f'<span style="background:{badge_bg};color:{badge_color};padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600;">{status}</span>'
                    f'</div>'
                )
            else:
                jira_html_parts.append(f'<a href="https://issues.redhat.com/browse/{jira_key}" style="color:#5794F2;" target="_blank">{jira_key}</a>')
        
        jira_links_html = "<br>".join(jira_html_parts) if jira_html_parts else "N/A"
        
        # Color code by severity (based on number of affected items)
        border_color = "#F2495C" if len(failures) > 3 else "#FF9830" if len(failures) > 1 else "#FADE2A"
        
        html += f'''
            <div style="background:var(--bg-secondary);border-radius:8px;padding:20px;margin-bottom:16px;border-left:4px solid {border_color};">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                    <span style="font-weight:600;color:#fff;font-size:16px;">⚠️ {issue["title"]}</span>
                    <span style="background:var(--bg-canvas);padding:4px 12px;border-radius:12px;color:#F2495C;font-size:12px;font-weight:600;">{len(failures)} affected</span>
                </div>
                
                <div style="background:var(--bg-canvas);border-radius:6px;padding:12px;margin-bottom:15px;">
                    <div style="color:var(--text-secondary);font-size:11px;margin-bottom:6px;text-transform:uppercase;">Affected Resources:</div>
                    <div style="display:flex;flex-wrap:wrap;gap:6px;">
        '''
        
        for f in failures[:6]:
            html += f'<span style="background:var(--bg-primary);padding:4px 8px;border-radius:4px;font-size:11px;color:#c9d1d9;font-family:monospace;">{f["name"]}</span>'
        
        if len(failures) > 6:
            html += f'<span style="color:var(--text-secondary);font-size:11px;padding:4px;">+{len(failures)-6} more</span>'
        
        html += '''
                    </div>
                </div>
        '''
        
        # Add VERIFY ON SERVER section with command and output
        if verify_cmd or raw_outputs:
            html += f'''
                <div style="background:#0a0e14;border:1px solid #30363d;border-radius:6px;margin-bottom:15px;overflow:hidden;">
                    <div style="background:#161b22;padding:10px 14px;border-bottom:1px solid #30363d;display:flex;align-items:center;gap:8px;">
                        <span style="color:#73BF69;font-size:12px;">▶</span>
                        <span style="color:#8b949e;font-size:11px;text-transform:uppercase;font-weight:600;">How to verify on server:</span>
                    </div>
            '''
            
            if verify_cmd:
                html += f'''
                    <div style="padding:12px 14px;border-bottom:1px solid #21262d;">
                        <div style="color:#58a6ff;font-size:11px;margin-bottom:6px;">COMMAND:</div>
                        <code style="display:block;background:#0d1117;padding:10px 12px;border-radius:4px;font-family:'JetBrains Mono',Monaco,monospace;font-size:12px;color:#e6edf3;white-space:pre-wrap;word-break:break-all;">$ {verify_cmd}</code>
                    </div>
                '''
            
            if raw_outputs:
                # Combine and limit raw outputs, escape HTML
                combined_output = raw_outputs[0] if raw_outputs else "(no output)"
                # Escape HTML special characters
                combined_output = str(combined_output).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                html += f'''
                    <div style="padding:12px 14px;">
                        <div style="color:#f85149;font-size:11px;margin-bottom:6px;">OUTPUT (detected issues):</div>
                        <pre style="background:#0d1117;padding:10px 12px;border-radius:4px;font-family:'JetBrains Mono',Monaco,monospace;font-size:11px;color:#f85149;white-space:pre-wrap;word-break:break-all;margin:0;max-height:250px;overflow-y:auto;">{combined_output}</pre>
                    </div>
                '''
            
            html += '''
                </div>
            '''
        
        html += f'''
                <div style="color:var(--text-secondary);font-size:13px;margin-bottom:15px;">
                    {issue["description"]}
                </div>
                
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:15px;">
                    <div>
                        <div style="color:#F2495C;font-weight:600;font-size:12px;margin-bottom:8px;">🎯 ROOT CAUSES</div>
                        <ul style="color:#c9d1d9;font-size:12px;margin-left:16px;line-height:1.6;">
        '''
        for cause in issue.get("root_cause", [])[:3]:
            html += f'<li>{cause}</li>'
        
        html += f'''
                        </ul>
                    </div>
                    <div>
                        <div style="color:#73BF69;font-weight:600;font-size:12px;margin-bottom:8px;">💡 REMEDIATION</div>
                        <ul style="color:#c9d1d9;font-size:12px;margin-left:16px;line-height:1.6;">
        '''
        for suggestion in issue.get("suggestions", [])[:3]:
            html += f'<li><code style="background:var(--bg-canvas);padding:1px 4px;border-radius:3px;font-size:11px;">{suggestion}</code></li>'
        
        # Build bug assessment section
        bug_assessment_html = ""
        for jira_key in jira_keys:
            if jira_key in bug_status_info:
                bug_info = bug_status_info[jira_key]
                detail = bug_info.get('assessment_detail', '')
                if detail:
                    bug_assessment_html += f'<div style="font-size:11px;color:#c9d1d9;margin-top:4px;">{detail}</div>'
        
        html += f'''
                        </ul>
                    </div>
                </div>
                
                <div style="margin-top:15px;padding:12px;background:#0d1117;border-radius:6px;">
                    <div style="color:#5794F2;font-weight:600;font-size:12px;margin-bottom:8px;">🐛 RELATED JIRA BUGS (vs {cluster_version})</div>
                    <div style="margin-bottom:8px;">
                        {jira_links_html}
                    </div>
                    {bug_assessment_html}
                </div>
        '''
        
        # Add INVESTIGATION section with determined root cause (only for full RCA)
        if show_investigation:
            investigations = data.get("investigations", [])
            determined_causes = data.get("determined_causes", [])
        else:
            investigations = []
            determined_causes = []
        
        if determined_causes:
            # Show the determined root cause prominently
            best_cause = determined_causes[0]  # Use first (usually most relevant)
            confidence_color = "#73BF69" if best_cause["confidence"] == "high" else "#FF9830" if best_cause["confidence"] == "medium" else "#8b949e"
            inv_id = best_cause.get("investigation_id", "inv")
            
            html += f'''
                <div style="margin-top:15px;padding:16px;background:linear-gradient(135deg, #1a2332 0%, #0d1117 100%);border:1px solid #30363d;border-radius:8px;">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                        <div style="color:#B877D9;font-weight:600;font-size:13px;">🔬 INVESTIGATED ROOT CAUSE</div>
                        <span style="background:{confidence_color}22;color:{confidence_color};padding:3px 10px;border-radius:10px;font-size:10px;font-weight:600;text-transform:uppercase;">{best_cause["confidence"]} confidence</span>
                    </div>
                    <div style="background:#161b22;border-left:3px solid {confidence_color};padding:12px 16px;border-radius:4px;margin-bottom:12px;">
                        <div style="color:#fff;font-size:15px;font-weight:600;margin-bottom:4px;">🎯 {best_cause["cause"]}</div>
                        <div style="color:#8b949e;font-size:12px;">{best_cause["explanation"]}</div>
                    </div>
                    <details style="margin-top:10px;">
                        <summary style="cursor:pointer;color:#58a6ff;font-size:13px;font-weight:600;padding:8px 0;">
                            📋 Detailed Investigation ({len(investigations)} diagnostic commands executed)
                        </summary>
                        <div id="inv-{inv_id}" style="margin-top:12px;max-height:800px;overflow-y:auto;">
            '''
            
            # Add investigation details for ALL issues
            for inv in investigations:
                failure_name = inv.get("failure_name", "")
                results = inv.get("results", [])
                
                html += f'''
                            <div style="margin-bottom:16px;background:#0d1117;border-radius:6px;padding:12px;">
                                <div style="color:#8b949e;font-size:11px;margin-bottom:10px;border-bottom:1px solid #21262d;padding-bottom:8px;">
                                    Investigation for: <span style="color:#c9d1d9;font-family:monospace;">{failure_name}</span>
                                </div>
                '''
                
                for r in results:
                    desc = r.get("description", "")
                    cmd = r.get("command", "")
                    output = r.get("output", "")
                    # Escape HTML
                    output_escaped = str(output).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")[:1500]
                    # Skip empty / no-output results to reduce noise
                    if output_escaped.strip() in ("(no output)", "(error: )", ""):
                        continue
                    
                    html += f'''
                                <div style="margin-bottom:12px;">
                                    <div style="color:#58a6ff;font-size:12px;font-weight:600;margin-bottom:4px;">📌 {desc}</div>
                                    <code style="display:block;background:#161b22;padding:6px 10px;border-radius:4px;font-size:11px;color:#8b949e;margin-bottom:4px;word-break:break-all;">$ {cmd}</code>
                                    <pre style="background:#0a0e14;padding:10px 12px;border-radius:4px;font-size:11px;color:#e6edf3;margin:0;white-space:pre-wrap;word-break:break-all;max-height:200px;overflow-y:auto;line-height:1.5;">{output_escaped}</pre>
                                </div>
                    '''
                
                html += '''
                            </div>
                '''
            
            html += '''
                        </div>
                    </details>
            '''
            
            drilldown = data.get("drilldown")
            if drilldown and drilldown.get("results"):
                dd_results = drilldown["results"]
                dd_conclusion = drilldown.get("conclusion")
                
                html += f'''
                    <details style="margin-top:12px;">
                        <summary style="cursor:pointer;color:#B877D9;font-size:13px;font-weight:600;padding:8px 0;">
                            🔬 Deep Drill-Down ({len(dd_results)} additional diagnostic commands)
                        </summary>
                        <div style="margin-top:12px;max-height:800px;overflow-y:auto;">
                '''
                
                if dd_conclusion:
                    cc = "#73BF69" if dd_conclusion["confidence"] == "high" else "#FF9830"
                    html += f'''
                            <div style="background:#0a1a0a;border:1px solid {cc};border-radius:6px;padding:14px;margin-bottom:14px;">
                                <div style="color:{cc};font-weight:700;font-size:14px;margin-bottom:6px;">✅ Root Cause Identified</div>
                                <div style="color:#e6edf3;font-size:13px;margin-bottom:8px;">{dd_conclusion["conclusion"]}</div>
                    '''
                    if dd_conclusion.get("fix"):
                        fix_escaped = str(dd_conclusion["fix"]).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        html += f'''
                                <div style="color:#58a6ff;font-size:12px;margin-bottom:4px;">🔧 <strong>How to fix:</strong></div>
                                <div style="color:#c9d1d9;font-size:12px;padding:8px 12px;background:#161b22;border-radius:4px;">{fix_escaped}</div>
                        '''
                    if dd_conclusion.get("doc"):
                        html += f'''
                                <div style="margin-top:8px;">
                                    <a href="{dd_conclusion["doc"]}" target="_blank" style="color:#58a6ff;font-size:11px;">📖 Red Hat Documentation →</a>
                                </div>
                        '''
                    html += '''
                            </div>
                    '''
                
                for r in dd_results:
                    desc = r.get("description", "")
                    cmd = r.get("command", "")
                    output = r.get("output", "")
                    output_escaped = str(output).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")[:2000]
                    if output_escaped.strip() in ("(no output)", "(error: )", ""):
                        continue
                    html += f'''
                            <div style="margin-bottom:12px;">
                                <div style="color:#B877D9;font-size:12px;font-weight:600;margin-bottom:4px;">📌 {desc}</div>
                                <code style="display:block;background:#161b22;padding:6px 10px;border-radius:4px;font-size:11px;color:#8b949e;margin-bottom:4px;word-break:break-all;">$ {cmd}</code>
                                <pre style="background:#0a0e14;padding:10px 12px;border-radius:4px;font-size:11px;color:#e6edf3;margin:0;white-space:pre-wrap;word-break:break-all;max-height:200px;overflow-y:auto;line-height:1.5;">{output_escaped}</pre>
                            </div>
                    '''
                
                html += '''
                        </div>
                    </details>
                '''
            
            followup = data.get("followup")
            if followup and followup.get("results"):
                fu_results = followup["results"]
                fu_conclusion = followup.get("conclusion")
                
                html += f'''
                    <details style="margin-top:12px;">
                        <summary style="cursor:pointer;color:#39D353;font-size:13px;font-weight:600;padding:8px 0;">
                            🤖 AI Deep Investigation ({len(fu_results)} diagnostic checks auto-executed)
                        </summary>
                        <div style="margin-top:12px;max-height:800px;overflow-y:auto;">
                '''
                
                if fu_conclusion:
                    fcc = "#73BF69" if fu_conclusion["confidence"] == "high" else "#FF9830"
                    html += f'''
                            <div style="background:#0a1a0a;border:1px solid {fcc};border-radius:6px;padding:14px;margin-bottom:14px;">
                                <div style="color:{fcc};font-weight:700;font-size:14px;margin-bottom:6px;">🔒 AI-Verified Root Cause</div>
                                <div style="color:#e6edf3;font-size:13px;margin-bottom:8px;">{fu_conclusion["conclusion"]}</div>
                    '''
                    if fu_conclusion.get("fix"):
                        fix_esc = str(fu_conclusion["fix"]).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        html += f'''
                                <div style="color:#58a6ff;font-size:12px;margin-bottom:4px;">🔧 <strong>How to fix:</strong></div>
                                <div style="color:#c9d1d9;font-size:12px;padding:8px 12px;background:#161b22;border-radius:4px;">{fix_esc}</div>
                        '''
                    if fu_conclusion.get("needs_manual"):
                        manual_esc = str(fu_conclusion["needs_manual"]).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                        html += f'''
                                <div style="margin-top:8px;color:#FF9830;font-size:12px;">
                                    ⚠️ <strong>Manual steps needed:</strong> {manual_esc}
                                </div>
                        '''
                    if fu_conclusion.get("doc"):
                        html += f'''
                                <div style="margin-top:8px;">
                                    <a href="{fu_conclusion["doc"]}" target="_blank" style="color:#58a6ff;font-size:11px;">📖 Red Hat Documentation →</a>
                                </div>
                        '''
                    html += '''
                            </div>
                    '''
                
                for r in fu_results:
                    desc = r.get("description", "")
                    cmd = r.get("command", "")
                    output = r.get("output", "")
                    output_escaped = str(output).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")[:3000]
                    if output_escaped.strip() in ("(no output)", "(error: )", ""):
                        continue
                    html += f'''
                            <div style="margin-bottom:12px;">
                                <div style="color:#39D353;font-size:12px;font-weight:600;margin-bottom:4px;">🤖 {desc}</div>
                                <code style="display:block;background:#161b22;padding:6px 10px;border-radius:4px;font-size:11px;color:#8b949e;margin-bottom:4px;word-break:break-all;">$ {cmd}</code>
                                <pre style="background:#0a0e14;padding:10px 12px;border-radius:4px;font-size:11px;color:#e6edf3;margin:0;white-space:pre-wrap;word-break:break-all;max-height:200px;overflow-y:auto;line-height:1.5;">{output_escaped}</pre>
                            </div>
                    '''
                
                html += '''
                        </div>
                    </details>
                '''
            
            next_steps = best_cause.get("next_steps", [])
            doc_url = best_cause.get("doc_url", "")
            if next_steps or doc_url:
                html += '''
                    <div style="margin-top:14px;padding:14px;background:linear-gradient(135deg, #1a1a0a 0%, #0d1117 100%);border:1px solid #FF9830;border-radius:8px;">
                        <div style="color:#FF9830;font-weight:700;font-size:13px;margin-bottom:10px;">🧪 Recommended Next Steps (if cause still unclear)</div>
                        <ol style="color:#c9d1d9;font-size:12px;margin-left:18px;line-height:1.8;">
                '''
                for step in next_steps:
                    step_escaped = str(step).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    if "http" in step:
                        import re as _re
                        step_escaped = _re.sub(
                            r'(https?://[^\s,)]+)',
                            r'<a href="\1" target="_blank" style="color:#58a6ff;">\1</a>',
                            step_escaped
                        )
                    html += f'<li>{step_escaped}</li>'
                html += '''
                        </ol>
                '''
                if doc_url:
                    html += f'''
                        <div style="margin-top:8px;">
                            <a href="{doc_url}" target="_blank" style="color:#58a6ff;font-size:12px;">📖 Red Hat Knowledge Base Article →</a>
                        </div>
                    '''
                html += '''
                    </div>
                '''
            
            html += '''
                </div>
            '''
        
        html += '''
            </div>
        '''
    
    html += '''
        </div>
    </div>
    '''
    return html

def escape_html(text):
    """Escape HTML special characters"""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

class SSHConnectionError(Exception):
    """Raised when SSH connection to the target host fails."""
    def __init__(self, message, host=None, user=None, key_path=None, original_error=None):
        self.host = host
        self.user = user
        self.key_path = key_path
        self.original_error = original_error
        super().__init__(message)


def get_ssh_client():
    """
    Get or create SSH client.
    Connects directly to the target host that has oc access.
    Raises SSHConnectionError with detailed info on failure.
    Thread-safe: uses lock to prevent duplicate connection setup.
    """
    global ssh_client

    with _ssh_lock:
        if ssh_client is not None:
            transport = ssh_client.get_transport()
            if transport and transport.is_active():
                return ssh_client
            ssh_client = None

        if not HOST:
            raise SSHConnectionError(
                "No target host configured. Set RH_LAB_HOST environment variable or pass --server <host>.",
                host=HOST, user=USER, key_path=KEY_PATH,
            )
        if not KEY_PATH:
            raise SSHConnectionError(
                "No SSH key path configured. Set SSH_KEY_PATH environment variable.",
                host=HOST, user=USER, key_path=KEY_PATH,
            )
        if not os.path.isfile(KEY_PATH):
            raise SSHConnectionError(
                f"SSH key file not found: {KEY_PATH}",
                host=HOST, user=USER, key_path=KEY_PATH,
            )

        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh_client.connect(HOST, username=USER, key_filename=KEY_PATH, timeout=10)
        except paramiko.AuthenticationException as e:
            ssh_client = None
            raise SSHConnectionError(
                f"SSH authentication failed for {USER}@{HOST} (key: {KEY_PATH}): {e}",
                host=HOST, user=USER, key_path=KEY_PATH, original_error=e,
            )
        except paramiko.SSHException as e:
            ssh_client = None
            raise SSHConnectionError(
                f"SSH protocol error connecting to {USER}@{HOST}: {e}",
                host=HOST, user=USER, key_path=KEY_PATH, original_error=e,
            )
        except OSError as e:
            ssh_client = None
            raise SSHConnectionError(
                f"Cannot connect to {HOST} -- host unreachable or connection refused: {e}",
                host=HOST, user=USER, key_path=KEY_PATH, original_error=e,
            )
        except Exception as e:
            ssh_client = None
            raise SSHConnectionError(
                f"SSH connection failed to {USER}@{HOST} (key: {KEY_PATH}): {e}",
                host=HOST, user=USER, key_path=KEY_PATH, original_error=e,
            )

        return ssh_client

def ssh_command(command, timeout=30):
    """Execute command via SSH. Raises SSHConnectionError if connection fails."""
    full_cmd = f"export KUBECONFIG={KUBECONFIG} && {command}"
    try:
        client = get_ssh_client()
        stdin, stdout, stderr = client.exec_command(full_cmd, timeout=timeout)
        channel = stdout.channel
        channel.settimeout(timeout)
        output = stdout.read().decode().strip()
        return output
    except SSHConnectionError:
        raise
    except Exception:
        return ""

def collect_data():
    """Collect all cluster health data. Raises SSHConnectionError if cannot connect."""
    import sys
    
    def log(msg):
        print(f"  {msg}", flush=True)
    
    log("📊 Starting data collection...")
    
    # ── Validate SSH connection upfront ──
    log("  → Verifying SSH connection to host...")
    try:
        client = get_ssh_client()
    except SSHConnectionError:
        raise  # Propagate with full details
    
    # Quick smoke test — verify oc is reachable (capture stderr for diagnostics)
    log("  → Verifying oc CLI access...")
    diag_cmd = (
        f"export KUBECONFIG={KUBECONFIG}; "
        "echo \"KUBECONFIG=$KUBECONFIG\"; "
        "echo \"KUBECONFIG_EXISTS=$(test -f $KUBECONFIG && echo yes || echo no)\"; "
        "echo \"OC_PATH=$(which oc 2>/dev/null || echo NOT_FOUND)\"; "
        "OC_OUT=$(oc whoami 2>&1); OC_RC=$?; "
        "echo \"OC_RC=$OC_RC\"; "
        "echo \"OC_OUT=$OC_OUT\""
    )
    # Run raw (without ssh_command's KUBECONFIG prefix) to control the flow
    try:
        raw_client = get_ssh_client()
        stdin, stdout, stderr = raw_client.exec_command(diag_cmd, timeout=15)
        diag_output = stdout.read().decode().strip()
    except SSHConnectionError:
        raise
    except Exception as e:
        diag_output = f"Failed to run diagnostics: {e}"

    # Parse diagnostic output
    diag = {}
    for line in diag_output.split('\n'):
        if '=' in line:
            key, _, val = line.partition('=')
            diag[key.strip()] = val.strip()

    oc_rc = diag.get('OC_RC', '1')
    oc_out = diag.get('OC_OUT', '')
    oc_path = diag.get('OC_PATH', 'NOT_FOUND')
    kc_exists = diag.get('KUBECONFIG_EXISTS', 'no')

    if oc_rc != '0' or not oc_out or oc_out == 'NOT_FOUND':
        # Check if this is an auth/login issue that we can auto-fix
        is_auth_issue = any(kw in oc_out.lower() for kw in [
            'unauthorized', 'must be logged in', 'token', 'forbidden',
            'certificate has expired', 'certificate is not yet valid',
        ]) if oc_out else False

        if is_auth_issue and oc_path != 'NOT_FOUND' and kc_exists == 'yes':
            log(f"  ⚠ Auth expired: {oc_out}")
            log(f"  → Attempting auto-login with kubeadmin credentials...")
            # Derive paths from KUBECONFIG — kubeadmin-password is in the same dir
            kc_dir = '/'.join(KUBECONFIG.rsplit('/', 1)[:-1]) if '/' in KUBECONFIG else '.'
            login_cmd = (
                f"export KUBECONFIG={KUBECONFIG}; "
                f"PASS_FILE={kc_dir}/kubeadmin-password; "
                "if [ -f \"$PASS_FILE\" ]; then "
                "  oc login -u kubeadmin -p $(cat \"$PASS_FILE\") 2>&1; "
                "  echo \"LOGIN_RC=$?\"; "
                "  echo \"LOGIN_USER=$(oc whoami 2>&1)\"; "
                "else "
                "  echo \"LOGIN_RC=1\"; "
                "  echo \"LOGIN_USER=PASS_FILE_NOT_FOUND: $PASS_FILE\"; "
                "fi"
            )
            try:
                raw_client = get_ssh_client()
                stdin, stdout, stderr = raw_client.exec_command(login_cmd, timeout=20)
                login_output = stdout.read().decode().strip()
            except SSHConnectionError:
                raise
            except Exception as e:
                login_output = f"LOGIN_RC=1\nLOGIN_USER=auto-login failed: {e}"

            login_info = {}
            for line in login_output.split('\n'):
                if '=' in line:
                    key, _, val = line.partition('=')
                    login_info[key.strip()] = val.strip()

            login_rc = login_info.get('LOGIN_RC', '1')
            login_user = login_info.get('LOGIN_USER', '')

            if login_rc == '0' and login_user and 'PASS_FILE_NOT_FOUND' not in login_user:
                log(f"  ✓ Auto-login successful! Connected as: {login_user}")
            else:
                # Auto-login failed — report full details
                fail_reason = login_user or 'unknown error'
                log(f"  ✗ Auto-login failed: {fail_reason}")
                raise SSHConnectionError(
                    f"'oc' CLI check failed on {HOST}: {oc_out}\n"
                    f"  Auto-login attempted but failed: {fail_reason}\n"
                    f"  KUBECONFIG={KUBECONFIG} (exists: {kc_exists})\n"
                    f"  oc path: {oc_path}\n"
                    f"  Manually run: oc login -u kubeadmin -p $(cat {kc_dir}/kubeadmin-password)",
                    host=HOST, user=USER, key_path=KEY_PATH,
                )
        else:
            # Non-auth issue — fail with detailed error
            details = []
            if oc_path == 'NOT_FOUND':
                details.append("'oc' binary not found in PATH")
            if kc_exists == 'no':
                details.append(f"KUBECONFIG file not found: {KUBECONFIG}")
            if oc_out and oc_path != 'NOT_FOUND':
                details.append(f"oc error: {oc_out}")
            if not details:
                details.append("oc whoami returned empty output")

            detail_str = '; '.join(details)
            raise SSHConnectionError(
                f"'oc' CLI check failed on {HOST}: {detail_str}\n"
                f"  KUBECONFIG={KUBECONFIG} (exists: {kc_exists})\n"
                f"  oc path: {oc_path}\n"
                f"  Ensure the cluster API is reachable and the kubeconfig is valid.",
                host=HOST, user=USER, key_path=KEY_PATH,
            )
    else:
        log(f"  ✓ Connected as: {oc_out}")
    
    # Run optimized commands
    log("  → Checking nodes...")
    nodes_out = ssh_command("oc get nodes --no-headers", timeout=15)
    
    log("  → Checking cluster operators...")
    operators_out = ssh_command("oc get co --no-headers", timeout=15)
    
    log("  → Checking pod status...")
    pods_out = ssh_command(
        "oc get pods -A --no-headers --field-selector=status.phase!=Running,status.phase!=Succeeded 2>/dev/null",
        timeout=15
    )
    pod_count = ssh_command("oc get pods -A --no-headers 2>/dev/null | wc -l", timeout=15)
    
    log("  → Checking KubeVirt status...")
    kubevirt_out = ssh_command("oc get kubevirt -A --no-headers 2>/dev/null", timeout=10)
    vmi_out = ssh_command("oc get vmi -A --no-headers 2>/dev/null", timeout=10)
    
    log("  → Checking node resources...")
    top_out = ssh_command("oc adm top nodes --no-headers 2>/dev/null", timeout=15)
    
    log("  → Getting cluster version...")
    version_out = ssh_command("oc version 2>/dev/null | grep 'Server Version'", timeout=10)
    
    # NEW CHECKS based on common Jira bugs
    log("  → Checking etcd health...")
    etcd_out = ssh_command("oc get pods -n openshift-etcd -l app=etcd --no-headers 2>/dev/null", timeout=10)
    etcd_leader = ssh_command("oc rsh -n openshift-etcd -c etcdctl $(oc get pods -n openshift-etcd -l app=etcd -o name 2>/dev/null | head -1) etcdctl endpoint status --cluster -w table 2>/dev/null | grep -v 'ENDPOINT' | head -5", timeout=15)
    
    log("  → Checking certificates...")
    certs_out = ssh_command("oc get certificates -A --no-headers 2>/dev/null; oc get secret -A -o json 2>/dev/null | grep -o '\"notAfter\":\"[^\"]*\"' | head -10", timeout=15)
    
    log("  → Checking PVC status...")
    pvc_out = ssh_command("oc get pvc -A --no-headers 2>/dev/null | grep -v Bound | head -20", timeout=10)
    
    log("  → Checking VM migrations...")
    migrations_out = ssh_command("oc get vmim -A --no-headers 2>/dev/null | grep -v Succeeded | head -20", timeout=10)
    
    log("  → Checking alerts...")
    alerts_out = ssh_command("oc get prometheusrules -A --no-headers 2>/dev/null | wc -l; oc exec -n openshift-monitoring -c prometheus prometheus-k8s-0 -- curl -s 'http://localhost:9090/api/v1/alerts' 2>/dev/null | grep -o '\"alertname\":\"[^\"]*\"' | sort | uniq -c | sort -rn | head -10", timeout=20)
    
    log("  → Checking CSI drivers...")
    csi_out = ssh_command("oc get pods -A --no-headers 2>/dev/null | grep -E 'csi|driver' | grep -v Running", timeout=10)
    
    log("  → Checking OOM events...")
    oom_out = ssh_command("oc get events -A --field-selector reason=OOMKilled --no-headers 2>/dev/null | tail -10", timeout=10)
    
    log("  → Checking failed migrations...")
    failed_migrations = ssh_command("oc get vmim -A -o json 2>/dev/null | grep -E '\"phase\":\"Failed\"' | wc -l", timeout=10)
    
    # NEW CNV-SPECIFIC CHECKS based on Jira bugs
    log("  → Checking virt-handler pods...")
    virt_handler_out = ssh_command("oc get pods -n openshift-cnv -l kubevirt.io=virt-handler --no-headers 2>/dev/null", timeout=10)
    virt_handler_mem = ssh_command("oc adm top pods -n openshift-cnv -l kubevirt.io=virt-handler --no-headers 2>/dev/null", timeout=10)
    
    log("  → Checking virt-launcher pods...")
    virt_launcher_issues = ssh_command("oc get pods -A -l kubevirt.io=virt-launcher --no-headers 2>/dev/null | grep -v Running | head -10", timeout=10)
    
    log("  → Checking virt-controller/virt-api...")
    virt_ctrl_out = ssh_command("oc get pods -n openshift-cnv -l 'kubevirt.io in (virt-controller,virt-api)' --no-headers 2>/dev/null", timeout=10)
    
    log("  → Checking DataVolumes...")
    dv_stuck = ssh_command("oc get dv -A --no-headers 2>/dev/null | grep -vE 'Succeeded|PVCBound' | head -15", timeout=10)
    
    log("  → Checking VolumeSnapshots...")
    snapshots_out = ssh_command("oc get volumesnapshot -A --no-headers 2>/dev/null | grep -v 'true' | head -10", timeout=10)
    
    log("  → Checking cordoned nodes...")
    cordoned_nodes = ssh_command("oc get nodes --no-headers 2>/dev/null | grep SchedulingDisabled", timeout=10)
    vms_on_cordoned = ""
    if cordoned_nodes:
        cordoned_list = [line.split()[0] for line in cordoned_nodes.split('\n') if line]
        if cordoned_list:
            log("  → Checking VMs on cordoned nodes...")
            vms_on_cordoned = ssh_command(f"oc get vmi -A -o wide --no-headers 2>/dev/null | grep -E '{'|'.join(cordoned_list)}' | head -10", timeout=10)
    
    log("  → Checking stuck migrations...")
    stuck_migrations = ssh_command("oc get vmim -A --no-headers 2>/dev/null | grep Running", timeout=10)
    
    log("  → Checking HyperConverged status...")
    hco_status = ssh_command("oc get hyperconverged -n openshift-cnv kubevirt-hyperconverged -o jsonpath='{.status.conditions}' 2>/dev/null", timeout=10)
    
    log("✅ Data collection complete!")
    
    # Parse nodes
    nodes = {"healthy": [], "unhealthy": []}
    for line in nodes_out.split('\n'):
        if line:
            parts = line.split()
            if len(parts) >= 3:
                name, status, roles = parts[0], parts[1], parts[2]
                node_info = {"name": name, "status": status, "roles": roles}
                if status == "Ready":
                    nodes["healthy"].append(node_info)
                else:
                    nodes["unhealthy"].append(node_info)
    
    # Parse operators
    operators = {"healthy": [], "degraded": [], "unavailable": []}
    for line in operators_out.split('\n'):
        if line:
            parts = line.split()
            if len(parts) >= 5:
                name, available, degraded = parts[0], parts[2], parts[4]
                if available == "False":
                    operators["unavailable"].append(name)
                elif degraded == "True":
                    operators["degraded"].append(name)
                else:
                    operators["healthy"].append(name)
    
    # Parse pods
    pods = {"healthy": 0, "unhealthy": []}
    try:
        total = int(pod_count.strip()) if pod_count.strip().isdigit() else 0
    except:
        total = 0
    
    for line in pods_out.split('\n'):
        if line:
            parts = line.split()
            if len(parts) >= 4:
                ns, name, ready, status = parts[0], parts[1], parts[2], parts[3]
                restarts = parts[4] if len(parts) > 4 else "0"
                if status not in ["Completed", "Succeeded"]:
                    pods["unhealthy"].append({
                        "ns": ns, "name": name, "ready": ready, 
                        "status": status, "restarts": restarts
                    })
    pods["healthy"] = total - len(pods["unhealthy"])
    
    # Parse kubevirt
    kubevirt = {"installed": False, "status": None, "vms_running": 0, "failed_vmis": []}
    if kubevirt_out and "No resources" not in kubevirt_out:
        kubevirt["installed"] = True
        parts = kubevirt_out.split()
        kubevirt["status"] = parts[-1] if parts else "Unknown"
    
    for line in vmi_out.split('\n'):
        if line:
            parts = line.split()
            if len(parts) >= 4:
                if parts[3] in ["Failed", "Error"]:
                    kubevirt["failed_vmis"].append({"ns": parts[0], "name": parts[1], "status": parts[3]})
                elif parts[3] == "Running":
                    kubevirt["vms_running"] += 1
    
    # Parse resources
    resources = {"nodes": [], "high_cpu": [], "high_memory": []}
    for line in top_out.split('\n'):
        if line:
            parts = line.split()
            if len(parts) >= 5:
                name = parts[0]
                try:
                    cpu_cores = parts[1]
                    cpu_pct = int(parts[2].replace('%', ''))
                    mem_bytes = parts[3]
                    mem_pct = int(parts[4].replace('%', ''))
                    resources["nodes"].append({
                        "name": name, "cpu": cpu_pct, "memory": mem_pct,
                        "cpu_cores": cpu_cores, "mem_bytes": mem_bytes
                    })
                    if cpu_pct > 85:
                        resources["high_cpu"].append(f"{name}: {cpu_pct}%")
                    if mem_pct > 85:
                        resources["high_memory"].append(f"{name}: {mem_pct}%")
                except:
                    pass
    
    # Version
    version = version_out.split(':')[-1].strip() if version_out else "Unknown"
    
    # Parse NEW checks
    # etcd status
    etcd = {"healthy": 0, "unhealthy": [], "leader_info": etcd_leader.strip() if etcd_leader else ""}
    for line in etcd_out.split('\n'):
        if line:
            parts = line.split()
            if len(parts) >= 3:
                name, ready, status = parts[0], parts[1], parts[2]
                if status == "Running" and ready.split('/')[0] == ready.split('/')[1]:
                    etcd["healthy"] += 1
                else:
                    etcd["unhealthy"].append({"name": name, "status": status})
    
    # Pending PVCs
    pvcs = {"pending": []}
    for line in pvc_out.split('\n'):
        if line:
            parts = line.split()
            if len(parts) >= 4:
                pvcs["pending"].append({"ns": parts[0], "name": parts[1], "status": parts[2]})
    
    # VM Migrations (not succeeded)
    migrations = {"failed": [], "running": 0}
    try:
        migrations["failed_count"] = int(failed_migrations.strip()) if failed_migrations.strip().isdigit() else 0
    except:
        migrations["failed_count"] = 0
    for line in migrations_out.split('\n'):
        if line:
            parts = line.split()
            if len(parts) >= 4:
                phase = parts[3] if len(parts) > 3 else "Unknown"
                if phase == "Running":
                    migrations["running"] += 1
                elif phase not in ["Succeeded", "Running"]:
                    migrations["failed"].append({"ns": parts[0], "name": parts[1], "phase": phase})
    
    # OOM events
    oom_events = []
    for line in oom_out.split('\n'):
        if line and "OOMKilled" in line:
            parts = line.split()
            if len(parts) >= 5:
                oom_events.append({"ns": parts[0], "object": parts[4] if len(parts) > 4 else "unknown"})
    
    # CSI issues
    csi_issues = []
    for line in csi_out.split('\n'):
        if line:
            parts = line.split()
            if len(parts) >= 4:
                csi_issues.append({"ns": parts[0], "pod": parts[1], "status": parts[3]})
    
    # Parse CNV-specific checks
    # virt-handler
    virt_handler = {"healthy": 0, "unhealthy": [], "high_memory": []}
    for line in virt_handler_out.split('\n'):
        if line:
            parts = line.split()
            if len(parts) >= 3:
                name, ready, status = parts[0], parts[1], parts[2]
                restarts = parts[3] if len(parts) > 3 else "0"
                if status == "Running" and ready.split('/')[0] == ready.split('/')[1]:
                    virt_handler["healthy"] += 1
                else:
                    virt_handler["unhealthy"].append({"name": name, "status": status, "restarts": restarts})
    # Check memory
    for line in virt_handler_mem.split('\n'):
        if line:
            parts = line.split()
            if len(parts) >= 3:
                name, cpu, mem = parts[0], parts[1], parts[2]
                mem_mi = int(mem.replace('Mi', '').replace('Gi', '000')) if 'Mi' in mem or 'Gi' in mem else 0
                if mem_mi > 500:  # > 500Mi is concerning
                    virt_handler["high_memory"].append({"name": name, "memory": mem})
    
    # virt-launcher issues
    virt_launcher_bad = []
    for line in virt_launcher_issues.split('\n'):
        if line:
            parts = line.split()
            if len(parts) >= 4:
                virt_launcher_bad.append({"ns": parts[0], "pod": parts[1], "status": parts[3]})
    
    # virt-controller/api
    virt_ctrl = {"healthy": 0, "unhealthy": []}
    for line in virt_ctrl_out.split('\n'):
        if line:
            parts = line.split()
            if len(parts) >= 3:
                name, ready, status = parts[0], parts[1], parts[2]
                if status == "Running":
                    virt_ctrl["healthy"] += 1
                else:
                    virt_ctrl["unhealthy"].append({"name": name, "status": status})
    
    # DataVolumes stuck
    dv_issues = []
    for line in dv_stuck.split('\n'):
        if line:
            parts = line.split()
            if len(parts) >= 4:
                dv_issues.append({"ns": parts[0], "name": parts[1], "phase": parts[3] if len(parts) > 3 else "Unknown"})
    
    # VolumeSnapshots not ready
    snapshot_issues = []
    for line in snapshots_out.split('\n'):
        if line:
            parts = line.split()
            if len(parts) >= 3:
                snapshot_issues.append({"ns": parts[0], "name": parts[1]})
    
    # Cordoned nodes with VMs
    cordoned_vms = []
    if vms_on_cordoned:
        for line in vms_on_cordoned.split('\n'):
            if line:
                parts = line.split()
                if len(parts) >= 4:
                    cordoned_vms.append({"ns": parts[0], "vm": parts[1], "node": parts[4] if len(parts) > 4 else "unknown"})
    
    # Stuck migrations (running for too long)
    stuck_migs = []
    for line in stuck_migrations.split('\n'):
        if line:
            parts = line.split()
            if len(parts) >= 3:
                stuck_migs.append({"ns": parts[0], "name": parts[1]})
    
    # HCO status
    hco_healthy = "Available" in hco_status if hco_status else False
    
    # Run dynamically added checks from Jira analysis
    dynamic_check_results = {}
    if SUGGESTED_NEW_CHECKS:
        for check in SUGGESTED_NEW_CHECKS:
            check_name = check.get("name", "unknown")
            try:
                # Execute the check command
                if check_name == "etcd_latency":
                    result = ssh_command("oc exec -n openshift-etcd $(oc get pods -n openshift-etcd -l app=etcd -o name | head -1) -- etcdctl endpoint health --cluster -w json 2>/dev/null", timeout=15)
                elif check_name == "kubelet_health":
                    result = ssh_command("oc get nodes -o jsonpath='{range .items[*]}{.metadata.name} {.status.conditions[?(@.type==\"Ready\")].status}{\"\\n\"}{end}' 2>/dev/null", timeout=15)
                elif check_name == "cert_expiry":
                    result = ssh_command("oc get secret -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name} {.type}{\"\\n\"}{end}' 2>/dev/null | grep tls | head -10", timeout=15)
                elif check_name == "network_migration":
                    result = ssh_command("oc get network.operator cluster -o jsonpath='{.spec.migration}' 2>/dev/null", timeout=10)
                elif check_name == "catalog_source":
                    result = ssh_command("oc get catalogsource -n openshift-marketplace --no-headers 2>/dev/null", timeout=10)
                elif check_name == "router_health":
                    result = ssh_command("oc get pods -n openshift-ingress -l ingresscontroller.operator.openshift.io/deployment-ingresscontroller --no-headers 2>/dev/null", timeout=10)
                elif check_name == "image_pull":
                    result = ssh_command("oc get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded 2>/dev/null | grep -i imagepull | head -10", timeout=15)
                else:
                    result = ssh_command("echo 'Check not implemented'", timeout=5)
                
                # Parse result for issues
                issues_found = []
                if result:
                    # Simple issue detection
                    if "error" in result.lower() or "fail" in result.lower() or "false" in result.lower():
                        issues_found = [{"raw": result[:200]}]
                
                dynamic_check_results[check_name] = {
                    "raw_output": result[:500] if result else "",
                    "issues": issues_found,
                    "jira": check.get("jira", ""),
                    "description": check.get("description", "")
                }
            except Exception as e:
                dynamic_check_results[check_name] = {
                    "raw_output": f"Error: {str(e)}",
                    "issues": [],
                    "jira": check.get("jira", ""),
                    "description": check.get("description", "")
                }
    
    return {
        "nodes": nodes,
        "operators": operators,
        "pods": pods,
        "kubevirt": kubevirt,
        "resources": resources,
        "version": version,
        "cluster": HOST,
        "timestamp": datetime.now(),
        # New checks
        "etcd": etcd,
        "pvcs": pvcs,
        "migrations": migrations,
        "oom_events": oom_events,
        "csi_issues": csi_issues,
        # CNV-specific
        "virt_handler": virt_handler,
        "virt_launcher_bad": virt_launcher_bad,
        "virt_ctrl": virt_ctrl,
        "dv_issues": dv_issues,
        "snapshot_issues": snapshot_issues,
        "cordoned_vms": cordoned_vms,
        "stuck_migrations": stuck_migs,
        "hco_healthy": hco_healthy,
        # Dynamic checks from Jira
        "dynamic_checks": dynamic_check_results,
    }

def has_issues(data):
    """Check for any issues"""
    return (
        len(data["nodes"]["unhealthy"]) > 0 or
        len(data["operators"]["degraded"]) > 0 or
        len(data["operators"]["unavailable"]) > 0 or
        len(data["pods"]["unhealthy"]) > 0 or
        len(data["kubevirt"]["failed_vmis"]) > 0 or
        len(data["resources"]["high_cpu"]) > 0 or
        len(data["resources"]["high_memory"]) > 0 or
        # New checks
        len(data["etcd"]["unhealthy"]) > 0 or
        len(data["pvcs"]["pending"]) > 0 or
        len(data["migrations"]["failed"]) > 0 or
        data["migrations"]["failed_count"] > 0 or
        len(data["oom_events"]) > 0 or
        len(data["csi_issues"]) > 0 or
        # CNV-specific
        len(data["virt_handler"]["unhealthy"]) > 0 or
        len(data["virt_handler"]["high_memory"]) > 0 or
        len(data["virt_launcher_bad"]) > 0 or
        len(data["virt_ctrl"]["unhealthy"]) > 0 or
        len(data["dv_issues"]) > 0 or
        len(data["snapshot_issues"]) > 0 or
        len(data["cordoned_vms"]) > 0 or
        len(data["stuck_migrations"]) > 0
    )

def generate_error_report_html(ssh_error):
    """Generate an HTML error report when SSH connection fails."""
    from datetime import datetime as _dt
    ts = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
    host = escape_html(str(ssh_error.host or '(not set)'))
    user = escape_html(str(ssh_error.user or '(not set)'))
    key = escape_html(str(ssh_error.key_path or '(not set)'))
    error_msg = escape_html(str(ssh_error))
    orig = ''
    if ssh_error.original_error:
        orig = escape_html(f"{type(ssh_error.original_error).__name__}: {ssh_error.original_error}")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Health Check — Connection Error</title>
<style>
  :root {{ --bg:#1a1a2e; --card:#16213e; --red:#e74c3c; --yellow:#f39c12; --text:#e0e0e0; --muted:#888; }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:'Segoe UI',system-ui,-apple-system,sans-serif; background:var(--bg); color:var(--text); padding:20px; }}
  .container {{ max-width:800px; margin:0 auto; }}
  .header {{ text-align:center; padding:30px 0; }}
  .header h1 {{ color:var(--red); font-size:2em; margin-bottom:10px; }}
  .header .ts {{ color:var(--muted); font-size:0.9em; }}
  .error-card {{ background:var(--card); border:2px solid var(--red); border-radius:12px; padding:24px; margin:20px 0; }}
  .error-card h2 {{ color:var(--red); margin-bottom:16px; font-size:1.3em; }}
  .error-msg {{ background:#1a1a1a; border-radius:8px; padding:16px; font-family:monospace; color:#ff6b6b;
    white-space:pre-wrap; word-break:break-word; margin-bottom:16px; font-size:0.95em; }}
  .details {{ margin:16px 0; }}
  .details table {{ width:100%; border-collapse:collapse; }}
  .details td {{ padding:8px 12px; border-bottom:1px solid #333; }}
  .details td:first-child {{ color:var(--yellow); font-weight:600; width:100px; }}
  .details td:last-child {{ font-family:monospace; }}
  .troubleshoot {{ background:var(--card); border:1px solid #333; border-radius:12px; padding:24px; margin:20px 0; }}
  .troubleshoot h2 {{ color:var(--yellow); margin-bottom:16px; }}
  .troubleshoot ol {{ padding-left:20px; }}
  .troubleshoot li {{ margin:8px 0; line-height:1.6; }}
  .troubleshoot code {{ background:#1a1a1a; padding:2px 8px; border-radius:4px; font-size:0.9em; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>&#x274C; Connection Error</h1>
    <div class="ts">{ts}</div>
  </div>
  <div class="error-card">
    <h2>SSH Connection Failed</h2>
    <div class="error-msg">{error_msg}</div>
    <div class="details">
      <table>
        <tr><td>Host</td><td>{host}</td></tr>
        <tr><td>User</td><td>{user}</td></tr>
        <tr><td>SSH Key</td><td>{key}</td></tr>
        {"<tr><td>Detail</td><td>" + orig + "</td></tr>" if orig else ""}
      </table>
    </div>
  </div>
  <div class="troubleshoot">
    <h2>&#x1F527; Troubleshooting</h2>
    <ol>
      <li>Verify the host is reachable: <code>ssh {user}@{host}</code></li>
      <li>Check the SSH key exists and has correct permissions (<code>chmod 600</code>)</li>
      <li>Ensure <code>RH_LAB_HOST</code> and <code>SSH_KEY_PATH</code> environment variables are set correctly</li>
      <li>If using <code>--server</code>, double-check the hostname/IP is correct</li>
      <li>Verify the target host allows SSH key-based authentication</li>
      <li>Check firewall rules and network connectivity to port 22</li>
    </ol>
  </div>
</div>
</body>
</html>"""


def generate_html_report(data, include_rca=False, rca_level='none', ai_rca=False):
    """Generate Grafana-style HTML dashboard report
    
    rca_level can be:
    - 'none': No RCA, just health checks
    - 'bugs': Match failures to known bugs (no deep investigation)
    - 'full': Full RCA with deep investigation
    ai_rca: If True, run Gemini-powered AI analysis on the collected data
    """
    # Handle legacy include_rca parameter
    if include_rca and rca_level == 'none':
        rca_level = 'full'
    
    issues = has_issues(data)
    
    # Pattern matching runs whenever RCA or AI-RCA is requested.
    # It's fast and free -- Gemini builds on top of its findings.
    rca_html = ""
    email_rca_data = {}
    analysis = None
    need_patterns = (rca_level != 'none' or ai_rca) and issues

    if need_patterns:
        print(f"  🔬 Running pattern analysis...", flush=True)
        print(f"     → Matching failures to known issues database...", flush=True)
        analysis = analyze_failures(data)
        print(f"     → Found {len(analysis)} issue(s) to analyze", flush=True)

        if RCA_JIRA:
            print(f"     → Searching Jira for related bugs...", flush=True)

        if RCA_EMAIL:
            print(f"     → Searching emails for related discussions...", flush=True)
            email_rca_data = search_emails_for_issues(analysis)
            for item in analysis:
                if isinstance(item, dict):
                    item['email_searches'] = email_rca_data.get('keywords', [])

        if rca_level == 'full':
            print(f"     → Running deep investigation commands...", flush=True)
            analysis = run_deep_investigation(analysis, ssh_command)
            print(f"     → Deep investigation complete", flush=True)

        if rca_level != 'none':
            print(f"     → Generating RCA HTML section...", flush=True)
            rca_html = generate_rca_html(analysis, data.get("version", ""), show_investigation=(rca_level == 'full'), email_data=email_rca_data)
            print(f"  ✅ Rule-based RCA complete", flush=True)

    # Gemini AI RCA -- always receives the pattern findings
    ai_rca_html = ""
    if ai_rca and issues:
        print(f"  🤖 Running Gemini AI analysis (building on {len(analysis or [])} pattern findings)...", flush=True)
        try:
            try:
                from healthchecks.ai_analysis import analyze_with_gemini, generate_ai_rca_html, suggest_new_patterns, suggest_root_cause_rules
            except ImportError:
                from ai_analysis import analyze_with_gemini, generate_ai_rca_html, suggest_new_patterns, suggest_root_cause_rules
            ai_markdown = analyze_with_gemini(data, rule_analysis=analysis)
            if ai_markdown:
                ai_rca_html = generate_ai_rca_html(ai_markdown)
                print(f"  ✅ AI analysis complete", flush=True)
                # Gemini feedback loop: suggest new patterns + root cause rules
                try:
                    new_patterns = suggest_new_patterns(data, ai_markdown, rule_analysis=analysis)
                    if new_patterns:
                        print(f"  🧠 Gemini suggested {len(new_patterns)} new pattern(s) for the knowledge base", flush=True)
                    new_rc_rules = suggest_root_cause_rules(data, ai_markdown, rule_analysis=analysis)
                    if new_rc_rules:
                        print(f"  🧠 Gemini suggested {len(new_rc_rules)} new root cause rule(s)", flush=True)
                except Exception as exc:
                    print(f"  ⚠️  Pattern suggestion step failed (non-fatal): {exc}", flush=True)
            else:
                print(f"  ⚠️  AI analysis skipped (no API key or API error)", flush=True)
        except Exception as e:
            print(f"  ⚠️  AI analysis failed: {e}", flush=True)
    
    status_color = "#FF9830" if issues else "#73BF69"
    status_text = "ATTENTION NEEDED" if issues else "ALL SYSTEMS HEALTHY"
    
    # Calculate totals
    total_nodes = len(data['nodes']['healthy']) + len(data['nodes']['unhealthy'])
    healthy_nodes = len(data['nodes']['healthy'])
    total_ops = len(data['operators']['healthy']) + len(data['operators']['degraded']) + len(data['operators']['unavailable'])
    healthy_ops = len(data['operators']['healthy'])
    total_pods = data['pods']['healthy'] + len(data['pods']['unhealthy'])
    unhealthy_pods = len(data['pods']['unhealthy'])
    
    # Build health check cards
    def health_card(title, icon, status_ok, value, subtitle="", color_override=None):
        if color_override:
            color = color_override
        else:
            color = "#73BF69" if status_ok else "#F2495C"
        status_class = "ok" if status_ok else "error"
        return f'''
        <div class="panel stat-panel {status_class}">
            <div class="panel-title">{icon} {title}</div>
            <div class="stat-value" style="color:{color}">{value}</div>
            <div class="stat-subtitle">{subtitle}</div>
        </div>'''
    
    # Build gauge for percentage
    def gauge_panel(title, icon, value, max_val, unit=""):
        pct = (value / max_val * 100) if max_val > 0 else 0
        color = "#73BF69" if pct >= 90 else "#FF9830" if pct >= 70 else "#F2495C"
        return f'''
        <div class="panel gauge-panel">
            <div class="panel-title">{icon} {title}</div>
            <div class="gauge-container">
                <svg viewBox="0 0 120 70" class="gauge-svg">
                    <path d="M 10 60 A 50 50 0 0 1 110 60" fill="none" stroke="#2c3235" stroke-width="8" stroke-linecap="round"/>
                    <path d="M 10 60 A 50 50 0 0 1 110 60" fill="none" stroke="{color}" stroke-width="8" stroke-linecap="round" 
                          stroke-dasharray="{pct * 1.57} 157" class="gauge-fill"/>
                </svg>
                <div class="gauge-value" style="color:{color}">{value}<span class="gauge-max">/{max_val}</span></div>
            </div>
            <div class="gauge-label">{unit}</div>
        </div>'''
    
    # Group pods by namespace for issues panel
    pods_by_ns = {}
    for p in data["pods"]["unhealthy"]:
        pods_by_ns.setdefault(p["ns"], []).append(p)
    
    # Build issues list HTML
    issues_html = ""
    if pods_by_ns:
        for ns in sorted(pods_by_ns.keys())[:6]:
            issues_html += f'<div class="issue-ns">{ns}</div>'
            for pod in pods_by_ns[ns][:3]:
                issues_html += f'''<div class="issue-item">
                    <span class="issue-name">{pod["name"][:40]}</span>
                    <span class="issue-status">{pod["status"]}</span>
                </div>'''
            if len(pods_by_ns[ns]) > 3:
                issues_html += f'<div class="issue-more">+{len(pods_by_ns[ns])-3} more</div>'
    
    # Build resource usage bars
    resource_rows = ""
    for node in data["resources"]["nodes"][:12]:
        cpu_pct = node["cpu"]
        mem_pct = node["memory"]
        cpu_color = "#73BF69" if cpu_pct < 70 else "#FF9830" if cpu_pct < 85 else "#F2495C"
        mem_color = "#73BF69" if mem_pct < 70 else "#FF9830" if mem_pct < 85 else "#F2495C"
        resource_rows += f'''
        <div class="resource-row">
            <div class="resource-node-name">{node["name"][:25]}</div>
            <div class="resource-bar-wrap">
                <div class="resource-bar">
                    <div class="resource-bar-fill" style="width:{cpu_pct}%;background:{cpu_color}"></div>
                </div>
                <span class="resource-pct">{cpu_pct}%</span>
            </div>
            <div class="resource-bar-wrap">
                <div class="resource-bar">
                    <div class="resource-bar-fill" style="width:{mem_pct}%;background:{mem_color}"></div>
                </div>
                <span class="resource-pct">{mem_pct}%</span>
            </div>
        </div>'''
    
    html = f'''<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>CNV HealthCrew AI - {data["cluster"]}</title>
<style>
:root {{
    --bg-canvas: #111217;
    --bg-primary: #181b1f;
    --bg-secondary: #22252b;
    --border: #2c3235;
    --text-primary: #d8d9da;
    --text-secondary: #8e8e8e;
    --green: #73BF69;
    --yellow: #FF9830;
    --red: #F2495C;
    --blue: #5794F2;
    --purple: #B877D9;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg-canvas); color: var(--text-primary); min-height: 100vh; }}

/* Top Navigation */
.navbar {{ background: var(--bg-primary); border-bottom: 1px solid var(--border); padding: 0 24px; height: 52px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 100; }}
.navbar-brand {{ display: flex; align-items: center; gap: 12px; }}
.navbar-logo {{ width: 32px; height: 32px; background: linear-gradient(135deg, #FF6B35 0%, #F7931E 100%); border-radius: 8px; display: flex; align-items: center; justify-content: center; font-weight: 700; color: white; }}
.navbar-title {{ font-size: 18px; font-weight: 600; color: var(--text-primary); }}
.navbar-title span {{ color: var(--red); }}
.navbar-status {{ display: flex; align-items: center; gap: 8px; padding: 6px 16px; border-radius: 16px; font-size: 13px; font-weight: 500; background: {"rgba(242,73,92,0.15)" if issues else "rgba(115,191,105,0.15)"}; color: {status_color}; }}
.navbar-status::before {{ content: ''; width: 8px; height: 8px; border-radius: 50%; background: {status_color}; animation: pulse 2s infinite; }}
@keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.5; }} }}

/* Dashboard Container */
.dashboard {{ padding: 24px; max-width: 1800px; margin: 0 auto; }}

/* Dashboard Header */
.dash-header {{ margin-bottom: 24px; }}
.dash-header h1 {{ font-size: 24px; font-weight: 600; margin-bottom: 8px; }}
.dash-meta {{ display: flex; gap: 24px; color: var(--text-secondary); font-size: 13px; }}
.dash-meta span {{ display: flex; align-items: center; gap: 6px; }}

/* Grid Layout */
.grid {{ display: grid; gap: 16px; }}
.grid-4 {{ grid-template-columns: repeat(4, 1fr); }}
.grid-3 {{ grid-template-columns: repeat(3, 1fr); }}
.grid-2 {{ grid-template-columns: repeat(2, 1fr); }}
.grid-full {{ grid-template-columns: 1fr; }}
@media (max-width: 1400px) {{ .grid-4 {{ grid-template-columns: repeat(2, 1fr); }} }}
@media (max-width: 900px) {{ .grid-4, .grid-3, .grid-2 {{ grid-template-columns: 1fr; }} }}

/* Panel Base */
.panel {{ background: var(--bg-primary); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }}
.panel-title {{ font-size: 12px; font-weight: 500; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px; padding: 12px 16px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 8px; }}

/* Stat Panels */
.stat-panel {{ text-align: center; padding-bottom: 16px; }}
.stat-panel.ok {{ border-top: 3px solid var(--green); }}
.stat-panel.error {{ border-top: 3px solid var(--red); }}
.stat-panel.warn {{ border-top: 3px solid var(--yellow); }}
.stat-value {{ font-size: 42px; font-weight: 700; padding: 20px 16px 8px; font-variant-numeric: tabular-nums; }}
.stat-subtitle {{ font-size: 13px; color: var(--text-secondary); }}

/* Gauge Panels */
.gauge-panel {{ text-align: center; padding-bottom: 16px; }}
.gauge-container {{ position: relative; padding: 16px; }}
.gauge-svg {{ width: 120px; height: 70px; }}
.gauge-fill {{ transition: stroke-dasharray 0.5s ease; }}
.gauge-value {{ font-size: 28px; font-weight: 700; margin-top: -10px; }}
.gauge-max {{ font-size: 16px; color: var(--text-secondary); font-weight: 400; }}
.gauge-label {{ font-size: 12px; color: var(--text-secondary); margin-top: 4px; }}

/* Health Check Grid */
.check-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; padding: 16px; }}
.check-card {{ background: var(--bg-secondary); border-radius: 6px; padding: 0; display: flex; flex-direction: column; transition: background 0.2s; cursor: pointer; overflow: hidden; }}
.check-card:hover {{ background: #2a2d33; }}
.check-card-row {{ display: flex; align-items: center; gap: 12px; padding: 14px 16px; }}
.check-icon {{ font-size: 20px; }}
.check-info {{ flex: 1; min-width: 0; }}
.check-name {{ font-size: 13px; font-weight: 500; margin-bottom: 2px; }}
.check-result {{ font-size: 12px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.check-status {{ font-size: 18px; }}
.check-cmd {{ display: none; padding: 8px 16px 12px; border-top: 1px solid var(--border); }}
.check-cmd.show {{ display: block; }}
.check-cmd code {{ display: block; background: #1a1d23; color: #79c0ff; font-family: 'SF Mono', 'Consolas', 'Courier New', monospace; font-size: 11px; padding: 8px 10px; border-radius: 4px; white-space: pre-wrap; word-break: break-all; line-height: 1.5; }}
.check-cmd-label {{ font-size: 10px; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; font-weight: 600; }}
.check-validates {{ font-size: 11px; color: #8b949e; line-height: 1.5; margin-top: 6px; padding: 6px 8px; background: rgba(139,148,158,0.08); border-radius: 4px; border-left: 2px solid #3b82f6; }}
.check-validates-label {{ font-size: 9px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600; margin-bottom: 2px; }}
.check-expand {{ font-size: 10px; color: var(--text-secondary); margin-left: auto; transition: transform 0.2s; }}
.check-card.open .check-expand {{ transform: rotate(180deg); }}
.check-section-title {{ grid-column: 1 / -1; font-size: 11px; font-weight: 600; color: var(--blue); text-transform: uppercase; letter-spacing: 1px; padding: 8px 0 4px; border-bottom: 1px solid var(--border); margin-top: 8px; }}

/* Resource Usage */
.resource-header {{ display: grid; grid-template-columns: 200px 1fr 1fr; gap: 16px; padding: 8px 16px; font-size: 11px; font-weight: 600; color: var(--text-secondary); text-transform: uppercase; border-bottom: 1px solid var(--border); }}
.resource-body {{ max-height: 400px; overflow-y: auto; }}
.resource-row {{ display: grid; grid-template-columns: 200px 1fr 1fr; gap: 16px; padding: 10px 16px; border-bottom: 1px solid var(--bg-canvas); align-items: center; }}
.resource-row:last-child {{ border-bottom: none; }}
.resource-row:hover {{ background: var(--bg-secondary); }}
.resource-node-name {{ font-family: 'JetBrains Mono', Monaco, monospace; font-size: 12px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.resource-bar-wrap {{ display: flex; align-items: center; gap: 12px; }}
.resource-bar {{ flex: 1; height: 8px; background: var(--bg-canvas); border-radius: 4px; overflow: hidden; }}
.resource-bar-fill {{ height: 100%; border-radius: 4px; transition: width 0.3s; }}
.resource-pct {{ font-size: 12px; font-weight: 600; min-width: 45px; text-align: right; font-variant-numeric: tabular-nums; }}

/* Issues Panel */
.issues-body {{ padding: 16px; max-height: 350px; overflow-y: auto; }}
.issue-ns {{ font-size: 12px; font-weight: 600; color: var(--blue); padding: 8px 0 6px; border-bottom: 1px solid var(--border); margin-bottom: 8px; }}
.issue-item {{ display: flex; justify-content: space-between; align-items: center; padding: 8px 12px; background: var(--bg-secondary); border-radius: 4px; margin-bottom: 6px; font-size: 12px; }}
.issue-name {{ font-family: 'JetBrains Mono', Monaco, monospace; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 70%; }}
.issue-status {{ color: var(--red); font-weight: 500; white-space: nowrap; }}
.issue-more {{ font-size: 11px; color: var(--text-secondary); padding: 4px 0 8px; }}

/* RCA Panel styling */
.rca-panel {{ margin-top: 16px; }}

/* Footer */
.dash-footer {{ margin-top: 32px; padding: 24px; text-align: center; color: var(--text-secondary); font-size: 12px; border-top: 1px solid var(--border); }}
.dash-footer-status {{ font-size: 14px; font-weight: 600; color: {status_color}; margin-bottom: 8px; }}
</style>
</head>
<body>

<nav class="navbar">
    <div class="navbar-brand">
        <div class="navbar-logo">🏥</div>
        <div class="navbar-title">CNV <span>HealthCrew</span> AI</div>
    </div>
    <div class="navbar-status">{status_text}</div>
</nav>

<div class="dashboard">
    <div class="dash-header">
        <h1>{data["cluster"]}</h1>
        <div class="dash-meta">
            {"<span>🏠 Lab: " + LAB_NAME + "</span>" if LAB_NAME else ""}
            <span>📅 {data["timestamp"].strftime("%Y-%m-%d %H:%M:%S")}</span>
            <span>🏷️ Version {data["version"]}</span>
            <span>🔍 17 Health Checks</span>
        </div>
    </div>

    <!-- Main Stats Row -->
    <div class="grid grid-4" style="margin-bottom:16px;">
        {gauge_panel("Nodes", "🖥️", healthy_nodes, total_nodes, "Ready")}
        {gauge_panel("Operators", "⚙️", healthy_ops, total_ops, "Available")}
        {gauge_panel("Pods", "📦", data['pods']['healthy'], total_pods, "Running")}
        {gauge_panel("VMs", "💻", data['kubevirt']['vms_running'], data['kubevirt']['vms_running'] or 1, "Running")}
    </div>

    <!-- Secondary Stats Row -->
    <div class="grid grid-4" style="margin-bottom:16px;">
        {health_card("etcd Members", "🗄️", not data['etcd']['unhealthy'], data['etcd']['healthy'], "Healthy")}
        {health_card("PVCs Pending", "💾", not data['pvcs']['pending'], len(data['pvcs']['pending']), "", "#73BF69" if not data['pvcs']['pending'] else "#F2495C")}
        {health_card("OOM Events", "💥", not data['oom_events'], len(data['oom_events']), "Recent", "#73BF69" if not data['oom_events'] else "#F2495C")}
        {health_card("Migrations", "🔄", data['migrations']['failed_count'] == 0, data['migrations']['running'], "Running")}
    </div>

    <!-- Main Content Grid -->
    <div class="grid grid-2" style="margin-bottom:16px;">
        <!-- Resource Usage Panel -->
        <div class="panel">
            <div class="panel-title">📊 Node Resource Usage</div>
            <div class="resource-header">
                <div>Node</div>
                <div>CPU</div>
                <div>Memory</div>
            </div>
            <div class="resource-body">
                {resource_rows if resource_rows else '<div style="padding:40px;text-align:center;color:var(--text-secondary);">No resource data</div>'}
            </div>
        </div>

        <!-- Issues Panel -->
        <div class="panel">
            <div class="panel-title" style="color:var(--red);">⚠️ Unhealthy Pods ({unhealthy_pods})</div>
            <div class="issues-body">
                {issues_html if issues_html else '<div style="padding:40px;text-align:center;color:var(--green);">✅ All pods healthy</div>'}
            </div>
        </div>
    </div>

    <!-- Health Checks Panel -->
    <div class="panel" style="margin-bottom:16px;">
        <div class="panel-title">🧪 Health Check Results <span style="font-size:10px;color:var(--text-secondary);font-weight:400;margin-left:auto;">Click a check to see the command &amp; what it validates</span></div>
        <div class="check-grid">
            <div class="check-card">
                <div class="check-card-row">
                    <span class="check-icon">🖥️</span>
                    <div class="check-info">
                        <div class="check-name">Nodes</div>
                        <div class="check-result">{healthy_nodes}/{total_nodes} Ready</div>
                    </div>
                    <span class="check-status">{'✅' if not data['nodes']['unhealthy'] else '❌'}</span>
                    <span class="check-expand">▼</span>
                </div>
                <div class="check-cmd">
                    <div class="check-cmd-label">Validation command</div>
                    <code>oc get nodes --no-headers</code>
                    <div class="check-validates"><div class="check-validates-label">What it checks</div>All nodes must show 'Ready' status. Flags any node that is NotReady, SchedulingDisabled, or Unknown.</div>
                </div>
            </div>
            <div class="check-card">
                <div class="check-card-row">
                    <span class="check-icon">⚙️</span>
                    <div class="check-info">
                        <div class="check-name">Cluster Operators</div>
                        <div class="check-result">{healthy_ops}/{total_ops} Available</div>
                    </div>
                    <span class="check-status">{'✅' if not data['operators']['degraded'] and not data['operators']['unavailable'] else '❌'}</span>
                    <span class="check-expand">▼</span>
                </div>
                <div class="check-cmd">
                    <div class="check-cmd-label">Validation command</div>
                    <code>oc get co --no-headers</code>
                    <div class="check-validates"><div class="check-validates-label">What it checks</div>Every operator must have AVAILABLE=True and DEGRADED=False. Flags operators that are unavailable or degraded.</div>
                </div>
            </div>
            <div class="check-card">
                <div class="check-card-row">
                    <span class="check-icon">📦</span>
                    <div class="check-info">
                        <div class="check-name">Pods</div>
                        <div class="check-result">{data['pods']['healthy']} Running, {unhealthy_pods} Unhealthy</div>
                    </div>
                    <span class="check-status">{'✅' if not data['pods']['unhealthy'] else '❌'}</span>
                    <span class="check-expand">▼</span>
                </div>
                <div class="check-cmd">
                    <div class="check-cmd-label">Validation command</div>
                    <code>oc get pods -A --no-headers --field-selector=status.phase!=Running,status.phase!=Succeeded</code>
                    <div class="check-validates"><div class="check-validates-label">What it checks</div>Lists pods NOT in Running or Succeeded state (CrashLoopBackOff, Pending, Error, Unknown, ImagePullBackOff).</div>
                </div>
            </div>
            <div class="check-card">
                <div class="check-card-row">
                    <span class="check-icon">💻</span>
                    <div class="check-info">
                        <div class="check-name">KubeVirt</div>
                        <div class="check-result">{data['kubevirt']['status'] if data['kubevirt']['installed'] else 'Not installed'}, {data['kubevirt']['vms_running']} VMs</div>
                    </div>
                    <span class="check-status">{'✅' if data['kubevirt']['status'] == 'Deployed' and not data['kubevirt']['failed_vmis'] else '⚠️' if data['kubevirt']['installed'] else '➖'}</span>
                    <span class="check-expand">▼</span>
                </div>
                <div class="check-cmd">
                    <div class="check-cmd-label">Validation commands</div>
                    <code>oc get kubevirt -A --no-headers
oc get vmi -A --no-headers</code>
                    <div class="check-validates"><div class="check-validates-label">What it checks</div>KubeVirt CR must show 'Deployed' phase. Counts running VMs and identifies failed/stuck VMIs.</div>
                </div>
            </div>
            <div class="check-card">
                <div class="check-card-row">
                    <span class="check-icon">📊</span>
                    <div class="check-info">
                        <div class="check-name">Resource Usage</div>
                        <div class="check-result">{len(data['resources']['high_cpu'])} high CPU, {len(data['resources']['high_memory'])} high mem</div>
                    </div>
                    <span class="check-status">{'✅' if not data['resources']['high_cpu'] and not data['resources']['high_memory'] else '❌'}</span>
                    <span class="check-expand">▼</span>
                </div>
                <div class="check-cmd">
                    <div class="check-cmd-label">Validation command</div>
                    <code>oc adm top nodes --no-headers</code>
                    <div class="check-validates"><div class="check-validates-label">What it checks</div>Shows CPU/memory usage per node. Flags nodes above threshold (default: CPU >85%, Memory >80%).</div>
                </div>
            </div>
            <div class="check-card">
                <div class="check-card-row">
                    <span class="check-icon">🗄️</span>
                    <div class="check-info">
                        <div class="check-name">etcd Health</div>
                        <div class="check-result">{data['etcd']['healthy']} members healthy</div>
                    </div>
                    <span class="check-status">{'✅' if not data['etcd']['unhealthy'] else '❌'}</span>
                    <span class="check-expand">▼</span>
                </div>
                <div class="check-cmd">
                    <div class="check-cmd-label">Validation commands</div>
                    <code>oc get pods -n openshift-etcd -l app=etcd --no-headers
oc rsh -n openshift-etcd -c etcdctl &lt;etcd-pod&gt; etcdctl endpoint status --cluster -w table</code>
                    <div class="check-validates"><div class="check-validates-label">What it checks</div>All etcd member pods must be Running. Checks cluster-wide endpoint health, leader election, DB size, and raft index lag.</div>
                </div>
            </div>
            <div class="check-card">
                <div class="check-card-row">
                    <span class="check-icon">💾</span>
                    <div class="check-info">
                        <div class="check-name">PVC Status</div>
                        <div class="check-result">{len(data['pvcs']['pending'])} pending</div>
                    </div>
                    <span class="check-status">{'✅' if not data['pvcs']['pending'] else '❌'}</span>
                    <span class="check-expand">▼</span>
                </div>
                <div class="check-cmd">
                    <div class="check-cmd-label">Validation command</div>
                    <code>oc get pvc -A --no-headers | grep -v Bound</code>
                    <div class="check-validates"><div class="check-validates-label">What it checks</div>All PVCs should be Bound. Pending PVCs indicate storage provisioning failure or missing StorageClass.</div>
                </div>
            </div>
            <div class="check-card">
                <div class="check-card-row">
                    <span class="check-icon">🔄</span>
                    <div class="check-info">
                        <div class="check-name">VM Migrations</div>
                        <div class="check-result">{data['migrations']['running']} running, {len(data['migrations']['failed']) + data['migrations']['failed_count']} failed</div>
                    </div>
                    <span class="check-status">{'✅' if not data['migrations']['failed'] and data['migrations']['failed_count'] == 0 else '❌'}</span>
                    <span class="check-expand">▼</span>
                </div>
                <div class="check-cmd">
                    <div class="check-cmd-label">Validation commands</div>
                    <code>oc get vmim -A --no-headers | grep -v Succeeded
oc get vmim -A -o json | grep '"phase":"Failed"' | wc -l</code>
                    <div class="check-validates"><div class="check-validates-label">What it checks</div>Lists active/pending/failed migrations. Only 'Succeeded' is healthy. High failure count suggests underlying storage/network issues.</div>
                </div>
            </div>
            <div class="check-card">
                <div class="check-card-row">
                    <span class="check-icon">💥</span>
                    <div class="check-info">
                        <div class="check-name">OOM Events</div>
                        <div class="check-result">{len(data['oom_events'])} recent events</div>
                    </div>
                    <span class="check-status">{'✅' if not data['oom_events'] else '❌'}</span>
                    <span class="check-expand">▼</span>
                </div>
                <div class="check-cmd">
                    <div class="check-cmd-label">Validation command</div>
                    <code>oc get events -A --field-selector reason=OOMKilled --no-headers</code>
                    <div class="check-validates"><div class="check-validates-label">What it checks</div>Lists recent OOMKilled events across all namespaces. OOM events indicate pods running out of memory limits.</div>
                </div>
            </div>
            <div class="check-card">
                <div class="check-card-row">
                    <span class="check-icon">🔌</span>
                    <div class="check-info">
                        <div class="check-name">CSI Drivers</div>
                        <div class="check-result">{len(data['csi_issues'])} issues</div>
                    </div>
                    <span class="check-status">{'✅' if not data['csi_issues'] else '❌'}</span>
                    <span class="check-expand">▼</span>
                </div>
                <div class="check-cmd">
                    <div class="check-cmd-label">Validation command</div>
                    <code>oc get pods -A --no-headers | grep -E 'csi|driver' | grep -v Running</code>
                    <div class="check-validates"><div class="check-validates-label">What it checks</div>CSI driver pods must be Running. Down CSI drivers mean storage operations will fail.</div>
                </div>
            </div>
            
            <div class="check-section-title">CNV / KubeVirt Checks</div>
            
            <div class="check-card">
                <div class="check-card-row">
                    <span class="check-icon">🔧</span>
                    <div class="check-info">
                        <div class="check-name">virt-handler</div>
                        <div class="check-result">{data['virt_handler']['healthy']} healthy, {len(data['virt_handler']['high_memory'])} high mem</div>
                    </div>
                    <span class="check-status">{'✅' if not data['virt_handler']['unhealthy'] and not data['virt_handler']['high_memory'] else '❌'}</span>
                    <span class="check-expand">▼</span>
                </div>
                <div class="check-cmd">
                    <div class="check-cmd-label">Validation commands</div>
                    <code>oc get pods -n openshift-cnv -l kubevirt.io=virt-handler --no-headers
oc adm top pods -n openshift-cnv -l kubevirt.io=virt-handler --no-headers</code>
                    <div class="check-validates"><div class="check-validates-label">What it checks</div>All virt-handler DaemonSet pods must be Running. Checks memory/CPU usage -- high memory (>500Mi) indicates possible leak (CNV-66551).</div>
                </div>
            </div>
            <div class="check-card">
                <div class="check-card-row">
                    <span class="check-icon">🎛️</span>
                    <div class="check-info">
                        <div class="check-name">virt-controller/api</div>
                        <div class="check-result">{data['virt_ctrl']['healthy']} healthy</div>
                    </div>
                    <span class="check-status">{'✅' if not data['virt_ctrl']['unhealthy'] else '❌'}</span>
                    <span class="check-expand">▼</span>
                </div>
                <div class="check-cmd">
                    <div class="check-cmd-label">Validation command</div>
                    <code>oc get pods -n openshift-cnv -l 'kubevirt.io in (virt-controller,virt-api)' --no-headers</code>
                    <div class="check-validates"><div class="check-validates-label">What it checks</div>virt-controller and virt-api pods must be Running. These are the CNV control plane components.</div>
                </div>
            </div>
            <div class="check-card">
                <div class="check-card-row">
                    <span class="check-icon">🚀</span>
                    <div class="check-info">
                        <div class="check-name">virt-launcher</div>
                        <div class="check-result">{len(data['virt_launcher_bad'])} unhealthy</div>
                    </div>
                    <span class="check-status">{'✅' if not data['virt_launcher_bad'] else '❌'}</span>
                    <span class="check-expand">▼</span>
                </div>
                <div class="check-cmd">
                    <div class="check-cmd-label">Validation command</div>
                    <code>oc get pods -A -l kubevirt.io=virt-launcher --no-headers | grep -v Running</code>
                    <div class="check-validates"><div class="check-validates-label">What it checks</div>Finds virt-launcher pods not Running. Each VM has a launcher pod -- unhealthy launcher = VM problem.</div>
                </div>
            </div>
            <div class="check-card">
                <div class="check-card-row">
                    <span class="check-icon">💿</span>
                    <div class="check-info">
                        <div class="check-name">DataVolumes</div>
                        <div class="check-result">{len(data['dv_issues'])} stuck/pending</div>
                    </div>
                    <span class="check-status">{'✅' if not data['dv_issues'] else '❌'}</span>
                    <span class="check-expand">▼</span>
                </div>
                <div class="check-cmd">
                    <div class="check-cmd-label">Validation command</div>
                    <code>oc get dv -A --no-headers | grep -vE 'Succeeded|PVCBound'</code>
                    <div class="check-validates"><div class="check-validates-label">What it checks</div>DataVolumes should be Succeeded or PVCBound. Stuck DVs indicate import/clone failures.</div>
                </div>
            </div>
            <div class="check-card">
                <div class="check-card-row">
                    <span class="check-icon">📸</span>
                    <div class="check-info">
                        <div class="check-name">VolumeSnapshots</div>
                        <div class="check-result">{len(data['snapshot_issues'])} not ready</div>
                    </div>
                    <span class="check-status">{'✅' if not data['snapshot_issues'] else '❌'}</span>
                    <span class="check-expand">▼</span>
                </div>
                <div class="check-cmd">
                    <div class="check-cmd-label">Validation command</div>
                    <code>oc get volumesnapshot -A --no-headers | grep -v 'true'</code>
                    <div class="check-validates"><div class="check-validates-label">What it checks</div>Volume snapshots should show readyToUse=true. Unready snapshots indicate backup/clone problems.</div>
                </div>
            </div>
            <div class="check-card">
                <div class="check-card-row">
                    <span class="check-icon">🚧</span>
                    <div class="check-info">
                        <div class="check-name">Cordoned VMs</div>
                        <div class="check-result">{len(data['cordoned_vms'])} VMs at risk</div>
                    </div>
                    <span class="check-status">{'✅' if not data['cordoned_vms'] else '❌'}</span>
                    <span class="check-expand">▼</span>
                </div>
                <div class="check-cmd">
                    <div class="check-cmd-label">Validation commands</div>
                    <code>oc get nodes --no-headers | grep SchedulingDisabled
oc get vmi -A -o wide --no-headers | grep &lt;cordoned-node&gt;</code>
                    <div class="check-validates"><div class="check-validates-label">What it checks</div>Finds cordoned/drained nodes and identifies VMs running on them. These VMs are at risk during maintenance.</div>
                </div>
            </div>
            <div class="check-card">
                <div class="check-card-row">
                    <span class="check-icon">⏳</span>
                    <div class="check-info">
                        <div class="check-name">Stuck Migrations</div>
                        <div class="check-result">{len(data['stuck_migrations'])} running/stuck</div>
                    </div>
                    <span class="check-status">{'✅' if not data['stuck_migrations'] else '⚠️'}</span>
                    <span class="check-expand">▼</span>
                </div>
                <div class="check-cmd">
                    <div class="check-cmd-label">Validation command</div>
                    <code>oc get vmim -A --no-headers | grep Running</code>
                    <div class="check-validates"><div class="check-validates-label">What it checks</div>Finds migrations stuck in Running state. Long-running migrations may be hung due to network/storage issues.</div>
                </div>
            </div>
        </div>
    </div>

    {rca_html}

    {ai_rca_html}

    <div class="dash-footer">
        <div class="dash-footer-status">Cluster Status: {status_text}</div>
        <div>Generated by CNV HealthCrew AI | Based on real CNV/OCP Jira bugs</div>
    </div>
</div>

<script>
document.querySelectorAll('.check-card').forEach(function(card) {{
    card.addEventListener('click', function() {{
        var cmd = this.querySelector('.check-cmd');
        if (cmd) {{
            cmd.classList.toggle('show');
            this.classList.toggle('open');
        }}
    }});
}});
</script>

</body>
</html>'''
    return html

def print_console_report(data):
    """Print beautiful console report"""
    # ANSI colors
    G = '\033[92m'  # Green
    Y = '\033[93m'  # Yellow
    R = '\033[91m'  # Red
    B = '\033[94m'  # Blue
    C = '\033[96m'  # Cyan
    W = '\033[97m'  # White
    D = '\033[2m'   # Dim
    BD = '\033[1m'  # Bold
    X = '\033[0m'   # Reset
    
    issues = has_issues(data)
    w = 72
    
    print()
    print(f"{B}╔{'═'*w}╗{X}")
    print(f"{B}║{X}  {BD}{W}🏥 CNV HEALTHCREW AI - CLUSTER HEALTH REPORT{X}".ljust(w+20) + f"{B}║{X}")
    print(f"{B}╠{'═'*w}╣{X}")
    print(f"{B}║{X}  {D}Cluster:{X} {C}{data['cluster']}{X}".ljust(w+25) + f"{B}║{X}")
    print(f"{B}║{X}  {D}Version:{X} {data['version']}   {D}Time:{X} {data['timestamp'].strftime('%Y-%m-%d %H:%M:%S')}".ljust(w+15) + f"{B}║{X}")
    print(f"{B}╠{'═'*w}╣{X}")
    
    # Summary line function
    def summary_line(icon, label, ok, value):
        status = f"{G}✓{X}" if ok else f"{R}✗{X}"
        color = G if ok else Y
        print(f"{B}║{X}  {status}  {BD}{label.ljust(22)}{X} {color}{value}{X}".ljust(w+30) + f"{B}║{X}")
    
    # Nodes
    n_ok = len(data["nodes"]["unhealthy"]) == 0
    n_total = len(data["nodes"]["healthy"]) + len(data["nodes"]["unhealthy"])
    summary_line("🖥️", "Nodes", n_ok, f"{len(data['nodes']['healthy'])}/{n_total} Ready")
    
    # Operators
    o_bad = len(data["operators"]["degraded"]) + len(data["operators"]["unavailable"])
    o_total = len(data["operators"]["healthy"]) + o_bad
    summary_line("⚙️", "Cluster Operators", o_bad == 0, f"{len(data['operators']['healthy'])}/{o_total} Available")
    
    # Pods
    p_bad = len(data["pods"]["unhealthy"])
    p_total = data["pods"]["healthy"] + p_bad
    summary_line("📦", "Pods", p_bad == 0, f"{data['pods']['healthy']}/{p_total} Running" + (f" ({R}{p_bad} unhealthy{X})" if p_bad else ""))
    
    # KubeVirt
    if data["kubevirt"]["installed"]:
        kv_ok = data["kubevirt"]["status"] == "Deployed" and len(data["kubevirt"]["failed_vmis"]) == 0
        summary_line("💻", "KubeVirt", kv_ok, f"{data['kubevirt']['status']} ({data['kubevirt']['vms_running']} VMs)")
    
    # Resources
    r_bad = len(data["resources"]["high_cpu"]) + len(data["resources"]["high_memory"])
    summary_line("📊", "Resources", r_bad == 0, "Normal" if r_bad == 0 else f"{r_bad} nodes high usage")
    
    # etcd
    etcd_ok = len(data["etcd"]["unhealthy"]) == 0
    summary_line("🗄️", "etcd", etcd_ok, f"{data['etcd']['healthy']} members healthy" if etcd_ok else f"{len(data['etcd']['unhealthy'])} unhealthy")
    
    # PVCs
    pvc_bad = len(data["pvcs"]["pending"])
    summary_line("💾", "PVCs", pvc_bad == 0, "All Bound" if pvc_bad == 0 else f"{pvc_bad} Pending")
    
    # VM Migrations
    mig_bad = len(data["migrations"]["failed"]) + data["migrations"]["failed_count"]
    mig_run = data["migrations"]["running"]
    summary_line("🔄", "VM Migrations", mig_bad == 0, f"{mig_run} running" if mig_bad == 0 else f"{mig_bad} failed")
    
    # OOM Events
    oom_count = len(data["oom_events"])
    summary_line("💥", "OOM Events", oom_count == 0, "None" if oom_count == 0 else f"{oom_count} recent")
    
    # CSI Drivers
    csi_bad = len(data["csi_issues"])
    summary_line("🔌", "CSI Drivers", csi_bad == 0, "Healthy" if csi_bad == 0 else f"{csi_bad} issues")
    
    # CNV-specific checks
    if data["kubevirt"]["installed"]:
        print(f"{B}╠{'─'*w}╣{X}")
        print(f"{B}║{X}  {BD}{C}CNV/KubeVirt Checks:{X}".ljust(w+25) + f"{B}║{X}")
        
        # virt-handler
        vh_bad = len(data["virt_handler"]["unhealthy"]) + len(data["virt_handler"]["high_memory"])
        summary_line("🔧", "virt-handler", vh_bad == 0, f"{data['virt_handler']['healthy']} healthy" if vh_bad == 0 else f"{vh_bad} issues")
        
        # virt-controller/api
        vc_bad = len(data["virt_ctrl"]["unhealthy"])
        summary_line("🎛️", "virt-controller/api", vc_bad == 0, f"{data['virt_ctrl']['healthy']} healthy" if vc_bad == 0 else f"{vc_bad} unhealthy")
        
        # virt-launcher
        vl_bad = len(data["virt_launcher_bad"])
        summary_line("🚀", "virt-launcher pods", vl_bad == 0, "All healthy" if vl_bad == 0 else f"{vl_bad} issues")
        
        # DataVolumes
        dv_bad = len(data["dv_issues"])
        summary_line("💿", "DataVolumes", dv_bad == 0, "All ready" if dv_bad == 0 else f"{dv_bad} stuck")
        
        # Snapshots
        snap_bad = len(data["snapshot_issues"])
        summary_line("📸", "VolumeSnapshots", snap_bad == 0, "All ready" if snap_bad == 0 else f"{snap_bad} not ready")
        
        # Cordoned nodes with VMs
        cord_bad = len(data["cordoned_vms"])
        summary_line("🚧", "VMs on cordoned nodes", cord_bad == 0, "None" if cord_bad == 0 else f"{cord_bad} VMs at risk")
        
        # Stuck migrations
        stuck_bad = len(data["stuck_migrations"])
        summary_line("⏳", "Stuck migrations", stuck_bad == 0, "None" if stuck_bad == 0 else f"{stuck_bad} stuck")
    
    # Dynamic checks from Jira (if any)
    if data.get("dynamic_checks"):
        print(f"{B}╠{'─'*w}╣{X}")
        print(f"{B}║{X}  {BD}{C}🆕 Jira-Suggested Checks:{X}".ljust(w+28) + f"{B}║{X}")
        for check_name, check_data in data["dynamic_checks"].items():
            check_has_issues = bool(check_data.get("issues"))
            jira = check_data.get("jira", "")
            desc = check_data.get("description", check_name)[:30]
            summary_line("🔍", f"{check_name} ({jira})", not check_has_issues, "OK" if not check_has_issues else "Issues found")
    
    print(f"{B}╠{'═'*w}╣{X}")
    
    # Issues detail
    if issues:
        print(f"{B}║{X}  {Y}{BD}⚠️  ISSUES DETECTED:{X}".ljust(w+25) + f"{B}║{X}")
        print(f"{B}║{X}".ljust(w+7) + f"{B}║{X}")
        
        # Unhealthy pods grouped
        if data["pods"]["unhealthy"]:
            by_ns = {}
            for p in data["pods"]["unhealthy"]:
                by_ns.setdefault(p["ns"], []).append(p)
            
            count = 0
            for ns in sorted(by_ns.keys()):
                if count >= 4:
                    remaining = len(data["pods"]["unhealthy"]) - sum(len(by_ns[n]) for n in list(by_ns.keys())[:4])
                    print(f"{B}║{X}    {D}...and {remaining} more unhealthy pods{X}".ljust(w+15) + f"{B}║{X}")
                    break
                print(f"{B}║{X}    {C}{ns}/{X}".ljust(w+20) + f"{B}║{X}")
                for pod in by_ns[ns][:2]:
                    print(f"{B}║{X}      {D}•{X} {pod['name'][:35]} {R}{pod['status']}{X}".ljust(w+25) + f"{B}║{X}")
                if len(by_ns[ns]) > 2:
                    print(f"{B}║{X}      {D}...+{len(by_ns[ns])-2} more{X}".ljust(w+15) + f"{B}║{X}")
                count += 1
        
        # Pending PVCs
        if data["pvcs"]["pending"]:
            print(f"{B}║{X}".ljust(w+7) + f"{B}║{X}")
            print(f"{B}║{X}    {Y}Pending PVCs:{X}".ljust(w+20) + f"{B}║{X}")
            for pvc in data["pvcs"]["pending"][:3]:
                print(f"{B}║{X}      {D}•{X} {pvc['ns']}/{pvc['name']}".ljust(w+15) + f"{B}║{X}")
            if len(data["pvcs"]["pending"]) > 3:
                print(f"{B}║{X}      {D}...+{len(data['pvcs']['pending'])-3} more{X}".ljust(w+15) + f"{B}║{X}")
        
        # Failed Migrations
        if data["migrations"]["failed"] or data["migrations"]["failed_count"] > 0:
            print(f"{B}║{X}".ljust(w+7) + f"{B}║{X}")
            print(f"{B}║{X}    {Y}Failed VM Migrations:{X}".ljust(w+20) + f"{B}║{X}")
            for mig in data["migrations"]["failed"][:3]:
                print(f"{B}║{X}      {D}•{X} {mig['ns']}/{mig['name']}: {R}{mig['phase']}{X}".ljust(w+25) + f"{B}║{X}")
        
        # OOM Events
        if data["oom_events"]:
            print(f"{B}║{X}".ljust(w+7) + f"{B}║{X}")
            print(f"{B}║{X}    {Y}Recent OOM Events:{X}".ljust(w+20) + f"{B}║{X}")
            for oom in data["oom_events"][:3]:
                print(f"{B}║{X}      {D}•{X} {oom['ns']}/{oom['object']}".ljust(w+15) + f"{B}║{X}")
        
        # CSI Issues
        if data["csi_issues"]:
            print(f"{B}║{X}".ljust(w+7) + f"{B}║{X}")
            print(f"{B}║{X}    {Y}CSI Driver Issues:{X}".ljust(w+20) + f"{B}║{X}")
            for csi in data["csi_issues"][:3]:
                print(f"{B}║{X}      {D}•{X} {csi['pod']}: {R}{csi['status']}{X}".ljust(w+25) + f"{B}║{X}")
        
        print(f"{B}║{X}".ljust(w+7) + f"{B}║{X}")
    
    # Footer
    print(f"{B}╠{'═'*w}╣{X}")
    if issues:
        print(f"{B}║{X}  {Y}{BD}STATUS: ATTENTION NEEDED{X}".ljust(w+25) + f"{B}║{X}")
    else:
        print(f"{B}║{X}  {G}{BD}STATUS: CLUSTER HEALTHY ✨{X}".ljust(w+25) + f"{B}║{X}")
    print(f"{B}╚{'═'*w}╝{X}")
    print()

def main():
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RESET = '\033[0m'
    
    print(f"\n{'='*60}")
    print(f"  {BLUE}🔍 CNV HealthCrew AI Starting...{RESET}")
    print(f"{'='*60}\n")
    
    # Show configuration
    print(f"  {YELLOW}⚙️  Configuration:{RESET}")
    if SERVER_HOST:
        print(f"     Server: {SERVER_HOST}")
    else:
        print(f"     Server: Using environment (RH_LAB_HOST)")
    if LAB_NAME:
        print(f"     Lab: {LAB_NAME}")
    print(f"     RCA Level: {'Full' if USE_AI else 'Bug Match' if RCA_BUGS else 'None'}")
    print(f"     AI RCA: {'Yes' if AI_RCA else 'No'}")
    print(f"     Jira RCA: {'Yes' if RCA_JIRA else 'No'}")
    print(f"     Email RCA: {'Yes' if RCA_EMAIL else 'No'}")
    print(f"     Send Email: {'Yes' if SEND_EMAIL else 'No'}")
    print()
    
    # Check Jira for new bugs that might need health checks
    if CHECK_JIRA_NEW:
        print(f"  {YELLOW}🔍 Checking Jira for new test suggestions...{RESET}")
        new_checks = check_jira_for_new_tests()
        if new_checks:
            print(f"  💡 {len(new_checks)} new checks will be included in this run.\n")
    
    print(f"  {BLUE}📡 Connecting to cluster...{RESET}")
    print(f"     Host: {HOST or '(not set)'}")
    print(f"     User: {USER}")
    print(f"     Key:  {KEY_PATH or '(not set)'}")
    print()
    
    try:
        print(f"\n  {BLUE}📊 Collecting cluster data...{RESET}")
        data = collect_data()
        
        # Print console report
        print(f"\n  {BLUE}📋 Generating console report...{RESET}", flush=True)
        print_console_report(data)
        
        # Determine RCA level: full (--ai), bugs (--rca-bugs), or none
        if USE_AI:
            rca_level = 'full'
        elif RCA_BUGS:
            rca_level = 'bugs'
        else:
            rca_level = 'none'
        
        print(f"\n  {BLUE}📄 Generating HTML report...{RESET}", flush=True)
        if rca_level != 'none':
            print(f"     RCA Level: {rca_level}", flush=True)
        
        # Generate and save HTML report with appropriate RCA level
        html = generate_html_report(data, rca_level=rca_level, ai_rca=AI_RCA)
        timestamp = data["timestamp"].strftime("%Y-%m-%d_%H-%M-%S")
        
        # Ensure reports directory exists
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        reports_dir = os.path.join(project_dir, 'reports')
        os.makedirs(reports_dir, exist_ok=True)
        
        html_file = f"health_report_{timestamp}.html"
        md_file = f"health_report_{timestamp}.md"
        html_path = os.path.join(reports_dir, html_file)
        md_path = os.path.join(reports_dir, md_file)
        
        print(f"  {YELLOW}💾 Saving HTML report...{RESET}")
        with open(html_path, 'w') as f:
            f.write(html)
        print(f"     ✅ Saved: {html_file}")
        
        # Also save simple markdown
        print(f"  {YELLOW}💾 Saving Markdown report...{RESET}")
        md_content = f"""# CNV HealthCrew AI Report
**Cluster:** {data['cluster']}  
**Date:** {data['timestamp'].strftime('%Y-%m-%d %H:%M:%S')}  
**Version:** {data['version']}

## Summary
- **Nodes:** {len(data['nodes']['healthy'])}/{len(data['nodes']['healthy'])+len(data['nodes']['unhealthy'])} Ready
- **Operators:** {len(data['operators']['healthy'])}/{len(data['operators']['healthy'])+len(data['operators']['degraded'])+len(data['operators']['unavailable'])} Available  
- **Pods:** {data['pods']['healthy']} Running, {len(data['pods']['unhealthy'])} Unhealthy
- **VMs:** {data['kubevirt']['vms_running']} Running

## {'⚠️ Issues' if has_issues(data) else '✅ No Issues'}
"""
        if data['pods']['unhealthy']:
            by_ns = {}
            for p in data['pods']['unhealthy']:
                by_ns.setdefault(p['ns'], []).append(p)
            md_content += "\n### Unhealthy Pods\n"
            for ns in sorted(by_ns.keys()):
                md_content += f"\n**{ns}/**\n"
                for pod in by_ns[ns]:
                    md_content += f"- `{pod['name']}`: {pod['status']}\n"
        
        with open(md_path, 'w') as f:
            f.write(md_content)
        print(f"     ✅ Saved: {md_file}")
        
        print(f"\n  {GREEN}{'='*50}{RESET}")
        print(f"  {GREEN}✅ Health check complete!{RESET}")
        print(f"  {GREEN}{'='*50}{RESET}")
        print(f"\n  📄 Reports saved:")
        print(f"     • {html_file}")
        print(f"     • {md_file}")
        
        if SEND_EMAIL:
            print(f"\n  📧 Sending email report to {EMAIL_TO}...", flush=True)
            cluster_name = data.get('version', 'Unknown Cluster')
            # Calculate issue count from data
            issue_count = (
                len(data.get('nodes', {}).get('unhealthy', [])) +
                len(data.get('operators', {}).get('degraded', [])) +
                len(data.get('operators', {}).get('unavailable', [])) +
                len(data.get('pods', {}).get('unhealthy', [])) +
                len(data.get('kubevirt', {}).get('failed_vmis', []))
            )
            send_email_report(html_path, EMAIL_TO, cluster_name=cluster_name, issue_count=issue_count, report_data=data)
        
        if has_issues(data):
            if USE_AI:
                print(f"\n  🔍 Full Root Cause Analysis included in report")
            elif RCA_BUGS:
                print(f"\n  🐛 Bug matching included in report (use --ai for full investigation)")
            else:
                print(f"\n  💡 Tip: Run with --rca-bugs for bug matching or --ai for full RCA")
            if AI_RCA:
                print(f"\n  🤖 AI Root Cause Analysis included in report")
            elif not AI_RCA:
                print(f"  💡 Tip: Run with --ai-rca for Gemini-powered AI analysis")
        
        print()
        
    except SSHConnectionError as e:
        RED = '\033[91m'
        print(f"\n  {RED}{'='*60}{RESET}")
        print(f"  {RED}❌ CONNECTION ERROR{RESET}")
        print(f"  {RED}{'='*60}{RESET}")
        print(f"\n  {RED}{e}{RESET}\n")
        print(f"  {YELLOW}Connection details:{RESET}")
        print(f"     Host:  {e.host or '(not set)'}")
        print(f"     User:  {e.user or '(not set)'}")
        print(f"     Key:   {e.key_path or '(not set)'}")
        if e.original_error:
            print(f"     Error:  {type(e.original_error).__name__}: {e.original_error}")
        print()
        print(f"  {YELLOW}Troubleshooting:{RESET}")
        print(f"     1. Verify the host is reachable: ssh {e.user or 'root'}@{e.host or '<host>'}")
        print(f"     2. Check SSH key exists and has correct permissions")
        print(f"     3. Ensure RH_LAB_HOST and SSH_KEY_PATH are set correctly")
        print(f"     4. If using --server, verify the hostname is correct")
        print()

        # Generate an error report so the dashboard shows useful info
        try:
            from datetime import datetime as _dt
            project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            reports_dir = os.path.join(project_dir, 'reports')
            os.makedirs(reports_dir, exist_ok=True)
            timestamp = _dt.now().strftime("%Y-%m-%d_%H-%M-%S")
            html_file = f"health_report_{timestamp}.html"
            html_path = os.path.join(reports_dir, html_file)
            error_html = generate_error_report_html(e)
            with open(html_path, 'w') as f:
                f.write(error_html)
            print(f"  {YELLOW}📄 Error report saved: {html_file}{RESET}")
        except Exception:
            pass

        print()
        sys.exit(1)

    except Exception as e:
        print(f"\n  ❌ Error: {e}\n")
        import traceback
        traceback.print_exc()
    finally:
        if ssh_client:
            ssh_client.close()

if __name__ == "__main__":
    main()
