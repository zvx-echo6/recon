"""
PAD-US land classification lookup.

Provides point-in-polygon queries against the USGS Protected Areas Database
(PAD-US) stored in a local PostGIS database. Returns land ownership,
management, and public access information for any lat/lon coordinate.

Connection pool is lazy-initialized on first call. If PostgreSQL is unreachable,
functions return empty results gracefully (feature degrades, doesn't crash).
"""
import os

import psycopg2
import psycopg2.pool

from .utils import setup_logging

logger = setup_logging('recon.landclass')

_pool = None
_pool_failed = False

# ── Label mappings from PAD-US domain tables ────────────────────────────
# Extracted from PADUS4_0_Geodatabase.gdb domain lookup layers.
# ogr2ogr lowercases all column names.

AGENCY_NAME_MAP = {
    'TVA': 'Tennessee Valley Authority',
    'BLM': 'Bureau of Land Management',
    'BOEM': 'Bureau of Ocean Energy Management',
    'USBR': 'Bureau of Reclamation',
    'FWS': 'U.S. Fish and Wildlife Service',
    'USFS': 'Forest Service',
    'DOD': 'Department of Defense',
    'USACE': 'Army Corps of Engineers',
    'DOE': 'Department of Energy',
    'NPS': 'National Park Service',
    'NRCS': 'Natural Resources Conservation Service',
    'ARS': 'Agricultural Research Service',
    'BIA': 'Bureau of Indian Affairs',
    'NOAA': 'National Oceanic and Atmospheric Administration',
    'BPA': 'Bonneville Power Administration',
    'OTHF': 'Other or Unknown Federal Land',
    'TRIB': 'American Indian Lands',
    'SPR': 'State Park and Recreation',
    'SDC': 'State Department of Conservation',
    'SLB': 'State Land Board',
}

AGENCY_TYPE_MAP = {
    'FED': 'Federal',
    'TRIB': 'American Indian Lands',
    'STAT': 'State',
    'DIST': 'Regional Agency Special District',
    'LOC': 'Local Government',
    'NGO': 'Non-Governmental Organization',
    'PVT': 'Private',
    'JNT': 'Joint',
    'UNK': 'Unknown',
    'TERR': 'Territorial',
    'DESG': 'Designation',
}

DESIGNATION_TYPE_MAP = {
    'NP': 'National Park',
    'NM': 'National Monument',
    'NCA': 'Conservation Area',
    'NF': 'National Forest',
    'NG': 'National Grassland',
    'PUB': 'National Public Lands',
    'NT': 'National Scenic or Historic Trail',
    'NWR': 'National Wildlife Refuge',
    'WA': 'Wilderness Area',
    'WSR': 'Wild and Scenic River',
    'WSA': 'Wilderness Study Area',
    'MPA': 'Marine Protected Area',
    'NRA': 'National Recreation Area',
    'NSBV': 'National Scenic, Botanical or Volcanic Area',
    'NLS': 'National Lakeshore or Seashore',
    'IRA': 'Inventoried Roadless Area',
    'ACEC': 'Area of Critical Environmental Concern',
    'RNA': 'Research Natural Area',
    'REC': 'Recreation Management Area',
    'RMA': 'Resource Management Area',
    'WPA': 'Watershed Protection Area',
    'REA': 'Research or Educational Area',
    'HCA': 'Historic or Cultural Area',
    'MIT': 'Mitigation Land or Bank',
    'MIL': 'Military Land',
    'ACC': 'Access Area',
    'SDA': 'Special Designation Area',
    'PROC': 'Approved or Proclamation Boundary',
    'FOTH': 'Federal Other or Unknown',
    'ND': 'Not Designated',
}

PUBLIC_ACCESS_MAP = {
    'OA': 'Open Access',
    'RA': 'Restricted Access',
    'XA': 'Closed',
    'UK': 'Unknown',
}

GAP_STATUS_MAP = {
    '1': 'Managed for biodiversity (disturbance events proceed)',
    '2': 'Managed for biodiversity (disturbance suppressed)',
    '3': 'Multiple uses (extractive/OHV)',
    '4': 'No known mandate for biodiversity protection',
}

CATEGORY_MAP = {
    'Fee': 'Fee',
    'Easement': 'Easement',
    'Other': 'Other',
    'Unknown': 'Unknown',
    'Designation': 'Designation',
    'Marine': 'Marine Area',
    'Proclamation': 'Approved, Proclamation or Extent Boundary',
}

STATE_MAP = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
    'DC': 'District of Columbia', 'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii',
    'ID': 'Idaho', 'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa',
    'KS': 'Kansas', 'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine',
    'MD': 'Maryland', 'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota',
    'MS': 'Mississippi', 'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska',
    'NV': 'Nevada', 'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico',
    'NY': 'New York', 'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio',
    'OK': 'Oklahoma', 'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island',
    'SC': 'South Carolina', 'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas',
    'UT': 'Utah', 'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington',
    'WV': 'West Virginia', 'WI': 'Wisconsin', 'WY': 'Wyoming',
}


def _decode(code, label_map):
    """Decode a PAD-US code using a label map. Returns decoded label or the raw code."""
    if not code:
        return ''
    code = str(code).strip()
    return label_map.get(code, code)


def _get_pool():
    """Lazy-init the connection pool. Returns None if Postgres is unreachable."""
    global _pool, _pool_failed
    if _pool is not None:
        return _pool
    if _pool_failed:
        return None

    try:
        _pool = psycopg2.pool.SimpleConnectionPool(
            minconn=1,
            maxconn=3,
            host=os.environ.get('PADUS_DB_HOST', 'localhost'),
            port=int(os.environ.get('PADUS_DB_PORT', '5432')),
            dbname=os.environ.get('PADUS_DB_NAME', 'padus'),
            user=os.environ.get('PADUS_DB_USER', 'overture'),
            password=os.environ.get('PADUS_DB_PASSWORD', ''),
            connect_timeout=5,
        )
        logger.info("PAD-US PostgreSQL connection pool initialized")
        return _pool
    except Exception as e:
        _pool_failed = True
        logger.warning(f"PAD-US PostgreSQL unavailable, land classification disabled: {e}")
        return None


def _query_all(sql, params):
    """Execute a query and return all rows as a list of dicts, or empty list."""
    pool = _get_pool()
    if pool is None:
        return []

    conn = None
    try:
        conn = pool.getconn()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            if not rows:
                return []
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, row)) for row in rows]
    except Exception as e:
        logger.warning(f"PAD-US query error: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return []
    finally:
        if conn:
            try:
                pool.putconn(conn)
            except Exception:
                pass


def lookup_landclass(lat, lon):
    """
    Look up PAD-US land classifications for a point.

    Returns a list of classification dicts, ordered by area ascending
    (smallest/most specific first). Empty list on error or no results.
    """
    rows = _query_all(
        """SELECT unit_nm, mang_name, mang_type, own_name, own_type,
                  des_tp, gap_sts, pub_access, category, gis_acres, state_nm
           FROM pad_units
           WHERE ST_Intersects(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
           ORDER BY gis_acres ASC
           LIMIT 10""",
        (lon, lat)
    )

    results = []
    for row in rows:
        pa_code = str(row.get('pub_access', '')).strip()

        results.append({
            'unit_name': (row.get('unit_nm') or '').strip(),
            'manager_name': _decode(row.get('mang_name'), AGENCY_NAME_MAP),
            'manager_type': _decode(row.get('mang_type'), AGENCY_TYPE_MAP),
            'owner_type': _decode(row.get('own_type'), AGENCY_TYPE_MAP),
            'designation_type': _decode(row.get('des_tp'), DESIGNATION_TYPE_MAP),
            'gap_status': str(row.get('gap_sts', '')).strip(),
            'public_access': _decode(pa_code, PUBLIC_ACCESS_MAP),
            'public_access_code': pa_code,
            'category': _decode(row.get('category'), CATEGORY_MAP),
            'acres': row.get('gis_acres'),
            'state': _decode(row.get('state_nm'), STATE_MAP),
        })

    return results


def format_summary(classifications):
    """
    Format a human-readable summary from classification results.

    Returns the most specific unit name, or None if no results.
    """
    if not classifications:
        return None
    # First result is smallest/most specific (ordered by acres ASC)
    return classifications[0].get('unit_name') or None
