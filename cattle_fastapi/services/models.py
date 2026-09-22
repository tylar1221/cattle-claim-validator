from sqlalchemy import Column, Integer, BigInteger, String, Text, Float, DateTime
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()
from sqlalchemy import Boolean
import uuid

class User(Base):
    __tablename__ = "users"

    id = Column(String(50), primary_key=True, default=lambda: "user_" + uuid.uuid4().hex[:12])
    username = Column(String(80), unique=True, nullable=False, index=True)
    password_hash = Column(Text, nullable=False)
    full_name = Column(Text)
    role = Column(String(20), default="agent")     # e.g. "admin" | "agent"
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Session(Base):
    __tablename__ = "sessions"

    token = Column(String(64), primary_key=True)       # random session id, stored in the cookie
    user_id = Column(String(50), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)

class Case(Base):
    __tablename__ = "cases"

    id = Column(String(50), primary_key=True)          # e.g. "case_ab12cd34ef56"
    domain = Column(String(10), nullable=False)        # 'live' | 'dead'
    loan_no = Column(Text)
    farmer_name = Column(Text)
    village = Column(Text)
    taluka = Column(Text)
    district = Column(Text)
    occupation = Column(Text)
    insurer_org = Column(Text)
    remarks = Column(Text)
    survey_date = Column(Text)
    address = Column(Text)
    sub_case_status = Column(Text)
    animal_type = Column(Text)
    age = Column(Text)
    gender = Column(Text)
    breed = Column(Text)
    tag_no = Column(Text)
    market_value = Column(Text)
    color = Column(Text)
    swish_of_tail = Column(Text)
    right_horn = Column(Text)
    left_horn = Column(Text)
    lactation = Column(Text)
    daily_milk = Column(Text)
    distinguishing_feature = Column(Text)
    sum_insured = Column(Text)
    policy_duration = Column(Text)
    premium_amt = Column(Text)
    drive_folder_id = Column(Text)
    drive_folder_link = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True))
    created_by_user_id = Column(String(50), index=True)   # NEW

class Capture(Base):
    __tablename__ = "captures"

    id = Column(Integer, primary_key=True, autoincrement=True)
    filename = Column(Text, nullable=False)
    case_id = Column(String(50), index=True)
    step_id = Column(String(100))
    source = Column(String(20), nullable=False)

    server_received_at = Column(Text, nullable=False)
    device_timestamp = Column(Text)
    device_time_source = Column(String(30))
    device_ntp_drift_ms = Column(BigInteger)           # BigInteger on purpose (see note below)
    device_clock_changed = Column(Integer)

    lat = Column(Float)
    lon = Column(Float)
    gps_accuracy_m = Column(Float)
    drift_server_vs_device_ms = Column(BigInteger)     # BigInteger on purpose

    exif_signals = Column(Text)
    client_frame_hash = Column(String(64))
    server_frame_hash = Column(String(64), index=True)  # index = fast duplicate-photo lookup
    frame_hash_match = Column(Integer)

    device_timezone = Column(String(64))
    timezone_mismatch_flag = Column(Integer)
    device_date_changed = Column(Integer)
    device_date_wrong_at_anchor = Column(Integer)

    resolution = Column(String(30))
    device_info = Column(Text)
    tamper_check_score = Column(Integer)
    tamper_check_band = Column(String(20))

    normalize_scale_applied = Column(Float)
    normalize_skipped = Column(Integer)
    normalize_skip_reason = Column(String(50))

    drive_file_id = Column(String(100))
    drive_file_link = Column(Text)

    created_at = Column(DateTime(timezone=True), server_default=func.now())