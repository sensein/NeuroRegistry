"""
converters/aind.py — Fetch AIND schemas from sensein/undata and convert to LinkML
----------------------------------------------------------------------------------
Rather than fighting with the AIND GitHub API (which reorganizes frequently),
we pull from sensein/undata which already has AIND schemas extracted and
content-addressed at:
  https://github.com/sensein/undata/tree/main/backend/seed/schemas

These are AIND-format YAML files with content-addressed names like:
  subject_groupstate_c3111258d67b.yaml
  bloodrecording_f41aac8cd524.yaml
  epocheddata_de2698b4844e.yaml
  etc.

Each file describes one AIND schema type with its fields, types, and
constraints. We convert each to a LinkML class + slots.

Falls back to PyPI aind-data-schema if the undata repo is unavailable,
then falls back to hardcoded core types if PyPI also fails.
"""

from __future__ import annotations
import httpx, yaml, json, re
from pathlib import Path

UNDATA_API = "https://api.github.com/repos/sensein/undata/contents/backend/seed/schemas"
UNDATA_RAW = "https://raw.githubusercontent.com/sensein/undata/main/backend/seed/schemas"
OUT_PATH   = Path("schemas/aind.yml")

JSON_TYPE_MAP = {
    "string":   "xsd:string",
    "number":   "xsd:float",
    "integer":  "xsd:integer",
    "boolean":  "xsd:boolean",
    "array":    "xsd:string",
    "object":   "xsd:string",
    "null":     "xsd:string",
}


def fetch_json(url: str) -> dict | list:
    resp = httpx.get(url, timeout=30, follow_redirects=True,
                     headers={"Accept": "application/vnd.github+json"})
    resp.raise_for_status()
    return resp.json()


def fetch_yaml(url: str) -> dict:
    resp = httpx.get(url, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    return yaml.safe_load(resp.text) or {}


def clean(s: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_]', '_', s).strip('_')


def class_name_from_filename(filename: str) -> str:
    """
    Convert content-addressed filename to a clean class name.
    e.g. "subject_groupstate_c3111258d67b.yaml" → "SubjectGroupState"
         "bloodrecording_f41aac8cd524.yaml" → "BloodRecording"
    """
    # Strip extension and trailing hash (last token after final underscore
    # that looks like a hex hash)
    stem = filename.replace(".yaml", "").replace(".yml", "")
    parts = stem.split("_")
    # Remove trailing hash token (short hex string)
    if parts and re.match(r'^[0-9a-f]{8,}$', parts[-1].lower()):
        parts = parts[:-1]
    # CamelCase
    return "".join(p.capitalize() for p in parts if p)


def extract_from_aind_yaml(cls_name: str, data: dict) -> tuple[dict, dict]:
    """
    Extract a class and its slots from an AIND YAML schema file.

    AIND YAML schema files use a JSON-Schema-like format with:
      properties: {field_name: {type, description, ...}}
      required: [field_name, ...]
    Or they may be flat dicts with field definitions.
    """
    classes: dict = {}
    slots:   dict = {}

    # Handle JSON Schema style
    if "properties" in data:
        required = set(data.get("required") or [])
        slot_names = []
        for prop, pbody in data["properties"].items():
            if not isinstance(pbody, dict):
                continue
            raw_type = pbody.get("type", "string")
            if isinstance(raw_type, list):
                raw_type = next((t for t in raw_type if t != "null"), "string")
            xsd = JSON_TYPE_MAP.get(str(raw_type).lower(), "xsd:string")
            # Extract units from description or title
            desc = pbody.get("description", pbody.get("title", ""))
            units = ""
            m = re.search(r'\(units?:\s*([^)]+)\)', desc, re.IGNORECASE)
            if m:
                units = m.group(1).strip()
            key = f"{cls_name}__{prop}"
            slots[key] = {
                "description": desc,
                "slot_uri":    f"https://aind-data-schema.readthedocs.io/en/stable/#{prop}",
                "range":       xsd.replace("xsd:", ""),
                "multivalued": raw_type == "array",
                "required":    prop in required,
            }
            if units:
                slots[key]["description"] = f"{desc} (units: {units})"
            slot_names.append(key)
        if slot_names:
            classes[cls_name] = {
                "description": data.get("description", data.get("title", f"AIND {cls_name}")),
                "class_uri":   f"https://aind-data-schema.readthedocs.io/en/stable/#{cls_name}",
                "slots":       slot_names,
            }
        # Recurse $defs
        for d_name, d_body in (data.get("$defs") or data.get("definitions") or {}).items():
            sub_name = clean(f"{cls_name}_{d_name}")
            sub_cls, sub_slt = extract_from_aind_yaml(sub_name, d_body)
            classes.update(sub_cls)
            slots.update(sub_slt)

    # Handle flat AIND format: top-level keys are field names with type info
    elif any(isinstance(v, dict) and "type" in v for v in data.values()):
        slot_names = []
        for field, fdef in data.items():
            if not isinstance(fdef, dict) or field.startswith("_"):
                continue
            raw_type = fdef.get("type", "string")
            xsd = JSON_TYPE_MAP.get(str(raw_type).lower(), "xsd:string")
            key = f"{cls_name}__{field}"
            slots[key] = {
                "description": fdef.get("description", ""),
                "slot_uri":    f"https://aind-data-schema.readthedocs.io/en/stable/#{field}",
                "range":       xsd.replace("xsd:", ""),
                "multivalued": False,
                "required":    fdef.get("required", False),
            }
            slot_names.append(key)
        if slot_names:
            classes[cls_name] = {
                "description": f"AIND {cls_name}",
                "class_uri":   f"https://aind-data-schema.readthedocs.io/en/stable/#{cls_name}",
                "slots":       slot_names,
            }

    return classes, slots


def convert_from_undata() -> tuple[dict, dict]:
    """Pull AIND schemas from sensein/undata backend/seed/schemas/."""
    print("[aind] Fetching from sensein/undata…")
    all_classes: dict = {}
    all_slots:   dict = {}

    listing = fetch_json(UNDATA_API)
    if not isinstance(listing, list):
        raise ValueError("Unexpected API response")

    yaml_files = [f for f in listing
                  if isinstance(f, dict) and
                  f.get("name", "").endswith((".yaml", ".yml"))]

    print(f"[aind]   Found {len(yaml_files)} schema files")

    for f in yaml_files:
        filename  = f["name"]
        cls_name  = class_name_from_filename(filename)
        raw_url   = f.get("download_url") or f"{UNDATA_RAW}/{filename}"
        try:
            data = fetch_yaml(raw_url)
            cls, slt = extract_from_aind_yaml(cls_name, data)
            all_classes.update(cls)
            all_slots.update(slt)
        except Exception as e:
            print(f"[aind]   WARNING: {filename} — {e}")

    print(f"[aind]   Loaded {len(all_classes)} classes from undata")
    return all_classes, all_slots


def convert_from_pypi() -> tuple[dict, dict]:
    """Fall back to generating schemas from the aind-data-schema PyPI package."""
    import subprocess, sys
    print("[aind] Trying pip install aind-data-schema…")
    subprocess.run(
        [sys.executable, "-m", "pip", "install",
         "aind-data-schema", "--break-system-packages", "-q"],
        capture_output=True
    )
    from aind_data_schema.core.subject import Subject
    from aind_data_schema.core.session import Session
    from aind_data_schema.core.instrument import Instrument
    all_classes: dict = {}
    all_slots:   dict = {}
    for model_cls in [Subject, Session, Instrument]:
        name = model_cls.__name__
        schema = model_cls.model_json_schema()
        cls, slt = extract_from_aind_yaml(name, schema)
        all_classes.update(cls)
        all_slots.update(slt)
    print(f"[aind]   Loaded {len(all_classes)} classes from PyPI")
    return all_classes, all_slots


def hardcoded_fallback() -> tuple[dict, dict]:
    classes = {
        "Subject": {
            "description": "AIND subject metadata.",
            "class_uri": "https://aind-data-schema.readthedocs.io/en/stable/#Subject",
            "slots": ["subject__subject_id","subject__sex","subject__date_of_birth",
                      "subject__species","subject__genotype"],
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
            "slots": ["instrument__instrument_id","instrument__instrument_type"],
        },
    }
    slots = {
        "subject__subject_id":       {"description":"Subject ID","range":"string","slot_uri":"schema:identifier"},
        "subject__sex":              {"description":"Biological sex","range":"string"},
        "subject__date_of_birth":    {"description":"Date of birth","range":"date"},
        "subject__species":          {"description":"Species","range":"string"},
        "subject__genotype":         {"description":"Genotype","range":"string"},
        "session__session_start_time":     {"description":"Session start","range":"datetime"},
        "session__session_end_time":       {"description":"Session end","range":"datetime"},
        "session__experimenter_full_name": {"description":"Experimenter name","range":"string","multivalued":True},
        "session__rig_id":                 {"description":"Rig ID","range":"string"},
        "instrument__instrument_id":       {"description":"Instrument ID","range":"string"},
        "instrument__instrument_type":     {"description":"Instrument type","range":"string"},
    }
    return classes, slots


def convert() -> dict:
    all_classes, all_slots = {}, {}

    # Try undata first (fastest, most complete)
    try:
        all_classes, all_slots = convert_from_undata()
    except Exception as e:
        print(f"[aind]   undata fetch failed — {e}")

    # Fall back to PyPI
    if not all_classes:
        try:
            all_classes, all_slots = convert_from_pypi()
        except Exception as e:
            print(f"[aind]   PyPI fallback failed — {e}")

    # Last resort: hardcoded
    if not all_classes:
        print("[aind]   Using hardcoded fallback")
        all_classes, all_slots = hardcoded_fallback()

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
