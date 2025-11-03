from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List, Dict, Any
import threading, queue, sqlite3, socket, time, os, re

from handlers.durangaldea_model import run_durangaldea_model
from handlers.script_locatie import get_closest_distance_time

MODEL_SAFE = "deepseek_durangaldea"
ADAPTER_NAME = "valy3124/durangaldea-assistantFinalPD"

BASE_DIR = os.getenv("WEBAPP_BASE_DIR", os.path.expanduser("~/Futural_WebApp"))
JOB_LOG_DIR = os.getenv(
    "JOB_LOG_DIR",
    os.path.join(BASE_DIR, "logs", MODEL_SAFE, os.getenv("SLURM_JOB_ID", "local")),
)
os.makedirs(JOB_LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(JOB_LOG_DIR, "model_debug.log")

DB_PATH = os.getenv("CONV_DB", os.path.join(BASE_DIR, "data", "conversations.sqlite"))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

query_queue: "queue.Queue[dict]" = queue.Queue(maxsize=128)
worker_should_run = True
app = FastAPI()
model_ready = True

class QueryInput(BaseModel):
    text: str
    token: Optional[str] = None
    session_id: Optional[str] = None
    client_ip: Optional[str] = None

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

def _job_metadata():
    return {
        "model": MODEL_SAFE,
        "adapter": ADAPTER_NAME,
        "slurm_job_id": os.getenv("SLURM_JOB_ID", ""),
        "node": os.getenv("SLURMD_NODENAME", "") or socket.gethostname(),
    }

_API_BLOCK_RE = re.compile(r"<API>(.*?)</API>", re.DOTALL)

def extract_api_call_from_answer(response_text: str) -> Optional[str]:
    if not response_text:
        return None
    m = _API_BLOCK_RE.search(response_text)
    if not m:
        return None
    return m.group(1).strip()

def extract_answer_only(response_text: str) -> str:
    parts = response_text.split("### Answer:", 1)
    return parts[1].strip() if len(parts) == 2 else response_text.strip()

def parse_api_call(call_str: str) -> Dict[str, Any]:
    pattern = r'(\w+)\s*=\s*(?:"([^"]+)"|([\d.]+))'
    matches = re.findall(pattern, call_str or "")
    kwargs: Dict[str, Any] = {}
    for key, str_value, num_value in matches:
        if str_value != "":
            kwargs[key] = str_value
        else:
            kwargs[key] = float(num_value) if "." in num_value else int(num_value)
    return kwargs

def _format_with_api_result(cleaned_text: str, api_result: Any) -> str:
    try:
        if isinstance(api_result, dict):
            items = [api_result]
        elif isinstance(api_result, list):
            items = api_result
        else:
            items = []

        if not items:
            return cleaned_text or "No relevant information found."

        first = items[0]

        if isinstance(first, dict):
            if "distance" in first:
                try:
                    km = float(first["distance"]) / 1000.0
                    suffix = f"{km:.2f} km away"
                except Exception:
                    suffix = "distance available"
                return f"{cleaned_text} {suffix}".strip()

            if "time" in first:
                try:
                    minutes = float(first["time"])
                    suffix = f"{minutes:.1f} minutes away"
                except Exception:
                    suffix = "time available"
                return f"{cleaned_text} {suffix}".strip()

            if "addresses" in first and isinstance(first["addresses"], list):
                joined = "; ".join(first["addresses"])
                return f"{cleaned_text} {joined}".strip()

        return cleaned_text or "No relevant information found."
    except Exception as e:
        log(f"[FORMAT][WARN] failed to format API result: {e}")
        return cleaned_text or "No relevant information found."

def _finalize_user_text(raw_model_text: str) -> str:
    base = extract_answer_only(raw_model_text)
    api_call_text = extract_api_call_from_answer(base)
    cleaned = _API_BLOCK_RE.sub("", base).strip()

    if not api_call_text:
        return cleaned or "No relevant information found."

    api_result = None
    try:
        kwargs = parse_api_call(api_call_text)
        log(f"[API][EXEC] get_closest_distance_time kwargs={kwargs}")
        api_result = get_closest_distance_time(**kwargs)
    except Exception as e:
        log(f"[API][ERROR] execution failed: {e}")

    return _format_with_api_result(cleaned, api_result)

def _worker_loop():
    log("[WORKER] deepseek started")
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
            raw = run_durangaldea_model(text) or ""
            log(f"[RAW OUT] {repr(raw[:1000])}")
            resp_text = _finalize_user_text(raw)
            log(f"[FINAL OUT] {repr(resp_text)}")

        except Exception as e:
            log(f"[ERROR] generation failed: {e}")
            err_text = str(e)
            resp_text = "Sorry, something went wrong while processing your request."

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
                meta = _job_metadata()
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
                log(f"[WARN] DB write failed: {e}")

            query_queue.task_done()

threading.Thread(target=_worker_loop, daemon=True).start()

@app.get("/health")
async def health():
    return {"status": "running" if model_ready else "loading", "queue_len": query_queue.qsize()}

@app.post("/query")
async def query(input: QueryInput):
    text = (input.text or "").strip()
    if not text:
        return {"response": "[EMPTY PROMPT]"}

    done = threading.Event()
    result: Dict[str, str] = {}
    job = {
        "text": text,
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
    rows: List[Dict[str, Any]] = []
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
