from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class SkosMappingTypeEnum(str, Enum):
    EXACT_MATCH   = "EXACT_MATCH"
    CLOSE_MATCH   = "CLOSE_MATCH"
    BROAD_MATCH   = "BROAD_MATCH"
    NARROW_MATCH  = "NARROW_MATCH"
    RELATED_MATCH = "RELATED_MATCH"


class ProvenanceInfo(BaseModel):
    created_by:  str
    created_at:  datetime
    modified_by: Optional[str]      = None
    modified_at: Optional[datetime] = None
    derived_from: List[str]         = Field(default_factory=list)
    method:      Optional[str]      = None


class SkosMapping(BaseModel):
    hash_id:      Optional[str]                  = None
    mapping_type: Optional[SkosMappingTypeEnum]  = None
    target:       Optional[str]                  = None


class Relation(BaseModel):
    hash_id:    Optional[str]            = None
    subject:    str
    predicate:  str
    object:     str
    provenance: Optional[ProvenanceInfo] = None


class RegistryEntity(BaseModel):
    hash_id:      Optional[str]        = None
    name:         str
    definition:   str
    provenance:   ProvenanceInfo
    skos_mappings: List[SkosMapping]   = Field(default_factory=list)


class RegistryClass(RegistryEntity):
    iri:              Optional[str]  = None
    is_abstract:      bool           = False
    # Stored as hash_id references; graph edges (HAS_PROPERTY, HAS_RELATION,
    # MIXIN) are the traversal mechanism — these lists mirror them for hashing.
    properties:   List[str]          = Field(default_factory=list)
    relations:    List[str]          = Field(default_factory=list)
    parent_class: Optional[str]      = None
    mixins:       List[str]          = Field(default_factory=list)
    source_label:     Optional[str]  = None
    registry_version: Optional[str]  = None


class RegistryProperty(RegistryEntity):
    iri:              Optional[str]  = None
    value_range:      str
    units:            Optional[str]  = None
    multivalued:      bool           = False
    required:         bool           = False
    source_label:     Optional[str]  = None
    registry_version: Optional[str]  = None
