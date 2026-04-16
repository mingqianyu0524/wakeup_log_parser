#!/usr/bin/env python3
"""
Wakeup Log Analyzer UI
----------------------
NiceGUI-based desktop tool (runs at http://localhost:8080).

Features:
  • Add multiple device log paths via folder picker
  • Parse all devices in one click (runs in background thread)
  • Expandable result list: device → wakeup event → full O1 details
"""

from __future__ import annotations

import threading
from pathlib import Path

from nicegui import app, run, ui

from broadcast_parser import (
    _DEVICE_TYPE_NAMES,
    align_sessions,
    is_log_file,
    parse_device_logs,
)

# ── folder picker (uses tkinter just for the native dialog) ──────────────────

def _pick_folder() -> str | None:
    """Open a native OS folder-picker dialog and return the selected path."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="选择设备 hilog 目录")
        root.destroy()
        return path or None
    except Exception:
        return None


# ── helper: render one O1 wakeup event card ──────────────────────────────────

def _render_broadcast_detail(bc: dict) -> None:
    d = bc.get("Decoded", {})
    if not d:
        ui.label(bc.get("Broadcast", "—")).classes("text-xs font-mono text-gray-500")
        return

    btype = d.get("typeName", "—")
    dtype = d.get("deviceTypeName", "—")
    ts    = d.get("timestamp_human", "—")
    ts_b  = d.get("timestamp_bytes", [])
    ts_u  = d.get("timestamp_unix", "—")
    udid  = d.get("udid", [])
    energy  = d.get("voiceEnergy", "—")
    conf    = d.get("voiceConfidence", "—")
    supp    = d.get("suppressed", "—")
    supp_s  = ("抑制" if supp == 63 else "不抑制") if isinstance(supp, int) else "—"
    recv_t  = bc.get("ReceiveTime")

    with ui.column().classes("gap-0 text-xs font-mono"):
        ui.label(f"广播类型: {btype}")
        ui.label(f"设备类型: {dtype}  [raw={d.get('deviceType','—')}]")
        ui.label(
            f"时间戳字节: {ts_b}  →  unix={ts_u}  →  {ts}"
        )
        if recv_t:
            ui.label(f"收到时间: {recv_t}").classes("text-blue-700")
        ui.label(f"设备UDID: {udid}")
        ui.label(f"声强: {energy}   声纹置信度: {conf}   抑制: {supp_s}({supp})")
        ui.label(f"原始数据: {bc.get('Broadcast','—')}").classes("text-gray-500")


def _render_event_card(idx: int, o1: dict) -> None:
    """Render an expandable card for one O1 wakeup event."""
    t1  = o1.get("L1WakeupTime")
    t2  = o1.get("L2WakeupTime") or "—"
    dec = o1.get("Decision") or "—"
    dtype_name = o1.get("deviceTypeName") or "—"
    dtype_raw  = o1.get("deviceType")
    dtype_str  = f"{dtype_name}[{dtype_raw}]" if dtype_raw else dtype_name

    # Show T1 if it exists, otherwise fall back to T2
    display_time     = t1 if t1 else t2
    display_time_lbl = "T1" if t1 else "T2"

    dec_label = "响应" if dec == "true" else "不响应" if dec == "false" else dec
    dec_badge_cls = (
        "bg-green-100 text-green-700" if dec == "true"
        else "bg-red-100 text-red-600" if dec == "false"
        else "bg-gray-100 text-gray-500"
    )
    dec_color = (
        "text-green-600" if dec == "true"
        else "text-red-500" if dec == "false"
        else "text-gray-500"
    )

    exp = ui.expansion().classes("w-full border rounded mb-1")
    with exp.add_slot("header"):
        with ui.row().classes("items-center gap-3 w-full py-0.5"):
            # Colored index badge
            ui.label(f"#{idx + 1}").classes(
                "bg-indigo-500 text-white text-xs font-bold "
                "px-2 py-0.5 rounded-full min-w-[2rem] text-center"
            )
            # Single wakeup time (T1 preferred, T2 fallback)
            ui.label(f"{display_time_lbl}=").classes("text-gray-400 text-xs -mr-2")
            ui.label(display_time).classes("font-mono text-sm text-gray-700")
            # Decision badge
            ui.label(dec_label).classes(
                f"text-xs font-semibold px-2 py-0.5 rounded {dec_badge_cls} ml-auto"
            )

    with exp:
        with ui.grid(columns=2).classes("w-full gap-x-6 gap-y-1 text-sm p-2"):

            # ── left column ────────────────────────────────────────────
            with ui.column().classes("gap-1"):
                ui.label("📡 唤醒时间线").classes("font-bold text-blue-700")
                _kv("设备类型",       dtype_str)
                _kv("一级唤醒 T1",    o1.get("L1WakeupTime"))
                _kv("发送预唤醒广播 T1a", o1.get("L1BroadcastTime"))
                _kv("二级唤醒 T2",    o1.get("L2WakeupTime"))
                _kv("发送唤醒广播 T2a",   o1.get("L2BroadcastTime"))
                _kv("决策时间 T3",    o1.get("DecisionTime"))
                ui.label(f"决策结果: {dec}").classes(f"font-semibold {dec_color}")

            # ── right column: sent broadcast details ───────────────────
            with ui.column().classes("gap-1"):
                ui.label("📤 发送广播详情").classes("font-bold text-blue-700")
                l1_bd = o1.get("L1BroadcastData")
                l2_bd = o1.get("L2BroadcastData")
                if l1_bd:
                    ui.label("预唤醒广播 (T1a):").classes("font-medium mt-1")
                    _render_broadcast_detail({"Decoded": l1_bd, "Broadcast": l1_bd.get("raw","")})
                else:
                    ui.label("预唤醒广播 (T1a): 无").classes("text-gray-400")
                if l2_bd:
                    ui.label("唤醒广播 (T2a):").classes("font-medium mt-1")
                    _render_broadcast_detail({"Decoded": l2_bd, "Broadcast": l2_bd.get("raw","")})
                else:
                    ui.label("唤醒广播 (T2a): 无").classes("text-gray-400")

        # ── received broadcasts ─────────────────────────────────────────
        received = o1.get("ReceivedBroadcasts", [])
        if received:
            ui.label(f"📥 收到广播 ({len(received)} 条)").classes(
                "font-bold text-blue-700 px-2 mt-2"
            )
            for rb in received:
                with ui.expansion(
                    f"  收到时间: {rb.get('ReceiveTime','—')}"
                ).classes("w-full border-l-2 border-blue-200 ml-2 mb-1"):
                    _render_broadcast_detail(rb)
        else:
            ui.label("📥 收到广播: 无").classes("text-gray-400 px-2 mt-1 text-sm")


def _kv(label: str, value) -> None:
    display = value if value else "—"
    ui.label(f"{label}: {display}").classes("text-sm")


def _parse_time_ms(t: str | None) -> int | None:
    """Parse 'MM-DD HH:MM:SS.mmm' → absolute ms (suitable for delta calculation)."""
    if not t:
        return None
    try:
        date_str, time_str = t.split(" ", 1)
        month, day = map(int, date_str.split("-"))
        hms, ms_str = time_str.rsplit(".", 1)
        h, m, s = map(int, hms.split(":"))
        return (((month * 31 + day) * 86400 + h * 3600 + m * 60 + s) * 1000
                + int(ms_str))
    except Exception:
        return None


def _inline_parse_btn(label: str, bc: dict) -> None:
    """Lazy-rendered inline 解析 button for a single broadcast entry."""
    detail_box = ui.column().classes("gap-0 pl-2 mt-0.5")
    detail_box.set_visibility(False)
    _ps: dict = {"rendered": False, "shown": False}

    def _toggle(box=detail_box, b=bc, ps=_ps, lbl=label):
        if not ps["rendered"]:
            with box:
                ui.label(lbl).classes("text-xs font-medium text-blue-700")
                _render_broadcast_detail(b)
            ps["rendered"] = True
        ps["shown"] = not ps["shown"]
        box.set_visibility(ps["shown"])

    ui.button("解析", on_click=_toggle, icon="science").props(
        "flat dense size=xs"
    ).classes("text-indigo-500 self-start mt-0.5")


# ── alignment view helpers ────────────────────────────────────────────────────

_CONF_BORDER = {
    "green":    "border-green-400",
    "yellow":   "border-yellow-400",
    "red":      "border-red-400",
    "isolated": "border-gray-300",
}
_CONF_ICON = {
    "green":    "🟢",
    "yellow":   "🟡",
    "red":      "🔴",
    "isolated": "🔵",
}

# Device response priority: higher integer = higher priority (should respond first)
_DEVICE_PRIORITY: dict[int, int] = {
    240: 10,  # 车机
    230: 9,   # 移动智慧屏
    220: 8,   # OMELCD
    210: 7,   # 音箱
    150: 6,   # 大屏
    110: 5,   # 手表
    80:  4,   # 耳机
    50:  3,   # 手机
    40:  2,   # 平板
    30:  1,   # PC
}


def _classify_session(entries: dict) -> tuple[str, str, str]:
    """
    Classify a session's wakeup response pattern.
    Returns (status, label, badge_css_classes):
      "normal"  — correct (highest-priority device responded) or no response
      "double"  — 2+ devices responded and highest-priority device is among them
      "wrong"   — 2+ devices responded but highest-priority NOT among them,
                  OR exactly 1 responded but it's not the highest-priority device
    """
    responding_types: list[int] = []
    all_types:        list[int] = []
    for entry in entries.values():
        ev    = entry["event"]
        dtype = ev.get("deviceType")
        if dtype is not None:
            all_types.append(dtype)
        if ev.get("Decision") == "true" and dtype is not None:
            responding_types.append(dtype)

    n_resp = len(responding_types)
    if n_resp == 0:
        return ("normal", "", "")

    highest = (
        max(all_types, key=lambda t: _DEVICE_PRIORITY.get(t, 0))
        if all_types else None
    )

    if n_resp == 1:
        if highest is None or responding_types[0] == highest:
            return ("normal", "", "")
        return (
            "wrong", "错响",
            "bg-yellow-100 text-yellow-800 border border-yellow-400",
        )

    # n_resp >= 2
    if highest in responding_types:
        return (
            "double", "双响",
            "bg-red-100 text-red-700 border border-red-400",
        )
    return (
        "wrong", "错响",
        "bg-yellow-100 text-yellow-800 border border-yellow-400",
    )


def _build_dev_info(sessions: list[dict]) -> dict[str, tuple[str, str]]:
    """
    Build {dev_name: (dtype_name, udid_hex)} from the first available event
    per device across all sessions.  Used to label absent-device slots without
    relying on raw directory names.
    """
    info: dict[str, tuple[str, str]] = {}
    for sess in sessions:
        for dev_name, entry in sess["entries"].items():
            if dev_name not in info:
                ev       = entry["event"]
                dtype    = ev.get("deviceTypeName") or "—"
                udid_raw = ev.get("DeviceUdid")
                udid     = ",".join(str(b) for b in udid_raw) if udid_raw else "未知"
                info[dev_name] = (dtype, udid)
    return info


def _render_aligned_device_row(
    dev_name: str,
    entry:    dict | None,
    dev_info: dict[str, tuple[str, str]],
) -> None:
    """Render one device row (expandable) in the aligned session view."""

    # ── absent device ──────────────────────────────────────────────────────────
    if entry is None:
        dtype, udid = dev_info.get(dev_name, ("—", "未知"))
        with ui.row().classes(
            "w-full border rounded px-3 py-2 mb-1 bg-gray-50 "
            "opacity-60 items-center gap-2"
        ):
            ui.label("⬜").classes("text-base")
            ui.label(f"{dtype}  UDID:{udid}").classes("text-sm text-gray-500 flex-1")
            ui.label("缺席").classes("text-xs text-gray-400 italic")
        return

    # ── present device ─────────────────────────────────────────────────────────
    conf        = entry.get("confidence", "isolated")
    icon        = _CONF_ICON.get(conf, "⬜")
    ev          = entry["event"]
    dtype_name  = ev.get("deviceTypeName") or "—"
    device_udid = ev.get("DeviceUdid")
    udid_str    = ",".join(str(b) for b in device_udid) if device_udid else "未知"
    dec         = ev.get("Decision") or "—"
    dec_label   = "响应" if dec == "true" else "不响应" if dec == "false" else dec
    dec_badge   = (
        "bg-green-100 text-green-700" if dec == "true"
        else "bg-gray-100 text-gray-500"
    )
    recv_from = entry.get("received_from", [])
    no_recv   = entry.get("not_received_from", [])

    # ── expansion with custom header ──────────────────────────────────────────
    exp = ui.expansion().classes(
        f"w-full border-l-4 {_CONF_BORDER.get(conf, 'border-gray-300')} "
        "border border-gray-200 rounded mb-1"
    )
    with exp.add_slot("header"):
        with ui.row().classes("items-center gap-2 w-full py-0.5"):
            ui.label(icon).classes("text-base leading-none")
            ui.label(f"{dtype_name}  UDID:{udid_str}").classes("text-sm flex-1")
            ui.label(dec_label).classes(
                f"text-xs font-semibold px-2 py-0.5 rounded {dec_badge}"
            )

    with exp:
        with ui.column().classes("gap-2 p-2 text-sm"):

            # ── 时间栏 ──────────────────────────────────────────────────────
            ui.label("时间").classes(
                "text-xs font-bold text-gray-400 uppercase tracking-wide"
            )
            anchor_ms = (
                _parse_time_ms(ev.get("L1WakeupTime"))
                or _parse_time_ms(ev.get("L2WakeupTime"))
            )

            def _row_time(label: str, t_str: str | None) -> None:
                ms = _parse_time_ms(t_str)
                with ui.row().classes("items-baseline gap-1 flex-nowrap"):
                    ui.label(label + ":").classes(
                        "text-sm text-gray-600 w-36 shrink-0"
                    )
                    ui.label(t_str or "—").classes("text-sm font-mono")
                    if (ms is not None and anchor_ms is not None
                            and ms != anchor_ms):
                        delta = ms - anchor_ms
                        ui.label(f"【+{delta}ms】").classes(
                            "text-xs text-gray-400 italic"
                        )

            recv_list = ev.get("ReceivedBroadcasts", [])
            with ui.column().classes("gap-0 pl-1"):
                _row_time("T1   一级唤醒",       ev.get("L1WakeupTime"))
                _row_time("T1a  预唤醒广播",      ev.get("L1BroadcastTime"))
                _row_time("T2   二级唤醒",        ev.get("L2WakeupTime"))
                _row_time("T2a  发出唤醒广播",    ev.get("L2BroadcastTime"))
                if recv_list:
                    earliest = min(
                        (r.get("ReceiveTime") for r in recv_list
                         if r.get("ReceiveTime")),
                        default=None,
                    )
                    _row_time(
                        f"T2b  接收广播（{len(recv_list)}条）", earliest
                    )
                _row_time("T3   决策",            ev.get("DecisionTime"))

            ui.separator().classes("my-0.5")

            # ── 广播栏 ──────────────────────────────────────────────────────
            ui.label("广播数据").classes(
                "text-xs font-bold text-gray-400 uppercase tracking-wide"
            )
            l1bd   = ev.get("L1BroadcastData")
            l1_raw = l1bd.get("raw", "") if l1bd else ""
            l2bd   = ev.get("L2BroadcastData")
            l2_raw = l2bd.get("raw", "") if l2bd else ""

            def _sent_row(raw: str, bd: dict, expand_lbl: str) -> None:
                supp       = (bd.get("suppressed") == 63) if bd else False
                is_fail    = (bd.get("type") == 2) if bd else False
                dtype_name = (bd.get("deviceTypeName") or "—") if bd else "—"
                udid_bytes = (bd.get("udid") or []) if bd else []
                udid_str   = ",".join(str(b) for b in udid_bytes) or "未知"
                type_name  = (bd.get("typeName") or "广播") if bd else "广播"
                dev_disp   = f"[{dtype_name}] UDID:{udid_str}"
                head_text  = f"{dev_disp}  ·  {type_name}"
                if supp:
                    head_text += "  ·  抑制"
                if is_fail:
                    bg_cls, head_cls, body_cls, arrow_cls = (
                        "bg-red-50", "text-red-700", "text-red-800", "text-red-600",
                    )
                elif supp:
                    bg_cls, head_cls, body_cls, arrow_cls = (
                        "bg-orange-50", "text-orange-700", "text-orange-800", "text-orange-600",
                    )
                else:
                    bg_cls, head_cls, body_cls, arrow_cls = (
                        "bg-green-50", "text-green-700", "text-green-800", "text-green-600",
                    )
                with ui.row().classes(
                    f"items-start gap-2 {bg_cls} rounded px-1 flex-wrap"
                ):
                    ui.label("↑发").classes(f"{arrow_cls} w-10 shrink-0")
                    with ui.column().classes("gap-0 flex-1"):
                        ui.label(head_text).classes(
                            f"text-xs {head_cls} font-semibold not-italic"
                        )
                        ui.label(raw).classes(f"{body_cls} break-all")
                    _inline_parse_btn(expand_lbl, {"Decoded": bd, "Broadcast": raw})

            with ui.column().classes("gap-1 pl-1 font-mono text-xs"):
                if l1_raw:
                    _sent_row(l1_raw, l1bd, "预唤醒广播 T1a")
                if l2_raw:
                    _sent_row(l2_raw, l2bd, "唤醒广播 T2a")

                # Iterate ALL received broadcasts (not just session-peer matches)
                # so wakeup-failure broadcasts and traffic from unlabelled peers
                # also appear here.
                all_recv = ev.get("ReceivedBroadcasts", [])
                peer_by_udid: dict[str, str] = {
                    rf.get("udid", ""): rf["device"]
                    for rf in recv_from if rf.get("udid")
                }
                matched_udids = set(peer_by_udid)

                for rb in all_recv:
                    dec          = rb.get("Decoded") or {}
                    rb_udid      = ",".join(
                        str(b) for b in (dec.get("udid") or [])
                    ) or "未知"
                    rb_tname     = dec.get("typeName", "广播")
                    rb_dtype_dec = dec.get("deviceTypeName", "—")
                    raw_str      = rb.get("Broadcast", "—")
                    rb_type      = dec.get("type")
                    rb_supp      = (dec.get("suppressed") == 63)
                    rb_recv_t    = rb.get("ReceiveTime", "")

                    peer_dev = peer_by_udid.get(rb_udid)
                    if peer_dev:
                        peer_dtype, _ = dev_info.get(peer_dev, (rb_dtype_dec, rb_udid))
                        dev_display   = f"[{peer_dtype}] UDID:{rb_udid}"
                    else:
                        dev_display   = f"[{rb_dtype_dec}] UDID:{rb_udid}"

                    # Coloring priority:
                    #   type-2 失败广播  → 红底
                    #   suppressed (byte[12]==63) → 橙底
                    #   其他              → 绿底
                    if rb_type == 2:
                        bg_cls, head_cls, body_cls, arrow_cls = (
                            "bg-red-50", "text-red-700",
                            "text-red-800", "text-red-600",
                        )
                    elif rb_supp:
                        bg_cls, head_cls, body_cls, arrow_cls = (
                            "bg-orange-50", "text-orange-700",
                            "text-orange-800", "text-orange-600",
                        )
                    else:
                        bg_cls, head_cls, body_cls, arrow_cls = (
                            "bg-green-50", "text-green-700",
                            "text-green-800", "text-green-600",
                        )

                    head_text = f"{dev_display}  ·  {rb_tname}"
                    if rb_supp:
                        head_text += "  ·  抑制"

                    with ui.row().classes(
                        f"items-start gap-2 {bg_cls} rounded px-1 flex-wrap"
                    ):
                        ui.label("↓收").classes(f"{arrow_cls} w-10 shrink-0")
                        with ui.column().classes("gap-0 flex-1"):
                            ui.label(head_text).classes(
                                f"text-xs {head_cls} font-semibold not-italic"
                            )
                            if rb_recv_t:
                                ui.label(f"⏱ 收到 {rb_recv_t}").classes(
                                    f"text-xs {head_cls} opacity-70"
                                )
                            ui.label(raw_str).classes(f"{body_cls} break-all")
                        _inline_parse_btn(f"收到广播 [{rb_dtype_dec}]", rb)

                # Session peers we expected to hear from but did NOT
                # (only list peers whose UDID does not already appear above;
                # otherwise a partial miss — e.g. got T2a but not T1a — would
                # look contradictory).
                for nr in no_recv:
                    nr_dtype, _ = dev_info.get(nr["device"], ("—", ""))
                    nr_udid     = nr.get("udid", "")
                    if nr_udid and nr_udid in matched_udids:
                        continue
                    dev_display = f"[{nr_dtype}] UDID:{nr_udid}"
                    missing     = nr.get("missing_bcasts", [])
                    prefix      = (
                        f"未收到{missing[0]}" if len(missing) == 1
                        else "未收到设备广播"
                    )
                    with ui.row().classes(
                        "items-start gap-2 bg-yellow-50 rounded px-1"
                    ):
                        ui.label("✗").classes("text-yellow-600 w-10 shrink-0")
                        ui.label(f"{prefix} {dev_display}").classes(
                            "text-yellow-700 italic"
                        )

                if not l1_raw and not l2_raw and not all_recv and not no_recv:
                    ui.label("无广播数据").classes("text-gray-400 italic")


def _render_session_list(
    sessions: list[dict],
    all_device_names: list[str],
    dev_info: dict[str, tuple[str, str]],
) -> None:
    """Render a (possibly filtered) list of aligned session expansions."""
    if not sessions:
        ui.label("无匹配结果").classes("text-gray-400 text-sm italic")
        return

    for sess in sessions:
        n_devs                         = len(sess["entries"])
        status, status_lbl, status_css = _classify_session(sess["entries"])

        exp = ui.expansion().classes("w-full border rounded shadow-sm mb-1")
        with exp.add_slot("header"):
            with ui.row().classes("items-center gap-3 w-full py-1"):
                ui.icon("compare_arrows").classes("text-gray-500 text-base")
                ui.label(
                    f"唤醒 #{sess['session_id']}  {sess['anchor_time']}  ({n_devs}台设备)"
                ).classes("text-sm flex-1")
                if status_lbl:
                    ui.label(status_lbl).classes(
                        f"text-xs font-bold px-2 py-0.5 rounded {status_css}"
                    )

        with exp:
            with ui.column().classes("w-full gap-0 p-2"):

                # ── 响应结果 summary ──────────────────────────────────────
                with ui.row().classes(
                    "gap-2 flex-wrap items-center px-1 py-1.5 bg-gray-50 rounded mb-2"
                ):
                    ui.label("响应结果").classes("text-xs font-bold text-gray-600")
                    for dev in all_device_names:
                        dentry = sess["entries"].get(dev)
                        if dentry is None:
                            continue
                        ev       = dentry["event"]
                        d        = ev.get("Decision")
                        lbl      = "响应" if d == "true" else "不响应"
                        # Read directly from this event (not the cached dev_info)
                        dtype    = ev.get("deviceTypeName") or "—"
                        udid_raw = ev.get("DeviceUdid")
                        udid     = ",".join(str(b) for b in udid_raw) if udid_raw else "未知"
                        badge    = (
                            "bg-green-100 text-green-700" if d == "true"
                            else "bg-gray-100 text-gray-500"
                        )
                        ui.label(f"{dtype}·{udid}  {lbl}").classes(
                            f"text-xs font-mono px-2 py-0.5 rounded {badge}"
                        )

                # ── device rows ───────────────────────────────────────────
                for dev in all_device_names:
                    _render_aligned_device_row(
                        dev, sess["entries"].get(dev), dev_info
                    )


def _render_aligned_sessions(
    sessions: list[dict],
    all_device_names: list[str],
) -> None:
    """Render the full cross-device alignment view with filter bar + expansion list."""
    if not sessions:
        ui.label("无对齐结果").classes("text-gray-400")
        return

    dev_info = _build_dev_info(sessions)

    ui.label(f"唤醒对齐视图 (共 {len(sessions)} 次唤醒)").classes(
        "text-lg font-semibold text-gray-700"
    )
    with ui.row().classes("gap-3 text-xs text-gray-500 mb-2 flex-wrap items-center"):
        ui.label("🟢 广播匹配")
        ui.label("🟡 时间估算(IQR内)")
        ui.label("🔴 时间估算(IQR外)")
        ui.label("⬜ 未参与")
        ui.label("·")
        ui.label("标题红=双响  黄=错响")

    # ── filter bar ────────────────────────────────────────────────────────────
    with ui.row().classes(
        "gap-2 items-center flex-wrap border rounded p-2 mb-2 bg-gray-50"
    ):
        ui.label("过滤").classes("text-xs font-bold text-gray-500 shrink-0")
        t_start = (
            ui.input(placeholder="开始 MM-DD HH:MM", value="")
            .props("dense clearable")
            .classes("w-40")
        )
        ui.label("~").classes("text-xs text-gray-400 shrink-0")
        t_end = (
            ui.input(placeholder="结束 MM-DD HH:MM", value="")
            .props("dense clearable")
            .classes("w-40")
        )
        ui.button("应用", icon="filter_list", on_click=lambda: _refresh()).props(
            "flat dense size=sm"
        ).classes("text-blue-600 shrink-0")
        ui.separator().props("vertical inset").classes("mx-1 shrink-0")
        cb_normal = ui.checkbox("正常",  value=True)
        cb_double = ui.checkbox("双响", value=True)
        cb_wrong  = ui.checkbox("错响",  value=True)

    counter_lbl = ui.label("").classes("text-xs text-gray-500 mb-1")
    sess_col    = ui.column().classes("w-full gap-0")

    def _matches(sess: dict) -> bool:
        anchor        = sess["anchor_time"]  # "MM-DD HH:MM:SS.mmm"
        anchor_prefix = anchor[:11]          # "MM-DD HH:MM"
        ts = (t_start.value or "").strip()
        te = (t_end.value   or "").strip()
        if ts and anchor_prefix < ts:
            return False
        if te and anchor_prefix > te:
            return False
        status, _, _ = _classify_session(sess["entries"])
        if status == "normal" and not cb_normal.value:
            return False
        if status == "double" and not cb_double.value:
            return False
        if status == "wrong" and not cb_wrong.value:
            return False
        return True

    def _refresh() -> None:
        filtered = [s for s in sessions if _matches(s)]
        counter_lbl.set_text(f"显示 {len(filtered)} / {len(sessions)} 次唤醒")
        sess_col.clear()
        with sess_col:
            _render_session_list(filtered, all_device_names, dev_info)

    cb_normal.on_value_change(lambda _: _refresh())
    cb_double.on_value_change(lambda _: _refresh())
    cb_wrong.on_value_change(lambda _: _refresh())

    _refresh()  # Initial render (no filter applied)


# ── main page ─────────────────────────────────────────────────────────────────

@ui.page("/")
def index():
    # Per-session state
    device_rows: list[dict] = []   # each: {"input": ui.input}
    result_area = None
    _state: dict = {"all_results": {}}  # shared between on_parse and on_align

    # ── header ────────────────────────────────────────────────────────────
    with ui.header().classes("bg-blue-700 text-white items-center px-6 py-3 shadow"):
        ui.icon("sensors").classes("text-2xl mr-2")
        ui.label("唤醒日志分析工具").classes("text-xl font-bold")

    with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):

        # ── device path section ────────────────────────────────────────────
        ui.label("设备日志路径").classes("text-lg font-semibold text-gray-700")
        ui.label(
            "请选择每台设备的 hilog/ 目录（或直接输入路径）"
        ).classes("text-sm text-gray-500 -mt-3")

        rows_container = ui.column().classes("w-full gap-2")

        def add_device_row(initial_path: str = "") -> None:
            row_state: dict = {}
            with rows_container:
                with ui.row().classes("w-full items-center gap-2") as row_el:
                    inp = ui.input(
                        placeholder="/path/to/logs/deviceX/hilog",
                        value=initial_path,
                    ).classes("flex-1 font-mono text-sm")
                    row_state["input"] = inp

                    def on_browse(rs=row_state):
                        path = _pick_folder()
                        if path:
                            rs["input"].value = path

                    def on_delete(re=row_el, rs=row_state):
                        device_rows.remove(rs)
                        re.delete()

                    ui.button("浏览", on_click=on_browse).props("flat dense").classes(
                        "text-blue-600"
                    )
                    ui.button(icon="delete", on_click=on_delete).props(
                        "flat dense round"
                    ).classes("text-red-400")
            device_rows.append(row_state)

        # Start with one empty row
        add_device_row()

        with ui.row().classes("gap-3 mt-1"):
            ui.button("+ 添加设备", on_click=lambda: add_device_row()).props(
                "outline"
            ).classes("text-blue-700 border-blue-400")

            parse_btn = ui.button("开始解析", icon="play_arrow").classes(
                "bg-blue-700 text-white px-6"
            )

        ui.separator()

        # ── results area ────────────────────────────────────────────────
        results_label = ui.label("解析结果将显示在此处").classes(
            "text-gray-400 text-sm"
        )
        result_area = ui.column().classes("w-full gap-3")

        align_btn = ui.button("对齐分析", icon="compare_arrows").classes(
            "mt-2 bg-indigo-600 text-white"
        )
        align_btn.set_visibility(False)
        align_area = ui.column().classes("w-full gap-3")

        # ── parse logic ───────────────────────────────────────────────────

        async def on_parse():
            paths = [rs["input"].value.strip() for rs in device_rows
                     if rs["input"].value.strip()]
            if not paths:
                ui.notify("请先添加设备日志路径", type="warning")
                return

            # Build (device_name, hilog_dir) pairs.
            # Walk up past generic intermediate directories so that paths like
            # ".../RemoteLog_commercial_DEV-A/.../remoteLog/hilog" and
            # ".../RemoteLog_commercial_DEV-B/.../remoteLog/hilog" don't both
            # resolve to the same "remoteLog" key and overwrite each other.
            _GENERIC_DIR_NAMES = {"hilog", "remoteLog", "log", "logs"}
            path_pairs: list[tuple[str, Path]] = []
            for p in paths:
                hilog_dir = Path(p)
                node = hilog_dir
                while node.name in _GENERIC_DIR_NAMES and node.parent != node:
                    node = node.parent
                device_name = node.name or hilog_dir.name
                path_pairs.append((device_name, hilog_dir))

            # Pre-count log files per device (fast, synchronous)
            file_counts: dict[str, int] = {
                dev: (
                    sum(1 for f in hd.iterdir() if f.is_file() and is_log_file(f))
                    if hd.is_dir() else 0
                )
                for dev, hd in path_pairs
            }

            parse_btn.props("loading")
            results_label.set_text("正在解析…")
            result_area.clear()
            align_area.clear()
            align_btn.set_visibility(False)
            _state["all_results"] = {}

            # ── progress UI ───────────────────────────────────────────────────
            with result_area:
                prog_device = ui.label("").classes(
                    "text-sm text-gray-700 font-medium"
                )
                prog_bar   = ui.linear_progress(value=0).classes("w-full")
                prog_files = ui.label("").classes("text-xs text-gray-500")
                prog_wait  = ui.label("").classes("text-xs text-gray-400 italic mt-1")

            # Shared state: background thread writes, timer reads (GIL-safe)
            _prog: dict = {"current": 0, "total": 0, "file": ""}

            def _tick() -> None:
                t = _prog["total"]
                c = _prog["current"]
                ratio = c / t if t > 0 else 0.0
                prog_bar.value = ratio
                pct = round(ratio * 100)
                prog_files.set_text(
                    f"{pct}%  {c}/{t}  {_prog['file']}" if t > 0 else ""
                )

            progress_timer = ui.timer(0.15, _tick, active=True)

            # ── parse loop ────────────────────────────────────────────────────
            all_results: dict[str, list[dict]] = {}
            errors:      list[str] = []

            for idx, (device_name, hilog_dir) in enumerate(path_pairs):
                remaining = [
                    str(path_pairs[i][1]) for i in range(idx + 1, len(path_pairs))
                ]
                prog_device.set_text(f"正在解析: {hilog_dir}")
                prog_wait.set_text(
                    "等待: " + ", ".join(remaining) if remaining else ""
                )
                _prog["current"] = 0
                _prog["total"]   = file_counts.get(device_name, 0)
                _prog["file"]    = ""

                if not hilog_dir.is_dir():
                    errors.append(f"{hilog_dir}: 目录不存在")
                    continue

                def _cb(current: int, total: int, filename: str) -> None:
                    _prog["current"] = current
                    _prog["total"]   = total
                    _prog["file"]    = filename

                try:
                    events = await run.io_bound(parse_device_logs, hilog_dir, _cb)
                    all_results[device_name] = events
                except Exception as exc:
                    errors.append(f"{device_name}: {exc}")

            # ── stop progress, render results ─────────────────────────────────
            progress_timer.cancel()
            result_area.clear()
            parse_btn.props(remove="loading")

            with result_area:
                if errors:
                    for err in errors:
                        ui.notify(err, type="negative")

                if not all_results:
                    results_label.set_text("未找到任何唤醒事件，请确认路径和日志格式正确。")
                    return

                total = sum(len(v) for v in all_results.values())
                results_label.set_text(
                    f"解析完成：{len(all_results)} 台设备，共 {total} 次唤醒事件"
                )

                for device_name, events in all_results.items():
                    device_udid      = None
                    device_type_name = None
                    for ev in events:
                        if device_udid is None:
                            udid_bytes = ev.get("DeviceUdid")
                            if udid_bytes:
                                device_udid = ",".join(str(b) for b in udid_bytes)
                        if device_type_name is None:
                            device_type_name = ev.get("deviceTypeName")
                        if device_udid and device_type_name:
                            break
                    udid_part = f"  UDID:{device_udid}" if device_udid else ""
                    type_part = f"  [{device_type_name}]" if device_type_name else ""
                    with ui.expansion(
                        f"📱{type_part}{udid_part}  ({len(events)} 次唤醒)",
                        icon="devices",
                    ).classes("w-full border rounded shadow-sm"):
                        if not events:
                            ui.label("未找到唤醒事件").classes("text-gray-400 p-2")
                        else:
                            with ui.column().classes("w-full gap-0 p-2"):
                                for i, o1 in enumerate(events):
                                    _render_event_card(i, o1)

            _state["all_results"] = all_results
            align_btn.set_visibility(len(all_results) >= 2)

        async def on_align():
            align_area.clear()
            sessions = await run.io_bound(
                lambda: align_sessions(_state["all_results"])
            )
            with align_area:
                _render_aligned_sessions(sessions, list(_state["all_results"].keys()))

        align_btn.on_click(on_align)
        parse_btn.on_click(on_parse)


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title="唤醒日志分析工具",
        favicon="🔔",
        port=8080,
        reload=False,
    )
