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
- `parse_device_logs(device_hilog_dir: Path, progress_cb=None) -> list[dict]` — 返回 O1 列表；`progress_cb(current, total, filename)` 在每个文件开始前调用
- `align_sessions(all_results: dict[str, list[dict]]) -> list[dict]` — 返回 AlignedSession 列表
- `_DEVICE_TYPE_NAMES: dict[int, str]` — `{40:"平板", 50:"手机", 110:"手表", 240:"车机"}`
- `is_log_file(path: Path) -> bool` — 判断文件是否为日志文件（`hiapplogcat-log*` 或 `hilog*.txt`）

### `wakeup_ui.py`

NiceGUI UI，端口 8080，单页应用。

**全局辅助函数（模块级，在 `index()` 外）：**

| 函数 | 说明 |
|------|------|
| `_render_broadcast_detail(bc)` | 渲染一条广播的解析结果（类型/设备/时间戳/UDID/声强/置信度/抑制） |
| `_render_event_card(idx, o1)` | 渲染解析视图中一条唤醒事件展开卡（T1/T1a/T2/T2a/T3 + 广播） |
| `_kv(label, value)` | 渲染 key-value 行，值为 None 时显示 "—" |
| `_classify_session(entries)` | 判断唤醒响应模式：normal / double(双响) / wrong(错响) |
| `_build_dev_info(sessions)` | 从 sessions 数据构建 `{dev_name: (dtype_name, udid_csv)}` 映射（udid 为 "205,181,212,173" 形式） |
| `_render_aligned_device_row(dev_name, entry, dev_info)` | 渲染对齐视图中单台设备的展开行 |
| `_render_session_list(sessions, all_device_names, dev_info, ...)` | 渲染过滤后的 session 列表（纯渲染，无状态），由 `_render_aligned_sessions` 的 `_refresh()` 调用 |
| `_render_aligned_sessions(sessions, all_device_names)` | 渲染完整对齐视图：过滤栏 + 响应式 `sess_col` 容器 |

**`index()` 内部结构：**
1. 设备路径输入行（`add_device_row`）
2. "开始解析"按钮 → `on_parse()` — 后台线程 `parse_device_logs`，结果存入 `_state["all_results"]`；带进度条 UI
3. 解析结果区：每台设备一个 `ui.expansion`（标题 `📱 [{deviceTypeName}]  UDID:{csv}  (N次唤醒)`，**不含目录名**；csv 为 `205,181,212,173` 形式，即广播 raw 第 6-9 字节），展开后每条唤醒事件调用 `_render_event_card`
4. "对齐分析"按钮 → `on_align()` — 调用 `align_sessions`，结果传入 `_render_aligned_sessions`

**解析进度条实现（`on_parse()` 内）：**
```python
# 预计算各设备文件数
file_counts = {path: sum(1 for f in Path(path).iterdir() if is_log_file(f)) ...}

# 共享进度状态（GIL 保证线程安全的简单赋值）
_prog = {"current": 0, "total": 0, "file": ""}

def _cb(c, t, fname):
    _prog["current"] = c
    _prog["total"]   = t
    _prog["file"]    = fname

# UI 定时器（0.15s）读取 _prog 更新进度条
def _tick():
    prog_bar.value = _prog["current"] / _prog["total"] if _prog["total"] > 0 else 0.0
    prog_files.set_text(f"{_prog['current']}/{_prog['total']}  {_prog['file']}")

progress_timer = ui.timer(0.15, _tick, active=True)

# 每个设备顺序 await
for hilog_dir, path_str in device_paths:
    await run.io_bound(parse_device_logs, hilog_dir, _cb)

progress_timer.cancel()
```

**device_name 推导（`on_parse()` 内）：**

从用户输入路径向上遍历，跳过通用中间目录名 `{"hilog", "remoteLog", "log", "logs"}`，取第一个有意义的目录名作为设备键。例：

```
.../RemoteLog_commercial_DEV-A/.../remoteLog/hilog  →  "RemoteLog_commercial_DEV-A"
.../phone/hilog                                     →  "phone"
```

防止多台设备日志路径都以 `remoteLog/hilog` 结尾时产生键碰撞（第二台覆盖第一台）。

**对齐视图过滤器（`_render_aligned_sessions()` 内）：**
- `t_start` / `t_end`：`ui.input`，格式 `MM-DD HH:MM`（精确到分钟）
- `cb_normal` / `cb_double` / `cb_wrong`：`ui.checkbox`
- "应用"按钮触发 `_refresh()`；checkbox 变化也触发 `_refresh()`
- `_matches(sess)` 过滤逻辑：
  - `anchor_time[:11]`（取 `"MM-DD HH:MM"`）与 `t_start` / `t_end` 字符串比较（空则跳过）
  - 按 `_classify_session(entries)[0]`（status: "normal"/"double"/"wrong"）检查 checkbox
- `_refresh()` = `sess_col.clear()` + 在 `sess_col` 内调用 `_render_session_list(filtered_sessions, ...)`

**bug fix — 响应结果行 `—·未知`：**
- 根因：`_build_dev_info` 从第一个 session 读取 dtype/UDID；若那个 session 的广播数据缺失，缓存值为 `("—","未知")`，后续 session 的真实数据被掩盖。
- 修复：在 `_render_session_list` 中，当 `dentry is not None`，直接读取 `ev`（当前 session 的事件对象）：
  ```python
  dtype    = ev.get("deviceTypeName") or "—"
  udid_raw = ev.get("DeviceUdid")
  udid     = ",".join(str(b) for b in udid_raw) if udid_raw else "未知"  # csv 字节形式
  ```
  缺席设备仍使用 `dev_info` 缓存。

**置信度常量（`_CONF_BORDER`, `_CONF_ICON`）：**
- green → 🟢 / `border-green-400`
- yellow → 🟡 / `border-yellow-400`
- red → 🔴 / `border-red-400`
- isolated → 🔵 / `border-gray-300`

**设备响应优先级（`_DEVICE_PRIORITY`）：**
```python
{240:10, 230:9, 220:8, 210:7, 150:6, 110:5, 80:4, 50:3, 40:2, 30:1}
# 车机 > 移动智慧屏 > OMELCD > 音箱 > 大屏 > 手表 > 耳机 > 手机 > 平板 > PC
```

**`_classify_session` 判断规则：**
- 0 响应 → normal
- 1 响应 + 最高优先级设备 → normal
- 1 响应 + 非最高优先级设备 → **错响**（黄）
- 2+ 响应 + 最高优先级设备在内 → **双响**（红）
- 2+ 响应 + 最高优先级设备不在内 → **错响**（黄）

---

## 核心数据结构

### O1 对象

```python
{
    "deviceType":         int | None,   # 40=平板 50=手机 110=手表 240=车机
    "deviceTypeName":     str | None,   # "平板"/"手机"/"手表"/"车机"
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
    "deviceType":       int,   # 40/50/110/240
    "deviceTypeName":   str,   # "平板"/"手机"/"手表"/"车机"
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
            "not_received_from": list,  # [{"device": str, "udid": str, "missing_bcasts": list[str]}]
            # udid 采用 CSV 字节形式：'205,181,212,173'（= 广播 raw 第 6-9 字节），由 _udid_csv() 生成
            # missing_bcasts: ["T1a"] / ["T2a"] / ["T1a","T2a"] — 对方发了但本机未收到的广播
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
| [1]   | 设备类型：40=平板 50=手机 110=手表 240=车机   |
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

# Android 发送：getEncryptData:: ... dec data = [-24, 81, ...]
_RE_ANDROID_SEND  = re.compile(r"getEncryptData::.*?dec data = \[([-\d,\s]+)\]")
# Android 接收（主）：parseResponse version 1::[0, 50, 105, -82, ...]
_RE_ANDROID_RECV  = re.compile(r"parseResponse.*?version\s+\d+::\[([-\d,\s]+)\]")
# Android 接收（备）：CommonUtil: onAwareResult data:[0, 50, 105, -71, ...]
_RE_ANDROID_RECV2 = re.compile(r"onAwareResult\s+data:\[([-\d,\s]+)\]")
# 鸿蒙发送 T1a（预唤醒）：buildBytes: {"0":1,"1":50,...}
_RE_HMOS_SEND     = re.compile(r"buildBytes:\s*(\{[^}]+\})")
# 鸿蒙发送 T2a（唤醒）：sendMsgs = 0,50,105,169,...
_RE_HMOS_SEND2    = re.compile(r"\bsendMsgs\s*=\s*([\d,\s]+)")
# 鸿蒙接收（唤醒/预唤醒 type 0/1）：judgeDeviceInList,deviceData:DeviceData{...mOriginData=0,240,...}
# 必须带 "judgeDeviceInList,deviceData" 关键字；裸 "mOriginData=" 会误匹配大量无关日志。
_RE_HMOS_RECV      = re.compile(r"judgeDeviceInList,deviceData:DeviceData\{[^}]*mOriginData=([\d,]+)")
# 鸿蒙接收（唤醒失败 type 2）：onAwareResult::deviceData:DeviceData{...mOriginData=2,...}
# 同时要求首字节为 "2,"，因为 "onAwareResult::deviceData" 关键字匹配到的条目非常多。
_RE_HMOS_RECV_FAIL = re.compile(r"onAwareResult::deviceData:DeviceData\{[^}]*mOriginData=(2,[\d,]+)")
```

Android 接收两种格式均由同一 handler 处理：`_RE_ANDROID_RECV.search(line) or _RE_ANDROID_RECV2.search(line)`。  
Android 使用 int8（有负值），经 `parse_bracketed_int8()` 转换为 uint8；鸿蒙原始即 uint8，使用 `parse_csv_uint8()`。

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

每台设备一个 `ui.expansion`，标题格式（**不含目录名**）：
```
📱  [{deviceTypeName}]  UDID:{csv}  (N次唤醒)   # csv = "205,181,212,173"
```

展开后每条唤醒事件调用 `_render_event_card`，左右两列：
- 左：时间线（T1/T1a/T2/T2a/T3/决策）
- 右：发送广播详情（L1BroadcastData、L2BroadcastData）
- 底：收到广播列表（ReceivedBroadcasts）

### 对齐视图

顶部**过滤栏**：
- 时间段：`开始 MM-DD HH:MM` / `结束 MM-DD HH:MM` 输入框 + "应用"按钮
- 类型复选框：`正常` / `双响` / `错响`（默认全选）
- 响应式 `sess_col` 容器，按过滤条件实时更新

按会话（session）列出 `ui.expansion`（**自定义 header slot**），标题：
```
唤醒 #{session_id}  {anchor_time}  (N台设备)  [双响/错响]
```
session 展开后：
1. **响应结果摘要行**：`{dtype}·{udid}  响应/不响应` 徽章（绿/灰），所有参与设备并排显示（**直接读自当前 session 的 event，不使用 dev_info 缓存**）
2. **设备行**（每台一个可展开 `_render_aligned_device_row`）

设备行 header（**自定义 header slot**）：
```
{置信度图标} {deviceTypeName}  UDID:{csv}    [响应/不响应]   # csv = "205,181,212,173"
```

设备行展开内容（三段）：
- **时间栏**：T1/T1a/T2/T2a/T3，仅显示时间字符串
- **广播栏**：
  - `↑T1a` / `↑T2a`：本机发送广播 raw bytes（蓝色）
  - `↓收`：**遍历 `ReceivedBroadcasts` 整表**（不再只显示与 session peer UDID 匹配的条目）
    - type 0/1（唤醒/预唤醒）绿底
    - type 2（唤醒失败）红底
    - 每条显示 `[deviceType] UDID:xx · {typeName}` + raw bytes + 解析按钮
    - 会话里已识别的对端用 `dev_info` 标注设备类型，未识别的直接用解码出的 `deviceTypeName`
  - `✗`：会话对端（`no_recv`）发了但本机完全未收到的广播（黄底）；若该对端的 UDID 已经出现在 `↓收` 列表里（比如收到了 T2a 但没收到 T1a），跳过以避免自相矛盾
- **解析广播**按钮（懒加载，点击后展开 `_render_broadcast_detail`）

缺席设备显示为静态灰色行（`{dtype}  UDID:{udid}  缺席`，**不含目录名**）。

---

## 已知边界情况

- 设备时钟不同步：时间偏移算法依赖至少 3 条 Phase 1 匹配对来计算可靠的 median/IQR；样本不足时退化为 `off=0, iqr=0`，回退窗口为 2000ms。
- BLE 广播重复：同一内容每 ~20ms 广播一次，`ReceivedBroadcasts` 按 raw 字节去重，仅保留首次收到。
- 跨文件事件：`_scan_file` 通过 `carry` 参数将未完成的 pending 事件传递到下一个文件；文件序列结束后若仍有 carry，直接追加为最后一条记录。
- T2 重复打印：同一 `===start::` 时间戳在相邻文件重复出现时自动去重（文件内：`pending["t2"] == ts` 则跳过；跨文件：`wakeup_events[-1]["t2"] == ts` 则跳过），避免产生无 T3 的残缺记录。
- Phase 1 双广播匹配：对每个事件同时检查 L1BroadcastData（T1a）和 L2BroadcastData（T2a）的 raw 字节，任意一个在接收索引中命中即合并（`for sent_raw in filter(None, [l2.get("raw"), l1.get("raw")])`）。
- 广播丢失（没有任何设备收到）：Phase 1 无法匹配，依赖 Phase 2 时间估算。

---

## 依赖

```
nicegui>=1.4.0
openpyxl>=3.1.0
```

运行：`python wakeup_ui.py`，访问 http://localhost:8080
