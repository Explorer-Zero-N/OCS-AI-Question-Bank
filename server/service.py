"""业务编排：一次查询从进来到出去要经过哪几步。

流程固定，顺序不能换：
    token 校验 -> 计数 -> 空题判断 -> 题型归一化
    -> 非题目拦截 -> 查缓存 -> 调模型 -> 写缓存 -> 返回

另外维护一份「最近请求」的内存快照，用于排查
「OCS 到底有没有把题发过来」这个问题——服务如果把访问日志关掉，
从外面就完全看不见请求有没有到、被哪一条规则挡了。
"""

import threading
import time
from collections import deque

from . import config, detector, llm, store

# 只留最近 20 条，够看清"刚才发生了什么"，又不会涨内存
_recent = deque(maxlen=20)
_recent_lock = threading.Lock()


def _record(qtype, title, outcome, message, elapsed_ms):
    item = {
        "t": time.strftime("%H:%M:%S"),
        "type": qtype,
        "title": " ".join((title or "").split())[:60],
        "outcome": outcome,      # ok / cached / skipped / empty / error / denied
        "message": message,
        "ms": elapsed_ms,
    }
    with _recent_lock:
        _recent.appendleft(item)   # 最新的在最前面


def get_recent():
    with _recent_lock:
        return list(_recent)


def handle_query(title, options, qtype_raw, token):
    """返回 (http状态码, 响应dict)。

    _handle 返回 (状态码, 响应体, 结果类型, 题型)，
    响应体保持和改造前完全一致——不加任何调试字段，OCS 那边不受影响。
    """
    t0 = time.time()
    code, body, outcome, qtype = _handle(title, options, qtype_raw, token)
    _record(qtype, title, outcome, body.get("message", ""),
            int((time.time() - t0) * 1000))
    return code, body


def _handle(title, options, qtype_raw, token):
    if config.TOKEN and token != config.TOKEN:
        return 401, {"code": 0, "data": None, "message": "token 无效"}, "denied", "?"

    qtype = (qtype_raw or "").strip().lower()
    if qtype.isdigit():
        qtype = config.NUM_TYPES.get(int(qtype), "unknown")
    if qtype not in config.TYPES:
        qtype = "unknown"

    # 计数放在最前面。
    # 原来的顺序是先判空题再计数，结果是：如果 OCS 的 ${title} 占位符没生效，
    # 每条请求都返回"题目为空"，统计却一直是 0，看起来就像服务没收到请求。
    # 现在空标题也算一次查询，并单独计入 empty，假死现象不会再出现。
    store.bump("queries")

    title = (title or "").strip()
    if not title:
        store.bump("empty")
        return (200, {"code": 0, "data": None, "message": "题目为空"},
                "empty", qtype)

    # 0. 非题目拦截：OCS 在课件页/说明页会把大段正文当题目传过来，
    #    这里先挡掉，避免浪费额度又答非所问。返回 code=0 让 OCS 回退其他题库。
    ok, why = detector.looks_like_question(title, qtype, options)
    if not ok:
        store.bump("skipped")
        return (200, {"code": 0, "data": None,
                      "message": f"非题目内容，已跳过（{why}）"}, "skipped", qtype)

    h = store.qhash_of(title, options, qtype)

    # 1. 缓存
    hit = store.cache_get(h)
    if hit:
        store.bump("cache_hits")
        return (200, {"code": 1, "data": {"question": title, "answer": hit},
                      "message": "缓存命中"}, "cached", qtype)

    # 2. 大模型
    try:
        answer = llm.ask(title, options, qtype)
    except Exception as e:
        store.bump("errors")
        return (200, {"code": 0, "data": None, "message": f"模型调用失败: {e}"},
                "error", qtype)

    if not answer:
        store.bump("errors")
        return (200, {"code": 0, "data": None, "message": "模型未返回有效答案"},
                "error", qtype)

    store.cache_put(h, title, answer)
    store.bump("api_calls")
    return (200, {"code": 1, "data": {"question": title, "answer": answer},
                  "message": "请求成功"}, "ok", qtype)
