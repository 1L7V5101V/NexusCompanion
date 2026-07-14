# VitalCast iOS App — 需求说明书

## 一句话

一个 iPhone App，后台监听 Apple Watch 健康数据（心率、步数、睡眠、体重等），数据变化时自动上传到自建服务器，供 AI Agent 读取和主动推送。

---

## 架构

```
Apple Watch → HealthKit (iPhone)
                  │
       HKObserverQuery 自动触发
                  │
        App 读最新数据 → POST 到服务器
                  │
  服务器存 vitals.json / alerts.json
                  │
        AI Agent 的 ProactiveLoop tick (40-80min)
                  → MCP server 读 vitals.json
                  → LLM 判断 → 主动推送消息
```

---

## 数据采集

全部由 `HKObserverQuery` 自动触发。**没有定时器，没有轮询，没有固定间隔上传。** Apple Watch 有心率/步数等新数据写入 HealthKit 时，iOS 自动唤醒 App 处理。

| 指标 | HealthKit 类型 | 读取方式 |
|------|---------------|---------|
| heartRate | HKQuantityTypeIdentifierHeartRate | 读最新一条样本 |
| steps | HKQuantityTypeIdentifierStepCount | 读今日累计 (HKStatisticsQuery) |
| sleep | HKCategoryTypeIdentifierSleepAnalysis | 读最近一晚 |
| bodyWeight | HKQuantityTypeIdentifierBodyMass | 读最新一条 |
| activeEnergy | HKQuantityTypeIdentifierActiveEnergyBurned | 读今日累计 (HKStatisticsQuery) |

---

## 上报接口

### POST /api/vitals — 健康数据上报

每条记录带客户端生成的 `id`（`type + ISO date` 的 SHA256 前缀），服务器用 `id` 做 upsert：

```json
[
  {
    "id": "a3f8c2...",
    "type": "heartRate",
    "value": 72,
    "unit": "count/min",
    "date": "2026-07-11T10:30:00Z"
  },
  {
    "id": "b7d1e9...",
    "type": "steps",
    "value": 8423,
    "unit": "count",
    "date": "2026-07-11T10:30:00Z"
  }
]
```

### POST /api/alert — 高心率即时报警

数据变化时本地判断 `heartRate > 120`，满足条件立即上报。同样带 `id` 防重复：

```json
{
  "id": "e4f2a1...",
  "type": "high_heart_rate",
  "value": 125,
  "unit": "count/min",
  "timestamp": "2026-07-11T10:30:05Z",
  "message": "Heart rate is 125 bpm"
}
```

**服务器约定**：vitals 用 `id` 做 key 覆盖存储（upsert）；alerts 用 `id` 去重，已存储的重复 `id` 直接返回 200 但不重复入队。该约定在 App 和 MCP server 两端必须同步实现。

---

## App 能力要求

| 能力 | 必要？ | 说明 |
|------|:-----:|------|
| HealthKit 读取权限 | ✅ | heartRate / steps / sleep / bodyWeight / activeEnergy |
| HKObserverQuery 后台唤醒 | ✅ | 数据变化时系统自动叫醒 App |
| HKSampleQuery / HKStatisticsQuery | ✅ | 读最新数据 / 读今日累计 |
| Background URLSession | ✅ | App 被唤醒后 POST 数据到服务器 |
| 本地简单数值判断 | ✅ | 心率 > 120 时额外 POST /api/alert |
| 后台长时间运行 | ❌ | ObserverQuery触发→工作→结束，没有常驻后台 |
| 定时器 / 轮询 | ❌ |HKObserverQuery 一个不需要 |
| watchOS App | ❌ | Watch 数据系统自动同步到 iPhone HealthKit |
| APNs 推送 | ❌ | 服务器不主动推 App，仅 App 往服务器推 |
| WebSocket 长连接 | ❌ | 不需要 |

---

---

## 已知约束 & 应对方案

### 约束 1：HKObserverQuery 的延迟不可控

`enableBackgroundDelivery(frequency: .immediate)` 是**请求**而非保障。iOS 会按电量、网络、App 使用频率动态节流。静止待机时，数据写入到 App 被唤醒可能有 15-30 分钟延迟。

**对架构的影响**：数据是"最终一致"的，下游 AI Agent **不应假设数据是分钟级新鲜的**。但因为 ProactiveLoop tick 间隔为 40-80 分钟，数据总是比 tick 先到，不影响核心流程。

### 约束 2：后台执行窗口有限（约 30 秒）

ObserverQuery 唤醒后，`UIApplication.backgroundTimeRemaining` 约 30 秒。网络抖动或服务器慢时，POST 可能超时被杀，导致数据丢失。

**应对方案**：APP 必须在本地维护一个**失败重试队列**。

```
ObserverQuery 唤醒 → 先重试队列头部一小批
                  → 再处理当前数据
                  → POST 成功 → 继续
                  → POST 失败 → 追加到本地队列尾部
                               下次唤醒时重试，成功则移除
```

推荐用 `UserDefaults` 存 JSON 数组，不需要 SQLite。

**三个必须处理的细节（直接看下面伪代码中的标注）：**

```swift
// ============================================
// 核心流程（伪代码，各语言框架按此模式实现）
// ============================================

// ① 并发防护：所有队列操作走同一个串行队列
let queue = DispatchQueue(label: "com.vitalcast.retry")
let maxRetryPerWake = 5       // ② 每次最多重试 5 条
let defaults = UserDefaults.standard

func onHealthKitDataChanged() {
    // ③ 每条数据带客户端 id
    let sample = HealthSample(
        id: "\(type)_\(isoDate)".sha256.prefix(12),  // ← 幂等 id
        type: "heartRate",
        value: 72,
        unit: "count/min",
        date: "2026-07-11T10:30:00Z"
    )

    queue.sync {   // ← ① 串行队列，避免并发覆盖
        // 1. 先重试队列里的旧数据（最多 5 条）
        var pending = loadPending()          // 从 UserDefaults 读
        let toRetry = pending.prefix(maxRetryPerWake)  // ② 上限
        for item in toRetry {
            if upload(item) { markDone(&pending, item) }
            else { break /* 网络还不行，剩下的下次再试 */ }
        }

        // 2. 再处理当前数据
        if upload(sample) {
            // 成功 → 无事可做
        } else {
            // 失败 → 追加到队列尾部
            pending.append(sample.dict)
            savePending(pending)             // 写回 UserDefaults
        }
    }
}
```

---

## 架构要求 — 高解耦

每个健康指标是一个独立的 `Provider`，遵循统一协议：

```swift
protocol HealthProvider {
    var type: HealthMetricType { get }
    var sampleType: HKSampleType { get }
    func fetchLatest() async throws -> HealthSample?
}
```

现有 5 个 Provider，添加新指标 = 新建文件 + 注册一行。上传层和报警层对 Provider 无感。

---

## 开发环境

- **开发机**：Windows（VS Code）
- **目标设备**：iPhone（个人用，侧载，不上架 App Store）
- **iOS 目标版本**：17+
- **编译环境**：没有 Mac，依赖云端编译（EAS / Codemagic 等）
- **预算**：最好 $0，愿意花 $99/yr Apple Developer Program

---

## 不需要的（已排除）

- ❌ 不每 30s 上传
- ❌ 不用定时器
- ❌ 不用 BGTaskScheduler
- ❌ 不用 APNs 推送
- ❌ 不用 WebSocket
- ❌ 不用 watchOS App
- ❌ 不上架 App Store
