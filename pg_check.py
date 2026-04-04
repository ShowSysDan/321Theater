"""Quick diagnostic: check where data lives in PostgreSQL schemas."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from db_adapter import _read_pg_config

DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'advance.db')
settings = _read_pg_config(DATABASE)

import psycopg2
conn = psycopg2.connect(
    host=settings['pg_host'],
    port=int(settings['pg_port']),
    dbname=settings['pg_dbname'],
    user=settings['pg_user'],
    password=settings['pg_password'],
    connect_timeout=10,
)
cur = conn.cursor()

app_schema = settings.get('pg_app_schema', 'theater321')
shared_schema = settings.get('pg_shared_schema', 'shared')

print(f"App schema: {app_schema}")
print(f"Shared schema: {shared_schema}")
print()

# Check what schemas exist and who owns them
cur.execute("SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname != 'information_schema' ORDER BY nspname")
print("All schemas:")
for row in cur.fetchall():
    print(f"  {row[0]:30s} owner: {row[1]}")
print()

# Check row counts in each schema
for schema in [app_schema, shared_schema, 'public']:
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = %s AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """, (schema,))
    tables = [r[0] for r in cur.fetchall()]
    if not tables:
        print(f"Schema '{schema}': no tables found (or no access)")
        continue
    print(f"Schema '{schema}' — {len(tables)} tables:")
    for t in tables:
        try:
            cur.execute(f'SELECT count(*) FROM "{schema}"."{t}"')
            count = cur.fetchone()[0]
            if count > 0:
                print(f"  {t:40s} {count:>6d} rows")
        except Exception:
            conn.rollback()
            print(f"  {t:40s} (no access)")
    print()

# Check what the app would see at runtime
cur.execute(f'SET search_path TO "{app_schema}", "{shared_schema}"')
for t in ['users', 'shows', 'contacts', 'asset_types', 'asset_items']:
    try:
        cur.execute(f'SELECT count(*) FROM "{t}"')
        count = cur.fetchone()[0]
        print(f"Runtime view: {t:30s} {count:>6d} rows")
    except Exception:
        conn.rollback()
        cur.execute(f'SET search_path TO "{app_schema}", "{shared_schema}"')
        print(f"Runtime view: {t:30s} ERROR")

conn.close()
