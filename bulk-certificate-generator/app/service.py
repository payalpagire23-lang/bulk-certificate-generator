"""Business logic: create a job, process it, query it."""
import logging
import os
import uuid

from . import db, generator
from .validation import validate_recipient

log = logging.getLogger(__name__)

# Job statuses
PENDING = "pending"
PROCESSING = "processing"
COMPLETED = "completed"
COMPLETED_WITH_ERRORS = "completed_with_errors"
FAILED = "failed"
FINAL_STATUSES = {COMPLETED, COMPLETED_WITH_ERRORS, FAILED}


# ----------------------------------------------------------------------------- create
def create_job(conn, meta: dict) -> str:
    """Save the job + one row per recipient in a single transaction.

    Invalid recipients are stored as 'failed' with the reason (they never block the valid ones).
    """
    job_id = uuid.uuid4().hex
    now = db.utcnow()
    seen_emails = set()
    rows = []
    for idx, raw in enumerate(meta["recipients"]):
        clean, error = validate_recipient(raw, seen_emails)
        if error:
            raw_dict = raw if isinstance(raw, dict) else {}
            rows.append((uuid.uuid4().hex, job_id, idx,
                         _as_text(raw_dict.get("name")), _as_text(raw_dict.get("email")), _as_text(raw_dict.get("course")),
                         "failed", error, now))
        else:
            rows.append((uuid.uuid4().hex, job_id, idx, clean["name"], clean["email"], clean["course"],
                         "pending", None, now))

    with conn:  # commit or rollback as one unit
        conn.execute(
            "INSERT INTO jobs (id, event_name, title, issuer, issue_date, status, total, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (job_id, meta["event_name"], meta["title"], meta["issuer"], meta["issue_date"],
             PENDING, len(rows), now),
        )
        conn.executemany(
            "INSERT INTO certificates (id, job_id, idx, recipient_name, recipient_email, course, status, error, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return job_id


def _as_text(value):
    return None if value is None else str(value)[:300]


# ----------------------------------------------------------------------------- process
def process_job(config: dict, job_id: str) -> None:
    """Generate all pending certificates of a job.

    Safe to call again after a crash/restart: it only touches certificates that are still 'pending'.
    Each certificate is wrapped in its own try/except and committed separately, so
    (a) one failure never stops the others and (b) progress is visible while the job runs.
    """
    conn = db.connect(config["DATABASE"])
    try:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            log.error("process_job: job %s not found", job_id)
            return
        with conn:
            conn.execute("UPDATE jobs SET status = ?, started_at = COALESCE(started_at, ?) WHERE id = ?",
                         (PROCESSING, db.utcnow(), job_id))

        job_dir = os.path.join(config["STORAGE_DIR"], job_id)
        os.makedirs(job_dir, exist_ok=True)

        pending = conn.execute(
            "SELECT * FROM certificates WHERE job_id = ? AND status = 'pending' ORDER BY idx", (job_id,)
        ).fetchall()

        for cert in pending:
            path = os.path.join(job_dir, f"{cert['id']}.pdf")
            try:
                generator.generate_certificate(
                    {
                        "name": cert["recipient_name"], "email": cert["recipient_email"], "course": cert["course"],
                        "event_name": job["event_name"], "title": job["title"],
                        "issuer": job["issuer"], "issue_date": job["issue_date"],
                    },
                    path,
                    config.get("FONT_PATH", ""),
                )
                with conn:
                    conn.execute("UPDATE certificates SET status='success', file_path=?, generated_at=?, error=NULL WHERE id=?",
                                 (path, db.utcnow(), cert["id"]))
            except Exception as exc:  # noqa: BLE001 - isolate every failure
                log.exception("certificate %s failed", cert["id"])
                with conn:
                    conn.execute("UPDATE certificates SET status='failed', error=?, generated_at=? WHERE id=?",
                                 (f"Generation error: {exc}"[:500], db.utcnow(), cert["id"]))

        _finalize_job(conn, job_id)
    except Exception:  # unexpected (e.g. DB problem) -> don't leave the job stuck in 'processing'
        log.exception("job %s crashed", job_id)
        with conn:
            conn.execute("UPDATE jobs SET status=?, completed_at=? WHERE id=?", (FAILED, db.utcnow(), job_id))
    finally:
        conn.close()


def _finalize_job(conn, job_id):
    counts = _counts(conn, job_id)
    if counts["success"] == 0:
        status = FAILED
    elif counts["failed"] > 0:
        status = COMPLETED_WITH_ERRORS
    else:
        status = COMPLETED
    with conn:
        conn.execute("UPDATE jobs SET status=?, completed_at=? WHERE id=?", (status, db.utcnow(), job_id))


# ----------------------------------------------------------------------------- queries
def _counts(conn, job_id) -> dict:
    counts = {"pending": 0, "success": 0, "failed": 0}
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM certificates WHERE job_id=? GROUP BY status", (job_id,)):
        counts[row["status"]] = row["n"]
    return counts


def get_job_summary(conn, job_id, max_failures=100):
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        return None
    return _job_dict(conn, job, max_failures)


def _job_dict(conn, job, max_failures=None):
    counts = _counts(conn, job["id"])
    total = job["total"]
    done = counts["success"] + counts["failed"]
    data = {
        "id": job["id"],
        "event_name": job["event_name"],
        "status": job["status"],
        "total": total,
        "successful": counts["success"],
        "failed": counts["failed"],
        "pending": counts["pending"],
        "progress_percent": round(done * 100 / total, 1) if total else 100.0,
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "completed_at": job["completed_at"],
    }
    if max_failures is not None:
        rows = conn.execute(
            "SELECT idx, recipient_name, recipient_email, error FROM certificates "
            "WHERE job_id=? AND status='failed' ORDER BY idx LIMIT ?", (job["id"], max_failures)).fetchall()
        data["failures"] = [
            {"index": r["idx"], "name": r["recipient_name"], "email": r["recipient_email"], "error": r["error"]}
            for r in rows
        ]
        data["failures_truncated"] = counts["failed"] > len(rows)
    return data


def list_jobs(conn, limit=20, offset=0):
    rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return [_job_dict(conn, r) for r in rows]


def list_certificates(conn, job_id, status=None, limit=100, offset=0):
    sql = "SELECT * FROM certificates WHERE job_id = ?"
    params = [job_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY idx LIMIT ? OFFSET ?"
    params += [limit, offset]
    return conn.execute(sql, params).fetchall()


def certificate_dict(row) -> dict:
    data = {
        "id": row["id"],
        "index": row["idx"],
        "name": row["recipient_name"],
        "email": row["recipient_email"],
        "course": row["course"],
        "status": row["status"],
        "error": row["error"],
        "generated_at": row["generated_at"],
    }
    if row["status"] == "success":
        data["download_url"] = f"/api/certificates/{row['id']}/download"
    return data


def recover_unfinished_jobs(conn):
    """Job ids that were pending/processing when the server stopped."""
    return [r["id"] for r in conn.execute("SELECT id FROM jobs WHERE status IN ('pending','processing') ORDER BY created_at")]
