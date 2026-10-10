"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
from typing import Annotated, Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Real environment variables take priority over .env
load_dotenv(override=False)

MONGO_URI = os.environ.get("MONGO_URI") or os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB") or os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_db():
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])

    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R10)
# --------------------------------------------------------------------------- #
def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt(ms: int) -> datetime:
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def compute_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """R1: Shift times and attendance dates are IST.
    Exception - overnight shifts (shift_end <= shift_start): a punch-in whose IST time
    is strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    ist_date = punch_in_ist.date()
    ist_time = punch_in_ist.time()

    h_start, m_start = map(int, shift_start.split(":"))
    h_end, m_end = map(int, shift_end.split(":"))
    is_overnight = (h_end, m_end) <= (h_start, m_start)

    if is_overnight and ist_time < time(h_end, m_end):
        return (ist_date - timedelta(days=1)).isoformat()
    return ist_date.isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, record_date: str) -> int:
    """R2: Grace period 10 minutes. Late only if strictly more than 10:00 after shift_start.
    Then whole minutes (floor) since shift_start, not since grace period ended.
    """
    h_start, m_start = map(int, shift_start.split(":"))
    d = date.fromisoformat(record_date)
    start_dt = datetime(d.year, d.month, d.day, h_start, m_start, 0, tzinfo=IST)
    diff_sec = (punch_in_dt - start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_overtime(punch_out_dt: datetime, shift_end: str, record_date: str, shift_start: str) -> int:
    """R3: Whole minutes (floor) between shift_end and punch_out, counted only if >= 30, else 0."""
    h_start, m_start = map(int, shift_start.split(":"))
    h_end, m_end = map(int, shift_end.split(":"))
    is_overnight = (h_end, m_end) <= (h_start, m_start)
    d = date.fromisoformat(record_date)
    if is_overnight:
        end_d = d + timedelta(days=1)
    else:
        end_d = d
    end_dt = datetime(end_d.year, end_d.month, end_d.day, h_end, m_end, 0, tzinfo=IST)
    diff_sec = (punch_out_dt - end_dt).total_seconds()
    ot_minutes = int(diff_sec // 60) if diff_sec > 0 else 0
    return ot_minutes if ot_minutes >= 30 else 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    sec = int((punch_out_dt - punch_in_dt).total_seconds())
    val = Decimal(sec) / Decimal(3600)
    return float(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up(val: float, decimals: int) -> float:
    d = Decimal(str(val))
    pattern = Decimal("0." + "0" * decimals)
    return float(d.quantize(pattern, rounding=ROUND_HALF_UP))


def count_working_days(start_str: str, end_str: str, joined_on_str: str) -> int:
    """R7: Mon-Fri working days, counted from joined_on for mid-month joiners."""
    start_d = max(date.fromisoformat(start_str), date.fromisoformat(joined_on_str))
    end_d = date.fromisoformat(end_str)
    if start_d > end_d:
        return 0
    cnt = 0
    cur = start_d
    while cur <= end_d:
        if cur.weekday() < 5:
            cnt += 1
        cur += timedelta(days=1)
    return cnt


def doc_to_attendance_record(doc: dict) -> dict:
    history_out = []
    for h in doc.get("history", []) or []:
        h_changes = {}
        for k, v in (h.get("changes") or {}).items():
            c_from = v.get("from")
            c_to = v.get("to")
            if isinstance(c_from, datetime):
                c_from = dt_to_epoch_ms(c_from)
            if isinstance(c_to, datetime):
                c_to = dt_to_epoch_ms(c_to)
            h_changes[k] = {"from": c_from, "to": c_to}
        history_out.append({
            "at": dt_to_epoch_ms(h["at"]),
            "by": h["by"],
            "reason": h["reason"],
            "changes": h_changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0) or 0,
        "overtime_minutes": doc.get("overtime_minutes", 0) or 0,
        "half_day": doc.get("half_day", False) or False,
        "history": history_out,
    }


def doc_to_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc["created_at"]),
    }


# --------------------------------------------------------------------------- #
# Request Schemas
# --------------------------------------------------------------------------- #
EpochMillisStrict = Annotated[int, Field(ge=100000000000, le=4102444800000, strict=True)]


class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "09:30"
    shift_end: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "18:30"
    joined_on: str


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillisStrict] = None
    status: Annotated[str, Field(pattern=r"^(PRESENT|WFH|ON_DUTY)$")] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillisStrict] = None


class RegularizeRequest(BaseModel):
    status: Optional[Annotated[str, Field(pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")]] = None
    punch_in: Optional[EpochMillisStrict] = None
    punch_out: Optional[EpochMillisStrict] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start must differ from shift_end")
    try:
        datetime.strptime(body.joined_on, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid joined_on date")

    now = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return doc_to_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [doc_to_employee(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = epoch_ms_to_dt(body.punched_at)
    else:
        punch_in_dt = datetime.now(timezone.utc).replace(microsecond=0)

    record_date = compute_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, emp["shift_start"], record_date)

    doc = {
        "emp_code": body.emp_code,
        "date": record_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return doc_to_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punched_at_dt = epoch_ms_to_dt(body.punched_at)
    else:
        punched_at_dt = datetime.now(timezone.utc).replace(microsecond=0)

    # Closes the employee's most recent record whose punch_in is at or before punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched_at_dt}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No matching punch-in found")

    punch_in_dt = record["punch_in"]
    if punched_at_dt <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")

    diff_sec = (punched_at_dt - punch_in_dt).total_seconds()
    if diff_sec > 24 * 3600:
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    work_hrs = compute_work_hours(punch_in_dt, punched_at_dt)
    ot_min = compute_overtime(punched_at_dt, emp["shift_end"], record["date"], emp["shift_start"])
    is_half = work_hrs < 4.50

    # Atomic update to guarantee single winner under concurrency
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punched_at_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": is_half,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    return doc_to_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = Query(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [doc_to_attendance_record(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    target_status = body.status if body.status is not None else record["status"]

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Punch times cannot be supplied for ABSENT or LEAVE")
        final_punch_in = None
        final_punch_out = None
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            final_punch_in = epoch_ms_to_dt(body.punch_in)
        else:
            final_punch_in = record.get("punch_in")

        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

        # punch_in must stay on record's attendance date per R1
        calc_date = compute_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

        if body.punch_out is not None:
            final_punch_out = epoch_ms_to_dt(body.punch_out)
        else:
            final_punch_out = record.get("punch_out")

        if final_punch_out is not None:
            if final_punch_out <= final_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            diff_sec = (final_punch_out - final_punch_in).total_seconds()
            if diff_sec > 24 * 3600:
                raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    # Recompute derived fields
    if target_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(final_punch_in, emp["shift_start"], date)
        if final_punch_out is not None:
            new_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            new_overtime_minutes = compute_overtime(final_punch_out, emp["shift_end"], date, emp["shift_start"])
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Determine changes
    old_status = record["status"]
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0) or 0
    old_overtime_minutes = record.get("overtime_minutes", 0) or 0
    old_half_day = record.get("half_day", False) or False

    changes = {}
    if target_status != old_status:
        changes["status"] = {"from": old_status, "to": target_status}
    if final_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": final_punch_in}
    if final_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": final_punch_out}
    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late_minutes != old_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if new_overtime_minutes != old_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    current_history_len = len(record.get("history", []) or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": current_history_len},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return doc_to_attendance_record(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    working_days = count_working_days(month_start, month_end, emp["joined_on"])

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$project": {
                "is_weekday": {
                    "$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [2, 3, 4, 5, 6],
                    ]
                },
                "is_present_status": {
                    "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                },
                "half_day": {"$ifNull": ["$half_day", False]},
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_weekday", "$is_present_status"]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        agg = res[0]
        present_days = float(agg["present_days"])
        leave_days = int(agg["leave_days"])
        late_count = int(agg["late_count"])
        total_late_minutes = int(agg["total_late_minutes"])
        total_overtime_minutes = int(agg["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up(present_days / working_days * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None),
):
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    match_stage = {"joined_on": {"$lte": month_end}}
    if department:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {
                                                "$in": [
                                                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}},
                                                    [2, 3, 4, 5, 6],
                                                ]
                                            },
                                        ]
                                    },
                                    {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "work_hours_records": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]},
                            ]
                        },
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "work_hours_sum": {"$sum": "$work_hours_records.work_hours"},
                "work_hours_count": {"$size": "$work_hours_records"},
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]

    res = list(db.employees.aggregate(pipeline))
    items = []
    for r in res:
        wh_cnt = r["work_hours_count"]
        if wh_cnt > 0:
            avg_wh = round_half_up(float(r["work_hours_sum"]) / wh_cnt, 2)
        else:
            avg_wh = None
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = Query(default=None),
):
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {
            "$match": {"rank": {"$lte": limit}}
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
    ])

    res = list(db.attendance_logs.aggregate(pipeline))
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in res
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    # Check department exists
    dept_emp_count = db.employees.count_documents({"department": department})
    if dept_emp_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    d_from = date.fromisoformat(from_)
    d_to = date.fromisoformat(to)
    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' date must be on or after 'from' date")
    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

    dept_emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]

    from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    to_dt_plus_1 = datetime(d_to.year, d_to.month, d_to.day, tzinfo=timezone.utc) + timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": dept_emp_codes},
                "date": {"$gte": from_, "$lte": to},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "date_obj": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1],
                },
            }
        },
        {"$sort": {"date_obj": 1}},
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj"}},
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": "$date_obj"},
                        [2, 3, 4, 5, 6],
                    ]
                },
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "cnt"},
                ],
                "as": "hc",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "present_count": 1,
                "late_count": 1,
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.cnt", 0]}, 0]},
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {
                                    "$trunc": [
                                        {
                                            "$add": [
                                                {"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]},
                                                0.5,
                                            ]
                                        }
                                    ]
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "raw_moving_avg": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$raw_moving_avg", None]},
                        {
                            "$divide": [
                                {
                                    "$trunc": [
                                        {
                                            "$add": [
                                                {"$multiply": ["$raw_moving_avg", 10000]},
                                                0.5,
                                            ]
                                        }
                                    ]
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in res:
        rate = r["attendance_rate"]
        mavg = r["moving_avg_7d"]
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": round_half_up(rate, 4) if rate is not None else None,
            "moving_avg_7d": round_half_up(mavg, 4) if mavg is not None else None,
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = Query(default=None),
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        skip = (page - 1) * page_size
        exp = db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
        year, m = map(int, month.split("-"))
        _, last_day = calendar.monthrange(year, m)
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"

        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
            {
                "$project": {
                    "is_weekday": {
                        "$in": [
                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                            [2, 3, 4, 5, 6],
                        ]
                    },
                    "is_present_status": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "status": 1,
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {"$and": ["$is_weekday", "$is_present_status"]},
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                }
            },
        ]
        exp = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        year, m = map(int, month.split("-"))
        _, last_day = calendar.monthrange(year, m)
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"

        match_stage = {"joined_on": {"$lte": month_end}}
        if department:
            match_stage["department"] = department

        pipeline = [
            {"$match": match_stage},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", month_start]},
                                        {"$lte": ["$date", month_end]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {
                "$project": {
                    "department": 1,
                    "emp_code": 1,
                    "present_days": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {
                                                    "$in": [
                                                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}},
                                                        [2, 3, 4, 5, 6],
                                                    ]
                                                },
                                            ]
                                        },
                                        {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            }
                        }
                    },
                    "work_hours_records": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {
                                "$and": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$$l.work_hours", None]},
                                ]
                            },
                        }
                    },
                    "late_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                            }
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {"$ifNull": ["$$l.late_minutes", 0]},
                            }
                        }
                    },
                    "leave_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {"$eq": ["$$l.status", "LEAVE"]},
                            }
                        }
                    },
                    "on_duty_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                            }
                        }
                    },
                }
            },
            {
                "$project": {
                    "department": 1,
                    "present_days": 1,
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                    "work_hours_sum": {"$sum": "$work_hours_records.work_hours"},
                    "work_hours_count": {"$size": "$work_hours_records"},
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "present_days": {"$sum": "$present_days"},
                    "work_hours_sum": {"$sum": "$work_hours_sum"},
                    "work_hours_count": {"$sum": "$work_hours_count"},
                    "late_count": {"$sum": "$late_count"},
                    "total_late_minutes": {"$sum": "$total_late_minutes"},
                    "leave_count": {"$sum": "$leave_count"},
                    "on_duty_count": {"$sum": "$on_duty_count"},
                }
            },
            {"$sort": {"_id": 1}},
        ]
        exp = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "employees", "explain": exp}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        year, m = map(int, month.split("-"))
        _, last_day = calendar.monthrange(year, m)
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"

        pipeline = [
            {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})

        pipeline.extend([
            {"$project": {"emp_code": "$_id", "name": "$emp.name", "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ])
        exp = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(status_code=422, detail="department, from, and to are required for department_trend")
        d_from = date.fromisoformat(from_)
        d_to = date.fromisoformat(to)
        if d_to < d_from or (d_to - d_from).days > 92:
            raise HTTPException(status_code=422, detail="Invalid date range")

        dept_emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]
        from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
        to_dt_plus_1 = datetime(d_to.year, d_to.month, d_to.day, tzinfo=timezone.utc) + timedelta(days=1)

        pipeline = [
            {"$match": {"emp_code": {"$in": dept_emp_codes}, "date": {"$gte": from_, "$lte": to}}},
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {"$project": {"date_obj": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
            {"$densify": {"field": "date_obj", "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt_plus_1]}}},
            {"$sort": {"date_obj": 1}},
            {
                "$project": {
                    "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj"}},
                    "is_working_day": {"$in": [{"$dayOfWeek": "$date_obj"}, [2, 3, 4, 5, 6]]},
                    "present_count": {"$ifNull": ["$present_count", 0.0]},
                    "late_count": {"$ifNull": ["$late_count", 0]},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"cur_date": "$date"},
                    "pipeline": [
                        {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$cur_date"]}]}}},
                        {"$count": "cnt"},
                    ],
                    "as": "hc",
                }
            },
            {
                "$project": {
                    "date": 1,
                    "is_working_day": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.cnt", 0]}, 0]},
                }
            },
            {
                "$project": {
                    "date": 1,
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": {
                        "$cond": [
                            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                            {
                                "$divide": [
                                    {
                                        "$trunc": [
                                            {
                                                "$add": [
                                                    {"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]},
                                                    0.5,
                                                ]
                                            }
                                        ]
                                    },
                                    10000,
                                ]
                            },
                            None,
                        ]
                    },
                }
            },
            {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"raw_moving_avg": {"$avg": "$attendance_rate", "window": {"documents": [-6, "current"]}}}}},
            {"$project": {"date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$cond": [{"$ne": ["$raw_moving_avg", None]}, {"$divide": [{"$trunc": [{"$add": [{"$multiply": ["$raw_moving_avg", 10000]}, 0.5]}]}, 10000]}, None]}}},
            {"$sort": {"date": 1}},
        ]
        exp = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}

    else:
        raise HTTPException(status_code=422, detail=f"Unknown endpoint: {endpoint}")
