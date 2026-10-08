"""Central konfiguration for ladeagenten.

Alt der begrænser hvad modellen kan få adgang til, står her: tilladte
prisområder, maksimalt tidsvindue, hvilke datasæt der må læses, og hvor meget
én samtale må koste og slå op (runde 2).
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

# "I nat" i kundesprog: fra kl. 22 i dag til kl. 07 i morgen (lokal tid). Samme
# definition bruges i prompt v2, i MCP-serverens instruktioner og i evals-facit.
NIGHT_START_HOUR = 22
NIGHT_END_HOUR = 7

# --- Lofter pr. samtale (runde 2) ----------------------------------------------
# Hårde lofter i agent-loopet: når et af dem nås, stopper samtalen, og kunden får
# en fast besked om at spørge mere konkret. Lofterne ligger i kode, ikke i prompten.
MAX_COST_DKK_PER_CONVERSATION = 2.0
MAX_TOOL_CALLS_PER_CONVERSATION = 10

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
AUDIT_FILE = ROOT / "data" / "audit.jsonl"          # én linje pr. oprettet ladeplan (audit trail)
TRACE_DIR = ROOT / "traces"                          # kort jsonl-spor pr. dag (som hidtil)
TRACE_STORE = ROOT / "evals" / "reports" / "traces"  # fuldt trace pr. samtale, ét trace-id pr. svar

# Snapshot-vinduet. Evals kører ALTID mod snapshottet, aldrig mod live data,
# så resultatet er det samme uanset hvornår man kører dem.
SNAPSHOT_START = "2026-09-07"
SNAPSHOT_END = "2026-09-16"      # eksklusiv
SNAPSHOT_TODAY = "2026-09-14"    # "i dag" set fra agentens synspunkt under evals

# --- Model og pris -------------------------------------------------------------
DEFAULT_MODEL = os.getenv("LADEAGENT_MODEL", "claude-opus-5")
JUDGE_MODEL = "claude-haiku-4-5"

# USD pr. 1M tokens (Anthropic first-party, listepriser okt. 2026). Cache-læsning
# 0,1x af inputprisen (Fable 5.1: 0,025x), cache-skrivning (5 min) 1,25x.
# Haiku 5.5: prisen gælder prompts op til 100k tokens; agentens prompts er langt under.
PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
}
CACHE_READ_FACTOR: dict[str, float] = {"claude-fable-5-1": 0.025}
DEFAULT_CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25
USD_TO_DKK = 6.9  # fast kurs til rapportering, dokumenteret i README

# Pæne navne til rapporter og forsiden
MODEL_NAMES: dict[str, str] = {
    "claude-fable-5-1": "Claude Fable 5.1", "claude-opus-5-5": "Claude Opus 5.5", "claude-opus-5": "Claude Opus 5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5", "claude-sonnet-5": "Claude Sonnet 5",
    "claude-haiku-5-5": "Claude Haiku 5.5", "claude-haiku-4-5": "Claude Haiku 4.5",
}


def price_for(model: str) -> tuple[float, float]:
    """Returnerer (input, output) USD pr. 1M tokens. Ukendt model er en fejl, ikke et gæt."""
    if model not in PRICES_USD_PER_MTOK:
        raise KeyError(f"Ingen listepris for {model!r} i config.PRICES_USD_PER_MTOK. Tilføj den før kørsel.")
    return PRICES_USD_PER_MTOK[model]


def cache_read_factor(model: str) -> float:
    return CACHE_READ_FACTOR.get(model, DEFAULT_CACHE_READ_FACTOR)
