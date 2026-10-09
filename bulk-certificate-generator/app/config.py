"""Configuration. Every value can be overridden with an environment variable."""
import os

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def default_config() -> dict:
    return {
        # SQLite file (relational DB, zero setup).
        "DATABASE": os.environ.get("CERT_DATABASE", os.path.join(BASE_DIR, "instance", "certificates.db")),
        # Where generated PDFs are stored: storage/<job_id>/<certificate_id>.pdf
        "STORAGE_DIR": os.environ.get("CERT_STORAGE_DIR", os.path.join(BASE_DIR, "storage")),
        # True  -> job is processed in a background thread pool (API returns 202 immediately)
        # False -> job is processed inside the request (used by tests, API returns 201)
        "ASYNC_PROCESSING": _bool(os.environ.get("CERT_ASYNC", "true")),
        "MAX_WORKERS": int(os.environ.get("CERT_MAX_WORKERS", "4")),
        # Upper limit of recipients per request (protects the server).
        "MAX_RECIPIENTS": int(os.environ.get("CERT_MAX_RECIPIENTS", "1000")),
        # Optional TTF font (e.g. for non-Latin names). Empty = built-in Helvetica.
        "FONT_PATH": os.environ.get("CERT_FONT_PATH", ""),
    }
