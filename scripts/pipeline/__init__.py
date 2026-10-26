# -*- coding: utf-8 -*-
"""
scripts/pipeline/ —— 朝廷流水线总控体系（阶段 1：骨架先行）

中式官称体系（历史原型对应）：
  - 内阁       orchestrator.py   总控编排器（首辅总揽）
  - 都察院     inspector.py      系统检查官（御史台监察，管"做没做"）
  - 大理寺     （已有共识裁判）  管"观点裁决对不对"，与都察院定位区分
  - 通政司/驿传/六部/起居注      阶段 2 拆分的细粒度角色
  - 内阁档册   pipeline_state.json   流水线状态机
  - 弹章       inspection_report.json 都察院检查报告
"""
