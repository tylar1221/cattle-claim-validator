# =========================================================================
# GOOGLE DRIVE SERVICE -- case-evidence storage.
#
# Design (see chat): RDS holds metadata (who/what/when/where/forensic
# signals). Drive holds the actual bytes (images, videos, reports). This
# service is the ONLY thing in the backend that talks to Drive -- nothing
# else should import googleapiclient directly.
#
# Folder layout on Drive (matches organize.py's local layout 1:1):
#
#   <DRIVE_ROOT_FOLDER>/                (default name: "Cattle Claims")
#   └── CASE-<case_id>/                 e.g. "CASE-case_ab12cd34ef56"
#       ├── Live/                       (or "Dead/" -- whichever domain)
#       │   ├── Flank/
#       │   ├── Front Head/
#       │   ├── Rear View/
#       │   ├── Muzzle/
#       │   ├── Ear Tag/
#       │   ├── Owner Photo/
#       │   ├── Video/
#       │   ├── Scar Injury/
#       │   └── Geotag/
#       ├── Forensics/                  (ELA heatmaps, future forensic exports)
#       └── Case Summary.txt
#
# Thread-safety: googleapiclient services are backed by httplib2, which is
# NOT thread-safe. FastAPI runs request handlers AND BackgroundTasks on a
# threadpool, so a single shared service instance causes intermittent
# "[Errno 32] Broken pipe" errors under concurrent uploads. Fixed with a
# thread-local service, exactly as in the reference service.
#
# ⚠️ FIX -- DUPLICATE FOLDERS (found via real testing): get_or_create_case_folder
# is called from _upload_capture_to_drive (a background task scheduled after
# EVERY capture upload). When several captures land close together, multiple
# background tasks call this concurrently for the SAME case. Google Drive's
# _find_child_folder -> _create_folder sequence is NOT atomic -- two threads
# can both search, both see "not there yet", and both create the folder. This
# is exactly the race organize.py already solved for its OWN Drive-summary
# writes via _lock_for_case. Fixed here the SAME way: one lock per case_id,
# held for the entire resolve-or-create operation.
#
# Auth: auto-detects whichever credential file exists:
#   1. service_account.json  -- preferred for servers / AWS
#   2. token_combined.pickle -- the existing OAuth token (dev machines only)
# See _build_service() for the exact order and what each requires.
# =========================================================================
import os
import pickle
import threading
from datetime import datetime
from typing import Dict, List, Optional

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload
from googleapiclient.errors import HttpError

from services.db import get_conn  # for looking up case metadata when building folder names

# -------------------------------------------------------------------------
# CONFIG -- everything environment-overridable so dev/local/prod can differ
# without code changes.
# -------------------------------------------------------------------------
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
OAUTH_TOKEN_FILE      = os.getenv("GOOGLE_OAUTH_TOKEN_FILE",      "token_combined.pickle")
CREDENTIALS_FILE      = os.getenv("GOOGLE_CREDENTIALS_FILE",      "credentials.json")  # only for first-ever OAuth consent

# Name of the top-level "everything lives here" folder on Drive.
DRIVE_ROOT_FOLDER_NAME = os.getenv("DRIVE_ROOT_FOLDER_NAME", "Cattle Claims")
# Optionally nest that root under an existing Drive folder (leave as 'root'
# to put it at the top of My Drive).
DRIVE_PARENT_FOLDER_ID = os.getenv("DRIVE_PARENT_FOLDER_ID", "root")

# Read-only Drive scopes for a service account; full Drive scope for OAuth
# so a human-run app can also see folders they made manually.
SERVICE_ACCOUNT_SCOPES = ["https://www.googleapis.com/auth/drive"]
OAUTH_SCOPES           = ["https://www.googleapis.com/auth/drive"]


# -------------------------------------------------------------------------
# Sanitisation -- Drive folder/file names are more permissive than POSIX,
# but slashes and control characters still break things (they'd be
# interpreted as path separators in a URL or in Drive's query language).
# Kept deliberately close to organize.py's _safe_name so the local export
# and the Drive export can be eyeballed side by side.
# -------------------------------------------------------------------------
def _safe_name(s) -> str:
    import re
    if not s:
        return "unknown"
    cleaned = re.sub(r"[^\w\-\. ]", "_", str(s))[:80].strip()
    return cleaned or "unknown"


# =========================================================================
# FIX (duplicate folders): one lock per case_id, held for the ENTIRE
# resolve-or-create operation. Without this, concurrent background upload
# tasks each independently call _find_child_folder -> (not found) ->
# _create_folder, and both end up creating the same folder. This is the
# same per-case-lock pattern organize.py already uses for its summary
# uploads (_lock_for_case) -- the class-level folder-ID cache alone is
# NOT sufficient, since the race happens BEFORE anything is cached.
#
# Locked at MODULE level (not per-instance) because GoogleDriveService is
# designed to be instantiated freely at call sites (see the docstring on
# the class) -- a per-instance lock would allow two instances to race
# against each other. Module-level ensures a single global lock table.
# =========================================================================
_case_creation_locks: Dict[str, threading.Lock] = {}
_case_creation_locks_guard = threading.Lock()


def _lock_for_case_folder(case_id: str) -> threading.Lock:
    """Returns a (lazily created) per-case lock for folder-resolution."""
    with _case_creation_locks_guard:
        if case_id not in _case_creation_locks:
            _case_creation_locks[case_id] = threading.Lock()
        return _case_creation_locks[case_id]


class GoogleDriveService:
    """
    One instance per process. Call sites just do:

        drive = GoogleDriveService()
        case_folder = drive.get_or_create_case_folder("case_ab12cd34ef56", domain="live")
        drive.upload_file(case_folder["folder_id"], local_path="/tmp/x.jpg",
                          drive_name="flank_live_1.jpg")

    Nothing outside this class should know about Drive file IDs vs folder
    IDs vs links -- everything it returns is already in the shape the
    caller needs.
    """

    def __init__(self):
        self._local = threading.local()
        # Folder-ID cache so we don't hit Drive's API on every single
        # capture upload to re-resolve "where does this case's Ear Tag
        # folder live". Keyed by (case_id, subpath) -> folder_id. Purely
        # in-memory; safe to lose on restart (we'd just re-resolve).
        self._folder_id_cache: Dict[tuple, str] = {}
        self._cache_lock = threading.Lock()
        self._authenticated = False

    # ------------------------------------------------------------------
    # AUTH
    # ------------------------------------------------------------------
    def _build_service(self):
        """
        Build a fresh Drive client for the calling thread. Tries, in order:
          1. Service-account JSON  (production / AWS-friendly)
          2. OAuth pickled token   (dev machines, existing token)
        Returns None (and logs a clear reason) if neither is usable -- the
        caller treats Drive-unavailable as a soft failure, never a crash.
        """
        creds = None

        # -- 1. Service account ------------------------------------------------
        if os.path.exists(SERVICE_ACCOUNT_FILE):
            try:
                creds = service_account.Credentials.from_service_account_file(
                    SERVICE_ACCOUNT_FILE, scopes=SERVICE_ACCOUNT_SCOPES
                )
                print(f"[Drive] Using service account: {SERVICE_ACCOUNT_FILE}")
            except Exception as e:
                print(f"[Drive] Service account load failed ({e}); falling through to OAuth")

        # -- 2. OAuth token ----------------------------------------------------
        if creds is None and os.path.exists(OAUTH_TOKEN_FILE):
            try:
                with open(OAUTH_TOKEN_FILE, "rb") as f:
                    creds = pickle.load(f)
                print(f"[Drive] Using OAuth token: {OAUTH_TOKEN_FILE}")
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    with open(OAUTH_TOKEN_FILE, "wb") as f:
                        pickle.dump(creds, f)
                    print("[Drive] OAuth token refreshed")
            except Exception as e:
                print(f"[Drive] OAuth token load failed: {e}")
                creds = None

        if creds is None or not getattr(creds, "valid", False):
            print("[Drive] NO VALID CREDENTIALS -- Drive uploads will be skipped")
            return None

        # cache_discovery=False avoids a noisy file-cache warning + global state
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    @property
    def service(self):
        """Thread-local Drive client, built lazily on first use in each thread."""
        svc = getattr(self._local, "drive_service", None)
        if svc is None:
            svc = self._build_service()
            self._local.drive_service = svc
        return svc

    def is_available(self) -> bool:
        """Cheap check call sites can use before trying a real upload."""
        return self.service is not None

    # ------------------------------------------------------------------
    # FOLDER MANAGEMENT
    # ------------------------------------------------------------------
    def _find_child_folder(self, parent_id: str, name: str) -> Optional[str]:
        """Returns the folder ID of <name> directly under <parent_id>, or None."""
        # Escape single quotes in the name -- Drive's query language uses
        # ' as its string delimiter, so a name like "Bob's Cattle" would
        # otherwise break the query string.
        safe = name.replace("'", "\\'")
        q = (
            f"name = '{safe}' "
            f"and '{parent_id}' in parents "
            f"and mimeType = 'application/vnd.google-apps.folder' "
            f"and trashed = false"
        )
        try:
            resp = self.service.files().list(
                q=q, spaces="drive", fields="files(id, name)", pageSize=5
            ).execute()
            files = resp.get("files", [])
            return files[0]["id"] if files else None
        except HttpError as e:
            print(f"[Drive] _find_child_folder({name}) failed: {e}")
            return None

    def _create_folder(self, parent_id: str, name: str) -> Optional[str]:
        """Creates <name> directly under <parent_id>. Returns the new ID."""
        try:
            meta = {
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            }
            folder = self.service.files().create(
                body=meta, fields="id, name"
            ).execute()
            return folder["id"]
        except HttpError as e:
            print(f"[Drive] _create_folder({name}) failed: {e}")
            return None

    def _get_or_create_folder(self, parent_id: str, name: str) -> Optional[str]:
        """
        Idempotent: returns an existing folder ID if <name> already exists
        under <parent_id>, else creates it. This is the ONLY folder helper
        the rest of the service should use -- guarantees we never end up
        with two "Ear Tag" folders under the same case, which was a real
        failure mode in an earlier design that just called create every time.

        ⚠️ NOTE: this method itself is NOT thread-safe against concurrent
        callers with the SAME (parent_id, name). Callers MUST hold the
        per-case lock (_lock_for_case_folder) -- see get_or_create_case_folder.
        """
        existing = self._find_child_folder(parent_id, name)
        if existing:
            return existing
        return self._create_folder(parent_id, name)

    def get_or_create_root_folder(self) -> Optional[str]:
        """
        The single top-level folder everything lives under, e.g.
        "Cattle Claims". Cached for the process lifetime once resolved.
        """
        with self._cache_lock:
            cached = self._folder_id_cache.get(("__root__", ""), None)
        if cached:
            return cached

        root_id = self._find_child_folder(DRIVE_PARENT_FOLDER_ID, DRIVE_ROOT_FOLDER_NAME)
        if not root_id:
            root_id = self._create_folder(DRIVE_PARENT_FOLDER_ID, DRIVE_ROOT_FOLDER_NAME)
        if not root_id:
            return None

        with self._cache_lock:
            self._folder_id_cache[("__root__", "")] = root_id
        return root_id

    def get_or_create_case_folder(self, case_id: str, domain: str = "live") -> Optional[Dict]:
        """
        Returns the case folder for this case_id, creating it (and any
        missing subfolders) on first call. Idempotent -- safe to call on
        every capture upload.

        ⚠️ FIX (duplicate folders): the entire resolve-or-create operation
        is serialized per-case via _lock_for_case_folder. Without this,
        concurrent background upload tasks each independently search-then-
        create, and both succeed in creating the SAME folder (Drive's
        create() is not a "create-if-not-exists" -- it always creates).
        Locking here means the second-and-later callers wait, then find
        the folder the first caller just created -- exactly like the
        per-case lock organize.py already uses for its Drive summaries.

        Also writes drive_folder_id / drive_folder_link to the `cases`
        table IMMEDIATELY on first resolution, rather than waiting for a
        downstream caller to remember to do it.
        """
        # ---------------- CRITICAL SECTION START ----------------
        # Hold the per-case lock for the ENTIRE folder resolution. The
        # folder-ID cache check is inside the lock too -- otherwise a
        # second thread could pass the cache check, then block, then run
        # the full search-and-create anyway, defeating the purpose.
        with _lock_for_case_folder(case_id):

            root_id = self.get_or_create_root_folder()
            if not root_id:
                return None

            case_folder_name = f"CASE-{case_id}"

            # Fast path: already resolved this case's folder in a previous
            # call -- read it from cache (under cache lock) and skip Drive.
            with self._cache_lock:
                case_folder_id = self._folder_id_cache.get((case_id, ""), None)

            if not case_folder_id:
                case_folder_id = self._get_or_create_folder(root_id, case_folder_name)
                if not case_folder_id:
                    return None
                with self._cache_lock:
                    self._folder_id_cache[(case_id, "")] = case_folder_id

            SUBFOLDER_NAMES = [
                ("flank",        "Flank"),
                ("front_head",   "Front Head"),
                ("rear_view",    "Rear View"),
                ("muzzle",       "Muzzle"),
                ("ear_tag",      "Ear Tag"),
                ("owner_photo",  "Owner Photo"),
                ("video",        "Video"),
                ("scar_injury",  "Scar Injury"),
                ("geotag",       "Geotag"),
            ]

            domain_root_name = "Live" if domain == "live" else "Dead"

            # Cache key for the domain root, so repeat calls skip the
            # find-or-create for "Live"/"Dead" too.
            with self._cache_lock:
                domain_root_id = self._folder_id_cache.get((case_id, domain_root_name), None)

            if not domain_root_id:
                domain_root_id = self._get_or_create_folder(case_folder_id, domain_root_name)
                if domain_root_id:
                    with self._cache_lock:
                        self._folder_id_cache[(case_id, domain_root_name)] = domain_root_id

            subfolders = {domain: domain_root_id}

            # FIX: Only create the subfolders under the DOMAIN root (Live/ or
            # Dead/), once each. Previously the loop skipped live_root/dead_root
            # keys but then unconditionally added "Forensics" INSIDE the domain
            # root -- which is why you also saw a duplicate "Forensics" folder
            # sitting inside Live/ in the screenshot. Forensics belongs at the
            # CASE root only (alongside Live/, Dead/, Case Summary.*), not
            # inside each domain subfolder.
            for key, name in SUBFOLDER_NAMES:
                cache_key = (case_id, f"{domain_root_name}/{name}")
                with self._cache_lock:
                    existing_id = self._folder_id_cache.get(cache_key, None)
                if not existing_id:
                    existing_id = self._get_or_create_folder(domain_root_id, name)
                    if existing_id:
                        with self._cache_lock:
                            self._folder_id_cache[cache_key] = existing_id
                subfolders[key] = existing_id

            # Forensics at the CASE root -- ONCE, not per-domain, and NOT
            # duplicated inside the domain root (that was the extra bug).
            with self._cache_lock:
                forensics_id = self._folder_id_cache.get((case_id, "__forensics__"), None)
            if not forensics_id:
                forensics_id = self._get_or_create_folder(case_folder_id, "Forensics")
                if forensics_id:
                    with self._cache_lock:
                        self._folder_id_cache[(case_id, "__forensics__")] = forensics_id
            subfolders["forensics"] = forensics_id

            drive_link = f"https://drive.google.com/drive/folders/{case_folder_id}"

            # Record the folder reference in the DB. Idempotent UPDATE,
            # wrapped so a DB hiccup can never break folder creation.
            try:
                conn = get_conn()
                conn.execute(
                    "UPDATE cases SET drive_folder_id = ?, drive_folder_link = ? WHERE id = ?",
                    (case_folder_id, drive_link, case_id),
                )
                conn.commit()
                conn.close()
            except Exception as e:
                print(f"[Drive] Could not persist folder link for case {case_id} (non-fatal): {e}")

            return {
                "folder_id": case_folder_id,
                "drive_link": drive_link,
                "folder_name": case_folder_name,
                "subfolders": subfolders,
            }
        # ---------------- CRITICAL SECTION END ----------------

    # ------------------------------------------------------------------
    # MAPPING capture step_id -> which subfolder it belongs in
    # ------------------------------------------------------------------
    STEP_TO_SUBFOLDER = {
        "flank":              "flank",
        "flank_live":         "flank",
        "head":               "front_head",
        "front_view_live":    "front_head",
        "rear_view_live":     "rear_view",
        "muzzle":             "muzzle",
        "muzzle_live":        "muzzle",
        "ear_tag":            "ear_tag",
        "ear_tag_live":       "ear_tag",
        "dead_ear_tag":       "ear_tag",
        "live_ear_tag_live":  "ear_tag",
        "live_eardemo_live":  "ear_tag",
        "owner_photo":        "owner_photo",
        "owner_photo_live":   "owner_photo",
        "video":              "video",
        "video_live":         "video",
        "scar_injury":        "scar_injury",
        "scar_injury_live":   "scar_injury",
        "geotag":             "geotag",
        "geotag_live":        "geotag",
    }

    def folder_for_capture(self, case_folder: Dict, step_id: str) -> Optional[str]:
        """
        Given the case folder dict from get_or_create_case_folder and a
        capture's step_id, returns the specific subfolder ID the file
        should land in.
        """
        # Normalize the incoming step_id before lookup. The frontend sends
        # captureKey(step) = "<domain>_<step.id>[_<side>]", but
        # STEP_TO_SUBFOLDER's keys are the bare step ids.
        normalized = step_id
        for prefix in ("live_", "dead_"):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix):]
                break
        for suffix in ("_left", "_right"):
            if normalized.endswith(suffix):
                normalized = normalized[:-len(suffix)]
                break

        sub = self.STEP_TO_SUBFOLDER.get(normalized) or self.STEP_TO_SUBFOLDER.get(step_id)
        subfolders = case_folder.get("subfolders", {})
        if sub and sub in subfolders and subfolders[sub]:
            return subfolders[sub]
        return subfolders.get("live") or subfolders.get("dead") or case_folder["folder_id"]

    # ------------------------------------------------------------------
    # UPLOADS
    # ------------------------------------------------------------------
    def upload_file(self, parent_folder_id: str, local_path: str,
                    drive_name: str, mime_type: Optional[str] = None) -> Optional[Dict]:
        """
        Uploads a file from local disk into the given Drive folder. Used
        for LARGE files (videos) where streaming from a local temp file is
        the right pattern -- resumable upload handles flaky connections.

        Returns {"file_id", "file_name", "file_link"} or None on failure.
        """
        try:
            meta = {"name": drive_name, "parents": [parent_folder_id]}
            media = MediaFileUpload(
                local_path,
                mimetype=mime_type,
                resumable=True,
            )
            result = self.service.files().create(
                body=meta, media_body=media, fields="id, name, webViewLink"
            ).execute()
            self._make_file_viewable(result["id"])
            return {
                "file_id": result["id"],
                "file_name": result["name"],
                "file_link": result.get("webViewLink"),
            }
        except HttpError as e:
            print(f"[Drive] upload_file({drive_name}) failed: {e}")
            return None

    def upload_bytes(self, parent_folder_id: str, data: bytes,
                     drive_name: str, mime_type: str = "image/jpeg") -> Optional[Dict]:
        """
        Same as upload_file but from an in-memory bytes object. Used for
        the common case where the FastAPI handler already has the capture
        bytes in hand (it reads them for hashing/EXIF/OCR anyway) and
        shouldn't have to round-trip through a temp file just to hand
        them to Drive.
        """
        import io
        try:
            meta = {"name": drive_name, "parents": [parent_folder_id]}
            media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=False)
            result = self.service.files().create(
                body=meta, media_body=media, fields="id, name, webViewLink"
            ).execute()
            self._make_file_viewable(result["id"])
            return {
                "file_id": result["id"],
                "file_name": result["name"],
                "file_link": result.get("webViewLink"),
            }
        except HttpError as e:
            print(f"[Drive] upload_bytes({drive_name}) failed: {e}")
            return None

    def _make_file_viewable(self, file_id: str):
        """Best-effort: anyone-with-link can view. Failure is non-fatal."""
        try:
            self.service.permissions().create(
                fileId=file_id,
                body={"role": "reader", "type": "anyone"},
                fields="id",
            ).execute()
        except HttpError as e:
            print(f"[Drive] _make_file_viewable({file_id}) failed (non-fatal): {e}")

    def make_folder_viewable(self, folder_id: str):
        """Same idea, for the case folder itself."""
        try:
            self.service.permissions().create(
                fileId=folder_id,
                body={"role": "reader", "type": "anyone"},
                fields="id",
            ).execute()
        except HttpError as e:
            print(f"[Drive] make_folder_viewable({folder_id}) failed (non-fatal): {e}")

    # ------------------------------------------------------------------
    # TEXT SUMMARIES
    # ------------------------------------------------------------------
    def upload_text(self, parent_folder_id: str, filename: str, content: str) -> Optional[Dict]:
        """
        Uploads an arbitrary UTF-8 string as a file on Drive. Used by
        organize.py to push the case_summary.txt / case_summary.json up
        alongside the images. Overwrites by name if a file with that name
        already exists in the folder -- otherwise every summarize run
        would pile up "case_summary (1).txt", "case_summary (2).txt", etc.
        """
        safe = filename.replace("'", "\\'")
        q = f"name = '{safe}' and '{parent_folder_id}' in parents and trashed = false"
        try:
            existing = self.service.files().list(
                q=q, fields="files(id)", pageSize=1
            ).execute().get("files", [])
            data = content.encode("utf-8")
            if existing:
                media = MediaIoBaseUpload(
                    __import__("io").BytesIO(data),
                    mimetype="text/plain",
                    resumable=False,
                )
                updated = self.service.files().update(
                    fileId=existing[0]["id"],
                    media_body=media,
                    fields="id, name, webViewLink",
                ).execute()
                return {
                    "file_id": updated["id"],
                    "file_name": updated["name"],
                    "file_link": updated.get("webViewLink"),
                }
            else:
                return self.upload_bytes(
                    parent_folder_id, data, filename, mime_type="text/plain"
                )
        except HttpError as e:
            print(f"[Drive] upload_text({filename}) failed: {e}")
            return None

_drive_singleton = None
_drive_singleton_lock = threading.Lock()

def get_drive_service() -> GoogleDriveService:
    """Process-wide singleton. Reuse this everywhere -- fresh instances
    defeat the folder-ID cache and multiply Drive API calls."""
    global _drive_singleton
    if _drive_singleton is None:
        with _drive_singleton_lock:
            if _drive_singleton is None:
                _drive_singleton = GoogleDriveService()
    return _drive_singleton