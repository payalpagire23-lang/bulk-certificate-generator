"""HTTP layer (thin): parse input, call the service, shape the response."""
import io
import os
import re
import zipfile

from flask import Blueprint, current_app, jsonify, request, send_file

from . import db, service
from .validation import validate_job_payload

bp = Blueprint("api", __name__, url_prefix="/api")


def _conn():
    return db.connect(current_app.config["DATABASE"])


def _error(message, status, **extra):
    return jsonify({"error": message, **extra}), status


def _int_arg(name, default, minimum, maximum):
    try:
        value = int(request.args.get(name, default))
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


@bp.get("/health")
def health():
    return jsonify({"status": "ok"})


@bp.post("/jobs")
def create_job():
    data = request.get_json(silent=True)
    if data is None:
        return _error("Request body must be valid JSON (Content-Type: application/json)", 400)

    meta, errors = validate_job_payload(data, current_app.config["MAX_RECIPIENTS"])
    if errors:
        return _error("Validation failed", 400, details=errors)

    conn = _conn()
    try:
        job_id = service.create_job(conn, meta)
    finally:
        conn.close()

    if current_app.config["ASYNC_PROCESSING"]:
        current_app.extensions["executor"].submit(service.process_job, dict(current_app.config), job_id)
        status_code = 202  # accepted, still processing
    else:
        service.process_job(dict(current_app.config), job_id)
        status_code = 201

    conn = _conn()
    try:
        summary = service.get_job_summary(conn, job_id)
    finally:
        conn.close()
    summary["status_url"] = f"/api/jobs/{job_id}"
    summary["certificates_url"] = f"/api/jobs/{job_id}/certificates"
    summary["download_all_url"] = f"/api/jobs/{job_id}/download"
    return jsonify(summary), status_code


@bp.get("/jobs")
def list_jobs():
    conn = _conn()
    try:
        jobs = service.list_jobs(conn, _int_arg("limit", 20, 1, 100), _int_arg("offset", 0, 0, 10**9))
    finally:
        conn.close()
    return jsonify({"jobs": jobs})


@bp.get("/jobs/<job_id>")
def job_status(job_id):
    conn = _conn()
    try:
        summary = service.get_job_summary(conn, job_id)
    finally:
        conn.close()
    if summary is None:
        return _error("Job not found", 404)
    return jsonify(summary)


@bp.get("/jobs/<job_id>/certificates")
def job_certificates(job_id):
    status = request.args.get("status")
    if status and status not in {"pending", "success", "failed"}:
        return _error("'status' must be one of: pending, success, failed", 400)
    conn = _conn()
    try:
        if service.get_job_summary(conn, job_id, max_failures=0) is None:
            return _error("Job not found", 404)
        rows = service.list_certificates(conn, job_id, status,
                                         _int_arg("limit", 100, 1, 500), _int_arg("offset", 0, 0, 10**9))
    finally:
        conn.close()
    return jsonify({"job_id": job_id, "certificates": [service.certificate_dict(r) for r in rows]})


@bp.get("/certificates/<cert_id>/download")
def download_certificate(cert_id):
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM certificates WHERE id = ?", (cert_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return _error("Certificate not found", 404)
    if row["status"] == "pending":
        return _error("Certificate is not generated yet", 409)
    if row["status"] == "failed":
        return _error("Certificate generation failed", 409, reason=row["error"])
    if not row["file_path"] or not os.path.exists(row["file_path"]):
        return _error("Certificate file is missing on the server", 410)
    return send_file(row["file_path"], mimetype="application/pdf", as_attachment=True,
                     download_name=f"certificate_{_safe(row['recipient_name'])}.pdf")


@bp.get("/jobs/<job_id>/download")
def download_job_zip(job_id):
    """All successfully generated certificates of a job as one ZIP."""
    conn = _conn()
    try:
        job = service.get_job_summary(conn, job_id, max_failures=0)
        if job is None:
            return _error("Job not found", 404)
        rows = service.list_certificates(conn, job_id, "success", limit=10**9)
    finally:
        conn.close()
    if not rows:
        return _error("No certificates are available for download yet", 409)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zf:  # PDFs are already compressed
        for row in rows:
            if row["file_path"] and os.path.exists(row["file_path"]):
                zf.write(row["file_path"], f"{row['idx'] + 1:04d}_{_safe(row['recipient_name'])}.pdf")
    buffer.seek(0)
    return send_file(buffer, mimetype="application/zip", as_attachment=True,
                     download_name=f"certificates_{job_id[:8]}.zip")


def _safe(text):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text or "certificate").strip("_")[:50] or "certificate"
