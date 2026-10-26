# V7 · A 股个人决策与可审计模拟交易工作台

> `v0.1-preview` · 公开源码预览。用多策略观点、确定性交易规则和可追溯记录，减少个人研究中的信息分散与主观判断盲区。

这是一个持续开发中的个人项目，包含数据适配、事件研判、策略契约、量化研究、模拟撮合与工作台。当前初版提供**不需要密钥、私有数据库或原电脑赛季状态的离线核心流程演示**，并保留原 V7 界面与主要源码。

[快速运行](#先运行一次) · [核心能力](#核心能力) · [架构说明](docs/ARCHITECTURE.md) · [已知问题](docs/KNOWN_ISSUES.md)

## 项目简介

V7 是一个持续迭代的个人 A 股研究与模拟交易项目，将行情、新闻事件、多策略分析和模拟账户整合到同一个工作台。系统记录研究判断、风控检查和模拟成交过程，方便日常查看、核对与复盘。

## 核心能力

| 能力 | 解决的问题 |
|---|---|
| 多策略研究 | 用角色契约和独立 AI Quant 账户承载不同研究视角，区分观点、共识和风险提醒 |
| 市场与事件工作台 | 汇集市场状态、新闻事件、个股线索与研究页面，减少信息来回查找 |
| 数据准入与风险约束 | 检查数据时效和必要证据，证据不足时保留 HOLD / NO_SIGNAL，避免把缺失包装成确定结论 |
| 模拟交易与可审计账本 | 将 T+1、整手、滑点和费用落实到撮合与账户记录，可从成交记录复核现金和持仓 |
| 无密钥历史演示 | 用明确标注的历史行情和假设订单，独立跑通撮合、同日卖出拦截与账本对账 |

整体链路：**行情与事件 → 策略研判 → 数据与风控检查 → 模拟成交 → 账本与复盘**。LLM 用于新闻和事件的语义研判，交易约束与账本计算使用确定性逻辑；当前公开演示验证后半段核心流程，实时采集与 LLM 连接需另行配置。

## 先运行一次

验证环境：**Windows、64 位 Python 3.13**。先确认 `py -3.13 --version` 可用；没有 Python Launcher 时，用已安装 Python 3.13 解释器的完整路径代替首行的 `py -3.13`。以下命令在仓库根目录运行；不需要激活虚拟环境。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m scripts.public_preview
```

打开 [本地演示](http://127.0.0.1:8020/demo)。默认端口 8020，不占用既有工作台端口。若端口被占用，在启动命令后加 `--port 8021`。macOS/Linux 可使用 `python3.13` 建立虚拟环境、`.venv/bin/python` 执行同样的模块命令；本次首发优先验证 Windows。

不启动网页，直接核对核心流程：

```powershell
.\.venv\Scripts\python.exe -m scripts.public_preview --check
```

也可运行 `python -m scripts.public_demo --json` 查看假设成交明细。回放核心本身只使用 Python 标准库；网页所需依赖由 `requirements.txt` 提供。

## 历史回放示例

示例使用3条历史行情和预设的假设订单，展示买入撮合、T+1限制、次日卖出、费用计算与账本对账的运行过程。

行情样本来自 BaoStock 历史未复权数据（000001，2026-08-28、08-31、09-01），具体步骤如下：

1. 以第一条行情作为前收基准，第二个交易日按收盘价代理输入假设买单。
2. 调用原有 `SimulationFillEngine`、`AShareRules` 和 `UnifiedCostModel` 计算整手、滑点与佣金。
3. 同日卖出被 T+1 拒绝；下一交易日按同样规则计算退出费用。
4. 用原有 `ArenaAccountsManager` 更新隔离示例账户，再由演示成交记录核对最终现金。

每次演示仅使用临时目录，结束后清理；不会建立正式赛季、连接行情源/LLM、启动调度器或累计 Forward Day。这是**历史价格代理上的规则演示**，不证明对应订单曾真实发生、盘中必能成交或策略有盈利能力。[样本来源与边界](docs/DEMO_PROVENANCE.md)

[原 V7 界面](http://127.0.0.1:8020/v7)也可浏览。没有分发的数据诚实显示为不可用或待确认，页面不会填入私人账户或伪造实时行情。公开入口只接受 GET/HEAD，默认仅绑定本机回环地址。

## 项目结构与核心取舍

| 模块 | 实现位置 | 作用 |
|---|---|---|
| 数据与准入 | `scripts/datasource/` | 数据源适配、时效与完整性检查 |
| 策略与事件 | `scripts/analysis/`、`config/skill_contracts_v2.json` | 将观点转成有证据要求的规则与决策输出 |
| 量化研究 | `scripts/ai_quant/`、`scripts/research/` | 特征、排序模型、校准与历史评估 |
| 模拟执行 | `scripts/competition/` | 账户、交易约束、费用、成交与账本 |
| API 与界面 | `scripts/daemon/`、`workbench_v7/` | 六个收益账户与风险观察角色的只读工作台 |
| 独立演示 | `scripts/public_demo.py`、`scripts/public_preview.py` | 无私有状态的可复现入口 |

产品上的核心取舍是：**观点与执行许可分开；缺数据明确停留在观察；用统一规则和账本检验结果**。原 V7 包含五个规则/角色策略、一个独立 AI Quant 收益账户，以及不交易的风险观察角色。角色名称属于历史接口标签，不表示相关人物参与或背书。[架构说明](docs/ARCHITECTURE.md)

## 依赖与验证

`requirements.txt` 是可运行公开预览所需的最小依赖；`constraints-preview.txt` 固定本次新环境验收使用的依赖版本。研究和原实时栈单独列出：

| 文件 | 用途 |
|---|---|
| `requirements-dev.txt` | 预览与数据适配层的离线验收测试 |
| `requirements-quant.txt` | NumPy、pandas、LightGBM、scikit-learn、joblib 等研究依赖 |
| `requirements-live.txt` | 历史调度、数据源、LLM 和 Windows 托盘依赖；不等同于正式赛季开通 |

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

默认测试集合验证当前公开入口、历史撮合、无私有状态启动、只读边界和离线数据适配。仓库还保留历史测试，部分依赖未分发的审查材料或旧状态，未声明全量通过。真实数据源需要使用者自行配置凭证；默认演示不读取 `.env`、模型连接档或继承的 LLM/Webhook 凭证。Tushare 仅保留可选适配钩子，未纳入本版支持范围。

## 已知限制与维护方向

这是首个公开快照，**原运行系统的故障没有因发布整理而消失**：日终数据恢复、AI Quant 历史准入与退出监控解耦、两项角色策略证据接入仍待后续补丁。没有公开经过合格 Forward 验证的盈利结论。[已知问题](docs/KNOWN_ISSUES.md)

原始音频/转录、完整新闻语料、个人持仓与交易、生产状态、数据库、密钥、内部聊天与审查包均不在此仓库。长期开发过程将以可核对的脱敏里程碑和后续真实提交记录展示。代码、公开模型资源与第三方依赖的许可边界分别说明；首版尚未授予整个目录统一的再分发许可。


## 发布与补丁检查

每次提交前运行 `python -B tools/check_public_release.py --git`，并用Gitleaks独立检查目录和完整历史。仓库CI会重复执行隐私检查与公开预览测试；首次远端执行结果须在上传后确认。[隐私检查范围](docs/PRIVACY.md)
