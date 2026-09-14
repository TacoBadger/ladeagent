"""Central konfiguration for ladeagenten.

Alt der begrænser hvad modellen kan få adgang til, står her: tilladte
prisområder, maksimalt tidsvindue, og hvilke datasæt der må læses.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# --- Adgangsgrænser for tools -------------------------------------------------
# Kun danske prisområder. Energi Data Service dækker CO2 og produktion for netop
# disse to, så alle tools taler om det samme.
AREAS: dict[str, str] = {"DK1": "Vestdanmark (vest for Storebælt)", "DK2": "Østdanmark (øst for Storebælt)"}
MAX_WINDOW_DAYS = 7          # et tool-kald må højst spænde over 7 dage
MAX_KWH = 500.0              # rimelig øvre grænse for en privat lade-/varmepumpeopgave
MAX_KW = 50.0

# --- Datakilder ----------------------------------------------------------------
EDS_BASE = "https://api.energidataservice.dk/dataset"
DATASETS = {
    "prices": "DayAheadPrices",               # 15-min day-ahead, DKK og EUR pr. MWh
    "co2": "CO2EmisProg",                     # 5-min CO2-prognose, g/kWh
    "mix": "ElectricityProdex5MinRealtime",   # 5-min produktion pr. kilde, MW
}
SNAPSHOT_DIR = ROOT / "data" / "snapshot"
CACHE_DIR = ROOT / "data" / "cache"
PLANS_FILE = ROOT / "data" / "plans.json"
TRACE_DIR = ROOT / "traces"

# Snapshot-vinduet. Evals kører ALTID mod snapshottet, aldrig mod live data,
# så resultatet er det samme uanset hvornår man kører dem.
SNAPSHOT_START = "2026-09-07"
SNAPSHOT_END = "2026-09-16"      # eksklusiv
SNAPSHOT_TODAY = "2026-09-14"    # "i dag" set fra agentens synspunkt under evals

# --- Model og pris -------------------------------------------------------------
DEFAULT_MODEL = os.getenv("LADEAGENT_MODEL", "claude-opus-5")
JUDGE_MODEL = "claude-haiku-4-5"

# USD pr. 1M tokens (Anthropic first-party, sept. 2026). Cache-læsning 0,1x,
# cache-skrivning 1,25x af inputprisen.
PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
}
USD_TO_DKK = 6.9  # fast kurs til rapportering, dokumenteret i README


def price_for(model: str) -> tuple[float, float]:
    """Returnerer (input, output) USD pr. 1M tokens. Ukendt model -> Opus-pris (konservativt)."""
    return PRICES_USD_PER_MTOK.get(model, PRICES_USD_PER_MTOK["claude-opus-5"])
