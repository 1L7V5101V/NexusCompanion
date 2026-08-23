# C0 证据：dashboard /metrics 导出与指标视图（任务 4.1–4.2）

- 记录日期：2026-08-23
- 代码位置：
  - `core/telemetry/metrics.py`（MetricRegistry / Counter / Histogram / Timer / `get()` / `get_default_registry()`）
  - `core/telemetry/builtin.py`（`register_builtin_metrics` 幂等注册 turn/存储/迁移六族）
  - `core/telemetry/metrics_export.py`（`export_json` / `export_prometheus_text`）
  - `bootstrap/dashboard_api.py`（`create_dashboard_app(metric_registry=...)` + `/metrics` 路由）
  - `frontend/dashboard/src/main.tsx`（指标视图 / MetricsView，数据源 `/metrics?format=json`）
  - `scripts/load/driver.py` / `scenarios/simulate.py`（`drive_turns(metric_registry=...)` 记录 turn 指标）
- 相关截图：`dashboard-metrics-ui.png`（headless Chrome 实渲染截图）

## 4.1 /metrics 端点（JSON + Prometheus 文本双格式）

路由挂在 dashboard 进程 FastAPI 应用上（`bootstrap/dashboard_api.py`），默认走进程级
`get_default_registry()`；测试可显式注入 `metric_registry`。默认输出 Prometheus 文本，
`?format=json` 输出 JSON。

验证（`tests/test_dashboard_metrics.py`）：

- Prometheus 文本：200 + `text/plain`，已记录指标含 `# HELP` / `# TYPE` 与样本行
  （`turns_total{channel="cli"} 3.0`），timer 按 histogram 语义展开 `_bucket/_sum/_count`。
- JSON：200 + 可解析，含已记录指标的最新值；counter 跨系列、timer 含 count/sum/buckets。
- 导出反映最新记录值：cli 累加 1+4=5、telegram 2，两格式一致。
- 显式传入的 registry 是唯一数据源（不污染进程默认注册表）。

**UI 验证中发现并修复的缺陷**：JSON 导出最初用 `json.dumps` 直接序列化快照，
histogram/timer 的 `+Inf` bucket 上界 `le` 是 `float('inf')`，被序列化成 JSON 不存在的
字面量 `Infinity`。Python `json.loads` 宽容接受它（pytest 假绿），但浏览器 `JSON.parse`
抛 `Unexpected token 'I'`，dashboard 指标视图因此空白。修复：`export_json` 对样本递归
规约非 JSON float 常量（`inf`/`NaN` → `null`），并新增严格解析断言（`parse_constant`
抛错），保证输出对 Python 与浏览器两种消费者都合法。

## 4.2 dashboard metric tile 展示 + SQLite 读路径无回归

前端指标视图（`MetricsView`，`frontend/dashboard/src/main.tsx`）：

- 数据源 `api<MetricSample[]>('/metrics?format=json')`，与 Prometheus 抓取同路径（D4）。
- 六张 MetricTile：turn 总数（按 channel 拆分）/ turn 平均耗时；存储操作数（按 backend
  拆分）/ 存储平均耗时；迁移导入行数 / 迁移批处理平均耗时。
- counter 跨系列求和，histogram/timer 显示平均耗时（sum/count）；指标视图内 5s 轮询，
  计数器滚动 sparkline（最近 20 次采样）；空数据时显示引导文案（C1B/C1D 记录点写入后
  才有值）。

验证：

1. 后端测试：`tests/test_dashboard_metrics.py` +
   `tests/test_load_metrics.py`（`drive_turns` 注入 registry 后真实记录 turn 指标，
   2 channel × 2 轮 → turns_total 每 channel 2、timer 跨系列 count 和 4）+ 
   `tests/test_metrics_registry.py`，共 15 passed。
2. 回归：`test_existing_sqlite_read_paths_no_regression` 断言挂载 `/metrics` 后
   `/api/dashboard/logs`（历史日志）与 `/api/dashboard/sessions`（交付/会话）仍 200。
3. 实浏览器渲染（headless Chrome + CDP，截图 `dashboard-metrics-ui.png`）：注入
   cli=12/telegram=7 turn、0.05s/0.12s 耗时、sqlite=3 存储、500 行迁移后，页面点「指标」
   tab 呈现：

   | 卡片 | 呈现值 |
   |---|---|
   | Turn 总数 | 19（cli 12 · telegram 7） |
   | Turn 平均耗时 | 85ms（2 次采样） |
   | 存储操作数 | 3（sqlite 3） |
   | 存储操作平均耗时 | 2ms |
   | 迁移导入行数 | 500 |
   | 迁移批处理平均耗时 | 450ms |

   DOM 断言 + 截图均确认指标数据正确呈现，无请求错误模态框。

结论：任务 4.1「`/metrics` 返回 200 与可解析的 JSON/Prometheus 文本」与 4.2
「metric tile 读取 export 并展示 turn/存储/迁移指标，SQLite 读路径无回归」达成。
