---
name: public-contract-hanshu
description: 均线支撑与波段策略的项目工程规则摘要
---

# 均线支撑与波段策略

这是本项目策略配置的工程摘要，规则值来自 `config/skill_contracts_v2.json`。它不包含博主音频、转录、长篇摘录或个人角色档案。目录名与participant_id保留历史接口兼容性；不表示任何真实人物参与、授权或背书。

以下是策略合同，不是已发生的交易或收益证明。所需证据缺失时保持观察；生产接入情况以代码及公开版本的已知限制说明为准。

```json
{
  "participant_id": "P-HANSHU",
  "version": "V2",
  "skill_type": "PERSONA_SKILL",
  "investment_style": "20MA生命线回踩低吸、不追高、中短线",
  "universe_policy": "熟悉的硬科技、制造、医药等主板标的",
  "candidate_filters": [
    "真实价格与成交数据",
    "主板可交易标的"
  ],
  "entry_rules": [
    "回踩低吸条件必须由真实可用指标证明",
    "缺失MA5/MA20计算时不得宣称均线事实"
  ],
  "position_policy": {
    "max_positions": 2,
    "buy_ratio": 0.25,
    "max_daily_opportunities": 3
  },
  "exit_rules": [
    "Skill自有止损/止盈合同",
    "系统仅做账户与交易制度安全校验"
  ],
  "holding_horizon": "中短线",
  "evidence_requirements": [
    "price",
    "change_pct",
    "turnover",
    "amount",
    "MA5/MA20 only when actually computed"
  ],
  "forbidden_claims": [
    "未计算的MA20生命线确认",
    "未计算的MA5站上",
    "未提供证据的趋势斜率"
  ]
}
```
