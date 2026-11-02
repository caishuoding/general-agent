# 统一 Token 计数

附件与知识库的新文档使用当前管理端 LLM 配置选择分词器。嵌入模型负责向量化，不决定切片计数模型。默认目标 800 Token、完整块容差 200 Token、正文重叠最多 160 Token，原有结构和句子边界策略继续生效。

业务调用统一为：

```python
counter = TokenCounter.for_settings(settings, data_directory)
tokens = counter.count(text)
prefix = counter.prefix(text, budget=800)
remaining = counter.available(settings, messages, tools)
```

每次处理创建快照；新上传会跟随管理端最新 LLM，无需重启。已有切片保存当时的模型与计数指纹，切换 LLM 不单独触发重切；读取时用当前 LLM 核算预算。结构预算、解析或视觉规则变化仍可触发启动升级。人工清空的资料以 `parse_info.index_cleared=true` 标记，启动不从原件恢复；知识库可主动重试生成。

图片自动升级比较独立视觉规则版本；聊天模型或窗口配置变化不会重新描述已有图片。图片缓存仍保留实际模型修订，主动重试时可使用新模型重新生成描述。

## 安装 GLM-5.3-Flash 官方文件

在仓库根目录执行：

```bash
PYTHONPATH=backend .venv/bin/python -m general_agent.infrastructure.tokenizer_setup --directory .local
```

安装器读取 [智谱官方仓库](https://huggingface.co/zai-org/GLM-5.3-Flash/tree/46a050bec13002ad29ee795c16e61cf4fa3c4a9c) 的固定版本 `tokenizer.json`，仅加载数据，不执行下载内容。注册的精确连接为 `https://open.bigmodel.cn/api/paas/v4`／`glm-5.3-flash`。文件为 20,217,442 字节，SHA-256：`19e773648cb4e65de8660ea6365e10acca112d42a854923df93db4a6f333a82d`；离线样例 `你好，世界！` 为 4 Token。

重复安装复用已校验文件，保留其他模型注册；自定义网关可通过 `--base-url` 指定精确地址。文件或官方版本改变时须重新核对来源与固定哈希，不允许套用其他 GLM 模型的分词器。本机官方 CDN 不可达时，从智谱 ModelScope 仓库取得同一内容，并核对上述官方哈希后安装。

资产保存在应用数据目录的 `tokenizers/glm-5.3-flash/`，注册保存在 `tokenizers.json`；容器挂载同一目录，运行时不自动联网下载。

## 切换模型服务商

提供对应模型官方的标准 `tokenizer.json`，在数据目录注册精确的服务地址和模型；相对路径以数据目录为基准：

```json
{
  "models": [
    {
      "base_url": "https://provider.example/v1",
      "model": "exact-model-name",
      "adapter": "huggingface",
      "path": "tokenizers/exact-model/tokenizer.json",
      "sha256": "填写文件实际的64位小写SHA-256",
      "source": "官方文件来源链接"
    }
  ]
}
```

当前官方文件适配器支持 Hugging Face `tokenizer.json`，不限发布该文件的模型服务商。其他分词格式或官方计数 API 需要在计数模块增加适配器，业务层接口不用改变；尚未接入的格式不声称已支持。

匹配到官方文件时 `method=official_tokenizer`；未配置对应模型时 `method=character_estimate`，按 UTF-8 字节数保守估算。已匹配配置损坏或校验不符会报错，避免悄悄换用其他模型。旧 `MODEL_TOKENIZERS` 显式映射仍优先兼容，但只有模型名、没有服务地址隔离，推荐改用注册文件。`DOCUMENT_CHUNK_TOKENIZER_MODEL` 仅作为显式覆盖保留；默认不设置，跟随回答 LLM。

普通文本计数不自动添加 BOS、EOS、聊天模板、图片或协议包装。`available` 对文本序列化计数并预留协议空间，现有工具循环结合真实 `usage` 校正读取预算；整轮请求和计费用量仍以模型返回为准。

智谱图片消息不把 Base64 传输内容当成文字计数。输入预算按每图 8,192 个计数单位预留（依据该模型官方 `processor_config.json` 的默认每图最多 8,000 视觉 Token，另留协议余量）；这属于预算上界估算，不是 API 返回的实际用量。文档阅读仍结合最近真实 `prompt_tokens` 进一步限制可用空间；缺少真实视觉用量时不假定还有文档预算。摘要只接收图片旁的文字与重新读取提示，不把 Base64 编码当作历史正文。
