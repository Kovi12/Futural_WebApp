import os
import re
import time
import uuid
import requests
import subprocess
import threading
import logging
from typing import Dict, Set
from collections import defaultdict
from threading import Lock
from threading import Timer
from time import time as now

from flask import Flask, request, jsonify, session
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.exceptions import HTTPException


def _setup_logger() -> logging.Logger:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logger = logging.getLogger("dual_backend")
    logger.setLevel(level)

    if not logger.handlers:
        fmt = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
        datefmt = "%Y-%m-%d %H:%M:%S"

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
        logger.addHandler(stream_handler)

        log_file = os.getenv("LOG_FILE")
        if log_file:
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
            logger.addHandler(file_handler)

    return logger

log = _setup_logger()

app = Flask(__name__)
CORS(
    app,
    supports_credentials=True,
    resources={r"/*": {"origins": [
        "http://141.85.248.2:7171",
        "http://141.85.248.2:7172",
        "http://141.85.248.2:7173",
    ]}},
    expose_headers=["Set-Cookie"],
)

app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(32))

NODE_ORDER = ["wn02", "wn03", "wn01", "wn04"]

UPSTREAM = {
    "compression": {"up_model": "llama_dual", "slurm_name": "llama_dual",
                    "port": int(os.getenv("PORT_LLAMADUAL", "23459"))},
    "meteo":       {"up_model": "llama_dual", "slurm_name": "llama_dual",
                    "port": int(os.getenv("PORT_LLAMADUAL", "23459"))},
    "durangaldea": {"up_model": "durangaldea", "slurm_name": "durangaldea",
                    "port": int(os.getenv("PORT_DURANGALDEA", "23456"))},
}

ROUTE_TO_JOB = {
    "meteo": "llama_dual",
    "compression": "llama_dual",
    "durangaldea": "deepseek_dual",
}

ROUTE_LABELS = ("meteo", "compression", "durangaldea", "search")

def resolve_upstream(route: str) -> dict:
    m = UPSTREAM.get(route)
    if not m:
        return {"up_model": None, "slurm_name": None, "port": None}
    return m

MODEL_LOCKS = {"llama_dual": Lock(), "durangaldea": Lock()}

client_sessions: Dict[str, dict] = {}

model_tokens: defaultdict[str, Set[str]] = defaultdict(set)

running_jobs = {}

scheduling_jobs = defaultdict(bool)

monitor_threads = {}
monitor_stops = {}

sched_lock = Lock()
SSH_HOST = os.getenv("FEP_SSH_HOST", "fep")

scheduling_status = defaultdict(lambda: {
    "node": None,
    "job_id": None,
    "phase": "idle",
    "since": None,
    "tried": [],
})

pending_cancel_timers = {}
CANCEL_GRACE_SECONDS = int(os.getenv("CANCEL_GRACE_SECONDS", "400"))

IDLE_TIMEOUT_SECONDS = int(os.getenv("IDLE_TIMEOUT_SECONDS", "1300"))
TOKEN_IDLE_SECONDS = IDLE_TIMEOUT_SECONDS
ROTATION_CANCEL_ENABLED = os.getenv("ROTATION_CANCEL_ENABLED", "1") != "0"

last_query_time = defaultdict(lambda: time.time())

TERMINAL_STATES = {"CA", "CD", "F", "NF", "SE", "TO", "PR", "DL"}
RUN_STATES = {"R"}
PENDING_STATES = {"PD", "CF"}

SAFE_NO_CANCEL_WINDOW = 10
NODE_SWITCH_REASONS = {"NodeDown", "ReqNodeNotAvail", "Drain", "Maint", "Hardware", "Reservation"}
DO_NOT_SWITCH_REASONS = {
    "QOSMaxJobsPerUserLimit", "QOSMaxSubmitJobPerUserLimit",
    "AssocMaxJobsPerUser", "AssociationJobLimit",
    "Priority", "PartitionMaxNodes", "PartitionMaxCPUsPerUser"
}
PENDING_ROTATE_AT = 10
PENDING_HARD_ROTATE_AT = 20
MIN_USERS_TO_SUPPRESS_ROTATION = 3
ROTATION_ATTEMPT_LIMIT = 4

limiter = Limiter(key_func=get_remote_address, app=app, default_limits=[])


def audit_cancel(route_or_up, job_id, why):
    up_model = route_or_up if route_or_up in ("llama_dual", "durangaldea") else resolve_upstream(route_or_up)["up_model"]
    with sched_lock:
        users = 0
        for r, cfg in UPSTREAM.items():
            if cfg["up_model"] == up_model:
                users += len(model_tokens[r])
    log.info("AUDIT scancel job_id=%s upstream=%s why=%s users_total=%s", job_id, up_model, why, users)

def ssh(cmd):
    log.info("SSH: %s", cmd)
    res = subprocess.run(
        ["ssh", "-F", "/root/.ssh/config", SSH_HOST, cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if res.returncode != 0:
        log.warning("SSH nonzero rc=%s stderr=%s", res.returncode, (res.stderr or "").strip())
    return res.returncode, res.stdout, res.stderr

def submit_job_to_node(model, node):
    job_key = ROUTE_TO_JOB.get(model, model)
    slurm_cmd = (
        "bash -lc 'cd /export/home/proiecte/aux/ovidiu.ghibea/Futural_WebApp "
        f"&& ./slurm_job_dual.sh {job_key} {node}'"
    )
    rc, out, err = ssh(slurm_cmd)
    if rc != 0:
        log.error("Submit to %s failed: %s", node, err.strip())
        return None

    job_id = None
    for line in out.splitlines():
        if "Job submitted with ID" in line:
            job_id = line.strip().split()[-1]
            break
    if job_id:
        log.info("Submitted job %s on %s for route %s job=%s", job_id, node, model, job_key)
    else:
        log.error("Could not parse job id from output:\n%s", out)
    return job_id

def list_jobs_by_name(route_label: str):
    job_key = ROUTE_TO_JOB.get(route_label, route_label)
    cmd = (
        "bash -lc 'u=$(whoami); "
        f"squeue -h -o \"%i %t %N %j\" -u \"$u\" -n \"{job_key},{job_key}_webapp\"'"
    )
    rc, out, err = ssh(cmd)
    if rc != 0 or not out.strip():
        log.warning("list_jobs_by_name rc=%s err=%s", rc, (err or "").strip())
        return []
    res = []
    for line in out.strip().splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) >= 3:
            res.append({
                "job_id": parts[0],
                "short": parts[1],
                "node": parts[2],
                "name": parts[3] if len(parts) == 4 else "",
            })
    log.debug("list_jobs_by_name route=%s found=%s", route_label, len(res))
    return res


def pick_preferred_job(jobs: list[dict]) -> dict | None:
    if not jobs:
        return None
    running = [j for j in jobs if j["short"] == "R"]
    if running:
        return running[0]
    pending = [j for j in jobs if j["short"] == "PD"]
    if pending:
        return pending[0]
    return sorted(jobs, key=lambda j: int(j["job_id"]))[-1]

def get_job_info(job_id: str):
    rc, out, err = ssh(f"scontrol -o show job {job_id}")
    if rc != 0 or not out.strip():
        log.debug("get_job_info job_id=%s not found rc=%s", job_id, rc)
        return None
    line = out.strip().splitlines()[0]
    fields = {}
    for part in line.split():
        if "=" in part:
            k, v = part.split("=", 1)
            fields[k] = v
    state_full = fields.get("JobState", "")
    short = {
        "RUNNING": "R", "PENDING": "PD", "COMPLETING": "CG",
        "CANCELLED": "CA", "COMPLETED": "CD", "FAILED": "F",
        "TIMEOUT": "TO", "NODE_FAIL": "NF", "PREEMPTED": "PR"
    }.get(state_full, fields.get("StateCompact", ""))
    info = {
        "state": state_full,
        "short": short or "",
        "reason": fields.get("Reason", ""),
        "nodelist": fields.get("NodeList", ""),
        "reqnodelist": fields.get("ReqNodeList", ""),
    }
    log.debug("get_job_info job_id=%s short=%s reason=%s", job_id, info["short"], info["reason"])
    return info

def scancel(job_id):
    log.info("scancel %s", job_id)
    ssh(f"scancel {job_id}")

def healthcheck_url_for(route_label):
    port = resolve_upstream(route_label)["port"]
    return f"http://host.docker.internal:{port}/health"

def should_switch_node(reason: str) -> bool:
    if reason_matches_any(reason, DO_NOT_SWITCH_REASONS):
        return False
    if reason_matches_any(reason, NODE_SWITCH_REASONS):
        return True
    return False

def reason_matches_any(reason: str, candidates: set[str]) -> bool:
    if not reason:
        return False
    r = reason.strip()
    if r.startswith("(") and r.endswith(")"):
        r = r[1:-1]
    return any(k in r for k in candidates)

# stand-in for embedding classification --- to be changed and improved later
_RE_METEO = re.compile(r"\b(weather|forecast|temperature|rain|precip|wind|gust|snow|sunrise|sunset|today|tomorrow|weekend)\b", re.I)
_RE_COMP_AGRO = re.compile(r"\b(plant|sow|seed|germination|harvest|crop|cultivar|greenhouse|soil|frost|season)\b", re.I)
_RE_COMP_MULTI = re.compile(r"\b(and|also|both|together|combine)\b", re.I)

_RE_DURA_LOCAL = re.compile(r"\b(durangaldea|abadiñ|abadino|txanporta|matiena|amorebieta|zornotza|abant|ergarate|erkoreka)\b", re.I)
_RE_DURA_POI   = re.compile(r"\b(pharmacy|hospital|supermarket|clinic|school|park|gas|police|health|ambulance)\b", re.I)
_RE_DURA_DIST  = re.compile(r"\b(km|kilometer|kilometre|min|minute|closest|nearby|within|distance|drive|driving|walk|walking)\b", re.I)

def route_rules_only(text: str) -> dict:
    t = (text or "").strip()
    t_l = t.lower()

    if _RE_METEO.search(t_l):
        return {"route": "meteo", "confidence": 0.95, "reason": "meteo_keywords"}

    if _RE_COMP_AGRO.search(t_l) and not _RE_METEO.search(t_l):
        conf = 0.9 + (0.06 if _RE_COMP_MULTI.search(t_l) else 0.0)
        return {"route": "compression", "confidence": min(conf, 0.98), "reason": "agro_terms"}

    if _RE_DURA_LOCAL.search(t_l) and (_RE_DURA_POI.search(t_l) or _RE_DURA_DIST.search(t_l)):
        return {"route": "durangaldea", "confidence": 0.92, "reason": "durangaldea_local+poi_or_distance"}

    return {"route": "search", "confidence": 0.5, "reason": "fallback"}

def schedule_cancel_if_last(route_label):
    with sched_lock:
        t = pending_cancel_timers.get(route_label)
        if t and t.is_alive():
            return

    def _do_cancel():
        up = resolve_upstream(route_label)
        up_model = up["up_model"]

        with sched_lock:
            any_users = False
            for r, cfg in UPSTREAM.items():
                if cfg["up_model"] == up_model and len(model_tokens[r]) > 0:
                    any_users = True
                    break
            running = running_jobs.get(up_model)

        if any_users:
            log.info("CANCEL abort users still present upstream=%s", up_model)
            return

        if running:
            try:
                audit_cancel(up_model, running["job_id"], "cancel_model_grace_expired")
                scancel(running["job_id"])
                with sched_lock:
                    running_jobs.pop(up_model, None)
                    scheduling_jobs[route_label] = False
                    scheduling_status[route_label].update({"node": None, "job_id": None, "phase": "idle", "since": now()})
                log.info("CANCEL cancelled job=%s upstream=%s", running["job_id"], up_model)
            except Exception as e:
                log.exception("CANCEL error cancelling job for upstream '%s': %s", up_model, e)

    timer = Timer(CANCEL_GRACE_SECONDS, _do_cancel)
    with sched_lock:
        pending_cancel_timers[route_label] = timer
    log.info("CANCEL scheduled for route=%s in %ss upstream-aware", route_label, CANCEL_GRACE_SECONDS)
    timer.start()

def abort_scheduled_cancel(route_label):
    t = pending_cancel_timers.get(route_label)
    if t and t.is_alive():
        t.cancel()
        log.info("CANCEL abort scheduled cancel route=%s", route_label)
        with sched_lock:
            pending_cancel_timers.pop(route_label, None)

def token_reaper():
    if TOKEN_IDLE_SECONDS <= 0:
        log.info("TOKEN REAPER disabled")
        return
    while True:
        now_ts = time.time()
        to_expire = []
        to_check_cancel = []

        with sched_lock:
            for tok, info in list(client_sessions.items()):
                last_seen = info.get("last_seen", now_ts)
                if now_ts - last_seen > TOKEN_IDLE_SECONDS:
                    to_expire.append((tok, info["model"]))

            for tok, route in to_expire:
                client_sessions.pop(tok, None)
                model_tokens[route].discard(tok)
                print(f"[TOKEN REAPER] expired token={tok} route={route}")
                if len(model_tokens[route]) == 0:
                    to_check_cancel.append(route)

        for route in to_check_cancel:
            schedule_cancel_if_last(route)

        time.sleep(30)

def monitor_existing_job(route_label, job_id, stop_event):
    up = resolve_upstream(route_label)
    up_model = up["up_model"]

    submit_ts = time.time()
    reason_anchor = None
    reason_since = submit_ts
    consecutive_misses = 0

    with sched_lock:
        scheduling_status[route_label].update({
            "job_id": job_id,
            "phase": "pending",
            "since": submit_ts,
            "reason": None,
        })
        scheduling_status[route_label]["tried"].append(["existing", f"attach:{job_id}"])

    while True:
        if stop_event.is_set():
            log.info("MONITOR %s stop requested; leaving job %s untouched", route_label, job_id)
            with sched_lock:
                scheduling_status[route_label]["tried"].append(["existing", f"stopped_no_cancel:{job_id}"])
            return "stopped"

        info = get_job_info(job_id)
        if not info:
            consecutive_misses += 1
            if consecutive_misses <= 3:
                time.sleep(3)
                continue
            with sched_lock:
                scheduling_status[route_label]["tried"].append(["existing", f"lost_or_done:{job_id}:None"])
            return "lost"
        consecutive_misses = 0

        short = info["short"] or ""
        reason = (info["reason"] or "").strip()
        now_ts = time.time()

        if short in RUN_STATES:
            with sched_lock:
                running_jobs[up_model] = {"job_id": job_id, "node": info.get("nodelist") or ""}
                scheduling_jobs[route_label] = False
                scheduling_status[route_label].update({"phase": "running", "since": time.time()})
                scheduling_status[route_label]["tried"].append(["existing", f"running:{job_id}"])
            log.info("MONITOR %s existing job RUNNING %s", route_label, job_id)
            return "running"

        if short in TERMINAL_STATES:
            with sched_lock:
                scheduling_status[route_label]["tried"].append(["existing", f"terminal:{job_id}:{short}:{reason}"])
            return "terminal"

        if short not in PENDING_STATES:
            with sched_lock:
                scheduling_status[route_label].update({"phase": "pending", "reason": reason})
            time.sleep(5)
            continue

        if reason != reason_anchor:
            reason_anchor = reason
            reason_since = now_ts
        elapsed_reason = int(now_ts - reason_since)
        elapsed_total = int(now_ts - submit_ts)

        with sched_lock:
            scheduling_status[route_label].update({
                "phase": "pending",
                "reason": reason,
                "elapsed_pd": elapsed_total,
            })

        if elapsed_total < SAFE_NO_CANCEL_WINDOW:
            time.sleep(5)
            continue

        do_rotate = False
        if elapsed_reason >= PENDING_ROTATE_AT and should_switch_node(reason):
            do_rotate = True
        if not do_rotate and elapsed_total >= PENDING_HARD_ROTATE_AT and not reason_matches_any(reason, DO_NOT_SWITCH_REASONS):
            do_rotate = True

        with sched_lock:
            users_waiting = len(model_tokens[route_label])
        if do_rotate and users_waiting >= MIN_USERS_TO_SUPPRESS_ROTATION and not reason_matches_any(reason, NODE_SWITCH_REASONS):
            do_rotate = False

        if do_rotate:
            why_msg = f"reason='{reason}' elapsed_reason={elapsed_reason}s total={elapsed_total}s"
            if ROTATION_CANCEL_ENABLED:
                log.info("MONITOR %s rotating (cancel) from existing %s: %s", route_label, job_id, why_msg)
                audit_cancel(up_model, job_id, "rotation_pending")
                scancel(job_id)
                with sched_lock:
                    scheduling_status[route_label]["tried"].append(["existing", f"cancelled_after_wait:{job_id}:{reason}"])
                    scheduling_status[route_label].update({"phase": "switching", "since": time.time()})
                return "rotate"
            else:
                log.info("MONITOR %s rotation (non-cancelling) from existing %s: %s", route_label, job_id, why_msg)
                with sched_lock:
                    scheduling_status[route_label]["tried"].append(["existing", f"skipped_cancel:{job_id}:{reason}"])
                    scheduling_status[route_label].update({"phase": "switching", "since": time.time()})
                return "rotate"

        time.sleep(5)

def monitor_model_launch(route_label, initial_job_id=None):
    up = resolve_upstream(route_label)
    up_model = up["up_model"]

    stop_event = monitor_stops[route_label]
    rotations = 0

    try:
        if initial_job_id:
            outcome = monitor_existing_job(route_label, initial_job_id, stop_event)
            if outcome in ("running", "stopped"):
                return

        for node in NODE_ORDER:
            if stop_event.is_set():
                log.info("MONITOR %s stop requested before submitting to %s", route_label, node)
                return

            with sched_lock:
                if running_jobs.get(up_model):
                    log.info("MONITOR %s upstream already running, exit monitor", route_label)
                    return
                scheduling_status[route_label].update({
                    "node": node,
                    "job_id": None,
                    "phase": "submitting",
                    "since": time.time(),
                    "reason": None,
                    "elapsed_pd": 0,
                    "req_node": f"dgxa100-ncit-{node}",
                    "rotations": rotations,
                })

            already = pick_preferred_job(list_jobs_by_name(route_label))
            if already:
                log.info("MONITOR %s found existing job %s (%s), attaching", route_label, already['job_id'], already['short'])
                outcome = monitor_existing_job(route_label, already["job_id"], stop_event)
                if outcome in ("running", "stopped"):
                    return

            job_id = submit_job_to_node(route_label, node)
            if not job_id:
                with sched_lock:
                    scheduling_status[route_label]["tried"].append([node, "submit_failed"])
                continue

            with sched_lock:
                scheduling_status[route_label].update({
                    "job_id": job_id,
                    "phase": "pending",
                    "since": time.time(),
                    "reason": None,
                })
                scheduling_status[route_label]["tried"].append([node, f"submitted:{job_id}"])

            submit_ts = time.time()
            reason_anchor = None
            reason_since = submit_ts
            consecutive_misses = 0
            rotated = False

            while True:
                if stop_event.is_set():
                    log.info("MONITOR %s stop requested; leaving job %s untouched", route_label, job_id)
                    with sched_lock:
                        scheduling_status[route_label]["tried"].append([node, f"stopped_no_cancel:{job_id}"])
                    return

                info = get_job_info(job_id)
                if not info:
                    consecutive_misses += 1
                    if consecutive_misses <= 3:
                        time.sleep(3)
                        continue
                    with sched_lock:
                        scheduling_status[route_label]["tried"].append([node, f"lost_or_done:{job_id}:None"])
                    break
                consecutive_misses = 0

                short = info["short"] or ""
                reason = (info["reason"] or "").strip()
                now_ts = time.time()

                if short in RUN_STATES:
                    with sched_lock:
                        running_jobs[up_model] = {"job_id": job_id, "node": node}
                        scheduling_jobs[route_label] = False
                        scheduling_status[route_label].update({"phase": "running", "since": time.time()})
                        scheduling_status[route_label]["tried"].append([node, f"running:{job_id}"])
                    log.info("MONITOR %s RUNNING on %s, job %s", route_label, node, job_id)
                    return

                if short in TERMINAL_STATES:
                    with sched_lock:
                        scheduling_status[route_label]["tried"].append([node, f"terminal:{job_id}:{short}:{reason}"])
                    break

                if short not in PENDING_STATES:
                    with sched_lock:
                        scheduling_status[route_label].update({"phase": "pending", "reason": reason})
                    time.sleep(5)
                    continue

                if reason != reason_anchor:
                    reason_anchor = reason
                    reason_since = now_ts
                elapsed_reason = int(now_ts - reason_since)
                elapsed_total = int(now_ts - submit_ts)

                with sched_lock:
                    scheduling_status[route_label].update({
                        "phase": "pending",
                        "reason": reason,
                        "elapsed_pd": elapsed_total,
                        "rotations": rotations,
                    })

                if elapsed_total < SAFE_NO_CANCEL_WINDOW:
                    time.sleep(5)
                    continue

                do_rotate = False
                if elapsed_reason >= PENDING_ROTATE_AT and should_switch_node(reason):
                    do_rotate = True
                if not do_rotate and elapsed_total >= PENDING_HARD_ROTATE_AT and not reason_matches_any(reason, DO_NOT_SWITCH_REASONS):
                    do_rotate = True

                with sched_lock:
                    users_waiting = len(model_tokens[route_label])
                if do_rotate and users_waiting >= MIN_USERS_TO_SUPPRESS_ROTATION and not reason_matches_any(reason, NODE_SWITCH_REASONS):
                    log.info("MONITOR %s rotation suppressed users_waiting=%s reason=%s", route_label, users_waiting, reason)
                    do_rotate = False

                if do_rotate:
                    why_msg = f"node={node} job={job_id} reason='{reason}' elapsed_reason={elapsed_reason}s total={elapsed_total}s"
                    rotations += 1
                    if ROTATION_CANCEL_ENABLED:
                        log.info("MONITOR %s rotating (cancel) %s", route_label, why_msg)
                        audit_cancel(up_model, job_id, "rotation_pending")
                        scancel(job_id)
                        with sched_lock:
                            scheduling_status[route_label]["tried"].append([node, f"cancelled_after_wait:{job_id}:{reason}"])
                    else:
                        log.info("MONITOR %s rotating (non-cancelling) %s", route_label, why_msg)
                        with sched_lock:
                            scheduling_status[route_label]["tried"].append([node, f"skipped_cancel:{job_id}:{reason}"])

                    with sched_lock:
                        scheduling_status[route_label].update({"phase": "switching", "since": time.time(), "rotations": rotations})
                    rotated = True
                    break

                time.sleep(5)

            if rotated:
                continue

        with sched_lock:
            scheduling_jobs[route_label] = False
            scheduling_status[route_label].update({"phase": "exhausted", "node": None, "job_id": None, "since": time.time()})
        log.warning("MONITOR %s exhausted nodes; still not running", route_label)

    except Exception as e:
        with sched_lock:
            scheduling_jobs[route_label] = False
            scheduling_status[route_label].update({"phase": "error", "since": time.time(), "reason": str(e)})
        log.exception("MONITOR %s ERROR: %s", route_label, e)

@app.errorhandler(429)
def ratelimit_handler(e: HTTPException):
    resp = jsonify({"error": "Too many requests", "detail": getattr(e, "description", "rate limit exceeded")})
    retry = getattr(e, "retry_after", None)
    if retry is not None:
        resp.headers["Retry-After"] = str(retry)
    return resp, 429

def token_or_ip():
    try:
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            tok = data.get("token")
        else:
            tok = request.args.get("token")
    except Exception:
        tok = None
    return tok or get_remote_address()

@app.before_request
def _dbg():
    try:
        log.debug("SESSION %s", dict(session))
    except Exception:
        log.debug("SESSION <unavailable>")

    log.info("REQ %s %s ip=%s ua=%s", request.method, request.path, request.remote_addr, request.headers.get("User-Agent"))

@app.errorhandler(Exception)
def _unhandled_error(ex):
    if isinstance(ex, HTTPException):
        log.warning("HTTP %s on %s: %s", ex.code, request.path, ex.description)
        return ex
    log.exception("Unhandled exception on %s", request.path)
    return jsonify({"error": "Internal server error"}), 500

@app.route("/health", methods=["GET"])
@limiter.limit("600/minute", key_func=get_remote_address)
def health():
    return jsonify({
        "status": "ok",
        "active_model": session.get("model"),
        "active_url": session.get("url"),
    })

@app.route("/progress/<route>", methods=["GET"])
@limiter.limit("600/minute", key_func=get_remote_address)
@limiter.limit("500/minute", key_func=token_or_ip)
def progress(route):
    if route not in UPSTREAM:
        return jsonify({"error": "Invalid model"}), 400

    tok = request.args.get("token")
    with sched_lock:
        if tok and tok in client_sessions and client_sessions[tok]["model"] == route:
            client_sessions[tok]["last_seen"] = now()

    up = resolve_upstream(route)
    up_model = up["up_model"]

    with sched_lock:
        prog = dict(scheduling_status[route])
        running = running_jobs.get(up_model)
        users = len(model_tokens[route])
        sched_in_progress = scheduling_jobs[route]

    if prog.get("since"):
        try:
            prog["elapsed"] = round(now() - float(prog["since"]), 1)
        except Exception:
            prog["elapsed"] = None

    health_obj = None
    if running:
        try:
            hc = requests.get(healthcheck_url_for(route), timeout=(0.5, 0.8))
            if hc.ok:
                health_obj = hc.json()
        except Exception as e:
            health_obj = {"status": "unreachable", "error": str(e)}

    if running and health_obj and health_obj.get("status") == "running":
        high = "ready"
    elif sched_in_progress:
        high = "scheduling"
    elif running:
        high = "launching"
    else:
        high = "idle"

    remaining_idle = None
    with sched_lock:
        if running:
            last_used = last_query_time.get(up_model, now())
            remaining_idle = max(0, IDLE_TIMEOUT_SECONDS - int(now() - last_used))

    return jsonify({
        "high_status": high,
        "progress": prog,
        "running": running,
        "users": users,
        "health": health_obj,
        "idle_seconds_remaining": remaining_idle,
    })

@app.route("/status/<job_id>", methods=["GET"])
@limiter.limit("700/minute", key_func=get_remote_address)
@limiter.limit("500/minute", key_func=token_or_ip)
def status(job_id):
    token = request.args.get("token")

    route = None
    up_model = None
    port = None

    if token and token in client_sessions:
        route = client_sessions[token]["model"]
        up = resolve_upstream(route)
        up_model = up["up_model"]
        port = up["port"]
    else:
        with sched_lock:
            for umodel, info in running_jobs.items():
                if info.get("job_id") == job_id:
                    up_model = umodel
                    for r, cfg in UPSTREAM.items():
                        if cfg["up_model"] == up_model:
                            route = r
                            port = cfg["port"]
                            break
                    break

    if not port:
        return jsonify({"status": "loading"})

    try:
        res = requests.get(f"http://host.docker.internal:{port}/health", timeout=(0.5, 0.8))
        if res.ok and res.json().get("status") == "running":
            return jsonify({"status": "ready"})
    except requests.exceptions.ConnectionError:
        pass
    except Exception as e:
        return jsonify({"status": "loading", "error": str(e), "active_port": port})

    return jsonify({"status": "loading", "active_port": port})

@app.route("/select_model", methods=["POST"])
@limiter.limit("40/minute", key_func=get_remote_address)
@limiter.limit("24/minute; 9/10second", key_func=token_or_ip)
def select_model():
    data = (request.get_json() or {})
    route = data.get("model")
    token = data.get("token") or str(uuid.uuid4())

    if route not in UPSTREAM:
        return jsonify({"error": "Invalid model"}), 400

    up = resolve_upstream(route)
    up_model = up["up_model"]
    port = up["port"]
    log.info("SELECT route=%s up_model=%s port=%s token=%s", route, up_model, port, token)

    need_cancel_old = None

    now_ts = time.time()
    with sched_lock:
        prev = client_sessions.get(token)
        if not prev:
            client_sessions[token] = {"model": route, "last_seen": now_ts}
            model_tokens[route].add(token)
        elif prev["model"] != route:
            old = prev["model"]
            model_tokens[old].discard(token)
            client_sessions[token] = {"model": route, "last_seen": now_ts}
            model_tokens[route].add(token)
            if len(model_tokens[old]) == 0:
                need_cancel_old = old
        else:
            client_sessions[token]["last_seen"] = now_ts

        running = running_jobs.get(up_model)

    if need_cancel_old is not None:
        schedule_cancel_if_last(need_cancel_old)

    abort_scheduled_cancel(route)
    if running:
        job_id = running["job_id"]
        log.info("SELECT running found up_model=%s job_id=%s node=%s", up_model, job_id, running.get("node"))
        ready = False
        try:
            log.info("HEALTH probe route=%s url=%s", route, healthcheck_url_for(route))
            hc = requests.get(healthcheck_url_for(route), timeout=(0.5, 0.8))
            ready = hc.ok and hc.json().get("status") == "running"
            log.info("HEALTH result ok=%s body=%s", hc.ok, (hc.text[:200] if hasattr(hc, 'text') else hc))
        except Exception as e:
            log.warning("HEALTH probe error route=%s err=%r", route, e)

        return jsonify({
            "status": "ready" if ready else "launching",
            "model": route,
            "port": port,
            "job_id": job_id,
            "node": running["node"],
            "token": token,
            "note": "reusing running job" if ready else "waiting for health",
        })

    with sched_lock:
        existing_monitor = monitor_threads.get(route)
        if scheduling_jobs[route] and existing_monitor and existing_monitor.is_alive():
            progress = dict(scheduling_status[route])
            return jsonify({
                "status": "waiting",
                "message": "Scheduling in progress. Waiting for job to start running...",
                "model": route,
                "job_id": progress.get("job_id"),
                "progress": progress,
                "token": token,
            })

        attach = pick_preferred_job(list_jobs_by_name(route))
        scheduling_jobs[route] = True
        stop_evt = threading.Event()
        monitor_stops[route] = stop_evt
        t = threading.Thread(
            target=monitor_model_launch,
            args=(route, attach["job_id"] if attach else None),
            daemon=True
        )
        monitor_threads[route] = t
        t.start()

        scheduling_status[route].update({
            "node": attach.get("node") if attach else None,
            "job_id": attach["job_id"] if attach else None,
            "phase": "pending" if attach and attach["short"] != "R" else ("running" if attach else "submitting"),
            "since": time.time(),
            "tried": scheduling_status[route].get("tried", []),
        })

    with sched_lock:
        progress = dict(scheduling_status[route])

    return jsonify({
        "status": "waiting" if not attach or attach["short"] != "R" else "launching",
        "message": "Trying nodes in order; may rotate if pending persists...",
        "model": route,
        "job_id": attach["job_id"] if attach else None,
        "progress": progress,
        "token": token,
    })

@app.route("/chat", methods=["POST"])
@limiter.limit("60/minute", key_func=get_remote_address)
@limiter.limit("30/minute; 12/10second", key_func=token_or_ip)
def chat():
    data = request.get_json() or {}
    token = data.get("token")
    if not token or token not in client_sessions:
        return jsonify({"error": "Missing or unknown token"}), 400

    route = client_sessions[token]["model"]
    up = resolve_upstream(route)
    up_model = up["up_model"]
    port = up["port"]
    url = f"http://host.docker.internal:{port}"

    with sched_lock:
        if not running_jobs.get(up_model):
            return jsonify({"error": "Model not ready yet"}), 409

    prompt = (data.get("message") or "").strip()
    if not prompt:
        return jsonify({"error": "Empty message"}), 400

    last_query_time[up_model] = time.time()
    with sched_lock:
        if token in client_sessions:
            client_sessions[token]["last_seen"] = time.time()
    log.info("CHAT start route=%s up_model=%s port=%s token=%s", route, up_model, port, token)
    with MODEL_LOCKS[up_model]:
        try:
            payload = {
                "text": prompt,
                "token": token,
                "client_ip": request.remote_addr,
                "route": route,
                "adapter": route,
                "task": route,
            }
            log.debug("CHAT route=%s token=%s forwarding_to=%s", route, token, url)
            res = requests.post(f"{url}/query", json=payload, timeout=400)
            if res.ok:
                return jsonify(res.json())
            else:
                log.warning("CHAT upstream error status=%s body=%s", res.status_code, res.text[:500])
                return jsonify({"error": "Failed to query model", "detail": res.text}), 502
        except Exception as e:
            log.exception("CHAT request failed route=%s token=%s", route, token)
            return jsonify({"error": f"Request failed: {str(e)}"}), 500

@app.route("/cancel_model", methods=["POST"])
def cancel_model():
    log.info("CANCEL endpoint hit")
    data = request.get_json(silent=True) or {}
    tok = data.get("token")

    route = None
    with sched_lock:
        if tok:
            entry = client_sessions.pop(tok, None)
            if entry:
                route = entry["model"]
                model_tokens[route].discard(tok)
        else:
            m = session.get("model")
            if m:
                route = m

    session.pop("job_id", None)
    session.pop("url", None)
    session.pop("port", None)
    session.pop("model", None)
    session.modified = True

    if not route:
        return jsonify({"status": "no active session"})

    schedule_cancel_if_last(route)
    with sched_lock:
        users_left = len(model_tokens[route])
    log.info("CANCEL requested token=%s route=%s users_left=%s scheduled in %ss if upstream unused", tok, route, users_left, CANCEL_GRACE_SECONDS)

    return jsonify({
        "status": "scheduled",
        "grace_seconds": CANCEL_GRACE_SECONDS,
        "users_left": users_left,
    })

PHASE_ORDER = ["idle", "submitting", "switching", "pending", "running"]
PHASE_TEXT = {
    "idle": "Waiting to schedule…",
    "submitting": "Submitting job to cluster…",
    "switching": "Trying another node…",
    "pending": "Waiting in queue…",
    "running": "Model process starting…",
}

def _estimate_percent(phase: str, ready: bool) -> int:
    if ready:
        return 100
    if phase not in PHASE_ORDER:
        return 5
    idx = PHASE_ORDER.index(phase)
    return max(1, min(95, round(idx * (100/(len(PHASE_ORDER))))))

@app.route("/progress_ui/<route>", methods=["GET"])
@limiter.limit("600/minute", key_func=get_remote_address)
@limiter.limit("500/minute", key_func=token_or_ip)
def progress_ui(route):
    if route not in UPSTREAM:
        return jsonify({"error": "Invalid model"}), 400

    tok = request.args.get("token")
    with sched_lock:
        if tok and tok in client_sessions and client_sessions[tok]["model"] == route:
            client_sessions[tok]["last_seen"] = now()

        prog = dict(scheduling_status[route])
        up_model = resolve_upstream(route)["up_model"]
        running = running_jobs.get(up_model)
        users = len(model_tokens[route])
        sched_in_progress = scheduling_jobs[route]

    elapsed = None
    if prog.get("since"):
        try:
            elapsed = round(now() - float(prog["since"]), 1)
        except Exception:
            pass

    health_obj = None
    ready = False
    if running:
        try:
            hc = requests.get(healthcheck_url_for(route), timeout=(0.5, 0.8))
            if hc.ok:
                health_obj = hc.json()
                ready = health_obj.get("status") == "running"
        except Exception as e:
            health_obj = {"status": "unreachable", "error": str(e)}

    if ready:
        phase = "running"
    elif sched_in_progress:
        phase = prog.get("phase") or "submitting"
    elif running:
        phase = "running"
    else:
        phase = "idle"

    percent = _estimate_percent(phase, ready)
    text = PHASE_TEXT.get(phase, phase)

    return jsonify({
        "ready": ready,
        "phase": phase,
        "phase_text": text,
        "percent": percent,
        "elapsed": elapsed,
        "running": running,
        "progress": prog,
        "users": users,
        "health": health_obj,
    })

@app.route("/route", methods=["POST"])
@limiter.limit("120/minute", key_func=get_remote_address)
def route_query():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Empty text"}), 400

    result = route_rules_only(text)
    out = {
        "route": result["route"],
        "confidence": result["confidence"],
        "reason": result["reason"],
        "features": {}
    }
    return jsonify(out)

@app.route("/answer", methods=["POST"])
@limiter.limit("90/minute", key_func=get_remote_address)
def answer_once():
    data  = request.get_json(silent=True) or {}
    text  = (data.get("text") or "").strip()
    route = (data.get("route") or "").strip()
    token = (data.get("token") or str(uuid.uuid4())).strip()

    if route not in ROUTE_LABELS:
        return jsonify({"error": "Invalid route"}), 400
    if not text:
        return jsonify({"error": "Empty text"}), 400

    if route == "search":
        return jsonify({
            "status": "ready",
            "source": "search",
            "panel": {"kind": "search", "query": text, "snippets": []}
        })

    with app.test_request_context(json={"model": route, "token": token}):
        sel_resp = select_model().get_json()

    status = sel_resp.get("status")
    job_id = sel_resp.get("job_id")

    if status in ("waiting", "launching"):
        with app.test_request_context(query_string={"token": token}):
            prog = progress_ui(route).get_json()

        return jsonify({
            "status": "loading",
            "source": route,
            "progress": {
                "phase": (prog.get("phase") or "pending"),
                "percent": prog.get("percent"),
                "node": (prog.get("running") or {}).get("node")
                        or (prog.get("progress") or {}).get("node"),
                "job_id": (prog.get("running") or {}).get("job_id") or job_id,
            },
            "token": token
        }), 202

    with app.test_request_context(json={"token": token, "message": text}):
        chat_resp = chat().get_json()

    return jsonify({
        "status": "ready",
        "source": route,
        "panel": {"kind": route, "text": chat_resp.get("response") or "[No response]"},
        "token": token,
        "job_id": job_id
    })

if __name__ == "__main__":
    threading.Thread(target=token_reaper, daemon=True).start()
    port = int(os.getenv("DUAL_BACKEND_PORT", "8010"))
    log.info("Starting server on 0.0.0.0:%s", port)
    app.run(host="0.0.0.0", port=port, threaded=True)
