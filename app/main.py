# app/main.py
"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded; real environment variables win)

Done: foundation, employees, punch-in/out, list, PATCH.
All endpoints of the contract are implemented.
"""
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_cls, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)  # real environment variables win over .env

# tz_aware=True: BSON datetimes come back timezone-aware (UTC), not naive.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
MIN_MS = 100_000_000_000
MAX_MS = 4_102_444_800_000

# Epoch milliseconds: strict int (rejects floats/strings/bools) within range.
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    create_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan
)


def create_indexes():
    """Idempotent: create_index is a no-op when the index already exists."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined"
    )
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date"
    )
    # list /attendance: sort date desc, emp_code asc (also serves date range filters)
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp"
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="status_date_emp",
    )
    # punch-out: latest record of an employee with punch_in <= punched_at
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in"
    )


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def now_dt() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def parse_hhmm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date_cls:
    """R1: IST date of punch_in; overnight shifts: punch-in earlier than
    shift_end belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < parse_hhmm(shift_end):
        d -= timedelta(days=1)
    return d


def shift_start_dt(d: date_cls, shift_start: str) -> datetime:
    return datetime.combine(d, parse_hhmm(shift_start), tzinfo=IST)


def shift_end_dt(d: date_cls, shift_start: str, shift_end: str) -> datetime:
    end = datetime.combine(d, parse_hhmm(shift_end), tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return end


def parse_date(s: str, name: str = "date") -> date_cls:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        return date_cls.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, f"{name} is not a valid date")


# --------------------------------------------------------------------------- #
# Business-rule helpers (R2-R5)
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, att_date: date_cls, shift_start: str) -> int:
    """R2: late only if strictly > 10:00 after shift_start; then floor minutes
    since shift_start (not since end of grace)."""
    delta = int((punch_in - shift_start_dt(att_date, shift_start)).total_seconds())
    return delta // 60 if delta > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds/3600 rounded to 2 decimals HALF-UP using Decimal."""
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def compute_overtime(
    punch_out: datetime, att_date: date_cls, shift_start: str, shift_end: str
) -> int:
    """R3: floor minutes after shift_end, counted only when >= 30."""
    end = shift_end_dt(att_date, shift_start, shift_end)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: rounded work_hours < 4.50."""
    return work_hours is not None and work_hours < 4.5


# --------------------------------------------------------------------------- #
# Serialisers: never expose _id, instants as epoch ms
# --------------------------------------------------------------------------- #
TIME_FIELDS = {"punch_in", "punch_out"}


def employee_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "name": d["name"],
        "email": d["email"],
        "department": d["department"],
        "shift_start": d["shift_start"],
        "shift_end": d["shift_end"],
        "joined_on": d["joined_on"],
        "created_at": to_ms(d["created_at"]),
    }


def history_out(entries: list) -> list:
    out = []
    for e in entries:
        changes = {}
        for k, v in e["changes"].items():
            if k in TIME_FIELDS:
                v = {"from": to_ms(v.get("from")), "to": to_ms(v.get("to"))}
            changes[k] = v
        out.append(
            {"at": to_ms(e["at"]), "by": e["by"], "reason": e["reason"], "changes": changes}
        )
    return out


def record_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": to_ms(d.get("punch_in")),
        "punch_out": to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes", 0),
        "overtime_minutes": d.get("overtime_minutes", 0),
        "half_day": d.get("half_day", False),
        "history": history_out(d.get("history", [])),
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date_cls.fromisoformat(v)  # ValueError -> 422 for e.g. 2026-02-30
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_dt()
    try:
        db.employees.insert_one(doc)  # unique index decides, no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    cur = (
        db.employees.find(q)
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_out(d) for d in cur],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()
    att_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, att_date, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    """Shared by GET /attendance and the explain endpoint."""
    if date_from is not None:
        parse_date(date_from, "date_from")
    if date_to is not None:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cur = (
        db.attendance_logs.find(q)
        .sort(ATTENDANCE_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [record_out(d) for d in cur],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Shared helpers for punch-out / regularize
# --------------------------------------------------------------------------- #
def derive(emp: dict, att_date: date_cls, punch_in, punch_out) -> dict:
    """Recompute all derived fields (R2-R5) from the final punch times."""
    if punch_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, att_date, emp["shift_start"])
    if punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    wh = compute_work_hours(punch_in, punch_out)
    return {
        "late_minutes": late,
        "work_hours": wh,
        "overtime_minutes": compute_overtime(
            punch_out, att_date, emp["shift_start"], emp["shift_end"]
        ),
        "half_day": compute_half_day(wh),
    }


def validate_out(punch_in: datetime, punch_out: datetime) -> None:
    if punch_out <= punch_in:
        raise HTTPException(422, "punch_out must be after punch_in")
    if punch_out - punch_in > timedelta(hours=24):
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")


# --------------------------------------------------------------------------- #
# Punch-out
# --------------------------------------------------------------------------- #
class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()

    # most recent record with punch_in <= punched_at (works across midnight)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    validate_out(rec["punch_in"], ts)

    derived = derive(emp, date_cls.fromisoformat(rec["date"]), rec["punch_in"], ts)
    # Atomic compare-and-set: only succeeds while punch_out is still null,
    # so two simultaneous punch-outs give exactly one 200.
    res = db.attendance_logs.update_one(
        {
            "emp_code": rec["emp_code"],
            "date": rec["date"],
            "punch_in": rec["punch_in"],
            "punch_out": None,
        },
        {"$set": {"punch_out": ts, **derived}},
    )
    if res.matched_count == 0:
        raise HTTPException(409, "already punched out")
    rec.update({"punch_out": ts, **derived})
    return record_out(rec)


# --------------------------------------------------------------------------- #
# Regularize (manual correction + audit trail)
# --------------------------------------------------------------------------- #
class RegularizeIn(BaseModel):
    # Derived fields are not declared, so if a client sends them they are ignored.
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


CHANGE_FIELDS = [
    "status", "punch_in", "punch_out",
    "work_hours", "late_minutes", "overtime_minutes", "half_day",
]


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    att_date = parse_date(date, "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "attendance record not found")

    status = body.status or rec["status"]
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if body.punch_in is not None and attendance_date(
            new_in, emp["shift_start"], emp["shift_end"]
        ) != att_date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None:
            validate_out(new_in, new_out)

    new_vals = {"status": status, "punch_in": new_in, "punch_out": new_out}
    new_vals.update(derive(emp, att_date, new_in, new_out))

    changes = {}
    for f in CHANGE_FIELDS:
        if rec.get(f) != new_vals[f]:
            changes[f] = {"from": rec.get(f), "to": new_vals[f]}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    entry = {
        "at": now_dt(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    old_history = rec.get("history", [])

    # Optimistic lock: the update only applies if the record is exactly as we
    # read it (same history length and same times/status). Otherwise 409.
    res = db.attendance_logs.update_one(
        {
            "emp_code": emp_code,
            "date": date,
            "history": {"$size": len(old_history)},
            "status": rec["status"],
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
        },
        {"$set": new_vals, "$push": {"history": entry}},
    )
    if res.matched_count == 0:
        raise HTTPException(409, "record was modified concurrently, retry")

    rec.update(new_vals)
    rec["history"] = old_history + [entry]
    return record_out(rec)


# --------------------------------------------------------------------------- #
# Analytics helpers
# --------------------------------------------------------------------------- #
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
EPOCH_FALLBACK = datetime(1970, 1, 1)


def month_bounds(month: str):
    """First and last calendar day of a YYYY-MM month."""
    try:
        y, m = int(month[:4]), int(month[5:7])
        first = date_cls(y, m, 1)
        nxt = date_cls(y + 1, 1, 1) if m == 12 else date_cls(y, m + 1, 1)
    except ValueError:
        raise HTTPException(422, "invalid month")
    return first, nxt - timedelta(days=1)


def count_working_days(first: date_cls, last: date_cls, joined_on: str) -> int:
    """R7: Mon-Fri in [first, last], counted from joined_on if later."""
    start = max(first, date_cls.fromisoformat(joined_on))
    n, d = 0, start
    while d <= last:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def round_half_up(value, places: int = 2) -> float:
    """R8: half-up rounding via Decimal."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, ROUND_HALF_UP))


# Aggregation expression builders. `p` is the field prefix: "" for attendance
# documents, "logs." for lookup-joined logs inside the department pipeline.
def is_present(p: str) -> dict:
    return {"$in": [f"${p}status", PRESENCE]}


def is_weekday(p: str) -> dict:
    # $dayOfWeek: 1=Sunday .. 7=Saturday, so Mon-Fri = 2..6
    dow = {"$dayOfWeek": {"$dateFromString": {"dateString": f"${p}date", "onNull": EPOCH_FALLBACK}}}
    return {"$in": [dow, [2, 3, 4, 5, 6]]}


def present_value(p: str) -> dict:
    """1 for a present weekday record, 0.5 for a half day, else 0 (R5-R7)."""
    return {
        "$cond": [
            {"$and": [is_present(p), is_weekday(p)]},
            {"$cond": [f"${p}half_day", 0.5, 1]},
            0,
        ]
    }


def count_if(cond: dict) -> dict:
    return {"$sum": {"$cond": [cond, 1, 0]}}


def late_gt0(p: str) -> dict:
    return {"$gt": [f"${p}late_minutes", 0]}


# --------------------------------------------------------------------------- #
# GET /analytics/employees/{emp_code}/monthly
# --------------------------------------------------------------------------- #
def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": present_value("")},
                "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
                "late_count": count_if(late_gt0("")),
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    first, last = month_bounds(month)
    working_days = count_working_days(first, last, emp["joined_on"])
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    r = rows[0] if rows else {}
    present = float(r.get("present_days", 0))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present),
        "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        "attendance_pct": (
            None if working_days == 0 else round_half_up(Decimal(str(present)) / working_days * 100)
        ),
    }


# --------------------------------------------------------------------------- #
# GET /analytics/departments/summary
# --------------------------------------------------------------------------- #
def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    match = {"joined_on": {"$lte": last.isoformat()}}  # R9 headcount
    if department is not None:
        match["department"] = department
    p = "logs."
    has_hours = {"$and": [is_present(p), {"$isNumber": "$logs.work_hours"}]}
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}}],
                "as": "logs",
            }
        },
        # preserveNullAndEmptyArrays keeps employees with zero logs (headcount!)
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "emps": {"$addToSet": "$emp_code"},
                "present_days": {"$sum": present_value(p)},
                "late_count": count_if(late_gt0(p)),
                "total_late_minutes": {"$sum": "$logs.late_minutes"},
                "leave_count": count_if({"$eq": ["$logs.status", "LEAVE"]}),
                "on_duty_count": count_if({"$eq": ["$logs.status", "ON_DUTY"]}),
                # hours in hundredths as integers so the average is exact
                "work_hundredths": {
                    "$sum": {
                        "$cond": [
                            has_hours,
                            {"$toLong": {"$round": [{"$multiply": ["$logs.work_hours", 100]}, 0]}},
                            0,
                        ]
                    }
                },
                "work_n": count_if(has_hours),
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=MONTH_PATTERN),
    department: Optional[str] = None,
):
    items = []
    for r in db.employees.aggregate(department_summary_pipeline(month, department)):
        n = r["work_n"]
        avg = None
        if n:
            avg = float(
                (Decimal(r["work_hundredths"]) / Decimal(n) / Decimal(100)).quantize(
                    Decimal("0.01"), ROUND_HALF_UP
                )
            )
        items.append(
            {
                "department": r["_id"],
                "headcount": len(r["emps"]),
                "present_days": round_half_up(float(r["present_days"])),
                "avg_work_hours": avg,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# GET /analytics/leaderboard/late
# --------------------------------------------------------------------------- #
def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipe = [
        {"$match": {
            "date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are dropped
    ]
    if department is not None:
        pipe.append({"$match": {"emp.department": department}})  # rank inside the department
    pipe += [
        # $rank = standard competition ranking (1, 2, 2, 4)
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies to the rank, not the position
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return pipe


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    rows = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": rows}


# --------------------------------------------------------------------------- #
# GET /analytics/departments/{department}/trend
# --------------------------------------------------------------------------- #
SENTINEL = datetime(1900, 1, 1)


def validate_trend_range(from_: str, to: str):
    d_from, d_to = parse_date(from_, "from"), parse_date(to, "to")
    if d_to < d_from:
        raise HTTPException(422, "to must be >= from")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    return d_from, d_to


def department_trend_pipeline(department: str, d_from: date_cls, d_to: date_cls) -> list:
    lo = datetime(d_from.year, d_from.month, d_from.day)
    hi = lo + timedelta(days=(d_to - d_from).days + 1)  # $densify upper bound is exclusive
    p = "logs."
    present_any = {"$cond": [is_present(p), {"$cond": ["$logs.half_day", 0.5, 1]}, 0]}
    day_str = {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}}
    window = {"documents": [-6, "current"]}
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}}],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$logs.date",
            "present_count": {"$sum": present_any},
            "late_count": count_if(late_gt0(p)),
        }},
        # a null key (employees without logs) becomes a sentinel date outside the
        # range, so $densify always has input; it is filtered out below
        {"$project": {
            "_id": 0,
            "d": {"$dateFromString": {"dateString": "$_id", "onNull": SENTINEL}},
            "present_count": 1,
            "late_count": 1,
        }},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$match": {"d": {"$gte": lo, "$lt": hi}}},
        {"$addFields": {"day": day_str}},
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$day"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$addFields": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": "$d"}, [2, 3, 4, 5, 6]]},
        }},
        # rate in ten-thousandths, rounded half-up with integer maths:
        # floor((10000*2p + h) / (2h)) avoids float ties
        {"$addFields": {
            "rate_k": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$floor": {"$divide": [
                    {"$add": [{"$multiply": [10000, {"$multiply": [2, "$present_count"]}]}, "$headcount"]},
                    {"$multiply": [2, "$headcount"]},
                ]}},
                None,
            ]},
        }},
        {"$addFields": {"nn": {"$cond": [{"$eq": ["$rate_k", None]}, 0, 1]}}},
        {"$setWindowFields": {
            "sortBy": {"d": 1},
            "output": {
                "sum_k": {"$sum": "$rate_k", "window": window},
                "n": {"$sum": "$nn", "window": window},
            },
        }},
        {"$sort": {"d": 1}},
        {"$project": {
            "_id": 0,
            "date": day_str,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": {"$cond": [{"$eq": ["$rate_k", None]}, None, {"$divide": ["$rate_k", 10000]}]},
            "moving_avg_7d": {"$cond": [
                {"$eq": ["$n", 0]},
                None,
                {"$divide": [
                    {"$floor": {"$divide": [
                        {"$add": [{"$multiply": [2, "$sum_k"]}, "$n"]},
                        {"$multiply": [2, "$n"]},
                    ]}},
                    10000,
                ]},
            ]},
        }},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    d_from, d_to = validate_trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "department not found")
    items = list(db.employees.aggregate(department_trend_pipeline(department, d_from, d_to)))
    for it in items:
        it["present_count"] = float(it["present_count"])
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# GET /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
def run_explain(command: dict) -> dict:
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))


def need(**params):
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, f"missing required parameter(s): {', '.join(missing)}")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # Each branch uses the SAME builder the real endpoint uses.
    if endpoint == "attendance_list":
        q = build_attendance_query(emp_code, date_from, date_to, status)
        command = {
            "find": "attendance_logs",
            "filter": q,
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        need(emp_code=emp_code, month=month)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": employee_monthly_pipeline(emp_code, month), "cursor": {}}
    elif endpoint == "department_summary":
        need(month=month)
        collection = "employees"
        command = {"aggregate": collection, "pipeline": department_summary_pipeline(month, department), "cursor": {}}
    elif endpoint == "late_leaderboard":
        need(month=month)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": late_leaderboard_pipeline(month, limit, department), "cursor": {}}
    else:  # department_trend
        need(department=department, **{"from": from_, "to": to})
        d_from, d_to = validate_trend_range(from_, to)
        collection = "employees"
        command = {"aggregate": collection, "pipeline": department_trend_pipeline(department, d_from, d_to), "cursor": {}}
    return {"endpoint": endpoint, "collection": collection, "explain": run_explain(command)}