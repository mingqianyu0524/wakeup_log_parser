# CLAUDE.md — 项目上下文

本文件为 Claude Code 会话提供完整上下文，确保跨会话断点续接。

---

## 项目定位

解析多台设备（Android + 鸿蒙）的 hilog 日志，提取 BLE 唤醒协同事件，生成结构化 O1 对象，并在 NiceGUI Web UI 中可视化，支持跨设备唤醒记录对齐。

---

## 文件职责

### `broadcast_parser.py`

项目核心，无 UI 依赖，三大模块：

1. **日志扫描** (`_scan_file`, `parse_device_logs`)
   - 逐行扫描日志文件，识别 T1/T2/T3 事件和广播
   - 支持跨文件事件续接（`carry` 参数）
   - 返回完整的 wakeup_events、sent_bcast、recv_bcast 列表

2. **广播解析** (`decode_broadcast`, `parse_bracketed_int8`, `parse_csv_uint8`, `parse_json_bytes`)
   - Android int8 → uint8 转换：负值 +256
   - 4 种日志格式：Android 发送/接收、鸿蒙发送/接收
   - 广播格式详见下文

3. **对齐算法** (`align_sessions`)
   - Phase 1：广播 raw 字节精确匹配（Union-Find）
   - Phase 2：median + IQR 时间偏移回退
   - Phase 3：构建 AlignedSession，worst-link 置信度

公开 API：
- `parse_device_logs(device_hilog_dir: Path) -> list[dict]` — 返回 O1 列表
- `align_sessions(all_results: dict[str, list[dict]]) -> list[dict]` — 返回 AlignedSession 列表
- `_DEVICE_TYPE_NAMES: dict[int, str]` — `{50:"手机", 110:"手表", 240:"车机"}`

### `wakeup_ui.py`

NiceGUI UI，端口 8080，单页应用。

**全局辅助函数（模块级，在 `index()` 外）：**

| 函数 | 说明 |
|------|------|
| `_render_broadcast_detail(bc)` | 渲染一条广播的解析结果（类型/设备/时间戳/UDID/声强/置信度/抑制） |
| `_render_event_card(idx, o1)` | 渲染解析视图中一条唤醒事件展开卡（T1/T1a/T2/T2a/T3 + 广播） |
| `_kv(label, value)` | 渲染 key-value 行，值为 None 时显示 "—" |
| `_render_aligned_device_row(dev_name, entry)` | 渲染对齐视图中单台设备的展开行（含 T1/T1a/T2/T2a/T3 详情） |
| `_render_aligned_sessions(sessions, all_device_names)` | 渲染完整对齐视图（expansion 列表） |

**`index()` 内部结构：**
1. 设备路径输入行（`add_device_row`）
2. "开始解析"按钮 → `on_parse()` — 后台线程 `parse_device_logs`，结果存入 `_state["all_results"]`
3. 解析结果区：每台设备一个 `ui.expansion`，展开后每条唤醒事件调用 `_render_event_card`
4. "对齐分析"按钮 → `on_align()` — 调用 `align_sessions`，结果传入 `_render_aligned_sessions`

**置信度常量（`_CONF_BORDER`, `_CONF_ICON`）：**
- green → 🟢 / `border-green-400`
- yellow → 🟡 / `border-yellow-400`
- red → 🔴 / `border-red-400`
- isolated → 🔵 / `border-gray-300`

---

## 核心数据结构

### O1 对象

```python
{
    "deviceType":         int | None,   # 50=手机 110=手表 240=车机
    "deviceTypeName":     str | None,   # "手机"/"手表"/"车机"
    "DeviceUdid":         list[int] | None,  # 4字节 UDID
    "L1WakeupTime":       str | None,   # "MM-DD HH:MM:SS.mmm" T1
    "L1BroadcastTime":    str | None,   # T1a
    "L1BroadcastData":    dict | None,  # decode_broadcast() 返回值
    "L2WakeupTime":       str | None,   # T2
    "L2BroadcastTime":    str | None,   # T2a
    "L2BroadcastData":    dict | None,  # decode_broadcast() 返回值
    "ReceivedBroadcasts": list[dict],   # [{Broadcast, ReceiveTime, Decoded}]
    "DecisionTime":       str | None,   # T3
    "Decision":           str | None,   # "true"/"false"
}
```

### decode_broadcast() 返回值

```python
{
    "type":             int,   # 0/1/2
    "typeName":         str,   # "唤醒广播"/"预唤醒广播"/"唤醒失败广播"
    "deviceType":       int,   # 50/110/240
    "deviceTypeName":   str,   # "手机"/"手表"/"车机"
    "timestamp_bytes":  list,  # b[2:6] 原始字节
    "timestamp_unix":   int,   # big-endian uint32 Unix 秒
    "timestamp_human":  str,   # "2026年03月11日17时28分30秒"
    "udid":             list,  # b[6:10]
    "voiceEnergy":      int,
    "voiceConfidence":  int,
    "suppressed":       int,   # 0=不抑制 63=抑制
    "raw":              str,   # "0,240,105,174,..."
}
```

### AlignedSession

```python
{
    "session_id":  int,
    "anchor_time": str,   # "MM-DD HH:MM:SS.mmm" 最早 T1/T2
    "entries": {
        "<device_name>": {
            "event":             dict,  # O1 对象
            "confidence":        str,   # "green"/"yellow"/"red"/"isolated"
            "received_from":     list,  # [{"device": str, "udid": str}]
            "not_received_from": list,  # [{"device": str, "udid": str}]
        }
    }
    # 缺席设备：entries 中没有该 key
}
```

---

## 广播格式（15 字节，uint8）

| 索引  | 含义                                          |
|-------|-----------------------------------------------|
| [0]   | 广播类型：0=唤醒 1=预唤醒 2=唤醒失败          |
| [1]   | 设备类型：50=手机 110=手表 240=车机           |
| [2-5] | Unix 时间戳，big-endian uint32，秒            |
| [6-9] | 设备 UDID                                     |
| [10]  | 声强（预唤醒广播为 0）                         |
| [11]  | 声纹置信度                                     |
| [12]  | 抑制标志：0=不抑制 63=抑制                    |
| [13-14]| 保留                                         |

**Android int8 → uint8**：负值 +256，由 `_int8_to_uint8()` 处理。

---

## 日志匹配正则

```python
_RE_T1        = re.compile(r"===de")
_RE_T2        = re.compile(r"===start::")
_RE_T3        = re.compile(r"onResult::\s*isShouldResponse")
_RE_DECISION  = re.compile(r"isShouldResponse\s*=\s*(true|false)", re.IGNORECASE)

_RE_ANDROID_SEND = re.compile(r"getEncryptData::.*?dec data = \[([-\d,\s]+)\]")
_RE_ANDROID_RECV = re.compile(r"parseResponse.*?version\s+\d+::\[([-\d,\s]+)\]")
_RE_HMOS_SEND    = re.compile(r"buildBytes:\s*(\{[^}]+\})")
_RE_HMOS_RECV    = re.compile(r"mOriginData=([\d,]+)")
```

---

## 唤醒时间窗口

```
anchor_ms = T1_ms if T1 else T2_ms
T4_ms     = anchor_ms + 2000  (唤醒窗口结束)

T1a 时间窗：[anchor_ms, T2_ms)  — 发送预唤醒广播，type==1
T2a 时间窗：[T2_ms, T3_ms+1000) — 发送唤醒广播，type==0
T2b 时间窗：[anchor_ms, T4_ms]  — 收到广播（去重）
```

车机/手表没有预唤醒，T1 == T2，L1BroadcastData 为 None。

---

## UI 结构说明

### 解析视图

每台设备一个 `ui.expansion`，标题格式：
```
📱 {device_dir_name}  [{deviceTypeName}]  UDID:{hex}  (N次唤醒)
```

展开后每条唤醒事件调用 `_render_event_card`，左右两列：
- 左：时间线（T1/T1a/T2/T2a/T3/决策）
- 右：发送广播详情（L1BroadcastData、L2BroadcastData）
- 底：收到广播列表（ReceivedBroadcasts）

### 对齐视图

按会话（session）列出 `ui.expansion`，标题：
```
唤醒 #{session_id}  {anchor_time}  (N台设备)
```

展开后每台设备一行（`_render_aligned_device_row`），标题格式：
```
{置信度图标} {deviceTypeName}  UDID:{hex}  ({device_dir_name})
```

展开后显示：T1 → T1a+广播解析 → T2 → T2a+广播解析 → T3+决策 → 已收到/未收到广播注释。

缺席设备显示为静态灰色行（不可展开）。

---

## 已知边界情况

- 设备时钟不同步：时间偏移算法依赖至少 3 条 Phase 1 匹配对来计算可靠的 median/IQR；样本不足时退化为 `off=0, iqr=0`，回退窗口为 2000ms。
- BLE 广播重复：同一内容每 ~20ms 广播一次，`ReceivedBroadcasts` 按 raw 字节去重，仅保留首次收到。
- 跨文件事件：`_scan_file` 通过 `carry` 参数将未完成的 pending 事件传递到下一个文件。
- 广播丢失（没有任何设备收到）：Phase 1 无法匹配，依赖 Phase 2 时间估算。

---

## 依赖

```
nicegui>=1.4.0
openpyxl>=3.1.0
```

运行：`python wakeup_ui.py`，访问 http://localhost:8080
