<h1 align='center'>NeuroGhost</p>

<h3 align='center'>A shared vocabulary for neuroscience data.</h3>

<p align='center'><img width="500" height="500" alt="image" src="https://github.com/user-attachments/assets/e70a2916-acea-44bf-9f23-537f290d6f92" /></p>

---

## What is this?

Every neuroscience lab describes its data a little differently. One group calls
someone an *Investigator*, another calls them a *Person*, another calls them a
*Researcher*. Same idea, three names. Multiply that across every concept —
brains, experiments, devices, subjects, publications — and it becomes very hard
for different groups to share or combine data.

**NeuroGhost is a public catalog of these vocabularies.** Any lab or project
can publish their schema (their definitions), and the registry automatically
compares it to every other schema already in the catalog, so you can see:

- What terms already exist for a concept you care about
- Which terms across different schemas actually mean the same thing
- Which are unique to a particular project
- How the definitions have evolved over time

Think of it as a **Rosetta Stone for neuroscience data models.**

---

## Key concepts (short version)

**Schema** — one group's vocabulary. A list of the things they care about
(*classes*), the things they measure (*properties*), and any constraints
(*rules*). Schemas are written in a simple text format called
[LinkML](https://linkml.io/), one file per project.

**Class** — a type of thing. `Person`, `BrainRegion`, `Recording`, `Publication`.

**Property** — something a class has or does. A `Person` has a `name` and an `orcid`.

**Rule** — a constraint on a property. `age` must be a positive integer.
`sampling_rate` must be measured in Hz.

**Alignment** — the registry's automatic comparison between two classes from
different schemas, expressed as a number between 0 and 1:

- **0.0** = "these are definitely the same thing"
- **0.5** = "these are related but not identical"
- **1.0** = "these have nothing to do with each other"

**Registry version** — every time a schema is added or updated, the whole
registry gets a new version number (like `1.2.0`). Old versions never disappear
— you can always go back and see what the registry looked like at any point in
time.

---

## How the distance function works (in plain English)

When the registry compares two classes, it looks at three things and combines
them into a single distance score:

1. **IRI match** *(weight: 60%)* — Do the two classes explicitly point to the
   same universal identifier? For example, both saying "this is the same as
   schema.org's Person" is a perfect match.

2. **Name similarity** *(weight: 15%)* — Do the class names mean similar things?
   Uses AI embeddings (the same math that powers modern search) to compare, so
   `Investigator` and `Researcher` register as similar even though the letters
   are different.

3. **Definition similarity** *(weight: 25%)* — Do the plain-English descriptions
   describe similar things? Same embedding approach.

You can play with these weights live on the **Concepts** page of the website —
slide them around and watch alignments recompute in real time.

---

## Using the website

Go to **[sensein.group/NeuroGhost](https://sensein.group/NeuroGhost/)**.
You'll see seven tabs:

- **Concepts** — classes grouped by meaning. Aligned classes from different
  schemas collapse into a single card.
- **Diff** — pick any two schemas, see which classes overlap and which are unique
  to each.
- **Graph Schema** — how the underlying database is structured (for the curious).
- **Transform** — convert a CSV from one schema's format to another.
- **Query** — structured query builder + Claude-assisted natural language queries.
- **Provenance** — a full timeline of every change to the registry, with per-class
  version diffs.
- **Register** — submit a new schema.

Every view has download buttons. You can grab a single class, a whole schema,
the entire registry, or a CSV diff between two schemas.

---

## API Reference

**Base URL:** `https://sensein.group/NeuroGhost`

The registry exposes two kinds of endpoints:

| Type | Transport | Auth | Status |
|------|-----------|------|--------|
| Read (schemas, alignments, provenance) | Static JSON via GitHub Pages | None | ✅ Live |
| Transform (field mapping between schemas) | Serverless function (planned) | None | 🔜 Planned |

CORS is open on all endpoints. No API key required.

---

### `GET /data/registry.json`

Returns the full registry at the current version: all sources, classes,
properties, and pre-computed cross-schema alignments.

```bash
curl https://sensein.group/NeuroGhost/data/registry.json
```

**Response**
```json
{
  "registry_version": "1.7.0",
  "generated_at": "2026-07-23T12:40:24Z",
  "sources": [
    { "label": "bbqs",  "version": "1.0.0", "class_count": 29 },
    { "label": "bids",  "version": "1.9.0", "class_count": 1  },
    { "label": "dandi", "version": "0.6.8", "class_count": 20 },
    { "label": "nwb",   "version": "2.7.0", "class_count": 53 }
  ],
  "classes": [
    {
      "uid":        "8003dcad-...",
      "iri":        "https://registry.sensein.io/obj/Subject",
      "name":       "Subject",
      "definition": "A research participant.",
      "abstract":   false,
      "source":     "bbqs",
      "properties": [
        {
          "uid":        "807f8fec-...",
          "name":       "age",
          "definition": "Age in years.",
          "datatype":   "xsd:integer",
          "multivalued": false,
          "required":   false,
          "source":     "bbqs"
        }
      ],
      "alignments": [
        {
          "target_uid":    "f4f8e4a4-...",
          "target_name":   "Participant",
          "target_source": "bids",
          "distance":      0.12,
          "method":        "composite",
          "scores": { "iri": 0.0, "name": 0.08, "desc": 0.19, "slot": 0.0 }
        }
      ]
    }
  ]
}
```

`distance` ranges from **0.0** (identical) to **1.0** (unrelated).

---

### `GET /data/versions/{version}.json`

Frozen snapshot of the registry at a specific version. Snapshots never change.

```bash
curl https://sensein.group/NeuroGhost/data/versions/1.2.0.json
```

Same shape as `registry.json`. To list available versions, check the
`registry_version` field of the live registry and the `data/versions/`
directory.

---

### `GET /data/provenance.json`

Changelog — every schema ingestion: who submitted it, when, and which registry
version it produced.

```bash
curl https://sensein.group/NeuroGhost/data/provenance.json
```

---

### Client-side filtering

There are no server-side query parameters (static files). Filter in your
client after fetching:

```js
const reg = await fetch("https://sensein.group/NeuroGhost/data/registry.json")
              .then(r => r.json());

// All classes from one schema
const bbqs = reg.classes.filter(c => c.source === "bbqs");

// Close alignments across any two schemas (distance < 0.35)
const pairs = reg.classes.flatMap(c =>
  c.alignments
    .filter(a => a.distance < 0.35)
    .map(a => ({ from: `${c.source}/${c.name}`, to: `${a.target_source}/${a.target_name}`, distance: a.distance }))
);
```

---

### `GET /api/transform` — field mapping *(planned)*

Returns the computed field-to-field mapping between two schemas, derived from
the alignment graph. No data is sent — this is purely a schema-level lookup.

```
GET /api/transform?from=bbqs&to=bids
```

**Planned response**
```json
{
  "from": "bbqs",
  "to":   "bids",
  "mappings": [
    {
      "from_class":    "Subject",
      "to_class":      "Participant",
      "distance":      0.12,
      "field_mappings": [
        { "from_field": "age",        "to_field": "age",          "confidence": 0.97 },
        { "from_field": "species",    "to_field": "species",      "confidence": 0.91 },
        { "from_field": "subject_id", "to_field": "participant_id","confidence": 0.85 }
      ]
    }
  ]
}
```

Because the alignment data is already in `registry.json`, this endpoint can be
pre-computed at export time and served as a static file — no compute layer
needed.

---

### `POST /api/transform` — data transform *(planned)*

Send a dataset in one schema's format; receive it mapped to another. Unlike the
GET above, this requires a live compute layer (a serverless function that reads
the field-mapping and rewrites the payload).

```bash
curl -X POST https://sensein.group/NeuroGhost/api/transform \
  -H "Content-Type: application/json" \
  -d '{
    "from": "bbqs",
    "to":   "bids",
    "data": {
      "subject_id": "sub-01",
      "age": 24,
      "species": "Homo sapiens"
    }
  }'
```

**Planned response**
```json
{
  "from": "bbqs",
  "to":   "bids",
  "result": {
    "participant_id": "sub-01",
    "age": 24,
    "species": "Homo sapiens"
  },
  "unmapped_fields": [],
  "warnings": []
}
```

This will be implemented as a lightweight serverless function (Cloudflare Worker
or equivalent) that reads the static registry alignment map and applies field
renaming. Not yet live.

---

## Adding your own schema

The easiest way, no setup required:

1. Write your schema as a LinkML `.yml` file (see the
   [LinkML tutorial](https://linkml.io/linkml/intro/tutorial01.html) or copy
   `schemas/bbqs.yml` from this repo as a template).
2. Go to the [Register tab](https://sensein.group/NeuroGhost/) on the website.
3. Paste your YAML, give it a name, click **Open GitHub Issue**.
4. A GitHub Issue opens in a new tab, pre-filled. Click **Submit**.
5. Within a couple of minutes, an automated workflow will:
   - Validate your schema
   - Add it to the registry graph
   - Compute alignments against every other schema
   - Bump the registry version
   - Archive a permanent snapshot
   - Comment on your issue with a link to your schema in the browser

That's it. No installation, no pull request, no reviewers required.

---

## Running it locally (optional)

Only needed if you want to develop the registry itself, or bulk-load schemas
outside the web flow.

```bash
git clone https://github.com/sensein/NeuroGhost.git
cd NeuroGhost
pip install -r requirements.txt

# 1. Seed with schema.org as the base vocabulary
python neuro_ghost/seed.py

# 2. Fetch + convert external schemas (BIDS, NWB, DANDI, openMINDS, AIND)
python neuro_ghost/converters/run_all.py

# 3. Load a schema
python neuro_ghost/ingest_linkml.py --file schemas/bbqs.yml

# 4. Compute alignments
python neuro_ghost/align.py --source bbqs

# 5. Export snapshot the frontend reads
python neuro_ghost/export_json.py --bump minor --schema bbqs
```

Then open `index.html` in a browser and you'll see the local snapshot.


## Under the hood

Nothing exotic:

- **[LadybugDB](https://ladybugdb.com/)** — embedded graph database that stores
  every class, property, and relationship. Runs as a Python package, no server.
- **[LinkML](https://linkml.io/)** — the human-friendly schema format everyone
  writes their vocabularies in.
- **[sentence-transformers](https://sbert.net/)** — the local embedding model
  (`all-MiniLM-L6-v2`) that powers semantic distance.
- **Static HTML + GitHub Pages** — the website is one file, no framework.
- **GitHub Actions** — the automation that runs on every schema submission.

Every design decision favors "nothing to run, nothing to maintain." The
registry lives in a single GitHub repo. The website lives on GitHub Pages. The
database is rebuilt fresh in CI from the source `.yml` files every time
something changes.

---

## Contributing

- **Register a schema:** use the [Register tab](https://sensein.group/NeuroGhost/) on the site.
- **Report an issue or suggest a feature:** [open an issue](https://github.com/sensein/NeuroGhost/issues/new).
- **Improve the tooling:** PRs welcome, especially around the distance function.

---

## License

CC0-1.0 — public domain. Fork it, remix it, run your own registry.
