# =========================================================================
# Two things live here together, on purpose:
#   1. compute_capture_flags -- the same Clock Integrity / Duplicate Check /
#      etc. computation used everywhere else in the backend.
#   2. organize_case -- builds a case's readable summary (.txt/.json) and
#      pushes it to Drive alongside the case folder. NO local disk
#      involvement at all -- the summaries are built as strings in memory
#      and uploaded directly via GoogleDriveService.upload_text. Photos
#      themselves already went straight to Drive from api/captures.py's
#      background task; this function only ever handles the summary docs.
# =========================================================================
import json
import re
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from services.db import get_conn

CLOCK_DRIFT_THRESHOLD_MS = 120_000
CAPTURE_UPLOAD_GAP_THRESHOLD_MS = 72 * 60 * 60 * 1000
ANCHOR_NTP_DRIFT_THRESHOLD_MS = 120_000

# ⚠️ NEW -- one lock per case_id. organize_case() is scheduled as a
# BackgroundTask after EVERY capture upload, so several captures landing
# close together (common on a fast connection) fire off several
# organize_case() calls concurrently for the SAME case. GoogleDriveService
# .upload_text() avoids duplicate files by searching Drive for an existing
# file by name before creating one -- but that search has a real
# propagation-delay race: two concurrent calls can both run their search
# before either one's create() becomes visible, so both conclude "nothing
# there yet" and both create a file. This is what produced duplicate
# Case Summary.txt / .json files. Serializing all Drive-summary writes for
# a given case_id through one lock closes that race.
_case_locks: dict[str, threading.Lock] = {}
_case_locks_guard = threading.Lock()


def _lock_for_case(case_id: str) -> threading.Lock:
    with _case_locks_guard:
        if case_id not in _case_locks:
            _case_locks[case_id] = threading.Lock()
        return _case_locks[case_id]


def _format_ist(timestamp_str):
    if not timestamp_str:
        return timestamp_str
    try:
        if isinstance(timestamp_str, datetime):
            dt = timestamp_str
        else:
            dt = datetime.fromisoformat(str(timestamp_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %I:%M:%S %p IST")
    except (ValueError, TypeError):
        return str(timestamp_str)


def compute_capture_flags(row: dict, conn) -> dict:
    device_time_source = row.get("device_time_source")
    device_ntp_drift_ms = row.get("device_ntp_drift_ms")
    device_clock_changed = row.get("device_clock_changed")
    drift_ms = row.get("drift_server_vs_device_ms")
    server_frame_hash = row.get("server_frame_hash")

    duplicate_of = None
    if server_frame_hash:
        duplicate_row = conn.execute(
            "SELECT id, case_id, step_id, source, server_received_at FROM captures "
            "WHERE server_frame_hash = ? AND id != ? LIMIT 1",
            (server_frame_hash, row.get("id")),
        ).fetchone()
        duplicate_of = dict(duplicate_row) if duplicate_row else None

    exif_signals = json.loads(row["exif_signals"]) if row.get("exif_signals") else None
    frame_hash_match = row.get("frame_hash_match")
    timezone_mismatch_flag = row.get("timezone_mismatch_flag")

    return {
        "clock_tamper_flag": bool(device_clock_changed),
        "date_changed": bool(row.get("device_date_changed")),
        "date_wrong_at_anchor": bool(row.get("device_date_wrong_at_anchor")),
        "unanchored_flag": (device_time_source == "device-fallback"),
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
        "exif_signals": exif_signals,
        "server_frame_hash": server_frame_hash,
        "frame_hash_match": None if frame_hash_match is None else bool(frame_hash_match),
        "timezone_mismatch_flag": None if timezone_mismatch_flag is None else bool(timezone_mismatch_flag),
        "duplicate_image_flag": duplicate_of is not None,
        "duplicate_of": duplicate_of,
    }


def _safe_name(s):
    if not s:
        return "unknown"
    return re.sub(r"[^\w\-]", "_", str(s))[:60]


def organize_case(case_id, conn=None):
    """
    Builds this case's readable summary (JSON + human-readable .txt) from
    the DB and pushes both to Drive. No local disk at any point -- photos
    already live on Drive (uploaded directly by api/captures.py), and the
    summaries are built as in-memory strings here, never written to a
    file first. Safe to call repeatedly; each call just overwrites the
    same two Drive files (Case Summary.txt / .json) with the current,
    complete picture.

    Returns the case_id on success, None if the case doesn't exist. If
    Drive is unavailable, the summary is silently NOT pushed (logged,
    non-fatal) -- there is no local fallback anymore, so an offline Drive
    means the summary just doesn't update until Drive is reachable again;
    the underlying DB rows are unaffected and the next successful
    organize_case call will catch it up.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_conn()
    try:
        case_row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not case_row:
            return None
        case_row = dict(case_row)

        captures = conn.execute(
            "SELECT * FROM captures WHERE case_id = ? ORDER BY id", (case_id,)
        ).fetchall()

        summary_captures = []
        used_names = {}
        for cap_row in captures:
            cap_dict = dict(cap_row)
            cap_dict["computed_flags"] = compute_capture_flags(cap_dict, conn)

            # Purely a readable label for the summary now -- no file this
            # points at locally. Kept so the .txt output still reads
            # sensibly ("--- ear_tag_live (ear_tag_live.jpg) ---").
            ext = ".webm" if (cap_dict.get("filename") or "").endswith(".webm") else ".jpg"
            base_name = _safe_name(cap_dict["step_id"])
            count = used_names.get(base_name, 0)
            used_names[base_name] = count + 1
            cap_dict["organized_filename"] = f"{base_name}{'' if count == 0 else f'_{count + 1}'}{ext}"
            if not cap_dict.get("drive_file_link"):
                cap_dict["_warning"] = "not yet on Drive (upload pending or failed)"

            summary_captures.append(cap_dict)

        summary = {"case": case_row, "captures": summary_captures}
        json_content = json.dumps(summary, indent=2, default=str)
        txt_content = _build_readable_summary(case_row, summary_captures)

        # -----------------------------------------------------------------
        # Push straight to Drive. Wrapped in its own try/except so a Drive
        # failure never raises out of this function -- callers (upload
        # endpoint, case-detail save) schedule this as a fire-and-forget
        # background task and shouldn't crash over it.
        #
        # ⚠️ NEW -- the actual folder-resolve + upload_text calls are now
        # serialized per case_id via _lock_for_case(). See that helper's
        # comment above for exactly which race this closes.
        # -----------------------------------------------------------------
        try:
            from services.google_drive_service import get_drive_service
            drive = get_drive_service()

            if drive.is_available():
                with _lock_for_case(case_id):
                    case_folder = drive.get_or_create_case_folder(
                        case_id, domain=case_row.get("domain", "live")
                    )
                    if case_folder:
                        drive.upload_text(case_folder["folder_id"], "Case Summary.txt", txt_content)
                        drive.upload_text(case_folder["folder_id"], "Case Summary.json", json_content)

                        conn.execute(
                            "UPDATE cases SET drive_folder_id = ?, drive_folder_link = ? WHERE id = ?",
                            (case_folder["folder_id"], case_folder["drive_link"], case_id),
                        )
                        conn.commit()
            else:
                print(f"[organize] Drive unavailable -- summary for case {case_id} not pushed this round")
        except Exception as e:
            print(f"[organize] Drive summary upload failed for case {case_id} (non-fatal): {e}")

        return case_id
    finally:
        if owns_conn:
            conn.close()


def _clock_integrity_summary(flags, cap):
    if cap.get("source") not in ("camera", "geotag-generated"):
        return "N/A (gallery upload)"
    if flags["unanchored_flag"]:
        return "No server time reference available"
    if flags["clock_tamper_flag"]:
        return "Date changed since anchor" if flags["date_changed"] else "Clock changed since anchor"
    if flags["anchor_drift_flag"]:
        return "Date was wrong at session start" if flags["date_wrong_at_anchor"] else "Clock was wrong at session start"
    if flags["capture_upload_gap_flag"]:
        drift_ms = cap.get("drift_server_vs_device_ms")
        hours = abs(drift_ms) / 3600000 if drift_ms is not None else 0
        return f"Long capture-to-upload gap ({hours:.0f}h)"
    if flags["clock_drift_flag"]:
        drift_ms = cap.get("drift_server_vs_device_ms")
        seconds = abs(drift_ms) / 1000 if drift_ms is not None else 0
        return f"{seconds:.0f}s drift"
    if flags["timezone_mismatch_flag"]:
        return "Timezone doesn't match location"
    return "Consistent"


def _build_readable_summary(case_row, captures):
    lines = [
        f"CASE SUMMARY -- {case_row['id']}",
        "=" * 60,
        f"Farmer:  {case_row.get('farmer_name') or '(not filled in)'}",
        f"Village: {case_row.get('village') or '(not filled in)'}",
        f"Domain:  {case_row.get('domain')}",
        f"Created: {_format_ist(case_row.get('updated_at') or case_row.get('created_at'))}",
        "",
    ]
    for cap in captures:
        flags = cap["computed_flags"]
        lines.append(f"--- {cap['step_id']}  ({cap['organized_filename']}) ---")
        lines.append(f"  Source:          {cap['source']}")
        lines.append(f"  Drive:           {cap.get('drive_file_link') or '(upload pending)'}")
        lines.append(f"  Captured:        {_format_ist(cap.get('device_timestamp')) or '(no device timestamp)'}")
        lines.append(f"  Server received: {_format_ist(cap['server_received_at'])}")
        if cap.get("lat") is not None and cap.get("lon") is not None:
            lines.append(f"  Location:        {cap['lat']}, {cap['lon']}  (https://www.google.com/maps/search/?api=1&query={cap['lat']},{cap['lon']})")
        else:
            lines.append(f"  Location:        Not available")
        lines.append(f"  Resolution:      {cap.get('resolution') or '(not recorded)'}")
        lines.append(f"  Device:          {cap.get('device_info') or '(not recorded)'}")
        lines.append(f"  Clock Integrity: {_clock_integrity_summary(flags, cap)}")
        if cap.get("tamper_check_score") is not None:
            lines.append(f"  Tamper Check:    {cap['tamper_check_score']}/100 -- {cap.get('tamper_check_band', '')}")
        else:
            lines.append(f"  Tamper Check:    (not computed -- video, or check hadn't finished when this was generated)")
        lines.append(f"  Frame Integrity: {'Verified untouched' if flags['frame_hash_match'] else ('MISMATCH' if flags['frame_hash_match'] is False else 'Not checked')}")
        lines.append(f"  Duplicate Check: {'SEEN BEFORE' if flags['duplicate_image_flag'] else 'Not seen before'}")
        if cap.get("normalize_scale_applied") is not None:
            if cap.get("normalize_skipped"):
                lines.append(f"  Pixel Norm.:     Skipped ({cap.get('normalize_skip_reason')})")
            else:
                lines.append(f"  Pixel Norm.:     {cap.get('normalize_scale_applied')}x applied (infra only, no consumer yet)")
        
        warnings = []
        if flags["clock_tamper_flag"]:
            warnings.append("CLOCK CHANGED MID-SESSION")
        if flags["anchor_drift_flag"]:
            warnings.append("CLOCK WRONG AT SESSION START")
        if flags["unanchored_flag"]:
            warnings.append("NO SERVER TIME REFERENCE")
        if flags["timezone_mismatch_flag"]:
            warnings.append("TIMEZONE DOESN'T MATCH LOCATION")
        if flags["frame_hash_match"] is False:
            warnings.append("FRAME HASH MISMATCH (ALTERED IN TRANSIT)")
        if flags["duplicate_image_flag"]:
            dup = flags["duplicate_of"]
            warnings.append(f"DUPLICATE of case {dup['case_id']} / {dup['step_id']}" if dup else "DUPLICATE")
        if flags["exif_signals"] and flags["exif_signals"].get("flags"):
            high = [f for f in flags["exif_signals"]["flags"] if f["weight"] == "High"]
            if high:
                warnings.append(f"{len(high)} HIGH-WEIGHT EXIF FLAG(S)")
        
        lines.append(f"  Warnings:        {', '.join(warnings) if warnings else 'None'}")
        lines.append("")

    return "\n".join(lines)