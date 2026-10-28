"""Langfuse tracing (section 9: traces, cost per document, accuracy tracking).

Langfuse is optional in development: when no keys are configured the helpers
return lightweight no-op spans so every call site can stay unconditional and the
pipeline still runs offline.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from engine.settings import get_settings


@dataclass
class Span:
    """A trace span. Records what the autonomy gates need: model, tokens, cost."""

    name: str
    trace_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.perf_counter)
    ended_at: float | None = None
    error: str | None = None
    _sink: Any = None

    def update(self, **kwargs: Any) -> None:
        self.metadata.update(kwargs)

    def record_usage(
        self,
        *,
        model: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        if model is not None:
            self.metadata["model"] = model
        if tokens_in is not None:
            self.metadata["tokens_in"] = tokens_in
        if tokens_out is not None:
            self.metadata["tokens_out"] = tokens_out
        if cost_usd is not None:
            self.metadata["cost_usd"] = cost_usd

    @property
    def latency_ms(self) -> int:
        end = self.ended_at if self.ended_at is not None else time.perf_counter()
        return int((end - self.started_at) * 1000)

    def finish(self, error: str | None = None) -> None:
        self.ended_at = time.perf_counter()
        self.error = error
        if self._sink is not None:
            with contextlib.suppress(Exception):
                self._sink(self)


class Tracer:
    """Thin wrapper over the Langfuse client plus an in-memory span log.

    The in-memory log is what the tests assert on and what the accuracy
    dashboards read when Langfuse is not configured.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self.spans: list[Span] = []
        self._client = self._build_client()

    def _build_client(self) -> Any:
        if not self.settings.langfuse_enabled:
            return None
        try:  # pragma: no cover - requires Langfuse credentials
            from langfuse import Langfuse

            return Langfuse(
                public_key=self.settings.langfuse_public_key,
                secret_key=self.settings.langfuse_secret_key,
                host=self.settings.langfuse_host,
            )
        except Exception:  # pragma: no cover - degrade to local spans
            return None

    @contextlib.contextmanager
    def span(self, name: str, **metadata: Any) -> Iterator[Span]:
        span = Span(name=name, trace_id=metadata.pop("trace_id", new_trace_id()), metadata=metadata)
        span._sink = self._export
        try:
            yield span
        except Exception as exc:
            span.finish(error=f"{type(exc).__name__}: {exc}")
            raise
        else:
            span.finish()

    def _export(self, span: Span) -> None:  # pragma: no cover - network
        self.spans.append(span)
        if self._client is None:
            return
        with contextlib.suppress(Exception):
            self._client.trace(
                id=span.trace_id,
                name=span.name,
                metadata={**span.metadata, "latency_ms": span.latency_ms, "error": span.error},
            )

    def flush(self) -> None:  # pragma: no cover - network
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.flush()


def new_trace_id() -> str:
    import uuid

    return uuid.uuid4().hex


_tracer: Tracer | None = None


def get_tracer() -> Tracer:
    global _tracer
    if _tracer is None:
        _tracer = Tracer()
    return _tracer
