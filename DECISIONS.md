# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (uniqueness and joins), `(department, joined_on)` and `joined_on` (headcount). `attendance_logs`: unique `(emp_code, date)` (punch-in race, monthly report, PATCH), `(date, emp_code)` (list sort, leaderboard date range), `(status, date, emp_code)` (status filter), `(emp_code, punch_in)` (punch-out lookup). Rejected: an index on `late_minutes`; the date range already narrows the scan enough.

2. **Punch-in race.** Both requests compute the same date and call `insert_one`. The unique `(emp_code, date)` index lets exactly one succeed; the other gets `DuplicateKeyError`, which I return as 409. No check-then-insert.

3. **Ties.** `$rank` gives 1, 2, 2, 4, and I keep rows with `rank <= limit`, so everyone tied at the cutoff is returned, even if that is more than `limit` rows.

4. **Headcount.** The pipeline starts from `employees`, joins logs with `$lookup`, and uses `$unwind` with `preserveNullAndEmptyArrays`, so people with no logs stay. Headcount is the distinct employees per department.

5. **At 100x.** Pre-aggregate daily department totals into a summary collection at write time, and replace skip pagination with keyset pagination.
