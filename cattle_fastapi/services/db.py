# =========================================================================
# SQLite storage -- capture metadata only. Photos themselves live on disk
# in uploads/ (see api/captures.py); this table just indexes them plus the
# timestamp/GPS/trust fields the Matrix sheet cares about.
#
# Plain sqlite3 (stdlib), no ORM -- this is a small internship project, not
# worth the SQLAlchemy setup weight yet. Easy to swap in later if the
# schema grows past what raw SQL is comfortable for.
# =========================================================================
import sqlite3
from pathlib import Path

DB_PATH = Path("cattle_claims.db")


def get_conn():
  # =========================================================================
# SQLite storage -- capture metadata only. Photo/video bytes go straight
# to Google Drive at upload time (see api/captures.py + services/
# google_drive_service.py) -- nothing is ever written to local disk. This
# table just indexes what's on Drive (drive_file_id/drive_file_link) plus
# the timestamp/GPS/trust fields the Matrix sheet cares about.
# =========================================================================
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cases (
            id TEXT PRIMARY KEY,               -- e.g. "case_<uuid>"
            domain TEXT NOT NULL,               -- 'live' | 'dead'
            loan_no TEXT,
            farmer_name TEXT,
            village TEXT,
            -- Case / Policy Information
            taluka TEXT, district TEXT, occupation TEXT, insurer_org TEXT,
            remarks TEXT, survey_date TEXT, address TEXT, sub_case_status TEXT,
            -- Description of the Animal Proposed for Insurance
            animal_type TEXT, age TEXT, gender TEXT, breed TEXT, tag_no TEXT,
            market_value TEXT, color TEXT, swish_of_tail TEXT, right_horn TEXT,
            left_horn TEXT, lactation TEXT, daily_milk TEXT,
            distinguishing_feature TEXT, sum_insured TEXT, policy_duration TEXT,
            premium_amt TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS captures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,             -- stored filename on disk (uploads/)
            case_id TEXT,
            step_id TEXT,
            source TEXT NOT NULL,               -- 'camera' | 'gallery'

            -- Rank 1 on the Trust Model sheet: THE SERVER'S OWN CLOCK at
            -- the moment the bytes were received. The client cannot edit,
            -- spoof, or influence this value -- it is read from this
            -- process's own clock, never from anything the request claims.
            server_received_at TEXT NOT NULL,

            -- What the CLIENT claims (device clock, or NTP/monotonic-
            -- anchored time for camera captures, or EXIF DateTimeOriginal
            -- for gallery uploads). A claim, not evidence on its own --
            -- its value comes from being cross-checked against
            -- server_received_at below.
            device_timestamp TEXT,
            device_time_source TEXT,            -- 'ntp' | 'device-fallback' | 'exif' | null
            device_ntp_drift_ms INTEGER,
            device_clock_changed INTEGER,       -- 0/1, from the client's own monotonic-anchor check

            lat REAL,
            lon REAL,
            gps_accuracy_m REAL,

            -- Rule Engine sheet: "Device clock vs server time drift", >120s
            -- -> flag, don't reject. Computed HERE server-side at insert
            -- time, not trusted from anything the client sends, since the
            -- whole point is the client could lie about its own drift.
            drift_server_vs_device_ms INTEGER,

            -- Server-side EXIF Detection Signals (gallery uploads only) --
            -- JSON blob from services/exif_service.py: exif_present flag,
            -- individual weighted signals, and raw-ish extracted values.
            -- NULL for camera captures, where this doesn't apply.
            exif_signals TEXT,

            -- Frame integrity hash (Matrix scenario #5 Live -- "close the
            -- gap"). client_frame_hash is the SHA-256 the browser computed
            -- at the moment of capture, BEFORE upload. server_frame_hash
            -- is recomputed HERE from the bytes actually received. A
            -- mismatch means something altered the data in transit.
            client_frame_hash TEXT,
            server_frame_hash TEXT,
            frame_hash_match INTEGER,

            -- Matrix scenario #7 Live -- device-reported IANA timezone
            -- (from the browser's Intl API) cross-checked against what
            -- the GPS coordinates imply. A wrong-but-consistent offset
            -- (a genuine traveller) is expected to happen sometimes and
            -- stays a soft, informational flag -- never a rejection.
            device_timezone TEXT,
            timezone_mismatch_flag INTEGER,

            -- NEW: purely cosmetic refinements distinguishing a calendar
            -- DATE change from an ordinary time-of-day change -- the
            -- underlying tamper signal is unchanged either way
            -- (device_clock_changed / device_ntp_drift_ms above), this
            -- just lets the UI say "Date changed" instead of the more
            -- generic "Clock changed" when that's specifically what
            -- happened.
            device_date_changed INTEGER,
            device_date_wrong_at_anchor INTEGER,

            -- NEW: these were shown on the webpage the whole time but
            -- NEVER actually reached the backend at all -- they only ever
            -- existed in the browser's own memory. Real gap, found while
            -- comparing the organized_exports summary against the
            -- webpage side by side.
            resolution TEXT,        -- e.g. "1706 × 1280", client-computed
            device_info TEXT,       -- e.g. "Android · Chrome 152", from the user-agent
            tamper_check_score INTEGER,   -- the ELA-based 0-100 score (client-side only check)
            tamper_check_band TEXT,       -- 'low' | 'uncertain' | 'elevated'

            -- NEW: Pixel-Size Normalization -- INFRASTRUCTURE, no
            -- consumer yet (see NORMALIZE_CONFIG's block comment in
            -- static/index.html). Known synchronously at capture time,
            -- sent with the initial upload. normalize_scale_applied=1
            -- means either the photo was already close to the target
            -- size, or normalization didn't apply to this capture at all
            -- (see normalize_skipped/normalize_skip_reason to tell those
            -- two apart).
            normalize_scale_applied REAL,
            normalize_skipped INTEGER,        -- 0/1
            normalize_skip_reason TEXT,       -- e.g. 'no-matched-box', 'exceeds-max-upscale'

            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    # ⚠️ MIGRATION SAFETY NET: CREATE TABLE IF NOT EXISTS above only helps
    # on a brand-new database -- it does NOT retroactively add columns to
    # an existing captures table on someone's machine that already has
    # data in it (resolution/device_info/tamper_check_* hit exactly this
    # in the previous session). ALTER TABLE ADD COLUMN, one at a time,
    # each wrapped so "column already exists" is silently ignored --
    # idempotent, safe to run on every startup, and means a future new
    # column never needs a manual migration step again either.
    new_columns = [
        ("normalize_scale_applied", "REAL"),
        ("normalize_skipped", "INTEGER"),
        ("normalize_skip_reason", "TEXT"),
        ("ocr_signals", "TEXT"),
        # NEW -- Google Drive storage. The actual image/video bytes now live
        # on Drive, not on local disk. `filename` still exists and is still
        # used for the *temporary* local file during processing -- these two
        # columns are what the record of "where is this permanently" points at.
        ("drive_file_id",   "TEXT"),
        ("drive_file_link", "TEXT"),
    ]
    for col_name, col_type in new_columns:
        try:
            conn.execute(f"ALTER TABLE captures ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    # NEW -- separate from the captures migration above, since this
    # column belongs to the `cases` table instead. Tracks when Case
    # Details were last actually SAVED (set explicitly in Python as
    # timezone-aware UTC in api/cases.py's update_case_details, same
    # pattern as server_received_at elsewhere -- NOT SQLite's own
    # datetime('now') default, which produces an ambiguous naive string
    # with no timezone marker at all). Used to answer a real report: the
    # case_summary.txt "Created" line previously showed cases.created_at
    # (set once, the moment the case was first opened) -- useful, but not
    # what was actually wanted, which is when the details form was
    # actually filled in and saved, a materially different moment for
    # anyone who opens the app well before actually filling the form.
    try:
        conn.execute("ALTER TABLE cases ADD COLUMN updated_at TEXT")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise
    # NEW -- case-level Drive folder reference, so api/cases.py can return a
# shareable link without having to re-query Drive every time.
    case_new_columns = [
        ("drive_folder_id",   "TEXT"),
        ("drive_folder_link", "TEXT"),
    ]
    for col_name, col_type in case_new_columns:
        try:
            conn.execute(f"ALTER TABLE cases ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
    # ⚠️ REMOVAL, per explicit request: Marker Scale (ArUco) is being
    # dropped from the system completely, not just hidden -- including
    # columns already added to an existing database by a previous run of
    # this same migration. DROP COLUMN needs SQLite 3.35+ (bundled with
    # Python 3.11+); wrapped so this is a no-op on an older SQLite or on a
    # database that never had these columns in the first place, rather
    # than crashing startup over a cleanup step.
    dropped_columns = ["marker_detected", "marker_id", "marker_scale_px_per_mm"]
    for col_name in dropped_columns:
        try:
            conn.execute(f"ALTER TABLE captures DROP COLUMN {col_name}")
        except sqlite3.OperationalError:
            pass  # column doesn't exist, or SQLite too old to support DROP COLUMN -- either way, nothing to do

    conn.commit()
    conn.close()
