"""
seed.py — Populate the SenseIn Schema Registry from schema.org

Fetches schema.org's machine-readable JSON-LD and inserts the core type
hierarchy as SchemaClass + SchemaProperty nodes using the same make_base
identity pattern as the main registry.

Seeded types (top-level schema.org hierarchy):
  Thing → CreativeWork, Event, Organization, Person, Place,
           Product, Action, MedicalEntity
  + AudioObject, ImageObject, VideoObject (embedded media)

Each class gets its full set of schema.org properties as SchemaProperty
nodes linked via HAS_PROPERTY.

Usage:
    python seed.py               # inserts into ./registry.lbug
    python seed.py --dry-run     # prints counts, writes nothing
    python seed.py --wipe        # drop + re-seed (use with care)

The script is idempotent: it checks whether Thing already exists before
inserting anything.
"""

from __future__ import annotations
import click, httpx, rdflib

from db import get_connection, make_base, make_uid, now_iso, REG

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCHEMA_ORG_JSONLD = (
    "https://schema.org/version/latest/schemaorg-current-https.jsonld"
)

SCHEMA  = rdflib.Namespace("https://schema.org/")
RDFS    = rdflib.RDFS
RDF     = rdflib.RDF

# Curated type list — explicit rather than BFS so we don't pull in
# ComedyEvent, AMRadioChannel, and 900 other irrelevant schema.org types.
#
# Core identity
SEED_ROOTS_CORE = [
    "Thing", "Person", "Organization",
]

# Bio / neuro — MedicalEntity and BioChemEntity subtrees are fully relevant
SEED_ROOTS_BIO = [
    "MedicalEntity",
    "AnatomicalStructure", "BrainStructure", "Nerve", "AnatomicalSystem",
    "MedicalCondition", "InfectiousDisease", "MedicalSignOrSymptom",
    "MedicalStudy", "MedicalObservationalStudy", "MedicalTrial",
    "MedicalProcedure", "DiagnosticProcedure", "TherapeuticProcedure",
    "MedicalTest", "ImagingTest", "BloodTest",
    "MedicalDevice", "Drug", "Substance",
    "BioChemEntity", "Gene", "Protein", "MolecularEntity", "ChemicalSubstance",
]

# Research output
SEED_ROOTS_RESEARCH = [
    "CreativeWork", "Article", "ScholarlyArticle", "Dataset",
    "SoftwareApplication", "SoftwareSourceCode",
]

# Supporting
SEED_ROOTS_SUPPORTING = [
    "Event", "ConferenceEvent", "EducationEvent",
    "Place",
]

SEED_ROOTS = (
    SEED_ROOTS_CORE
    + SEED_ROOTS_BIO
    + SEED_ROOTS_RESEARCH
    + SEED_ROOTS_SUPPORTING
)

# Helpers imported from db.py

# ---------------------------------------------------------------------------
# Fetch + parse schema.org
# ---------------------------------------------------------------------------

def fetch_schema_graph() -> rdflib.Graph:
    print("Fetching schema.org JSON-LD …")
    resp = httpx.get(SCHEMA_ORG_JSONLD, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    g = rdflib.Graph()
    g.parse(data=resp.text, format="json-ld")
    print(f"  Parsed {len(g)} triples.")
    return g


def collect_classes(g: rdflib.Graph) -> dict[str, dict]:
    """
    Return a dict keyed by short name (e.g. "Person") with:
      iri, label, comment, subclass_of (list of short names), props (list)
    Only includes types in SEED_ROOTS — no BFS expansion.
    """
    wanted: set[str] = set(SEED_ROOTS)

    classes: dict[str, dict] = {}
    for name in wanted:
        node    = SCHEMA[name]
        label   = str(next(g.objects(node, RDFS.label),   name))
        comment = str(next(g.objects(node, RDFS.comment), ""))
        parents = [
            str(p).replace("https://schema.org/", "")
            for p in g.objects(node, RDFS.subClassOf)
            if str(p).startswith("https://schema.org/")
        ]
        classes[name] = {
            "iri":         str(node),
            "label":       label,
            "comment":     comment,
            "subclass_of": parents,
            "props":       [],   # filled below
        }

    # Attach properties (schema:domainIncludes links prop → class)
    for prop_node in g.subjects(RDF.type, RDF.Property):
        short_prop = str(prop_node).replace("https://schema.org/", "")
        prop_label = str(next(g.objects(prop_node, RDFS.label),   short_prop))
        prop_comment = str(next(g.objects(prop_node, RDFS.comment), ""))
        ranges = [
            str(r) for r in g.objects(prop_node, SCHEMA.rangeIncludes)
        ]
        for domain in g.objects(prop_node, SCHEMA.domainIncludes):
            short_domain = str(domain).replace("https://schema.org/", "")
            if short_domain in classes:
                classes[short_domain]["props"].append({
                    "name":    short_prop,
                    "iri":     str(prop_node),
                    "label":   prop_label,
                    "comment": prop_comment,
                    "ranges":  ranges,
                })
    return classes

# ---------------------------------------------------------------------------
# Insert into LadybugDB
# ---------------------------------------------------------------------------

def seed(db_path: str = "./registry.lbug",
         dry_run: bool = False,
         wipe: bool = False,
         registry_version: str = "1.0.0") -> None:

    conn = get_connection(db_path)

    if wipe and not dry_run:
        print("Wiping existing SchemaClass and SchemaProperty nodes …")
        conn.execute("MATCH (n:SchemaClass) DETACH DELETE n")
        conn.execute("MATCH (n:SchemaProperty) DETACH DELETE n")

    # Idempotency check
    if not dry_run and not wipe:
        r = conn.execute(
            "MATCH (n:SchemaClass {iri: $iri}) RETURN n.uid LIMIT 1",
            {"iri": "https://schema.org/Thing"}
        )
        if r.has_next():
            print("schema.org seed already present — skipping. "
                  "Use --wipe to re-seed.")
            return

    g = fetch_schema_graph()
    classes = collect_classes(g)

    print(f"Inserting {len(classes)} classes …")
    class_uid: dict[str, str] = {}   # short_name → uid

    # Pass 1: insert all class nodes
    # LadybugDB requires uid (PK) in the MATCH clause of MERGE, so we do a
    # manual existence check + CREATE instead.
    for name, info in classes.items():
        b = make_base(name, iri=info["iri"])
        class_uid[name] = b["uid"]
        if dry_run:
            continue
        exists = conn.execute(
            "MATCH (n:SchemaClass {iri: $iri}) RETURN n.uid LIMIT 1",
            {"iri": info["iri"]}
        ).has_next()
        if exists:
            continue
        conn.execute("""
            CREATE (:SchemaClass {
                uid:           $uid,
                iri:           $iri,
                uri:           $uri,
                version:       $version,
                created_at:    $created_at,
                name:          $name,
                definition:    $definition,
                abstract:      false,
                source_label:  'schema.org'
            })
        """, {**b,
              "name":       info["label"],
              "definition": info["comment"]})

    # Pass 2: subclass relationships
    print("Linking subclass relationships …")
    for name, info in classes.items():
        for parent_name in info["subclass_of"]:
            if parent_name not in classes:
                continue
            if dry_run:
                continue
            conn.execute("""
                MATCH (c:SchemaClass {iri: $ciri}),
                      (p:SchemaClass {iri: $piri})
                MERGE (c)-[:SUBCLASS_OF]->(p)
            """, {"ciri": info["iri"],
                  "piri": f"https://schema.org/{parent_name}"})

    # Pass 3: properties
    total_props = sum(len(info["props"]) for info in classes.values())
    print(f"Inserting {total_props} properties …")
    seen_props: set[str] = set()   # avoid duplicate SchemaProperty nodes

    for name, info in classes.items():
        for prop in info["props"]:
            prop_key = prop["iri"]

            if dry_run:
                continue

            # Insert the property node once per unique IRI
            if prop_key not in seen_props:
                seen_props.add(prop_key)
                b = make_base(prop["name"], iri=prop["iri"])
                range_uri = prop["ranges"][0] if prop["ranges"] else ""
                exists = conn.execute(
                    "MATCH (p:SchemaProperty {iri: $iri}) RETURN p.uid LIMIT 1",
                    {"iri": prop["iri"]}
                ).has_next()
                if not exists:
                    conn.execute("""
                        CREATE (:SchemaProperty {
                            uid:          $uid,
                            iri:          $iri,
                            uri:          $uri,
                            version:      $version,
                            created_at:   $created_at,
                            name:         $name,
                            definition:   $definition,
                            datatype:     $datatype,
                            range_uri:    $range_uri,
                            multivalued:  false,
                            required:     false,
                            source_label: 'schema.org'
                        })
                    """, {**b,
                          "name":       prop["label"],
                          "definition": prop["comment"],
                          "datatype":   "xsd:string",
                          "range_uri":  range_uri})

            # Link class → property
            conn.execute("""
                MATCH (c:SchemaClass  {iri: $ciri}),
                      (p:SchemaProperty {iri: $piri})
                MERGE (c)-[:HAS_PROPERTY]->(p)
            """, {"ciri": info["iri"], "piri": prop_key})

    if dry_run:
        print(f"\n[dry-run] Would insert:")
        print(f"  {len(classes)} SchemaClass nodes")
        print(f"  {total_props} SchemaProperty nodes (deduplicated)")
        for name, info in sorted(classes.items())[:12]:
            print(f"    {name}: {len(info['props'])} props, "
                  f"parents={info['subclass_of']}")
        print("  … (showing first 12)")
    else:
        # Summary
        nc = conn.execute("MATCH (n:SchemaClass) RETURN count(n)").get_next()[0]
        np = conn.execute("MATCH (n:SchemaProperty) RETURN count(n)").get_next()[0]
        print(f"\nDone. Registry now has {nc} classes, {np} properties.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--db",      default="./registry.lbug", show_default=True,
              help="Path to LadybugDB file.")
@click.option("--dry-run", is_flag=True,
              help="Print what would be inserted without writing.")
@click.option("--wipe",    is_flag=True,
              help="Delete existing classes/properties before seeding.")
def cli(db: str, dry_run: bool, wipe: bool) -> None:
    """Seed the SenseIn Schema Registry from schema.org."""
    seed(db_path=db, dry_run=dry_run, wipe=wipe)

if __name__ == "__main__":
    cli()
