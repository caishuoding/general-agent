# 使用 PostgreSQL 保存业务数据

Status: Partially superseded by ADR 0010 for public deployment and account ownership.

聊天、长期记忆、来源关联与用量记录使用本机 PostgreSQL，附件原文件保存在本机目录。PostgreSQL 是用户明确指定的数据库选择；相较于单文件数据库，这需要独立的数据库服务、连接配置与结构迁移，但保持项目使用指定的数据库栈。数据库通过 Docker Compose 在本机运行并使用持久化存储卷，前后端直接在本机运行；当前仍是个人本机应用。Mem0 内部存储的接入方式与是否安装向量检索扩展需随 Mem0 方案核实。
