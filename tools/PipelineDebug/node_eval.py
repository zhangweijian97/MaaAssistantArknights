#!/usr/bin/env python3
"""node_eval.py - offline recognition evaluator for MAA pipeline nodes (standard library only)

Feeds one static screenshot plus a pipeline node name to MaaCore and prints the recognition
result. No controller and no device are involved: MaaCore recognition depends on the image
only. Useful for threshold tuning, template comparison and recognition triage.

C API used (libMaaCore): AsstSetUserDir / AsstLoadResource / AsstCreateEx / AsstEvalTaskNode
/ AsstDestroy / AsstGetVersion. AsstEvalTaskNode is an experimental C API, so the MaaCore
build in use has to export it; otherwise the tool stops with an environment error.

stdout carries exactly one line of JSON (machine readable), progress goes to stderr:
  {"tool":"node_eval v1","node":"...","screenshot":{"path":"...","width":W,"height":H},
   "maa_version":"...","raw":<JSON object returned by MaaCore>,"duration_ms":N}
Shapes of "raw":
  {"ok":false,"error":"imread_failed"|"task_not_found",...}
  {"ok":true,"matched":false,"task":"..."}                                 # not matched
  {"ok":true,"matched":true,"task":"...","algorithm":"MatchTemplate",
   "rect":[x,y,w,h],"roi":[x,y,w,h],"result":{"score":s,"template":"...",...}}

Flow: locate MaaCore -> AsstSetUserDir -> AsstLoadResource -> AsstCreateEx (no connect!)
      -> AsstEvalTaskNode -> AsstDestroy.
AsstSetUserDir comes before AsstLoadResource on purpose: the engine logger is created by the
first resource load, so the order decides where core.log lands.

AsstEvalTaskNode is a two-stage call: first with buff=None to obtain the required buffer size
(trailing '\0' included), then with an allocated buffer. A return value of NullSize (2**64-1)
means failure.

Usage:
  python3 node_eval.py --node AutoRaise@SelectAll --image snap_003.png
  python3 node_eval.py --node AutoRaise@SelectAll --image snap.png --maa-root ~/MAA

Exit codes: 0 = evaluation finished (raw produced, matched or not);
            1 = environment error (no library found / API missing / resource load failed /
                instance creation failed);
            2 = evaluation call failed (NullSize / invalid JSON).
"""

import argparse
import ctypes
import json
import os
import struct
import sys
import time
from pathlib import Path

# AsstSize NullSize (static_cast<AsstSize>(-1))
SIZE_MAX = 2**64 - 1
# Engine working directory (holds core.log; kept out of the repository)
DEFAULT_USER_DIR = Path("/tmp/node-eval-logs")
# A MAA root is a directory holding resource/tasks/ (checkout root or build tree root)
MAA_ROOT_MARKER = Path("resource") / "tasks"
# Library locations relative to a MAA root, most common first
LIBRARY_CANDIDATES = (
    "install-arm64/libMaaCore.dylib",
    "install/libMaaCore.dylib",
    "build-arm64/bin/libMaaCore.dylib",
    "build/bin/libMaaCore.dylib",
    "build/MaaCore.xcframework/macos-arm64/libMaaCore.dylib",
    "libMaaCore.dylib",
)
# AsstMsg callback signature; eval never emits task callbacks, so the body can stay empty
CALLBACK_TYPE = ctypes.CFUNCTYPE(None, ctypes.c_int32, ctypes.c_char_p, ctypes.c_void_p)


def log(msg):
    print(f"[node_eval] {msg}", file=sys.stderr)


# ---------------------------------------------------------------- MaaCore location

def locate_maa_root(start):
    """Walk up from *start* and return the first directory holding resource/tasks/, or None."""
    current = Path(start).resolve()
    for directory in [current, *current.parents]:
        if (directory / MAA_ROOT_MARKER).is_dir():
            return directory
    return None


def locate_library(root):
    """Return the MaaCore shared library inside *root*, or None if there is none."""
    for relative in LIBRARY_CANDIDATES:
        candidate = root / relative
        if candidate.is_file():
            return candidate
    for candidate in sorted(root.glob("*/libMaaCore.dylib")):  # non-standard build layouts
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------- MaaCore ctypes binding

def declare_common(lib):
    """Declare the C API needed to set up an engine instance."""
    lib.AsstGetVersion.restype = ctypes.c_char_p
    lib.AsstSetUserDir.restype = ctypes.c_uint8
    lib.AsstSetUserDir.argtypes = [ctypes.c_char_p]
    lib.AsstLoadResource.restype = ctypes.c_uint8
    lib.AsstLoadResource.argtypes = [ctypes.c_char_p]
    lib.AsstCreateEx.restype = ctypes.c_void_p
    lib.AsstCreateEx.argtypes = [CALLBACK_TYPE, ctypes.c_void_p]
    lib.AsstDestroy.argtypes = [ctypes.c_void_p]


def declare_eval(lib):
    """Declare the experimental offline evaluation API."""
    lib.AsstEvalTaskNode.restype = ctypes.c_size_t
    lib.AsstEvalTaskNode.argtypes = [
        ctypes.c_void_p,  # handle
        ctypes.c_char_p,  # task_name
        ctypes.c_char_p,  # image_path
        ctypes.c_char_p,  # buff (None = size query only)
        ctypes.c_size_t,  # buff_size
    ]


def redirect_c_layer_log(log_path):
    """Redirect the engine's C-level stdout log (fd 1) into *log_path*.

    The engine prints TRC-level logs through fd 1 in large amounts, which would drown the
    single JSON line on stdout. dup() saves the terminal fd, dup2() points fd 1 at the log
    file, then the saved fd becomes the new sys.stdout so Python output still reaches the
    terminal. The old stdout object is detached so its close/GC cannot close fd 1.
    Returns the new stdout (pass it explicitly when handing over to subprocesses).
    """
    saved = os.dup(1)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    os.dup2(fd, 1)
    os.close(fd)
    previous = sys.stdout
    sys.stdout = os.fdopen(saved, "w", encoding="utf-8", buffering=1)
    try:
        previous.detach()  # drop fd 1, keep GC/close from closing it
    except Exception:
        pass
    return sys.stdout


_ignore_callback = CALLBACK_TYPE(lambda msg_id, details_json, custom_arg: None)


# ---------------------------------------------------------------- evaluation

def eval_task_node(lib, handle, node, image_path):
    """Two-stage AsstEvalTaskNode call. Returns (raw dict or None, duration ms, failure or None)."""
    started = time.monotonic()
    need = lib.AsstEvalTaskNode(
        handle, node.encode("utf-8"), str(image_path).encode("utf-8"), None, 0)
    if need in (0, SIZE_MAX):
        why = "NullSize" if need == SIZE_MAX else "0"
        return None, int((time.monotonic() - started) * 1000), f"AsstEvalTaskNode returned {why}"
    buf = ctypes.create_string_buffer(need)
    got = lib.AsstEvalTaskNode(
        handle, node.encode("utf-8"), str(image_path).encode("utf-8"), buf, need)
    duration_ms = int((time.monotonic() - started) * 1000)
    if got in (0, SIZE_MAX):
        why = "NullSize" if got == SIZE_MAX else "0"
        return None, duration_ms, f"content stage returned {why} (first stage asked for {need} bytes)"
    try:
        return json.loads(buf.value.decode("utf-8", "replace")), duration_ms, None
    except json.JSONDecodeError as e:
        return None, duration_ms, f"returned value is not valid JSON: {e}; raw head: {buf.value[:160]!r}"


def read_png_size(path):
    """Read width/height from the PNG IHDR chunk (struct.unpack('>II'), no dependencies)."""
    with open(path, "rb") as f:
        head = f.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None, None
    return struct.unpack(">II", head[16:24])


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(
        description="MAA offline recognition evaluation: screenshot + node name -> node result JSON (no device)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="example: python3 node_eval.py --node AutoRaise@SelectAll --image snap_003.png")
    parser.add_argument("--node", required=True, metavar="NAME",
                        help="pipeline node name (e.g. AutoRaise@SelectAll)")
    parser.add_argument("--image", required=True, metavar="PNG",
                        help="screenshot to evaluate (PNG)")
    parser.add_argument("--maa-root", default=None, metavar="DIR",
                        help="MAA root: a directory holding resource/ and a MaaCore library "
                             "(default: probe upward from the current working directory)")
    parser.add_argument("--timeout", type=float, default=30.0, metavar="SECONDS",
                        help="total duration record limit (evaluation itself takes seconds; "
                             "written to the output for reference only)")
    args = parser.parse_args()

    image_path = Path(args.image).expanduser().resolve()
    total_started = time.monotonic()

    # 0) preflight
    if not image_path.exists():
        log(f"missing: screenshot does not exist: {image_path}")
        return 1

    # 1) locate the MAA root and its MaaCore library
    if args.maa_root:
        maa_root = Path(args.maa_root).expanduser().resolve()
        if not (maa_root / "resource").is_dir():
            log(f"error: {maa_root} does not look like a MAA root (no resource/ subdirectory)")
            return 1
    else:
        maa_root = locate_maa_root(Path.cwd())
        if maa_root is None:
            log(f"error: no MAA root found from {Path.cwd()} upward "
                f"(a MAA root holds a {MAA_ROOT_MARKER.as_posix()}/ subdirectory)")
            log("pass --maa-root DIR to point at a MAA checkout or build tree")
            return 1
    lib_path = locate_library(maa_root)
    if lib_path is None:
        log(f"error: no MaaCore library found under {maa_root}; looked for: "
            + ", ".join(LIBRARY_CANDIDATES))
        log("pass --maa-root DIR pointing at a MAA build tree that contains the library")
        return 1
    log(f"MaaCore        = {lib_path}")
    log(f"resource root  = {maa_root} (the engine appends /resource itself)")

    # 2) separate the C-level log before loading the library (it logs from the first load on)
    DEFAULT_USER_DIR.mkdir(parents=True, exist_ok=True)
    core_log = DEFAULT_USER_DIR / "core.log"
    redirect_c_layer_log(core_log)

    try:
        lib = ctypes.CDLL(str(lib_path))
    except OSError as e:
        log(f"error: cannot load {lib_path}: {e}")
        return 1
    declare_common(lib)
    if not hasattr(lib, "AsstEvalTaskNode"):
        log(f"error: the loaded MaaCore does not export AsstEvalTaskNode: {lib_path}")
        log("this tool needs a MaaCore build that provides the experimental offline "
            "evaluation API (see README.md)")
        return 1
    declare_eval(lib)
    version = lib.AsstGetVersion().decode()
    log(f"MaaCore version = {version} (engine log: {core_log})")

    # 3) user dir + resources + instance (no device connection: recognition is image-only)
    if not lib.AsstSetUserDir(str(DEFAULT_USER_DIR).encode()):
        log("warning: AsstSetUserDir failed (continuing)")
    if not lib.AsstLoadResource(str(maa_root).encode()):
        log(f"error: AsstLoadResource failed; check {maa_root}/resource/")
        return 1
    log("AsstLoadResource OK")
    handle = lib.AsstCreateEx(_ignore_callback, None)
    if not handle:
        log("error: AsstCreateEx failed")
        return 1
    log(f"AsstCreateEx OK handle=0x{handle:x} (no device connected)")

    # 4) evaluate
    try:
        raw, duration_ms, failure = eval_task_node(lib, handle, args.node, image_path)
    finally:
        lib.AsstDestroy(handle)
    log(f"AsstEvalTaskNode finished ({duration_ms} ms)")

    width, height = read_png_size(image_path)
    output = {
        "tool": "node_eval v1",
        "node": args.node,
        "screenshot": {"path": str(image_path), "width": width, "height": height},
        "maa_version": version,
        "raw": raw,
        "duration_ms": duration_ms,
        "timeout_s": args.timeout,
        "total_ms": int((time.monotonic() - total_started) * 1000),
    }

    # stdout carries this single JSON line only
    print(json.dumps(output, ensure_ascii=False))

    if raw is None:
        log(f"failed: {failure}")
        return 2
    if not raw.get("ok", False):
        log(f"evaluation finished with ok=false (error={raw.get('error')}); "
            "input problem rather than environment problem, exit code stays 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
