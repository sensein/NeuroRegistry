"""
db.py — Shared DB setup for the SenseIn Schema Registry
--------------------------------------------------------
Single source of truth for:
  - LadybugDB connection
  - DDL:
      Registry entity node tables → generated from schemas/meta_model.yaml.
        Edit that file and rebuild the DB to change node structure.
      Infrastructure node tables (SchemaSource, SchemaVersionSnapshot,
        SchemaActivity, SemanticIdentity) → defined here; rarely change.
      Relationship tables → defined here.
  - Identity helpers (make_hash_id, make_base, make_iri, now_iso)

Import this in seed.py, ingest_linkml.py, align.py, export_json.py
so every script gets the same tables without duplicating DDL.
"""

from __future__ import annotations
import datetime
import hashlib as _hashlib
import json as _json
import uuid
from pathlib import Path

import ladybug as lb
import yaml as _yaml

# ---------------------------------------------------------------------------
# Registry namespace
# ---------------------------------------------------------------------------

REG = "https://registry.sensein.io/"

# ---------------------------------------------------------------------------
# Schema YAML — edit this file to change registry entity node structure
# ---------------------------------------------------------------------------

SCHEMA_YAML = Path(__file__).parent.parent / "schemas" / "meta_model.yaml"

# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()

def make_uid() -> str:
    """Generate a random UUID string. Kept for backward compatibility."""
    return str(uuid.uuid4())

def make_hash_id(content: dict) -> str:
    """Return sha256:<hex> for a canonicalised content dict."""
    canonical = _json.dumps(content, sort_keys=True, separators=(",", ":"))
    digest = _hashlib.sha256(canonical.encode()).hexdigest()
    return f"sha256:{digest}"

def make_iri(object_id: str) -> str:
    return f"{REG}obj/{object_id}"

def make_uri(object_id: str, version: str = "1.0.0") -> str:
    """Kept for backward compatibility. Prefer make_iri for new code."""
    return f"{REG}obj/{object_id}/v/{version}"

def make_base(object_id: str, version: str = "1.0.0",
              iri: str | None = None,
              created_by: str = "system") -> dict:
    """Return the shared identity fields for a new registry entity.

    hash_id is derived from the IRI so the same concept from any source
    produces the same key. created_by and created_at are the only provenance
    fields set here; callers add modified_by / modified_at / derived_from
    as needed.
    """
    iri_ = iri or make_iri(object_id)
    return {
        "hash_id":    make_hash_id({"iri": iri_}),
        "iri":        iri_,
        "created_by": created_by,
        "created_at": now_iso(),
    }

def bump_version(ver: str, bump: str = "patch") -> str:
    """
    Bump a semver string.
      bump="patch"  1.0.0 → 1.0.1
      bump="minor"  1.0.0 → 1.1.0
      bump="major"  1.0.0 → 2.0.0
    """
    major, minor, patch = (int(x) for x in ver.split("."))
    if bump == "major":
        return f"{major+1}.0.0"
    elif bump == "minor":
        return f"{major}.{minor+1}.0"
    else:
        return f"{major}.{minor}.{patch+1}"

# ---------------------------------------------------------------------------
# Content-addressed identity helpers (used by ingest layer)
# ---------------------------------------------------------------------------

def compute_content_id(iri: str = "", datatype: str = "",
                       range_uri: str = "", units: str = "",
                       pattern: str = "", multivalued: bool = False,
                       required: bool = False) -> str:
    """SHA-256 of semantic graph fields. Kept for backward compatibility."""
    payload = _json.dumps({
        "iri":         iri or "",
        "datatype":    datatype or "",
        "range_uri":   range_uri or "",
        "units":       units or "",
        "pattern":     pattern or "",
        "multivalued": bool(multivalued),
        "required":    bool(required),
    }, sort_keys=True)
    return _hashlib.sha256(payload.encode()).hexdigest()


def compute_class_content_id(iri: str = "", abstract: bool = False) -> str:
    """Simpler hash for classes. Kept for backward compatibility."""
    payload = _json.dumps({"iri": iri or "", "abstract": bool(abstract)},
                           sort_keys=True)
    return _hashlib.sha256(payload.encode()).hexdigest()


def skos_relation(distance: float, is_subclass: bool = False) -> str:
    """
    Map a numeric distance to a SKOS mapping relation.
      0.0        → skos:exactMatch
      ≤ 0.1      → skos:closeMatch
      ≤ 0.4      → skos:broadMatch / skos:narrowMatch
      ≤ 0.7      → skos:relatedMatch
      > 0.7      → (no relation — don't write the edge)
    """
    if distance == 0.0:
        return "skos:exactMatch"
    if distance <= 0.1:
        return "skos:closeMatch"
    if distance <= 0.4:
        return "skos:narrowMatch" if is_subclass else "skos:broadMatch"
    if distance <= 0.7:
        return "skos:relatedMatch"
    return ""


# ---------------------------------------------------------------------------
# YAML → DDL generator
# ---------------------------------------------------------------------------

_LINKML_TYPE_MAP: dict[str, str] = {
    "string":     "STRING",
    "str":        "STRING",
    "datetime":   "STRING",
    "boolean":    "BOOLEAN",
    "bool":       "BOOLEAN",
    "uriorcurie": "STRING",
    "uri":        "STRING",
    "integer":    "INT64",
    "int":        "INT64",
    "double":     "DOUBLE",
    "float":      "FLOAT",
}


def _resolve_slots(cls_name: str, classes: dict, all_slots: dict) -> dict:
    """Collect effective slots for a class including inherited ones (own wins)."""
    cls_def = classes.get(cls_name, {})
    parent = cls_def.get("is_a")
    parent_slots = _resolve_slots(parent, classes, all_slots) if parent else {}
    own_slots = {s: all_slots.get(s, {}) for s in cls_def.get("slots", [])}
    return {**parent_slots, **own_slots}


def _build_registry_ddl(yaml_path: str | Path = SCHEMA_YAML) -> list[str]:
    """
    Read the meta-model YAML and return CREATE NODE TABLE statements for all
    non-abstract, non-inline classes.

    Column rules per slot:
    - db_inline class ref (e.g. ProvenanceInfo) → flatten its slots inline.
    - Multivalued class ref → REL table (handled in _REL_DDL below; skipped here).
    - Non-multivalued class ref → STRING column (hash_id FK).
    - Scalar with db_json or multivalued → STRING (stored as JSON array).
    - Plain scalar → mapped type; identifier slots get PRIMARY KEY.
    """
    schema = _yaml.safe_load(Path(yaml_path).read_text())
    classes: dict = schema.get("classes", {})
    all_slots: dict = schema.get("slots", {})

    inline_classes = {
        name
        for name, cls_def in classes.items()
        if cls_def.get("annotations", {}).get("db_inline")
    }

    stmts: list[str] = []

    for cls_name, cls_def in classes.items():
        if cls_def.get("abstract") or cls_name in inline_classes:
            continue

        slots = _resolve_slots(cls_name, classes, all_slots)
        columns: list[str] = []

        for slot_name, slot_def in slots.items():
            range_    = slot_def.get("range", "string")
            multi     = slot_def.get("multivalued", False)
            is_id     = slot_def.get("identifier", False)
            db_json   = slot_def.get("annotations", {}).get("db_json", False)

            if range_ in inline_classes:
                if not multi:
                    for sub_name, sub_def in _resolve_slots(
                        range_, classes, all_slots
                    ).items():
                        sub_range = sub_def.get("range", "string")
                        sub_multi = sub_def.get("multivalued", False)
                        sub_json  = sub_def.get("annotations", {}).get("db_json", False)
                        if sub_range not in classes:
                            if sub_json or sub_multi:
                                columns.append(f"    {sub_name:<24} STRING")
                            else:
                                db_type = _LINKML_TYPE_MAP.get(sub_range, "STRING")
                                columns.append(f"    {sub_name:<24} {db_type}")
                # multivalued inline → not supported; skip

            elif range_ in classes:
                if not multi:
                    # Non-multivalued class ref → STRING FK (hash_id)
                    columns.append(f"    {slot_name:<24} STRING")
                # multivalued → REL table, not a column

            else:
                # Scalar
                if db_json or multi:
                    columns.append(f"    {slot_name:<24} STRING")
                else:
                    db_type = _LINKML_TYPE_MAP.get(range_, "STRING")
                    if is_id:
                        columns.append(
                            f"    {slot_name:<24} {db_type} PRIMARY KEY"
                        )
                    else:
                        columns.append(f"    {slot_name:<24} {db_type}")

        if columns:
            col_str = ",\n".join(columns)
            stmts.append(
                f"CREATE NODE TABLE IF NOT EXISTS {cls_name} (\n{col_str}\n)"
            )

    return stmts


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

# Registry entity node tables — generated from schemas/meta_model.yaml.
# To add/remove columns: edit the YAML and rebuild the database.
_REGISTRY_NODE_DDL: list[str] = _build_registry_ddl()

# Infrastructure node tables — not part of the meta-model; defined here.
_INFRASTRUCTURE_NODE_DDL: list[str] = [
    # SchemaSource — origin schemas ingested into the registry
    """CREATE NODE TABLE IF NOT EXISTS SchemaSource (
        uid              STRING PRIMARY KEY,
        iri              STRING,
        uri              STRING,
        version          STRING,
        created_at       STRING,
        label            STRING,
        mime_type        STRING,
        registry_version STRING
    )""",

    # SchemaVersionSnapshot — one per (schema_name, semver) pair
    """CREATE NODE TABLE IF NOT EXISTS SchemaVersionSnapshot (
        uid              STRING PRIMARY KEY,
        iri              STRING,
        uri              STRING,
        version          STRING,
        created_at       STRING,
        schema_label     STRING,
        yml_path         STRING,
        class_count      INT64,
        property_count   INT64,
        rule_count       INT64,
        changes_summary  STRING,
        registry_version STRING
    )""",

    # SchemaActivity — PROV-O activity log (defined but not yet written by any script)
    """CREATE NODE TABLE IF NOT EXISTS SchemaActivity (
        uid              STRING PRIMARY KEY,
        iri              STRING,
        uri              STRING,
        version          STRING,
        created_at       STRING,
        activity         STRING,
        agent            STRING,
        started_at       STRING,
        issue_number     STRING,
        registry_version STRING
    )""",

    # SemanticIdentity — canonical node per unique content hash for cross-source dedup
    """CREATE NODE TABLE IF NOT EXISTS SemanticIdentity (
        uid           STRING PRIMARY KEY,
        content_id    STRING,
        canonical_uri STRING,
        datatype      STRING,
        units         STRING,
        iri           STRING,
        created_at    STRING
    )""",
]

# Relationship tables — multivalued meta-model edges + alignment infrastructure.
_REL_DDL: list[str] = [
    # --- Meta-model multivalued edges ---
    "CREATE REL TABLE IF NOT EXISTS HAS_PROPERTY       (FROM RegistryClass    TO RegistryProperty)",
    "CREATE REL TABLE IF NOT EXISTS HAS_RELATION       (FROM RegistryClass    TO Relation)",
    "CREATE REL TABLE IF NOT EXISTS HAS_SKOS_MAPPING   (FROM RegistryClass    TO SkosMapping)",
    "CREATE REL TABLE IF NOT EXISTS HAS_SKOS_MAPPING_P (FROM RegistryProperty TO SkosMapping)",
    "CREATE REL TABLE IF NOT EXISTS MIXIN              (FROM RegistryClass    TO RegistryClass)",
    "CREATE REL TABLE IF NOT EXISTS SUBCLASS_OF        (FROM RegistryClass    TO RegistryClass)",

    # --- Version chains (carry diff data between consecutive versions) ---
    """CREATE REL TABLE IF NOT EXISTS PRIOR_VERSION (
        FROM RegistryClass TO RegistryClass,
        diff_summary        STRING,
        changed_fields      STRING,
        added_properties    STRING,
        removed_properties  STRING,
        definition_from     STRING,
        definition_to       STRING,
        registry_version    STRING,
        created_at          STRING
    )""",
    """CREATE REL TABLE IF NOT EXISTS PRIOR_VERSION_P (
        FROM RegistryProperty TO RegistryProperty,
        diff_summary        STRING,
        changed_fields      STRING,
        definition_from     STRING,
        definition_to       STRING,
        datatype_from       STRING,
        datatype_to         STRING,
        registry_version    STRING,
        created_at          STRING
    )""",
    """CREATE REL TABLE IF NOT EXISTS PRIOR_VERSION_R (
        FROM Rule TO Rule,
        diff_summary        STRING,
        changed_fields      STRING,
        registry_version    STRING,
        created_at          STRING
    )""",

    # --- Infrastructure edges ---
    "CREATE REL TABLE IF NOT EXISTS APPLIES_TO         (FROM Rule             TO RegistryClass)",
    "CREATE REL TABLE IF NOT EXISTS PROV_GENERATED     (FROM RegistryClass    TO SchemaActivity)",
    "CREATE REL TABLE IF NOT EXISTS PROV_GENERATED_P   (FROM RegistryProperty TO SchemaActivity)",
    "CREATE REL TABLE IF NOT EXISTS PROV_GENERATED_R   (FROM Rule             TO SchemaActivity)",
    "CREATE REL TABLE IF NOT EXISTS FROM_SOURCE        (FROM RegistryClass    TO SchemaSource)",
    "CREATE REL TABLE IF NOT EXISTS FROM_SOURCE_P      (FROM RegistryProperty TO SchemaSource)",
    "CREATE REL TABLE IF NOT EXISTS HAS_IDENTITY       (FROM RegistryClass    TO SemanticIdentity)",
    "CREATE REL TABLE IF NOT EXISTS HAS_IDENTITY_P     (FROM RegistryProperty TO SemanticIdentity)",

    # --- Alignment ---
    """CREATE REL TABLE IF NOT EXISTS ALIGNED_TO (
        FROM RegistryClass TO RegistryClass,
        distance         DOUBLE,
        method           STRING,
        skos_relation    STRING,
        score_iri        DOUBLE,
        score_name       DOUBLE,
        score_desc       DOUBLE,
        score_slot       DOUBLE,
        registry_version STRING
    )""",
]

DDL = _REGISTRY_NODE_DDL + _INFRASTRUCTURE_NODE_DDL + _REL_DDL


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------

def _migrate_aligned_to(conn: lb.Connection) -> None:
    """Drop and recreate ALIGNED_TO if it lacks current columns."""
    try:
        conn.execute("""
            MATCH (a:RegistryClass), (b:RegistryClass)
            WHERE a.hash_id <> b.hash_id
            WITH a, b LIMIT 1
            CREATE (a)-[:ALIGNED_TO {
                distance: 0.0, method: '__probe__',
                skos_relation: '',
                score_iri: 0.0, score_name: 0.0,
                score_desc: 0.0, score_slot: 0.0,
                registry_version: ''
            }]->(b)
        """)
        conn.execute(
            "MATCH ()-[r:ALIGNED_TO {method: '__probe__'}]->() DELETE r"
        )
    except Exception:
        try:
            conn.execute("DROP TABLE ALIGNED_TO")
        except Exception:
            pass
        conn.execute("""
            CREATE REL TABLE ALIGNED_TO (
                FROM RegistryClass TO RegistryClass,
                distance         DOUBLE,
                method           STRING,
                skos_relation    STRING,
                score_iri        DOUBLE,
                score_name       DOUBLE,
                score_desc       DOUBLE,
                score_slot       DOUBLE,
                registry_version STRING
            )
        """)


def _migrate_prior_version(conn: lb.Connection) -> None:
    """Drop and recreate PRIOR_VERSION tables if they lack diff fields."""
    try:
        conn.execute("""
            MATCH (a:RegistryClass), (b:RegistryClass)
            WHERE a.hash_id <> b.hash_id
            WITH a, b LIMIT 1
            CREATE (a)-[:PRIOR_VERSION {
                diff_summary: '__probe__', changed_fields: '',
                added_properties: '', removed_properties: '',
                definition_from: '', definition_to: '',
                registry_version: '', created_at: ''
            }]->(b)
        """)
        conn.execute(
            "MATCH ()-[r:PRIOR_VERSION {diff_summary: '__probe__'}]->() DELETE r"
        )
    except Exception:
        try:
            conn.execute("DROP TABLE PRIOR_VERSION")
        except Exception:
            pass
        conn.execute("""
            CREATE REL TABLE PRIOR_VERSION (
                FROM RegistryClass TO RegistryClass,
                diff_summary        STRING,
                changed_fields      STRING,
                added_properties    STRING,
                removed_properties  STRING,
                definition_from     STRING,
                definition_to       STRING,
                registry_version    STRING,
                created_at          STRING
            )
        """)

    try:
        conn.execute("""
            MATCH (a:RegistryProperty), (b:RegistryProperty)
            WHERE a.hash_id <> b.hash_id
            WITH a, b LIMIT 1
            CREATE (a)-[:PRIOR_VERSION_P {
                diff_summary: '__probe__', changed_fields: '',
                definition_from: '', definition_to: '',
                datatype_from: '', datatype_to: '',
                registry_version: '', created_at: ''
            }]->(b)
        """)
        conn.execute(
            "MATCH ()-[r:PRIOR_VERSION_P {diff_summary: '__probe__'}]->() DELETE r"
        )
    except Exception:
        try:
            conn.execute("DROP TABLE PRIOR_VERSION_P")
        except Exception:
            pass
        conn.execute("""
            CREATE REL TABLE PRIOR_VERSION_P (
                FROM RegistryProperty TO RegistryProperty,
                diff_summary     STRING,
                changed_fields   STRING,
                definition_from  STRING,
                definition_to    STRING,
                datatype_from    STRING,
                datatype_to      STRING,
                registry_version STRING,
                created_at       STRING
            )
        """)


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------

def get_connection(db_path: str = "./registry.lbug") -> lb.Connection:
    """Open (or create) a LadybugDB database and ensure all tables exist."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db   = lb.Database(db_path)
    conn = lb.Connection(db)
    for stmt in DDL:
        conn.execute(stmt)
    _migrate_aligned_to(conn)
    _migrate_prior_version(conn)
    return conn


# ---------------------------------------------------------------------------
# Registry version helpers
# ---------------------------------------------------------------------------

PROVENANCE_PATH = "data/provenance.json"

def current_registry_version(provenance_path: str = PROVENANCE_PATH) -> str:
    """Read current registry version from provenance.json. Default 0.0.0."""
    import json
    p = Path(provenance_path)
    if not p.exists():
        return "0.0.0"
    entries = json.loads(p.read_text())
    if not entries:
        return "0.0.0"
    return entries[-1]["registry_version"]

def next_registry_version(current: str, bump: str = "minor") -> str:
    return bump_version(current, bump)

def append_provenance(entry: dict,
                      provenance_path: str = PROVENANCE_PATH) -> None:
    """Append a provenance entry to data/provenance.json."""
    import json
    p = Path(provenance_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    entries = json.loads(p.read_text()) if p.exists() else []
    entries.append(entry)
    p.write_text(json.dumps(entries, indent=2))
