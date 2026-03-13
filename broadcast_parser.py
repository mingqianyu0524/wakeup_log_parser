#!/usr/bin/env python3
"""
Broadcast Parser
----------------
Parses BLE wakeup broadcast data from Android and HarmonyOS log files,
correlates them with wakeup events (T1/T2/T3), and produces structured
O1 objects ready for UI consumption.

Broadcast sources (4 types):
  Android sends    getEncryptData::...dec data = [-24, 81, ...]
  Android receives parseResponse version N::[0, 50, ...]
  HarmonyOS sends  buildBytes: {"0":1,"1":50,...}
  HarmonyOS recv   deviceData:DeviceData{...mOriginData=0,240,...}

Broadcast payload format (15 bytes, indices 0-based, timestamp big-endian):
  [0]    type          0=wakeup  1=pre-wakeup  2=wakeup_fail
  [1]    deviceType    50=phone  110=watch     240=car
  [2-5]  unix timestamp (uint32 big-endian, seconds)
  [6-9]  device UDID
  [10]   voice energy level (pre-wakeup broadcast: 0)
  [11]   voice confidence
  [12]   suppression    0=not suppressed  63=suppressed
  [13-14] reserved
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── log-line timestamp ────────────────────────────────────────────────────────

_TS_RE = re.compile(r"(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})")

_CURRENT_YEAR = datetime.now().year


def extract_timestamp(line: str) -> str | None:
    m = _TS_RE.search(line)
    return m.group(1) if m else None


def ts_to_ms(ts: str) -> int:
    """Convert 'MM-DD HH:MM:SS.mmm' to milliseconds since an epoch suitable for comparison."""
    try:
        dt = datetime.strptime(f"{_CURRENT_YEAR}-{ts}", "%Y-%m-%d %H:%M:%S.%f")
        return int(dt.timestamp() * 1000)
    except ValueError:
        return 0


# ── wakeup event patterns (same as parse_wakeup_logs.py) ─────────────────────

_RE_T1 = re.compile(r"===de")
_RE_T2 = re.compile(r"===start::")
_RE_T3 = re.compile(r"onResult::\s*isShouldResponse")
_RE_DECISION = re.compile(r"isShouldResponse\s*=\s*(true|false)", re.IGNORECASE)

# ── broadcast regex patterns ──────────────────────────────────────────────────

# Android sends: "getEncryptData::...dec data = [-24, 81, ...]"
_RE_ANDROID_SEND = re.compile(
    r"getEncryptData::.*?dec data = \[([-\d,\s]+)\]"
)

# Android receives: "parseResponse version N::[0, 50, ...]"
_RE_ANDROID_RECV = re.compile(
    r"parseResponse.*?version\s+\d+::\[([-\d,\s]+)\]"
)

# HarmonyOS sends: optional "[hash]buildBytes: {...}"
_RE_HMOS_SEND = re.compile(
    r"buildBytes:\s*(\{[^}]+\})"
)

# HarmonyOS receives: "deviceData:DeviceData{...mOriginData=0,240,...}"
_RE_HMOS_RECV = re.compile(
    r"mOriginData=([\d,]+)"
)

# ── byte-array converters ─────────────────────────────────────────────────────

def _int8_to_uint8(vals: list[int]) -> list[int]:
    """Convert signed int8 list (Android) to unsigned uint8 (add 256 for negatives)."""
    return [v + 256 if v < 0 else v for v in vals]


def parse_bracketed_int8(s: str) -> list[int] | None:
    """Parse '[-24, 81, 104, ...]' → uint8 list."""
    try:
        vals = [int(x.strip()) for x in s.split(",") if x.strip()]
        return _int8_to_uint8(vals)
    except ValueError:
        return None


def parse_csv_uint8(s: str) -> list[int] | None:
    """Parse '0,240,105,174,...' → uint8 list (already unsigned)."""
    try:
        return [int(x.strip()) for x in s.split(",") if x.strip()]
    except ValueError:
        return None


def parse_json_bytes(s: str) -> list[int] | None:
    """Parse '{"0":1,"1":50,...}' → uint8 list ordered by integer key."""
    try:
        d = json.loads(s)
        max_key = max(int(k) for k in d)
        return [int(d[str(i)]) for i in range(max_key + 1)]
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


# ── broadcast payload decoder ─────────────────────────────────────────────────

_DEVICE_TYPE_NAMES = {50: "手机", 110: "手表", 240: "车机"}
_BROADCAST_TYPE_NAMES = {0: "唤醒广播", 1: "预唤醒广播", 2: "唤醒失败广播"}


def decode_broadcast(b: list[int]) -> dict:
    """
    Decode a 15-byte uint8 broadcast payload into a structured dict.
    Timestamps in bytes[2:6] are big-endian uint32 Unix seconds.
    """
    if len(b) < 13:
        return {"raw": ",".join(str(x) for x in b), "parse_error": "too short"}

    ts_unix = int.from_bytes(b[2:6], "big")
    try:
        ts_human = datetime.fromtimestamp(ts_unix, tz=timezone.utc).strftime(
            "%Y年%m月%d日%H时%M分%S秒"
        )
    except (OSError, OverflowError, ValueError):
        ts_human = "无效时间戳"

    device_type = b[1]
    device_name = _DEVICE_TYPE_NAMES.get(device_type, f"未知({device_type})")
    bcast_type  = b[0]
    bcast_name  = _BROADCAST_TYPE_NAMES.get(bcast_type, f"未知({bcast_type})")

    return {
        "type":             bcast_type,
        "typeName":         bcast_name,
        "deviceType":       device_type,
        "deviceTypeName":   device_name,
        "timestamp_bytes":  list(b[2:6]),
        "timestamp_unix":   ts_unix,
        "timestamp_human":  ts_human,
        "udid":             list(b[6:10]),
        "voiceEnergy":      b[10] if len(b) > 10 else None,
        "voiceConfidence":  b[11] if len(b) > 11 else None,
        "suppressed":       b[12] if len(b) > 12 else None,
        "raw":              ",".join(str(x) for x in b),
    }


# ── log-file scanner ──────────────────────────────────────────────────────────

def is_log_file(path: Path) -> bool:
    name = path.name
    if name.startswith("hiapplogcat-log"):
        return True
    if name.startswith("hilog") and name.endswith(".txt"):
        return True
    return False


def _scan_file(
    filepath: Path,
    initial_pending: dict | None = None,
) -> tuple[list[dict], list[dict], list[dict], dict | None]:
    """
    Single-pass scan of one log file.

    Args:
        filepath:        Path to the log file.
        initial_pending: An incomplete wakeup event carried over from the
                         previous file (handles cross-file truncation).

    Returns:
        wakeup_events    – list of completed {t1, t2, t3, decision} dicts
        sent_bcast       – list of {ts, ts_ms, decoded}
        recv_bcast       – list of {ts, ts_ms, raw, decoded}
        pending          – the still-open event at EOF, or None
    """
    wakeup_events: list[dict] = []
    sent_bcast:    list[dict] = []
    recv_bcast:    list[dict] = []

    pending: dict | None = initial_pending

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                # ── wakeup events ──────────────────────────────────────────
                if _RE_T1.search(line):
                    ts = extract_timestamp(line)
                    if ts:
                        if pending:
                            wakeup_events.append(pending)
                        pending = {
                            "t1": ts, "t1_ms": ts_to_ms(ts),
                            "t2": None, "t2_ms": None,
                            "t3": None, "t3_ms": None,
                            "decision": None,
                        }

                elif _RE_T2.search(line):
                    ts = extract_timestamp(line)
                    if not ts:
                        continue
                    ts_ms = ts_to_ms(ts)
                    if pending is None:
                        pending = {
                            "t1": None, "t1_ms": None,
                            "t2": ts,   "t2_ms": ts_ms,
                            "t3": None, "t3_ms": None,
                            "decision": None,
                        }
                    elif pending["t2"] is None:
                        pending["t2"]    = ts
                        pending["t2_ms"] = ts_ms
                    else:
                        wakeup_events.append(pending)
                        pending = {
                            "t1": None, "t1_ms": None,
                            "t2": ts,   "t2_ms": ts_ms,
                            "t3": None, "t3_ms": None,
                            "decision": None,
                        }

                elif _RE_T3.search(line):
                    ts = extract_timestamp(line)
                    if not ts:
                        continue
                    ts_ms = ts_to_ms(ts)
                    if pending is None:
                        pending = {
                            "t1": None, "t1_ms": None,
                            "t2": None, "t2_ms": None,
                            "t3": ts,   "t3_ms": ts_ms,
                            "decision": None,
                        }
                    if pending["t3"] is None:
                        pending["t3"]    = ts
                        pending["t3_ms"] = ts_ms
                        m = _RE_DECISION.search(line)
                        pending["decision"] = m.group(1).lower() if m else "unknown"
                        wakeup_events.append(pending)
                        pending = None

                # ── broadcast events ───────────────────────────────────────

                # Android sends
                m = _RE_ANDROID_SEND.search(line)
                if m:
                    ts = extract_timestamp(line)
                    b  = parse_bracketed_int8(m.group(1))
                    if ts and b:
                        sent_bcast.append({
                            "ts":      ts,
                            "ts_ms":   ts_to_ms(ts),
                            "decoded": decode_broadcast(b),
                        })
                    continue

                # Android receives
                m = _RE_ANDROID_RECV.search(line)
                if m:
                    ts = extract_timestamp(line)
                    b  = parse_bracketed_int8(m.group(1))
                    if ts and b:
                        raw = ",".join(str(x) for x in b)
                        recv_bcast.append({
                            "ts":      ts,
                            "ts_ms":   ts_to_ms(ts),
                            "raw":     raw,
                            "decoded": decode_broadcast(b),
                        })
                    continue

                # HarmonyOS sends
                m = _RE_HMOS_SEND.search(line)
                if m:
                    ts = extract_timestamp(line)
                    b  = parse_json_bytes(m.group(1))
                    if ts and b:
                        sent_bcast.append({
                            "ts":      ts,
                            "ts_ms":   ts_to_ms(ts),
                            "decoded": decode_broadcast(b),
                        })
                    continue

                # HarmonyOS receives
                m = _RE_HMOS_RECV.search(line)
                if m:
                    ts = extract_timestamp(line)
                    b  = parse_csv_uint8(m.group(1))
                    if ts and b:
                        raw = ",".join(str(x) for x in b)
                        recv_bcast.append({
                            "ts":      ts,
                            "ts_ms":   ts_to_ms(ts),
                            "raw":     raw,
                            "decoded": decode_broadcast(b),
                        })

    except OSError as exc:
        print(f"  [WARN] Cannot read {filepath}: {exc}", file=sys.stderr)

    # Do NOT flush pending here — return it to the caller so it can be
    # threaded into the next file's scan as initial state (cross-file carry).
    return wakeup_events, sent_bcast, recv_bcast, pending


# ── O1 object builder ─────────────────────────────────────────────────────────

def _build_o1(ev: dict, sent: list[dict], recv: list[dict]) -> dict:
    """
    Given a wakeup event and the full sent/received broadcast lists for
    the device, extract the broadcasts that belong to this event and
    return an O1 object.

    Time windows:
      anchor_ms  = t1_ms if t1 else t2_ms
      T4_ms      = anchor_ms + 2000   (wakeup window ends)
      T1a window = [anchor_ms, t2_ms)  — pre-wakeup sent, type==1
      T2a window = [t2_ms,     t3_ms + 1000)  — wakeup sent, type==0
      T2b window = [anchor_ms, T4_ms]  — all received
    """
    t1_ms     = ev.get("t1_ms")
    t2_ms     = ev.get("t2_ms")
    t3_ms     = ev.get("t3_ms")
    anchor_ms = t1_ms if t1_ms is not None else t2_ms

    T4_ms = (anchor_ms + 2000) if anchor_ms is not None else None

    # ── T1a: pre-wakeup broadcast sent by this device ─────────────────────
    t1a_entry = None
    if anchor_ms is not None and t2_ms is not None:
        for s in sent:
            if (anchor_ms <= s["ts_ms"] < t2_ms
                    and s["decoded"].get("type") == 1):
                t1a_entry = s
                break

    # ── T2a: wakeup broadcast sent by this device ─────────────────────────
    t2a_entry = None
    t2a_end   = (t3_ms + 1000) if t3_ms is not None else (T4_ms or 0)
    if t2_ms is not None:
        for s in sent:
            if (t2_ms <= s["ts_ms"] < t2a_end
                    and s["decoded"].get("type") == 0):
                t2a_entry = s
                break

    # ── deviceType / UDID from sent wakeup broadcast (fallback: pre-wakeup) ─
    device_type = None
    device_udid = None
    src = t2a_entry or t1a_entry
    if src:
        device_type = src["decoded"].get("deviceType")
        device_udid = src["decoded"].get("udid")

    # ── T2b: received broadcasts in the wakeup window ─────────────────────
    # De-duplicate by raw byte content: BLE advertises at ~20ms intervals,
    # so the same payload may appear dozens of times. Keep only the first
    # occurrence of each unique raw string.
    received = []
    seen_raw: set[str] = set()
    if anchor_ms is not None and T4_ms is not None:
        for r in recv:
            if anchor_ms <= r["ts_ms"] <= T4_ms:
                if r["raw"] not in seen_raw:
                    seen_raw.add(r["raw"])
                    received.append({
                        "Broadcast":   r["raw"],
                        "ReceiveTime": r["ts"],
                        "Decoded":     r["decoded"],
                    })

    return {
        "deviceType":        device_type,
        "deviceTypeName":    _DEVICE_TYPE_NAMES.get(device_type, "未知") if device_type else None,
        "DeviceUdid":        device_udid,
        "L1WakeupTime":      ev.get("t1"),
        "L1BroadcastTime":   t1a_entry["ts"] if t1a_entry else None,
        "L1BroadcastData":   t1a_entry["decoded"] if t1a_entry else None,
        "L2WakeupTime":      ev.get("t2"),
        "L2BroadcastTime":   t2a_entry["ts"] if t2a_entry else None,
        "L2BroadcastData":   t2a_entry["decoded"] if t2a_entry else None,
        "ReceivedBroadcasts": received,
        "DecisionTime":      ev.get("t3"),
        "Decision":          ev.get("decision"),
    }


# ── public API ────────────────────────────────────────────────────────────────

def parse_device_logs(device_hilog_dir: Path, progress_cb=None) -> list[dict]:
    """
    Parse all log files in *device_hilog_dir* and return a list of O1 objects,
    one per detected wakeup event.

    Args:
        device_hilog_dir: path to <device>/hilog/ directory
        progress_cb:      optional callable(current: int, total: int, filename: str)
                          called before each file is scanned (1-indexed current)
    """
    log_files = sorted(
        f for f in device_hilog_dir.iterdir()
        if f.is_file() and is_log_file(f)
    )
    if not log_files:
        return []

    total_files = len(log_files)
    all_wakeup: list[dict] = []
    all_sent:   list[dict] = []
    all_recv:   list[dict] = []

    # Thread pending state across files so wakeup events that straddle a
    # file boundary are completed rather than split into two fragments.
    carry: dict | None = None
    for i, lf in enumerate(log_files):
        if progress_cb:
            progress_cb(i + 1, total_files, lf.name)
        w, s, r, carry = _scan_file(lf, initial_pending=carry)
        all_wakeup.extend(w)
        all_sent.extend(s)
        all_recv.extend(r)
    # Flush any event still open after the final file
    if carry:
        all_wakeup.append(carry)

    # Sort by anchor time for deterministic ordering
    all_wakeup.sort(key=lambda e: e.get("t1_ms") or e.get("t2_ms") or 0)
    all_sent.sort(key=lambda e: e["ts_ms"])
    all_recv.sort(key=lambda e: e["ts_ms"])

    return [_build_o1(ev, all_sent, all_recv) for ev in all_wakeup]


# ── cross-device alignment ────────────────────────────────────────────────────

class _UnionFind:
    """Path-compressed, union-by-rank disjoint set."""

    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.rank   = [0] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]  # path halving
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


def _udid_hex(udid_bytes) -> str:
    """Convert 4-byte UDID list to uppercase hex string, or '未知' if absent."""
    if not udid_bytes:
        return "未知"
    try:
        return "".join(f"{b:02X}" for b in udid_bytes)
    except (TypeError, ValueError):
        return "未知"


def _median(vals: list) -> float:
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    return float(s[mid]) if n % 2 == 1 else (s[mid - 1] + s[mid]) / 2.0


def _iqr(vals: list) -> float:
    s = sorted(vals)
    n = len(s)
    return float(s[(3 * n) // 4] - s[n // 4])


def _anchor_ms_of(o1: dict) -> int | None:
    """Return anchor_ms (T1 or T2 in ms) for an O1 event."""
    t1 = o1.get("L1WakeupTime")
    t2 = o1.get("L2WakeupTime")
    if t1:
        return ts_to_ms(t1)
    if t2:
        return ts_to_ms(t2)
    return None


def align_sessions(all_results: dict[str, list[dict]]) -> list[dict]:
    """
    Group wakeup events from multiple devices into cross-device aligned sessions.

    Algorithm:
      Phase 1 — Union events whose sent-broadcast raw matches a received-broadcast raw.
      Phase 2 — Time-offset fallback (median + IQR) for still-unmatched events.
      Phase 3 — Build sessions from Union-Find components; assign per-device confidence
                 using the worst-link rule: one yellow link forces yellow, one red forces red.

    Returns a list of AlignedSession dicts::

        {
            "session_id":  int,
            "anchor_time": str,
            "entries": {
                "<device>": {
                    "event":             dict,   # O1 from parse_device_logs()
                    "confidence":        str,    # "green"|"yellow"|"red"|"isolated"
                    "received_from":     list,   # [{"device":str, "udid":str}, ...]
                    "not_received_from": list,   # [{"device":str, "udid":str}, ...]
                }, ...
            }
        }

    Absent devices simply have no key in "entries".
    """
    devices: list[str] = list(all_results.keys())

    if len(devices) < 2:
        # Single-device: every event is its own isolated session
        result = []
        for dev in devices:
            for i, ev in enumerate(all_results[dev]):
                ams = _anchor_ms_of(ev) or 0
                try:
                    at = (datetime.fromtimestamp(ams / 1000)
                          .strftime("%m-%d %H:%M:%S.") + f"{ams % 1000:03d}")
                except Exception:
                    at = "—"
                result.append({
                    "session_id":  i + 1,
                    "anchor_time": at,
                    "entries": {
                        dev: {
                            "event":             ev,
                            "confidence":        "isolated",
                            "received_from":     [],
                            "not_received_from": [],
                        }
                    },
                })
        return result

    # ── flatten all events ────────────────────────────────────────────────────
    flat: list[tuple[str, int, dict]] = []  # (device_name, local_idx, o1)
    for dev in devices:
        for idx, ev in enumerate(all_results[dev]):
            flat.append((dev, idx, ev))

    total = len(flat)
    uf = _UnionFind(total)

    node_of: dict[tuple[str, int], int] = {
        (dev, idx): nid for nid, (dev, idx, _) in enumerate(flat)
    }
    anchor_ms: list[int | None] = [_anchor_ms_of(ev) for (_, _, ev) in flat]

    # ── Phase 1: broadcast-exact matching ────────────────────────────────────
    # Build inverted index: raw_str -> [node_id that received it]
    recv_index: dict[str, list[int]] = {}
    for nid, (_, _, ev) in enumerate(flat):
        for rb in ev.get("ReceivedBroadcasts", []):
            raw = rb.get("Broadcast", "")
            if raw:
                recv_index.setdefault(raw, []).append(nid)

    # Union sender with all receivers of the same raw payload
    for nid, (dev, _, ev) in enumerate(flat):
        l2 = ev.get("L2BroadcastData") or {}
        l1 = ev.get("L1BroadcastData") or {}
        sent_raw = l2.get("raw") or l1.get("raw") or ""
        if not sent_raw:
            continue
        for recv_nid in recv_index.get(sent_raw, []):
            if flat[recv_nid][0] != dev:
                uf.union(nid, recv_nid)

    # ── Phase 2: time-offset fallback matching ────────────────────────────────
    # Collect per-pair deltas from Phase 1 matches only
    pair_deltas: dict[tuple[str, str], list[float]] = {}
    for na in range(total):
        for nb in range(na + 1, total):
            if uf.find(na) != uf.find(nb):
                continue
            dev_a, dev_b = flat[na][0], flat[nb][0]
            if dev_a == dev_b:
                continue
            key = (min(dev_a, dev_b), max(dev_a, dev_b))
            ams_a, ams_b = anchor_ms[na], anchor_ms[nb]
            if ams_a is None or ams_b is None:
                continue
            # delta = anchor_{key[0]} - anchor_{key[1]}
            delta = ams_a - ams_b if key[0] == dev_a else ams_b - ams_a
            pair_deltas.setdefault(key, []).append(delta)

    pair_stats: dict[tuple[str, str], tuple[float, float]] = {}
    for key, deltas in pair_deltas.items():
        if len(deltas) >= 3:
            off   = _median(deltas)
            iqr_v = _iqr(deltas)
        else:
            off, iqr_v = 0.0, 0.0
        pair_stats[key] = (off, iqr_v)

    # Match still-unmatched events between each ordered device pair
    for dev_x in devices:
        for dev_y in devices:
            if dev_x >= dev_y:
                continue
            key        = (dev_x, dev_y)
            off, iqr_v = pair_stats.get(key, (0.0, 0.0))
            outer      = max(3.0 * iqr_v, 2000.0)

            x_nodes = [node_of[(dev_x, i)] for i in range(len(all_results[dev_x]))]
            y_nodes = [node_of[(dev_y, i)] for i in range(len(all_results[dev_y]))]

            def _comp_has_dev(node_id: int, target_dev: str) -> bool:
                root = uf.find(node_id)
                return any(
                    flat[i][0] == target_dev
                    for i in range(total)
                    if uf.find(i) == root
                )

            for nx in x_nodes:
                if _comp_has_dev(nx, dev_y):
                    continue  # already joined to dev_y
                ams_x = anchor_ms[nx]
                if ams_x is None:
                    continue

                best_ny, best_diff = None, float("inf")
                for ny in y_nodes:
                    if _comp_has_dev(ny, dev_x):
                        continue
                    ams_y = anchor_ms[ny]
                    if ams_y is None:
                        continue
                    diff = abs((ams_x - ams_y) - off)
                    if diff <= outer and diff < best_diff:
                        best_diff = diff
                        best_ny = ny

                if best_ny is not None:
                    uf.union(nx, best_ny)

    # ── Phase 3: build sessions with per-device confidence ────────────────────
    components: dict[int, list[int]] = {}
    for nid in range(total):
        components.setdefault(uf.find(nid), []).append(nid)

    _CONF_RANK = {"green": 0, "yellow": 1, "red": 2}

    def _session_anchor(nids: list[int]) -> int:
        times = [anchor_ms[i] for i in nids if anchor_ms[i] is not None]
        return min(times) if times else 0

    sessions: list[dict] = []
    sid = 1

    for _, nids in sorted(components.items(), key=lambda kv: _session_anchor(kv[1])):
        # One node per device in this component
        dev_node: dict[str, int] = {}
        for nid in nids:
            dev_node[flat[nid][0]] = nid

        # Earliest anchor time as session label
        times = [anchor_ms[nid] for nid in nids if anchor_ms[nid] is not None]
        ams_min = min(times) if times else None
        if ams_min is not None:
            try:
                at = (datetime.fromtimestamp(ams_min / 1000)
                      .strftime("%m-%d %H:%M:%S.") + f"{ams_min % 1000:03d}")
            except Exception:
                at = "—"
        else:
            at = "—"

        entries: dict[str, dict] = {}

        for dev_i, ni in dev_node.items():
            ev_i  = flat[ni][2]
            ams_i = anchor_ms[ni]

            if len(dev_node) == 1:
                entries[dev_i] = {
                    "event":             ev_i,
                    "confidence":        "isolated",
                    "received_from":     [],
                    "not_received_from": [],
                }
                continue

            link_confs: list[str] = []
            recv_from:  list[dict] = []
            no_recv:    list[dict] = []

            for dev_j, nj in dev_node.items():
                if dev_j == dev_i:
                    continue
                ev_j  = flat[nj][2]
                ams_j = anchor_ms[nj]

                # Did dev_i directly receive dev_j's sent broadcast?
                l2j      = ev_j.get("L2BroadcastData") or {}
                l1j      = ev_j.get("L1BroadcastData") or {}
                sent_raw = l2j.get("raw") or l1j.get("raw") or ""
                rb_match = None
                if sent_raw:
                    for rb in ev_i.get("ReceivedBroadcasts", []):
                        if rb.get("Broadcast") == sent_raw:
                            rb_match = rb
                            break

                if rb_match is not None:
                    # Green link: broadcast received directly
                    link_confs.append("green")
                    dec = rb_match.get("Decoded") or {}
                    recv_from.append({
                        "device": dev_j,
                        "udid":   _udid_hex(dec.get("udid")),
                    })
                else:
                    # Time-estimate link: classify by IQR distance
                    key        = (min(dev_i, dev_j), max(dev_i, dev_j))
                    off, iqr_v = pair_stats.get(key, (0.0, 0.0))
                    if ams_i is not None and ams_j is not None:
                        delta = ams_i - ams_j if key == (dev_i, dev_j) else ams_j - ams_i
                        diff  = abs(delta - off)
                        link_confs.append("yellow" if diff <= iqr_v else "red")
                    else:
                        link_confs.append("yellow")
                    no_recv.append({
                        "device": dev_j,
                        "udid":   _udid_hex(ev_j.get("DeviceUdid")),
                    })

            # Worst-link rule: one yellow/red link dominates
            confidence = (
                max(link_confs, key=lambda c: _CONF_RANK.get(c, 0))
                if link_confs else "isolated"
            )

            entries[dev_i] = {
                "event":             ev_i,
                "confidence":        confidence,
                "received_from":     recv_from,
                "not_received_from": no_recv,
            }

        sessions.append({
            "session_id":  sid,
            "anchor_time": at,
            "entries":     entries,
        })
        sid += 1

    return sessions


# ── CLI test entry point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import json as _json

    logs_root = Path("./logs")
    if not logs_root.is_dir():
        print(f"[ERROR] ./logs/ not found", file=sys.stderr)
        sys.exit(1)

    for device_dir in sorted(logs_root.iterdir()):
        if not device_dir.is_dir():
            continue
        hilog_dir = device_dir / "hilog"
        if not hilog_dir.is_dir():
            continue

        print(f"\n{'='*60}")
        print(f"Device: {device_dir.name}")
        print(f"{'='*60}")
        results = parse_device_logs(hilog_dir)
        print(_json.dumps(results, ensure_ascii=False, indent=2))
        print(f"→ {len(results)} wakeup event(s)")
