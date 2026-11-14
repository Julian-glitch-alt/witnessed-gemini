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
    claims = conn.execute("SELECT * FROM claims ORDER BY created_at DESC").fetchall()
    conn.close()
    return templates.TemplateResponse(
        request=request, name="console_list.html", context={"claims": claims}
    )


@app.get("/console/new", response_class=HTMLResponse)
async def console_new_form(request: Request):
    return templates.TemplateResponse(
        request=request, name="console_new.html", context={}
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
    now_iso = datetime.now(timezone.utc).isoformat()

    new_total = prior_uploads + 1
    retakes_left = max(0, 4 - new_total)
    retake_counts = json.loads(session["retake_counts"] or "{}")
    retake_counts[slot] = new_total - 1

    with conn:
        conn.execute(
            """
            INSERT INTO photos (
                session_token, slot, sha256, path, reading_json, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token,
                slot,
                sha256,
                str(photo_path),
                reading_json,
                status,
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

    findings = run_checks(policy, readings, photos=photo_hashes)
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
            slot: (r.model_dump() if r and hasattr(r, "model_dump") else r)
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

    findings = conn.execute(
        "SELECT * FROM findings WHERE claim_id = ?", (id,)
    ).fetchall()
    decisions = conn.execute(
        "SELECT * FROM decisions WHERE claim_id = ? ORDER BY timestamp DESC", (id,)
    ).fetchall()
    conn.close()

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

    return templates.TemplateResponse(
        request=request,
        name="console_claim.html",
        context={
            "claim": claim,
            "session": session,
            "photos": photos,
            "findings": findings,
            "decisions": decisions,
            "summary_line": summary_line,
            "is_valid_seal": is_valid_seal,
            "ai_summary": ai_summary,
        },
    )


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
        context={"result_message": None, "result_status": None},
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
        },
    )
