"""
converters/openminds.py — Fetch openMINDS schema and convert to LinkML
----------------------------------------------------------------------
Source: https://github.com/HBP-MINDS/openMINDS
        schemas/core/v4/  (JSON-LD / JSON Schema hybrid)

openMINDS uses a custom JSON-LD schema format with @type, @id, properties.
Each file under schemas/ defines one type.
"""

from __future__ import annotations
import httpx, yaml, json, re
from pathlib import Path

GITHUB_API = "https://api.github.com/repos/HBP-MINDS/openMINDS/contents/schemas/core/v4"
GITHUB_RAW = "https://raw.githubusercontent.com/HBP-MINDS/openMINDS/main/schemas/core/v4"
OUT_PATH   = Path("schemas/openminds.yml")

OPENMINDS_TYPE_MAP = {
    "string":  "xsd:string",
    "integer": "xsd:integer",
    "number":  "xsd:float",
    "boolean": "xsd:boolean",
    "date":    "xsd:date",
}


def fetch_dir_listing(url: str) -> list[dict]:
    resp = httpx.get(url, timeout=30, follow_redirects=True,
                     headers={"Accept": "application/vnd.github+json"})
    resp.raise_for_status()
    return resp.json()


def fetch_json(url: str) -> dict:
    resp = httpx.get(url, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def convert() -> dict:
    print("[openminds] Fetching schema listing…")
    all_classes: dict = {}
    all_slots:   dict = {}

    try:
        listing = fetch_dir_listing(GITHUB_API)
        subdirs = [item for item in listing if item.get("type") == "dir"]
        print(f"[openminds]   Found {len(subdirs)} subdirectories")

        for subdir in subdirs[:8]:  # cap to avoid rate limits
            subdir_name = subdir["name"]
            try:
                files = fetch_dir_listing(subdir["url"])
                schema_files = [f for f in files if f["name"].endswith(".schema.json")]
                for sf in schema_files[:10]:
                    try:
                        schema = fetch_json(sf["download_url"])
                        type_name = schema.get("_type", "").split("/")[-1] or sf["name"].replace(".schema.json","")
                        if not type_name:
                            continue
                        # Clean name
                        cls_name = re.sub(r'[^a-zA-Z0-9_]', '_', type_name)
                        required_props = set(schema.get("required", []))
                        slot_names = []
                        for prop_name, prop_def in (schema.get("properties") or {}).items():
                            if prop_name.startswith("@") or prop_name.startswith("_"):
                                continue
                            raw_type = prop_def.get("type", "string")
                            if isinstance(raw_type, list):
                                raw_type = next((t for t in raw_type if t != "null"), "string")
                            xsd = OPENMINDS_TYPE_MAP.get(raw_type, "xsd:string")
                            key = f"{cls_name}__{prop_name}"
                            all_slots[key] = {
                                "description": prop_def.get("description", ""),
                                "slot_uri": f"https://openminds.ebrains.eu/core/{prop_name}",
                                "range":    xsd.replace("xsd:", ""),
                                "multivalued": raw_type == "array",
                                "required": prop_name in required_props,
                            }
                            slot_names.append(key)
                        all_classes[cls_name] = {
                            "description": schema.get("description", f"openMINDS {type_name}"),
                            "class_uri":   f"https://openminds.ebrains.eu/core/{type_name}",
                            "slots":       slot_names,
                        }
                    except Exception as e:
                        print(f"[openminds]   WARNING: {sf['name']} — {e}")
            except Exception as e:
                print(f"[openminds]   WARNING: subdir {subdir_name} — {e}")

    except Exception as e:
        print(f"[openminds]   WARNING: listing failed — {e}")

    # Fallback core types
    if not all_classes:
        print("[openminds]   Using fallback core types")
        all_classes = {
            "Subject": {
                "description": "A subject studied in a neuroscience experiment.",
                "class_uri": "https://openminds.ebrains.eu/core/Subject",
                "slots": ["subject__lookupLabel","subject__biologicalSex",
                          "subject__species","subject__strain"],
            },
            "Dataset": {
                "description": "A neuroscience dataset.",
                "class_uri": "https://openminds.ebrains.eu/core/Dataset",
                "slots": ["dataset__title","dataset__description",
                          "dataset__license","dataset__digitalIdentifier"],
            },
            "BrainAtlas": {
                "description": "A brain atlas.",
                "class_uri": "https://openminds.ebrains.eu/sands/BrainAtlas",
                "slots": ["brainatlas__shortName","brainatlas__fullName",
                          "brainatlas__ontologyIdentifier"],
            },
        }
        all_slots = {
            "subject__lookupLabel": {"description":"Label for lookup","range":"string"},
            "subject__biologicalSex": {"description":"Biological sex of subject","range":"string"},
            "subject__species": {"description":"Species of subject","range":"string"},
            "subject__strain": {"description":"Strain of subject","range":"string"},
            "dataset__title": {"description":"Dataset title","range":"string","slot_uri":"schema:name"},
            "dataset__description": {"description":"Dataset description","range":"string","slot_uri":"schema:description"},
            "dataset__license": {"description":"License","range":"string","slot_uri":"schema:license"},
            "dataset__digitalIdentifier": {"description":"DOI or other digital identifier","range":"string","slot_uri":"schema:identifier"},
            "brainatlas__shortName": {"description":"Short name","range":"string"},
            "brainatlas__fullName": {"description":"Full name","range":"string"},
            "brainatlas__ontologyIdentifier": {"description":"Ontology term URI","range":"uriorcurie"},
        }

    return {
        "id":      "https://openminds.ebrains.eu/",
        "name":    "openminds",
        "title":   "openMINDS Schema",
        "description": "Open Metadata Initiative for Neuroscience Data Structures (openMINDS).",
        "license": "MIT",
        "version": "4.0.0",
        "prefixes": {
            "linkml":     "https://w3id.org/linkml/",
            "schema":     "https://schema.org/",
            "openminds":  "https://openminds.ebrains.eu/",
            "xsd":        "http://www.w3.org/2001/XMLSchema#",
        },
        "default_prefix": "openminds",
        "default_range":  "string",
        "imports": ["linkml:types"],
        "classes": all_classes,
        "slots":   all_slots,
    }


def run(out_path: Path = OUT_PATH) -> None:
    schema = convert()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        yaml.dump(schema, f, default_flow_style=False, allow_unicode=True)
    print(f"[openminds] Wrote {out_path} "
          f"({len(schema['classes'])} classes, {len(schema['slots'])} slots)")


if __name__ == "__main__":
    run()
