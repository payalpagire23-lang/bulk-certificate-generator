"""PDF certificate generation using ReportLab.

One fixed template: landscape A4, double border, title, recipient name, event/course text, date, issuer.
"""
import os
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

_registered_fonts = set()


def _font(font_path: str):
    """Returns (regular, bold) font names. Uses a custom TTF when configured."""
    if font_path and os.path.exists(font_path):
        if font_path not in _registered_fonts:
            pdfmetrics.registerFont(TTFont("CertFont", font_path))
            _registered_fonts.add(font_path)
        return "CertFont", "CertFont"
    return "Helvetica", "Helvetica-Bold"


def _fit_font_size(text, font, start_size, max_width, min_size=14):
    """Shrink the font until the text fits the available width (handles long names)."""
    size = start_size
    while size > min_size and pdfmetrics.stringWidth(text, font, size) > max_width:
        size -= 1
    return size


def generate_certificate(data: dict, output_path: str, font_path: str = "") -> None:
    """Create the certificate PDF at output_path.

    data keys: name, email, course, event_name, title, issuer, issue_date
    Written to a temp file first and renamed, so a half-written PDF is never left behind.
    """
    regular, bold = _font(font_path)
    if regular == "Helvetica":
        # The built-in font only supports Western (cp1252) characters. Fail clearly instead of printing garbage.
        for key in ("name", "course", "event_name", "title", "issuer"):
            try:
                (data.get(key) or "").encode("cp1252")
            except UnicodeEncodeError:
                raise ValueError(f"'{key}' has characters the default font cannot print "
                                 f"(set CERT_FONT_PATH to a Unicode .ttf font)") from None
    width, height = landscape(A4)
    tmp_path = output_path + ".tmp"

    try:
        c = canvas.Canvas(tmp_path, pagesize=(width, height))
        c.setTitle(f"{data['title']} - {data['name']}")

        # Double border
        c.setStrokeColor(colors.HexColor("#1F3A5F"))
        c.setLineWidth(6)
        c.rect(25, 25, width - 50, height - 50)
        c.setStrokeColor(colors.HexColor("#C9A227"))
        c.setLineWidth(2)
        c.rect(38, 38, width - 76, height - 76)

        center = width / 2
        max_text_width = width - 140

        c.setFillColor(colors.HexColor("#1F3A5F"))
        size = _fit_font_size(data["title"], bold, 38, max_text_width)
        c.setFont(bold, size)
        c.drawCentredString(center, height - 120, data["title"])

        c.setFillColor(colors.black)
        c.setFont(regular, 16)
        c.drawCentredString(center, height - 170, "This certificate is proudly presented to")

        c.setFillColor(colors.HexColor("#C9A227"))
        size = _fit_font_size(data["name"], bold, 40, max_text_width)
        c.setFont(bold, size)
        c.drawCentredString(center, height - 240, data["name"])
        c.setStrokeColor(colors.HexColor("#C9A227"))
        c.setLineWidth(1)
        c.line(center - 220, height - 252, center + 220, height - 252)

        c.setFillColor(colors.black)
        c.setFont(regular, 16)
        if data.get("course"):
            line1 = f"for successfully completing {data['course']}"
            line2 = f"at {data['event_name']}"
        else:
            line1 = "for participating in"
            line2 = data["event_name"]
        c.drawCentredString(center, height - 300, _truncate(line1, regular, 16, max_text_width))
        c.drawCentredString(center, height - 325, _truncate(line2, regular, 16, max_text_width))

        c.setFont(regular, 13)
        c.drawString(90, 90, f"Date: {data['issue_date']}")
        if data.get("issuer"):
            c.drawRightString(width - 90, 90, _truncate(f"Issued by: {data['issuer']}", regular, 13, 330))

        c.showPage()
        c.save()
        os.replace(tmp_path, output_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _truncate(text, font, size, max_width):
    while len(text) > 3 and pdfmetrics.stringWidth(text, font, size) > max_width:
        text = text[:-4] + "..."
    return text
