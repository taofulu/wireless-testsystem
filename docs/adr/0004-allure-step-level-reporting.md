# Allure 实现步骤级报告，断言点映射回文本步骤

生成代码时用模板把每个测试步骤渲染为独立的 Allure step（含断言），执行后解析 Allure 报告，将 pass/fail 精确映射回原始文本用例的第 N 步展示。

**Considered Options**: 用例级 pass/fail 摘要——粒度太粗，无法回答"哪一步挂了"；自定义 marker + 日志解析——重复造轮子，Allure 已有成熟报告与 UI。

**Consequences**: AW pytest 框架需统一 Allure 集成约定；步骤级映射是本系统区别于通用 pytest 平台的核心能力，模板层必须保证 step 标记与结构化步骤一一对应。
