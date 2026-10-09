# PipelineDebug

Debugging tools for MAA task pipeline recognition. Both are standalone Python 3 scripts that
use the standard library only, print progress to stderr, and keep machine readable results on
stdout.

| Tool | Purpose | Input | Output |
|------|---------|-------|--------|
| `timeline_analyzer.py` | Align post-run evidence into one timeline and point at the usual failure layers | A directory with `core.log`, `events.log`, `snap_*.png` | `timeline.json` + `timeline.md` |
| `node_eval.py` | Run one pipeline node's recognition against a single screenshot, no device involved | A screenshot (PNG) + a pipeline node name | One line of JSON on stdout |

## timeline_analyzer.py

Aligns the three kinds of evidence a MAA run produces into one timeline:

- `core.log`: engine log, its millisecond timestamps are the main axis
- `events.log`: event log, one JSON object per line
- `snap_*.png`: sequential screenshots, aligned by file mtime

Any of the three may be missing; the analysis degrades but still runs. Events coming from
`core.log` and from `events.log` are merged when they describe the same callback. The report
contains five diagnostic anchors - connection, threshold, semantics, entry and errors - each
listing the events that belong to it, so a "the task ran but the result is wrong" investigation
has a starting point.

```shell
python3 tools/PipelineDebug/timeline_analyzer.py --dir /path/to/evidence --out /tmp/timeline-out
```

| Argument | Meaning |
|----------|---------|
| `--dir` | Evidence directory (required) |
| `--out` | Output directory, default `/tmp/timeline-out/`; must not be inside `--dir` |
| `--node` | Keep only events whose node / `details.task` / `details.subtask` / `name` contains this substring (case-insensitive) |
| `--msg` | Keep only events whose message name contains this substring, e.g. `SubTaskStart` |
| `--level` | Keep only events with one of these log levels, e.g. `INF,WRN,ERR`; plain `events.log` lines and snapshots count as `INF` |
| `--json-only` | Write `timeline.json` only |

Output: `timeline.json` (machine readable: input inventory, merged timeline, diagnostic
anchors) and `timeline.md` (the same content as a readable report). Exit codes: `0` success,
`2` invalid `--dir` / `--out`. The text inside the generated report is currently Chinese;
field names in `timeline.json` are English.

## node_eval.py

Runs a single pipeline node's recognition against one static screenshot and returns what the
engine saw: `score`, `rect`, `roi`, plus the algorithm and template the node used. No
controller and no device are involved - MAA recognition depends on the image only - so it
works without an emulator, which makes it usable for threshold tuning, template comparison
and recognition triage.

It requires a MaaCore dynamic library that exports the experimental C API `AsstEvalTaskNode`
(offline evaluation of one node). The script reports an environment error if the library in
use does not provide it.

```shell
python3 tools/PipelineDebug/node_eval.py --node AutoRaise@SelectAll --image snap_003.png
python3 tools/PipelineDebug/node_eval.py --node AutoRaise@SelectAll --image snap.png --maa-root ~/MAA
```

| Argument | Meaning |
|----------|---------|
| `--node` | Pipeline node name, e.g. `AutoRaise@SelectAll` (required) |
| `--image` | Screenshot to evaluate, PNG (required) |
| `--maa-root` | MAA root: a directory holding `resource/` and a MaaCore library. When omitted, the script probes upward from the current working directory for a directory containing `resource/tasks/` and stops with an error if there is none |
| `--timeout` | Reference value only: it is written to the output, the evaluation itself is not interrupted |

Output: one line of JSON on stdout, progress on stderr:

```json
{"tool": "node_eval v1", "node": "AutoRaise@SelectAll",
 "screenshot": {"path": "...", "width": 1280, "height": 720},
 "maa_version": "...", "raw": {"ok": true, "matched": true, "task": "AutoRaise@SelectAll",
 "algorithm": "MatchTemplate", "rect": [1223, 32, 31, 16], "roi": [1223, 32, 31, 16],
 "result": {"score": 0.999991, "template": "..."}}, "duration_ms": 412}
```

`raw` mirrors the engine response: `{"ok": false, "error": "imread_failed" | "task_not_found"}`
for bad input, `{"ok": true, "matched": false}` when the node was evaluated but did not match.
Engine logs (`core.log`) are written to `/tmp/node-eval-logs/`, events that would otherwise
flood the single JSON line on stdout are redirected there. Exit codes: `0` evaluation finished
(matched or not), `1` environment error (no library, missing API, resource load or instance
creation failed), `2` the evaluation call itself failed.

## Requirements

Python 3 with the standard library. `node_eval.py` additionally needs a MaaCore build (macOS:
`libMaaCore.dylib`) and its companion libraries next to it, plus the MAA `resource/` tree;
`timeline_analyzer.py` has no other requirement.

## Typical workflow

1. Run the pipeline you are debugging and collect `core.log`, `events.log` and screenshots.
2. `timeline_analyzer.py` narrows down which node and which moment went wrong.
3. `node_eval.py` replays that single node on the screenshot from step 2, to inspect the score
   and the matched rectangle in isolation.
