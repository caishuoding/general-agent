Status: ready-for-human

# 合并主回答图并删除嵌套编排

## 目标

把当前 `prepare_context -> agent -> finalize` 外层图和 `prepare_model -> call_model -> execute_tools` 内层图合并成一张主回答图。同一会话使用稳定 `conversation_id` 线程，主、子智能体共享循环节点实现，不再由主图节点启动另一张主工具图。

## 验收

- 主图节点为 `prepare_context`、`prepare_model`、`call_model`、`execute_tools`、`finalize`。
- 自动收信在 `prepare_model` 完成；子任务结束检查在 `finalize` 完成。
- 同一会话新消息和中断继续都使用同一 thread；重试不复用失败尝试的半套工具消息。
- State 保留现有 Skill、工具、轮次、失败计数、子任务交付和回答字段，并增加当前 `answer_id`、`failure` 所需字段。
- 不增加生成文件引用字段，不新增跨轮上下文表。
- 旧 `agent` 包装节点、嵌套主图调用和仅服务旧实现的分支被删除。
- 新增或修改函数及回调使用中文用途注释。

## Comments

已实现，等待 review。审查聚焦图拓扑、State 字段和恢复语义。
