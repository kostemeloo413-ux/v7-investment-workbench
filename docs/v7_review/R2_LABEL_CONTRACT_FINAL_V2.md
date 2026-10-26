> 公开脱敏副本：保留合同设计与历史研究记录。原始研究数据和内部审查包不随本仓库发布；文中的历史状态不代表此公开版本已通过运行验收。公开副本按自己的文件哈希校验，不继承原生产赛季状态。

# R2 · 标签契约终稿 V2 (R2 Label Contract Final V2 · 最终冻结版)

> 状态：**终审冻结版 (READY_FOR_HUMAN_FINAL_CONFIRMATION)**
> 文档性质：根据用户【最终人工审查修订】编写，**彻底锁定历史 Label 的无歧义时序定义、双轨 Entry Proxy、MAE/MFE 起始日、派生模拟净收益模型及终极字段表**。
> **严格红线：未经用户人工最终明确确认前，禁止修改 labels.py 核心代码，禁止修改 dataset.py，禁止修改任何生产代码，禁止开始 Phase 12 验证，禁止宣布 Research Validated。**

---

## 目录
- [一、 核心决策框架：Scheme A 定义](#一-核心决策框架scheme-a-定义)
- [二、 时序三切分定义 (Cutoff / Signal / Execution)](#二-时序三切分定义-cutoff--signal--execution)
- [三、 历史研究入场双轨制定义 (VWAP Proxy vs Next Open Proxy)](#三-历史研究入场双轨制定义-vwap-proxy-vs-next-open-proxy)
- [四、 两种 Proxy 的用途区别与敏感性否决红线](#四-两种-proxy-的用途区别与敏感性否决红线)
- [五、 【修订 1】未来 1～5 日收益窗口无歧义定义 (future_1d ~ future_5d Gross Return)](#五-修订-1未来-15-日收益窗口无歧义定义-future_1d--future_5d-gross-return)
- [六、 风险与极值路径严格定义 (realized_mae/mfe_daily_bar 起始日与计算)](#六-风险与极值路径严格定义-realized_maemfe_daily_bar-起始日与计算)
- [七、 标签成熟期与有效窗口边界规则 (Label Valid Horizon)](#七-标签成熟期与有效窗口边界规则-label-valid-horizon)
- [八、 交易成本模型与 Market Label 彻底解耦架构](#八-交易成本模型与-market-label-彻底解耦架构)
- [九、 【修订 2】模拟净收益的科学派生定义 (future_Nd_net_return)](#九-修订-2模拟净收益的科学派生定义-future_nd_net_return)
- [十、 防未来数据泄露核查机制](#十-防未来数据泄露核查机制)
- [十一、 Research 与 Production 的绝对物理边界](#十一-research-与-production-的绝对物理边界)
- [十二、 当前已知历史数据限制客观陈述](#十二-当前已知历史数据限制客观陈述)
- [十三、 【终极字段表】全量定义矩阵](#十三-终极字段表全量定义矩阵)

---

## 一、 核心决策框架：Scheme A 定义

1. **框架确认**：
   - 生产系统采用 **5 分钟离散决策网格 (Scheme A)**；
   - 每个 5 分钟节点触发一次新的候选数据刷新、特征计算与决策推断；
   - 坚决**不建设 5 年全市场 1 分钟/Tick 级高频历史数据库**。
2. **重大边界重申**：
   - 我们确认的是“生产端 5 分钟决策框架”；
   - **绝对不是确认历史数据能够完全复现真实 5 分钟盘中成交**。

---

## 二、 时序三切分定义 (Cutoff / Signal / Execution)

任何时间点 $t$，时序严格遵守绝对偏序关系：
$$\text{Feature Cutoff Time} < \text{Signal Time} < \text{Execution Time}$$

```text
时间轴 ──────────────────────────────────────────────────────────►
             t_cutoff                 t_signal              t_exec
                 │                       │                     │
                 ▼                       ▼                     ▼
        【Feature Cutoff】         【Signal Time】       【Execution Time】
         允许使用的信息截止点       模型推理完成并产出     订单真正允许模拟/
         只读 <= t_cutoff 历史     BUY/WAIT 等决策时点   实际撮合成交的时点
```

### 三大时点严格语义：
1. **Feature Cutoff Time**：本次决策允许使用的信息截止时点，严格以此为界拉取历史与截面快照；
2. **Signal Time**：模型完成前向推理并输出 Top 5 与 BUY/WAIT/NO_TRADE 决策的时点；
3. **Execution Time**：订单真正允许在模拟账户中撮合成交的时点。

### 三条绝对禁止铁律：
- **禁止提前假设**：Signal 产生之前，绝不假设已经成交；
- **禁止特征穿透**：严禁使用 Signal 之后的信息构造 Signal 依赖的特征；
- **禁止事后修饰**：严禁使用未来行情反向修改当期已固化的 Signal 与决策。

---

## 三、 历史研究入场双轨制定义 (VWAP Proxy vs Next Open Proxy)

历史研究强制建立**双轨 Entry Proxy 体系**：

### 轨道 A：`HISTORICAL_VWAP_PROXY`（主轨 · 价格代表性研究代理）
- **价格定义**：
  $$\text{Historical VWAP Proxy} = \frac{\text{amount}_T}{\text{volume}_T}$$
  $$\text{Proxy Entry Price}_{\text{vwap}} = \text{Historical VWAP Proxy} \times (1.0 + \text{Slippage})$$
- **严格定性（必须披露）**：
  > **HISTORICAL_VWAP_PROXY 存在明显的【Label-side Entry Look-ahead / Target Construction Bias】（标签侧入场构建后视偏差）！**
- **严密数理陈述**：
  1. 它不是 Feature Leakage（特征工程没有偷看未来）；
  2. 但它是 Label Entry Construction 中的后视偏差：模型在 $t_{\text{cutoff}}$（如 10:25）做预测，但 Label Entry 使用了收盘后才能知道的 $T$ 日全天成交量加权信息；
  3. **禁止宣称**它是严格无未来信息的历史 Entry；禁止称为“真实成交价”、“无偏真实 Entry”或“完全复现生产成交价”；
  4. 必须在所有 Research Limitations 中明确披露该假设。

### 轨道 B：`HISTORICAL_NEXT_OPEN_PROXY`（辅轨 · 保守压力测试基准）
- **价格定义**：
  $$\text{Entry Price}_{\text{open}} = \text{Open}_{T+1} \times (1.0 + \text{Slippage})$$
- **时间锚点与语义**：
  - 时间锚点：$T$ 日 15:00 收盘后产生信号，$T+1$ 日 09:30:00 开盘价执行成交；
  - 角色：**【严格无未来信息的保守压力测试基准】**；
  - 它不代表生产系统盘中实时成交，用于验证策略是否严重依赖 VWAP Entry 假设。

---

## 四、 两种 Proxy 的用途区别与敏感性否决红线

在未来的 Research Validation 中，必须同时汇报两套结果：

```text
                                  候选模型评估
                                       │
                 ┌─────────────────────┴─────────────────────┐
                 ▼                                           ▼
      【主轨: VWAP Proxy 结果】                    【辅轨: Next Open Proxy 结果】
      • Gross/Net Return_vwap                      • Gross/Net Return_open
      • realized_mae_daily_bar                     • realized_mae_daily_bar
      • realized_mfe_daily_bar                     • realized_mfe_daily_bar
      • Win Rate_vwap                              • Win Rate_open
                 │                                           │
                 └─────────────────────┬─────────────────────┘
                                       ▼
                          【敏感性大考一票否决红线】
                 若 VWAP Proxy 表现良好，而 Next Open 明显恶化：
                                       │
                                       ▼
                      ⛔ 必须明确标记为：
             【Entry Price Assumption Sensitive】(对入场假设过度敏感)
                      严禁直接宣布 Alpha 已经被验证！
```

---

## 五、 【修订 1】未来 1～5 日收益窗口无歧义定义 (future_1d ~ future_5d Gross Return)

统一删除任何可能导致歧义的“从 Entry Date 开始计算”表述。

全系统统一标准定义：
> **【`future_Nd` 定义为：实际 Entry Date 之后第 $N$ 个完整交易日收盘价，相对于 Entry Price 的收益。】**

确保 VWAP Proxy 与 Next Open Proxy 使用完全一致的 $N$ 定义，两者具有对称可比的经济含义：

| 字段名 | 入场代理方案 | 决策信号日 (Signal Date) | 实际入场日 (Entry Date) | 收益评估目标日 (Target Date) | 目标价格 (Exit Price) | 实际跨越交易日数 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`future_1d_gross_return`** | **VWAP Proxy** | $T$ 日 | **$T$ 日** (盘中) | **$T$ 日之后第 1 个交易日 ($T+1$)** | $\text{Close}_{T+1}$ | 经历 1 个完整交易日收盘 |
| **`future_1d_gross_return`** | **Next Open** | $T$ 日 | **$T+1$ 日** (开盘) | **$T+1$ 日之后第 1 个交易日 ($T+2$)** | $\text{Close}_{T+2}$ | 经历 1 个完整交易日收盘 |
| **`future_5d_gross_return`** | **VWAP Proxy** | $T$ 日 | **$T$ 日** (盘中) | **$T$ 日之后第 5 个交易日 ($T+5$)** | $\text{Close}_{T+5}$ | 经历 5 个完整交易日收盘 |
| **`future_5d_gross_return`** | **Next Open** | $T$ 日 | **$T+1$ 日** (开盘) | **$T+1$ 日之后第 5 个交易日 ($T+6$)** | $\text{Close}_{T+6}$ | 经历 5 个完整交易日收盘 |

### 数学计算公式：
$$\text{future\_Nd\_gross\_return} = \frac{\text{Close}_{\text{target}}}{\text{Entry Proxy Price}} - 1.0$$
- 分母统一使用对应的 Entry Proxy Price（VWAP 或 Next Open）；
- 分子统一来自 Entry 之后的数据，**严禁使用 Entry 之前的数据计算未来收益**。

---

## 六、 风险与极值路径严格定义 (realized_mae/mfe_daily_bar 起始日与计算)

### 1. 语义与命名分立原则
- **历史走出来的实际事实（标签事实）**：统一命名为 **`realized_mae_daily_bar`** 与 **`realized_mfe_daily_bar`**；
- **未来模型预测出的值（推断预期）**：统一命名为 **`predicted_mae`** 与 **`predicted_mfe`**；
- **禁止混淆**：绝对禁止把历史走出的实际结果命名为 `expected_mae / expected_mfe`。

### 2. Daily Bar 严禁使用 $T$ 日 High/Low（起始日纠偏）
- **禁止原因**：日线无法证明 $T$ 日的 High/Low 是发生在盘中 Entry 之前还是之后，使用 $T$ 日数据会导致严重的事后偏误或虚假风险；
- **保守起算定义**：
  对于在 $T$ 日入场的样本（VWAP Proxy），扫描区间严格限定在 **实际 Entry Date 之后第 1 个交易日至第 $N$ 个交易日 ($T+1 \sim T+N$)**：
  $$\text{realized\_mae\_daily\_bar} = \frac{\min_{k \in [T+1, \, T+N]} (\text{Low}_k)}{\text{Entry Proxy Price}} - 1.0 \quad (\le 0)$$
  $$\text{realized\_mfe\_daily\_bar} = \frac{\max_{k \in [T+1, \, T+N]} (\text{High}_k)}{\text{Entry Proxy Price}} - 1.0 \quad (\ge 0)$$
- 对于在 $T+1$ 日入场的样本（Next Open Proxy），扫描区间严格限定在 **$T+2 \sim T+1+N$**；
- **明确披露**：
  > **【当前日 K 数据无法重建 Entry 之后剩余盘中时段的微观 MAE/MFE，历史标签保守定义为从入场次日起算；绝不伪称为精确盘中微观 MAE/MFE。】**

---

## 七、 标签成熟期与有效窗口边界规则 (Label Valid Horizon)

对于位于历史数据集尾部（如最近 1~5 个交易日）的样本：
- 当 $N$ 日窗口尚未走完时，`future_Nd_gross_return`、`realized_mae_daily_bar` 严格置为 **`None / Null`**；
- **严禁伪造数据**；
- `is_mature = True` 当且仅当未来完整 5 个交易日均已发生，未成熟样本一票否决禁止进入 5 日目标训练集。

---

## 八、 交易成本模型与 Market Label 彻底解耦架构

严禁将交易成本混死在 Market Return Label 内部：

```text
┌─────────────────────────────────────────────────────────────┐
│                 解耦的行情标签与回测仿真架构                 │
├──────────────────────────────┬──────────────────────────────┤
│ 📈 纯净行情标签层 (Market Label)│ 💰 回测与撮合仿真层 (Backtest) │
├──────────────────────────────┼──────────────────────────────┤
│ • future_Nd_gross_return     │ • Commission (万三, 最低5元) │
│ • realized_mae_daily_bar     │ • Stamp Duty (卖出单向0.05%) │
│ • realized_mfe_daily_bar     │ • Slippage 参数化执行假设    │
│ (只负责纯净价格变动与极值)    │ (负责资金流Cash Flow与真实净利)│
└──────────────────────────────┴──────────────────────────────┘
```

### 1. 成本三要素独立定义（禁止混淆、禁止重复扣费）：
- **佣金 (Commission)**：买入与卖出双向收取，$\max(5.0, \, \text{amount} \times 0.0003)$；
- **印花税 (Stamp Duty)**：**严格只在卖出侧单向收取**，$\text{amount}_{\text{sell}} \times 0.0005$，买入侧绝对不收；
- **滑点 (Slippage)**：买卖双边参数化偏离，**若已在成交价格层面吸收，严禁在收益率公式中二次重复扣减**！

### 2. 滑点参数化与 4 档敏感性分析：
必须支持 `0.05%`, `0.10%`, `0.20%`, `0.30%` 四档注入测试，禁止硬编码 0.10%。

---

## 九、 【修订 2】模拟净收益的科学派生定义 (future_Nd_net_return)

核心行情标签必须优先保留纯净的 `future_Nd_gross_return`。

禁止将减法公式作为绝对数学定义。正式修订定义如下：
> **【`future_Nd_net_return` 是交易成本模型在指定：资金规模、佣金规则、印花税规则、滑点参数条件下派生出的模拟净收益。摩擦相减（friction subtraction）只能作为简易近似表达，绝非 Market Label 本体。】**

- **严格物理分立**：
  - **Market Label**：只负责无偏表达资产价格运动的客观物理量（Gross Return、MAE、MFE）；
  - **Trading Backtest Net P&L**：负责在指定资金账户、订单撮合、费率约束下模拟出的资本净收益；
- 避免因未来手续费或印花税调整导致历史基础行情标签需要全部重算。

---

## 十、 防未来数据泄露核查机制

1. **时序阻断**：特征计算切片索引 $\le \text{current\_idx}$，严禁包含未来行；
2. **入场隔离**：Signal 计算不使用收盘后数据；
3. **VWAP 后视披露**：显式标注其 Target Construction Bias；
4. **尾部成熟过滤**：未成熟样本强制 Null 并 dropna。

---

## 十一、 Research 与 Production 的绝对物理边界

- **Research**：离线批量处理、使用 VWAP / Next Open 代理、训练模型、出具 OOF / OOS 报告；
- **Production**：每 5 分钟轮询实时盘面快照、使用瞬时可执行价、仅做前向推断，**盘中严禁训练模型**。

---

## 十二、 当前已知历史数据限制客观陈述

1. **无全量高频快照**：无 5 年分钟级盘口，历史研究采用 VWAP / Next Open 代理；
2. **无历史退市标的**：缺少退市股票历史数据，存在一定幸存者偏差；
3. **ST 状态为静态**：缺少历史上动态戴帽/摘帽的精确时序区间。

---

## 十三、 【终极字段表】全量定义矩阵

| 字段名 (Field Name) | 数据类型 | 数学定义 / 计算公式 | 数据来源 | 允许使用信息截止时间 | 是否允许未来数据 | 业务用途 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`feature_cutoff`** | `TEXT (ISO8601)` | 本次决策允许使用的信息截止时戳 | 调度时钟 | $t_{\text{cutoff}}$ 瞬时 | ❌ 严禁 | 确立特征计算时序边界 |
| **`signal_time`** | `TEXT (ISO8601)` | 模型推理完成并产出决策的时戳 | 推理引擎 | $t_{\text{signal}}$ 瞬时 | ❌ 严禁 | 决策产生锚点 |
| **`execution_time`** | `TEXT (ISO8601)` | 订单被允许撮合成交的时戳 | 撮合引擎 | $t_{\text{exec}}$ 瞬时 | ❌ 严禁 | 实际成交锚点 |
| **`entry_price`** | `REAL` | 实际买入价格（生产为快照价，研究为代理价） | 行情快照 / 日K | $t_{\text{exec}}$ 或 $T$ 日日K | ❌ 严禁(生产) / ⚠️代理(研究) | 买入成本基准 |
| **`entry_proxy_type`** | `TEXT` | `LIVE_SNAPSHOT` / `HISTORICAL_VWAP_PROXY` / `HISTORICAL_NEXT_OPEN_PROXY` | 系统参数 | 确定模型时 | ❌ 严禁 | 标明价格口径与研究假说 |
| **`future_1d_gross_return`**| `REAL` | $(P_{\text{entry}+1,\text{close}} / P_{\text{entry}}) - 1$ | 历史日K | Entry 之后第1日收盘 | ⚠️ 标签专用 (未来1日) | 1日纯净行情学习目标 |
| **`future_2d_gross_return`**| `REAL` | $(P_{\text{entry}+2,\text{close}} / P_{\text{entry}}) - 1$ | 历史日K | Entry 之后第2日收盘 | ⚠️ 标签专用 (未来2日) | 2日纯净行情学习目标 |
| **`future_3d_gross_return`**| `REAL` | $(P_{\text{entry}+3,\text{close}} / P_{\text{entry}}) - 1$ | 历史日K | Entry 之后第3日收盘 | ⚠️ 标签专用 (未来3日) | 3日纯净行情学习目标 |
| **`future_4d_gross_return`**| `REAL` | $(P_{\text{entry}+4,\text{close}} / P_{\text{entry}}) - 1$ | 历史日K | Entry 之后第4日收盘 | ⚠️ 标签专用 (未来4日) | 4日纯净行情学习目标 |
| **`future_5d_gross_return`**| `REAL` | $(P_{\text{entry}+5,\text{close}} / P_{\text{entry}}) - 1$ | 历史日K | Entry 之后第5日收盘 | ⚠️ 标签专用 (未来5日) | 5日核心排序主力目标 |
| **`future_5d_net_return`**  | `REAL` | 成本模型派生净收益（非底层Market Label本体） | 成本模型派生 | Entry 之后第5日收盘 | ⚠️ 派生标签 | 考虑摩擦后的经济评价 |
| **`realized_mae_daily_bar`**| `REAL` | $(\min_{k \in [\text{entry}+1, \text{entry}+N]}(L_k) / P_{\text{entry}}) - 1$ | 历史日K | Entry 之后第5日收盘 | ⚠️ 标签专用 (次日起算极值) | 真实日K级下撤事实标签 |
| **`realized_mfe_daily_bar`**| `REAL` | $(\max_{k \in [\text{entry}+1, \text{entry}+N]}(H_k) / P_{\text{entry}}) - 1$ | 历史日K | Entry 之后第5日收盘 | ⚠️ 标签专用 (次日起算极值) | 真实日K级冲高事实标签 |
| **`predicted_mae`** | `REAL` | 模型针对该标的预测的预期最大下撤 | 风险模型/估计器 | $t_{\text{signal}}$ 瞬时 | ❌ 严禁 (纯历史推断) | 止损位定锚与风控披露 |
| **`predicted_mfe`** | `REAL` | 模型针对该标的预测的预期冲高幅度 | 风险模型/估计器 | $t_{\text{signal}}$ 瞬时 | ❌ 严禁 (纯历史推断) | 止盈位定锚与盈亏比计算 |
| **`gross_return`** | `REAL` | 泛指未扣规费的毛收益 | 历史日K | 退出日收盘后 | ⚠️ 标签专用 | 纯净行情变动统一概念 |
| **`net_return`** | `REAL` | 扣除佣金、印花税、滑点后的净收益 | 回测/撮合引擎 | 退出日收盘后 | ⚠️ 仿真专用 | 实盘与回测资金流评价 |
| **`is_mature`** | `INTEGER` | 1=未来5日已走完可用于训练; 0=未走完 | 时序状态 | 样本构建时 | ❌ 严禁 | 训练样本准入硬闸门 |

---

*（本文档为 R2 标签契约最终版本，存放于 docs/v7_review/R2_LABEL_CONTRACT_FINAL_V2.md）*
