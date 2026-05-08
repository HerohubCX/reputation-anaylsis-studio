"""
HeroHub Reputation Pipeline — Flask Application
Run with:  python app.py
Then open: http://localhost:5000
"""

import os
import json
import csv
import io
import sqlite3
import logging
import threading
from datetime import datetime, date, timedelta
from dateutil.relativedelta import relativedelta
from pathlib import Path

from flask import (Flask, render_template, request, jsonify, redirect,
                   url_for, send_from_directory, flash)
from flask_apscheduler import APScheduler

# ── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
DB_FILE     = BASE_DIR / "pipeline.db"
OUTPUT_DIR  = BASE_DIR / "output" / "dashboards"
CACHE_DIR   = BASE_DIR / "data" / "review_cache"
BG_FILE     = BASE_DIR / "static" / "bg_image.txt"

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ── Flask app ──────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder='.')
app.secret_key = os.urandom(24)

scheduler = APScheduler()

# In-memory job status tracker  {job_id: {status, message, progress}}
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

# Set of run_ids that have been requested to stop
cancelled_jobs: set[str] = set()
cancelled_lock = threading.Lock()


def _is_cancelled(job_id: str) -> bool:
    with cancelled_lock:
        return job_id in cancelled_jobs


def _mark_cancelled(job_id: str):
    with cancelled_lock:
        cancelled_jobs.add(job_id)


def _clear_cancelled(job_id: str):
    with cancelled_lock:
        cancelled_jobs.discard(job_id)


# ── Config helpers ─────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

def get_bg_image() -> str:
    if BG_FILE.exists():
        return BG_FILE.read_text(encoding="utf-8").strip()
    return "linear-gradient(135deg,#0d1b2e,#1a2f4a)"  # fallback


# ── Database ───────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS dealers (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                place_id    TEXT NOT NULL UNIQUE,
                created_at  TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS runs (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                dealer_id         INTEGER REFERENCES dealers(id),
                dealer_name       TEXT,
                status            TEXT DEFAULT 'pending',
                started_at        TEXT,
                finished_at       TEXT,
                dashboard         TEXT,
                error             TEXT,
                date_range_label  TEXT
            );
        """)
        # Migrate existing DBs that don't yet have the date_range_label column
        try:
            db.execute("ALTER TABLE runs ADD COLUMN date_range_label TEXT")
        except Exception:
            pass  # Column already exists
        # Migrate: add location column to dealers
        try:
            db.execute("ALTER TABLE dealers ADD COLUMN location TEXT DEFAULT ''")
        except Exception:
            pass  # Column already exists


# ── Review cache helpers ───────────────────────────────────────────────────

def save_review_cache(dealer_id: int, dealer_name: str, place_id: str,
                      reviews: list[dict]):
    """Persist raw reviews to disk so subsequent runs can skip Apify."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{dealer_id}.json"
    payload = {
        "dealer_id":    dealer_id,
        "dealer_name":  dealer_name,
        "place_id":     place_id,
        "fetched_at":   datetime.now().isoformat(),
        "review_count": len(reviews),
        "reviews":      reviews,
    }
    cache_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    logger.info(f"[Cache] Saved {len(reviews)} reviews for dealer {dealer_id}")


def load_review_cache(dealer_id: int) -> dict | None:
    """Return cached review payload or None if no cache exists."""
    cache_file = CACHE_DIR / f"{dealer_id}.json"
    if not cache_file.exists():
        return None
    try:
        return json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        return None


def get_cache_meta(dealer_id: int) -> dict | None:
    """Return lightweight cache metadata (no review list) for UI display."""
    data = load_review_cache(dealer_id)
    if not data:
        return None
    return {
        "fetched_at":   data.get("fetched_at", ""),
        "review_count": data.get("review_count", 0),
    }


# ── Date range helpers ─────────────────────────────────────────────────────

def _parse_review_date(date_str: str):
    """Return a date object from an ISO-ish or relative date string, or None."""
    if not date_str or not isinstance(date_str, str):
        return None
    s = date_str.strip()

    # Try common exact formats against the full string
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%d/%m/%Y",
        "%B %d, %Y",
        "%b %d, %Y",
    ):
        try:
            return datetime.strptime(s, fmt).date()
        except (ValueError, TypeError):
            continue

    # Fallback: let dateutil handle anything exotic (e.g. "March 4, 2023")
    try:
        from dateutil import parser as du_parser
        return du_parser.parse(s, dayfirst=False).date()
    except Exception:
        pass

    # Last resort: handle Apify-style relative strings ("2 years ago", "a month ago", etc.)
    today = date.today()
    sl = s.lower()
    try:
        if "year" in sl:
            n = 1 if sl.startswith("a ") or sl.startswith("an ") else int(sl.split()[0])
            return today - relativedelta(years=n)
        if "month" in sl:
            n = 1 if sl.startswith("a ") or sl.startswith("an ") else int(sl.split()[0])
            return today - relativedelta(months=n)
        if "week" in sl:
            n = 1 if sl.startswith("a ") or sl.startswith("an ") else int(sl.split()[0])
            return today - timedelta(weeks=n)
        if "day" in sl:
            n = 1 if sl.startswith("a ") or sl.startswith("an ") else int(sl.split()[0])
            return today - timedelta(days=n)
        if "hour" in sl or "minute" in sl or "just now" in sl:
            return today
    except Exception:
        pass

    return None


def filter_reviews_by_date(reviews: list[dict],
                           date_range: str,
                           from_date: str | None = None,
                           to_date: str | None   = None) -> tuple[list[dict], str]:
    """
    Filter a list of review dicts to those within the requested date range.
    Returns (filtered_reviews, human_readable_label).

    date_range values:
      'all'    — no filtering
      '1yr'    — last 1 year
      '2yr'    — last 2 years
      '3yr'    — last 3 years
      '5yr'    — last 5 years
      '10yr'   — last 10 years
      'custom' — use from_date / to_date (YYYY-MM-DD strings)
    """
    today = date.today()

    if date_range == "all" or not date_range:
        return reviews, "All Years"

    if date_range == "custom":
        try:
            start = datetime.strptime(from_date, "%Y-%m-%d").date() if from_date else None
            end   = datetime.strptime(to_date,   "%Y-%m-%d").date() if to_date   else today
        except (ValueError, TypeError):
            return reviews, "All Years"
        label = (f"{start.strftime('%b %d, %Y') if start else 'Beginning'} – "
                 f"{end.strftime('%b %d, %Y')}")
    else:
        years = int(date_range.replace("yr", ""))
        start = today - relativedelta(years=years)
        end   = today
        label = f"Last {years} Year{'s' if years > 1 else ''}"

    filtered = []
    for r in reviews:
        d = _parse_review_date(r.get("date", ""))
        if d is None:
            continue  # skip undated reviews when a filter is active
        if start and d < start:
            continue
        if d > end:
            continue
        filtered.append(r)

    logger.info(f"[DateFilter] {date_range!r}: {len(filtered)}/{len(reviews)} reviews kept "
                f"({label})")
    return filtered, label


# ── Background pipeline runner ─────────────────────────────────────────────

def _set_job(job_id: str, **kwargs):
    with jobs_lock:
        jobs.setdefault(job_id, {}).update(kwargs)

def run_pipeline_job(job_id: str, dealer_id: int, dealer_name: str,
                     place_id: str, cfg: dict,
                     location: str = "",
                     date_range: str = "all",
                     from_date: str | None = None,
                     to_date: str | None   = None,
                     hide_chrome: bool = False,
                     use_cached: bool = False):
    """Runs the full pipeline in a background thread."""
    from pipeline.apify     import scrape_reviews, reviews_to_csv
    from pipeline.analyzer  import analyze_reviews
    from pipeline.generator import generate_dashboard

    bg_url = get_bg_image()

    with get_db() as db:
        db.execute(
            "UPDATE runs SET status='running', started_at=? WHERE id=?",
            (datetime.now().isoformat(), job_id)
        )

    def _check_cancel():
        if _is_cancelled(job_id):
            raise InterruptedError("Run stopped by user.")

    try:
        _clear_cancelled(job_id)

        # ── Reviews: load from cache or scrape fresh ───────────────────────
        cached = load_review_cache(dealer_id) if use_cached else None

        if cached:
            reviews = cached["reviews"]
            fetched_at = cached.get("fetched_at", "")[:10]
            _set_job(job_id, status="running",
                     step=f"Using cached scrape from {fetched_at} ({len(reviews)} reviews)…",
                     progress=30)
            logger.info(f"[Pipeline] Using cached reviews for {dealer_name} ({len(reviews)} reviews)")
        else:
            _set_job(job_id, status="running", step="Scraping reviews from Google…", progress=10)
            reviews = scrape_reviews(
                place_id    = place_id,
                dealer_name = dealer_name,
                actor_id    = cfg["apify_actor_id"],
                api_token   = cfg["apify_token"],
                max_reviews = cfg.get("max_reviews_per_dealer", 500),
            )
            save_review_cache(dealer_id, dealer_name, place_id, reviews)

        _check_cancel()

        # ── Apply date filter ──────────────────────────────────────────────
        reviews, range_label = filter_reviews_by_date(
            reviews, date_range, from_date, to_date
        )
        if not reviews:
            raise ValueError(
                f"No reviews found in the selected date range ({range_label}). "
                "Try a wider range or 'All Years'."
            )

        _check_cancel()

        _set_job(job_id, step=f"Analyzing {len(reviews)} reviews with Claude… ({range_label})",
                 progress=55, date_range_label=range_label)

        reviews_csv = reviews_to_csv(reviews)

        analysis = analyze_reviews(
            dealer_name = dealer_name,
            reviews_csv = reviews_csv,
            api_key     = cfg["claude_api_key"],
        )

        _check_cancel()

        analysis["date_range_label"] = range_label
        analysis["location"] = location  # from dealer record, not Claude inference

        _set_job(job_id, step="Generating dashboard…", progress=85)

        dashboard_path = generate_dashboard(
            data        = analysis,
            bg_image_url= bg_url,
            output_dir  = str(OUTPUT_DIR),
            hide_chrome = hide_chrome,
        )
        dashboard_filename = Path(dashboard_path).name

        with get_db() as db:
            db.execute(
                "UPDATE runs SET status='done', finished_at=?, dashboard=?, "
                "date_range_label=? WHERE id=?",
                (datetime.now().isoformat(), dashboard_filename, range_label, job_id)
            )

        _set_job(job_id, status="done", step="Done!", progress=100,
                 dashboard=dashboard_filename, date_range_label=range_label)
        logger.info(f"[Pipeline] Done — {dealer_name} → {dashboard_filename} ({range_label})")

    except InterruptedError:
        logger.info(f"[Pipeline] Stopped by user — {dealer_name}")
        with get_db() as db:
            db.execute(
                "UPDATE runs SET status='stopped', finished_at=? WHERE id=?",
                (datetime.now().isoformat(), job_id)
            )
        _set_job(job_id, status="stopped", step="Stopped", progress=0)
        _clear_cancelled(job_id)

    except Exception as e:
        logger.error(f"[Pipeline] Failed for {dealer_name}: {e}", exc_info=True)
        with get_db() as db:
            db.execute(
                "UPDATE runs SET status='error', finished_at=?, error=? WHERE id=?",
                (datetime.now().isoformat(), str(e), job_id)
            )
        _set_job(job_id, status="error", step=f"Error: {e}", progress=0)
    finally:
        _clear_cancelled(job_id)


def _start_run(dealer_id: int, dealer_name: str, place_id: str,
               location: str = "",
               date_range: str = "all",
               from_date: str | None = None,
               to_date: str | None   = None,
               hide_chrome: bool = False,
               use_cached: bool = False) -> int:
    """Insert a run record and start the background thread. Returns run_id."""
    cfg = load_config()
    if not cfg.get("apify_token") or not cfg.get("claude_api_key"):
        raise ValueError("API keys are not configured. Go to Settings first.")

    with get_db() as db:
        cur = db.execute(
            "INSERT INTO runs (dealer_id, dealer_name, status, started_at) VALUES (?,?,?,?)",
            (dealer_id, dealer_name, "queued", datetime.now().isoformat())
        )
        run_id = cur.lastrowid

    _set_job(str(run_id), status="queued", step="Queued…", progress=0)

    t = threading.Thread(
        target=run_pipeline_job,
        args=(str(run_id), dealer_id, dealer_name, place_id, cfg),
        kwargs={"location": location, "date_range": date_range,
                "from_date": from_date, "to_date": to_date,
                "hide_chrome": hide_chrome, "use_cached": use_cached},
        daemon=True,
    )
    t.start()
    return run_id


# ── Scheduled job ──────────────────────────────────────────────────────────

def scheduled_run_all():
    logger.info("[Scheduler] Running scheduled pipeline for all dealers")
    with get_db() as db:
        dealers = db.execute("SELECT * FROM dealers").fetchall()
    for d in dealers:
        try:
            _start_run(d["id"], d["name"], d["place_id"])
        except Exception as e:
            logger.error(f"[Scheduler] Failed to start run for {d['name']}: {e}")


def apply_schedule(cfg: dict):
    """Add or remove the weekly job based on config."""
    job_id = "weekly_run"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    if cfg.get("schedule_enabled"):
        day  = cfg.get("schedule_day", "monday")
        hour = int(cfg.get("schedule_hour", 7))
        scheduler.add_job(
            id       = job_id,
            func     = scheduled_run_all,
            trigger  = "cron",
            day_of_week = day,
            hour     = hour,
            minute   = 0,
        )
        logger.info(f"[Scheduler] Scheduled weekly run on {day} at {hour:02d}:00")


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    with get_db() as db:
        dealers = db.execute("""
            SELECT d.*, r.status as last_status, r.dashboard as last_dashboard,
                   r.finished_at as last_run, r.error as last_error
            FROM dealers d
            LEFT JOIN runs r ON r.id = (
                SELECT id FROM runs WHERE dealer_id=d.id ORDER BY id DESC LIMIT 1
            )
            ORDER BY d.created_at DESC, d.id DESC
        """).fetchall()
        total_runs = db.execute("SELECT COUNT(*) FROM runs WHERE status='done'").fetchone()[0]
        total_dealers = db.execute("SELECT COUNT(*) FROM dealers").fetchone()[0]
        recent_runs = db.execute("""
            SELECT r.id, r.dealer_name, r.dashboard, r.date_range_label, r.finished_at
            FROM runs r
            WHERE r.status='done' AND r.dashboard IS NOT NULL
            ORDER BY r.finished_at DESC
            LIMIT 3
        """).fetchall()
        # Per-dealer recent runs (last 3 successful per dealer)
        all_dealer_runs = db.execute("""
            SELECT dealer_id, dashboard, date_range_label, finished_at,
                   ROW_NUMBER() OVER (PARTITION BY dealer_id ORDER BY finished_at DESC) as rn
            FROM runs
            WHERE status='done' AND dashboard IS NOT NULL
        """).fetchall()
        dealer_runs = {}
        for r in all_dealer_runs:
            if r['rn'] <= 3:
                dealer_runs.setdefault(r['dealer_id'], []).append(r)
    cfg = load_config()
    cache_meta = {d["id"]: get_cache_meta(d["id"]) for d in dealers}
    return render_template("index.html", dealers=dealers,
                           total_runs=total_runs, total_dealers=total_dealers,
                           config=cfg, cache_meta=cache_meta,
                           recent_runs=recent_runs, dealer_runs=dealer_runs)


@app.route("/dealers/add", methods=["POST"])
def add_dealer():
    name     = request.form.get("name", "").strip()
    place_id = request.form.get("place_id", "").strip()
    location = request.form.get("location", "").strip()
    if not name or not place_id:
        flash("Dealer name and Place ID are required.", "error")
        return redirect(url_for("index"))
    try:
        with get_db() as db:
            db.execute("INSERT INTO dealers (name, place_id, location) VALUES (?,?,?)",
                       (name, place_id, location))
        flash(f"'{name}' added successfully.", "success")
    except sqlite3.IntegrityError:
        flash(f"A dealer with that Place ID already exists.", "error")
    return redirect(url_for("index"))


@app.route("/dealers/upload", methods=["POST"])
def upload_dealers():
    f = request.files.get("csv_file")
    if not f:
        flash("No file selected.", "error")
        return redirect(url_for("index"))
    content = f.read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(content))
    added = 0
    skipped = 0
    for row in reader:
        name     = (row.get("name") or row.get("dealer_name") or "").strip()
        place_id = (row.get("place_id") or row.get("Place ID") or "").strip()
        location = (row.get("location") or row.get("Location")
                    or row.get("city") or row.get("City") or "").strip()
        if not name or not place_id:
            skipped += 1
            continue
        try:
            with get_db() as db:
                db.execute("INSERT INTO dealers (name, place_id, location) VALUES (?,?,?)",
                           (name, place_id, location))
            added += 1
        except sqlite3.IntegrityError:
            skipped += 1
    flash(f"Imported {added} dealers. {skipped} skipped (duplicates or missing data).", "success")
    return redirect(url_for("index"))


@app.route("/dealers/<int:dealer_id>/delete", methods=["POST"])
def delete_dealer(dealer_id):
    with get_db() as db:
        row = db.execute("SELECT name FROM dealers WHERE id=?", (dealer_id,)).fetchone()
        db.execute("DELETE FROM dealers WHERE id=?", (dealer_id,))
    if row:
        flash(f"'{row['name']}' removed.", "success")
    return redirect(url_for("index"))


@app.route("/run/<int:dealer_id>", methods=["POST"])
def run_one(dealer_id):
    with get_db() as db:
        dealer = db.execute("SELECT * FROM dealers WHERE id=?", (dealer_id,)).fetchone()
    if not dealer:
        return jsonify({"error": "Dealer not found"}), 404

    payload     = request.get_json() or {}
    date_range  = payload.get("date_range", "all")
    from_date   = payload.get("from_date")
    to_date     = payload.get("to_date")
    hide_chrome = bool(payload.get("hide_chrome", False))
    use_cached  = bool(payload.get("use_cached", False))

    try:
        run_id = _start_run(dealer["id"], dealer["name"], dealer["place_id"],
                            location=dealer["location"] or "",
                            date_range=date_range,
                            from_date=from_date,
                            to_date=to_date,
                            hide_chrome=hide_chrome,
                            use_cached=use_cached)
        return jsonify({"run_id": run_id, "status": "queued"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/run/all", methods=["POST"])
def run_all():
    with get_db() as db:
        dealers = db.execute("SELECT * FROM dealers").fetchall()
    if not dealers:
        return jsonify({"error": "No dealers configured."}), 400

    payload     = request.get_json() or {}
    date_range  = payload.get("date_range", "all")
    from_date   = payload.get("from_date")
    to_date     = payload.get("to_date")
    hide_chrome = bool(payload.get("hide_chrome", False))
    use_cached  = bool(payload.get("use_cached", False))

    run_ids = []
    for d in dealers:
        try:
            run_id = _start_run(d["id"], d["name"], d["place_id"],
                                date_range=date_range,
                                from_date=from_date,
                                to_date=to_date,
                                hide_chrome=hide_chrome,
                                use_cached=use_cached)
            run_ids.append(run_id)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    return jsonify({"run_ids": run_ids, "count": len(run_ids)})


@app.route("/status/<int:run_id>")
def job_status(run_id):
    with jobs_lock:
        job = jobs.get(str(run_id), {})
    if not job:
        with get_db() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row:
            job = {"status": row["status"], "step": row["status"].capitalize(),
                   "progress": 100 if row["status"] == "done" else 0,
                   "dashboard": row["dashboard"], "error": row["error"],
                   "date_range_label": row["date_range_label"]}
    return jsonify(job)


@app.route("/stop/<int:run_id>", methods=["POST"])
def stop_run(run_id):
    with jobs_lock:
        job = jobs.get(str(run_id), {})
    if job.get("status") in ("running", "queued"):
        _mark_cancelled(str(run_id))
        _set_job(str(run_id), status="stopping", step="Stopping…", progress=0)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "reason": "Run is not active"}), 400


@app.route("/results")
def results():
    with get_db() as db:
        runs = db.execute(
            "SELECT * FROM runs WHERE status='done' ORDER BY finished_at DESC"
        ).fetchall()
    return render_template("results.html", runs=runs)


@app.route("/output/dashboards/<path:filename>")
def serve_dashboard(filename):
    return send_from_directory(OUTPUT_DIR, filename)


@app.route("/pdf/<path:filename>")
def pdf_print_view(filename):
    """
    Return a portrait print view of a dashboard HTML file — Full Report
    section stripped, portrait @page CSS injected, auto-print on load.
    The browser's Print → Save as PDF produces the portrait PDF.
    """
    from flask import Response, abort
    import re as _re

    filepath = OUTPUT_DIR / filename
    if not filepath.exists():
        abort(404)

    html = filepath.read_text(encoding="utf-8")

    # ── Strip the Full Report section ────────────────────────────────────────
    SPLIT_MARKER = '<div class="section-divider">Full Report</div>'
    if SPLIT_MARKER in html:
        before, after = html.split(SPLIT_MARKER, 1)
        idx = after.find('\n\n</div>')
        if idx != -1:
            html = before + after[idx:]

    # ── Wrap each section-divider + its next sibling in a no-break container ─
    # This is the most reliable way to keep titles with their content.
    import re as _re
    html = _re.sub(
        r'(<div class="section-divider">[^<]*(?:<[^/][^>]*>[^<]*</[^>]+>)*[^<]*</div>)\s*\n(\s*<div)',
        r'<div class="pdf-section-group">\1\n\2',
        html
    )
    # Close each open pdf-section-group before the next one or before </div> of .body
    # Simpler: wrap divider + immediate next sibling using a two-pass replace
    # Instead, use a cleaner regex that handles the full block
    # Reset and use a line-by-line approach
    html = _re.sub(r'<div class="pdf-section-group">', '', html)  # clear any partial wraps

    # Line-by-line wrap: find section-divider lines, wrap with next sibling block
    lines = html.split('\n')
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if 'class="section-divider"' in line:
            out.append('<div class="pdf-section-group">')
            out.append(line)
            i += 1
            # Skip any blank lines, then collect the next sibling div block
            while i < len(lines) and lines[i].strip() == '':
                out.append(lines[i])
                i += 1
            # Emit the opening tag of the next sibling div (rest closes naturally,
            # so we just close the group wrapper after the next top-level div closes)
            # Count open divs to find when the sibling div closes
            if i < len(lines) and lines[i].lstrip().startswith('<div'):
                depth = 0
                while i < len(lines):
                    l = lines[i]
                    depth += l.count('<div') - l.count('</div')
                    out.append(l)
                    i += 1
                    if depth <= 0:
                        break
            out.append('</div><!-- /pdf-section-group -->')
        else:
            out.append(line)
            i += 1
    html = '\n'.join(out)

    # ── Inject portrait print CSS right before </head> ──────────────────────
    portrait_css = """
<style id="pdf-portrait-override">
/* ── Portrait PDF print styles ── */
@page { size: A4 portrait; margin: 12mm 10mm; }
@media print {
  body { background: #fff !important; }
  .topbar { position: static !important; box-shadow: none !important; }
  .pdf-btn, .report-toggle, .report-section-wrapper { display: none !important; }
  .page-hero { page-break-after: avoid; border-radius: 0; }
  .hero-inner { padding: 1.5rem 1.25rem; }
  .metrics-strip { gap: 0.5rem; }
  .body { padding: 1rem 0.75rem; max-width: 100%; }
  .grid-2 { grid-template-columns: 1fr 1fr; gap: 0.75rem; }
  .col-stack { gap: 0.75rem; }
  .card { page-break-inside: avoid; }
  .section-divider { margin-bottom: 0.6rem; break-after: avoid; page-break-after: avoid; }
  .section-divider + * { break-before: avoid; page-break-before: avoid; }
  .section-divider + .grid-2,
  .section-divider + .col-stack,
  .section-divider + .card { break-before: avoid; page-break-before: avoid; }
  .pdf-section-group { break-inside: avoid; page-break-inside: avoid; }
  .csf-list { grid-template-columns: 1fr; }
  .footer { page-break-before: avoid; }
  canvas { max-height: 180px !important; }
}
/* Auto-open print dialog hint banner */
.pdf-hint-bar {
  position: fixed; bottom: 0; left: 0; right: 0; z-index: 9999;
  background: #0d1b2e; color: #fff; padding: 10px 20px;
  display: flex; align-items: center; justify-content: space-between;
  font-family: 'Inter', sans-serif; font-size: 13px; font-weight: 500;
  box-shadow: 0 -4px 20px rgba(0,0,0,0.3);
}
.pdf-hint-bar a {
  display: inline-flex; align-items: center; gap: 6px;
  background: #00b8a0; color: #fff; border: none; padding: 7px 16px;
  border-radius: 6px; font-size: 12px; font-weight: 700; cursor: pointer;
  text-decoration: none; font-family: inherit;
}
.pdf-hint-bar .pdf-hint-close {
  background: rgba(255,255,255,0.1); padding: 5px 10px; border-radius: 5px;
  cursor: pointer; font-size: 11px; color: rgba(255,255,255,0.6);
  border: none; font-family: inherit; margin-left: 10px;
}
@media print { .pdf-hint-bar { display: none !important; } }
</style>
"""
    html = html.replace("</head>", portrait_css + "</head>", 1)

    # ── Inject hint banner + auto-print script before </body> ───────────────
    print_script = """
<div class="pdf-hint-bar" id="pdfHintBar">
  <span>📄 Portrait PDF — use <strong>Print → Save as PDF</strong> (set to Portrait, No margins)</span>
  <div style="display:flex;gap:8px;align-items:center">
    <a href="javascript:window.print()">
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><rect x="2" y="5" width="12" height="8" rx="1" stroke="currentColor" stroke-width="1.5"/><path d="M5 5V3a1 1 0 011-1h4a1 1 0 011 1v2M5 11h6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
      Print / Save as PDF
    </a>
    <button class="pdf-hint-close" onclick="document.getElementById('pdfHintBar').style.display='none'">✕ Close</button>
  </div>
</div>
<script>
// Small delay so charts render before print dialog opens
window.addEventListener('load', function() {
  setTimeout(function() { window.print(); }, 1200);
});
</script>
"""
    html = html.replace("</body>", print_script + "\n</body>", 1)

    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/settings", methods=["GET", "POST"])
def settings():
    cfg = load_config()
    if request.method == "POST":
        cfg["apify_token"]           = request.form.get("apify_token", "").strip()
        cfg["apify_actor_id"]        = request.form.get("apify_actor_id", "").strip()
        cfg["claude_api_key"]        = request.form.get("claude_api_key", "").strip()
        cfg["schedule_enabled"]      = "schedule_enabled" in request.form
        cfg["schedule_day"]          = request.form.get("schedule_day", "monday")
        cfg["schedule_hour"]         = int(request.form.get("schedule_hour", 7))
        cfg["max_reviews_per_dealer"]= int(request.form.get("max_reviews", 500))
        save_config(cfg)
        apply_schedule(cfg)
        flash("Settings saved.", "success")
        return redirect(url_for("settings"))
    return render_template("settings.html", config=cfg)


@app.route("/history")
def history():
    with get_db() as db:
        runs = db.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT 100"
        ).fetchall()
    return render_template("history.html", runs=runs)


# ── Boot ───────────────────────────────────────────────────────────────────

# Always run at startup (works with both gunicorn and direct execution)
init_db()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

cfg = load_config()
scheduler.init_app(app)
scheduler.start()
apply_schedule(cfg)

if __name__ == "__main__":
    # Copy bg image to static if present
    src_bg = BASE_DIR.parent.parent / "bg_image.txt"  # session working dir
    if src_bg.exists() and not BG_FILE.exists():
        BG_FILE.write_text(src_bg.read_text(encoding="utf-8"), encoding="utf-8")

    import webbrowser, threading as _t
    _t.Timer(1.2, lambda: webbrowser.open("http://localhost:5000")).start()

    logger.info("HeroHub Pipeline running at http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
