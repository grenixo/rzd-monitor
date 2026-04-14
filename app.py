#!/usr/bin/env python3
"""
РЖД Монитор — веб-интерфейс
Запуск: python3 app.py
Открыть: http://<IP>:5000
"""

from flask import Flask, jsonify, request, send_from_directory, session
from functools import wraps
import json, os, time, smtplib, threading, logging, hashlib, secrets
from datetime import datetime, timedelta
from collections import defaultdict
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests as req

app = Flask(__name__, static_folder="static")

_data_dir    = os.environ.get("RZD_DATA_DIR", os.path.expanduser("~"))
CONFIG_FILE  = os.path.join(_data_dir, "rzd_config.json")
STATE_FILE   = os.path.join(_data_dir, "rzd_state.json")
LOG_FILE     = os.path.join(_data_dir, "rzd_monitor.log")
HISTORY_FILE = os.path.join(_data_dir, "rzd_history.json")

logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%d.%m.%Y %H:%M:%S",
)
log = logging.getLogger()

# ── Конфиг ────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "smtp_host":         "smtp.gmail.com",
    "smtp_port":         587,
    "smtp_user":         "",
    "smtp_password":     "",
    "ntfy_url":          "",
    "ntfy_topic":        "",
    "ntfy_token":        "",
    "ntfy_user":         "",
    "ntfy_password":     "",
    "monitoring":        False,
    "interval_min":      5,
    "secret_key":        "",
    "ui_password_hash":  "",
    "ui_password_salt":  "",
    "routes": [
        {
            "id":        "1",
            "from_code": "2004000",
            "from_name": "Санкт-Петербург",
            "to_code":   "2060150",
            "to_name":   "Ижевск",
            "dates":     ["2026-07-31", "2026-08-01", "2026-08-02"],
            "active":    True,
            "email_to":  "",
        }
    ]
}

def load_config():
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
            # Добавляем недостающие ключи из дефолта
            for k, v in DEFAULT_CONFIG.items():
                if k not in cfg:
                    cfg[k] = v
            return cfg
    except (FileNotFoundError, json.JSONDecodeError):
        return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def load_history():
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except:
        return []

def save_history(history):
    cutoff = (datetime.now() - timedelta(days=3)).isoformat()
    history = [h for h in history if h.get("ts", "") >= cutoff]
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

# ── Auth ──────────────────────────────────────────────────────────

def _init_secret_key():
    cfg = load_config()
    if not cfg.get("secret_key"):
        cfg["secret_key"] = secrets.token_hex(32)
        save_config(cfg)
    return cfg["secret_key"]

app.secret_key = _init_secret_key()

def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return dk.hex(), salt

def verify_password(password, hashed, salt):
    return hash_password(password, salt)[0] == hashed

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        cfg = load_config()
        if cfg.get("ui_password_hash") and not session.get("authenticated"):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# ── Brute-force protection ────────────────────────────────────────

_MAX_ATTEMPTS  = 5
_LOCKOUT_SEC   = 15 * 60   # 15 минут
_login_attempts: dict = {}  # {ip: {"count": int, "locked_until": float}}

def _get_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()

def _check_rate_limit():
    """Возвращает (ok, seconds_left). ok=False если IP заблокирован."""
    ip  = _get_ip()
    now = time.time()
    rec = _login_attempts.get(ip)
    if rec and rec["locked_until"] > now:
        return False, int(rec["locked_until"] - now)
    return True, 0

def _record_failure():
    ip  = _get_ip()
    now = time.time()
    rec = _login_attempts.setdefault(ip, {"count": 0, "locked_until": 0})
    # Сбрасываем счётчик если предыдущая блокировка уже истекла
    if rec["locked_until"] and rec["locked_until"] < now:
        rec["count"] = 0
        rec["locked_until"] = 0
    rec["count"] += 1
    if rec["count"] >= _MAX_ATTEMPTS:
        rec["locked_until"] = now + _LOCKOUT_SEC
        log.warning(f"Login brute-force: IP {ip} заблокирован на {_LOCKOUT_SEC // 60} мин")

def _reset_attempts():
    _login_attempts.pop(_get_ip(), None)

# ── РЖД API ───────────────────────────────────────────────────────

rzd_session = req.Session()
rzd_session.headers.update({
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer":         "https://ticket.rzd.ru/",
})

def fetch_trains(from_code, to_code, date_str):
    url = "https://ticket.rzd.ru/api/v1/railway-service/prices/train-pricing"
    params = {
        "service_provider": "B2B_RZD", "getByLocalTime": "true",
        "carGrouping": "DontGroup", "origin": from_code, "destination": to_code,
        "departureDate": f"{date_str}T00:00:00",
        "specialPlacesDemand": "StandardPlacesAndForDisabledPersons",
        "carIssuingType": "Passenger", "getTrainsFromSchedule": "true",
        "adultPassengersQuantity": 1, "childrenPassengersQuantity": 0,
        "hasPlacesForLargeFamily": "false",
    }
    try:
        r = rzd_session.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Ошибка запроса {from_code}→{to_code} {date_str}: {e}")
        return None

CAR_TYPE_RU = {
    "Compartment": "Купе", "ReservedSeat": "Плацкарт",
    "Lux": "СВ", "Soft": "Мягкий", "Sedentary": "Сидячий", "Common": "Общий",
}

def summarize_cars(car_groups):
    by_type = defaultdict(lambda: {
        "total": 0, "lower": 0, "upper": 0, "lower_side": 0, "upper_side": 0,
        "min_price": None, "max_price": None, "name": ""
    })
    for car in car_groups:
        t = car.get("CarType", "Unknown")
        g = by_type[t]
        g["name"]        = CAR_TYPE_RU.get(t, car.get("CarTypeName", t))
        g["total"]      += car.get("TotalPlaceQuantity", 0)
        g["lower"]      += car.get("LowerPlaceQuantity", 0)
        g["upper"]      += car.get("UpperPlaceQuantity", 0)
        g["lower_side"] += car.get("LowerSidePlaceQuantity", 0)
        g["upper_side"] += car.get("UpperSidePlaceQuantity", 0)
        mp, xp = car.get("MinPrice"), car.get("MaxPrice")
        if mp is not None:
            g["min_price"] = mp if g["min_price"] is None else min(g["min_price"], mp)
        if xp is not None:
            g["max_price"] = xp if g["max_price"] is None else max(g["max_price"], xp)
    return dict(by_type)

def parse_response(data):
    trains = data.get("Trains") or []
    result = []
    for t in trains:
        cars = summarize_cars(t.get("CarGroups") or [])
        total = sum(
            g["total"] or g["lower"] + g["upper"] + g["lower_side"] + g["upper_side"]
            for g in cars.values()
        )
        result.append({
            "number":      t.get("DisplayTrainNumber") or t.get("TrainNumber", "?"),
            "depart":      (t.get("LocalDepartureDateTime") or t.get("DepartureDateTime") or "")[:16],
            "arrive":      (t.get("LocalArrivalDateTime")   or t.get("ArrivalDateTime")   or "")[:16],
            "duration":    t.get("TripDuration"),
            "distance":    t.get("TripDistance"),
            "origin":      t.get("OriginName") or t.get("InitialStationName", ""),
            "dest":        t.get("DestinationName") or t.get("FinalStationName", ""),
            "total_seats": total,
            "cars":        cars,
            "er":          t.get("HasElectronicRegistration", False),
            "from_schedule": t.get("IsFromSchedule", False),
        })
    err = (data.get("errorInfo") or data.get("ErrorInfo") or {})
    return result, err.get("Message") or err.get("ProviderError") or ""

# ── Email ─────────────────────────────────────────────────────────

def send_email(cfg, subject, body, email_to=None):
    raw = email_to or cfg.get("email_to", "")
    # Поддержка нескольких получателей через запятую или точку с запятой
    recipients = [e.strip() for e in raw.replace(";", ",").split(",") if e.strip()]
    if not cfg.get("smtp_user") or not recipients:
        log.warning("Email не настроен (нет smtp_user или получателя)")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg["smtp_user"]
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(body, "plain", "utf-8"))
    try:
        with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"]), timeout=30) as s:
            s.ehlo(); s.starttls(); s.login(cfg["smtp_user"], cfg["smtp_password"])
            s.sendmail(cfg["smtp_user"], recipients, msg.as_string())
        log.info(f"Email отправлен → {', '.join(recipients)}: {subject}")
        return True
    except Exception as e:
        log.error(f"Ошибка email: {e}")
        return False

# ── ntfy ──────────────────────────────────────────────────────────

def send_ntfy(cfg, title, body, ntfy_topic=None, click_url=None):
    url   = cfg.get("ntfy_url", "").rstrip("/")
    topic = (ntfy_topic or cfg.get("ntfy_topic", "")).strip()
    if not url or not topic:
        log.warning("ntfy не настроен (нет url или topic)")
        return False
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Title": title.encode("utf-8").decode("latin-1", errors="replace"),
    }
    if click_url:
        headers["Click"] = click_url
    token    = cfg.get("ntfy_token", "").strip()
    ntfy_user = cfg.get("ntfy_user", "").strip()
    ntfy_pass = cfg.get("ntfy_password", "").strip()
    auth = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif ntfy_user and ntfy_pass:
        auth = (ntfy_user, ntfy_pass)
    try:
        r = req.post(f"{url}/{topic}", data=body.encode("utf-8"), headers=headers, auth=auth, timeout=10)
        r.raise_for_status()
        log.info(f"ntfy отправлен → {topic}: {title}")
        return True
    except Exception as e:
        log.error(f"Ошибка ntfy: {e}")
        return False

_RU_MONTHS = ["января","февраля","марта","апреля","мая","июня",
              "июля","августа","сентября","октября","ноября","декабря"]

# ── Мониторинг ────────────────────────────────────────────────────

monitor_thread = None
stop_event     = threading.Event()

def monitor_loop():
    log.info("Мониторинг запущен")
    while not stop_event.is_set():
        cfg   = load_config()
        state = load_state()
        hist  = load_history()
        # found_new: {route_id: {"route": ..., "by_date": {date: [train, ...]}}}
        found_new = {}

        for route in cfg.get("routes", []):
            if not route.get("active"):
                continue
            for date_str in route.get("dates", []):
                data = fetch_trains(route["from_code"], route["to_code"], date_str)
                if not data:
                    continue
                trains, err_msg = parse_response(data)

                hist.append({
                    "ts":        datetime.now().isoformat(),
                    "route_id":  route["id"],
                    "from_name": route["from_name"],
                    "to_name":   route["to_name"],
                    "date":      date_str,
                    "trains":    len(trains),
                    "seats":     sum(t["total_seats"] for t in trains),
                    "error":     err_msg,
                })

                for t in trains:
                    if t["total_seats"] == 0:
                        continue
                    bucket = (t["total_seats"] // 10) * 10
                    key = f"{route['id']}:{date_str}:{t['number']}:{bucket}"
                    if key not in state:
                        state[key] = datetime.now().isoformat()
                        rid = route["id"]
                        if rid not in found_new:
                            found_new[rid] = {"route": route, "by_date": {}}
                        if date_str not in found_new[rid]["by_date"]:
                            found_new[rid]["by_date"][date_str] = []
                        found_new[rid]["by_date"][date_str].append(t)

                time.sleep(2)

        save_history(hist)

        for rid, entry in found_new.items():
            route    = entry["route"]
            by_date  = entry["by_date"]
            email_to = route.get("email_to", "").strip()

            from_node  = route.get("from_node_id", "")
            to_node    = route.get("to_node_id", "")
            ntfy_topic = route.get("ntfy_topic", "").strip()

            # ── Email: подробное письмо со всеми датами ──
            email_lines = [f"Найдены свободные места!\n{route['from_name']} → {route['to_name']}\n"]
            for date_str, trains in sorted(by_date.items()):
                email_lines.append(f"\n{date_str}:")
                for t in trains:
                    h, m = divmod(int(t["duration"] or 0), 60)
                    email_lines.append(f"  Поезд {t['number']}  {t['depart'][11:16]}→{t['arrive'][11:16]}  {h}ч{m:02d}м  {t['total_seats']} мест")
                    for car_type, g in t["cars"].items():
                        seats = g["total"] or g["lower"]+g["upper"]+g["lower_side"]+g["upper_side"]
                        if seats:
                            price = f"от {g['min_price']:,.0f}₽".replace(",","_") if g["min_price"] else ""
                            email_lines.append(f"    • {g['name']}: {seats} мест {price}")
                if from_node and to_node:
                    d = datetime.strptime(date_str, "%Y-%m-%d")
                    url_date = f"{d.year}-{d.month}-{d.day}"
                    email_lines.append(f"  https://ticket.rzd.ru/searchresults/v/1/{from_node}/{to_node}/{url_date}?adult=1")
            if not (from_node and to_node):
                email_lines.append("\nhttps://ticket.rzd.ru/")
            subject = f"РЖД: билеты {route['from_name']} → {route['to_name']}"
            if email_to:
                send_email(cfg, subject, "\n".join(email_lines), email_to=email_to)

            # ── ntfy: одно короткое уведомление на каждую дату ──
            for date_str in sorted(by_date.keys()):
                d = datetime.strptime(date_str, "%Y-%m-%d")
                date_fmt = f"{d.day} {_RU_MONTHS[d.month - 1]} {d.year}"
                ntfy_title = f"{route['from_name']} → {route['to_name']}"
                ntfy_body  = f"Есть билеты на {date_fmt}"
                if from_node and to_node:
                    url_date  = f"{d.year}-{d.month}-{d.day}"
                    click_url = f"https://ticket.rzd.ru/searchresults/v/1/{from_node}/{to_node}/{url_date}?adult=1"
                else:
                    click_url = "https://ticket.rzd.ru/"
                send_ntfy(cfg, ntfy_title, ntfy_body, ntfy_topic=ntfy_topic, click_url=click_url)

        save_state(state)

        log.info(f"Проверка завершена. Следующая через {cfg.get('interval_min',5)} мин.")
        stop_event.wait(int(cfg.get("interval_min", 5)) * 60)

    log.info("Мониторинг остановлен")

def start_monitor():
    global monitor_thread, stop_event
    if monitor_thread and monitor_thread.is_alive():
        return
    stop_event = threading.Event()
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()

def stop_monitor():
    stop_event.set()

# ── API роуты ─────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/api/config", methods=["GET"])
@login_required
def get_config():
    cfg = load_config()
    _exclude = {"smtp_password", "ntfy_token", "ntfy_password", "secret_key", "ui_password_hash", "ui_password_salt"}
    safe = {k: v for k, v in cfg.items() if k not in _exclude}
    safe["smtp_password"] = "••••••••" if cfg.get("smtp_password") else ""
    safe["ntfy_token"]    = "••••••••" if cfg.get("ntfy_token") else ""
    safe["ntfy_password"] = "••••••••" if cfg.get("ntfy_password") else ""
    safe["monitoring_active"] = monitor_thread is not None and monitor_thread.is_alive()
    return jsonify(safe)

@app.route("/api/config", methods=["POST"])
@login_required
def update_config():
    cfg  = load_config()
    data = request.json
    for k in ["smtp_host","smtp_port","smtp_user","interval_min","ntfy_url","ntfy_topic","ntfy_user"]:
        if k in data:
            cfg[k] = data[k]
    if data.get("smtp_password") and not data["smtp_password"].startswith("•"):
        cfg["smtp_password"] = data["smtp_password"]
    if data.get("ntfy_token") and not data["ntfy_token"].startswith("•"):
        cfg["ntfy_token"] = data["ntfy_token"]
    if data.get("ntfy_password") and not data["ntfy_password"].startswith("•"):
        cfg["ntfy_password"] = data["ntfy_password"]
    save_config(cfg)
    return jsonify({"ok": True})

@app.route("/api/routes", methods=["GET"])
@login_required
def get_routes():
    return jsonify(load_config().get("routes", []))

@app.route("/api/routes", methods=["POST"])
@login_required
def add_route():
    cfg   = load_config()
    route = request.json
    route["id"] = str(int(time.time()))
    cfg["routes"].append(route)
    save_config(cfg)
    return jsonify({"ok": True, "id": route["id"]})

@app.route("/api/routes/<route_id>", methods=["PUT"])
@login_required
def update_route(route_id):
    cfg = load_config()
    for i, r in enumerate(cfg["routes"]):
        if r["id"] == route_id:
            cfg["routes"][i] = {**r, **request.json, "id": route_id}
            break
    save_config(cfg)
    return jsonify({"ok": True})

@app.route("/api/routes/<route_id>", methods=["DELETE"])
@login_required
def delete_route(route_id):
    cfg = load_config()
    cfg["routes"] = [r for r in cfg["routes"] if r["id"] != route_id]
    save_config(cfg)
    return jsonify({"ok": True})

@app.route("/api/monitoring", methods=["GET"])
@login_required
def get_monitoring():
    return jsonify({"active": monitor_thread is not None and monitor_thread.is_alive()})

@app.route("/api/monitoring", methods=["POST"])
@login_required
def toggle_monitoring():
    data = request.json
    cfg  = load_config()
    if data.get("active"):
        cfg["monitoring"] = True
        save_config(cfg)
        start_monitor()
    else:
        cfg["monitoring"] = False
        save_config(cfg)
        stop_monitor()
    return jsonify({"ok": True, "active": data.get("active")})

@app.route("/api/check_now", methods=["POST"])
@login_required
def check_now():
    """Ручная немедленная проверка одного маршрута/даты."""
    data      = request.json
    from_code = data.get("from_code", "2004000")
    to_code   = data.get("to_code",   "2060150")
    date_str  = data.get("date")
    resp = fetch_trains(from_code, to_code, date_str)
    if not resp:
        return jsonify({"error": "Не удалось получить данные"}), 500
    trains, err_msg = parse_response(resp)
    return jsonify({"trains": trains, "error": err_msg})

@app.route("/api/history", methods=["GET"])
@login_required
def get_history():
    hist = load_history()
    days = int(request.args.get("days", 1))
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    hist = [h for h in hist if h.get("ts", "") >= cutoff]
    return jsonify(list(reversed(hist)))

@app.route("/api/history", methods=["DELETE"])
@login_required
def clear_history():
    save_history([])
    return jsonify({"ok": True})

@app.route("/api/test_email", methods=["POST"])
@login_required
def test_email():
    cfg      = load_config()
    data     = request.json or {}
    email_to = data.get("email_to", "").strip()
    if not email_to:
        return jsonify({"ok": False, "error": "Не указан получатель"})
    ok = send_email(cfg, "Тест — РЖД Монитор", "Если вы получили это письмо, email настроен правильно.", email_to=email_to)
    return jsonify({"ok": ok})

@app.route("/api/test_ntfy", methods=["POST"])
@login_required
def test_ntfy():
    cfg = load_config()
    ok  = send_ntfy(cfg, "Тест — РЖД Монитор", "Если вы получили это уведомление, ntfy настроен правильно.")
    return jsonify({"ok": ok})

@app.route("/api/station_search", methods=["GET"])
@login_required
def station_search():
    """Поиск станции по названию через API РЖД."""
    q = request.args.get("q", "")
    if len(q) < 2:
        return jsonify([])
    try:
        r = rzd_session.get(
            "https://ticket.rzd.ru/api/v1/suggests",
            params={
                "Query": q,
                "TransportType": "bus,avia,rail,aeroexpress,suburban,boat",
                "GroupResults": "true",
                "RailwaySortPriority": "true",
                "SynonymOn": 1,
                "Language": "ru",
            },
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        results = []
        # Сначала города (у них expressCode — агрегированный код)
        for item in (data.get("city") or []):
            code = item.get("expressCode", "")
            if not code:
                continue
            region = item.get("region", "")
            label = item.get("name", "")
            if region:
                label += f" ({region.split(',')[0]})"
            node_id = item.get("nodeId") or item.get("id") or ""
            results.append({"name": label, "code": code, "node_id": node_id})
            if len(results) >= 5:
                break
        # Потом конкретные ж/д станции
        for item in (data.get("train") or []):
            code = item.get("expressCode", "")
            if not code:
                continue
            region = item.get("region", "")
            label = item.get("name", "")
            if region:
                label += f" — {region.split(',')[0]}"
            node_id = item.get("nodeId") or item.get("id") or ""
            results.append({"name": label, "code": code, "node_id": node_id})
            if len(results) >= 12:
                break
        return jsonify(results)
    except Exception as e:
        log.error(f"station_search error: {e}")
        return jsonify([])

@app.route("/api/me", methods=["GET"])
def get_me():
    cfg = load_config()
    has_password = bool(cfg.get("ui_password_hash"))
    authenticated = not has_password or bool(session.get("authenticated"))
    return jsonify({"authenticated": authenticated, "has_password": has_password})

@app.route("/api/login", methods=["POST"])
def api_login():
    ok, seconds_left = _check_rate_limit()
    if not ok:
        mins = (seconds_left + 59) // 60
        return jsonify({"ok": False, "error": f"Слишком много попыток. Подождите {mins} мин."}), 429

    data = request.json or {}
    password = data.get("password", "")
    cfg = load_config()
    if not cfg.get("ui_password_hash"):
        session["authenticated"] = True
        return jsonify({"ok": True})
    if verify_password(password, cfg["ui_password_hash"], cfg["ui_password_salt"]):
        _reset_attempts()
        session["authenticated"] = True
        return jsonify({"ok": True})
    _record_failure()
    rec = _login_attempts.get(_get_ip(), {})
    if rec.get("locked_until", 0) > time.time():
        mins = _LOCKOUT_SEC // 60
        return jsonify({"ok": False, "error": f"Слишком много попыток. Подождите {mins} мин."}), 429
    left = _MAX_ATTEMPTS - rec.get("count", 0)
    return jsonify({"ok": False, "error": f"Неверный пароль. Осталось попыток: {left}"}), 401

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("authenticated", None)
    return jsonify({"ok": True})

@app.route("/api/change_password", methods=["POST"])
@login_required
def change_password():
    data = request.json or {}
    current = data.get("current_password", "")
    new_pw  = data.get("new_password", "")
    cfg = load_config()
    if cfg.get("ui_password_hash"):
        if not verify_password(current, cfg["ui_password_hash"], cfg["ui_password_salt"]):
            return jsonify({"ok": False, "error": "Неверный текущий пароль"}), 401
    if new_pw:
        hashed, salt = hash_password(new_pw)
        cfg["ui_password_hash"] = hashed
        cfg["ui_password_salt"] = salt
    else:
        cfg["ui_password_hash"] = ""
        cfg["ui_password_salt"] = ""
    save_config(cfg)
    return jsonify({"ok": True})

if __name__ == "__main__":
    # Восстанавливаем мониторинг если был включён
    cfg = load_config()
    if cfg.get("monitoring"):
        start_monitor()
    app.run(host="0.0.0.0", port=5000, debug=False)
