"""业务编排：一次查询从进来到出去要经过哪几步。

流程固定，顺序不能换：
    token 校验 -> 空题判断 -> 题型归一化 -> 计数
    -> 非题目拦截 -> 查缓存 -> 调模型 -> 写缓存 -> 返回
"""

from . import config, detector, llm, store


def handle_query(title, options, qtype_raw, token):
    """返回 (http状态码, 响应dict)。"""
    if config.TOKEN and token != config.TOKEN:
        return 401, {"code": 0, "data": None, "message": "token 无效"}

    title = (title or "").strip()
    if not title:
        return 200, {"code": 0, "data": None, "message": "题目为空"}

    qtype = (qtype_raw or "").strip().lower()
    if qtype.isdigit():
        qtype = config.NUM_TYPES.get(int(qtype), "unknown")
    if qtype not in config.TYPES:
        qtype = "unknown"

    store.bump("queries")

    # 0. 非题目拦截：OCS 在课件页/说明页会把大段正文当题目传过来，
    #    这里先挡掉，避免浪费额度又答非所问。返回 code=0 让 OCS 回退其他题库。
    ok, why = detector.looks_like_question(title, qtype, options)
    if not ok:
        store.bump("skipped")
        return 200, {"code": 0, "data": None,
                     "message": f"非题目内容，已跳过（{why}）"}

    h = store.qhash_of(title, options, qtype)

    # 1. 缓存
    hit = store.cache_get(h)
    if hit:
        store.bump("cache_hits")
        return 200, {"code": 1, "data": {"question": title, "answer": hit},
                     "message": "缓存命中"}

    # 2. 大模型
    try:
        answer = llm.ask(title, options, qtype)
    except Exception as e:
        store.bump("errors")
        return 200, {"code": 0, "data": None, "message": f"模型调用失败: {e}"}

    if not answer:
        store.bump("errors")
        return 200, {"code": 0, "data": None, "message": "模型未返回有效答案"}

    store.cache_put(h, title, answer)
    store.bump("api_calls")
    return 200, {"code": 1, "data": {"question": title, "answer": answer},
                 "message": "请求成功"}
