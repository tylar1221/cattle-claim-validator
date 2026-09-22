# =========================================================================
# CAPTURE UPLOAD -- this endpoint is the whole reason the Matrix sheet's
# Live-capture scenarios can reach T1 now. See the comment on
# server_received_at below -- that one line is the actual Rank-1 anchor.
# =========================================================================
import hashlib
import json
import os
import uuid
import threading          # <-- ADD THIS

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from services.google_drive_service import GoogleDriveService, _safe_name

from fastapi import APIRouter, Depends, UploadFile, File, Form, BackgroundTasks

from services.db import get_conn
from services.auth import require_login
from services.exif_service import analyze_gallery_exif
from services.organize import compute_capture_flags, organize_case  # NEW -- shared flag logic + auto-organize

router = APIRouter(prefix="/api/captures", tags=["captures"])



# Rule Engine sheet has TWO SEPARATE checks that both use the same raw
# "server_received_at minus device_timestamp" number, but mean very
# different things -- conflating them was a real bug (caught while
# reviewing Matrix scenario #12): a genuine offline-queued upload from a
# few hours ago would otherwise get flagged identically to actual clock
# tampering, which is exactly the false-positive trap #12 warns about.
CLOCK_DRIFT_THRESHOLD_MS = 120_000        # "Device clock vs server time drift" -- ordinary skew
CAPTURE_UPLOAD_GAP_THRESHOLD_MS = 72 * 60 * 60 * 1000  # "Capture-to-upload gap" -- 72h, normal for field staff
ANCHOR_NTP_DRIFT_THRESHOLD_MS = 120_000   # "Device clock vs GNSS/NTP UTC drift" -- Rule Engine's OTHER 120s check,
                                           # measured at the moment the time-anchor was established, not since

# Module-level singleton -- the DriveService is expensive to build (reads
# the credential file, resolves the root folder ID on first use) and is
# thread-safe by design (thread-local service instances inside), so one
# shared instance is correct and cheapest.
_drive_service = None
_drive_lock = threading.Lock()

def _get_drive():
    global _drive_service
    if _drive_service is None:
        with _drive_lock:
            if _drive_service is None:
                _drive_service = GoogleDriveService()
    return _drive_service


def _upload_capture_to_drive(capture_id: int, contents: bytes, case_id: str,
                             step_id: str, drive_filename: str,
                             mime_type: str):
    """
    Background task: push one capture's bytes up to Drive and record the
    resulting file ID on the capture row.

    Runs AFTER the upload response has already been sent (see how it's
    scheduled below), so a slow Drive call never delays the person's
    upload. Nothing ever touches local disk -- `contents` is held in
    memory (this function is a BackgroundTask closure, so the bytes stay
    alive until this runs) and goes straight to Drive. If Drive is down,
    this capture's bytes are genuinely gone once this task finishes --
    see the honesty note on that tradeoff where this is scheduled below.
    """
    drive = _get_drive()
    if not drive.is_available():
        print(f"[captures] Drive not available -- capture {capture_id} stays local-only")
        return

    try:
        # Find/create the case folder. Domain lets us pick Live/ vs Dead/.
        domain = "live" if step_id.startswith("live_") else "dead"
        case_folder = drive.get_or_create_case_folder(case_id, domain=domain)
        if not case_folder:
            print(f"[captures] Could not get/create Drive folder for case {case_id}")
            return

        # Record the case folder link on the cases row (idempotent -- set
        # every time, cheap, and self-heals if the folder got recreated).
        conn = get_conn()
        conn.execute(
            "UPDATE cases SET drive_folder_id = ?, drive_folder_link = ? WHERE id = ?",
            (case_folder["folder_id"], case_folder["drive_link"], case_id),
        )
        conn.commit()
        conn.close()

        # Which subfolder does this specific step go in?
        target_folder_id = drive.folder_for_capture(case_folder, step_id)
        if not target_folder_id:
            print(f"[captures] No target folder resolved for step {step_id}")
            return

        # Push the bytes. Resumable upload via MediaFileUpload handles a
        # flaky connection better than a single-shot upload would.
        result = drive.upload_bytes(
            target_folder_id, contents, drive_filename, mime_type=mime_type
        )
        if not result:
            print(f"[captures] Drive upload failed for capture {capture_id}")
            return

        # Record the Drive location on the capture row. This is what the
        # frontend / organizers / future external systems read to find
        # the actual file.
        conn = get_conn()
        conn.execute(
            "UPDATE captures SET drive_file_id = ?, drive_file_link = ? WHERE id = ?",
            (result["file_id"], result["file_link"], capture_id),
        )
        conn.commit()
        conn.close()
        print(f"[captures] Drive upload OK: capture {capture_id} -> {result['file_link']}")

    except Exception as e:
        # ⚠️ Broad catch on purpose. This task is scheduled before
        # organize_case -- if it raised here, organize_case would never
        # run for this capture (BackgroundTasks chains abort on uncaught
        # exceptions, same failure mode as the OCR task's history).
        # A Drive failure must degrade gracefully, not silently break
        # the whole export pipeline.
        print(f"[captures] Drive upload crashed for capture {capture_id}: {e}")


        
@router.post("/upload")
async def upload_capture(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    case_id: str = Form(...),
    step_id: str = Form(...),
    source: str = Form(...),                      # 'camera' | 'gallery'
    device_timestamp: str | None = Form(None),      # ISO 8601, client's best-guess capture time
    device_time_source: str | None = Form(None),    # 'ntp' | 'device-fallback' | 'exif'
    device_ntp_drift_ms: int | None = Form(None),
    device_clock_changed: bool = Form(False),
    device_date_changed: bool = Form(False),          # NEW -- did the calendar DATE change mid-session, not just time-of-day
    device_date_wrong_at_anchor: bool = Form(False),   # NEW -- was the date already wrong when the time-anchor was first set up
    file_last_modified: int | None = Form(None),       # NEW -- the file's OWN filesystem timestamp (ms since epoch), independent of EXIF content
    lat: float | None = Form(None),
    lon: float | None = Form(None),
    gps_accuracy_m: float | None = Form(None),
    client_frame_hash: str | None = Form(None),   # SHA-256 hex, computed by the browser at the moment of capture
    device_timezone: str | None = Form(None),     # IANA name, e.g. "Asia/Kolkata", from Intl.DateTimeFormat
    resolution: str | None = Form(None),           # NEW -- e.g. "1706 × 1280", was shown on screen but never sent before
    device_info: str | None = Form(None),          # NEW -- e.g. "Android · Chrome 152", same gap
    tamper_check_score: int | None = Form(None),   # NEW -- the client-side ELA score, same gap
    tamper_check_band: str | None = Form(None),    # NEW -- 'low' | 'uncertain' | 'elevated'
    normalize_scale_applied: float | None = Form(None),   # NEW -- Pixel-Size Normalization, infra only, see static/index.html's NORMALIZE_CONFIG
    normalize_skipped: bool | None = Form(None),           # NEW
    normalize_skip_reason: str | None = Form(None),        # NEW
    user=Depends(require_login),

):
    # ---------------------------------------------------------------
    # THE ACTUAL RANK-1 ANCHOR. This is read from the server process's
    # own clock, right now, at the moment these bytes arrived -- not from
    # anything in the request body, which the client fully controls and
    # could set to whatever it wants. This single line is what every T1
    # cell in the Matrix sheet's Live column actually depends on.
    # ---------------------------------------------------------------
    server_received_at = datetime.now(timezone.utc)

    ext = os.path.splitext(file.filename or "")[1] or ".jpg"
    stored_name = f"{uuid.uuid4().hex}{ext}"   # kept only as a stable identifier for this capture's Drive filename below -- no longer a disk path
    contents = await file.read()

    # Frame integrity hash -- Matrix scenario #5 Live's "close the gap"
    # fix. Recomputed HERE from the bytes actually received, independent
    # of whatever the client claims, so a client that lies about its own
    # hash would need the ACTUAL bytes to match anyway for this to pass.
    # Flag-only, never reject -- a mismatch is a strong signal, but this
    # is new code and a false positive here should never be able to block
    # a legitimate upload.
    server_frame_hash = hashlib.sha256(contents).hexdigest()
    frame_hash_match = None
    if client_frame_hash:
        frame_hash_match = (client_frame_hash.lower() == server_frame_hash.lower())

    # Matrix scenario #3 Gallery -- "duplicate-hash lookup against
    # previously submitted images". The hash was already being computed
    # for a different reason (#5's integrity check) but was never actually
    # checked against anything else in the database until now. Catches
    # the same exact photo file being resubmitted -- across ANY case, not
    # just this one, since fraud often means reusing one real photo across
    # multiple different claims. Flag-only: a genuine duplicate could also
    # be an honest re-upload after a mistake, not necessarily fraud.
    conn_dupe = get_conn()
    duplicate_row = conn_dupe.execute(
        "SELECT id, case_id, step_id, source, server_received_at FROM captures "
        "WHERE server_frame_hash = ? LIMIT 1",
        (server_frame_hash,),
    ).fetchone()
    conn_dupe.close()
    duplicate_of = dict(duplicate_row) if duplicate_row else None

    # Matrix scenario #7 Live -- compare the browser's own reported IANA
    # timezone (Intl.DateTimeFormat, e.g. "Asia/Kolkata") against a coarse
    # estimate of what the GPS coordinates imply. Uses Python's built-in
    # zoneinfo for a REAL, precise offset for the device's claimed
    # timezone -- but the "what the coordinates imply" side is still only
    # a longitude/15 estimate, not a real timezone-boundary lookup, so the
    # tolerance below is deliberately wide to avoid false positives near
    # timezone edges (same caveat as the Gallery EXIF offset check).
    # Soft flag only, per the Matrix sheet: a wrong-but-consistent offset
    # is usually just an honest traveller, not tampering.
    TIMEZONE_MISMATCH_TOLERANCE_HOURS = 2.5
    timezone_mismatch_flag = None
    if device_timezone and lat is not None and lon is not None:
        try:
            tz = ZoneInfo(device_timezone)
            actual_offset_hours = datetime.now(tz).utcoffset().total_seconds() / 3600
            expected_offset_hours = round(lon / 15 * 2) / 2  # nearest half-hour
            timezone_mismatch_flag = abs(actual_offset_hours - expected_offset_hours) > TIMEZONE_MISMATCH_TOLERANCE_HOURS
        except (ZoneInfoNotFoundError, ValueError):
            pass  # unrecognized timezone name from the browser -- skip rather than guess

    # Server-side EXIF Detection Signals -- gallery uploads only, and only
    # for images (a video file has no EXIF to speak of, and this endpoint
    # also receives .webm blobs from the Video step's gallery-upload path).
    # This is the AUTHORITATIVE version of the check -- unlike the earlier
    # client-side EXIF read used for display, this can't be bypassed by
    # tampering with the browser, since it runs here on the server against
    # the actual bytes received.
    exif_signals_json = None
    is_image = (file.content_type or "").startswith("image/") or ext.lower() in (".jpg", ".jpeg", ".png", ".heic", ".webp")
    if source == "gallery" and is_image:
        try:
            signals = analyze_gallery_exif(contents, server_received_at=server_received_at, file_last_modified=file_last_modified)
            exif_signals_json = json.dumps(signals)
        except Exception as e:
            # Analysis failing should never break the upload itself --
            # record that it failed as its own signal instead.
            exif_signals_json = json.dumps({
                "exif_present": False,
                "flags": [{"signal": "analysis_error", "weight": "Low", "detail": str(e)}],
                "info": {},
            })

    # NEW -- server-side ear tag digit OCR (services/eartag_ocr_service.py).
    # ⚠️ FIX #2, found via real testing: an earlier version awaited this
    # synchronously via run_in_threadpool -- that stopped OCR from
    # freezing the WHOLE server (fix #1), but this capture's OWN response
    # still didn't return until OCR finished, and two inference passes
    # (0°/180° retry) plus preprocessing can run past the client's
    # 8-second upload timeout. When that happened, the client treated an
    # upload the server actually completed as failed, re-queued it, and
    # every retry minted a fresh case -- caught by organized_exports
    # filling with duplicate case folders while the client's "queued"
    # counter never drained. Now OCR runs fully as a background task
    # (added below, after we have capture_id) -- the response returns
    # immediately regardless of how long OCR takes, and the result gets
    # written to this row afterward. Only the step-id check happens here.
    #
    # step_id here is actually captureKey(step) from the frontend
    # (static/index.html's captureKey() = step.domain + "_" + step.id),
    # NOT the bare step.id -- so the real values in the wild are
    # "live_ear_tag_live" and "dead_ear_tag", not "ear_tag_live"/"ear_tag".
    # NEW -- "live_eardemo_live" added: a testing-only step (no client-
    # side detection gate, freeform capture like Owner Photo/Scar-Injury)
    # to let OCR be tested through the real live-capture flow without
    # fighting the ear_tag step's detection requirements.

    # Cross-check: how far does the client's claimed capture time sit from
    # the moment we actually received it? Computed HERE, not trusted from
    # the client, since a client that lies about its timestamp would also
    # lie about its own drift measurement.
    drift_ms = None
    if device_timestamp:
        try:
            dt = datetime.fromisoformat(device_timestamp.replace("Z", "+00:00"))
            drift_ms = int((server_received_at - dt).total_seconds() * 1000)
        except ValueError:
            drift_ms = None  # malformed timestamp from client -- don't crash the upload over it

    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO captures (
            filename, case_id, step_id, source, server_received_at,
            device_timestamp, device_time_source, device_ntp_drift_ms,
            device_clock_changed, lat, lon, gps_accuracy_m,
            drift_server_vs_device_ms, exif_signals,
            client_frame_hash, server_frame_hash, frame_hash_match,
            device_timezone, timezone_mismatch_flag,
            device_date_changed, device_date_wrong_at_anchor,
            resolution, device_info, tamper_check_score, tamper_check_band,
            normalize_scale_applied, normalize_skipped, normalize_skip_reason
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        RETURNING id""",
        (
            stored_name, case_id, step_id, source,
            server_received_at.isoformat(),
            device_timestamp, device_time_source, device_ntp_drift_ms,
            1 if device_clock_changed else 0,
            lat, lon, gps_accuracy_m,
            drift_ms, exif_signals_json,
            client_frame_hash, server_frame_hash,
            None if frame_hash_match is None else (1 if frame_hash_match else 0),
            device_timezone,
            None if timezone_mismatch_flag is None else (1 if timezone_mismatch_flag else 0),
            1 if device_date_changed else 0,
            1 if device_date_wrong_at_anchor else 0,
            resolution, device_info, tamper_check_score, tamper_check_band,
            normalize_scale_applied,
            None if normalize_skipped is None else (1 if normalize_skipped else 0),
            normalize_skip_reason,
        ),
    )
    capture_id = cur.fetchone()["id"]
    conn.commit()
    conn.close()

    # ---------------------------------------------------------------
    # BACKGROUND TASK ORDER MATTERS -- FastAPI runs BackgroundTasks
    # sequentially, in the order they're added, so this ordering is
    # deliberate:
    #   1. Drive upload  -- records drive_file_id on the row FIRST, so
    #                       that organize_case (which reads the row) can
    #                       see the Drive link when it builds the summary
    #   2. OCR           -- records ocr_signals on the row, so
    #                       organize_case sees it
    #   3. organize_case -- rebuilds the case summary from a DB where
    #                       both of the above are already present
    # Getting this order wrong produces summaries missing the Drive
    # link or the OCR line on EVERY upload, not just occasionally.
    # ---------------------------------------------------------------
    drive_filename = f"{_safe_name(step_id)}_{capture_id}{ext}"
    mime = file.content_type or ("video/webm" if ext == ".webm" else "image/jpeg")
    background_tasks.add_task(
        _upload_capture_to_drive,
        capture_id, contents, case_id, step_id, drive_filename, mime,
    )

    
    background_tasks.add_task(organize_case, case_id)
    # NEW -- surface whatever case-folder link we already have on record.
    # On the FIRST capture of a case this will be None (the folder is
    # created in the background task just scheduled above); on every later
    # capture it's already populated. The frontend also re-fetches via
    # GET /api/cases/{case_id}/drive_link once the background task finishes.
    conn_link = get_conn()
    link_row = conn_link.execute(
        "SELECT drive_folder_id, drive_folder_link FROM cases WHERE id = ?",
        (case_id,),
    ).fetchone()
    conn_link.close()
    case_drive_folder_id = link_row["drive_folder_id"] if link_row else None
    case_drive_folder_link = link_row["drive_folder_link"] if link_row else None

    return {
        "id": capture_id,
        "filename": stored_name,
        "server_received_at": server_received_at.isoformat(),
        "drift_server_vs_device_ms": drift_ms,
        "drive_upload_pending": True,
        "case_drive_folder_id": case_drive_folder_id,       # NEW
        "case_drive_folder_link": case_drive_folder_link,   # NEW   # NEW -- the actual Drive link lands a moment later, see drive_file_link on GET /{case_id}
        # THREE separate signals now, not one conflated "drift_flag":
        #
        # 1. clock_tamper_flag -- the RELIABLE signal. Comes from the
        #    client's own monotonic-clock tracking (device_clock_changed),
        #    which detects an actual wall-clock EDIT since the session's
        #    time anchor was established -- true regardless of how large
        #    or small the resulting gap is. This is what scenario #6/#11
        #    actually need.
        #
        # 2. clock_drift_flag -- ordinary short-range clock inaccuracy
        #    (>120s, <=72h). Flag only, matches the Rule Engine's "server
        #    time is authoritative anyway" rationale -- never implies
        #    tampering on its own.
        #
        # 3. capture_upload_gap_flag -- a LARGE gap (>72h). This is
        #    scenario #12's honest case: a field worker capturing offline
        #    and uploading days later. Explicitly NOT labeled as
        #    suspicious -- flagged only "for review", per the Rule Engine
        #    sheet's own wording, so the UI can show a neutral note
        #    instead of a tamper-style warning.
        "clock_tamper_flag": bool(device_clock_changed),
        # NEW: purely cosmetic refinements to the message above, NOT new
        # security signals -- clock_tamper_flag/anchor_drift_flag remain
        # the actual authoritative flags either way. These just let the
        # UI say "Date changed" instead of the more generic "Clock
        # changed" when that's specifically what happened (crossed a
        # calendar-day boundary, not just a same-day time adjustment).
        "date_changed": bool(device_date_changed),
        "date_wrong_at_anchor": bool(device_date_wrong_at_anchor),
        # NEW: the gap this fixes -- when device_time_source is
        # 'device-fallback', the client had NO server-verified time
        # reference at all when this capture happened (weak/no signal at
        # the moment the session's anchor was established). Crucially,
        # device_ntp_drift_ms is always 0 in that case -- not because the
        # clock was verified accurate, but because there was nothing to
        # compare it against. Without this explicit flag, a
        # pre-tampered clock during a weak-signal session would pass
        # every other check silently, which is exactly the risk the
        # Matrix sheet's scenario #2 Live calls out by name.
        "unanchored_flag": (device_time_source == "device-fallback"),
        # NEW: was the device's raw wall clock already wrong at the moment
        # the NTP time-anchor was established (not changed mid-session --
        # wrong from the start)? This is the ACTUAL Rule Engine check for
        # "device clock vs GNSS/NTP UTC drift" -- previously computed on
        # the client and sent as device_ntp_drift_ms, but never checked
        # against anything. A manually-set clock at page load, with no
        # further change during the session, produces exactly this
        # pattern: clock_tamper_flag stays false (nothing changed SINCE
        # the anchor) while this catches it instead.
        "anchor_drift_flag": (
            device_ntp_drift_ms is not None
            and abs(device_ntp_drift_ms) > ANCHOR_NTP_DRIFT_THRESHOLD_MS
        ),
        "clock_drift_flag": (
            drift_ms is not None
            and CLOCK_DRIFT_THRESHOLD_MS < abs(drift_ms) <= CAPTURE_UPLOAD_GAP_THRESHOLD_MS
        ),
        "capture_upload_gap_flag": (
            drift_ms is not None and abs(drift_ms) > CAPTURE_UPLOAD_GAP_THRESHOLD_MS
        ),
        "exif_signals": json.loads(exif_signals_json) if exif_signals_json else None,
        "server_frame_hash": server_frame_hash,
        "frame_hash_match": frame_hash_match,
        "timezone_mismatch_flag": timezone_mismatch_flag,
        "duplicate_image_flag": duplicate_of is not None,
        "duplicate_of": duplicate_of,  # {id, case_id, step_id, server_received_at} of the earlier submission, or None
        "normalize_scale_applied": normalize_scale_applied,   # NEW -- Pixel-Size Normalization, infra only
        "normalize_skipped": normalize_skipped,
        "normalize_skip_reason": normalize_skip_reason,
    }


@router.post("/{capture_id}/tamper_check")
async def update_tamper_check(capture_id: int, tamper_check_score: int = Form(...), tamper_check_band: str = Form(...), user=Depends(require_login)):
    """
    NEW -- a small, separate update, NOT a re-upload. The Tamper Check
    score is computed in the browser in a background step that runs AFTER
    the photo/file upload already started (by design, so heavy pixel
    analysis never delays the visible capture) -- meaning it genuinely
    isn't ready yet at the moment of the original upload. Re-sending the
    whole file again once it's ready would recreate the exact duplicate-
    upload bug found and fixed earlier; this just updates the two relevant
    columns on the ALREADY-uploaded row instead.
    """
    conn = get_conn()
    row = conn.execute("SELECT case_id FROM captures WHERE id = ?", (capture_id,)).fetchone()
    if not row:
        conn.close()
        return {"error": "not found"}
    conn.execute(
        "UPDATE captures SET tamper_check_score = ?, tamper_check_band = ? WHERE id = ?",
        (tamper_check_score, tamper_check_band, capture_id),
    )
    conn.commit()
    case_id = row["case_id"]
    conn.close()
    organize_case(case_id)  # refresh the organized-folder summary with the now-complete data
    return {"ok": True}


@router.get("/{case_id}")
async def list_captures_for_case(case_id: str, user=Depends(require_login)):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM captures WHERE case_id = ? ORDER BY id", (case_id,)
    ).fetchall()
    # NEW -- each capture now includes BOTH the raw stored columns AND the
    # same computed flags shown on the webpage (Clock Integrity, Duplicate
    # Check, etc.), recomputed fresh from stored data via compute_capture_flags
    # above. Previously this only returned raw columns -- exactly the same
    # data underneath, but nothing here actually turned it into the flags a
    # reviewer would want, even though every capture upload already computed
    # them once, just never persisted or exposed again after that first
    # response.
    result = []
    for r in rows:
        row_dict = dict(r)
        row_dict["computed_flags"] = compute_capture_flags(row_dict, conn)
        result.append(row_dict)
    conn.close()
    return result
