"""Typed models for an Industry Pack.

A pack is a versioned, self-contained bundle with five parts (section 7):

1. document catalogue
2. extraction hints
3. validation rules
4. connectors
5. evaluation set
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FieldType = Literal["string", "decimal", "date", "int", "bool", "array", "gstin"]


class FieldSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    label: str | None = None
    type: FieldType = "string"
    mandatory: bool = False
    aliases: list[str] = Field(default_factory=list)
    item_fields: list["FieldSpec"] = Field(default_factory=list)
    derived_from: str | None = None
    description: str | None = None


class DocumentConstraints(BaseModel):
    mandatory_any_of: list[list[str]] = Field(default_factory=list)
    totals_field: str | None = None


class DocumentTypeSpec(BaseModel):
    key: str
    label: str
    description: str | None = None
    posts_to: str | None = None
    classify_hints: list[str] = Field(default_factory=list)
    constraints: DocumentConstraints = Field(default_factory=DocumentConstraints)
    line_item_key: str | None = None
    fields: list[FieldSpec] = Field(default_factory=list)

    @property
    def mandatory_fields(self) -> list[str]:
        return [f.name for f in self.fields if f.mandatory]

    def field(self, name: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.name == name), None)

    def flat_field_paths(self, include_line_items: bool = True) -> list[str]:
        paths: list[str] = []
        for spec in self.fields:
            if spec.type == "array":
                if include_line_items:
                    paths.extend(f"{spec.name}.{item.name}" for item in spec.item_fields)
            else:
                paths.append(spec.name)
        return paths


class RuleBreach(BaseModel):
    fail: str | None = None
    warn: str | None = None


class RuleDefinition(BaseModel):
    """A declarative rule (Appendix B). The engine reads these; the named
    ``check`` is the implementation the engine dispatches to."""

    key: str
    version: str = "1.0.0"
    title: str
    check: str
    description: str | None = None
    applies_to: list[str] = Field(default_factory=lambda: ["*"])
    severity: Literal["fail", "warn", "info"] = "warn"
    fields: list[str] = Field(default_factory=list)
    requires_lookup: str | None = None
    degrade_to: Literal["warn", "info", "skip"] = "warn"
    breach: RuleBreach = Field(default_factory=RuleBreach)
    settings: dict[str, Any] = Field(default_factory=dict)

    def applies(self, doc_type: str) -> bool:
        return "*" in self.applies_to or doc_type in self.applies_to


class ConnectorTargetSpec(BaseModel):
    label: str
    role: str
    transport: str
    description: str | None = None
    requires: list[str] = Field(default_factory=list)


class VoucherMapEntry(BaseModel):
    doc_type: str
    condition: str = "always"
    voucher_type: str | None = None
    notes: str | None = None
    entries: list[dict[str, Any]] = Field(default_factory=list)


class MasterDataHandling(BaseModel):
    party_match_order: list[str] = Field(default_factory=lambda: ["gstin", "normalised_name"])
    create_party_only_after_approval: bool = True
    never_create_or_rename_ledger: bool = True
    tax_ledgers_mapped_at_onboarding: list[str] = Field(default_factory=list)
    optional_masters: list[str] = Field(default_factory=list)


class ConnectorSpec(BaseModel):
    targets: dict[str, ConnectorTargetSpec] = Field(default_factory=dict)
    voucher_map: list[VoucherMapEntry] = Field(default_factory=list)
    master_data_handling: MasterDataHandling = Field(default_factory=MasterDataHandling)
    narration_template: str = "ADE/{doc_type_short}/{record_short}"
    idempotency_key_template: str = "{tenant_id}:{doc_type}:{voucher_type}:{business_key}"
    autonomy_defaults: dict[str, Any] = Field(default_factory=dict)

    def voucher_type_for(self, doc_type: str, condition: str = "always") -> str | None:
        for entry in self.voucher_map:
            if entry.doc_type == doc_type and entry.condition == condition:
                return entry.voucher_type
        for entry in self.voucher_map:
            if entry.doc_type == doc_type:
                return entry.voucher_type
        return None


class EvaluationCase(BaseModel):
    id: str
    doc_type: str
    tags: list[str] = Field(default_factory=list)
    file: str | None = None


class EvaluationSet(BaseModel):
    notes: str | None = None
    source: str | None = None
    source_caveat: str | None = None
    documents_dir: str = "eval/documents"
    gate: dict[str, Any] = Field(default_factory=dict)
    cases: list[EvaluationCase] = Field(default_factory=list)


class PackManifest(BaseModel):
    key: str
    name: str
    industry: str
    version: str
    status: Literal["draft", "canary", "active", "retired"] = "draft"
    description: str | None = None
    locale: list[str] = Field(default_factory=list)
    currency: str = "INR"
    parts: dict[str, str] = Field(default_factory=dict)
    targets: dict[str, Any] = Field(default_factory=dict)
