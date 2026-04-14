#!/usr/bin/env python3
"""
domain_remap.py — Fix RECON concept domain classifications without API calls.

What this does:
  1. Strips "Reference" from concepts that have other real domains
  2. Remaps variant domain spellings to canonical names
  3. Reclassifies solo-Reference concepts using their subdomain tags
  4. Writes a JSONL file of true unknowns for API re-enrichment

Each window file is a JSON array of concept dicts.
Field names: "domain" (list), "subdomain" (list)

Usage:
  python3 /opt/recon/scripts/domain_remap.py --dry-run   # report only
  python3 /opt/recon/scripts/domain_remap.py             # apply fixes
  python3 /opt/recon/scripts/domain_remap.py --workers 16
"""

import json
import argparse
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

CONCEPTS_DIR = Path("/opt/recon/data/concepts")
UNKNOWNS_OUTPUT = Path("/opt/recon/data/remap_unknowns.jsonl")

CANONICAL_DOMAINS = {
    "Defense & Tactics", "Sustainment Systems", "Off-Grid Systems",
    "Foundational Skills", "Communications", "Medical", "Food Systems",
    "Navigation", "Logistics", "Power Systems", "Leadership",
    "Scenario Playbooks", "Water Systems", "Security", "Community Coordination"
}

# Variant → Canonical mapping
VARIANT_MAP = {
    # Defense & Tactics
    "Tactical Ops": "Defense & Tactics",
    "Tactical_Ops": "Defense & Tactics",
    "Tactical Operations": "Defense & Tactics",
    "Tactical": "Defense & Tactics",
    "Tactical Skills": "Defense & Tactics",
    "Tactics": "Defense & Tactics",
    "Tactics & Defense": "Defense & Tactics",
    "Reconnaissance": "Defense & Tactics",
    "Fire Support": "Defense & Tactics",
    "Improvised Munitions": "Defense & Tactics",
    "Military Intelligence": "Defense & Tactics",
    "Military History": "Defense & Tactics",
    "Military Engineering": "Defense & Tactics",
    # Medical
    "Medical Care": "Medical",
    "Medical Alternatives": "Medical",
    "Medical/Dental": "Medical",
    "Medical & Dental": "Medical",
    "medical": "Medical",
    "Medical Awareness": "Medical",
    "Medical Disasters": "Medical",
    "Medical Emergency Survival": "Medical",
    "Medical Procedures": "Medical",
    "Medical Treatment": "Medical",
    "Medical Science": "Medical",
    "Medical History": "Medical",
    "Medical Diagnosis": "Medical",
    "Medical Skills": "Medical",
    "Medical Supply": "Medical",
    "Medical Gear": "Medical",
    "Medical Kits": "Medical",
    "Medical Logistics": "Logistics",
    "Medical First Aid": "Medical",
    "Medical Ethics": "Medical",
    "Medical Reference Ranges": "Medical",
    "Medical andSurgical Hints": "Medical",
    "Medical Aspects of Radiation Injury": "Medical",
    "Medical Uses": "Medical",
    "Medical Care in Developing Countries": "Medical",
    "Survival Medicine": "Medical",
    "Emergency War Surgery": "Medical",
    "First Aid": "Medical",
    "First Aid and Life Saving": "Medical",
    "Veterinary Medicine": "Medical",
    "Veterinary Hygiene": "Medical",
    "Veterinary": "Medical",
    "Pharmacology": "Medical",
    "Public Health": "Medical",
    "Health": "Medical",
    # Food Systems
    "Food_Systems": "Food Systems",
    "Food_systems": "Food Systems",
    "food_systems": "Food Systems",
    "Food Preservation": "Food Systems",
    "Food Safety": "Food Systems",
    "Food Security": "Food Systems",
    "Food & Nutrition": "Food Systems",
    "Diet & Nutrition": "Food Systems",
    "Culinary Arts": "Food Systems",
    "Foodprocessing": "Food Systems",
    "Food": "Food Systems",
    # Sustainment Systems
    "Sustainment_Systems": "Sustainment Systems",
    "Agriculture": "Sustainment Systems",
    "Agriculture & Natural Resources": "Sustainment Systems",
    "Agriculture and Natural Resources": "Sustainment Systems",
    "Horticulture": "Sustainment Systems",
    "Gardening": "Sustainment Systems",
    "Hydroponics": "Sustainment Systems",
    "Survival Skills": "Sustainment Systems",
    # Foundational Skills
    "Foundational_Skills": "Foundational Skills",
    "Primitive Living Skills": "Foundational Skills",
    "Woodcraft": "Foundational Skills",
    "Home Workshop": "Foundational Skills",
    "Science": "Foundational Skills",
    "Engineering": "Foundational Skills",
    "Construction": "Foundational Skills",
    "Industrial Processes": "Foundational Skills",
    "Machine Technology": "Foundational Skills",
    "Training": "Foundational Skills",
    "Education": "Foundational Skills",
    # Off-Grid Systems
    "Off-Grid_Systems": "Off-Grid Systems",
    "Appropriate Technology": "Off-Grid Systems",
    # Power Systems
    "Homebrewed Electricity": "Power Systems",
    "Renewable Energy": "Power Systems",
    "Renewable Energy FAQs": "Power Systems",
    "Alternative Fuels": "Power Systems",
    "Power_Systems": "Power Systems",
    # Water Systems
    "Water_Systems": "Water Systems",
    # Community Coordination
    "Community_Coordination": "Community Coordination",
    "Community_coordination": "Community Coordination",
    "Community": "Community Coordination",
    # Leadership
    "Leadership & Planning": "Leadership",
    "Planning": "Leadership",
    "Administration": "Leadership",
    "Governance": "Leadership",
    "Government": "Leadership",
    # Communications
    "Emergency Communications": "Communications",
    # Security
    "Security Systems": "Security",
    # Logistics
    "Transportation": "Logistics",
    # Scenario Playbooks
    "General Preparedness": "Scenario Playbooks",
    "Emergency Preparedness": "Scenario Playbooks",
    "Emergency Management": "Scenario Playbooks",
    "Wilderness Preparedness": "Scenario Playbooks",
    "Urban Preparedness": "Scenario Playbooks",
    "Winter Preparedness": "Scenario Playbooks",
    # Discard (noise domains)
    "Humor": None,
    "Recreation": None,
    "Business": None,
    "Finance": None,
    "Economics": None,
    "Economics/Finances": None,
    "Weird Science": None,
}

# Subdomain keyword → canonical domain (for solo-Reference reclassification)
SUBDOMAIN_MAP = {
    "first aid": "Medical",
    "emergency care": "Medical",
    "emergency medicine": "Medical",
    "trauma": "Medical",
    "anatomy": "Medical",
    "oral rehydration": "Medical",
    "ors": "Medical",
    "pharmacology": "Medical",
    "toxicology": "Medical",
    "antidote": "Medical",
    "nerve agent": "Defense & Tactics",
    "chemical warfare": "Defense & Tactics",
    "biological warfare": "Defense & Tactics",
    "nbc": "Defense & Tactics",
    "infectious disease": "Medical",
    "microbiology": "Medical",
    "virology": "Medical",
    "bacteriology": "Medical",
    "pediatric": "Medical",
    "surgery": "Medical",
    "wound care": "Medical",
    "veterinary": "Medical",
    "dental": "Medical",
    "dentistry": "Medical",
    "herbal": "Medical",
    "medicinal plant": "Medical",
    "medicinal herb": "Medical",
    "herbalism": "Medical",
    "food preservation": "Food Systems",
    "canning": "Food Systems",
    "fermentation": "Food Systems",
    "food storage": "Food Systems",
    "food safety": "Food Systems",
    "cooking": "Food Systems",
    "food processing": "Food Systems",
    "agriculture": "Sustainment Systems",
    "soil": "Sustainment Systems",
    "permaculture": "Sustainment Systems",
    "agroforestry": "Sustainment Systems",
    "livestock": "Sustainment Systems",
    "animal husbandry": "Sustainment Systems",
    "beekeeping": "Sustainment Systems",
    "foraging": "Sustainment Systems",
    "hunting": "Sustainment Systems",
    "fishing": "Sustainment Systems",
    "gardening": "Sustainment Systems",
    "mycology": "Sustainment Systems",
    "mushroom": "Sustainment Systems",
    "water purification": "Water Systems",
    "water filtration": "Water Systems",
    "water sanitation": "Water Systems",
    "water disinfection": "Water Systems",
    "water storage": "Water Systems",
    "well construction": "Water Systems",
    "rainwater": "Water Systems",
    "solar": "Power Systems",
    "wind turbine": "Power Systems",
    "battery": "Power Systems",
    "batteries": "Power Systems",
    "generator": "Power Systems",
    "photovoltaic": "Power Systems",
    "charge controller": "Power Systems",
    "inverter": "Power Systems",
    "biogas": "Off-Grid Systems",
    "biomass": "Off-Grid Systems",
    "wood gasification": "Off-Grid Systems",
    "rocket stove": "Off-Grid Systems",
    "mechanical system": "Off-Grid Systems",
    "power transmission": "Off-Grid Systems",
    "radio": "Communications",
    "ham radio": "Communications",
    "amateur radio": "Communications",
    "antenna": "Communications",
    "meshtastic": "Communications",
    "encryption": "Communications",
    "navigation": "Navigation",
    "celestial navigation": "Navigation",
    "land navigation": "Navigation",
    "map reading": "Navigation",
    "compass": "Navigation",
    "pottery": "Foundational Skills",
    "ceramics": "Foundational Skills",
    "blacksmithing": "Foundational Skills",
    "woodworking": "Foundational Skills",
    "leatherwork": "Foundational Skills",
    "textile": "Foundational Skills",
    "masonry": "Foundational Skills",
    "metalworking": "Foundational Skills",
    "historical technology": "Foundational Skills",
    "weapons": "Defense & Tactics",
    "firearms": "Defense & Tactics",
    "ballistics": "Defense & Tactics",
    "tactics": "Defense & Tactics",
    "perimeter": "Security",
    "surveillance": "Security",
    "supply chain": "Logistics",
    "logistics": "Logistics",
    "leadership": "Leadership",
    "governance": "Leadership",
    "community": "Community Coordination",
    "emergency preparedness": "Scenario Playbooks",
    "disaster": "Scenario Playbooks",
    "evacuation": "Scenario Playbooks",
}


def remap_domains(domains):
    """Remap a list of domain strings — variants to canonical, strip Reference."""
    result = set()
    for d in domains:
        if d == "Reference":
            continue
        if d in CANONICAL_DOMAINS:
            result.add(d)
        elif d in VARIANT_MAP:
            mapped = VARIANT_MAP[d]
            if mapped:  # None means discard
                result.add(mapped)
        # Unknown non-canonical domains: drop them
    return list(result)


def classify_by_subdomain(subdomains):
    """Try to infer canonical domain(s) from subdomain keyword matching."""
    found = set()
    for sd in subdomains:
        sd_lower = sd.lower().strip()
        for key, domain in SUBDOMAIN_MAP.items():
            if key in sd_lower:
                found.add(domain)
    return list(found) if found else None


def process_window_file(filepath, dry_run):
    """Process one window JSON file (array of concepts). Returns per-file stats."""
    stats = defaultdict(int)
    unknowns = []

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            concepts = json.load(f)
    except Exception as e:
        return {"parse_error": 1}, []

    if not isinstance(concepts, list):
        return {"skip_not_list": 1}, []

    modified = False

    for concept in concepts:
        if not isinstance(concept, dict):
            continue

        raw_domains = concept.get("domain", [])
        if isinstance(raw_domains, str):
            raw_domains = [raw_domains]

        subdomains = concept.get("subdomain", [])
        if isinstance(subdomains, str):
            subdomains = [subdomains]

        has_reference = "Reference" in raw_domains
        non_reference = [d for d in raw_domains if d != "Reference"]

        if not has_reference:
            # No Reference — just fix any variant names
            remapped = remap_domains(raw_domains)
            if set(remapped) != set(raw_domains):
                concept["domain"] = remapped
                modified = True
                stats["variant_remapped"] += 1
            else:
                stats["no_change"] += 1
            continue

        # Has Reference — what else does it have?
        remapped_others = remap_domains(non_reference)

        if remapped_others:
            # Reference + real domains: drop Reference, keep the rest
            concept["domain"] = remapped_others
            modified = True
            stats["reference_stripped"] += 1
            continue

        # Solo Reference (or Reference + only-noise): try subdomain lookup
        inferred = classify_by_subdomain(subdomains)
        if inferred:
            concept["domain"] = inferred
            concept["_reclassified_from_reference"] = True
            modified = True
            stats["subdomain_reclassified"] += 1
            continue

        # True unknown — needs API re-enrichment
        unknowns.append({
            "filepath": str(filepath),
            "title": concept.get("title", ""),
            "subdomain": subdomains,
            "content_preview": str(concept.get("content", concept.get("summary", "")))[:300],
        })
        stats["needs_enrichment"] += 1

    if modified and not dry_run:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(concepts, f, indent=2, ensure_ascii=False)

    return dict(stats), unknowns


def main():
    parser = argparse.ArgumentParser(description="Remap RECON concept domains")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()

    print(f"[REMAP] Scanning {CONCEPTS_DIR}")
    print(f"[REMAP] Dry run: {args.dry_run} | Workers: {args.workers}")

    window_files = [
        f for f in CONCEPTS_DIR.rglob("window_*.json")
    ]
    print(f"[REMAP] Found {len(window_files):,} window files")

    total_stats = defaultdict(int)
    all_unknowns = []
    lock = threading.Lock()
    done = 0

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(process_window_file, f, args.dry_run): f for f in window_files}
        for future in as_completed(futures):
            file_stats, unknowns = future.result()
            with lock:
                for k, v in file_stats.items():
                    total_stats[k] += v
                all_unknowns.extend(unknowns)
                done += 1
                if done % 5000 == 0:
                    print(f"  {done:,}/{len(window_files):,} files processed...")

    print("\n── Results ─────────────────────────────────────────────────")
    for status, count in sorted(total_stats.items(), key=lambda x: -x[1]):
        print(f"  {status:<35} {count:>10,}")

    total_concepts = sum(total_stats.values())
    print(f"\n  Total concepts processed:       {total_concepts:>10,}")
    print(f"  True unknowns for re-enrichment:{len(all_unknowns):>10,}")

    if not args.dry_run and all_unknowns:
        with open(UNKNOWNS_OUTPUT, "w", encoding="utf-8") as f:
            for item in all_unknowns:
                f.write(json.dumps(item) + "\n")
        print(f"\n  Unknowns written to: {UNKNOWNS_OUTPUT}")

    if args.dry_run:
        print("\n  [DRY RUN] No files were modified.")


if __name__ == "__main__":
    main()
