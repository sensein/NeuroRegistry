# SenseIn Schema Registry

Decentralised schema registry. Backed by LadybugDB (embedded property graph)
and served over HTTP via FastAPI.

## Quick start

```bash
pip install -r requirements.txt
uvicorn schema_registry:app --reload
```

Docs at `http://localhost:8000/docs`. Database persists to `./registry.lbug`.
No server, no Docker.

---

## Node model

All nodes share a **base identity** (conceptual inheritance — fields are
repeated on each table since LadybugDB does not support table inheritance):

| Field | Description |
|---|---|
| `uid` | UUID — LadybugDB primary key |
| `iri` | Stable concept identifier (never changes across versions) |
| `uri` | Versioned registry URI — `registry.sensein.io/obj/{id}/v/{semver}` |
| `version` | Semver string |
| `created_at` | ISO-8601 timestamp |

### SchemaClass
`name`, `definition`

Relationships: `SUBCLASS_OF`, `MIXIN`, `SKOS_BROADER`, `SKOS_RELATED`,
`HAS_PROPERTY`, `PRIOR_VERSION`, `PROV_GENERATED`, `FROM_SOURCE`

### SchemaProperty
`name`, `definition`, `datatype`, `range_uri`

Relationships: `HAS_PROPERTY` (from SchemaClass), `PRIOR_VERSION_P`,
`PROV_GENERATED_P`

### SchemaRule
Validation constraints live here — not on SchemaProperty.

`name`, `rule_spec`, `units`, `min_val`, `max_val`, `pattern`,
`multivalued`, `required`

Relationships: `APPLIES_TO` → SchemaClass, `PRIOR_VERSION_R`,
`PROV_GENERATED_R`

### SchemaTransform
`name`, `spec`

### SchemaSource
`label`, `mime_type`

### SchemaActivity
PROV-O activity. `activity`, `agent`, `started_at`

---

## Versioning

Append-only. A version bump:
1. Creates a new node (new `uid`, new `uri`, same `iri`)
2. Links new → old via `PRIOR_VERSION`
3. Records a `SchemaActivity`

Nothing is ever deleted.

---

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Node counts per type |
| GET | `/schema/classes` | List all classes |
| GET | `/schema/class/{id}` | All versions of a class |
| GET | `/schema/class/{id}/properties` | Properties on a class |
| POST | `/schema/class` | Create a class |
| POST | `/schema/class/{id}/bump` | Bump version |
| GET | `/schema/property/{id}` | All versions of a property |
| POST | `/schema/property` | Create a property |
| GET | `/schema/rule/{id}` | Get a rule |
| POST | `/schema/rule` | Create a rule |
| GET | `/schema/transform/{id}` | Get a transform |
| POST | `/schema/transform` | Create a transform |
| GET | `/provenance/class/{id}` | Provenance history |
| GET | `/distance/{id1}/{id2}` | Distance stub (TBD) |

---

## Open questions

- **VOL sets** — design TBD
- **Users / Org / Roles** — access control layer TBD
- **Distance function** — semantic + structural metric, scientists to spec
- **Rule spec format** — Python callable string vs SHACL vs JSON expression
- **Transform spec format** — TBD with team
