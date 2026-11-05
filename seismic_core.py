"""ส่วนแกนกลาง: ดึงข้อมูลแผ่นดินไหว + วิเคราะห์ประกันภัย (ไม่ผูกกับ transport ใด)

โมดูลนี้เป็น Python ล้วน ไม่ import MCP จึงทดสอบ (unit test) ได้โดยตรง
ส่วน `seismic_mcp_server.py` เป็นตัวเชื่อม tools เหล่านี้เข้ากับ MCP
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("seismic-mcp")

USGS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
TMD_URL = "https://data.tmd.go.th/api/DailySeismicEvent/v1/"
TMD_DEMO_PARAMS = {"uid": "api", "ukey": "api12345", "format": "json"}

HTTP_TIMEOUT = float(os.environ.get("SEISMIC_HTTP_TIMEOUT", "60"))
# timezone ที่ใช้คำนวณ "วันนี้" ของ tools TMD (TMD รายงานเป็นเวลาไทย)
LOCAL_TZ = ZoneInfo(os.environ.get("SEISMIC_TZ", "Asia/Bangkok"))
# TMD คืนข้อมูลก้อนใหญ่ตั้งแต่ต้นปี จึง cache ไว้ช่วงสั้น ๆ
TMD_CACHE_TTL = float(os.environ.get("SEISMIC_TMD_CACHE_TTL", "120"))


class ToolError(Exception):
    """error ที่จะถูกแปลงเป็น JSON-RPC error response"""

    INVALID_PARAMS = -32602
    UPSTREAM = -32001  # API ภายนอกล้มเหลว (HTTP / network)
    PARSE = -32002  # API ภายนอกคืนข้อมูลที่อ่านไม่ได้

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# ---------------------------------------------------------------------------
# ฟังก์ชันช่วยเหลือ
# ---------------------------------------------------------------------------

def _num(value: Any) -> float | None:
    """แปลงค่าจากผู้ให้บริการ ("3.7", 3.7, None) เป็น float"""
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _today_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def _parse_date(value: str, field: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ToolError(ToolError.INVALID_PARAMS, f"'{field}' must be YYYY-MM-DD, got {value!r}") from None


def _time_sort_key(event: dict) -> float:
    try:
        return datetime.fromisoformat(event["time"].replace("Z", "+00:00")).timestamp()
    except Exception:
        return float("-inf")


async def _get_json(url: str, params: dict) -> Any:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, params=params)
    except httpx.HTTPError as e:
        raise ToolError(ToolError.UPSTREAM, f"Request to {url} failed: {e}") from None
    if resp.status_code >= 400:
        raise ToolError(
            ToolError.UPSTREAM,
            f"{url} returned HTTP {resp.status_code}",
            {"status": resp.status_code, "body": resp.text[:500]},
        )
    if resp.status_code == 204 or not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        raise ToolError(ToolError.PARSE, f"{url} returned non-JSON content", {"body": resp.text[:500]}) from None


# ---------------------------------------------------------------------------
# แปลงข้อมูลแต่ละแหล่ง -> EarthquakeEvent
# ---------------------------------------------------------------------------

def normalize_usgs_feature(feature: dict) -> dict:
    props = feature.get("properties") or {}
    coords = (feature.get("geometry") or {}).get("coordinates") or [None, None, None]
    coords = list(coords) + [None] * (3 - len(coords))
    t = props.get("time")
    iso = (
        datetime.fromtimestamp(t / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        if isinstance(t, (int, float))
        else None
    )
    return {
        "id": str(feature.get("id")),
        "time": iso,
        "place": props.get("place"),
        "magnitude": _num(props.get("mag")),
        "latitude": _num(coords[1]),
        "longitude": _num(coords[0]),
        "depth_km": _num(coords[2]),
        "source": "USGS",
        "url": props.get("url"),
        "raw": feature,
    }


def _tmd_time(item: dict) -> str | None:
    """เวลา ISO8601 ของ event จาก TMD (รูปแบบตาม spec คือ `DateTime` หรือรูปแบบจริงคือ `DateTimeUTC`)"""
    if item.get("DateTime"):
        return str(item["DateTime"])
    if item.get("DateTimeUTC"):
        try:
            dt = datetime.strptime(str(item["DateTimeUTC"])[:19], "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            return str(item["DateTimeUTC"])
    return None


def _tmd_local_date(item: dict) -> str | None:
    """วันที่ตามเวลาไทย (YYYY-MM-DD) ของ event จาก TMD"""
    if item.get("DateTimeThai"):
        return str(item["DateTimeThai"])[:10]
    t = _tmd_time(item)
    if not t:
        return None
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
    except ValueError:
        return t[:10]


def _tmd_id(item: dict) -> str:
    if item.get("EventID") not in (None, ""):
        return f"TMD-{item['EventID']}"
    # ข้อมูลจริงไม่มี EventID: สร้าง id ที่คงที่จากเวลา + ตำแหน่งศูนย์กลาง
    key = f"{item.get('DateTimeUTC')}|{item.get('Latitude')}|{item.get('Longitude')}|{item.get('Magnitude')}"
    return "TMD-" + hashlib.sha1(key.encode()).hexdigest()[:12]


def normalize_tmd_item(item: dict) -> dict:
    return {
        "id": _tmd_id(item),
        "time": _tmd_time(item),
        "place": item.get("Location") or item.get("OriginThai") or item.get("TitleThai"),
        "magnitude": _num(item.get("Magnitude")),
        "latitude": _num(item.get("Latitude")),
        "longitude": _num(item.get("Longitude")),
        "depth_km": _num(item.get("DepthKm", item.get("Depth"))),
        "source": "TMD_DailySeismicEvent",
        "url": item.get("URL") or item.get("Url") or item.get("url"),
        "raw": item,
    }


# ---------------------------------------------------------------------------
# Tools สำหรับดึงข้อมูล
# ---------------------------------------------------------------------------

async def usgs_search_earthquakes(args: dict) -> dict:
    radius_fields = sum(args.get(k) is not None for k in ("latitude", "longitude", "maxradiuskm"))
    if radius_fields not in (0, 3):
        raise ToolError(
            ToolError.INVALID_PARAMS,
            "Radius search needs all of 'latitude', 'longitude' and 'maxradiuskm' (or none of them).",
        )
    query: dict[str, Any] = {"format": "geojson"}
    for key in ("starttime", "endtime", "minmagnitude", "maxmagnitude",
                "latitude", "longitude", "maxradiuskm", "limit"):
        if args.get(key) is not None:
            query[key] = args[key]
    query.setdefault("orderby", "time")

    data = await _get_json(USGS_URL, query)
    if not isinstance(data, dict) or not isinstance(data.get("features"), list):
        raise ToolError(ToolError.PARSE, "USGS response is not a GeoJSON FeatureCollection")
    events = [normalize_usgs_feature(f) for f in data["features"]]
    return {"events": events, "query": query}


_tmd_cache: tuple[float, list[dict]] | None = None


async def _fetch_tmd_items(date: str | None = None) -> list[dict]:
    """ดึงรายการ event ดิบจาก TMD  API จริงไม่สนพารามิเตอร์ `Date` และคืนข้อมูล
    ตั้งแต่ต้นปีเสมอ ผู้เรียกจึงต้องกรองวันที่เอง และผลลัพธ์จะถูก cache ไว้ช่วงสั้น ๆ"""
    global _tmd_cache
    now = time.monotonic()
    if _tmd_cache and now - _tmd_cache[0] < TMD_CACHE_TTL:
        return _tmd_cache[1]
    params = dict(TMD_DEMO_PARAMS)
    if date:
        params["Date"] = date
    data = await _get_json(TMD_URL, params)
    if data is None:
        items: Any = []
    elif isinstance(data, dict):
        items = data.get("DailySeismicEvent", data.get("DailyEarthquakes", []))
    else:
        raise ToolError(ToolError.PARSE, "TMD response is not a JSON object")
    if isinstance(items, dict):  # กรณีมี event เดียวและส่งมาเป็น object
        items = [items]
    if not isinstance(items, list):
        raise ToolError(ToolError.PARSE, "TMD response has no event list")
    items = [i for i in items if isinstance(i, dict)]
    _tmd_cache = (now, items)
    return items


def _dedupe_sort(events: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for e in events:
        seen.setdefault(e["id"], e)
    return sorted(seen.values(), key=_time_sort_key, reverse=True)


async def tmd_get_daily_earthquakes(args: dict) -> dict:
    date = _parse_date(args["date"], "date") if args.get("date") else _today_local().strftime("%Y-%m-%d")
    items = await _fetch_tmd_items(date)
    events = _dedupe_sort([normalize_tmd_item(i) for i in items if _tmd_local_date(i) == date])
    return {"events": events, "query": {"date": date}}


async def tmd_get_recent_earthquakes(args: dict) -> dict:
    days = args["days"]
    today = _today_local().date()
    dates = {(today - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(days)}
    items = await _fetch_tmd_items(today.strftime("%Y-%m-%d"))
    events = _dedupe_sort([normalize_tmd_item(i) for i in items if _tmd_local_date(i) in dates])
    return {"events": events, "query": {"days": days, "dates": sorted(dates, reverse=True)}}


# ---------------------------------------------------------------------------
# Tools วิเคราะห์ประกันภัย / parametric
# ---------------------------------------------------------------------------

DEFAULT_PAYOUT_CURVE = [
    {"max_distance_km": 50, "payout_percent": 0.5},
    {"max_distance_km": 100, "payout_percent": 0.25},
    {"max_distance_km": 200, "payout_percent": 0.1},
]


def _event_point(event: dict) -> tuple[float, float, float] | None:
    mag, lat, lon = _num(event.get("magnitude")), _num(event.get("latitude")), _num(event.get("longitude"))
    if mag is None or lat is None or lon is None:
        return None
    return mag, lat, lon


async def match_events_to_assets(args: dict) -> dict:
    max_d = args["max_distance_km"]
    min_m = args["min_magnitude"]
    matches: dict[str, dict] = {}
    skipped = 0
    for event in args["events"]:
        pt = _event_point(event)
        if pt is None:
            skipped += 1
            continue
        mag, elat, elon = pt
        if mag < min_m:
            continue
        for asset in args["assets"]:
            dist = haversine_km(elat, elon, asset["latitude"], asset["longitude"])
            if dist <= max_d:
                m = matches.setdefault(asset["asset_id"], {
                    "asset_id": asset["asset_id"],
                    "name": asset.get("name"),
                    "sum_insured": asset.get("sum_insured"),
                    "events": [],
                })
                m["events"].append({"event": event, "distance_km": round(dist, 3)})
    for m in matches.values():
        m["events"].sort(key=lambda x: x["distance_km"])
    result = {
        "matches": list(matches.values()),
        "parameters": {"max_distance_km": max_d, "min_magnitude": min_m},
    }
    if skipped:
        result["skipped_events"] = skipped  # จำนวน event ที่ไม่มีขนาดหรือพิกัด
    return result


async def compute_parametric_payout(args: dict) -> dict:
    event = args["event"]
    pt = _event_point(event)
    if pt is None:
        raise ToolError(ToolError.INVALID_PARAMS,
                        "'event' must have numeric 'magnitude', 'latitude' and 'longitude'.")
    mag, elat, elon = pt
    min_m = args["min_magnitude_trigger"]
    max_d = args["max_distance_km_trigger"]
    curve = args.get("payout_curve") or DEFAULT_PAYOUT_CURVE
    for band in curve:
        if not 0 <= band["payout_percent"] <= 1:
            raise ToolError(ToolError.INVALID_PARAMS, "payout_percent must be between 0.0 and 1.0")
    curve = sorted(curve, key=lambda b: b["max_distance_km"])  # "band แรก" = band ที่แคบที่สุดที่ครอบระยะทางนั้น

    payouts = []
    total = 0.0
    for asset in args["assets"]:
        dist = haversine_km(elat, elon, asset["latitude"], asset["longitude"])
        pct = 0.0
        if mag >= min_m and dist <= max_d:
            pct = next((b["payout_percent"] for b in curve if dist <= b["max_distance_km"]), 0.0)
        amount = round(asset["sum_insured"] * pct, 2)
        total += amount
        payouts.append({
            "asset_id": asset["asset_id"],
            "name": asset.get("name"),
            "distance_km": round(dist, 3),
            "sum_insured": asset["sum_insured"],
            "payout_percent": pct,
            "payout_amount": amount,
        })
    return {
        "event_id": event.get("id"),
        "event_magnitude": mag,
        "triggered": mag >= min_m,
        "parameters": {
            "min_magnitude_trigger": min_m,
            "max_distance_km_trigger": max_d,
            "payout_curve": curve,
        },
        "asset_payouts": payouts,
        "total_payout": round(total, 2),
    }


# ---------------------------------------------------------------------------
# ทะเบียน tools (ชื่อ -> คำอธิบาย, inputSchema, handler)
# ---------------------------------------------------------------------------

_ASSET_PROPS = {
    "asset_id": {"type": "string"},
    "name": {"type": "string"},
    "latitude": {"type": "number", "minimum": -90, "maximum": 90},
    "longitude": {"type": "number", "minimum": -180, "maximum": 180},
}

TOOLS: dict[str, dict] = {
    "usgs_search_earthquakes": {
        "description": "Search earthquakes from the USGS FDSN API with basic filters.",
        "handler": usgs_search_earthquakes,
        "inputSchema": {
            "type": "object",
            "properties": {
                "starttime": {"type": "string", "description": "YYYY-MM-DD or ISO8601."},
                "endtime": {"type": "string", "description": "YYYY-MM-DD or ISO8601."},
                "minmagnitude": {"type": "number", "description": "Minimum magnitude.", "default": 4.5},
                "maxmagnitude": {"type": "number", "description": "Maximum magnitude, optional."},
                "latitude": {"type": "number", "minimum": -90, "maximum": 90,
                             "description": "Center latitude for radius search, optional."},
                "longitude": {"type": "number", "minimum": -180, "maximum": 180,
                              "description": "Center longitude for radius search, optional."},
                "maxradiuskm": {"type": "number", "exclusiveMinimum": 0,
                                "description": "Search radius in kilometers, optional."},
                "limit": {"type": "integer", "description": "Maximum number of events.",
                          "default": 20, "minimum": 1, "maximum": 200},
            },
            "required": ["starttime", "endtime"],
            "additionalProperties": False,
        },
    },
    "tmd_get_daily_earthquakes": {
        "description": "Get daily earthquakes from the TMD DailySeismicEvent demo API "
                       "(Thai Meteorological Department; dates are Thai local time).",
        "handler": tmd_get_daily_earthquakes,
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD. If omitted, use today's date."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "tmd_get_recent_earthquakes": {
        "description": "Get recent earthquakes from TMD over the last N days using DailySeismicEvent.",
        "handler": tmd_get_recent_earthquakes,
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Number of days back from today (including today).",
                         "default": 3, "minimum": 1, "maximum": 7},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "match_events_to_assets": {
        "description": "Match earthquake events to insured assets within a given radius and magnitude threshold.",
        "handler": match_events_to_assets,
        "inputSchema": {
            "type": "object",
            "properties": {
                "events": {"type": "array", "description": "List of EarthquakeEvent objects.",
                           "items": {"type": "object"}},
                "assets": {
                    "type": "array",
                    "description": "Insured assets with coordinates.",
                    "items": {
                        "type": "object",
                        "properties": {**_ASSET_PROPS,
                                       "sum_insured": {"type": "number",
                                                       "description": "Optional total insured value."}},
                        "required": ["asset_id", "latitude", "longitude"],
                        "additionalProperties": True,
                    },
                },
                "max_distance_km": {"type": "number", "description": "Max distance to consider an asset affected.",
                                    "default": 100, "minimum": 0},
                "min_magnitude": {"type": "number", "description": "Minimum magnitude to consider.", "default": 4.5},
            },
            "required": ["events", "assets"],
            "additionalProperties": False,
        },
    },
    "compute_parametric_payout": {
        "description": "Compute a simple parametric payout for an earthquake event and a set of assets, "
                       "based on distance and magnitude thresholds. Default curve: <=50 km 50%, "
                       "<=100 km 25%, <=200 km 10%, beyond 0%.",
        "handler": compute_parametric_payout,
        "inputSchema": {
            "type": "object",
            "properties": {
                "event": {"type": "object", "description": "Single EarthquakeEvent to evaluate."},
                "assets": {
                    "type": "array",
                    "description": "Assets with coordinates and sum_insured.",
                    "items": {
                        "type": "object",
                        "properties": {**_ASSET_PROPS, "sum_insured": {"type": "number", "minimum": 0}},
                        "required": ["asset_id", "latitude", "longitude", "sum_insured"],
                        "additionalProperties": True,
                    },
                },
                "min_magnitude_trigger": {"type": "number", "description": "Minimum magnitude to trigger any payout.",
                                          "default": 5.0},
                "max_distance_km_trigger": {"type": "number", "description": "Max distance for eligibility.",
                                            "default": 200, "minimum": 0},
                "payout_curve": {
                    "type": "array",
                    "description": "Optional distance-based payout curve.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "max_distance_km": {"type": "number", "minimum": 0},
                            "payout_percent": {"type": "number", "minimum": 0, "maximum": 1,
                                               "description": "Fraction of sum_insured (0.0–1.0)."},
                        },
                        "required": ["max_distance_km", "payout_percent"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["event", "assets"],
            "additionalProperties": False,
        },
    },
}
