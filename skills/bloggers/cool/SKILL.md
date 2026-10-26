---
name: public-contract-cool
description: 盘中结构反转策略的项目工程规则摘要
---

# 盘中结构反转策略

这是本项目策略配置的工程摘要，规则值来自 `config/skill_contracts_v2.json`。它不包含博主音频、转录、长篇摘录或个人角色档案。目录名与participant_id保留历史接口兼容性；不表示任何真实人物参与、授权或背书。

以下是策略合同，不是已发生的交易或收益证明。所需证据缺失时保持观察；生产接入情况以代码及公开版本的已知限制说明为准。

```json
{
  "participant_id": "P-COOL",
  "version": "V2",
  "skill_type": "PERSONA_SKILL",
  "investment_style": "超短、高弹性、假跌破反转、低频高要求",
  "universe_policy": "高弹性且可验证流动性的主板标的",
  "candidate_filters": [
    "真实价格与成交数据",
    "真实盘中结构 only when available"
  ],
  "entry_rules": [
    "假跌破/收回/吞没必须有对应真实数据",
    "日内涨幅与换手不得单独证明盘中结构"
  ],
  "position_policy": {
    "max_positions": 2,
    "buy_ratio": 0.2,
    "max_daily_opportunities": 3
  },
  "exit_rules": [
    "Skill自有超短止损/止盈合同",
    "系统仅做确定性执行安全校验"
  ],
  "holding_horizon": "超短",
  "evidence_requirements": [
    "price",
    "change_pct",
    "turnover",
    "intraday structure only when captured"
  ],
  "forbidden_claims": [
    "仅凭涨幅和换手宣称假跌破确认",
    "未提供分时证据的洗盘后收回",
    "未提供K线证据的长阳吞没"
  ]
}
```
