---
name: public-contract-ai-alpha
description: 规则量化策略的项目工程规则摘要
---

# 规则量化策略

这是本项目策略配置的工程摘要，规则值来自 `config/skill_contracts_v2.json`。它不包含博主音频、转录、长篇摘录或个人角色档案。目录名与participant_id保留历史接口兼容性；不表示任何真实人物参与、授权或背书。

以下是策略合同，不是已发生的交易或收益证明。所需证据缺失时保持观察；生产接入情况以代码及公开版本的已知限制说明为准。

```json
{
  "participant_id": "P-RULE-QUANT",
  "version": "V2",
  "skill_type": "RULE_BASED_SKILL",
  "investment_style": "多因子动量、质量、流动性与规则筛选",
  "universe_policy": "规则定义的主板候选池",
  "candidate_filters": [
    "确定性因子字段",
    "规则阈值",
    "可复现输入"
  ],
  "entry_rules": [
    "BUY/SELL必须由Rule Contract触发",
    "规则输入缺失则NO_SIGNAL，不由中央主观判断补齐"
  ],
  "position_policy": {
    "max_positions": 3,
    "buy_ratio": 0.2,
    "max_daily_opportunities": 3
  },
  "exit_rules": [
    "规则合同定义的退出条件",
    "系统仅做确定性执行安全校验"
  ],
  "holding_horizon": "规则定义",
  "evidence_requirements": [
    "factor values",
    "rule version",
    "thresholds",
    "evidence_refs"
  ],
  "forbidden_claims": [
    "将规则筛选包装成AI人格判断",
    "缺失因子值时声称规则命中"
  ]
}
```
