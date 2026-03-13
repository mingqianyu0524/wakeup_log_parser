# 唤醒日志分析工具

跨设备 BLE 唤醒协同日志的解析与可视化工具，支持 Android / 鸿蒙设备日志。

## 功能概览

- 多设备日志同时解析
- 按唤醒事件结构化输出 O1 对象（T1/T1a/T2/T2a/T3 + 广播内容）
- 广播数据完整解析（类型、设备类型、时间戳、UDID、声强、声纹置信度、抑制标志）
- 跨设备唤醒对齐：广播精确匹配 + 时间偏移回退（median + IQR）
- NiceGUI Web UI，PC 浏览器访问

## 安装

```bash
pip install -r requirements.txt
```

## 启动 UI

```bash
python wakeup_ui.py
```

浏览器访问 http://localhost:8080

## 命令行测试（无 UI）

在项目根目录放置如下结构的日志目录：

```
logs/
  deviceA/hilog/hiapplogcat-log.0.txt
  deviceB/hilog/hilog.01.txt
```

```bash
python broadcast_parser.py
```

## 日志路径规则

UI 中填写每台设备的 `hilog/` 目录路径，支持：

- `hiapplogcat-log*`
- `hilog*.txt`

设备名取自目录名（`hilog` 上一级目录名）。

## 日志文件格式要求

工具通过正则匹配以下关键行识别唤醒事件与广播：

| 事件     | 匹配关键字                          |
|----------|-------------------------------------|
| 一级唤醒 T1 | `===de`                          |
| 二级唤醒 T2 | `===start::`                     |
| 决策 T3  | `onResult:: isShouldResponse`       |
| Android 发送广播 | `getEncryptData::...dec data = [...]` |
| Android 接收广播 | `parseResponse version N::[...]` |
| 鸿蒙 发送广播 | `buildBytes: {"0":...}`          |
| 鸿蒙 接收广播 | `mOriginData=0,240,...`          |

## 广播数据格式（15 字节，uint8）

| 字节      | 含义                                                 |
|-----------|------------------------------------------------------|
| [0]       | 广播类型：`0`=唤醒广播 `1`=预唤醒广播 `2`=唤醒失败 |
| [1]       | 设备类型：`50`=手机 `110`=手表 `240`=车机           |
| [2-5]     | unix 时间戳（big-endian uint32，秒）                 |
| [6-9]     | 设备 UDID                                            |
| [10]      | 声强（预唤醒广播固定为 0）                           |
| [11]      | 声纹置信度                                           |
| [12]      | 抑制标志：`0`=不抑制 `63`=抑制                      |
| [13-14]   | 保留                                                 |

> **Android 符号转换**：Android 使用 int8 存储，鸿蒙使用 uint8。
> 解析时对负值自动 +256（`_int8_to_uint8`）。

## O1 对象结构

每次唤醒事件解析为一个 O1 对象：

```json
{
  "deviceType":         50,
  "deviceTypeName":     "手机",
  "DeviceUdid":         [138, 6, 207, 159],
  "L1WakeupTime":       "03-09 16:38:17.100",
  "L1BroadcastTime":    "03-09 16:38:17.479",
  "L1BroadcastData":    { ... },
  "L2WakeupTime":       "03-09 16:38:22.100",
  "L2BroadcastTime":    "03-09 16:38:22.644",
  "L2BroadcastData":    { ... },
  "ReceivedBroadcasts": [
    { "Broadcast": "0,240,...", "ReceiveTime": "...", "Decoded": { ... } }
  ],
  "DecisionTime":       "03-09 16:38:23.500",
  "Decision":           "true"
}
```

## 对齐算法

1. **Phase 1（精确匹配）**：对比发送广播 raw 字节与其他设备收到的广播 raw 字节，完全一致则合并入同一次唤醒。
2. **Phase 2（时间回退）**：基于 Phase 1 匹配对计算设备两两时间偏移的 median + IQR，在 `max(3×IQR, 2000ms)` 窗口内最近邻匹配未对齐事件。
3. **置信度**（worst-link 规则）：
   - 🟢 广播精确匹配
   - 🟡 时间估算，偏差 ≤ 1×IQR
   - 🔴 时间估算，偏差 > 1×IQR
   - 🔵 孤立事件

## 项目文件

| 文件                  | 说明                                                     |
|-----------------------|----------------------------------------------------------|
| `broadcast_parser.py` | 核心：日志扫描、广播解析、O1 构建、跨设备对齐算法        |
| `wakeup_ui.py`        | NiceGUI UI：解析视图 + 唤醒对齐视图                      |
| `parse_wakeup_logs.py`| 早期独立 CLI 脚本（已被 broadcast_parser.py 取代）        |
| `requirements.txt`    | `nicegui>=1.4.0`、`openpyxl>=3.1.0`                     |
