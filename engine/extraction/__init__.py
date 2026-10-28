"""Extraction (section 6.2): classification, splitting, and typed extraction."""

from engine.extraction.agent import ExtractionAgent, get_extraction_agent  # noqa: F401
from engine.extraction.classifier import ClassificationResult, classify, split_documents  # noqa: F401
from engine.extraction.schemas import build_pydantic_model  # noqa: F401

__all__ = [
    "ExtractionAgent",
    "get_extraction_agent",
    "ClassificationResult",
    "classify",
    "split_documents",
    "build_pydantic_model",
]
