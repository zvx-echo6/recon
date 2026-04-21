"""
RECON geocode — structured preprocessing, multi-source retrieval, reranking.

Replaces the naive Photon-only search with:
  1. usaddress parsing + intent classification (ADDRESS / POI / LOCALITY / COORD / POSTCODE)
  2. Multi-source retrieval: ADDRESS → Netsyms + Photon; POI/LOCALITY → Photon /api
  3. Python reranker with weighted signals

Public entry point: geocode(query, limit) → {query, results, count}
"""

import math
import re
import logging

import requests
import usaddress
from rapidfuzz import fuzz

from .utils import setup_logging

logger = setup_logging('recon.geocode')

# ── Trace logger for reranking audit ──
_trace_logger = logging.getLogger('recon.geocode.trace')
_trace_handler = logging.FileHandler('/tmp/geocode_rerank_trace.log')
_trace_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
_trace_logger.addHandler(_trace_handler)
_trace_logger.setLevel(logging.DEBUG)

# ── Config constants ──
PHOTON_URL = "http://localhost:2322"
GEOCODE_BIAS_LAT = 42.5736
GEOCODE_BIAS_LON = -114.6066
GEOCODE_BIAS_ZOOM = 10
ADDRESS_BOOK_ANNOTATION_RADIUS_M = 75

# ── Reranker weights ──
# Derived from research analysis of failure modes:
#   housenumber_exact is the strongest signal because Photon's soft-boost
#   lets wrong-number results bubble up.  street_name_fuzz and locality_fuzz
#   handle abbreviation/case variation.  source_authority gives Netsyms a
#   boost for US addresses since it has USPS-verified data.
W_HOUSENUMBER_EXACT      =  6.0   # exact housenumber match
W_HOUSENUMBER_MISMATCH   = -5.0   # housenumber present but wrong
W_STREET_NAME_FUZZ       =  3.0   # fuzzy street name similarity [0..1] * weight
W_TOKEN_COVERAGE         =  2.0   # fraction of query tokens found in result
W_STREET_TYPE_MATCH      =  1.5   # "st" matches "street", etc.
W_LOCALITY_FUZZ          =  2.0   # city/state fuzzy match
W_SOURCE_AUTHORITY        =  2.0   # Netsyms for US addresses
W_LAYER_RANK             =  1.0   # type-appropriate results ranked higher
W_PHOTON_POSITION_NORM   =  1.0   # Photon's native ranking (normalized by position)
W_STATE_EXACT            =  1.0   # exact state code match
W_POI_CLASS_BOOST        =  3.0   # amenity/shop/etc boost for business-name queries
W_HIGHWAY_CLASS_PENALTY  = -4.0   # highway/route penalty for business-name queries

# ── US abbreviation expansions ──
# Applied ONLY to parsed StreetName/StreetNamePostType tokens, NOT to ordinals.
_STREET_TYPE_ABBREVS = {
    'st': 'street', 'ave': 'avenue', 'blvd': 'boulevard', 'dr': 'drive',
    'rd': 'road', 'ln': 'lane', 'ct': 'court', 'cir': 'circle',
    'pl': 'place', 'way': 'way', 'pkwy': 'parkway', 'hwy': 'highway',
    'trl': 'trail', 'ter': 'terrace', 'sq': 'square',
}
_DIRECTIONAL_ABBREVS = {
    'n': 'north', 's': 'south', 'e': 'east', 'w': 'west',
    'ne': 'northeast', 'nw': 'northwest', 'se': 'southeast', 'sw': 'southwest',
}
_ORDINAL_RE = re.compile(r'^\d+(st|nd|rd|th)$', re.IGNORECASE)

# ── Road keywords (for detecting when query is about a road vs a business) ──
_ROAD_KEYWORDS = (
    set(_STREET_TYPE_ABBREVS.keys())
    | set(_STREET_TYPE_ABBREVS.values())
    | {'route', 'rte', 'pass'}
)

# ── US state codes ──
_STATE_CODES = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA',
    'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD',
    'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ',
    'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC',
    'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY', 'DC',
}

# ── Full state name → code (for intent classifier) ──
_STATE_NAME_TO_CODE = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE',
    'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI', 'idaho': 'ID',
    'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS',
    'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
    'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN',
    'mississippi': 'MS', 'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE',
    'nevada': 'NV', 'new hampshire': 'NH', 'new jersey': 'NJ',
    'new mexico': 'NM', 'new york': 'NY', 'north carolina': 'NC',
    'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK', 'oregon': 'OR',
    'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC',
    'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT',
    'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA',
    'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY',
}

# Coordinate regex
_COORD_RE = re.compile(r'^\s*(-?\d+\.?\d*)\s*[,\s]\s*(-?\d+\.?\d*)\s*$')


# ═══════════════════════════════════════════════════════════════════
#  STEP 1: PREPROCESSING
# ═══════════════════════════════════════════════════════════════════

def _parse_coords(text):
    """Return (lat, lon) if text looks like coordinates with valid bounds, else None."""
    m = _COORD_RE.match(text.strip())
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


def _classify_and_parse(query):
    """
    Parse query with usaddress, classify intent, expand abbreviations.

    Returns (intent, parsed_dict) where:
      intent: 'ADDRESS' | 'POI' | 'LOCALITY' | 'POSTCODE' | 'COORD' | 'UNKNOWN'
      parsed_dict: {number, street, city, state, zipcode, raw_query, expanded_query}
    """
    q = query.strip()
    parsed = {
        'number': None, 'street': None, 'street_raw': None,
        'city': None, 'state': None,
        'zipcode': None, 'raw_query': q, 'expanded_query': q,
    }

    # Coordinate check first
    if _parse_coords(q):
        return 'COORD', parsed

    # Try usaddress
    try:
        tagged, addr_type = usaddress.tag(q)
    except usaddress.RepeatedLabelError:
        # Ambiguous input — fall back to free-text Photon
        return 'UNKNOWN', parsed

    # Extract components
    number = tagged.get('AddressNumber', '').strip()
    street_name = tagged.get('StreetName', '').strip()
    street_pre_dir = tagged.get('StreetNamePreDirectional', '').strip()
    street_post_type = tagged.get('StreetNamePostType', '').strip()
    place = tagged.get('PlaceName', '').strip()
    state = tagged.get('StateName', '').strip()
    zipcode = tagged.get('ZipCode', '').strip()

    # ── Fix usaddress edge case: "214 N St Filer" ──
    # usaddress reads single-letter directional + "St" as PreDirectional + empty,
    # mashing "St Filer" into StreetName.  Detect: PreDirectional is single letter,
    # StreetName has 2+ tokens where the first is a street type.
    if (street_pre_dir and len(street_pre_dir) <= 2
            and not street_name.strip().startswith(street_pre_dir)
            and ' ' in street_name):
        name_tokens = street_name.split()
        first_lower = name_tokens[0].lower()
        if first_lower in _STREET_TYPE_ABBREVS or first_lower in _STREET_TYPE_ABBREVS.values():
            # "N" is actually the street name, "St" is the post-type
            street_name = street_pre_dir
            street_post_type = name_tokens[0]
            if len(name_tokens) > 1:
                place = ' '.join(name_tokens[1:])
            street_pre_dir = ''

    # ── Expand abbreviations (guard ordinals) ──
    expanded_parts = []

    if number:
        parsed['number'] = number
        expanded_parts.append(number)

    if street_pre_dir:
        exp = _DIRECTIONAL_ABBREVS.get(street_pre_dir.lower(), street_pre_dir)
        expanded_parts.append(exp)

    if street_name:
        # Don't expand ordinals: "21st" stays "21st"
        if _ORDINAL_RE.match(street_name):
            expanded_parts.append(street_name)
        else:
            # Expand directional abbreviation if it IS the street name
            exp = _DIRECTIONAL_ABBREVS.get(street_name.lower(), street_name)
            expanded_parts.append(exp)
        parsed['street'] = street_name

    if street_post_type:
        if _ORDINAL_RE.match(street_post_type):
            expanded_parts.append(street_post_type)
        else:
            exp = _STREET_TYPE_ABBREVS.get(street_post_type.lower(), street_post_type)
            expanded_parts.append(exp)

    # Build raw street (original abbreviations, for Netsyms) and expanded (for Photon)
    raw_street_parts = []
    if street_pre_dir:
        raw_street_parts.append(street_pre_dir)
    if street_name:
        raw_street_parts.append(street_name)
    if street_post_type:
        raw_street_parts.append(street_post_type)
    parsed['street_raw'] = ' '.join(raw_street_parts)

    # Build the full expanded street
    if expanded_parts:
        # The street is everything after the number
        street_full = ' '.join(expanded_parts[1:] if number else expanded_parts)
        parsed['street'] = street_full

    if place:
        parsed['city'] = place
        expanded_parts.append(place)
    if state:
        parsed['state'] = state.upper()
        expanded_parts.append(state)
    if zipcode:
        parsed['zipcode'] = zipcode
        expanded_parts.append(zipcode)

    parsed['expanded_query'] = ' '.join(expanded_parts)

    # ── Intent classification ──
    if addr_type == 'Street Address' and number:
        return 'ADDRESS', parsed
    elif zipcode and not number and not street_name:
        return 'POSTCODE', parsed
    elif addr_type == 'Ambiguous':
        # Check if it looks like a locality: last token(s) are a state code or name
        tokens = q.replace(',', ' ').split()
        if len(tokens) >= 2:
            last_upper = tokens[-1].upper()
            if last_upper in _STATE_CODES:
                parsed['city'] = ' '.join(tokens[:-1])
                parsed['state'] = last_upper
                return 'LOCALITY', parsed
            # Check full state names (single-word like "idaho" or two-word like "new york")
            last_lower = tokens[-1].lower()
            if last_lower in _STATE_NAME_TO_CODE:
                parsed['city'] = ' '.join(tokens[:-1])
                parsed['state'] = _STATE_NAME_TO_CODE[last_lower]
                return 'LOCALITY', parsed
            if len(tokens) >= 3:
                two_word = f"{tokens[-2].lower()} {last_lower}"
                if two_word in _STATE_NAME_TO_CODE:
                    parsed['city'] = ' '.join(tokens[:-2])
                    parsed['state'] = _STATE_NAME_TO_CODE[two_word]
                    return 'LOCALITY', parsed
        return 'UNKNOWN', parsed
    else:
        return 'UNKNOWN', parsed


# ═══════════════════════════════════════════════════════════════════
#  STEP 2: RETRIEVAL
# ═══════════════════════════════════════════════════════════════════

def _retrieve_netsyms(parsed, limit=10):
    """Query Netsyms for structured address lookup. Returns list of candidate dicts."""
    try:
        from . import netsyms
    except Exception:
        return []

    results = []
    number = parsed.get('number', '')
    street = parsed.get('street_raw') or parsed.get('street', '')
    city = parsed.get('city', '')
    state = parsed.get('state', '')
    zipcode = parsed.get('zipcode', '')

    if number and street:
        rows = netsyms.lookup_by_street(
            number, street, city=city, state=state, zipcode=zipcode, limit=limit
        )
    elif zipcode:
        rows = netsyms.lookup_by_zipcode(zipcode, limit=limit)
    else:
        return []

    for row in rows:
        addr_parts = [row['number'], row['street']]
        if row.get('street2'):
            addr_parts.append(row['street2'])
        addr_parts.extend([row['city'], row['state'], row['zipcode']])
        display = ' '.join(p for p in addr_parts if p)
        results.append({
            'name': display,
            'lat': row['lat'],
            'lon': row['lon'],
            'source': 'netsyms',
            'type': 'street_address',
            'raw': row,
            '_number': row.get('number', ''),
            '_street': row.get('street', ''),
            '_city': row.get('city', ''),
            '_state': row.get('state', ''),
        })
    return results


def _retrieve_photon_structured(parsed, limit=10):
    """Query Photon /structured endpoint for address lookup."""
    params = {'limit': limit, 'countrycode': 'US'}
    if parsed.get('street'):
        params['street'] = parsed['street']
    if parsed.get('number'):
        params['housenumber'] = parsed['number']
    if parsed.get('city'):
        params['city'] = parsed['city']
    if parsed.get('state'):
        params['state'] = parsed['state']

    if 'street' not in params:
        return []

    try:
        resp = requests.get(f"{PHOTON_URL}/structured", params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.debug("Photon /structured failed: %s", e)
        return []

    return _parse_photon_features(data.get('features', []), 'photon')


def _retrieve_photon_freetext(query, limit=10):
    """Query Photon /api for free-text search with location bias."""
    try:
        params = {
            'q': query,
            'limit': limit,
            'lat': GEOCODE_BIAS_LAT,
            'lon': GEOCODE_BIAS_LON,
            'zoom': GEOCODE_BIAS_ZOOM,
        }
        resp = requests.get(f"{PHOTON_URL}/api", params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.debug("Photon /api failed: %s", e)
        return []

    return _parse_photon_features(data.get('features', []), 'photon')


def _parse_photon_features(features, source):
    """Convert Photon GeoJSON features to candidate dicts."""
    results = []
    for i, feature in enumerate(features):
        props = feature.get('properties', {})
        coords = feature.get('geometry', {}).get('coordinates', [0, 0])

        osm_key = props.get('osm_key', '')
        osm_value = props.get('osm_value', '')
        feat_type = props.get('type', '')
        has_hn = bool(props.get('housenumber'))

        if osm_key in ('amenity', 'shop', 'tourism', 'leisure', 'office'):
            rtype = 'poi'
        elif has_hn or osm_value in ('house', 'residential'):
            rtype = 'street_address'
        elif feat_type in ('city', 'town', 'village', 'hamlet', 'county', 'state', 'country'):
            rtype = 'locality'
        else:
            rtype = 'poi'

        # Build display name
        parts = []
        hn = props.get('housenumber')
        street = props.get('street')
        name = props.get('name', '')
        if hn and street:
            parts.append(f"{hn} {street}")
            if name and name != street:
                parts.append(name)
        elif name:
            parts.append(name)
        elif street:
            parts.append(street)
        for key in ('city', 'county', 'state', 'country'):
            v = props.get(key)
            if v and (not parts or v != parts[-1]):
                parts.append(v)
        display = ', '.join(p for p in parts if p) or 'Unknown'

        results.append({
            'name': display,
            'lat': coords[1],
            'lon': coords[0],
            'source': source,
            'type': rtype,
            'raw': props,
            '_photon_rank': i,
            '_number': props.get('housenumber', ''),
            '_street': props.get('street', ''),
            # For locality results, the name IS the city (Photon omits 'city' on city-type features)
            '_city': props.get('city', '') or (props.get('name', '') if rtype == 'locality' else ''),
            '_state': props.get('state', ''),
        })
    return results


# ═══════════════════════════════════════════════════════════════════
#  STEP 3: RERANKER
# ═══════════════════════════════════════════════════════════════════

def _expand_street_type(s):
    """Expand a street type abbreviation for comparison."""
    return _STREET_TYPE_ABBREVS.get(s.lower(), s.lower())


def _score_candidate(candidate, parsed, intent):
    """
    Score a candidate against the parsed query.
    Returns (total_score, signal_breakdown_dict).
    """
    signals = {}
    total = 0.0

    query_number = (parsed.get('number') or '').strip().upper()
    query_street = (parsed.get('street') or '').strip().upper()
    query_city = (parsed.get('city') or '').strip().upper()
    query_state = (parsed.get('state') or '').strip().upper()

    cand_number = (candidate.get('_number') or '').strip().upper()
    cand_street = (candidate.get('_street') or '').strip().upper()
    cand_city = (candidate.get('_city') or '').strip().upper()
    cand_state = (candidate.get('_state') or '').strip().upper()

    # ── Housenumber ──
    if intent == 'ADDRESS' and query_number:
        if cand_number == query_number:
            signals['housenumber_exact'] = W_HOUSENUMBER_EXACT
            total += W_HOUSENUMBER_EXACT
        elif cand_number and cand_number != query_number:
            signals['housenumber_mismatch'] = W_HOUSENUMBER_MISMATCH
            total += W_HOUSENUMBER_MISMATCH

    # ── Street name fuzz ──
    if query_street and cand_street:
        # Expand both for comparison
        q_expanded = ' '.join(_expand_street_type(t) for t in query_street.split())
        c_expanded = ' '.join(_expand_street_type(t) for t in cand_street.split())
        ratio = fuzz.token_sort_ratio(q_expanded, c_expanded) / 100.0
        score = ratio * W_STREET_NAME_FUZZ
        signals['street_name_fuzz'] = round(score, 2)
        total += score

    # ── Street type match ──
    if query_street and cand_street:
        q_tokens = set(_expand_street_type(t) for t in query_street.split())
        c_tokens = set(_expand_street_type(t) for t in cand_street.split())
        # Check if the street type words overlap
        street_types = set(_STREET_TYPE_ABBREVS.values())
        q_types = q_tokens & street_types
        c_types = c_tokens & street_types
        if q_types and q_types & c_types:
            signals['street_type_match'] = W_STREET_TYPE_MATCH
            total += W_STREET_TYPE_MATCH

    # ── Token coverage ──
    raw_q = parsed.get('raw_query', '').upper()
    q_tokens = set(raw_q.replace(',', ' ').split())
    if q_tokens:
        cand_text = candidate.get('name', '').upper()
        matched = sum(1 for t in q_tokens if t in cand_text)
        coverage = matched / len(q_tokens)
        score = coverage * W_TOKEN_COVERAGE
        signals['token_coverage'] = round(score, 2)
        total += score

    # ── Locality fuzz ──
    if query_city and cand_city:
        ratio = fuzz.ratio(query_city, cand_city) / 100.0
        score = ratio * W_LOCALITY_FUZZ
        signals['locality_fuzz'] = round(score, 2)
        total += score

    # ── State exact ──
    if query_state and cand_state:
        if cand_state == query_state:
            signals['state_exact'] = W_STATE_EXACT
            total += W_STATE_EXACT

    # ── Source authority ──
    if candidate.get('source') == 'netsyms' and intent == 'ADDRESS':
        signals['source_authority'] = W_SOURCE_AUTHORITY
        total += W_SOURCE_AUTHORITY

    # ── Layer rank (type-appropriate bonus) ──
    cand_type = candidate.get('type', '')
    if intent == 'ADDRESS' and cand_type == 'street_address':
        signals['layer_rank'] = W_LAYER_RANK
        total += W_LAYER_RANK
    elif intent == 'LOCALITY' and cand_type == 'locality':
        signals['layer_rank'] = W_LAYER_RANK
        total += W_LAYER_RANK
    elif intent == 'POI' and cand_type == 'poi':
        signals['layer_rank'] = W_LAYER_RANK
        total += W_LAYER_RANK

    # ── Photon position normalization ──
    photon_rank = candidate.get('_photon_rank')
    if photon_rank is not None:
        # Top result gets full bonus, decays linearly
        score = max(0, (1.0 - photon_rank / 10.0)) * W_PHOTON_POSITION_NORM
        signals['photon_position'] = round(score, 2)
        total += score

    # ── Business intent POI boost ──
    # When the query has no road keywords (likely a business/POI search),
    # boost amenity/shop/etc results and penalize highway/route results.
    # Skipped for LOCALITY, POSTCODE, COORD queries where class is irrelevant.
    if intent not in ('LOCALITY', 'POSTCODE', 'COORD'):
        q_tokens_lower = set(parsed.get('raw_query', '').lower().replace(',', ' ').split())
        if not (q_tokens_lower & _ROAD_KEYWORDS):
            osm_key = (candidate.get('raw') or {}).get('osm_key', '')
            if osm_key in ('amenity', 'shop', 'tourism', 'leisure', 'office', 'craft'):
                signals['poi_class_boost'] = W_POI_CLASS_BOOST
                total += W_POI_CLASS_BOOST
            elif osm_key in ('highway', 'route'):
                signals['highway_class_penalty'] = W_HIGHWAY_CLASS_PENALTY
                total += W_HIGHWAY_CLASS_PENALTY

    return round(total, 2), signals


def _build_match_code(candidate, parsed, intent):
    """Build a match_code dict indicating match quality for each field."""
    mc = {}
    if intent == 'ADDRESS':
        q_num = (parsed.get('number') or '').strip().upper()
        c_num = (candidate.get('_number') or '').strip().upper()
        if q_num and c_num == q_num:
            mc['housenumber'] = 'matched'
        elif q_num and c_num:
            mc['housenumber'] = 'unmatched'
        elif q_num and not c_num:
            mc['housenumber'] = 'inferred'

        q_street = (parsed.get('street') or '').strip().upper()
        c_street = (candidate.get('_street') or '').strip().upper()
        if q_street and c_street:
            q_exp = ' '.join(_expand_street_type(t) for t in q_street.split())
            c_exp = ' '.join(_expand_street_type(t) for t in c_street.split())
            ratio = fuzz.token_sort_ratio(q_exp, c_exp) / 100.0
            mc['street'] = 'matched' if ratio > 0.8 else 'unmatched'
        elif q_street:
            mc['street'] = 'inferred'

        q_city = (parsed.get('city') or '').strip().upper()
        c_city = (candidate.get('_city') or '').strip().upper()
        if q_city and c_city:
            ratio = fuzz.ratio(q_city, c_city) / 100.0
            mc['city'] = 'matched' if ratio > 0.8 else 'unmatched'
        elif q_city:
            mc['city'] = 'inferred'

    return mc


def _rerank(candidates, parsed, intent, query, limit):
    """Score, sort, and trim candidates. Trace-log top 3."""
    scored = []
    for c in candidates:
        total, signals = _score_candidate(c, parsed, intent)
        c['_score'] = total
        c['_signals'] = signals
        scored.append(c)

    scored.sort(key=lambda c: c['_score'], reverse=True)

    # Trace log for audit
    _trace_logger.debug("─── Query: %r  intent=%s ───", query, intent)
    for i, c in enumerate(scored):
        osm_key = (c.get('raw') or {}).get('osm_key', '—')
        osm_val = (c.get('raw') or {}).get('osm_value', '—')
        _trace_logger.debug(
            "  #%d score=%.2f src=%s key=%s/%s name=%s",
            i, c['_score'], c.get('source', '?'), osm_key, osm_val,
            c.get('name', '?')[:60]
        )
        _trace_logger.debug("      signals=%s", c.get('_signals', {}))

    # Clean internal fields and add match_code
    result = []
    for c in scored[:limit]:
        mc = _build_match_code(c, parsed, intent)

        # Assign confidence from score
        score = c.get('_score', 0)
        if score >= 10:
            confidence = 'exact'
        elif score >= 5:
            confidence = 'high'
        elif score >= 2:
            confidence = 'medium'
        else:
            confidence = 'low'

        entry = {
            'name': c['name'],
            'lat': c['lat'],
            'lon': c['lon'],
            'source': c['source'],
            'confidence': confidence,
            'type': c.get('type', 'poi'),
            'raw': c.get('raw'),
        }
        if mc:
            entry['match_code'] = mc
        result.append(entry)

    return result


# ═══════════════════════════════════════════════════════════════════
#  STEP 4: ANNOTATION
# ═══════════════════════════════════════════════════════════════════

def _haversine_m(lat1, lon1, lat2, lon2):
    """Haversine distance in meters."""
    R = 6_371_000
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _annotate_with_address_book(results):
    """Add labeled_as to results within radius of an address book entry."""
    try:
        from . import address_book
        entries = address_book.load()
    except Exception:
        return
    for result in results:
        rlat, rlon = result.get('lat'), result.get('lon')
        if rlat is None or rlon is None:
            continue
        for entry in entries:
            elat, elon = entry.get('lat'), entry.get('lon')
            if elat is None or elon is None:
                continue
            if _haversine_m(rlat, rlon, elat, elon) <= ADDRESS_BOOK_ANNOTATION_RADIUS_M:
                result['labeled_as'] = entry['name']
                break


# ═══════════════════════════════════════════════════════════════════
#  PUBLIC API
# ═══════════════════════════════════════════════════════════════════

def geocode(query, limit=10):
    """
    Structured geocoding with multi-source retrieval and reranking.

    Returns {query, results: [...], count} — always 200-safe.
    """
    limit = max(1, min(limit, 20))
    q = (query or '').strip()
    empty = {'query': q, 'results': [], 'count': 0}

    if not q:
        return empty

    # ── Coordinate detection ──
    coords = _parse_coords(q)
    if coords:
        return {
            'query': q,
            'results': [{
                'name': q,
                'lat': coords[0],
                'lon': coords[1],
                'source': 'coordinates',
                'confidence': 'exact',
                'type': 'coordinates',
                'raw': None,
            }],
            'count': 1,
        }

    # ── Address book nickname short-circuit ──
    normalized_q = ' '.join(q.lower().replace(',', ' ').split())
    is_single_word = ' ' not in normalized_q
    try:
        from . import address_book
        ab_match = address_book.lookup(q)
        if (ab_match
                and ab_match['confidence'] == 'exact'
                and ab_match.get('lat') and ab_match.get('lon')
                and is_single_word):
            logger.info("geocode: nickname short-circuit %r → %s", q, ab_match['name'])
            return {
                'query': q,
                'results': [{
                    'name': ab_match.get('address') or ab_match['name'],
                    'lat': ab_match['lat'],
                    'lon': ab_match['lon'],
                    'source': 'address_book',
                    'confidence': 'exact',
                    'type': 'nickname',
                    'raw': ab_match,
                }],
                'count': 1,
            }
    except Exception as e:
        logger.debug("geocode: address_book lookup failed: %s", e)

    # ── Classify intent + parse ──
    intent, parsed = _classify_and_parse(q)
    logger.debug("geocode: intent=%s parsed=%s", intent, parsed)

    # ── Retrieve candidates ──
    candidates = []

    if intent == 'ADDRESS':
        # Parallel: Netsyms (structured) + Photon (freetext with expanded query)
        netsyms_results = _retrieve_netsyms(parsed, limit=limit)
        photon_results = _retrieve_photon_freetext(
            parsed.get('expanded_query', q), limit=limit
        )
        # Also try Photon /structured for addresses
        photon_struct = _retrieve_photon_structured(parsed, limit=5)
        candidates = netsyms_results + photon_results + photon_struct

    elif intent == 'POSTCODE':
        netsyms_results = _retrieve_netsyms(parsed, limit=limit)
        photon_results = _retrieve_photon_freetext(q, limit=limit)
        candidates = netsyms_results + photon_results

    elif intent in ('LOCALITY', 'POI', 'UNKNOWN'):
        candidates = _retrieve_photon_freetext(q, limit=limit)

    # ── Deduplicate by (lat, lon) proximity ──
    deduped = []
    for c in candidates:
        is_dup = False
        for existing in deduped:
            if (_haversine_m(c['lat'], c['lon'], existing['lat'], existing['lon']) < 50
                    and c.get('source') == existing.get('source')):
                is_dup = True
                break
        if not is_dup:
            deduped.append(c)
    candidates = deduped

    # ── Rerank ──
    results = _rerank(candidates, parsed, intent, q, limit)

    # ── Address book annotation ──
    _annotate_with_address_book(results)

    logger.info("geocode: %r → intent=%s, %d results", q, intent, len(results))
    return {'query': q, 'results': results, 'count': len(results)}
