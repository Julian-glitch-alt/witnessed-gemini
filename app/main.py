import base64
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import random
import secrets
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import qrcode

from app.db import get_connection, get_data_dir, init_db, log_claim_event
from witnessed.checks import map_parts_to_claimed_area, run_checks
from witnessed.providers import FakeProvider, get_provider
from witnessed.schemas import (
    VALID_PARTS,
    DamageReading,
    Finding,
    PlateReading,
    Policy,
    VinReading,
    WideReading,
)
from witnessed.seal import load_or_create_key, seal, verify
from witnessed.summary import summarize

SLOT_SCHEMAS: dict[str, type[Any]] = {
    "wide": WideReading,
    "damage": DamageReading,
    "plate": PlateReading,
    "vin": VinReading,
}

VALID_SLOTS = {"wide", "damage", "plate", "vin"}

HUMAN_LABELS: dict[str, str] = {
    "plate_match": "Plate",
    "vehicle_match": "Vehicle",
    "vin_format": "VIN",
    "damage_present": "Damage visible",
    "damage_location": "Damage location",
    "parts_match": "Parts match",
    "duplicate_photo": "Duplicate photo",
    "photo_reused": "Photo reused",
}

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    load_or_create_key()
    yield


app = FastAPI(title="Witnessed", lifespan=lifespan)

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def get_needs_decision_count() -> int:
    try:
        conn = get_connection()
        count = conn.execute(
            "SELECT COUNT(*) FROM claims WHERE status = 'ready_for_review'"
        ).fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0


def format_relative_time(iso_str: str | None) -> str:
    if not iso_str:
        return "recently"
    try:
        dt = datetime.fromisoformat(iso_str)
        now = datetime.now(timezone.utc)
        diff = now - dt
        seconds = int(diff.total_seconds())
        if seconds < 60:
            return "just now"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        days = hours // 24
        return f"{days}d ago"
    except Exception:
        return "recently"


def format_readable_datetime(val: Any, time_val: Any = None) -> str:
    if not val:
        return "Not given"
    s = str(val).strip()
    if not s or s.lower() in ("n/a", "none", "null", "not given"):
        return "Not given"

    t_str = str(time_val).strip() if time_val else ""
    if t_str.lower() in ("n/a", "none", "null", "not given"):
        t_str = ""

    try:
        if len(s) == 10 and s.count("-") == 2:
            d = datetime.strptime(s, "%Y-%m-%d").date()
            if t_str:
                t = None
                for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p"):
                    try:
                        t = datetime.strptime(t_str, fmt).time()
                        break
                    except ValueError:
                        pass
                if t:
                    dt = datetime.combine(d, t)
                    hour_str = dt.strftime("%I:%M %p").lstrip("0")
                    return f"{dt.strftime('%b')} {dt.day}, {dt.year}, {hour_str}"
            return f"{d.strftime('%b')} {d.day}, {d.year}"
        else:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is not None:
                dt = dt.astimezone()
            hour_str = dt.strftime("%I:%M %p").lstrip("0")
            return f"{dt.strftime('%b')} {dt.day}, {dt.year}, {hour_str}"
    except Exception:
        return s


def format_readable_time(time_val: Any) -> str:
    if not time_val:
        return "Not given"
    s = str(time_val).strip()
    if not s or s.lower() in ("n/a", "none", "null", "not given"):
        return "Not given"
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p"):
        try:
            t = datetime.strptime(s, fmt).time()
            return datetime.combine(date(2000, 1, 1), t).strftime("%I:%M %p").lstrip("0")
        except ValueError:
            pass
    return s


def get_claimant_initials(name: str | None) -> str:
    if not name or not name.strip():
        return "CL"
    parts = name.strip().split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    return parts[0][:2].upper()


def parse_iso_ts(iso_str: str | None) -> float:
    if not iso_str:
        return 0.0
    try:
        return datetime.fromisoformat(iso_str).timestamp()
    except Exception:
        return 0.0


def compute_claim_lane(claim: Any, claim_findings: list[Any]) -> tuple[str, int, str]:
    if claim["status"] == "pending_capture":
        return ("waiting", 3, "No photos yet.")

    def _field(f: Any, key: str) -> str:
        if hasattr(f, "__getitem__"):
            try:
                return str(f[key] or "")
            except (KeyError, IndexError, TypeError):
                pass
        return str(getattr(f, key, "") or "")

    def _status(f: Any) -> str:
        return _field(f, "status")

    def _chk_name(f: Any) -> str:
        name = _field(f, "check_name")
        if not name:
            name = _field(f, "check")
        return name

    reds = [f for f in claim_findings if _status(f) == "red"]
    if reds:
        chk = _chk_name(reds[0])
        label = HUMAN_LABELS.get(chk, chk)
        reason = _field(reds[0], "reason")
        why = f"{label}: {reason}" if label else reason
        return ("mismatch", 0, why)

    ambers = [f for f in claim_findings if _status(f) == "amber"]
    if ambers:
        chk = _chk_name(ambers[0])
        label = HUMAN_LABELS.get(chk, chk)
        reason = _field(ambers[0], "reason")
        why = f"{label}: {reason}" if label else reason
        return ("review", 1, why)

    return ("clear", 2, "All checks matched.")


def build_findings_summary(findings: list[Any]) -> str:
    if not findings:
        return "No issues found by the automated checks. The adjuster makes the decision."

    def _status(f: Any) -> str:
        if hasattr(f, "status"):
            return getattr(f, "status")
        return f["status"]

    def _check(f: Any) -> str:
        if hasattr(f, "check"):
            return getattr(f, "check")
        try:
            return f["check_name"]
        except Exception:
            return f["check"]

    issues = [
        HUMAN_LABELS.get(_check(f), _check(f))
        for f in findings
        if _status(f) in ("red", "amber")
    ]
    if issues:
        count = len(issues)
        if count == 1:
            return f"1 issue needs review: {issues[0]}."
        else:
            return f"{count} issues need review: {', '.join(issues)}."
    return "No issues found by the automated checks. The adjuster makes the decision."


def generate_qr_data_uri(data_url: str) -> str:
    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(data_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.get("/", response_class=RedirectResponse)
async def root():
    return RedirectResponse(url="/console", status_code=303)


@app.get("/console", response_class=HTMLResponse)
async def console_list(request: Request):
    conn = get_connection()
    claims_rows = conn.execute("SELECT * FROM claims").fetchall()
    findings_rows = conn.execute("SELECT * FROM findings").fetchall()
    photos_rows = conn.execute("SELECT * FROM photos").fetchall()
    conn.close()

    findings_by_claim: dict[str, list[Any]] = {}
    for f in findings_rows:
        findings_by_claim.setdefault(f["claim_id"], []).append(f)

    claim_items = []
    for c in claims_rows:
        cid = c["id"]
        c_findings = findings_by_claim.get(cid, [])
        lane, priority, why_here = compute_claim_lane(c, c_findings)
        claim_items.append({
            "id": c["id"],
            "claimant_name": c["claimant_name"] or "N/A",
            "initials": get_claimant_initials(c["claimant_name"]),
            "make": c["make"] or "",
            "model": c["model"] or "",
            "color": c["color"] or "",
            "plate": c["plate"] or "",
            "state": c["state"] or "",
            "claimed_damage_area": c["claimed_damage_area"] or "N/A",
            "status": c["status"],
            "created_at": c["created_at"],
            "created_ts": parse_iso_ts(c["created_at"]),
            "updated_relative": format_relative_time(c["created_at"]),
            "lane": lane,
            "lane_priority": priority,
            "why_here": why_here,
            "vehicle_str": f"{c['color']} {c['make']} {c['model']}".strip(),
        })

    # Sort: red first (0), then amber (1), then clear (2), then waiting (3); newest first within a group
    sorted_claims = sorted(claim_items, key=lambda x: (x["lane_priority"], -x["created_ts"]))

    waiting_decision_count = sum(1 for c in claim_items if c["status"] == "ready_for_review")
    mismatch_count = sum(1 for c in claim_items if c["lane"] == "mismatch")
    waiting_claimant_count = sum(1 for c in claim_items if c["status"] == "pending_capture")
    decided_count = sum(1 for c in claim_items if c["status"] in ("approved", "recapture_requested", "escalated_to_siu"))
    total_claims = len(claim_items)

    waiting_claims = [c for c in claim_items if c["status"] == "pending_capture"]
    latest_waiting_claimant = waiting_claims[0]["claimant_name"] if waiting_claims else None

    now = datetime.now()
    header_subline = f"{now.strftime('%A, %B')} {now.day}. {waiting_decision_count} need a decision, {waiting_claimant_count} waiting for the claimant."

    photos_read = sum(1 for p in photos_rows if p["status"] == "read")
    readings_unchecked = sum(1 for p in photos_rows if p["status"] == "unchecked")
    models = [p["model"] for p in photos_rows if "model" in p.keys() and p["model"]]
    if models:
        from collections import Counter
        model_used_most = Counter(models).most_common(1)[0][0]
    else:
        model_used_most = "gemini-2.5-flash"

    # Claims this week: bars are the last 7 days ending today in the server's local date
    today_local = datetime.now().date()
    dates_7 = [today_local - timedelta(days=6 - i) for i in range(7)]

    def _get_local_date(iso_str: str | None) -> date | None:
        if not iso_str:
            return None
        try:
            dt = datetime.fromisoformat(iso_str)
            if dt.tzinfo is not None:
                return dt.astimezone().date()
            return dt.date()
        except Exception:
            return None

    day_claims_map: dict[date, list[Any]] = {d: [] for d in dates_7}
    for c in claim_items:
        c_date = _get_local_date(c["created_at"])
        if c_date in day_claims_map:
            day_claims_map[c_date].append(c)

    max_day_claims = max((len(day_claims_map[d]) for d in dates_7), default=0)

    chart_days = []
    for d_date in dates_7:
        d_claims = day_claims_map[d_date]
        total_d = len(d_claims)
        clear_d = sum(1 for c in d_claims if c["lane"] == "clear")
        review_d = sum(1 for c in d_claims if c["lane"] == "review")
        mismatch_d = sum(1 for c in d_claims if c["lane"] == "mismatch")
        waiting_d = sum(1 for c in d_claims if c["lane"] == "waiting")

        segments = []
        if total_d > 0:
            if clear_d > 0:
                segments.append({
                    "lane": "Clear",
                    "color": "green",
                    "count": clear_d,
                    "pct": (clear_d / total_d) * 100,
                })
            if review_d > 0:
                segments.append({
                    "lane": "Review",
                    "color": "amber",
                    "count": review_d,
                    "pct": (review_d / total_d) * 100,
                })
            if mismatch_d > 0:
                segments.append({
                    "lane": "Mismatch",
                    "color": "red",
                    "count": mismatch_d,
                    "pct": (mismatch_d / total_d) * 100,
                })
            if waiting_d > 0:
                segments.append({
                    "lane": "Waiting",
                    "color": "gray",
                    "count": waiting_d,
                    "pct": (waiting_d / total_d) * 100,
                })
            height_pct = min(100, max(24, int((total_d / max(max_day_claims, 1)) * 100)))
        else:
            height_pct = 0

        chart_days.append({
            "date": d_date.isoformat(),
            "day": d_date.strftime("%a"),
            "total": total_d,
            "clear": clear_d,
            "review": review_d,
            "mismatch": mismatch_d,
            "waiting": waiting_d,
            "height_pct": height_pct,
            "segments": segments,
        })

    model_provider = os.getenv("MODEL_PROVIDER", "fake").strip().lower()
    model_status_label = "Live inference" if model_provider == "gemini" else "Demo data (fake model)"

    counts = {
        "needs_me": waiting_decision_count,
        "mismatch": mismatch_count,
        "waiting": waiting_claimant_count,
        "decided": decided_count,
        "all": total_claims,
    }

    return templates.TemplateResponse(
        request=request,
        name="console_list.html",
        context={
            "claims": sorted_claims,
            "counts": counts,
            "header_subline": header_subline,
            "waiting_decision_count": waiting_decision_count,
            "mismatch_count": mismatch_count,
            "waiting_claimant_count": waiting_claimant_count,
            "latest_waiting_claimant": latest_waiting_claimant,
            "avg_minutes": 3,
            "photos_read": photos_read,
            "readings_unchecked": readings_unchecked,
            "model_used_most": model_used_most,
            "chart_days": chart_days,
            "needs_decision_count": waiting_decision_count,
            "active_nav": "today",
            "model_status_label": model_status_label,
        },
    )


@app.get("/console/new", response_class=HTMLResponse)
async def console_new_form(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="console_new.html",
        context={
            "needs_decision_count": get_needs_decision_count(),
            "active_nav": "new",
        },
    )


@app.post("/console/new", response_class=HTMLResponse)
async def console_create_claim(
    request: Request,
    make: str = Form(...),
    model: str = Form(...),
    color: str = Form(...),
    plate: str = Form(...),
    state: str = Form(...),
    claimant_name: str = Form(...),
    claimant_statement: str = Form(""),
    claimed_damage_area: str = Form(""),
):
    claim_id = f"CLM-{secrets.token_hex(4).upper()}"
    token = secrets.token_urlsafe(16)
    code_letter = random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ")
    session_code = f"{code_letter}{random.randint(1,9)}-{random.randint(10,99)}"
    now_iso = datetime.now(timezone.utc).isoformat()

    conn = get_connection()
    with conn:
        conn.execute(
            """
            INSERT INTO claims (
                id, make, model, color, plate, state,
                claimant_name, claimant_statement, claimed_damage_area,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                claim_id,
                make.strip(),
                model.strip(),
                color.strip(),
                plate.strip(),
                state.strip().upper(),
                claimant_name.strip(),
                claimant_statement.strip(),
                claimed_damage_area.strip().lower(),
                "pending_capture",
                now_iso,
            ),
        )
        conn.execute(
            """
            INSERT INTO sessions (
                token, claim_id, session_code, status, retake_counts, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (token, claim_id, session_code, "open", "{}", now_iso),
        )

    claim = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    conn.close()

    log_claim_event(
        claim_id,
        "claim_created",
        f"Claim created for {claimant_name.strip()} ({color.strip()} {make.strip()} {model.strip()})",
    )

    capture_url = str(request.url_for("capture_page", token=token))
    qr_data_uri = generate_qr_data_uri(capture_url)

    return templates.TemplateResponse(
        request=request,
        name="console_created.html",
        context={
            "claim": claim,
            "session_code": session_code,
            "capture_url": capture_url,
            "qr_data_uri": qr_data_uri,
            "needs_decision_count": get_needs_decision_count(),
            "active_nav": "new",
        },
    )


@app.get("/c/{token}", response_class=HTMLResponse)
async def capture_page(request: Request, token: str):
    conn = get_connection()
    session = conn.execute(
        "SELECT * FROM sessions WHERE token = ?", (token,)
    ).fetchone()
    if not session:
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")

    claim = conn.execute(
        "SELECT * FROM claims WHERE id = ?", (session["claim_id"],)
    ).fetchone()

    photos_rows = conn.execute(
        "SELECT * FROM photos WHERE session_token = ? ORDER BY id DESC", (token,)
    ).fetchall()
    conn.close()

    existing_photos: dict[str, dict[str, Any]] = {}
    for row in photos_rows:
        if row["slot"] not in existing_photos:
            existing_photos[row["slot"]] = {
                "sha256": row["sha256"],
                "status": row["status"],
                "model": row["model"] if "model" in row.keys() else None,
            }

    log_claim_event(
        session["claim_id"],
        "link_opened",
        f"Capture link opened (session {session['session_code']})",
    )

    req_slots_json = session["requested_slots"] if "requested_slots" in session.keys() else None
    requested_slots: list[str] = []
    if req_slots_json:
        try:
            parsed = json.loads(req_slots_json)
            if isinstance(parsed, list):
                requested_slots = parsed
        except Exception:
            pass

    is_recapture_round = bool(requested_slots)
    num_requested = len(requested_slots)
    kept_slots = [s for s in ("plate", "vin", "damage", "wide") if s not in requested_slots]
    current_round = session["round"] if "round" in session.keys() and session["round"] else 1

    r2_photos = [
        row for row in photos_rows
        if ("round" in row.keys() and row["round"] == current_round)
    ]
    r2_taken_slots: dict[str, Any] = {}
    for row in r2_photos:
        if row["slot"] not in r2_taken_slots:
            r2_taken_slots[row["slot"]] = row

    new_photos_taken_count = len([s for s in requested_slots if s in r2_taken_slots])
    is_ready_to_send = (new_photos_taken_count == num_requested) and (num_requested > 0)
    missing_slots = [s for s in requested_slots if s not in r2_taken_slots]

    SLOT_NAMES_HUMAN = {
        "plate": "License plate",
        "vin": "VIN plate",
        "damage": "Damage close-up",
        "wide": "The whole vehicle",
    }
    missing_slots_str = ", ".join(SLOT_NAMES_HUMAN.get(s, s) for s in missing_slots)

    kept_photos: dict[str, Any] = {}
    for s in kept_slots:
        prior_photo = next(
            (
                row for row in photos_rows
                if row["slot"] == s and ("round" not in row.keys() or row["round"] != current_round)
            ),
            None,
        )
        if not prior_photo:
            prior_photo = next((row for row in photos_rows if row["slot"] == s), None)
        if prior_photo:
            kept_photos[s] = prior_photo

    retake_counts = json.loads(session["retake_counts"] or "{}")
    existing_answers = {}
    if claim and "answers_json" in claim.keys() and claim["answers_json"]:
        try:
            existing_answers = json.loads(claim["answers_json"])
        except Exception:
            existing_answers = {}

    return templates.TemplateResponse(
        request=request,
        name="capture.html",
        context={
            "token": token,
            "session_code": session["session_code"],
            "session_status": session["status"],
            "claim": claim,
            "existing_photos": existing_photos,
            "existing_answers": existing_answers,
            "retake_counts": retake_counts,
            "is_recapture_round": is_recapture_round,
            "requested_slots": requested_slots,
            "num_requested": num_requested,
            "kept_slots": kept_slots,
            "kept_photos": kept_photos,
            "r2_taken_slots": r2_taken_slots,
            "new_photos_taken_count": new_photos_taken_count,
            "is_ready_to_send": is_ready_to_send,
            "missing_slots": missing_slots,
            "missing_slots_str": missing_slots_str,
            "slot_names_human": SLOT_NAMES_HUMAN,
        },
    )


@app.post("/c/{token}/answers")
async def save_answers(request: Request, token: str):
    conn = get_connection()
    session = conn.execute(
        "SELECT * FROM sessions WHERE token = ?", (token,)
    ).fetchone()
    if not session:
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")

    if session["status"] != "open":
        conn.close()
        raise HTTPException(status_code=400, detail="Session is closed")

    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("Expected JSON object")
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    # Validate types
    incident_type = data.get("incident_type")
    date_val = data.get("date")
    time_val = data.get("time")
    place_val = data.get("place")
    city_val = data.get("city")
    state_val = data.get("state")
    statement_val = data.get("statement")
    other_vehicle_val = data.get("other_vehicle")
    if other_vehicle_val is None:
        other_vehicle_val = data.get("other-vehicle")
    police_val = data.get("police")
    drivable_val = data.get("drivable")
    parts_val = data.get("parts")
    other_parts_note_val = data.get("other_parts_note")
    if other_parts_note_val is None:
        other_parts_note_val = data.get("other_parts")

    for field_name, field_val in [
        ("incident_type", incident_type),
        ("date", date_val),
        ("time", time_val),
        ("place", place_val),
        ("city", city_val),
        ("state", state_val),
        ("statement", statement_val),
        ("other_vehicle", other_vehicle_val),
        ("police", police_val),
        ("drivable", drivable_val),
        ("other_parts_note", other_parts_note_val),
    ]:
        if field_val is not None and not isinstance(field_val, str):
            conn.close()
            raise HTTPException(
                status_code=400, detail=f"{field_name} must be a string or null"
            )

    if parts_val is not None and not isinstance(parts_val, list):
        conn.close()
        raise HTTPException(status_code=400, detail="parts must be a list of strings")

    parts_list = [str(p) for p in (parts_val or [])]
    claimed_area = map_parts_to_claimed_area(parts_list) if parts_list else None

    claim = conn.execute(
        "SELECT * FROM claims WHERE id = ?", (session["claim_id"],)
    ).fetchone()
    existing_answers = {}
    if claim and "answers_json" in claim.keys() and claim["answers_json"]:
        try:
            existing_answers = json.loads(claim["answers_json"])
        except Exception:
            existing_answers = {}

    merged_answers = {
        **existing_answers,
        "incident_type": incident_type if incident_type is not None else existing_answers.get("incident_type"),
        "date": date_val if date_val is not None else existing_answers.get("date"),
        "time": time_val if time_val is not None else existing_answers.get("time"),
        "place": place_val if place_val is not None else existing_answers.get("place"),
        "city": city_val if city_val is not None else existing_answers.get("city"),
        "state": state_val if state_val is not None else existing_answers.get("state"),
        "statement": statement_val if statement_val is not None else existing_answers.get("statement"),
        "other_vehicle": other_vehicle_val if other_vehicle_val is not None else (existing_answers.get("other_vehicle") or existing_answers.get("other-vehicle")),
        "police": police_val if police_val is not None else existing_answers.get("police"),
        "drivable": drivable_val if drivable_val is not None else existing_answers.get("drivable"),
        "parts": parts_list if parts_val is not None else existing_answers.get("parts", []),
        "other_parts_note": other_parts_note_val if other_parts_note_val is not None else existing_answers.get("other_parts_note"),
    }

    answers_json = json.dumps(merged_answers)
    stmt = (
        statement_val.strip()
        if statement_val and isinstance(statement_val, str) and statement_val.strip()
        else None
    )

    with conn:
        if parts_val is not None:
            conn.execute(
                """
                UPDATE claims
                SET answers_json = ?,
                    claimant_statement = COALESCE(?, claimant_statement),
                    claimed_damage_area = ?
                WHERE id = ?
                """,
                (answers_json, stmt, claimed_area, session["claim_id"]),
            )
        else:
            conn.execute(
                """
                UPDATE claims
                SET answers_json = ?,
                    claimant_statement = COALESCE(?, claimant_statement)
                WHERE id = ?
                """,
                (answers_json, stmt, session["claim_id"]),
            )
    conn.close()

    parts_desc = ", ".join(parts_list) if parts_list else "None"
    log_claim_event(
        session["claim_id"],
        "answers_saved",
        f"Claimant answers saved: parts listed: {parts_desc}",
    )

    return {
        "ok": True,
        "claimed_damage_area": claimed_area,
        "answers": merged_answers,
    }


@app.post("/c/{token}/photo/{slot}")
async def upload_photo(request: Request, token: str, slot: str):
    if slot not in VALID_SLOTS:
        raise HTTPException(status_code=400, detail=f"Invalid slot: {slot}")

    conn = get_connection()
    session = conn.execute(
        "SELECT * FROM sessions WHERE token = ?", (token,)
    ).fetchone()
    if not session:
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")

    if session["status"] != "open":
        conn.close()
        raise HTTPException(status_code=400, detail="Session is closed")

    req_slots_json = session["requested_slots"] if "requested_slots" in session.keys() else None
    current_round = session["round"] if "round" in session.keys() and session["round"] else 1
    if req_slots_json:
        try:
            req_slots = json.loads(req_slots_json)
            if isinstance(req_slots, list) and slot not in req_slots:
                conn.close()
                raise HTTPException(
                    status_code=400,
                    detail=f"Slot '{slot}' was not requested for recapture.",
                )
        except HTTPException:
            raise
        except Exception:
            pass

    if req_slots_json:
        prior_uploads = conn.execute(
            "SELECT COUNT(*) FROM photos WHERE session_token = ? AND slot = ? AND round = ?",
            (token, slot, current_round),
        ).fetchone()[0]
    else:
        prior_uploads = conn.execute(
            "SELECT COUNT(*) FROM photos WHERE session_token = ? AND slot = ?",
            (token, slot),
        ).fetchone()[0]

    if prior_uploads >= 4:
        conn.close()
        raise HTTPException(
            status_code=409, detail="Maximum retakes exceeded for this slot"
        )

    photo_bytes = await request.body()
    if not (
        photo_bytes.startswith(b"\xff\xd8\xff")
        or photo_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    ):
        conn.close()
        raise HTTPException(
            status_code=415,
            detail="Unsupported media type. Only JPEG and PNG are allowed.",
        )

    sha256 = hashlib.sha256(photo_bytes).hexdigest()
    ext = ".png" if photo_bytes.startswith(b"\x89PNG\r\n\x1a\n") else ".jpg"
    photo_path = get_data_dir() / "photos" / f"{sha256}{ext}"
    with open(photo_path, "wb") as f:
        f.write(photo_bytes)

    try:
        provider = get_provider()
    except Exception as exc:
        logger.exception("Error obtaining provider: %s", exc)
        provider = None

    reading = None
    if provider is not None:
        try:
            reading = provider.read(slot, photo_bytes)
        except Exception as exc:
            logger.exception("Provider error reading photo for slot %s: %s", slot, exc)
            reading = None

    reading_json = (
        reading.model_dump_json()
        if hasattr(reading, "model_dump_json")
        else (json.dumps(reading) if reading else None)
    )
    status = "read" if reading is not None else "unchecked"
    model_used = (
        getattr(provider, "last_model", None)
        or getattr(provider, "model", None)
        or getattr(provider, "name", None)
        or ("fake" if isinstance(provider, FakeProvider) else "gemini")
    )
    now_iso = datetime.now(timezone.utc).isoformat()

    new_total = prior_uploads + 1
    retakes_left = max(0, 4 - new_total)
    retake_counts = json.loads(session["retake_counts"] or "{}")
    retake_counts[slot] = new_total - 1

    with conn:
        conn.execute(
            """
            INSERT INTO photos (
                session_token, slot, sha256, path, reading_json, status, model, round, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token,
                slot,
                sha256,
                str(photo_path),
                reading_json,
                status,
                model_used,
                current_round,
                now_iso,
            ),
        )
        conn.execute(
            "UPDATE sessions SET retake_counts = ? WHERE token = ?",
            (json.dumps(retake_counts), token),
        )
    conn.close()

    log_claim_event(
        session["claim_id"],
        "photo_taken",
        f"Photo taken for slot {slot} using model {model_used}",
    )

    return {
        "ok": True,
        "slot": slot,
        "sha256": sha256,
        "status": status,
        "model": model_used,
        "retakes_left": retakes_left,
    }


@app.post("/c/{token}/submit")
async def submit_capture(request: Request, token: str):
    conn = get_connection()
    session = conn.execute(
        "SELECT * FROM sessions WHERE token = ?", (token,)
    ).fetchone()
    if not session:
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")

    if session["status"] != "open":
        conn.close()
        raise HTTPException(status_code=400, detail="Session is closed")

    photos_rows = conn.execute(
        "SELECT * FROM photos WHERE session_token = ? ORDER BY id DESC", (token,)
    ).fetchall()

    req_slots_json = session["requested_slots"] if "requested_slots" in session.keys() else None
    is_recapture = False
    requested_slots: list[str] = []
    if req_slots_json:
        try:
            parsed = json.loads(req_slots_json)
            if isinstance(parsed, list) and parsed:
                is_recapture = True
                requested_slots = parsed
        except Exception:
            pass

    current_round = session["round"] if "round" in session.keys() and session["round"] else (2 if is_recapture else 1)

    latest_photos: dict[str, Any] = {}
    if is_recapture:
        r2_photos = [
            row for row in photos_rows
            if ("round" in row.keys() and row["round"] == current_round)
        ]
        r2_map = {row["slot"]: row for row in r2_photos}
        for s in requested_slots:
            if s not in r2_map:
                s_photo = next((row for row in photos_rows if row["slot"] == s), None)
                if not s_photo:
                    conn.close()
                    raise HTTPException(
                        status_code=400,
                        detail=f"Photos for all requested slots ({', '.join(requested_slots)}) are required before submitting.",
                    )
                r2_map[s] = s_photo
        for slot in VALID_SLOTS:
            if slot in requested_slots:
                latest_photos[slot] = r2_map[slot]
            else:
                prior_photo = next(
                    (
                        row for row in photos_rows
                        if row["slot"] == slot and ("round" not in row.keys() or row["round"] != current_round)
                    ),
                    None,
                )
                if not prior_photo:
                    prior_photo = next((row for row in photos_rows if row["slot"] == slot), None)
                if not prior_photo:
                    conn.close()
                    raise HTTPException(
                        status_code=400,
                        detail=f"Missing photo for slot {slot}.",
                    )
                latest_photos[slot] = prior_photo
    else:
        for row in photos_rows:
            if row["slot"] not in latest_photos:
                latest_photos[row["slot"]] = row

        if not all(slot in latest_photos for slot in VALID_SLOTS):
            conn.close()
            raise HTTPException(
                status_code=400,
                detail="All four slots (wide, damage, plate, vin) are required before submitting.",
            )

    claim = conn.execute(
        "SELECT * FROM claims WHERE id = ?", (session["claim_id"],)
    ).fetchone()
    if not claim:
        conn.close()
        raise HTTPException(status_code=404, detail="Claim not found")

    body_data: dict[str, Any] = {}
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body_data = await request.json()
            if not isinstance(body_data, dict):
                body_data = {}
        except Exception:
            body_data = {}
    else:
        try:
            form = await request.form()
            body_data = dict(form)
        except Exception:
            body_data = {}

    if is_recapture:
        signer_name = claim["signer_name"] or claim["claimant_name"] or "Claimant"
        signature_str = claim["signature_image"] or ""
        sig_sha256 = claim["signature"][:32] if claim["signature"] else ""
        answers_dict: dict[str, Any] = {}
        if "answers_json" in claim.keys() and claim["answers_json"]:
            try:
                answers_dict = json.loads(claim["answers_json"])
            except Exception:
                answers_dict = {}
        claimed_area = claim["claimed_damage_area"]
    else:
        signer_name = (
            body_data.get("signer_name")
            or (claim["claimant_name"] if claim["claimant_name"] else None)
        )
        if isinstance(signer_name, str):
            signer_name = signer_name.strip()
        if not signer_name:
            conn.close()
            raise HTTPException(status_code=400, detail="Signer name is required.")

        confirmed_val = body_data.get("confirmed")
        if isinstance(confirmed_val, str):
            confirmed = confirmed_val.lower() in ("true", "1", "yes", "on")
        else:
            confirmed = bool(confirmed_val)
        if not confirmed:
            conn.close()
            raise HTTPException(
                status_code=400,
                detail="You must confirm the information is true to the best of your knowledge.",
            )

        signature_data = body_data.get("signature")
        if not signature_data or not isinstance(signature_data, str) or not signature_data.strip():
            conn.close()
            raise HTTPException(status_code=400, detail="Signature is required before submitting.")

        signature_str = signature_data.strip()
        if not signature_str.startswith("data:image/png;base64,"):
            conn.close()
            raise HTTPException(status_code=400, detail="Signature must be a PNG data URL.")

        if len(signature_str.encode("utf-8")) > 300 * 1024:
            conn.close()
            raise HTTPException(status_code=400, detail="Signature image exceeds 300 KB limit.")

        try:
            sig_b64_part = signature_str.split(",", 1)[1]
            sig_bytes = base64.b64decode(sig_b64_part)
        except Exception:
            conn.close()
            raise HTTPException(status_code=400, detail="Invalid signature base64 data.")

        if len(sig_bytes) > 300 * 1024:
            conn.close()
            raise HTTPException(status_code=400, detail="Signature image exceeds 300 KB limit.")

        sig_sha256 = hashlib.sha256(sig_bytes).hexdigest()

        answers_dict: dict[str, Any] = {}
        if "answers_json" in claim.keys() and claim["answers_json"]:
            try:
                answers_dict = json.loads(claim["answers_json"])
            except Exception:
                answers_dict = {}
        if "answers" in body_data and isinstance(body_data["answers"], dict):
            body_answers = dict(body_data["answers"])
            if "other-vehicle" in body_answers and "other_vehicle" not in body_answers:
                body_answers["other_vehicle"] = body_answers["other-vehicle"]
            for k, v in body_answers.items():
                if v is not None:
                    answers_dict[k] = v
        elif not answers_dict and claim["claimant_statement"]:
            answers_dict = {
                "statement": claim["claimant_statement"],
                "parts": [claim["claimed_damage_area"]] if claim["claimed_damage_area"] else [],
            }

        if not answers_dict:
            conn.close()
            raise HTTPException(status_code=400, detail="Answers are required before submitting.")

        if "parts" in answers_dict and isinstance(answers_dict["parts"], list):
            claimed_area = map_parts_to_claimed_area([str(p) for p in answers_dict["parts"]])
        else:
            claimed_area = claim["claimed_damage_area"]

    # Reconstruct readings
    readings: dict[str, Any] = {}
    photo_hashes: dict[str, str] = {}
    for slot in VALID_SLOTS:
        row = latest_photos[slot]
        photo_hashes[slot] = row["sha256"]
        r_json = row["reading_json"]
        if r_json:
            try:
                readings[slot] = SLOT_SCHEMAS[slot].model_validate_json(r_json)
            except Exception:
                readings[slot] = None
        else:
            readings[slot] = None

    claimed_parts_list = []
    if "parts" in answers_dict and isinstance(answers_dict["parts"], list):
        claimed_parts_list = [str(p) for p in answers_dict["parts"] if p]
    elif claim["claimed_damage_area"]:
        claimed_parts_list = [claim["claimed_damage_area"]]

    policy = Policy(
        make=claim["make"],
        model=claim["model"],
        color=claim["color"],
        plate=claim["plate"],
        state=claim["state"],
        claimant_name=claim["claimant_name"],
        claimant_statement=claim["claimant_statement"],
        claimed_damage_area=claimed_area,
        claimed_parts=claimed_parts_list,
    )

    reused_photos: dict[str, str] = {}
    if photo_hashes:
        placeholders = ",".join("?" for _ in photo_hashes)
        params = list(photo_hashes.values()) + [claim["id"]]
        reused_rows = conn.execute(
            f"""
            SELECT photos.sha256, sessions.claim_id
            FROM photos
            JOIN sessions ON photos.session_token = sessions.token
            WHERE photos.sha256 IN ({placeholders})
              AND sessions.claim_id != ?
            ORDER BY photos.id ASC
            """,
            params,
        ).fetchall()
        for r_row in reused_rows:
            if r_row["sha256"] not in reused_photos:
                reused_photos[r_row["sha256"]] = r_row["claim_id"]

    findings = run_checks(policy, readings, photos=photo_hashes, reused_photos=reused_photos)
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        provider = get_provider()
    except Exception as exc:
        logger.exception("Error obtaining provider: %s", exc)
        provider = None

    package_data = {
        "answers": answers_dict,
        "claim": {
            "id": claim["id"],
            "claimant_name": claim["claimant_name"],
            "claimant_statement": claim["claimant_statement"],
            "claimed_damage_area": claimed_area,
        },
        "confirmed": True,
        "findings": [f.model_dump() for f in findings],
        "photos": photo_hashes,
        "policy": {
            "color": claim["color"],
            "make": claim["make"],
            "model": claim["model"],
            "plate": claim["plate"],
            "state": claim["state"],
        },
        "readings": {
            slot: {
                "model": (
                    latest_photos[slot]["model"]
                    if "model" in latest_photos[slot].keys() and latest_photos[slot]["model"]
                    else (
                        getattr(provider, "last_model", None)
                        or getattr(provider, "model", None)
                        or getattr(provider, "name", None)
                        or ("fake" if isinstance(provider, FakeProvider) else "gemini")
                    )
                ),
                "reading": (r.model_dump() if r and hasattr(r, "model_dump") else r),
            }
            for slot, r in readings.items()
        },
        "session_code": session["session_code"],
        "signature_sha256": sig_sha256,
        "signer_name": signer_name,
        "timestamps": {
            "created_at": claim["created_at"],
            "submitted_at": now_iso,
        },
    }

    if is_recapture:
        package_data["version"] = 2
        package_data["previous_signature"] = claim["signature"]

    pkg_bytes, sig_b64 = seal(package_data)

    ai_summary_dict = None
    if provider is not None:
        try:
            ai_summary_dict = summarize(findings, provider)
        except Exception as exc:
            logger.exception("Provider error generating summary during submit: %s", exc)
            ai_summary_dict = None

    ai_summary_json = json.dumps(ai_summary_dict)

    with conn:
        conn.execute("DELETE FROM findings WHERE claim_id = ?", (claim["id"],))
        for f in findings:
            conn.execute(
                """
                INSERT INTO findings (claim_id, check_name, status, reason)
                VALUES (?, ?, ?, ?)
                """,
                (claim["id"], f.check, f.status, f.reason),
            )
        if is_recapture:
            conn.execute(
                """
                UPDATE claims
                SET status = 'ready_for_review',
                    package_bytes = ?,
                    signature = ?,
                    previous_signature = ?,
                    version = 2,
                    round = 2,
                    ai_summary = ?
                WHERE id = ?
                """,
                (
                    pkg_bytes,
                    sig_b64,
                    claim["signature"],
                    ai_summary_json,
                    claim["id"],
                ),
            )
            conn.execute(
                "UPDATE sessions SET status = 'submitted', requested_slots = NULL WHERE token = ?",
                (token,),
            )
        else:
            conn.execute(
                """
                UPDATE claims
                SET status = 'ready_for_review',
                    package_bytes = ?,
                    signature = ?,
                    ai_summary = ?,
                    signer_name = ?,
                    signature_image = ?,
                    signed_at = ?,
                    answers_json = ?,
                    claimed_damage_area = ?
                WHERE id = ?
                """,
                (
                    pkg_bytes,
                    sig_b64,
                    ai_summary_json,
                    signer_name,
                    signature_str,
                    now_iso,
                    json.dumps(answers_dict),
                    claimed_area,
                    claim["id"],
                ),
            )
            conn.execute(
                "UPDATE sessions SET status = 'submitted' WHERE token = ?", (token,)
            )
    conn.close()

    if is_recapture:
        log_claim_event(
            claim["id"],
            "round_2_sent",
            f"Round 2 photos submitted for slots: {', '.join(requested_slots)}",
        )
        log_claim_event(
            claim["id"],
            "sealed",
            "Package sealed with Ed25519 signature (version 2)",
        )
    else:
        log_claim_event(
            claim["id"],
            "submitted",
            f"Claim evidence submitted by {signer_name}",
        )
        log_claim_event(
            claim["id"],
            "sealed",
            "Package sealed with Ed25519 signature (version 1)",
        )

    accept_header = request.headers.get("accept", "")
    if "application/json" in accept_header:
        return {"ok": True, "redirect": f"/c/{token}/complete"}
    return RedirectResponse(url=f"/c/{token}/complete", status_code=303)


@app.post("/c/{token}/recapture/send")
async def recapture_send(request: Request, token: str):
    return await submit_capture(request, token)


@app.get("/c/{token}/complete", response_class=HTMLResponse)
async def capture_complete(request: Request, token: str):
    conn = get_connection()
    session = conn.execute(
        "SELECT * FROM sessions WHERE token = ?", (token,)
    ).fetchone()
    if not session:
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")
    claim = conn.execute(
        "SELECT * FROM claims WHERE id = ?", (session["claim_id"],)
    ).fetchone()
    conn.close()
    return templates.TemplateResponse(
        request=request,
        name="c_complete.html",
        context={
            "session_code": session["session_code"],
            "claim": claim,
        },
    )


@app.get("/photos/{sha256}")
async def serve_photo(sha256: str):
    data_dir = get_data_dir() / "photos"
    for ext in (".jpg", ".png"):
        candidate = data_dir / f"{sha256}{ext}"
        if candidate.exists():
            media = "image/png" if ext == ".png" else "image/jpeg"
            return FileResponse(candidate, media_type=media)
    raise HTTPException(status_code=404, detail="Photo not found")


@app.get("/console/claims/{id}", response_class=HTMLResponse)
async def claim_report(request: Request, id: str):
    conn = get_connection()
    claim = conn.execute("SELECT * FROM claims WHERE id = ?", (id,)).fetchone()
    if not claim:
        conn.close()
        raise HTTPException(status_code=404, detail="Claim not found")

    session = conn.execute(
        "SELECT * FROM sessions WHERE claim_id = ? ORDER BY created_at DESC", (id,)
    ).fetchone()
    photos: dict[str, Any] = {}
    if session:
        photos_rows = conn.execute(
            "SELECT * FROM photos WHERE session_token = ? ORDER BY id DESC",
            (session["token"],),
        ).fetchall()
        for row in photos_rows:
            if row["slot"] not in photos:
                photos[row["slot"]] = row

    findings_rows = conn.execute(
        "SELECT * FROM findings WHERE claim_id = ?", (id,)
    ).fetchall()
    decisions = conn.execute(
        "SELECT * FROM decisions WHERE claim_id = ? ORDER BY timestamp DESC", (id,)
    ).fetchall()

    queue_rows = conn.execute(
        "SELECT id FROM claims WHERE status = 'ready_for_review' ORDER BY created_at DESC"
    ).fetchall()
    needs_decision_count = len(queue_rows)

    reused_slots = set()
    for s, p in photos.items():
        if p and "sha256" in p.keys() and p["sha256"]:
            sha = p["sha256"]
            row = conn.execute(
                """
                SELECT 1 FROM photos
                JOIN sessions ON photos.session_token = sessions.token
                WHERE photos.sha256 = ? AND sessions.claim_id != ?
                LIMIT 1
                """,
                (sha, id),
            ).fetchone()
            if row:
                reused_slots.add(s)

    claim_notes = conn.execute(
        "SELECT * FROM claim_notes WHERE claim_id = ? ORDER BY id DESC", (id,)
    ).fetchall()
    timeline_events = conn.execute(
        "SELECT * FROM claim_events WHERE claim_id = ? ORDER BY id ASC", (id,)
    ).fetchall()

    conn.close()

    queue_ids = [r["id"] for r in queue_rows]
    if id in queue_ids:
        k = queue_ids.index(id) + 1
        n = len(queue_ids)
        next_claim_id = queue_ids[(queue_ids.index(id) + 1) % n] if n > 1 else None
    else:
        k = 1
        n = max(1, len(queue_ids))
        next_claim_id = queue_ids[0] if queue_ids else None

    status_order = {"red": 0, "amber": 1, "unchecked": 2, "green": 3}
    findings = sorted(
        findings_rows,
        key=lambda f: status_order.get(
            f["status"] if hasattr(f, "__getitem__") else getattr(f, "status", ""), 99
        ),
    )

    summary_line = build_findings_summary(findings)
    is_valid_seal = None
    if claim["package_bytes"] and claim["signature"]:
        is_valid_seal = verify(claim["package_bytes"], claim["signature"])

    ai_summary = None
    if "ai_summary" in claim.keys() and claim["ai_summary"] is not None:
        try:
            ai_summary = json.loads(claim["ai_summary"])
        except Exception:
            ai_summary = None
    elif findings:
        try:
            provider = get_provider()
            ai_summary = summarize(findings, provider)
        except Exception as exc:
            logger.exception("Provider error generating summary for report: %s", exc)
            ai_summary = None

    group_definitions = [
        {
            "id": "vehicle_and_plate",
            "name": "Vehicle and plate",
            "checks": ["plate_match", "vehicle_match", "vin_format"],
        },
        {
            "id": "damage",
            "name": "Damage",
            "checks": ["damage_present", "damage_location", "parts_match"],
        },
        {
            "id": "photo_integrity",
            "name": "Photo integrity",
            "checks": ["duplicate_photo", "photo_reused"],
        },
    ]

    severity_order = {"red": 0, "amber": 1, "unchecked": 2, "green": 3}

    seen_checks = set()
    raw_groups = []
    for g_def in group_definitions:
        g_findings = []
        for chk in g_def["checks"]:
            f_row = next((f for f in findings_rows if f["check_name"] == chk), None)
            if f_row:
                seen_checks.add(chk)
                g_findings.append(f_row)

        g_findings.sort(key=lambda f: severity_order.get(f["status"], 99))

        worst_sev = min((severity_order.get(f["status"], 99) for f in g_findings), default=2)
        if worst_sev == 0:
            g_status = "red"
            g_status_label = "Mismatch"
        elif worst_sev == 1:
            g_status = "amber"
            g_status_label = "Review"
        elif worst_sev == 2:
            g_status = "unchecked"
            g_status_label = "Unchecked"
        else:
            g_status = "green"
            g_status_label = "Match"

        raw_groups.append({
            "id": g_def["id"],
            "name": g_def["name"],
            "findings": g_findings,
            "worst_sev": worst_sev,
            "status": g_status,
            "status_label": g_status_label,
        })

    other_findings = [f for f in findings_rows if f["check_name"] not in seen_checks]
    if other_findings:
        other_findings.sort(key=lambda f: severity_order.get(f["status"], 99))
        other_worst = min((severity_order.get(f["status"], 99) for f in other_findings), default=2)
        if other_worst == 0:
            o_status = "red"
            o_label = "Mismatch"
        elif other_worst == 1:
            o_status = "amber"
            o_label = "Review"
        elif other_worst == 2:
            o_status = "unchecked"
            o_label = "Unchecked"
        else:
            o_status = "green"
            o_label = "Match"

        raw_groups.append({
            "id": "other",
            "name": "Other checks",
            "findings": other_findings,
            "worst_sev": other_worst,
            "status": o_status,
            "status_label": o_label,
        })

    raw_groups.sort(key=lambda g: g["worst_sev"])

    all_numbered_findings = []
    groups = []
    finding_num = 1
    for idx, g in enumerate(raw_groups, start=1):
        numbered_findings = []
        for f_row in g["findings"]:
            chk = f_row["check_name"]
            item = {
                "number": finding_num,
                "check_name": chk,
                "label": HUMAN_LABELS.get(chk, chk),
                "status": f_row["status"],
                "reason": f_row["reason"],
                "adjuster_label": (
                    f_row["adjuster_label"]
                    if "adjuster_label" in f_row.keys()
                    else None
                ),
            }
            numbered_findings.append(item)
            all_numbered_findings.append(item)
            finding_num += 1

        groups.append({
            "id": g["id"],
            "title": f"{idx}. {g['name']}",
            "findings": numbered_findings,
            "status": g["status"],
            "status_label": g["status_label"],
        })

    sha_to_slots: dict[str, list[str]] = {}
    for s, p in photos.items():
        if p and "sha256" in p.keys() and p["sha256"]:
            sha_to_slots.setdefault(p["sha256"], []).append(s)
    duplicate_slots = {
        s for sha, slist in sha_to_slots.items() if len(slist) > 1 for s in slist
    }

    slot_markers: dict[str, int] = {}
    slot_borders: dict[str, str] = {}

    for item in all_numbered_findings:
        if item["status"] not in ("red", "amber"):
            continue
        chk = item["check_name"]
        inv_slots: list[str] = []
        if chk == "plate_match":
            inv_slots = ["plate"]
        elif chk == "vehicle_match":
            inv_slots = ["wide"]
        elif chk == "vin_format":
            inv_slots = ["vin"]
        elif chk in ("damage_present", "damage_location"):
            inv_slots = ["damage", "wide"]
        elif chk == "duplicate_photo":
            inv_slots = [s for s in ("wide", "damage", "plate", "vin") if s in duplicate_slots]
        elif chk == "photo_reused":
            inv_slots = [s for s in ("wide", "damage", "plate", "vin") if s in reused_slots]

        for s in inv_slots:
            if s not in slot_borders:
                slot_borders[s] = item["status"]
                slot_markers[s] = item["number"]
            elif slot_borders[s] == "amber" and item["status"] == "red":
                slot_borders[s] = "red"
                slot_markers[s] = item["number"]

    has_issues = any(f["status"] in ("red", "amber") for f in findings_rows)
    if has_issues:
        suggested_step = {
            "type": "review",
            "title": "Adjuster review",
            "bg": "amber",
            "line": f"Based on findings 1 to {len(all_numbered_findings)}. You make the decision.",
        }
    else:
        suggested_step = {
            "type": "fast_track",
            "title": "Fast track",
            "bg": "green",
            "line": f"Based on findings 1 to {len(all_numbered_findings)}. You make the decision.",
        }

    vin_as_read = None
    if "vin" in photos and photos["vin"]["reading_json"]:
        try:
            vin_data = json.loads(photos["vin"]["reading_json"])
            vin_as_read = vin_data.get("vin")
        except Exception:
            vin_as_read = None

    answers = {}
    if "answers_json" in claim.keys() and claim["answers_json"]:
        try:
            answers = json.loads(claim["answers_json"])
        except Exception:
            answers = {}
    if "other-vehicle" in answers and "other_vehicle" not in answers:
        answers["other_vehicle"] = answers["other-vehicle"]

    date_raw = answers.get("date")
    time_raw = answers.get("time")
    if date_raw:
        date_of_loss_formatted = format_readable_datetime(date_raw, time_raw)
    elif claim["created_at"]:
        date_of_loss_formatted = format_readable_datetime(claim["created_at"])
    else:
        date_of_loss_formatted = "Not given"

    time_formatted = format_readable_time(time_raw)

    signed_at_raw = claim["signed_at"] or claim["created_at"]
    signed_at_formatted = format_readable_datetime(signed_at_raw) if signed_at_raw else "Not given"

    submitted_date_str = signed_at_formatted
    ready_date_str = signed_at_formatted

    sig_short = claim["signature"][:8] if claim["signature"] else ""

    seen_parts: list[str] = []
    for s in ("wide", "damage"):
        p_row = photos.get(s)
        if p_row and p_row["reading_json"]:
            try:
                r_data = json.loads(p_row["reading_json"])
                for part in r_data.get("parts_seen") or []:
                    if part in VALID_PARTS and part not in seen_parts:
                        seen_parts.append(part)
            except Exception:
                pass

    listed_parts: list[str] = []
    if answers and "parts" in answers and isinstance(answers["parts"], list):
        for part in answers["parts"]:
            if part and str(part) not in listed_parts:
                listed_parts.append(str(part))
    elif claim["claimed_damage_area"]:
        listed_parts.append(claim["claimed_damage_area"].capitalize())

    seen_parts_str = ", ".join(seen_parts) if seen_parts else "None"
    listed_parts_str = ", ".join(listed_parts) if listed_parts else "None"

    is_round_2 = (
        ("version" in claim.keys() and (claim["version"] or 1) >= 2)
        or ("round" in claim.keys() and (claim["round"] or 1) >= 2)
        or ("previous_signature" in claim.keys() and bool(claim["previous_signature"]))
    )

    return templates.TemplateResponse(
        request=request,
        name="console_claim.html",
        context={
            "claim": claim,
            "session": session,
            "answers": answers,
            "date_of_loss_formatted": date_of_loss_formatted,
            "time_formatted": time_formatted,
            "signed_at_formatted": signed_at_formatted,
            "photos": photos,
            "findings": findings,
            "findings_groups": groups,
            "all_numbered_findings": all_numbered_findings,
            "slot_markers": slot_markers,
            "slot_borders": slot_borders,
            "suggested_step": suggested_step,
            "decisions": decisions,
            "summary_line": summary_line,
            "is_valid_seal": is_valid_seal,
            "ai_summary": ai_summary,
            "vin_as_read": vin_as_read,
            "submitted_date_str": submitted_date_str,
            "ready_date_str": ready_date_str,
            "sig_short": sig_short,
            "k": k,
            "n": n,
            "next_claim_id": next_claim_id,
            "needs_decision_count": needs_decision_count,
            "active_nav": "claim",
            "human_labels": HUMAN_LABELS,
            "seen_parts": seen_parts,
            "listed_parts": listed_parts,
            "seen_parts_str": seen_parts_str,
            "listed_parts_str": listed_parts_str,
            "claim_notes": claim_notes,
            "timeline_events": timeline_events,
            "is_round_2": is_round_2,
        },
    )


@app.post("/console/claims/{id}/findings/{check}/label")
async def label_finding(
    request: Request,
    id: str,
    check: str,
    label: str | None = Form(None),
):
    if label is None:
        try:
            body = await request.json()
            if isinstance(body, dict):
                label = body.get("label")
        except Exception:
            pass

    if not label or label.strip().lower() not in {"confirm", "false_alarm"}:
        raise HTTPException(
            status_code=400,
            detail="Invalid label. Allowed values: confirm, false_alarm.",
        )

    clean_label = label.strip().lower()
    conn = get_connection()
    claim = conn.execute("SELECT id FROM claims WHERE id = ?", (id,)).fetchone()
    if not claim:
        conn.close()
        raise HTTPException(status_code=404, detail="Claim not found")

    with conn:
        conn.execute(
            "UPDATE findings SET adjuster_label = ? WHERE claim_id = ? AND check_name = ?",
            (clean_label, id, check),
        )
    conn.close()

    log_claim_event(
        id,
        "finding_label",
        f"Finding {check} labeled as {clean_label}",
    )

    accept_header = request.headers.get("accept", "")
    if "application/json" in accept_header or request.headers.get("x-requested-with") == "XMLHttpRequest":
        return {"ok": True, "claim_id": id, "check": check, "label": clean_label}

    return RedirectResponse(url=f"/console/claims/{id}", status_code=303)


@app.get("/console/claims/{id}/package")
async def download_package(id: str):
    conn = get_connection()
    claim = conn.execute("SELECT * FROM claims WHERE id = ?", (id,)).fetchone()
    conn.close()
    if not claim or not claim["package_bytes"] or not claim["signature"]:
        raise HTTPException(status_code=404, detail="Sealed package not found")

    package_json = {
        "package": claim["package_bytes"].decode("utf-8"),
        "signature": claim["signature"],
    }
    return Response(
        content=json.dumps(package_json, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="claim_{id}_package.json"'
        },
    )


@app.post("/console/claims/{id}/decision")
async def make_decision(
    request: Request,
    id: str,
    action: str | None = Form(None),
    note: str | None = Form(None),
    slots: list[str] | None = Form(None),
    reason: str | None = Form(None),
):
    action_val = action
    note_val = note
    slots_val = slots
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
            if isinstance(body, dict):
                action_val = body.get("action") or action_val
                note_val = body.get("note") or body.get("reason") or note_val
                slots_val = body.get("slots") or slots_val
        except Exception:
            pass
    else:
        try:
            form = await request.form()
            if not action_val:
                action_val = form.get("action")
            if not note_val:
                note_val = form.get("note") or form.get("reason")
            slots_list = form.getlist("slots")
            if slots_list:
                slots_val = slots_list
        except Exception:
            pass

    if not action_val:
        raise HTTPException(status_code=400, detail="Action is required.")

    action_clean = action_val.strip().lower()
    allowed_actions = {
        "approve",
        "edit_and_approve",
        "recapture",
        "escalate",
        "refer_to_siu",
        "field_inspection",
    }
    if action_clean not in allowed_actions:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action: {action_clean}. Allowed actions: {', '.join(sorted(allowed_actions))}.",
        )

    conn = get_connection()
    claim = conn.execute("SELECT * FROM claims WHERE id = ?", (id,)).fetchone()
    if not claim:
        conn.close()
        raise HTTPException(status_code=404, detail="Claim not found")

    status_map = {
        "approve": "approved",
        "edit_and_approve": "approved",
        "recapture": "recapture_requested",
        "escalate": "escalated_to_siu",
        "refer_to_siu": "escalated_to_siu",
        "field_inspection": "field_inspection",
    }
    new_status = status_map[action_clean]
    now_iso = datetime.now(timezone.utc).isoformat()
    clean_note = note_val.strip() if note_val and isinstance(note_val, str) and note_val.strip() else None

    if action_clean == "recapture":
        valid_selected = []
        if slots_val:
            if isinstance(slots_val, str):
                slots_val = [slots_val]
            valid_selected = [s for s in slots_val if s in VALID_SLOTS]
        if not valid_selected:
            valid_selected = list(VALID_SLOTS)

        session = conn.execute(
            "SELECT * FROM sessions WHERE claim_id = ? ORDER BY created_at DESC", (id,)
        ).fetchone()
        if session:
            retake_counts = json.loads(session["retake_counts"] or "{}")
            for s in valid_selected:
                retake_counts[s] = 0
            new_round = (session["round"] if "round" in session.keys() and session["round"] else 1) + 1
            with conn:
                conn.execute(
                    """
                    UPDATE sessions
                    SET status = 'open',
                        requested_slots = ?,
                        retake_counts = ?,
                        round = ?
                    WHERE token = ?
                    """,
                    (json.dumps(valid_selected), json.dumps(retake_counts), new_round, session["token"]),
                )
                conn.execute(
                    "UPDATE claims SET round = ? WHERE id = ?",
                    (new_round, id),
                )
            log_claim_event(
                id,
                "recapture_requested",
                f"Recapture requested for slots: {', '.join(valid_selected)}. Reason: {clean_note or 'None'}",
            )

    with conn:
        conn.execute(
            """
            INSERT INTO decisions (claim_id, action, note, timestamp)
            VALUES (?, ?, ?, ?)
            """,
            (id, action_clean, clean_note, now_iso),
        )
        conn.execute(
            "UPDATE claims SET status = ? WHERE id = ?", (new_status, id)
        )
    conn.close()

    if action_clean != "recapture":
        log_claim_event(
            id,
            "decision",
            f"Decision: {action_clean}. Note: {clean_note or 'None'}",
        )

    accept_header = request.headers.get("accept", "")
    if "application/json" in accept_header or request.headers.get("x-requested-with") == "XMLHttpRequest":
        return {"ok": True, "action": action_clean, "status": new_status, "note": clean_note}

    return RedirectResponse(url=f"/console/claims/{id}", status_code=303)


@app.post("/console/claims/{id}/recapture")
async def request_recapture(
    request: Request,
    id: str,
    slots: list[str] | None = Form(None),
    reason: str | None = Form(None),
    note: str | None = Form(None),
):
    return await make_decision(
        request=request,
        id=id,
        action="recapture",
        note=reason or note,
        slots=slots,
        reason=reason,
    )


@app.post("/console/claims/{id}/notes")
async def add_claim_note(
    request: Request,
    id: str,
    note: str | None = Form(None),
):
    note_val = note
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
            if isinstance(body, dict):
                note_val = body.get("note") or note_val
        except Exception:
            pass

    clean_note = (note_val or "").strip()
    if not clean_note:
        raise HTTPException(status_code=400, detail="Note cannot be empty.")

    conn = get_connection()
    claim = conn.execute("SELECT id FROM claims WHERE id = ?", (id,)).fetchone()
    if not claim:
        conn.close()
        raise HTTPException(status_code=404, detail="Claim not found")

    now_iso = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute(
            """
            INSERT INTO claim_notes (claim_id, author, note, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (id, "Adjuster", clean_note, now_iso),
        )
    conn.close()

    log_claim_event(id, "note", clean_note)

    accept_header = request.headers.get("accept", "")
    if "application/json" in accept_header or request.headers.get("x-requested-with") == "XMLHttpRequest":
        return {"ok": True, "note": clean_note, "created_at": now_iso}
    return RedirectResponse(url=f"/console/claims/{id}", status_code=303)


@app.get("/console/claims/{id}/timeline")
async def get_claim_timeline(id: str):
    conn = get_connection()
    claim = conn.execute("SELECT id FROM claims WHERE id = ?", (id,)).fetchone()
    if not claim:
        conn.close()
        raise HTTPException(status_code=404, detail="Claim not found")
    events = conn.execute(
        "SELECT * FROM claim_events WHERE claim_id = ? ORDER BY id ASC", (id,)
    ).fetchall()
    conn.close()
    return {"events": [dict(e) for e in events]}


@app.get("/verify", response_class=HTMLResponse)
async def verify_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="verify.html",
        context={
            "result_message": None,
            "result_status": None,
            "needs_decision_count": get_needs_decision_count(),
            "active_nav": "verify",
        },
    )


@app.post("/verify", response_class=HTMLResponse)
async def verify_package_file(
    request: Request, package_file: UploadFile = File(...)
):
    content = await package_file.read()
    is_valid = False
    try:
        data = json.loads(content.decode("utf-8"))
        pkg_str = data.get("package")
        sig = data.get("signature")
        if pkg_str and sig:
            pkg_bytes = pkg_str.encode("utf-8")
            is_valid = verify(pkg_bytes, sig)
    except Exception:
        is_valid = False

    if is_valid:
        result_message = "Valid signature"
        result_status = "valid"
    else:
        result_message = "Signature does not match: package was altered"
        result_status = "invalid"

    return templates.TemplateResponse(
        request=request,
        name="verify.html",
        context={
            "result_message": result_message,
            "result_status": result_status,
            "needs_decision_count": get_needs_decision_count(),
            "active_nav": "verify",
        },
    )
