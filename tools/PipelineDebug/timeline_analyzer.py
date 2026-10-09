#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
timeline_analyzer v1 — post-run evidence timeline analyzer for MAA (standard library only)

Aligns the three kinds of evidence produced by a MAA task-chain run into one timeline:
  core.log    engine log (millisecond timestamps as the main axis)
  events.log  event log (one JSON object per line)
  snap_*.png  sequential screenshots (aligned by file mtime)

Writes timeline.json (machine readable) + timeline.md (human readable), including four
diagnostic anchors (connection / threshold / semantics / entry) plus an error layer.

Usage:
  python3 timeline_analyzer.py --dir <evidence dir> [--out <output dir>]
      [--node <node substring>] [--msg <message substring>] [--level INF,WRN,ERR] [--json-only]

Filters:
  --node   case-insensitive substring match against an event's node and its
           details.task / details.subtask / name fields
  --msg    case-insensitive substring match against message names
           (SubTaskStart / ConnectionInfo / ...)
  --level  filter core-side events by log level; plain events.log lines and
           snapshots carry no level and count as INF
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

TOOL = "timeline_analyzer v1"

# ---- 启发式常量 ----
THRESHOLD_DEFAULT = 0.7   # MAA 模板匹配默认阈值（日志无显式阈值字段时的估计）
THRESHOLD_NEAR = 0.05     # score <= THRESHOLD + NEAR 视为"接近或低于阈值"的诊断候选
MERGE_WINDOW = 1.5        # core.log 回调与 events.log 同事件的合并时间窗（秒）
ENTRY_NODES = 5           # 起点层锚点展示的任务链起始节点数
SNAP_NEARBY = 3           # 截图索引里每张快照列出附近事件数
SNAP_NEARBY_WIN = 8.0     # 快照"附近"事件的时间窗（秒）

# ---- 解析正则 ----
CORE_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}\.\d{3})\]\[(\w+)\]\[Px\d+\]\[Tx\d+\]\s?(.*)$"
)
CALLBACK_RE = re.compile(
    r"^(?P<pre>[│┌└ ]*)Assistant::append_callback \| (?P<msg>\w+) (?P<rest>\{.*)$"
)
MATCH_RE = re.compile(
    r"^(?P<pre>[│┌└ ]*)match_templ \| (?P<templ>\S+) \[(?P<method>[^\]]*)\] "
    r"score: (?P<score>-?[\d.]+) rect: \[(?P<rect>[^\]]*)\] roi: \[(?P<roi>[^\]]*)\]"
)
CALL_RE = re.compile(
    r"^(?P<pre>[│┌└ ]*)Call ` (?P<cmd>.*?) ` ret (?P<ret>-?\d+) , cost (?P<cost>\d+) ms , "
    r"stdout size: (?P<so>\d+) , socket size: (?P<sk>\d+)"
)
TREE_CHARS = "│┌└"


def log(msg):
    print(f"[timeline_analyzer] {msg}", file=sys.stderr)


def _nums(s):
    return [int(x) for x in s.split(",") if x.strip()]


def _depth(prefix):
    return sum(1 for ch in prefix if ch in TREE_CHARS)


def make_event(t, ts, source, etype, msg, node, summary, detail, core_line=None, level=None):
    """统一事件元素，键序与规格一致。"""
    return {
        "t": t,
        "ts": ts,
        "source": source,
        "type": etype,
        "msg": msg,
        "node": node,
        "summary": summary,
        "detail": detail,
        "core_line": core_line,
        "nearest_snap": None,
        "level": level,
    }


# ---------------------------------------------------------------- core.log

def summarize_callback(msg, j):
    """为回调事件生成一句话摘要。"""
    details = j.get("details") or {}
    task = details.get("task")
    subtask = details.get("subtask")
    action = details.get("action")
    res = details.get("result")
    score = res.get("score") if isinstance(res, dict) else None
    what = j.get("what") or ""
    why = j.get("why") or ""

    if msg in ("SubTaskStart", "SubTaskCompleted"):
        s = f"{task or subtask or '?'} {'开始' if msg == 'SubTaskStart' else '完成'}"
        if action:
            s += f" {action}"
        if score is not None:
            s += f" score={score:.4f}"
        return s
    if msg == "TaskChainStart":
        return f"任务链 {j.get('taskchain')} (taskid={j.get('taskid')}) 开始"
    if msg == "TaskChainCompleted":
        return f"任务链 {j.get('taskchain')} (taskid={j.get('taskid')}) 完成"
    if msg == "TaskChainError":
        return f"任务链 {j.get('taskchain')} 错误 why={why or details.get('why', '') or '?'}"
    if msg == "SubTaskError":
        return f"子任务 {task or subtask or '?'} 错误 why={why or details.get('why', '') or '?'}"
    if msg == "AllTasksCompleted":
        return "全部任务完成"
    if msg == "ConnectionInfo":
        if what == "FastestWayToScreencap":
            return f"截图方式选定 {details.get('method')} cost={details.get('cost')}ms"
        if what == "ResolutionGot":
            return f"分辨率 {details.get('width')}x{details.get('height')}"
        if what == "ResolutionInfo":
            return f"分辨率信息 {details.get('width')}x{details.get('height')} why={why or '?'}"
        if what == "Connected":
            return f"已连接 {details.get('address', '')}".strip()
        if what == "UuidGot":
            return f"设备 UUID {details.get('uuid', '')}"
        if what == "ScreencapCost":
            return f"截图耗时 avg={details.get('avg')}ms"
        if what == "EmulatorFPS":
            return f"模拟器刷新率 {details.get('fps')}fps"
        if what == "ConnectFailed":
            return f"连接失败 why={why or details.get('why', '') or '?'}"
        return f"ConnectionInfo {what}".strip()
    if msg == "AsyncCallInfo":
        return f"异步调用 {what} ret={details.get('ret')} cost={details.get('cost')}ms"
    base = msg + (f" {what}" if what else "")
    if j.get("name"):
        base += f" {j['name']}"
    return base.strip()


def callback_node(j):
    details = j.get("details") or {}
    # 注意：ScreenshotTaskPlugin 类回调的 subtask 在 JSON 顶层而非 details 里
    return (details.get("task") or details.get("subtask") or j.get("subtask")
            or j.get("name") or j.get("what") or j.get("taskchain") or "")


def parse_core(path, first_date_ref):
    """解析 core.log。返回 (events, info)。info 含行数/级别分布/时间范围。"""
    events = []
    levels = {}
    n_lines = 0
    t_first = t_last = None
    cont_skipped = 0

    with open(path, encoding="utf-8", errors="replace") as f:
        for lineno, raw in enumerate(f, 1):
            n_lines += 1
            line = raw.rstrip("\n")
            m = CORE_RE.match(line)
            if not m:
                # 非标准行：Call 命令的多行 stdout 续行，附到上一条 call/log 事件
                if events and events[-1]["type"] in ("call", "log"):
                    events[-1]["detail"].setdefault("continuation", []).append(line)
                else:
                    cont_skipped += 1
                continue
            date, hms, level, rest = m.groups()
            levels[level] = levels.get(level, 0) + 1
            if first_date_ref[0] is None:
                first_date_ref[0] = date
            if t_first is None:
                t_first = f"{date} {hms}"
            t_last = f"{date} {hms}"
            try:
                epoch = datetime.fromisoformat(f"{date} {hms}").timestamp()
            except ValueError:
                epoch = 0.0

            pm = CALLBACK_RE.match(rest)
            if pm:
                try:
                    j = json.loads(pm.group("rest"))
                except json.JSONDecodeError:
                    j = {"raw": pm.group("rest")}
                msg = pm.group("msg")
                events.append(make_event(
                    f"{date} {hms}", epoch, "core", "callback", msg,
                    callback_node(j), summarize_callback(msg, j), j, lineno, level))
                continue

            mm = MATCH_RE.match(rest)
            if mm:
                detail = {
                    "template": mm.group("templ"),
                    "method": mm.group("method"),
                    "score": float(mm.group("score")),
                    "rect": _nums(mm.group("rect")),
                    "roi": _nums(mm.group("roi")),
                    "depth": _depth(mm.group("pre")),
                }
                events.append(make_event(
                    f"{date} {hms}", epoch, "core", "match", "match_templ",
                    mm.group("templ"),
                    f"模板匹配 {mm.group('templ')} score={mm.group('score')} rect={_nums(mm.group('rect'))}",
                    detail, lineno, level))
                continue

            cm = CALL_RE.match(rest)
            if cm:
                cmd = cm.group("cmd")
                detail = {
                    "cmd": cmd,
                    "ret": int(cm.group("ret")),
                    "cost_ms": int(cm.group("cost")),
                    "stdout_size": int(cm.group("so")),
                    "socket_size": int(cm.group("sk")),
                    "depth": _depth(cm.group("pre")),
                }
                events.append(make_event(
                    f"{date} {hms}", epoch, "core", "call", "call",
                    cmd.split()[0].rsplit("/", 1)[-1] if cmd.split() else "",
                    f"Call {cmd} ret={cm.group('ret')} cost={cm.group('cost')}ms",
                    detail, lineno, level))
                continue

            # 其余行：树形前缀 + 模块 | 内容（无 | 则整行是内容）
            pre = re.match(r"^[│┌└ ]*", rest).group(0)
            body = rest[len(pre):]
            if " | " in body:
                module, content = body.split(" | ", 1)
            else:
                module, content = "", body
            events.append(make_event(
                f"{date} {hms}", epoch, "core", "log", level, module.strip(),
                content.strip()[:160], {"content": content.strip(), "depth": _depth(pre)},
                lineno, level))

    info = {"lines": n_lines, "parsed": len(events), "levels": levels,
            "time_range": [t_first, t_last] if t_first else None,
            "continuation_lines_attached": n_lines - len(events) - cont_skipped}
    return events, info


# ---------------------------------------------------------------- events.log

def parse_events(path, first_date_ref):
    """解析 events.log。返回 (events, count, skipped)。"""
    events = []
    skipped = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                j = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                log(f"events.log 第 {lineno} 行 JSON 解析失败，跳过")
                continue
            ts = float(j.get("ts", 0.0))
            dt = datetime.fromtimestamp(ts)
            date = first_date_ref[0] or dt.strftime("%Y-%m-%d")
            msg = j.get("msg", "")
            # 注意：events.log 的 name 常为空，节点信息只在 core 侧 details.task 里；
            # 这里不落到 taskchain（"Custom" 会与 core 侧节点名冲突，破坏合并配对）
            node = j.get("name") or j.get("what") or ""
            summary = summarize_callback(msg, j)
            ev = make_event(f"{date} {dt.strftime('%H:%M:%S.%f')[:-3]}", ts,
                            "event", "callback", msg, node, summary, j,
                            None, None)
            ev["_event_line"] = lineno
            events.append(ev)
    return events


# ---------------------------------------------------------------- 快照

SNAP_RE = re.compile(r"^snap_(\d+|FINAL)\.png$")


def collect_snapshots(d):
    """收集快照文件。返回事件列表与文件名清单。"""
    events = []
    files = []
    for p in sorted(d.iterdir()):
        m = SNAP_RE.match(p.name)
        if not m or not p.is_file():
            continue
        files.append(p.name)
        seq = m.group(1)
        mt = p.stat().st_mtime
        dt = datetime.fromtimestamp(mt)
        events.append(make_event(
            dt.strftime("%Y-%m-%d %H:%M:%S"), mt, "snap", "snapshot", "Snapshot",
            p.name, f"快照 {p.name}（mtime 对齐，{p.stat().st_size} bytes）",
            {"file": p.name, "seq": seq, "mtime": mt, "size": p.stat().st_size}))
    return events, files


# ---------------------------------------------------------------- 合并与对齐

def merge_core_event(timeline):
    """core 回调与 events.log 行按 msg 分组、时间窗内贪心配对合并。"""
    by_msg_core = {}
    by_msg_event = {}
    for i, ev in enumerate(timeline):
        if ev["source"] == "core" and ev["type"] == "callback":
            by_msg_core.setdefault(ev["msg"], []).append(i)
        elif ev["source"] == "event":
            by_msg_event.setdefault(ev["msg"], []).append(i)

    merged_into = {}   # core idx -> event idx
    removed = set()
    n_merged = 0
    for msg, ev_idx in by_msg_event.items():
        co_idx = by_msg_core.get(msg, [])
        i = j = 0
        while i < len(ev_idx) and j < len(co_idx):
            ei, ci = ev_idx[i], co_idx[j]
            ev, co = timeline[ei], timeline[ci]
            if abs(ev["ts"] - co["ts"]) <= MERGE_WINDOW:
                enode, cnode = ev["node"], co["node"]
                if not enode or not cnode or enode == cnode:
                    merged_into[ci] = ei
                    removed.add(ei)
                    n_merged += 1
                    i += 1
                    j += 1
                    continue
            # 不配对：时间近但节点冲突，或时间差超窗——推进 ts 较小一侧
            if ev["ts"] < co["ts"]:
                i += 1
            else:
                j += 1

    out = []
    for i, ev in enumerate(timeline):
        if i in removed:
            continue
        if i in merged_into:
            other = timeline[merged_into[i]]
            ev = dict(ev)
            ev["source"] = "core+event"
            ev["node"] = ev["node"] or other["node"]
            ev["detail"] = {"core": ev["detail"], "event": other["detail"]}
        out.append(ev)
    return out, n_merged


def attach_nearest_snap(timeline):
    snaps = [ev for ev in timeline if ev["type"] == "snapshot"]
    if not snaps:
        return
    for ev in timeline:
        if ev["type"] == "snapshot":
            ev["nearest_snap"] = ev["node"]
            continue
        best = min(snaps, key=lambda s: (abs(s["ts"] - ev["ts"]), s["node"]))
        ev["nearest_snap"] = best["node"]
        ev["detail"]["snap_dt"] = round(ev["ts"] - best["ts"], 3)


# ---------------------------------------------------------------- 过滤

def node_blob(ev):
    parts = [ev.get("node") or ""]
    dicts = []
    d = ev.get("detail") or {}
    if isinstance(d, dict) and "core" in d:
        dicts = [d.get("core") or {}, d.get("event") or {}]
    elif isinstance(d, dict):
        dicts = [d]
    for dd in dicts:
        if not isinstance(dd, dict):
            continue
        parts.append(dd.get("name") or "")
        parts.append(dd.get("subtask") or "")
        if ev.get("type") == "match":
            parts.append(dd.get("template") or "")
        det = dd.get("details")
        if isinstance(det, dict):
            parts.append(det.get("task") or "")
            parts.append(det.get("subtask") or "")
    return " ".join(p for p in parts if p).lower()


def apply_filters(timeline, node=None, msg=None, level=None):
    out = timeline
    if node:
        q = node.lower()
        out = [e for e in out if q in node_blob(e)]
    if msg:
        q = msg.lower()
        out = [e for e in out if q in (e["msg"] or "").lower()]
    if level:
        lv = {x.strip().upper() for x in level.split(",") if x.strip()}
        keep = []
        for e in out:
            l = e.get("level") or "INF"   # 纯 events.log 行与快照视为 INF
            if l in lv:
                keep.append(e)
        out = keep
    return out


# ---------------------------------------------------------------- 诊断锚点

def _iter_details(ev):
    """产出事件携带的各 detail dict（合并事件含 core/event 两份）。"""
    d = ev.get("detail")
    if isinstance(d, dict):
        if "core" in d:
            for k in ("core", "event"):
                v = d.get(k)
                if isinstance(v, dict):
                    yield v
        else:
            yield d


def _walk_json(o, fn):
    if isinstance(o, dict):
        for k, v in o.items():
            fn(k, v)
            _walk_json(v, fn)
    elif isinstance(o, list):
        for x in o:
            _walk_json(x, fn)


def anchor_connection(tl):
    idx = [i for i, e in enumerate(tl)
           if e["msg"] == "ConnectionInfo" or (e["msg"] == "AsyncCallInfo" and
              any((dd or {}).get("what") == "Connect" for dd in _iter_details(e)))]
    summary = "无连接事件"
    notes = []
    if idx:
        whats = []
        for i in idx:
            e = tl[i]
            for dd in _iter_details(e):
                w = dd.get("what")
                if w and w not in whats:
                    whats.append(w)
        con = next((tl[i] for i in idx if "已连接" in tl[i]["summary"]), None)
        fastest = next((tl[i] for i in idx if "截图方式" in tl[i]["summary"]), None)
        parts = []
        if con:
            parts.append(f"{con['t']} 已连接 (core:{con['core_line']})" if con["core_line"] else f"{con['t']} 已连接")
        if fastest:
            # 合并事件的 detail 是 {"core":..., "event":...}，method/cost 在 details 层
            m = cost = None
            for dd in _iter_details(fastest):
                det = dd.get("details") or {}
                m = det.get("method") or m
                cost = det.get("cost") or cost
            label = f"{m}({cost}ms)" if m else None
            parts.append(f"截图方式 {label}（{fastest['t']}）" if label else f"截图方式选定（{fastest['t']}）")
        summary = "；".join(parts) if parts else f"连接序列 {','.join(whats[:6])}"
        notes.append("连接序列: " + " → ".join(whats))
        # ConnectFailed 检查
        fail = [i for i in idx if "ConnectFailed" in tl[i]["summary"] or
                any((dd or {}).get("what") == "ConnectFailed" for dd in _iter_details(tl[i]))]
        if fail:
            whys = []
            for i in fail:
                for dd in _iter_details(tl[i]):
                    w = dd.get("why") or (dd.get("details") or {}).get("why")
                    if w:
                        whys.append(str(w))
            notes.append("存在 ConnectFailed，why=" + ("; ".join(whys) or "未记录"))
        else:
            notes.append("无 ConnectFailed")
    focus = None
    if idx:
        # 锚点行首位置指向 Connected 核心事件（无则退回序列首条）
        focus = next((i for i in idx if "已连接" in tl[i]["summary"]), idx[0])
    return {"summary": summary, "events": idx, "notes": "；".join(notes), "focus": focus}


def anchor_threshold(tl):
    idx = []
    for i, e in enumerate(tl):
        if e["type"] != "match":
            continue
        score = (e.get("detail") or {}).get("score")
        if isinstance(score, (int, float)) and score <= THRESHOLD_DEFAULT + THRESHOLD_NEAR:
            idx.append(i)
    if idx:
        worst = min(idx, key=lambda i: tl[i]["detail"]["score"])
        w = tl[worst]
        summary = (f"阈值按默认 {THRESHOLD_DEFAULT} 估计（日志无显式阈值字段）；"
                   f"score≤{THRESHOLD_DEFAULT + THRESHOLD_NEAR} 的 match 共 {len(idx)} 条，"
                   f"最低 {w['detail']['score']}（{w['detail']['template']} @ {w['t']}）")
    else:
        summary = (f"阈值按默认 {THRESHOLD_DEFAULT} 估计；无 score≤{THRESHOLD_DEFAULT + THRESHOLD_NEAR} 的 match 事件")
    notes = "候选含低于阈值（识别失败）与贴线（临界命中）两类，排查阈值层问题时先看这些"
    return {"summary": summary, "events": idx, "notes": notes}


def anchor_semantics(tl):
    alt_keys = {"on_error_next", "exceeded_next", "onErrorNext", "exceededNext"}
    executed = set()
    idx = []
    alt_hits = {}   # 备选节点名 -> 是否有执行证据
    for i, e in enumerate(tl):
        if e["type"] != "callback":
            continue
        for dd in _iter_details(e):
            det = dd.get("details") or {}
            t = det.get("task")
            if e["msg"] == "SubTaskStart" and t:
                executed.add(t)

    def scan(k, v):
        if k in alt_keys and isinstance(v, list):
            for n in v:
                if isinstance(n, str) and n not in alt_hits:
                    alt_hits[n] = None

    for i, e in enumerate(tl):
        if e["type"] != "callback":
            continue
        for dd in _iter_details(e):
            _walk_json(dd, scan)

    # 备选路径节点（含 Ocr 变体）的执行证据
    for n in list(alt_hits):
        if n in executed:
            alt_hits[n] = True
        elif f"{n}Ocr" in executed or f"Ocr{n}" in executed:
            alt_hits[n] = True
    for i, e in enumerate(tl):
        if e["msg"] in ("SubTaskError", "TaskChainError"):
            idx.append(i)
        elif e["msg"] == "SubTaskStart":
            for dd in _iter_details(e):
                t = (dd.get("details") or {}).get("task")
                if t and (t in alt_hits or "Ocr" in t):
                    idx.append(i)
                    break
    if alt_hits:
        exec_list = [n for n, v in alt_hits.items() if v]
        no_list = [n for n, v in alt_hits.items() if not v]
        seg = []
        if exec_list:
            seg.append("已执行备选节点: " + ",".join(exec_list))
        if no_list:
            seg.append("声明未执行: " + ",".join(no_list[:8]))
        summary = "；".join(seg) if seg else "发现备选路径声明，无执行证据"
    elif idx:
        summary = "日志无 onErrorNext/exceededNext 字段；发现错误相关或 Ocr 备选节点事件（见 events）"
    else:
        summary = "未发现 onErrorNext/exceededNext 字段，也未发现 Ocr 备选节点事件；无备选路径触发证据"
    return {"summary": summary, "events": idx,
            "notes": "语义层排查入口：任务'成功'但结果不对时，看备选路径（onErrorNext/exceededNext/@Ocr 变体）是否被触发"}


def anchor_entry(tl):
    start_i = next((i for i, e in enumerate(tl) if e["msg"] == "TaskChainStart"), None)
    if start_i is None:
        return {"summary": "无 TaskChainStart 事件", "events": [], "notes": ""}
    start = tl[start_i]
    taskid = None
    chain = ""
    for dd in _iter_details(start):
        if dd.get("taskid") is not None:
            taskid = dd["taskid"]
        chain = dd.get("taskchain") or chain
    node_idx = []
    names = []
    for i in range(start_i, len(tl)):
        e = tl[i]
        if e["msg"] == "TaskChainCompleted":
            break
        if e["msg"] == "SubTaskStart":
            ok = False
            for dd in _iter_details(e):
                det = dd.get("details") or {}
                # taskid 在回调 JSON 顶层（events.log 行没有 taskid 字段）
                tid = dd.get("taskid", det.get("taskid"))
                if taskid is None or tid == taskid:
                    ok = True
                    # 节点名：details.task → details.subtask → 顶层 subtask
                    n = det.get("task") or det.get("subtask") or dd.get("subtask")
                    if n:
                        names.append(n)
                    break
            if ok:
                node_idx.append(i)
            if len(node_idx) >= ENTRY_NODES:
                break
    summary = (f"任务链 {chain}(taskid={taskid}) 起点序列: "
               + (" → ".join(names) if names else "无节点事件"))
    notes = f"起点层排查入口：前置链（返回按钮/界面确认等）是否按预期走完；共列出前 {len(node_idx)} 个节点"
    return {"summary": summary, "events": [start_i] + node_idx, "notes": notes}


def anchor_errors(tl):
    idx = []
    n_err = n_wrn = n_sub_err = n_chain_err = 0
    for i, e in enumerate(tl):
        lvl = e.get("level")
        if lvl == "ERR":
            idx.append(i)
            n_err += 1
        elif lvl == "WRN":
            idx.append(i)
            n_wrn += 1
        elif e["msg"] == "SubTaskError":
            idx.append(i)
            n_sub_err += 1
        elif e["msg"] == "TaskChainError":
            idx.append(i)
            n_chain_err += 1
    if idx:
        summary = f"WRN {n_wrn} 条 / ERR {n_err} 条 / SubTaskError {n_sub_err} / TaskChainError {n_chain_err}"
    else:
        summary = "无 WRN/ERR/SubTaskError/TaskChainError 事件（错误层干净）"
    return {"summary": summary, "events": idx,
            "notes": "错误层排查入口：WRN 多为任务配置告警（如 implicit fullscreen roi），ERR/SubTaskError 才是执行失败"}


# ---------------------------------------------------------------- markdown

def fmt_t(e):
    return e["t"]


def build_markdown(doc, tl, input_info, missing):
    lines = []
    ap = lines.append
    ap(f"# MAA 跑后时间线")
    ap("")
    ap(f"- 生成: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ｜ 工具: {TOOL} ｜ 证据目录: {doc['input']['dir']}")

    # ---- 1 概览 ----
    ap("")
    ap("## 1 概览")
    ap("")
    cl = doc["input"]["core_log"]
    if cl:
        lv = " / ".join(f"{k} {v}" for k, v in sorted(cl["levels"].items()))
        tr = cl.get("time_range") or ["?", "?"]
        ap(f"- core.log: {cl['lines']} 行（{lv}），时间 {tr[0]} → {tr[1]}")
    else:
        ap("- core.log: 缺失")
    el = doc["input"]["events_log"]
    ap(f"- events.log: {el['count'] if el else 0} 条" + ("（缺失）" if not el else ""))
    sn = doc["input"]["snapshots"]
    ap(f"- 快照: {sn['count'] if sn else 0} 张" + (f"（{', '.join(sn['files'])}）" if sn and sn["files"] else ("（缺失）" if not sn else "")))
    if missing:
        ap(f"- **缺失声明**: {', '.join(missing)}（缺失部分的对齐能力受限）")
    n_err = sum(1 for e in tl if e.get("level") == "ERR")
    n_sub_err = sum(1 for e in tl if e["msg"] == "SubTaskError")
    n_chain_err = sum(1 for e in tl if e["msg"] == "TaskChainError")
    n_wrn = sum(1 for e in tl if e.get("level") == "WRN")
    dur = (tl[-1]["ts"] - tl[0]["ts"]) if tl else 0
    # 运行时长优先取 core.log 时间轴（快照 mtime 可能是复制时间，会把总跨度拉偏）
    run_dur = dur
    run_src = "时间线首尾"
    cl_tr = (doc["input"]["core_log"] or {}).get("time_range") if doc["input"].get("core_log") else None
    if cl_tr and cl_tr[0] and cl_tr[1]:
        try:
            run_dur = datetime.fromisoformat(cl_tr[1]).timestamp() - datetime.fromisoformat(cl_tr[0]).timestamp()
            run_src = "core.log 时间轴"
        except ValueError:
            pass
    line = f"- 时间线事件: {len(tl)} 条，运行时长约 {run_dur:.1f} 秒（{run_src}）"
    if abs(dur - run_dur) > 5:
        line += f"；时间线总跨度 {dur:.1f} 秒（含日志轴之外的快照 mtime）"
    ap(line)
    ap(f"- 错误: ERR {n_err} / SubTaskError {n_sub_err} / TaskChainError {n_chain_err} / WRN {n_wrn}")

    # ---- 2 分段时间 ----
    ap("")
    ap("## 2 分段时间")
    ap("")
    cbs = [e for e in tl if e["type"] == "callback"]
    conn = [e for e in cbs if e["msg"] in ("ConnectionInfo", "AsyncCallInfo")]
    ap("### 连接段")
    ap("")
    if conn:
        for e in conn:
            ap(f"- {fmt_t(e)}  {e['summary']}")
    else:
        ap("- 无连接事件")
    ap("")

    # 任务链分段（按 TaskChainStart/Completed 切）
    chains = []
    cur = None
    for i, e in enumerate(tl):
        if e["msg"] == "TaskChainStart":
            cur = {"start_i": i, "events": []}
        if cur is not None:
            cur["events"].append((i, e))
            if e["msg"] in ("TaskChainCompleted", "TaskChainError"):
                chains.append(cur)
                cur = None
    if cur is not None:
        chains.append(cur)

    if chains:
        for ci, ch in enumerate(chains, 1):
            start = ch["events"][0][1]
            end = ch["events"][-1][1]
            name, tid = "", ""
            for dd in _iter_details(start):
                name = dd.get("taskchain") or name
                tid = dd.get("taskid", tid)
            ap(f"### 任务链 {name}#{tid}（{fmt_t(start)} → {fmt_t(end)}，{end['ts'] - start['ts']:.1f}s）")
            ap("")
            # 节点序列表
            pending = {}
            rows = []
            for i, e in ch["events"]:
                if e["type"] != "callback":
                    continue
                node, action, score = None, None, None
                for dd in _iter_details(e):
                    det = dd.get("details") or {}
                    node = det.get("task") or det.get("subtask") or dd.get("subtask") or node
                    action = det.get("action") or action
                    res = det.get("result")
                    score = res.get("score") if isinstance(res, dict) else score
                if e["msg"] == "SubTaskStart" and node:
                    pending[node] = e
                elif e["msg"] == "SubTaskCompleted" and node:
                    st = pending.pop(node, None)
                    dt = (e["ts"] - st["ts"]) if st else None
                    rows.append((node, action, score, dt, "完成"))
                elif e["msg"] == "SubTaskError":
                    st = pending.pop(node, None) if node else None
                    dt = (e["ts"] - st["ts"]) if st else None
                    rows.append((node, action, score, dt, "错误"))
            if rows:
                ap("| # | 节点 | 动作 | 得分 | 耗时s | 结果 |")
                ap("|---|------|------|------|-------|------|")
                for ri, (node, action, score, dt, res) in enumerate(rows, 1):
                    sc = f"{score:.4f}" if isinstance(score, (int, float)) else "-"
                    dts = f"{dt:.2f}" if dt is not None else "-"
                    ap(f"| {ri} | {node or '-'} | {action or '-'} | {sc} | {dts} | {res} |")
            else:
                ap("- 无节点级事件（events.log 无节点名时此表受限）")
            ap("")
    else:
        ap("### 任务链")
        ap("")
        ap("- 无 TaskChainStart/Completed 分段")
        ap("")

    endmsgs = [e for e in cbs if e["msg"] in ("AllTasksCompleted", "TaskChainError")]
    ap("### 结束段")
    ap("")
    if endmsgs:
        for e in endmsgs:
            ap(f"- {fmt_t(e)}  {e['summary']}")
    else:
        ap("- 无明确结束事件")

    # ---- 3 诊断锚点 ----
    ap("")
    ap("## 3 诊断锚点")
    ap("")
    names = [("connection", "连接层"), ("threshold", "阈值层"),
             ("semantics", "语义层"), ("entry", "起点层"), ("errors", "错误层")]
    for key, zh in names:
        a = doc["diagnostic_anchors"][key]
        if a["events"]:
            e0 = tl[a.get("focus", a["events"][0])]
            loc = f"{fmt_t(e0)}(core:{e0['core_line']})" if e0.get("core_line") else fmt_t(e0)
            extra = f" 等 {len(a['events'])} 处" if len(a["events"]) > 1 else ""
            ap(f"- **{zh}** → {loc}{extra} → {a['summary']}")
        else:
            ap(f"- **{zh}** → 无事件 → {a['summary']}")

    # ---- 4 截图索引 ----
    ap("")
    ap("## 4 截图索引")
    ap("")
    snaps = [e for e in tl if e["type"] == "snapshot"]
    if snaps:
        ap("| 文件 | 时间(mtime) | 附近事件 |")
        ap("|------|------------|----------|")
        for s in snaps:
            near = sorted((e for e in tl if e["type"] != "snapshot"),
                          key=lambda e: abs(e["ts"] - s["ts"]))[:SNAP_NEARBY]
            near = [e for e in near if abs(e["ts"] - s["ts"]) <= SNAP_NEARBY_WIN]
            txt = "；".join(f"{e['t'][11:19]} {e['msg']} {e['summary'][:40]}" for e in near) or "（时间窗内无事件）"
            ap(f"| {s['node']} | {s['t']} | {txt} |")
        ap("")
        ap("> 注意：快照按文件 mtime 对齐；若证据目录是复制产物，mtime 是复制时间而非原始拍摄时间，对齐会整体偏移（本表如实反映）。")
    else:
        ap("- 无快照文件")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 主流程

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="timeline_analyzer.py", description="MAA post-run evidence timeline analyzer (core.log + events.log + snapshots)",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("Usage:")[1] if "Usage:" in __doc__ else None)
    ap.add_argument("--dir", required=True, help="evidence directory (core.log / events.log / snap_*.png; a missing source is not fatal)")
    ap.add_argument("--out", default="/tmp/timeline-out/", help="output directory (default /tmp/timeline-out/)")
    ap.add_argument("--node", help="filter by node name (case-insensitive substring of task/subtask/name fields)")
    ap.add_argument("--msg", help="filter by message type (e.g. SubTaskStart / ConnectionInfo)")
    ap.add_argument("--level", help="filter by log level (e.g. INF,WRN,ERR)")
    ap.add_argument("--json-only", action="store_true", help="write timeline.json only, skip timeline.md")
    args = ap.parse_args(argv)

    in_dir = Path(args.dir).expanduser()
    if not in_dir.is_dir():
        print(f"[timeline_analyzer] error: evidence directory does not exist or is not a directory: {in_dir}", file=sys.stderr)
        return 2
    out_dir = Path(args.out).expanduser()
    in_res, out_res = in_dir.resolve(), out_dir.resolve()
    if out_res == in_res or in_res in out_res.parents:
        print(f"[timeline_analyzer] error: output directory must not live inside the evidence directory (evidence is read-only): {out_res}", file=sys.stderr)
        return 2

    missing = []
    first_date_ref = [None]

    # core.log
    core_events, core_info = [], None
    core_path = in_dir / "core.log"
    if core_path.is_file():
        core_events, core_info = parse_core(core_path, first_date_ref)
        log(f"core.log: {core_info['lines']} 行 → {core_info['parsed']} 事件 "
            f"({core_info['time_range'][0]} → {core_info['time_range'][1]})")
    else:
        missing.append("core.log")
        log("core.log 缺失，仅用 events.log + 快照对齐")

    # events.log
    event_events = []
    events_count = 0
    events_path = in_dir / "events.log"
    if events_path.is_file():
        event_events = parse_events(events_path, first_date_ref)
        events_count = len(event_events)
        log(f"events.log: {events_count} 条事件")
    else:
        missing.append("events.log")
        log("events.log 缺失")

    # 快照
    snap_events, snap_files = collect_snapshots(in_dir)
    if not snap_files:
        missing.append("snapshots")
        log("无 snap_*.png 快照")
    else:
        log(f"快照: {len(snap_files)} 张 ({', '.join(snap_files)})")

    # 合并 + 排序 + 对齐
    raw = core_events + event_events + snap_events
    src_order = {"core": 0, "event": 1, "snap": 2}
    raw.sort(key=lambda e: (e["ts"], src_order.get(e["source"], 3),
                            e.get("core_line") or e.get("_event_line") or 0))
    timeline, n_merged = merge_core_event(raw)
    attach_nearest_snap(timeline)
    log(f"合并 core+event 同源事件 {n_merged} 对；统一时间线 {len(timeline)} 条")

    # 过滤
    filtered = apply_filters(timeline, args.node, args.msg, args.level)
    if args.node or args.msg or args.level:
        log(f"过滤后剩余 {len(filtered)} 条 (--node={args.node} --msg={args.msg} --level={args.level})")
    for e in filtered:
        e.pop("_event_line", None)

    # 诊断锚点（基于过滤后的时间线）
    anchors = {
        "connection": anchor_connection(filtered),
        "threshold": anchor_threshold(filtered),
        "semantics": anchor_semantics(filtered),
        "entry": anchor_entry(filtered),
        "errors": anchor_errors(filtered),
    }

    tr = (core_info or {}).get("time_range")
    doc = {
        "tool": TOOL,
        "input": {
            "dir": str(in_dir),
            "core_log": None if core_info is None else core_info,
            "events_log": None if events_path.is_file() is False else {"count": events_count},
            "snapshots": {"count": len(snap_files), "files": snap_files} if snap_files else None,
            "missing": missing,
        },
        "timeline": filtered,
        "diagnostic_anchors": anchors,
    }
    if tr:
        doc["input"]["core_log"]["time_range"] = tr

    out_dir.mkdir(parents=True, exist_ok=True)
    out_json = out_dir / "timeline.json"
    out_json.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"已写 {out_json}")

    out_md = None
    if not args.json_only:
        out_md = out_dir / "timeline.md"
        out_md.write_text(build_markdown(doc, filtered, doc["input"], missing), encoding="utf-8")
        log(f"已写 {out_md}")

    # duration_run 优先取 core.log 时间轴（快照 mtime 若是复制时间会拉偏总跨度）
    duration_run = round(filtered[-1]["ts"] - filtered[0]["ts"], 2) if filtered else 0
    tr = (core_info or {}).get("time_range")
    if tr and tr[0] and tr[1]:
        try:
            duration_run = round(datetime.fromisoformat(tr[1]).timestamp()
                                 - datetime.fromisoformat(tr[0]).timestamp(), 2)
        except ValueError:
            pass
    print(json.dumps({
        "events": len(filtered),
        "out_json": str(out_json),
        "out_md": str(out_md) if out_md else None,
        "duration_run": duration_run,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
