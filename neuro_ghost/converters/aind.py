"""
converters/aind.py — Convert AIND schemas to LinkML
----------------------------------------------------------------------------------
Primary source is the official `aind-data-schema` PyPI package. Every model in
`aind_data_schema.core` is a Pydantic model; `model_json_schema()` yields a full
JSON Schema with a `properties` block plus a `$defs` section describing every
nested model. We turn the root model and each `$def` into a LinkML class so no
field is left behind.

Why PyPI and not the GitHub API: the AIND GitHub layout reorganizes frequently.

Fallbacks, in order:
  1. sensein/undata content-addressed schemas (backend/seed). Each schema file
     lists property hashes that resolve against backend/seed/elements. This seed
     is a *partial* snapshot — unresolved hashes are skipped — so it is only a
     fallback when PyPI is unavailable.
  2. A hardcoded core (Subject / Session / Instrument) as a last resort.
"""

from __future__ import annotations
import httpx, yaml, re
from pathlib import Path

UNDATA_SCHEMAS_API  = "https://api.github.com/repos/sensein/undata/contents/backend/seed/schemas"
UNDATA_ELEMENTS_API = "https://api.github.com/repos/sensein/undata/contents/backend/seed/elements"
OUT_PATH   = Path("schemas/aind.yml")
DOCS       = "https://aind-data-schema.readthedocs.io/en/stable/"

JSON_TYPE_MAP = {
    "string":   "xsd:string",
    "number":   "xsd:float",
    "integer":  "xsd:integer",
    "boolean":  "xsd:boolean",
    "array":    "xsd:string",
    "object":   "xsd:string",
    "null":     "xsd:string",
}

# JSON Schema 'format' hints that map to richer LinkML primitives.
FORMAT_MAP = {
    "date":      "date",
    "date-time": "datetime",
    "time":      "datetime",
    "uri":       "uri",
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
    return re.sub(r'[^a-zA-Z0-9_]', '_', str(s)).strip('_')


def _units(desc: str) -> str:
    m = re.search(r'\(units?:\s*([^)]+)\)', desc, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def json_range(pbody: dict) -> tuple[str, bool]:
    """
    Resolve a JSON-Schema property body to a (linkml_range, multivalued) pair.

    Handles: plain type, list-of-types, anyOf/oneOf unions, array items, and
    JSON Schema 'format' hints (date/date-time). Enums and $ref/allOf
    references degrade to string (their structure is captured as separate
    classes via $defs), which matches how the sibling converters model ranges.
    """
    t = pbody.get("type")
    fmt = pbody.get("format")

    # Union types: pick the first concrete (non-null) branch.
    if t is None:
        for key in ("anyOf", "oneOf"):
            for opt in pbody.get(key) or []:
                if isinstance(opt, dict) and opt.get("type") and opt["type"] != "null":
                    t = opt["type"]
                    fmt = fmt or opt.get("format")
                    if t == "array":
                        pbody = opt  # so items lookup below sees the array body
                    break
            if t:
                break

    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")

    if t == "array":
        items = pbody.get("items") or {}
        item_t = items.get("type") if isinstance(items, dict) else None
        item_fmt = items.get("format") if isinstance(items, dict) else None
        rng = (FORMAT_MAP.get(str(item_fmt))
               or JSON_TYPE_MAP.get(str(item_t).lower(), "xsd:string").replace("xsd:", ""))
        return rng, True

    rng = FORMAT_MAP.get(str(fmt)) or JSON_TYPE_MAP.get(str(t).lower(), "xsd:string").replace("xsd:", "")
    return rng, False


def add_class_from_json(cls_name: str, body: dict,
                        classes: dict, slots: dict) -> None:
    """Add one LinkML class (+ its slots) from a JSON-Schema object body."""
    props = body.get("properties")
    if not isinstance(props, dict):
        return
    required = set(body.get("required") or [])
    slot_names = []
    for prop, pbody in props.items():
        if not isinstance(pbody, dict):
            continue
        rng, multivalued = json_range(pbody)
        desc = str(pbody.get("description", pbody.get("title", "")) or "")
        units = _units(desc)
        key = f"{cls_name}__{prop}"
        slots[key] = {
            "description": f"{desc} (units: {units})" if units else desc,
            "slot_uri":    f"{DOCS}#{prop}",
            "range":       rng,
            "multivalued": multivalued,
            "required":    prop in required,
        }
        slot_names.append(key)
    classes[cls_name] = {
        "description": str(body.get("description", body.get("title", f"AIND {cls_name}")) or f"AIND {cls_name}"),
        "class_uri":   f"{DOCS}#{cls_name}",
        "slots":       slot_names,
    }


def extract_json_schema(root_name: str, schema: dict) -> tuple[dict, dict]:
    """
    Convert a Pydantic/JSON-Schema document into LinkML classes + slots.

    The root model becomes one class; every entry in `$defs`/`definitions`
    (nested models, enums-with-properties) becomes its own class keyed by its
    definition name so shared defs dedupe naturally across models.
    """
    classes: dict = {}
    slots:   dict = {}
    add_class_from_json(root_name, schema, classes, slots)
    for d_name, d_body in (schema.get("$defs") or schema.get("definitions") or {}).items():
        if isinstance(d_body, dict) and "properties" in d_body:
            add_class_from_json(clean(d_name), d_body, classes, slots)
    return classes, slots


def convert_from_pypi() -> tuple[dict, dict]:
    """
    Generate schemas from every model in the aind-data-schema core package.

    Enumerating models dynamically keeps us robust to the package reorganizing
    its module layout (an earlier hardcoded `import … session` broke exactly
    this way).
    """
    import subprocess, sys, importlib, pkgutil, inspect

    def _load_core():
        import aind_data_schema.core as core
        return core

    try:
        core = _load_core()
    except Exception:
        print("[aind] Installing aind-data-schema…")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "aind-data-schema", "-q"],
            capture_output=True,
        )
        importlib.invalidate_caches()
        core = _load_core()

    from pydantic import BaseModel

    all_classes: dict = {}
    all_slots:   dict = {}
    seen: set = set()

    for mod_info in pkgutil.iter_modules(core.__path__):
        mod = importlib.import_module(f"aind_data_schema.core.{mod_info.name}")
        for name, obj in inspect.getmembers(mod, inspect.isclass):
            if not (issubclass(obj, BaseModel) and obj.__module__ == mod.__name__):
                continue
            if name in seen:
                continue
            seen.add(name)
            try:
                schema = obj.model_json_schema()
            except Exception as e:
                print(f"[aind]   WARNING: {name} — {e}")
                continue
            cls, slt = extract_json_schema(name, schema)
            all_classes.update(cls)
            all_slots.update(slt)

    print(f"[aind]   Loaded {len(all_classes)} classes, "
          f"{len(all_slots)} slots from PyPI")
    return all_classes, all_slots


def convert_from_undata() -> tuple[dict, dict]:
    """
    Pull AIND schemas from sensein/undata (content-addressed).

    Each schemas/*.yaml file lists `semantic.properties` as sha256 hashes that
    reference elements/*.yaml (one property definition each). We build a map of
    {sha256 → element} and resolve. The seed is partial, so hashes with no
    corresponding element are skipped.
    """
    print("[aind] Fetching from sensein/undata…")

    def _list_yaml(api: str) -> list[dict]:
        out, page = [], 1
        while True:
            batch = fetch_json(f"{api}?per_page=100&page={page}")
            if not isinstance(batch, list) or not batch:
                break
            out += [f for f in batch
                    if isinstance(f, dict) and f.get("name", "").endswith((".yaml", ".yml"))]
            if len(batch) < 100:
                break
            page += 1
        return out

    # Build element lookup by sha256.
    element_map: dict = {}
    for f in _list_yaml(UNDATA_ELEMENTS_API):
        try:
            data = fetch_yaml(f.get("download_url"))
            if isinstance(data, dict) and data.get("sha256"):
                element_map[data["sha256"]] = data
        except Exception:
            continue
    print(f"[aind]   Indexed {len(element_map)} elements")

    all_classes: dict = {}
    all_slots:   dict = {}
    resolved = missing = 0

    for f in _list_yaml(UNDATA_SCHEMAS_API):
        try:
            data = fetch_yaml(f.get("download_url"))
        except Exception as e:
            print(f"[aind]   WARNING: {f.get('name')} — {e}")
            continue
        prov = (data.get("provenance") or [{}])[0]
        cls_name = clean(prov.get("class") or prov.get("name") or f["name"].split("_")[0]) \
            .replace(" ", "")
        cls_name = "".join(p for p in re.split(r'[_\s]+', cls_name)) or "AindClass"
        prop_hashes = ((data.get("semantic") or {}).get("properties")) or []

        slot_names = []
        for phash in prop_hashes:
            el = element_map.get(phash)
            if not el:
                missing += 1
                continue
            resolved += 1
            eprov = (el.get("provenance") or [{}])[0]
            field = clean(eprov.get("name") or phash[:12])
            sem = el.get("semantic") or {}
            dtype = str(sem.get("data_type", "string")).lower()
            key = f"{cls_name}__{field}"
            all_slots[key] = {
                "description": str(eprov.get("description", "") or ""),
                "slot_uri":    f"{DOCS}#{field}",
                "range":       JSON_TYPE_MAP.get(dtype, "xsd:string").replace("xsd:", ""),
                "multivalued": dtype == "array",
            }
            slot_names.append(key)

        if slot_names:
            all_classes[cls_name] = {
                "description": str(prov.get("description") or f"AIND {cls_name}"),
                "class_uri":   f"{DOCS}#{cls_name}",
                "slots":       slot_names,
            }

    print(f"[aind]   Loaded {len(all_classes)} classes from undata "
          f"({resolved} properties resolved, {missing} unresolved/skipped)")
    return all_classes, all_slots


def hardcoded_fallback() -> tuple[dict, dict]:
    classes = {
        "Subject": {
            "description": "AIND subject metadata.",
            "class_uri": f"{DOCS}#Subject",
            "slots": ["subject__subject_id", "subject__sex", "subject__date_of_birth",
                      "subject__species", "subject__genotype"],
        },
        "Session": {
            "description": "AIND acquisition session.",
            "class_uri": f"{DOCS}#Session",
            "slots": ["session__session_start_time", "session__session_end_time",
                      "session__experimenter_full_name", "session__rig_id"],
        },
        "Instrument": {
            "description": "AIND instrument / rig.",
            "class_uri": f"{DOCS}#Instrument",
            "slots": ["instrument__instrument_id", "instrument__instrument_type"],
        },
    }
    slots = {
        "subject__subject_id":       {"description": "Subject ID", "range": "string", "slot_uri": "schema:identifier"},
        "subject__sex":              {"description": "Biological sex", "range": "string"},
        "subject__date_of_birth":    {"description": "Date of birth", "range": "date"},
        "subject__species":          {"description": "Species", "range": "string"},
        "subject__genotype":         {"description": "Genotype", "range": "string"},
        "session__session_start_time":     {"description": "Session start", "range": "datetime"},
        "session__session_end_time":       {"description": "Session end", "range": "datetime"},
        "session__experimenter_full_name": {"description": "Experimenter name", "range": "string", "multivalued": True},
        "session__rig_id":                 {"description": "Rig ID", "range": "string"},
        "instrument__instrument_id":       {"description": "Instrument ID", "range": "string"},
        "instrument__instrument_type":     {"description": "Instrument type", "range": "string"},
    }
    return classes, slots


def convert() -> dict:
    all_classes, all_slots = {}, {}

    # PyPI is the authoritative, complete source.
    try:
        all_classes, all_slots = convert_from_pypi()
    except Exception as e:
        print(f"[aind]   PyPI conversion failed — {e}")

    # Fall back to the (partial) undata seed.
    if not all_classes:
        try:
            all_classes, all_slots = convert_from_undata()
        except Exception as e:
            print(f"[aind]   undata fetch failed — {e}")

    # Last resort: hardcoded core.
    if not all_classes:
        print("[aind]   Using hardcoded fallback")
        all_classes, all_slots = hardcoded_fallback()

    return {
        "id":      DOCS,
        "name":    "aind",
        "title":   "AIND Data Schema",
        "description": "Allen Institute for Neural Dynamics data schema.",
        "license": "MIT",
        "version": "1.0.0",
        "prefixes": {
            "linkml": "https://w3id.org/linkml/",
            "schema": "https://schema.org/",
            "aind":   DOCS,
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
