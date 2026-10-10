# REVIEW.md

Defects found in the starter `app/main.py`, checked against `openapi.yaml` rules R1-R10.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | Top of file | Imports duplicated (`Optional`, `BaseModel`, `MongoClient`); the docstring sits after imports so it is not a module docstring. | Reading the file; linters flag redefinitions. | Single import block, docstring first. |
| 2 | `compute_late_minutes` | Ignores overnight shifts (R1): start is built on the punch-in's own IST date, so a 02:00 punch-in for a 22:00 shift is treated as hugely negative/zero instead of late. | Employee with 22:00-06:00 shift punching in at 22:30 or 02:00. | Takes the attendance date as an argument and builds shift start from it. |
| 3 | `compute_work_hours` | `round(float, 2)` is banker's rounding on a binary float, not half-up (R4); e.g. 4.495h may round wrong. | Punch times giving exactly x.xx5 hours. | `Decimal(secs)/Decimal(3600)` quantized with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | No 30-minute threshold (R3): 10 minutes over returns 10. Shift end is always on the same date, wrong for overnight shifts. Hard-codes the date string handling. | Punch-out 10 min after shift end; overnight shift punch-out next morning. | Counts only if floor minutes >= 30; overnight end is next day. |
| 5 | `EmployeeIn` | No validation: any `emp_code`, email, lengths, `HH:MM` format, `joined_on` can be garbage, `shift_start == shift_end` allowed. | POST with `emp_code: "x"`, `shift_start: "99:99"`, `joined_on: "abc"` returns 201. | Field patterns/lengths, date validator, model validator for shift times. |
| 6 | `/health` | Never pings MongoDB, always "ok". | Stop MongoDB; health still 200. | `ping` command, 503 on failure. |
| 7 | `create_indexes` | Only one index; no unique index on `employees.emp_code`, no index for sort/filter on attendance. `@app.on_event` is deprecated. | Duplicate concurrent creates; `explain` shows COLLSCAN. | `lifespan` creating unique `emp_code`, `(emp_code,date)`, `(date,emp_code)`, `(status,date,emp_code)`, `(department,joined_on)` idempotently. |
| 8 | `create_employee` | Check-then-insert race: two concurrent requests can both pass `find_one`. `created_at` is a naive local `datetime.now()` and returned as a datetime, not epoch ms. | Fire parallel POSTs with the same code; read `created_at` in the response. | Rely on the unique index and catch `DuplicateKeyError` -> 409; UTC time, returned as epoch ms. |
| 9 | `list_employees` | `skip = page * page_size` skips the first page (page 1 returns the second page, usually empty). `total` ignores the department filter. No sort by `emp_code`. `page`/`page_size` not validated (0, negative, huge). | GET `/employees` returns empty with 6 employees loaded; filter by department shows wrong total. | `skip=(page-1)*page_size`, `count_documents(q)`, sort by `emp_code`, `Query(ge=1, le=100)`. |
| 10 | `punch_in` | `date` is the plain IST calendar date, ignoring the overnight rule (R1). Late computed with the old helper. | Overnight employee punching in at 02:00. | `attendance_date()` implements R1; late uses that date. |
| 11 | `to_ist_instant` / models | Stores `punched_at` as an IST-aware datetime and range-checks it by hand inside a helper; naive and aware datetimes get mixed because the client was not `tz_aware`. | Compare returned values with what Mongo stores. | `EpochMs` strict-int type (422 for floats/strings/seconds), UTC datetimes, `tz_aware=True`. |
| 12 | `list_attendance` | Loads the whole collection into memory and sorts in Python (slow at ~100k rows). No `emp_code` tiebreak in the sort. | Large collection; `explain` / latency. | `find().sort().skip().limit()` with an index, sort `date desc, emp_code asc`, `count_documents` with the same filter. |
| 13 | `list_attendance` output | Leaks MongoDB `_id` as `"id"`; returns raw datetimes instead of epoch ms. | Look at the JSON. | `record_out()` whitelists fields and converts instants to ms. |
| 14 | `list_attendance` params | `page`/`page_size` unvalidated (page 0 gives a negative slice); `status` not validated; `date_from` / `date_to` not validated and `date_from > date_to` not rejected. | `?page=0`, `?status=FOO`, `?date_from=2026-02-01&date_to=2026-01-01`. | Query constraints, `Literal` status, date parsing, 422 when from > to. |
| 15 | Client creation | `MongoClient` uses the 30 s default server-selection timeout and naive datetimes. | `/health` or requests hang when DB is down. | `serverSelectionTimeoutMS=5000`, `tz_aware=True`. |
| 16 | Filters (`if department:`, `if emp_code:`) | Truthiness checks silently ignore an empty string instead of matching exactly (`department=` should return nothing). | `GET /employees?department=` returns everyone. | Use `is not None` checks. |

## Looked at and decided NOT a defect

- `StrictInt` on `punched_at`: correctly rejects floats and strings (spec requires 422).
- `ms // 1000` truncation: matches R1 (truncate to whole seconds).
- `Literal["PRESENT","WFH","ON_DUTY"]` on punch-in: matches the spec (ABSENT/LEAVE are not creatable).
- Unique index `(emp_code, date)` plus `DuplicateKeyError` -> 409 on punch-in: the right way to handle the race.
- `201` status codes on the create endpoints: match the contract.
