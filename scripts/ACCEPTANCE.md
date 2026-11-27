# MotionFarm workflow acceptance

## Fast isolated regression

From the project root, using the existing MotionFarm Python environment:

```
node scripts/test_preview_render.js
PYTHONPATH=.:scripts OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m unittest scripts.test_workflow_acceptance -v
```

`test_workflow_acceptance` runs a loopback HTTP server, two ordinary-user accounts,
SQLite, durable queues, tag attribution, search permissions, personal presets,
execution snapshots, download permissions, cancellation and request retries.
Every motion and result is a tiny synthetic array in a temporary directory.
It **does not** validate HY generation, solver output or real browser rendering.

`test_preview_render.js` executes the actual app renderer in a small Canvas mock.
It guards zero-size layout, transient draw exceptions, animation-loop survival
and timecode updates. It is not a Safari/WebGL screenshot test.

## Live execution acceptance (explicit opt-in)

Use two dedicated ordinary QA accounts with non-temporary passwords. Obtain their
existing browser sessions and scoped Gadget keys using normal administrative or
login APIs. Set `MF_A_SESSION`, `MF_B_SESSION`, `MF_A_KEY`, `MF_B_KEY` in the process
environment. Do not put them in scripts, reports, shell history or source control.

```
python scripts/acceptance_live.py --base http://127.0.0.1:18765 \
  --state /tmp/motionfarm-live-acceptance.json --allow-live-work
```

Use an allowed origin and valid TLS when remote. If using a loopback transport behind
a proxy, set `MF_ORIGIN` to the configured public origin. Do not disable TLS verification.
The runner persists IDs before submitting; repeat the same command/state path to
resume after jobs progress. It performs one 2-second exact-English generation,
one retarget job with a versioned preset, a cancellation probe, and read/permission
checks. It never starts/stops automatic production or deletes results.
The jobs, preset and acceptance tag remain as an audit trail. Job IDs and
results, never credentials, are recorded in the state file.

Only `stage: complete` demonstrates the live API sequence succeeded. A pending,
failed or interrupted run must not be called passed. Inspect both job statuses
and the `checks` list. Idempotent transport retry and reconnect are tested;
forcing a model/solver crash and its automatic retry is not tested by this runner.

## Browser pass

On a supported browser, select the resulting motion, verify nonzero timecode,
visible skeleton, pause/resume/seek, and change between review/Gallery while
loading. Select the saved preset by name, inspect version and weights, open
retarget preview and verify downloaded NPZ matches the report hash.
Repeat browsing with the second ordinary account and verify private motion,
preset, job and download remain inaccessible. Browser visual and interaction
results should be recorded separately from API assertions. Neither transport
success nor a visible animation certifies physical-robot safety.
