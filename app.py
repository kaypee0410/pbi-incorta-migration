"""Power BI Extraction Insights - local web app.

Run:  python app.py      then open http://127.0.0.1:5001
Upload a .pbix (or .pbit / .bim); the server decodes it in a background thread and writes the migration
workbook (.xlsx) and a JSON export to outputs/<job id>/.
"""
from __future__ import annotations

import gc
import json
import logging
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from pbi_insights import __version__
from pbi_insights.extract.package import SUPPORTED_EXTENSIONS
from pbi_insights.pipeline import STEPS, Options, run
from pbi_insights.report.webview import build_view, counts_from_summary, diff_counts

BASE = Path(__file__).resolve().parent
UPLOADS = Path(os.getenv("PBI_UPLOAD_DIR", BASE / "uploads"))
OUTPUTS = Path(os.getenv("PBI_OUTPUT_DIR", BASE / "outputs"))
MAX_MB = int(os.getenv("PBI_MAX_UPLOAD_MB", "1024"))
KEEP_UPLOADS = os.getenv("PBI_KEEP_UPLOADS", "0") == "1"
WORKERS = threading.BoundedSemaphore(int(os.getenv("PBI_WORKERS", "2")))
JOB_ID = re.compile(r"^[a-f0-9]{12}$")

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024
log = logging.getLogger("pbi_insights")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()


# ------------------------------------------------------------------------------------------ helpers
def _job_dir(job_id: str) -> Path:
    if not JOB_ID.match(job_id or ""):
        abort(404)
    return OUTPUTS / job_id


def _update(job_id: str, **kw):
    with LOCK:
        JOBS[job_id].update(kw)


def _worker(job_id: str, upload: Path, options: Options):
    out_dir = OUTPUTS / job_id

    def progress(i, msg):
        with LOCK:
            job = JOBS[job_id]
            job["step"] = max(job["step"], i)
            job["message"] = msg
            job["log"] = (job["log"] + [msg])[-40:]

    with WORKERS:
        _update(job_id, status="running", started=time.time())
        try:
            res = run(upload, out_dir, options, progress)
            try:
                view = build_view(json.loads(res.json_path.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001 - the workbook is written: keep the downloads, report the page
                log.exception("job %s: could not build the page view", job_id)
                view = None
            meta = {"id": job_id, "file": JOBS[job_id]["file"], "created": JOBS[job_id]["created"],
                    "summary": res.summary, "workbook": res.workbook.name, "json": res.json_path.name,
                    "incorta": res.incorta_zip.name if res.incorta_zip else None,
                    "options": JOBS[job_id]["options"], "view": view}
            (out_dir / "meta.json").write_text(json.dumps(meta, default=str), encoding="utf-8")
            _update(job_id, status="done", step=len(STEPS), message="Workbook ready", summary=res.summary,
                    view=view, workbook=res.workbook.name, json=res.json_path.name,
                    incorta=meta["incorta"], finished=time.time())
            log.info("job %s done: %s", job_id, res.workbook)
        except Exception as ex:  # noqa: BLE001 - report every failure to the page
            log.error("job %s failed\n%s", job_id, traceback.format_exc())
            _update(job_id, status="error", error=str(ex) or ex.__class__.__name__, finished=time.time())
        finally:
            gc.collect()                      # release the model's file handles before deleting (Windows)
            if not KEEP_UPLOADS:
                shutil.rmtree(upload.parent, ignore_errors=True)


def _read_meta(job_id: str) -> dict | None:
    """A finished run's meta.json. Runs saved before the current page get their view built from the JSON export."""
    folder = _job_dir(job_id)
    path = folder / "meta.json"
    if not path.is_file():
        return None
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if "view" not in m:
        hits = sorted(folder.glob("*_inventory.json"))
        try:
            m["view"] = build_view(json.loads(hits[0].read_text(encoding="utf-8"))) if hits else None
        except Exception:  # noqa: BLE001 - an export the view cannot read: the downloads still work
            log.exception("could not build the view of run %s", job_id)
            m["view"] = None
        m.pop("preview", None)
        try:
            path.write_text(json.dumps(m, default=str), encoding="utf-8")
        except OSError:
            pass
    return m


def _done_file(job_id: str) -> Path:
    return _job_dir(job_id) / "done.json"


def _done_state(job_id: str) -> dict:
    try:
        return json.loads(_done_file(job_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _with_done(job_id: str, view: dict | None) -> dict | None:
    """The view with each review measure's saved done flag."""
    if not view:
        return view
    done = _done_state(job_id)
    groups = [{**g, "measures": [{**m, "done": bool(done.get(m["name"]))} for m in g["measures"]]}
              for g in view.get("groups") or []]
    return {**view, "groups": groups}


def _show_name(job: dict) -> str:
    """The file name as uploaded (the export stores the server-safe name, e.g. spaces become underscores)."""
    return job.get("file") or ""


def _public(job: dict) -> dict:
    keys = ("id", "file", "status", "step", "message", "log", "error", "created", "options")
    out = {k: job.get(k) for k in keys}
    out["steps"] = STEPS
    if job.get("status") == "done":
        out["downloads"] = _downloads(job)
        out["view"] = _with_done(job["id"], job.get("view"))
        if out["view"] and _show_name(job):
            out["view"]["file"] = _show_name(job)
    return out


def _downloads(job: dict) -> dict:
    d = {"workbook": f"/api/jobs/{job['id']}/download/xlsx", "json": f"/api/jobs/{job['id']}/download/json"}
    if job.get("incorta"):
        d["package"] = f"/api/jobs/{job['id']}/download/incorta"
    return d


# ------------------------------------------------------------------------------------------ routes
@app.get("/")
def index():
    return render_template("index.html", max_mb=MAX_MB, version=__version__, steps=STEPS,
                           extensions=",".join(SUPPORTED_EXTENSIONS))


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__}


@app.post("/api/jobs")
def create_job():
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify(error="Choose a .pbix, .pbit or .bim file first."), 400
    name = secure_filename(f.filename) or "upload.pbix"
    if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
        return jsonify(error=f"{f.filename} is not a Power BI file. Use a .pbix, .pbit or .bim file."), 400
    options = Options(read_data=request.form.get("read_data") == "on", incorta=request.form.get("incorta") == "on")
    job_id = uuid.uuid4().hex[:12]
    up_dir = UPLOADS / job_id
    up_dir.mkdir(parents=True, exist_ok=True)
    upload = up_dir / name
    f.save(upload)
    with LOCK:
        JOBS[job_id] = {"id": job_id, "file": f.filename, "status": "queued", "step": 0, "message": "Waiting to start",
                        "log": [], "created": time.time(), "options": vars(options).copy()}
    threading.Thread(target=_worker, args=(job_id, upload, options), daemon=True, name=f"job-{job_id}").start()
    return jsonify(id=job_id), 202


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        if job:
            return jsonify(_public(job))
    m = _read_meta(job_id)
    if m is None:
        abort(404)
    return jsonify(_public({**m, "status": "done", "step": len(STEPS), "message": "Workbook ready", "log": []}))


@app.post("/api/jobs/<job_id>/done")
def mark_done(job_id):
    """Save one review measure's done flag in the run folder, next to the workbook."""
    if _read_meta(job_id) is None:
        abort(404)
    body = request.get_json(silent=True) or {}
    name = body.get("measure")
    if not isinstance(name, str) or not name:
        return jsonify(error="Name the measure."), 400
    with LOCK:
        done = _done_state(job_id)
        if body.get("done"):
            done[name] = True
        else:
            done.pop(name, None)
        _done_file(job_id).write_text(json.dumps(done), encoding="utf-8")
    return jsonify(measure=name, done=bool(done.get(name)))


@app.get("/api/history")
def history():
    """Finished runs grouped by file name, newest first, each with what changed from the run before it."""
    runs = []
    if OUTPUTS.is_dir():
        for meta in OUTPUTS.glob("*/meta.json"):
            if JOB_ID.match(meta.parent.name):
                m = _read_meta(meta.parent.name)
                if m and m.get("id"):
                    runs.append(m)
    by_file: dict[str, list] = {}
    for m in sorted(runs, key=lambda m: m.get("created") or 0, reverse=True):
        by_file.setdefault(m.get("file") or "", []).append(m)
    out = []
    for file, ms in by_file.items():
        rows = []
        for i, m in enumerate(ms):
            counts = counts_from_summary(m.get("summary") or {})
            older = ms[i + 1] if i + 1 < len(ms) else None
            rows.append({"id": m["id"], "created": m.get("created"),
                         "summary": f"{counts['tabs']} tabs, {counts['insights']} insights, {counts['measures']} measures",
                         "diff": diff_counts(counts, counts_from_summary(older.get("summary") or {})) if older else "",
                         "downloads": _downloads(m), "viewable": bool(m.get("view"))})
        out.append({"file": file, "latest": rows[0]["created"], "runs": rows})
    out.sort(key=lambda h: h["latest"] or 0, reverse=True)
    return jsonify(out[:15])


@app.get("/api/jobs/<job_id>/download/<kind>")
def download(job_id, kind):
    folder = _job_dir(job_id)
    pattern = {"xlsx": "*_migration_inventory.xlsx", "json": "*_inventory.json",
               "incorta": "*_incorta_outputs.zip"}.get(kind)
    if pattern is None:
        abort(404)
    hits = sorted(folder.glob(pattern))
    if not hits:
        abort(404)
    return send_file(hits[0], as_attachment=True, download_name=hits[0].name)


@app.errorhandler(RequestEntityTooLarge)
def too_large(_):
    return jsonify(error=f"The file is larger than {MAX_MB} MB. Raise PBI_MAX_UPLOAD_MB in .env to allow it."), 413


if __name__ == "__main__":
    UPLOADS.mkdir(exist_ok=True)
    OUTPUTS.mkdir(exist_ok=True)
    host, port = os.getenv("PBI_HOST", "127.0.0.1"), int(os.getenv("PBI_PORT", "5001"))
    print(f"Power BI Extraction Insights v{__version__}: open http://{host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)
