"""
align.py — Semantic alignment between schema sources
-----------------------------------------------------
Computes ALIGNED_TO edges between SchemaClass nodes across sources.

Distance function:
  distance = 1 - (
      W_IRI  * score_iri   +   # 0.6  — exact IRI match (binary)
      W_NAME * score_name  +   # 0.15 — semantic name similarity (embeddings)
      W_DESC * score_desc  +   # 0.25 — semantic definition similarity (embeddings)
      W_SLOT * score_slot      # 0.0  — slot Jaccard (stubbed)
  )

Semantic similarity uses sentence-transformers all-MiniLM-L6-v2 (~80MB,
CPU-friendly). Falls back to difflib if sentence-transformers not installed.

All four raw subscores are stored on the ALIGNED_TO edge so the frontend
weight slider can recompute distance client-side without a new DB query.

Usage:
    python align.py
    python align.py --source bbqs
    python align.py --threshold 0.5
    python align.py --dry-run
"""

from __future__ import annotations
import difflib
from itertools import combinations
from typing import Iterator

import click

from db import get_connection

# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------

W_IRI   = 0.6
W_NAME  = 0.15
W_DESC  = 0.25
W_SLOT  = 0.0

DB_PATH = "./registry.lbug"

# ---------------------------------------------------------------------------
# Embedding model (lazy load)
# ---------------------------------------------------------------------------

_model = None

def _get_model():
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer("all-MiniLM-L6-v2")
            click.echo("  Loaded sentence-transformers all-MiniLM-L6-v2")
        except Exception as e:
            _model = "fallback"
            click.echo(f"  Could not load sentence-transformers ({type(e).__name__}) — "
                       "falling back to difflib for name/desc similarity.")
            click.echo("  Fix: pip install 'sentence-transformers>=2.7.0,<3.0.0'")
            click.echo("       or:  brew install ffmpeg")
    return _model


def _embed_similarity(text_a: str, text_b: str) -> float:
    """Cosine similarity between two texts via embeddings or difflib fallback."""
    if not text_a or not text_b:
        return 0.0
    model = _get_model()
    if model == "fallback":
        return difflib.SequenceMatcher(None,
                                       text_a.lower(),
                                       text_b.lower()).ratio()
    import numpy as np
    embs = model.encode([text_a, text_b], normalize_embeddings=True)
    return float(np.dot(embs[0], embs[1]))


# ---------------------------------------------------------------------------
# Signal functions
# ---------------------------------------------------------------------------

def _score_iri(iri_a: str, iri_b: str) -> float:
    if not iri_a or not iri_b:
        return 0.0
    return 1.0 if iri_a.rstrip("/") == iri_b.rstrip("/") else 0.0


def _score_name(name_a: str, name_b: str) -> float:
    return _embed_similarity(name_a, name_b)


def _score_desc(desc_a: str, desc_b: str) -> float:
    return _embed_similarity(desc_a, desc_b)


def _score_slot(slots_a: set[str], slots_b: set[str]) -> float:
    """Jaccard overlap — stubbed at W=0 until scientists spec it."""
    if not slots_a and not slots_b:
        return 0.0
    union = slots_a | slots_b
    return len(slots_a & slots_b) / len(union) if union else 0.0


# ---------------------------------------------------------------------------
# Compute
# ---------------------------------------------------------------------------

def compute_distance(a: dict, b: dict) -> tuple[float, str, dict]:
    """
    Returns (distance, method, subscores).
    subscores = {iri, name, desc, slot} — stored on edge for UI weight slider.
    """
    s_iri  = _score_iri(a["iri"],        b["iri"])
    s_name = _score_name(a["name"],      b["name"])
    s_desc = _score_desc(a["definition"],b["definition"])
    s_slot = _score_slot(set(a.get("slot_iris", [])),
                         set(b.get("slot_iris", [])))

    total_w = W_IRI + W_NAME + W_DESC + W_SLOT
    if total_w == 0:
        return 1.0, "none", {"iri": 0.0, "name": 0.0, "desc": 0.0, "slot": 0.0}

    similarity = (
        W_IRI  * s_iri  +
        W_NAME * s_name +
        W_DESC * s_desc +
        W_SLOT * s_slot
    ) / total_w

    distance = round(1.0 - similarity, 6)

    # Dominant method — most informative signal
    if s_iri == 1.0:
        method = "iri"
    elif s_desc > 0.75:
        method = "semantic-desc"
    elif s_name > 0.75:
        method = "semantic-name"
    else:
        method = "composite"

    subscores = {
        "iri":  round(s_iri,  6),
        "name": round(s_name, 6),
        "desc": round(s_desc, 6),
        "slot": round(s_slot, 6),
    }
    return distance, method, subscores


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def load_classes(conn, source_label: str | None = None) -> list[dict]:
    if source_label:
        rows = conn.execute("""
            MATCH (n:SchemaClass {source_label: $src})
            RETURN n.uid, n.iri, n.name, n.definition, n.source_label
        """, {"src": source_label}).get_all()
    else:
        rows = conn.execute("""
            MATCH (n:SchemaClass)
            RETURN n.uid, n.iri, n.name, n.definition, n.source_label
        """).get_all()

    classes = []
    for uid, iri, name, defn, src in rows:
        slot_iris = [
            r[0] for r in conn.execute("""
                MATCH (c:SchemaClass {uid: $uid})-[:HAS_PROPERTY]->(p:SchemaProperty)
                RETURN p.iri
            """, {"uid": uid}).get_all()
        ]
        classes.append({
            "uid":        uid,
            "iri":        iri  or "",
            "name":       name or "",
            "definition": defn or "",
            "source":     src  or "",
            "slot_iris":  slot_iris,
        })
    return classes


def pairs_across_sources(
    classes: list[dict], source_a: str | None
) -> Iterator[tuple[dict, dict]]:
    sources = {c["source"] for c in classes}
    if len(sources) < 2:
        return
    if source_a:
        ga = [c for c in classes if c["source"] == source_a]
        gb = [c for c in classes if c["source"] != source_a]
        for a in ga:
            for b in gb:
                yield a, b
    else:
        by_src: dict[str, list] = {}
        for c in classes:
            by_src.setdefault(c["source"], []).append(c)
        for s1, s2 in combinations(list(by_src), 2):
            for a in by_src[s1]:
                for b in by_src[s2]:
                    yield a, b


def write_alignment(conn, uid_a: str, uid_b: str, distance: float,
                    method: str, subscores: dict,
                    registry_version: str = "") -> None:
    # Remove stale edge
    conn.execute("""
        MATCH (a:SchemaClass {uid: $ua})-[r:ALIGNED_TO]->(b:SchemaClass {uid: $ub})
        DELETE r
    """, {"ua": uid_a, "ub": uid_b})
    conn.execute("""
        MATCH (a:SchemaClass {uid: $ua}), (b:SchemaClass {uid: $ub})
        CREATE (a)-[:ALIGNED_TO {
            distance: $d, method: $m,
            score_iri: $si, score_name: $sn,
            score_desc: $sd, score_slot: $ss,
            registry_version: $rv
        }]->(b)
    """, {
        "ua": uid_a, "ub": uid_b,
        "d":  distance, "m": method,
        "si": subscores["iri"],  "sn": subscores["name"],
        "sd": subscores["desc"], "ss": subscores["slot"],
        "rv": registry_version,
    })


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--source",           default=None,
              help="Align this source against all others.")
@click.option("--db",               default=DB_PATH, show_default=True)
@click.option("--threshold",        default=0.7, show_default=True, type=float,
              help="Only write edges with distance <= threshold.")
@click.option("--registry-version", default="", help="Registry version to stamp on edges.")
@click.option("--dry-run",          is_flag=True)
def cli(source, db, threshold, registry_version, dry_run):
    """Compute semantic alignment between SchemaClass nodes across sources."""
    conn    = get_connection(db)
    classes = load_classes(conn)

    if not classes:
        click.echo("No classes found. Run seed.py and ingest_linkml.py first.")
        return

    sources = {c["source"] for c in classes}
    click.echo(f"Loaded {len(classes)} classes from "
               f"{len(sources)} sources: {', '.join(sorted(sources))}")

    # Pre-load model once before the loop
    if W_NAME > 0 or W_DESC > 0:
        _get_model()

    written = skipped = exact = 0

    for a, b in pairs_across_sources(classes, source):
        distance, method, subscores = compute_distance(a, b)

        if distance > threshold:
            skipped += 1
            continue

        if distance == 0.0:
            exact += 1

        if dry_run:
            click.echo(
                f"  [{method}] {a['source']}:{a['name']} ↔ "
                f"{b['source']}:{b['name']}  "
                f"d={distance:.4f}  "
                f"(iri={subscores['iri']:.2f} "
                f"name={subscores['name']:.2f} "
                f"desc={subscores['desc']:.2f})"
            )
        else:
            write_alignment(conn, a["uid"], b["uid"],
                            distance, method, subscores, registry_version)
        written += 1

    action = "Would write" if dry_run else "Wrote"
    click.echo(f"\n{action} {written} ALIGNED_TO edges "
               f"({exact} exact IRI, {skipped} above threshold={threshold}).")


if __name__ == "__main__":
    cli()
