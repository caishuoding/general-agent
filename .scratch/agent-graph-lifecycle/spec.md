Status: ready-for-human

# 单主 Agent 图与清晰生命周期边界

## 1. 背景

当前实现存在两张常驻 LangGraph：

1. 外层 `ConversationGraph`：`prepare_context -> agent -> finalize`。
2. 内层 `AgentToolGraph`：`prepare_model -> call_model <-> execute_tools -> settle_answer -> finish`。

外层的实际职责是回答生命周期和业务收尾，不是 Agent 编排；内层才是 Agent 模型—工具循环。两层本身可以工作，但当前实现把 Conversation、Answer Attempt 和 Agent Run 三种生命周期混在同一个会话级检查点中，产生了以下复杂度：

- 外层使用稳定 `conversation_id` 作为 LangGraph thread，多个回答共享线程。
- 需要 `OwnedSaver`、`checkpoint_threads`、`answer_id` 校验来解决旧回答迟到写入新回答的问题。
- 需要 `history_position`、旧 State 消息与业务消息表的去重合并。
- `ConversationState` 同时保存会话历史和当前回答状态，名字与实际作用不一致。
- 外层只包含线性节点，没有真正的条件路由或 Agent 选择。
- 外层手工调用内层图，父子图关系不透明，两个 State 和两套检查点难以诊断。
- Session、Answer、Run 的业务事实同时存在于业务 PostgreSQL 和 LangGraph checkpoint 中，存在两套真相。
- 生成文件、Skill 状态和上下文摘要等字段没有清晰归属，容易继续堆进错误 State。

ADR 0021 要求回答中断后可显式继续，因此不能简单删除持久化。目标是保留恢复能力，同时把状态边界收敛到可解释的模型。

## 2. 核心决策

采用以下领域模型：

| 概念 | 身份与持久化 | 生命周期 |
| --- | --- | --- |
| Conversation | PostgreSQL 聚合根 | 长期 |
| Message | PostgreSQL 业务记录 | 长期 |
| Generated File | PostgreSQL 业务索引 | 长期 |
| Answer Attempt | 主 Agent 图的 thread | 单次回答 |
| Agent Run | 主或子 Agent 图的 thread | 单次 Agent 执行 |
| Conversation Summary | PostgreSQL 派生上下文 | 随会话和资料来源失效 |

核心原则：

1. LangGraph thread 对应一次可恢复执行，不直接对应 Conversation。
2. 主 Agent 使用一张图完成上下文准备、模型工具循环和最终收尾。
3. 子 Agent 在真正派工时创建独立图实例和独立 thread，不构成固定外层架构。
4. Conversation、Message、Generated File 和 Conversation Summary 的权威数据在 PostgreSQL。
5. 图 State 只保存当前执行所需的工作快照和引用，不复制领域事实。
6. 不把模型凭据、客户端、宿主绝对路径、文件正文或私有推理放入 State。

## 3. 目标图结构

### 3.1 主回答图

主图 thread 使用 `answer:<assistant_message_id>`，同一回答的恢复使用同一 thread，新回答或重试使用新 thread。

```text
START
  |
  v
initialize_run
  |
  v
prepare_context
  |
  v
prepare_model
  |
  v
call_model
  | \
  |  \ 无工具调用
  |   v
  | settle_answer
  |   |
  |   v
  | finalize
  |   |
  |   v
  |  END
  |
  | 有工具调用
  v
execute_tools
  |
  v
collect_inbox? ---- 有子任务结果/活动任务 ----+
  |                                          |
  +----------- 无待收结果 -------------------+
  |
  +--> prepare_model -> call_model
```

节点职责：

- `initialize_run`：幂等创建或恢复根 Agent 与主 Agent Run；校验回答仍处于可执行状态。
- `prepare_context`：读取业务消息、附件清单、知识库范围、记忆、已用 Skill 和生成文件清单；构造本次模型上下文。
- `prepare_model`：按预算压缩旧交互，重建系统提示和当前 Skill 指令，保证工具协议完整。
- `call_model`：真实模型流式调用、断线续写、工具调用累积。
- `execute_tools`：执行业务工具、Skill 工具和子 Agent 编排工具；持久化已完成步骤。
- `collect_inbox`：仅在存在子任务信箱活动时收取结果。
- `settle_answer`：阻止在子任务未结束或制作命令仍运行时提前结束。
- `finalize`：来源核验、知识库引用解析、消息终态持久化、关闭根执行、提交会话摘要。

主 Graph 内部必须只有一套 `StateGraph`。上下文准备和 finalize 不能再由另一张外层 Graph 包裹。

### 3.2 子 Agent 图

子 Agent 使用同一组 Agent 循环节点，但省略 `prepare_context`、`finalize` 和会话级业务收尾。

thread 使用 `run:<agent_run_id>`。

```text
START -> prepare_model -> call_model <-> execute_tools
                         \-> settle_answer -> finish -> END
```

子 Agent 的独立上下文、工具权限、来源集合和运行预算保持不变。补充任务继续创建新的 Agent Run；已有子 Agent 历史从业务表恢复，不通过主图 State 传递。

### 3.3 图实例关系

目标不是“固定两层图”，而是：

```text
一个主 Agent 图实例
    |
    +-- 按需创建 0..N 个子 Agent 图实例
```

主图和子图可以复用节点实现、工具执行器和预算策略，但每个图实例拥有独立 thread 和 State。

## 4. State 所有权

### 4.1 主图 State

建议命名为 `AnswerState`，至少包含：

```python
class AnswerState(TypedDict):
    version: int
    root_run_id: str
    messages: list[dict[str, Any]]
    source_attachment_ids: list[str]
    generated_files: list[GeneratedFileRef]
    used_skills: list[str]
    tool_calls: list[dict[str, Any]]
    round_index: int
    tool_failures: dict[str, int]
    document_calls: dict[str, int]
    used_tools: bool
    answer: str | None
    failure: str | None
    inbox_deliveries: list[str]
    settled: bool
    watching_inbox: bool
    web_sources: list[dict[str, Any]]
    knowledge_source_candidates: list[dict[str, Any]]
    knowledge_sources: list[dict[str, Any]]
```

不允许放入 State：

- 模型 API Key 或其他凭据。
- Provider 客户端、数据库连接、线程池等运行对象。
- Generated File 的宿主绝对路径。
- Generated File 正文。
- Conversation 全量业务消息作为可写副本。
- 私有推理内容。

### 4.2 业务数据库权威字段

以下数据不以图 State 为权威来源：

- Conversation 元数据。
- 用户和助手消息正文、位置、状态。
- Skill 名称集合。
- Generated File 记录及不可变副本。
- Agent、Agent Run、来源、工具请求和用量。
- Conversation Summary 与摘要水位。

每个新回答通过 `prepare_context` 从业务库加载当前有效快照。检查点丢失时仍可以从业务数据重建新执行，而不是让会话永久不可用。

### 4.3 运行上下文

`ConversationContext` 继续作为不可变运行参数，保存：

- `conversation_id`
- `account_id`
- `user_message_id`
- `assistant_message_id`
- `root_run_id`
- `settings`
- `operation`
- `delivery`

它不保存可修改的回答、历史或工具结果。

## 5. 上下文压缩与会话摘要

### 5.1 问题

当前压缩摘要依赖稳定 conversation thread 携带；如果改为 answer thread，新回答只从业务消息重建，会重复总结旧历史。需要单独的 Conversation Summary 存储。

### 5.2 设计

新增 `conversation_contexts`：

```sql
CREATE TABLE conversation_contexts (
    conversation_id UUID PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    summary TEXT NOT NULL DEFAULT '',
    summary_through_position BIGINT NOT NULL DEFAULT 0,
    summary_tokens INTEGER NOT NULL DEFAULT 0,
    source_revision TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

职责：

- `prepare_context` 读取摘要和摘要水位之后的近日志消息。
- 摘要作为派生资料注入，不获得系统指令权限。
- 当前用户请求始终保留，不进入摘要。
- 新摘要提交必须同时推进 `summary_through_position`；失败不改变旧摘要。
- 原始 Message 永不因摘要删除。
- 资料范围变化、附件撤销或记忆删除使关联摘要失效并重新生成。

### 5.3 压缩执行

- 继续复用 `ContextCompactor` 的分组、预算、分批摘要和失败保留策略。
- Graph 只在 `prepare_model` 前压缩工作上下文。
- `finalize` 负责将已确认的新摘要和当前回答水位持久化到 `conversation_contexts`。
- 若摘要写入失败，回答本身仍按明确失败规则处理；不得用未提交摘要生成后续上下文。

## 6. Generated Files 与按需读取

### 6.1 数据库扩展

在 `generated_files` 增加：

```sql
ALTER TABLE generated_files ADD COLUMN workspace_path TEXT;
ALTER TABLE generated_files ADD COLUMN project_path TEXT;
ALTER TABLE generated_files ADD COLUMN size_bytes BIGINT;
```

字段规则：

- `path`：后端内部不可变 artifact 路径，不进入模型上下文。
- `workspace_path`：发布时的 workspace 相对路径。
- `project_path`：可选的可编辑项目目录，如 `projects/demo`。
- `size_bytes`：可选文件大小，便于模型决定是否读取。

历史记录允许 `workspace_path` 和 `project_path` 为空，后续通过 `materialize` 使用不可变副本。

### 6.2 State 清单

`prepare_context` 从 `generated_files` 读取当前会话最近文件，构造精简清单：

```json
[
  {
    "file_id": "uuid",
    "filename": "deck.pptx",
    "media_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "workspace_path": "projects/deck/exports/deck.pptx",
    "project_path": "projects/deck",
    "artifact_url": "/api/conversations/.../generated-files/.../file",
    "created_at": "2026-11-01T..."
  }
]
```

清单是资料，不是系统指令。模型根据用户请求选择是否读取。

### 6.3 读取工具

新增 `read_generated_file`：

输入：

```json
{
  "file_id": "uuid",
  "mode": "metadata | text | materialize"
}
```

行为：

- `metadata`：返回文件元数据。
- `text`：仅对受支持的 UTF-8 文本类型返回有界内容。
- `materialize`：把不可变 artifact 复制到 `inputs/generated/<file_id>/<filename>`，返回相对路径。
- PPTX 不直接读取二进制；materialize 后交给 PPT skill 的 `edit-native-pptx` 或续接流程。
- 所有操作重新校验账号、会话和文件归属。
- 不允许模型传入宿主绝对路径。
- 单次文本返回受现有上下文预算限制。

第一版只在主 Agent 开放该工具；如后续子 Agent 明确需要，再按任务显式授权。

### 6.4 PPT 发布元数据

`publish_skill_file` 增加可选 `project_path`，或在后端按 `projects/<slug>/` 约定安全推断：

- 保存 `workspace_path`。
- 保存可编辑项目目录。
- 返回 `file_id`、`filename`、`relative_path` 和下载 URL。

不直接修改第三方 Skill 的完整工作流；若 Skill 协议允许，只补充最小字段说明。

## 7. Skill 与会话工作区

- 继续使用 `conversation_skills` 保存会话已用 Skill 名称。
- Skill 正文仍按需加载，不自动注入全部历史 Skill 描述。
- `SkillSession` 在每次图执行时由后端重建，不进入 State。
- `used_skills` 只作为当前执行的工作快照。
- 会话 workspace 继续按 `account_id + conversation_id` 持久化。
- `run_skill_command`、`publish_skill_file` 和 `read_generated_file` 的授权继续以当前 Run 与当前回答为准。
- 恢复执行时读取业务数据库和 workspace，不恢复 Python 栈、Docker 容器或模型网络连接。

## 8. 检查点与恢复

### 8.1 Thread 规则

| 图实例 | Thread ID |
| --- | --- |
| 主回答 | `answer:<assistant_message_id>` |
| 子 Agent | `run:<agent_run_id>` |

好处：

- 新回答天然使用新 thread，旧回答不能写入新回答。
- 不需要通过 `answer_id` 比较判断 thread 是否已被新回答复用。
- `history_position` 和旧 State 合并逻辑可以移除。
- `OwnedSaver` 只保留“回答仍生成中、资料未失效、操作未取消”的生命周期校验。

### 8.2 恢复语义

- 服务重启：所有 `generating` 且可恢复的回答转为 `interrupted`，不自动重发模型请求。
- 用户点击继续：校验账号、最新回答、模型配置、资料快照和原操作。
- 有主图检查点：用空输入恢复；已有节点结果复用。
- 无主图检查点：从业务消息重新初始化，创建新的主 Run。
- finalize 已完成：再次恢复必须幂等返回，不重复发模型请求。
- finalize 未完成但模型已完成：恢复只执行 settle/finalize，不重新调用模型。
- 用户停止、回答重试或资料撤销：终止旧 thread 的继续资格。
- 子 Agent 恢复时重新建立调度，已完成子任务结果不重复交付。

### 8.3 版本与旧检查点

本次图拓扑和 thread 语义发生变化，必须提升 `STATE_VERSION`。

旧版外层 conversation 检查点不能安全迁移到新主图。建议部署时：

1. 把所有正在生成或中断的旧回答标记为 `failed` 或 `stopped`，提示用户重试。
2. 清理旧主图 thread 和对应 checkpoint 记录。
3. 保留原始业务消息、生成文件、Agent 用量和来源记录。
4. 新版本回答从新的 answer thread 开始。

不做旧外层图与新主图的双写兼容；双轨会延长两套状态的生命周期，违背本次收敛目标。

## 9. 文件级改造计划

### Phase 1：领域与数据库

- 新增 ADR，记录“会话不是图，回答尝试与 Agent Run 才是图”。
- 新增 `conversation_contexts` 迁移。
- 扩展 `generated_files` 的 `workspace_path`、`project_path`、`size_bytes`。
- 增加生成文件清单与摘要的 repository 方法。
- 增加迁移测试和历史数据兼容测试。

### Phase 2：State 与 Context

- 新增 `AnswerState`，替代外层 `ConversationState`。
- 扩展 `ConversationContext` 的 `root_run_id`。
- 把 `prepare_context`、`finalize` 的业务逻辑收回到 `ChatExecution` 或新的 `AnswerWorkflow` 边界。
- 移除 `history_position`、旧 State 消息合并和会话 thread 推断。
- 加入 `generated_files` 清单和 Conversation Summary 注入。

### Phase 3：单主图

- 把主图节点组合为一个 StateGraph。
- 复用当前工具循环节点实现，避免复制 `call_model`、`execute_tools`、`collect_inbox` 和 `settle_answer`。
- 主图增加 `prepare_context`、`initialize_run`、`finalize`。
- 子 Agent 继续使用同一 Agent 循环节点，但使用独立 Run State 和 `run:<run_id>` thread。
- 保留同步和流式共用入口。
- 移除 `ConversationGraph` 的嵌套调用路径。

### Phase 4：检查点切换

- 主图 thread 改为 `answer:<answer_id>`。
- `OwnedSaver` 简化为生命周期与资料有效性校验。
- 更新 `checkpoint_threads` 注册规则。
- 增加新旧版本检查点切换和旧回答终止策略。
- 更新启动恢复、继续执行、停止和重试逻辑。

### Phase 5：Generated Files 与 Skill 接入

- `publish_skill_file` 写入相对路径和可选项目路径。
- `prepare_context` 注入文件清单。
- 新增 `read_generated_file`。
- 更新 PPT 后续编辑说明：先读取清单，再 materialize 或继续已有项目。
- 增加同会话跨轮 PPT 修改测试。

### Phase 6：清理

- 删除外层 `ConversationGraph` 和对应 State 类型。
- 删除仅服务于稳定会话 thread 的合并、归属和重复状态逻辑。
- 删除不再使用的 checkpoint thread 类型。
- 更新 ADR、GLOSSARY、计划文档和验证记录。
- 运行类型检查、目标测试和完整回归。

## 10. 验收标准

### 架构验收

- 主回答只由一张 LangGraph 图执行。
- 子 Agent 是按需创建的独立图实例，不是固定第二层。
- Conversation 不再作为 LangGraph thread。
- 主图 thread 与 assistant answer 一一对应。
- Graph State 不包含凭据、宿主路径、文件正文或私有推理。

### 功能验收

- 同一回答在模型、工具、收尾任一步骤中断后可继续。
- 继续执行不重复已完成的模型和工具步骤。
- finalize 重试不会重复保存助手消息。
- 子 Agent 并行、等待、收信和恢复行为不变。
- Skill workspace 跨轮可继续使用。
- PPT 生成后下一轮可从生成文件清单发现并按需加载。
- 生成文件不能跨账号、跨会话访问。
- 上下文摘要跨轮复用，且原始消息完整保留。
- 资料撤销后旧摘要和检查点不能继续提供已撤销内容。

### 测试验收

- 主图节点与路由测试。
- answer thread 隔离测试。
- finalize-only 恢复测试。
- 服务重启显式继续测试。
- 上下文压缩跨轮复用测试。
- 子 Agent 恢复与结果去重测试。
- Skill workspace 与 generated file 测试。
- 迁移、旧版本检查和权限测试。
- 全量 mypy、后端测试、前端构建和真实 PPT Docker 闭环。

## 11. 风险与取舍

### 风险 1：旧检查点无法直接续接

图拓扑变化后旧外层 thread 无法在新主图恢复。接受一次升级中断，要求旧回答重试，换取长期状态模型收敛。

### 风险 2：跨轮摘要需要新的持久化

不再依赖 conversation thread 后，摘要必须有业务存储，否则每轮重复压缩。新增 `conversation_contexts` 是必要成本。

### 风险 3：重构范围大

主图合并会触及 Chat、Streaming、Agent Runtime、Skill、Checkpoint、多 Agent 和测试。应分阶段提交，先完成数据库与状态边界，再切图，不把生成文件功能作为前置依赖。

### 风险 4：过度抽象

不建立通用 Workflow Framework。只建立一个主 Agent 图和一个子 Agent 图工厂，复用现有节点实现，避免为了“优雅”再引入第三套编排层。

## 12. 待确认项

1. 是否接受本次升级时终止旧进行中回答并要求重试。
2. Conversation Summary 是否采用新表，还是复用现有派生摘要记录。
3. Generated File 的 `project_path` 由工具显式传入还是后端按目录约定推断。
4. 是否需要为子 Agent 开放 `read_generated_file`，还是第一版仅主 Agent 可用。

## Comments

2026-11-01：单图重构已实现，等待人工复核图拓扑、State 字段用途和恢复语义。详见 `issues/02-post-implementation-review.md`。

## 13. 本轮聚焦实施方案（2026-11-01）

本节针对“把一次回答完整编排为一张图”的请求，作为实施时的优先依据；与前文冲突处以本节为准。用户随后明确要求 State 保存生成文件引用并支持下一轮继续操作，该部分按第 14 节纳入本次方案；前文其他生成文件扩展不自动成为实施要求。当前仅补充方案，尚未修改运行代码或现有 ADR。

### 13.1 交付边界

- 一次主回答只有一个 StateGraph、一份工作 State、一个 `answer:<answer_id>` 检查点线程。
- 新消息和重试使用新回答身份；继续执行沿用原回答、Run、operation、额度与已用时间。
- 子智能体按需运行独立 `run:<run_id>` 图；主、子图共享节点实现，不在主图节点内再次调用主工具图的 invoke。
- 同步和流式入口调用同一个回答执行模块，只提供不同的输出方式。
- 跨轮会话上下文脱离检查点单独存储；执行检查点和业务事实仍分别管理。

### 13.2 准确拓扑

```text
START -> initialize_run -> prepare_context -> prepare_model -> call_model
                                                           |         |
                                                       有工具      候选回答
                                                           v         v
                                                     execute_tools  settle_answer
                                                           |         |
                            +------------------------------+     需要继续
                            |                              |         v
                        未启用信箱                    已启用信箱  collect_inbox（可等待）
                            |                              v         |
                            +-> prepare_model <------- collect_inbox-+

settle_answer -- 可以提交 --> finalize -- 已提交 --> END
                                  |
                            最终核验发现待处理结果
                                  v
                             collect_inbox
```

`collect_inbox` 收取主智能体结果信箱；显式 `wait_agents` 继续作为模型工具使用。`prepare_model` 每次真实请求前执行预算和压缩；系统提示与 Skill 指令在外发时从可信配置重建。普通业务失败从失败节点路由至 finalize；进程暂停直接退出执行，保留检查点，不进入失败终态。用户停止由现有停止入口负责终态，旧执行不能覆盖它。

### 13.3 模块改造

| 文件/模块 | 目标职责 |
| --- | --- |
| `agent/graph.py` | 将 `AgentToolGraph.run` 内的节点实现抽出；提供共享循环节点及连边装配，不在节点内部编译和调用另一张主图。主、子图分别装配需要的节点。 |
| `agent/conversation.py` | 替换为完整 `AnswerGraph`：装配初始化、上下文、共享循环、统一收尾；移除原 `agent` 包裹节点及双图状态转换。可沿用文件位置，避免仅为重命名扩大变动。 |
| `agent/state.py` | 共享 `AgentState` 加主回答专属 `AnswerState`；完整消息快照继续覆盖写入，不引入追加 reducer。 |
| `agent/runner.py` | 提供权限、工具、Skill、网关和调度的运行绑定；主路径不再自己启动工具图；子执行仍由调度器启动独立工作图。 |
| `conversation/execution.py` | 保留材料准备、引用解析等业务能力；删除仅转发至第二张主图的 `prepare` 调用路径。 |
| 新增 `conversation/context.py` | 读取、提交、失效会话工作上下文；处理消息水位与新增历史拼接。 |
| `conversation/chat.py`、`streaming.py` | 接入 start/resume 两个回答入口；保持现有公开协议。 |
| `infrastructure/checkpoints.py`、`agent_cleanup.py` | 改为回答线程注册与恢复；保留取消、撤销、归属和迟到写入防护，并将会话上下文加入派生材料清理。 |
| `agent/inbox.py`、`repository.py` | 将候选回答检查与最终业务提交的职责理顺，支持收尾重放和根锁内的最终一致性检查。 |

运行绑定不进入检查点。每次 start/resume 重建客户端、SkillSession、工具定义和子任务调度；依赖 root_run_id 的绑定从 State/业务库按需取得。不能让恢复依赖 initialize_run 或 prepare_context 上一次执行留下的 Python 对象。

当前闭包中的图片去重集合、动态轮数、读取预算等也要审计：需要跨节点续接的标识保存至工作 State，预算和工具集合按已保存状态重新计算，不能在重启后静默重置。

### 13.4 状态和跨轮上下文

`AnswerState` 承接 AgentState 的消息、轮次、工具调用、失败计数、Skill、信箱交付标识；增加 version、answer_id、root_run_id、source_snapshot、source_attachment_ids、history_through_position、answer、failure、引用候选及最终引用。root_run_id 是 initialize_run 的幂等业务创建结果，不要求调用方提前猜测或创建另一份 Run。

运行上下文保留账号、会话、用户消息、回答消息、配置、operation 和传输方式；可提供运行绑定工厂，不携带可变工作消息。State 不保存客户端、密钥和私有推理；模型实际需要的有界工具正文仍可出现在工作 messages，不能把“禁止另存全量文件”误写成“工具正文一律不得进入 State”。

跨轮上下文采用 `conversation_contexts`，替代前文仅存 summary 字符串的设计：

| 字段 | 含义 |
| --- | --- |
| conversation_id | 会话主键，删除会话时级联删除 |
| version | 上下文格式版本 |
| through_position | 已合入工作消息的业务消息水位，与摘要覆盖范围不同 |
| messages | 有界工作快照：摘要及保留的完整交互，不保存系统指令和当轮刷新材料 |
| source_snapshot | 附件/知识库来源版本快照，复用现有校验语义 |
| answer_id | 提交该快照的回答，供幂等检查和诊断 |
| updated_at | 更新时间 |

只存摘要与一个最大位置不够：ContextCompactor 会保留当前请求、近期交互及完整工具组，并不保证所有较早内容都被连续压缩。直接以最大位置截断历史会漏消息。保存有界工作快照可以延续现有摘要和工具结果，无须重新总结已压缩历史。

新回答读取有效快照，再拼接 through_position 之后、当前用户消息位置之前及当前用户消息本身的 complete 消息；按消息 ID 去重。失效或格式不兼容时从业务历史重建。快照水位只在回答成功提交时推进，不把失败草稿当作已完成回答。新回答重新加载记忆和材料，重新加载系统与 Skill 指令。记忆删除/纠正、附件或知识库撤销及会话删除同时使相应上下文失效，不能只删除 LangGraph 表。

### 13.5 收尾与恢复的提交规则

1. initialize_run 复用 `AgentRepository.start` 的幂等行为。即使根 Run 已落库而首次检查点未提交，恢复也必须返回同一 Run；修正前文“无检查点就新建 Run”的描述。
2. settle_answer 检查后台命令、活动子任务和未读结果。需要继续时收信再进入模型；可以交付时生成候选结果，不能提前把执行永久关闭而阻止 finalize 重放。
3. finalize 在根锁内复核活动任务和未读事件。若仍需处理，返回继续路由；否则在同一业务事务内更新消息正文/引用/终态、保存会话上下文、完成主 Run、关闭根执行及落实需要的业务清理。需让 repository 方法复用传入连接，避免每个方法另开事务。长时间外部调用不放在该事务里。
4. finalize 业务提交后、图检查点提交前进程退出时，以数据库中的终态为准；再次进入幂等读取已提交结果，不重新调用模型、不覆盖 stopped、不让旧回答推进较新上下文水位。资料失效校验、最终提交和撤销操作遵守一致的锁顺序。
5. 模型已完成但对应图检查点未提交时，复用原 Run 的业务步骤缓存；完整工具调用与工具返回继续用稳定调用身份去重。实际外发后未落缓存的模型请求可能需要重发，不能承诺外部调用 exactly-once；每次真实请求仍计量。未知结果的命令核验状态，不盲目重执行。
6. 图 State 未持久化的流式正文仍可能在 messages 中；失败出口保留数据库中已输出的草稿，不能仅靠 State.answer 覆盖为旧值。正常新模型轮次采用现有草稿替换语义。
7. 子任务调度在每次执行/续接入口按业务记录重建，不能只放在可能被检查点跳过的初始化节点；已结束 Run 不重复排队，信箱按已有交付标识去重。

### 13.6 检查点切换与 ADR

本方案会重议 ADR 0017 的固定外层流程，以及 ADR 0021 的稳定会话 thread；保留其显式续接、独立业务存储、预算压缩和副作用核验要求。实施时补充新 ADR 并标注替代范围，当前不先改成已接受。

- 提升 State 格式版本，主图使用 `answer:<answer_id>`，子图保持 `run:<run_id>`。
- 保留 checkpoint_threads 的会话/回答映射与 OwnedSaver 有效性防护；独立 thread 只能消除跨回答复用问题，不能代替权限、停止、撤销和晚到写入检查。
- 新格式不猜测旧拓扑的执行位置。建议切换前让旧执行完成；遗留中断回答保留原业务记录并提示旧版本需重试。是否接受失去旧执行的原位续接资格，需作为实施前的升级取舍确认，不自动批量改成 failed/stopped 或删除用户数据。
- 有效已完成旧会话的最新工作快照可转换为 conversation_contexts；必须核对 committed answer、格式和资料快照。转换失败只回退业务历史，不影响原始消息。
- 运行中的旧模型请求不能在新版认领后继续写入；切换需要停止旧执行进程并按既有归属校验拒绝迟到结果。

### 13.7 提交顺序和验收

1. 提取共享循环节点和运行绑定，先保持现有行为，验证主/子工具循环。
2. 增加会话上下文存储、失效和快照构造，覆盖摘要/完整工具组/水位；此阶段不形成长期双写架构。
3. 装配完整 AnswerGraph，一次切换主执行路径、thread、start/resume/stop、finalize 事务；关联旧版切换策略和新 ADR。
4. 删除旧嵌套主图调用、旧 State 转换及稳定会话检查点历史拼接；更新测试和文档。

以上为开发提交顺序，不要求中间提交分别作为可部署版本上线。每个阶段做目标检查，最终完成 mypy 和后端回归；公开前端协议保持兼容时无需加入 PPT/Docker 制作闭环作为本次架构验收。

必须覆盖的故障窗口：根 Run 创建后首次检查点前、模型缓存后检查点前、工具副作用提交后检查点前、子任务结束/交付与主图恢复交错、候选回答后 finalize 前、finalize 业务提交后图提交前。还需验证重复继续、长离线不消耗额度时限、停止及资料撤销后的晚到写入、新回答/重试隔离、跨轮压缩消息不丢不重复、同步/流式结果一致。

架构验收直接检查：主执行没有“节点内启动另一张主图”的路径；一个回答只注册一个主图 thread；子任务独立；所有新增或修改函数（含回调）具备中文用途注释。

## 14. 已确认补充：State 中的生成文件引用

> 2026-11-01 后续决定：本节不实施。PPT Master 已以原项目 SVG、设计说明、素材和会话工作区支持后续修改，不把生成文件或项目引用加入 State。

用户要求将会话生成的文件引用保存在 State，使下一轮主智能体能够发现文件并继续修改或操作。本节扩展第 13 节范围；不要求先完成整套独立生成文件管理功能。

### 14.1 字段与含义

在 AnswerState 增加 `generated_files: list[GeneratedFileRef]`，空会话为 `[]`，按 file_id 去重。每项至少包含：

```json
{
  "file_id": "真实发布记录 UUID",
  "filename": "report.pptx",
  "media_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  "workspace_path": "projects/report/exports/report.pptx",
  "project_path": "projects/report",
  "digest": "已发布副本的真实 SHA-256",
  "created_by_run_id": "产出该版本的 Run UUID",
  "created_at": "实际发布时间"
}
```

workspace_path 是会话工作区相对路径，project_path 是可选的可编辑项目目录。没有可靠项目元数据时允许为空，不能从最终文件名捏造项目目录。下载地址可由 file_id 构造，不作为文件身份。State 不保存文件字节或宿主绝对路径。

文件引用指向已发布的不可变版本；workspace_path 指向可能继续变化的工作文件。两者不能视为同一个不可变对象。继续编辑并发布会得到新的 file_id，保留旧记录；同名发布不按 filename 合并或覆盖。

### 14.2 写入与重启

1. publish_skill_file 先保存真实文件副本和 generated_files 业务记录，返回完整引用元数据。
2. execute_tools 从可信工具结果提取生成文件引用，合并入 State.generated_files，再提交节点检查点。模型在正文里声称生成了文件不能创建引用。
3. 发布已成功但节点检查点未提交时，复用原稳定发布身份，通过业务记录补齐引用；不得再次生成重复版本。
4. 同一回答继续执行时，从检查点还原 generated_files，并校验归属/文件有效性；不重新执行已完成的发布。

现有 SkillService.publish 已按 file_id 幂等，但幂等返回分支也必须补齐新增元数据，不能首次返回和重放返回不同的引用结构。generated_files 表需以增量迁移记录 workspace_path 和可选 project_path；复用已有 digest、created_by_run_id、created_at。历史记录允许新路径字段为空。

### 14.3 下一轮可见

下一轮回答使用新的 answer thread，不会自动继承上一条 thread 的字段。prepare_context 必须从当前账号/会话的 generated_files 业务记录加载引用，填入新 State.generated_files；文件发现不依赖上一轮是否成功 finalize，已成功发布的文件即使回答中断也可恢复发现。

State 字段不会自动发送给模型。模型输入装配必须明确把引用清单投影为带标记的资料消息：说明文件名、file_id、类型、发布时间及可编辑位置，使模型知道怎样选择和读取；不提升为系统指令。每次外发使用当前清单的单份投影，不能反复追加相同清单；恢复和跨轮保存时剔除旧投影并重新生成。

工作消息压缩不以删除 generated_files 元数据作为压缩手段。文件清单也计入真实输入预算；文件较多时按预算注入当前请求明确引用的文件及近期条目，保留按 file_id/名称查询或分页列出的能力，不能因现有 list_files 的 LIMIT 100 让旧文件永久不可发现。实现时扩展现有列表能力即可，不向模型直接注入全部历史文件内容。

### 14.4 继续操作的实际路径

- 工作区及项目仍存在：检查对应目录/文件，按用户指定版本和项目继续编辑，复用现有 read_work_file、run_skill_command 等工具。
- 需要操作指定已发布版本：核验 digest；工作文件已变更时，不能假称工作路径仍是该旧版本。
- 工作路径缺失或历史记录没有路径：扩展现有 import_skill_source，增加 `kind=generated`，按 file_id 验证当前账号和会话，并校验不可变副本摘要，复制到受控的 workspace 输入目录后操作。无需为同一复制行为另造 read_generated_file 工具。
- PPT 等二进制文件依照已加载 Skill 的读取/编辑流程处理。若只剩交付文件、项目源文件缺失，明确区分“编辑已有交付文件”和“恢复原始制作项目”，不能承诺从单个成品恢复全部源文件。
- 发布修改结果后，把新的引用合并入 State；旧副本保持可访问。workspace_path 仅为定位提示，所有读写仍通过现有授权和安全路径解析。

### 14.5 实施与验证

在第 13 节第二步加入元数据增量迁移和 SkillService 列表/发布结果扩展，在第三步加入 State 字段、工具结果合并及模型输入投影，并补充 generated 文件导入分支。

至少验收：第一轮生成、第二轮仅以自然语言指代该文件继续修改；清除相关历史工具消息或触发压缩后仍可发现；重启续接保留引用；发布后检查点前崩溃不重复发布；同名多个版本和指定旧版操作正确；工作区路径缺失时能导入真实副本；历史记录路径为空仍可用；跨账号/跨会话拒绝访问；目录规模超过模型注入范围时旧文件仍可按需发现。

## 15. 最终实施决策（2026-11-01）

本节替代第 13、14 节中与其冲突的内容，作为当前实现基线。

### 15.1 图与线程

主回答使用一张 `StateGraph`，固定线程为 `conversation_id`。同一会话的新消息沿用该线程，官方 checkpointer 加载旧 State；中断继续恢复同一线程。重试创建新的回答尝试，但从用户消息对应的有效执行基线开始，不继承失败尝试的半套工具消息。

主图只有以下业务节点：

```text
prepare_context -> prepare_model -> call_model
                                   | 工具调用 -> execute_tools -> prepare_model
                                   | 无工具调用 -> finalize
finalize -- 尚有子任务/后台命令 --> prepare_model
finalize -- 提交成功或失败终结 --> END
```

- `prepare_context` 初始化本轮业务执行，刷新记忆、附件材料和材料位置，不手动恢复 checkpoint。
- `prepare_model` 自动接收已到达的子任务结果，组织完整模型输入并压缩工作消息。
- `call_model` 发起模型请求并写入模型消息。
- `execute_tools` 执行业务、Skill 和子智能体工具。
- `finalize` 检查子任务、后台命令和活动执行；可交付时核验引用并保存业务终态。不能交付时把已取得的状态交回模型循环。

`collect_inbox` 和 `settle_answer` 不再作为图节点：自动收信并入 `prepare_model`，结束前检查并入 `finalize`。子智能体仍通过工具编排，子图只复用循环节点和运行绑定，拥有独立 Run 状态。

### 15.2 State

主图 State 以现有 `AgentState` 的字段为主，保留 `messages`、`used_skills`、`tool_calls`、`round_index`、`tool_failures`、`document_calls`、`used_tools`、`answer`、`inbox_deliveries`、`watching_inbox` 和 `settled`。再加入 `answer_id`、`failure` 及必要的材料标识。`messages` 保存工作上下文和摘要；系统提示、工具定义、客户端、凭据、数据库连接和 SkillSession 在运行时重建，不写入 State。

不增加生成文件字段，不新增独立跨轮上下文表，不再依赖 `history_position` 手动拼回完整历史。报告、引用和 Run 的事实仍写业务存储。

### 15.3 实现约束

- 主图节点内部不得 `invoke` 另一张主工具图；主、子图通过共享节点工厂装配。
- 同一会话主回答串行执行，旧回答的迟到写入继续受回答归属、停止和资料撤销校验。
- 最终业务提交保持幂等；工具、模型请求和子任务交付继续使用现有业务幂等身份。
- 旧嵌套图专用代码和不再使用的函数、字段转换必须删除。

### 15.4 尚未完成的 review

代码合并完成后仍需人工 review：确认五节点拓扑是否符合最终讨论；逐项核对 State 字段为什么存在、由哪个节点写、恢复时是否必须；检查保留的 `settled`、`watching_inbox`、`inbox_deliveries` 等控制字段能否进一步收敛；检查旧检查点升级和重试基线策略。该事项记录在 [`issues/02-post-implementation-review.md`](issues/02-post-implementation-review.md)。
