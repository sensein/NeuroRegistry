"""
converters/nwb.py — Fetch NWB schema and convert to LinkML
-----------------------------------------------------------
Source: https://github.com/NeurodataWithoutBorders/nwb-schema
        core/nwb.*.yaml files (NWB format spec)

NWB format spec uses a custom YAML format with neurodata_types.
We extract each neurodata_type as a SchemaClass with its datasets/attributes
as SchemaProperty slots.
"""

from __future__ import annotations
import httpx, yaml
from pathlib import Path

GITHUB_API = "https://api.github.com/repos/NeurodataWithoutBorders/nwb-schema/contents/core"
GITHUB_RAW = "https://raw.githubusercontent.com/NeurodataWithoutBorders/nwb-schema/dev/core"
OUT_PATH   = Path("schemas/nwb.yml")

NWB_CORE_FILES = [
    "nwb.base.yaml",
    "nwb.behavior.yaml",
    "nwb.device.yaml",
    "nwb.ecephys.yaml",
    "nwb.file.yaml",
    "nwb.image.yaml",
    "nwb.misc.yaml",
    "nwb.ophys.yaml",
    "nwb.ogen.yaml",
    "nwb.retinotopy.yaml",
]

NWB_TYPE_MAP = {
    "text":    "xsd:string",
    "ascii":   "xsd:string",
    "float":   "xsd:float",
    "float32": "xsd:float",
    "float64": "xsd:float",
    "int":     "xsd:integer",
    "int8":    "xsd:integer",
    "int16":   "xsd:integer",
    "int32":   "xsd:integer",
    "int64":   "xsd:integer",
    "uint8":   "xsd:integer",
    "uint16":  "xsd:integer",
    "uint32":  "xsd:integer",
    "uint64":  "xsd:integer",
    "bool":    "xsd:boolean",
    "scalar":  "xsd:float",
    "numeric": "xsd:float",
    "datetime":    "xsd:dateTime",
    "isodatetime": "xsd:dateTime",
}


def fetch_yaml(filename: str) -> list[dict]:
    url = f"{GITHUB_RAW}/{filename}"
    resp = httpx.get(url, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    # NWB YAML files use multiple documents
    return list(yaml.safe_load_all(resp.text))


def extract_attributes(neurodata_type: dict) -> list[tuple[str, dict]]:
    """Extract attribute/dataset definitions from an NWB neurodata_type."""
    attrs = []
    for section in ("attributes", "datasets", "links"):
        for item in (neurodata_type.get(section) or []):
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("target_type", "")
            if not name:
                continue
            raw_dtype = item.get("dtype", "text")
            if isinstance(raw_dtype, list):
                raw_dtype = raw_dtype[0].get("dtype", "text") if isinstance(raw_dtype[0], dict) else raw_dtype[0]
            xsd = NWB_TYPE_MAP.get(str(raw_dtype).lower(), "xsd:string")
            attrs.append((name, {
                "description": item.get("doc", ""),
                "slot_uri":    f"https://nwb-schema.readthedocs.io/en/latest/#{name}",
                "range":       xsd.replace("xsd:", ""),
                "multivalued": item.get("quantity", 1) in ("*", "+"),
                "required":    item.get("quantity", 1) == 1,
            }))
    return attrs


def convert() -> dict:
    print("[nwb] Fetching NWB core schema files…")
    classes: dict = {}
    slots:   dict = {}

    for filename in NWB_CORE_FILES:
        try:
            docs = fetch_yaml(filename)
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                for ndt in (doc.get("neurodata_types") or [doc]):
                    if not isinstance(ndt, dict):
                        continue
                    name = ndt.get("neurodata_type_def")
                    if not name:
                        continue
                    parent = ndt.get("neurodata_type_inc")
                    attrs = extract_attributes(ndt)
                    slot_names = []
                    for attr_name, attr_def in attrs:
                        key = f"{name}__{attr_name}"
                        slots[key] = attr_def
                        slot_names.append(key)
                    class_def: dict = {
                        "description": ndt.get("doc", ""),
                        "class_uri":   f"https://nwb-schema.readthedocs.io/en/latest/#{name}",
                        "slots":       slot_names,
                    }
                    if parent:
                        class_def["is_a"] = parent
                    classes[name] = class_def
            print(f"[nwb]   {filename} → {len(classes)} types so far")
        except Exception as e:
            print(f"[nwb]   WARNING: {filename} failed — {e}")

    return {
        "id":      "https://nwb-schema.readthedocs.io/en/latest/",
        "name":    "nwb",
        "title":   "NWB Schema",
        "description": "Neurodata Without Borders (NWB) format specification.",
        "license": "BSD-3-Clause",
        "version": "2.7.0",
        "prefixes": {
            "linkml": "https://w3id.org/linkml/",
            "nwb":    "https://nwb-schema.readthedocs.io/en/latest/",
            "xsd":    "http://www.w3.org/2001/XMLSchema#",
        },
        "default_prefix": "nwb",
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
    print(f"[nwb] Wrote {out_path} "
          f"({len(schema['classes'])} classes, {len(schema['slots'])} slots)")


if __name__ == "__main__":
    run()
