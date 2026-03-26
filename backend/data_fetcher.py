"""
data_fetcher.py — Fetches real California GHG + AQI data.

Priority chain per source:
  GHG : CARB Excel (live download) → cached Excel → synthetic
  AQI : AirNow API (live, keyed)   → cached JSON  → synthetic
  County / GeoJSON: always synthetic / GH-hosted (no live replacement)

Every public load_* function returns (data, source_string) where
source_string ∈ {"live", "cached", "synthetic"}.
get_data_status() exposes the last-run summary for /api/data-status.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
from dotenv import load_dotenv

# Load .env from project root (one level above backend/)
load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
AIRNOW_KEY     = os.getenv("AIRNOW_API_KEY",    "")
NASA_FIRMS_KEY = os.getenv("NASA_FIRMS_KEY",    "")
PURPLEAIR_KEY  = os.getenv("PURPLEAIR_API_KEY", "")
OPENAQ_KEY     = os.getenv("OPENAQ_API_KEY",    "")

CARB_EXCEL_URL = (
    "https://ww2.arb.ca.gov/sites/default/files/2025-11/"
    "nc-2000-2023_ghg_inventory_trends_figures.xlsx"
)
COUNTIES_GEOJSON_URL = (
    "https://raw.githubusercontent.com/codeforamerica/click_that_hood"
    "/master/public/data/california-counties.geojson"
)

AIRNOW_CITIES = {
    "Los Angeles":  {"lat": 34.0522,  "lon": -118.2437},
    "San Diego":    {"lat": 32.7157,  "lon": -117.1611},
    "Riverside":    {"lat": 33.9806,  "lon": -117.3755},
    "Sacramento":   {"lat": 38.5816,  "lon": -121.4944},
    "Fresno":       {"lat": 36.7378,  "lon": -119.7871},
}

# ---------------------------------------------------------------------------
# Module-level status dict — updated by every load call
# ---------------------------------------------------------------------------
_status: dict = {
    "ghg_source":          "unknown",
    "aqi_source":          "unknown",
    "county_source":       "synthetic",
    "data_through_year":   2023,
    "last_fetch":          None,
    "errors":              [],
}


def get_data_status() -> dict:
    return dict(_status)


# ---------------------------------------------------------------------------
# Domain constants (shared by live parser + synthetic generator)
# ---------------------------------------------------------------------------
SECTORS = [
    "Transportation",
    "Industrial",
    "Agriculture",
    "Electric Power",
    "Commercial & Residential",
    "Recycling & Waste",
    "High GWP",
]
SECTOR_SHARES = [0.372, 0.222, 0.084, 0.147, 0.095, 0.038, 0.042]

# Keywords used when scanning Excel rows for sector names
_EXCEL_SECTOR_KEYWORDS: list[tuple[str, str]] = [
    ("transportation",      "Transportation"),
    ("on-road",             "Transportation"),
    ("off-road",            "Transportation"),
    ("industrial",          "Industrial"),
    ("manufacturing",       "Industrial"),
    ("oil and gas",         "Industrial"),
    ("oil & gas",           "Industrial"),
    ("fugitive",            "Industrial"),
    ("agriculture",         "Agriculture"),
    ("livestock",           "Agriculture"),
    ("electric power",      "Electric Power"),
    ("electricity",         "Electric Power"),
    ("in-state",            "Electric Power"),
    ("commercial",          "Commercial & Residential"),
    ("residential",         "Commercial & Residential"),
    ("recycl",              "Recycling & Waste"),
    ("waste",               "Recycling & Waste"),
    ("landfill",            "Recycling & Waste"),
    ("high gwp",            "High GWP"),
    ("high global",         "High GWP"),
    ("fluorinated",         "High GWP"),
    ("solvent",             "High GWP"),
]

CA_COUNTIES = [
    "Alameda", "Alpine", "Amador", "Butte", "Calaveras", "Colusa",
    "Contra Costa", "Del Norte", "El Dorado", "Fresno", "Glenn",
    "Humboldt", "Imperial", "Inyo", "Kern", "Kings", "Lake", "Lassen",
    "Los Angeles", "Madera", "Marin", "Mariposa", "Mendocino", "Merced",
    "Modoc", "Mono", "Monterey", "Napa", "Nevada", "Orange", "Placer",
    "Plumas", "Riverside", "Sacramento", "San Benito", "San Bernardino",
    "San Diego", "San Francisco", "San Joaquin", "San Luis Obispo",
    "San Mateo", "Santa Barbara", "Santa Clara", "Santa Cruz", "Shasta",
    "Sierra", "Siskiyou", "Solano", "Sonoma", "Stanislaus", "Sutter",
    "Tehama", "Trinity", "Tulare", "Tuolumne", "Ventura", "Yolo", "Yuba",
]

COUNTY_SECTOR_MAP = {
    "Los Angeles":     "Transportation",   "San Bernardino":  "Transportation",
    "Riverside":       "Transportation",   "Orange":          "Transportation",
    "San Diego":       "Transportation",   "Sacramento":      "Transportation",
    "Ventura":         "Transportation",   "San Francisco":   "Transportation",
    "San Mateo":       "Transportation",   "Santa Clara":     "Industrial",
    "Alameda":         "Industrial",       "Contra Costa":    "Industrial",
    "Kern":            "Industrial",       "Solano":          "Industrial",
    "Fresno":          "Agriculture",      "San Joaquin":     "Agriculture",
    "Stanislaus":      "Agriculture",      "Tulare":          "Agriculture",
    "Kings":           "Agriculture",      "Merced":          "Agriculture",
    "Madera":          "Agriculture",      "Imperial":        "Agriculture",
    "Colusa":          "Agriculture",      "Glenn":           "Agriculture",
    "Sutter":          "Agriculture",      "Yolo":            "Agriculture",
    "Monterey":        "Agriculture",      "San Luis Obispo": "Agriculture",
    "Santa Barbara":   "Commercial & Residential",
    "Napa":            "Commercial & Residential",
    "Sonoma":          "Commercial & Residential",
    "Shasta":          "Electric Power",   "Lassen":          "Electric Power",
}

COUNTY_BASE = {
    "Los Angeles": 180.0, "San Bernardino": 72.0, "Riverside": 65.0,
    "Orange": 55.0, "San Diego": 50.0, "Sacramento": 28.0,
    "Kern": 40.0, "Fresno": 22.0, "Alameda": 20.0,
    "Santa Clara": 18.0, "Contra Costa": 17.0, "San Joaquin": 14.0,
    "Ventura": 12.0, "Stanislaus": 10.0, "Tulare": 9.5,
}

# ---------------------------------------------------------------------------
# Synthetic generators (deterministic seeds for reproducibility)
# ---------------------------------------------------------------------------

def _generate_ghg_data() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    rows = []
    for y in range(2000, 2024):
        if y <= 2006:
            total = 493 + rng.normal(0, 3)
        elif y == 2020:
            total = 395 + rng.normal(0, 2)
        else:
            total = 493 * (0.988 ** (y - 2000)) + rng.normal(0, 2)
        for sector, share in zip(SECTORS, SECTOR_SHARES):
            mod = 1.0
            if sector == "Transportation":
                mod = 0.991 ** (y - 2000)
            elif sector == "Electric Power":
                mod = 0.972 ** (y - 2000)
            elif sector == "Agriculture":
                mod = 1.003 ** (y - 2000)
            val = total * share * mod + rng.normal(0, 0.5)
            rows.append({"year": y, "sector": sector, "emissions_mmtco2e": round(val, 2)})
    return pd.DataFrame(rows)


def _generate_county_data() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    rows = []
    for county in CA_COUNTIES:
        base = COUNTY_BASE.get(county, rng.uniform(1.5, 8.0))
        v23 = base * 0.92  + rng.normal(0, base * 0.04)
        v22 = base * 0.935 + rng.normal(0, base * 0.04)
        rows.append({
            "county":                 county,
            "emissions_mmtco2e_2023": round(max(v23, 0.1), 2),
            "emissions_mmtco2e_2022": round(max(v22, 0.1), 2),
            "yoy_change_pct":         round((v23 - v22) / v22 * 100, 1),
            "primary_sector":         COUNTY_SECTOR_MAP.get(county, rng.choice(SECTORS)),
            "per_capita_tco2e":       round(rng.uniform(4.5, 18.0), 1),
        })
    return pd.DataFrame(rows)


def _generate_aqi_data() -> dict:
    rng = np.random.default_rng(13)
    profiles = {
        "Los Angeles": {"base": 78,  "peak": 145},
        "San Diego":   {"base": 52,  "peak":  98},
        "Riverside":   {"base": 90,  "peak": 165},
        "Sacramento":  {"base": 58,  "peak": 115},
        "Fresno":      {"base": 85,  "peak": 160},
    }
    months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    result = {}
    for city, p in profiles.items():
        monthly = []
        for i in range(12):
            if 5 <= i <= 8:
                val = p["peak"] * rng.uniform(0.85, 1.0)
            elif i in (9, 10):
                val = p["peak"] * rng.uniform(0.70, 0.95)
            else:
                val = p["base"] * rng.uniform(0.80, 1.10)
            monthly.append(round(val))
        result[city] = {"months": months, "aqi": monthly,
                        "annual_avg": round(sum(monthly) / 12), "live_aqi": None}
    return result


# ---------------------------------------------------------------------------
# CARB Excel parser
# ---------------------------------------------------------------------------

def _map_sector(raw: str) -> str | None:
    """Map a raw Excel cell string to one of our canonical sector names."""
    lower = raw.lower().strip()
    for keyword, canonical in _EXCEL_SECTOR_KEYWORDS:
        if keyword in lower:
            return canonical
    return None


def _parse_carb_excel(path: Path) -> tuple[pd.DataFrame | None, int | None]:
    """
    Scan every sheet of the CARB Excel for a table with year-columns (2000–2023)
    and sector-name rows.  Returns (long_df, data_through_year) or (None, None).
    """
    try:
        xl = pd.ExcelFile(path, engine="openpyxl")
    except Exception as e:
        logger.error(f"Cannot open Excel file: {e}")
        return None, None

    logger.info(f"CARB Excel sheets ({len(xl.sheet_names)}): {xl.sheet_names[:8]}")
    TARGET_YEARS = set(range(2000, 2025))

    for sheet in xl.sheet_names:
        try:
            raw = xl.parse(sheet, header=None)
            if raw.empty or raw.shape[0] < 4 or raw.shape[1] < 5:
                continue

            # ── Find a header row that contains ≥10 year integers ──────
            header_row_idx = None
            year_col_map: dict[int, int] = {}

            for r in range(min(25, len(raw))):
                hits: dict[int, int] = {}
                for c, val in enumerate(raw.iloc[r]):
                    try:
                        yr = int(float(str(val).replace(",", "").strip()))
                        if yr in TARGET_YEARS:
                            hits[yr] = c
                    except (ValueError, TypeError):
                        pass
                if len(hits) >= 10:
                    header_row_idx = r
                    year_col_map = hits
                    break

            if header_row_idx is None:
                continue

            logger.info(
                f"Sheet '{sheet}': year header at row {header_row_idx}, "
                f"years {min(year_col_map)} – {max(year_col_map)}"
            )

            # ── Walk rows below the header, extract sector + values ─────
            rows_out: list[dict] = []
            seen_sectors: set[str] = set()

            for r in range(header_row_idx + 1, len(raw)):
                row = raw.iloc[r]
                # Sector label is in the first non-empty text cell (cols 0-3)
                label = ""
                for c in range(min(4, len(row))):
                    cell = row.iloc[c]
                    if pd.notna(cell) and str(cell).strip() not in ("", "nan"):
                        label = str(cell).strip()
                        break
                if not label:
                    continue

                sector = _map_sector(label)
                if sector is None:
                    continue

                # Prefer the first occurrence of each sector per sheet to avoid
                # picking up sub-sector rows that accidentally match keywords.
                if sector in seen_sectors:
                    continue
                seen_sectors.add(sector)

                for year, col in year_col_map.items():
                    try:
                        v = float(str(row.iloc[col]).replace(",", ""))
                        if pd.notna(v) and 0 < v < 1_000_000:
                            # CARB sometimes stores values ×1000 (TGCO2e not MMTCO2e)
                            # Sanity check: CA Transportation in 2000 ≈ 170 MMTCO2e
                            rows_out.append({
                                "year": year,
                                "sector": sector,
                                "emissions_mmtco2e": round(v, 2),
                            })
                    except (ValueError, TypeError, IndexError):
                        pass

            if len(rows_out) < 20:
                continue

            df = pd.DataFrame(rows_out)

            # ── Unit sanity check ────────────────────────────────────────
            # CA Transportation 2000 should be ~150-200 MMTCO2e.
            # If median transport value is >10 000, values are in MTCO2e → ÷ 1e6
            # If median is >500, likely KTCO2e → ÷ 1e3
            transport_rows = df[df["sector"] == "Transportation"]["emissions_mmtco2e"]
            if len(transport_rows):
                med = transport_rows.median()
                if med > 10_000:
                    df["emissions_mmtco2e"] = (df["emissions_mmtco2e"] / 1_000_000).round(2)
                    logger.info("Unit correction: ÷1e6 (values were in MTCO2e)")
                elif med > 500:
                    df["emissions_mmtco2e"] = (df["emissions_mmtco2e"] / 1_000).round(2)
                    logger.info("Unit correction: ÷1e3 (values were in KTCO2e)")

            # Deduplicate (same sector may appear in multiple matching rows)
            df = df.groupby(["year", "sector"])["emissions_mmtco2e"].sum().reset_index()
            through_year = int(df["year"].max())

            logger.info(
                f"Parsed {len(df)} sector-year rows from sheet '{sheet}', "
                f"data through {through_year}"
            )
            return df, through_year

        except Exception as e:
            logger.debug(f"Sheet '{sheet}' parse error: {e}")
            continue

    logger.warning("No usable data found in any CARB Excel sheet")
    return None, None


# ---------------------------------------------------------------------------
# AirNow live fetch
# ---------------------------------------------------------------------------

async def _fetch_airnow_live() -> tuple[dict | None, list[str]]:
    """
    Fetch current-conditions AQI from AirNow for each city.
    Returns (live_readings_dict, error_list) where live_readings_dict maps
    city → current_aqi int.  Returns (None, errors) on complete failure.
    """
    if not AIRNOW_KEY:
        return None, ["AIRNOW_API_KEY not set in .env"]

    readings: dict[str, int] = {}
    errors: list[str] = []

    async with httpx.AsyncClient(timeout=12) as client:
        for city, coords in AIRNOW_CITIES.items():
            url = (
                "https://www.airnowapi.org/aq/observation/latLong/current/"
            )
            params = {
                "format":    "application/json",
                "latitude":  coords["lat"],
                "longitude": coords["lon"],
                "distance":  25,
                "API_KEY":   AIRNOW_KEY,
            }
            try:
                r = await client.get(url, params=params)
                if r.status_code != 200:
                    errors.append(f"{city}: HTTP {r.status_code}")
                    continue
                obs_list = r.json()
                if not isinstance(obs_list, list) or not obs_list:
                    errors.append(f"{city}: no observations returned (sensor may be offline)")
                    continue
                # Prefer O3; fall back to PM2.5, then whatever is first
                chosen = None
                for param_pref in ("O3", "PM2.5", "PM10"):
                    for obs in obs_list:
                        if obs.get("ParameterName") == param_pref:
                            chosen = obs
                            break
                    if chosen:
                        break
                if not chosen:
                    chosen = obs_list[0]
                aqi_val = int(chosen.get("AQI", 0))
                param   = chosen.get("ParameterName", "?")
                area    = chosen.get("ReportingArea", city)
                readings[city] = aqi_val
                logger.info(f"AirNow {city}: {aqi_val} ({param}) — {area}")
            except Exception as e:
                errors.append(f"{city}: {e}")

    if not readings:
        return None, errors
    return readings, errors


def _build_aqi_from_live_readings(live: dict[str, int]) -> dict:
    """
    Generate a full 12-month profile calibrated to today's live AQI reading.
    The live reading is used to rescale the seasonal synthetic profile so the
    annual average matches the live snapshot.
    """
    synth = _generate_aqi_data()
    months = synth[next(iter(synth))]["months"]
    result = {}
    for city, aqi_val in live.items():
        base = synth.get(city, synth["Los Angeles"])
        old_avg = base["annual_avg"] or 1
        scale   = aqi_val / old_avg
        monthly = [max(1, round(v * scale)) for v in base["aqi"]]
        result[city] = {
            "months":     months,
            "aqi":        monthly,
            "annual_avg": round(sum(monthly) / 12),
            "live_aqi":   aqi_val,   # today's actual reading
        }
    # Fill any cities missing from AirNow response with synthetic
    for city in AIRNOW_CITIES:
        if city not in result:
            result[city] = synth[city]
    return result


# ---------------------------------------------------------------------------
# Public load functions
# ---------------------------------------------------------------------------

async def load_ghg_emissions() -> tuple[pd.DataFrame, str]:
    """Returns (df, source) where source ∈ {"live","cached","synthetic"}."""
    excel_cache = DATA_DIR / "carb_ghg_inventory.xlsx"
    csv_cache   = DATA_DIR / "ghg_emissions.csv"

    downloaded_fresh = False

    # ── Step 1: try to download Excel if not cached ──────────────────────
    if not excel_cache.exists():
        logger.info("Downloading CARB GHG Excel …")
        try:
            async with httpx.AsyncClient(
                timeout=45, follow_redirects=True
            ) as client:
                r = await client.get(CARB_EXCEL_URL)
                r.raise_for_status()
                excel_cache.write_bytes(r.content)
                downloaded_fresh = True
                logger.info(f"CARB Excel downloaded ({len(r.content):,} bytes)")
        except Exception as e:
            err = f"CARB Excel download failed: {e}"
            logger.warning(err)
            _status["errors"].append(err)

    # ── Step 2: try to parse cached Excel ────────────────────────────────
    if excel_cache.exists():
        try:
            df, through_year = _parse_carb_excel(excel_cache)
            if df is not None and len(df) >= 20:
                df.to_csv(csv_cache, index=False)
                source = "live" if downloaded_fresh else "cached"
                _status["ghg_source"]        = source
                _status["data_through_year"] = through_year or 2023
                _status["last_fetch"]        = datetime.utcnow().isoformat()
                return df, source
            else:
                logger.warning("Excel parsed but returned insufficient rows; using synthetic")
        except Exception as e:
            err = f"CARB Excel parse error: {e}"
            logger.warning(err)
            _status["errors"].append(err)

    # ── Step 3: fall back to CSV cache (from previous run) ───────────────
    if csv_cache.exists():
        try:
            df = pd.read_csv(csv_cache)
            if len(df) > 20:
                _status["ghg_source"] = "cached"
                return df, "cached"
        except Exception:
            pass

    # ── Step 4: generate synthetic ───────────────────────────────────────
    logger.info("Generating synthetic GHG data")
    df = _generate_ghg_data()
    df.to_csv(csv_cache, index=False)
    _status["ghg_source"]        = "synthetic"
    _status["data_through_year"] = 2023
    return df, "synthetic"


async def load_county_emissions() -> tuple[pd.DataFrame, str]:
    cache_path = DATA_DIR / "county_emissions.csv"
    if cache_path.exists():
        df = pd.read_csv(cache_path)
        _status["county_source"] = "synthetic"
        return df, "synthetic"
    df = _generate_county_data()
    df.to_csv(cache_path, index=False)
    _status["county_source"] = "synthetic"
    return df, "synthetic"


async def load_counties_geojson() -> dict:
    cache_path = DATA_DIR / "california-counties.geojson"
    if cache_path.exists():
        with open(cache_path) as f:
            return json.load(f)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(COUNTIES_GEOJSON_URL)
            r.raise_for_status()
            data = r.json()
            with open(cache_path, "w") as f:
                json.dump(data, f)
            return data
    except Exception as e:
        logger.warning(f"GeoJSON download failed ({e})")
        return {"type": "FeatureCollection", "features": []}


async def load_aqi_data() -> tuple[dict, str]:
    """Returns (aqi_dict, source) where source ∈ {"live","cached","synthetic"}."""
    cache_path = DATA_DIR / "aqi_cities.json"

    # ── Step 1: try AirNow live ───────────────────────────────────────────
    live_readings, aqi_errors = await _fetch_airnow_live()
    if aqi_errors:
        for e in aqi_errors:
            logger.warning(f"AirNow: {e}")
        _status["errors"].extend([f"AirNow - {e}" for e in aqi_errors])

    if live_readings:
        data = _build_aqi_from_live_readings(live_readings)
        with open(cache_path, "w") as f:
            json.dump(data, f)
        _status["aqi_source"] = "live"
        return data, "live"

    # ── Step 2: try cached JSON ───────────────────────────────────────────
    if cache_path.exists():
        with open(cache_path) as f:
            data = json.load(f)
        _status["aqi_source"] = "cached"
        return data, "cached"

    # ── Step 3: generate synthetic ────────────────────────────────────────
    data = _generate_aqi_data()
    with open(cache_path, "w") as f:
        json.dump(data, f)
    _status["aqi_source"] = "synthetic"
    return data, "synthetic"


# ---------------------------------------------------------------------------
# PM2.5 helpers
# ---------------------------------------------------------------------------

def _pm25_category(pm25: float) -> str:
    if pm25 <= 0:    return "No Data"
    if pm25 < 12:    return "Good"
    if pm25 < 35.4:  return "Moderate"
    if pm25 < 55.4:  return "Unhealthy for Sensitive Groups"
    if pm25 < 150.4: return "Unhealthy"
    return "Very Unhealthy"


def _file_is_fresh(path: Path, ttl_sec: int) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < ttl_sec


# ---------------------------------------------------------------------------
# PurpleAir live sensors
# ---------------------------------------------------------------------------

async def fetch_purpleair_sensors() -> tuple[list, str]:
    """
    Returns (sensors_list, source).  Cache TTL = 5 min.
    Each sensor: name, lat, lon, pm25_now, pm25_24hr, category, last_seen
    """
    cache_path = DATA_DIR / "purpleair_sensors.json"
    CACHE_TTL  = 5 * 60

    if _file_is_fresh(cache_path, CACHE_TTL):
        with open(cache_path) as f:
            return json.load(f), "cached"

    if not PURPLEAIR_KEY:
        logger.warning("PURPLEAIR_API_KEY not set in .env")
        if cache_path.exists():
            with open(cache_path) as f:
                return json.load(f), "cached"
        return [], "error"

    params = {
        "fields":        "name,latitude,longitude,pm2.5_10minute,pm2.5_24hour,last_seen",
        "location_type": 0,
        "nwlng": -124.48, "nwlat": 42.01,
        "selng": -114.13, "selat": 32.53,
    }
    headers = {"X-API-Key": PURPLEAIR_KEY}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                "https://api.purpleair.com/v1/sensors",
                params=params, headers=headers
            )
            r.raise_for_status()
            raw = r.json()

        fields   = raw.get("fields", [])
        rows     = raw.get("data", [])
        sensors  = []

        for row in rows:
            if len(row) < len(fields):
                continue
            s       = dict(zip(fields, row))
            lat, lon = s.get("latitude"), s.get("longitude")
            if lat is None or lon is None:
                continue
            pm_now  = s.get("pm2.5_10minute")
            pm_24hr = s.get("pm2.5_24hour")
            pm_val  = float(pm_now  if pm_now  is not None else (pm_24hr or 0))
            pm_24v  = float(pm_24hr if pm_24hr is not None else 0)
            sensors.append({
                "name":      s.get("name", "Unknown"),
                "lat":       round(float(lat), 6),
                "lon":       round(float(lon), 6),
                "pm25_now":  round(pm_val,  1),
                "pm25_24hr": round(pm_24v,  1),
                "category":  _pm25_category(pm_val),
                "last_seen": s.get("last_seen"),
            })

        with open(cache_path, "w") as f:
            json.dump(sensors, f)
        logger.info(f"PurpleAir: {len(sensors)} sensors loaded")
        return sensors, "live"

    except Exception as e:
        logger.warning(f"PurpleAir fetch failed - {e}")
        # Don't surface PurpleAir errors in the status banner; show silently
        if cache_path.exists():
            with open(cache_path) as f:
                return json.load(f), "cached"
        return [], "error"


# ---------------------------------------------------------------------------
# NASA FIRMS wildfire detection
# ---------------------------------------------------------------------------

async def fetch_wildfires() -> tuple[list, str]:
    """
    Returns (fires_list, source).  Cache TTL = 15 min.
    Each fire: lat, lon, confidence, frp, acq_date, acq_time, satellite
    """
    cache_path = DATA_DIR / "wildfires.json"
    CACHE_TTL  = 15 * 60

    if _file_is_fresh(cache_path, CACHE_TTL):
        with open(cache_path) as f:
            return json.load(f), "cached"

    if not NASA_FIRMS_KEY:
        logger.warning("NASA_FIRMS_KEY not set in .env")
        if cache_path.exists():
            with open(cache_path) as f:
                return json.load(f), "cached"
        return [], "error"

    url = (
        f"https://firms.modaps.eosdis.nasa.gov/api/area/csv"
        f"/{NASA_FIRMS_KEY}/VIIRS_SNPP_NRT/-124.48,32.53,-114.13,42.01/1"
    )

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            text = r.text

        fires = []
        lines = [ln for ln in text.strip().splitlines() if ln.strip()]

        if len(lines) < 2:
            # Empty dataset = no fires
            with open(cache_path, "w") as f:
                json.dump([], f)
            logger.info("NASA FIRMS: 0 fire points (no active fires)")
            return [], "live"

        header = [c.strip() for c in lines[0].split(",")]
        for line in lines[1:]:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < len(header):
                continue
            row = dict(zip(header, parts))
            try:
                fires.append({
                    "lat":        float(row["latitude"]),
                    "lon":        float(row["longitude"]),
                    "confidence": row.get("confidence", "n"),
                    "frp":        float(row.get("frp", 0)),
                    "acq_date":   row.get("acq_date", ""),
                    "acq_time":   row.get("acq_time", ""),
                    "satellite":  row.get("satellite", ""),
                    "bright_ti4": float(row.get("bright_ti4", 0)),
                    "daynight":   row.get("daynight", "D"),
                })
            except (ValueError, KeyError):
                pass

        with open(cache_path, "w") as f:
            json.dump(fires, f)
        logger.info(f"NASA FIRMS: {len(fires)} fire points loaded")
        return fires, "live"

    except Exception as e:
        logger.warning(f"NASA FIRMS fetch failed - {e}")
        _status["errors"].append(f"NASA FIRMS - {e}")
        if cache_path.exists():
            with open(cache_path) as f:
                return json.load(f), "cached"
        return [], "error"


# ---------------------------------------------------------------------------
# OpenAQ official station network
# ---------------------------------------------------------------------------

async def fetch_openaq_stations() -> tuple[list, str]:
    """
    Two-phase fetch from OpenAQ v3.

    Phase 1 — GET /v3/locations?bbox=CA : get location IDs + metadata.
               The base /locations response does NOT include latest sensor
               values; sensors[] only carries {id, name, parameter}.

    Phase 2 — GET /v3/locations/{id}/sensors (parallel, capped at 30):
               this sub-endpoint returns sensors with a `latest.value` field.
               Extract pm25 where parameter.name == "pm25".

    Only stations with an actual pm25 reading are returned.
    Returns (stations_list, source).
    Each station: name, city, lat, lon, pm25, last_updated, provider.
    """
    cache_path = DATA_DIR / "openaq_stations.json"
    CACHE_TTL  = 5 * 60

    if _file_is_fresh(cache_path, CACHE_TTL):
        with open(cache_path) as f:
            return json.load(f), "cached"

    if not OPENAQ_KEY:
        logger.warning("OPENAQ_API_KEY not set")
        if cache_path.exists():
            with open(cache_path) as f:
                return json.load(f), "cached"
        return [], "error"

    headers = {"X-API-Key": OPENAQ_KEY}
    cutoff  = datetime.now(timezone.utc) - timedelta(hours=48)

    try:
        # ── Phase 1: fetch locations ──────────────────────────────────────
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                "https://api.openaq.org/v3/locations",
                params={"bbox": "-124.48,32.53,-114.13,42.01", "limit": 100},
                headers=headers,
            )
            r.raise_for_status()
            all_locs = r.json().get("results", [])

        logger.info(f"OpenAQ Phase 1: {len(all_locs)} locations fetched")

        # Keep only stations active within the last 48 h
        active_locs = []
        for loc in all_locs:
            dt_str = (loc.get("datetimeLast") or {}).get("utc", "")
            if dt_str:
                try:
                    ts = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc
                    )
                    if ts < cutoff:
                        continue
                except ValueError:
                    pass
            active_locs.append(loc)

        if not active_locs:
            active_locs = all_locs[:30]   # fallback: take 30 most-recently-listed

        active_locs = active_locs[:30]    # cap to limit parallel requests

        # ── Phase 2: fetch sensors for each location in parallel ──────────
        async def _get_sensors(client: httpx.AsyncClient, loc_id: int) -> list:
            try:
                resp = await client.get(
                    f"https://api.openaq.org/v3/locations/{loc_id}/sensors",
                    headers=headers,
                )
                if resp.status_code == 200:
                    return resp.json().get("results", [])
            except Exception:
                pass
            return []

        async with httpx.AsyncClient(timeout=20) as client:
            sensor_lists = await asyncio.gather(
                *[_get_sensors(client, loc["id"]) for loc in active_locs]
            )

        # ── Build station records ─────────────────────────────────────────
        stations = []
        for loc, sensors in zip(active_locs, sensor_lists):
            coords = loc.get("coordinates", {})
            lat = coords.get("latitude")
            lon = coords.get("longitude")
            if lat is None or lon is None:
                continue

            pm25         = None
            last_updated = (loc.get("datetimeLast") or {}).get("local", "")

            for sensor in sensors:
                param = sensor.get("parameter", {})
                if param.get("name") in ("pm25", "PM2.5", "PM25"):
                    latest = sensor.get("latest") or {}
                    val    = latest.get("value")
                    if val is not None:
                        pm25         = round(float(val), 1)
                        last_updated = (latest.get("datetime") or {}).get("local", last_updated)
                    break

            if pm25 is None:
                continue   # skip stations with no reading

            provider_obj = loc.get("provider") or {}
            provider = provider_obj.get("name", "Unknown") if isinstance(provider_obj, dict) else "Unknown"

            stations.append({
                "name":         loc.get("name", "Unknown Station"),
                "city":         loc.get("locality") or "California",
                "lat":          round(float(lat), 6),
                "lon":          round(float(lon), 6),
                "pm25":         pm25,
                "last_updated": last_updated,
                "provider":     provider,
            })

        with open(cache_path, "w") as f:
            json.dump(stations, f)

        logger.info(
            f"OpenAQ Phase 2: {len(active_locs)} locations queried, "
            f"{len(stations)} have pm25 readings"
        )
        return stations, "live"

    except Exception as e:
        logger.warning(f"OpenAQ fetch failed - {e}")
        if cache_path.exists():
            with open(cache_path) as f:
                return json.load(f), "cached"
        return [], "error"


# ---------------------------------------------------------------------------
# AirNow multi-pollutant fetch (all parameters, not just best AQI)
# ---------------------------------------------------------------------------

def _aqi_color(aqi: int) -> str:
    if aqi <= 50:  return "#22c55e"
    if aqi <= 100: return "#eab308"
    if aqi <= 150: return "#f97316"
    if aqi <= 200: return "#ef4444"
    return "#9333ea"


def _aqi_category(aqi: int) -> str:
    if aqi <= 50:  return "Good"
    if aqi <= 100: return "Moderate"
    if aqi <= 150: return "Unhealthy for Sensitive Groups"
    if aqi <= 200: return "Unhealthy"
    return "Very Unhealthy"


async def fetch_pollution_cities() -> tuple[list, str]:
    """
    Fetch ALL pollutant readings from AirNow for the 5 major cities.
    Returns (cities_list, source).
    Each city: {city, pollutants: [{name, aqi, category, color}], overall_aqi, ...}
    """
    if not AIRNOW_KEY:
        return [], "error"

    result = []

    async with httpx.AsyncClient(timeout=15) as client:
        for city, coords in AIRNOW_CITIES.items():
            url = "https://www.airnowapi.org/aq/observation/latLong/current/"
            params = {
                "format":    "application/json",
                "latitude":  coords["lat"],
                "longitude": coords["lon"],
                "distance":  50,
                "API_KEY":   AIRNOW_KEY,
            }
            try:
                r = await client.get(url, params=params)
                if r.status_code != 200:
                    logger.warning(f"Pollution {city}: HTTP {r.status_code}")
                    continue
                obs_list = r.json()
                if not isinstance(obs_list, list) or not obs_list:
                    continue

                pollutants = []
                seen: set[str] = set()
                for obs in obs_list:
                    param = obs.get("ParameterName", "")
                    if not param or param in seen:
                        continue
                    seen.add(param)
                    aqi_val = int(obs.get("AQI", 0))
                    cat = obs.get("Category", {}).get("Name", _aqi_category(aqi_val))
                    pollutants.append({
                        "name":     param,
                        "aqi":      aqi_val,
                        "category": cat,
                        "color":    _aqi_color(aqi_val),
                    })

                overall_aqi = max((p["aqi"] for p in pollutants), default=0)
                result.append({
                    "city":             city,
                    "lat":              coords["lat"],
                    "lon":              coords["lon"],
                    "pollutants":       pollutants,
                    "overall_aqi":      overall_aqi,
                    "overall_color":    _aqi_color(overall_aqi),
                    "overall_category": _aqi_category(overall_aqi),
                    "updated":          datetime.utcnow().isoformat(),
                })
                logger.info(f"Pollution {city}: {len(pollutants)} pollutants, overall AQI={overall_aqi}")

            except Exception as e:
                logger.warning(f"Pollution fetch {city}: {e}")

    return result, ("live" if result else "error")
