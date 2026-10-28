"""Bridge agent configuration.

The agent's own settings live next to it on the customer's PC. Only the cloud's
public key is here: the agent can verify commands but can never create one.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any

DEFAULT_PATH = pathlib.Path.home() / ".autonomous-data-entry" / "agent.json"


@dataclass
class AgentConfig:
    cloud_url: str = "http://127.0.0.1:8088"
    credential_id: str = ""
    # The cloud's Ed25519 public key, base64. Commands that fail verification are
    # refused, whatever they ask for.
    cloud_public_key: str = ""
    # TallyPrime's XML interface, which the customer must enable (Appendix E).
    tally_url: str = "http://127.0.0.1:9000"
    tally_company: str = ""
    tally_timeout_seconds: int = 30
    poll_seconds: int = 5
    # Local on-disk queue so a dropped connection or a closed Tally loses nothing.
    state_dir: str = str(DEFAULT_PATH.parent)
    require_company_match: bool = True
    app_version: str = "0.0.0"
    verbose: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def state_path(self) -> pathlib.Path:
        path = pathlib.Path(self.state_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def queue_path(self) -> pathlib.Path:
        return self.state_path / "queue.sqlite3"

    @property
    def status_path(self) -> pathlib.Path:
        return self.state_path / "status.json"

    @classmethod
    def load(cls, path: pathlib.Path | None = None) -> "AgentConfig":
        path = path or DEFAULT_PATH
        if not path.exists():
            raise FileNotFoundError(
                f"agent configuration not found at {path}. Run "
                "`python -m bridge_agent.cli init --cloud-url ... --credential-id ... "
                "--cloud-public-key ...` first, using the values printed at onboarding."
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        extra = {k: v for k, v in data.items() if k not in known}
        return cls(**{k: v for k, v in data.items() if k in known}, extra=extra)

    def save(self, path: pathlib.Path | None = None) -> pathlib.Path:
        path = path or DEFAULT_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {**{k: getattr(self, k) for k in self.__dataclass_fields__ if k != "extra"}, **self.extra}
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path
