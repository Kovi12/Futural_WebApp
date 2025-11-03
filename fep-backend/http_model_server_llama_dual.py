from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime
from typing import Optional
from transformers import pipeline
from bs4 import BeautifulSoup
from common.path_utils import get_worldcities_csv

from unsloth import FastLanguageModel
import torch, os, re, unicodedata, csv, requests, threading, queue, sqlite3, time, socket

MODEL_PATH = "meta-llama/Llama-3.1-8B-Instruct"
ADAPTERS = {
    "compression": "Llama3-8b_fn_nometeo",
    "meteo": "Llama3-8b_fn",
}
MODEL_SAFE = "llama_dual"

BASE_DIR = os.getenv("WEBAPP_BASE_DIR", os.path.expanduser("~/Futural_WebApp"))
JOB_LOG_DIR = os.getenv("JOB_LOG_DIR", os.path.join(BASE_DIR, "logs", MODEL_SAFE, os.getenv("SLURM_JOB_ID", "local")))
os.makedirs(JOB_LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(JOB_LOG_DIR, "model_debug.log")
DB_PATH = os.getenv("CONV_DB", os.path.join(BASE_DIR, "data", "conversations.sqlite"))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

query_queue: "queue.Queue[dict]" = queue.Queue(maxsize=128)
worker_should_run = True

app = FastAPI()
model_ready = False
pipe = None
tokenizer = None
model = None


class QueryInput(BaseModel):
    text: str
    token: Optional[str] = None
    session_id: Optional[str] = None
    client_ip: Optional[str] = None
    adapter: Optional[str] = None
    model: Optional[str] = None


def log(msg: str):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass


_db_lock = threading.Lock()
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.execute("PRAGMA journal_mode=WAL;")
_db.execute("PRAGMA synchronous=NORMAL;")
_db.execute(
    """
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        user_token TEXT,
        session_id TEXT,
        client_ip TEXT,
        model TEXT,
        adapter TEXT,
        slurm_job_id TEXT,
        node TEXT,
        queue_len INTEGER,
        queue_wait_ms INTEGER,
        gen_time_ms INTEGER,
        latency_ms INTEGER,
        prompt TEXT,
        response TEXT,
        error TEXT
    )
    """
)
_db.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_ts ON messages(user_token, ts);")
_db.execute("CREATE INDEX IF NOT EXISTS idx_messages_job ON messages(slurm_job_id);")
_db.commit()


def _db_insert(row: dict):
    try:
        with _db_lock:
            _db.execute(
                """
                INSERT INTO messages
                (ts, user_token, session_id, client_ip, model, adapter, slurm_job_id, node,
                 queue_len, queue_wait_ms, gen_time_ms, latency_ms, prompt, response, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("ts"),
                    row.get("user_token"),
                    row.get("session_id"),
                    row.get("client_ip"),
                    row.get("model"),
                    row.get("adapter"),
                    row.get("slurm_job_id"),
                    row.get("node"),
                    row.get("queue_len"),
                    row.get("queue_wait_ms"),
                    row.get("gen_time_ms"),
                    row.get("latency_ms"),
                    row.get("prompt"),
                    row.get("response"),
                    row.get("error"),
                ),
            )
            _db.commit()
    except Exception as e:
        log(f"[WARN] DB insert failed: {e}")


def _job_metadata(adapter: str):
    return {
        "model": MODEL_SAFE,
        "adapter": adapter,
        "slurm_job_id": os.getenv("SLURM_JOB_ID", ""),
        "node": os.getenv("SLURMD_NODENAME", "") or socket.gethostname(),
    }


def format_prompt(prompt: str) -> str:
    return (
        "<|begin_of_text|>\n"
        "<|start_header_id|>system<|end_header_id|>\n"
        "<|eot_id|>\n"
        "<|start_header_id|>user<|end_header_id|>\n"
        f"{prompt}\n"
        "<|eot_id|>\n"
        "<|start_header_id|>assistant<|end_header_id|>\n"
    )


def sanitize_output(raw_text: str, adapter: str) -> str:
    if adapter == "compression":
        parts = re.findall(
            r'(^[\w\d\s,.?!]+)|</.+?>\s*(.*?)(?=<.+?>|$)|<API>.*-->\s*(.*)\s*</API>',
            raw_text,
        )
        return "".join(p for group in parts for p in group if p).strip()
    return raw_text.strip()


def _f_to_c(x: float) -> int:
    return int(round((x - 32.0) * 5.0 / 9.0))


def _mph_to_kmh_value(v: float) -> int:
    return int(round(v * 1.6))


def _parse_speed_cell(s: str, imperial: bool) -> str:
    s = s.strip().lower()
    if "mph" in s:
        imperial = True
    num = re.sub(r"[^0-9.\-]", "", s)
    if not num:
        return "-"
    if "-" in num:
        a, b = num.split("-", 1)
        try:
            a = float(a)
            b = float(b)
        except ValueError:
            return "-"
        if imperial:
            a = _mph_to_kmh_value(a)
            b = _mph_to_kmh_value(b)
        return f"{int(round(a))}-{int(round(b))}"
    else:
        try:
            v = float(num)
        except ValueError:
            return "-"
        if imperial:
            v = _mph_to_kmh_value(v)
        return str(int(round(v)))


def _parse_precip_cell(s: str, imperial: bool) -> str:
    s = s.strip().lower()
    if s == "-":
        return "-"
    less = s.startswith("<")
    is_inch = '"' in s or re.search(r"\bin\b", s)
    m = re.search(r"([0-9]*\.?[0-9]+)", s)
    if not m:
        return s
    val = float(m.group(1))
    if imperial or is_inch:
        mm = int(round(val * 25.4))
        return f"< {mm}" if less else str(mm)
    mm = int(round(val))
    return f"< {mm}" if less else str(mm)


def parse_response(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    table = soup.find("table", class_="picto three-hourly-view")
    if not table:
        return None

    clouds = [c.find("img")["title"]
              for c in table.find_all("div", class_="pictoicon")
              if c.find("img")]

    def _grab_degrees(tr_class):
        row = table.find("tr", class_=tr_class)
        if not row:
            return []
        txt = row.get_text("\n", strip=True)
        cells = [t.strip() for t in txt.split("\n") if "°" in t]
        cells = cells[::2] if len(cells) > 8 else cells
        out = []
        for t in cells:
            is_f = "°f" in t.lower()
            num = t.replace("°", "").replace("C", "").replace("F", "").strip()
            try:
                n = int(num)
            except ValueError:
                continue
            out.append(_f_to_c(float(n)) if is_f else n)
        return out

    temps = _grab_degrees("temperatures")
    felt = _grab_degrees("windchills")
    try:
        app_log = f"[PARSE TEMP ROW] {table.find('tr','temperatures').get_text(' | ', strip=True)}"
        log(app_log)
    except Exception:
        pass
    try:
        app_log = f"[PARSE FEEL ROW] {table.find('tr','windchills').get_text(' | ', strip=True)}"
        log(app_log)
    except Exception:
        pass

    wind_row = table.find("tr", class_="windspeeds")
    wind_dir, wind_spd = [], []
    if wind_row:
        parts = wind_row.get_text("\n", strip=True).split("\n")[2:]
        wind_dir = parts[0::3]
        wind_spd = [_parse_speed_cell(s, imperial=False) for s in parts[1::3]]

    prec_row = table.find("tr", class_="precips")
    prec_prob, prec_amt = [], []
    if prec_row:
        parts = prec_row.get_text("\n", strip=True).split("\n")[2:]
        prec_prob = parts[1::3]
        prec_amt = [_parse_precip_cell(s, imperial=False) for s in parts[2::3]]

    return {
        "temperatures_real (°C)": temps,
        "temperature_felt (°C)": felt,
        "wind_speed (km/h)": wind_spd,
        "wind_direction": wind_dir,
        "precipitation_size (mm/3h)": prec_amt,
        "precipitation_probability": prec_prob,
        "cloud_cover": clouds,
    }


def generate_meteoblue_url(lat, lon) -> str:
    NS = "N" if lat >= 0 else "S"
    EW = "E" if lon >= 0 else "W"
    return f"https://www.meteoblue.com/en/weather/week/{abs(lat):.3f}{NS}{abs(lon):.3f}{EW}"


def meteo_api_call(lat, lon):
    url = generate_meteoblue_url(lat, lon)
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-GB,en;q=0.9"}
    res = requests.get(url, headers=headers, timeout=30)
    if res.status_code == 200:
        return parse_response(res.text)
    return None


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("utf-8").strip().lower()


def extract_city_lat_lon(city: str):
    if not city:
        return None, None
    target = _norm(city)
    path = get_worldcities_csv()
    exact, starts, contains = None, None, None
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                try:
                    name = _norm(row[1])
                    lat = float(row[2])
                    lon = float(row[3])
                except (IndexError, ValueError):
                    continue
                if name == target and exact is None:
                    exact = (lat, lon)
                elif name.startswith(target) and starts is None:
                    starts = (lat, lon)
                elif target in name and contains is None:
                    contains = (lat, lon)
    except FileNotFoundError:
        return None, None
    return exact or starts or contains or (None, None)


_nlp = None
def get_nlp():
    global _nlp
    if _nlp is not None:
        return _nlp
    try:
        import spacy
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
        try:
            spacy.prefer_gpu(False)
        except Exception:
            pass
        _nlp = spacy.load("en_core_web_sm",
                          disable=["tagger", "parser", "lemmatizer", "attribute_ruler"])
        log("[NLP] spaCy en_core_web_sm loaded (CPU)")
    except Exception as e:
        log(f"[NLP][WARN] spaCy unavailable ({e}); will use regex fallback")
        _nlp = None
    return _nlp


_CITY_FALLBACK = re.compile(r"\b(?:in|for|at|around|near)\s+([A-Za-zÀ-ÿ'’\-\s]+)", re.I)

def extract_city_name(text: str) -> Optional[str]:
    t = (text or "").strip()
    nlp = get_nlp()
    preps = r"\b(?:in|on|at|to|for|from|by|near|around|about|into|onto)\b"

    if nlp:
        try:
            doc = nlp(t)
            candidates = []
            for ent in doc.ents:
                if ent.label_ == "GPE":
                    city = ent.text.strip(" ,.")
                    city = re.sub(f"^{preps}\\s+", "", city, flags=re.IGNORECASE)
                    city = re.sub(f"\\s+{preps}$", "", city, flags=re.IGNORECASE)
                    city = re.split(preps, city, flags=re.IGNORECASE)[0].strip(" ,.")
                    city = re.sub(
                        r"\b(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
                        "",
                        city,
                        flags=re.IGNORECASE,
                    ).strip(" ,.")
                    if city:
                        candidates.append(city)
            if candidates:
                return candidates[0]
        except Exception as e:
            log(f"[NLP][WARN] spaCy failed ({e}); using regex fallback")

    _CITY_FALLBACK = re.compile(r"\b(?:in|for|at|around|near|on)\s+([A-Za-zÀ-ÿ'’\-\s]+)", re.I)
    m = _CITY_FALLBACK.search(t)
    if m:
        city = m.group(1).strip(" ,.")
        city = re.split(preps, city, flags=re.IGNORECASE)[0].strip(" ,.")
        city = re.sub(
            r"\b(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            "",
            city,
            flags=re.IGNORECASE,
        ).strip(" ,.")
        return city

    return None


def _safe_set_adapter(m, name: str):
    try:
        if hasattr(m, "set_adapter"):
            m.set_adapter(name)
            return True
    except Exception as e:
        log(f"[WARN] set_adapter failed: {e}")
    return False


log(f"[BOOT] Loading base model: {MODEL_PATH}")
try:
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=MODEL_PATH,
        max_seq_length=1550,
        dtype=torch.float16,
        load_in_4bit=True,
        device_map="auto",
    )
    for name, path in ADAPTERS.items():
        try:
            model.load_adapter(path, adapter_name=name)
            log(f"[BOOT] Loaded adapter '{name}' from {path}")
        except TypeError:
            model.load_adapter(path)
            log(f"[BOOT] Loaded adapter (no-named) from {path}")

    FastLanguageModel.for_inference(model)
    pipe = pipeline("text-generation", model=model, tokenizer=tokenizer, device_map="auto")
    model_ready = True
    log("[BOOT] Llama dual server ready.")
except Exception as e:
    model_ready = False
    log(f"[FATAL] Model failed to load: {e}")
    raise


def _worker_loop():
    log("[WORKER] started")
    while worker_should_run:
        try:
            job = query_queue.get(timeout=5.0)
        except queue.Empty:
            continue

        err_text = None
        resp_text = None
        enqueued_ts = job.get("start_ts", time.time())
        proc_start = time.time()

        try:
            text = job["text"]
            adapter = job["adapter"]

            log(f"[SWITCH] Request wants adapter={adapter}, current_thread={threading.get_ident()}")
            try:
                before = time.time()
                ok = _safe_set_adapter(model, adapter)
                log(f"[SWITCH] set_adapter({adapter}) returned={ok} in {time.time() - before:.2f}s")
            except Exception as e:
                log(f"[SWITCH][ERROR] adapter switch failed for {adapter}: {e}")

            if adapter == "meteo":
                city = extract_city_name(text)
                lat, lon = extract_city_lat_lon(city) if city else (None, None)
                log(f"[METEO] city={city!r} lat={lat} lon={lon}")
                if lat and lon:
                    data = meteo_api_call(lat, lon)
                    if not data:
                        resp_text = f"Could not fetch weather for {city}."
                    else:
                        prompt = format_prompt(str(data))
                        log(f"[GEN] starting generation for adapter={adapter}")
                        with torch.no_grad():
                            out = pipe(prompt, truncation=True, max_new_tokens=1200)[0]["generated_text"]
                        log(f"[GEN] finished generation for adapter={adapter}")

                        split = out.split("<|start_header_id|>assistant<|end_header_id|>")[-1]
                        resp_text = sanitize_output(split, adapter)
                        log(f"[GEN RAW {adapter}] {out!r}")
                        log(f"[GEN SPLIT {adapter}] {split!r}")
                        log(f"[GEN CLEAN {adapter}] {resp_text!r}")
                else:
                    resp_text = "No valid city found in the input."
            else:
                prompt = format_prompt(text)
                with torch.no_grad():
                    out = pipe(prompt, truncation=True, max_new_tokens=1500)[0]["generated_text"]
                split = out.split("<|start_header_id|>assistant<|end_header_id|>")[-1]
                resp_text = sanitize_output(split, adapter)
                log(f"[GEN RAW {adapter}] {out!r}")
                log(f"[GEN SPLIT {adapter}] {split!r}")
                log(f"[GEN CLEAN {adapter}] {resp_text!r}")

        except Exception as e:
            log(f"[ERROR] job failed: {e}")
            err_text = "[ERROR] Model processing failed."
            resp_text = err_text
        finally:
            proc_end = time.time()
            queue_wait_ms = int(max(0, (proc_start - enqueued_ts) * 1000))
            gen_time_ms = int(max(0, (proc_end - proc_start) * 1000))
            total_latency_ms = int(max(0, (proc_end - enqueued_ts) * 1000))

            try:
                job["result"]["response"] = resp_text
                job["done"].set()
            except Exception:
                pass

            try:
                meta = _job_metadata(job["adapter"])
                row = {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "user_token": job.get("token"),
                    "session_id": job.get("session_id"),
                    "client_ip": job.get("client_ip"),
                    "model": meta["model"],
                    "adapter": meta["adapter"],
                    "slurm_job_id": meta["slurm_job_id"],
                    "node": meta["node"],
                    "queue_len": job.get("queue_len"),
                    "queue_wait_ms": queue_wait_ms,
                    "gen_time_ms": gen_time_ms,
                    "latency_ms": total_latency_ms,
                    "prompt": job.get("text"),
                    "response": resp_text,
                    "error": err_text,
                }
                _db_insert(row)
            except Exception as e:
                log(f"[WARN] logging to DB failed: {e}")

            query_queue.task_done()


threading.Thread(target=_worker_loop, daemon=True).start()


@app.get("/health")
async def health():
    current = None
    try:
        if hasattr(model, "active_adapter"):
            current = getattr(model, "active_adapter")
    except Exception:
        pass
    return {
        "status": "running" if model_ready else "loading",
        "queue_len": query_queue.qsize(),
        "current_adapter": current,
    }


@app.post("/query")
async def query(input: QueryInput):
    text = (input.text or "").strip()
    if not text:
        return {"response": "[EMPTY PROMPT]"}

    adapter = (input.adapter or input.model or "compression").strip().lower()
    if adapter not in ADAPTERS:
        adapter = "compression"

    done = threading.Event()
    result = {}

    job = {
        "text": text,
        "adapter": adapter,
        "done": done,
        "result": result,
        "token": input.token,
        "session_id": input.session_id,
        "client_ip": input.client_ip,
        "queue_len": query_queue.qsize(),
        "start_ts": time.time(),
    }

    try:
        query_queue.put(job, timeout=5.0)
    except queue.Full:
        return {"response": "[SERVER BUSY] Please retry shortly."}

    done.wait(timeout=300)
    return {"response": result.get("response", "[TIMEOUT OR EMPTY RESPONSE]")}


@app.get("/history")
async def history(token: str, limit: int = 50):
    limit = max(1, min(limit, 500))
    rows = []
    try:
        with _db_lock:
            cur = _db.execute(
                """
                SELECT ts, model, adapter, slurm_job_id, node,
                       queue_len, queue_wait_ms, gen_time_ms, latency_ms,
                       prompt, response, error
                FROM messages
                WHERE user_token = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (token, limit),
            )
            for r in cur.fetchall():
                rows.append({
                    "ts": r[0],
                    "model": r[1],
                    "adapter": r[2],
                    "slurm_job_id": r[3],
                    "node": r[4],
                    "queue_len": r[5],
                    "queue_wait_ms": r[6],
                    "gen_time_ms": r[7],
                    "latency_ms": r[8],
                    "prompt": r[9],
                    "response": r[10],
                    "error": r[11],
                })
    except Exception as e:
        return {"error": f"DB read failed: {e}"}
    return {"items": rows, "count": len(rows)}
