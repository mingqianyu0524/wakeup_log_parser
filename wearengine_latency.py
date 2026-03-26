#!/usr/bin/env python3
"""
WearEngine 协同链路时延分析脚本

解析手表和手机的 HiLog 日志，计算各阶段时延并输出到 Excel。

事件序列（手表日志）：
  a  [before] getConnectedDevices
  b  [after]  getConnectedDevices
  c  [after]  registerMessageReceiver
  d  WatchWearLink sendData enter serviceId
  e  br watch->phone length 106 : 5a00650034017c
  l  Succeeded in receiving message

事件序列（手机日志）：
  f  WearServiceLP len 106 [BrRecv] 5a00650034017c
  g  notifyObservers enter, the data is: 0x01
  h  Ability onRequest HiWearAbility
  i  isCarNearby =
  j  device =
  k  send message: code =
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

# ── 正则模式 ─────────────────────────────────────────────────────────────────
TS = r'(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})'

WATCH_PATTERNS = {
    'a': re.compile(rf'{TS}.*\[before\] getConnectedDevices'),
    'b': re.compile(rf'{TS}.*\[after\] getConnectedDevices'),
    'c': re.compile(rf'{TS}.*\[after\] registerMessageReceiver'),
    'd': re.compile(rf'{TS}.*WatchWearLink.*sendData enter serviceId'),
    'e': re.compile(rf'{TS}.*br watch->phone length 106 : 5a00650034017c'),
    'l': re.compile(rf'{TS}.*Succeeded in receiving message'),
}

PHONE_PATTERNS = {
    'f': re.compile(rf'{TS}.*WearServiceLP.*len 106.*\[BrRecv\].*5a00650034017c'),
    'g': re.compile(rf'{TS}.*notifyObservers enter, the data is: 0x01'),
    'h': re.compile(rf'{TS}.*Ability onRequest.*HiWearAbility'),
    'i': re.compile(rf'{TS}.*isCarNearby\s*='),
    'j': re.compile(rf'{TS}.*\bdevice\s*='),
    'k': re.compile(rf'{TS}.*send message: code\s*='),
}

# 事件的前驱（强制有序：每个事件只在其前驱出现后才记录）
WATCH_PREREQ = {'b': 'a', 'c': 'b', 'd': 'c', 'e': 'd', 'l': 'e'}
PHONE_PREREQ  = {'g': 'f', 'h': 'g', 'i': 'h', 'j': 'i', 'k': 'j'}


# ── 工具函数 ──────────────────────────────────────────────────────────────────
def parse_ts(ts_str: str) -> datetime:
    return datetime.strptime(ts_str, '%m-%d %H:%M:%S.%f')


def ms_diff(t1, t2):
    """返回 t2 - t1 的毫秒数；任一为 None 则返回空字符串。"""
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


# ── 日志解析 ──────────────────────────────────────────────────────────────────
def _parse_sessions(lines: list[str], patterns: dict, prereq: dict,
                    start_key: str, end_key: str) -> list[dict]:
    """
    通用状态机解析器。
    - start_key 触发新会话开始；
    - end_key 触发当前会话关闭；
    - prereq 字典保证事件严格有序（只有前驱已记录才接受当前事件）。
    """
    sessions: list[dict] = []
    cur: dict = {}

    for line in lines:
        for key, pat in patterns.items():
            m = pat.search(line)
            if not m:
                continue
            ts = parse_ts(m.group(1))

            if key == start_key:
                # 开启新会话（若有未完成会话则先保存）
                if cur:
                    sessions.append(cur)
                cur = {start_key: ts}
            elif cur and key not in cur:
                # 强制有序：只在前驱事件已记录时才接受
                if prereq.get(key) in cur:
                    cur[key] = ts
                    if key == end_key:
                        sessions.append(cur)
                        cur = {}
            break  # 每行只匹配一个模式

    if cur:
        sessions.append(cur)

    return sessions


def parse_watch_sessions(lines: list[str]) -> list[dict]:
    return _parse_sessions(lines, WATCH_PATTERNS, WATCH_PREREQ,
                           start_key='a', end_key='l')


def parse_phone_sessions(lines: list[str]) -> list[dict]:
    return _parse_sessions(lines, PHONE_PATTERNS, PHONE_PREREQ,
                           start_key='f', end_key='k')


# ── 时延计算 ──────────────────────────────────────────────────────────────────
def calc_latencies(watch_sessions: list[dict], phone_sessions: list[dict]) -> list[dict]:
    n = max(len(watch_sessions), len(phone_sessions))
    rows = []
    for i in range(n):
        w = watch_sessions[i] if i < len(watch_sessions) else {}
        p = phone_sessions[i] if i < len(phone_sessions) else {}

        total   = ms_diff(w.get('a'), w.get('l'))          # l - a
        phone_t = ms_diff(p.get('f'), p.get('j'))          # j - f
        p2w     = ms_diff(p.get('j'), p.get('k'))          # k - j  (phone->watch传输)

        # w2p = (l-e) - (k-f)；手表总计 = e - a
        le      = ms_diff(w.get('e'), w.get('l'))          # l - e
        kf      = ms_diff(p.get('f'), p.get('k'))          # k - f
        w2p        = (le - kf)               if (le != '' and kf != '') else ''
        watch_only = ms_diff(w.get('a'), w.get('e'))       # e - a

        rows.append({
            'gcd_watch':   ms_diff(w.get('a'), w.get('b')),  # b - a
            'rmr':         ms_diff(w.get('b'), w.get('c')),  # c - b
            'app2we':      ms_diff(w.get('c'), w.get('d')),  # d - c  startRemoteApp->wearEngine
            'we2sa':       ms_diff(w.get('d'), w.get('e')),  # e - d  wearEngine->SA
            'sa2we':       ms_diff(p.get('f'), p.get('g')),  # g - f  SA->wearEngine
            'we2ab':       ms_diff(p.get('g'), p.get('h')),  # h - g  wearEngine->aibase
            'isCarNearby': ms_diff(p.get('h'), p.get('i')),  # i - h
            'gcd_phone':   ms_diff(p.get('i'), p.get('j')),  # j - i
            'sendMsg':     ms_diff(p.get('j'), p.get('k')),  # k - j
            'watch_only':  watch_only,                        # e - a
            'phone_total': phone_t,                           # j - f
            'w2p':         w2p,                               # (l-e)-(k-f)
            'p2w':         p2w,                               # k - j
            'total':       total,                             # l - a
        })
    return rows


# ── Excel 输出 ────────────────────────────────────────────────────────────────
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
    #  A     B    C    D    E     F    G    H    I    J     K    L    M    N    O
    # 序号 [─── 手表(B-E) ───]  [───────── 手机(F-J) ─────────]  [────── 总计(K-O) ──────]
    ws['A1'] = ''
    _cell_style(ws['A1'], bg='F2F2F2')

    ws.merge_cells('B1:E1')
    ws['B1'] = '手表'
    _cell_style(ws['B1'], bold=True, bg='D6E4F0')

    ws.merge_cells('F1:J1')
    ws['F1'] = '手机'
    _cell_style(ws['F1'], bold=True, bg='E2F0D9')

    ws.merge_cells('K1:O1')
    ws['K1'] = '总计'
    _cell_style(ws['K1'], bold=True, bg='FFF2CC')

    # ── 第 2 行：指标列标题 ──────────────────────────────────────────────
    col_defs = [
        # (标题,                             背景色)
        ('序号',                             'F2F2F2'),   # A
        ('getConnectedDevices\n(b-a)',        'D6E4F0'),   # B
        ('registerMessageReceiver\n(c-b)',    'D6E4F0'),   # C
        ('startRemoteApp\n->wearEngine(d-c)', 'D6E4F0'),   # D
        ('wearEngine->SA\n(e-d)',             'D6E4F0'),   # E
        ('SA->wearEngine\n(g-f)',             'E2F0D9'),   # F
        ('wearEngine->aibase\n(h-g)',         'E2F0D9'),   # G
        ('isCarNearby\n(i-h)',                'E2F0D9'),   # H
        ('getConnectedDevices\n(j-i)',        'E2F0D9'),   # I
        ('sendMessage\n(k-j)',                'E2F0D9'),   # J
        ('手表总计\n(e-a)',                    'FFF2CC'),   # K
        ('手机总计\n(j-f)',                   'FFF2CC'),   # L
        ('w2p传输时延\n(l-e)-(k-f)',          'FFF2CC'),   # M
        ('p2w传输时延\n(k-j)',               'FFF2CC'),   # N
        ('total\n(l-a)',                      'FFF2CC'),   # O
    ]
    for col_idx, (label, bg) in enumerate(col_defs, 1):
        cell = ws.cell(row=2, column=col_idx, value=label)
        _cell_style(cell, bold=True, bg=bg, wrap=True)

    ws.row_dimensions[1].height = 22
    ws.row_dimensions[2].height = 50

    # ── 第 3 行起：数据 ──────────────────────────────────────────────────
    DATA_KEYS = [
        'gcd_watch', 'rmr', 'app2we', 'we2sa',          # B-E 手表
        'sa2we', 'we2ab', 'isCarNearby', 'gcd_phone', 'sendMsg',  # F-J 手机
        'watch_only', 'phone_total', 'w2p', 'p2w', 'total',       # K-O 总计
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
    col_widths = [6, 22, 24, 22, 18, 18, 20, 16, 22, 16, 18, 14, 18, 16, 12]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # ── 备注与事件说明 ────────────────────────────────────────────────────
    note_row = len(rows) + 4

    def note(row, col, text, bold=False):
        cell = ws.cell(row=row, column=col, value=text)
        cell.font = Font(bold=bold, name='微软雅黑', size=9)
        cell.alignment = Alignment(horizontal='left', vertical='center')

    note(note_row,     1, '* 数值单位：毫秒（ms）', bold=True)
    note(note_row + 1, 1, '* w2p（watch→phone传输时延）= (l-e) - (k-f)　　'
                          'p2w（phone→watch传输时延）= k - j', bold=False)
    note(note_row + 2, 1, '* 手表总计 = e - a（手表发送数据前的处理时延）', bold=False)

    note(note_row + 4, 1, '事件说明：', bold=True)

    descriptions = [
        # (标识, 日志关键字,                                   含义)
        ('a', '[before] getConnectedDevices',                  '手表：开始获取已连接设备列表'),
        ('b', '[after]  getConnectedDevices',                  '手表：完成获取已连接设备列表'),
        ('c', '[after]  registerMessageReceiver',              '手表：完成注册消息接收器'),
        ('d', 'WatchWearLink：sendData enter serviceId',       '手表：wearEngine 发送通知至 SA'),
        ('e', 'br watch->phone length 106 : 5a00650034017c01','手表：SA 发送数据帧至手机'),
        ('f', 'WearServiceLP len 106 [BrRecv] 5a00650034017c','手机：SA 接收到来自手表的数据帧'),
        ('g', 'notifyObservers enter, the data is: 0x01',      '手机：wearEngine 接收到通知'),
        ('h', 'Ability onRequest HiWearAbility',               '手机：aibase 收到唤醒请求'),
        ('i', 'isCarNearby =',                                 '手机：完成车辆距离判断'),
        ('j', 'device =',                                      '手机：获取到目标设备信息'),
        ('k', 'send message: code =',                          '手机：向手表发送响应消息'),
        ('l', 'Succeeded in receiving message',                '手表：成功接收到来自手机的消息'),
    ]
    for offset, (key, pattern, meaning) in enumerate(descriptions):
        r = note_row + 5 + offset
        note(r, 1, key, bold=True)
        note(r, 2, pattern)
        ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=15)
        note(r, 3, meaning)
        ws.row_dimensions[r].height = 16

    wb.save(output_path)
    print(f"\n✓ 结果已保存：{output_path}")


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main():
    print('=' * 55)
    print('  WearEngine 协同链路时延分析')
    print('=' * 55)

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

    # ── 简要统计 ──────────────────────────────────────────────────────────
    print(f'\n共分析 {len(rows)} 组数据，各指标统计（ms）：')
    stat_labels = [
        ('getConnectedDevices（手表）',   'gcd_watch'),
        ('registerMessageReceiver',       'rmr'),
        ('startRemoteApp->wearEngine',    'app2we'),
        ('wearEngine->SA',                'we2sa'),
        ('SA->wearEngine',                'sa2we'),
        ('wearEngine->aibase',            'we2ab'),
        ('isCarNearby',                   'isCarNearby'),
        ('getConnectedDevices（手机）',   'gcd_phone'),
        ('sendMessage',                   'sendMsg'),
        ('手表总计（e-a）',               'watch_only'),
        ('手机总计（j-f）',               'phone_total'),
        ('w2p传输时延（(l-e)-(k-f)）',   'w2p'),
        ('p2w传输时延',                   'p2w'),
        ('total（l-a）',                  'total'),
    ]
    for label, key in stat_labels:
        vals = [r[key] for r in rows if r.get(key) != '']
        if vals:
            print(f'  {label:28s}  avg={sum(vals)/len(vals):7.1f}  '
                  f'min={min(vals):7.1f}  max={max(vals):7.1f}')
        else:
            print(f'  {label:28s}  （无数据）')


if __name__ == '__main__':
    main()
