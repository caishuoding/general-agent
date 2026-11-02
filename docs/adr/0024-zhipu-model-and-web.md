# ADR 0024：统一使用智谱聊天、视觉、搜索与网页读取

Status: Accepted

2026-11-02：用户要求全部迁移到智谱，同时保留搜索与读图。本决定替代 ADR 0009、0011、0015、0016 中的具体 DeepSeek 连接和文件身份方案；文档授权、检索与生命周期规则不变。

主智能体、子智能体、上下文压缩、文档图片描述和 Skill 读图均使用全局聊天连接 `https://open.bigmodel.cn/api/paas/v4`／`glm-5.3-flash`。流式工具调用开启 `tool_stream=true`，不关闭模型思考，也不启用需要持久回传私有推理的保留思考模式。计数精确注册该模型官方分词文件及校验值，不复用其他模型分词器。

图片直接使用本地原件构造 `image_url` Data URL；只对发送副本做大小／尺寸适配。本地原件继续按账号、会话、知识库选择和 Skill 允许路径授权。删除远端 Files 上传、续期、重传和删除代码，并通过迁移 070 删除三个 `provider_file` 列；不创建旧协议兼容层。

联网独立配置 `zhipu-web`，调用 `/web_search`（search_pro）和 `/reader`。模型工具仅保留 `search_web(query)` 与 `fetch_web(url)`；删除旧供应商要求的 `ori_question`，也删除没有官方数据来源的相关性分数过滤。来源编号、停止守卫、请求缓存、有限重试和主从独立额度保持。

`credits` 是应用沿用的非空成功请求计数，不表示智谱积分或账单；真实费用以供应商账单为准。

Mem0 为独立官方服务，需要通过 `/configure` 重新注入同一个 GLM 聊天连接。其事实提取及记忆变更判断使用 GLM；记忆向量化与检索仍使用已有 Qwen 嵌入模型，知识库嵌入配置亦保持。嵌入模型与聊天模型有不同用途，不能把聊天模型名填入 embedding 接口。

协议依据：[GLM-5.3-Flash](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash)、[智谱 API 索引](https://docs.bigmodel.cn/llms.txt)、[官方模型仓库](https://huggingface.co/zai-org/GLM-5.3-Flash)。真实验证见 [迁移记录](../verification-zhipu-migration.md)。旧验收文档保留当时事实，不代表当前连接。
