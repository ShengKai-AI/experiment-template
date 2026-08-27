---
document_type: experiment_log
run_id: <run_id>
status: <completed | failed | cancelled>
started_at: <ISO 8601 北京时间，例如 2026-08-27T15:30:00+08:00>
ended_at: <ISO 8601 北京时间，例如 2026-08-27T18:10:00+08:00>
created_at: <ISO 8601 北京时间，例如 2026-08-27T15:25:00+08:00>
updated_at: <ISO 8601 北京时间，例如 2026-08-27T18:15:00+08:00>
branch: <Git 分支>
commit: <实际 commit>
---

# <Run 名称>

## 实验目标

简要说明本次 Run 验证的内容和主要对照关系。

## 执行内容

| Execution | Experiment | 类型 | 状态 | 关键设置 |
|---|---|---|---|---|
| `<exec-id>` | `<experiment-id>@<version>` | `<类型>` | `<状态>` | `<简要设置>` |

完整配置和阶段记录：`runs/<run_id>/run.yaml`

## 实际过程

- 完成的主要阶段：
- 重要参数调整：
- 失败、重试或人工干预：
- 与原计划的差异：

## 结果

| Execution | 指标 | 结果 | 对照差异 |
|---|---|---:|---:|
| `<exec-id>` | `<metric>` | `<value>` | `<difference>` |

## 结论

- 实验效果：
- 对实验假设的支持情况：
- 当前不能确定的结论：

## 异常与限制

- 异常：
- 缺失结果：
- 可比性限制：

## 产物

- 关键结果：
- 模型或检查点：
- 日志：
