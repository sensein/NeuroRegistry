"""
converters/bids.py — Fetch BIDS schema and convert to LinkML
-------------------------------------------------------------
Source: https://github.com/bids-standard/bids-specification
        src/schema/objects/  (YAML files per object type)

BIDS schema is split into multiple YAML files. We ingest every object
group so that no schema element is dropped:
  objects/metadata.yaml   → sidecar metadata fields (→ slots on BIDSMetadata)
  objects/columns.yaml    → data dictionary columns (→ slots on BIDSColumn)
  objects/entities.yaml   → entities like sub, ses, task (→ slots on BIDSEntity)
  objects/datatypes.yaml  → imaging datatypes (→ one SchemaClass each)
  objects/suffixes.yaml   → file suffixes (→ one SchemaClass each)

Every extracted slot is attached to a class so the downstream ingester
(ingest_linkml.py, which only ingests slots referenced by a class) picks
all of them up — earlier versions capped a single class at 60 slots and
dropped hundreds of fields on the floor.
"""

from __future__ import annotations
import re
import httpx, yaml
from pathlib import Path

GITHUB_RAW = "https://raw.githubusercontent.com/bids-standard/bids-specification/master/src/schema/objects"
OUT_PATH   = Path("schemas/bids.yml")

# Object files whose entries become slots (properties), keyed by the class
# that will own them and a human description of that class.
SLOT_FILES = {
    "metadata": ("BIDSMetadata", "BIDS sidecar JSON metadata fields."),
    "columns":  ("BIDSColumn",   "BIDS tabular (TSV) data-dictionary columns."),
    "entities": ("BIDSEntity",   "BIDS filename entities (sub, ses, task, …)."),
}

# Object files whose entries are controlled-vocabulary terms; each entry
# becomes its own SchemaClass.
CLASS_FILES = {
    "datatypes": "BIDS imaging datatype.",
    "suffixes":  "BIDS file suffix.",
}

XSD_TYPE_MAP = {
    "number":  "xsd:float",
    "integer": "xsd:integer",
    "string":  "xsd:string",
    "boolean": "xsd:boolean",
    "array":   "xsd:string",
    "object":  "xsd:string",
}

GLOSSARY = "https://bids-specification.readthedocs.io/en/stable/glossary.html"


def fetch(name: str) -> dict:
    resp = httpx.get(f"{GITHUB_RAW}/{name}.yaml", timeout=30, follow_redirects=True)
    resp.raise_for_status()
    return yaml.safe_load(resp.text) or {}


def clean(s: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_]', '_', str(s)).strip('_')


def camel(s: str) -> str:
    return "".join(p.capitalize() for p in re.split(r'[^a-zA-Z0-9]+', str(s)) if p)


def bids_range(defn: dict) -> tuple[str, bool]:
    """
    Resolve a BIDS object 'type' to a (linkml_range, multivalued) pair.

    Handles plain types, list-of-types, and anyOf/oneOf unions (falling back
    to the first non-null option). Defaults to string.
    """
    t = defn.get("type")
    if t is None:
        for key in ("anyOf", "oneOf"):
            for opt in defn.get(key) or []:
                if isinstance(opt, dict) and opt.get("type") and opt["type"] != "null":
                    t = opt["type"]
                    break
            if t:
                break
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    multivalued = (t == "array")
    xsd = XSD_TYPE_MAP.get(str(t), "xsd:string")
    return xsd.replace("xsd:", ""), multivalued


def convert() -> dict:
    print("[bids] Fetching schema…")
    classes: dict = {}
    slots:   dict = {}

    # ------------------------------------------------------------------
    # Property groups: metadata, columns, entities → slots
    # ------------------------------------------------------------------
    for group, (cls_name, cls_desc) in SLOT_FILES.items():
        try:
            objs = fetch(group)
        except Exception as e:
            print(f"[bids]   WARNING: {group} fetch failed — {e}")
            continue

        slot_names = []
        for name, defn in (objs or {}).items():
            if not isinstance(defn, dict):
                continue
            rng, multivalued = bids_range(defn)
            key = f"{group}__{name}"          # namespaced to avoid cross-group clashes
            slots[key] = {
                "description": str(defn.get("description", "") or "").strip(),
                # anchor on the real object name so equivalent fields across
                # groups resolve to the same IRI (and dedupe at ingest time)
                "slot_uri":    f"{GLOSSARY}#{name}",
                "range":       rng,
                "multivalued": multivalued,
            }
            slot_names.append(key)

        classes[cls_name] = {
            "description": cls_desc,
            "class_uri":   "https://bids-specification.readthedocs.io/en/stable/",
            "slots":       slot_names,
        }
        print(f"[bids]   {group}: {len(slot_names)} slots → {cls_name}")

    # ------------------------------------------------------------------
    # Vocabulary groups: datatypes, suffixes → one class per term
    # ------------------------------------------------------------------
    for group, desc in CLASS_FILES.items():
        try:
            objs = fetch(group)
        except Exception as e:
            print(f"[bids]   WARNING: {group} fetch failed — {e}")
            continue

        count = 0
        for name, defn in (objs or {}).items():
            if not isinstance(defn, dict):
                continue
            cls_name = camel(name) or clean(name)
            if not cls_name or cls_name in classes:
                continue
            value = defn.get("value", name)
            classes[cls_name] = {
                "description": str(defn.get("description", "") or "").strip() or desc,
                "class_uri":   f"{GLOSSARY}#{value}",
                "slots":       [],
            }
            count += 1
        print(f"[bids]   {group}: {count} classes")

    print(f"[bids]   total {len(classes)} classes, {len(slots)} slots")

    return {
        "id":      "https://bids-specification.readthedocs.io/en/stable/",
        "name":    "bids",
        "title":   "BIDS Schema",
        "description": "Brain Imaging Data Structure (BIDS) specification schema.",
        "license": "CC0-1.0",
        "version": "1.9.0",
        "prefixes": {
            "linkml": "https://w3id.org/linkml/",
            "schema": "https://schema.org/",
            "bids":   "https://bids-specification.readthedocs.io/en/stable/",
            "xsd":    "http://www.w3.org/2001/XMLSchema#",
        },
        "default_prefix": "bids",
        "default_range":  "string",
        "imports": ["linkml:types"],
        "classes": classes,
        "slots":   slots,
    }


def run(out_path: Path = OUT_PATH) -> None:
    schema = convert()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        yaml.dump(schema, f, default_flow_style=False, allow_unicode=True)
    print(f"[bids] Wrote {out_path} "
          f"({len(schema['classes'])} classes, {len(schema['slots'])} slots)")


if __name__ == "__main__":
    run()
