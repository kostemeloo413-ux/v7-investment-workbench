---
name: public-contract-liao-xiaoai
description: 价值成长与分批仓位策略的项目工程规则摘要
---

# 价值成长与分批仓位策略

这是本项目策略配置的工程摘要，规则值来自 `config/skill_contracts_v2.json`。它不包含博主音频、转录、长篇摘录或个人角色档案。目录名与participant_id保留历史接口兼容性；不表示任何真实人物参与、授权或背书。

以下是策略合同，不是已发生的交易或收益证明。所需证据缺失时保持观察；生产接入情况以代码及公开版本的已知限制说明为准。

```json
{
  "participant_id": "P-LIAO-XIAOAI",
  "version": "V2",
  "skill_type": "PERSONA_SKILL",
  "investment_style": "产业趋势、核心龙头、价值成长、3·3·1分批建仓",
  "universe_policy": "产业趋势与核心龙头候选",
  "candidate_filters": [
    "真实基本面/行情证据",
    "主板可交易标的"
  ],
  "entry_rules": [
    "首仓、加仓条件必须显式写入position_plan",
    "无限子弹不是无限仓位许可"
  ],
  "position_policy": {
    "max_positions": 2,
    "buy_ratio": 0.3,
    "max_daily_opportunities": 3,
    "scale_in": "3·3·1 contract"
  },
  "exit_rules": [
    "Skill自有逻辑破坏/止盈退出合同",
    "系统仅做确定性执行安全校验"
  ],
  "holding_horizon": "较长持有周期",
  "evidence_requirements": [
    "price",
    "fundamental evidence when claimed",
    "evidence_refs"
  ],
  "forbidden_claims": [
    "无限加仓",
    "缺失证据的产业趋势事实",
    "缺失证据的核心龙头事实"
  ]
}
```
