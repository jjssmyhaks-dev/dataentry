"""GSTIN format validation and GSTN lookups.

Appendix B, "Decision: GSTN data source": production status and taxpayer checks
use the official GSTN source through an empanelled GST Suvidha Provider. The
third-party/fixture path exists for prototypes and local development only and is
never the production source. When a lookup is unavailable every dependent rule
degrades to a warning and the record is rechecked later, rather than blocking.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from engine.settings import get_settings

GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Structure: 2 state digits + 5 letters + 4 digits + 1 letter (PAN) + 1 entity
# code + 'Z' by default + 1 checksum character.
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z][0-9A-Z][0-9A-Z]$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")

STATE_CODES: dict[str, str] = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana", "07": "Delhi",
    "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram",
    "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh",
    "24": "Gujarat", "26": "Dadra and Nagar Haveli and Daman and Diu",
    "27": "Maharashtra", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep",
    "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh",
    "38": "Ladakh", "97": "Other Territory", "99": "Centre Jurisdiction",
}

STATE_NAME_TO_CODE: dict[str, str] = {name.lower(): code for code, name in STATE_CODES.items()}
_COMMON_ALIASES = {
    "orissa": "21",
    "pondicherry": "34",
    "uttaranchal": "05",
    "bombay": "27",
    "bangalore": "29",
    "bengaluru": "29",
    "gurgaon": "06",
    "gurugram": "06",
    "noida": "09",
    "dadra and nagar haveli": "26",
    "daman and diu": "26",
}

GstinState = Literal["active", "cancelled", "suspended", "inactive", "unknown"]


def normalise_gstin(value: Any) -> str | None:
    """GSTINs are stored uppercase and trimmed (Appendix A)."""
    if value is None:
        return None
    text = re.sub(r"\s+", "", str(value)).upper()
    return text or None


def gstin_checksum(first_fourteen: str) -> str:
    """The GSTN checksum character for the first 14 characters."""
    total = 0
    for index, char in enumerate(first_fourteen.upper()):
        value = GSTIN_CHARS.index(char)
        product = value * (2 if index % 2 else 1)
        total += product // len(GSTIN_CHARS) + product % len(GSTIN_CHARS)
    return GSTIN_CHARS[(len(GSTIN_CHARS) - (total % len(GSTIN_CHARS))) % len(GSTIN_CHARS)]


def validate_gstin(value: Any, *, allow_urp: bool = True) -> tuple[bool, str | None]:
    """Return ``(ok, reason)``. ``reason`` is plain language for the reviewer."""
    gstin = normalise_gstin(value)
    if not gstin:
        return False, "GSTIN is missing."
    if allow_urp and gstin in {"URP", "UNREGISTERED"}:
        return True, None
    if len(gstin) != 15:
        return False, f"GSTIN must be 15 characters, this one has {len(gstin)}."
    if not GSTIN_RE.match(gstin):
        return False, "GSTIN does not match the required state code, PAN and entity pattern."
    if gstin[:2] not in STATE_CODES:
        return False, f"State code {gstin[:2]} is not a valid GST state code."
    if not PAN_RE.match(gstin[2:12]):
        return False, "The PAN part of the GSTIN is not in the required pattern."
    if gstin_checksum(gstin[:14]) != gstin[14]:
        return False, "GSTIN checksum character does not match."
    return True, None


def state_code_of_gstin(value: Any) -> str | None:
    gstin = normalise_gstin(value)
    if gstin and len(gstin) >= 2 and gstin[:2] in STATE_CODES:
        return gstin[:2]
    return None


def state_code_of_place_of_supply(value: Any) -> str | None:
    """Accept '27', 'Maharashtra' or '27-Maharashtra'."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    code_match = re.match(r"^(\d{2})", text)
    if code_match and code_match.group(1) in STATE_CODES:
        return code_match.group(1)
    cleaned = re.sub(r"[^a-z ]", "", text.lower()).strip()
    if cleaned in STATE_NAME_TO_CODE:
        return STATE_NAME_TO_CODE[cleaned]
    return _COMMON_ALIASES.get(cleaned)


def state_name(code: str | None) -> str | None:
    return STATE_CODES.get(code) if code else None


# ── lookup ──────────────────────────────────────────────────────────────────


@dataclass
class GstinStatus:
    gstin: str
    state: GstinState = "unknown"
    legal_name: str | None = None
    trade_name: str | None = None
    taxpayer_type: str | None = None
    registration_date: dt.date | None = None
    source: str = "gsp"
    degraded: bool = False
    detail: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_cancelled(self) -> bool:
        return self.state in {"cancelled", "suspended", "inactive"}


class GstnUnavailable(RuntimeError):
    """Raised when no GSTN source can be reached; rules degrade to a warning."""


class GstnClient:
    """GSTN lookups through the GSP, with a prototype fixture and a cache.

    ``status()`` never raises: an unavailable source comes back as a degraded
    result so the rule produces a warning instead of a failure.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self._fixtures = self._load_fixtures()
        self._memory: dict[str, GstinStatus] = {}

    # -- fixture (prototype only) -------------------------------------------

    def _load_fixtures(self) -> dict[str, dict[str, Any]]:
        path = (
            self.settings.packs_dir
            / "manufacturing"
            / "eval"
            / "gstn_fixtures.json"
        )
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8")).get("gstins", {})

    def _fixture_status(self, gstin: str) -> GstinStatus | None:
        fixture = self._fixtures.get(gstin)
        if fixture is None:
            return None
        return GstinStatus(
            gstin=gstin,
            state=fixture.get("status", "unknown"),
            legal_name=fixture.get("legal_name"),
            trade_name=fixture.get("trade_name"),
            taxpayer_type=fixture.get("taxpayer_type"),
            source="fixture",
            degraded=False,
            detail="Prototype fixture data, not the production GSTN source.",
            raw=fixture,
        )

    # -- public API ---------------------------------------------------------

    async def status(self, gstin: str, *, force: bool = False, cache: dict | None = None) -> GstinStatus:
        gstin = normalise_gstin(gstin) or ""
        ok, reason = validate_gstin(gstin)
        if not ok:
            return GstinStatus(
                gstin=gstin,
                state="unknown",
                degraded=True,
                detail=f"Not looked up: {reason}",
            )

        if not force and gstin in self._memory:
            return self._memory[gstin]

        if cache is not None:
            cached = cache.get(gstin)
            if cached is not None and not force:
                self._memory[gstin] = cached
                return cached

        result: GstinStatus
        if self.settings.gsp_use_fixture or not self.settings.gsp_base_url:
            fixture = self._fixture_status(gstin)
            result = fixture or GstinStatus(
                gstin=gstin,
                state="active",
                source="fixture-default",
                degraded=True,
                detail=(
                    "No GSTN source is configured, so this GSTIN was not verified "
                    "against the GSTN. It is treated as unverified, not as active."
                ),
            )
            # An unconfigured production path must never read as a verified active
            # status, so the default result is flagged degraded.
            if fixture is None:
                result.state = "unknown"
        else:
            try:
                result = await self._call_gsp(gstin)
            except (GstnUnavailable, httpx.HTTPError) as exc:
                result = GstinStatus(
                    gstin=gstin,
                    state="unknown",
                    degraded=True,
                    detail=f"GSTN lookup unavailable: {type(exc).__name__}",
                )

        self._memory[gstin] = result
        return result

    async def _call_gsp(self, gstin: str) -> GstinStatus:  # pragma: no cover - network
        """Call the empanelled GSP.

        The exact payload and endpoint depend on the provider chosen during
        onboarding (still open in Appendix B), so this method keeps to the shape
        agreed with the provider and is exercised once credentials exist.
        """
        if not (self.settings.gsp_client_id and self.settings.gsp_client_secret):
            raise GstnUnavailable("GSP credentials are not configured")
        headers = {
            "client_id": self.settings.gsp_client_id,
            "client_secret": self.settings.gsp_client_secret,
        }
        async with httpx.AsyncClient(timeout=self.settings.gsp_lookup_timeout_seconds) as client:
            response = await client.get(
                f"{self.settings.gsp_base_url.rstrip('/')}/gstin/{gstin}",
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()

        return GstinStatus(
            gstin=gstin,
            state=_normalise_status(payload.get("status") or payload.get("sts")),
            legal_name=payload.get("legal_name") or payload.get("lgnm"),
            trade_name=payload.get("trade_name") or payload.get("tradeNam"),
            taxpayer_type=payload.get("taxpayer_type") or payload.get("dty"),
            source=f"gsp:{self.settings.gsp_provider or 'unset'}",
            raw=payload,
        )


def _normalise_status(value: Any) -> GstinState:
    text = str(value or "").strip().lower()
    mapping: dict[str, GstinState] = {
        "active": "active",
        "act": "active",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "cnl": "cancelled",
        "suspended": "suspended",
        "sus": "suspended",
        "inactive": "inactive",
        "ina": "inactive",
    }
    return mapping.get(text, "unknown")


_gstn: GstnClient | None = None


def get_gstn_client() -> GstnClient:
    global _gstn
    if _gstn is None:
        _gstn = GstnClient()
    return _gstn


def gstin_fixtures_path() -> pathlib.Path:
    return get_settings().packs_dir / "manufacturing" / "eval" / "gstn_fixtures.json"
