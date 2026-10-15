# Issue tracker: Local Markdown

本项目的任务与规格保存在 .scratch/ 中。

## 文件约定

- 每个功能使用独立目录：.scratch/<feature-slug>/。
- 规格保存在 .scratch/<feature-slug>/spec.md。
- 实现任务各占一个文件：.scratch/<feature-slug>/issues/<NN>-<slug>.md。
- 每个功能的任务编号从 01 开始，新增任务沿用已有编号，不合并为一个任务文件。
- 规格与任务顶部使用 Status: 行记录分流状态，取值见 triage-labels.md。
- 评论及讨论追加到文件末尾的 ## Comments 下。

## 发布与读取

- 技能要求“发布到 issue tracker”时，按上述约定写入本地文件。
- 同一规格或任务已有文件时，更新原文件，避免重复创建。
- 技能要求“读取任务”时，读取指定文件；仅有编号时，在对应功能目录中定位。
- 交付时提供文件链接。本地发布不代表已创建远程 Issue。
