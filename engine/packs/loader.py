"""Pack loading, checksums and the pack registry.

Pack files are the source of truth: they are versioned in git and synced into
``public.pack_versions`` with a checksum. A change to a GST rule therefore ships
as a pack version and can be rolled out to one tenant first, with rollback,
without an engine release.
"""

from __future__ import annotations

import functools
import hashlib
import json
import pathlib
from dataclasses import dataclass, field
from typing import Any


from engine.db import J
from engine.packs.models import (
    ConnectorSpec,
    DocumentTypeSpec,
    EvaluationSet,
    PackManifest,
    RuleDefinition,
)
from engine.settings import get_settings


class PackError(RuntimeError):
    pass


def _read_json(path: pathlib.Path) -> Any:
    if not path.exists():
        raise PackError(f"pack file is missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PackError(f"{path.name} is not valid JSON: {exc}") from exc


def compute_checksum(directory: pathlib.Path) -> str:
    """Stable checksum over every file in the pack, so a version is pinned."""
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(directory)).replace("\\", "/").encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass
class Pack:
    """A loaded, validated pack."""

    manifest: PackManifest
    documents: dict[str, DocumentTypeSpec]
    hints: dict[str, Any]
    rules: list[RuleDefinition]
    connectors: ConnectorSpec
    evaluation: EvaluationSet
    hsn_rates: dict[str, dict[str, Any]]
    directory: pathlib.Path
    checksum: str
    rules_by_key: dict[str, RuleDefinition] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.rules_by_key = {r.key: r for r in self.rules}

    @property
    def key(self) -> str:
        return self.manifest.key

    @property
    def version(self) -> str:
        return self.manifest.version

    def document(self, key: str) -> DocumentTypeSpec:
        try:
            return self.documents[key]
        except KeyError:
            raise PackError(
                f"pack {self.key} {self.version} has no document type {key!r}; "
                f"known types: {sorted(self.documents)}"
            ) from None

    def rules_for(self, doc_type: str) -> list[RuleDefinition]:
        return [r for r in self.rules if r.applies(doc_type)]

    def classification_table(self) -> list[tuple[str, list[str]]]:
        return [(spec.key, spec.classify_hints) for spec in self.documents.values()]

    def hints_for(self, doc_type: str) -> list[str]:
        return list(self.hints.get("document_hints", {}).get(doc_type, []))

    def hsn_rate(self, hsn: str | None) -> dict[str, Any] | None:
        if not hsn:
            return None
        cleaned = str(hsn).strip()
        for length in (8, 6, 4, 5, 3, 2):
            if len(cleaned) >= length and cleaned[:length] in self.hsn_rates:
                return self.hsn_rates[cleaned[:length]]
        return None

    def to_db_payload(self) -> dict[str, Any]:
        return {
            "document_catalogue": [d.model_dump(mode="json") for d in self.documents.values()],
            "extraction_hints": self.hints,
            "validation_rules": [r.model_dump(mode="json") for r in self.rules],
            "connectors": self.connectors.model_dump(mode="json"),
            "evaluation_set": self.evaluation.model_dump(mode="json"),
        }


def load_pack(
    key: str = "manufacturing",
    *,
    version: str | None = None,
    packs_dir: pathlib.Path | None = None,
) -> Pack:
    """Load a pack from disk. ``version`` is accepted for future multi-version
    directories; today a pack directory holds exactly one version."""
    base = packs_dir or get_settings().packs_dir
    directory = base / key
    if not directory.is_dir():
        raise PackError(f"pack directory not found: {directory}")

    manifest = PackManifest.model_validate(_read_json(directory / "pack.json"))
    if version is not None and version != manifest.version:
        raise PackError(
            f"requested pack {key} version {version} but {directory} holds {manifest.version}"
        )

    parts = manifest.parts
    catalogue = _read_json(directory / parts.get("document_catalogue", "document_catalogue.json"))
    documents = {
        spec["key"]: DocumentTypeSpec.model_validate(spec) for spec in catalogue["documents"]
    }

    rules_raw = _read_json(directory / parts.get("validation_rules", "validation_rules.json"))
    rules = [RuleDefinition.model_validate(r) for r in rules_raw["rules"]]
    duplicates = {r.key for r in rules if [x.key for x in rules].count(r.key) > 1}
    if duplicates:
        raise PackError(f"duplicate rule keys in {key}: {sorted(duplicates)}")

    hsn_path = directory / parts.get("hsn_rates", "hsn_rates.json")
    hsn_rates = _read_json(hsn_path).get("rates", {}) if hsn_path.exists() else {}

    return Pack(
        manifest=manifest,
        documents=documents,
        hints=_read_json(directory / parts.get("extraction_hints", "extraction_hints.json")),
        rules=rules,
        connectors=ConnectorSpec.model_validate(
            _read_json(directory / parts.get("connectors", "connectors.json"))
        ),
        evaluation=EvaluationSet.model_validate(
            _read_json(directory / parts.get("evaluation_set", "evaluation_set.json"))
        ),
        hsn_rates=hsn_rates,
        directory=directory,
        checksum=compute_checksum(directory),
    )


class PackRegistry:
    """Loads packs per tenant and caches them (section 7)."""

    def __init__(self, packs_dir: pathlib.Path | None = None) -> None:
        self.packs_dir = packs_dir or get_settings().packs_dir
        self._cache: dict[str, Pack] = {}

    def get(self, key: str, version: str | None = None) -> Pack:
        cache_key = f"{key}@{version or 'current'}"
        if cache_key not in self._cache:
            self._cache[cache_key] = load_pack(key, version=version, packs_dir=self.packs_dir)
        return self._cache[cache_key]

    def available(self) -> list[str]:
        if not self.packs_dir.is_dir():
            return []
        return sorted(
            p.name for p in self.packs_dir.iterdir() if (p / "pack.json").exists()
        )

    def invalidate(self) -> None:
        self._cache.clear()


@functools.lru_cache(maxsize=1)
def get_registry() -> PackRegistry:
    return PackRegistry()


# ── registry persistence ────────────────────────────────────────────────────


async def sync_pack(db, pack: Pack, *, status: str = "active", notes: str | None = None) -> None:
    """Write the pack and its version into the control plane."""
    async with db.control() as cur:
        await cur.execute(
            """
            insert into public.packs (key, name, industry, description)
            values (%s, %s, %s, %s)
            on conflict (key) do update
              set name = excluded.name,
                  industry = excluded.industry,
                  description = excluded.description
            """,
            (pack.key, pack.manifest.name, pack.manifest.industry, pack.manifest.description),
        )
        payload = pack.to_db_payload()
        await cur.execute(
            """
            insert into public.pack_versions
              (pack_key, version, checksum, status, document_catalogue, extraction_hints,
               validation_rules, connectors, evaluation_set, released_at, notes)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
            on conflict (pack_key, version) do update
              set checksum = excluded.checksum,
                  document_catalogue = excluded.document_catalogue,
                  extraction_hints = excluded.extraction_hints,
                  validation_rules = excluded.validation_rules,
                  connectors = excluded.connectors,
                  evaluation_set = excluded.evaluation_set,
                  notes = excluded.notes
            """,
            (
                pack.key,
                pack.version,
                pack.checksum,
                status,
                J(payload["document_catalogue"]),
                J(payload["extraction_hints"]),
                J(payload["validation_rules"]),
                J(payload["connectors"]),
                J(payload["evaluation_set"]),
                notes,
            ),
        )


async def bind_pack_to_tenant(db, tenant_id, pack: Pack, *, layer: str = "base") -> None:
    async with db.control() as cur:
        await cur.execute(
            "select id from public.pack_versions where pack_key = %s and version = %s",
            (pack.key, pack.version),
        )
        row = await cur.fetchone()
        if row is None:
            raise PackError(f"pack {pack.key} {pack.version} has not been synced")
        await cur.execute(
            """
            insert into public.tenant_packs (tenant_id, pack_version_id, layer, status)
            values (%s, %s, %s, 'active')
            on conflict (tenant_id, pack_version_id) do update
              set status = 'active', rolled_back_at = null
            """,
            (tenant_id, row["id"], layer),
        )


async def roll_back_pack(db, tenant_id, pack_version_id) -> None:
    """Roll a pack back for one tenant without touching anyone else."""
    async with db.control() as cur:
        await cur.execute(
            "update public.tenant_packs set status = 'rolled_back', rolled_back_at = now() "
            "where tenant_id = %s and pack_version_id = %s",
            (tenant_id, pack_version_id),
        )


async def active_pack_for_tenant(db, tenant_id, fallback_key: str = "manufacturing") -> tuple[str, str | None]:
    """(pack_key, version) the tenant should run."""
    async with db.control() as cur:
        await cur.execute(
            """
            select pv.pack_key, pv.version
            from public.tenant_packs tp
            join public.pack_versions pv on pv.id = tp.pack_version_id
            where tp.tenant_id = %s and tp.status = 'active'
            order by tp.priority desc, tp.activated_at desc
            limit 1
            """,
            (tenant_id,),
        )
        row = await cur.fetchone()
    if row:
        return row["pack_key"], row["version"]
    return fallback_key, None
