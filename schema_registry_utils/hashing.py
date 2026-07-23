import hashlib
import json

from schema_registry_utils.models import RegistryClass, RegistryProperty

_EXCLUDED_FIELDS = {"hash_id", "provenance", "skos_mappings"}


def compute_hash_id(entity: RegistryClass | RegistryProperty) -> str:
    """Compute a content-based hash_id for a RegistryClass or RegistryProperty.

    Everything but hash_id, provenance, and skos_mappings is treated as
    identity-defining content.
    """
    content = entity.model_dump(exclude=_EXCLUDED_FIELDS)
    canonical = json.dumps(_normalize(content), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def assign_hash_id(entity: RegistryClass | RegistryProperty) -> RegistryClass | RegistryProperty:
    """Compute entity's hash_id from its current content, then suffix its name
    with the first 4 hex characters of the digest (e.g. "age" -> "age_a1b2").

    Mutates entity in place and returns it. Note: since name is part of the
    hashed content, the resulting hash_id will no longer match a fresh
    compute_hash_id() call on the entity after this mutation.
    """
    hash_id = compute_hash_id(entity)
    digest = hash_id.split(":", 1)[1]
    entity.hash_id = hash_id
    entity.name = f"{entity.name}_{digest[:4]}"
    return entity


def _normalize(value):
    if isinstance(value, dict):
        return {key: _normalize(val) for key, val in value.items()}
    if isinstance(value, list):
        normalized = [_normalize(val) for val in value]
        if all(isinstance(val, str) for val in normalized):
            # reference lists (properties/relations/mixins) are unordered sets
            return sorted(normalized)
        return normalized
    return value
