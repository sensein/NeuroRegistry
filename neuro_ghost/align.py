"""
align.py — Compute semantic alignment between schema sources
============================================================

WHY THIS FILE EXISTS
--------------------
After ingesting multiple schemas (BIDS, NWB, DANDI, BBQS, etc.), we have
thousands of classes and properties living in the graph — but they don't
know about each other yet. A BIDS "subject" and a DANDI "Participant" are
clearly related, but the graph has no edge saying so.

This file computes those relationships and writes them into the graph as
ALIGNED_TO edges, each carrying:
  - distance    : 0.0 (identical) to 1.0 (completely unrelated)
  - skos_relation: the W3C SKOS vocabulary term for the relationship type
  - score_iri   : contribution from IRI matching
  - score_name  : contribution from name similarity
  - score_desc  : contribution from definition similarity
  - score_slot  : contribution from shared properties (stub)

WHY SKOS?
---------
SKOS (Simple Knowledge Organization System) is a W3C standard vocabulary
for describing relationships between concepts in different vocabularies.
It gives us a shared language for saying:
  skos:exactMatch   → these two concepts ARE the same thing
  skos:closeMatch   → these are very similar but not identical
  skos:broadMatch   → concept A is broader/more general than concept B
  skos:narrowMatch  → concept A is narrower/more specific than concept B
  skos:relatedMatch → related but neither broader nor narrower

This makes our alignment data interoperable with any tool that speaks SKOS.

HOW THE DISTANCE FUNCTION WORKS
--------------------------------
We combine four signals into a single distance score:

  1. IRI match (weight 0.6)
     If two classes explicitly point to the same ontology IRI
     (e.g. both say class_uri: schema:Person), they are definitionally
     the same concept. This is the strongest possible signal.
     Score: 1.0 if IRIs match, 0.0 otherwise (binary).

  2. Name similarity (weight 0.15)
     How semantically similar are the class names?
     Uses sentence-transformers to encode both names as vectors, then
     computes cosine similarity. "Investigator" and "Researcher" score
     high because their embedding vectors are close in semantic space.
     Falls back to difflib if sentence-transformers is unavailable.

  3. Definition similarity (weight 0.25)
     How semantically similar are the plain-English descriptions?
     Same embedding approach as name, but on the longer definition text.
     More information = more reliable signal.

  4. Slot/property Jaccard (weight 0.0 — stubbed)
     What fraction of properties do the two classes share?
     Stubbed at weight=0 until scientists specify how to weight it.
     The function exists and is wired up — just turn up the weight when ready.

The weights are chosen so IRI match dominates. If two classes share an IRI,
no amount of name/definition dissimilarity can push them apart. The semantic
signals (name + definition) are there to catch cases where schemas don't
declare explicit IRI anchors.

EMBEDDING CACHE (PARQUET)
--------------------------
Computing embeddings is slow (~1-2 seconds per class on CPU). With hundreds
of classes, re-computing every time we run alignment would be painful.

Solution: we cache embeddings in data/embeddings.parquet.
  - First run: compute embeddings, save to parquet
  - Subsequent runs: load from parquet, only compute new ones

The parquet file is committed to the repo so CI doesn't need to recompute
from scratch on every submission.

PAIRS ACROSS SOURCES
---------------------
We only compare classes from DIFFERENT sources. Comparing BIDS to BIDS
doesn't tell us anything new. The function pairs_across_sources() generates
all cross-source pairs efficiently.

USAGE
-----
  python align.py --source bbqs           # align bbqs against all others
  python align.py                          # align all source pairs
  python align.py --dry-run               # print pairs without writing
  python align.py --threshold 0.5         # only write edges with d <= 0.5
  python align.py --min-signal 0.4        # skip truly unrelated pairs
"""

from __future__ import annotations
import difflib
from itertools import combinations
from pathlib import Path
from typing import Iterator

import click

from db import get_connection, skos_relation as compute_skos_relation

# ---------------------------------------------------------------------------
# Distance weights
# ---------------------------------------------------------------------------
# These are the weights used to combine the four signals into one score.
# They must sum to 1.0 (or we normalise by their sum, as we do below).
#
# Current rationale:
#   IRI match is the gold standard — if both classes say "I am schema:Person"
#   then they ARE the same concept by definition. Weight = 0.6.
#
#   Definition similarity captures semantic meaning from natural language.
#   Definitions are longer than names, so they carry more signal. Weight = 0.25.
#
#   Name similarity is useful for cases with no IRI anchor and terse names.
#   Weight = 0.15.
#
#   Slot Jaccard is stubbed — scientists are specifying how to handle it.
#   When ready, reduce IRI/name/desc weights proportionally to make room.

W_IRI   = 0.60
W_NAME  = 0.15
W_DESC  = 0.25
W_SLOT  = 0.00   # stubbed — increase when scientists spec this

DB_PATH        = "./registry.lbug"
EMBEDDINGS_PATH = Path("data/embeddings.parquet")

# ---------------------------------------------------------------------------
# Embedding model (lazy-loaded, cached globally)
# ---------------------------------------------------------------------------
# We use "all-MiniLM-L6-v2" — a lightweight but powerful sentence embedding
# model that maps text to a 384-dimensional vector space. It was trained on
# over a billion sentence pairs and understands domain concepts well.
#
# "Lazy loading" means we don't import sentence_transformers at module level —
# we wait until the first time we actually need it. This means the module
# can be imported even if sentence_transformers isn't installed, and the
# fallback (difflib) will kick in instead.

_model = None

def _get_model():
    """
    Load the sentence-transformers model, or fall back to difflib.

    Returns either the loaded SentenceTransformer model, or the string
    "fallback" to signal that difflib should be used instead.

    The fallback is triggered by:
      - sentence-transformers not installed (ImportError)
      - torchcodec/FFmpeg dependency missing (RuntimeError on newer versions)
      - any other import failure

    Recommendation to avoid the fallback:
      pip install "sentence-transformers>=2.7.0,<3.0.0"
    """
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer("all-MiniLM-L6-v2")
            click.echo("  Loaded sentence-transformers all-MiniLM-L6-v2")
        except Exception as e:
            _model = "fallback"
            click.echo(
                f"  Could not load sentence-transformers ({type(e).__name__}) — "
                "falling back to difflib.\n"
                "  Fix: pip install 'sentence-transformers>=2.7.0,<3.0.0'"
            )
    return _model


# ---------------------------------------------------------------------------
# Embedding cache (parquet)
# ---------------------------------------------------------------------------

def _load_embedding_cache() -> dict[str, list[float]]:
    """
    Load pre-computed text embeddings from parquet, if the file exists.

    The cache maps text → embedding vector (list of 384 floats).
    Keys are the exact text strings that were embedded.

    Returns an empty dict if no cache exists yet.
    """
    if not EMBEDDINGS_PATH.exists():
        return {}
    try:
        import pandas as pd
        df = pd.read_parquet(EMBEDDINGS_PATH)
        # Columns: "text", "embedding" (list of floats stored as object)
        return {row["text"]: row["embedding"] for _, row in df.iterrows()}
    except Exception as e:
        click.echo(f"  WARNING: could not load embedding cache — {e}")
        return {}


def _save_embedding_cache(cache: dict[str, list[float]]) -> None:
    """
    Save the embedding cache to parquet.
    Called after computing new embeddings so they're available next run.
    """
    try:
        import pandas as pd
        EMBEDDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame([
            {"text": text, "embedding": emb}
            for text, emb in cache.items()
        ])
        df.to_parquet(EMBEDDINGS_PATH, index=False)
        click.echo(f"  Saved {len(cache)} embeddings → {EMBEDDINGS_PATH}")
    except Exception as e:
        click.echo(f"  WARNING: could not save embedding cache — {e}")


# Module-level cache — loaded once per run
_embedding_cache: dict[str, list[float]] = {}
_cache_dirty = False   # track if we need to re-save


def _embed(text: str) -> list[float] | None:
    """
    Get the embedding vector for a text string.

    First checks the in-memory cache (populated from parquet on first call).
    If not cached, computes using the model and adds to cache.
    If model is fallback, returns None (difflib will be used instead).

    An embedding vector is a list of 384 floats. Two texts with similar
    meanings will have vectors that are close in this 384-dimensional space,
    measured by cosine similarity.
    """
    global _embedding_cache, _cache_dirty
    if not _embedding_cache:
        _embedding_cache = _load_embedding_cache()

    if not text or not text.strip():
        return None

    if text in _embedding_cache:
        return _embedding_cache[text]

    model = _get_model()
    if model == "fallback":
        return None

    # Compute the embedding
    emb = model.encode([text], normalize_embeddings=True)[0].tolist()
    _embedding_cache[text] = emb
    _cache_dirty = True
    return emb


# ---------------------------------------------------------------------------
# Signal functions
# ---------------------------------------------------------------------------

def _score_iri(iri_a: str, iri_b: str) -> float:
    """
    Binary IRI match score.

    1.0 if both IRIs are non-empty and identical (ignoring trailing slashes).
    0.0 otherwise.

    Why binary? If two schemas explicitly declare the same ontology IRI,
    there is no ambiguity — they are the same concept. No partial credit.
    """
    if not iri_a or not iri_b:
        return 0.0
    return 1.0 if iri_a.rstrip("/") == iri_b.rstrip("/") else 0.0


def _score_semantic(text_a: str, text_b: str) -> float:
    """
    Semantic similarity between two text strings.

    Tries embedding-based cosine similarity first (range 0.0–1.0).
    Falls back to difflib sequence matching if embeddings unavailable.

    Cosine similarity of normalized embeddings = dot product of the vectors.
    A score of 1.0 means the texts are semantically identical.
    A score of 0.0 means they have no semantic overlap.
    """
    if not text_a or not text_b:
        return 0.0

    emb_a = _embed(text_a)
    emb_b = _embed(text_b)

    if emb_a is not None and emb_b is not None:
        # Cosine similarity = dot product (vectors are already normalized)
        import numpy as np
        return float(np.dot(emb_a, emb_b))

    # Difflib fallback: ratio() returns fraction of matching characters
    return difflib.SequenceMatcher(None, text_a.lower(), text_b.lower()).ratio()


def _score_slot(slots_a: set[str], slots_b: set[str]) -> float:
    """
    Jaccard similarity between two sets of property IRIs.

    Jaccard = |intersection| / |union|
    Range 0.0–1.0. 1.0 means identical property sets.

    Currently weight=0 (stubbed) — included for completeness and future use.
    When scientists specify how to incorporate property overlap into alignment,
    increase W_SLOT and decrease other weights proportionally.
    """
    if not slots_a and not slots_b:
        return 0.0
    union = slots_a | slots_b
    if not union:
        return 0.0
    return len(slots_a & slots_b) / len(union)


# ---------------------------------------------------------------------------
# Combined distance function
# ---------------------------------------------------------------------------

def compute_distance(a: dict, b: dict) -> tuple[float, str, dict]:
    """
    Compute the alignment distance between two class dicts.

    Returns a tuple of:
      distance    : float 0.0–1.0 (0.0 = same, 1.0 = unrelated)
      method      : string describing the dominant signal
      subscores   : dict with individual signal scores for the UI weight slider

    The subscores are stored on the ALIGNED_TO edge so the frontend can
    recompute distance with different weights without re-running alignment.

    Algorithm:
      similarity = (W_IRI * s_iri + W_NAME * s_name + W_DESC * s_desc + W_SLOT * s_slot)
                   / (W_IRI + W_NAME + W_DESC + W_SLOT)
      distance   = 1 - similarity
    """
    s_iri  = _score_iri(a["iri"],         b["iri"])
    s_name = _score_semantic(a["name"],   b["name"])
    s_desc = _score_semantic(a["definition"], b["definition"])
    s_slot = _score_slot(
        set(a.get("slot_iris", [])),
        set(b.get("slot_iris", []))
    )

    total_weight = W_IRI + W_NAME + W_DESC + W_SLOT
    if total_weight == 0:
        return 1.0, "none", {"iri": 0.0, "name": 0.0, "desc": 0.0, "slot": 0.0}

    similarity = (
        W_IRI  * s_iri  +
        W_NAME * s_name +
        W_DESC * s_desc +
        W_SLOT * s_slot
    ) / total_weight

    distance = round(1.0 - similarity, 6)

    # Determine which signal dominated the result — for the "method" label
    # stored on the edge and shown in the UI.
    if s_iri == 1.0 and W_IRI > 0:
        method = "iri"                     # explicit ontology anchor
    elif s_desc > 0.75 and W_DESC > 0:
        method = "semantic-desc"           # definition similarity drove it
    elif s_name > 0.75 and W_NAME > 0:
        method = "semantic-name"           # name similarity drove it
    else:
        method = "composite"               # mixture of signals

    return distance, method, {
        "iri":  round(s_iri,  6),
        "name": round(s_name, 6),
        "desc": round(s_desc, 6),
        "slot": round(s_slot, 6),
    }


# ---------------------------------------------------------------------------
# Load classes from the graph
# ---------------------------------------------------------------------------

def load_classes(conn, source_label: str | None = None) -> list[dict]:
    """
    Load SchemaClass nodes from the graph for alignment.

    Only loads the LATEST version of each class (no PRIOR_VERSION pointing
    to it). We don't align old versions — only current ones.

    Also loads the property IRIs for each class (for slot Jaccard scoring).
    """
    if source_label:
        rows = conn.execute("""
            MATCH (n:SchemaClass {source_label: $src})
            WHERE NOT EXISTS {
                MATCH (newer:SchemaClass)-[:PRIOR_VERSION]->(n)
            }
            RETURN n.uid, n.iri, n.name, n.definition,
                   n.source_label, n.content_id
        """, {"src": source_label}).get_all()
    else:
        rows = conn.execute("""
            MATCH (n:SchemaClass)
            WHERE NOT EXISTS {
                MATCH (newer:SchemaClass)-[:PRIOR_VERSION]->(n)
            }
            RETURN n.uid, n.iri, n.name, n.definition,
                   n.source_label, n.content_id
        """).get_all()

    classes = []
    for uid, iri, name, defn, src, cid in rows:
        # Get property IRIs for slot Jaccard scoring
        slot_iris = [
            r[0] for r in conn.execute("""
                MATCH (c:SchemaClass {uid: $uid})-[:HAS_PROPERTY]->(p:SchemaProperty)
                RETURN p.iri
            """, {"uid": uid}).get_all() if r[0]
        ]
        # Check if this class has a known parent (for broadMatch vs narrowMatch)
        parent_iris = [
            r[0] for r in conn.execute("""
                MATCH (c:SchemaClass {uid: $uid})-[:SUBCLASS_OF]->(p:SchemaClass)
                RETURN p.iri
            """, {"uid": uid}).get_all() if r[0]
        ]
        classes.append({
            "uid":         uid,
            "iri":         iri        or "",
            "name":        name       or "",
            "definition":  defn       or "",
            "source":      src        or "",
            "content_id":  cid        or "",
            "slot_iris":   slot_iris,
            "parent_iris": parent_iris,
        })
    return classes


def pairs_across_sources(
    classes: list[dict],
    source_a: str | None,
) -> Iterator[tuple[dict, dict]]:
    """
    Generate all cross-source class pairs for alignment.

    If source_a is given: pairs every class from source_a with every class
    from every other source (targeted alignment).

    If source_a is None: pairs every source against every other source
    (full cross-product alignment).

    We never pair a class with another class from the same source —
    that would be intra-source comparison which adds no value here.
    """
    sources = {c["source"] for c in classes if c["source"]}
    if len(sources) < 2:
        return  # Need at least 2 sources to align

    if source_a:
        group_a = [c for c in classes if c["source"] == source_a]
        group_b = [c for c in classes if c["source"] != source_a]
        for a in group_a:
            for b in group_b:
                yield a, b
    else:
        by_source: dict[str, list] = {}
        for c in classes:
            by_source.setdefault(c["source"], []).append(c)
        for s1, s2 in combinations(list(by_source.keys()), 2):
            for a in by_source[s1]:
                for b in by_source[s2]:
                    yield a, b


# ---------------------------------------------------------------------------
# Write alignment to graph
# ---------------------------------------------------------------------------

def write_alignment(conn, uid_a: str, uid_b: str,
                    distance: float, method: str,
                    subscores: dict, skos_rel: str,
                    registry_version: str = "") -> None:
    """
    Write an ALIGNED_TO edge between two SchemaClass nodes.

    We delete any existing edge first to avoid duplicates (alignment is
    re-runnable). The new edge carries:
      - distance: the computed score
      - skos_relation: the W3C SKOS vocabulary term
      - method: which signal dominated
      - score_*: individual signal subscores (for frontend weight slider)
      - registry_version: when this alignment was computed
    """
    # Remove stale edge if it exists
    conn.execute("""
        MATCH (a:SchemaClass {uid: $ua})-[r:ALIGNED_TO]->(b:SchemaClass {uid: $ub})
        DELETE r
    """, {"ua": uid_a, "ub": uid_b})

    conn.execute("""
        MATCH (a:SchemaClass {uid: $ua}), (b:SchemaClass {uid: $ub})
        CREATE (a)-[:ALIGNED_TO {
            distance:         $d,
            method:           $m,
            skos_relation:    $sr,
            score_iri:        $si,
            score_name:       $sn,
            score_desc:       $sd,
            score_slot:       $ss,
            registry_version: $rv
        }]->(b)
    """, {
        "ua": uid_a,
        "ub": uid_b,
        "d":  distance,
        "m":  method,
        "sr": skos_rel,
        "si": subscores["iri"],
        "sn": subscores["name"],
        "sd": subscores["desc"],
        "ss": subscores["slot"],
        "rv": registry_version,
    })


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--source",    default=None,
              help="Align this source against all others. "
                   "Default: align all source pairs.")
@click.option("--db",        default=DB_PATH, show_default=True)
@click.option("--threshold", default=0.5, show_default=True, type=float,
              help="Only write ALIGNED_TO edges where distance <= threshold. "
                   "Keeps the graph clean by excluding clearly unrelated pairs.")
@click.option("--min-signal", default=0.4, show_default=True, type=float,
              help="Skip pairs where no single signal exceeds this value. "
                   "Prevents storing 'both are zero' non-matches that add noise.")
@click.option("--registry-version", default="",
              help="Registry version to stamp on alignment edges.")
@click.option("--dry-run",   is_flag=True,
              help="Print alignment pairs without writing to graph.")
@click.option("--save-cache", is_flag=True, default=True,
              help="Save newly computed embeddings to parquet cache.")
def cli(source, db, threshold, min_signal,
        registry_version, dry_run, save_cache) -> None:
    """
    Compute semantic alignment between SchemaClass nodes across sources.

    Writes ALIGNED_TO edges with distance, SKOS relation, and subscores.
    Only processes the latest version of each class.

    Examples:
      python align.py --source bbqs --dry-run
      python align.py --source bbqs
      python align.py --threshold 0.3  # only close matches
    """
    global _cache_dirty

    conn    = get_connection(db)
    classes = load_classes(conn)

    if not classes:
        click.echo("No classes found. Run seed.py and ingest_linkml.py first.")
        return

    sources = {c["source"] for c in classes if c["source"]}
    click.echo(
        f"Loaded {len(classes)} classes from {len(sources)} sources: "
        f"{', '.join(sorted(sources))}"
    )

    # Pre-load the model once before the loop.
    # Even if we have a parquet cache, new classes may need fresh embeddings.
    if W_NAME > 0 or W_DESC > 0:
        _get_model()
        _embedding_cache.update(_load_embedding_cache())

    written = skipped = exact = 0

    for a, b in pairs_across_sources(classes, source):
        distance, method, subscores = compute_distance(a, b)

        # Filter 1: distance too high — these classes are unrelated
        if distance > threshold:
            skipped += 1
            continue

        # Filter 2: no meaningful signal in any dimension.
        # This catches the case where IRI=0, name difflib=0.3, desc=0.2
        # and neither is really saying "these are related". Storing that
        # edge would add noise to the Concepts view.
        max_signal = max(
            subscores["iri"], subscores["name"],
            subscores["desc"], subscores["slot"]
        )
        if max_signal < min_signal:
            skipped += 1
            continue

        if distance == 0.0:
            exact += 1

        # Determine SKOS relation.
        # We check if class A is a subclass of class B or vice versa
        # to distinguish broadMatch from narrowMatch.
        b_is_parent_of_a = bool(a["iri"] and b["iri"] in a["parent_iris"])
        skos_rel = compute_skos_relation(distance, is_subclass=b_is_parent_of_a)

        if dry_run:
            click.echo(
                f"  [{skos_rel}] {a['source']}:{a['name']} ↔ "
                f"{b['source']}:{b['name']}  "
                f"d={distance:.4f}  "
                f"(iri={subscores['iri']:.2f} "
                f"name={subscores['name']:.2f} "
                f"desc={subscores['desc']:.2f})"
            )
        else:
            write_alignment(
                conn, a["uid"], b["uid"],
                distance, method, subscores, skos_rel,
                registry_version,
            )
        written += 1

    action = "Would write" if dry_run else "Wrote"
    click.echo(
        f"\n{action} {written} ALIGNED_TO edges "
        f"({exact} exact IRI matches, {skipped} skipped)."
    )

    # Save newly computed embeddings to parquet cache
    if save_cache and _cache_dirty and not dry_run:
        _save_embedding_cache(_embedding_cache)


if __name__ == "__main__":
    cli()
