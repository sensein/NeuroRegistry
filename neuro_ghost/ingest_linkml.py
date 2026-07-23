"""
ingest_linkml.py — Ingest LinkML schemas into the SenseIn Schema Registry
--------------------------------------------------------------------------
Reads any .yml file in the schemas/ directory (or a specific file) that
follows LinkML format and inserts its classes and slots as SchemaClass +
SchemaProperty nodes in LadybugDB, using the same base identity pattern
(uid, iri, uri, version, created_at) as the rest of the registry.

What gets extracted from LinkML:
  classes:
    name           → object_id, SchemaClass.name
    description    → SchemaClass.definition
    class_uri      → SchemaClass.iri  (prefix resolved, e.g. schema:Person
                     → https://schema.org/Person)
    is_a           → SUBCLASS_OF relationship to parent SchemaClass
    abstract       → SchemaClass.abstract
    slots          → HAS_PROPERTY edges to SchemaProperty nodes

  slots:
    name           → SchemaProperty.name
    description    → SchemaProperty.definition
    slot_uri       → SchemaProperty.iri  (prefix resolved)
    range          → SchemaProperty.datatype (primitives) or
                     SchemaProperty.range_uri (class refs)
    multivalued    → SchemaProperty.multivalued  (stored on property for now)
    required       → SchemaProperty.required

Every inserted node gets a FROM_SOURCE / FROM_SOURCE_P edge to a
SchemaSource node representing the .yml file.

Usage:
    python ingest_linkml.py                        # ingest all schemas/*.yml
    python ingest_linkml.py --file schemas/bbqs.yml
    python ingest_linkml.py --dry-run
    python ingest_linkml.py --wipe --file schemas/bbqs.yml
"""

from __future__ import annotations
import datetime, uuid, os
from pathlib import Path
from typing import Any

import click
import yaml
import ladybug as lb

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

DB_PATH = "./registry.lbug"

# ---------------------------------------------------------------------------
# Prefix registry — expand CURIE → full IRI
# ---------------------------------------------------------------------------

KNOWN_PREFIXES: dict[str, str] = {
    "schema":  "https://schema.org/",
    "xsd":     "http://www.w3.org/2001/XMLSchema#",
    "linkml":  "https://w3id.org/linkml/",
    "bbqs":    "https://brain-bbq-clone.lovable.app/schema#",
}

# LinkML primitive ranges → xsd datatype
LINKML_PRIMITIVES: dict[str, str] = {
    "string":    "xsd:string",
    "integer":   "xsd:integer",
    "float":     "xsd:float",
    "double":    "xsd:double",
    "boolean":   "xsd:boolean",
    "date":      "xsd:date",
    "datetime":  "xsd:dateTime",
    "uri":       "xsd:anyURI",
    "uriorcurie":"xsd:anyURI",
    "curie":     "xsd:anyURI",
}

REG = "https://registry.sensein.io/"

def resolve_prefix(curie: str, prefixes: dict[str, str]) -> str:
    """Expand a CURIE like 'schema:Person' to its full IRI."""
    if ":" not in curie:
        return curie
    prefix, local = curie.split(":", 1)
    all_prefixes = {**KNOWN_PREFIXES, **prefixes}
    if prefix in all_prefixes:
        return all_prefixes[prefix] + local
    return curie  # return as-is if unknown prefix

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
    return {
        "uid":        make_uid(),
        "iri":        iri or make_iri(object_id),
        "uri":        make_uri(object_id, version),
        "version":    version,
        "created_at": now_iso(),
    }

# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_linkml(path: Path) -> dict[str, Any]:
    """
    Parse a LinkML .yml file and return a normalised dict with:
      meta:     {id, name, version, description}
      prefixes: {prefix: uri}
      classes:  {name: {iri, definition, is_a, abstract, slots: [name,...]}}
      slots:    {name: {iri, definition, datatype, range_uri,
                        multivalued, required}}
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    prefixes = {k: v for k, v in (raw.get("prefixes") or {}).items()}
    version  = str(raw.get("version", "1.0.0"))

    meta = {
        "id":          raw.get("id", ""),
        "name":        raw.get("name", path.stem),
        "version":     version,
        "description": raw.get("description", ""),
    }

    # ---- slots ----
    slots: dict[str, dict] = {}
    for slot_name, slot_def in (raw.get("slots") or {}).items():
        slot_def = slot_def or {}
        raw_range  = slot_def.get("range", "string")
        slot_uri   = slot_def.get("slot_uri", "")
        resolved_iri = resolve_prefix(slot_uri, prefixes) if slot_uri else ""

        if raw_range in LINKML_PRIMITIVES:
            datatype  = LINKML_PRIMITIVES[raw_range]
            range_uri = ""
        else:
            # range is a class reference
            datatype  = ""
            range_uri = resolve_prefix(
                raw_range, prefixes
            ) if ":" in raw_range else make_iri(raw_range)

        slots[slot_name] = {
            "iri":         resolved_iri,
            "definition":  slot_def.get("description", ""),
            "datatype":    datatype,
            "range_uri":   range_uri,
            "multivalued": bool(slot_def.get("multivalued", False)),
            "required":    bool(slot_def.get("required", False)),
        }

    # ---- classes ----
    classes: dict[str, dict] = {}
    for cls_name, cls_def in (raw.get("classes") or {}).items():
        cls_def = cls_def or {}
        class_uri = cls_def.get("class_uri", "")
        resolved_iri = resolve_prefix(class_uri, prefixes) if class_uri else ""

        classes[cls_name] = {
            "iri":        resolved_iri,
            "definition": cls_def.get("description", ""),
            "is_a":       cls_def.get("is_a", None),
            "abstract":   bool(cls_def.get("abstract", False)),
            "slots":      cls_def.get("slots") or [],
        }

    return {"meta": meta, "prefixes": prefixes,
            "classes": classes, "slots": slots}


# ---------------------------------------------------------------------------
# Inserter
# ---------------------------------------------------------------------------

def insert_schema(conn: lb.Connection, parsed: dict,
                  source_label: str, dry_run: bool = False) -> dict:
    meta    = parsed["meta"]
    classes = parsed["classes"]
    slots   = parsed["slots"]
    version = meta["version"]

    stats = {"classes": 0, "properties": 0, "rels": 0}

    # -- SchemaSource node ---------------------------------------------------
    src_uid = make_uid()
    src_uri = f"{REG}source/{src_uid}"
    if not dry_run:
        exists = conn.execute(
            "MATCH (s:SchemaSource {label: $label}) RETURN s.uid LIMIT 1",
            {"label": source_label}
        ).has_next()
        if not exists:
            conn.execute("""
                CREATE (:SchemaSource {
                    uid: $uid, iri: $uri, uri: $uri,
                    version: $version, created_at: $t,
                    label: $label, mime_type: 'application/yaml'
                })
            """, {"uid": src_uid, "uri": src_uri, "version": version,
                  "t": now_iso(), "label": source_label})
        else:
            r = conn.execute(
                "MATCH (s:SchemaSource {label: $label}) RETURN s.uid LIMIT 1",
                {"label": source_label}
            )
            src_uid = r.get_next()[0]

    # -- SchemaClass nodes ---------------------------------------------------
    class_uid_map: dict[str, str] = {}  # cls_name → uid in DB

    for cls_name, cls in classes.items():
        iri = cls["iri"] or make_iri(cls_name)
        b   = make_base(cls_name, version=version, iri=iri)
        class_uid_map[cls_name] = b["uid"]

        if dry_run:
            stats["classes"] += 1
            continue

        # Check if already exists (same iri + source)
        exists = conn.execute(
            "MATCH (n:SchemaClass {iri: $iri, source_label: $src}) RETURN n.uid LIMIT 1",
            {"iri": iri, "src": source_label}
        ).has_next()
        if exists:
            r = conn.execute(
                "MATCH (n:SchemaClass {iri: $iri, source_label: $src}) RETURN n.uid LIMIT 1",
                {"iri": iri, "src": source_label}
            )
            class_uid_map[cls_name] = r.get_next()[0]
            continue

        conn.execute("""
            CREATE (:SchemaClass {
                uid: $uid, iri: $iri, uri: $uri,
                version: $version, created_at: $created_at,
                name: $name, definition: $definition,
                abstract: $abstract, source_label: $source_label
            })
        """, {**b, "name": cls_name, "definition": cls["definition"],
              "abstract": cls["abstract"], "source_label": source_label})

        # FROM_SOURCE
        conn.execute("""
            MATCH (c:SchemaClass {uid: $cuid}), (s:SchemaSource {uid: $suid})
            CREATE (c)-[:FROM_SOURCE]->(s)
        """, {"cuid": b["uid"], "suid": src_uid})

        stats["classes"] += 1

    # -- SUBCLASS_OF relationships -------------------------------------------
    for cls_name, cls in classes.items():
        if not cls["is_a"] or dry_run:
            continue
        parent_name = cls["is_a"]
        child_uid   = class_uid_map.get(cls_name)
        parent_uid  = class_uid_map.get(parent_name)

        if child_uid and parent_uid:
            conn.execute("""
                MATCH (c:SchemaClass {uid: $cuid}), (p:SchemaClass {uid: $puid})
                CREATE (c)-[:SUBCLASS_OF]->(p)
            """, {"cuid": child_uid, "puid": parent_uid})
            stats["rels"] += 1
        elif child_uid:
            # Parent may be from a different source (e.g. schema.org seed)
            # Try matching by name across all sources
            r = conn.execute(
                "MATCH (p:SchemaClass {name: $name}) RETURN p.uid LIMIT 1",
                {"name": parent_name}
            )
            if r.has_next():
                conn.execute("""
                    MATCH (c:SchemaClass {uid: $cuid}), (p:SchemaClass {uid: $puid})
                    CREATE (c)-[:SUBCLASS_OF]->(p)
                """, {"cuid": child_uid, "puid": r.get_next()[0]})
                stats["rels"] += 1

    # -- SchemaProperty nodes + HAS_PROPERTY ---------------------------------
    # Collect all slot names actually used across classes
    used_slots: set[str] = set()
    for cls in classes.values():
        used_slots.update(cls["slots"])

    prop_uid_map: dict[str, str] = {}  # slot_name → uid

    for slot_name in used_slots:
        slot = slots.get(slot_name)
        if not slot:
            continue

        iri = slot["iri"] or make_iri(slot_name)
        b   = make_base(slot_name, version=version, iri=iri)
        prop_uid_map[slot_name] = b["uid"]

        if dry_run:
            stats["properties"] += 1
            continue

        exists = conn.execute(
            "MATCH (p:SchemaProperty {iri: $iri, source_label: $src}) RETURN p.uid LIMIT 1",
            {"iri": iri, "src": source_label}
        ).has_next()
        if exists:
            r = conn.execute(
                "MATCH (p:SchemaProperty {iri: $iri, source_label: $src}) RETURN p.uid LIMIT 1",
                {"iri": iri, "src": source_label}
            )
            prop_uid_map[slot_name] = r.get_next()[0]
            continue

        conn.execute("""
            CREATE (:SchemaProperty {
                uid: $uid, iri: $iri, uri: $uri,
                version: $version, created_at: $created_at,
                name: $name, definition: $definition,
                datatype: $datatype, range_uri: $range_uri,
                multivalued: $multivalued, required: $required,
                source_label: $source_label
            })
        """, {**b, "name": slot_name,
              "definition": slot["definition"],
              "datatype":   slot["datatype"],
              "range_uri":  slot["range_uri"],
              "multivalued": slot["multivalued"],
              "required":    slot["required"],
              "source_label": source_label})

        # FROM_SOURCE_P
        conn.execute("""
            MATCH (p:SchemaProperty {uid: $puid}), (s:SchemaSource {uid: $suid})
            CREATE (p)-[:FROM_SOURCE_P]->(s)
        """, {"puid": b["uid"], "suid": src_uid})

        stats["properties"] += 1

    # -- HAS_PROPERTY edges --------------------------------------------------
    if not dry_run:
        for cls_name, cls in classes.items():
            cls_uid = class_uid_map.get(cls_name)
            if not cls_uid:
                continue
            for slot_name in cls["slots"]:
                prop_uid = prop_uid_map.get(slot_name)
                if not prop_uid:
                    continue
                conn.execute("""
                    MATCH (c:SchemaClass {uid: $cuid}),
                          (p:SchemaProperty {uid: $puid})
                    CREATE (c)-[:HAS_PROPERTY]->(p)
                """, {"cuid": cls_uid, "puid": prop_uid})
                stats["rels"] += 1

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--file",    default=None,
              help="Path to a specific .yml file. Defaults to all schemas/*.yml")
@click.option("--db",      default=DB_PATH, show_default=True,
              help="Path to LadybugDB file.")
@click.option("--dry-run", is_flag=True,
              help="Parse and print counts without writing to DB.")
@click.option("--wipe",    is_flag=True,
              help="Remove existing nodes for this source before re-inserting.")
def cli(file: str | None, db: str, dry_run: bool, wipe: bool) -> None:
    """Ingest LinkML .yml schemas into the registry graph."""
    conn = lb.Connection(lb.Database(db))

    if file:
        files = [Path(file)]
    else:
        schemas_dir = Path("schemas")
        if not schemas_dir.exists():
            click.echo("No schemas/ directory found. "
                       "Create it and add .yml files, or use --file.")
            return
        files = sorted(schemas_dir.glob("*.yml"))
        if not files:
            click.echo("No .yml files found in schemas/")
            return

    for path in files:
        click.echo(f"\nParsing {path} …")
        parsed       = parse_linkml(path)
        source_label = parsed["meta"]["name"]

        if wipe and not dry_run:
            click.echo(f"  Wiping existing nodes for source '{source_label}' …")
            conn.execute(
                "MATCH (n:SchemaClass {source_label: $src}) DETACH DELETE n",
                {"src": source_label}
            )
            conn.execute(
                "MATCH (n:SchemaProperty {source_label: $src}) DETACH DELETE n",
                {"src": source_label}
            )

        stats = insert_schema(conn, parsed, source_label, dry_run=dry_run)

        prefix = "[dry-run] Would insert" if dry_run else "Inserted"
        click.echo(f"  {prefix}: {stats['classes']} classes, "
                   f"{stats['properties']} properties, "
                   f"{stats['rels']} relationships")

        if not dry_run:
            nc = conn.execute("MATCH (n:SchemaClass) RETURN count(n)").get_next()[0]
            np = conn.execute("MATCH (n:SchemaProperty) RETURN count(n)").get_next()[0]
            click.echo(f"  Registry total: {nc} classes, {np} properties.")


if __name__ == "__main__":
    cli()
