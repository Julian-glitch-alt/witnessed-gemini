from app.db import get_connection
from app.seed import seed


def test_seed_creates_three_claims_and_finishes_flow():
    claim_ids = seed()
    assert len(claim_ids) == 3

    conn = get_connection()
    claims = conn.execute("SELECT * FROM claims").fetchall()
    c_map = {c["id"]: c for c in claims}

    # All 3 claims exist and are ready for review with package and signature
    for cid in claim_ids:
        assert cid in c_map
        assert c_map[cid]["status"] == "ready_for_review"
        assert c_map[cid]["package_bytes"] is not None
        assert c_map[cid]["signature"] is not None

    # Claim (a) is all green
    findings_a = conn.execute(
        "SELECT * FROM findings WHERE claim_id = ?", (claim_ids[0],)
    ).fetchall()
    statuses_a = [f["status"] for f in findings_a]
    assert all(s == "green" for s in statuses_a)

    # Claim (b) has plate_match red
    findings_b = conn.execute(
        "SELECT * FROM findings WHERE claim_id = ?", (claim_ids[1],)
    ).fetchall()
    plate_f = next(f for f in findings_b if f["check_name"] == "plate_match")
    assert plate_f["status"] == "red"

    # Claim (c) has damage_location amber
    findings_c = conn.execute(
        "SELECT * FROM findings WHERE claim_id = ?", (claim_ids[2],)
    ).fetchall()
    dmg_f = next(f for f in findings_c if f["check_name"] == "damage_location")
    assert dmg_f["status"] == "amber"

    conn.close()
