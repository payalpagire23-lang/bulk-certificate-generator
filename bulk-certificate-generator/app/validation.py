"""Input validation.

Two levels:
  1. Request level   -> the whole request is rejected with HTTP 400 (nothing is saved).
  2. Recipient level -> only that recipient is marked 'failed' with a reason; other recipients continue.
"""
import re
from datetime import date

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

MAX_NAME = 100
MAX_EMAIL = 254
MAX_COURSE = 150
MAX_EVENT = 200


def _clean_str(value, field, max_len, required=False):
    """Returns (clean_value, error)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return (None, f"'{field}' is required") if required else (None, None)
    if not isinstance(value, str):
        return None, f"'{field}' must be a string"
    value = value.strip()
    if CONTROL_CHARS_RE.search(value):
        return None, f"'{field}' contains invalid control characters"
    if len(value) > max_len:
        return None, f"'{field}' must be at most {max_len} characters"
    return value, None


def validate_job_payload(data, max_recipients):
    """Validate request-level fields. Returns (meta, errors)."""
    errors = []
    if not isinstance(data, dict):
        return None, ["Request body must be a JSON object"]

    event_name, err = _clean_str(data.get("event_name"), "event_name", MAX_EVENT, required=True)
    if err:
        errors.append(err)
    title, err = _clean_str(data.get("title"), "title", MAX_EVENT)
    if err:
        errors.append(err)
    issuer, err = _clean_str(data.get("issuer"), "issuer", MAX_EVENT)
    if err:
        errors.append(err)

    issue_date = data.get("issue_date")
    if issue_date in (None, ""):
        issue_date = date.today().isoformat()
    else:
        try:
            issue_date = date.fromisoformat(str(issue_date)).isoformat()
        except ValueError:
            errors.append("'issue_date' must be in YYYY-MM-DD format")

    recipients = data.get("recipients")
    if not isinstance(recipients, list) or len(recipients) == 0:
        errors.append("'recipients' must be a non-empty list")
    elif len(recipients) > max_recipients:
        errors.append(f"'recipients' can contain at most {max_recipients} items per request")

    if errors:
        return None, errors
    meta = {
        "event_name": event_name,
        "title": title or "Certificate of Completion",
        "issuer": issuer or "",
        "issue_date": issue_date,
        "recipients": recipients,
    }
    return meta, []


def validate_recipient(recipient, seen_emails):
    """Validate one recipient. Returns (clean_dict, error_message). Mutates seen_emails (duplicate check)."""
    if not isinstance(recipient, dict):
        return None, "Recipient must be an object with 'name' and 'email'"

    problems = []
    name, err = _clean_str(recipient.get("name"), "name", MAX_NAME, required=True)
    if err:
        problems.append(err)
    email, err = _clean_str(recipient.get("email"), "email", MAX_EMAIL, required=True)
    if err:
        problems.append(err)
    elif not EMAIL_RE.match(email):
        problems.append("'email' is not a valid email address")
    course, err = _clean_str(recipient.get("course"), "course", MAX_COURSE)
    if err:
        problems.append(err)

    if not problems and email.lower() in seen_emails:
        problems.append(f"duplicate email '{email}' in this request")

    if problems:
        return None, "; ".join(problems)
    seen_emails.add(email.lower())
    return {"name": name, "email": email, "course": course}, None
