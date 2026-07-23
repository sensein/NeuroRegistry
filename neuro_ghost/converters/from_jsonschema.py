"""
converters/from_jsonschema.py — Convert JSON Schema to LinkML YAML
------------------------------------------------------------------
Used by the schema-submission workflow when a contributor submits their
schema as JSON Schema (```json or ```json-schema fence) instead of LinkML.

Supports:
  - Top-level object with "properties"
  - "$defs" / "definitions" sections (one class per object definition)
  - JSON Schema primitive types → LinkML ranges
  - required[] → required: true on slots
  - array type → multivalued: true

Usage (CLI):
    python -m neuro_ghost.converters.from_jsonschema input.json output.yml
    python -m neuro_ghost.converters.from_jsonschema input.json  # prints to stdout

Usage (library):
    from neuro_ghost.converters.from_jsonschema import convert
    linkml_dict = convert(json_schema_dict, name="my-schema")
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

import yaml


_JSON_TYPE_MAP: dict[str, str] = {
    "string":  "string",
    "integer": "integer",
    "number":  "float",
    "boolean": "boolean",
    "array":   "string",   # multivalued scalar; complex arrays stay as string
    "object":  "string",   # nested objects stored as JSON string FK
    "null":    "string",
}


def _json_type(jtype) -> tuple[str, bool]:
    """Return (linkml_range, multivalued) for a JSON Schema type field."""
    if isinstance(jtype, list):
        # e.g. ["string", "null"] — take first non-null
        jtype = next((t for t in jtype if t != "null"), "string")
    multivalued = jtype == "array"
    return _JSON_TYPE_MAP.get(jtype, "string"), multivalued


def _process_object(obj_name: str, obj_def: dict,
                    classes: dict, slots: dict) -> None:
    """Extract one class + its slots from a JSON Schema object definition."""
    props    = obj_def.get("properties") or {}
    required = set(obj_def.get("required") or [])
    cls_slots: list[str] = []

    for prop_name, prop_def in props.items():
        if not isinstance(prop_def, dict):
            continue

        raw_type = prop_def.get("type", "string")
        # Handle $ref — treat as a class reference (string FK for now)
        if "$ref" in prop_def:
            ref = prop_def["$ref"].rsplit("/", 1)[-1]
            linkml_range = ref
            multivalued  = False
        else:
            linkml_range, multivalued = _json_type(raw_type)

        # items type for arrays
        if raw_type == "array":
            items = prop_def.get("items", {})
            if isinstance(items, dict):
                inner_type = items.get("type", "string")
                linkml_range, _ = _json_type(inner_type)
            multivalued = True

        slot: dict = {
            "range":       linkml_range,
            "multivalued": multivalued,
            "required":    prop_name in required,
        }
        desc = prop_def.get("description") or prop_def.get("title") or ""
        if desc:
            slot["description"] = desc

        # Only write defaults when non-default to keep output clean
        if not multivalued:
            del slot["multivalued"]
        if not slot["required"]:
            del slot["required"]

        slots[prop_name] = slot
        cls_slots.append(prop_name)

    cls: dict = {"slots": cls_slots}
    desc = obj_def.get("description") or obj_def.get("title") or ""
    if desc:
        cls["description"] = desc
    classes[obj_name] = cls


def convert(data: dict, name: str) -> dict:
    """
    Convert a parsed JSON Schema dict to a LinkML schema dict.

    Parameters
    ----------
    data : dict
        Parsed JSON Schema (from json.loads or yaml.safe_load).
    name : str
        Schema name, used as the LinkML `name` field and in the ID IRI.

    Returns
    -------
    dict
        A LinkML-compatible dict ready to be YAML-serialised.

    Raises
    ------
    ValueError
        If no object definitions are found in the schema.
    """
    classes: dict = {}
    slots:   dict = {}

    # "$defs" (draft-2019-09+) and "definitions" (draft-07)
    for section in ("$defs", "definitions"):
        for def_name, def_obj in (data.get(section) or {}).items():
            if isinstance(def_obj, dict) and (
                def_obj.get("type") == "object" or "properties" in def_obj
            ):
                _process_object(def_name, def_obj, classes, slots)

    # Top-level object
    if data.get("type") == "object" or "properties" in data:
        cls_name = data.get("title") or name.replace("-", "_").title()
        _process_object(cls_name, data, classes, slots)

    if not classes:
        raise ValueError(
            "No object definitions found in JSON Schema. "
            "The schema must have at least one object type with 'properties'."
        )

    return {
        "id":          f"https://registry.sensein.io/schema/{name}",
        "name":        name,
        "description": data.get("description") or data.get("title") or "",
        "version":     "1.0.0",
        "prefixes": {
            "linkml": "https://w3id.org/linkml/",
            "xsd":    "http://www.w3.org/2001/XMLSchema#",
        },
        "imports": ["linkml:types"],
        "default_range": "string",
        "classes": classes,
        "slots":   slots,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m neuro_ghost.converters.from_jsonschema "
              "<input.json> [output.yml]")
        sys.exit(1)

    in_path  = Path(sys.argv[1])
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    raw = in_path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = yaml.safe_load(raw)

    name = in_path.stem
    result = convert(data, name)
    output = yaml.dump(result, default_flow_style=False, allow_unicode=True,
                       sort_keys=False)

    if out_path:
        out_path.write_text(output, encoding="utf-8")
        print(f"Wrote {out_path}  "
              f"({len(result['classes'])} classes, {len(result['slots'])} slots)")
    else:
        print(output)


if __name__ == "__main__":
    main()
