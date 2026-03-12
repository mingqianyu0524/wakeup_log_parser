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


def _scan_file(filepath: Path) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Single-pass scan of one log file.

    Returns:
        wakeup_events  – list of {t1, t2, t3, decision} dicts with _ms fields
        sent_bcast     – list of {ts, ts_ms, decoded}
        recv_bcast     – list of {ts, ts_ms, raw, decoded}
    """
    wakeup_events: list[dict] = []
    sent_bcast:    list[dict] = []
    recv_bcast:    list[dict] = []

    pending: dict | None = None

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

    if pending:
        wakeup_events.append(pending)

    return wakeup_events, sent_bcast, recv_bcast


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

    # ── deviceType from sent wakeup broadcast (fallback: pre-wakeup) ──────
    device_type = None
    src = t2a_entry or t1a_entry
    if src:
        device_type = src["decoded"].get("deviceType")

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

def parse_device_logs(device_hilog_dir: Path) -> list[dict]:
    """
    Parse all log files in *device_hilog_dir* and return a list of O1 objects,
    one per detected wakeup event.

    Args:
        device_hilog_dir: path to <device>/hilog/ directory
    """
    log_files = sorted(
        f for f in device_hilog_dir.iterdir()
        if f.is_file() and is_log_file(f)
    )
    if not log_files:
        return []

    all_wakeup: list[dict] = []
    all_sent:   list[dict] = []
    all_recv:   list[dict] = []

    for lf in log_files:
        w, s, r = _scan_file(lf)
        all_wakeup.extend(w)
        all_sent.extend(s)
        all_recv.extend(r)

    # Sort by anchor time for deterministic ordering
    all_wakeup.sort(key=lambda e: e.get("t1_ms") or e.get("t2_ms") or 0)
    all_sent.sort(key=lambda e: e["ts_ms"])
    all_recv.sort(key=lambda e: e["ts_ms"])

    return [_build_o1(ev, all_sent, all_recv) for ev in all_wakeup]


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
