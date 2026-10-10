# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the HROne assignment. All code is in `app/main.py`.

## Run

```bash
pip install -r requirements.txt
# create a .env (not committed) with:
#   MONGO_URI=mongodb://localhost:27017
#   MONGO_DB=attendance_db
python -m uvicorn app.main:app --port 8000
```

Needs Python 3.11+ and MongoDB 6.0+ (Atlas free tier works). Environment variables
override `.env`. Indexes are created idempotently at startup. Docs: `http://localhost:8000/docs`.

## What is implemented

All endpoints in `openapi.yaml`: health, employees (create/list), punch-in, punch-out,
attendance list, PATCH regularization with audit history, employee monthly summary,
department summary, late leaderboard, department trend, and `/admin/explain/{endpoint}`.

Notes:
- Uniqueness and races (employee create, punch-in, punch-out) rely on unique indexes and
  conditional updates, not check-then-insert. PATCH uses an optimistic lock (409 for the loser).
- Rounding uses `Decimal` half-up (or integer maths inside pipelines).
- The 7-day moving average is the mean of the reported (rounded) `attendance_rate` values.
- `REVIEW.md` lists the defects found in the starter code; `DECISIONS.md` has the design answers.

## Testing status

(Edit this honestly before submitting.)

- Tested by hand in `/docs` with the sample data: ...
- Not tested / known gaps: ...
