"""Shared fixtures.

The pack and the evaluation set are the same files the product ships, so a test
that passes here is a test about the real pack rather than about a copy of it.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# psycopg's async mode needs a selector loop; set before any test creates one.
if sys.platform == "win32":  # pragma: no cover - platform specific
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from engine.packs.loader import Pack, load_pack  # noqa: E402


@pytest.fixture(scope="session")
def pack() -> Pack:
    return load_pack("manufacturing")


@pytest.fixture(scope="session")
def docs_dir(pack: Pack) -> pathlib.Path:
    return pack.directory / pack.evaluation.documents_dir


@pytest.fixture(scope="session")
def gstn_fixtures(pack: Pack) -> dict:
    path = pack.directory / "eval" / "gstn_fixtures.json"
    return json.loads(path.read_text(encoding="utf-8")).get("gstins", {})


@pytest.fixture(scope="session")
def sample_text(docs_dir: pathlib.Path):
    def read(case_id: str) -> str:
        return (docs_dir / f"{case_id}.txt").read_text(encoding="utf-8")

    return read


@pytest.fixture(scope="session")
def sample_truth(docs_dir: pathlib.Path):
    def read(case_id: str) -> dict:
        return json.loads((docs_dir / f"{case_id}.truth.json").read_text(encoding="utf-8"))

    return read
