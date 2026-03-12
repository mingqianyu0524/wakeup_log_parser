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


# ── main page ─────────────────────────────────────────────────────────────────

@ui.page("/")
def index():
    # Per-session state
    device_rows: list[dict] = []   # each: {"input": ui.input}
    result_area = None

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
                    # Derive UDID from the first event that has it
                    device_udid = None
                    for ev in events:
                        udid_bytes = ev.get("DeviceUdid")
                        if udid_bytes:
                            device_udid = "".join(f"{b:02X}" for b in udid_bytes)
                            break
                    udid_part = f"  UDID={device_udid}" if device_udid else ""
                    with ui.expansion(
                        f"📱 {device_name}{udid_part}  ({len(events)} 次唤醒)",
                        icon="devices",
                    ).classes("w-full border rounded shadow-sm"):
                        if not events:
                            ui.label("未找到唤醒事件").classes("text-gray-400 p-2")
                        else:
                            with ui.column().classes("w-full gap-0 p-2"):
                                for i, o1 in enumerate(events):
                                    _render_event_card(i, o1)

        parse_btn.on_click(on_parse)


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title="唤醒日志分析工具",
        favicon="🔔",
        port=8080,
        reload=False,
    )
