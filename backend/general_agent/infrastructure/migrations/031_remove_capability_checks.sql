-- 能力预检已移除，删除只保存探针结果的表；其余调用用量仍保留在 model_calls。
DROP TABLE IF EXISTS capability_checks;
