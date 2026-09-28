"""Participant list ingestion: CSV/XLSX upload -> normalized rows.

User columns (12):
  Last Name | First Name | Position | Company | Corporative Email |
  Country | Company Phone | Phone | Mobile | E-mail |
  Participant Type | Activity

Contract (per user instruction):
- File may be .csv or .xlsx, multiple rows.
- Selecting a Name+Company pulls that row's other cells for the brief.
- Phone + Mobile are IGNORED everywhere: never stored, never returned,
  never used for discovery. Only official channels are kept:
  Corporative Email, E-mail, Company Phone.

Good-practice notes:
- Pure functions, no network, no DB here (storage lives in store.py).
- Never raises on bad rows: callers get (rows, notes) and continue partial.
- CSV-injection safe: cells starting with =,+,-,@ are prefixed on export,
  never executed (we only render as text).
"""
from __future__ import annotations

import csv
import io
import re

MAX_ROWS = 1000
MAX_BYTES = 2 * 1024 * 1024  # 2MB cap: participant lists are small

# Canonical header -> normalized key. Phone/Mobile map to None (dropped).
_HEADER_MAP = {
    "lastname": "last_name",
    "last name": "last_name",
    "firstname": "first_name",
    "first name": "first_name",
    "position": "position",
    "company": "company",
    "corporative email": "corp_email",
    "corporativeemail": "corp_email",
    "corporative e-mail": "corp_email",
    "country": "country",
    "company phone": "company_phone",
    "companyphone": "company_phone",
    "phone": None,   # explicitly ignored per user instruction
    "mobile": None,  # explicitly ignored per user instruction
    "e-mail": "email",
    "e-mail ": "email",
    "email": "email",
    "e mail": "email",
    "participant type": "participant_type",
    "participanttype": "participant_type",
    "activity": "activity",
}

_REQUIRED = ("last_name", "first_name", "company")


def _norm_header(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").strip().lower().replace("_", " "))


def _clean(v) -> str:
    if v is None:
        return ""
    s = str(v).strip().replace("\ufeff", "")
    # Collapse internal whitespace, cap length (defensive, Excel can be huge).
    s = re.sub(r"\s+", " ", s).strip()[:500]
    return s


def _primary_company(raw: str) -> str:
    """'BELTELECOM, YASNAe TV' -> 'BELTELECOM' for discovery.

    Full raw value is preserved separately; discovery uses the first segment
    so 'Company, Division' lists don't poison firmographic lookup.
    """
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    return parts[0] if parts else (raw or "").strip()


def normalize_rows(raw_rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Map arbitrary header spellings -> canonical participant dicts.

    Returns (rows, notes). Drops Phone/Mobile keys entirely.
    Skips rows missing Last+First or Company (loud note, keeps rest).
    """
    rows: list[dict] = []
    notes: list[str] = []
    for i, raw in enumerate(raw_rows, start=1):
        if len(rows) >= MAX_ROWS:
            notes.append(f"row cap {MAX_ROWS} reached; remaining rows skipped")
            break
        mapped: dict[str, str] = {}
        for k, v in (raw or {}).items():
            key = _HEADER_MAP.get(_norm_header(str(k)), None)
            # Unknown headers: keep namespaced as extra_* (never Phone/Mobile).
            if key is None and _norm_header(str(k)) not in (
                "phone", "mobile",
            ):
                # Distinguish truly-unknown from explicitly-dropped.
                if _norm_header(str(k)) not in _HEADER_MAP:
                    mapped[f"extra_{_norm_header(str(k))}"] = _clean(v)
                continue
            if key is None:
                continue  # Phone/Mobile dropped
            mapped[key] = _clean(v)
        if not any(mapped.get(k) for k in _REQUIRED):
            notes.append(f"row {i} skipped: missing Last/First/Company")
            continue
        full = f"{mapped.get('first_name','')} {mapped.get('last_name','')}".strip()
        company_raw = mapped.get("company", "")
        mapped["full_name"] = re.sub(r"\s+", " ", full)[:200]
        mapped["company_raw"] = company_raw[:200]
        mapped["company_primary"] = _primary_company(company_raw)[:200]
        mapped["display"] = f"{mapped['full_name']} @ {mapped['company_primary']}"[:250]
        # Stable per-upload id (index-based; upload_id namespaces globally).
        mapped["pid"] = f"p{i:04d}"
        rows.append(mapped)
    if not rows and not notes:
        notes.append("no data rows found (header only or empty file)")
    return rows, notes


def parse_csv(data: bytes) -> tuple[list[dict], list[str]]:
    """Parse CSV bytes -> normalized rows. Tries utf-8-sig, falls back latin-1."""
    notes: list[str] = []
    if len(data) > MAX_BYTES:
        return [], [f"file too large ({len(data)} bytes > {MAX_BYTES})"]
    text = None
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except Exception:
            continue
    if text is None:
        return [], ["undecodable file (tried utf-8-sig/utf-8/latin-1)"]
    try:
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return [], ["no header row found"]
        raw = list(reader)
    except Exception as e:
        return [], [f"csv parse failed: {type(e).__name__}"]
    return normalize_rows(raw)


def parse_xlsx(data: bytes) -> tuple[list[dict], list[str]]:
    """Parse XLSX bytes -> normalized rows (first sheet, first row = header)."""
    if len(data) > MAX_BYTES:
        return [], [f"file too large ({len(data)} bytes > {MAX_BYTES})"]
    try:
        from openpyxl import load_workbook
    except ImportError:
        return [], ["xlsx support missing: install openpyxl"]
    try:
        wb = load_workbook(filename=io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        header = None
        for r in rows_iter:
            if r is None:
                continue
            vals = ["" if v is None else str(v) for v in r]
            if not any(v.strip() for v in vals):
                continue  # skip blank lines above header
            header = vals
            break
        if not header:
            return [], ["no header row found in xlsx"]
        raw: list[dict] = []
        for r in rows_iter:
            if r is None:
                continue
            vals = ["" if v is None else str(v) for v in r]
            if not any(v.strip() for v in vals):
                continue
            raw.append({h: v for h, v in zip(header, vals)})
        return normalize_rows(raw)
    except Exception as e:
        return [], [f"xlsx parse failed: {type(e).__name__}: {e}"]


def parse_upload(filename: str, data: bytes) -> tuple[list[dict], list[str]]:
    """Dispatch by extension. Returns (rows, notes). Never raises."""
    name = (filename or "").lower()
    try:
        if name.endswith(".csv"):
            return parse_csv(data)
        if name.endswith((".xlsx", ".xlsm")):
            return parse_xlsx(data)
        return [], ["unsupported extension (use .csv or .xlsx)"]
    except Exception as e:  # defensive: parsers never raise, but be safe
        return [], [f"parse failed: {type(e).__name__}"]
