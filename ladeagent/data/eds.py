"""Klient mod Energinet's Energi Data Service (ingen nøgle nødvendig).

To lag:
  1. `fetch_records`  – rå kald med disk-cache, så samme forespørgsel aldrig
     rammer API'et to gange.
  2. `DataStore`      – normaliserede pandas-tabeller i lokal dansk tid, time-
     opløsning, som tools.py regner på. Kan indlæses fra et frosset snapshot
     (evals) eller fra live cache (drift).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from ladeagent import config


def _cache_path(dataset: str, start: str, end: str, areas: list[str]) -> Path:
    key = hashlib.sha1(f"{dataset}|{start}|{end}|{','.join(sorted(areas))}".encode()).hexdigest()[:16]
    return config.CACHE_DIR / f"{dataset}_{start}_{end}_{key}.json"


def fetch_records(dataset: str, start: str, end: str, areas: list[str], use_cache: bool = True) -> list[dict]:
    """Henter alle rækker for et datasæt i [start, end) for de givne prisområder."""
    path = _cache_path(dataset, start, end, areas)
    if use_cache and path.exists():
        return json.loads(path.read_text())
    params = {
        "start": f"{start}T00:00",
        "end": f"{end}T00:00",
        "filter": json.dumps({"PriceArea": areas}),
        "limit": 0,  # 0 = alle rækker
    }
    r = requests.get(f"{config.EDS_BASE}/{dataset}", params=params, timeout=60)
    r.raise_for_status()
    records = r.json()["records"]
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records))
    return records


# --- Normalisering ---------------------------------------------------------------

def prices_frame(records: list[dict]) -> pd.DataFrame:
    """15-min day-ahead -> timepriser i DKK/kWh, lokal tid (TimeDK)."""
    df = pd.DataFrame(records)
    if df.empty:
        return pd.DataFrame(columns=["area", "hour", "dkk_per_kwh", "eur_per_mwh"])
    df["hour"] = pd.to_datetime(df["TimeDK"]).dt.floor("h")
    df["dkk_per_kwh"] = df["DayAheadPriceDKK"] / 1000.0
    out = (
        df.groupby(["PriceArea", "hour"], as_index=False)
        .agg(dkk_per_kwh=("dkk_per_kwh", "mean"), eur_per_mwh=("DayAheadPriceEUR", "mean"))
        .rename(columns={"PriceArea": "area"})
        .sort_values(["area", "hour"])
        .reset_index(drop=True)
    )
    return out


def co2_frame(records: list[dict]) -> pd.DataFrame:
    """5-min CO2-prognose -> timegennemsnit g/kWh, lokal tid."""
    df = pd.DataFrame(records)
    if df.empty:
        return pd.DataFrame(columns=["area", "hour", "g_per_kwh"])
    df["hour"] = pd.to_datetime(df["Minutes5DK"]).dt.floor("h")
    return (
        df.groupby(["PriceArea", "hour"], as_index=False)
        .agg(g_per_kwh=("CO2Emission", "mean"))
        .rename(columns={"PriceArea": "area"})
        .sort_values(["area", "hour"])
        .reset_index(drop=True)
    )


_MIX_COLS = {
    "OffshoreWindPower": "havvind",
    "OnshoreWindPower": "landvind",
    "SolarPower": "sol",
    "ProductionGe100MW": "centrale_vaerker",
    "ProductionLt100MW": "decentrale_vaerker",
}


def mix_frame(records: list[dict]) -> pd.DataFrame:
    """5-min produktion (MW) -> timegennemsnit pr. kilde, lokal tid."""
    df = pd.DataFrame(records)
    if df.empty:
        return pd.DataFrame(columns=["area", "hour", *_MIX_COLS.values()])
    df["hour"] = pd.to_datetime(df["Minutes5DK"]).dt.floor("h")
    agg = {new: (old, "mean") for old, new in _MIX_COLS.items()}
    out = (
        df.groupby(["PriceArea", "hour"], as_index=False)
        .agg(**agg)
        .rename(columns={"PriceArea": "area"})
        .sort_values(["area", "hour"])
        .reset_index(drop=True)
    )
    return out.fillna(0.0)


# --- DataStore --------------------------------------------------------------------

@dataclass
class DataStore:
    prices: pd.DataFrame
    co2: pd.DataFrame
    mix: pd.DataFrame
    source: str  # "snapshot" eller "live"

    @classmethod
    def from_snapshot(cls, snapshot_dir: Path | None = None) -> "DataStore":
        d = snapshot_dir or config.SNAPSHOT_DIR
        return cls(
            prices=pd.read_parquet(d / "prices.parquet"),
            co2=pd.read_parquet(d / "co2.parquet"),
            mix=pd.read_parquet(d / "mix.parquet"),
            source="snapshot",
        )

    @classmethod
    def live(cls, days_back: int = 2, days_forward: int = 2, areas: list[str] | None = None) -> "DataStore":
        areas = areas or list(config.AREAS)
        today = date.today()
        start = (today - timedelta(days=days_back)).isoformat()
        end = (today + timedelta(days=days_forward)).isoformat()
        return cls(
            prices=prices_frame(fetch_records(config.DATASETS["prices"], start, end, areas)),
            co2=co2_frame(fetch_records(config.DATASETS["co2"], start, end, areas)),
            mix=mix_frame(fetch_records(config.DATASETS["mix"], start, end, areas)),
            source="live",
        )

    def coverage(self) -> dict[str, dict[str, str]]:
        """Hvilket tidsrum hver tabel dækker. Bruges af tools til ærlige 'ingen data'-svar."""
        out = {}
        for name in ("prices", "co2", "mix"):
            df = getattr(self, name)
            if df.empty:
                out[name] = {"from": "", "to": ""}
            else:
                out[name] = {"from": df["hour"].min().isoformat(), "to": df["hour"].max().isoformat()}
        return out


def build_snapshot(start: str = config.SNAPSHOT_START, end: str = config.SNAPSHOT_END) -> dict[str, int]:
    """Henter data fra EDS og fryser det som parquet i data/snapshot/."""
    areas = list(config.AREAS)
    config.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    frames = {
        "prices": prices_frame(fetch_records(config.DATASETS["prices"], start, end, areas, use_cache=False)),
        "co2": co2_frame(fetch_records(config.DATASETS["co2"], start, end, areas, use_cache=False)),
        "mix": mix_frame(fetch_records(config.DATASETS["mix"], start, end, areas, use_cache=False)),
    }
    for name, df in frames.items():
        df.to_parquet(config.SNAPSHOT_DIR / f"{name}.parquet", index=False)
    meta = {"start": start, "end": end, "areas": areas, "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "rows": {k: int(len(v)) for k, v in frames.items()}}
    (config.SNAPSHOT_DIR / "meta.json").write_text(json.dumps(meta, indent=2))
    return meta["rows"]
