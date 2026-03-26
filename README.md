# 🛰️ CalAtmos — California Air Intelligence Platform

![Live](https://img.shields.io/badge/Status-Live-brightgreen)
![Python](https://img.shields.io/badge/Python-3.11-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-Latest-green)
![Data Sources](https://img.shields.io/badge/Data_Sources-4-orange)

> Fusing NASA satellite fire detection · 7,000+ IoT sensors · CARB official GHG inventory · OpenAQ government stations —
> data sources CARB doesn't combine publicly, in one place.

## Built by

**Chinmay Das** · ex-Data Engineer Intern @ California Air Resources Board (CARB)
M.S. Computer Science · UC Riverside '25
🟢 Open to Data Engineer / AI Engineer / Geospatial ML roles in CA
[LinkedIn](https://linkedin.com/in/chinmay-das07) · [GitHub](https://github.com/das-Chinmay)

---

## Live Data Sources

| Source | Data | Update Frequency |
|---|---|---|
| CARB Official | County GHG emissions 2000–2023 | Annual |
| PurpleAir | 7,354 IoT PM2.5 sensors | Every 2 min |
| NASA FIRMS | Satellite wildfire detection | Hourly |
| OpenAQ v3 | 19 government air quality stations | Real-time |
| EPA AirNow | Multi-pollutant city data (O3, PM2.5, PM10, NO2) | Hourly |

---

## What It Shows

- **County Choropleth Map** — All 58 CA counties colored green→red by MMTCO₂e, pulsing markers on top 5 emitters
- **PurpleAir PM2.5 Map** — 7,000+ live IoT sensor dots colored by air quality category
- **NASA FIRMS Wildfire Map** — Satellite-detected fire points, sized by fire radiative power, last 24 h
- **OpenAQ Stations Map** — Government ground monitors with real PM2.5 readings (two-phase v3 API fetch)
- **Air Pollution Breakdown** — Live O3 / PM2.5 / PM10 readings for LA, San Diego, Riverside, Sacramento, Fresno
- **County Comparison Tool** — Pick any two counties, get side-by-side emissions, per-capita, YoY change + CSS bar chart
- **4 KPI Cards** — Statewide emissions, live sensor count, active fire points, counties monitored

---

## Tech Stack

| Layer | Tech |
|---|---|
| Backend | Python 3.11 · FastAPI · Uvicorn · Pandas · HTTPX · asyncio |
| Frontend | Vanilla HTML/CSS/JS · Leaflet.js 1.9 · No framework, no build step |
| APIs | PurpleAir v1 · NASA FIRMS VIIRS · OpenAQ v3 · EPA AirNow · CARB Open Data |
| Hosting | Render (backend) · GitHub (repo) |

---

## Run Locally

```bash
git clone https://github.com/das-Chinmay/calatmos.git
cd calatmos
cp .env.example .env          # fill in your API keys
pip install -r requirements.txt
uvicorn backend.main:app --reload
```

Open **http://localhost:8000**

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/summary` | KPI stats: top emitter, YoY change, most improved county |
| GET | `/api/emissions/by-county` | All 58 CA counties with 2023 vs 2022 emissions |
| GET | `/api/emissions/by-year` | Statewide GHG totals 2000–2023 |
| GET | `/api/emissions/by-sector` | Sector breakdown for latest year |
| GET | `/api/geospatial/hotspots` | GeoJSON with emission intensity per county |
| GET | `/api/sensors/purpleair` | Live PM2.5 from 7,000+ IoT sensors |
| GET | `/api/sensors/wildfires` | NASA FIRMS satellite fire detections |
| GET | `/api/sensors/openaq` | OpenAQ v3 government stations with real PM2.5 |
| GET | `/api/pollution/cities` | Multi-pollutant AQI for 5 major cities |
| GET | `/api/aqi/cities` | Monthly AQI profile for 5 cities |
| GET | `/api/data-status` | Live vs cached vs synthetic per data source |

---

## Project Structure

```
calatmos/
├── backend/
│   ├── main.py          — FastAPI app + all endpoints
│   ├── data_fetcher.py  — Live data fetching (PurpleAir, FIRMS, OpenAQ, AirNow, CARB)
│   └── spatial.py       — GeoJSON enrichment, county analysis
├── frontend/
│   └── index.html       — Complete single-file dashboard
├── data/                — Auto-populated on first run (gitignored)
├── render.yaml          — Render.com deployment config
├── requirements.txt
└── .env.example
```

---

## Internship Context

This project was built using real emissions data from my internship experience at the **California Air Resources Board (CARB)**. During my time at CARB, I worked directly with the GHG inventory pipeline — processing sector-level emissions data, validating county-level aggregations, and building internal dashboards to support the agency's climate reporting obligations under AB 32 and SB 32.

---

MIT © 2025 Chinmay Das · UC Riverside MSCS
