# Bounded output/history memory measurements (issue #80)

Date: 2026-09-28. Runner revision `ceab65e` + issue-80 changes.
Method: direct A/B of the old capture path (`subprocess.run` with
`PIPE`, full strings retained) vs the new path
(`automation/bounded_output.py:run_bounded`: child streams to spool
files, only bounded head/tail slices enter memory). Representative
workload shapes from the issue: large test/build logs
(`seq`-generated multi-MB stdout), repeated shell calls (20x), and a
fail -> inspect -> fix -> rerun loop (covered in
`automation/test_bounded_output.py::test_failing_test_inspect_fix_rerun_sequence`).

Scope note: these numbers isolate the runner-side capture/retention
effect (dependency stripping from #79 is a separate stream and is not
mixed in here, per the issue's "measured and documented separately"
requirement). Fork-binary peak RSS (the ~600-615 MB real-agent number
from #52) is unchanged by this runner-side work; the fork-side effect
(Task-tool truncation, Changes 1-2 in
`automation/patches/issue-80-fork-change.md`) must be measured with
`memory_benchmark.py` + Docker 512 MB trials when that change lands.

## Results

| Workload | Old retained | New retained | Ratio | Tail (error) preserved |
|---|---|---|---|---|
| Single ~2.0 MB build log | 1,988,895 B | 32,822 B | 0.017 | yes (`300000` final line) |
| Single ~3.4 MB log (tracemalloc) | peak 9,946 KB | peak 394 KB | 0.040 | yes (`500000` final line) |
| 20x repeated ~0.6 MB shell calls | 11,777,900 B total | 656,440 B total | 0.056 | yes |
| 6 jobs x 1 MB outputs, cap 3 | unbounded growth | <= 3 records, <= 3 workspaces | bounded | yes (`TAIL-MARKER`) |
| Failing 800 KB test log in rerun loop | full log in record | <= 4,200 chars, `FAIL test_big.py::test_x` kept | bounded | yes |

Capture-time overhead of spooling vs PIPE was within noise on this
host (both ~0.01 s for the 2 MB case, ~0.1 s for the 20x loop):
spooling trades temp-file IO for O(bound) memory, which is the
correct trade on a 512 MB worker with an ephemeral filesystem.

## Bounds now enforced (all configurable, all fail closed to defaults)

- `RUNNER_MAX_CAPTURE_BYTES` (default 2 MiB): per-stream spool budget.
- `RUNNER_MAX_STREAM_CHARS` (default 32768): per-stream in-memory bound.
- `RUNNER_MAX_OUTPUT_CHARS` (default 4000): terminal result bound,
  head (~5/8) + tail (~3/8) with an explicit omission marker.
- `RUNNER_MAX_RETAINED_JOBS` (default 20): terminal job retention cap
  with workspace deletion on eviction.
- `RUNNER_SPOOL_DIR`: spool directory override (default system temp).

## Raw data

`bounded-output-issue-80-run-36452901431.json` (this directory).
