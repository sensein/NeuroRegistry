"""
converters/aind.py — Fetch AIND data schema and convert to LinkML
-----------------------------------------------------------------
Source: https://github.com/AllenNeuralDynamics/aind-data-schema
        src/aind_data_schema/core/  (Pydantic models published as JSON Schema)
        Also exposes JSON Schema at:
        https://raw.githubusercontent.com/AllenNeuralDynamics/aind-data-schema/
          main/src/aind_data_schema/schemas/

AIND publishes pre-generated JSON Schema files — one per model.
"""

from __future__ import annotations
import httpx, yaml, json, re
from pathlib import Path

GITHUB_RAW     = "https://raw.githubusercontent.com/AllenNeuralDynamics/aind-data-schema/main"
SCHEMA_DIR_API = "https://api.github.com/repos/AllenNeuralDynamics/aind-data-schema/contents/src/aind_data_schema/schemas"
OUT_PATH       = Path("schemas/aind.yml")

JSON_TYPE_MAP = {
    "string":  "xsd:string",
    "number":  "xsd:float",
    "integer": "xsd:integer",
    "boolean": "xsd:boolean",
    "array":   "xsd:string",
    "object":  "xsd:string",
    "null":    "xsd:string",
}


def fetch_json(url: str) -> dict:
    resp = httpx.get(url, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def clean_name(s: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_]', '_', s.replace(" ", "_"))


def extract_from_json_schema(schema_name: str, schema: dict) -> tuple[dict, dict]:
    """Recursively extract classes and slots from a JSON Schema."""
    classes: dict = {}
    slots:   dict = {}

    def process_obj(name: str, obj: dict, parent: str | None = None) -> None:
        if not isinstance(obj, dict):
            return
        props = obj.get("properties", {})
        if not props and obj.get("type") != "object":
            return
        req = set(obj.get("required") or [])
        slot_names = []
        for prop_name, prop_body in props.items():
            if not isinstance(prop_body, dict):
                continue
            raw_type = prop_body.get("type", "string")
            if isinstance(raw_type, list):
                raw_type = next((t for t in raw_type if t != "null"), "string")
            xsd = JSON_TYPE_MAP.get(raw_type, "xsd:string")
            units = ""
            if "unit" in prop_name.lower() or "units" in prop_name.lower():
                units = prop_body.get("default", "")
            key = f"{clean_name(name)}__{prop_name}"
            slots[key] = {
                "description": prop_body.get("description", prop_body.get("title", "")),
                "slot_uri": f"https://aind-data-schema.readthedocs.io/en/stable/#{prop_name}",
                "range":    xsd.replace("xsd:", ""),
                "multivalued": raw_type == "array",
                "required": prop_name in req,
            }
            if units:
                slots[key]["description"] += f" (units: {units})"
            slot_names.append(key)

            # Recurse into nested objects
            if raw_type == "object" or "properties" in prop_body:
                sub_name = clean_name(f"{name}_{prop_name}")
                process_obj(sub_name, prop_body, name)

        cls_def: dict = {
            "description": obj.get("description", obj.get("title", f"AIND {name}")),
            "class_uri":   f"https://aind-data-schema.readthedocs.io/en/stable/#{name}",
            "slots":       slot_names,
        }
        if parent:
            cls_def["is_a"] = parent
        classes[clean_name(name)] = cls_def

        # Process $defs
        for def_name, def_body in (obj.get("$defs") or obj.get("definitions") or {}).items():
            process_obj(def_name, def_body, None)

    process_obj(schema_name, schema)
    return classes, slots


def convert() -> dict:
    print("[aind] Fetching AIND schema…")
    all_classes: dict = {}
    all_slots:   dict = {}

    try:
        listing = fetch_json(SCHEMA_DIR_API)
        json_files = [f for f in listing if f["name"].endswith(".json")]
        print(f"[aind]   Found {len(json_files)} schema files")

        for sf in json_files[:15]:
            try:
                schema = fetch_json(sf["download_url"])
                name = clean_name(sf["name"].replace(".json","").replace("_schema",""))
                cls, slt = extract_from_json_schema(name, schema)
                all_classes.update(cls)
                all_slots.update(slt)
                print(f"[aind]   {sf['name']}: +{len(cls)} classes")
            except Exception as e:
                print(f"[aind]   WARNING: {sf['name']} — {e}")

    except Exception as e:
        print(f"[aind]   WARNING: listing failed — {e}")

    # Fallback
    if not all_classes:
        print("[aind]   Using fallback core types")
        all_classes = {
            "Subject": {
                "description": "AIND subject metadata.",
                "class_uri": "https://aind-data-schema.readthedocs.io/en/stable/#Subject",
                "slots": ["subject__subject_id","subject__sex","subject__date_of_birth",
                          "subject__species","subject__genotype","subject__background_strain"],
            },
            "Session": {
                "description": "AIND acquisition session.",
                "class_uri": "https://aind-data-schema.readthedocs.io/en/stable/#Session",
                "slots": ["session__session_start_time","session__session_end_time",
                          "session__experimenter_full_name","session__rig_id"],
            },
            "Instrument": {
                "description": "AIND instrument / rig.",
                "class_uri": "https://aind-data-schema.readthedocs.io/en/stable/#Instrument",
                "slots": ["instrument__instrument_id","instrument__instrument_type",
                          "instrument__manufacturer"],
            },
        }
        all_slots = {
            "subject__subject_id": {"description":"Subject identifier","range":"string","slot_uri":"schema:identifier"},
            "subject__sex": {"description":"Biological sex","range":"string"},
            "subject__date_of_birth": {"description":"Date of birth","range":"date"},
            "subject__species": {"description":"Species","range":"string"},
            "subject__genotype": {"description":"Genotype","range":"string"},
            "subject__background_strain": {"description":"Background strain","range":"string"},
            "session__session_start_time": {"description":"Session start","range":"datetime"},
            "session__session_end_time": {"description":"Session end","range":"datetime"},
            "session__experimenter_full_name": {"description":"Experimenter name","range":"string","multivalued":True},
            "session__rig_id": {"description":"Rig identifier","range":"string"},
            "instrument__instrument_id": {"description":"Instrument identifier","range":"string"},
            "instrument__instrument_type": {"description":"Type of instrument","range":"string"},
            "instrument__manufacturer": {"description":"Manufacturer","range":"string"},
        }

    return {
        "id":      "https://aind-data-schema.readthedocs.io/en/stable/",
        "name":    "aind",
        "title":   "AIND Data Schema",
        "description": "Allen Institute for Neural Dynamics data schema.",
        "license": "MIT",
        "version": "1.0.0",
        "prefixes": {
            "linkml": "https://w3id.org/linkml/",
            "schema": "https://schema.org/",
            "aind":   "https://aind-data-schema.readthedocs.io/en/stable/",
            "xsd":    "http://www.w3.org/2001/XMLSchema#",
        },
        "default_prefix": "aind",
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
    print(f"[aind] Wrote {out_path} "
          f"({len(schema['classes'])} classes, {len(schema['slots'])} slots)")


if __name__ == "__main__":
    run()
