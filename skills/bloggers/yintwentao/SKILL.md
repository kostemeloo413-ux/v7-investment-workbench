---
name: public-contract-yintwentao
description: 科技成长轮动策略的项目工程规则摘要
---

# 科技成长轮动策略

这是本项目策略配置的工程摘要，规则值来自 `config/skill_contracts_v2.json`。它不包含博主音频、转录、长篇摘录或个人角色档案。目录名与participant_id保留历史接口兼容性；不表示任何真实人物参与、授权或背书。

以下是策略合同，不是已发生的交易或收益证明。所需证据缺失时保持观察；生产接入情况以代码及公开版本的已知限制说明为准。

```json
{
  "participant_id": "P-YINWENTAO",
  "version": "V2",
  "skill_type": "PERSONA_SKILL",
  "investment_style": "科技成长、大资金合力、阵营博弈、波段",
  "universe_policy": "科技成长与可交易主板标的",
  "candidate_filters": [
    "真实价格、成交额与换手证据"
  ],
  "entry_rules": [
    "波段与阵营判断由Skill依据证据形成",
    "成交额高只能表述为高成交额"
  ],
  "position_policy": {
    "max_positions": 2,
    "buy_ratio": 0.3,
    "max_daily_opportunities": 3
  },
  "exit_rules": [
    "Skill自有破位/波段退出合同",
    "系统仅做确定性执行安全校验"
  ],
  "holding_horizon": "波段",
  "evidence_requirements": [
    "price",
    "change_pct",
    "turnover",
    "amount",
    "capital_flow only when actually available"
  ],
  "forbidden_claims": [
    "无资金流数据时宣称机构坚决买入",
    "无主力净流入数据时宣称大资金流入"
  ]
}
```
