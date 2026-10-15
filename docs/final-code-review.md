# 最终独立代码审查

固定基线：`df47109`；审查提交：`2592284499d47965d48aac8a6be050e897287132`。两轴由独立代理并行检查。下列为修复前发现；修复结果另见 [最终审查修复验证](verification-final-review.md)。

## Standards

审查范围：`git diff df47109...2592284499d47965d48aac8a6be050e897287132`。本报告为静态 Standards 审查，未调用云服务或运行应用测试。

- **硬规范，P3：测试中的 SQL 函数缺用途注释。** 用户的全局注释要求及 [spec.md:155](https://github.com/caishuoding/general-agent/blob/2592284499d47965d48aac8a6be050e897287132/.scratch/general-agent/spec.md#L155) 明确覆盖所有新增函数。新增 `CREATE FUNCTION … RETURNS trigger` 定义未附中文用途说明：`tests/test_mem0_adapter.py:82`、`tests/test_memory_deletion.py:120,156`、`tests/test_rebuild.py:218,225`、`tests/test_memory_correction.py:116`、`tests/test_source_deletion.py:96`、`tests/test_memory_recall.py:118`。例如 [test_rebuild.py:218](https://github.com/caishuoding/general-agent/blob/2592284499d47965d48aac8a6be050e897287132/tests/test_rebuild.py#L218) 的 `CREATE FUNCTION reject_rebuild_progress()` 只有执行正文；外层测试的 docstring 没有逐个说明这些嵌套 SQL 函数。应在各定义前说明故障注入用途。650 个 Python 函数的中文 docstring 扫描及前端函数/回调检查未发现同类遗漏。

- **设计判断，possible Duplicated Code：回答材料编排重复。** [chat.py:50](https://github.com/caishuoding/general-agent/blob/2592284499d47965d48aac8a6be050e897287132/backend/general_agent/chat.py#L50) 与 [streaming.py:121](https://github.com/caishuoding/general-agent/blob/2592284499d47965d48aac8a6be050e897287132/backend/general_agent/streaming.py#L121) 都从 `history_material = self.execution.history.prepare(...)` 开始，重复搜索优先级、附件准备、记忆准备和全文准备整段分支。调整任一回答路由须同步修改两处。建议将这段共享准备行为收进现有 `ChatExecution`，同步和流式层保留各自传输与持久化差异。这是维护性判断，不是硬性违反规范。

- **设计判断，possible Message Chains：来源删除依赖内部对象路径。** [source_deletion.py:34](https://github.com/caishuoding/general-agent/blob/2592284499d47965d48aac8a6be050e897287132/backend/general_agent/source_deletion.py#L34)、48、62 使用 `self.memory.memory.embedding.spaces.local_settings()` / `.lease(maintenance=True)`，并在36行直接打开 SDK。来源删除需要了解删除服务、记忆服务、嵌入配置、空间管理的整条对象结构；任一层调整都会波及它。建议由记忆边界提供“核验可保留事实”和“维护租约”行为，隐藏导航路径。该建议不要求更换 Mem0 或 PostgreSQL。

结论：1 类低优先级注释违规（8 处 SQL 定义），2 项非阻塞设计判断；未发现其他已核实的文档规范硬违反。

## Spec

审查范围：`git diff df47109...2592284499d47965d48aac8a6be050e897287132`，18 项任务及 84 条用户故事。

1. **[P2] 实现错误：当前选择或具名文件可能完全未进入问答材料。** [attachments.py:183](https://github.com/caishuoding/general-agent/blob/2592284499d47965d48aac8a6be050e897287132/backend/general_agent/attachments.py#L183) 对会话所有已发送文字文件统一按匹配数、上传时间排序并截取 12 块，没有优先或限定本条选择、具名文件。公开 API 复现：先发送由 `OLD_CODE=RED.\n` 重复 2000 次构成的 `old.txt`，再上传并仅选择内容为 `NEW_CODE=GREEN.` 的 `latest.txt`，提问“latest.txt 的代号是什么？”。模型实际收到的 12 个片段全部来自 `old.txt`，新文件正文完全缺失。因此不能可靠回答当前指定资料的问题。规格故事 56 原句：“我希望针对资料中的具体问题获得有依据的回答，以便快速定位相关内容。”应先解析当前所选或具名文件范围，再在该范围检索。

2. **[P2] 实现错误：普通联网查询被泛称误判为附件请求。** [search.py:138](https://github.com/caishuoding/general-agent/blob/2592284499d47965d48aac8a6be050e897287132/backend/general_agent/search.py#L138) 仅凭“文件、资料、图片”等词强制准备附件材料。已配置模型及搜索、没有上传文件时，发送“联网搜索：北京旅游资料”，返回 502 和“请先上传并随消息发送”；实际搜索调用为 0。规格故事 67 原句：“我希望明确要求时联网查询信息，以便获取模型已有知识之外的资料。”应区分查询主题中的普通词与对当前附件的实际引用，允许公开查询直接搜索。

未发现独立的整项功能缺失或额外范围。两项均已通过真实 PostgreSQL 随机测试库和 HTTP 替身复现；[证据](verification-final-review.md)。未调用真实云服务，用户已接受的真实服务待验不计为缺陷。

Standards：1 类 P3 硬规范问题（8处）及2项非阻塞设计判断；Spec：2项问题，最高P2。

## 父级补充验证

Standards 审查者另提示附件删除后刷新会话的迟到响应可能覆盖新选会话，已交统一实现者复现并修复；不混入两轴原报告计数。

## 处置结果

上述两轴问题和设计判断均由同一实现者在独立工作树处理，提交 `c131bc3`，合并 `ea8de48`；额外的会话切换竞争也已通过延迟真实 HTTP 响应复现并修复。父级随后将另一个已有外层说明的 SQL 故障函数注释贴近其定义，提交 `54e1601`，合并 `21a25a7`。详见 [修复与红绿证据](verification-final-review.md)，整合版最终检查见 [整体交付验证](verification-final.md)。

完整浏览器检查随后发现早期测试的全局状态提示选择器存在歧义，仅修正测试定位（`b117579`，合并 `9f68126`），未修改产品行为；最终结果见 [整体交付验证](verification-final.md)。
