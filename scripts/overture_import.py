#!/usr/bin/env python3
"""Overture Maps Places → PostgreSQL import script (v2).

Downloads Overture Places Parquet from S3 via DuckDB (public bucket, no credentials),
filters to North America bounding box, and inserts into local PostgreSQL with PostGIS.

Usage:
    cd /opt/recon && venv/bin/python scripts/overture_import.py

Re-runnable (idempotent via UPSERT).
"""

import json
import logging
import os
import re
import sys
import time

import duckdb
import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('overture_import')

# --- Config ---
OVERTURE_RELEASE = '2026-04-15.0'
S3_PATH = f's3://overturemaps-us-west-2/release/{OVERTURE_RELEASE}/theme=places/type=place/*'

# North America bounding box (generous — includes Hawaii, Puerto Rico, Canada)
BBOX = {
    'xmin': -170.0,
    'xmax': -50.0,
    'ymin': 15.0,
    'ymax': 85.0,
}

BATCH_SIZE = 50_000
OSM_RECORD_RE = re.compile(r'^([nwr])(\d+)@\d+$')

DB_CONFIG = {
    'host': os.environ.get('OVERTURE_DB_HOST', 'localhost'),
    'port': int(os.environ.get('OVERTURE_DB_PORT', '5432')),
    'dbname': os.environ.get('OVERTURE_DB_NAME', 'overture'),
    'user': os.environ.get('OVERTURE_DB_USER', 'overture'),
    'password': os.environ.get('OVERTURE_DB_PASSWORD', ''),
}


def create_table(conn):
    """Create places table and indexes if they don't exist."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS places (
                id TEXT PRIMARY KEY,
                geometry GEOMETRY(Point, 4326),
                name TEXT,
                basic_category TEXT,
                confidence REAL,
                phone TEXT,
                website TEXT,
                socials JSONB,
                brand_name TEXT,
                brand_wikidata TEXT,
                osm_type CHAR(1),
                osm_id BIGINT,
                source_record_id TEXT,
                raw_sources JSONB
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_places_osm
            ON places(osm_type, osm_id) WHERE osm_type IS NOT NULL;
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_places_geom
            ON places USING GIST(geometry);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_places_name_trgm
            ON places USING GIN(name gin_trgm_ops);
        """)
    conn.commit()
    log.info('Table and indexes ready')


def parse_osm_ref(sources):
    """Extract OSM type letter and ID from Overture sources array."""
    if not sources:
        return None, None, None
    for src in sources:
        record_id = None
        if isinstance(src, dict):
            record_id = src.get('record_id', '')
        elif hasattr(src, '__getitem__'):
            # DuckDB struct — try attribute access
            try:
                record_id = src['record_id']
            except (KeyError, TypeError, IndexError):
                pass
        if not record_id:
            continue
        m = OSM_RECORD_RE.match(str(record_id))
        if m:
            return m.group(1), int(m.group(2)), str(record_id)
    return None, None, None


def run_import():
    """Main import: DuckDB reads S3 Parquet → PostgreSQL via chunked OFFSET/LIMIT."""
    log.info(f'Overture release: {OVERTURE_RELEASE}')
    log.info(f'S3 path: {S3_PATH}')
    log.info(f'Bounding box: {BBOX}')

    # Connect to PostgreSQL
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    create_table(conn)

    # Set up DuckDB with httpfs and spatial for S3 access
    duck = duckdb.connect()
    duck.execute("INSTALL httpfs; LOAD httpfs;")
    duck.execute("INSTALL spatial; LOAD spatial;")
    duck.execute("SET s3_region='us-west-2';")

    # Use a materialized approach: DuckDB query → Arrow → iterate in Python
    query = f"""
        SELECT
            id,
            ST_X(geometry) AS lon,
            ST_Y(geometry) AS lat,
            names.primary AS name,
            basic_category,
            confidence,
            phones,
            websites,
            socials,
            brand,
            sources
        FROM read_parquet('{S3_PATH}', hive_partitioning=true)
        WHERE bbox.xmin >= {BBOX['xmin']}
          AND bbox.xmax <= {BBOX['xmax']}
          AND bbox.ymin >= {BBOX['ymin']}
          AND bbox.ymax <= {BBOX['ymax']}
    """

    log.info('Starting DuckDB query against S3 (this will take several minutes)...')
    t_start = time.time()

    # Execute and fetch all as Arrow for efficient iteration
    result_rel = duck.sql(query)

    upsert_sql = """
        INSERT INTO places (id, geometry, name, basic_category, confidence,
                            phone, website, socials, brand_name, brand_wikidata,
                            osm_type, osm_id, source_record_id, raw_sources)
        VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            geometry = EXCLUDED.geometry,
            name = EXCLUDED.name,
            basic_category = EXCLUDED.basic_category,
            confidence = EXCLUDED.confidence,
            phone = EXCLUDED.phone,
            website = EXCLUDED.website,
            socials = EXCLUDED.socials,
            brand_name = EXCLUDED.brand_name,
            brand_wikidata = EXCLUDED.brand_wikidata,
            osm_type = EXCLUDED.osm_type,
            osm_id = EXCLUDED.osm_id,
            source_record_id = EXCLUDED.source_record_id,
            raw_sources = EXCLUDED.raw_sources
    """

    template = """(
        %(id)s,
        ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
        %(name)s,
        %(basic_category)s,
        %(confidence)s,
        %(phone)s,
        %(website)s,
        %(socials)s::jsonb,
        %(brand_name)s,
        %(brand_wikidata)s,
        %(osm_type)s,
        %(osm_id)s,
        %(source_record_id)s,
        %(raw_sources)s::jsonb
    )"""

    total = 0
    osm_refs = 0
    batch = []

    log.info('DuckDB query executing, fetching results in chunks...')

    # Fetch in chunks using fetchmany on the relation
    chunk_size = BATCH_SIZE
    while True:
        chunk = result_rel.fetchmany(chunk_size)
        if not chunk:
            break

        for row in chunk:
            row_id = row[0]
            lon = row[1]
            lat = row[2]
            name = row[3]
            basic_cat = row[4]
            conf = row[5]
            phones = row[6]
            websites = row[7]
            socials_raw = row[8]
            brand_raw = row[9]
            sources_raw = row[10]

            if lon is None or lat is None:
                continue

            # Phone: first element of VARCHAR[]
            phone = None
            if phones and len(phones) > 0:
                phone = str(phones[0]) if phones[0] else None

            # Website: first element of VARCHAR[]
            website = None
            if websites and len(websites) > 0:
                website = str(websites[0]) if websites[0] else None

            # Socials: VARCHAR[] → JSON array of strings
            socials_json = None
            if socials_raw and len(socials_raw) > 0:
                socials_json = json.dumps([str(s) for s in socials_raw if s])

            # Brand: struct with wikidata and names.primary
            brand_name = None
            brand_wikidata = None
            if brand_raw:
                try:
                    if isinstance(brand_raw, dict):
                        brand_wikidata = brand_raw.get('wikidata')
                        names_struct = brand_raw.get('names')
                        if names_struct and isinstance(names_struct, dict):
                            brand_name = names_struct.get('primary')
                    else:
                        # DuckDB struct — access by key
                        brand_wikidata = brand_raw['wikidata'] if 'wikidata' in dir(brand_raw) else None
                        try:
                            brand_wikidata = brand_raw[0]  # wikidata is first field
                            names_struct = brand_raw[1]     # names is second field
                            if names_struct:
                                brand_name = names_struct[0]  # primary is first field
                        except (IndexError, TypeError):
                            pass
                except Exception:
                    pass

            # Sources: parse OSM cross-reference
            sources_list = None
            if sources_raw:
                if isinstance(sources_raw, (list, tuple)):
                    sources_list = []
                    for s in sources_raw:
                        if isinstance(s, dict):
                            sources_list.append(s)
                        else:
                            # DuckDB struct tuple — convert
                            try:
                                sources_list.append({
                                    'dataset': s[1] if len(s) > 1 else None,
                                    'record_id': s[3] if len(s) > 3 else None,
                                })
                            except (TypeError, IndexError):
                                pass

            osm_type_letter, osm_id_val, source_record_id = parse_osm_ref(sources_list)
            if osm_type_letter:
                osm_refs += 1

            raw_sources_json = json.dumps(sources_list) if sources_list else None

            batch.append({
                'id': row_id,
                'lon': float(lon),
                'lat': float(lat),
                'name': name,
                'basic_category': basic_cat,
                'confidence': float(conf) if conf is not None else None,
                'phone': phone,
                'website': website,
                'socials': socials_json,
                'brand_name': brand_name,
                'brand_wikidata': brand_wikidata,
                'osm_type': osm_type_letter,
                'osm_id': osm_id_val,
                'source_record_id': source_record_id,
                'raw_sources': raw_sources_json,
            })

            if len(batch) >= BATCH_SIZE:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_values(
                        cur, upsert_sql, batch,
                        template=template,
                        page_size=BATCH_SIZE
                    )
                conn.commit()
                total += len(batch)
                elapsed = time.time() - t_start
                rate = total / elapsed if elapsed > 0 else 0
                log.info(f'Inserted {total:,} rows ({osm_refs:,} OSM xrefs) '
                         f'[{rate:.0f} rows/sec, {elapsed:.0f}s elapsed]')
                batch = []

    # Flush remaining
    if batch:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur, upsert_sql, batch,
                template=template,
                page_size=BATCH_SIZE
            )
        conn.commit()
        total += len(batch)

    duck.close()

    # Final stats
    elapsed = time.time() - t_start
    log.info(f'Import complete: {total:,} rows, {osm_refs:,} OSM cross-refs, '
             f'{elapsed:.0f}s total ({total/elapsed:.0f} rows/sec)')

    # Verify
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM places")
        count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM places WHERE osm_type IS NOT NULL")
        osm_count = cur.fetchone()[0]
        log.info(f'Final table: {count:,} total rows, {osm_count:,} with OSM cross-references')

    conn.close()


if __name__ == '__main__':
    run_import()
