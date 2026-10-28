"""集中保存发送给模型的工具描述；工具参数与执行权限由各工具模块定义。"""

TOOL_DESCRIPTIONS = {
    'get_current_time': '读取当前北京时间和星期；无需参数。',
    'add_memory': '当前用户表达了值得长期保存的稳定事实、偏好或明确要求记住时调用；从真实会话自动提取，无需编写记忆正文。',
    'search_memories': '查找当前用户的长期记忆；更新或删除前先查找。空 query 列出最多 50 条。',
    'update_memory': '按当前用户要求更新已查找到的记忆，使用返回的 memory_id 和 version；目标不明确时先询问。',
    'delete_memory': '按当前用户要求删除已查找到的记忆，使用返回的 memory_id 和 version；不删除原聊天。',
    'search_web': '搜索最新公开信息或核验事实。query 用简洁关键词和必要的 site:/after:/before: 等操作符；ori_question 写当前用户完整搜索意图，超长时压缩到 128 字。只引用实际返回的 [W编号]。',
    'fetch_web': '读取指定 HTTP(S) 网页正文；仅引用实际返回的 [W编号]。抓取可能不完整。',
    'search_documents': '检索当前会话文档，返回完整匹配切片、原文位置和可选 image_id；可限定 document_ids。每轮最多五次。',
    'read_document': '按页读取 PDF／Word 或按段落读取 TXT／Markdown；省略范围顺序读取全部切片。超限时沿原范围使用 next_cursor 续读；每轮最多五次。',
    'search_knowledge': '检索当前会话绑定的知识库；只传 query。返回带 [K编号] 的切片和图片/表格类型；每轮最多五次。',
    'spawn_agent': '创建一个边界清晰、可独立验收的子任务，立即返回 ID 并在后台执行。多个独立任务可在同一轮分别调用。task 写目标，context 写必要背景，expected_output 写交付要求；instructions 仅补充方法。',
    'wait_agents': '零参数等待任一子任务的新结果；已有结果立即交付，无活动任务返回 idle。结果也会在模型轮次间自动交付。',
    'send_agent_message': '给已完成的子智能体追加任务并延续其上下文；运行中返回 agent_busy。instructions 可调整下一轮方法。',
    'cancel_agent': '取消不再需要的活动子任务；已发出的请求仍可能产生用量。',
}
