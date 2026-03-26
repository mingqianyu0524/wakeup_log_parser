#!/usr/bin/env python3
"""
WearEngine 协同链路时延分析脚本

解析手表和手机的 HiLog 日志，计算各阶段时延并输出到 Excel。
"""

from __future__ import annotations

import re
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    print("缺少依赖，请执行: pip install openpyxl")
    sys.exit(1)

# ── 正则模式 ────────────────────────────────────────────────────────────────
TS = r'(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})'

WATCH_PATTERNS = {
    'a': re.compile(rf'{TS}.*\[before\] getConnectedDevices'),
    'b': re.compile(rf'{TS}.*\[after\] getConnectedDevices'),
    'c': re.compile(rf'{TS}.*\[after\] registerMessageReceiver'),
    'd': re.compile(rf'{TS}.*Succeeded in receiving message, elapsed'),
    'e': re.compile(rf'{TS}.*Interaction elapsed'),
}

PHONE_PATTERNS = {
    'f': re.compile(rf'{TS}.*Ability onRequest.*HiWearAbility'),
    'g': re.compile(rf'{TS}.*isCarNearby\s*='),
    'h': re.compile(rf'{TS}.*\bdevice\s*='),
    'i': re.compile(rf'{TS}.*send message: code\s*='),
}

WATCH_ORDER = ['a', 'b', 'c', 'd', 'e']
PHONE_ORDER = ['f', 'g', 'h', 'i']


# ── 工具函数 ────────────────────────────────────────────────────────────────
def parse_ts(ts_str: str) -> datetime:
    return datetime.strptime(ts_str, '%m-%d %H:%M:%S.%f')


def ms_diff(t1, t2):
    """返回 t2 - t1 的毫秒数，任一为 None 则返回空字符串。"""
    if t1 is None or t2 is None:
        return ''
    return round((t2 - t1).total_seconds() * 1000, 1)


def read_log_dir(log_dir: str) -> list[str]:
    """读取目录下所有文件，按文件名排序后合并行。"""
    p = Path(log_dir)
    files = sorted(f for f in p.iterdir() if f.is_file())
    if not files:
        print(f"警告：目录 {log_dir} 中未找到任何文件")
        return []
    lines = []
    for f in files:
        try:
            with open(f, encoding='utf-8', errors='ignore') as fh:
                lines.extend(fh.readlines())
        except OSError as e:
            print(f"警告：无法读取 {f}：{e}")
    return lines


# ── 日志解析 ────────────────────────────────────────────────────────────────
def parse_watch_sessions(lines: list[str]) -> list[dict]:
    """
    状态机解析手表日志。
    每遇到 [before] getConnectedDevices 开启新会话；
    遇到 Interaction elapsed 关闭当前会话。
    """
    sessions: list[dict] = []
    cur: dict = {}

    for line in lines:
        for key, pat in WATCH_PATTERNS.items():
            m = pat.search(line)
            if not m:
                continue
            ts = parse_ts(m.group(1))

            if key == 'a':
                # 开启新会话（若有未完成会话则先保存）
                if cur:
                    sessions.append(cur)
                cur = {'a': ts}
            elif cur and key not in cur:
                cur[key] = ts
                if key == 'e':
                    sessions.append(cur)
                    cur = {}
            break  # 每行只匹配一个模式

    if cur:
        sessions.append(cur)

    return sessions


def parse_phone_sessions(lines: list[str]) -> list[dict]:
    """
    状态机解析手机日志。
    每遇到 Ability onRequest.*HiWearAbility 开启新会话；
    遇到 send message 关闭当前会话。
    """
    sessions: list[dict] = []
    cur: dict = {}

    for line in lines:
        for key, pat in PHONE_PATTERNS.items():
            m = pat.search(line)
            if not m:
                continue
            ts = parse_ts(m.group(1))

            if key == 'f':
                if cur:
                    sessions.append(cur)
                cur = {'f': ts}
            elif cur and key not in cur:
                cur[key] = ts
                if key == 'i':
                    sessions.append(cur)
                    cur = {}
            break

    if cur:
        sessions.append(cur)

    return sessions


# ── 时延计算 ────────────────────────────────────────────────────────────────
def calc_latencies(watch_sessions: list[dict], phone_sessions: list[dict]) -> list[dict]:
    n = max(len(watch_sessions), len(phone_sessions))
    rows = []
    for i in range(n):
        ws = watch_sessions[i] if i < len(watch_sessions) else {}
        ps = phone_sessions[i] if i < len(phone_sessions) else {}
        rows.append({
            'getConnectedDevices':        ms_diff(ws.get('a'), ws.get('b')),   # b - a
            'registerMessageReceiver':    ms_diff(ws.get('b'), ws.get('c')),   # c - b
            'receiveMessage':             ms_diff(ws.get('c'), ws.get('d')),   # d - c
            'isCarNearby':                ms_diff(ps.get('f'), ps.get('g')),   # g - f
            'getConnectedDevicesPhone':   ms_diff(ps.get('g'), ps.get('h')),   # h - g
            'sendMessage':                ms_diff(ps.get('h'), ps.get('i')),   # i - h
            'watchTotal':                 ms_diff(ws.get('a'), ws.get('e')),   # e - a
            'phoneTotal':                 ms_diff(ps.get('f'), ps.get('i')),   # i - f
        })
    return rows


# ── Excel 输出 ──────────────────────────────────────────────────────────────
def _cell_style(cell, bold=False, bg=None, align='center', wrap=False):
    cell.font = Font(bold=bold, name='微软雅黑', size=10)
    cell.alignment = Alignment(horizontal=align, vertical='center', wrap_text=wrap)
    if bg:
        cell.fill = PatternFill('solid', fgColor=bg)
    thin = Side(style='thin', color='AAAAAA')
    cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)


def write_excel(rows: list[dict], output_path: Path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'WearEngine时延分析'

    # ── 第 1 行：手表 / 手机 / 总计 大标题 ───────────────────────────────
    #   A      B      C      D      E      F      G      H      I
    #  序号  [──── 手表 ────]      [──── 手机 ────]      [── 总计 ──]
    ws['A1'] = ''
    _cell_style(ws['A1'], bg='F2F2F2')

    ws.merge_cells('B1:D1')
    ws['B1'] = '手表'
    _cell_style(ws['B1'], bold=True, bg='D6E4F0')

    ws.merge_cells('E1:G1')
    ws['E1'] = '手机'
    _cell_style(ws['E1'], bold=True, bg='E2F0D9')

    ws.merge_cells('H1:I1')
    ws['H1'] = '总计'
    _cell_style(ws['H1'], bold=True, bg='FFF2CC')

    # ── 第 2 行：指标列标题 ──────────────────────────────────────────────
    col_defs = [
        ('序号',                            'F2F2F2'),
        ('getConnectedDevices\n(b-a)',       'D6E4F0'),
        ('registerMessageReceiver\n(c-b)',   'D6E4F0'),
        ('receiveMessage\n(d-c)',            'D6E4F0'),
        ('isCarNearby\n(g-f)',               'E2F0D9'),
        ('getConnectedDevices\n(h-g)',       'E2F0D9'),
        ('sendMessage\n(i-h)',               'E2F0D9'),
        ('手表总计\n(e-a)',                  'FFF2CC'),
        ('手机总计\n(i-f)',                  'FFF2CC'),
    ]
    for col_idx, (label, bg) in enumerate(col_defs, 1):
        cell = ws.cell(row=2, column=col_idx, value=label)
        _cell_style(cell, bold=True, bg=bg, wrap=True)

    ws.row_dimensions[1].height = 22
    ws.row_dimensions[2].height = 42

    # ── 第 3 行起：数据 ──────────────────────────────────────────────────
    DATA_KEYS = [
        'getConnectedDevices',
        'registerMessageReceiver',
        'receiveMessage',
        'isCarNearby',
        'getConnectedDevicesPhone',
        'sendMessage',
        'watchTotal',
        'phoneTotal',
    ]
    for row_idx, row_data in enumerate(rows, 3):
        ws.cell(row=row_idx, column=1, value=row_idx - 2).alignment = \
            Alignment(horizontal='center', vertical='center')
        for col_idx, key in enumerate(DATA_KEYS, 2):
            val = row_data.get(key, '')
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.alignment = Alignment(horizontal='center', vertical='center')
        ws.row_dimensions[row_idx].height = 18

    # ── 列宽 ─────────────────────────────────────────────────────────────
    col_widths = [6, 24, 26, 18, 16, 24, 16, 14, 14]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # ── 备注与事件说明 ────────────────────────────────────────────────────
    note_row = len(rows) + 4
    note_style = {'bold': False, 'align': 'left'}

    def note(row, col, text, bold=False):
        cell = ws.cell(row=row, column=col, value=text)
        cell.font = Font(bold=bold, name='微软雅黑', size=9)
        cell.alignment = Alignment(horizontal='left', vertical='center')

    note(note_row,     1, '* 数值单位：毫秒（ms）', bold=True)
    note(note_row + 2, 1, '事件说明：', bold=True)

    descriptions = [
        ('a', '[before] getConnectedDevices',       '手表：开始获取已连接设备列表'),
        ('b', '[after] getConnectedDevices',        '手表：完成获取已连接设备列表'),
        ('c', '[after] registerMessageReceiver',    '手表：完成注册消息接收器'),
        ('d', 'Succeeded in receiving message',     '手表：成功接收到来自手机的消息'),
        ('e', 'Interaction elapsed',                '手表：整体交互完成'),
        ('f', 'Ability onRequest HiWearAbility',    '手机：HiWear 服务收到唤醒请求'),
        ('g', 'isCarNearby =',                      '手机：完成车辆距离判断'),
        ('h', 'device =',                           '手机：获取到目标设备信息'),
        ('i', 'send message: code =',               '手机：向手表发送消息'),
    ]
    for offset, (key, pattern, meaning) in enumerate(descriptions):
        r = note_row + 3 + offset
        note(r, 1, key, bold=True)
        note(r, 2, pattern)
        ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=9)
        note(r, 3, meaning)
        ws.row_dimensions[r].height = 16

    wb.save(output_path)
    print(f"\n✓ 结果已保存：{output_path}")


# ── 主流程 ──────────────────────────────────────────────────────────────────
def main():
    print('=' * 50)
    print('  WearEngine 协同链路时延分析')
    print('=' * 50)

    watch_dir = input('\n请输入手表日志路径（例如: D:/xxx/watch/hilog/）: ').strip()
    if not os.path.isdir(watch_dir):
        print(f'错误：路径不存在或不是目录：{watch_dir}')
        sys.exit(1)

    phone_dir = input('请输入手机日志路径（例如: D:/xxx/phone/hilog/）: ').strip()
    if not os.path.isdir(phone_dir):
        print(f'错误：路径不存在或不是目录：{phone_dir}')
        sys.exit(1)

    print('\n[1/4] 读取手表日志...')
    watch_lines = read_log_dir(watch_dir)
    print(f'      共 {len(watch_lines)} 行')

    print('[2/4] 读取手机日志...')
    phone_lines = read_log_dir(phone_dir)
    print(f'      共 {len(phone_lines)} 行')

    print('[3/4] 解析日志...')
    watch_sessions = parse_watch_sessions(watch_lines)
    phone_sessions = parse_phone_sessions(phone_lines)
    print(f'      手表会话：{len(watch_sessions)} 个')
    print(f'      手机会话：{len(phone_sessions)} 个')

    if not watch_sessions and not phone_sessions:
        print('\n未匹配到任何事件，请确认日志路径和日志格式是否正确。')
        sys.exit(1)

    print('[4/4] 计算时延并写入 Excel...')
    rows = calc_latencies(watch_sessions, phone_sessions)

    output_path = Path(__file__).parent / 'wearengine_latency.xlsx'
    write_excel(rows, output_path)

    # ── 简要统计 ─────────────────────────────────────────────────────────
    print(f'\n共分析 {len(rows)} 组数据，各指标统计（ms）：')
    stat_labels = [
        ('getConnectedDevices（手表）',  'getConnectedDevices'),
        ('registerMessageReceiver',      'registerMessageReceiver'),
        ('receiveMessage',               'receiveMessage'),
        ('isCarNearby',                  'isCarNearby'),
        ('getConnectedDevices（手机）',  'getConnectedDevicesPhone'),
        ('sendMessage',                  'sendMessage'),
        ('手表总计（e-a）',              'watchTotal'),
        ('手机总计（i-f）',              'phoneTotal'),
    ]
    for label, key in stat_labels:
        vals = [r[key] for r in rows if r.get(key) != '']
        if vals:
            print(f'  {label:30s}  avg={sum(vals)/len(vals):7.1f}  '
                  f'min={min(vals):7.1f}  max={max(vals):7.1f}')
        else:
            print(f'  {label:30s}  （无数据）')


if __name__ == '__main__':
    main()
