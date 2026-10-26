---
name: public-contract-anti-demo
description: 市场过热风险观察的项目工程规则摘要
---

# 市场过热风险观察

这是本项目策略配置的工程摘要，规则值来自 `config/skill_contracts_v2.json`。它不包含博主音频、转录、长篇摘录或个人角色档案。目录名与participant_id保留历史接口兼容性；不表示任何真实人物参与、授权或背书。

以下是策略合同，不是已发生的交易或收益证明。所需证据缺失时保持观察；生产接入情况以代码及公开版本的已知限制说明为准。

```json
{
  "participant_id": "P-RISK-SENTINEL",
  "version": "V2",
  "skill_type": "RISK_SENTINEL",
  "investment_style": "observe/alert/soft-risk context",
  "universe_policy": "观察范围由风险监控合同定义",
  "candidate_filters": [
    "真实风险证据"
  ],
  "entry_rules": [],
  "position_policy": {
    "initial_capital": 0,
    "max_positions": 0,
    "trading_account": false,
    "ranking_enabled": false
  },
  "exit_rules": [],
  "holding_horizon": "N/A",
  "evidence_requirements": [
    "evidence_refs",
    "alert reason"
  ],
  "forbidden_claims": [
    "BUY",
    "SELL",
    "收益排名",
    "持有本金"
  ]
}
```
