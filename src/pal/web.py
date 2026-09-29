# ruff: noqa: E501

"""Local PAL management console for publication and production operations.

The console is deliberately a thin local control plane. It reads the immutable
PAL objects through existing validators and delegates every state transition to
the existing lifecycle APIs; it never edits library JSON files directly.

Traceability: BRAND-001 through BRAND-005; PRD-RELEASE-001/002, PRD-MOUNT-003/004, PRD-TECH-001;
SLC-011 through SLC-015; WEB-001 through WEB-006; PSM-001/002/004/006/007/008/009/010; ACC-007, ACC-008, ACC-011, ACC-012.
"""

from __future__ import annotations

import errno
import ipaddress
import json
import os
import secrets
import socket
import webbrowser
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .build_identity import BUILD_ID
from .config_mount import resolve_config_root
from .deletion import recover_deletion
from .errors import IntegrityError, PALError, PathSafetyError
from .installation_monitor import inspect_installations, repair_installation
from .integrity import check_library_contents
from .io import atomic_replace_json
from .library import doctor_development_context, load_json_object
from .maintenance import cleanup_path, maintenance_lock, skill_action_path
from .paths import canonical_existing_root, require_safe_id
from .platform_support import is_link
from .production_mount import recover_production
from .publication import sync_production
from .schema_catalog import validate_config_instance
from .skill_actions import execute_skill_action, preview_skill_action, recover_skill_action
from .skill_browser import skill_details, skill_file
from .status import library_status
from .usage import recover_usage_transactions

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
ACTION_HEADER = "X-PAL-Action"
ACTION_CONFIRMATION = "confirm"


LOGO_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 166 98" class="pal-mark" fill="none" role="img" aria-label="PAL">\n  <g stroke="currentColor" stroke-width="12" stroke-linecap="round" stroke-linejoin="round">\n    <path d="M12 86V45C12 32.3 20.3 24 33 24S54 32.3 54 45 45.7 66 33 66 12 57.7 12 45"/>\n    <path d="M113 66V45C113 32.3 104.7 24 92 24S71 32.3 71 45 79.3 66 92 66 113 57.7 113 45"/>\n    <path d="M136 10V53C136 61.5 140.5 66 149 66H154"/>\n  </g>\n</svg>'

FAVICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" fill="none" role="img" aria-labelledby="title">\n  <title id="title">PAL 图标</title>\n  <rect width="64" height="64" rx="17" fill="#233d38"/>\n  <path d="M19 49V29C19 21.7 23.7 17 31 17S43 21.7 43 29 38.3 41 31 41 19 36.3 19 29" stroke="#edf3dc" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>\n</svg>'


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PAL · 个人能力库</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<script>
(() => { let preference; try { preference = localStorage.getItem('pal-theme'); } catch (_) {} document.documentElement.dataset.theme = ['light','dark'].includes(preference) ? preference : matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'; })();
</script>
<style>
:root { color-scheme: light; --brand-ink:#233d38; --ink:#202b38; --muted:#657385; --line:#e2e7ed; --paper:#f5f7fa; --panel:#fff; --blue:#315cce; --soft:#edf2ff; --green:#18714f; --red:#b33b36; --amber:#99611d; }
* { box-sizing:border-box; }
body { margin:0; background:var(--paper); color:var(--ink); font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif; }
button,input { font:inherit; } button { display:inline-flex; align-items:center; justify-content:center; gap:7px; min-height:38px; padding:7px 13px; border:1px solid var(--line); border-radius:8px; background:var(--panel); color:var(--ink); cursor:pointer; transition:background .16s, border-color .16s, box-shadow .16s; }
button:hover { background:var(--paper); border-color:#bfcbdc; } button:disabled { opacity:.48; cursor:not-allowed; } button.primary { background:var(--blue); border-color:var(--blue); color:#fff; } button.primary:hover { background:#244db9; } button.danger { color:var(--red); } button.danger.primary { background:var(--red); border-color:var(--red); color:#fff; } button.quiet { background:transparent; border-color:transparent; } button.small { min-height:32px; padding:4px 10px; font-size:12px; } button.icon-button { padding:8px; }
:focus-visible { outline:3px solid #91adf4; outline-offset:3px; } svg { width:18px; height:18px; flex-shrink:0; fill:none; stroke:currentColor; stroke-width:1.7; stroke-linecap:round; stroke-linejoin:round; }
h1,h2,h3,p { margin:0; } h1 { font-size:27px; letter-spacing:-.5px; line-height:1.35; } h2 { font-size:16px; } h3 { font-size:14px; } code { font:12px/1.6 ui-monospace,SFMono-Regular,monospace; overflow-wrap:anywhere; } .small-copy { font-size:12px; color:var(--muted); } .eyebrow { color:var(--muted); font-size:11px; letter-spacing:1.4px; font-weight:600; }
.shell { display:grid; grid-template-columns:224px minmax(0,1fr); min-height:100vh; } .sidebar { position:sticky; top:0; height:100vh; background:var(--panel); border-right:1px solid var(--line); padding:28px 18px 20px; display:flex; flex-direction:column; gap:30px; }
.brand { display:flex; align-items:center; gap:12px; padding:0 10px; } .brand-mark { display:grid; place-items:center; width:62px; flex-shrink:0; } .brand-mark .pal-mark { width:62px; height:40px; color:var(--brand-ink); } .brand-copy { min-width:0; } .brand strong { display:block; font-size:13px; font-weight:600; letter-spacing:.06em; white-space:nowrap; } .brand .small-copy { font-size:9px; line-height:1.5; margin-top:2px; }
.nav-label { margin:0 12px 9px; } nav { display:grid; gap:6px; } nav button { justify-content:flex-start; gap:12px; border:0; padding:11px 13px; background:transparent; color:var(--muted); } nav button[aria-current="page"] { background:var(--soft); color:var(--blue); font-weight:600; }
.sidebar-bottom { margin-top:auto; display:grid; gap:16px; } .local-label { border-top:1px solid var(--line); padding:16px 10px 0; font-size:12px; color:var(--muted); } .dot { display:inline-block; width:7px; height:7px; border-radius:50%; background:var(--green); margin-right:7px; }
.workspace { min-width:0; } .topbar { min-height:73px; border-bottom:1px solid var(--line); display:flex; align-items:center; justify-content:space-between; gap:16px; padding:16px 36px; background:var(--panel); } .library-name { font-weight:600; overflow-wrap:anywhere; } .top-actions { display:flex; flex-wrap:wrap; gap:12px; align-items:center; justify-content:flex-end; } .health { font-size:12px; color:var(--green); } .health.bad { color:var(--red); }
main { max-width:1460px; margin:auto; padding:32px 36px 48px; } .page-heading { display:flex; align-items:center; justify-content:space-between; gap:20px; margin-bottom:25px; } .page-heading p { color:var(--muted); margin-top:7px; font-size:13px; } .panel { border:1px solid var(--line); border-radius:12px; background:var(--panel); min-width:0; } .panel-head { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:19px 22px; border-bottom:1px solid var(--line); } .panel-body { padding:22px; } .list-tools { padding:17px 22px; display:grid; gap:14px; border-bottom:1px solid var(--line); } .search { display:flex; align-items:center; gap:9px; color:var(--muted); background:var(--paper); border:1px solid transparent; border-radius:7px; padding:7px 11px; } .search:focus-within { border-color:#91adf4; } .search input { min-width:0; width:100%; border:0; outline:none; background:transparent; color:var(--ink); } .filters { display:flex; flex-wrap:wrap; gap:5px; } .filters button { min-height:30px; padding:3px 10px; border-color:transparent; font-size:12px; color:var(--muted); } .filters button[aria-pressed="true"] { background:var(--soft); color:var(--blue); } .skill-heading { display:flex; align-items:center; flex-wrap:wrap; gap:9px; } .badge { display:inline-flex; align-items:center; white-space:nowrap; padding:2px 8px; font-size:11px; font-weight:500; border-radius:5px; color:var(--muted); background:#f0f3f6; } .badge.ok { color:var(--green); background:#eaf6ef; } .badge.warn { color:var(--amber); background:#fff3df; } .badge.blue { color:var(--blue); background:#e9efff; } .badge.bad { color:var(--red); background:#ffedeb; }        .change-list { list-style:none; margin:0 0 16px; padding:0; display:grid; gap:11px; } .change-list li { display:flex; gap:9px; align-items:start; font-size:12px; }

.empty { text-align:center; padding:46px 24px; color:var(--muted); } .empty svg { width:28px; height:28px; margin-bottom:8px; color:#92a3b6; } .empty strong { display:block; color:var(--ink); margin-bottom:5px; } .empty p { font-size:13px; } .notice { border-radius:8px; padding:13px 15px; background:var(--soft); color:#3a568a; font-size:13px; margin-bottom:18px; } .notice.error { background:#fff0ed; color:#913c35; } .notice.warn { background:#fff5e5; color:#865c20; }
.data-list { display:grid; gap:18px; margin:0; } .data-list dt { font-size:12px; color:var(--muted); margin-bottom:4px; } .data-list dd { margin:0; overflow-wrap:anywhere; } .management-page { display:grid; gap:22px; }
.section-heading { display:flex; align-items:flex-start; gap:12px; min-width:0; } .section-heading > div { min-width:0; } .section-heading p { color:var(--muted); font-size:12px; margin-top:5px; }
.section-icon { display:grid; place-items:center; width:36px; height:36px; flex-shrink:0; background:var(--soft); color:var(--blue); border-radius:9px; }
.management-summary { display:flex; align-items:center; justify-content:space-between; gap:18px; padding:20px 22px; } .management-summary > button { flex-shrink:0; } .management-summary .skill-heading { gap:10px; }
.management-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; padding:20px 22px; }
.monitor-card,.task-card { min-width:0; display:flex; flex-direction:column; border:1px solid var(--line); border-radius:10px; padding:18px; }
.monitor-card .skill-heading { justify-content:space-between; } .monitor-message { color:var(--muted); font-size:13px; margin-top:12px; overflow-wrap:anywhere; }
.monitor-versions { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; margin:14px 0 0; padding:12px; border-radius:8px; background:var(--paper); } .monitor-versions dt { font-size:11px; color:var(--muted); } .monitor-versions dd { margin:3px 0 0; }
.card-footer { margin-top:auto; padding-top:18px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; } .card-footer .small-copy { display:inline-flex; align-items:center; gap:6px; }
.section-footer { display:flex; align-items:center; justify-content:space-between; gap:18px; padding:18px 22px; border-top:1px solid var(--line); background:var(--paper); border-radius:0 0 12px 12px; } .section-footer > button { flex-shrink:0; } .section-footer p { margin-top:5px; } .section-footer strong { font-size:13px; }
.management-note { display:flex; align-items:flex-start; gap:8px; color:var(--muted); font-size:12px; } .management-note svg { width:16px; height:18px; } .management-page > .notice { margin:0; }
.library-facts { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:20px 32px; } .library-facts .wide { grid-column:1/-1; } .library-facts dd code { display:block; padding:10px 12px; background:var(--paper); border:1px solid var(--line); border-radius:8px; }
.library-stages { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; margin:0; } .library-stages > div { padding:16px; border:1px solid var(--line); border-radius:10px; background:var(--paper); } .library-stages dt { font-size:12px; color:var(--muted); } .library-stages dd { margin:7px 0; display:flex; align-items:baseline; gap:6px; } .library-stages strong { font-size:26px; line-height:1.25; } .library-stages span { font-size:12px; color:var(--muted); } .library-stage-note { margin-top:14px; }
.task-card p { font-size:13px; color:var(--muted); margin-top:8px; } .task-card .section-icon { margin-bottom:12px; }
@media(max-width:720px) { .management-grid { grid-template-columns:1fr; } .management-summary,.section-footer { align-items:flex-start; flex-direction:column; } .library-stages { gap:8px; } .library-stages > div { padding:12px; } }
@media(max-width:660px) { .management-page { gap:16px; } .management-grid,.management-summary,.section-footer,.management-page .panel-body { padding:16px; } .top-actions { width:100%; justify-content:flex-start; gap:6px; } .top-actions .health { margin-right:auto; } .library-facts { grid-template-columns:1fr; gap:18px; } .library-stages { grid-template-columns:1fr; } .library-stages > div { display:grid; grid-template-columns:1fr auto; align-items:center; column-gap:10px; } .library-stages p { grid-column:1/-1; } .library-stages dd { margin:0; } }
.sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; } [hidden] { display:none !important; }
dialog { border:1px solid var(--line); border-radius:16px; padding:0; width:min(560px,calc(100% - 32px)); max-height:calc(100dvh - 48px); color:var(--ink); background:var(--panel); box-shadow:0 24px 100px #13233b33; overflow:auto; } dialog[open] { animation:dialog-in .2s ease-out; } dialog::backdrop { background:#19283c70; backdrop-filter:blur(3px); animation:fade-in .18s ease-out; } .dialog-heading { padding:25px 25px 16px; display:flex; align-items:center; gap:13px; } .dialog-symbol { display:grid; place-items:center; width:38px; height:38px; border-radius:11px; background:var(--soft); color:var(--blue); flex-shrink:0; } dialog[data-state="failure"] .dialog-symbol { background:#ffedeb; color:var(--red); } dialog[data-state="success"] .dialog-symbol { background:#eaf6ef; color:var(--green); } .dialog-heading h2 { font-size:19px; } .dialog-body { padding:0 25px 24px; overflow-wrap:anywhere; } .dialog-description { color:var(--muted); font-size:13px; margin-bottom:18px; white-space:pre-line; } .dialog-actions { position:sticky; bottom:0; display:flex; justify-content:flex-end; gap:9px; padding:16px 25px; background:var(--paper); border-top:1px solid var(--line); } .dialog-actions button { min-width:80px; } .dialog-body .change-list { max-height:260px; overflow:auto; padding:15px; background:var(--paper); border-radius:8px; } .step-number { color:var(--blue); background:var(--soft); min-width:26px; height:26px; display:grid; place-items:center; font-size:12px; border-radius:50%; } .dialog-body details { border-top:1px solid var(--line); padding-top:12px; margin-top:15px; } summary { cursor:pointer; color:var(--muted); font-size:12px; } .spinner { display:inline-block; flex-shrink:0; vertical-align:middle; width:19px; height:19px; border:2px solid var(--line); border-top-color:var(--blue); border-radius:50%; animation:spin .75s linear infinite; }
.view-enter { animation:fade-in .2s ease-out; } @keyframes spin { to { transform:rotate(360deg); } } @keyframes fade-in { from { opacity:.35; } to { opacity:1; } } @keyframes dialog-in { from { opacity:.4; transform:translateY(8px) scale(.99); } to { opacity:1; transform:translateY(0) scale(1); } }
@media(max-width:1150px) { .shell { grid-template-columns:190px minmax(0,1fr); } main { padding:26px 24px; } .topbar { padding:16px 24px; }  .sidebar { padding:24px 12px; } }
@media(max-width:920px) { .shell { grid-template-columns:minmax(0,1fr); } .sidebar { position:static; height:auto; padding:15px 20px 0; gap:16px; border-right:0; border-bottom:1px solid var(--line); } .brand { padding:0; } .brand .small-copy,.nav-label,.sidebar-bottom { display:none; } nav { display:flex; gap:6px; } nav button { border-radius:8px 8px 0 0; } .topbar { min-height:60px; } }
@media(max-width:660px) {  main { padding:24px 16px 40px; } .topbar { padding:12px 16px; flex-wrap:wrap; gap:8px; } .top-actions { gap:8px; } .page-heading { align-items:flex-start; margin-bottom:20px; } h1 { font-size:23px; } .page-heading p { font-size:12px; }   .panel-head,.list-tools{ padding:15px 16px; } nav button { padding:9px 11px; font-size:13px; gap:7px; } .dialog-heading { padding:20px 20px 14px; } .dialog-body { padding:0 20px 20px; } .dialog-actions { padding:14px 20px; flex-wrap:wrap; } }
@media(prefers-reduced-motion:reduce) { *,*::before,*::after { animation-duration:.01ms !important; animation-iteration-count:1 !important; transition-duration:.01ms !important; scroll-behavior:auto !important; } }
.health { display:inline-flex; align-items:center; gap:7px; padding:7px 12px; border-radius:8px; font-size:13px; font-weight:650; color:var(--green); background:#eaf6ef; border:1px solid #b7ddc8; white-space:nowrap; }
.health.bad { background:#ffedeb; border-color:#edbbb6; color:var(--red); } .health.wait { background:var(--soft); border-color:#bbccef; color:var(--blue); } .health.warn { background:#fff3df; border-color:#e7c992; color:var(--amber); }
.health svg,.health .spinner { width:16px; height:16px; } .theme-toggle { min-width:38px; } .skill-summary { margin:7px 0 0; color:var(--ink); font-size:13px; display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; overflow-wrap:anywhere; }
dialog[data-wide="true"] { width:min(800px,calc(100% - 32px)); } :root[data-theme="dark"] { color-scheme:dark; --brand-ink:#f4f6ef; --ink:#e2e9f3; --muted:#a4b2c6; --line:#344157; --paper:#141c29; --panel:#1d2838; --blue:#a1baff; --soft:#283959; --green:#79d6ac; --red:#ffa39b; --amber:#f0ca80; }
[data-theme="dark"] button.primary { background:#4064cf; border-color:#4064cf; color:#fff; } [data-theme="dark"] button.primary:hover { background:#5075e1; } [data-theme="dark"] button.danger.primary { background:#a9423d; border-color:#a9423d; } [data-theme="dark"] button:hover { border-color:#6e83a3; } [data-theme="dark"] .badge { background:#303e52; }
[data-theme="dark"] .badge.ok,[data-theme="dark"] .health,[data-theme="dark"] dialog[data-state="success"] .dialog-symbol { background:#203f36; color:var(--green); border-color:#3c7360; }
[data-theme="dark"] .badge.warn,[data-theme="dark"] .notice.warn,[data-theme="dark"] .health.warn { background:#493b23; color:var(--amber); border-color:#8b7040; }
[data-theme="dark"] .badge.bad,[data-theme="dark"] .notice.error,[data-theme="dark"] .health.bad,[data-theme="dark"] dialog[data-state="failure"] .dialog-symbol { background:#482c31; color:var(--red); border-color:#885357; }

[data-theme="dark"] .badge.blue,[data-theme="dark"] .notice,[data-theme="dark"] .health.wait { background:var(--soft); color:var(--blue); border-color:#536b9c; } [data-theme="dark"] dialog::backdrop { background:#060b14b3; }
.skill-page { display:grid; gap:18px; } .detail-toolbar { display:flex; flex-wrap:wrap; gap:12px 20px; align-items:center; } .detail-clis { display:flex; gap:8px; flex-wrap:wrap; } .detail-toolbar label { display:flex; gap:8px; align-items:center; color:var(--muted); font-size:12px; } select { font:inherit; color:var(--ink); background:var(--panel); border:1px solid var(--line); border-radius:7px; padding:8px 30px 8px 10px; max-width:100%; } .detail-tabs { display:flex; gap:5px; border-bottom:1px solid var(--line); padding:0 20px; } .detail-tabs button { border:0; border-radius:0; border-bottom:3px solid transparent; padding:13px 20px; background:transparent; color:var(--muted); } .detail-tabs button[aria-selected="true"] { color:var(--blue); border-bottom-color:var(--blue); font-weight:600; } .detail-back { margin-bottom:16px; } #page-title { overflow-wrap:anywhere; } .overview { padding:28px; } .overview-description { font-size:15px; margin:12px 0 24px; } .technical-info { border-top:1px solid var(--line); padding-top:18px; margin-top:24px; } .technical-info dl { margin-top:15px; }
.file-layout { display:grid; grid-template-columns:240px minmax(0,1fr); min-height:480px; } .file-sidebar { border-right:1px solid var(--line); padding:17px 12px; background:var(--paper); border-radius:0 0 0 12px; } .file-sidebar h3 { padding:0 8px 10px; } .file-tree,.file-tree ul { list-style:none; padding:0; margin:0; } .file-tree ul { padding-left:14px; } .file-tree summary { padding:6px; color:var(--ink); font-size:13px; overflow-wrap:anywhere; } .file-tree button { width:100%; justify-content:flex-start; text-align:left; padding:6px 8px; border:0; background:transparent; min-height:33px; font-size:12px; overflow-wrap:anywhere; } .file-tree button[aria-current="true"] { background:var(--soft); color:var(--blue); } .file-tree button span { min-width:0; overflow-wrap:anywhere; } .file-tree svg { width:14px; } .file-viewer { min-width:0; } .file-bar { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:10px; padding:14px 20px; border-bottom:1px solid var(--line); } .file-path { flex:1; min-width:140px; overflow-wrap:anywhere; } .file-content { padding:24px; min-height:300px; overflow:auto; } .source-code { counter-reset:line; font:12px/1.8 ui-monospace,SFMono-Regular,monospace; tab-size:4; } .source-line { display:flex; min-height:1.8em; } .source-line::before { counter-increment:line; content:counter(line); flex:0 0 42px; text-align:right; padding-right:16px; color:var(--muted); user-select:none; } .source-line code { white-space:pre-wrap; overflow-wrap:anywhere; min-width:0; } .file-metadata { padding:12px 20px; border-top:1px solid var(--line); color:var(--muted); font-size:12px; }
.markdown { line-height:1.85; overflow-wrap:anywhere; } .markdown > :first-child { margin-top:0; } .markdown h1 { font-size:24px; } .markdown h2 { font-size:20px; } .markdown h3 { font-size:16px; } .markdown h1,.markdown h2,.markdown h3,.markdown h4 { margin:24px 0 12px; } .markdown p,.markdown ul,.markdown ol,.markdown pre,.markdown blockquote { margin:0 0 15px; } .markdown li > p { margin-bottom:7px; } .markdown a { color:var(--blue); text-underline-offset:3px; } .markdown pre { background:var(--paper); padding:16px; border-radius:8px; overflow:auto; } .markdown pre code { white-space:pre-wrap; overflow-wrap:anywhere; } .markdown :not(pre) > code { background:var(--paper); padding:2px 5px; border-radius:4px; } .markdown blockquote { margin-left:0; padding:8px 16px; border-left:3px solid var(--line); color:var(--muted); } .markdown table { display:block; max-width:100%; overflow:auto; border-collapse:collapse; margin-bottom:18px; } .markdown td,.markdown th { padding:8px 13px; border:1px solid var(--line); text-align:left; } .markdown hr { border:0; border-top:1px solid var(--line); margin:20px 0; } .image-label { color:var(--muted); font-size:12px; }
@media(max-width:660px) { .file-layout { grid-template-columns:1fr; } .file-sidebar { border-right:0; border-bottom:1px solid var(--line); border-radius:0; max-height:240px; overflow:auto; } .overview,.file-content { padding:18px; } .detail-toolbar { align-items:stretch; flex-direction:column; gap:10px; } .detail-toolbar label { justify-content:space-between; } .detail-toolbar select { max-width:75%; } .detail-tabs { padding:0 12px; } .file-bar { padding:12px 16px; } .detail-back { margin-bottom:12px; } }


.stage-guide { container-type:inline-size; padding:20px; margin-bottom:22px; border:1px solid var(--line); border-radius:12px; background:var(--panel); }
.stage-guide-heading { display:flex; align-items:baseline; flex-wrap:wrap; gap:4px 14px; } .stage-guide-heading h2 { font-size:14px; } .stage-guide-heading p { color:var(--muted); font-size:12px; }
.stage-steps { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:80px; list-style:none; margin:18px 0 0; padding:0; }
.stage-step { position:relative; display:flex; align-items:start; gap:12px; min-width:0; padding:16px; border:1px solid var(--line); border-radius:10px; background:var(--paper); }
.stage-number { display:grid; place-items:center; flex:0 0 30px; height:30px; border:1px solid var(--blue); border-radius:50%; background:var(--soft); color:var(--blue); font-size:14px; font-weight:700; }
.stage-step-copy { min-width:0; } .stage-step-copy h3 { margin:1px 0 4px; font-size:14px; } .stage-step-copy p { font-size:12px; color:var(--muted); } .stage-library { display:block; margin-top:9px; font-size:11px; color:var(--blue); }
.stage-connector { position:absolute; left:calc(100% + 8px); top:50%; transform:translateY(-50%); width:64px; display:flex; flex-direction:column; align-items:center; gap:4px; color:var(--muted); font-size:11px; white-space:nowrap; } .stage-connector svg { width:50px; height:18px; color:var(--blue); }
.skill-card { border:1px solid var(--line); border-radius:12px; background:var(--panel); margin-bottom:18px; overflow:hidden; } .skill-card-head { display:flex; align-items:start; justify-content:space-between; flex-wrap:wrap; gap:12px; padding:20px; } .skill-card-head .skill-summary { margin:8px 0 0; }
.skill-flow { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); border-top:1px solid var(--line); } .skill-stage { display:flex; flex-direction:column; align-items:start; gap:10px; padding:18px; min-width:0; } .skill-stage + .skill-stage { border-left:1px solid var(--line); } .skill-stage h3 { display:flex; align-items:center; gap:8px; font-size:15px; } .skill-stage h3 span { display:inline-grid; place-items:center; border:1px solid var(--line); border-radius:50%; width:23px; height:23px; font-size:12px; color:var(--muted); } .stage-state { font-size:13px; } .stage-action { margin-top:auto; padding-top:12px; } .skill-targets { display:grid; gap:7px; width:100%; font-size:12px; } .skill-targets>div { display:flex; flex-wrap:wrap; justify-content:space-between; gap:6px; } .skill-targets>div>span:last-child { color:var(--muted); }
.skill-manage,.skill-technical,.skill-manage-note { padding:13px 20px; border-top:1px solid var(--line); font-size:12px; color:var(--muted); } .skill-manage>div { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; padding-top:16px; } .skill-manage summary,.skill-technical summary { cursor:pointer; } .skill-technical .data-list { margin-top:14px; } .skill-technical code { overflow-wrap:anywhere; }
.action-impact { display:grid; margin:0 0 18px; } .action-impact>div { display:grid; grid-template-columns:90px 1fr; gap:12px; padding:12px 0; border-bottom:1px solid var(--line); } .action-impact dt { color:var(--muted); } .action-impact dd { margin:0; }
@media(max-width:680px) { .skill-flow { grid-template-columns:1fr; } .skill-stage + .skill-stage { border-left:0; border-top:1px solid var(--line); } .stage-guide { padding:16px; } .skill-card-head { padding:16px; } }
@container (max-width:700px) { .stage-steps { grid-template-columns:1fr; gap:42px; margin-top:16px; } .stage-step { padding:13px; } .stage-library { margin-top:5px; } .stage-connector { left:18px; top:calc(100% + 1px); width:auto; height:40px; transform:none; flex-direction:row-reverse; gap:9px; } .stage-connector svg { width:30px; height:18px; transform:rotate(90deg); } }
@media(max-width:380px) { .top-actions .health { flex-basis:100%; margin:0 0 3px; } .sidebar { padding-left:12px; padding-right:12px; } nav { gap:1px; } nav button { padding:9px 6px; gap:4px; font-size:12px; white-space:nowrap; } nav button svg { width:16px; } }
.skill-title-link { padding:0; min-height:28px; border:0; background:transparent; font-size:15px; font-weight:650; text-align:left; overflow-wrap:anywhere; justify-content:flex-start; } .skill-title-link:hover { background:transparent; color:var(--blue); text-decoration:underline; text-underline-offset:4px; } .skill-card-head > div { flex:1; min-width:200px; } .detail-entry { flex-shrink:0; color:var(--blue); border-color:var(--line); } .detail-entry svg:last-child { width:14px; }
.dialog-heading h2 { flex:1; }
body:has(dialog[data-drawer="true"][open]) { overflow:hidden; }
dialog[data-drawer="true"] { position:fixed; inset:0 0 0 auto; margin:0; width:min(540px,100%); max-width:100vw; height:100dvh; max-height:100dvh; border-radius:16px 0 0 16px; border-width:0 0 0 1px; overflow:hidden; }
dialog[data-drawer="true"][open] { display:flex; flex-direction:column; animation:drawer-in .24s ease-out; }
dialog[data-drawer="true"] .dialog-heading { flex-shrink:0; padding:24px 24px 16px; }
dialog[data-drawer="true"] .dialog-body { min-height:0; flex:1; overflow-y:auto; overscroll-behavior:contain; padding:0 24px 24px; }
dialog[data-drawer="true"] .dialog-actions { position:static; flex-shrink:0; }
.guide-tabs { display:flex; gap:4px; position:sticky; top:0; z-index:1; background:var(--panel); border-bottom:1px solid var(--line); padding:0 0 12px; margin-bottom:24px; }
.guide-tabs button { flex:1; font-size:12px; border-color:transparent; padding:8px; color:var(--muted); background:transparent; white-space:nowrap; }
.guide-tabs button[aria-selected="true"] { color:var(--blue); background:var(--soft); font-weight:600; }
.guide-lead { margin-bottom:22px; } .guide-lead h3 { font-size:20px; line-height:1.5; margin:5px 0 8px; } .guide-lead p { color:var(--muted); font-size:13px; }
.guide-steps { list-style:none; margin:0; padding:0; display:grid; gap:14px; } .guide-steps li { display:flex; gap:13px; padding:17px; border:1px solid var(--line); border-radius:10px; } .guide-steps .step-number { flex-shrink:0; } .guide-steps h3 { font-size:14px; } .guide-steps p { color:var(--muted); font-size:13px; margin-top:6px; } .guide-tag { display:block; margin-top:10px; color:var(--blue); font-size:11px; }
.guide-tip { display:flex; gap:10px; padding:14px 16px; border-radius:10px; background:var(--soft); color:var(--ink); font-size:12px; margin-top:18px; } .guide-tip svg { color:var(--blue); margin-top:1px; }
.guide-rows { margin:0; display:grid; gap:0; } .guide-rows > div { padding:16px 0; border-bottom:1px solid var(--line); } .guide-rows > div:first-child { padding-top:0; } .guide-rows dt { font-weight:600; font-size:13px; } .guide-rows dd { margin:6px 0 0; color:var(--muted); font-size:13px; } .guide-section-title { font-size:14px; margin:22px 0 14px; }
.guide-command { display:grid; gap:5px; padding:12px 14px; border:1px solid var(--line); border-radius:8px; background:var(--paper); margin-top:10px; } .guide-command span { font-size:12px; color:var(--muted); } .guide-command code { color:var(--ink); }
@keyframes drawer-in { from { transform:translateX(100%); } to { transform:translateX(0); } }
@media(max-width:540px) { dialog[data-drawer="true"] { border-radius:0; border:0; } dialog[data-drawer="true"] .dialog-heading { padding:20px 18px 14px; } dialog[data-drawer="true"] .dialog-body { padding:0 18px 20px; } .guide-steps li { padding:14px; } }
</style>
</head>
<body>
<div class="shell">
  <aside class="sidebar">
    <div class="brand"><div class="brand-mark">__PAL_LOGO__</div><div class="brand-copy"><strong>个人能力库</strong><div class="small-copy">Personal Ability Library</div></div></div>
    <div><p class="eyebrow nav-label">工作空间</p><nav aria-label="控制台导航">
      <button data-view="collection" aria-current="page"><span data-icon="grid"></span>Skill 集合</button>
      <button data-view="sync"><span data-icon="refresh"></span>CLI 与系统</button>
      <button data-view="maintenance"><span data-icon="settings"></span>库与维护</button>
    </nav></div>
    <div class="sidebar-bottom"><div class="local-label"><span class="dot"></span>本地工作空间</div></div>
  </aside>
  <div class="workspace">
    <header class="topbar"><div><span class="small-copy">外挂库 / </span><span id="library-name" class="library-name">正在读取</span></div><div class="top-actions"><span id="health" class="health wait" role="status"><span class="spinner" aria-hidden="true"></span>读取中</span><button id="theme-toggle" class="theme-toggle small" data-action="theme" aria-label="切换到夜间模式" title="切换到夜间模式"></button><button class="quiet small" data-action="refresh"><span data-icon="refresh"></span>刷新</button><button class="quiet small" data-action="help"><span data-icon="help"></span>使用说明</button></div></header>
    <main id="main" tabindex="-1"><div class="page-heading"><div><h1 id="page-title">Skill 集合</h1><p id="page-description">将开发内容发布到生产，再按需同步 CLI。</p></div></div><div id="content" aria-busy="true"><div class="empty"><span class="spinner" aria-hidden="true"></span><p>正在读取外挂库…</p></div></div></main>
  </div>
</div>
<p id="announcement" class="sr-only" role="status" aria-live="polite"></p>
<dialog id="action-dialog" aria-labelledby="dialog-title" aria-describedby="dialog-description">
  <div class="dialog-heading"><div id="dialog-symbol" class="dialog-symbol"></div><h2 id="dialog-title" tabindex="-1"></h2><button id="drawer-close" class="quiet icon-button" aria-label="关闭使用说明" hidden><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6"/></svg></button></div>
  <div class="dialog-body"><p id="dialog-description" class="dialog-description"></p><div id="dialog-content"></div></div>
  <div id="dialog-actions" class="dialog-actions"></div>
</dialog>
<script>
const icons = {
  moon:'<path d="M20 15A9 9 0 0 1 9 4a9 9 0 1 0 11 11Z"/>',
  sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1"/>',
  grid:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  settings:'<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3" fill="var(--panel)"/><circle cx="15" cy="17" r="3" fill="var(--panel)"/>',
  help:'<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 0 1 5 0c0 2-2.5 2-2.5 4m0 3h.01"/>',
  refresh:'<path d="M20 8a8 8 0 0 0-14-2L3 9m0-6v6h6m-5 7a8 8 0 0 0 14 2l3-3m0 6v-6h-6"/>',
  search:'<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>',
  skill:'<path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5"/>',
  check:'<path d="m5 12 4 4L19 6"/>',
  alert:'<circle cx="12" cy="12" r="9"/><path d="M12 7v6m0 4h.01"/>',
  arrow:'<path d="M5 12h14m-5-5 5 5-5 5"/>',
  file:'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Zm0 0v6h6M8 13h8m-8 4h6"/>',
  undo:'<path d="M4 5v6h6m-6 0c3-7 16-7 16 2 0 5-6 7-10 5"/>',
};
const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.skill}</svg>`;
document.querySelectorAll('[data-icon]').forEach(node => { node.innerHTML = icon(node.dataset.icon); });
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const badge = (label, kind = '') => `<span class="badge ${kind}">${esc(label)}</span>`;
const time = value => value ? new Intl.DateTimeFormat('zh-CN',{dateStyle:'medium',timeStyle:'short'}).format(new Date(value)) : '尚无记录';
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
let data = null, stateValid = false, stateError = '', view = 'collection', filter = 'all', search = '';
let busy = false, modal = null, returnFocus = null, drawerClosing = false;
const dialog = $('action-dialog');
let skillPage = null, detailRequest = 0, fileRequest = 0;
let monitor = null, monitorError = '', monitoring = false, monitorRequest = 0;
const announce = text => { $('announcement').textContent = text; };
function showDialog({state = 'info', title, description = '', body = '', buttons = [], wide = false, drawer = false}) {
  dialog.dataset.wide = String(wide);
  dialog.dataset.drawer = String(drawer);
  $('drawer-close').hidden = !drawer;
  if (!dialog.open) returnFocus = document.activeElement;
  dialog.dataset.state = state;
  dialog.setAttribute('aria-busy',String(state === 'working'));
  $('dialog-symbol').innerHTML = state === 'working' ? '<span class="spinner" aria-hidden="true"></span>' : icon(state === 'success' ? 'check' : state === 'failure' ? 'alert' : drawer ? 'help' : 'skill');
  $('dialog-title').textContent = title;
  $('dialog-description').textContent = description;
  $('dialog-content').innerHTML = body;
  $('dialog-actions').replaceChildren();
  buttons.forEach(({label, action, primary = false, danger = false, focus = false}) => {
    const button = document.createElement('button');
    button.textContent = label; button.className = `${primary ? 'primary' : ''} ${danger ? 'danger' : ''}`;
    button.addEventListener('click', action); button.dataset.initialFocus = String(focus);
    $('dialog-actions').append(button);
  });
  $('dialog-actions').hidden = !buttons.length;
  if (!dialog.open) dialog.showModal();
  (drawer ? $('drawer-close') : dialog.querySelector('[data-initial-focus="true"]') || $('dialog-title')).focus({preventScroll:true});
}
function closeDialog() {
  if (busy || drawerClosing) return;
  const finish = () => {
    const pending = modal; modal = null;
    dialog.close(); drawerClosing = false;
    if (pending) pending(false);
    const target = returnFocus?.isConnected ? returnFocus : document.querySelector(`nav [data-view="${view}"]`);
    target?.focus({preventScroll:true});
  };
  if (dialog.dataset.drawer === 'true' && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
    drawerClosing = true;
    dialog.animate([{transform:'translateX(0)'},{transform:'translateX(100%)'}],{duration:180,easing:'ease-in'}).finished.then(finish,finish);
  } else finish();
}
$('drawer-close').addEventListener('click',closeDialog);
function confirmAction({title, description, body = '', label = '确认', danger = false}) {
  if (busy || dialog.open) return Promise.resolve(false);
  return new Promise(resolve => {
    modal = resolve;
    showDialog({state:'confirm',title,description,body,buttons:[
      {label:'取消',focus:true,action:closeDialog},
      {label,primary:true,danger,action:() => { if (!modal) return; const accept = modal; modal = null; accept(true); }},
    ]});
  });
}
function resultDialog(state, title, description, body = '') {
  showDialog({state,title,description,body,buttons:[{label:state === 'failure' ? '关闭' : '完成',primary:true,focus:true,action:closeDialog}]});
}
dialog.addEventListener('cancel', event => { event.preventDefault(); if (!busy) closeDialog(); });
dialog.addEventListener('keydown', event => {
  if (event.key !== 'Tab') return;
  const controls = [...dialog.querySelectorAll('button:not(:disabled), summary, [href], input:not(:disabled)')].filter(node => node.tabIndex !== -1 && node.getClientRects().length);
  if (!controls.length) { event.preventDefault(); $('dialog-title').focus(); return; }
  const first = controls[0], last = controls[controls.length - 1];
  if (event.shiftKey && (document.activeElement === first || !controls.includes(document.activeElement))) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && (document.activeElement === last || !controls.includes(document.activeElement))) { event.preventDefault(); first.focus(); }
});
async function request(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-PAL-Action':'confirm'},body:JSON.stringify(body)});
  let payload;
  try { payload = await response.json(); } catch (_) { throw new Error('服务返回了无效响应，请刷新状态后确认。'); }
  if (!response.ok) throw new Error(payload.error || `请求失败（${response.status}）`);
  return payload;
}
function acceptStatus(next) {
  data = next; stateValid = true; stateError = ''; render();
}
function invalidate(error) { stateValid = false; stateError = error.message; render(); }
async function execute({title, description, path, body, production = false, successTitle = '操作已完成'}) {
  if (busy) return;
  busy = true; monitorRequest++; monitoring = false; monitor = null; renderHealth();
  showDialog({state:'working',title,description});
  const minimumVisible = delay(850);
  let completed = false;
  try {
    const result = await request(path, body);
    completed = true;
    const next = result.status || (body === undefined ? result : await request('/api/status'));
    acceptStatus(next);
    if (result.monitor) { monitor = result.monitor; monitorError = ''; render(); }
    else await refreshMonitor(true);
    await minimumVisible;
    const checkFailed = path === '/api/doctor' && !monitorHealthy();
    resultDialog(checkFailed ? 'info' : 'success',checkFailed ? '检查完成 · 有项目需处理' : successTitle,result.message || '已读取最新状态。');
  } catch (error) {
    await minimumVisible;
    invalidate(error);
    resultDialog('failure',completed ? '操作已完成，状态刷新失败' : '操作未完成',
      completed ? '服务已确认本次操作完成。请关闭提示后刷新状态，确认最新结果；无需重复提交。' : production ? '尚未确认操作结果，请先刷新或检查状态再决定下一步。' : '请根据以下原因处理后重试。',
      `<div class="notice error" role="alert">${esc(error.message)}</div>`);
  } finally { busy = false; renderHealth(); }
}
function versionRefs(unit) {
  return {
    development: unit.development_revision_id ?? unit.current_revision_id ?? null,
    production: unit.production_revision_id ?? unit.active_revision_id ?? null,
    mounted: unit.mounted_revision_id ?? null,
  };
}
function shortRevision(revision) {
  const value = String(revision ?? '');
  return value.length > 18 ? `${value.slice(0,10)}…${value.slice(-6)}` : value;
}
function unpublished(unit) {
  const r = versionRefs(unit);
  return !unit.pending_deletion && r.development !== null && r.development !== r.production;
}
function unsynced(unit) {
  const r = versionRefs(unit);
  return !!unit.pending_deletion || (unit.mount_enabled === false ? r.mounted !== null : r.production !== r.mounted);
}
function hasVersionDrift(unit) { return unpublished(unit) || unsynced(unit); }
function versionSummary(unit) {
  const r = versionRefs(unit);
  if (unit.pending_deletion) return '删除待完成';
  if (unit.mount_enabled === false && r.mounted !== null) return 'CLI 待卸载';
  if (r.development !== null && r.development !== r.production) return r.production === null ? '尚未发布' : '开发有更新';
  if (unit.mount_enabled === false && r.mounted === null) return '已卸载 · 生产保留';
  if (r.production === null && r.mounted !== null) return 'CLI 待卸载';
  if (r.production !== r.mounted) return r.mounted === null ? '生产已发布 · 尚未挂载' : '生产已更新 · CLI 待同步';
  if (r.production !== null && r.development === null) return '生产保留 · 无开发副本';
  return mountHealthy() ? '三层内容一致' : '三层记录一致 · 安装待核查';
}
function skillButton(action, unit, label, primary = false) {
  return `<button class="${primary ? 'primary' : 'small'}" data-action="skill-${action}" data-unit="${esc(unit.unit_id)}" ${data.mount.recovery_required ? 'disabled' : ''}>${esc(label)}</button>`;
}
function cliSkillState(unit, cli) {
  const target = monitorCurrent() ? monitor.targets?.find(item => item.cli_id === cli) : null;
  const recorded = versionRefs(unit).mounted !== null;
  if (!target) return recorded ? '有挂载记录 · 待核查' : '无挂载记录 · 待核查';
  if (!['healthy','unconfigured'].includes(target.mount.state)) return monitorLabels[target.mount.state] || '未能确认';
  return recorded ? '已挂载' : '未挂载';
}
function renderRows() {
  const visible = data.units.filter(unit => {
    const r = versionRefs(unit);
    const matches = filter === 'all' || filter === 'unpublished' && unpublished(unit) || filter === 'unsynced' && unsynced(unit);
    return matches && `${unit.unit_id} ${unit.description || ''}`.toLowerCase().includes(search.toLowerCase());
  });
  $('skill-list').innerHTML = visible.length ? visible.map(unit => {
    const r = versionRefs(unit), deleting = unit.pending_deletion, dirty = r.development !== null && r.development !== r.production;
    const devState = deleting ? '已移除' : r.development === null ? '无开发副本' : dirty ? r.production === null ? '等待首次发布' : '有未发布修改' : '与生产内容一致';
    const prodState = r.production === null ? deleting ? '已从生产移除' : '尚未发布' : r.mounted === null ? '内容保留，可挂载' : r.production !== r.mounted ? '新内容已发布' : '当前发布内容';
    const cliState = deleting || unit.mount_enabled === false && r.mounted !== null || r.production === null && r.mounted !== null ? '等待卸载' : r.mounted === null ? unit.mount_enabled === false ? '已卸载' : '尚未挂载' : r.mounted === r.production ? '与生产内容一致' : '仍在使用此前内容';
    const contentButton = source => `<button class="quiet small" data-action="skill-details" data-unit="${esc(unit.unit_id)}" data-source="${source}">查看内容与文件</button>`;
    return `<article class="skill-card" data-skill="${esc(unit.unit_id)}"><div class="skill-card-head"><div><div class="skill-heading"><button class="skill-title-link" data-action="skill-details" data-unit="${esc(unit.unit_id)}">${esc(unit.unit_id)}</button>${badge(versionSummary(unit),hasVersionDrift(unit) ? 'warn' : mountHealthy() ? 'ok' : '')}</div><p class="skill-summary">${esc(unit.description || (deleting ? '删除操作尚未全部完成。' : '用途说明尚未读取'))}</p></div><button class="small detail-entry" data-action="skill-details" data-unit="${esc(unit.unit_id)}">${icon('file')}查看详情${icon('arrow')}</button></div>
      <div class="skill-flow" aria-label="${esc(unit.unit_id)} 的三层内容">
      <section class="skill-stage" data-version="开发库"><h3><span>1</span>开发库</h3><p class="small-copy">正在修改的内容</p><strong class="stage-state">${esc(devState)}</strong>${r.development ? contentButton('development') : ''}<div class="stage-action">${dirty && !deleting ? skillButton('publish',unit,'发布到生产 →',true) : '<span class="small-copy">在 CLI 中创建和更新</span>'}</div></section>
      <section class="skill-stage" data-version="生产库"><h3><span>2</span>生产库</h3><p class="small-copy">已发布的内容</p><strong class="stage-state">${esc(prodState)}</strong>${r.production ? contentButton('production') : ''}<div class="stage-action">${r.production && !deleting && (r.production !== r.mounted || unit.mount_enabled === false) ? skillButton('mount',unit,r.mounted === null ? '挂载到 CLI →' : '同步更新到 CLI →',true) : '<span class="small-copy">发布后可手动挂载</span>'}</div></section>
      <section class="skill-stage" data-version="CLI 挂载"><h3><span>3</span>CLI 挂载库</h3><p class="small-copy">上次同步内容 · 逐端核查安装</p><strong class="stage-state">${esc(cliState)}</strong><div class="skill-targets">${data.library.target_clis.map(cli => `<div><span>${esc(cliName(cli))}</span><span>${esc(cliSkillState(unit,cli))}</span></div>`).join('')}</div>${r.mounted ? contentButton('mounted') : ''}<div class="stage-action">${deleting || r.mounted !== null ? skillButton('unmount',unit,deleting ? '完成 CLI 卸载' : '从 CLI 卸载',deleting) : '<span class="small-copy">挂载后在新 CLI 会话使用</span>'}</div></section>
      </div>${deleting ? '<p class="skill-manage-note">开发和生产已移除。CLI 清理完成后，这个 Skill 才退出列表。</p>' : `<details class="skill-manage"><summary>管理此 Skill</summary>${r.development && r.production && dirty ? `<div><p>放弃未发布修改，开发内容恢复到当前生产版。</p>${skillButton('discard',unit,'放弃未发布修改')}</div>` : ''}<div><p>让整个 Skill 退出管理，并明确清理各层内容。</p><button class="small danger" data-action="skill-delete" data-unit="${esc(unit.unit_id)}">删除 Skill</button></div></details>`}
      <details class="skill-technical"><summary>技术信息</summary><dl class="data-list">${[['开发库',r.development],['生产库',r.production],['CLI 挂载',r.mounted]].map(([label,revision])=>`<div><dt>${esc(label)} revision</dt><dd><code>${esc(revision || '无')}</code></dd></div>`).join('')}</dl></details></article>`;
  }).join('') : `<div class="empty">${icon('search')}<strong>${data.units.length ? '没有符合条件的 Skill' : '从第一个 Skill 开始'}</strong><p>${data.units.length ? '试试其他关键词或筛选条件。' : '在 Claude Code 或 Codex 中创建并提交后，回到这里刷新。'}</p></div>`;
}
function renderCollection() {
  const steps = [['创建或修改 Skill','在 Claude Code / Codex 中完成','开发库','手动发布'],['发布到生产','保存已确认的内容，等待同步','生产库','手动同步'],['同步后使用','同步到 CLI 后，在新会话中使用','CLI 挂载库','']];
  const guide = `<section class="stage-guide" aria-labelledby="stage-guide-title"><div class="stage-guide-heading"><h2 id="stage-guide-title">Skill 使用流程 · 3 个步骤</h2><p>按顺序推进，发布与同步都由你手动触发。</p></div><ol class="stage-steps" role="list">${steps.map(([title,description,library,action],index)=>`<li class="stage-step"><span class="stage-number" aria-hidden="true">${index+1}</span><div class="stage-step-copy"><h3>${title}</h3><p>${description}</p><span class="stage-library">${library}</span></div>${action ? `<span class="stage-connector"><span>${action}</span><svg viewBox="0 0 50 18" aria-hidden="true"><path d="M1 9h46m-6-6 6 6-6 6"/></svg></span>` : ''}</li>`).join('')}</ol></section>`;
  return `${guide}<section><div class="list-tools"><label class="search">${icon('search')}<input id="skill-search" type="search" aria-label="搜索 Skill" placeholder="搜索名称或用途" value="${esc(search)}"></label><div class="filters" aria-label="筛选 Skill">${[['all','全部'],['unpublished','未发布'],['unsynced','未同步']].map(([key,label])=>`<button data-filter="${key}" aria-pressed="${filter === key}">${label}</button>`).join('')}</div><p class="small-copy">${filter === 'unpublished' ? '尚未发布的新 Skill，以及有未发布修改的 Skill。' : filter === 'unsynced' ? '等待挂载、更新或卸载的内容；已主动卸载且清理完成的不在此列。' : '查看详情了解用途与文件；发布和同步都只影响明确选择的内容。'}</p></div><div id="skill-list"></div></section>`;
}
function syncSummary() {
  const mount = data.mount;
  const label = mount.recovery_required ? '操作未完成 · 需恢复' : mount.sync_required ? 'CLI 有待同步变化' : mountHealthy() ? 'CLI 挂载符合当前选择' : '挂载记录已读取 · 安装待核查';
  return `<div class="section-footer sync-summary"><div><strong>${esc(label)}</strong><p class="small-copy">同步已启用的生产内容，完成待卸载项；已卸载的 Skill 保留停用。未发布修改不会带入。</p></div><button data-action="sync" ${!data.production.active_version_id || mount.recovery_required ? 'disabled' : ''}>同步全部待处理项</button></div>`;
}
const monitorLabels = {healthy:'校验通过',outdated:'版本需更新',missing:'尚未安装',disabled:'未启用',drift:'内容不一致','source-drift':'源文件异常',conflict:'来源冲突',unknown:'未能确认',unconfigured:'尚未同步', 'recovery-required':'需处理恢复'};
function monitorCurrent() { return monitor && Date.now() - new Date(monitor.checked_at).getTime() < 90000 && monitor.production_version_id === data?.production.active_version_id && monitor.mounted_version_id === data?.mount.version_id; }
function monitoredTargets() { return monitorCurrent() && data.library.target_clis.every(cli => monitor.targets?.some(target => target.cli_id === cli)); }
function mountHealthy() { return monitoredTargets() && monitor.targets.every(target => ['healthy','unconfigured'].includes(target.mount.state)); }
function monitorHealthy() { return mountHealthy() && monitor.targets.every(target => target.creation.state === 'healthy'); }
function monitorCard(target, kind) {
  const item = target[kind], creation = kind === 'creation', healthy = item.state === 'healthy';
  const actionLabel = creation ? item.state === 'outdated' ? '更新系统入口' : item.state === 'missing' ? '安装系统入口' : '修复系统入口' : '修复现有挂载';
  const hint = healthy ? '已通过检查' : item.state === 'unconfigured' ? '发布后可手动同步' : item.state === 'source-drift' ? '请先恢复源文件，自动修复不可用' : '检查后按结果处理';
  return `<section class="monitor-card" data-monitor-kind="${kind}" data-cli="${esc(target.cli_id)}"><div class="skill-heading"><h3>${esc(cliName(target.cli_id))}</h3>${badge(monitorLabels[item.state] || '未能确认',healthy ? 'ok' : item.state === 'unconfigured' ? '' : 'warn')}</div>${creation ? `<dl class="monitor-versions"><div><dt>已安装版本</dt><dd><code>${esc(item.installed_version || '未确认')}</code></dd></div><div><dt>当前所需版本</dt><dd><code>${esc(item.expected_version || '待核查')}</code></dd></div></dl>` : ''}<p class="monitor-message">${esc(item.message || (healthy ? '安装内容已通过校验。' : '等待安装核查。'))}</p><div class="card-footer">${item.repairable ? `<button data-action="repair-installation" data-kind="${kind}" data-cli="${esc(target.cli_id)}" ${!monitorCurrent() || monitoring ? 'disabled' : ''}>${icon('refresh')}${esc(actionLabel)}</button>` : `<span class="small-copy">${icon(healthy ? 'check' : 'help')}${hint}</span>`}</div></section>`;
}
function renderSync() {
  const current = monitorCurrent();
  const targets = current ? monitor.targets : data.library.target_clis.map(cli_id => ({cli_id,creation:{state:'unknown',message:'等待安装核查'},mount:{state:'unknown',message:'等待安装核查'}}));
  const issues = targets.reduce((count,target) => count + Number(target.creation.state !== 'healthy') + Number(!['healthy','unconfigured'].includes(target.mount.state)),0);
  const label = monitoring ? '正在检查' : monitorError ? '检查失败' : !current ? '等待核查' : issues ? `${issues} 项需处理` : '检查通过';
  return `<div class="management-page"><section class="panel management-summary" aria-label="安装监测摘要"><div class="section-heading"><span class="section-icon">${monitoring ? '<span class="spinner" aria-hidden="true"></span>' : icon(issues || monitorError ? 'alert' : 'check')}</span><div><div class="skill-heading"><h2>安装监测</h2>${badge(label,monitoring ? 'blue' : !current || issues || monitorError ? 'warn' : 'ok')}</div><p>${monitor ? `上次完成：${esc(time(monitor.checked_at))}` : '尚未完成检查'} · 页面打开时及每分钟自动核查</p></div></div><button data-action="monitor" ${monitoring ? 'disabled' : ''}>${icon('refresh')}${monitoring ? '正在检查' : '重新检查安装'}</button></section>${monitorError ? `<div class="notice error" role="alert">${esc(monitorError)}</div>` : ''}<section class="panel"><div class="panel-head"><div class="section-heading"><span class="section-icon">${icon('skill')}</span><div><h2>系统创建入口</h2><p>PAL 提供的 pal-create-skill，用于在 CLI 中创建与更新 Skill。</p></div></div></div><div class="management-grid">${targets.map(target => monitorCard(target,'creation')).join('')}</div></section><section class="panel"><div class="panel-head"><div class="section-heading"><span class="section-icon">${icon('grid')}</span><div><h2>业务 Skill 挂载</h2><p>核查两端上次同步的内容；修复现有安装或手动同步新的生产内容。</p></div></div></div><div class="management-grid">${targets.map(target => monitorCard(target,'mount')).join('')}</div>${syncSummary()}</section><p class="management-note">${icon('help')}检查覆盖安装文件与启用状态。更新系统入口或同步 Skill 后，请打开新的 CLI 会话使用。</p></div>`;
}
async function refreshMonitor(duringOperation = false) {
  if (!data || !stateValid || monitoring || busy && !duringOperation) return;
  const id = ++monitorRequest;
  monitoring = true; monitorError = ''; renderHealth();
  if (view === 'sync') $('content').innerHTML = renderSync();
  try { const next = await request('/api/monitor'); if (id !== monitorRequest) return; monitor = next; }
  catch (error) { if (id !== monitorRequest) return; monitor = null; monitorError = error.message; }
  finally { if (id === monitorRequest) { monitoring = false; renderHealth(); if (view === 'sync') $('content').innerHTML = renderSync(); else if (view === 'collection' && stateValid) renderRows(); } }
}
async function repairInstallation(kind, cli_id) {
  if (!monitorCurrent() || monitoring) return;
  const item = monitor.targets.find(target => target.cli_id === cli_id)?.[kind];
  if (!item?.repairable) return;
  const creation = kind === 'creation', title = creation ? '更新或修复系统创建入口' : '修复现有 CLI 挂载';
  const description = creation ? `为 ${cliName(cli_id)} 安装当前 PAL 所需的系统创建入口 ${item.expected_version}。完成后打开新会话。` : `恢复 ${cliName(cli_id)} 上次同步的业务内容。当前生产中尚未同步的内容需要另行同步。`;
  if (!await confirmAction({title:`${title}？`,description,label:'确认修复'})) return;
  await execute({title:`正在${title}`,description:'正在通过官方 CLI 修复并重新校验，请保持页面打开。',path:'/api/installations/repair',body:{kind,cli_id,expected_version:creation ? item.expected_version : data.mount.version_id},successTitle:'修复并核验完成'});
}
function maintenanceTasks() {
  return `<section class="panel"><div class="panel-head"><div class="section-heading"><span class="section-icon">${icon('settings')}</span><div><h2>维护操作</h2><p>日常检查与异常处理，可查看执行进度和结果。</p></div></div></div><div class="management-grid"><section class="task-card"><span class="section-icon">${icon('check')}</span><h3>完整性检查</h3><p>分别校验开发、生产及上次挂载的内容，并核查 CLI 安装。</p><div class="card-footer"><button data-action="doctor">${icon('check')}检查状态</button></div></section><section class="task-card"><span class="section-icon">${icon('undo')}</span><h3>异常恢复</h3><p>继续处理上次未完成的同步或清理事务；操作中断时使用。</p><div class="card-footer"><button data-action="recover">${icon('refresh')}处理异常恢复</button></div></section></div></section>`;
}
function renderMaintenance() {
  const counts = [['开发库','development','创建与修改的内容'],['生产库','production','已经发布的内容'],['CLI 挂载库','mounted','上次同步的内容']];
  return `<div class="management-page"><section class="panel"><div class="panel-head"><div class="section-heading"><span class="section-icon">${icon('skill')}</span><div><h2>外挂库信息</h2><p>当前管理的库与 CLI 创建时默认写入的位置。</p></div></div>${badge('本地')}</div><div class="panel-body"><dl class="data-list library-facts"><div><dt>库名称</dt><dd>${esc(data.library.library_id)}</dd></div><div><dt>目标 CLI</dt><dd>${data.library.target_clis.map(cli => esc(cliName(cli))).join(' / ')}</dd></div><div class="wide"><dt>保存位置</dt><dd><code>${esc(data.library.library_root)}</code></dd></div><div class="wide"><dt>默认创建库</dt><dd>${data.default_binding.matches_selected ? '当前外挂库' : data.default_binding.configured ? `<code>${esc(data.default_binding.library_root)}</code>` : '尚未配置'}</dd></div></dl></div></section><section class="panel"><div class="panel-head"><div class="section-heading"><span class="section-icon">${icon('grid')}</span><div><h2>三层内容</h2><p>查看各层记录中的 Skill 数量，发布与同步由你手动推进。</p></div></div></div><div class="panel-body"><dl class="library-stages">${counts.map(([label,key,description])=>`<div><dt>${label}</dt><dd><strong>${data.units.filter(unit=>versionRefs(unit)[key] !== null).length}</strong><span>个 Skill</span></dd><p class="small-copy">${description}</p></div>`).join('')}</dl><p class="small-copy library-stage-note">CLI 挂载数量来自同步记录，实际安装状态请在「CLI 与系统」查看。</p></div><div class="section-footer"><p class="small-copy">查看单个 Skill 的用途、文件及三层内容差异。</p><button data-view="collection">前往 Skill 集合${icon('arrow')}</button></div></section>${maintenanceTasks()}<p class="management-note">${icon('help')}创建、修改和使用 Skill 请进入 Claude Code / Codex。使用指引在顶部「使用说明」。</p></div>`;
}
function renderHealth() {
  const initial = !data && !stateError, installationIssue = !monitorHealthy();
  const label = busy ? '操作进行中' : initial ? '正在读取' : !stateValid ? '状态异常 · 待检查' : data?.mount.recovery_required ? '同步需恢复' : monitoring ? '正在核查安装' : monitorError ? '安装检查失败' : !monitorCurrent() ? '安装状态待核查' : installationIssue ? '安装有异常 · 请处理' : data?.mount.sync_required ? '生产已发布 · 待同步' : '状态正常';
  const waiting = busy || initial || monitoring, warning = data?.mount.sync_required || data?.mount.recovery_required || installationIssue;
  $('health').className = `health ${waiting ? 'wait' : !stateValid ? 'bad' : warning ? 'warn' : ''}`;
  $('health').innerHTML = `${waiting ? '<span class="spinner" aria-hidden="true"></span>' : icon(!stateValid || warning ? 'alert' : 'check')}${esc(label)}`;
  $('health').title = '点击查看系统入口和 CLI 挂载检查结果';
}
function renderTheme() {
  const dark = document.documentElement.dataset.theme === 'dark';
  const label = dark ? '切换到日间模式' : '切换到夜间模式';
  $('theme-toggle').innerHTML = icon(dark ? 'sun' : 'moon');
  $('theme-toggle').setAttribute('aria-label',label); $('theme-toggle').title = label;
  $('theme-toggle').setAttribute('aria-pressed',String(dark));
}
function toggleTheme() {
  document.documentElement.dataset.theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem('pal-theme',document.documentElement.dataset.theme); } catch (_) {}
  renderTheme();
}
renderTheme();
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', event => {
  let saved; try { saved = localStorage.getItem('pal-theme'); } catch (_) {}
  if (!['light','dark'].includes(saved)) { document.documentElement.dataset.theme = event.matches ? 'dark' : 'light'; renderTheme(); }
});
function render() {
  const titles = {skill:[skillPage?.unit_id || 'Skill 详情','查看用途与实际交付文件。'],collection:['我的 Skill','同一个 Skill：开发中修改，发布后保存到生产，再手动挂载给 CLI 使用。'],sync:['CLI 与系统','监测系统创建入口与业务 Skill 挂载，按需更新或修复。'],maintenance:['库与维护','查看外挂库信息，检查状态或处理未完成的事务。']};
  $('page-title').textContent = titles[view][0]; $('page-description').textContent = titles[view][1];
  document.querySelectorAll('[data-view]').forEach(button => { if (button.dataset.view === (view === 'skill' ? 'collection' : view)) button.setAttribute('aria-current','page'); else button.removeAttribute('aria-current'); });
  $('library-name').textContent = data?.library.library_id || '外挂库';
  renderHealth();
  $('content').setAttribute('aria-busy','false');
  if (!data && !stateError) {
    renderHealth();
    $('content').setAttribute('aria-busy','true');
    $('content').innerHTML = '<div class="empty"><span class="spinner" aria-hidden="true"></span><p>正在读取外挂库…</p></div>';
    return;
  }
  if (!stateValid) {
    $('content').innerHTML = `<div class="notice error" role="alert"><strong>暂时无法确认最新状态</strong><p>${esc(stateError)}</p><p>读取成功前暂停 Skill 操作；你可以刷新状态或进行检查。</p></div>${maintenanceTasks()}`;
    return;
  }
  if (view === 'skill') { $('content').innerHTML = renderSkillPage(); return; }
  $('content').innerHTML = view === 'collection' ? renderCollection() : view === 'sync' ? renderSync() : renderMaintenance();
  if (view === 'collection') renderRows();
}
function focusRow(unitId, previousAction, source = '') {
  const row = [...document.querySelectorAll('[data-skill]')].find(node => node.dataset.skill === unitId);
  const button = (source ? row?.querySelector(`[data-action="${previousAction}"][data-source="${source}"]`) : null) || row?.querySelector(`[data-action="${previousAction}"]`) || row?.querySelector('[data-action="undo"]') || row?.querySelector('button') || $('skill-search');
  button?.focus({preventScroll:true});
}
async function syncCurrent() {
  if (!stateValid || !data.production.active_version_id || data.mount.recovery_required) return;
  const version = data.production.active_version_id;
  const updates = data.units.map(unit => {
    const desired = unit.mount_enabled === false ? null : unit.production_revision_id;
    if (!unit.pending_deletion && desired === unit.mounted_revision_id) return '';
    const action = unit.pending_deletion ? '完成删除：卸载 CLI 内容' : !desired ? '从 CLI 卸载' : unit.mounted_revision_id ? '同步生产更新' : '挂载已发布内容';
    return `<li><strong>${esc(unit.unit_id)}</strong><span>${action}</span></li>`;
  }).filter(Boolean);
  const body = updates.length ? `<ul class="change-list">${updates.join('')}</ul>` : '<p class="small-copy">内容已同步；本次重新校验现有安装。</p>';
  if (!await confirmAction({title:'同步全部待处理项？',description:`同步已启用 Skill 的当前生产内容，并完成待卸载项。已卸载的 Skill 保持停用；未发布开发内容不会带入。完成后请打开新 CLI 会话。`,body,label:'确认同步'})) return;
  await execute({title:'正在同步 CLI',description:'正在安装当前生产内容。',path:'/api/sync',body:{version_id:version},production:true,successTitle:'CLI 同步完成'});
}
async function refresh() {
  if (busy || dialog.open) return;
  if (view === 'skill') { await skillDetails(skillPage.unit_id); return; }
  await execute({title:'正在刷新状态',description:'正在读取外挂库与当前生产集合。',path:'/api/status',successTitle:'状态已刷新'});
}
async function skillAction(action, unitId) {
  if (busy || dialog.open || !stateValid) return;
  let plan;
  busy = true;
  showDialog({state:'working',title:'正在核对操作范围',description:'读取开发、生产和 CLI 挂载基线。'});
  try { [plan] = await Promise.all([request('/api/skill-actions/preview',{action,unit_id:unitId}),delay(350)]); }
  catch (error) { busy = false; resultDialog('failure','暂时无法执行',error.message); return; }
  busy = false; dialog.close();
  const current = revision => revision ? shortRevision(revision) : '无';
  const impacts = {
    publish:['保留开发内容','更新为当前开发内容','保持现状，之后手动同步'],
    mount:['保持现状','保留已发布内容','安装此 Skill 的当前生产内容'],
    unmount:['保持现状','保留已发布内容','从 Claude Code、Codex 卸载此 Skill'],
    discard:['恢复到当前生产内容','保持现状','保持现状'],
    delete:['删除开发内容','从当前生产移除',plan.mounted_revision_id ? '等待你手动完成卸载，条目继续保留' : '没有此 Skill 的挂载记录'],
  };
  const labels = {publish:'发布到生产',mount:plan.mounted_revision_id ? '同步更新到 CLI' : '挂载到 CLI',unmount:plan.pending_deletion ? '完成 CLI 卸载' : '从 CLI 卸载',discard:'放弃未发布修改',delete:'删除 Skill'};
  const label = labels[action];
  const notes = {publish:'本次只发布，不自动同步 CLI。',mount:'只同步此 Skill；其他 Skill 的挂载内容保持原状。完成后请打开新 CLI 会话。',unmount:plan.pending_deletion ? '确认两端卸载后，此 Skill 才退出列表。' : '卸载后 Skill 和生产内容仍留在库中，可重新挂载。当前会话已加载的内容不会被撤回。',discard:'未发布修改会被当前生产内容替换。',delete:'内部既有证据按原规则保留。CLI 尚有安装时，删除后请继续完成卸载。'};
  const body = `<dl class="action-impact">${['开发库','生产库','CLI 挂载库'].map((name,index)=>`<div><dt>${name}</dt><dd>${esc(impacts[action][index])}</dd></div>`).join('')}</dl><details class="technical-info"><summary>本次内容基线</summary><p>开发 ${esc(current(plan.development_revision_id))}<br>生产 ${esc(current(plan.production_revision_id))}<br>CLI ${esc(current(plan.mounted_revision_id))}</p></details>`;
  if (!await confirmAction({title:`${label} · ${unitId}`,description:notes[action],body,label:`确认${action === 'discard' ? '放弃修改' : action === 'delete' ? '删除 Skill' : action === 'publish' ? '发布' : action === 'unmount' ? '卸载' : '同步'}`,danger:['delete','discard'].includes(action)})) return;
  await execute({title:`正在${label}`,description:'正在执行已确认的操作，请保持页面打开。',path:'/api/skill-actions/execute',body:{action,unit_id:unitId,token:plan.token},production:true,successTitle:action === 'delete' && plan.mounted_revision_id ? '删除待完成 · 请继续卸载 CLI' : `${label}已完成`});
}
const cliName = cli => cli === 'claude-code' ? 'Claude Code' : cli === 'codex' ? 'Codex' : cli;
const fileSize = bytes => bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
const selectedSource = () => skillPage?.document?.[skillPage.source];
const selectedArtifact = () => selectedSource()?.artifacts[skillPage.artifact];
function backToCollection() {
  const unitId = skillPage?.unit_id, originSource = skillPage?.originSource, originEntry = skillPage?.originEntry;
  detailRequest++; fileRequest++; view = 'collection'; render(); focusRow(unitId,'skill-details',originSource);
  if (!originSource) [...document.querySelectorAll('[data-skill]')].find(node=>node.dataset.skill===unitId)?.querySelector(originEntry === 'name' ? '.skill-title-link' : '.detail-entry')?.focus({preventScroll:true});
}
async function skillDetails(unitId, preferredSource = '') {
  const token = ++detailRequest; fileRequest++;
  skillPage = {unit_id:unitId,originSource:preferredSource,originEntry:document.activeElement?.classList.contains('skill-title-link') ? 'name' : 'details',loading:true,source:'production',artifact:0,tab:'overview',path:null,file:null,fileLoading:false,fileError:'',fileMode:'preview'};
  view = 'skill'; render(); $('page-title').setAttribute('tabindex','-1'); $('page-title').focus({preventScroll:true});
  try {
    const document = await request(`/api/skills/${encodeURIComponent(unitId)}`);
    if (token !== detailRequest || view !== 'skill') return;
    const defaultSource = unpublished(data.units.find(unit => unit.unit_id === unitId) || {}) ? 'development' : 'production';
    skillPage.document = document; skillPage.source = document[preferredSource] ? preferredSource : document[defaultSource] ? defaultSource : document.development ? 'development' : document.production ? 'production' : 'mounted';
  } catch (error) { if (token === detailRequest) skillPage.error = error.message; }
  finally { if (token === detailRequest && view === 'skill') { skillPage.loading = false; render(); } }
}
function fileTreeHTML(files) {
  const root = {children:new Map()};
  files.forEach(file => {
    let node = root;
    file.display_path.split('/').forEach((name,index,parts) => {
      if (!node.children.has(name)) node.children.set(name,{name,children:new Map()});
      node = node.children.get(name); if (index === parts.length-1) node.file = file;
    });
  });
  const nodes = parent => [...parent.children.values()].sort((a,b) => Number(!!a.file)-Number(!!b.file) || a.name.localeCompare(b.name)).map(node => node.file ? `<li><button data-file-path="${esc(node.file.path)}" aria-current="${skillPage.path === node.file.path}" title="${esc(node.file.display_path)}">${icon('file')}<span>${esc(node.name)}</span></button></li>` : `<li><details open class="file-folder"><summary>${esc(node.name)}</summary><ul>${nodes(node)}</ul></details></li>`).join('');
  return `<ul class="file-tree" aria-label="Skill 文件目录">${nodes(root)}</ul>`;
}
function renderSkillPage() {
  const back = `<button class="quiet small detail-back" data-action="skill-back">${icon('undo')}返回 Skill 集合</button>`;
  if (skillPage.loading) return `${back}<section class="panel empty" aria-busy="true"><span class="spinner" aria-hidden="true"></span><p>正在读取 Skill 详情…</p></section>`;
  if (skillPage.error) return `${back}<section class="panel panel-body"><div class="notice error" role="alert">${esc(skillPage.error)}</div><button data-action="skill-reload">重新读取详情</button></section>`;
  const source = selectedSource(), artifact = selectedArtifact();
  const same = skillPage.document.production?.revision_id === skillPage.document.development?.revision_id;
  const sourceNote = skillPage.source === 'production' ? '当前已发布内容 · 同步后供 CLI 使用' : skillPage.source === 'mounted' ? 'CLI 挂载内容 · 新会话使用的版本' : same ? '开发内容 · 与生产一致' : '开发内容 · 尚未发布';
  return `${back}<div class="skill-page"><div class="detail-toolbar"><label>内容来源<select id="skill-source" aria-label="内容来源">${[['production','生产版'],['mounted','CLI 挂载版'],['development','开发版']].filter(([key]) => skillPage.document[key]).map(([key,label]) => `<option value="${key}" ${skillPage.source === key ? 'selected' : ''}>${label}</option>`).join('')}</select></label>${source.artifacts.length > 1 ? `<label>CLI 适配<select id="skill-artifact" aria-label="CLI 适配">${source.artifacts.map((item,index) => `<option value="${index}" ${index === skillPage.artifact ? 'selected' : ''}>${esc(item.covered_clis.map(cliName).join(' / '))}</option>`).join('')}</select></label>` : `<div class="detail-clis">${artifact.covered_clis.map(cli => badge(cliName(cli),'blue')).join(' ')}</div>`}<span class="small-copy">${esc(sourceNote)}</span></div><section class="panel"><div class="detail-tabs" role="tablist" aria-label="Skill 详情视图">${[['overview','概览'],['files','文件']].map(([key,label])=>`<button id="skill-tab-${key}" role="tab" data-skill-tab="${key}" aria-selected="${skillPage.tab === key}" aria-controls="skill-detail-panel" tabindex="${skillPage.tab === key ? '0' : '-1'}">${label}${key === 'files' ? ` · ${artifact.files.length}` : ''}</button>`).join('')}</div><div id="skill-detail-panel" role="tabpanel" aria-labelledby="skill-tab-${skillPage.tab}">${skillPage.tab === 'overview' ? `<div class="overview"><p class="eyebrow">用途</p><p class="overview-description">${esc(artifact.description)}</p><article class="markdown">${artifact.markdown_html || `<p>${esc(artifact.instructions)}</p>`}</article><details class="technical-info"><summary>技术信息</summary><dl class="data-list"><div><dt>Revision</dt><dd><code>${esc(source.revision_id)}</code></dd></div><div><dt>产物</dt><dd><code>${esc(artifact.artifact_id)}</code></dd></div><div><dt>适配规范</dt><dd><code>${esc(artifact.profile_id)}</code></dd></div></dl></details></div>` : `<div class="file-layout"><aside class="file-sidebar"><h3>文件目录</h3>${fileTreeHTML(artifact.files)}</aside><section class="file-viewer" aria-label="文件内容">${renderFileViewer()}</section></div>`}</div></section></div>`;
}
function renderFileViewer() {
  const artifact = selectedArtifact(), selected = artifact.files.find(file => file.path === skillPage.path), file = skillPage.file;
  if (!selected) return '<div class="empty">选择左侧文件查看内容。</div>';
  const controls = file?.kind === 'markdown' ? `<div class="filters" aria-label="Markdown 显示方式">${[['preview','预览'],['source','源码']].map(([mode,label])=>`<button class="small" data-file-mode="${mode}" aria-pressed="${skillPage.fileMode === mode}">${label}</button>`).join('')}</div>` : '';
  let body;
  if (skillPage.fileLoading) body = '<div class="empty" role="status"><span class="spinner" aria-hidden="true"></span><p>正在读取文件…</p></div>';
  else if (skillPage.fileError) body = `<div class="notice error" role="alert">${esc(skillPage.fileError)}</div><button data-action="file-retry">重试读取</button><p class="small-copy">若版本已变化，请刷新详情后再查看。</p>`;
  else if (!file) body = '';
  else if (['binary','large'].includes(file.kind)) body = `<div class="empty">${icon('file')}<strong>无法在线预览</strong><p>${esc(file.message)}</p></div>`;
  else if (file.kind === 'markdown' && skillPage.fileMode === 'preview') body = `<article class="markdown">${file.markdown_html}</article>`;
  else body = `<div class="source-code" aria-label="文件源码">${file.content.split('\n').map(line=>`<div class="source-line"><code>${esc(line) || ' '}</code></div>`).join('')}</div>`;
  return `<div class="file-bar"><div class="file-path"><strong>${esc(selected.display_path)}</strong><p class="small-copy">${fileSize(selected.size)} · 只读</p></div>${controls}</div><div class="file-content" aria-busy="${skillPage.fileLoading}">${body}</div><div class="file-metadata"><details><summary>文件信息</summary><p>SHA256 <code>${esc(selected.sha256)}</code></p></details></div>`;
}
function updateFileViewer() {
  const viewer = document.querySelector('.file-viewer');
  if (viewer) viewer.innerHTML = renderFileViewer();
  document.querySelectorAll('[data-file-path]').forEach(button => button.setAttribute('aria-current',String(button.dataset.filePath === skillPage.path)));
}
async function openSkillFile(path) {
  const artifact = selectedArtifact();
  if (!artifact?.files.some(file => file.path === path)) return;
  const token = ++fileRequest, pageToken = detailRequest;
  skillPage.path = path; skillPage.file = null; skillPage.fileError = ''; skillPage.fileLoading = true; skillPage.fileMode = 'preview';
  updateFileViewer();
  const selectedButton = [...document.querySelectorAll('[data-file-path]')].find(button => button.dataset.filePath === path);
  for (let parent = selectedButton?.parentElement; parent && !parent.classList.contains('file-sidebar'); parent = parent.parentElement) { if (parent.tagName === 'DETAILS') parent.open = true; }
  if (selectedButton) { const sidebar = selectedButton.closest('.file-sidebar'); if (sidebar.scrollHeight > sidebar.clientHeight) sidebar.scrollTop = selectedButton.offsetTop - sidebar.offsetTop - 45; }
  const source = selectedSource();
  const query = new URLSearchParams({unit_id:skillPage.unit_id,source:skillPage.source,revision_id:source.revision_id,version_id:source.version_id || '',artifact_id:artifact.artifact_id,path});
  try {
    const file = await request(`/api/skill-file?${query}`);
    if (token !== fileRequest || pageToken !== detailRequest || view !== 'skill') return;
    skillPage.file = file;
  } catch (error) { if (token === fileRequest && pageToken === detailRequest) skillPage.fileError = error.message; }
  finally { if (token === fileRequest && pageToken === detailRequest && view === 'skill') { skillPage.fileLoading = false; updateFileViewer(); } }
}
async function selectSkillTab(tab) {
  skillPage.tab = tab; render(); $(`skill-tab-${tab}`).focus({preventScroll:true});
  if (tab === 'files' && !skillPage.path) await openSkillFile(selectedArtifact().canonical_path);
}
async function followDocumentLink(href) {
  const artifact = selectedArtifact();
  const currentPath = skillPage.tab === 'overview' ? artifact.canonical_path : skillPage.path;
  let resolved;
  try { resolved = new URL(href,`https://pal.invalid/${currentPath}`); } catch (_) { return; }
  let target; try { target = decodeURIComponent(resolved.pathname).slice(1); } catch (_) { resultDialog('failure','无法打开链接','文件链接格式无效。'); return; }
  if (resolved.origin !== 'https://pal.invalid' || !artifact.files.some(file => file.path === target)) {
    resultDialog('failure','无法打开链接','该链接没有指向当前 Skill 已交付的文件。'); return;
  }
  if (target !== currentPath || skillPage.tab === 'overview' && !resolved.hash) {
    skillPage.tab = 'files'; render(); await openSkillFile(target);
  }
  if (resolved.hash) {
    let slug; try { slug = decodeURIComponent(resolved.hash.slice(1)); } catch (_) { return; }
    const heading = [...document.querySelectorAll('#content .markdown h1,#content .markdown h2,#content .markdown h3,#content .markdown h4')].find(node => node.textContent.toLowerCase().trim().replace(/[^\p{L}\p{N} _-]/gu,'').replace(/\s+/g,'-') === slug);
    heading?.scrollIntoView({behavior:'smooth',block:'start'});
  }
}
function help() {
  const topic = ['sync','maintenance'].includes(view) ? 'care' : 'start';
  const topics = [['start','开始使用'],['manage','管理 Skill'],['care','检查与维护']];
  showDialog({drawer:true,title:'使用说明',description:'从创建到使用，让每一步都清楚。',body:`<div class="guide-tabs" role="tablist" aria-label="说明主题">${topics.map(([key,label])=>`<button id="guide-tab-${key}" role="tab" data-guide-topic="${key}" aria-selected="${topic === key}" aria-controls="guide-${key}" tabindex="${topic === key ? 0 : -1}">${label}</button>`).join('')}</div>
    <section id="guide-start" role="tabpanel" aria-labelledby="guide-tab-start" ${topic !== 'start' ? 'hidden' : ''}><div class="guide-lead"><span class="eyebrow">日常流程</span><h3>一份 Skill，在两个 CLI 中使用。</h3><p>在 Claude Code 或 Codex 里创建和更新，用控制台查看内容、发布和管理挂载。</p></div><ol class="guide-steps"><li><span class="step-number">1</span><div><h3>在 CLI 中创建或更新</h3><p>选择 PAL 提供的 <code>pal-create-skill</code>，告诉助手需要什么。更新时说明目标 Skill 和修改内容。</p><span class="guide-tag">完成后保存到开发库</span></div></li><li><span class="step-number">2</span><div><h3>查看内容，发布到生产</h3><p>回到控制台刷新，点击卡片顶部「查看详情」检查用途与文件。确认后点击「发布到生产」。</p><span class="guide-tag">逐个发布 · CLI 此时仍用原内容</span></div></li><li><span class="step-number">3</span><div><h3>手动同步，在新会话使用</h3><p>点击「挂载到 CLI」或「同步更新到 CLI」，再打开新的 Claude Code / Codex 会话使用 Skill。</p><span class="guide-tag">从已发布的生产内容同步</span></div></li></ol><div class="guide-tip">${icon('help')}<p>也可以在 CLI 中明确告诉助手发布或同步。单纯创建、修改和发布，都不会自动推进下一步。</p></div></section>
    <section id="guide-manage" role="tabpanel" aria-labelledby="guide-tab-manage" ${topic !== 'manage' ? 'hidden' : ''}><div class="guide-lead"><span class="eyebrow">内容与状态</span><h3>围绕同一个 Skill 管理。</h3><p>名称与「查看详情」均可打开完整详情。各层的「查看内容与文件」直接打开对应版本。</p></div><dl class="guide-rows"><div><dt>概览与文件</dt><dd>概览说明用途；文件页展开实际目录并提供只读预览。切换开发版、生产版、CLI 挂载版查看内容差异；编辑仍在 CLI 中完成。</dd></div><div><dt>未发布</dt><dd>包含尚未发布的新 Skill，以及开发库中尚未发布的修改。</dd></div><div><dt>未同步</dt><dd>包含待挂载、待更新和待卸载的内容。已主动卸载且清理完成的 Skill 不算待同步。</dd></div></dl><h3 class="guide-section-title">三个不同的管理动作</h3><dl class="guide-rows"><div><dt>从 CLI 卸载</dt><dd>停止在 CLI 中挂载，开发和生产内容保留，之后可以重新挂载。</dd></div><div><dt>放弃未发布修改</dt><dd>在「管理此 Skill」中操作：将开发内容恢复到当前生产版，生产和 CLI 不变。没有生产版时不提供此操作。</dd></div><div><dt>删除 Skill</dt><dd>移除开发和当前生产内容。仍有 CLI 挂载时显示「删除待完成」，点击「完成 CLI 卸载」后才退出列表。</dd></div></dl><div class="guide-tip">${icon('help')}<p>发布、同步和删除前都会展示影响范围。每项操作都需要你确认。</p></div></section>
    <section id="guide-care" role="tabpanel" aria-labelledby="guide-tab-care" ${topic !== 'care' ? 'hidden' : ''}><div class="guide-lead"><span class="eyebrow">状态与维护</span><h3>先看状态，再处理对应问题。</h3><p>顶部状态可点击，进入「CLI 与系统」查看两端的具体检查结果。</p></div><dl class="guide-rows"><div><dt>系统创建入口需要更新</dt><dd>更新 PAL 后，在「CLI 与系统」检查 pal-create-skill 的已安装版本，按提示更新或修复，再打开新会话。</dd></div><div><dt>业务 Skill 挂载异常</dt><dd>「修复现有挂载」恢复上次同步的内容；采用新的生产内容需手动同步。该页也可确认「同步全部待处理项」。</dd></div><div><dt>库状态不明或操作中断</dt><dd>在「库与维护」先执行完整性检查；存在未完成的同步或清理事务时，再使用异常恢复。</dd></div></dl><h3 class="guide-section-title">控制台启动与更新</h3><div class="guide-command"><span>首次设置外挂库</span><code>pal quickstart</code></div><div class="guide-command"><span>打开控制台 · 默认端口 8787</span><code>pal web</code></div><div class="guide-command"><span>停止服务 · 更新 PAL 后重新启动</span><code>pal web stop</code></div><div class="guide-command"><span>使用其他端口 · 停止时指定同一端口</span><code>pal web --port 8899<br>pal web stop --port 8899</code></div><div class="guide-tip">${icon('help')}<p>关闭网页不会停止服务。完整性检查验证内容与安装状态，不代表 Skill 效果；分析与评分暂未开放。</p></div></section>`,buttons:[{label:'关闭说明',action:closeDialog}]});
}
function selectGuideTopic(topic, focus = true) {
  dialog.querySelectorAll('[data-guide-topic]').forEach(button => { const selected = button.dataset.guideTopic === topic; button.setAttribute('aria-selected',String(selected)); button.tabIndex = selected ? 0 : -1; $(button.getAttribute('aria-controls')).hidden = !selected; });
  dialog.querySelector('.dialog-body').scrollTop = 0;
  if (focus) $(`guide-tab-${topic}`).focus({preventScroll:true});
}
dialog.addEventListener('click', event => {
  const button = event.target.closest('[data-guide-topic]');
  if (button && !drawerClosing) selectGuideTopic(button.dataset.guideTopic);
  if (event.target === dialog && dialog.dataset.drawer === 'true') {
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) closeDialog();
  }
});
dialog.addEventListener('keydown', event => {
  if (!event.target.dataset.guideTopic || !['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
  event.preventDefault();
  const topics = ['start','manage','care'], index = topics.indexOf(event.target.dataset.guideTopic);
  selectGuideTopic(event.key === 'Home' ? 'start' : event.key === 'End' ? 'care' : topics[(index + (event.key === 'ArrowRight' ? 1 : 2)) % 3]);
});
document.addEventListener('click', async event => {
  const link = event.target.closest('[data-doc-link]');
  if (link && view === 'skill') { event.preventDefault(); if (!dialog.open) await followDocumentLink(link.dataset.docLink); return; }
  const button = event.target.closest('button');
  if (!button || button.disabled || busy || dialog.open) return;
  if (button.dataset.view) { detailRequest++; fileRequest++; view = button.dataset.view; render(); $('content').className = 'view-enter'; return; }
  if (button.dataset.filter) { filter = button.dataset.filter; render(); document.querySelector(`[data-filter="${filter}"]`).focus({preventScroll:true}); return; }
  if (button.dataset.skillTab) { await selectSkillTab(button.dataset.skillTab); return; }
  if (button.dataset.filePath) { await openSkillFile(button.dataset.filePath); return; }
  if (button.dataset.fileMode) { skillPage.fileMode = button.dataset.fileMode; updateFileViewer(); document.querySelector(`[data-file-mode="${skillPage.fileMode}"]`).focus({preventScroll:true}); return; }
  const action = button.dataset.action;
  if (action === 'skill-back') backToCollection();
  if (action === 'skill-reload') await skillDetails(skillPage.unit_id);
  if (action === 'file-retry') await openSkillFile(skillPage.path);
  if (action === 'theme') toggleTheme();
  if (action === 'help') help();
  if (action === 'sync') await syncCurrent();
  if (action === 'monitor') await execute({title:'正在核查安装',description:'逐端读取官方安装状态并校验文件。',path:'/api/doctor',body:{},successTitle:'安装检查完成'});
  if (action === 'repair-installation') await repairInstallation(button.dataset.kind,button.dataset.cli);
  if (action === 'refresh') await refresh();
  if (action === 'doctor') await execute({title:'正在检查完整性',description:'正在分别校验开发、生产及上次挂载的内容。',path:'/api/doctor',body:{},successTitle:'检查完成'});
  if (action === 'recover' && await confirmAction({title:'处理异常恢复？',description:'检查并恢复上次未完成的 CLI 同步或清理事务。完成后会重新读取状态。',label:'开始恢复'})) await execute({title:'正在处理异常恢复',description:'正在检查并收敛未完成事务。请保持页面打开。',path:'/api/recover',body:{},production:true,successTitle:'恢复检查完成'});
  if (['skill-publish','skill-mount','skill-unmount','skill-discard','skill-delete'].includes(action)) await skillAction(action.slice(6),button.dataset.unit);
  if (action === 'skill-details') await skillDetails(button.dataset.unit,button.dataset.source);

});
document.addEventListener('change', async event => {
  if (!['skill-source','skill-artifact'].includes(event.target.id)) return;
  fileRequest++;
  const id = event.target.id;
  if (id === 'skill-source') { skillPage.source = event.target.value; skillPage.artifact = 0; }
  else skillPage.artifact = Number(event.target.value);
  skillPage.path = null; skillPage.file = null; skillPage.fileLoading = false; skillPage.fileError = ''; render(); $(id)?.focus({preventScroll:true});
  if (skillPage.tab === 'files') await openSkillFile(selectedArtifact().canonical_path);
});
document.addEventListener('keydown', async event => {
  if (view !== 'skill' || dialog.open) return;
  if (event.key === 'Escape' && !['SELECT','INPUT'].includes(event.target.tagName)) backToCollection();
  if (event.target.dataset.skillTab && ['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) {
    event.preventDefault(); await selectSkillTab(event.key === 'Home' ? 'overview' : event.key === 'End' ? 'files' : skillPage.tab === 'overview' ? 'files' : 'overview');
  }
});
document.addEventListener('input', event => { if (event.target.id === 'skill-search') { search = event.target.value; renderRows(); } });
$('health').addEventListener('click', () => { if (!busy && !dialog.open) { view = 'sync'; render(); } });
$('health').setAttribute('role','button'); $('health').tabIndex = 0;
$('health').addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); $('health').click(); } });
setInterval(() => { if (!document.hidden && !dialog.open) refreshMonitor(); },60000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) { renderHealth(); refreshMonitor(); } });
(async () => { try { acceptStatus(await request('/api/status')); await refreshMonitor(); } catch (error) { invalidate(error); } })();
</script>
</body>
</html>
"""
INDEX_HTML = INDEX_HTML.replace("__PAL_LOGO__", LOGO_SVG)
WEB_BUILD_ID = BUILD_ID


def _loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _status(root: Path, config_root: Path) -> dict[str, Any]:
    return {"web_build_id": WEB_BUILD_ID, **library_status(root, config_root)}


class _WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address: tuple[str, int], library_root: Path, config_root: Path) -> None:
        super().__init__(address, _WebHandler)
        self.library_root = library_root
        self.config_root = config_root
        self.stop_token: str | None = None
        self.instance_id: str | None = None


class _WebHandler(BaseHTTPRequestHandler):
    server: _WebServer

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        # Loopback binding alone does not prevent DNS rebinding. Validate before
        # dispatch, including read APIs and the independently authenticated stop API.
        host, port = self.server.server_address[:2]
        address = f"[{host}]" if ":" in host else host
        allowed = {f"{address}:{port}", f"localhost:{port}"}
        if port == 80:
            allowed.update({address, "localhost"})
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        fetch_sites = self.headers.get_all("Sec-Fetch-Site", [])
        authority = hosts[0].lower() if len(hosts) == 1 else ""
        external_navigation = (
            self.command == "GET"
            and self.path in {"/", "/index.html"}
            and self.headers.get("Sec-Fetch-Mode") == "navigate"
            and self.headers.get("Sec-Fetch-Dest") == "document"
        )
        if (
            authority not in allowed
            or (origins and origins != [f"http://{authority}"])
            or (
                fetch_sites
                and fetch_sites not in (["same-origin"], ["none"])
                and not external_navigation
            )
        ):
            self.close_connection = True
            self._error(HTTPStatus.FORBIDDEN, "仅允许通过本机地址同源访问 PAL Web")
            return False
        return True

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        self._send_bytes(
            status,
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _error(self, status: HTTPStatus, error: Exception | str) -> None:
        self._send_json(status, {"error": str(error)})

    def do_GET(self) -> None:  # noqa: N802 - stdlib HTTP handler contract
        path = urlsplit(self.path).path
        if path in {"/", "/index.html"}:
            self._send_bytes(HTTPStatus.OK, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/server":
            self._send_json(
                HTTPStatus.OK,
                {"proof": "PAL_WEB_SERVER", "instance_id": self.server.instance_id},
            )
            return
        if path == "/api/context":
            self._send_json(
                HTTPStatus.OK,
                {
                    "proof": "PAL_WEB_CONTEXT",
                    "library_root": str(self.server.library_root),
                    "config_root": str(self.server.config_root),
                    "web_build_id": WEB_BUILD_ID,
                },
            )
            return
        if path == "/favicon.svg":
            self._send_bytes(HTTPStatus.OK, FAVICON_SVG.encode("utf-8"), "image/svg+xml")
            return
        if path == "/favicon.ico":
            self._send_bytes(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            return
        if path not in {"/api/status", "/api/skill-file", "/api/monitor"} and not path.startswith(
            "/api/skills/"
        ):
            self._error(HTTPStatus.NOT_FOUND, "资源不存在")
            return
        try:
            if path == "/api/monitor":
                result = inspect_installations(
                    self.server.library_root, config_root=self.server.config_root
                )
            elif path == "/api/skill-file":
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                if any(len(values) != 1 for values in query.values()):
                    raise ValueError("文件请求包含重复参数")
                result = skill_file(
                    self.server.library_root, {key: values[0] for key, values in query.items()}
                )
            elif path.startswith("/api/skills/"):
                result = skill_details(self.server.library_root, path.removeprefix("/api/skills/"))
            else:
                result = _status(self.server.library_root, self.server.config_root)
            self._send_json(HTTPStatus.OK, result)
        except (PALError, OSError, ValueError) as exc:
            self._error(HTTPStatus.CONFLICT, exc)

    def _request_json(self) -> dict[str, Any]:
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit() or int(length) > 64 * 1024:
            raise ValueError("请求体无效或过大")
        raw = self.rfile.read(int(length))
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return value

    def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler contract
        path = urlsplit(self.path).path
        if path == "/api/stop":
            token = self.headers.get("X-PAL-Stop-Token", "")
            if not self.server.stop_token or not secrets.compare_digest(
                token, self.server.stop_token
            ):
                self._error(HTTPStatus.FORBIDDEN, "关闭服务的凭据无效")
                return
            self._send_json(
                HTTPStatus.OK,
                {"proof": "PAL_WEB_STOPPING", "message": "PAL Web 正在停止。"},
            )
            Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.headers.get(ACTION_HEADER) != ACTION_CONFIRMATION:
            self._error(HTTPStatus.FORBIDDEN, "状态变更需要显式操作确认")
            return
        try:
            body = self._request_json()
            if path in {"/api/skill-actions/preview", "/api/skill-actions/execute"}:
                expected = {"action", "unit_id"} | (
                    {"token"} if path.endswith("/execute") else set()
                )
                if set(body) != expected:
                    raise ValueError("Skill 操作请求字段不正确")
                operation = (
                    execute_skill_action if path.endswith("/execute") else preview_skill_action
                )
                result = operation(
                    self.server.library_root, config_root=self.server.config_root, **body
                )
            elif path == "/api/installations/repair":
                if set(body) != {"kind", "cli_id", "expected_version"}:
                    raise ValueError("修复请求字段不正确")
                if not all(isinstance(value, str) for value in body.values()):
                    raise ValueError("修复请求字段必须为字符串")
                result = repair_installation(
                    self.server.library_root,
                    config_root=self.server.config_root,
                    **body,
                )
            elif path == "/api/sync":
                if set(body) != {"version_id"}:
                    raise ValueError("同步请求必须包含当前生产标识")
                version = require_safe_id(body["version_id"], "当前生产标识")
                result = sync_production(
                    self.server.library_root,
                    config_root=self.server.config_root,
                    expected_version_id=version,
                )
            elif path == "/api/recover":
                skill_result = recover_skill_action(
                    self.server.library_root, config_root=self.server.config_root
                )
                deletion_result = recover_deletion(
                    self.server.library_root, config_root=self.server.config_root
                )
                production_result = recover_production(
                    self.server.library_root,
                    config_root=self.server.config_root,
                )
                usage_result = recover_usage_transactions(self.server.library_root)
                result = {
                    "proof": "PAL_PRODUCTION_RECOVERED",
                    **production_result,
                    "usage_recovery": usage_result,
                    "deletion_recovery": deletion_result,
                    "skill_recovery": skill_result,
                    "message": "恢复检查完成。",
                }
            elif path == "/api/doctor":
                integrity = check_library_contents(self.server.library_root)
                result = _status(self.server.library_root, self.server.config_root)
                result = {
                    "proof": "PAL_LIBRARY_CHECKED",
                    "monitor": inspect_installations(
                        self.server.library_root, config_root=self.server.config_root
                    ),
                    "status": result,
                    "content_checks": integrity["content_checks"],
                    "message": "；".join(
                        f"{item['label']}通过（{item['units']} 项）"
                        for item in integrity["content_checks"]
                    )
                    + "。安装核查结果见 CLI 与系统页面。",
                }
            else:
                self._error(HTTPStatus.NOT_FOUND, "资源不存在")
                return
            self._send_json(HTTPStatus.OK, result)
        except (PALError, OSError, ValueError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.CONFLICT, exc)


def create_server(
    library_root: Path,
    *,
    config_root: Path | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> _WebServer:
    """Validate roots and build a testable local PAL HTTP server."""

    if not _loopback_host(host):
        raise PathSafetyError("PAL Web 仅允许监听 loopback 地址")
    if port < 0 or port > 65535:
        raise PathSafetyError("Web 端口必须在 0 到 65535 之间")
    root = canonical_existing_root(library_root)
    resolved_config = resolve_config_root(config_root, create=False)
    # Only recognized recovery journals relax business-state checks, never identity
    # or path validation. Ordinary APIs retain their maintenance write gate.
    with maintenance_lock(root, recovery=True):
        context = doctor_development_context(root)
        pending = False
        for path, schema in (
            (skill_action_path(root), "skill-action.schema.json"),
            (cleanup_path(root), "permanent-deletion.schema.json"),
        ):
            if not path.exists():
                continue
            journal = load_json_object(path)
            validate_config_instance(schema, journal)
            if (journal["library_id"], journal["library_root"], journal["config_root"]) != (
                context["library_id"],
                str(root),
                str(resolved_config),
            ):
                raise IntegrityError("恢复记录与当前库或配置目录不一致")
            pending = True
        if not pending:
            _status(root, resolved_config)
    return _WebServer((host, port), root, resolved_config)


def run_web(
    library_root: Path,
    *,
    config_root: Path | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> int:
    """Run the local management console until interrupted."""

    try:
        server = create_server(
            library_root,
            config_root=config_root,
            host=host,
            port=port,
        )
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        url = _web_url(host, port)
        existing = _existing_console(host, port)
        if existing is not None:
            selected_root = canonical_existing_root(library_root)
            selected_config = resolve_config_root(config_root, create=False)
            if existing[:2] == (str(selected_root), str(selected_config)):
                if existing[2] != WEB_BUILD_ID:
                    raise PALError(
                        f"{url} 正在运行旧版 PAL Web；程序更新尚未在该服务生效。"
                        "请先执行 pal web stop，再重新启动。"
                        "如果旧服务不支持 stop，请停止原启动终端中的进程。"
                    ) from exc
                print(f"PAL Web 控制台已在运行：{url}")
                print("已复用现有服务；可运行 pal web stop 关闭。")
                if open_browser:
                    webbrowser.open(url)
                return 0
            detail = f"该地址已有其他库或配置的 PAL 控制台：{url}\n外挂库：{existing[0]}"
        else:
            detail = f"端口 {port} 已被占用，监听地址：{url}\n未能确认该服务是可用的 PAL 控制台。"
        raise PALError(
            f"{detail}\n可用 --port 指定其他端口，或使用 pal web --port 0 自动选择空闲端口。"
        ) from exc
    actual_host, actual_port = server.server_address[:2]
    url = _web_url(actual_host, actual_port)
    server.stop_token = secrets.token_hex(32)
    server.instance_id = secrets.token_hex(16)
    record_path: Path | None = None
    try:
        record_path = _web_record_path(server.config_root, actual_port)
        atomic_replace_json(
            record_path,
            {
                "host": actual_host,
                "port": actual_port,
                "token": server.stop_token,
                "instance_id": server.instance_id,
            },
        )
        print(f"PAL Web 控制台：{url}", flush=True)
        print("按 Ctrl-C 停止，或在其他终端运行 pal web stop。", flush=True)
        if open_browser:
            webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nPAL Web 已停止。")
    finally:
        try:
            if record_path is not None and record_path.is_file() and not is_link(record_path):
                try:
                    recorded = load_json_object(record_path)
                except (PALError, OSError):
                    recorded = {}
                if recorded.get("token") == server.stop_token:
                    record_path.unlink()
        finally:
            server.server_close()
    return 0


def _web_record_path(config_root: Path, port: int) -> Path:
    directory = config_root / "web"
    if is_link(directory):
        raise PathSafetyError(f"PAL Web 记录目录不能是符号链接：{directory}")
    directory.mkdir(exist_ok=True)
    return directory / f"port-{port}.json"


def stop_web(*, config_root: Path | None = None, port: int = DEFAULT_PORT) -> int:
    """Stop only a local PAL Web process registered in the selected config root."""

    if not 1 <= port <= 65535:
        raise PathSafetyError("关闭服务时端口必须在 1 到 65535 之间")
    root = resolve_config_root(config_root, create=False)
    record_path = root / "web" / f"port-{port}.json"
    if is_link(record_path) or not record_path.is_file():
        raise PALError(f"端口 {port} 没有可关闭的 PAL Web 服务记录")
    record = load_json_object(record_path)
    host, token, instance_id = (
        record.get("host"),
        record.get("token"),
        record.get("instance_id"),
    )
    if (
        not isinstance(host, str)
        or not _loopback_host(host)
        or record.get("port") != port
        or not isinstance(token, str)
        or len(token) != 64
        or not isinstance(instance_id, str)
        or len(instance_id) != 32
    ):
        raise PALError("PAL Web 服务记录无效，已拒绝关闭")
    connection = HTTPConnection(host, port, timeout=3)
    try:
        connection.request("GET", "/api/server")
        with connection.getresponse() as response:
            if (
                response.status != HTTPStatus.OK
                or response.headers.get_content_type() != "application/json"
            ):
                raise PALError("端口上的服务不匹配 PAL Web 登记记录，已拒绝关闭")
            identity = json.loads(response.read(1025))
        if identity != {"proof": "PAL_WEB_SERVER", "instance_id": instance_id}:
            raise PALError("端口上的服务不匹配 PAL Web 登记记录，已拒绝关闭")
        connection.request("POST", "/api/stop", body=b"", headers={"X-PAL-Stop-Token": token})
        with connection.getresponse() as response:
            if response.status != HTTPStatus.OK:
                raise PALError("目标服务未接受关闭请求；请检查服务是否已经更新")
            result = json.loads(response.read(1025))
            if not isinstance(result, dict) or result.get("proof") != "PAL_WEB_STOPPING":
                raise PALError("目标服务没有确认关闭，已拒绝报告成功")
    except (OSError, HTTPException, ValueError) as exc:
        raise PALError(f"无法连接端口 {port} 的 PAL Web 服务：{exc}") from exc
    finally:
        connection.close()
    print(f"PAL Web 正在停止：{_web_url(host, port)}")
    return 0


def _web_url(host: str, port: int) -> str:
    display_host = f"[{host}]" if ":" in host else host
    return f"http://{display_host}:{port}/"


def _existing_console(host: str, port: int) -> tuple[str, str, str | None] | None:
    """Recognize an existing console, including versions predating reuse support."""

    context = _existing_console_context(host, port)
    if context is not _CONTEXT_UNSUPPORTED:
        return context

    # Direct loopback connection: never send library data to a proxy or follow redirects.
    connection = HTTPConnection(host, port, timeout=2)
    try:
        connection.request("GET", "/api/status")
        with connection.getresponse() as response:
            if response.status != HTTPStatus.OK:
                return None
            if response.headers.get_content_type() != "application/json":
                return None
            maximum = 1024 * 1024
            body = response.read(maximum + 1)
            if len(body) > maximum:
                return None
        status = json.loads(body)
        if not isinstance(status, dict):
            return None
        library = status.get("library")
        if (
            not isinstance(library, dict)
            or not isinstance(library.get("library_id"), str)
            or not isinstance(library.get("library_root"), str)
            or not isinstance(status.get("config_root"), str)
            or not isinstance(status.get("production"), dict)
            or not isinstance(status.get("units"), list)
            or not isinstance(status.get("default_binding"), dict)
        ):
            return None
        build_id = status.get("web_build_id")
        return library["library_root"], status["config_root"], build_id
    except (OSError, HTTPException, ValueError):
        return None
    finally:
        connection.close()


_CONTEXT_UNSUPPORTED = object()


def _existing_console_context(host: str, port: int) -> tuple[str, str, str] | None | object:
    connection = HTTPConnection(host, port, timeout=2)
    try:
        connection.request("GET", "/api/context")
        with connection.getresponse() as response:
            if response.status == HTTPStatus.NOT_FOUND:
                response.read(8193)
                return _CONTEXT_UNSUPPORTED
            if (
                response.status != HTTPStatus.OK
                or response.headers.get_content_type() != "application/json"
            ):
                return None
            context = json.loads(response.read(8193))
        fields = ("library_root", "config_root", "web_build_id")
        if not isinstance(context, dict) or context.get("proof") != "PAL_WEB_CONTEXT":
            return None
        if not all(isinstance(context.get(key), str) and context[key] for key in fields):
            return None
        return tuple(context[key] for key in fields)
    except (OSError, HTTPException, ValueError):
        return None
    finally:
        connection.close()


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "INDEX_HTML", "create_server", "run_web", "stop_web"]
