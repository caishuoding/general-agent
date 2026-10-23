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

## 安装当前 DeepSeek 官方文件

在仓库根目录执行：

```bash
PYTHONPATH=backend .venv/bin/python -m general_agent.infrastructure.tokenizer_setup --directory .local
```

安装器使用 [DeepSeek 官方 Token 用量文档](https://api-docs.deepseek.com/quick_start/token_usage/) 当前链接的 [V4 离线包](https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip)，只读取其中 `tokenizer.json`，不执行下载代码；按固定 SHA-256 验证并原子注册 `https://api.deepseek.com` 的 `deepseek-flash`。重复执行会复用已校验文件，保留其他模型注册。通过代理使用同一模型时，可明确加 `--base-url` 注册实际连接地址。

本次核验的 tokenizer.json：6,367,096 字节，SHA-256 `89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614`。离线样例 `你好，世界！` 为 4 Token。文件或官方包后续变化时校验失败，需要核实新来源并更新固定校验值。

资产保存在应用数据目录的 `tokenizers/`，注册保存在 `tokenizers.json`；容器使用相同数据目录挂载。运行时不自动联网下载分词文件。

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
