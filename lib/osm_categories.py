"""
Human-readable category names for OSM class/type pairs.

Used by the place detail proxy to turn ("amenity", "cafe") into "Coffee shop".
Covers ~50 common categories; unmapped pairs fall back to title-cased class:type.
"""

# Exact (class, type) → label
CATEGORY_MAP = {
    # Amenity
    ("amenity", "cafe"): "Coffee shop",
    ("amenity", "restaurant"): "Restaurant",
    ("amenity", "fast_food"): "Fast food restaurant",
    ("amenity", "bar"): "Bar",
    ("amenity", "pub"): "Pub",
    ("amenity", "biergarten"): "Beer garden",
    ("amenity", "ice_cream"): "Ice cream shop",
    ("amenity", "fuel"): "Gas station",
    ("amenity", "charging_station"): "EV charging station",
    ("amenity", "parking"): "Parking",
    ("amenity", "bank"): "Bank",
    ("amenity", "atm"): "ATM",
    ("amenity", "pharmacy"): "Pharmacy",
    ("amenity", "hospital"): "Hospital",
    ("amenity", "clinic"): "Clinic",
    ("amenity", "dentist"): "Dentist",
    ("amenity", "doctors"): "Doctor's office",
    ("amenity", "veterinary"): "Veterinarian",
    ("amenity", "school"): "School",
    ("amenity", "university"): "University",
    ("amenity", "college"): "College",
    ("amenity", "library"): "Library",
    ("amenity", "post_office"): "Post office",
    ("amenity", "fire_station"): "Fire station",
    ("amenity", "police"): "Police station",
    ("amenity", "townhall"): "Town hall",
    ("amenity", "place_of_worship"): "Place of worship",
    ("amenity", "theatre"): "Theatre",
    ("amenity", "cinema"): "Cinema",
    ("amenity", "community_centre"): "Community center",
    ("amenity", "toilets"): "Restrooms",
    ("amenity", "drinking_water"): "Drinking water",
    ("amenity", "shelter"): "Shelter",
    ("amenity", "camping"): "Campground",
    # Shop
    ("shop", "supermarket"): "Supermarket",
    ("shop", "convenience"): "Convenience store",
    ("shop", "hardware"): "Hardware store",
    ("shop", "clothes"): "Clothing store",
    ("shop", "car_repair"): "Auto repair",
    ("shop", "car"): "Car dealership",
    ("shop", "bakery"): "Bakery",
    ("shop", "butcher"): "Butcher",
    # Leisure
    ("leisure", "park"): "Park",
    ("leisure", "playground"): "Playground",
    ("leisure", "sports_centre"): "Sports center",
    ("leisure", "swimming_pool"): "Swimming pool",
    ("leisure", "golf_course"): "Golf course",
    ("leisure", "nature_reserve"): "Nature reserve",
    ("leisure", "campsite"): "Campsite",
    # Tourism
    ("tourism", "hotel"): "Hotel",
    ("tourism", "motel"): "Motel",
    ("tourism", "guest_house"): "Guest house",
    ("tourism", "hostel"): "Hostel",
    ("tourism", "camp_site"): "Campsite",
    ("tourism", "viewpoint"): "Viewpoint",
    ("tourism", "museum"): "Museum",
    ("tourism", "information"): "Information",
    ("tourism", "attraction"): "Tourist attraction",
    ("tourism", "picnic_site"): "Picnic site",
    # Natural
    ("natural", "peak"): "Peak",
    ("natural", "spring"): "Spring",
    ("natural", "hot_spring"): "Hot spring",
    ("natural", "lake"): "Lake",
    ("natural", "water"): "Water body",
    ("natural", "cliff"): "Cliff",
    ("natural", "cave_entrance"): "Cave",
    # Highway
    ("highway", "bus_stop"): "Bus stop",
    ("highway", "rest_area"): "Rest area",
    # Boundary
    ("boundary", "administrative"): "Administrative boundary",
    ("boundary", "protected_area"): "Protected area",
    ("boundary", "national_park"): "National park",
    # Place
    ("place", "city"): "City",
    ("place", "town"): "Town",
    ("place", "village"): "Village",
    ("place", "hamlet"): "Hamlet",
    ("place", "suburb"): "Suburb",
    ("place", "neighbourhood"): "Neighborhood",
    # Building
    ("building", "yes"): "Building",
    # Waterway
    ("waterway", "river"): "River",
    ("waterway", "stream"): "Stream",
    ("waterway", "waterfall"): "Waterfall",
    # Landuse
    ("landuse", "cemetery"): "Cemetery",
    ("landuse", "forest"): "Forest",
    # Historic
    ("historic", "monument"): "Monument",
    ("historic", "memorial"): "Memorial",
    ("historic", "ruins"): "Ruins",
}

# Class-level wildcard fallbacks (when exact type isn't mapped)
CLASS_FALLBACKS = {
    "shop": "Shop",
    "amenity": "Amenity",
    "leisure": "Leisure",
    "tourism": "Tourism",
    "natural": "Natural feature",
    "historic": "Historic site",
}


def humanize_category(osm_class, osm_type):
    """Return a human-readable category string for an OSM class/type pair."""
    if not osm_class or not osm_type:
        return "Place"

    osm_class = osm_class.lower()
    osm_type = osm_type.lower()

    # Exact match
    label = CATEGORY_MAP.get((osm_class, osm_type))
    if label:
        return label

    # Class-level wildcard with formatted type
    prefix = CLASS_FALLBACKS.get(osm_class)
    if prefix:
        nice_type = osm_type.replace("_", " ").title()
        return f"{prefix}: {nice_type}" if prefix != nice_type else prefix

    # Generic fallback
    nice_class = osm_class.replace("_", " ").title()
    nice_type = osm_type.replace("_", " ").title()
    return f"{nice_class}: {nice_type}"
