# DECISIONS.md

1. **Indexes.**
On `employees`, we created unique `{ emp_code: 1 }` to guarantee business identity, `{ department: 1, joined_on: 1 }` for filtered headcount and trend queries, and `{ joined_on: 1 }` for global date matching. On `attendance_logs`, we created unique `{ emp_code: 1, date: 1 }` (guaranteeing one record per day), `{ date: -1, emp_code: 1 }` for paginated listing, `{ date: 1, late_minutes: 1 }` for leaderboard aggregations, and `{ emp_code: 1, punch_in: -1 }` for punch-outs. We rejected a standalone index on `{ status: 1 }` due to low cardinality and poor selectivity.

2. **Punch-in race.**
Both requests compute the attendance date and attempt `db.attendance_logs.insert_one(doc)`. The database engine enforces the compound unique index on `(emp_code, date)`: the first write commits with HTTP 201, while the second immediately triggers a `DuplicateKeyError`. Our code catches this exception and returns HTTP 409 Conflict without relying on racy check-then-insert lookups.

3. **Ties.**
The leaderboard returns all employees whose `rank <= limit`. We compute ranks using standard competition ranking via `$rank` in `$setWindowFields` (e.g., 1, 2, 2, 4). When employees tie at or below the limit threshold, both receive the shared rank and are returned, ordered secondarily by `emp_code` ascending.

4. **Headcount.**
The pipeline originates from the `employees` collection matching `joined_on <= month_end`, followed by a `$lookup` into `attendance_logs`. Employees with zero records simply yield an empty logs array but remain in the pipeline, ensuring their headcount is accurately tallied.

5. **One thing you would change.**
At 100x data volume, we would maintain pre-aggregated daily summaries using MongoDB change streams or scheduled rollup jobs, eliminating expensive runtime `$lookup` and multi-stage pipeline traversals across tens of millions of raw documents.
