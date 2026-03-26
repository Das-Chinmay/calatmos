"""
spatial.py — Geospatial analysis: attach emission intensity to county GeoJSON.
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def build_hotspot_geojson(geojson: dict, county_df: pd.DataFrame) -> dict:
    """
    Merge county emissions into GeoJSON features.
    Adds: emissions_mmtco2e, intensity_class (low/medium/high/critical),
    per_capita_tco2e, yoy_change_pct, primary_sector.
    """
    if not geojson.get("features"):
        return _synthetic_hotspot_geojson(county_df)

    county_map = {}
    for _, row in county_df.iterrows():
        county_map[row["county"].lower()] = row.to_dict()

    # Normalise for colour scale
    emissions = county_df["emissions_mmtco2e_2023"].values
    p25, p50, p75 = np.percentile(emissions, [25, 50, 75])

    def classify(val):
        if val <= p25:
            return "low"
        elif val <= p50:
            return "medium"
        elif val <= p75:
            return "high"
        return "critical"

    def colour(val):
        norm = (val - emissions.min()) / (emissions.max() - emissions.min() + 1e-9)
        if norm < 0.25:
            return "#22c55e"   # green
        elif norm < 0.50:
            return "#eab308"   # yellow
        elif norm < 0.75:
            return "#f97316"   # orange
        return "#ef4444"       # red

    features = []
    for feat in geojson["features"]:
        props = feat.get("properties", {})
        name = props.get("name", "")
        data = county_map.get(name.lower(), {})
        val = data.get("emissions_mmtco2e_2023", 2.0)
        feat["properties"] = {
            **props,
            "emissions_mmtco2e": data.get("emissions_mmtco2e_2023", 2.0),
            "emissions_prev": data.get("emissions_mmtco2e_2022", 2.0),
            "yoy_change_pct": data.get("yoy_change_pct", 0.0),
            "primary_sector": data.get("primary_sector", "Unknown"),
            "per_capita_tco2e": data.get("per_capita_tco2e", 8.0),
            "intensity_class": classify(val),
            "fill_color": colour(val),
        }
        features.append(feat)

    return {**geojson, "features": features}


def _synthetic_hotspot_geojson(county_df: pd.DataFrame) -> dict:
    """
    When GeoJSON download is unavailable, return a lightweight FeatureCollection
    with Point geometries for each county centroid (approximate).
    """
    centroids = {
        "Los Angeles": [-118.2437, 34.0522], "San Diego": [-117.1611, 32.7157],
        "Orange": [-117.8311, 33.8353], "Riverside": [-116.2023, 33.9534],
        "San Bernardino": [-116.4194, 34.1083], "Sacramento": [-121.4944, 38.5816],
        "Fresno": [-119.7871, 36.7378], "Kern": [-118.7626, 35.3733],
        "Alameda": [-122.0839, 37.6017], "Santa Clara": [-121.9552, 37.3541],
        "Contra Costa": [-121.9496, 37.9185], "San Joaquin": [-121.2908, 37.9022],
        "Stanislaus": [-120.9988, 37.5091], "Tulare": [-119.0520, 36.2077],
        "Ventura": [-119.1391, 34.3705],
    }
    features = []
    for _, row in county_df.iterrows():
        c = row["county"]
        lon, lat = centroids.get(c, [-119.5, 37.5])
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "name": c,
                "emissions_mmtco2e": row["emissions_mmtco2e_2023"],
                "yoy_change_pct": row["yoy_change_pct"],
                "primary_sector": row["primary_sector"],
                "per_capita_tco2e": row["per_capita_tco2e"],
                "intensity_class": "medium",
                "fill_color": "#eab308",
            },
        })
    return {"type": "FeatureCollection", "features": features}


def get_top_emitter_counties(county_df: pd.DataFrame, n: int = 5) -> list[dict]:
    top = county_df.nlargest(n, "emissions_mmtco2e_2023")
    return top[["county", "emissions_mmtco2e_2023", "yoy_change_pct", "per_capita_tco2e"]].to_dict("records")


def get_most_improved_county(county_df: pd.DataFrame) -> dict:
    best = county_df.loc[county_df["yoy_change_pct"].idxmin()]
    return best[["county", "yoy_change_pct", "emissions_mmtco2e_2023"]].to_dict()
