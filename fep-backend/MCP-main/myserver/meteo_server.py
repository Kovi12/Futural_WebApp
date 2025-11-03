# MCP-main/myserver/meteo_server.py
from __future__ import annotations
import os, csv, unicodedata, requests
from typing import Optional, Tuple
from mcp.server.fastmcp import FastMCP
from bs4 import BeautifulSoup
from collections import Counter
from datetime import datetime, timedelta

def _normalize(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ASCII", "ignore").decode("utf-8").lower()

def _worldcities_csv_path() -> Optional[str]:
    # Prefer your helper; else fallback to WEBAPP_BASE_DIR/data/worldcities.csv
    try:
        from common.path_utils import get_worldcities_csv  # type: ignore
        return get_worldcities_csv()
    except Exception:
        base = os.getenv("WEBAPP_BASE_DIR", os.path.expanduser("~/Futural_WebApp"))
        p = os.path.join(base, "data", "worldcities.csv")
        return p if os.path.exists(p) else None

def _city_to_latlon(city: str) -> Tuple[Optional[float], Optional[float]]:
    path = _worldcities_csv_path()
    if not path or not os.path.exists(path):
        return None, None
    target = _normalize(city)
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rdr = csv.reader(f)
            for row in rdr:
                # CSV layout used so far: [0:id, 1:city, 2:lat, 3:lon, ...]
                try:
                    if _normalize(row[1]) == target:
                        return float(row[2]), float(row[3])
                except Exception:
                    continue
    except Exception:
        return None, None
    return None, None

def _meteo_url(lat: float, lon: float) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"https://www.meteoblue.com/en/weather/week/{abs(lat):.3f}{ns}{abs(lon):.3f}{ew}"

def _parse_meteoblue(html: str) -> Optional[dict]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="picto three-hourly-view")
    if not table:
        return None

    clouds = [c.find("img")["title"] for c in table.find_all("div", class_="pictoicon") if c.find("img")]
    temps = [int(t.strip()[:-1]) for t in table.find("tr", class_="temperatures").text.split("\n") if t.strip().endswith("°")][::2]
    felt  = [int(t.strip()[:-1]) for t in table.find("tr", class_="windchills").text.split("\n") if t.strip().endswith("°")]
    wind_data = table.find("tr", class_="windspeeds").text.split("\n")[2:]
    wind_speed = wind_data[1::3]
    wind_dir   = wind_data[0::3]
    prec_data = table.find("tr", class_="precips").text.split("\n")[2:]
    prec_prob  = prec_data[1::3]
    prec_size  = prec_data[2::3]

    def _mph_to_kmh(s: str) -> str:
        try: return str(int(float(s) * 1.6))
        except Exception: return "-"

    def _in_to_mm(s: str) -> str:
        try: return str(int(float(s) * 25.4))
        except Exception: return s

    return {
        "temperatures_real_c": [(t - 32) * 5 // 9 for t in temps],
        "temperature_felt_c":  [(t - 32) * 5 // 9 for t in felt],
        "wind_speed_kmh":      [_mph_to_kmh(s) for s in wind_speed],
        "wind_direction":      wind_dir,
        "precipitation_probability": prec_prob,
        "precipitation_mm":    [_in_to_mm(s) if s != "-" else "-" for s in prec_size],
        "cloud_cover":         clouds,
    }

# ---- compaction helpers (keep the LLM prompt tiny) ----
def _as_ints(xs):
    out = []
    for x in xs or []:
        if isinstance(x, (int, float)):
            out.append(int(x))
            continue
        s = str(x).strip().replace("%", "").replace("mm", "")
        if s in ("", "-", "–"):  # skip empty / dashes
            continue
        try:
            out.append(int(round(float(s))))
        except Exception:
            pass
    return out

def _mode(xs, default="—"):
    try:
        return Counter([str(x).strip() for x in xs or [] if str(x).strip()]).most_common(1)[0][0]
    except Exception:
        return default

def _compress_weather(raw: dict, *, city: str, lat: float, lon: float, url: str) -> dict:
    temps = _as_ints(raw.get("temperatures_real_c"))
    felt  = _as_ints(raw.get("temperature_felt_c"))
    wind  = _as_ints(raw.get("wind_speed_kmh"))
    pprob = _as_ints(raw.get("precipitation_probability"))
    sky   = raw.get("cloud_cover") or []

    hi = max(temps) if temps else None
    lo = min(temps) if temps else None
    feels_hi = max(felt) if felt else None
    feels_lo = min(felt) if felt else None
    wind_max = max(wind) if wind else None
    precip_max = max(pprob) if pprob else None
    sky_mode = _mode(sky, default="—")

    steps = max(len(temps), len(sky), len(wind), len(pprob))
    start = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=steps * 3 if steps else 0)

    return {
        "city": city,
        "coords": {"lat": lat, "lon": lon},
        "period": {"start": start.isoformat() + "Z", "end": end.isoformat() + "Z"},
        "summary": {
            "temp_c": {"high": hi, "low": lo, "feels_high": feels_hi, "feels_low": feels_lo},
            "wind_kmh_max": wind_max,
            "precip_probability_max_pct": precip_max,
            "sky_mode": sky_mode,
        },
        "source_url": url,
    }

class MeteoServer:
    """
    Minimal MCP server with a single tool:
      - get_weather_json(city: str) -> dict (compact)
    """
    def __init__(self):
        self.app = FastMCP("meteo-tools")
        self._register_tools()

    def _register_tools(self):
        @self.app.tool()
        async def get_weather_json(city: str) -> dict:
            """Fetches 3-hourly weather info for a city and returns a compact JSON summary."""
            if not city or not city.strip():
                raise ValueError("city must be a non-empty string")

            lat, lon = _city_to_latlon(city.strip())
            if lat is None or lon is None:
                raise ValueError(f"Could not resolve city: {city}")

            url = _meteo_url(lat, lon)
            try:
                res = requests.get(url, timeout=15)
            except Exception as e:
                raise RuntimeError(f"Failed to fetch weather page: {e}")

            if res.status_code != 200:
                raise RuntimeError(f"Bad HTTP status from provider: {res.status_code}")

            raw = _parse_meteoblue(res.text)
            if not raw:
                raise RuntimeError("Failed to parse weather table from provider page")

            compact = _compress_weather(raw, city=city, lat=lat, lon=lon, url=url)
            return compact

    def run(self, transport: str = "stdio"):
        self.app.run(transport=transport)
