"""
SenseIn Schema Registry — LadybugDB + FastAPI
---------------------------------------------

Node hierarchy (conceptual base → specialised):

  BaseNode  (uid, iri, uri, version, created_at)
    ├── SchemaClass      (name, definition)
    ├── SchemaProperty   (name, definition, datatype, range_uri)
    ├── SchemaRule       (name, rule_spec, units, min_val, max_val,
    │                     pattern, multivalued, required)
    ├── SchemaTransform  (name, spec)
    ├── SchemaSource     (label, mime_type)
    └── SchemaActivity   (activity, agent, started_at)

LadybugDB does not support table inheritance, so the shared BaseNode
fields are repeated on each table. A helper (make_base) keeps this DRY.

Relationships:
  PRIOR_VERSION   SchemaClass      → SchemaClass
  PRIOR_VERSION_P SchemaProperty   → SchemaProperty
  HAS_PROPERTY    SchemaClass      → SchemaProperty
  APPLIES_TO      SchemaRule       → SchemaClass
  SUBCLASS_OF     SchemaClass      → SchemaClass
  MIXIN           SchemaClass      → SchemaClass
  SKOS_BROADER    SchemaClass      → SchemaClass
  SKOS_RELATED    SchemaClass      → SchemaClass
  PROV_GENERATED  SchemaClass      → SchemaActivity
  PROV_GENERATED_P SchemaProperty  → SchemaActivity
  PROV_GENERATED_R SchemaRule      → SchemaActivity
  FROM_SOURCE     SchemaClass      → SchemaSource

Install:  pip install -r requirements.txt
Run:      uvicorn schema_registry:app --reload
Docs:     http://localhost:8000/docs
"""

from __future__ import annotations
import uuid, datetime
from typing import Optional

import ladybug as lb
from fastapi import FastAPI, HTTPException, Body
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

db   = lb.Database("./registry.lbug")
conn = lb.Connection(db)

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

# Shared base fields on every table:
#   uid STRING PRIMARY KEY, iri STRING, uri STRING,
#   version STRING, created_at STRING

DDL = [
    """CREATE NODE TABLE IF NOT EXISTS SchemaClass (
        uid        STRING PRIMARY KEY,
        iri        STRING,
        uri        STRING,
        version    STRING,
        created_at STRING,
        name       STRING,
        definition STRING
    )""",

    """CREATE NODE TABLE IF NOT EXISTS SchemaProperty (
        uid        STRING PRIMARY KEY,
        iri        STRING,
        uri        STRING,
        version    STRING,
        created_at STRING,
        name       STRING,
        definition STRING,
        datatype   STRING,
        range_uri  STRING
    )""",

    # Validation constraints live here — they ARE rules, not property metadata
    """CREATE NODE TABLE IF NOT EXISTS SchemaRule (
        uid         STRING PRIMARY KEY,
        iri         STRING,
        uri         STRING,
        version     STRING,
        created_at  STRING,
        name        STRING,
        rule_spec   STRING,
        units       STRING,
        min_val     STRING,
        max_val     STRING,
        pattern     STRING,
        multivalued BOOLEAN,
        required    BOOLEAN
    )""",

    """CREATE NODE TABLE IF NOT EXISTS SchemaTransform (
        uid        STRING PRIMARY KEY,
        iri        STRING,
        uri        STRING,
        version    STRING,
        created_at STRING,
        name       STRING,
        spec       STRING
    )""",

    """CREATE NODE TABLE IF NOT EXISTS SchemaSource (
        uid        STRING PRIMARY KEY,
        iri        STRING,
        uri        STRING,
        version    STRING,
        created_at STRING,
        label      STRING,
        mime_type  STRING
    )""",

    """CREATE NODE TABLE IF NOT EXISTS SchemaActivity (
        uid        STRING PRIMARY KEY,
        iri        STRING,
        uri        STRING,
        version    STRING,
        created_at STRING,
        activity   STRING,
        agent      STRING,
        started_at STRING
    )""",

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
]

for stmt in DDL:
    conn.execute(stmt)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REG = "https://registry.sensein.io/"

def now_iso() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"

def make_uid() -> str:
    return str(uuid.uuid4())

def make_uri(object_id: str, version: str) -> str:
    return f"{REG}obj/{object_id}/v/{version}"

def make_iri(object_id: str) -> str:
    return f"{REG}obj/{object_id}"

def bump_version(ver: str) -> str:
    parts = ver.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)

def make_base(object_id: str, version: str, iri: str | None = None) -> dict:
    """Return the shared BaseNode fields as a dict."""
    return {
        "uid":        make_uid(),
        "iri":        iri or make_iri(object_id),
        "uri":        make_uri(object_id, version),
        "version":    version,
        "created_at": now_iso(),
    }

def record_activity(entity_uid: str, table: str,
                    activity: str, agent: str = "system") -> None:
    b = make_base("activity/" + entity_uid, "1.0.0")
    b["activity"]   = activity
    b["agent"]      = agent
    b["started_at"] = b["created_at"]
    conn.execute("""
        CREATE (:SchemaActivity {
            uid: $uid, iri: $iri, uri: $uri,
            version: $version, created_at: $created_at,
            activity: $activity, agent: $agent, started_at: $started_at
        })
    """, b)

    rel = {"SchemaClass":    "PROV_GENERATED",
           "SchemaProperty": "PROV_GENERATED_P",
           "SchemaRule":     "PROV_GENERATED_R"}.get(table)
    if rel:
        conn.execute(f"""
            MATCH (e:{table} {{uid: $euid}}), (a:SchemaActivity {{uid: $auid}})
            CREATE (e)-[:{rel}]->(a)
        """, {"euid": entity_uid, "auid": b["uid"]})


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------

app = FastAPI(
    title="SenseIn Schema Registry",
    version="0.4.0",
    description=(
        "Schema registry backed by LadybugDB. "
        "Every node carries uid · iri · uri · version · created_at."
    ),
)

# ---- Pydantic request models -----------------------------------------------

class ClassCreate(BaseModel):
    object_id: str
    name: str
    definition: str
    iri: Optional[str] = None
    version: str = "1.0.0"
    inherit_from_iri: Optional[str] = None
    mixin_iris: Optional[list[str]] = None
    skos_broader_iri: Optional[str] = None
    skos_related_iri: Optional[str] = None
    prov_agent: str = "anonymous"

class PropertyCreate(BaseModel):
    object_id: str
    name: str
    definition: str
    domain_class_iri: str
    iri: Optional[str] = None
    datatype: str = "xsd:string"
    range_uri: Optional[str] = None
    version: str = "1.0.0"
    prov_agent: str = "anonymous"

class RuleCreate(BaseModel):
    object_id: str
    name: str
    rule_spec: str
    applies_to_iris: list[str]
    iri: Optional[str] = None
    version: str = "1.0.0"
    units: Optional[str] = None
    min_val: Optional[str] = None
    max_val: Optional[str] = None
    pattern: Optional[str] = None
    multivalued: bool = False
    required: bool = False
    prov_agent: str = "anonymous"

class TransformCreate(BaseModel):
    object_id: str
    name: str
    spec: str
    iri: Optional[str] = None
    version: str = "1.0.0"

# ---- Class -----------------------------------------------------------------

@app.post("/schema/class", summary="Create a class")
def create_class(body: ClassCreate):
    b = make_base(body.object_id, body.version, body.iri)
    conn.execute("""
        CREATE (:SchemaClass {
            uid: $uid, iri: $iri, uri: $uri,
            version: $version, created_at: $created_at,
            name: $name, definition: $definition
        })
    """, {**b, "name": body.name, "definition": body.definition})

    if body.inherit_from_iri:
        conn.execute("""
            MATCH (c:SchemaClass {uid: $uid}), (p:SchemaClass {iri: $piri})
            CREATE (c)-[:SUBCLASS_OF]->(p)
        """, {"uid": b["uid"], "piri": body.inherit_from_iri})
    for m in (body.mixin_iris or []):
        conn.execute("""
            MATCH (c:SchemaClass {uid: $uid}), (m:SchemaClass {iri: $miri})
            CREATE (c)-[:MIXIN]->(m)
        """, {"uid": b["uid"], "miri": m})
    if body.skos_broader_iri:
        conn.execute("""
            MATCH (c:SchemaClass {uid: $uid}), (s:SchemaClass {iri: $siri})
            CREATE (c)-[:SKOS_BROADER]->(s)
        """, {"uid": b["uid"], "siri": body.skos_broader_iri})
    if body.skos_related_iri:
        conn.execute("""
            MATCH (c:SchemaClass {uid: $uid}), (s:SchemaClass {iri: $siri})
            CREATE (c)-[:SKOS_RELATED]->(s)
        """, {"uid": b["uid"], "siri": body.skos_related_iri})

    record_activity(b["uid"], "SchemaClass",
                    f"Created {body.object_id} v{body.version}", body.prov_agent)
    return b

@app.get("/schema/class/{object_id}", summary="Get a class (all versions)")
def get_class(object_id: str):
    r = conn.execute("""
        MATCH (n:SchemaClass)
        WHERE n.uri STARTS WITH $prefix
        RETURN n.uid, n.iri, n.uri, n.version, n.created_at, n.name, n.definition
        ORDER BY n.created_at
    """, {"prefix": f"{REG}obj/{object_id}/v/"})
    rows = r.get_all()
    if not rows:
        raise HTTPException(404, f"Class '{object_id}' not found")
    return rows

@app.get("/schema/classes", summary="List all classes")
def list_classes():
    r = conn.execute(
        "MATCH (n:SchemaClass) RETURN n.uid, n.iri, n.uri, n.name, n.version ORDER BY n.name"
    )
    return r.get_as_df().to_dict(orient="records")

@app.get("/schema/class/{object_id}/properties", summary="Properties on a class")
def get_class_properties(object_id: str):
    r = conn.execute("""
        MATCH (c:SchemaClass)-[:HAS_PROPERTY]->(p:SchemaProperty)
        WHERE c.uri STARTS WITH $prefix
        RETURN p.uid, p.iri, p.uri, p.name, p.definition,
               p.datatype, p.range_uri, p.version
    """, {"prefix": f"{REG}obj/{object_id}/v/"})
    return r.get_all()

@app.post("/schema/class/{object_id}/bump", summary="Bump class version")
def bump_class(object_id: str,
               new_definition: str = Body(...),
               prov_agent: str = Body("anonymous")):
    r = conn.execute("""
        MATCH (n:SchemaClass)
        WHERE n.uri STARTS WITH $prefix
        RETURN n.uid, n.iri, n.version, n.name
        ORDER BY n.created_at DESC LIMIT 1
    """, {"prefix": f"{REG}obj/{object_id}/v/"})
    rows = r.get_all()
    if not rows:
        raise HTTPException(404, f"Class '{object_id}' not found")
    old_uid, iri, old_ver, name = rows[0]
    new_ver = bump_version(old_ver)
    b = make_base(object_id, new_ver, iri)
    conn.execute("""
        CREATE (:SchemaClass {
            uid: $uid, iri: $iri, uri: $uri,
            version: $version, created_at: $created_at,
            name: $name, definition: $definition
        })
    """, {**b, "name": name, "definition": new_definition})
    conn.execute("""
        MATCH (new:SchemaClass {uid: $nuid}), (old:SchemaClass {uid: $ouid})
        CREATE (new)-[:PRIOR_VERSION]->(old)
    """, {"nuid": b["uid"], "ouid": old_uid})
    record_activity(b["uid"], "SchemaClass",
                    f"Bumped {object_id} to v{new_ver}", prov_agent)
    return {"old_version": old_ver, "new_version": new_ver, **b}

# ---- Property --------------------------------------------------------------

@app.post("/schema/property", summary="Create a property")
def create_property(body: PropertyCreate):
    b = make_base(body.object_id, body.version, body.iri)
    conn.execute("""
        CREATE (:SchemaProperty {
            uid: $uid, iri: $iri, uri: $uri,
            version: $version, created_at: $created_at,
            name: $name, definition: $definition,
            datatype: $datatype, range_uri: $range_uri
        })
    """, {**b, "name": body.name, "definition": body.definition,
          "datatype": body.datatype, "range_uri": body.range_uri or ""})
    conn.execute("""
        MATCH (c:SchemaClass {iri: $ciri}), (p:SchemaProperty {uid: $puid})
        CREATE (c)-[:HAS_PROPERTY]->(p)
    """, {"ciri": body.domain_class_iri, "puid": b["uid"]})
    record_activity(b["uid"], "SchemaProperty",
                    f"Created {body.object_id} v{body.version}", body.prov_agent)
    return b

@app.get("/schema/property/{object_id}", summary="Get a property (all versions)")
def get_property(object_id: str):
    r = conn.execute("""
        MATCH (n:SchemaProperty)
        WHERE n.uri STARTS WITH $prefix
        RETURN n.uid, n.iri, n.uri, n.version, n.created_at,
               n.name, n.definition, n.datatype, n.range_uri
        ORDER BY n.created_at
    """, {"prefix": f"{REG}obj/{object_id}/v/"})
    rows = r.get_all()
    if not rows:
        raise HTTPException(404, f"Property '{object_id}' not found")
    return rows

# ---- Rule ------------------------------------------------------------------

@app.post("/schema/rule", summary="Create a rule")
def create_rule(body: RuleCreate):
    b = make_base(body.object_id, body.version, body.iri)
    conn.execute("""
        CREATE (:SchemaRule {
            uid: $uid, iri: $iri, uri: $uri,
            version: $version, created_at: $created_at,
            name: $name, rule_spec: $rule_spec,
            units: $units, min_val: $min_val, max_val: $max_val,
            pattern: $pattern, multivalued: $multivalued, required: $required
        })
    """, {**b, "name": body.name, "rule_spec": body.rule_spec,
          "units": body.units or "", "min_val": body.min_val or "",
          "max_val": body.max_val or "", "pattern": body.pattern or "",
          "multivalued": body.multivalued, "required": body.required})
    for target_iri in body.applies_to_iris:
        conn.execute("""
            MATCH (r:SchemaRule {uid: $ruid}), (c:SchemaClass {iri: $ciri})
            CREATE (r)-[:APPLIES_TO]->(c)
        """, {"ruid": b["uid"], "ciri": target_iri})
    record_activity(b["uid"], "SchemaRule",
                    f"Created {body.object_id} v{body.version}", body.prov_agent)
    return b

@app.get("/schema/rule/{object_id}", summary="Get a rule")
def get_rule(object_id: str):
    r = conn.execute("""
        MATCH (n:SchemaRule)
        WHERE n.uri STARTS WITH $prefix
        RETURN n.uid, n.iri, n.uri, n.version, n.created_at,
               n.name, n.rule_spec, n.units, n.min_val, n.max_val,
               n.pattern, n.multivalued, n.required
        ORDER BY n.created_at
    """, {"prefix": f"{REG}obj/{object_id}/v/"})
    rows = r.get_all()
    if not rows:
        raise HTTPException(404, f"Rule '{object_id}' not found")
    return rows

# ---- Transform -------------------------------------------------------------

@app.post("/schema/transform", summary="Create a transform")
def create_transform(body: TransformCreate):
    b = make_base(body.object_id, body.version, body.iri)
    conn.execute("""
        CREATE (:SchemaTransform {
            uid: $uid, iri: $iri, uri: $uri,
            version: $version, created_at: $created_at,
            name: $name, spec: $spec
        })
    """, {**b, "name": body.name, "spec": body.spec})
    return b

@app.get("/schema/transform/{object_id}", summary="Get a transform")
def get_transform(object_id: str):
    r = conn.execute("""
        MATCH (n:SchemaTransform)
        WHERE n.uri STARTS WITH $prefix
        RETURN n.uid, n.iri, n.uri, n.version, n.created_at, n.name, n.spec
        ORDER BY n.created_at
    """, {"prefix": f"{REG}obj/{object_id}/v/"})
    rows = r.get_all()
    if not rows:
        raise HTTPException(404, f"Transform '{object_id}' not found")
    return rows

# ---- Provenance ------------------------------------------------------------

@app.get("/provenance/class/{object_id}", summary="Provenance for a class")
def get_class_provenance(object_id: str):
    r = conn.execute("""
        MATCH (n:SchemaClass)-[:PROV_GENERATED]->(a:SchemaActivity)
        WHERE n.uri STARTS WITH $prefix
        RETURN n.uid, n.uri, a.activity, a.agent, a.started_at
        ORDER BY a.started_at DESC
    """, {"prefix": f"{REG}obj/{object_id}/v/"})
    return r.get_all()

# ---- Distance stub ---------------------------------------------------------

@app.get("/distance/{id1}/{id2}", summary="Distance between objects (stub)")
def distance(id1: str, id2: str):
    return {
        "id1": id1, "id2": id2, "distance": None,
        "note": "Distance function pending scientist specification.",
    }

# ---- Health ----------------------------------------------------------------

@app.get("/health")
def health():
    counts = {}
    for t in ("SchemaClass", "SchemaProperty", "SchemaRule",
              "SchemaTransform", "SchemaSource", "SchemaActivity"):
        counts[t] = conn.execute(f"MATCH (n:{t}) RETURN count(n)").get_next()[0]
    return {"status": "ok", "node_counts": counts}
