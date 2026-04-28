"""
RECON Domain Taxonomy

Single source of truth for the 18 knowledge domains and their PeerTube
category ID mappings. IDs 100-117 are registered via the
peertube-plugin-recon-domains plugin.

Import VALID_DOMAINS from here instead of defining local sets.
"""

DOMAIN_CATEGORY_MAP = {
    'Agriculture & Livestock': 100,
    'Civil Organization': 101,
    'Communications': 102,
    'Food Systems': 103,
    'Foundational Skills': 104,
    'Logistics': 105,
    'Medical': 106,
    'Navigation': 107,
    'Operations': 108,
    'Power Systems': 109,
    'Preservation & Storage': 110,
    'Security': 111,
    'Shelter & Construction': 112,
    'Technology': 113,
    'Tools & Equipment': 114,
    'Vehicles': 115,
    'Water Systems': 116,
    'Wilderness Skills': 117,
}

VALID_DOMAINS = set(DOMAIN_CATEGORY_MAP.keys())

CATEGORY_DOMAIN_MAP = {v: k for k, v in DOMAIN_CATEGORY_MAP.items()}
