"""The extraction agent.

Two paths, one interface:

* **Model path** — a Pydantic AI agent with the pack's strict typed output. Used
  whenever a model is configured. The model is asked for a per-field confidence
  alongside the data, because section 6.2 requires per-field confidence scores.
  Output that fails schema validation is retried once with the validation error
  fed back, and then escalated — never silently accepted.
* **Baseline path** — the deterministic text-layer extractor. Used when no model
  is configured, or when the document is plain text and the baseline already
  reads it.

Both return ``ExtractedRecord`` objects, so nothing downstream knows the
difference except the recorded model name.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from engine.extraction.baseline import extract_segment
from engine.extraction.classifier import DocumentSegment
from engine.extraction.schemas import missing_mandatory, model_for_document, schema_instructions
from engine.models import ExtractedRecord, FieldValue
from engine.observability import get_tracer
from engine.packs.loader import Pack
from engine.settings import Settings, get_settings

BASELINE_MODEL = "baseline-text"
PROMPT_VERSION = "manufacturing-extract/v1"

MAX_SCHEMA_ATTEMPTS = 2


class ExtractionSchemaError(RuntimeError):
    """Output failed the pack's schema twice. The record is escalated, not posted."""


@dataclass
class ExtractionOutcome:
    records: list[ExtractedRecord] = field(default_factory=list)
    model: str = BASELINE_MODEL
    prompt_version: str = PROMPT_VERSION
    attempts: int = 1
    schema_ok: bool = True
    error: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None


class ExtractionAgent:
    def __init__(self, pack: Pack, settings: Settings | None = None) -> None:
        self.pack = pack
        self.settings = settings or get_settings()
        self._agents: dict[str, Any] = {}

    @property
    def use_model(self) -> bool:
        return self.settings.llm_enabled

    # -- public API ---------------------------------------------------------

    async def extract(
        self,
        segment: DocumentSegment,
        *,
        doc_type: str | None = None,
        prefer_model: bool = False,
    ) -> ExtractionOutcome:
        """Extract records from one segment.

        The baseline runs first when it can read the document, because it is free
        and exact; the model is used for anything the baseline cannot read, and
        always when ``prefer_model`` is set (images, photos, handwriting).
        """
        resolved_type = doc_type or segment.doc_type
        if not resolved_type or resolved_type == "unknown":
            return ExtractionOutcome(records=[], schema_ok=False, error="document type is unknown")

        with get_tracer().span(
            "extract",
            doc_type=resolved_type,
            pack_version=self.pack.version,
            chars=len(segment.text),
        ) as span:
            baseline = extract_segment(segment, self.pack) if not prefer_model else []
            if baseline and self._baseline_acceptable(resolved_type, baseline[0]):
                span.update(model=BASELINE_MODEL, path="baseline")
                outcome = ExtractionOutcome(records=baseline, trace_id=span.trace_id)
                span.record_usage(model=BASELINE_MODEL, tokens_in=0, tokens_out=0, cost_usd=0.0)
                return outcome

            if not self.use_model:
                span.update(model=BASELINE_MODEL, path="baseline_only")
                # Nothing better available: return the baseline result and let the
                # confidence router send it to a reviewer.
                span.record_usage(model=BASELINE_MODEL, tokens_in=0, tokens_out=0, cost_usd=0.0)
                return ExtractionOutcome(records=baseline, trace_id=span.trace_id)

            try:
                outcome = await self._extract_with_model(segment, resolved_type)
            except ExtractionSchemaError as exc:
                span.update(error=str(exc))
                raise
            except Exception as exc:  # pragma: no cover - provider failures
                span.update(error=f"{type(exc).__name__}: {exc}")
                raise ExtractionSchemaError(
                    f"the extraction model call failed: {type(exc).__name__}"
                ) from exc

            outcome.trace_id = span.trace_id
            span.record_usage(
                model=outcome.model,
                tokens_in=outcome.usage.get("tokens_in"),
                tokens_out=outcome.usage.get("tokens_out"),
                cost_usd=outcome.usage.get("cost_usd"),
            )
            return outcome

    def extract_sync(self, segment: DocumentSegment, *, doc_type: str | None = None) -> ExtractionOutcome:
        """Baseline extraction only, for tests and scripts."""
        resolved = doc_type or segment.doc_type
        if not resolved or resolved == "unknown":
            return ExtractionOutcome(records=[], schema_ok=False, error="document type is unknown")
        return ExtractionOutcome(records=extract_segment(segment, self.pack))

    # -- baseline gate ------------------------------------------------------

    def _baseline_acceptable(self, doc_type: str, record: ExtractedRecord) -> bool:
        """Is the baseline read good enough to skip a model call?"""
        spec = self.pack.document(doc_type)
        if missing_mandatory(spec, record.payload):
            return False
        if record.overall_confidence < 0.9:
            return False
        # A line-item document with no line items is not a good read.
        if spec.line_item_key and not record.payload.get(spec.line_item_key):
            return False
        return True

    # -- model path ---------------------------------------------------------

    def _build_agent(self, doc_type: str) -> Any:
        if doc_type in self._agents:
            return self._agents[doc_type]

        try:  # pragma: no cover - requires credentials
            from pydantic_ai import Agent
        except Exception as exc:  # pragma: no cover
            raise ExtractionSchemaError(f"pydantic-ai is not installed: {exc}") from exc

        model = self._build_model()
        envelope = self._build_envelope(doc_type)
        instructions = self._instructions(doc_type)

        agent: Any
        try:  # pragma: no cover - depends on installed pydantic-ai version
            agent = Agent(model, output_type=envelope, instructions=instructions)
        except TypeError:  # pragma: no cover - older pydantic-ai
            agent = Agent(model, result_type=envelope, system_prompt=instructions)

        self._agents[doc_type] = agent
        return agent

    def _build_model(self) -> Any:  # pragma: no cover - requires credentials
        from pydantic_ai.providers.openai import OpenAIProvider

        provider = OpenAIProvider(
            base_url=self.settings.openai_base_url or None,
            api_key=self.settings.openai_api_key,
        )
        for attribute in ("OpenAIChatModel", "OpenAIModel"):
            try:
                module = __import__("pydantic_ai.models.openai", fromlist=[attribute])
                model_class = getattr(module, attribute)
            except (ImportError, AttributeError):
                continue
            try:
                return model_class(self.settings.vision_model, provider=provider)
            except TypeError:
                return model_class(
                    self.settings.vision_model,
                    provider=provider,
                    base_url=self.settings.openai_base_url or None,
                )
        raise ExtractionSchemaError("no usable OpenAI model class found in pydantic-ai")

    def _build_envelope(self, doc_type: str) -> Any:  # pragma: no cover - requires credentials
        from pydantic import BaseModel, ConfigDict, Field, create_model

        data_model = model_for_document(self.pack.document(doc_type))
        return create_model(
            "ExtractionEnvelope",
            __config__=ConfigDict(extra="forbid"),
            data=(data_model, Field(description="The extracted document")),
            confidence=(
                dict[str, float],
                Field(
                    default_factory=dict,
                    description=(
                        "Confidence 0..1 per field path, for example "
                        "{'vendor_gstin': 0.99, 'line_items.0.amount': 0.71}. "
                        "Use a low value for anything illegible rather than guessing."
                    ),
                ),
            ),
        )

    def _instructions(self, doc_type: str) -> str:
        spec = self.pack.document(doc_type)
        parts = [
            self.pack.hints.get("system_prompt", ""),
            "",
            schema_instructions(spec, self.pack.hints_for(doc_type)),
        ]
        language_notes = self.pack.hints.get("language_notes", [])
        if language_notes:
            parts += ["", "Languages:", *[f"- {note}" for note in language_notes]]
        disambiguation = self.pack.hints.get("field_disambiguation", {})
        if disambiguation:
            parts += ["", "Field disambiguation:", *[f"- {k}: {v}" for k, v in disambiguation.items()]]
        return "\n".join(part for part in parts if part is not None)

    async def _extract_with_model(
        self, segment: DocumentSegment, doc_type: str
    ) -> ExtractionOutcome:  # pragma: no cover - requires credentials
        from pydantic import ValidationError

        agent = self._build_agent(doc_type)
        data_model = model_for_document(self.pack.document(doc_type))
        prompt = (
            f"Extract this {self.pack.document(doc_type).label} into the required schema.\n\n"
            f"--- document text ---\n{segment.text}\n--- end ---"
        )

        attempts = 0
        last_error: str | None = None
        for attempt in range(1, MAX_SCHEMA_ATTEMPTS + 1):
            attempts = attempt
            message = prompt
            if last_error:
                message = (
                    f"{prompt}\n\nYour previous answer failed schema validation:\n{last_error}\n"
                    "Return a corrected answer that validates."
                )
            result = await agent.run(message)
            envelope = result.output if hasattr(result, "output") else result.data
            raw = getattr(envelope, "data", envelope)
            try:
                validated = data_model.model_validate(
                    raw.model_dump() if hasattr(raw, "model_dump") else raw
                )
            except ValidationError as exc:
                last_error = str(exc)
                continue
            confidence_map = dict(getattr(envelope, "confidence", {}) or {})
            record = self._record_from_validated(
                validated, confidence_map, doc_type, segment
            )
            usage = self._usage_from_result(result)
            return ExtractionOutcome(
                records=[record],
                model=self.settings.vision_model,
                attempts=attempts,
                schema_ok=True,
                usage=usage,
            )

        raise ExtractionSchemaError(
            f"output failed schema validation after {attempts} attempts: {last_error}"
        )

    def _record_from_validated(
        self,
        validated: Any,
        confidence_map: dict[str, float],
        doc_type: str,
        segment: DocumentSegment,
    ) -> ExtractedRecord:
        spec = self.pack.document(doc_type)
        payload = validated.model_dump()
        fields: list[FieldValue] = []

        for field_spec in spec.fields:
            value = payload.get(field_spec.name)
            if field_spec.type == "array":
                for index, row in enumerate(value or []):
                    for key, cell in (row or {}).items():
                        item_spec = next((s for s in field_spec.item_fields if s.name == key), None)
                        path = f"{field_spec.name}.{index}.{key}"
                        fields.append(
                            FieldValue(
                                path=path,
                                label=item_spec.label if item_spec else key,
                                value_text=None if cell is None else str(cell),
                                value_num=cell if isinstance(cell, Decimal) else None,
                                confidence=float(confidence_map.get(path, 0.85)),
                                page_no=segment.page_from,
                                source_span="model output",
                                mandatory=bool(item_spec and item_spec.mandatory),
                            )
                        )
                continue
            confidence = float(confidence_map.get(field_spec.name, 0.9 if value is not None else 0.0))
            fields.append(
                FieldValue(
                    path=field_spec.name,
                    label=field_spec.label,
                    value_text=None if value is None else str(value),
                    value_num=value if isinstance(value, Decimal) else None,
                    value_date=value if hasattr(value, "year") and not isinstance(value, str) else None,
                    confidence=confidence,
                    page_no=segment.page_from,
                    source_span="model output",
                    mandatory=field_spec.mandatory,
                )
            )

        payload["_extraction"] = {
            "model": self.settings.vision_model,
            "pack_version": self.pack.version,
            "prompt_version": PROMPT_VERSION,
        }
        return ExtractedRecord(
            doc_type=doc_type,
            pack_key=self.pack.key,
            pack_version=self.pack.version,
            payload=payload,
            fields=fields,
            page_from=segment.page_from,
            page_to=segment.page_to,
            order_index=segment.order_index,
            model=self.settings.vision_model,
            prompt_version=PROMPT_VERSION,
            schema_ok=True,
        )

    @staticmethod
    def _usage_from_result(result: Any) -> dict[str, Any]:  # pragma: no cover
        usage: dict[str, Any] = {}
        try:
            details = result.usage()
            usage["tokens_in"] = getattr(details, "request_tokens", None)
            usage["tokens_out"] = getattr(details, "response_tokens", None)
        except Exception:
            pass
        return usage


def read_document_text(path: str | pathlib.Path) -> str:
    """Read a document into text.

    A PDF with a text layer is read through the same path as a text file, because
    the pages arrive here already reduced to text by the intake step. Images are
    routed to the vision model instead: they have no text layer, so
    ``extract`` is called with only the model available.
    """
    file = pathlib.Path(path)
    suffix = file.suffix.lower()
    if suffix in {".txt", ".text", ".csv", ".tsv", ".md"}:
        return file.read_text(encoding="utf-8", errors="replace")
    if suffix == ".json":
        return json.dumps(json.loads(file.read_text(encoding="utf-8")), indent=2)
    raise ValueError(
        f"{suffix or 'this file type'} has no text layer reader; send it to the vision model"
    )


_agent_cache: dict[str, ExtractionAgent] = {}


def get_extraction_agent(pack: Pack) -> ExtractionAgent:
    key = f"{pack.key}@{pack.version}"
    if key not in _agent_cache:
        _agent_cache[key] = ExtractionAgent(pack)
    return _agent_cache[key]
