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

    with ui.column().classes("gap-0 text-xs font-mono"):
        ui.label(f"广播类型: {btype}")
        ui.label(f"设备类型: {dtype}  [raw={d.get('deviceType','—')}]")
        ui.label(
            f"时间戳字节: {ts_b}  →  unix={ts_u}  →  {ts}"
        )
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
                udid     = "".join(f"{b:02X}" for b in udid_raw) if udid_raw else "未知"
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
    udid_str    = "".join(f"{b:02X}" for b in device_udid) if device_udid else "未知"
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
            with ui.column().classes("gap-0 pl-1"):
                _kv("T1   一级唤醒",     ev.get("L1WakeupTime"))
                _kv("T1a  预唤醒广播",   ev.get("L1BroadcastTime"))
                _kv("T2   二级唤醒",     ev.get("L2WakeupTime"))
                _kv("T2a  唤醒广播",     ev.get("L2BroadcastTime"))
                _kv("T3   决策",         ev.get("DecisionTime"))

            ui.separator().classes("my-0.5")

            # ── 广播栏 ──────────────────────────────────────────────────────
            ui.label("广播数据").classes(
                "text-xs font-bold text-gray-400 uppercase tracking-wide"
            )
            broadcasts_to_parse: list[tuple[str, dict]] = []

            l1bd   = ev.get("L1BroadcastData")
            l1_raw = l1bd.get("raw", "") if l1bd else ""
            l2bd   = ev.get("L2BroadcastData")
            l2_raw = l2bd.get("raw", "") if l2bd else ""

            with ui.column().classes("gap-0.5 pl-1 font-mono text-xs"):
                if l1_raw:
                    with ui.row().classes("items-start gap-2"):
                        ui.label("↑T1a").classes("text-blue-500 w-10 shrink-0")
                        ui.label(l1_raw).classes("text-gray-700 break-all")
                    broadcasts_to_parse.append(
                        ("预唤醒广播 T1a", {"Decoded": l1bd, "Broadcast": l1_raw})
                    )
                if l2_raw:
                    with ui.row().classes("items-start gap-2"):
                        ui.label("↑T2a").classes("text-blue-500 w-10 shrink-0")
                        ui.label(l2_raw).classes("text-gray-700 break-all")
                    broadcasts_to_parse.append(
                        ("唤醒广播 T2a", {"Decoded": l2bd, "Broadcast": l2_raw})
                    )
                for rf in recv_from:
                    rf_udid  = rf.get("udid", "")
                    rb_match = None
                    for rb in ev.get("ReceivedBroadcasts", []):
                        dec_rb = rb.get("Decoded") or {}
                        u_list = dec_rb.get("udid") or []
                        u_hex  = "".join(f"{b:02X}" for b in u_list) if u_list else ""
                        if u_hex == rf_udid:
                            rb_match = rb
                            break
                    raw_str = rb_match.get("Broadcast", "—") if rb_match else "—"
                    dev_lbl = rf["device"]
                    with ui.row().classes(
                        "items-start gap-2 bg-green-50 rounded px-1"
                    ):
                        ui.label("↓收").classes("text-green-600 w-10 shrink-0")
                        ui.label(raw_str).classes("text-green-800 break-all")
                    if rb_match:
                        broadcasts_to_parse.append(
                            (f"收到广播 ({dev_lbl})", rb_match)
                        )
                for nr in no_recv:
                    with ui.row().classes(
                        "items-start gap-2 bg-yellow-50 rounded px-1"
                    ):
                        ui.label("✗").classes("text-yellow-600 w-10 shrink-0")
                        ui.label(f"未收到 {nr['device']}").classes(
                            "text-yellow-700 italic"
                        )
                if not l1_raw and not l2_raw and not recv_from and not no_recv:
                    ui.label("无广播数据").classes("text-gray-400 italic")

            # ── 解析按钮（懒加载）────────────────────────────────────────────
            if broadcasts_to_parse:
                parse_box = ui.column().classes("gap-1 mt-1")
                parse_box.set_visibility(False)
                _ps: dict = {"rendered": False, "shown": False}

                def _toggle(box=parse_box, bcs=broadcasts_to_parse, ps=_ps):
                    if not ps["rendered"]:
                        with box:
                            for lbl, bc in bcs:
                                ui.label(lbl).classes(
                                    "text-xs font-medium text-blue-700 mt-1"
                                )
                                _render_broadcast_detail(bc)
                        ps["rendered"] = True
                    ps["shown"] = not ps["shown"]
                    box.set_visibility(ps["shown"])

                ui.button("解析广播", on_click=_toggle, icon="science").props(
                    "flat dense size=sm"
                ).classes("text-indigo-600 self-start")


def _render_aligned_sessions(
    sessions: list[dict],
    all_device_names: list[str],
) -> None:
    """Render the full cross-device alignment view as an expansion list."""
    if not sessions:
        ui.label("无对齐结果").classes("text-gray-400")
        return

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

    dev_info = _build_dev_info(sessions)

    for sess in sessions:
        n_devs                       = len(sess["entries"])
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
                        ev    = dentry["event"]
                        d     = ev.get("Decision")
                        lbl   = "响应" if d == "true" else "不响应"
                        dtype, udid = dev_info.get(dev, ("—", "未知"))
                        badge = (
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

            parse_btn.props("loading")
            results_label.set_text("正在解析…")
            result_area.clear()
            align_area.clear()
            align_btn.set_visibility(False)
            _state["all_results"] = {}

            # Run blocking I/O in thread pool
            all_results: dict[str, list[dict]] = {}
            errors: list[str] = []

            def do_parse():
                for p in paths:
                    hilog_dir = Path(p)
                    device_name = (
                        hilog_dir.parent.name
                        if hilog_dir.name == "hilog"
                        else hilog_dir.name
                    )
                    if not hilog_dir.is_dir():
                        errors.append(f"{p}: 目录不存在")
                        continue
                    try:
                        events = parse_device_logs(hilog_dir)
                        all_results[device_name] = events
                    except Exception as exc:
                        errors.append(f"{device_name}: {exc}")

            await run.io_bound(do_parse)

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
                    # Derive UDID and device type from the first event that has them
                    device_udid      = None
                    device_type_name = None
                    for ev in events:
                        if device_udid is None:
                            udid_bytes = ev.get("DeviceUdid")
                            if udid_bytes:
                                device_udid = "".join(f"{b:02X}" for b in udid_bytes)
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
