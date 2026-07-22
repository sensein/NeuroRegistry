"""
Schema Registry Service — LadybugDB + FastAPI
----------------------------------------------
Property graph backend (LadybugDB / Cypher) for a decentralised schema registry.
RDF triples are modelled as   (:SchemaNode)-[:PREDICATE {value}]->(:SchemaNode)
so the graph stays semantically triple-shaped while benefiting from Ladybug's
columnar, embedded store (no server, no Docker).

Objects:  Class · Property · Rule  — all versioned + PROV-O provenance.
Ingest:   accepts Turtle or JSON-LD (parsed via rdflib → inserted as Cypher).
Seed:     schema.org subset loaded on first startup.

Install:
    pip install ladybug fastapi uvicorn rdflib httpx

Run:
    uvicorn schema_registry:app --reload
    Docs → http://localhost:8000/docs
"""

from __future__ import annotations

import uuid
import datetime
import httpx
from typing import Optional

import ladybug as lb
from fastapi import FastAPI, HTTPException, Body
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Database — file-backed, persists between restarts
# ---------------------------------------------------------------------------

DB_PATH = "./registry.lbug"
db   = lb.Database(DB_PATH)
conn = lb.Connection(db)

# ---------------------------------------------------------------------------
# Schema bootstrap — node/rel tables created once
# ---------------------------------------------------------------------------

DDL = """
CREATE NODE TABLE IF NOT EXISTS SchemaNode (
    uri        STRING PRIMARY KEY,
    kind       STRING,          -- Class | Property | Rule | Transform | Source | Activity
    object_id  STRING,          -- short human id, e.g. "Person"
    name       STRING,
    definition STRING,
    version    STRING,
    created_at STRING
);

CREATE NODE TABLE IF NOT EXISTS Literal (
    id    STRING PRIMARY KEY,   -- uuid
    value STRING,
    dtype STRING
);

CREATE REL TABLE IF NOT EXISTS TRIPLE (
    FROM SchemaNode TO SchemaNode,
    predicate STRING
);

CREATE REL TABLE IF NOT EXISTS TRIPLE_LIT (
    FROM SchemaNode TO Literal,
    predicate STRING
);

CREATE REL TABLE IF NOT EXISTS PRIOR_VERSION (
    FROM SchemaNode TO SchemaNode
);

CREATE REL TABLE IF NOT EXISTS PROV_ACTIVITY (
    FROM SchemaNode TO SchemaNode,
    activity   STRING,
    agent      STRING,
    started_at STRING
);
"""

for stmt in DDL.strip().split(";"):
    s = stmt.strip()
    if s:
        conn.execute(s)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REG = "https://registry.sensein.io/"

def now_iso() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"

def make_uri(object_id: str, version: str) -> str:
    return f"{REG}obj/{object_id}/v/{version}"

def bump_version(ver: str) -> str:
    parts = ver.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)

def upsert_node(uri: str, kind: str, object_id: str = "",
                name: str = "", definition: str = "",
                version: str = "", created_at: str = "") -> None:
    conn.execute("""
        MERGE (n:SchemaNode {uri: $uri})
        ON CREATE SET n.kind=$kind, n.object_id=$oid, n.name=$name,
                      n.definition=$def, n.version=$ver, n.created_at=$cat
        ON MATCH  SET n.kind=$kind, n.object_id=$oid, n.name=$name,
                      n.definition=$def, n.version=$ver
    """, {"uri": uri, "kind": kind, "oid": object_id, "name": name,
          "def": definition, "ver": version, "cat": created_at or now_iso()})

def add_triple(subj_uri: str, predicate: str, obj_uri: str) -> None:
    conn.execute("""
        MATCH (s:SchemaNode {uri: $s}), (o:SchemaNode {uri: $o})
        MERGE (s)-[:TRIPLE {predicate: $p}]->(o)
    """, {"s": subj_uri, "p": predicate, "o": obj_uri})

def add_literal(subj_uri: str, predicate: str, value: str, dtype: str = "xsd:string") -> None:
    lit_id = str(uuid.uuid4())
    conn.execute("CREATE (l:Literal {id: $id, value: $val, dtype: $dt})",
                 {"id": lit_id, "val": value, "dt": dtype})
    conn.execute("""
        MATCH (s:SchemaNode {uri: $s}), (l:Literal {id: $lid})
        CREATE (s)-[:TRIPLE_LIT {predicate: $p}]->(l)
    """, {"s": subj_uri, "p": predicate, "lid": lit_id})

def record_provenance(entity_uri: str, activity: str, agent: str = "system") -> None:
    act_uri = REG + "activity/" + str(uuid.uuid4())
    upsert_node(act_uri, "Activity", created_at=now_iso())
    conn.execute("""
        MATCH (e:SchemaNode {uri: $e}), (a:SchemaNode {uri: $a})
        CREATE (e)-[:PROV_ACTIVITY {activity: $act, agent: $ag, started_at: $t}]->(a)
    """, {"e": entity_uri, "a": act_uri, "act": activity, "ag": agent, "t": now_iso()})


# ---------------------------------------------------------------------------
# Schema.org seed
# ---------------------------------------------------------------------------

SCHEMA_ORG_JSONLD = "https://schema.org/version/latest/schemaorg-current-https.jsonld"

def seed_schema_org() -> None:
    result = conn.execute(
        "MATCH (n:SchemaNode {uri: $uri}) RETURN n.uri",
        {"uri": REG + "seed/schemaorg"}
    )
    if result.has_next():
        print("[seed] schema.org already loaded, skipping.")
        return

    print("[seed] Fetching schema.org JSON-LD … (~10 s)")
    try:
        import rdflib
        resp = httpx.get(SCHEMA_ORG_JSONLD, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        g = rdflib.Graph()
        g.parse(data=resp.text, format="json-ld")

        RDF  = rdflib.RDF
        RDFS = rdflib.RDFS
        OWL  = rdflib.OWL

        # Only load OWL Classes and their labels/comments — keeps it manageable
        inserted = 0
        for subj in g.subjects(RDF.type, OWL.Class):
            uri_str = str(subj)
            label   = str(next(g.objects(subj, RDFS.label),   ""))
            comment = str(next(g.objects(subj, RDFS.comment), ""))
            short_id = uri_str.split("/")[-1].split("#")[-1]
            upsert_node(uri_str, "Class", short_id, label, comment, "1.0.0")
            # subClassOf relations
            for parent in g.objects(subj, RDFS.subClassOf):
                parent_str = str(parent)
                upsert_node(parent_str, "Class")
                add_triple(uri_str, "rdfs:subClassOf", parent_str)
            inserted += 1

        # Mark seed complete
        upsert_node(REG + "seed/schemaorg", "Source", "seed/schemaorg",
                    "schema.org seed", "", "1.0.0")
        record_provenance(REG + "seed/schemaorg", "Seeded from schema.org")
        print(f"[seed] Loaded {inserted} schema.org classes into LadybugDB")
    except Exception as e:
        print(f"[seed] WARNING: could not load schema.org — {e}")


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------

app = FastAPI(
    title="SenseIn Schema Registry",
    description="Decentralised schema registry — Classes · Properties · Rules · versioning · PROV-O. "
                "Backed by LadybugDB (embedded property graph, Cypher queries).",
    version="0.2.0",
)

@app.on_event("startup")
async def startup():
    seed_schema_org()


# ---- Pydantic models -------------------------------------------------------

class ClassCreate(BaseModel):
    id: str
    name: str
    definition: str
    version: str = "1.0.0"
    inherit_from: Optional[str] = None   # URI of parent class
    mixin: Optional[list[str]] = None
    skos_broader: Optional[str] = None
    skos_related: Optional[str] = None
    prov_agent: str = "anonymous"

class PropertyCreate(BaseModel):
    id: str
    name: str
    definition: str
    domain_class_uri: str
    data_type: str = "xsd:string"
    units: Optional[str] = None
    min_value: Optional[str] = None
    max_value: Optional[str] = None
    pattern: Optional[str] = None
    multivalued: bool = False
    required: bool = False
    version: str = "1.0.0"
    prov_agent: str = "anonymous"

class RuleCreate(BaseModel):
    id: str
    rule_spec: str               # Python expression / callable ref stored as literal
    applies_to: list[str]        # list of object URIs
    version: str = "1.0.0"
    prov_agent: str = "anonymous"

class IngestRDF(BaseModel):
    rdf_content: str
    mime_type: str = "text/turtle"   # or application/ld+json
    source_label: str = "external"
    prov_agent: str = "anonymous"


# ---- Classes ---------------------------------------------------------------

@app.post("/schema/class", summary="Create a Class")
def create_class(body: ClassCreate):
    obj_uri = make_uri(body.id, body.version)
    upsert_node(obj_uri, "Class", body.id, body.name, body.definition, body.version)

    if body.inherit_from:
        upsert_node(body.inherit_from, "Class")
        add_triple(obj_uri, "rdfs:subClassOf", body.inherit_from)
    for m in (body.mixin or []):
        upsert_node(m, "Class")
        add_triple(obj_uri, "reg:mixin", m)
    if body.skos_broader:
        upsert_node(body.skos_broader, "Class")
        add_triple(obj_uri, "skos:broader", body.skos_broader)
    if body.skos_related:
        upsert_node(body.skos_related, "Class")
        add_triple(obj_uri, "skos:related", body.skos_related)

    record_provenance(obj_uri, f"Created class {body.id} v{body.version}", body.prov_agent)
    return {"uri": obj_uri, "version": body.version}


@app.get("/schema/class/{class_id}", summary="Get a Class (all versions)")
def get_class(class_id: str):
    result = conn.execute(
        "MATCH (n:SchemaNode {object_id: $id, kind: 'Class'}) RETURN n.*",
        {"id": class_id}
    )
    rows = result.get_all()
    if not rows:
        raise HTTPException(404, f"Class '{class_id}' not found")
    return rows


@app.get("/schema/classes", summary="List all Classes")
def list_classes():
    result = conn.execute(
        "MATCH (n:SchemaNode) WHERE n.kind = 'Class' RETURN n.uri, n.object_id, n.name, n.version"
    )
    return result.get_as_df().to_dict(orient="records")


# ---- Properties ------------------------------------------------------------

@app.post("/schema/property", summary="Create a Property")
def create_property(body: PropertyCreate):
    obj_uri = make_uri(body.id, body.version)
    definition_extended = (
        f"{body.definition} | dataType:{body.data_type}"
        f"{' units:'+body.units if body.units else ''}"
        f"{' min:'+body.min_value if body.min_value else ''}"
        f"{' max:'+body.max_value if body.max_value else ''}"
        f"{' pattern:'+body.pattern if body.pattern else ''}"
        f" multivalued:{body.multivalued} required:{body.required}"
    )
    upsert_node(obj_uri, "Property", body.id, body.name, definition_extended, body.version)

    # Link to domain class
    upsert_node(body.domain_class_uri, "Class")
    add_triple(obj_uri, "rdfs:domain", body.domain_class_uri)

    record_provenance(obj_uri, f"Created property {body.id} v{body.version}", body.prov_agent)
    return {"uri": obj_uri, "version": body.version}


@app.get("/schema/property/{prop_id}", summary="Get a Property")
def get_property(prop_id: str):
    result = conn.execute(
        "MATCH (n:SchemaNode {object_id: $id, kind: 'Property'}) RETURN n.*",
        {"id": prop_id}
    )
    rows = result.get_all()
    if not rows:
        raise HTTPException(404, f"Property '{prop_id}' not found")
    return rows


# ---- Rules -----------------------------------------------------------------

@app.post("/schema/rule", summary="Create a Rule")
def create_rule(body: RuleCreate):
    obj_uri = make_uri(body.id, body.version)
    upsert_node(obj_uri, "Rule", body.id, body.id, body.rule_spec, body.version)

    for target in body.applies_to:
        upsert_node(target, "SchemaNode")
        add_triple(obj_uri, "reg:appliesTo", target)

    record_provenance(obj_uri, f"Created rule {body.id} v{body.version}", body.prov_agent)
    return {"uri": obj_uri, "version": body.version}


# ---- Ingest RDF ------------------------------------------------------------

@app.post("/ingest", summary="Ingest RDF (Turtle or JSON-LD) → inserts as nodes/triples")
def ingest_rdf(body: IngestRDF):
    import rdflib
    fmt_map = {"text/turtle": "turtle", "application/ld+json": "json-ld",
               "application/rdf+xml": "xml", "text/n3": "n3"}
    fmt = fmt_map.get(body.mime_type, "turtle")
    try:
        g = rdflib.Graph()
        g.parse(data=body.rdf_content, format=fmt)
    except Exception as e:
        raise HTTPException(400, f"RDF parse error: {e}")

    source_uri = REG + "source/" + str(uuid.uuid4())
    upsert_node(source_uri, "Source", source_uri, body.source_label, "", "1.0.0")

    inserted_nodes, inserted_triples = 0, 0
    for s, p, o in g:
        s_uri = str(s)
        p_str = str(p).split("/")[-1].split("#")[-1]  # short predicate label
        upsert_node(s_uri, "SchemaNode", s_uri.split("/")[-1])
        inserted_nodes += 1

        if isinstance(o, rdflib.URIRef):
            o_uri = str(o)
            upsert_node(o_uri, "SchemaNode", o_uri.split("/")[-1])
            add_triple(s_uri, p_str, o_uri)
        else:
            add_literal(s_uri, p_str, str(o))
        inserted_triples += 1

    record_provenance(source_uri, f"Ingested from {body.source_label}", body.prov_agent)
    return {"status": "ok", "source_uri": source_uri,
            "nodes_touched": inserted_nodes, "triples_inserted": inserted_triples}


# ---- Version update --------------------------------------------------------

@app.post("/schema/update/{object_id}", summary="Bump version of any object")
def update_version(
    object_id: str,
    new_definition: str = Body(...),
    prov_agent: str = Body("anonymous")
):
    result = conn.execute(
        "MATCH (n:SchemaNode {object_id: $id}) RETURN n.uri, n.version, n.kind ORDER BY n.created_at DESC LIMIT 1",
        {"id": object_id}
    )
    rows = result.get_all()
    if not rows:
        raise HTTPException(404, f"Object '{object_id}' not found")

    old_uri, old_ver, kind = rows[0]
    new_ver = bump_version(old_ver)
    new_uri = make_uri(object_id, new_ver)

    upsert_node(new_uri, kind, object_id, "", new_definition, new_ver)

    # Link old → new via PRIOR_VERSION
    conn.execute("""
        MATCH (old:SchemaNode {uri: $old}), (new:SchemaNode {uri: $new})
        CREATE (new)-[:PRIOR_VERSION]->(old)
    """, {"old": old_uri, "new": new_uri})

    record_provenance(new_uri, f"Updated {object_id} to v{new_ver}", prov_agent)
    return {"old_version": old_ver, "new_version": new_ver, "new_uri": new_uri}


# ---- Provenance ------------------------------------------------------------

@app.get("/provenance/{object_id}", summary="PROV-O history for an object")
def get_provenance(object_id: str):
    result = conn.execute("""
        MATCH (n:SchemaNode {object_id: $id})-[p:PROV_ACTIVITY]->(a:SchemaNode)
        RETURN n.uri AS object_uri, p.activity AS activity,
               p.agent AS agent, p.started_at AS time
        ORDER BY p.started_at DESC
    """, {"id": object_id})
    return result.get_all()


# ---- Relations -------------------------------------------------------------

@app.get("/schema/relations/{uri_encoded}", summary="Get all triples for a node")
def get_relations(uri_encoded: str):
    from urllib.parse import unquote
    node_uri = unquote(uri_encoded)
    out_result = conn.execute("""
        MATCH (s:SchemaNode {uri: $uri})-[t:TRIPLE]->(o:SchemaNode)
        RETURN s.uri AS subject, t.predicate AS predicate, o.uri AS object
    """, {"uri": node_uri})
    lit_result = conn.execute("""
        MATCH (s:SchemaNode {uri: $uri})-[t:TRIPLE_LIT]->(l:Literal)
        RETURN s.uri AS subject, t.predicate AS predicate, l.value AS object, l.dtype AS dtype
    """, {"uri": node_uri})
    return {
        "node_triples": out_result.get_all(),
        "literal_triples": lit_result.get_all(),
    }


# ---- Distance stub ---------------------------------------------------------

@app.get("/distance/{id1}/{id2}", summary="Distance between objects (stub)")
def distance(id1: str, id2: str):
    return {
        "id1": id1, "id2": id2,
        "distance": None,
        "note": "Distance function pending scientist specification (semantic + structural).",
    }


# ---- Health ----------------------------------------------------------------

@app.get("/health")
def health():
    node_count  = conn.execute("MATCH (n:SchemaNode) RETURN count(n)").get_next()[0]
    triple_count = conn.execute("MATCH ()-[t:TRIPLE]->() RETURN count(t)").get_next()[0]
    return {"status": "ok", "schema_nodes": node_count, "triples": triple_count}
