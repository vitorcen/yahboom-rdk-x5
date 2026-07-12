---
name: feedback-atomic-commits
description: 提交纪律——不要频繁小提交,功能模块实测稳定后再一次原子提交
metadata:
  type: feedback
---

用户要求(2026-07-12):不要每改一轮就 commit;调参/迭代中的改动先攒在工作区,
功能模块经实测稳定后再做一次原子提交。已发生的碎提交要软回退(git reset --soft)
合并成原子提交再让用户 push。

**Why:** Follow-me 调试期一小时内产生了 4 个 tuning 提交(9e2e02e..a2ba99a),
历史噪音大、无法按功能回溯;用户自己控制 push 节奏,本地历史要干净。

**How to apply:** 迭代期间只部署验证不提交;用户说"稳定了/收尾"或功能验收通过时,
把工作区改动(含相关 .memory 更新,memory 随功能同一提交,不单独提)整理成一个
`x(x): xxx` 单行提交——语言不限定,按各项目用户提示。commit 前确认 origin 位置,
只回退未 push 的提交。协议已固化到 [[SKILL]](.memory/SKILL.md 的 Committing 节)。

相关:[[rdk-x5-follow-me]]
