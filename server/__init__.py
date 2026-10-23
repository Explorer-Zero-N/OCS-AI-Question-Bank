"""OCS 自建 AI 题库 · 后端包

分层（依赖单向，只能从上往下 import）：
    api      HTTP 路由 + 静态文件
    service  一次查询的业务编排
    llm / matching / detector / store   各自独立的能力
    config   配置，最底层，谁都依赖它，它不依赖任何人
"""
