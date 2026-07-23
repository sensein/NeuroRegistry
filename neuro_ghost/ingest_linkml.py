"""
ingest_linkml.py — Load a LinkML schema into the NeuroGhost graph database
===========================================================================

WHY THIS FILE EXISTS
--------------------
When a researcher submits a schema (e.g. bbqs.yml, bids.yml), we need to
translate it from the human-readable LinkML YAML format into nodes and
relationships in our LadybugDB property graph.

This file is the bridge between the flat YAML file and the living graph.

WHAT LINKML IS
--------------
LinkML (Linked data Modeling Language) is a schema language used across
biomedical research. A LinkML file looks like this:

  classes:
    Person:
      description: A research investigator
      slots:
        - name
        - orcid

  slots:
    name:
      range: string
      description: Full name

We parse that YAML into our internal data structures, then write it into
LadybugDB as typed nodes connected by typed edges.

WHAT GETS CREATED IN THE GRAPH
-------------------------------
For every class → one RegistryClass node
For every slot  → one RegistryProperty node
For every class→slot relationship → one HAS_PROPERTY edge
For every is_a relationship → one SUBCLASS_OF edge
For every schema file → one SchemaSource node + one SchemaVersionSnapshot
For every node → a FROM_SOURCE edge back to the SchemaSource

VERSION DETECTION AND DIFFS
----------------------------
This file doesn't blindly overwrite. Before inserting anything, it checks
whether a class or property already exists from this source. If it does, it
computes a diff (what changed?) and only if something changed does it create
a new version node and link it to the old one via a PRIOR_VERSION edge.

This means re-running this file on the same schema is safe — it is idempotent
if nothing changed, and version-bumping if something did.

CONTENT-ADDRESSED IDENTITY
---------------------------
Every RegistryProperty gets a content_id — a SHA-256 hash of its semantic
fields (iri, value_range, units, constraints). Two properties from different
schemas with the same content_id are automatically the same concept. This
is how we deduplicate across BIDS, NWB, DANDI, etc. without any human
needing to manually say "these are the same thing."

See db.py: compute_content_id() for the hash function.

USAGE
-----
  python ingest_linkml.py --file schemas/bbqs.yml
  python ingest_linkml.py                          # all schemas/*.yml
  python ingest_linkml.py --dry-run                # preview, no writes
  python ingest_linkml.py --wipe --file schemas/bbqs.yml  # wipe and re-ingest
"""

from __future__ import annotations
import datetime, uuid, os
from pathlib import Path
from typing import Any

import click
import yaml

from db import (
    get_connection, make_base, make_uid, make_iri, now_iso,
    compute_content_id, REG,
)

DB_PATH = "./registry.lbug"

# ---------------------------------------------------------------------------
# Prefix resolution
# ---------------------------------------------------------------------------
# LinkML files use CURIEs like "schema:Person" instead of full IRIs like
# "https://schema.org/Person". We expand them to full IRIs using a prefix map.
#
# KNOWN_PREFIXES covers the most common ones. The schema file's own "prefixes:"
# block is merged on top, so schema-specific prefixes take precedence.

KNOWN_PREFIXES: dict[str, str] = {
    "schema":   "https://schema.org/",
    "xsd":      "http://www.w3.org/2001/XMLSchema#",
    "linkml":   "https://w3id.org/linkml/",
    "bbqs":     "https://brain-bbq-clone.lovable.app/schema#",
    "bids":     "https://bids-specification.readthedocs.io/en/stable/",
    "nwb":      "https://nwb-schema.readthedocs.io/en/latest/",
    "dandi":    "https://schema.dandiarchive.org/",
    "openminds":"https://openminds.ebrains.eu/",
    "aind":     "https://aind-data-schema.readthedocs.io/en/stable/",
}

# LinkML has its own built-in primitive types that map to XSD datatypes.
# We need this map because "range: string" in LinkML means xsd:string in RDF.
LINKML_PRIMITIVES: dict[str, str] = {
    "string":     "xsd:string",
    "integer":    "xsd:integer",
    "float":      "xsd:float",
    "double":     "xsd:double",
    "boolean":    "xsd:boolean",
    "date":       "xsd:date",
    "datetime":   "xsd:dateTime",
    "uri":        "xsd:anyURI",
    "uriorcurie": "xsd:anyURI",
    "curie":      "xsd:anyURI",
}


def resolve_prefix(curie: str, prefixes: dict[str, str]) -> str:
    """
    Expand a CURIE (Compact URI) to a full IRI.

    Example:
      resolve_prefix("schema:Person", {}) → "https://schema.org/Person"
      resolve_prefix("https://already.full/uri", {}) → "https://already.full/uri"
      resolve_prefix("unknownprefix:foo", {}) → "unknownprefix:foo"  (unchanged)

    Why: Storing full IRIs instead of CURIEs makes the graph self-contained.
    Two schemas using different prefixes for the same thing will resolve to
    the same IRI and thus get the same content_id.
    """
    if not curie or ":" not in curie:
        return curie
    # If it already looks like a full URL, don't expand it
    if curie.startswith("http://") or curie.startswith("https://"):
        return curie
    prefix, local = curie.split(":", 1)
    all_prefixes = {**KNOWN_PREFIXES, **prefixes}
    if prefix in all_prefixes:
        return all_prefixes[prefix] + local
    return curie


# ---------------------------------------------------------------------------
# LinkML parser
# ---------------------------------------------------------------------------

def parse_linkml(path: Path) -> dict[str, Any]:
    """
    Read a LinkML YAML file and return a clean, normalised Python dict.

    Input (LinkML YAML):
      classes:
        Person:
          description: A person
          class_uri: schema:Person
          slots: [name, email]
      slots:
        name:
          range: string
          slot_uri: schema:name

    Output (our internal format):
      {
        "meta": {"name": "bbqs", "version": "1.0.0", ...},
        "prefixes": {"schema": "https://schema.org/", ...},
        "classes": {
          "Person": {
            "iri": "https://schema.org/Person",
            "definition": "A person",
            "is_a": None,
            "is_abstract": False,
            "slots": ["name", "email"]
          }
        },
        "slots": {
          "name": {
            "iri": "https://schema.org/name",
            "definition": "",
            "value_range": "xsd:string",   ← primitive → XSD; class ref → IRI
            "multivalued": False,
            "required": False
          }
        }
      }

    The key normalisation step is value_range:
      - If range is a LinkML primitive (string, integer, etc.) → XSD CURIE
      - If range is a class name or CURIE → resolved IRI
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

    # Parse slots first — classes reference them by name
    slots: dict[str, dict] = {}
    for slot_name, slot_def in (raw.get("slots") or {}).items():
        slot_def   = slot_def or {}
        raw_range  = slot_def.get("range", "string")
        slot_uri   = slot_def.get("slot_uri", "")
        resolved_iri = resolve_prefix(slot_uri, prefixes) if slot_uri else ""

        # Normalise raw_range — can be None if the YAML slot has no range defined
        if not raw_range or not isinstance(raw_range, str):
            raw_range = "string"

        if raw_range in LINKML_PRIMITIVES:
            value_range = LINKML_PRIMITIVES[raw_range]
        else:
            # It's a reference to another class — store as a resolved IRI
            value_range = (resolve_prefix(raw_range, prefixes)
                          if ":" in raw_range else make_iri(raw_range))

        # Extract units from description if present (common in neuro schemas)
        desc = slot_def.get("description") or ""
        desc = str(desc) if desc is not None else ""
        units = ""
        if desc and "(units:" in desc.lower():
            import re
            m = re.search(r'\(units?:\s*([^)]+)\)', desc, re.IGNORECASE)
            if m:
                units = m.group(1).strip()

        slots[slot_name] = {
            "iri":         resolved_iri,
            "definition":  desc,
            "value_range": value_range,
            "units":       units,
            "multivalued": bool(slot_def.get("multivalued", False)),
            "required":    bool(slot_def.get("required", False)),
            "pattern":     slot_def.get("pattern", ""),
        }

    # Parse classes
    classes: dict[str, dict] = {}
    for cls_name, cls_def in (raw.get("classes") or {}).items():
        cls_def   = cls_def or {}
        class_uri = cls_def.get("class_uri", "")
        resolved_iri = resolve_prefix(class_uri, prefixes) if class_uri else ""

        classes[cls_name] = {
            "iri":         resolved_iri,
            "definition":  cls_def.get("description", ""),
            "is_a":        cls_def.get("is_a", None),
            "is_abstract": bool(cls_def.get("abstract", False)),
            "slots":       cls_def.get("slots") or [],
        }

    return {
        "meta":     meta,
        "prefixes": prefixes,
        "classes":  classes,
        "slots":    slots,
    }


# ---------------------------------------------------------------------------
# Version detection helpers
# ---------------------------------------------------------------------------
# These functions query the graph to find the most recent version of a class
# or property. "Most recent" means: no other node has a PRIOR_VERSION edge
# pointing TO this one (i.e. nothing is older than it in the chain).
#
# Why this query?  If we have:
#   Person v2 -[PRIOR_VERSION]-> Person v1
#
# Then v2 is the latest because nothing points PRIOR_VERSION to it.
# v1 is older because v2 points to it.

def _read_class(conn, iri: str, source_label: str) -> dict | None:
    """
    Find the latest version of a class in the graph by its IRI and source.

    Returns a dict with hash_id, definition, is_abstract, and the set of
    property IRIs currently attached to it. Returns None if no class exists.
    """
    r = conn.execute("""
        MATCH (n:RegistryClass {iri: $iri, source_label: $src})
        WHERE NOT EXISTS {
            MATCH (newer:RegistryClass)-[:PRIOR_VERSION]->(n)
        }
        RETURN n.hash_id, n.definition, n.is_abstract
        LIMIT 1
    """, {"iri": iri, "src": source_label})
    if not r.has_next():
        return None
    hash_id, defn, is_abstract = r.get_next()
    # Also get the set of property IRIs currently attached to this class.
    # We need this to detect if properties were added or removed.
    props_r = conn.execute("""
        MATCH (c:RegistryClass {hash_id: $hash_id})-[:HAS_PROPERTY]->(p:RegistryProperty)
        RETURN p.iri
    """, {"hash_id": hash_id})
    prop_iris = {row[0] for row in props_r.get_all() if row[0]}
    return {
        "hash_id":     hash_id,
        "definition":  defn or "",
        "is_abstract": bool(is_abstract),
        "prop_iris":   prop_iris,
    }


def _read_property(conn, iri: str, source_label: str) -> dict | None:
    """
    Find the latest version of a property in the graph by IRI and source.
    Returns None if not found.
    """
    r = conn.execute("""
        MATCH (n:RegistryProperty {iri: $iri, source_label: $src})
        WHERE NOT EXISTS {
            MATCH (newer:RegistryProperty)-[:PRIOR_VERSION_P]->(n)
        }
        RETURN n.hash_id, n.definition, n.value_range,
               n.units, n.multivalued, n.required
        LIMIT 1
    """, {"iri": iri, "src": source_label})
    if not r.has_next():
        return None
    hash_id, defn, value_range, units, mv, req = r.get_next()
    return {
        "hash_id":     hash_id,
        "definition":  defn        or "",
        "value_range": value_range or "",
        "units":       units       or "",
        "multivalued": bool(mv),
        "required":    bool(req),
    }


# ---------------------------------------------------------------------------
# Diff computation
# ---------------------------------------------------------------------------
# A diff tells us: what changed between the existing version in the graph
# and the new version we're about to ingest?
#
# If nothing changed → return None (don't create a new version, don't waste
# storage)
# If something changed → return a dict describing what changed, so we can
# store it on the PRIOR_VERSION edge for history browsing.

def _diff_class(existing: dict, new_cls: dict,
                new_prop_iris: set) -> dict | None:
    """
    Compare an existing class to its incoming new definition.

    Returns None if nothing changed (signals: skip, reuse existing hash_id).
    Returns a diff dict if something changed (signals: create new version).

    The diff dict is stored on the PRIOR_VERSION edge so the Provenance
    view can show exactly what changed and when.
    """
    changed = []

    # Check text fields
    if existing["definition"] != new_cls["definition"]:
        changed.append("definition")
    if existing["is_abstract"] != new_cls["is_abstract"]:
        changed.append("is_abstract")

    # Check property set changes
    # added = in new but not in existing
    # removed = in existing but not in new
    added   = new_prop_iris - existing["prop_iris"]
    removed = existing["prop_iris"] - new_prop_iris
    if added:
        changed.append("added_properties")
    if removed:
        changed.append("removed_properties")

    if not changed:
        return None  # Nothing changed — caller should reuse existing hash_id

    # Build a human-readable summary of what changed
    parts = []
    if "definition" in changed:
        parts.append("definition updated")
    if "is_abstract" in changed:
        parts.append(f"is_abstract → {new_cls['is_abstract']}")
    if added:
        parts.append(f"+{len(added)} properties")
    if removed:
        parts.append(f"-{len(removed)} properties")

    return {
        "changed_fields":      ",".join(changed),
        "added_properties":    ",".join(sorted(added)),
        "removed_properties":  ",".join(sorted(removed)),
        "definition_from":     existing["definition"],
        "definition_to":       new_cls["definition"],
        "diff_summary":        "; ".join(parts),
    }


def _diff_property(existing: dict, new_slot: dict) -> dict | None:
    """
    Compare an existing property to its incoming new definition.
    Same pattern as _diff_class — returns None if nothing changed.
    """
    changed = []
    if existing["definition"] != new_slot["definition"]:   changed.append("definition")
    if existing["value_range"] != new_slot["value_range"]: changed.append("value_range")
    if existing["units"] != new_slot.get("units", ""):     changed.append("units")
    if existing["multivalued"] != new_slot["multivalued"]: changed.append("multivalued")
    if existing["required"]    != new_slot["required"]:    changed.append("required")

    if not changed:
        return None

    parts = []
    if "definition"  in changed: parts.append("definition updated")
    if "value_range" in changed:
        parts.append(f"value_range: {existing['value_range']} → {new_slot['value_range']}")
    if "units"       in changed: parts.append("units updated")

    return {
        "changed_fields":  ",".join(changed),
        "definition_from": existing["definition"],
        "definition_to":   new_slot["definition"],
        "datatype_from":   existing["value_range"],   # column name kept for compat
        "datatype_to":     new_slot["value_range"],
        "diff_summary":    "; ".join(parts) or "metadata updated",
    }


# ---------------------------------------------------------------------------
# Semver helpers (used for SchemaVersionSnapshot versioning)
# ---------------------------------------------------------------------------

def _prev_schema_version(conn, source_label: str) -> str | None:
    """Find the most recent SchemaVersionSnapshot for this schema, or None."""
    r = conn.execute("""
        MATCH (s:SchemaVersionSnapshot {schema_label: $src})
        RETURN s.version, s.created_at
        ORDER BY s.created_at DESC LIMIT 1
    """, {"src": source_label})
    return r.get_next()[0] if r.has_next() else None


def _bump_semver(ver: str, level: str) -> str:
    """
    Increment a semver string at the given level.

    Examples:
      _bump_semver("1.0.0", "patch") → "1.0.1"
      _bump_semver("1.0.0", "minor") → "1.1.0"
      _bump_semver("1.2.3", "major") → "2.0.0"
    """
    parts = [int(x) for x in ver.split(".")]
    while len(parts) < 3:
        parts.append(0)
    if level == "major":
        parts[0] += 1; parts[1] = 0; parts[2] = 0
    elif level == "minor":
        parts[1] += 1; parts[2] = 0
    else:  # patch
        parts[2] += 1
    return ".".join(str(p) for p in parts)


# ---------------------------------------------------------------------------
# SemanticIdentity helpers
# ---------------------------------------------------------------------------
# A SemanticIdentity node is the canonical home for a content_id.
# When BIDS "participant_name" and AIND "subject_name" both hash to xyz789,
# they both get a HAS_IDENTITY_P edge pointing to the same SemanticIdentity
# node with content_id=xyz789.
#
# This is the deduplication mechanism across sources.

def _ensure_semantic_identity(conn, content_id: str, iri: str,
                               value_range: str, units: str,
                               registry_version: str) -> str:
    """
    Find or create a SemanticIdentity node for this content_id.

    Returns the uid of the SemanticIdentity node.

    Why "find or create"? Because the first source to ingest a concept
    creates the SemanticIdentity. Subsequent sources that hash to the same
    content_id just reuse it — this is the automatic deduplication.
    """
    r = conn.execute(
        "MATCH (n:SemanticIdentity {content_id: $cid}) RETURN n.uid LIMIT 1",
        {"cid": content_id}
    )
    if r.has_next():
        return r.get_next()[0]

    # First time we've seen this content_id — create the canonical node
    uid = make_uid()
    canonical_uri = f"{REG}id/{content_id}"
    conn.execute("""
        CREATE (:SemanticIdentity {
            uid:           $uid,
            content_id:    $cid,
            canonical_uri: $uri,
            datatype:      $dt,
            units:         $units,
            iri:           $iri,
            created_at:    $t
        })
    """, {
        "uid":   uid,
        "cid":   content_id,
        "uri":   canonical_uri,
        "dt":    value_range,
        "units": units,
        "iri":   iri,
        "t":     now_iso(),
    })
    return uid


# ---------------------------------------------------------------------------
# Main insertion logic
# ---------------------------------------------------------------------------

def insert_schema(conn, parsed: dict, source_label: str,
                  dry_run: bool = False,
                  registry_version: str = "",
                  yml_path: str = "") -> dict:
    """
    Insert (or update) a parsed LinkML schema into the LadybugDB graph.

    This is the core function of the ingestion pipeline. It:
      1. Creates or reuses the SchemaSource node for this schema
      2. For each slot/property:
         a. Computes its content_id (SHA-256 hash of semantic fields)
         b. Checks if it already exists in the graph from this source
         c. If new → create with make_base(), link to SemanticIdentity
         d. If changed → compute diff, create new node,
            link old→new with PRIOR_VERSION_P carrying the diff
         e. If unchanged → reuse existing hash_id, nothing created
      3. Same logic for classes, but classes version-bump when their
         property set changes (added or removed properties)
      4. Creates SUBCLASS_OF and HAS_PROPERTY edges
      5. Creates a SchemaVersionSnapshot recording the state of the whole
         schema at this ingest time, with its own independent semver

    Returns a stats dict showing what was created/updated/skipped.
    """
    meta    = parsed["meta"]
    classes = parsed["classes"]
    slots   = parsed["slots"]

    stats = {
        "classes_created":    0,
        "classes_updated":    0,
        "classes_unchanged":  0,
        "properties_created": 0,
        "properties_updated": 0,
        "properties_unchanged": 0,
        "rels":               0,
        "class_diffs":        [],
        "property_diffs":     [],
    }

    # ------------------------------------------------------------------
    # Step 1: SchemaSource node
    # ------------------------------------------------------------------
    # Each schema file gets one SchemaSource node. If this source has been
    # ingested before, we reuse the existing SchemaSource uid (we don't want
    # duplicate SchemaSource nodes for repeated ingests of the same schema).

    src_uid = make_uid()
    src_uri = f"{REG}source/{src_uid}"
    if not dry_run:
        r = conn.execute(
            "MATCH (s:SchemaSource {label: $label}) RETURN s.uid LIMIT 1",
            {"label": source_label}
        )
        if r.has_next():
            src_uid = r.get_next()[0]
        else:
            conn.execute("""
                CREATE (:SchemaSource {
                    uid: $uid, iri: $uri, uri: $uri,
                    version: $version, created_at: $t,
                    label: $label, mime_type: 'application/yaml',
                    registry_version: $rv
                })
            """, {
                "uid":     src_uid,
                "uri":     src_uri,
                "version": meta["version"],
                "t":       now_iso(),
                "label":   source_label,
                "rv":      registry_version,
            })

    # ------------------------------------------------------------------
    # Step 2: Pre-compute which property IRIs belong to each class
    # ------------------------------------------------------------------
    # We need to know the new property set for each class BEFORE we process
    # classes, so we can diff the class's property set against what's in the
    # graph. This is done up front to avoid order-of-processing issues.

    class_new_prop_iris: dict[str, set] = {}
    for cls_name, cls in classes.items():
        piris = set()
        for slot_name in cls["slots"]:
            slot = slots.get(slot_name)
            if slot:
                piris.add(slot["iri"] or make_iri(slot_name))
        class_new_prop_iris[cls_name] = piris

    # Collect all slot names used across any class in this schema
    used_slots: set[str] = set()
    for cls in classes.values():
        used_slots.update(cls["slots"])

    # ------------------------------------------------------------------
    # Step 3: Process properties (slots)
    # ------------------------------------------------------------------
    # We process properties before classes because a class's version may
    # need to bump due to property changes, and we need the property hash_ids
    # to create HAS_PROPERTY edges after classes are processed.

    prop_hash_id_map: dict[str, str] = {}  # slot_name → hash_id of latest version

    for slot_name in used_slots:
        slot = slots.get(slot_name)
        if not slot:
            continue

        iri = slot["iri"] or make_iri(slot_name)

        # Compute the content_id for SemanticIdentity deduplication.
        # Two properties that hash identically ARE the same concept,
        # regardless of what source they came from or what they're named.
        cid = compute_content_id(
            iri         = iri,
            datatype    = slot["value_range"],
            units       = slot.get("units", ""),
            pattern     = slot.get("pattern", ""),
            multivalued = slot["multivalued"],
            required    = slot["required"],
        )

        # Check if this property already exists in the graph from this source
        existing = _read_property(conn, iri, source_label) if not dry_run else None

        if existing is None:
            # New property — never seen before from this source
            b = make_base(slot_name, iri=iri)
            prop_hash_id_map[slot_name] = b["hash_id"]

            if dry_run:
                stats["properties_created"] += 1
                continue

            conn.execute("""
                CREATE (:RegistryProperty {
                    hash_id:      $hash_id,
                    iri:          $iri,
                    created_by:   $created_by,
                    created_at:   $created_at,
                    name:         $name,
                    definition:   $definition,
                    value_range:  $value_range,
                    units:        $units,
                    multivalued:  $multivalued,
                    required:     $required,
                    source_label: $source_label,
                    registry_version: $rv
                })
            """, {
                **b,
                "name":           slot_name,
                "definition":     slot["definition"] or "",
                "value_range":    slot["value_range"],
                "units":          slot.get("units", ""),
                "multivalued":    slot["multivalued"],
                "required":       slot["required"],
                "source_label":   source_label,
                "rv":             registry_version,
            })

            # Link to SemanticIdentity (find or create based on content_id)
            si_uid = _ensure_semantic_identity(
                conn, cid, iri, slot["value_range"],
                slot.get("units", ""), registry_version
            )
            conn.execute("""
                MATCH (p:RegistryProperty {hash_id: $phash_id}),
                      (si:SemanticIdentity {uid: $siuid})
                CREATE (p)-[:HAS_IDENTITY_P]->(si)
            """, {"phash_id": b["hash_id"], "siuid": si_uid})

            # Track where this property came from
            conn.execute("""
                MATCH (p:RegistryProperty {hash_id: $phash_id}),
                      (s:SchemaSource {uid: $suid})
                CREATE (p)-[:FROM_SOURCE_P]->(s)
            """, {"phash_id": b["hash_id"], "suid": src_uid})

            stats["properties_created"] += 1

        else:
            # Property exists — check if anything changed
            diff = _diff_property(existing, slot)

            if diff is None:
                # Nothing changed — reuse the existing hash_id as-is
                prop_hash_id_map[slot_name] = existing["hash_id"]
                stats["properties_unchanged"] += 1
            else:
                # Something changed — mint a new version node
                b = make_base(slot_name, iri=iri)
                prop_hash_id_map[slot_name] = b["hash_id"]

                conn.execute("""
                    CREATE (:RegistryProperty {
                        hash_id:      $hash_id,
                        iri:          $iri,
                        created_by:   $created_by,
                        created_at:   $created_at,
                        name:         $name,
                        definition:   $definition,
                        value_range:  $value_range,
                        units:        $units,
                        multivalued:  $multivalued,
                        required:     $required,
                        source_label: $source_label,
                        registry_version: $rv
                    })
                """, {
                    **b,
                    "name":           slot_name,
                    "definition":     slot["definition"] or "",
                    "value_range":    slot["value_range"],
                    "units":          slot.get("units", ""),
                    "multivalued":    slot["multivalued"],
                    "required":       slot["required"],
                    "source_label":   source_label,
                    "rv":             registry_version,
                })

                # PRIOR_VERSION_P edge: new node → old node, carrying the diff.
                # This creates the version chain we can walk to see history.
                conn.execute("""
                    MATCH (new:RegistryProperty {hash_id: $nhash_id}),
                          (old:RegistryProperty {hash_id: $ohash_id})
                    CREATE (new)-[:PRIOR_VERSION_P {
                        diff_summary:    $ds,
                        changed_fields:  $cf,
                        definition_from: $df,
                        definition_to:   $dt,
                        datatype_from:   $dtf,
                        datatype_to:     $dtt,
                        registry_version: $rv,
                        created_at:      $ca
                    }]->(old)
                """, {
                    "nhash_id": b["hash_id"],
                    "ohash_id": existing["hash_id"],
                    "ds":   diff["diff_summary"],
                    "cf":   diff["changed_fields"],
                    "df":   diff["definition_from"],
                    "dt":   diff["definition_to"],
                    "dtf":  diff["datatype_from"],
                    "dtt":  diff["datatype_to"],
                    "rv":   registry_version,
                    "ca":   now_iso(),
                })

                stats["properties_updated"] += 1
                stats["property_diffs"].append({
                    "name": slot_name,
                    "iri":  iri,
                    **diff,
                })

    # ------------------------------------------------------------------
    # Step 4: Process classes
    # ------------------------------------------------------------------

    class_hash_id_map: dict[str, str] = {}  # cls_name → hash_id of latest version

    for cls_name, cls in classes.items():
        iri           = cls["iri"] or make_iri(cls_name)
        new_prop_iris = class_new_prop_iris[cls_name]

        existing = _read_class(conn, iri, source_label) if not dry_run else None

        if existing is None:
            # New class
            b = make_base(cls_name, iri=iri)
            class_hash_id_map[cls_name] = b["hash_id"]

            if dry_run:
                stats["classes_created"] += 1
                continue

            conn.execute("""
                CREATE (:RegistryClass {
                    hash_id:      $hash_id,
                    iri:          $iri,
                    created_by:   $created_by,
                    created_at:   $created_at,
                    name:         $name,
                    definition:   $definition,
                    is_abstract:  $is_abstract,
                    source_label: $source_label,
                    registry_version: $rv
                })
            """, {
                **b,
                "name":           cls_name,
                "definition":     cls["definition"] or "",
                "is_abstract":    cls["is_abstract"],
                "source_label":   source_label,
                "rv":             registry_version,
            })

            conn.execute("""
                MATCH (c:RegistryClass {hash_id: $chash_id}),
                      (s:SchemaSource {uid: $suid})
                CREATE (c)-[:FROM_SOURCE]->(s)
            """, {"chash_id": b["hash_id"], "suid": src_uid})

            stats["classes_created"] += 1

        else:
            diff = _diff_class(existing, cls, new_prop_iris)

            if diff is None:
                class_hash_id_map[cls_name] = existing["hash_id"]
                stats["classes_unchanged"] += 1
            else:
                b = make_base(cls_name, iri=iri)
                class_hash_id_map[cls_name] = b["hash_id"]

                conn.execute("""
                    CREATE (:RegistryClass {
                        hash_id:      $hash_id,
                        iri:          $iri,
                        created_by:   $created_by,
                        created_at:   $created_at,
                        name:         $name,
                        definition:   $definition,
                        is_abstract:  $is_abstract,
                        source_label: $source_label,
                        registry_version: $rv
                    })
                """, {
                    **b,
                    "name":           cls_name,
                    "definition":     cls["definition"] or "",
                    "is_abstract":    cls["is_abstract"],
                    "source_label":   source_label,
                    "rv":             registry_version,
                })

                # PRIOR_VERSION edge with diff data
                conn.execute("""
                    MATCH (new:RegistryClass {hash_id: $nhash_id}),
                          (old:RegistryClass {hash_id: $ohash_id})
                    CREATE (new)-[:PRIOR_VERSION {
                        diff_summary:       $ds,
                        changed_fields:     $cf,
                        added_properties:   $ap,
                        removed_properties: $rp,
                        definition_from:    $df,
                        definition_to:      $dt,
                        registry_version:   $rv,
                        created_at:         $ca
                    }]->(old)
                """, {
                    "nhash_id": b["hash_id"],
                    "ohash_id": existing["hash_id"],
                    "ds":   diff["diff_summary"],
                    "cf":   diff["changed_fields"],
                    "ap":   diff["added_properties"],
                    "rp":   diff["removed_properties"],
                    "df":   diff["definition_from"],
                    "dt":   diff["definition_to"],
                    "rv":   registry_version,
                    "ca":   now_iso(),
                })

                stats["classes_updated"] += 1
                stats["class_diffs"].append({
                    "name": cls_name,
                    "iri":  iri,
                    **diff,
                })

    # ------------------------------------------------------------------
    # Step 5: Structural relationships
    # ------------------------------------------------------------------
    # Now that all nodes exist, we wire up the edges.
    # We check for existing edges before creating to avoid duplicates
    # (important because ingest is designed to be re-runnable safely).

    if not dry_run:
        # SUBCLASS_OF: class inheritance chains
        for cls_name, cls in classes.items():
            if not cls["is_a"]:
                continue
            child_hash_id  = class_hash_id_map.get(cls_name)
            parent_hash_id = class_hash_id_map.get(cls["is_a"])

            if child_hash_id and parent_hash_id:
                already = conn.execute("""
                    MATCH (c:RegistryClass {hash_id: $chash_id})-[:SUBCLASS_OF]->(p:RegistryClass {hash_id: $phash_id})
                    RETURN c.hash_id LIMIT 1
                """, {"chash_id": child_hash_id, "phash_id": parent_hash_id}).has_next()
                if not already:
                    conn.execute("""
                        MATCH (c:RegistryClass {hash_id: $chash_id}),
                              (p:RegistryClass {hash_id: $phash_id})
                        CREATE (c)-[:SUBCLASS_OF]->(p)
                    """, {"chash_id": child_hash_id, "phash_id": parent_hash_id})
                    stats["rels"] += 1
            elif child_hash_id and cls["is_a"]:
                # Parent might be from schema.org or another source —
                # try matching by name across all sources
                r = conn.execute(
                    "MATCH (p:RegistryClass {name: $name}) RETURN p.hash_id LIMIT 1",
                    {"name": cls["is_a"]}
                )
                if r.has_next():
                    p_hash_id = r.get_next()[0]
                    conn.execute("""
                        MATCH (c:RegistryClass {hash_id: $chash_id}),
                              (p:RegistryClass {hash_id: $phash_id})
                        CREATE (c)-[:SUBCLASS_OF]->(p)
                    """, {"chash_id": child_hash_id, "phash_id": p_hash_id})
                    stats["rels"] += 1

        # HAS_PROPERTY: class → its properties
        for cls_name, cls in classes.items():
            cls_hash_id = class_hash_id_map.get(cls_name)
            if not cls_hash_id:
                continue
            for slot_name in cls["slots"]:
                prop_hash_id = prop_hash_id_map.get(slot_name)
                if not prop_hash_id:
                    continue
                already = conn.execute("""
                    MATCH (c:RegistryClass {hash_id: $chash_id})-[:HAS_PROPERTY]->(p:RegistryProperty {hash_id: $phash_id})
                    RETURN c.hash_id LIMIT 1
                """, {"chash_id": cls_hash_id, "phash_id": prop_hash_id}).has_next()
                if not already:
                    conn.execute("""
                        MATCH (c:RegistryClass {hash_id: $chash_id}),
                              (p:RegistryProperty {hash_id: $phash_id})
                        CREATE (c)-[:HAS_PROPERTY]->(p)
                    """, {"chash_id": cls_hash_id, "phash_id": prop_hash_id})
                    stats["rels"] += 1

    # ------------------------------------------------------------------
    # Step 6: Schema version snapshot
    # ------------------------------------------------------------------
    # Once all classes and properties are processed, we record a whole-schema
    # snapshot. This has its own independent semver, separate from individual
    # class/property version chains.
    #
    # Schema semver meaning:
    #   patch → only text/definition changes
    #   minor → classes or properties added/removed
    #   (major → breaking, handled manually via the 'breaking' issue label)

    if not dry_run:
        prev_ver = _prev_schema_version(conn, source_label)
        if prev_ver is None:
            schema_ver = meta.get("version") or "1.0.0"
        else:
            has_structural_change = bool(
                stats["classes_created"] or
                stats["properties_created"] or
                any(d.get("added_properties") or d.get("removed_properties")
                    for d in stats["class_diffs"])
            )
            has_any_change = bool(
                stats["classes_created"] or stats["classes_updated"] or
                stats["properties_created"] or stats["properties_updated"]
            )
            if not has_any_change:
                # Nothing changed — no snapshot needed
                stats["schema_version"] = prev_ver
                stats["schema_unchanged"] = True
                return stats
            level      = "minor" if has_structural_change else "patch"
            schema_ver = _bump_semver(prev_ver, level)

        changes_summary = (
            f"+{stats['classes_created']} classes, "
            f"~{stats['classes_updated']} updated, "
            f"+{stats['properties_created']} props, "
            f"~{stats['properties_updated']} updated"
        )

        snap_uid = make_uid()
        snap_iri = f"{REG}schema/{source_label}/v/{schema_ver}"
        conn.execute("""
            CREATE (:SchemaVersionSnapshot {
                uid: $uid, iri: $iri, uri: $uri,
                version: $version, created_at: $created_at,
                schema_label: $sl, yml_path: $yp,
                class_count: $cc, property_count: $pc, rule_count: $rc,
                changes_summary: $cs, registry_version: $rv
            })
        """, {
            "uid":        snap_uid,
            "iri":        snap_iri,
            "uri":        snap_iri,
            "version":    schema_ver,
            "created_at": now_iso(),
            "sl":  source_label,
            "yp":  yml_path,
            "cc":  len(classes),
            "pc":  len(used_slots),
            "rc":  0,
            "cs":  changes_summary,
            "rv":  registry_version,
        })
        stats["schema_version"] = schema_ver

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--file",    default=None,
              help="Path to a specific .yml file. Default: all schemas/*.yml")
@click.option("--db",      default=DB_PATH, show_default=True)
@click.option("--dry-run", is_flag=True,
              help="Parse and count without writing to DB.")
@click.option("--wipe",    is_flag=True,
              help="Delete existing nodes for this source before re-ingesting.")
@click.option("--registry-version", default="",
              help="Registry semver to stamp on created nodes.")
@click.option("--issue",   default="", help="GitHub issue number (for provenance).")
@click.option("--agent",   default="anonymous", help="Who submitted this schema.")
def cli(file, db, dry_run, wipe, registry_version, issue, agent) -> None:
    """
    Ingest one or more LinkML .yml schemas into the NeuroGhost graph.

    Examples:
      python ingest_linkml.py --file schemas/bbqs.yml
      python ingest_linkml.py --file schemas/bids.yml --dry-run
      python ingest_linkml.py --wipe --file schemas/nwb.yml
    """
    conn = get_connection(db)

    if file:
        files = [Path(file)]
    else:
        schemas_dir = Path("schemas")
        if not schemas_dir.exists():
            click.echo("No schemas/ directory. Use --file or create schemas/.")
            return
        files = sorted(schemas_dir.glob("*.yml"))
        if not files:
            click.echo("No .yml files in schemas/")
            return

    for path in files:
        click.echo(f"\nParsing {path} …")
        try:
            parsed = parse_linkml(path)
        except Exception as e:
            click.echo(f"  ERROR parsing {path}: {e}")
            continue

        source_label = parsed["meta"]["name"]
        click.echo(f"  Schema: {source_label} v{parsed['meta']['version']} "
                   f"({len(parsed['classes'])} classes, {len(parsed['slots'])} slots)")

        if wipe and not dry_run:
            click.echo(f"  Wiping existing nodes for '{source_label}' …")
            conn.execute(
                "MATCH (n:RegistryClass {source_label: $src}) DETACH DELETE n",
                {"src": source_label}
            )
            conn.execute(
                "MATCH (n:RegistryProperty {source_label: $src}) DETACH DELETE n",
                {"src": source_label}
            )

        stats = insert_schema(
            conn, parsed, source_label,
            dry_run=dry_run,
            registry_version=registry_version,
            yml_path=str(path),
        )

        prefix = "[dry-run]" if dry_run else "Result:"
        click.echo(
            f"  {prefix} "
            f"+{stats.get('classes_created',0)} classes, "
            f"~{stats.get('classes_updated',0)} updated, "
            f"={stats.get('classes_unchanged',0)} unchanged | "
            f"+{stats.get('properties_created',0)} props, "
            f"~{stats.get('properties_updated',0)} updated, "
            f"={stats.get('properties_unchanged',0)} unchanged"
        )
        if stats.get("schema_version"):
            click.echo(f"  Schema version: {stats['schema_version']}")
        if stats.get("schema_unchanged"):
            click.echo(f"  Schema unchanged — no snapshot created.")

        if not dry_run:
            nc = conn.execute("MATCH (n:RegistryClass) RETURN count(n)").get_next()[0]
            np = conn.execute("MATCH (n:RegistryProperty) RETURN count(n)").get_next()[0]
            ni = conn.execute("MATCH (n:SemanticIdentity) RETURN count(n)").get_next()[0]
            click.echo(f"  Registry: {nc} classes, {np} properties, {ni} identities")


if __name__ == "__main__":
    cli()
