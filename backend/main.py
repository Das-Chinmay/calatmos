"""
main.py — FastAPI backend for CalAtmos: California Air Intelligence Platform.
Run: uvicorn backend.main:app --reload --port 8000
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .data_fetcher import (
    load_ghg_emissions,
    load_county_emissions,
    load_counties_geojson,
    load_aqi_data,
    get_data_status,
    fetch_purpleair_sensors,
    fetch_wildfires,
    fetch_openaq_stations,
    fetch_pollution_cities,
)
from .spatial import (
    build_hotspot_geojson,
    get_top_emitter_counties,
    get_most_improved_county,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="CalAtmos API",
    description="California Air Intelligence Platform — Chinmay Das | UCR MSCS",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")

# ---------------------------------------------------------------------------
# Core data cache (GHG / county / AQI)
# ---------------------------------------------------------------------------
_cache: dict[str, Any] = {}
_last_loaded: datetime | None = None


async def _ensure_data():
    global _last_loaded
    if _cache and _last_loaded:
        return
    logger.info("Loading core datasets ...")
    _cache["ghg"],      _cache["ghg_source"]    = await load_ghg_emissions()
    _cache["counties"], _cache["county_source"] = await load_county_emissions()
    _cache["geojson"]                           = await load_counties_geojson()
    _cache["aqi"],      _cache["aqi_source"]    = await load_aqi_data()
    _last_loaded = datetime.utcnow()
    logger.info(
        f"Core datasets ready - GHG: {_cache['ghg_source']}, "
        f"AQI: {_cache['aqi_source']}, County: {_cache['county_source']}"
    )


# ---------------------------------------------------------------------------
# Sensor cache (TTL-based, separate from core cache)
# ---------------------------------------------------------------------------
_sensor_cache: dict[str, dict] = {}


async def _get_sensor(key: str, fetch_fn, ttl_sec: int) -> tuple[Any, str]:
    """In-memory TTL wrapper for sensor endpoints."""
    now = datetime.utcnow()
    entry = _sensor_cache.get(key)
    if entry:
        age = (now - entry["at"]).total_seconds()
        if age < ttl_sec:
            return entry["data"], entry["source"]
    data, source = await fetch_fn()
    _sensor_cache[key] = {"data": data, "source": source, "at": now}
    return data, source


# ---------------------------------------------------------------------------
# Core endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/emissions/by-year")
async def emissions_by_year():
    await _ensure_data()
    df = _cache["ghg"]
    yearly = (
        df.groupby("year")["emissions_mmtco2e"]
        .sum().reset_index()
        .rename(columns={"emissions_mmtco2e": "total_mmtco2e"})
    )
    return {
        "labels":      yearly["year"].tolist(),
        "values":      [round(v, 2) for v in yearly["total_mmtco2e"].tolist()],
        "unit":        "MMTCO2e",
        "data_source": _cache.get("ghg_source", "unknown"),
    }


@app.get("/api/emissions/by-sector")
async def emissions_by_sector():
    await _ensure_data()
    df          = _cache["ghg"]
    latest_year = df["year"].max()
    sector_df   = (
        df[df["year"] == latest_year]
        .groupby("sector")["emissions_mmtco2e"].sum().reset_index()
        .sort_values("emissions_mmtco2e", ascending=False)
    )
    return {
        "year":        int(latest_year),
        "labels":      sector_df["sector"].tolist(),
        "values":      [round(v, 2) for v in sector_df["emissions_mmtco2e"].tolist()],
        "unit":        "MMTCO2e",
        "colors":      ["#6366f1","#f59e0b","#10b981","#ef4444","#3b82f6","#8b5cf6","#ec4899"],
        "data_source": _cache.get("ghg_source", "unknown"),
    }


@app.get("/api/emissions/by-county")
async def emissions_by_county():
    await _ensure_data()
    df = _cache["counties"].sort_values("emissions_mmtco2e_2023", ascending=False)
    return {
        "counties":       df["county"].tolist(),
        "emissions_2023": [round(v, 2) for v in df["emissions_mmtco2e_2023"].tolist()],
        "emissions_2022": [round(v, 2) for v in df["emissions_mmtco2e_2022"].tolist()],
        "yoy_change":     df["yoy_change_pct"].tolist(),
        "per_capita":     df["per_capita_tco2e"].tolist(),
        "primary_sector": df["primary_sector"].tolist(),
        "unit":           "MMTCO2e",
        "data_source":    _cache.get("county_source", "synthetic"),
    }


@app.get("/api/aqi/cities")
async def aqi_cities():
    await _ensure_data()
    return {
        "data":        _cache["aqi"],
        "parameter":   "Ozone / PM2.5 AQI",
        "year":        2023,
        "data_source": _cache.get("aqi_source", "unknown"),
    }


@app.get("/api/geospatial/hotspots")
async def geospatial_hotspots():
    await _ensure_data()
    return build_hotspot_geojson(_cache["geojson"], _cache["counties"])


@app.get("/api/summary")
async def summary():
    await _ensure_data()
    ghg      = _cache["ghg"]
    counties = _cache["counties"]

    latest_year = ghg["year"].max()
    prev_year   = latest_year - 1
    total_cur   = round(ghg[ghg["year"] == latest_year]["emissions_mmtco2e"].sum(), 1)
    total_prev  = round(ghg[ghg["year"] == prev_year]["emissions_mmtco2e"].sum(), 1)
    yoy_pct     = round((total_cur - total_prev) / total_prev * 100, 2)

    sector_agg    = ghg[ghg["year"] == latest_year].groupby("sector")["emissions_mmtco2e"].sum()
    top_sector    = sector_agg.idxmax()
    top_sector_val = round(sector_agg.max(), 1)

    top_emitters  = get_top_emitter_counties(counties)
    most_improved = get_most_improved_county(counties)

    return {
        "total_emissions_mmtco2e":     total_cur,
        "total_emissions_prev_year":   total_prev,
        "yoy_change_pct":              yoy_pct,
        "reporting_year":              int(latest_year),
        "top_emitting_sector":         top_sector,
        "top_emitting_sector_mmtco2e": top_sector_val,
        "top_county":                  top_emitters[0]["county"],
        "top_county_mmtco2e":          top_emitters[0]["emissions_mmtco2e_2023"],
        "top_5_counties":              top_emitters,
        "most_improved_county":        most_improved["county"],
        "most_improved_yoy_pct":       most_improved["yoy_change_pct"],
        "last_updated":                _last_loaded.isoformat() if _last_loaded else None,
        "data_source": {
            "ghg":    _cache.get("ghg_source",    "unknown"),
            "aqi":    _cache.get("aqi_source",    "unknown"),
            "county": _cache.get("county_source", "synthetic"),
        },
    }


@app.get("/api/data-status")
async def data_status():
    await _ensure_data()
    status = get_data_status()
    return {
        "carb_data":         _cache.get("ghg_source",    "unknown"),
        "aqi_data":          _cache.get("aqi_source",    "unknown"),
        "county_data":       _cache.get("county_source", "synthetic"),
        "last_fetch":        _last_loaded.isoformat() if _last_loaded else None,
        "data_through_year": status.get("data_through_year", 2023),
        "errors":            status.get("errors", []),
    }


# ---------------------------------------------------------------------------
# Sensor endpoints (PurpleAir / NASA FIRMS / OpenAQ)
# ---------------------------------------------------------------------------

@app.get("/api/sensors/purpleair")
async def sensors_purpleair():
    sensors, source = await _get_sensor("purpleair", fetch_purpleair_sensors, 300)
    return {
        "sensors":     sensors,
        "count":       len(sensors),
        "data_source": source,
        "timestamp":   datetime.utcnow().isoformat(),
    }


@app.get("/api/sensors/wildfires")
async def sensors_wildfires():
    fires, source = await _get_sensor("wildfires", fetch_wildfires, 900)
    return {
        "fires":       fires,
        "count":       len(fires),
        "data_source": source,
        "timestamp":   datetime.utcnow().isoformat(),
    }


@app.get("/api/sensors/openaq")
async def sensors_openaq():
    stations, source = await _get_sensor("openaq", fetch_openaq_stations, 300)

    # Auto-generate insight from the returned stations
    insight = ""
    if stations:
        by_pm25  = sorted(stations, key=lambda s: s["pm25"])
        lowest   = by_pm25[0]
        highest  = by_pm25[-1]
        low_name  = lowest["city"]  or lowest["name"]
        high_name = highest["city"] or highest["name"]
        insight = (
            f"{high_name} leading at {highest['pm25']} \u03bcg/m\u00b3 \u00b7 "
            f"{low_name} cleanest at {lowest['pm25']} \u03bcg/m\u00b3"
        )

    return {
        "stations":    stations,
        "count":       len(stations),
        "insight":     insight,
        "data_source": source,
        "timestamp":   datetime.utcnow().isoformat(),
    }


@app.get("/api/pollution/cities")
async def pollution_cities():
    cities, source = await _get_sensor("pollution", fetch_pollution_cities, 3600)
    return {
        "cities":      cities,
        "count":       len(cities),
        "data_source": source,
        "timestamp":   datetime.utcnow().isoformat(),
    }


@app.get("/api/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}
