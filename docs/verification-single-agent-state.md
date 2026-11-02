# 唯一 Agent 图重构验证

2026-11-02。当前设计见 [ADR 0023](adr/0023-unified-answer-graph.md) 和 [字段说明](agent-state.md)。本记录替代早期验证文档中对双图、旧执行器和旧 State 字段的描述。

## 结构与清理

- 主从均由 `build_agent_graph()` 编译；全后端只有一处 `StateGraph(...)`，包含 prepare_context、prepare_model、call_model、execute_tools、finalize。
- 唯一运行 State 为 AgentState；AgentStateUpdate 仅约束节点增量返回值，不是另一张图的状态。AgentContext 保存固定依赖和身份，AgentRunner 负责创建与恢复执行。
- 删除旧会话图、旧会话 State/Context/Result、旧执行器、旧工具图类和 SkillSession；运行代码与测试不再引用这些名称。删除对应字节码残留与空目录。
- 删除独立回答和工具使用标志、重复根 Run 身份、无调用的准备结果包装和进度写入器。测试改写为验证当前图的行为，保留流式续写、失败次数、文档额度、信箱与异常收尾覆盖。
- loaded_skills 为顶层路径清单。Skill 工具始终注册，普通工具和 Skill 工具共用失败额度与适用的幂等缓存；读取工作区无需加载 Skill，读取 Skill 文件要求已加载，越界与跨会话导入被拒绝。
- 修复新回答残留旧工具轨迹、当前用户身份选择、信箱更新覆盖历史、恢复缺少文档工具、同批文档预算未排除禁用工具、后台命令轮询状态丢失、错误输出误取旧答案等回归。

## 自动验证

- 应用镜像内执行 `pytest -q -o cache_dir=/tmp/refactor-pytest-cache`，通过 Compose 内网直接连接 PostgreSQL：**377 passed，7 skipped**。跳过项由各测试的环境条件控制；有一条 pypdf 弃用提示。
- 最终代码的单图、迁移、流式、主从与 Skill 专项复验：**53 passed**。
- 另外启用 `TEST_SKILL_DOCKER=1` 执行隔离环境与 PPT 导出测试：**2 passed**，覆盖实际容器执行、取消、工作区隔离和原生交付。
- `mypy`：**190 个源文件通过**。
- 所有修改或新增 Python 文件通过 Ruff 未使用符号检查；新增 Agent、chat、Skill 执行模块通过导入顺序检查。新增或修改函数及回调说明注释审计：**缺失 0**。
- 前端 TypeScript 与 Vite 构建通过；Docker 应用镜像构建通过。
- 宿主机完整测试曾出现 PostgreSQL 转发端口连接被意外关闭，后续设置阶段连带失败；容器内直连的完整测试通过。单独连续 2500 次宿主连接通过，尚未确认宿主断连的具体原因。

## 升级与运行检查

- 重启前已创建仅当前用户可读的数据库备份，并用 pg_restore 验证备份目录可解析。
- 迁移 069 已执行：旧图检查点一次性清除，旧未完成执行终结，消息、记忆、附件与生成文件保留。重复迁移不会清除新状态。
- 服务已使用新镜像重启，`/api/health` 返回 ok。
- 从运行容器直接读取 State schema：version、run_id、messages、model_round、loaded_skills、attachments、knowledge_bases、pending_agent_run_ids、tool_failure_counts、document_call_counts、finished、error。
- 运行容器确认 State 版本为 4，五个节点均存在，未加载 Skill 时其工具已注册。
- 应用容器可连接 Docker 管理接口，OCR 健康检查为 200。
