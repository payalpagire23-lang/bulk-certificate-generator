# Bulk Certificate Generator

Backend API (Python + Flask + SQLite + ReportLab) that accepts **one request with many recipients**, generates a PDF
certificate for each valid recipient from a single predefined template, tracks progress, and lets the client
download the results.

## 1. Setup

Requirements: Python 3.10+

```bash
python -m venv .venv
# Windows (PowerShell):  .venv\Scripts\Activate.ps1
# Windows (cmd):         .venv\Scripts\activate.bat
# macOS / Linux:         source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Run

```bash
python run.py
```
API runs at `http://127.0.0.1:5000`. The SQLite DB is created automatically in `instance/`, PDFs go to `storage/`.

## 3. Run tests

```bash
python -m unittest discover -s tests -t . -v
# or
pytest -v
```

## 4. Submit a generation request

`POST /api/jobs`

```bash
curl -X POST http://127.0.0.1:5000/api/jobs -H "Content-Type: application/json" -d @sample_request.json
```

Body:

| field | required | notes |
|---|---|---|
| `event_name` | yes | max 200 chars |
| `recipients` | yes | list, 1..1000 items |
| `recipients[].name` | yes | max 100 chars |
| `recipients[].email` | yes | valid email, unique inside the request |
| `recipients[].course` | no | printed on the certificate |
| `title` | no | default "Certificate of Completion" |
| `issuer` | no | printed bottom right |
| `issue_date` | no | `YYYY-MM-DD`, default today |

Response `202 Accepted` (job is processed in the background) containing the job id and links.

## 5. Check progress

`GET /api/jobs/<job_id>`

```json
{
  "id": "2696b1a5...", "status": "completed_with_errors",
  "total": 6, "successful": 3, "failed": 3, "pending": 0, "progress_percent": 100.0,
  "failures": [{"index": 4, "name": "Bad Email", "email": "not-an-email", "error": "'email' is not a valid email address"}]
}
```

Job status: `pending` -> `processing` -> `completed` | `completed_with_errors` | `failed` (nothing succeeded).

## 6. Retrieve certificates

| endpoint | purpose |
|---|---|
| `GET /api/jobs/<job_id>/certificates?status=success\|failed\|pending&limit=&offset=` | per-recipient result list |
| `GET /api/certificates/<certificate_id>/download` | one PDF |
| `GET /api/jobs/<job_id>/download` | ZIP of all successful PDFs |
| `GET /api/jobs` | recent jobs |

Windows PowerShell tip: use `curl.exe` (not `curl`) or Invoke-RestMethod; Postman / Thunder Client also work.

## 7. Design decisions

* **Flask + sqlite3 (stdlib)** - minimal moving parts; the schema (`jobs`, `certificates`) is relational with a FK and an index on `(job_id, status)`. Swapping SQLite for PostgreSQL only needs the `db.py` layer changed.
* **Background processing (ThreadPoolExecutor)** - a bulk request can contain 1000 recipients; doing it inside the HTTP request would time out. The API saves the job, returns `202` immediately, and the client polls the status endpoint. A thread pool needs no extra infrastructure (no Redis/Celery), which is the right size for this task. Trade-off: jobs run inside the web process; in production I would move to Celery/RQ + a real queue.
* **Failure isolation** - validation is two-level. Request-level problems (no recipients, > 1000, bad date) -> `400`, nothing saved. Recipient-level problems (bad email, empty name, duplicate email) -> that recipient is stored as `failed` with a reason, others continue. Each certificate generation has its own `try/except` and its own commit, so one crash never stops the job, and progress is visible while running.
* **Restart safety** - `process_job` only touches certificates still `pending`; on startup, unfinished jobs are re-submitted, so an interrupted job resumes without regenerating finished PDFs.
* **Atomic file writes** - PDF is written to `*.tmp` and renamed, so a half-written file is never served.
* **Job ids are UUIDs** - not guessable/enumerable.
* **Tests use synchronous mode** (`ASYNC_PROCESSING=False`) for determinism, plus one test with the real thread pool and one for restart recovery.

## 8. Configuration (environment variables)

`CERT_DATABASE`, `CERT_STORAGE_DIR`, `CERT_ASYNC` (true/false), `CERT_MAX_WORKERS` (4), `CERT_MAX_RECIPIENTS` (1000), `CERT_FONT_PATH`.

## 9. Known limitations

* Default font (Helvetica) prints Western characters only. A name like Devanagari text fails *that one certificate* with a clear message. Set `CERT_FONT_PATH` to a Unicode `.ttf`; note ReportLab does not do complex-script shaping, so Devanagari conjuncts may still look imperfect.
* No authentication, rate limiting, or e-mail delivery (out of scope).
* ZIP download is built in memory - fine for ~1000 small PDFs; stream it for much larger jobs.

## Project layout

```
app/__init__.py    app factory, startup recovery
app/routes.py      HTTP endpoints
app/service.py     create / process / query jobs
app/validation.py  request + recipient validation
app/generator.py   PDF template (ReportLab)
app/db.py          SQLite schema + connection
tests/test_api.py  29 tests
```
