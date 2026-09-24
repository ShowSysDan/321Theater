# 3·2·1→Theater
# © 2026 Dr. Phillips Center for the Performing Arts; portions © 2026 Thauma Systems, LLC.
# MIT Licensed — see LICENSE for details.
"""
PostgreSQL schema creation, migration and seeding for 3·2·1→Theater.

The app is PostgreSQL-only (3.0.0+). Connection settings come from
db_config.ini (see db_config.ini.example / db_adapter.CONFIG_PATH).

Usage:
  python init_db.py           — create schemas + tables (idempotent), apply
                                column migrations, and seed a FRESH install
                                (defaults are only written into empty tables;
                                the admin/admin123 account only when there are
                                no users at all)
  python init_db.py --migrate — schema + column migrations only, no seeding
                                (this also runs automatically on app startup)
  python init_db.py --reset   — DROP both schemas and ALL their data (asks
                                for confirmation), then re-run init
"""
import json
import re
import sys

import db_adapter

SEED_CONTACTS = [
    # Production
    ('Allie Shidel',          'Production Manager',         'Production',     '239-898-4419', ''),
    ('Alyssa Marinello',      'Production Manager',         'Production',     '860-707-0224', ''),
    ('Ashley Kreischer',      'Production Manager',         'Production',     '832-527-1507', ''),
    ('Cheyenne Young',        'Production Manager',         'Production',     '407-953-9686', ''),
    ('Jeff Sturgis',          'Production Manager',         'Production',     '801-971-2240', ''),
    ('John Gallagher',        'Production Manager',         'Production',     '732-770-1406', ''),
    ('Noah Mencia',           'Production Manager',         'Production',     '407-269-0286', ''),
    ('Troy Mitchell',         'Production Manager',         'Production',     '716-622-7675', ''),
    ('Don Teer',              'Director, Production',       'Production',     '407-376-4149', ''),
    ('Dw Phineas Perkins',    'Director, Production',       'Production',     '407-421-4331', ''),
    ('Kevin Griffin',         'Technical Director',         'Production',     '407-921-4584', ''),
    ('Rich Neu',              'Assoc Technical Director',   'Production',     '407-803-5153', ''),
    # Programming
    ('Andrew Birgensmith',    'Sr. Director, Programming',          'Programming',    '816-935-9120', ''),
    ('Chris Belt',            "Manager, Judson's Programming",      'Programming',    '689-248-6768', ''),
    ('Foster Cronin',         'VP, Programming',                    'Programming',    '267-438-4371', ''),
    ('Geraldine Diaz',        'Programming Coordinator',            'Programming',    '352-942-4672', ''),
    ('Jovanna Hernandez',     'Director, Regional Programming',     'Programming',    '407-430-8939', ''),
    ('Mariah Roberts',        'Manager, Commercial Booking',        'Programming',    '352-634-0157', ''),
    ('Melissa Hopkins',       'Programming Coordinator',            'Programming',    '407-839-0119', ''),
    ('Toni Chandler',         'Manager, Regional Arts Programming', 'Programming',    '386-216-4493', ''),
    ('Zachary Hines',         'Manager, Commercial Booking',        'Programming',    '609-413-1869', ''),
    # Event Manager
    ('Grace Smith',           'Sales Manager, Events',      'Event Manager',  '850-728-8183', ''),
    ('Jenna Rogers',          'Director, Events',           'Event Manager',  '407-383-6008', ''),
    ('Kelsie Taylor',         'Sr. Sales Manager, Events',  'Event Manager',  '407-739-5909', ''),
    ('Robyn Pigozzi',         'Sr. Manager, Events',        'Event Manager',  '407-619-2609', ''),
    ('Sarah-Lynn Sharpton',   'Event Manager',              'Event Manager',  '334-740-9042', ''),
    ('Trevor Starr',          'Event Manager',              'Event Manager',  '321-289-6063', ''),
    # Education Team
    ('Brooke Saad',           'Manager, Education',         'Education Team', '407-409-1035', ''),
    ('Gabrielle Lawlor',      'Supervisor, SoA Education',  'Education Team', '',             ''),
    ('Khristy Chamberlain',   'Manager, Education',         'Education Team', '954-439-6620', ''),
    ('Ryan Simpson',          'Director, Education',        'Education Team', '847-951-9439', ''),
    ('Sara York',             'Sr. Manager, Education',     'Education Team', '334-618-8886', ''),
    ('Tati Bello',            'SOA Manager, Education',     'Education Team', '321-263-8004', ''),
    # Hospitality
    ('Dana Desposito',        'Supervisor, Craft Services', 'Hospitality',    '772-349-1347', ''),
    ('Jackie Einfeldt',       'Manager, Concession',        'Hospitality',    '321-304-0482', ''),
    ('Jenna Wickens',         'Manager, Backstage Catering','Hospitality',    '772-521-5521', ''),
    ('F&B Management',        'Management',                 'Hospitality',    '',             'foodbeveragemanagement@drphillipscenter.org'),
    # Guest Services
    ('Aaron Sandford-Wetherell','Sr. Manager, Guest Services','Guest Services','407-489-8620',''),
    ('Diana Mattoni',         'Manager, Front of House',    'Guest Services', '',             ''),
    ('Meghan Godber',         'Manager, Guest Services',    'Guest Services', '407-353-1593', ''),
    ('Charlie Robuck',        'Manager, Front of House',    'Guest Services', '910-520-3668', ''),
    ('Zakiya Smith-Dore',     'Director, Guest Services',   'Guest Services', '407-373-1949', ''),
    # Security
    ('Security Dept',         'Security',                   'Security',       '',             'security@drphillipscenter.org'),
    # Runners
    ('Anik Pariseleti',       'Runner', 'Runners', '', ''),
    ('David Becker',          'Runner', 'Runners', '', ''),
    ('Josh Cassady',          'Runner', 'Runners', '', ''),
    ('Kathy Wiebe',           'Runner', 'Runners', '', ''),
    ('Keith (KJ) Sales',      'Runner', 'Runners', '', ''),
    ('Kenzie Smith',          'Runner', 'Runners', '', ''),
    ("Kyle O'Toole",          'Runner', 'Runners', '', ''),
    ('Luke St. Jean',         'Runner (no alcohol)', 'Runners', '', ''),
    ('Matt McGregor',         'Runner (no people)',  'Runners', '', ''),
    ('Rick Luciano',          'Runner', 'Runners', '', ''),
    ('Sofia Rivera',          'Runner', 'Runners', '', ''),
]

# (section_key, label, sort_order, collapsible, icon)
FORM_SECTIONS_SEED = [
    ('show_info',        'SHOW INFORMATION',         1,  0, '◈'),
    ('arrival_parking',  'ARRIVAL & PARKING',        2,  1, '◈'),
    ('security',         'SECURITY',                 3,  1, '◈'),
    ('hospitality',      'HOSPITALITY',              4,  1, '◈'),
    ('front_of_house',   'FRONT OF HOUSE',           5,  1, '◈'),
    ('audio_section',    'AUDIO',                    6,  1, '◈'),
    ('video_section',    'VIDEO / PROJECTION',       7,  1, '◈'),
    ('backline_section', 'BACKLINE',                 8,  1, '◈'),
    ('stage_props',      'STAGE & PROPS',            9,  1, '◈'),
    ('wardrobe',         'WARDROBE',                 10, 1, '◈'),
    ('special_elements', 'SPECIAL / OTHER ELEMENTS', 11, 1, '◈'),
    ('labor_needs',      'LABOR NEEDS',              12, 1, '◈'),
    ('general_info',     'GENERAL INFORMATION',      13, 1, '◈'),
]

# (section_key, field_key, label, field_type, sort_order,
#  options_json, contact_dept, conditional_show_when,
#  help_text, placeholder, width_hint, is_notes_field)
FORM_FIELDS_SEED = [
    # ── Show Information ──────────────────────────────────────────────────────
    ('show_info', 'show_name',           'SHOW NAME',                  'text',             10,  None, None, None, None, '',                       'full',  0),
    ('show_info', 'show_date',           'SHOW DATE',                  'date',             20,  None, None, None, None, '',                       'half',  0),
    ('show_info', 'show_time',           'SHOW TIME(S)',               'text',             30,  None, None, None, None, 'e.g. 7pm and 9pm',       'half',  0),
    ('show_info', 'venue',               'VENUE',                      'text',             40,  None, None, None, None, '',                       'full',  0),
    ('show_info', 'production_manager',  'PRODUCTION MANAGER',         'contact_dropdown', 50,  None, 'Production',    None, None, '',            'full',  0),
    ('show_info', 'performance_company',  'PERFORMANCE COMPANY',        'text',             62,  None, None, None, None, 'Touring company / artist', 'full', 0),
    ('show_info', 'tour_manager',        'TOUR MANAGER',               'text',             60,  None, None, None, None, 'Name · email / phone',   'full',  0),
    ('show_info', 'promoter',            'PROMOTER',                   'text',             70,  None, None, None, None, '',                       'full',  0),
    ('show_info', 'additional_contacts', 'ADDITIONAL CONTACTS',        'textarea',         80,  None, None, None, None, 'Name, role, phone/email...', 'full', 0),
    ('show_info', 'programming',         'PROGRAMMING',                'contact_dropdown', 90,  None, 'Programming',   None, None, '',            'full',  0),
    ('show_info', 'events',              'EVENTS',                     'contact_dropdown', 100, None, 'Event Manager', None, None, '',            'full',  0),
    ('show_info', 'hospitality_contact', 'HOSPITALITY',                'contact_dropdown', 110, None, 'Hospitality',   None, None, '',            'full',  0),
    ('show_info', 'guest_services',      'GUEST SERVICES',             'contact_dropdown', 120, None, 'Guest Services',None, None, '',            'full',  0),
    ('show_info', 'radio_channel',       'RADIO CHANNEL',              'text',             130, None, None, None, None, "e.g. 16/Judson's",       'half',  0),
    ('show_info', 'rental_works',        'RENTAL WORKS?',              'yes_no',           150, None, None, None, None, '',                       'half',  0),
    ('show_info', 'rentalworks_order_num','RENTALWORKS ORDER #',       'text',             155, None, None, 'rental_works=Yes', None, 'Order number', 'half', 0),
    ('show_info', 'mix_position',        'MIX POSITION',               'text',             158, None, None, None, None, 'e.g. FOH',               'half',  0),
    ('show_info', 'show_length',         'SHOW LENGTH',                'text',             159, None, None, None, None, 'e.g. 1hr 45min',         'half',  0),
    ('show_info', 'budget_what',         'BUDGET / ESTIMATE — WHAT',   'text',             160, None, None, None, None, 'What',                   'half',  0),
    ('show_info', 'budget_amount',       'BUDGET / ESTIMATE — AMOUNT', 'text',             170, None, None, None, None, 'Amount',                 'half',  0),

    # ── Arrival & Parking ─────────────────────────────────────────────────────
    ('arrival_parking', 'access_time',                 'ACCESS TIME TO BUILDING',                  'text',     10,  None, None, None, None, 'e.g. 3:30pm',      'half', 0),
    ('arrival_parking', 'loading_dock',                'LOADING DOCK — WHICH BAY(S)?',             'select',   20,
        json.dumps(['-', 'N/A', 'Bay 1', 'Bay 2', 'Bay 3', 'Bay 4', 'Bay 5', 'Bay 1+2', 'Bay 1+2+3', 'Other — See Notes']),
        None, None, None, '', 'half', 0),
    ('arrival_parking', 'vehicle_type',                'VEHICLE TYPE',                             'select',   30,
        json.dumps(['-', 'dpc Van (15-passenger)', 'dpc Truck', 'Rental Vehicle', 'Other']),
        None, None, None, '', 'half', 0),
    ('arrival_parking', 'vehicle_notes',               'VEHICLE NOTES',                            'text',     35,  None, None, 'vehicle_type=Other', None, 'Describe vehicle...', 'half', 0),
    ('arrival_parking', 'runner_needed',               'RUNNER NEEDED?',                           'yes_no',   80,  None, None, None, None, '', 'third', 0),
    ('arrival_parking', 'rental_car_needed',           'RENTAL CAR NEEDED?',                       'yes_no',   90,  None, None, None, None, '', 'third', 0),
    ('arrival_parking', 'rental_drop_offs',            'RENTAL DROP-OFFS?',                        'yes_no',   100, None, None, None, None, '', 'third', 0),
    ('arrival_parking', 'runner_contact',              'RUNNER',                                   'contact_dropdown', 105, None, 'Runners', 'runner_needed=Yes', None, '', 'half', 0),
    ('arrival_parking', 'runner_time',                 'RUNNER PICKUP TIME',                       'text',     110, None, None, 'runner_needed=Yes', None, 'e.g. 2:00pm', 'half', 0),
    ('arrival_parking', 'runner_vehicle',              'RUNNER VEHICLE',                           'select',   120,
        json.dumps(['-', 'dpc Van', 'dpc Truck', 'Rental Vehicle', 'Other']),
        None, 'runner_needed=Yes', None, '', 'half', 0),
    ('arrival_parking', 'parking_validations',         'PARKING VALIDATIONS NEEDED?',              'yes_no',   130, None, None, None, None, '', 'half', 0),
    ('arrival_parking', 'parking_validations_count',   'HOW MANY?',                                'number',   140, None, None, 'parking_validations=Yes', None, '', 'half', 0),
    ('arrival_parking', 'special_accommodations',      'SPECIAL ACCOMMODATIONS NEEDED?',           'yes_no',   150, None, None, None, None, '', 'half', 0),
    ('arrival_parking', 'special_accommodations_details','DETAILS',                                'textarea', 160, None, None, 'special_accommodations=Yes', None, '', 'full', 0),
    ('arrival_parking', 'additional_space',            'ADDITIONAL HOLDING / REHEARSAL SPACE NEEDED?', 'yes_no', 170, None, None, None, None, '', 'full', 0),
    ('arrival_parking', 'additional_space_details',    'DETAILS',                                  'textarea', 180, None, None, 'additional_space=Yes', None, '', 'full', 0),
    ('arrival_parking', 'arrival_notes',               'ARRIVAL & PARKING NOTES',                  'textarea', 190, None, None, None, None, 'Additional arrival and parking notes...', 'full', 1),

    # ── Security ──────────────────────────────────────────────────────────────
    ('security', 'backstage_headcount',    'HOW MANY PEOPLE BACKSTAGE (Cast/Crew/Staff)?', 'number',   10, None, None, None, None, '0',          'half', 0),
    ('security', 'credentials_badges',    'CREDENTIALS / BADGES?',                         'select',   20,
        json.dumps(['-', 'Yes - Tour Provided', 'No - Use dpc Lanyards']),
        None, None, None, '', 'half', 0),
    ('security', 'extra_security',        'EXTRA SECURITY NEEDS?',                         'yes_no',   30, None, None, None, None, '',           'half', 0),
    ('security', 'extra_security_details','DETAILS',                                        'textarea', 40, None, None, 'extra_security=Yes', None, '', 'full', 0),
    ('security', 'security_meeting',      'SECURITY MEETING NEEDED?',                      'yes_no',   50, None, None, None, None, '',           'half', 0),
    ('security', 'security_meeting_time', 'SECURITY MEETING TIME',                         'text',     60, None, None, 'security_meeting=Yes', None, 'e.g. 5:30pm', 'half', 0),
    ('security', 'security_notes',        'SECURITY NOTES',                                'textarea', 70, None, None, None, None, 'Security notes...', 'full', 1),

    # ── Hospitality ───────────────────────────────────────────────────────────
    ('hospitality', 'food_beverage',         'SPECIFIC FOOD & BEVERAGE NEEDS?', 'yes_no',   10, None, None, None, None, '', 'half', 0),
    ('hospitality', 'food_beverage_details', 'DETAILS',                          'textarea', 20, None, None, 'food_beverage=Yes', None, '', 'full', 0),
    ('hospitality', 'allergies',             'ALLERGIES TO BE AWARE OF?',        'yes_no',   30, None, None, None, None, '', 'half', 0),
    ('hospitality', 'allergies_details',     'DETAILS',                          'textarea', 40, None, None, 'allergies=Yes', None, '', 'full', 0),
    ('hospitality', 'hospitality_notes',     'HOSPITALITY NOTES',                'textarea', 50, None, None, None, None, 'Hospitality notes...', 'full', 1),

    # ── Front of House ────────────────────────────────────────────────────────
    ('front_of_house', 'foh_contact',            'FOH CONTACT',              'text',     10, None, None, None, None, 'Name / contact info', 'half', 0),
    ('front_of_house', 'foh_activations',        'SPECIAL FOH ACTIVATIONS?', 'yes_no',   20, None, None, None, None, '',                    'half', 0),
    ('front_of_house', 'foh_activations_details','DETAILS',                  'textarea', 30, None, None, 'foh_activations=Yes', None, '', 'full', 0),
    ('front_of_house', 'foh_notes',              'FRONT-OF-HOUSE NOTES',     'textarea', 40, None, None, None, None, 'FOH notes...', 'full', 1),

    # ── Audio ─────────────────────────────────────────────────────────────────
    ('audio_section', 'audio_foh_engineer',   'FOH ENGINEER?',         'yes_no',   10, None, None, None, None, '',               'half', 0),
    ('audio_section', 'audio_microphones',    'MICROPHONES',           'select',   20,
        json.dumps(['-', 'Venue Provided', 'Tour Provided', 'N/A']),
        None, None, None, '', 'half', 0),
    ('audio_section', 'audio_mic_count',      'MIC COUNT',             'number',   30, None, None, None, None, '0',              'third', 0),
    ('audio_section', 'audio_mic_types',      'MIC TYPES',             'text',     40, None, None, None, None, 'e.g. SM58, DI',  'full',  0),
    ('audio_section', 'audio_monitors',       'MONITORS',              'select',   50,
        json.dumps(['-', 'Venue Provided', 'Tour Provided', 'In-Ears', 'N/A']),
        None, None, None, '', 'half', 0),
    ('audio_section', 'audio_inears',         'IN-EARS?',              'yes_no',   60, None, None, None, None, '',               'half', 0),
    ('audio_section', 'audio_playback',       'PLAYBACK?',             'yes_no',   70, None, None, None, None, '',               'half', 0),
    ('audio_section', 'audio_recording',      'RECORDING?',            'yes_no',   80, None, None, None, None, '',               'half', 0),
    ('audio_section', 'audio_notes',          'AUDIO NOTES',           'textarea', 90, None, None, None, None, 'Audio notes...', 'full', 1),

    # ── Video / Projection ───────────────────────────────────────────────────
    ('video_section', 'video_projector_needed', 'PROJECTOR / VIDEO NEEDED?', 'yes_no',   10, None, None, None, None, '', 'half', 0),
    ('video_section', 'video_notes',            'VIDEO NOTES',               'textarea', 20, None, None, None, None, 'Video/projection notes...', 'full', 1),

    # ── Backline ─────────────────────────────────────────────────────────────
    ('backline_section', 'backline_piano',         'PIANO?',                    'yes_no',   10, None, None, None, None, '', 'half', 0),
    ('backline_section', 'backline_piano_notes',   'PIANO DETAILS',             'text',     20, None, None, 'backline_piano=Yes', None, 'Type, tuning...', 'half', 0),
    ('backline_section', 'backline_tuning',        'PIANO TUNING NEEDED?',      'yes_no',   30, None, None, 'backline_piano=Yes', None, '', 'half', 0),
    ('backline_section', 'backline_tuning_time',   'TUNING TIME',               'text',     40, None, None, 'backline_tuning=Yes', None, 'e.g. 4:00pm', 'half', 0),
    ('backline_section', 'backline_own_gear',      'TOUR BRINGS OWN GEAR?',     'yes_no',   50, None, None, None, None, '', 'half', 0),
    ('backline_section', 'backline_own_gear_list', 'GEAR LIST',                 'textarea', 60, None, None, 'backline_own_gear=Yes', None, 'List tour gear...', 'full', 0),
    ('backline_section', 'backline_rental_needed', 'RENTAL GEAR NEEDED?',       'yes_no',   70, None, None, None, None, '', 'half', 0),
    ('backline_section', 'backline_notes',         'BACKLINE NOTES',            'textarea', 80, None, None, None, None, 'Backline notes...', 'full', 1),

    # ── Stage & Props ─────────────────────────────────────────────────────────
    ('stage_props', 'stage_plot',              'STAGE PLOT?',               'yes_no',   10, None, None, None, None, '', 'half', 0),
    ('stage_props', 'music_stands',            'MUSIC STANDS?',             'yes_no',   20, None, None, None, None, '', 'half', 0),
    ('stage_props', 'musician_chairs',         'MUSICIAN CHAIRS?',          'yes_no',   30, None, None, None, None, '', 'half', 0),
    ('stage_props', 'other_equipment',         'OTHER EQUIPMENT?',          'yes_no',   40, None, None, None, None, '', 'half', 0),
    ('stage_props', 'other_equipment_list',    'EQUIPMENT LIST',            'textarea', 50, None, None, 'other_equipment=Yes', None, 'List required equipment...', 'full', 0),
    ('stage_props', 'stage_notes',             'STAGE & PROPS NOTES',       'textarea', 60, None, None, None, None, 'Stage notes...', 'full', 1),

    # ── Wardrobe ─────────────────────────────────────────────────────────────
    ('wardrobe', 'wardrobe_dressing_room', 'DRESSING ROOM NEEDED?',     'yes_no',   10, None, None, None, None, '', 'third', 0),
    ('wardrobe', 'wardrobe_equipment',     'WARDROBE EQUIPMENT?',        'yes_no',   20, None, None, None, None, '', 'third', 0),
    ('wardrobe', 'wardrobe_towels',        'TOWELS NEEDED?',             'yes_no',   30, None, None, None, None, '', 'third', 0),
    ('wardrobe', 'wardrobe_notes',         'WARDROBE NOTES',             'textarea', 40, None, None, None, None, 'Wardrobe notes...', 'full', 1),

    # ── Special / Other Elements ──────────────────────────────────────────────
    ('special_elements', 'special_elements_desc', 'SPECIAL ELEMENTS',      'textarea', 10, None, None, None, None, 'Describe any special requirements, effects, or elements...', 'full', 0),
    ('special_elements', 'haze_fog_needed',        'HAZE / FOG NEEDED?',    'yes_no',   20, None, None, None, None, '', 'half', 0),
    ('special_elements', 'special_notes',          'SPECIAL NOTES',         'textarea', 30, None, None, None, None, 'Additional special notes...', 'full', 1),

    # ── Labor Needs ───────────────────────────────────────────────────────────
    ('labor_needs', 'labor_load_in',          'LOAD-IN LABOR?',            'yes_no',   10, None, None, None, None, '', 'third', 0),
    ('labor_needs', 'labor_show_call',        'SHOW CALL LABOR?',          'yes_no',   20, None, None, None, None, '', 'third', 0),
    ('labor_needs', 'labor_load_out',         'LOAD-OUT LABOR?',           'yes_no',   30, None, None, None, None, '', 'third', 0),
    ('labor_needs', 'labor_estimate_needed',  'ESTIMATE NEEDED?',          'yes_no',   40, None, None, None, None, '', 'half', 0),
    ('labor_needs', 'labor_notes',            'LABOR NOTES',               'textarea', 50, None, None, None, None, 'Labor requirements...', 'full', 1),

    # ── General Information ───────────────────────────────────────────────────
    ('general_info', 'load_in_needed',  'LOAD-IN TIME NEEDED?', 'yes_no',   10, None, None, None, None, '', 'full', 0),
    ('general_info', 'load_in_details', 'DETAILS',              'textarea', 20, None, None, 'load_in_needed=Yes', None, '', 'full', 0),
    ('general_info', 'general_notes',   'GENERAL NOTES',        'textarea', 30, None, None, None, None, 'General notes...', 'full', 1),
]

# (category_name, sort_order)
POSITION_CATEGORIES_SEED = [
    ('Audio',     10),
    ('Lighting',  20),
    ('Video',     30),
    ('Stage',     40),
    ('Other',     50),
]

# (category_name, position_name, sort_order)
JOB_POSITIONS_SEED = [
    ('Audio',    'A1',                    10),
    ('Audio',    'A2',                    20),
    ('Audio',    'Monitor Engineer',      30),
    ('Audio',    'RF Technician',         40),
    ('Audio',    'Audio Technician',      50),
    ('Lighting', 'Lighting Designer',     10),
    ('Lighting', 'Lighting Technician',   20),
    ('Lighting', 'Followspot Operator',   30),
    ('Video',    'Video Director',        10),
    ('Video',    'Video Technician',      20),
    ('Video',    'Camera Operator',       30),
    ('Stage',    'Stage Manager',         10),
    ('Stage',    'Stage Hand',            20),
    ('Stage',    'Fly Technician',        30),
    ('Other',    'Production Manager',    10),
    ('Other',    'Runner',                20),
]

APP_SETTINGS_SEED = [
    # Server
    ('app_port',              '5400'),
    # Syslog
    ('syslog_enabled',        '0'),
    ('syslog_host',           '127.0.0.1'),
    ('syslog_port',           '514'),
    ('syslog_facility',       'LOG_LOCAL0'),
    # Venue list (JSON array)
    ('venue_list',            json.dumps(["Judson's Live", "Walt Disney Theater", "Alexis & Jim Pugh Theater", "Dr. Phillips CenterStage"])),
    # WiFi defaults
    ('wifi_network',          ''),
    ('wifi_password',         ''),
    # Upload limit
    ('upload_max_mb',         '20'),
    # Logo (base64 encoded image data, empty = no logo)
    ('logo_data',             ''),
]




def _seed_form_data(db):
    """Seed form_sections and form_fields if form_sections is empty."""
    if db.execute('SELECT COUNT(*) FROM form_sections').fetchone()[0] > 0:
        return

    section_id_map = {}
    for (section_key, label, sort_order, collapsible, icon) in FORM_SECTIONS_SEED:
        row = db.execute(
            """INSERT INTO form_sections
               (section_key, label, sort_order, collapsible, icon)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (section_key) DO NOTHING
               RETURNING id""",
            (section_key, label, sort_order, collapsible, icon)
        ).fetchone()
        if row is None:
            row = db.execute('SELECT id FROM form_sections WHERE section_key=%s',
                             (section_key,)).fetchone()
        section_id_map[section_key] = row['id']

    for row in FORM_FIELDS_SEED:
        (section_key, field_key, label, field_type, sort_order,
         options_json, contact_dept, conditional_show_when,
         help_text, placeholder, width_hint, is_notes_field) = row
        db.execute(
            """INSERT INTO form_fields
               (section_id, field_key, label, field_type, sort_order,
                options_json, contact_dept, conditional_show_when,
                help_text, placeholder, width_hint, is_notes_field)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (field_key) DO NOTHING""",
            (section_id_map[section_key], field_key, label, field_type, sort_order,
             options_json, contact_dept, conditional_show_when,
             help_text, placeholder, width_hint, is_notes_field)
        )
    print(f"  Seeded {len(FORM_SECTIONS_SEED)} sections and {len(FORM_FIELDS_SEED)} fields")


SCHEDULE_META_FIELDS_SEED = [
    # (field_key, label, field_type, advance_field_key, sort_order, width_hint)
    # wifi_network and wifi_code removed — WiFi comes from global Settings only
    ('radio_channel',    'RADIO CHANNEL',           'text', 'radio_channel', 30, 'half'),
    ('mix_position',     'MIX POSITION',            'text', 'mix_position',  40, 'half'),
    ('parking_security', 'PARKING & SECURITY INFO', 'text', None,            50, 'full'),
]


def _seed_schedule_meta_fields(db):
    """Seed schedule_meta_fields with defaults if the table is empty."""
    if db.execute('SELECT COUNT(*) FROM schedule_meta_fields').fetchone()[0] > 0:
        return
    for (fk, lbl, ft, afk, so, wh) in SCHEDULE_META_FIELDS_SEED:
        db.execute(
            """INSERT INTO schedule_meta_fields
               (field_key, label, field_type, advance_field_key, sort_order, width_hint)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (field_key) DO NOTHING""",
            (fk, lbl, ft, afk, so, wh)
        )
    print(f"  Seeded {len(SCHEDULE_META_FIELDS_SEED)} schedule meta fields")


def _seed_job_positions(db):
    """Seed position_categories and job_positions if position_categories is empty."""
    if db.execute('SELECT COUNT(*) FROM position_categories').fetchone()[0] > 0:
        return
    cat_id_map = {}
    for (cat_name, sort_order) in POSITION_CATEGORIES_SEED:
        cat_id_map[cat_name] = db.execute(
            'INSERT INTO position_categories (name, sort_order) VALUES (%s, %s) RETURNING id',
            (cat_name, sort_order)
        ).fetchone()['id']
    for (cat_name, pos_name, sort_order) in JOB_POSITIONS_SEED:
        db.execute(
            'INSERT INTO job_positions (category_id, name, sort_order) VALUES (%s, %s, %s)',
            (cat_id_map.get(cat_name), pos_name, sort_order)
        )
    print(f"  Seeded {len(POSITION_CATEGORIES_SEED)} position categories and {len(JOB_POSITIONS_SEED)} job positions")


def _seed_app_settings(db):
    """Seed app_settings defaults if the table is empty (fresh install only —
    never back-fills keys into a live deployment)."""
    if db.execute('SELECT COUNT(*) FROM app_settings').fetchone()[0] > 0:
        return
    for (key, value) in APP_SETTINGS_SEED:
        db.execute(
            'INSERT INTO app_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING',
            (key, value)
        )
    print(f"  Seeded {len(APP_SETTINGS_SEED)} app settings")


def _seed_contacts(db):
    """Seed the default contact list if the contacts table is empty."""
    if db.execute('SELECT COUNT(*) FROM contacts').fetchone()[0] > 0:
        return
    db.executemany(
        'INSERT INTO contacts (name, title, department, phone, email) VALUES (%s, %s, %s, %s, %s)',
        SEED_CONTACTS)
    print(f"  Seeded {len(SEED_CONTACTS)} contacts")


def _seed_admin_user(db):
    """Create admin/admin123 (forced password change) only when the users
    table is completely empty. users is the shared cross-app directory, so an
    install joining an existing directory never gets a default admin."""
    if db.execute('SELECT COUNT(*) FROM users').fetchone()[0] > 0:
        return False
    from werkzeug.security import generate_password_hash
    db.execute("""
        INSERT INTO users (username, password_hash, display_name, role, must_change_password)
        VALUES (%s, %s, %s, %s, 1)
        ON CONFLICT (username) DO NOTHING
    """, ('admin', generate_password_hash('admin123'), 'Administrator', 'admin'))
    return True


PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    role TEXT DEFAULT 'user',
    theme TEXT DEFAULT 'dark',
    last_login TIMESTAMP,
    last_conn_path TEXT DEFAULT '',
    last_conn_ip TEXT DEFAULT '',
    last_conn_at TIMESTAMP,
    must_change_password INTEGER DEFAULT 0,
    email TEXT DEFAULT '',
    is_readonly INTEGER DEFAULT 0,
    email_confirmed INTEGER DEFAULT 1,
    pending_approval INTEGER DEFAULT 0,
    is_scheduler INTEGER DEFAULT 0,
    is_asset_manager INTEGER DEFAULT 0,
    is_document_viewer INTEGER DEFAULT 0,
    viewer_venues TEXT DEFAULT NULL,
    viewer_doc_types TEXT DEFAULT NULL,
    -- Extra read-only grant for document viewers: when set, the viewer is also
    -- allowed to reach the (read-only) Labor Overview page in addition to /viewer.
    viewer_labor_overview INTEGER DEFAULT 0,
    -- Extra read-only grant for document viewers: when set, the viewer also gets
    -- a Show Calendar page limited to the shows their venue allow-list permits.
    viewer_show_calendar INTEGER DEFAULT 0,
    -- Per-user home dashboard layout preference.
    home_layout TEXT DEFAULT 'columns',    -- 'columns' | 'stacked'
    home_density TEXT DEFAULT 'normal',    -- 'normal'  | 'slim'
    -- Cross-application account flags. These live in the shared user
    -- directory so OTHER apps that point at the same database can read
    -- them. 321Theater itself never consults these columns, so they have
    -- no effect on anything in this app.
    is_app_user INTEGER DEFAULT 0,
    is_app_admin INTEGER DEFAULT 0,
    -- Account lock: when set, the user cannot log in and any active session
    -- is invalidated on the next role refresh. Purely a login gate — it does
    -- NOT delete or touch any data the user authored, so a locked account
    -- preserves the historical record on shows/events untouched.
    is_locked INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app_sessions (
    sid         TEXT PRIMARY KEY,
    user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
    data        TEXT NOT NULL DEFAULT '{}',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at  TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_app_sessions_expires ON app_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_app_sessions_user    ON app_sessions(user_id);

CREATE TABLE IF NOT EXISTS shows (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    show_date DATE,
    show_time TEXT DEFAULT '',
    load_in_date DATE DEFAULT NULL,
    load_in_time TEXT DEFAULT '',
    load_out_date DATE DEFAULT NULL,
    load_out_time TEXT DEFAULT '',
    venue TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    advance_version INTEGER DEFAULT 0,
    schedule_version INTEGER DEFAULT 0,
    postnotes_version INTEGER DEFAULT 0,
    cast_count INTEGER DEFAULT NULL,
    crew_count INTEGER DEFAULT NULL,
    performance_company TEXT DEFAULT '',
    is_test INTEGER DEFAULT 0,
    -- Prism event status tag ('Hold', 'Confirmed', ...) for shows imported
    -- from Prism. Kept current by the Prism sync. NULL = not a Prism import.
    prism_status TEXT DEFAULT NULL,
    -- Per-show classification: 'show' (default) or 'event'. Drives the home
    -- screen accent color and the per-recipient show/event email filter.
    show_mode TEXT DEFAULT 'show',
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    last_saved_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    last_saved_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS advance_data (
    id SERIAL PRIMARY KEY,
    show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    field_key TEXT NOT NULL,
    field_value TEXT DEFAULT '',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(show_id, field_key)
);

CREATE TABLE IF NOT EXISTS schedule_rows (
    id SERIAL PRIMARY KEY,
    show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    perf_id INTEGER DEFAULT NULL,
    day_date TEXT DEFAULT NULL,
    sort_order INTEGER DEFAULT 0,
    start_time TEXT DEFAULT '',
    end_time TEXT DEFAULT '',
    description TEXT DEFAULT '',
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS schedule_meta (
    id SERIAL PRIMARY KEY,
    show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    field_key TEXT NOT NULL,
    field_value TEXT DEFAULT '',
    UNIQUE(show_id, field_key)
);

CREATE TABLE IF NOT EXISTS post_show_notes (
    id SERIAL PRIMARY KEY,
    show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    field_key TEXT NOT NULL,
    field_value TEXT DEFAULT '',
    UNIQUE(show_id, field_key)
);

CREATE TABLE IF NOT EXISTS show_performances (
    id SERIAL PRIMARY KEY,
    show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    perf_date DATE,
    perf_time TEXT DEFAULT '',
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contacts (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    title TEXT DEFAULT '',
    department TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    email TEXT DEFAULT '',
    sort_order INTEGER DEFAULT 0,
    report_recipient     INTEGER DEFAULT 0,
    advance_recipient    INTEGER DEFAULT 0,
    production_recipient INTEGER DEFAULT 0,
    postnotes_recipient  INTEGER DEFAULT 0,
    system_recipient     INTEGER DEFAULT 0,
    venue_filter         TEXT DEFAULT NULL,
    mode_filter          TEXT DEFAULT NULL,
    user_id              INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- Partial unique index: at most one contact per user (NULLs allowed).
-- Authoritative dedup for the per-worker user→contact backfill race.
CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_user_unique
    ON contacts(user_id) WHERE user_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS export_log (
    id SERIAL PRIMARY KEY,
    show_id INTEGER REFERENCES shows(id) ON DELETE SET NULL,
    export_type TEXT,
    version INTEGER,
    exported_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    exported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    filename TEXT DEFAULT '',
    pdf_data BYTEA,
    s3_key TEXT DEFAULT NULL,
    content_hash TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS form_sections (
    id SERIAL PRIMARY KEY,
    section_key TEXT UNIQUE NOT NULL,
    label TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0,
    collapsible INTEGER DEFAULT 1,
    icon TEXT DEFAULT '◈',
    default_open INTEGER DEFAULT 1,
    asset_category_id INTEGER REFERENCES asset_categories(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS arts_groups (
    id                    SERIAL PRIMARY KEY,
    name                  TEXT UNIQUE NOT NULL,
    sort_order            INTEGER DEFAULT 0,
    primary_contact_name  TEXT DEFAULT '',
    primary_contact_email TEXT DEFAULT '',
    primary_contact_phone TEXT DEFAULT '',
    notes                 TEXT DEFAULT '',
    created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS arts_group_contacts (
    id             SERIAL PRIMARY KEY,
    arts_group_id  INTEGER NOT NULL REFERENCES arts_groups(id) ON DELETE CASCADE,
    name           TEXT DEFAULT '',
    email          TEXT DEFAULT '',
    phone          TEXT DEFAULT '',
    sort_order     INTEGER DEFAULT 0,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agc_group ON arts_group_contacts(arts_group_id);

CREATE TABLE IF NOT EXISTS form_fields (
    id SERIAL PRIMARY KEY,
    section_id INTEGER NOT NULL REFERENCES form_sections(id) ON DELETE CASCADE,
    field_key TEXT UNIQUE NOT NULL,
    label TEXT NOT NULL,
    field_type TEXT NOT NULL DEFAULT 'text',
    sort_order INTEGER DEFAULT 0,
    options_json TEXT DEFAULT NULL,
    contact_dept TEXT DEFAULT NULL,
    conditional_show_when TEXT DEFAULT NULL,
    help_text TEXT DEFAULT NULL,
    placeholder TEXT DEFAULT '',
    width_hint TEXT DEFAULT 'full',
    is_notes_field INTEGER DEFAULT 0,
    ai_hint TEXT DEFAULT NULL,
    display_as TEXT DEFAULT NULL,
    allow_multi INTEGER DEFAULT 0,
    auto_select_visible INTEGER DEFAULT 0,
    hide_from_pdf INTEGER DEFAULT 0,
    upload_button_only INTEGER DEFAULT 0,
    notes_content TEXT DEFAULT NULL,
    alert_departments TEXT DEFAULT NULL,
    alert_contact_ids TEXT DEFAULT NULL,
    pdf_template_id INTEGER DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS pdf_templates (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    pdf_data BYTEA,
    s3_key TEXT DEFAULT NULL,
    fields_json TEXT DEFAULT '[]',
    page_count INTEGER DEFAULT 1,
    created_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pdf_submissions (
    id SERIAL PRIMARY KEY,
    show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    field_key TEXT NOT NULL,
    template_id INTEGER REFERENCES pdf_templates(id) ON DELETE SET NULL,
    template_fields_snapshot TEXT DEFAULT NULL,
    values_json TEXT DEFAULT '{}',
    status TEXT DEFAULT 'draft',
    last_saved_by INTEGER,
    last_saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(show_id, field_key)
);
CREATE INDEX IF NOT EXISTS idx_pdf_submissions_show ON pdf_submissions(show_id);

CREATE TABLE IF NOT EXISTS field_alert_state (
    id SERIAL PRIMARY KEY,
    show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    field_key TEXT NOT NULL,
    last_alerted_value TEXT DEFAULT NULL,
    last_alerted_hash  TEXT DEFAULT NULL,
    last_alerted_at    TIMESTAMP DEFAULT NULL,
    pending_value      TEXT DEFAULT NULL,
    pending_prev_value TEXT DEFAULT NULL,
    pending_hash       TEXT DEFAULT NULL,
    pending_updated_at TIMESTAMP DEFAULT NULL,
    pending_updated_by INTEGER,
    UNIQUE(show_id, field_key)
);
CREATE INDEX IF NOT EXISTS idx_field_alert_pending
    ON field_alert_state(pending_updated_at);

CREATE TABLE IF NOT EXISTS notifications (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'system',
    title TEXT NOT NULL,
    body TEXT DEFAULT '',
    link_url TEXT DEFAULT NULL,
    show_id INTEGER REFERENCES shows(id) ON DELETE SET NULL,
    field_key TEXT DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    read_at TIMESTAMP DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_user_unread
    ON notifications(user_id, read_at);
CREATE INDEX IF NOT EXISTS idx_notifications_user_created
    ON notifications(user_id, created_at);

CREATE TABLE IF NOT EXISTS form_history (
    id SERIAL PRIMARY KEY,
    show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    form_type TEXT NOT NULL,
    saved_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    snapshot_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS active_sessions (
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    show_id       INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    tab           TEXT NOT NULL DEFAULT 'advance',
    focused_field TEXT,
    last_seen     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, show_id)
);

CREATE TABLE IF NOT EXISTS show_comments (
    id         SERIAL PRIMARY KEY,
    show_id    INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body       TEXT NOT NULL,
    deleted_at TIMESTAMP,
    deleted_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    edited_at  TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS show_attachments (
    id          SERIAL PRIMARY KEY,
    show_id     INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    uploaded_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    filename    TEXT NOT NULL,
    mime_type   TEXT DEFAULT 'application/octet-stream',
    file_data   BYTEA,
    file_size   INTEGER DEFAULT 0,
    s3_key      TEXT DEFAULT NULL,
    field_key   TEXT DEFAULT NULL,
    description TEXT DEFAULT '',
    deleted_at    TIMESTAMP DEFAULT NULL,
    deleted_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    is_compressed INTEGER DEFAULT 0,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_show_attachments_show ON show_attachments(show_id);

CREATE TABLE IF NOT EXISTS advance_reads (
    id           SERIAL PRIMARY KEY,
    show_id      INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    version_read INTEGER DEFAULT 0,
    read_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(show_id, user_id)
);

CREATE TABLE IF NOT EXISTS schedule_meta_fields (
    id               SERIAL PRIMARY KEY,
    field_key        TEXT UNIQUE NOT NULL,
    label            TEXT NOT NULL,
    field_type       TEXT DEFAULT 'text',
    advance_field_key TEXT DEFAULT NULL,
    sort_order       INTEGER DEFAULT 0,
    width_hint       TEXT DEFAULT 'half',
    show_in_contacts INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS schedule_templates (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS schedule_template_rows (
    id          SERIAL PRIMARY KEY,
    template_id INTEGER NOT NULL REFERENCES schedule_templates(id) ON DELETE CASCADE,
    sort_order  INTEGER DEFAULT 0,
    start_time  TEXT DEFAULT '',
    end_time    TEXT DEFAULT '',
    description TEXT DEFAULT '',
    notes       TEXT DEFAULT ''
);

-- Labor day presets: named sets of standing position calls applyable to a
-- show labor day (labor_preset_rows.quantity expands to N labor_requests).
CREATE TABLE IF NOT EXISTS labor_presets (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS labor_preset_rows (
    id           SERIAL PRIMARY KEY,
    preset_id    INTEGER NOT NULL REFERENCES labor_presets(id) ON DELETE CASCADE,
    sort_order   INTEGER DEFAULT 0,
    position_id  INTEGER REFERENCES job_positions(id) ON DELETE SET NULL,
    quantity     INTEGER DEFAULT 1,
    in_time      TEXT DEFAULT '',
    out_time     TEXT DEFAULT '',
    break_start  TEXT DEFAULT '',
    break_end    TEXT DEFAULT '',
    break2_start TEXT DEFAULT '',
    break2_end   TEXT DEFAULT '',
    notes        TEXT DEFAULT ''
);

-- ── Labor & Crew ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS position_categories (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    is_venue   INTEGER DEFAULT 0,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pay_rate_levels (
    id                 SERIAL PRIMARY KEY,
    name               TEXT NOT NULL,
    hourly_rate        DOUBLE PRECISION DEFAULT 0.0,
    include_in_estimate INTEGER DEFAULT 1,
    sort_order         INTEGER DEFAULT 0,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS job_positions (
    id            SERIAL PRIMARY KEY,
    category_id   INTEGER REFERENCES position_categories(id) ON DELETE SET NULL,
    name          TEXT NOT NULL,
    override_rate DOUBLE PRECISION DEFAULT NULL,
    sort_order    INTEGER DEFAULT 0,
    is_training   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS labor_requests (
    id                        SERIAL PRIMARY KEY,
    show_id                   INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    position_id               INTEGER REFERENCES job_positions(id) ON DELETE SET NULL,
    work_date                 DATE,
    in_time                   TEXT DEFAULT '',
    out_time                  TEXT DEFAULT '',
    break_start               TEXT DEFAULT '',
    break_end                 TEXT DEFAULT '',
    requested_name            TEXT DEFAULT '',
    is_scheduled              INTEGER DEFAULT 0,
    is_training_shift         INTEGER DEFAULT 0,
    scheduled_crew_member_id  INTEGER,
    scheduled_by              INTEGER REFERENCES users(id) ON DELETE SET NULL,
    scheduled_at              TIMESTAMP,
    sort_order                INTEGER DEFAULT 0,
    created_at                TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS crew_members (
    id             SERIAL PRIMARY KEY,
    name           TEXT NOT NULL,
    rate_level_id  INTEGER REFERENCES pay_rate_levels(id) ON DELETE SET NULL,
    sort_order     INTEGER DEFAULT 0,
    training_notes TEXT DEFAULT '',
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS crew_qualifications (
    crew_member_id INTEGER NOT NULL REFERENCES crew_members(id) ON DELETE CASCADE,
    position_id    INTEGER NOT NULL REFERENCES job_positions(id) ON DELETE CASCADE,
    status         INTEGER DEFAULT 2,
    PRIMARY KEY (crew_member_id, position_id)
);

CREATE TABLE IF NOT EXISTS labor_billable_items (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    cost_per_crew DOUBLE PRECISION DEFAULT 0.0,
    sort_order    INTEGER DEFAULT 0,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS show_labor_billable_items (
    show_id          INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    billable_item_id INTEGER NOT NULL REFERENCES labor_billable_items(id) ON DELETE CASCADE,
    PRIMARY KEY (show_id, billable_item_id)
);

-- Post-Show actual labor — a snapshot of the show's SCHEDULED labor lines that
-- the PM edits to bill ACTUALS, without ever touching the labor scheduler.
-- Pulled from the Post-Show tab: scheduled times are copied into the sched_*
-- columns (read-only reference), the PM enters the billable times in
-- in_time/out_time/break*. Blank actuals compute 0 hours → $0. pay_rate_snapshot
-- freezes the rate at pull time so later rate edits don't rewrite the invoice.
CREATE TABLE IF NOT EXISTS post_show_labor (
    id                 SERIAL PRIMARY KEY,
    show_id            INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    source_request_id  INTEGER REFERENCES labor_requests(id) ON DELETE SET NULL,
    position_id        INTEGER REFERENCES job_positions(id) ON DELETE SET NULL,
    work_date          DATE,
    sched_in_time      TEXT DEFAULT '',
    sched_out_time     TEXT DEFAULT '',
    sched_break_start  TEXT DEFAULT '',
    sched_break_end    TEXT DEFAULT '',
    sched_break2_start TEXT DEFAULT '',
    sched_break2_end   TEXT DEFAULT '',
    sched_crew_name    TEXT DEFAULT '',
    in_time            TEXT DEFAULT '',
    out_time           TEXT DEFAULT '',
    break_start        TEXT DEFAULT '',
    break_end          TEXT DEFAULT '',
    break2_start       TEXT DEFAULT '',
    break2_end         TEXT DEFAULT '',
    pay_rate_snapshot  DOUBLE PRECISION DEFAULT NULL,
    notes              TEXT DEFAULT '',
    is_added_hours     INTEGER DEFAULT 0,
    manual_hours       DOUBLE PRECISION DEFAULT NULL,
    crew_member_id     INTEGER,
    sort_order         INTEGER DEFAULT 0,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Per-day labor info for a show: the PM covering that day (when the show's
-- main PM is out) and day-specific notes. Both surface on the Labor Overview,
-- the Labor Scheduler, and the show's staffing tab. cover_pm stores the
-- contact NAME (same convention as the production_manager advance field).
CREATE TABLE IF NOT EXISTS show_labor_days (
    id         SERIAL PRIMARY KEY,
    show_id    INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    work_date  DATE NOT NULL,
    cover_pm   TEXT DEFAULT '',
    day_notes  TEXT DEFAULT '',
    updated_by INTEGER,
    updated_at TIMESTAMP,
    UNIQUE (show_id, work_date)
);

CREATE TABLE IF NOT EXISTS overhead_projects (
    id            SERIAL PRIMARY KEY,
    name          TEXT UNIQUE NOT NULL,
    description   TEXT DEFAULT '',
    client_name   TEXT DEFAULT '',
    billing_code  TEXT DEFAULT '',
    contact_name  TEXT DEFAULT '',
    contact_email TEXT DEFAULT '',
    contact_phone TEXT DEFAULT '',
    project_notes TEXT DEFAULT '',
    color         TEXT DEFAULT '',
    archived      INTEGER DEFAULT 0,
    sort_order    INTEGER DEFAULT 0,
    created_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS overhead_labor_groups (
    id            SERIAL PRIMARY KEY,
    work_date     DATE NOT NULL,
    project_id    INTEGER REFERENCES overhead_projects(id) ON DELETE SET NULL,
    name          TEXT NOT NULL DEFAULT 'General',
    contact_name  TEXT DEFAULT '',
    contact_email TEXT DEFAULT '',
    contact_phone TEXT DEFAULT '',
    project_notes TEXT DEFAULT '',
    sort_order    INTEGER DEFAULT 0,
    created_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS overhead_labor_requests (
    id                          SERIAL PRIMARY KEY,
    group_id                    INTEGER NOT NULL REFERENCES overhead_labor_groups(id) ON DELETE CASCADE,
    -- Provenance: which recurring template generated this row (NULL = manual).
    -- Plain INTEGER (not FK) because the templates table is defined later in
    -- this script and a forward reference fails on PostgreSQL. A stale id is
    -- harmless — we only use it to scope template-driven prunes / re-syncs.
    template_id                 INTEGER,
    work_date                   DATE NOT NULL,
    position_id                 INTEGER REFERENCES job_positions(id) ON DELETE SET NULL,
    in_time                     TEXT DEFAULT '',
    out_time                    TEXT DEFAULT '',
    break_start                 TEXT DEFAULT '',
    break_end                   TEXT DEFAULT '',
    requested_name              TEXT DEFAULT '',
    is_scheduled                INTEGER DEFAULT 0,
    is_training_shift           INTEGER DEFAULT 0,
    scheduled_crew_member_id    INTEGER,
    scheduled_by                INTEGER REFERENCES users(id) ON DELETE SET NULL,
    scheduled_at                TIMESTAMP,
    pay_rate_snapshot           DOUBLE PRECISION DEFAULT NULL,
    pay_rate_level_id_snapshot  INTEGER DEFAULT NULL,
    actual_in_time              TEXT DEFAULT '',
    actual_out_time             TEXT DEFAULT '',
    actual_break_start          TEXT DEFAULT '',
    actual_break_end            TEXT DEFAULT '',
    notes                       TEXT DEFAULT '',
    sort_order                  INTEGER DEFAULT 0,
    created_by                  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at                  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS overhead_labor_templates (
    id                     SERIAL PRIMARY KEY,
    name                   TEXT NOT NULL,
    position_id            INTEGER REFERENCES job_positions(id) ON DELETE SET NULL,
    quantity               INTEGER DEFAULT 1,
    days_of_week           TEXT DEFAULT '',
    start_date             DATE,
    end_date               DATE,
    in_time                TEXT DEFAULT '',
    out_time               TEXT DEFAULT '',
    break_start            TEXT DEFAULT '',
    break_end              TEXT DEFAULT '',
    default_group_name     TEXT DEFAULT 'Overhead',
    default_contact_name   TEXT DEFAULT '',
    default_contact_email  TEXT DEFAULT '',
    default_contact_phone  TEXT DEFAULT '',
    default_project_notes  TEXT DEFAULT '',
    is_active              INTEGER DEFAULT 1,
    last_generated_through DATE,
    sort_order             INTEGER DEFAULT 0,
    created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_oh_groups_date ON overhead_labor_groups(work_date);
CREATE INDEX IF NOT EXISTS idx_oh_requests_date ON overhead_labor_requests(work_date);
CREATE INDEX IF NOT EXISTS idx_oh_requests_group ON overhead_labor_requests(group_id);

-- ── Audit & Versioning ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS audit_log (
    id          SERIAL PRIMARY KEY,
    timestamp   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    username    TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT,
    show_id     INTEGER REFERENCES shows(id) ON DELETE SET NULL,
    before_json TEXT,
    after_json  TEXT,
    ip_address  TEXT,
    detail      TEXT,
    undone_at   TIMESTAMP,
    undone_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    undone_by_log_id INTEGER REFERENCES audit_log(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_log_user_id ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_show_id ON audit_log(show_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_action  ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts      ON audit_log(timestamp);

CREATE TABLE IF NOT EXISTS comment_versions (
    id         SERIAL PRIMARY KEY,
    comment_id INTEGER NOT NULL REFERENCES show_comments(id) ON DELETE CASCADE,
    body       TEXT NOT NULL,
    edited_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    edited_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS email_send_log (
    id               SERIAL PRIMARY KEY,
    show_id          INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    pdf_type         TEXT NOT NULL,
    trigger_type     TEXT NOT NULL,
    days_before      INTEGER,
    sent_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sent_by          TEXT DEFAULT '',
    recipient_count  INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_email_send_log_show ON email_send_log(show_id, pdf_type, sent_at);

-- Email send failures: one row per address that failed to deliver, so the
-- admin can review bounces, invalid addresses, transient SMTP errors, etc.
CREATE TABLE IF NOT EXISTS email_send_errors (
    id            SERIAL PRIMARY KEY,
    sent_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    recipient     TEXT DEFAULT '',
    subject       TEXT DEFAULT '',
    error_msg     TEXT DEFAULT '',
    smtp_code     TEXT DEFAULT '',
    pdf_type      TEXT DEFAULT '',
    show_id       INTEGER REFERENCES shows(id) ON DELETE SET NULL,
    triggered_by  TEXT DEFAULT '',
    resolved      INTEGER DEFAULT 0,
    resolved_at   TIMESTAMP,
    resolved_by   INTEGER REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_email_send_errors_unresolved
    ON email_send_errors(resolved, sent_at);
CREATE INDEX IF NOT EXISTS idx_email_send_errors_recipient
    ON email_send_errors(recipient);

-- General outgoing-email log: one row per attempted send (recipient + result).
CREATE TABLE IF NOT EXISTS email_outbox_log (
    id            SERIAL PRIMARY KEY,
    sent_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    recipient     TEXT NOT NULL,
    subject       TEXT NOT NULL DEFAULT '',
    purpose       TEXT NOT NULL DEFAULT '',
    success       INTEGER NOT NULL DEFAULT 0,
    error_message TEXT DEFAULT '',
    triggered_by  INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_email_outbox_log_sent ON email_outbox_log(sent_at DESC);

-- Per-venue logo overrides for PDF headers.
CREATE TABLE IF NOT EXISTS venue_logos (
    venue_name TEXT PRIMARY KEY,
    logo_data  TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Per-venue accent colors for PDF paperwork ('#rrggbb' hex, empty = app default).
CREATE TABLE IF NOT EXISTS venue_colors (
    venue_name      TEXT PRIMARY KEY,
    primary_color   TEXT NOT NULL DEFAULT '',
    secondary_color TEXT NOT NULL DEFAULT '',
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Security sign-in sheet personnel names (security_module.py, one row per
-- expected person per show, printed with a blank signature column).
CREATE TABLE IF NOT EXISTS security_signin_names (
    id         SERIAL PRIMARY KEY,
    show_id    INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0,
    created_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ssn_show ON security_signin_names(show_id);

-- ── Asset Manager ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS warehouse_locations (
    id         SERIAL PRIMARY KEY,
    name       TEXT UNIQUE NOT NULL,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS asset_categories (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS asset_types (
    id               SERIAL PRIMARY KEY,
    category_id      INTEGER NOT NULL REFERENCES asset_categories(id) ON DELETE CASCADE,
    parent_type_id   INTEGER REFERENCES asset_types(id) ON DELETE SET NULL,
    name             TEXT NOT NULL,
    manufacturer     TEXT DEFAULT '',
    model            TEXT DEFAULT '',
    photo            BYTEA,
    photo_mime       TEXT DEFAULT '',
    photo_s3_key     TEXT DEFAULT NULL,
    storage_location TEXT DEFAULT '',
    rental_cost      DOUBLE PRECISION DEFAULT 0.0,
    weekly_rate      DOUBLE PRECISION DEFAULT 0.0,
    reserve_count    INTEGER DEFAULT 0,
    is_consumable    INTEGER DEFAULT 0,
    is_system        INTEGER DEFAULT 0,
    is_package       INTEGER DEFAULT 0,
    is_kit           INTEGER DEFAULT 0,
    track_quantity   INTEGER DEFAULT 1,
    supplier_name    TEXT DEFAULT '',
    supplier_contact TEXT DEFAULT '',
    is_retired       INTEGER DEFAULT 0,
    retired_at       TIMESTAMP DEFAULT NULL,
    hide_from_pm     INTEGER DEFAULT 0,
    allow_unit_selection INTEGER DEFAULT 0,
    sort_order       INTEGER DEFAULT 0,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS asset_items (
    id                      SERIAL PRIMARY KEY,
    asset_type_id           INTEGER NOT NULL REFERENCES asset_types(id) ON DELETE CASCADE,
    barcode                 TEXT DEFAULT '',
    status                  TEXT DEFAULT 'available',
    condition               TEXT DEFAULT 'good',
    year_purchased          INTEGER DEFAULT NULL,
    purchase_value          DOUBLE PRECISION DEFAULT NULL,
    depreciation_years      INTEGER DEFAULT NULL,
    warranty_expires        DATE DEFAULT NULL,
    depreciation_start_date DATE DEFAULT NULL,
    replacement_cost        DOUBLE PRECISION DEFAULT NULL,
    is_container            INTEGER DEFAULT 0,
    container_item_id       INTEGER REFERENCES asset_items(id) ON DELETE SET NULL,
    system_type_id          INTEGER REFERENCES asset_types(id) ON DELETE SET NULL,
    sort_order              INTEGER DEFAULT 0,
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS asset_logs (
    id            SERIAL PRIMARY KEY,
    asset_item_id INTEGER NOT NULL REFERENCES asset_items(id) ON DELETE CASCADE,
    user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    log_date      DATE NOT NULL,
    log_type      TEXT NOT NULL DEFAULT 'note',
    body          TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS asset_maintenance (
    id            SERIAL PRIMARY KEY,
    asset_item_id INTEGER NOT NULL REFERENCES asset_items(id) ON DELETE CASCADE,
    removed_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    reason        TEXT DEFAULT '',
    notes         TEXT DEFAULT '',
    status        TEXT DEFAULT 'in_progress',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS show_assets (
    id             SERIAL PRIMARY KEY,
    show_id        INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    asset_type_id  INTEGER NOT NULL REFERENCES asset_types(id) ON DELETE CASCADE,
    asset_item_id  INTEGER REFERENCES asset_items(id) ON DELETE SET NULL,
    quantity       INTEGER DEFAULT 1,
    rental_start   DATE,
    rental_end     DATE,
    locked_price   DOUBLE PRECISION DEFAULT 0.0,
    original_locked_price DOUBLE PRECISION DEFAULT NULL,
    is_hidden      INTEGER DEFAULT 0,
    notes          TEXT DEFAULT '',
    added_by       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS show_external_rentals (
    id           SERIAL PRIMARY KEY,
    show_id      INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    description  TEXT NOT NULL DEFAULT '',
    cost         DOUBLE PRECISION DEFAULT 0.0,
    pdf_data     BYTEA,
    pdf_filename TEXT DEFAULT '',
    s3_key       TEXT DEFAULT NULL,
    sort_order   INTEGER DEFAULT 0,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS asset_type_system_members (
    system_type_id    INTEGER NOT NULL REFERENCES asset_types(id) ON DELETE CASCADE,
    component_type_id INTEGER NOT NULL REFERENCES asset_types(id) ON DELETE CASCADE,
    sort_order        INTEGER DEFAULT 0,
    quantity          INTEGER DEFAULT 1,
    PRIMARY KEY (system_type_id, component_type_id)
);

CREATE INDEX IF NOT EXISTS idx_show_assets_show   ON show_assets(show_id);
CREATE INDEX IF NOT EXISTS idx_show_assets_type   ON show_assets(asset_type_id);
CREATE INDEX IF NOT EXISTS idx_show_assets_item   ON show_assets(asset_item_id);
CREATE INDEX IF NOT EXISTS idx_asset_items_type   ON asset_items(asset_type_id);
CREATE INDEX IF NOT EXISTS idx_asset_maint_item   ON asset_maintenance(asset_item_id);
CREATE INDEX IF NOT EXISTS idx_asset_logs_item    ON asset_logs(asset_item_id);
CREATE INDEX IF NOT EXISTS idx_asset_logs_date    ON asset_logs(log_date);
CREATE INDEX IF NOT EXISTS idx_sys_members_sys    ON asset_type_system_members(system_type_id);
CREATE INDEX IF NOT EXISTS idx_sys_members_comp   ON asset_type_system_members(component_type_id);

-- ── User Registration & Recovery ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_pending_registration (
    id             SERIAL PRIMARY KEY,
    username       TEXT UNIQUE NOT NULL,
    display_name   TEXT DEFAULT '',
    email          TEXT NOT NULL,
    password_hash  TEXT NOT NULL,
    confirm_token  TEXT UNIQUE NOT NULL,
    token_expires  TIMESTAMP NOT NULL,
    email_confirmed INTEGER DEFAULT 0,
    admin_approved  INTEGER DEFAULT 0,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token      TEXT UNIQUE NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    used       INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- One-time codes for the public VPS gateway pre-auth (keyed by email, no FK
-- because users.email is not unique and may match several rows)
CREATE TABLE IF NOT EXISTS gateway_otp_codes (
    id         SERIAL PRIMARY KEY,
    email      TEXT NOT NULL,
    code_hash  TEXT NOT NULL,
    client_ip  TEXT DEFAULT '',
    expires_at TIMESTAMP NOT NULL,
    attempts   INTEGER DEFAULT 0,
    used       INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_gateway_otp_email ON gateway_otp_codes(email);

-- ── Site-Wide Messaging ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS site_messages (
    id              SERIAL PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    body_html       TEXT NOT NULL DEFAULT '',
    msg_type        TEXT NOT NULL DEFAULT 'motd',
    is_active       INTEGER DEFAULT 1,
    show_on_login   INTEGER DEFAULT 0,
    dismissible_by  TEXT DEFAULT 'user',
    expires_at      TIMESTAMP,
    scheduled_for   TIMESTAMP,
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS site_message_dismissals (
    message_id   INTEGER NOT NULL REFERENCES site_messages(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    dismissed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (message_id, user_id)
);

CREATE TABLE IF NOT EXISTS site_message_views (
    message_id   INTEGER NOT NULL REFERENCES site_messages(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    seen_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (message_id, user_id)
);

-- ── Asset Dashboard ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS asset_dashboards (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL DEFAULT 'My Dashboard',
    is_public   INTEGER DEFAULT 0,
    public_slug TEXT UNIQUE,
    layout      TEXT DEFAULT 'combined',
    config_json TEXT DEFAULT '{}',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_asset_dashboards_user ON asset_dashboards(user_id);
CREATE INDEX IF NOT EXISTS idx_asset_dashboards_slug ON asset_dashboards(public_slug);

-- ── AI Session Tracking ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ai_sessions (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    show_id    INTEGER REFERENCES shows(id) ON DELETE SET NULL,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at   TIMESTAMP,
    status     TEXT DEFAULT 'running'
);

CREATE INDEX IF NOT EXISTS idx_ai_sessions_status ON ai_sessions(status);

-- ── Cluster Heartbeat (multi-server leader election) ──────────────────────────

CREATE TABLE IF NOT EXISTS cluster_instances (
    instance_id TEXT PRIMARY KEY,
    ip          TEXT NOT NULL,
    hostname    TEXT,
    port        INTEGER,
    app_version TEXT,
    started_at  TIMESTAMP,
    last_seen   TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cluster_instances_last_seen ON cluster_instances(last_seen);

-- ── Prism FM integration (SANDBOXED — see prism_module.py) ────────────────────
-- Staging area for events pulled from Prism, the building's scheduling system.
-- The main app never reads these tables — prism_module only writes to
-- shows/show_performances/advance_data on an explicit admin import.

CREATE TABLE IF NOT EXISTS prism_events (
    id SERIAL PRIMARY KEY,
    prism_event_id INTEGER UNIQUE NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    event_status TEXT DEFAULT '',
    event_status_code TEXT DEFAULT '',
    first_date DATE DEFAULT NULL,
    last_date DATE DEFAULT NULL,
    venue_name TEXT DEFAULT '',
    stage_names TEXT DEFAULT '',
    tour_name TEXT DEFAULT '',
    number_of_shows INTEGER DEFAULT 0,
    is_rental INTEGER DEFAULT 0,
    dates_json TEXT DEFAULT '[]',
    raw_json TEXT DEFAULT '{}',
    content_hash TEXT DEFAULT '',
    prism_last_updated TEXT DEFAULT '',
    import_state TEXT DEFAULT 'new',
    imported_show_id INTEGER REFERENCES shows(id) ON DELETE SET NULL,
    imported_at TIMESTAMP DEFAULT NULL,
    imported_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_prism_events_state ON prism_events(import_state, first_date);

CREATE TABLE IF NOT EXISTS prism_sync_log (
    id SERIAL PRIMARY KEY,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP DEFAULT NULL,
    trigger_type TEXT DEFAULT 'manual',
    triggered_by TEXT DEFAULT '',
    window_start DATE DEFAULT NULL,
    window_end DATE DEFAULT NULL,
    status TEXT DEFAULT 'running',
    events_fetched INTEGER DEFAULT 0,
    events_new INTEGER DEFAULT 0,
    events_updated INTEGER DEFAULT 0,
    events_unchanged INTEGER DEFAULT 0,
    error_text TEXT DEFAULT '',
    debug_log TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_prism_sync_log_started ON prism_sync_log(started_at);

-- Venue and stage catalog as reported by Prism's venues API — refreshed on
-- every sync so the /prism page can document what exists and drive the
-- per-venue visibility filter (prism_hidden_stages in app_settings).

CREATE TABLE IF NOT EXISTS prism_venues (
    id SERIAL PRIMARY KEY,
    prism_venue_id INTEGER UNIQUE NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    city TEXT DEFAULT '',
    state TEXT DEFAULT '',
    capacity INTEGER DEFAULT NULL,
    is_active INTEGER DEFAULT 1,
    stages_json TEXT DEFAULT '[]',
    raw_json TEXT DEFAULT '{}',
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Per-page daily performance rollups (admin /admin/performance page).
-- One row per (stat_date, endpoint) -- the in-app collector merges its
-- buffered counters in with an additive upsert, so multiple Gunicorn
-- workers on multiple servers can all flush the same row safely. All
-- timings are wall-clock milliseconds. The slow_* columns describe the
-- single slowest database query seen on that page that day.

CREATE TABLE IF NOT EXISTS perf_page_stats (
    id SERIAL PRIMARY KEY,
    stat_date DATE NOT NULL,
    endpoint TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    total_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
    min_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
    max_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
    db_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
    db_query_count INTEGER NOT NULL DEFAULT 0,
    db_max_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
    slow_sql TEXT DEFAULT '',
    slow_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
    slow_path TEXT DEFAULT '',
    slow_at TIMESTAMP DEFAULT NULL,
    UNIQUE(stat_date, endpoint)
);
CREATE INDEX IF NOT EXISTS idx_perf_page_stats_date ON perf_page_stats(stat_date);

-- Individual slow-query log -- one row per query that ran at or above the
-- perf_slow_query_ms app setting (default 100 ms). Trimmed to 90 days by
-- run_hourly_maintenance().

CREATE TABLE IF NOT EXISTS perf_slow_queries (
    id SERIAL PRIMARY KEY,
    occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    endpoint TEXT DEFAULT '',
    path TEXT DEFAULT '',
    duration_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
    sql_text TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_perf_slow_queries_at ON perf_slow_queries(occurred_at);
"""


# Tables that belong in the shared schema (user/auth — reusable across apps).
# Only tables whose FK references stay within the shared schema belong here.
# Tables like active_sessions and audit_log reference shows (app schema)
# so they must live in the app schema.
SHARED_TABLES = {
    'users', 'app_settings',
    'password_reset_tokens', 'user_pending_registration',
    'site_messages', 'site_message_dismissals', 'site_message_views',
    # Server-side session store — shared so multiple apps can read the
    # same login state when pointed at the same PostgreSQL DB.
    'app_sessions',
}

# Regex to extract the table name from a CREATE TABLE statement
_CREATE_TABLE_RE = re.compile(
    r'CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)', re.IGNORECASE
)
_CREATE_INDEX_RE = re.compile(
    r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+\w+\s+ON\s+(\w+)',
    re.IGNORECASE,
)


def _table_for_stmt(stmt):
    """Return the table name referenced by a CREATE TABLE or CREATE INDEX statement."""
    m = _CREATE_TABLE_RE.search(stmt)
    if m:
        return m.group(1)
    m = _CREATE_INDEX_RE.search(stmt)
    if m:
        return m.group(1)
    return None


# Startup migrations run in EVERY gunicorn worker on EVERY start. Two rules
# keep that from hurting live traffic (both learned the hard way, 3.0.1):
#   * They're serialized across workers/servers by a PostgreSQL advisory
#     lock, so 4 workers never migrate concurrently.
#   * Statements already reflected in the catalog are SKIPPED. PostgreSQL
#     takes the table lock BEFORE honouring IF NOT EXISTS (AccessExclusive
#     for ALTER TABLE, Share for CREATE INDEX), so a "no-op" migration batch
#     used to lock ~40 hot tables — including the cross-app shared.users —
#     and deadlocked request transactions (DeadlockDetected 500s on restart).
_MIGRATE_LOCK_KEY = 321_000_001

_ADD_COL_RE = re.compile(r'ALTER\s+TABLE\s+"?(\w+)"?\.(\w+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)', re.I)
_DROP_NN_RE = re.compile(r'ALTER\s+TABLE\s+"?(\w+)"?\.(\w+)\s+ALTER\s+COLUMN\s+(\w+)\s+DROP\s+NOT\s+NULL', re.I)
_MK_INDEX_RE = re.compile(r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+ON\s+(?:"?(\w+)"?\.)?(\w+)', re.I)
_MK_TABLE_RE = re.compile(r'CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(?:"?(\w+)"?\.)?(\w+)', re.I)


def _catalog(cur, app_schema, shared_schema):
    """Snapshot of what already exists: tables, columns (+nullability), indexes."""
    sch = (app_schema, shared_schema)
    cur.execute("SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_schema IN (%s, %s)", sch)
    tables = {(a, b) for a, b in cur.fetchall()}
    cur.execute("SELECT table_schema, table_name, column_name, is_nullable "
                "FROM information_schema.columns WHERE table_schema IN (%s, %s)", sch)
    cols = {(a, b, c): n for a, b, c, n in cur.fetchall()}
    cur.execute("SELECT schemaname, indexname FROM pg_indexes WHERE schemaname IN (%s, %s)", sch)
    indexes = {(a, b) for a, b in cur.fetchall()}
    return {'tables': tables, 'cols': cols, 'indexes': indexes}


def _already_applied(stmt, cat, default_schema):
    """True when `stmt` is an idempotent DDL whose effect the catalog already
    shows, so executing it would only take locks. Unknown statements → False."""
    m = _ADD_COL_RE.search(stmt)
    if m:
        return (m.group(1), m.group(2), m.group(3)) in cat['cols']
    m = _DROP_NN_RE.search(stmt)
    if m:
        return cat['cols'].get((m.group(1), m.group(2), m.group(3))) == 'YES'
    m = _MK_INDEX_RE.search(stmt)
    if m:
        return ((m.group(2) or default_schema), m.group(1)) in cat['indexes']
    m = _MK_TABLE_RE.search(stmt)
    if m:
        return ((m.group(1) or default_schema), m.group(2)) in cat['tables']
    return False


def _apply_pg_schema(conn, app_schema, shared_schema):
    """
    Execute every PG_SCHEMA statement, committing after each success and
    retrying failures in later passes.

    Why: the statements are not strictly FK-ordered (e.g. form_sections
    references asset_categories, defined later), and a single-transaction
    pass can never converge — each failed statement rolls back every
    uncommitted CREATE before it, so on an EMPTY database a fresh init
    always died and migrate_db_postgres only ever managed to create the
    tail of the file. Per-statement commits make ordering irrelevant:
    anything whose FK target is missing simply succeeds on a later pass.

    Returns a list of (statement, error) still failing after all passes
    (empty on success).
    """
    cur = conn.cursor()
    # Drop full-line `--` comments BEFORE splitting on ';'. The splitter has
    # no real SQL parser, so a semicolon inside a comment used to cut the
    # NEXT statement's head off — 2.38.0's venue_colors comment did exactly
    # that ("...'#rrggbb'; empty = ..."), leaving venue_colors permanently
    # uncreated on PostgreSQL while SQLite (a real parser) sailed through.
    # PG_SCHEMA is pure DDL (no INSERTs), so line-level stripping is safe.
    sql_only = '\n'.join(
        line for line in PG_SCHEMA.splitlines()
        if not line.lstrip().startswith('--'))
    pending = [s.strip() for s in sql_only.split(';') if s.strip()]
    cat = _catalog(cur, app_schema, shared_schema)
    conn.commit()
    pending = [st for st in pending
               if not _already_applied(st, cat, shared_schema if _table_for_stmt(st) in SHARED_TABLES else app_schema)]
    failures = []
    for _pass in range(8):
        failures = []
        for stmt in pending:
            table = _table_for_stmt(stmt)
            if table and table in SHARED_TABLES:
                cur.execute(f'SET search_path TO "{shared_schema}"')
            else:
                cur.execute(f'SET search_path TO "{app_schema}", "{shared_schema}"')
            try:
                cur.execute(stmt)
                conn.commit()
            except Exception as e:
                conn.rollback()
                failures.append((stmt, e))
        if not failures:
            break
        pending = [s for s, _ in failures]
    cur.close()
    return failures




def _apply_column_migrations(cur, app_schema, shared_schema, cat=None, billable_is_new=None):
    """ALTER TABLE … ADD COLUMN IF NOT EXISTS (and friends) for columns added
    after a table first shipped. Fully idempotent. Returns the count applied."""
    app_alters = [
        f'ALTER TABLE "{app_schema}".shows ADD COLUMN IF NOT EXISTS last_saved_by INTEGER',
        f'ALTER TABLE "{app_schema}".shows ADD COLUMN IF NOT EXISTS last_saved_at TIMESTAMP',
        f"ALTER TABLE \"{app_schema}\".shows ADD COLUMN IF NOT EXISTS performance_company TEXT DEFAULT ''",
        f'ALTER TABLE "{app_schema}".shows ADD COLUMN IF NOT EXISTS load_in_date DATE DEFAULT NULL',
        f"ALTER TABLE \"{app_schema}\".shows ADD COLUMN IF NOT EXISTS load_in_time TEXT DEFAULT ''",
        f'ALTER TABLE "{app_schema}".shows ADD COLUMN IF NOT EXISTS load_out_date DATE DEFAULT NULL',
        f"ALTER TABLE \"{app_schema}\".shows ADD COLUMN IF NOT EXISTS load_out_time TEXT DEFAULT ''",
        f'ALTER TABLE "{app_schema}".export_log ADD COLUMN IF NOT EXISTS pdf_data BYTEA',
        f"ALTER TABLE \"{app_schema}\".export_log ADD COLUMN IF NOT EXISTS filename TEXT DEFAULT ''",
        f'ALTER TABLE "{app_schema}".export_log ADD COLUMN IF NOT EXISTS s3_key TEXT DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".export_log ADD COLUMN IF NOT EXISTS content_hash TEXT DEFAULT NULL',
        # Drop NOT NULL constraints so S3-migrated rows can have NULL file data
        f'ALTER TABLE "{app_schema}".show_attachments ALTER COLUMN file_data DROP NOT NULL',
        f'ALTER TABLE "{app_schema}".show_attachments ADD COLUMN IF NOT EXISTS s3_key TEXT DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".show_attachments ADD COLUMN IF NOT EXISTS field_key TEXT DEFAULT NULL',
        f"ALTER TABLE \"{app_schema}\".show_attachments ADD COLUMN IF NOT EXISTS description TEXT DEFAULT ''",
        f'ALTER TABLE "{app_schema}".show_external_rentals ADD COLUMN IF NOT EXISTS s3_key TEXT DEFAULT NULL',
        f"ALTER TABLE \"{app_schema}\".asset_types ADD COLUMN IF NOT EXISTS supplier_name TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".asset_types ADD COLUMN IF NOT EXISTS supplier_contact TEXT DEFAULT ''",
        f'ALTER TABLE "{app_schema}".asset_types ADD COLUMN IF NOT EXISTS is_retired INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".asset_types ADD COLUMN IF NOT EXISTS retired_at TIMESTAMP DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".asset_types ADD COLUMN IF NOT EXISTS weekly_rate DOUBLE PRECISION DEFAULT 0.0',
        f'ALTER TABLE "{app_schema}".asset_types ADD COLUMN IF NOT EXISTS is_system INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".asset_types ADD COLUMN IF NOT EXISTS is_package INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".asset_types ADD COLUMN IF NOT EXISTS photo_s3_key TEXT DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".asset_types ADD COLUMN IF NOT EXISTS hide_from_pm INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".asset_types ADD COLUMN IF NOT EXISTS allow_unit_selection INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".asset_type_system_members ADD COLUMN IF NOT EXISTS quantity INTEGER DEFAULT 1',
        f'ALTER TABLE "{app_schema}".show_assets ADD COLUMN IF NOT EXISTS asset_item_id INTEGER REFERENCES "{app_schema}".asset_items(id) ON DELETE SET NULL',
        f'CREATE INDEX IF NOT EXISTS idx_show_assets_item ON "{app_schema}".show_assets(asset_item_id)',
        f'ALTER TABLE "{app_schema}".contacts ADD COLUMN IF NOT EXISTS advance_recipient INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".contacts ADD COLUMN IF NOT EXISTS production_recipient INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".contacts ADD COLUMN IF NOT EXISTS postnotes_recipient INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".contacts ADD COLUMN IF NOT EXISTS system_recipient INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".contacts ADD COLUMN IF NOT EXISTS venue_filter TEXT DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".shows ADD COLUMN IF NOT EXISTS postnotes_version INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".position_categories ADD COLUMN IF NOT EXISTS is_venue INTEGER DEFAULT 0',
        f'''CREATE TABLE IF NOT EXISTS "{app_schema}".pay_rate_levels (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            hourly_rate DOUBLE PRECISION DEFAULT 0.0,
            include_in_estimate INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        f'ALTER TABLE "{app_schema}".pay_rate_levels ADD COLUMN IF NOT EXISTS include_in_estimate INTEGER DEFAULT 1',
        f'ALTER TABLE "{app_schema}".crew_members ADD COLUMN IF NOT EXISTS rate_level_id INTEGER',
        f'ALTER TABLE "{app_schema}".job_positions ADD COLUMN IF NOT EXISTS override_rate DOUBLE PRECISION DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".job_positions ADD COLUMN IF NOT EXISTS venue TEXT DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".job_positions ADD COLUMN IF NOT EXISTS is_training INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".crew_qualifications ADD COLUMN IF NOT EXISTS status INTEGER DEFAULT 2',
        f'ALTER TABLE "{app_schema}".crew_members ADD COLUMN IF NOT EXISTS training_notes TEXT DEFAULT \'\'',
        f'ALTER TABLE "{app_schema}".labor_requests ADD COLUMN IF NOT EXISTS is_training_shift INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".overhead_labor_requests ADD COLUMN IF NOT EXISTS is_training_shift INTEGER DEFAULT 0',
        f'''CREATE TABLE IF NOT EXISTS "{app_schema}".show_labor_billable_items (
            show_id          INTEGER NOT NULL REFERENCES "{app_schema}".shows(id) ON DELETE CASCADE,
            billable_item_id INTEGER NOT NULL REFERENCES "{app_schema}".labor_billable_items(id) ON DELETE CASCADE,
            PRIMARY KEY (show_id, billable_item_id)
        )''',
        f'''INSERT INTO "{app_schema}".show_labor_billable_items (show_id, billable_item_id)
            SELECT s.id, b.id
            FROM "{app_schema}".shows s
            CROSS JOIN "{app_schema}".labor_billable_items b
            ON CONFLICT DO NOTHING''',
        f'''CREATE TABLE IF NOT EXISTS "{app_schema}".show_labor_days (
            id         SERIAL PRIMARY KEY,
            show_id    INTEGER NOT NULL REFERENCES "{app_schema}".shows(id) ON DELETE CASCADE,
            work_date  DATE NOT NULL,
            cover_pm   TEXT DEFAULT '',
            day_notes  TEXT DEFAULT '',
            updated_by INTEGER,
            updated_at TIMESTAMP,
            UNIQUE (show_id, work_date)
        )''',
        f'CREATE INDEX IF NOT EXISTS idx_sld_show ON "{app_schema}".show_labor_days(show_id)',
        f'ALTER TABLE "{app_schema}".form_sections ADD COLUMN IF NOT EXISTS default_open INTEGER DEFAULT 1',
        f'ALTER TABLE "{app_schema}".form_sections ADD COLUMN IF NOT EXISTS asset_category_id INTEGER REFERENCES "{app_schema}".asset_categories(id) ON DELETE SET NULL',
        f'''CREATE TABLE IF NOT EXISTS "{app_schema}".arts_groups (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        # Arts group contact & notes fields (added to PG_SCHEMA later —
        # pre-existing PG tables need the columns backfilled here).
        f"ALTER TABLE \"{app_schema}\".arts_groups ADD COLUMN IF NOT EXISTS primary_contact_name TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".arts_groups ADD COLUMN IF NOT EXISTS primary_contact_email TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".arts_groups ADD COLUMN IF NOT EXISTS primary_contact_phone TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".arts_groups ADD COLUMN IF NOT EXISTS notes TEXT DEFAULT ''",
        f'''CREATE TABLE IF NOT EXISTS "{app_schema}".arts_group_contacts (
            id             SERIAL PRIMARY KEY,
            arts_group_id  INTEGER NOT NULL REFERENCES "{app_schema}".arts_groups(id) ON DELETE CASCADE,
            name           TEXT DEFAULT '',
            email          TEXT DEFAULT '',
            phone          TEXT DEFAULT '',
            sort_order     INTEGER DEFAULT 0,
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        f'CREATE INDEX IF NOT EXISTS idx_agc_group ON "{app_schema}".arts_group_contacts(arts_group_id)',
        f'ALTER TABLE "{app_schema}".schedule_rows ADD COLUMN IF NOT EXISTS perf_id INTEGER DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".schedule_rows ADD COLUMN IF NOT EXISTS day_date TEXT DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".shows ADD COLUMN IF NOT EXISTS assets_approved INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".shows ADD COLUMN IF NOT EXISTS assets_approved_by INTEGER',
        f'ALTER TABLE "{app_schema}".shows ADD COLUMN IF NOT EXISTS assets_approved_at TIMESTAMP',
        f'ALTER TABLE "{app_schema}".shows ADD COLUMN IF NOT EXISTS assets_approval_snapshot TEXT',
        f'ALTER TABLE "{app_schema}".form_fields ADD COLUMN IF NOT EXISTS ai_hint TEXT DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".form_fields ADD COLUMN IF NOT EXISTS display_as TEXT DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".form_fields ADD COLUMN IF NOT EXISTS allow_multi INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".form_fields ADD COLUMN IF NOT EXISTS auto_select_visible INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".form_fields ADD COLUMN IF NOT EXISTS hide_from_pdf INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".form_fields ADD COLUMN IF NOT EXISTS upload_button_only INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".form_fields ADD COLUMN IF NOT EXISTS notes_content TEXT DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".form_fields ADD COLUMN IF NOT EXISTS alert_departments TEXT DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".form_fields ADD COLUMN IF NOT EXISTS alert_contact_ids TEXT DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".form_fields ADD COLUMN IF NOT EXISTS pdf_template_id INTEGER DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".contacts ADD COLUMN IF NOT EXISTS user_id INTEGER',
        f'ALTER TABLE "{app_schema}".shows ADD COLUMN IF NOT EXISTS is_test INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".shows ADD COLUMN IF NOT EXISTS prism_status TEXT DEFAULT NULL',
        f"ALTER TABLE \"{app_schema}\".shows ADD COLUMN IF NOT EXISTS show_mode TEXT DEFAULT 'show'",
        f'ALTER TABLE "{app_schema}".contacts ADD COLUMN IF NOT EXISTS mode_filter TEXT DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".schedule_meta_fields ADD COLUMN IF NOT EXISTS show_in_contacts INTEGER DEFAULT 0',
        f"ALTER TABLE \"{app_schema}\".labor_requests ADD COLUMN IF NOT EXISTS break_start TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".labor_requests ADD COLUMN IF NOT EXISTS break_end TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".labor_requests ADD COLUMN IF NOT EXISTS break2_start TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".labor_requests ADD COLUMN IF NOT EXISTS break2_end TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".labor_requests ADD COLUMN IF NOT EXISTS notes TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".shows ADD COLUMN IF NOT EXISTS labor_notes TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".overhead_labor_requests ADD COLUMN IF NOT EXISTS break2_start TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".overhead_labor_requests ADD COLUMN IF NOT EXISTS break2_end TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".overhead_labor_requests ADD COLUMN IF NOT EXISTS actual_break2_start TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".overhead_labor_requests ADD COLUMN IF NOT EXISTS actual_break2_end TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".overhead_labor_templates ADD COLUMN IF NOT EXISTS break2_start TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".overhead_labor_templates ADD COLUMN IF NOT EXISTS break2_end TEXT DEFAULT ''",
        f'ALTER TABLE "{app_schema}".labor_requests ADD COLUMN IF NOT EXISTS work_date DATE',
        f'ALTER TABLE "{app_schema}".labor_requests ADD COLUMN IF NOT EXISTS is_scheduled INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".labor_requests ADD COLUMN IF NOT EXISTS scheduled_crew_member_id INTEGER',
        f'ALTER TABLE "{app_schema}".labor_requests ADD COLUMN IF NOT EXISTS scheduled_by INTEGER',
        f'ALTER TABLE "{app_schema}".labor_requests ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMP',
        f'ALTER TABLE "{app_schema}".show_comments ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP',
        f'ALTER TABLE "{app_schema}".show_comments ADD COLUMN IF NOT EXISTS deleted_by INTEGER',
        f'ALTER TABLE "{app_schema}".show_comments ADD COLUMN IF NOT EXISTS edited_at TIMESTAMP',
        f'ALTER TABLE "{app_schema}".contacts ADD COLUMN IF NOT EXISTS report_recipient INTEGER DEFAULT 0',
        f"ALTER TABLE \"{app_schema}\".asset_items ADD COLUMN IF NOT EXISTS condition TEXT DEFAULT 'good'",
        f'ALTER TABLE "{app_schema}".asset_items ADD COLUMN IF NOT EXISTS year_purchased INTEGER DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".asset_items ADD COLUMN IF NOT EXISTS purchase_value DOUBLE PRECISION DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".asset_items ADD COLUMN IF NOT EXISTS depreciation_years INTEGER DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".asset_items ADD COLUMN IF NOT EXISTS warranty_expires DATE DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".asset_items ADD COLUMN IF NOT EXISTS depreciation_start_date DATE DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".asset_items ADD COLUMN IF NOT EXISTS replacement_cost DOUBLE PRECISION DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".asset_items ADD COLUMN IF NOT EXISTS is_container INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".asset_items ADD COLUMN IF NOT EXISTS container_item_id INTEGER',
        f'ALTER TABLE "{app_schema}".asset_items ADD COLUMN IF NOT EXISTS system_type_id INTEGER',
        f'ALTER TABLE "{app_schema}".audit_log ADD COLUMN IF NOT EXISTS undone_at TIMESTAMP',
        f'ALTER TABLE "{app_schema}".audit_log ADD COLUMN IF NOT EXISTS undone_by INTEGER',
        f'ALTER TABLE "{app_schema}".audit_log ADD COLUMN IF NOT EXISTS undone_by_log_id INTEGER',
        # ── Overhead & Project Crew ──────────────────────────────────────
        f'ALTER TABLE "{app_schema}".overhead_labor_groups ADD COLUMN IF NOT EXISTS project_id INTEGER',
        f'ALTER TABLE "{app_schema}".overhead_labor_groups ADD COLUMN IF NOT EXISTS created_by INTEGER',
        f'ALTER TABLE "{app_schema}".overhead_labor_requests ADD COLUMN IF NOT EXISTS template_id INTEGER',
        f'ALTER TABLE "{app_schema}".overhead_labor_requests ADD COLUMN IF NOT EXISTS pay_rate_snapshot DOUBLE PRECISION DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".overhead_labor_requests ADD COLUMN IF NOT EXISTS pay_rate_level_id_snapshot INTEGER DEFAULT NULL',
        f"ALTER TABLE \"{app_schema}\".overhead_labor_requests ADD COLUMN IF NOT EXISTS actual_in_time TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".overhead_labor_requests ADD COLUMN IF NOT EXISTS actual_out_time TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".overhead_labor_requests ADD COLUMN IF NOT EXISTS actual_break_start TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".overhead_labor_requests ADD COLUMN IF NOT EXISTS actual_break_end TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".overhead_labor_requests ADD COLUMN IF NOT EXISTS notes TEXT DEFAULT ''",
        f'ALTER TABLE "{app_schema}".overhead_labor_requests ADD COLUMN IF NOT EXISTS created_by INTEGER',
        f"ALTER TABLE \"{app_schema}\".overhead_projects ADD COLUMN IF NOT EXISTS description TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".overhead_projects ADD COLUMN IF NOT EXISTS client_name TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".overhead_projects ADD COLUMN IF NOT EXISTS billing_code TEXT DEFAULT ''",
        f"ALTER TABLE \"{app_schema}\".overhead_projects ADD COLUMN IF NOT EXISTS color TEXT DEFAULT ''",
        f'ALTER TABLE "{app_schema}".overhead_projects ADD COLUMN IF NOT EXISTS archived INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".overhead_projects ADD COLUMN IF NOT EXISTS created_by INTEGER',
        f'ALTER TABLE "{app_schema}".show_assets ADD COLUMN IF NOT EXISTS original_locked_price DOUBLE PRECISION DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".shows ADD COLUMN IF NOT EXISTS cast_count INTEGER DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".shows ADD COLUMN IF NOT EXISTS crew_count INTEGER DEFAULT NULL',
        # Attachment archive (soft delete) — 2.36.0
        f'ALTER TABLE "{app_schema}".show_attachments ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".show_attachments ADD COLUMN IF NOT EXISTS deleted_by INTEGER',
        f'ALTER TABLE "{app_schema}".show_attachments ADD COLUMN IF NOT EXISTS is_compressed INTEGER DEFAULT 0',
        f'CREATE INDEX IF NOT EXISTS idx_show_attachments_show ON "{app_schema}".show_attachments(show_id)',
        # Manually added billable hours (prep work etc.) — 2.44.0
        f'ALTER TABLE "{app_schema}".post_show_labor ADD COLUMN IF NOT EXISTS is_added_hours INTEGER DEFAULT 0',
        f'ALTER TABLE "{app_schema}".post_show_labor ADD COLUMN IF NOT EXISTS manual_hours DOUBLE PRECISION DEFAULT NULL',
        f'ALTER TABLE "{app_schema}".post_show_labor ADD COLUMN IF NOT EXISTS crew_member_id INTEGER',
    ]

    shared_alters = [
        f"ALTER TABLE \"{shared_schema}\".users ADD COLUMN IF NOT EXISTS theme TEXT DEFAULT 'dark'",
        f'ALTER TABLE "{shared_schema}".users ADD COLUMN IF NOT EXISTS last_login TIMESTAMP',
        f"ALTER TABLE \"{shared_schema}\".users ADD COLUMN IF NOT EXISTS last_conn_path TEXT DEFAULT ''",
        f"ALTER TABLE \"{shared_schema}\".users ADD COLUMN IF NOT EXISTS last_conn_ip TEXT DEFAULT ''",
        f'ALTER TABLE "{shared_schema}".users ADD COLUMN IF NOT EXISTS last_conn_at TIMESTAMP',
        f"ALTER TABLE \"{shared_schema}\".users ADD COLUMN IF NOT EXISTS email TEXT DEFAULT ''",
        f'ALTER TABLE "{shared_schema}".users ADD COLUMN IF NOT EXISTS is_readonly INTEGER DEFAULT 0',
        f'ALTER TABLE "{shared_schema}".users ADD COLUMN IF NOT EXISTS email_confirmed INTEGER DEFAULT 1',
        f'ALTER TABLE "{shared_schema}".users ADD COLUMN IF NOT EXISTS pending_approval INTEGER DEFAULT 0',
        f'ALTER TABLE "{shared_schema}".users ADD COLUMN IF NOT EXISTS must_change_password INTEGER DEFAULT 0',
        f'ALTER TABLE "{shared_schema}".users ADD COLUMN IF NOT EXISTS is_scheduler INTEGER DEFAULT 0',
        f'ALTER TABLE "{shared_schema}".users ADD COLUMN IF NOT EXISTS is_asset_manager INTEGER DEFAULT 0',
        f'ALTER TABLE "{shared_schema}".users ADD COLUMN IF NOT EXISTS is_document_viewer INTEGER DEFAULT 0',
        f'ALTER TABLE "{shared_schema}".users ADD COLUMN IF NOT EXISTS viewer_venues TEXT DEFAULT NULL',
        f'ALTER TABLE "{shared_schema}".users ADD COLUMN IF NOT EXISTS viewer_doc_types TEXT DEFAULT NULL',
        f'ALTER TABLE "{shared_schema}".users ADD COLUMN IF NOT EXISTS viewer_labor_overview INTEGER DEFAULT 0',
        f'ALTER TABLE "{shared_schema}".users ADD COLUMN IF NOT EXISTS viewer_show_calendar INTEGER DEFAULT 0',
        f"ALTER TABLE \"{shared_schema}\".users ADD COLUMN IF NOT EXISTS home_layout TEXT DEFAULT 'columns'",
        f"ALTER TABLE \"{shared_schema}\".users ADD COLUMN IF NOT EXISTS home_density TEXT DEFAULT 'normal'",
        # Cross-app account flags — read by OTHER apps that share this
        # user directory; 321Theater never reads them itself.
        f'ALTER TABLE "{shared_schema}".users ADD COLUMN IF NOT EXISTS is_app_user INTEGER DEFAULT 0',
        f'ALTER TABLE "{shared_schema}".users ADD COLUMN IF NOT EXISTS is_app_admin INTEGER DEFAULT 0',
        # Account lock — blocks login without deleting the account or its data.
        f'ALTER TABLE "{shared_schema}".users ADD COLUMN IF NOT EXISTS is_locked INTEGER DEFAULT 0',
    ]

    if cat is None:
        cat = _catalog(cur, app_schema, shared_schema)
    if billable_is_new is None:
        billable_is_new = (app_schema, 'show_labor_billable_items') not in cat['tables']
    n = 0
    for sql in app_alters + shared_alters:
        if 'INSERT INTO' in sql and 'show_labor_billable_items' in sql:
            # One-time backfill for when the per-show selection table is first
            # created. Re-running it on every start re-enabled every billable
            # item on every show, undoing PMs' deliberate unchecks (a removed
            # parking charge reappeared on the invoice after each restart).
            if not billable_is_new:
                continue
        elif _already_applied(sql, cat, app_schema):
            continue
        cur.execute(sql)
        if _MK_TABLE_RE.search(sql):
            m = _MK_TABLE_RE.search(sql)
            cat['tables'].add((m.group(1) or app_schema, m.group(2)))
        n += 1

    # Data backfills. Each runs in its own SAVEPOINT so one failure can't
    # silently abort the rest of the migration transaction, and each only
    # touches rows it actually changes (no whole-table row locks per start).
    def _backfill(label, sql, params=None):
        cur.execute('SAVEPOINT _bf')
        try:
            cur.execute(sql, params)
            cur.execute('RELEASE SAVEPOINT _bf')
        except Exception as e:
            cur.execute('ROLLBACK TO SAVEPOINT _bf')
            print(f"[migrate_pg] {label} backfill warning: {e}")

    # original_locked_price for legacy show_assets rows that pre-date the
    # column (new rows always set it, so this is a no-op on current data).
    _backfill('original_locked_price', f"""
        UPDATE "{app_schema}".show_assets
           SET original_locked_price = locked_price
         WHERE original_locked_price IS NULL AND locked_price IS NOT NULL
    """)

    # Dedupe contacts duplicated by the pre-index user→contact backfill race,
    # then create the partial unique index so future inserts can't double up.
    # Once the index exists duplicates are impossible, so skip both.
    if (app_schema, 'idx_contacts_user_unique') not in cat['indexes']:
        _backfill('contacts user_id dedupe/index', f"""
            UPDATE "{app_schema}".contacts SET user_id = NULL
             WHERE user_id IS NOT NULL
               AND id NOT IN (
                   SELECT MIN(id) FROM "{app_schema}".contacts
                    WHERE user_id IS NOT NULL
                    GROUP BY user_id
               );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_user_unique
                ON "{app_schema}".contacts(user_id) WHERE user_id IS NOT NULL
        """)

    # shows.cast_count / crew_count from post_show_notes — only for shows that
    # actually HAVE a numeric note (the old form rewrote every NULL-count show
    # to NULL on every start, row-locking most of `shows`).
    for col in ('cast_count', 'crew_count'):
        _backfill(col, f"""
            UPDATE "{app_schema}".shows AS s
               SET {col} = CAST(p.field_value AS INTEGER)
              FROM "{app_schema}".post_show_notes p
             WHERE p.show_id = s.id
               AND p.field_key = %s
               AND p.field_value ~ '^[0-9]+$'
               AND s.{col} IS NULL
        """, (col,))

    return n


def _widen_real_columns(cur, app_schema, shared_schema):
    """Convert any remaining REAL (float4) columns to DOUBLE PRECISION.

    SQLite's REAL is a 64-bit double, but PostgreSQL's REAL is a 32-bit
    float (~7 significant digits): money/rate columns carried over from the
    SQLite schema could not hold cents above ~$262k and SQL-side sums
    drifted (1000 × 0.10 → 99.99905). Converting via ::text keeps the value
    the user typed (float4 prints its shortest round-trip form, e.g. '0.1',
    so we don't bake in float4 noise like 0.10000000149). Only touches
    columns still typed `real`, so it is a no-op after the first run and
    never rewrites a table on ordinary startups."""
    cur.execute(
        "SELECT table_schema, table_name, column_name FROM information_schema.columns "
        "WHERE table_schema IN (%s, %s) AND data_type = 'real' ORDER BY 1, 2, 3",
        (app_schema, shared_schema))
    cols = cur.fetchall()
    for sch, tbl, col in cols:
        cur.execute(f'ALTER TABLE "{sch}"."{tbl}" ALTER COLUMN "{col}" '
                    f'TYPE DOUBLE PRECISION USING "{col}"::text::double precision')
    return len(cols)


def _prepare_schemas(conn, app_schema, shared_schema, verbose=True):
    """Ensure both schemas exist and the role can create in them.
    Returns False (after printing why) when a schema is unusable."""
    conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute("SELECT current_user")
        pg_user = cur.fetchone()[0]
        if verbose:
            print(f"  Connected as PG role: {pg_user}")
        for sch in (app_schema, shared_schema):
            cur.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = %s", (sch,))
            row = cur.fetchone()
            if row:
                if verbose:
                    print(f"  Schema '{sch}' already exists (owner: {row[0]})")
                cur.execute("SELECT has_schema_privilege(current_user, %s, 'CREATE')", (sch,))
                if not cur.fetchone()[0]:
                    print(f"  ✗ Role '{pg_user}' lacks CREATE privilege on schema '{sch}'")
                    print("    Fix: connect as the DB owner and run:")
                    print(f"      GRANT ALL ON SCHEMA \"{sch}\" TO \"{pg_user}\";")
                    return False
            else:
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{sch}"')
                if verbose:
                    print(f"  Created schema '{sch}'")
        return True
    finally:
        cur.close()
        conn.autocommit = False


def _migrate(conn, app_schema, shared_schema, log_prefix):
    """Tables (PG_SCHEMA) then column migrations, committed. Returns True
    when every statement applied. Serialized across all workers/servers by a
    session-level advisory lock (see _MIGRATE_LOCK_KEY)."""
    cur = conn.cursor()
    cur.execute('SELECT pg_advisory_lock(%s)', (_MIGRATE_LOCK_KEY,))
    conn.commit()
    try:
        cur.execute("SELECT to_regclass(%s) IS NULL",
                    (f'"{app_schema}".show_labor_billable_items',))
        billable_is_new = cur.fetchone()[0]
        conn.commit()
        leftover = _apply_pg_schema(conn, app_schema, shared_schema)
        for stmt, err in leftover:
            first_line = stmt.splitlines()[0][:90]
            print(f"{log_prefix} statement still failing: {first_line}… → {err}")
        n = _apply_column_migrations(cur, app_schema, shared_schema,
                                     billable_is_new=billable_is_new)
        widened = _widen_real_columns(cur, app_schema, shared_schema)
        conn.commit()
        print(f"{log_prefix} Applied {n} pending column migration(s)")
        if widened:
            print(f"{log_prefix} Widened {widened} REAL (float4) column(s) to DOUBLE PRECISION")
    finally:
        try:
            conn.rollback()
            cur.execute('SELECT pg_advisory_unlock(%s)', (_MIGRATE_LOCK_KEY,))
            conn.commit()
        except Exception:
            pass   # closing the connection releases the lock anyway
        cur.close()
    return not leftover


def init_db_postgres(settings=None, seed=True):
    """
    Create/upgrade the PostgreSQL database. Creates two schema namespaces:
      - shared schema: user/auth tables (reusable across apps)
      - app schema:    theater-specific tables
    then applies the column migrations and (seed=True) seeds defaults into
    EMPTY tables only. Safe to run against an existing database.
    """
    settings = settings if settings is not None else db_adapter.read_db_settings()
    if not db_adapter.is_configured(settings):
        print(f"✗ {db_adapter.CONFIG_PATH} not found or missing [postgresql] section. "
              f"See db_config.ini.example.")
        return False
    app_schema, shared_schema = db_adapter.schemas(settings)
    print(f"  app_schema={app_schema!r}, shared_schema={shared_schema!r}")
    print(f"  host={settings.get('pg_host')}, dbname={settings.get('pg_dbname')}, user={settings.get('pg_user')}")
    try:
        conn = db_adapter.raw_connect(settings, search_path=False)
    except Exception as e:
        print(f"✗ PostgreSQL connection failed: {e}")
        return False
    print("  Connected OK")
    try:
        if not _prepare_schemas(conn, app_schema, shared_schema):
            return False
        if not _migrate(conn, app_schema, shared_schema, ' '):
            print("✗ PostgreSQL init failed: some schema statements could not be applied")
            return False
    except Exception as e:
        print(f"✗ PostgreSQL init failed: {e}")
        return False
    finally:
        conn.close()

    if seed:
        db = db_adapter.connect(settings)
        try:
            created_admin = _seed_admin_user(db)
            _seed_app_settings(db)
            _seed_form_data(db)
            _seed_schedule_meta_fields(db)
            _seed_job_positions(db)
            _seed_contacts(db)
            db.commit()
        except Exception as e:
            db.rollback()
            print(f"✗ Seeding failed: {e}")
            return False
        finally:
            db.close()
        if created_admin:
            print("✓ Admin account:   username=admin  password=admin123")
            print("⚠  Change the admin password after first login (you will be prompted)")
    print(f"✓ PostgreSQL ready — app schema: '{app_schema}', shared schema: '{shared_schema}'")
    return True


def migrate_db_postgres():
    """
    Apply schema migrations to the PostgreSQL database. Runs on every app
    startup: CREATE TABLE IF NOT EXISTS for new tables and ALTER TABLE …
    ADD COLUMN IF NOT EXISTS for new columns — idempotent, never seeds.

    Never raises — on error it prints a warning and returns so the app can
    still start against the existing schema.
    """
    settings = db_adapter.read_db_settings()
    if not db_adapter.is_configured(settings):
        print(f"[migrate_pg] {db_adapter.CONFIG_PATH} missing — skipping migrations")
        return
    try:
        app_schema, shared_schema = db_adapter.schemas(settings)
        conn = db_adapter.raw_connect(settings, search_path=False)
    except Exception as e:
        print(f"[migrate_pg] Cannot connect to PostgreSQL: {e}")
        return
    try:
        _migrate(conn, app_schema, shared_schema, '[migrate_pg]')
    except Exception as e:
        print(f"[migrate_pg] Migration warning: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def reset_postgres(settings=None):
    """DROP both schemas (all data) after an interactive YES confirmation."""
    settings = settings if settings is not None else db_adapter.read_db_settings()
    if not db_adapter.is_configured(settings):
        print(f"{db_adapter.CONFIG_PATH} not found or missing [postgresql] section.")
        return False
    app_schema, shared_schema = db_adapter.schemas(settings)
    confirm = input(f"This will DROP schemas '{app_schema}' and '{shared_schema}' and ALL their data. Type YES to confirm: ")
    if confirm != 'YES':
        print("Aborted.")
        return False
    conn = db_adapter.raw_connect(settings, search_path=False)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(f'DROP SCHEMA IF EXISTS "{app_schema}" CASCADE')
    cur.execute(f'DROP SCHEMA IF EXISTS "{shared_schema}" CASCADE')
    conn.close()
    print(f"✓ Dropped schemas '{app_schema}' and '{shared_schema}'")
    return True


if __name__ == '__main__':
    args = set(sys.argv[1:])
    if '--migrate' in args:
        migrate_db_postgres()
    elif args & {'--reset', '--reset-postgres'}:
        if reset_postgres():
            sys.exit(0 if init_db_postgres() else 1)
    elif not args or args <= {'--init-postgres'}:
        sys.exit(0 if init_db_postgres() else 1)
    else:
        print(__doc__)
        sys.exit(2)
