"""
Polymarket Whale Monitor — Public Web Dashboard
Public read access. Auto-refreshes every AUTO_REFRESH_MINUTES.
"""
import os
import re
import json
import time
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request, abort
from dotenv import load_dotenv

from polymarket_api import build_whale_profiles
from analyzer import analyze_whales
import demo_tracker

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32))

REPORTS_DIR = Path(__file__).parent / "reports"
REPORTS_DIR.mkdir(exist_ok=True)
WHALE_COUNT         = int(os.getenv("WHALE_COUNT", 40))
RUN_COOLDOWN        = int(os.getenv("RUN_COOLDOWN_SECONDS", 0))
AUTO_REFRESH_SECS   = int(os.getenv("AUTO_REFRESH_SECONDS", 30))

_last_run_time: float = 0
_run_lock   = threading.Lock()
_new_bets: list[dict] = []   # bets detected since previous run

# ── Job state ──────────────────────────────────────────────────────────────────

_job = {"running": False, "error": None, "last_finished": None}


def _prev_tx_hashes() -> set[str]:
    """Collect all tx_hashes from the second-most-recent raw report."""
    raws = sorted(REPORTS_DIR.glob("*_raw.json"), reverse=True)
    if len(raws) < 2:
        return set()
    prev = json.loads(raws[1].read_text())
    hashes = set()
    for p in prev:
        for t in p.get("recent_trades", []):
            h = t.get("tx_hash", "")
            if h:
                hashes.add(h)
    return hashes


def _run_analysis():
    global _new_bets
    _job["running"] = True
    _job["error"]   = None
    try:
        prev_hashes = _prev_tx_hashes()

        profiles = build_whale_profiles(top_n=WHALE_COUNT)
        if not profiles:
            _job["error"] = "No data returned from Polymarket API."
            return
        report = analyze_whales(profiles)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
        (REPORTS_DIR / f"{ts}_report.json").write_text(json.dumps(report, indent=2))
        (REPORTS_DIR / f"{ts}_raw.json").write_text(json.dumps(profiles, indent=2))
        _job["last_finished"] = ts

        # Detect new bets vs previous run
        found = []
        for p in profiles:
            for t in p.get("recent_trades", []):
                if t.get("tx_hash") and t["tx_hash"] not in prev_hashes:
                    found.append({
                        "whale":     p["name"],
                        "rank":      p["rank"],
                        "pnl_30d":   p.get("pnl_30d", 0),
                        "market":    t["market"],
                        "side":      t["side"],
                        "outcome":   t["outcome"],
                        "amount":    t["amount"],
                        "price":     t["price"],
                        "timestamp": t["timestamp"],
                        "tx_hash":   t.get("tx_hash", ""),
                    })
        found.sort(key=lambda x: x["timestamp"], reverse=True)
        _new_bets = found[:50]

        # Update demo portfolio
        try:
            demo_tracker.on_analysis_complete(_new_bets, profiles)
        except Exception as de:
            print(f"[DEMO TRACKER] {de}")

    except Exception as e:
        import traceback; traceback.print_exc()
        _job["error"] = str(e)
    finally:
        _job["running"] = False


def _auto_refresh_loop():
    """Background thread: re-run analysis every AUTO_REFRESH_SECS seconds."""
    while True:
        time.sleep(AUTO_REFRESH_SECS)
        with _run_lock:
            if _job["running"]:
                continue
        threading.Thread(target=_run_analysis, daemon=True).start()


# ── Safe report ID validation (prevent path traversal) ────────────────────────

_SAFE_ID = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}$")   # e.g. 2026-05-17_21-00

def _safe_report_id(report_id: str) -> bool:
    return bool(_SAFE_ID.match(report_id))


# ── Helpers ────────────────────────────────────────────────────────────────────

def _list_reports():
    return [
        {"id":    p.stem.replace("_report", ""),
         "label": p.stem.replace("_report", "").replace("_", " ")}
        for p in sorted(REPORTS_DIR.glob("*_report.json"), reverse=True)
    ]

def _load_report(rid):
    if not _safe_report_id(rid):
        return {}
    p = REPORTS_DIR / f"{rid}_report.json"
    return json.loads(p.read_text()) if p.exists() else {}

def _load_profiles(rid):
    if not _safe_report_id(rid):
        return []
    p = REPORTS_DIR / f"{rid}_raw.json"
    return json.loads(p.read_text()) if p.exists() else []


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    reports   = _list_reports()
    latest_id = reports[0]["id"] if reports else None
    report    = _load_report(latest_id) if latest_id else {}
    profiles  = _load_profiles(latest_id) if latest_id else []
    return render_template("index.html",
        reports=reports, active_id=latest_id,
        report=report, profiles=profiles, job=_job,
        run_cooldown=RUN_COOLDOWN)


@app.get("/report/<report_id>")
def view_report(report_id):
    if not _safe_report_id(report_id):
        abort(404)
    reports  = _list_reports()
    report   = _load_report(report_id)
    profiles = _load_profiles(report_id)
    return render_template("index.html",
        reports=reports, active_id=report_id,
        report=report, profiles=profiles, job=_job,
        run_cooldown=RUN_COOLDOWN)


@app.post("/api/run")
def api_run():
    global _last_run_time
    with _run_lock:
        if _job["running"]:
            return jsonify({"status": "already_running"}), 409
        elapsed = time.time() - _last_run_time
        if elapsed < RUN_COOLDOWN:
            wait = int(RUN_COOLDOWN - elapsed)
            return jsonify({"status": "cooldown", "wait_seconds": wait}), 429
        _last_run_time = time.time()

    threading.Thread(target=_run_analysis, daemon=True).start()
    return jsonify({"status": "started"})


@app.get("/api/status")
def api_status():
    elapsed = time.time() - _last_run_time
    cooldown_remaining = max(0, int(RUN_COOLDOWN - elapsed))
    return jsonify({
        "running":            _job["running"],
        "error":              _job["error"],
        "last_finished":      _job["last_finished"],
        "cooldown_remaining": cooldown_remaining,
        "new_bet_count":      len(_new_bets),
    })


@app.get("/api/new-bets")
def api_new_bets():
    return jsonify(_new_bets)


@app.get("/demo")
def demo_page():
    return render_template("demo.html", job=_job)


@app.get("/api/demo")
def api_demo():
    return jsonify(demo_tracker.get_demo_data())


@app.post("/api/demo/reset")
def api_demo_reset():
    demo_tracker.reset_demo()
    return jsonify({"status": "reset"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5050))
    print(f"Dashboard → http://localhost:{port}")
    threading.Thread(target=_auto_refresh_loop, daemon=True).start()
    # debug=False is critical — never expose Flask debugger publicly
    app.run(host="0.0.0.0", debug=False, port=port, use_reloader=False)
