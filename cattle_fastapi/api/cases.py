import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request, BackgroundTasks

from services.db import get_conn
from services.organize import compute_capture_flags, organize_case  # NEW -- organize_case call added below
from services.auth import require_login

router = APIRouter(prefix="/api/cases", tags=["cases"])


@router.post("")
async def create_case(
    domain: str = Form(...),                # 'live' | 'dead'
    loan_no: str | None = Form(None),
    farmer_name: str | None = Form(None),
    village: str | None = Form(None),
    case_id: str | None = Form(None),        # NEW -- client-generated, so a case can exist
    user=Depends(require_login),                                          # locally even before the server has ever seen it
                                              # (needed for multiple animals captured entirely
                                              # offline, syncing all at once later)
):
    conn = get_conn()
    if case_id:
        # Idempotent: the client may call this more than once for the SAME
        # case (e.g. every retry attempt tries to make sure the case
        # exists) -- if it's already there, just confirm it, don't error
        # or create a duplicate.
        existing = conn.execute("SELECT id, domain FROM cases WHERE id = ?", (case_id,)).fetchone()
        if existing:
            conn.close()
            return {"case_id": existing["id"], "domain": existing["domain"]}
    else:
        case_id = "case_" + uuid.uuid4().hex[:12]

    conn.execute(
        "INSERT INTO cases (id, domain, loan_no, farmer_name, village, created_by_user_id) VALUES (?,?,?,?,?,?)",
        (case_id, domain, loan_no, farmer_name, village, user["id"]),
    )
    conn.commit()
    conn.close()
    return {"case_id": case_id, "domain": domain}

@router.get("/{case_id}/drive_link")
async def get_case_drive_link(case_id: str, user=Depends(require_login)):
    """
    Returns the case's main Drive folder link (the one containing Live/,
    Dead/, Forensics/, Case Summary.txt, Case Summary.json).

    `ready` is False while the folder hasn't been created yet -- it gets
    created on the first successful capture upload (see the background
    task in api/captures.py), so this can be polled briefly after the
    first upload until it flips to True.
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT drive_folder_id, drive_folder_link FROM cases WHERE id = ?",
        (case_id,),
    ).fetchone()
    conn.close()
    if not row:
        return {"error": "not found", "ready": False}
    return {
        "case_id": case_id,
        "drive_folder_id": row["drive_folder_id"],
        "drive_folder_link": row["drive_folder_link"],
        "ready": bool(row["drive_folder_link"]),
    }
@router.get("/{case_id}")
async def get_case(case_id: str, user=Depends(require_login)):
    conn = get_conn()
    case = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    captures = conn.execute(
        "SELECT * FROM captures WHERE case_id = ? ORDER BY id", (case_id,)
    ).fetchall()
    if not case:
        conn.close()
        return {"error": "not found"}
    # NEW -- every capture now also carries the same computed flags shown on
    # the webpage (Clock Integrity, Duplicate Check, EXIF Integrity, etc.),
    # not just the raw stored columns. This is the natural "get everything
    # about a case" endpoint, so it's the one worth making fully complete.
    captures_out = []
    for c in captures:
        c_dict = dict(c)
        c_dict["computed_flags"] = compute_capture_flags(c_dict, conn)
        captures_out.append(c_dict)
    conn.close()
    return {"case": dict(case), "captures": captures_out}


# All Case Details form fields -- matches the original app's CD_FIELD_IDS
# list exactly (minus the cd_ prefix), so the frontend can send this as a
# single flat form regardless of which fields the person actually filled in.
CASE_DETAIL_FIELDS = [
    "loan_no", "farmer_name", "village", "taluka", "district", "occupation",
    "insurer_org", "remarks", "survey_date", "address", "sub_case_status",
    "animal_type", "age", "gender", "breed", "tag_no", "market_value", "color",
    "swish_of_tail", "right_horn", "left_horn", "lactation", "daily_milk",
    "distinguishing_feature", "sum_insured", "policy_duration", "premium_amt",
]


@router.put("/{case_id}")
async def update_case_details(case_id: str, request: Request, background_tasks: BackgroundTasks, user=Depends(require_login)):
    form = await request.form()
    conn = get_conn()
    existing = conn.execute("SELECT id FROM cases WHERE id = ?", (case_id,)).fetchone()
    if not existing:
        conn.close()
        return {"error": "not found"}

    updates = {f: form.get(f, "") for f in CASE_DETAIL_FIELDS if f in form}
    if updates:
        # NEW -- explicit, timezone-aware UTC (same pattern as
        # server_received_at in api/captures.py), not SQLite's own
        # datetime('now') default -- that produces an ambiguous naive
        # string with no timezone marker, which is exactly the kind of
        # thing that made the ORIGINAL "Created" timestamp confusing to
        # begin with. See services/db.py's updated_at column comment for
        # why this field exists at all.
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE cases SET {set_clause} WHERE id = ?", (*updates.values(), case_id))
        conn.commit()

    case = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    conn.close()
    # ⚠️ FIX, found via real testing: Case Details (Farmer name, Village,
    # etc.) updated the database correctly here, but organized_exports/
    # <case>/case_summary.txt only ever got regenerated by a NEW capture
    # upload (see api/captures.py) -- never by editing case details
    # themselves. Filling in details after photos were already captured
    # left the exported summary permanently stale (still showing
    # "(not filled in)") until another photo happened to be uploaded
    # later. Calling organize_case here too keeps the export current
    # the moment details are saved, same as it already is for captures.
    # ⚠️ FIX: same background-task pattern api/captures.py already uses
    # for this -- organize_case does real file I/O (copying photos,
    # writing the summary), and running it synchronously here would make
    # every Case Details save wait on that instead of returning
    # immediately, the same class of issue OCR blocking the event loop
    # caused earlier (see eartag_ocr_service.py's history) even though
    # this one's lighter-weight. Scheduling it as a background task keeps
    # the save itself fast while still keeping the export current.
    background_tasks.add_task(organize_case, case_id)
    return {"case": dict(case)}
