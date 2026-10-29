# API 说明

## 范围

V7 工作台通过 FastAPI 只读聚合接口消费数据，前缀为 `/api/v7`。下表按当前路由实现整理；完整参数、校验及类型以源码和运行服务的 `/openapi.json` 为准。

公开预览默认绑定 `127.0.0.1:8020`，只允许 GET/HEAD。原运行栈的历史 `/api/*` 路由不属于本表，也不代表公开预览提供相应写能力。

## 响应结构

聚合接口通过统一封装区分响应生成时间、底层数据新鲜度、状态、警告和数据：

```text
schema_version   响应结构版本
generated_at     响应生成时间，不等于源数据时间
data_freshness   底层数据新鲜度信息
status           当前接口状态
warnings         限制或读取异常
data             业务对象
```

消费者应同时处理缺失、过期与不可用情况，不以字段存在或 HTTP 200 直接判定数据可用于执行。部分路由采用专用视图，具体字段以实现为准。

## V7 聚合路由

| GET 路由 | 作用 | 参数说明 |
|---|---|---|
| `/overview` | 总览与上下文 | — |
| `/market` | 市场环境 | — |
| `/news` | 新闻与事件 | — |
| `/picks` | 策略候选 | — |
| `/stock/{symbol}` | 个股证据 | 证券代码 |
| `/ai-quant` | AI Quant 研究状态 | — |
| `/paper` | 模拟状态 | — |
| `/forward` | 实际时间跟踪及资格 | — |
| `/trade-plans` | 计划记录 | 可选 `status_filter` 等，见 OpenAPI |
| `/arena` | 竞技场汇总 | — |
| `/arena/{participant_id}` | 账户详情与成交列表 | `offset` ≥ 0；`limit` 为 1–100，默认 20 |
| `/data-center` | 数据覆盖与状态 | — |
| `/data-sources` | 数据源配置状态 | — |
| `/integrations/llm` | LLM 集成状态 | — |
| `/integrations/wecom` | 企业微信集成状态 | — |
| `/system` | 系统运行状态 | — |
| `/research` | 研究报告 | 可选 `report_id` |

实现位置：`scripts/daemon/v7_api.py`；候选与账户投影分别由 `v7_picks.py`、`v7_arena.py` 提供。

## 公开预览接口

| 接口 | 数据性质与用途 |
|---|---|
| `/api/ui-demo/*` | 仅用于合成界面；响应带合成标记与未授权执行状态 |
| `/api/demo` | 运行极小历史样本上的假设撮合并返回结果 |
| `/api/v7/*` | 真实聚合逻辑；未分发的生产输入保留不可用状态 |

合成界面的日期、资格字段和账户指标只用于组件渲染，不是市场、模型、成交或执行资格证据。

## 业务对象与状态

观点、规则信号、执行资格、订单和成交分别处理。`HOLD`、`NO_SIGNAL` 与过期状态的定义见[系统说明](SYSTEM_OVERVIEW.md)。竞技场展示成功 BUY/SELL 成交，受限或未成交订单保留在原始审计记录中，不计入展示成交数。
