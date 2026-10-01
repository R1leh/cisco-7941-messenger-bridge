#!/usr/bin/env python3
"""Idle/Services screens for a Cisco 7941 IP phone: clock, weather (met.no),
CBR currency rates, a currency converter, and infrastructure status.

IMPORTANT: on this phone/firmware (SIP41.8-5-4S, non-CUCM "USECALLMANAGER"
mode), CiscoIPPhoneText/Menu/Input screens are served and parsed as
ISO-8859-1 (latin-1) only. There is no Cyrillic (or other non-Latin) support
for these push-XML screens on this firmware, regardless of the encoding
declared in the HTTP response -- all visible text must stay ASCII/Latin.
CiscoIPPhoneImage was also tested (bitmap rendering) and does not work on
this firmware either: the phone hangs on "Requesting..." indefinitely even
though the server responds quickly with a valid payload.

All configuration below is read from environment variables; the defaults
are placeholders for local testing only -- set real values via your
systemd unit (see phone-idle.service.example) or a local .env, never commit
real values to this repo.
"""
import json
import os
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

CITY = os.environ.get("CITY", "Example City")
CITY_LAT = os.environ.get("CITY_LATIN", "Example City")
LAT = os.environ.get("LAT", "55.75")   # placeholder: Moscow
LON = os.environ.get("LON", "37.62")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow"))
CURRENCIES = os.environ.get("CURRENCIES", "USD,EUR,CNY").split(",")
BIND = os.environ.get("BIND", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8088"))
REFRESH = int(os.environ.get("REFRESH", "60"))
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8088")
WEATHER_TTL = 15 * 60
RATES_TTL = 60 * 60
STATUS_TTL = 20
UA = os.environ.get("MET_NO_USER_AGENT", "cisco-7941-messenger-bridge/1.0 (set MET_NO_USER_AGENT)")

# Optional: status of a second host (e.g. a VPN server) shown on /status.
UKNOW_HOST = os.environ.get("UKNOW_HOST", "")
UKNOW_PORT = os.environ.get("UKNOW_PORT", "8097")
UKNOW_TOKEN = os.environ.get("UKNOW_TOKEN", "")

SYMBOLS = {"USD": "$", "EUR": "EUR", "CNY": "CNY", "GBP": "GBP"}
DAYS_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

BASE_EN = {
    "clearsky": "clear", "fair": "fair", "partlycloudy": "partly cloudy",
    "cloudy": "cloudy", "fog": "fog",
    "rain": "rain", "rainshowers": "showers",
    "snow": "snow", "snowshowers": "snow showers",
    "sleet": "sleet", "sleetshowers": "sleet showers",
}

_cache = {}


def cached(key, ttl, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
        _cache[key] = (now, val)
        return val
    except Exception as e:
        print(f"{key} fetch failed: {e}", flush=True)
        return hit[1] if hit else None


def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean_code(code):
    code = code or ""
    for suf in ("_day", "_night", "_polartwilight"):
        if code.endswith(suf):
            code = code[: -len(suf)]
    return code


def describe_en(code):
    code = clean_code(code)
    thunder = "andthunder" in code
    code = code.replace("andthunder", "")
    prefix = ""
    if code.startswith("light"):
        prefix, code = "light ", code[5:]
    elif code.startswith("heavy"):
        prefix, code = "heavy ", code[5:]
    text = prefix + BASE_EN.get(code, code or "?")
    return text + (", thunder" if thunder else "")


def fetch_weather_full():
    """Full met.no forecast parse: current point + daily aggregates ahead."""
    url = ("https://api.met.no/weatherapi/locationforecast/2.0/compact"
           f"?lat={LAT}&lon={LON}")
    ts = json.loads(http_get(url))["properties"]["timeseries"]
    cur = ts[0]["data"]
    det = cur["instant"]["details"]
    sym = (cur.get("next_1_hours") or cur.get("next_6_hours") or {}).get("summary", {}).get("symbol_code")
    later = ts[6]["data"] if len(ts) > 6 else None

    days = {}
    for point in ts:
        t = datetime.fromisoformat(point["time"].replace("Z", "+00:00")).astimezone(TZ)
        day = t.date()
        d = point["data"]
        temp = d["instant"]["details"]["air_temperature"]
        sym6 = (d.get("next_6_hours") or d.get("next_1_hours") or {}).get("summary", {}).get("symbol_code")
        slot = days.setdefault(day, {"temps": [], "syms": [], "date": t})
        slot["temps"].append(temp)
        if sym6:
            slot["syms"].append(sym6)

    forecast = []
    today = datetime.now(TZ).date()
    for day, slot in sorted(days.items()):
        if day == today:
            continue
        forecast.append({
            "date": slot["date"],
            "tmin": min(slot["temps"]),
            "tmax": max(slot["temps"]),
            "sym": slot["syms"][len(slot["syms"]) // 2] if slot["syms"] else None,
        })
        if len(forecast) >= 4:
            break

    return {
        "t": det["air_temperature"],
        "wind": det.get("wind_speed"),
        "hum": det.get("relative_humidity"),
        "pressure": det.get("air_pressure_at_sea_level"),
        "sym": sym,
        "t6": later["instant"]["details"]["air_temperature"] if later else None,
        "sym6": (later.get("next_6_hours") or later.get("next_1_hours") or {}).get("summary", {}).get("symbol_code") if later else None,
        "forecast": forecast,
    }


def fetch_rates():
    root = ET.fromstring(http_get("https://www.cbr.ru/scripts/XML_daily.asp"))
    rates = {}
    for v in root.findall("Valute"):
        code = v.findtext("CharCode")
        rates[code] = float(v.findtext("Value").replace(",", ".")) / int(v.findtext("Nominal"))
    return {"date": root.get("Date"), "rates": rates}


def fetch_local_status():
    """Status of the host this script runs on (e.g. the Asterisk/SIP server)."""
    uptime_s = float(open("/proc/uptime").read().split()[0])
    registered = False
    try:
        # A separate root-owned daemon (astatus.py) writes this file every
        # ~15s, since this service usually runs unprivileged and can't poll
        # the Asterisk control socket directly. See astatus.py / README.
        with open("/run/phone-idle/asterisk_status.json") as f:
            st = json.load(f)
        if time.time() - st.get("ts", 0) < 60:
            registered = bool(st.get("registered"))
    except Exception:
        pass
    return {"uptime_s": uptime_s, "phone_registered": registered}


def fetch_remote_status():
    if not UKNOW_HOST or not UKNOW_TOKEN:
        return None
    url = f"http://{UKNOW_HOST}:{UKNOW_PORT}/status?token={UKNOW_TOKEN}"
    return json.loads(http_get(url, timeout=5))


def tfmt(t):
    return f"{t:+.0f}C"


def fmt_uptime(seconds):
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h or d:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


def xml_header():
    return '<?xml version="1.0" encoding="ISO-8859-1"?>\n'


def text_screen(title, body_lines, prompt=None, softkeys=None):
    now = datetime.now(TZ)
    prompt = prompt if prompt is not None else f"Updated {now:%H:%M}"
    xml = (xml_header() + "<CiscoIPPhoneText>"
           f"<Title>{escape(title)}</Title><Prompt>{escape(prompt)}</Prompt>"
           f"<Text>{escape(chr(10).join(body_lines))}</Text>")
    if softkeys:
        for i, (name, url_) in enumerate(softkeys, start=1):
            xml += (f"<SoftKeyItem><Name>{escape(name)}</Name>"
                    f"<URL>{escape(url_)}</URL><Position>{i}</Position></SoftKeyItem>")
    xml += "</CiscoIPPhoneText>"
    return xml


# ---------- screen 1: idle (compact summary) ----------

def render_idle(query=""):
    now = datetime.now(TZ)
    w = cached("weather", WEATHER_TTL, fetch_weather_full)
    r = cached("rates", RATES_TTL, fetch_rates)
    title = f"{CITY_LAT}  {now:%H:%M}  {DAYS_EN[now.weekday()]} {now:%d.%m}"
    lines = []
    if w:
        lines.append(f"{tfmt(w['t'])}  {describe_en(w['sym'])}")
        extra = []
        if w["wind"] is not None:
            extra.append(f"wind {w['wind']:.0f} m/s")
        if w["hum"] is not None:
            extra.append(f"hum {w['hum']:.0f}%")
        if extra:
            lines.append("  ".join(extra))
        if w["t6"] is not None:
            lines.append(f"In 6h: {tfmt(w['t6'])} {describe_en(w['sym6'])}")
    else:
        lines.append("Weather: no data")
    lines.append("")
    if r:
        parts = [f"{c} {r['rates'][c]:.2f}" for c in CURRENCIES if c in r["rates"]]
        for i in range(0, len(parts), 2):
            lines.append("  ".join(parts[i:i + 2]))
        lines.append(f"CBR rate for {r['date']}")
    else:
        lines.append("Rates: no data")
    return text_screen(title, lines, prompt=f"Updated {now:%H:%M}")


# ---------- screen 2: detailed weather + multi-day forecast ----------

def render_weather(query=""):
    now = datetime.now(TZ)
    w = cached("weather", WEATHER_TTL, fetch_weather_full)
    title = f"Weather: {CITY_LAT}"
    lines = []
    if not w:
        lines.append("No data (met.no unreachable)")
        return text_screen(title, lines, softkeys=[("Back", f"{BASE_URL}/services")])
    lines.append(f"Now: {tfmt(w['t'])}, {describe_en(w['sym'])}")
    extra = []
    if w["wind"] is not None:
        extra.append(f"wind {w['wind']:.0f} m/s")
    if w["hum"] is not None:
        extra.append(f"hum {w['hum']:.0f}%")
    if w["pressure"] is not None:
        extra.append(f"{w['pressure']:.0f} hPa")
    if extra:
        lines.append(", ".join(extra))
    if w["t6"] is not None:
        lines.append(f"In 6h: {tfmt(w['t6'])}, {describe_en(w['sym6'])}")
    lines.append("")
    lines.append("Forecast:")
    for day in w["forecast"]:
        dname = DAYS_EN[day["date"].weekday()]
        sym = describe_en(day["sym"]) if day["sym"] else "?"
        lines.append(f"{dname} {day['date']:%d.%m}: {tfmt(day['tmin'])}..{tfmt(day['tmax'])}, {sym}")
    return text_screen(title, lines, softkeys=[("Back", f"{BASE_URL}/services")])


# ---------- screen 3: currency rates (extended list) ----------

def render_rates(query=""):
    r = cached("rates", RATES_TTL, fetch_rates)
    title = "CBR currency rates"
    lines = []
    if not r:
        lines.append("No data (cbr.ru unreachable)")
        return text_screen(title, lines, softkeys=[("Back", f"{BASE_URL}/services")])
    lines.append(f"As of {r['date']}:")
    show = ["USD", "EUR", "CNY", "GBP", "TRY", "KZT", "BYN", "AMD", "AZN", "UZS"]
    for code in show:
        if code in r["rates"]:
            lines.append(f"{code}  {r['rates'][code]:.2f} RUB")
    return text_screen(title, lines, prompt=f"Total currencies in CBR feed: {len(r['rates'])}",
                        softkeys=[("Back", f"{BASE_URL}/services")])


# ---------- screen 4: infrastructure status ----------

def render_status(query=""):
    title = "Server status"
    lines = []
    g = cached("local_status", STATUS_TTL, fetch_local_status)
    if g:
        lines.append("Local host:")
        lines.append(f"  uptime {fmt_uptime(g['uptime_s'])}")
        lines.append(f"  SIP phone: {'registered' if g['phone_registered'] else 'NOT registered'}")
    else:
        lines.append("Local host: no data")
    lines.append("")
    u = cached("remote_status", STATUS_TTL, fetch_remote_status)
    if u:
        lines.append("Remote host:")
        lines.append(f"  uptime {fmt_uptime(u['uptime_s'])}")
        lines.append(f"  xray: {u['xray_clients']} clients, {u['xray_active_tcp']} sessions now")
        lines.append(f"  WireGuard: {u['wg_peers_online']}/{u['wg_peers_total']} online")
        lines.append(f"  hysteria2: {'up' if u['hysteria_active'] else 'down'}")
    else:
        lines.append("Remote host: not configured / unreachable")
    return text_screen(title, lines, softkeys=[("Back", f"{BASE_URL}/services")])


# ---------- screen 5: about ----------

ABOUT_LINES = [
    "Cisco 7941 messenger bridge",
    "Turns an office Cisco 7941 IP phone",
    "into an idle-screen dashboard:",
    "weather, exchange rates, a currency",
    "converter, and infra status.",
    "",
    "Open-source, MIT licensed.",
]


def render_about(query=""):
    return text_screen("About", ABOUT_LINES, prompt="cisco-7941-messenger-bridge",
                        softkeys=[("Back", f"{BASE_URL}/services")])


# ---------- currency converter: amount entered via keypad (CiscoIPPhoneInput) ----------
# Menu item -> CiscoIPPhoneInput (numeric field) -> Submit does a GET to
# /conv_out with the entered amount as a query param -> we compute and show
# the result.

CONV_PAIRS = {
    "RUB_USD": ("RUB", "USD"),
    "RUB_EUR": ("RUB", "EUR"),
    "RUB_CNY": ("RUB", "CNY"),
    "USD_RUB": ("USD", "RUB"),
    "EUR_RUB": ("EUR", "RUB"),
    "CNY_RUB": ("CNY", "RUB"),
}


def _parse_query(query):
    from urllib.parse import parse_qsl
    return dict(parse_qsl(query))


def render_converter_menu(query=""):
    items = [(f"{a} -> {b}", f"{BASE_URL}/conv_in?pair={a}_{b}") for a, b in CONV_PAIRS.values()]
    xml = (xml_header() + "<CiscoIPPhoneMenu><Title>Currency converter</Title>"
           "<Prompt>Select direction</Prompt>")
    for name, url_ in items:
        xml += f"<MenuItem><Name>{escape(name)}</Name><URL>{escape(url_)}</URL></MenuItem>"
    xml += (f'<SoftKeyItem><Name>Back</Name><URL>{escape(f"{BASE_URL}/services")}</URL>'
            "<Position>1</Position></SoftKeyItem></CiscoIPPhoneMenu>")
    return xml


def render_conv_in(query=""):
    q = _parse_query(query)
    pair = q.get("pair", "")
    if pair not in CONV_PAIRS:
        return text_screen("Error", ["Unknown currency pair."],
                            softkeys=[("Back", f"{BASE_URL}/converter")])
    frm, to = CONV_PAIRS[pair]
    return (xml_header() +
            "<CiscoIPPhoneInput>"
            f"<Title>{escape(frm)} to {escape(to)}</Title>"
            f"<Prompt>Enter amount in {escape(frm)}</Prompt>"
            f"<URL>{escape(f'{BASE_URL}/conv_out?pair={pair}')}</URL>"
            "<InputItem>"
            f"<DisplayName>Amount ({escape(frm)})</DisplayName>"
            "<QueryStringParam>amount</QueryStringParam>"
            "<DefaultValue></DefaultValue>"
            "<InputFlags>N</InputFlags>"
            "</InputItem>"
            "</CiscoIPPhoneInput>")


def render_conv_out(query=""):
    q = _parse_query(query)
    pair = q.get("pair", "")
    if pair not in CONV_PAIRS:
        return text_screen("Error", ["Unknown currency pair."],
                            softkeys=[("Back", f"{BASE_URL}/converter")])
    frm, to = CONV_PAIRS[pair]
    raw = q.get("amount", "0")
    digits = "".join(c for c in raw if c.isdigit() or c == ".")
    try:
        amount = float(digits) if digits else 0.0
    except ValueError:
        amount = 0.0

    r = cached("rates", RATES_TTL, fetch_rates)
    lines = [f"{amount:.2f} {frm}"]
    if not r:
        lines.append("")
        lines.append("No rate data (cbr.ru unreachable)")
    else:
        rates = r["rates"]
        if frm == "RUB":
            rate = rates.get(to)
            if rate:
                lines.append("=")
                lines.append(f"{amount / rate:.2f} {to}")
            else:
                lines.append("Rate not found")
        else:
            rate = rates.get(frm)
            if rate:
                lines.append("=")
                lines.append(f"{amount * rate:.2f} {to}")
            else:
                lines.append("Rate not found")
        lines.append("")
        lines.append(f"CBR rate for {r['date']}")
    return text_screen(f"{frm} -> {to}", lines,
                        softkeys=[("Again", f"{BASE_URL}/conv_in?pair={pair}"),
                                  ("Menu", f"{BASE_URL}/converter")])


# ---------- Services menu ----------

def render_menu(query=""):
    items = [
        ("Weather detail / forecast", f"{BASE_URL}/weather"),
        ("Currency rates (list)", f"{BASE_URL}/rates"),
        ("Currency converter", f"{BASE_URL}/converter"),
        ("Server status", f"{BASE_URL}/status"),
        ("About the project", f"{BASE_URL}/about"),
    ]
    xml = (xml_header() + "<CiscoIPPhoneMenu><Title>Services</Title>"
           "<Prompt>Select an item</Prompt>")
    for name, url_ in items:
        xml += f"<MenuItem><Name>{escape(name)}</Name><URL>{escape(url_)}</URL></MenuItem>"
    xml += "</CiscoIPPhoneMenu>"
    return xml


ROUTES = {
    "/": render_idle,
    "/idle": render_idle,
    "/services": render_menu,
    "/weather": render_weather,
    "/rates": render_rates,
    "/status": render_status,
    "/about": render_about,
    "/converter": render_converter_menu,
    "/conv_in": render_conv_in,
    "/conv_out": render_conv_out,
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path, _, query = self.path.partition("?")
        fn = ROUTES.get(path)
        if fn is None:
            self.send_error(404)
            return
        try:
            xml = fn(query)
        except Exception as e:
            print(f"render {path} failed: {e}", flush=True)
            xml = text_screen("Error", ["Failed to build screen:", str(e)])
        body = xml.encode("latin-1", "replace")
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=ISO-8859-1")
        self.send_header("Content-Length", str(len(body)))
        if path in ("/", "/idle"):
            self.send_header("Refresh", str(REFRESH))
        self.send_header("Expires", "-1")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    print(f"phone-idle listening on {BIND}:{PORT}", flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
