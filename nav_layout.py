# 3·2·1→Theater
# © 2026 Dr. Phillips Center for the Performing Arts; portions © 2026 Thauma Systems, LLC.
# MIT Licensed — see LICENSE for details.
"""
Sidebar navigation layout — admin-customizable order, grouping (section
labels), indenting, renaming, and cosmetic hiding of the main left-nav items.

Mirrors pdf_layouts.py: the CATALOG (what items exist, where they link, who
may see them) lives here in code; the saved layout (order / sections /
overrides) lives in the DB as JSON in `app_settings` under key `nav_layout`.
No saved layout → DEFAULT_ENTRIES.

SECURITY: each item's `audience` is code-only — the editor can hide an item
cosmetically but can never widen who sees it. Hiding a nav link also never
grants or revokes access; every route keeps its own permission decorator.
"""

import copy
import json
import logging

logger = logging.getLogger(__name__)

NAV_SETTING_KEY = 'nav_layout'
MAX_ENTRIES = 100
MAX_LABEL_LEN = 40

# Link spacing for the whole sidebar (Sidebar Editor → Spacing). 'compact'
# tightens the padding between links; 'comfortable' is the roomier classic look.
DENSITIES = ('compact', 'comfortable')
DEFAULT_DENSITY = 'compact'

# Audience keys are resolved against the session by app._nav_audience_ok().
AUDIENCE_LABELS = {
    'all':             'Everyone',
    'admin':           'Admins only',
    'content_admin':   'Admins & content admins',
    'asset_manager':   'Admins, content admins & asset managers',
    'labor_scheduler': 'Labor schedulers (staff, admins & users with the Scheduler permission)',
}

# `icon` is the inner markup of the nav <svg> (the wrapper with viewBox /
# stroke attributes lives in base.html). `active` lists endpoints that light
# the item up; `active_prefix` matches request.endpoint by prefix.
# `required` items can be moved/renamed but never hidden (so nobody strands
# themselves without Settings).
# NOTE: admin tools (Prism Sync, PDF Designer, Sidebar Editor) are deliberately
# NOT nav items — they're reached from the Settings tab bar to keep the
# sidebar short. Don't re-add them here without being asked.
NAV_CATALOG = [
    {'key': 'dashboards', 'label': 'Dashboards', 'endpoint': 'dashboards_list',
     'active': ('dashboards_list', 'dashboard_view'), 'audience': 'all',
     'icon': '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>'},
    {'key': 'crew_tracker', 'label': 'Skill Tracker', 'endpoint': 'crew_tracker',
     'active': ('crew_tracker',), 'audience': 'all',
     'icon': '<path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/>'},
    {'key': 'labor_overview', 'label': 'Labor Overview', 'endpoint': 'labor_overview',
     'active': ('labor_overview',), 'audience': 'all',
     'icon': '<rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="3" y1="10" x2="21" y2="10"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="7" y1="14" x2="9" y2="14"/><line x1="11" y1="14" x2="13" y2="14"/><line x1="15" y1="14" x2="17" y2="14"/><line x1="7" y1="18" x2="9" y2="18"/><line x1="11" y1="18" x2="13" y2="18"/>'},
    {'key': 'labor_scheduler', 'label': 'Labor Scheduler', 'endpoint': 'labor_scheduler',
     'active': ('labor_scheduler',), 'audience': 'labor_scheduler',
     'icon': '<rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/><path d="M9 16l2 2 4-4"/>'},
    {'key': 'overhead_crew', 'label': 'Overhead & Project Crew', 'endpoint': 'overhead_crew_page',
     'active': ('overhead_crew_page',), 'audience': 'all',
     'icon': '<rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/><path d="M8 14h2"/><path d="M14 14h2"/><path d="M8 18h2"/><path d="M14 18h2"/>'},
    {'key': 'combined_invoice', 'label': 'Combined Invoice', 'endpoint': 'combined_invoice_page',
     'active': ('combined_invoice_page',), 'audience': 'all',
     'icon': '<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><path d="M12 10.5v8"/><path d="M14.2 12.3c0-.9-1-1.6-2.2-1.6s-2.2.7-2.2 1.6 1 1.6 2.2 1.6 2.2.7 2.2 1.6-1 1.6-2.2 1.6-2.2-.7-2.2-1.6"/>'},
    {'key': 'asset_manager', 'label': 'Asset Manager', 'endpoint': 'assets_admin',
     'active': ('assets_admin', 'assets_retired', 'asset_reports', 'asset_approvals'),
     'audience': 'asset_manager',
     'icon': '<rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 7V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v2"/><line x1="12" y1="12" x2="12" y2="16"/><line x1="10" y1="14" x2="14" y2="14"/>'},
    {'key': 'asset_approvals', 'label': 'Approvals', 'endpoint': 'asset_approvals',
     'active': ('asset_approvals',), 'audience': 'asset_manager', 'badge': True,
     'icon': '<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/>'},
    {'key': 'asset_reports', 'label': 'Reports', 'endpoint': 'asset_reports',
     'active': ('asset_reports',), 'audience': 'asset_manager',
     'icon': '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>'},
    {'key': 'assets_retired', 'label': 'Retired Archive', 'endpoint': 'assets_retired',
     'active': ('assets_retired',), 'audience': 'asset_manager',
     'icon': '<circle cx="12" cy="12" r="9"/><line x1="9" y1="9" x2="15" y2="15"/><line x1="15" y1="9" x2="9" y2="15"/>'},
    {'key': 'settings', 'label': 'Settings', 'endpoint': 'settings',
     'active': ('settings',), 'audience': 'all', 'required': True,
     'icon': '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/>'},
]

_BY_KEY = {item['key']: item for item in NAV_CATALOG}

# Default layout — the classic sidebar grouped into sections, with the
# Combined Invoice tool nested under Settings.
DEFAULT_ENTRIES = [
    {'type': 'section', 'label': 'SYSTEM'},
    {'type': 'item', 'key': 'dashboards', 'label': '', 'indent': False, 'hidden': False},
    {'type': 'section', 'label': 'LABOR'},
    {'type': 'item', 'key': 'crew_tracker', 'label': '', 'indent': False, 'hidden': False},
    {'type': 'item', 'key': 'labor_overview', 'label': '', 'indent': False, 'hidden': False},
    {'type': 'item', 'key': 'labor_scheduler', 'label': '', 'indent': False, 'hidden': False},
    {'type': 'item', 'key': 'overhead_crew', 'label': '', 'indent': False, 'hidden': False},
    {'type': 'section', 'label': 'ASSETS'},
    {'type': 'item', 'key': 'asset_manager', 'label': '', 'indent': False, 'hidden': False},
    {'type': 'item', 'key': 'asset_approvals', 'label': '', 'indent': True, 'hidden': False},
    {'type': 'item', 'key': 'asset_reports', 'label': '', 'indent': True, 'hidden': False},
    {'type': 'item', 'key': 'assets_retired', 'label': '', 'indent': True, 'hidden': False},
    {'type': 'section', 'label': 'SETTINGS'},
    {'type': 'item', 'key': 'settings', 'label': '', 'indent': False, 'hidden': False},
    {'type': 'item', 'key': 'combined_invoice', 'label': '', 'indent': True, 'hidden': False},
]


def default_layout():
    return {'version': 1, 'density': DEFAULT_DENSITY,
            'entries': copy.deepcopy(DEFAULT_ENTRIES)}


def _default_entry_for(key):
    for e in DEFAULT_ENTRIES:
        if e['type'] == 'item' and e['key'] == key:
            return copy.deepcopy(e)
    return {'type': 'item', 'key': key, 'label': '', 'indent': False, 'hidden': False}


def _clean_label(v):
    return str(v or '').strip()[:MAX_LABEL_LEN]


def _clean_density(v):
    v = str(v or '').strip().lower()
    return v if v in DENSITIES else DEFAULT_DENSITY


def _normalize_entries(raw_entries):
    """Drop unknown/duplicate items and malformed entries; coerce field types;
    force required items visible; append catalog items missing from the list
    (so newly shipped features always show up until an admin re-organizes)."""
    entries = []
    seen = set()
    for e in (raw_entries or [])[:MAX_ENTRIES]:
        if not isinstance(e, dict):
            continue
        if e.get('type') == 'section':
            label = _clean_label(e.get('label'))
            if label:
                entries.append({'type': 'section', 'label': label})
        elif e.get('type') == 'item':
            key = e.get('key')
            item = _BY_KEY.get(key)
            if not item or key in seen:
                continue
            seen.add(key)
            entries.append({
                'type': 'item',
                'key': key,
                'label': _clean_label(e.get('label')),
                'indent': bool(e.get('indent')),
                'hidden': False if item.get('required') else bool(e.get('hidden')),
            })
    for item in NAV_CATALOG:
        if item['key'] not in seen:
            entries.append(_default_entry_for(item['key']))
    return entries


def parse_or_default(raw):
    """Parse saved layout JSON; any failure falls back to the defaults."""
    if not raw:
        return default_layout()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get('entries'), list):
            raise ValueError('layout JSON missing entries list')
    except Exception as e:
        logger.warning('nav_layout: parse failed (%s) — using defaults', e)
        return default_layout()
    return {'version': 1, 'density': _clean_density(data.get('density')),
            'entries': _normalize_entries(data['entries'])}


def validate_payload(payload):
    """Validate an editor payload before saving.
    Returns (cleaned_layout_dict, error_message_or_None)."""
    if not isinstance(payload, dict) or not isinstance(payload.get('entries'), list):
        return None, 'Payload must be an object with an entries list.'
    if len(payload['entries']) > MAX_ENTRIES:
        return None, f'Too many entries (max {MAX_ENTRIES}).'
    for e in payload['entries']:
        if not isinstance(e, dict):
            continue
        item = _BY_KEY.get(e.get('key')) if e.get('type') == 'item' else None
        if item and item.get('required') and e.get('hidden'):
            return None, f"'{item['label']}' is required and cannot be hidden."
    return {'version': 1, 'density': _clean_density(payload.get('density')),
            'entries': _normalize_entries(payload['entries'])}, None


def catalog_for_editor():
    """Catalog as the editor needs it: defaults, icons, and a human-readable
    audience description per item (visibility itself is never editable)."""
    return [{
        'key': item['key'],
        'label': item['label'],
        'icon': item['icon'],
        'required': bool(item.get('required')),
        'audience': item['audience'],
        'audience_label': AUDIENCE_LABELS.get(item['audience'], item['audience']),
    } for item in NAV_CATALOG]


def resolve(layout, audience_ok, endpoint):
    """Flatten a layout into render-ready entries for one request.
    `audience_ok(audience_key)` is the session-aware permission predicate.
    Section labels are emitted only when at least one visible item follows
    them (before the next section), so role-filtered groups collapse cleanly.
    """
    out = []
    pending_section = None
    for e in layout.get('entries', []):
        if e.get('type') == 'section':
            pending_section = e.get('label') or ''
            continue
        item = _BY_KEY.get(e.get('key'))
        if not item or e.get('hidden') or not audience_ok(item['audience']):
            continue
        if pending_section:
            out.append({'type': 'section', 'label': pending_section})
        pending_section = None
        active = endpoint in item.get('active', ())
        prefix = item.get('active_prefix')
        if not active and prefix and endpoint.startswith(prefix):
            active = True
        out.append({
            'type': 'item',
            'key': item['key'],
            'endpoint': item['endpoint'],
            'label': e.get('label') or item['label'],
            'icon': item['icon'],
            'indent': bool(e.get('indent')),
            'active': active,
            'badge': bool(item.get('badge')),
        })
    return out
