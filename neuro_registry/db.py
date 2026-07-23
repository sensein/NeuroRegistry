"""
db.py — Shared DB setup for the SenseIn Schema Registry
--------------------------------------------------------
Single source of truth for:
  - LadybugDB connection
  - DDL (all node + relationship tables)
  - Base identity helpers (make_base, make_uid, make_uri, now_iso)

Import this in seed.py, ingest_linkml.py, align.py, export_json.py
so every script gets the same tables without duplicating DDL.
"""

from __future__ import annotations
import datetime, uuid
from pathlib import Path

import ladybug as lb

# ---------------------------------------------------------------------------
# Registry namespace
# ---------------------------------------------------------------------------

REG = "https://registry.sensein.io/"

# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()

def make_uid() -> str:
    return str(uuid.uuid4())

def make_uri(object_id: str, version: str = "1.0.0") -> str:
    return f"{REG}obj/{object_id}/v/{version}"

def make_iri(object_id: str) -> str:
    return f"{REG}obj/{object_id}"

def make_base(object_id: str, version: str = "1.0.0",
              iri: str | None = None) -> dict:
    """Return the shared BaseNode identity fields as a dict."""
    return {
        "uid":        make_uid(),
        "iri":        iri or make_iri(object_id),
        "uri":        make_uri(object_id, version),
        "version":    version,
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
# DDL
# ---------------------------------------------------------------------------

DDL = [
    # ---- Node tables -------------------------------------------------------
    # All share base identity: uid PK, iri, uri, version, created_at
    # registry_version ties every node to the registry snapshot it was created in

    """CREATE NODE TABLE IF NOT EXISTS SchemaClass (
        uid              STRING PRIMARY KEY,
        iri              STRING,
        uri              STRING,
        version          STRING,
        created_at       STRING,
        name             STRING,
        definition       STRING,
        abstract         BOOLEAN,
        source_label     STRING,
        registry_version STRING
    )""",

    """CREATE NODE TABLE IF NOT EXISTS SchemaProperty (
        uid              STRING PRIMARY KEY,
        iri              STRING,
        uri              STRING,
        version          STRING,
        created_at       STRING,
        name             STRING,
        definition       STRING,
        datatype         STRING,
        range_uri        STRING,
        multivalued      BOOLEAN,
        required         BOOLEAN,
        source_label     STRING,
        registry_version STRING
    )""",

    """CREATE NODE TABLE IF NOT EXISTS SchemaRule (
        uid              STRING PRIMARY KEY,
        iri              STRING,
        uri              STRING,
        version          STRING,
        created_at       STRING,
        name             STRING,
        rule_spec        STRING,
        units            STRING,
        min_val          STRING,
        max_val          STRING,
        pattern          STRING,
        multivalued      BOOLEAN,
        required         BOOLEAN,
        registry_version STRING
    )""",

    """CREATE NODE TABLE IF NOT EXISTS SchemaTransform (
        uid              STRING PRIMARY KEY,
        iri              STRING,
        uri              STRING,
        version          STRING,
        created_at       STRING,
        name             STRING,
        spec             STRING,
        registry_version STRING
    )""",

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

    # ---- Relationship tables -----------------------------------------------
    "CREATE REL TABLE IF NOT EXISTS PRIOR_VERSION    (FROM SchemaClass TO SchemaClass)",
    "CREATE REL TABLE IF NOT EXISTS PRIOR_VERSION_P  (FROM SchemaProperty TO SchemaProperty)",
    "CREATE REL TABLE IF NOT EXISTS PRIOR_VERSION_R  (FROM SchemaRule TO SchemaRule)",
    "CREATE REL TABLE IF NOT EXISTS HAS_PROPERTY     (FROM SchemaClass TO SchemaProperty)",
    "CREATE REL TABLE IF NOT EXISTS APPLIES_TO       (FROM SchemaRule TO SchemaClass)",
    "CREATE REL TABLE IF NOT EXISTS SUBCLASS_OF      (FROM SchemaClass TO SchemaClass)",
    "CREATE REL TABLE IF NOT EXISTS MIXIN            (FROM SchemaClass TO SchemaClass)",
    "CREATE REL TABLE IF NOT EXISTS SKOS_BROADER     (FROM SchemaClass TO SchemaClass)",
    "CREATE REL TABLE IF NOT EXISTS SKOS_RELATED     (FROM SchemaClass TO SchemaClass)",
    "CREATE REL TABLE IF NOT EXISTS PROV_GENERATED   (FROM SchemaClass TO SchemaActivity)",
    "CREATE REL TABLE IF NOT EXISTS PROV_GENERATED_P (FROM SchemaProperty TO SchemaActivity)",
    "CREATE REL TABLE IF NOT EXISTS PROV_GENERATED_R (FROM SchemaRule TO SchemaActivity)",
    "CREATE REL TABLE IF NOT EXISTS FROM_SOURCE      (FROM SchemaClass TO SchemaSource)",
    "CREATE REL TABLE IF NOT EXISTS FROM_SOURCE_P    (FROM SchemaProperty TO SchemaSource)",

    # Alignment — distance + per-signal subscores for weight slider in UI
    """CREATE REL TABLE IF NOT EXISTS ALIGNED_TO (
        FROM SchemaClass TO SchemaClass,
        distance      DOUBLE,
        method        STRING,
        score_iri     DOUBLE,
        score_name    DOUBLE,
        score_desc    DOUBLE,
        score_slot    DOUBLE,
        registry_version STRING
    )""",
]

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
