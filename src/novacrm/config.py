"""Loads settings.yaml, the AE directory and the CSM roster; expands ${ENV} values."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return _expand(yaml.safe_load(fh) or {})


@dataclass
class AccountExecutive:
    name: str
    email: str
    phone: str


@dataclass
class CSM:
    name: str
    email: str


@dataclass
class Config:
    settings: dict
    aes: list[AccountExecutive]
    dedicated_csms: list[CSM]
    pooled_csm: CSM
    root: Path = field(default=ROOT)

    # convenience accessors -------------------------------------------------
    def tier(self, tier: str) -> dict:
        return self.settings["tiers"][tier]

    @property
    def voice(self) -> dict:
        return self.settings["voice"]

    @property
    def rocketlane(self) -> dict:
        return self.settings["rocketlane"]

    @property
    def gmail(self) -> dict:
        return self.settings["gmail"]

    def find_ae(self, email: str | None) -> AccountExecutive | None:
        if not email:
            return None
        email = email.strip().lower()
        return next((ae for ae in self.aes if ae.email.lower() == email), None)


def load_config(config_dir: Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    config_dir = config_dir or ROOT / "config"
    settings = _load_yaml(config_dir / "settings.yaml")
    aes_raw = _load_yaml(config_dir / "ae_directory.yaml").get("account_executives", [])
    roster = _load_yaml(config_dir / "csm_roster.yaml")
    return Config(
        settings=settings,
        aes=[AccountExecutive(**a) for a in aes_raw],
        dedicated_csms=[CSM(**c) for c in roster["dedicated"]],
        pooled_csm=CSM(**roster["pooled"]),
    )
