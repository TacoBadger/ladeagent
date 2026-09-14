"""De rene tool-funktioner. Ingen model, ingen netværk: kun DataStore ind, dict ud.

Samme funktioner bruges tre steder, så de er testet én gang:
  - mcp_server.py eksponerer dem som MCP-tools
  - agent.py kalder dem in-process under tool use
  - evals/make_golden.py regner facit med dem (deterministisk)

Alle beløb er SPOTPRIS ekskl. nettarif, elafgift og moms. Det står i hvert svar,
så modellen ikke kan komme til at love en kunde en totalpris.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pandas as pd
from pydantic import BaseModel, Field, field_validator

from ladeagent import config
from ladeagent.data.eds import DataStore

PRICE_NOTE = "Spotpris (day-ahead) ekskl. nettarif, elafgift og moms. Kundens faktiske pris afhænger af elaftale og netselskab."


class ToolError(Exception):
    """Fejl der skal tilbage til modellen som is_error=True, ikke som exception."""


# --- Input-modeller (pydantic validerer før noget regnes) -------------------------

def _parse_local(ts: str, name: str) -> datetime:
    try:
        return datetime.fromisoformat(ts)
    except ValueError as e:
        raise ToolError(f"{name} skal være ISO-8601 lokal tid, fx 2026-09-15T22:00. Fik: {ts!r}") from e


class AreaModel(BaseModel):
    area: str = Field(description="Prisområde: DK1 (vest for Storebælt) eller DK2 (øst for Storebælt).")

    @field_validator("area")
    @classmethod
    def _area(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in config.AREAS:
            raise ToolError(f"Ukendt prisområde {v!r}. Tilladt: {', '.join(config.AREAS)}.")
        return v


class WindowModel(AreaModel):
    start: str = Field(description="Start, lokal dansk tid, ISO-8601 (fx 2026-09-15T22:00).")
    end: str = Field(description="Slut (eksklusiv), lokal dansk tid, ISO-8601.")

    def bounds(self) -> tuple[datetime, datetime]:
        s, e = _parse_local(self.start, "start"), _parse_local(self.end, "end")
        if e <= s:
            raise ToolError("end skal ligge efter start.")
        if e - s > timedelta(days=config.MAX_WINDOW_DAYS):
            raise ToolError(f"Vinduet må højst være {config.MAX_WINDOW_DAYS} dage.")
        return s.replace(minute=0, second=0, microsecond=0), e.replace(minute=0, second=0, microsecond=0)


# --- Hjælpere ------------------------------------------------------------------------

def _slice(df: pd.DataFrame, area: str, start: datetime, end: datetime) -> pd.DataFrame:
    m = (df["area"] == area) & (df["hour"] >= start) & (df["hour"] < end)
    return df.loc[m].sort_values("hour").reset_index(drop=True)


def _require(df: pd.DataFrame, what: str, area: str, start: datetime, end: datetime, store: DataStore, table: str) -> None:
    if df.empty:
        cov = store.coverage()[table]
        raise ToolError(
            f"Ingen {what} for {area} i {start.isoformat()}–{end.isoformat()}. "
            f"Data findes for {cov['from']} til {cov['to']}. Sig det ærligt til kunden i stedet for at gætte."
        )


def _iso(ts: pd.Timestamp | datetime) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%dT%H:%M")


# --- Read-only tools ------------------------------------------------------------------

def get_day_ahead_prices(store: DataStore, area: str, start: str, end: str) -> dict:
    p = WindowModel(area=area, start=start, end=end)
    s, e = p.bounds()
    df = _slice(store.prices, p.area, s, e)
    _require(df, "priser", p.area, s, e, store, "prices")
    hours = [{"hour_start": _iso(h), "dkk_per_kwh": round(float(v), 4)} for h, v in zip(df["hour"], df["dkk_per_kwh"])]
    cheapest = df.loc[df["dkk_per_kwh"].idxmin()]
    dearest = df.loc[df["dkk_per_kwh"].idxmax()]
    return {
        "area": p.area, "start": _iso(s), "end": _iso(e), "unit": "DKK/kWh", "resolution": "time",
        "hours": hours,
        "mean_dkk_per_kwh": round(float(df["dkk_per_kwh"].mean()), 4),
        "cheapest_hour": {"hour_start": _iso(cheapest["hour"]), "dkk_per_kwh": round(float(cheapest["dkk_per_kwh"]), 4)},
        "most_expensive_hour": {"hour_start": _iso(dearest["hour"]), "dkk_per_kwh": round(float(dearest["dkk_per_kwh"]), 4)},
        "note": PRICE_NOTE, "source": f"Energi Data Service / DayAheadPrices ({store.source})",
    }


def find_cheapest_window(store: DataStore, area: str, start: str, end: str, kwh: float, max_kw: float) -> dict:
    """Billigste SAMMENHÆNGENDE vindue af hele timer til at lade `kwh` med max `max_kw`."""
    p = WindowModel(area=area, start=start, end=end)
    if not (0 < kwh <= config.MAX_KWH):
        raise ToolError(f"kwh skal være mellem 0 og {config.MAX_KWH}.")
    if not (0 < max_kw <= config.MAX_KW):
        raise ToolError(f"max_kw skal være mellem 0 og {config.MAX_KW}.")
    s, e = p.bounds()
    df = _slice(store.prices, p.area, s, e)
    _require(df, "priser", p.area, s, e, store, "prices")
    n_hours = math.ceil(kwh / max_kw)
    if n_hours > len(df):
        raise ToolError(f"Det kræver {n_hours} timer at lade {kwh} kWh ved {max_kw} kW, men vinduet har kun {len(df)} timer med data.")
    prices = df["dkk_per_kwh"].to_numpy()
    hours = df["hour"].tolist()
    # Energi pr. time: fuld effekt i alle timer undtagen evt. sidste (rest).
    per_hour = [max_kw] * n_hours
    per_hour[-1] = kwh - max_kw * (n_hours - 1)
    best_i, best_cost, worst_i, worst_cost = 0, math.inf, 0, -math.inf
    for i in range(len(prices) - n_hours + 1):
        cost = float(sum(prices[i + k] * per_hour[k] for k in range(n_hours)))
        if cost < best_cost:
            best_i, best_cost = i, cost
        if cost > worst_cost:
            worst_i, worst_cost = i, cost
    now_cost = float(sum(prices[k] * per_hour[k] for k in range(n_hours)))  # start straks ved vinduets begyndelse
    return {
        "area": p.area, "search_start": _iso(s), "search_end": _iso(e),
        "kwh": kwh, "max_kw": max_kw, "hours_needed": n_hours,
        "best_window": {"start": _iso(hours[best_i]), "end": _iso(hours[best_i] + timedelta(hours=n_hours)),
                        "cost_dkk": round(best_cost, 2), "avg_dkk_per_kwh": round(best_cost / kwh, 4)},
        "worst_window": {"start": _iso(hours[worst_i]), "end": _iso(hours[worst_i] + timedelta(hours=n_hours)),
                         "cost_dkk": round(worst_cost, 2)},
        "if_started_now": {"start": _iso(hours[0]), "cost_dkk": round(now_cost, 2)},
        "savings_vs_now_dkk": round(now_cost - best_cost, 2),
        "savings_vs_worst_dkk": round(worst_cost - best_cost, 2),
        "note": PRICE_NOTE, "source": f"Energi Data Service / DayAheadPrices ({store.source})",
    }


def estimate_cost(store: DataStore, area: str, start: str, end: str, kw: float) -> dict:
    """Spotomkostning ved at køre et konstant forbrug på `kw` i hele vinduet (fx varmepumpe)."""
    p = WindowModel(area=area, start=start, end=end)
    if not (0 < kw <= config.MAX_KW):
        raise ToolError(f"kw skal være mellem 0 og {config.MAX_KW}.")
    s, e = p.bounds()
    df = _slice(store.prices, p.area, s, e)
    _require(df, "priser", p.area, s, e, store, "prices")
    expected_hours = int((e - s).total_seconds() // 3600)
    if len(df) < expected_hours:
        raise ToolError(f"Kun {len(df)} af {expected_hours} timer har prisdata i vinduet. Vælg et vindue inden for {store.coverage()['prices']}.")
    kwh = kw * len(df)
    cost = float((df["dkk_per_kwh"] * kw).sum())
    return {
        "area": p.area, "start": _iso(s), "end": _iso(e), "kw": kw, "hours": len(df), "kwh": round(kwh, 2),
        "cost_dkk": round(cost, 2), "avg_dkk_per_kwh": round(cost / kwh, 4),
        "note": PRICE_NOTE, "source": f"Energi Data Service / DayAheadPrices ({store.source})",
    }


def get_co2_intensity(store: DataStore, area: str, start: str, end: str) -> dict:
    p = WindowModel(area=area, start=start, end=end)
    s, e = p.bounds()
    df = _slice(store.co2, p.area, s, e)
    _require(df, "CO2-data", p.area, s, e, store, "co2")
    lo = df.loc[df["g_per_kwh"].idxmin()]
    hi = df.loc[df["g_per_kwh"].idxmax()]
    return {
        "area": p.area, "start": _iso(s), "end": _iso(e), "unit": "g CO2/kWh", "resolution": "time",
        "hours": [{"hour_start": _iso(h), "g_per_kwh": round(float(v), 1)} for h, v in zip(df["hour"], df["g_per_kwh"])],
        "mean_g_per_kwh": round(float(df["g_per_kwh"].mean()), 1),
        "greenest_hour": {"hour_start": _iso(lo["hour"]), "g_per_kwh": round(float(lo["g_per_kwh"]), 1)},
        "dirtiest_hour": {"hour_start": _iso(hi["hour"]), "g_per_kwh": round(float(hi["g_per_kwh"]), 1)},
        "note": "Prognose fra Energinet (CO2EmisProg). Tallet er for strømmen i nettet, ikke for kundens elaftale.",
        "source": f"Energi Data Service / CO2EmisProg ({store.source})",
    }


def get_generation_mix(store: DataStore, area: str, start: str, end: str) -> dict:
    p = WindowModel(area=area, start=start, end=end)
    s, e = p.bounds()
    df = _slice(store.mix, p.area, s, e)
    _require(df, "produktionsdata", p.area, s, e, store, "mix")
    cols = ["havvind", "landvind", "sol", "centrale_vaerker", "decentrale_vaerker"]
    totals = {c: float(df[c].clip(lower=0).sum()) for c in cols}
    total = sum(totals.values()) or 1.0
    shares = {c: round(100 * v / total, 1) for c, v in totals.items()}
    green = round(shares["havvind"] + shares["landvind"] + shares["sol"], 1)
    return {
        "area": p.area, "start": _iso(s), "end": _iso(e), "unit": "% af produktion i området",
        "shares_pct": shares, "wind_and_solar_pct": green,
        "note": "Kun produktion i prisområdet; import/eksport er ikke med. Realtidsdata, kun for tidspunkter der er passeret.",
        "source": f"Energi Data Service / ElectricityProdex5MinRealtime ({store.source})",
    }


# --- Registry: navn -> (funktion, beskrivelse, input-schema) ---------------------------
# Beskrivelserne er dem modellen ser. De er præcise om hvad tool'et IKKE gør.

_WINDOW_SCHEMA = {
    "type": "object",
    "properties": {
        "area": {"type": "string", "enum": list(config.AREAS), "description": "DK1 = vest for Storebælt (Jylland/Fyn), DK2 = øst (Sjælland/øerne)."},
        "start": {"type": "string", "description": "Start i lokal dansk tid, ISO-8601, fx 2026-09-15T22:00."},
        "end": {"type": "string", "description": "Slut (eksklusiv) i lokal dansk tid, ISO-8601. Max 7 dage efter start."},
    },
    "required": ["area", "start", "end"],
    "additionalProperties": False,
}

READ_TOOLS: dict[str, dict] = {
    "get_day_ahead_prices": {
        "fn": get_day_ahead_prices,
        "description": "Timepriser (spot/day-ahead) i DKK/kWh for et prisområde og tidsrum, plus billigste og dyreste time. "
                       "Ekskl. nettarif, elafgift og moms. Kan IKKE slå kundens egen elaftale eller faktura op.",
        "schema": _WINDOW_SCHEMA,
    },
    "find_cheapest_window": {
        "fn": find_cheapest_window,
        "description": "Finder det billigste sammenhængende vindue af hele timer til at lade en given mængde kWh med en given max-effekt, "
                       "inden for et søgevindue. Returnerer også hvad det ville koste at starte straks, og besparelsen. Spotpris ekskl. tariffer og afgifter.",
        "schema": {
            "type": "object",
            "properties": {
                **_WINDOW_SCHEMA["properties"],
                "kwh": {"type": "number", "description": f"Energi der skal lades, kWh (0–{config.MAX_KWH:g})."},
                "max_kw": {"type": "number", "description": f"Ladeeffekt, kW (0–{config.MAX_KW:g}). Typisk 11 for en ladeboks, 3.7 for et almindeligt stik."},
            },
            "required": ["area", "start", "end", "kwh", "max_kw"],
            "additionalProperties": False,
        },
    },
    "estimate_cost": {
        "fn": estimate_cost,
        "description": "Spotomkostning i DKK ved et konstant forbrug på kW i hele tidsrummet, fx en varmepumpe kl. 17–20. "
                       "Brug to kald for at sammenligne to tidsrum. Ekskl. tariffer og afgifter.",
        "schema": {
            "type": "object",
            "properties": {**_WINDOW_SCHEMA["properties"], "kw": {"type": "number", "description": f"Konstant effekt i kW (0–{config.MAX_KW:g})."}},
            "required": ["area", "start", "end", "kw"],
            "additionalProperties": False,
        },
    },
    "get_co2_intensity": {
        "fn": get_co2_intensity,
        "description": "CO2-intensitet (g CO2/kWh) time for time fra Energinets prognose, med grønneste og mest CO2-tunge time. Gælder strømmen i nettet, ikke en bestemt elaftale.",
        "schema": _WINDOW_SCHEMA,
    },
    "get_generation_mix": {
        "fn": get_generation_mix,
        "description": "Andel af produktionen i prisområdet fra havvind, landvind, sol og værker (procent) for et tidsrum der er passeret. Realtidsdata; kan ikke sige noget om fremtiden.",
        "schema": _WINDOW_SCHEMA,
    },
}
