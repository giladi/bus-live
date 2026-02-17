from __future__ import annotations

import time
from functools import lru_cache
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, date
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

app = FastAPI()
templates = Jinja2Templates(directory="templates")

TZ = ZoneInfo("Asia/Jerusalem")

HEADERS = {
    # דומה למה שהספרייה עושה: UA + Referer כדי להיראות כמו דפדפן/אפליקציה
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.7",
    "Referer": "https://app.busnearby.co.il/",
}

STOP_SEARCH_URL = "https://app.busnearby.co.il/stopSearch"
STOPTIMES_URL_TMPL = "https://api.busnearby.co.il/directions/index/stops/1:{stop_id}/stoptimes"
STRIDE_BASE = "https://open-bus-stride-api.hasadna.org.il"

STOPS = ["20727", "26206"]
LINE = "63"
NUM_DEPARTURES = 5  # כמה יציאות להציג לכל תחנה


def _normalize_line(x: str) -> str:
    # כדי ש-"063" ו-"63" יושוו נכון
    return x.lstrip("0") or "0"


@lru_cache(maxsize=256)
def resolve_stop_id(stop_code: str) -> dict:
    """ממיר קוד תחנה (מהמשתמש) ל-stop_id + stop_name דרך stopSearch."""
    with httpx.Client(headers=HEADERS, timeout=10) as client:
        r = client.get(STOP_SEARCH_URL, params={"query": stop_code, "locale": "he"})
        r.raise_for_status()
        data = r.json()

    if not data:
        raise ValueError(f"Station not found for code {stop_code}")

    first = data[0]
    return {
        "stop_code": stop_code,
        "stop_id": str(first["stop_id"]),
        "stop_name": first.get("stop_name") or first.get("stopName") or "",
    }


def fetch_line_departures(stop_code: str, line: str) -> dict:
    """מחזיר רשימת יציאות קרובות לקו נתון בתחנה נתונה."""
    stop = resolve_stop_id(stop_code)
    now = int(time.time())

    params = {
        "numberOfDepartures": NUM_DEPARTURES,
        "timeRange": 86400,
        "startTime": now,
        "locale": "he",
    }

    with httpx.Client(headers=HEADERS, timeout=10) as client:
        r = client.get(STOPTIMES_URL_TMPL.format(stop_id=stop["stop_id"]), params=params)
        r.raise_for_status()
        times_data = r.json()

    departures = []
    want = _normalize_line(line)

    for item in times_data or []:
        times = (item.get("times") or [])
        if not times:
            continue
        t0 = times[0]

        route = str(t0.get("routeShortName", ""))
        if _normalize_line(route) != want:
            continue

        service_day = int(t0.get("serviceDay", 0))
        sec_into_day = t0.get("realtimeArrival", None)
        if sec_into_day is None:
            sec_into_day = t0.get("scheduledArrival", None)
        if sec_into_day is None:
            continue

        dep_epoch = service_day + int(sec_into_day)
        if dep_epoch < now:
            continue

        dep_dt = datetime.fromtimestamp(dep_epoch, TZ)
        minutes = max(0, int((dep_epoch - now) // 60))

        departures.append(
            {
                "line": route,
                "time_hhmm": dep_dt.strftime("%H:%M"),
                "in_minutes": minutes,
                "is_realtime": bool(t0.get("realtime", False)),
            }
        )

    departures.sort(key=lambda x: x["in_minutes"])

    return {
        "stop_code": stop_code,
        "stop_name": stop["stop_name"],
        "updated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "departures": departures[:NUM_DEPARTURES],
    }


@lru_cache(maxsize=64)
def stride_resolve_line_refs(route_short_name: str) -> list[dict[str, str]]:
    """
    ממיר מספר קו ציבורי (כמו 63) ל-line_ref/operator_ref של Stride
    באמצעות gtfs_routes/list.
    """
    params = {
        "route_short_name": _normalize_line(route_short_name),
        "limit": 200,
        # מצמצם רעש ליום הנוכחי (אפשר להסיר אם צריך)
        "date_from": date.today().isoformat(),
        "date_to": date.today().isoformat(),
    }

    with httpx.Client(timeout=15, headers={"User-Agent": HEADERS["User-Agent"]}) as client:
        r = client.get(f"{STRIDE_BASE}/gtfs_routes/list", params=params)
        r.raise_for_status()
        routes = r.json() or []

    pairs: dict[tuple[str, str], dict[str, str]] = {}
    for rt in routes:
        line_ref = rt.get("line_ref")
        operator_ref = rt.get("operator_ref")
        if line_ref is None or operator_ref is None:
            continue

        key = (str(line_ref), str(operator_ref))
        if key not in pairs:
            pairs[key] = {
                "line_ref": str(line_ref),
                "operator_ref": str(operator_ref),
                "agency_name": str(rt.get("agency_name") or ""),
                "route_long_name": str(rt.get("route_long_name") or ""),
                "direction": str(rt.get("route_direction") or ""),
            }

    return list(pairs.values())


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "stops": STOPS, "line": LINE},
    )


@app.get("/api/departures")
def api_departures():
    results = []
    errors = []
    for s in STOPS:
        try:
            results.append(fetch_line_departures(s, LINE))
        except Exception as e:
            errors.append({"stop_code": s, "error": str(e)})

    return JSONResponse({"line": LINE, "results": results, "errors": errors})


@app.get("/api/vehicles")
def api_vehicles(line: str = LINE):
    """
    מחזיר מיקומי אוטובוסים בזמן אמת לפי מספר קו ציבורי (כמו 63).
    תמיד מחזיר JSON (גם אם Stride איטי/נופל) כדי שה-frontend לא יקרוס.
    """
    now = datetime.now(TZ)
    since = now - timedelta(minutes=10)

    candidates = stride_resolve_line_refs(line)

    if not candidates:
        return JSONResponse(
            {
                "line": _normalize_line(line),
                "since": since.isoformat(),
                "candidates_tried": [],
                "vehicles": [],
                "errors": ["No GTFS routes found for this line (route_short_name)."],
            },
            status_code=200,
        )

    # Timeout אגרסיבי כדי לא להיתקע (זה פותר ReadTimeout ב-Render)
    timeout = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0)

    vehicles: list[dict[str, Any]] = []
    errors: list[str] = []

    # לא לנסות על יותר מדי אפשרויות (עוזר גם ל-timeout)
    max_candidates = 6
    candidates_to_try = candidates[:max_candidates]

    with httpx.Client(timeout=timeout, headers={"User-Agent": HEADERS["User-Agent"]}) as client:
        for c in candidates_to_try:
            params = {
                "recorded_at_time_from": since.isoformat(),
                "limit": 200,
                "siri_routes__line_ref": c["line_ref"],
                "siri_routes__operator_ref": c["operator_ref"],
                "order_by": "recorded_at_time desc",
            }

            try:
                r = client.get(f"{STRIDE_BASE}/siri_vehicle_locations/list", params=params)
                r.raise_for_status()
                data: list[dict[str, Any]] = r.json() or []
            except httpx.TimeoutException:
                errors.append(
                    f"Timeout fetching vehicles for line_ref={c['line_ref']} operator_ref={c['operator_ref']}"
                )
                continue
            except Exception as e:
                errors.append(
                    f"Error fetching vehicles for line_ref={c['line_ref']} operator_ref={c['operator_ref']}: {e}"
                )
                continue

            for item in data:
                lat = item.get("lat")
                lon = item.get("lon")

                nested = item.get("siri_vehicle_location") or item.get("vehicle_location") or {}
                if lat is None:
                    lat = nested.get("lat")
                if lon is None:
                    lon = nested.get("lon")

                if lat is None or lon is None:
                    continue

                vehicles.append(
                    {
                        "lat": float(lat),
                        "lon": float(lon),
                        "recorded_at_time": item.get("recorded_at_time") or nested.get("recorded_at_time"),
                        "vehicle_ref": item.get("siri_ride__vehicle_ref")
                        or item.get("siri_ride_vehicle_ref")
                        or item.get("vehicle_ref")
                        or item.get("vehicle_id")
                        or None,
                        "stride_line_ref": c["line_ref"],
                        "stride_operator_ref": c["operator_ref"],
                        "agency_name": c.get("agency_name", ""),
                        "route_long_name": c.get("route_long_name", ""),
                        "direction": c.get("direction", ""),
                    }
                )

            # אם מצאנו משהו — לא צריך להמשיך ולבזבז זמן
            if vehicles:
                break

    # דה-דופ בסיסי
    seen = set()
    uniq = []
    for v in vehicles:
        key = (v.get("vehicle_ref"), v["lat"], v["lon"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(v)

    return JSONResponse(
        {
            "line": _normalize_line(line),
            "since": since.isoformat(),
            "candidates_tried": candidates_to_try,
            "vehicles": uniq,
            "errors": errors,
        },
        status_code=200,
    )
