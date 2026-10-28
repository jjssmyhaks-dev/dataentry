"""Strict typed schemas generated from the pack's document catalogue.

Section 6.2: the vision-capable model returns data conforming to the pack's
strict schema (Pydantic models), and output that fails schema validation is
retried or escalated, never silently accepted. Building the models from the
catalogue is what makes a new document type a pack change rather than an engine
change.
"""

from __future__ import annotations

import datetime as dt
import functools
import re
from decimal import Decimal
from typing import Any, Type

from pydantic import BaseModel, ConfigDict, Field, create_model

from engine.packs.models import DocumentTypeSpec, FieldSpec

_PYTHON_TYPES: dict[str, Any] = {
    "string": str,
    "gstin": str,
    "decimal": Decimal,
    "date": dt.date,
    "int": int,
    "bool": bool,
}


def _snake(name: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()


def build_item_model(item_fields: list[FieldSpec], *, name: str) -> Type[BaseModel]:
    fields: dict[str, Any] = {}
    for spec in item_fields:
        python_type = _PYTHON_TYPES.get(spec.type, str)
        default = Field(default=None, description=spec.label or spec.name)
        if spec.mandatory:
            fields[spec.name] = (python_type, default)
        else:
            fields[spec.name] = (python_type | None, default)
    return create_model(name, __config__=ConfigDict(extra="forbid"), **fields)


@functools.lru_cache(maxsize=64)
def build_pydantic_model(doc_type: str, catalogue_json: str) -> Type[BaseModel]:
    """Build (and cache) the extraction model for one document type.

    ``catalogue_json`` participates in the cache key so a pack version change
    produces a new model rather than reusing a stale one.
    """
    import json

    spec = DocumentTypeSpec.model_validate(json.loads(catalogue_json))
    return _build_model_from_spec(spec)


def model_for_document(spec: DocumentTypeSpec) -> Type[BaseModel]:
    import json

    return build_pydantic_model(spec.key, json.dumps(spec.model_dump(mode="json"), sort_keys=True))


def _build_model_from_spec(spec: DocumentTypeSpec) -> Type[BaseModel]:
    fields: dict[str, Any] = {}
    for field_spec in spec.fields:
        if field_spec.type == "array":
            item_model = build_item_model(
                field_spec.item_fields, name=f"{_snake(spec.key)}_{_snake(field_spec.name)}_item"
            )
            base: Any = list[item_model]  # type: ignore[valid-type]
        else:
            base = _PYTHON_TYPES.get(field_spec.type, str)

        description = field_spec.label or field_spec.name
        if field_spec.aliases:
            description = f"{description} (also written as: {', '.join(field_spec.aliases)})"

        if field_spec.mandatory:
            fields[field_spec.name] = (base, Field(default=None, description=description))
        else:
            fields[field_spec.name] = (base | None, Field(default=None, description=description))

    model = create_model(
        f"{_snake(spec.key).title().replace('_', '')}Extraction",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
    model.model_config["json_schema_extra"] = {
        "doc_type": spec.key,
        "label": spec.label,
        "posts_to": spec.posts_to,
        "mandatory_any_of": spec.constraints.mandatory_any_of,
        "totals_field": spec.constraints.totals_field,
        "line_item_key": spec.line_item_key,
    }
    return model


def schema_instructions(spec: DocumentTypeSpec, hints: list[str]) -> str:
    """The prompt body handed to the extraction agent for one document type."""
    lines = [
        f"Document type: {spec.label} ({spec.key}).",
        "",
        "Fields to return:",
    ]
    for field_spec in spec.fields:
        if field_spec.type == "array":
            lines.append(
                f"- {field_spec.name}: a list of rows, each with "
                + ", ".join(
                    f"{i.name} ({i.type}{', mandatory' if i.mandatory else ''})"
                    for i in field_spec.item_fields
                )
            )
        else:
            lines.append(
                f"- {field_spec.name} ({field_spec.type}"
                f"{', mandatory' if field_spec.mandatory else ''})"
                + (f" — {field_spec.label}" if field_spec.label else "")
            )
    if spec.constraints.mandatory_any_of:
        for group in spec.constraints.mandatory_any_of:
            lines.append(f"At least one of these must be present: {', '.join(group)}")
    if hints:
        lines += ["", "How this industry writes these documents:", *[f"- {hint}" for hint in hints]]
    return "\n".join(lines)


def missing_mandatory(spec: DocumentTypeSpec, payload: dict[str, Any]) -> list[str]:
    """Mandatory fields that are absent, including the any-of groups."""
    missing: list[str] = []
    for field_spec in spec.fields:
        if field_spec.mandatory and payload.get(field_spec.name) in (None, "", [], {}):
            missing.append(field_spec.name)
    for group in spec.constraints.mandatory_any_of:
        if not any(payload.get(name) not in (None, "", [], {}, 0, Decimal("0")) for name in group):
            missing.append(" or ".join(group))
    return missing
