import base64
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import random
import secrets
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import qrcode

from app.db import get_connection, get_data_dir, init_db
from witnessed.checks import run_checks
from witnessed.providers import get_provider
from witnessed.schemas import (
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

    reds = [f for f in claim_findings if f["status"] == "red"]
    if reds:
        return ("mismatch", 0, reds[0]["reason"])

    ambers = [f for f in claim_findings if f["status"] == "amber"]
    if ambers:
        return ("review", 1, ambers[0]["reason"])

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

    issues = [_check(f) for f in findings if _status(f) in ("red", "amber")]
    if issues:
        return f"{len(issues)} issue(s) need review: {', '.join(issues)}"
    return "No issues found by the automated checks. The adjuster makes the decision."


def generate_qr_data_uri(data_url: str) -> str:
    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(data_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"


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

    days = ["Wed", "Thu", "Fri", "Sat", "Sun", "Mon", "Tue"]
    chart_days = []
    for d in days:
        chart_days.append({
            "day": d,
            "total": 0,
            "clear": 0,
            "review": 0,
            "mismatch": 0,
            "waiting": 0,
            "height_pct": 10,
        })
    if chart_days:
        last_day = chart_days[-1]
        last_day["total"] = total_claims
        last_day["clear"] = sum(1 for c in claim_items if c["lane"] == "clear")
        last_day["review"] = sum(1 for c in claim_items if c["lane"] == "review")
        last_day["mismatch"] = mismatch_count
        last_day["waiting"] = waiting_claimant_count
        last_day["height_pct"] = min(100, max(24, total_claims * 24))

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
    claimant_statement: str = Form(...),
    claimed_damage_area: str = Form(...),
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

    if session["status"] == "submitted":
        conn.close()
        return templates.TemplateResponse(
            request=request,
            name="c_submitted.html",
            context={"session_code": session["session_code"]},
        )

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

    retake_counts = json.loads(session["retake_counts"] or "{}")

    return templates.TemplateResponse(
        request=request,
        name="capture.html",
        context={
            "token": token,
            "session_code": session["session_code"],
            "existing_photos": existing_photos,
            "retake_counts": retake_counts,
        },
    )


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

    provider = get_provider()
    reading = provider.read(slot, photo_bytes)
    reading_json = (
        reading.model_dump_json()
        if hasattr(reading, "model_dump_json")
        else (json.dumps(reading) if reading else None)
    )
    status = "read" if reading is not None else "unchecked"
    model_used = getattr(provider, "last_model", None) or (
        "fake" if isinstance(provider, FakeProvider) else "gemini"
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
                session_token, slot, sha256, path, reading_json, status, model, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token,
                slot,
                sha256,
                str(photo_path),
                reading_json,
                status,
                model_used,
                now_iso,
            ),
        )
        conn.execute(
            "UPDATE sessions SET retake_counts = ? WHERE token = ?",
            (json.dumps(retake_counts), token),
        )
    conn.close()

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

    latest_photos: dict[str, Any] = {}
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

    policy = Policy(
        make=claim["make"],
        model=claim["model"],
        color=claim["color"],
        plate=claim["plate"],
        state=claim["state"],
        claimant_name=claim["claimant_name"],
        claimant_statement=claim["claimant_statement"],
        claimed_damage_area=claim["claimed_damage_area"],
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

    package_data = {
        "claim": {
            "id": claim["id"],
            "claimant_name": claim["claimant_name"],
            "claimant_statement": claim["claimant_statement"],
            "claimed_damage_area": claim["claimed_damage_area"],
        },
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
                    else "fake"
                ),
                "reading": (r.model_dump() if r and hasattr(r, "model_dump") else r),
            }
            for slot, r in readings.items()
        },
        "session_code": session["session_code"],
        "timestamps": {
            "created_at": claim["created_at"],
            "submitted_at": now_iso,
        },
    }

    pkg_bytes, sig_b64 = seal(package_data)

    provider = get_provider()
    ai_summary_dict = summarize(findings, provider)
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
        conn.execute(
            """
            UPDATE claims
            SET status = 'ready_for_review', package_bytes = ?, signature = ?, ai_summary = ?
            WHERE id = ?
            """,
            (pkg_bytes, sig_b64, ai_summary_json, claim["id"]),
        )
        conn.execute(
            "UPDATE sessions SET status = 'submitted' WHERE token = ?", (token,)
        )
    conn.close()

    accept_header = request.headers.get("accept", "")
    if "application/json" in accept_header:
        return {"ok": True, "redirect": f"/c/{token}/complete"}
    return RedirectResponse(url=f"/c/{token}/complete", status_code=303)


@app.get("/c/{token}/complete", response_class=HTMLResponse)
async def capture_complete(request: Request, token: str):
    conn = get_connection()
    session = conn.execute(
        "SELECT * FROM sessions WHERE token = ?", (token,)
    ).fetchone()
    conn.close()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return templates.TemplateResponse(
        request=request,
        name="c_complete.html",
        context={"session_code": session["session_code"]},
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
        provider = get_provider()
        ai_summary = summarize(findings, provider)

    group_definitions = [
        {
            "id": "vehicle_and_plate",
            "title": "1. Vehicle and plate",
            "checks": ["plate_match", "vehicle_match", "vin_format"],
        },
        {
            "id": "damage",
            "title": "2. Damage",
            "checks": ["damage_present", "damage_location"],
        },
        {
            "id": "photo_integrity",
            "title": "3. Photo integrity",
            "checks": ["duplicate_photo", "photo_reused"],
        },
    ]

    all_numbered_findings = []
    groups = []
    finding_num = 1
    slot_markers: dict[str, int] = {}
    slot_borders: dict[str, str] = {}

    check_to_slot = {
        "plate_match": "plate",
        "vehicle_match": "wide",
        "vin_format": "vin",
        "damage_present": "damage",
        "damage_location": "damage",
    }

    seen_checks = set()
    for g_def in group_definitions:
        g_findings = []
        for chk in g_def["checks"]:
            f_row = next((f for f in findings_rows if f["check_name"] == chk), None)
            if f_row:
                seen_checks.add(chk)
                item = {
                    "number": finding_num,
                    "check_name": f_row["check_name"],
                    "status": f_row["status"],
                    "reason": f_row["reason"],
                    "adjuster_label": (
                        f_row["adjuster_label"]
                        if "adjuster_label" in f_row.keys()
                        else None
                    ),
                }
                g_findings.append(item)
                all_numbered_findings.append(item)

                slot = check_to_slot.get(chk)
                if slot and item["status"] in ("red", "amber"):
                    if slot not in slot_borders or item["status"] == "red":
                        slot_borders[slot] = item["status"]
                        slot_markers[slot] = finding_num
                elif chk in ("duplicate_photo", "photo_reused") and item["status"] in ("red", "amber"):
                    for s in ("wide", "damage", "plate", "vin"):
                        if s in item["reason"] or chk == "duplicate_photo":
                            if s not in slot_borders or item["status"] == "red":
                                slot_borders[s] = item["status"]
                                slot_markers[s] = finding_num

                finding_num += 1

        if any(f["status"] == "red" for f in g_findings):
            g_status = "red"
            g_status_label = "Mismatch"
        elif any(f["status"] == "amber" for f in g_findings):
            g_status = "amber"
            g_status_label = "Review"
        elif any(f["status"] == "green" for f in g_findings):
            g_status = "green"
            g_status_label = "Match"
        else:
            g_status = "unchecked"
            g_status_label = "Unchecked"

        groups.append({
            "id": g_def["id"],
            "title": g_def["title"],
            "findings": g_findings,
            "status": g_status,
            "status_label": g_status_label,
        })

    other_findings = [f for f in findings_rows if f["check_name"] not in seen_checks]
    if other_findings:
        g_findings = []
        for f_row in other_findings:
            item = {
                "number": finding_num,
                "check_name": f_row["check_name"],
                "status": f_row["status"],
                "reason": f_row["reason"],
                "adjuster_label": (
                    f_row["adjuster_label"]
                    if "adjuster_label" in f_row.keys()
                    else None
                ),
            }
            g_findings.append(item)
            all_numbered_findings.append(item)
            finding_num += 1
        groups.append({
            "id": "other",
            "title": f"{len(groups) + 1}. Other checks",
            "findings": g_findings,
            "status": "amber" if any(f["status"] in ("red", "amber") for f in g_findings) else "green",
            "status_label": "Review" if any(f["status"] in ("red", "amber") for f in g_findings) else "Match",
        })

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

    submitted_date_str = "N/A"
    ready_date_str = "N/A"
    if claim["created_at"]:
        try:
            dt = datetime.fromisoformat(claim["created_at"])
            hour_str = dt.strftime("%I:%M %p").lstrip("0")
            submitted_date_str = f"{dt.strftime('%b')} {dt.day}, {dt.year}, {hour_str}"
            ready_date_str = f"{dt.strftime('%b')} {dt.day}, {dt.year}, {hour_str}"
        except Exception:
            submitted_date_str = claim["created_at"][:10]
            ready_date_str = claim["created_at"][:10]

    sig_short = claim["signature"][:8] if claim["signature"] else ""

    return templates.TemplateResponse(
        request=request,
        name="console_claim.html",
        context={
            "claim": claim,
            "session": session,
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
    id: str, action: str = Form(...), note: str | None = Form(None)
):
    action_clean = action.strip().lower()
    if action_clean not in {"approve", "recapture", "escalate"}:
        raise HTTPException(
            status_code=400,
            detail="Invalid action. Allowed actions are: approve, recapture, escalate.",
        )

    conn = get_connection()
    claim = conn.execute("SELECT * FROM claims WHERE id = ?", (id,)).fetchone()
    if not claim:
        conn.close()
        raise HTTPException(status_code=404, detail="Claim not found")

    status_map = {
        "approve": "approved",
        "recapture": "recapture_requested",
        "escalate": "escalated_to_siu",
    }
    new_status = status_map[action_clean]
    now_iso = datetime.now(timezone.utc).isoformat()

    with conn:
        conn.execute(
            """
            INSERT INTO decisions (claim_id, action, note, timestamp)
            VALUES (?, ?, ?, ?)
            """,
            (id, action_clean, note.strip() if note else None, now_iso),
        )
        conn.execute(
            "UPDATE claims SET status = ? WHERE id = ?", (new_status, id)
        )
    conn.close()

    return RedirectResponse(url=f"/console/claims/{id}", status_code=303)


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
