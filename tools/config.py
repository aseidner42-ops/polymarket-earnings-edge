"""Shared configuration: paths, env vars, and weights.yaml loading.

Single source of truth for where things live and how the model is tuned.
Import this everywhere instead of hard-coding paths or re-reading .env.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

import yaml
from dotenv import load_dotenv

# --- paths ------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = ROOT / "tools"
CONFIG_DIR = ROOT / "config"
TMP_DIR = ROOT / ".tmp"
OUTPUT_DIR = ROOT / "output"
WEIGHTS_PATH = CONFIG_DIR / "weights.yaml"
EDGE_LOG_PATH = OUTPUT_DIR / "edge_log.csv"

TMP_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# --- env --------------------------------------------------------------------
load_dotenv(ROOT / ".env")


def get_finnhub_key() -> str:
    key = os.getenv("FINNHUB_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "FINNHUB_API_KEY missing. Add it to .env: FINNHUB_API_KEY=your_key"
        )
    return key


# --- weights / tuning -------------------------------------------------------
@lru_cache(maxsize=1)
def load_weights() -> Dict[str, Any]:
    """Load and cache config/weights.yaml. Fail loudly if malformed."""
    if not WEIGHTS_PATH.exists():
        raise RuntimeError(f"Missing weights config: {WEIGHTS_PATH}")
    with open(WEIGHTS_PATH, "r") as fh:
        cfg = yaml.safe_load(fh)
    for section in ("weights", "logistic", "confidence", "defaults", "frictions"):
        if section not in cfg:
            raise RuntimeError(f"weights.yaml missing required section: {section}")
    return cfg
