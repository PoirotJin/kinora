# Roles, shared library and multi-user scheduling

An additive account migration grants the existing `jfc` account the `admin` role.
`jfc` is re-asserted as admin on every service start and cannot be disabled in
the app. Other accounts default to `user`. Passwords and existing session tokens
are preserved; roles are looked up from the account table on every request.

Admins retain production, scheduling, review, monitoring, archive/export and
user-creation access. The home page lists every user's custom job and queue state.
The account creation form creates ordinary users with a one-time displayed random
temporary password, requiring a password change on first login. Role selection is
not accepted from this form/API. Trusted CLI provisioning supports `--role`.

Ordinary users can submit, inspect and stop their own custom jobs, and view the
shared motion gallery and its playback. Job/result/cancel endpoints verify
ownership; a forged owner or role field cannot alter authorization. API policy is
default-deny for ordinary users. Authentication and password changes remain
available. The shared gallery is deliberately shared: it is not a private dataset
per user. Custom results are tagged with the submitting username so employees can
filter “我的作业”. Reviewer notes/history, seeds, task ids and error strings are
omitted from ordinary listing/detail responses. Ordinary accounts cannot call
review, deletion, production, monitoring, user administration or data-export
endpoints. Queue occupancy counts are admin-only. Ordinary users have a 5-hour
and weekly Qwen token cap.

## Queue

SQLite transactions serialize idempotent submission and disjoint seed reservation.
Each user may have at most 5 unfinished jobs and 10000 remaining candidates; the
global unfinished-job limit is 50. These limits are checked inside the submission
transaction. The frontend reuses the request ID after an uncertain response.

Persistent per-user last-service times implement rotation across users. Within a
user, jobs rotate by last update. Each GPU quantum contains at most two candidates;
automatic production retains its existing separate quantums. API preparation has
its own process lock and fair user turns. The global GPU lock is inherited by
inference subprocesses; the custom batch lock prevents two consumers selecting
the same queue. Cancellation is rechecked transactionally when claiming a job.

When GPU 0 free memory is below min(7500 MiB, 90% of total), custom generation stays
queued. A failed GPU status probe also retains queued work instead of trying to
load another model. This is a best-effort admission check, not a VRAM reservation
against unrelated processes outside MotionFarm. External jobs can still race with
the probe; inference errors remain isolated from the automatic production job.

Queue position is the current dynamic order for the next preparation/GPU quantum,
not a fixed completion position or ETA. Waiting has an indeterminate progress bar;
the completed-candidate bar reports committed batches / requested N. Position may
change as users arrive, jobs finish or resource availability changes. No inference
is preempted. A cancelled active batch finishes and retains its output.

## Removal

Admin deletion is reversible soft removal, recorded in the review SQLite database
with actor/time/reason and append-only removal/restore events. Removed motions
are hidden from lists, custom result lists, detail, thumbnail and preview routes.
Admins restore them from the home-page recycle bin. This does not rewrite PKL
shards, release disk space, erase vectors or revoke already downloaded copies.
Original archive/export packages may still contain removed motions and are admin
only. Physical storage totals continue to count retained data. The recycle-bin UI
shows the latest 100 removed records.

Tests use synthetic motion fixtures and mocked API/GPU execution. Ordinary/admin
browser tests run against an isolated temporary local server, which is stopped
after validation. No test accounts are left on the production server.
