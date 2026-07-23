"""
export_json.py — Export registry snapshot + provenance to data/
---------------------------------------------------------------
Runs after every ingest/align cycle. Produces:

  data/registry.json          — latest snapshot (frontend reads this)
  data/versions/{ver}.json    — archived snapshot for this registry version
  data/provenance.json        — append-only log of every registry version

Usage:
    python export_json.py
    python export_json.py --db ./registry.lbug --bump minor
    python export_json.py --issue 3 --agent sulimansharif --bump minor
"""

from __future__ import annotations
import json, shutil
from pathlib import Path

import click

from db import (
    get_connection, now_iso,
    current_registry_version, next_registry_version, append_provenance,
    PROVENANCE_PATH,
)

DATA_DIR = Path("data")
DB_PATH  = "./registry.lbug"


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------

def export_snapshot(conn, registry_version: str) -> dict:
    # ---- sources -----------------------------------------------------------
    src_rows = conn.execute(
        "MATCH (s:SchemaSource) RETURN s.uid, s.label, s.version"
    ).get_all()

    sources = []
    for _, label, ver in src_rows:
        count = conn.execute(
            "MATCH (n:RegistryClass {source_label: $src}) RETURN count(n)",
            {"src": label}
        ).get_next()[0]
        sources.append({"label": label, "version": ver or "1.0.0",
                        "class_count": count})

    # ---- classes -----------------------------------------------------------
    cls_rows = conn.execute("""
        MATCH (n:RegistryClass)
        RETURN n.hash_id, n.iri, n.name, n.definition,
               n.is_abstract, n.source_label, n.registry_version
        ORDER BY n.source_label, n.name
    """).get_all()

    classes = []
    for row in cls_rows:
        hash_id, iri, name, defn, is_abstract, source, reg_ver = row

        props = conn.execute("""
            MATCH (c:RegistryClass {hash_id: $hash_id})-[:HAS_PROPERTY]->(p:RegistryProperty)
            RETURN p.hash_id, p.iri, p.name, p.definition,
                   p.value_range, p.multivalued, p.required, p.source_label
            ORDER BY p.name
        """, {"hash_id": hash_id}).get_all()

        subclass_of = [
            r[0] for r in conn.execute("""
                MATCH (c:RegistryClass {hash_id: $hash_id})-[:SUBCLASS_OF]->(p:RegistryClass)
                RETURN p.iri
            """, {"hash_id": hash_id}).get_all() if r[0]
        ]

        align_rows = conn.execute("""
            MATCH (c:RegistryClass {hash_id: $hash_id})-[a:ALIGNED_TO]->(t:RegistryClass)
            RETURN t.hash_id, t.name, t.iri, t.source_label,
                   a.distance, a.method,
                   a.score_iri, a.score_name, a.score_desc, a.score_slot
            ORDER BY a.distance
        """, {"hash_id": hash_id}).get_all()

        classes.append({
            "hash_id":          hash_id,
            "iri":              iri or "",
            "name":             name or "",
            "definition":       defn or "",
            "registry_version": reg_ver or "",
            "is_abstract":      bool(is_abstract),
            "source":           source or "",
            "properties": [
                {
                    "hash_id":     r[0],
                    "iri":         r[1] or "",
                    "name":        r[2] or "",
                    "definition":  r[3] or "",
                    "value_range": r[4] or "",
                    "multivalued": bool(r[5]),
                    "required":    bool(r[6]),
                    "source":      r[7] or "",
                }
                for r in props
            ],
            "subclass_of": subclass_of,
            "alignments": [
                {
                    "target_hash_id": r[0],
                    "target_name":    r[1] or "",
                    "target_iri":     r[2] or "",
                    "target_source":  r[3] or "",
                    "distance":       float(r[4]) if r[4] is not None else 1.0,
                    "method":         r[5] or "",
                    "scores": {
                        "iri":  float(r[6]) if r[6] is not None else 0.0,
                        "name": float(r[7]) if r[7] is not None else 0.0,
                        "desc": float(r[8]) if r[8] is not None else 0.0,
                        "slot": float(r[9]) if r[9] is not None else 0.0,
                    }
                }
                for r in align_rows
            ],
        })

    return {
        "registry_version": registry_version,
        "generated_at":     now_iso(),
        "sources":          sources,
        "classes":          classes,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--db",     default=DB_PATH, show_default=True)
@click.option("--bump",   default="minor",
              type=click.Choice(["major", "minor", "patch"]),
              help="Version bump type. major=breaking, minor=new schema, patch=update.")
@click.option("--issue",  default="", help="GitHub issue number that triggered this.")
@click.option("--agent",  default="github-actions", help="Who triggered this.")
@click.option("--schema", default="", help="Schema name that was ingested.")
def cli(db: str, bump: str, issue: str, agent: str, schema: str) -> None:
    """Export registry snapshot, archive version, append provenance."""
    conn = get_connection(db)

    # Compute new registry version
    current = current_registry_version()
    new_ver  = next_registry_version(current, bump) if current != "0.0.0" else "1.0.0"
    click.echo(f"Registry version: {current} → {new_ver}")

    # Build snapshot
    snapshot = export_snapshot(conn, new_ver)
    nc = len(snapshot["classes"])
    ns = len(snapshot["sources"])

    # Write data/registry.json (latest)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    latest = DATA_DIR / "registry.json"
    latest.write_text(json.dumps(snapshot, indent=2))
    click.echo(f"Wrote {latest}  ({nc} classes, {ns} sources)")

    # Archive to data/versions/{ver}.json
    versions_dir = DATA_DIR / "versions"
    versions_dir.mkdir(exist_ok=True)
    archive = versions_dir / f"{new_ver}.json"
    shutil.copy(latest, archive)
    click.echo(f"Archived → {archive}")

    # Count changes vs previous version
    prev_path = DATA_DIR / "registry.json"
    classes_added = nc  # simplified — full diff would compare to previous snapshot

    # Append to provenance.json
    prov_entry = {
        "registry_version": new_ver,
        "previous_version": current,
        "timestamp":        now_iso(),
        "bump":             bump,
        "trigger":          "issue" if issue else "manual",
        "issue_number":     issue,
        "agent":            agent,
        "schema_ingested":  schema,
        "stats": {
            "classes_total":  nc,
            "sources_total":  ns,
        },
        "archive_path": f"data/versions/{new_ver}.json",
    }
    append_provenance(prov_entry)
    click.echo(f"Appended provenance entry for v{new_ver}")


if __name__ == "__main__":
    cli()
