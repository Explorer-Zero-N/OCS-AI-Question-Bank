"""存储层：答案缓存 + 运行统计，都用 SQLite。

两张表：
  answers  题目指纹 -> 答案，重复题目不再花钱
  stats    几个计数器，给控制台看

题目指纹的算法不能改：改了等于让所有历史缓存失效。
"""

import hashlib
import re
import sqlite3
import threading
import time
from pathlib import Path

from . import config

_lock = threading.Lock()


def _db():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.execute("""CREATE TABLE IF NOT EXISTS answers(
        qhash TEXT PRIMARY KEY, title TEXT NOT NULL, answer TEXT NOT NULL,
        model TEXT, hits INTEGER NOT NULL DEFAULT 0,
        created INTEGER NOT NULL, updated INTEGER NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS stats(
        k TEXT PRIMARY KEY, v INTEGER NOT NULL DEFAULT 0)""")
    for k in ("queries", "cache_hits", "api_calls", "errors", "skipped", "empty"):
        conn.execute("INSERT OR IGNORE INTO stats(k,v) VALUES (?,0)", (k,))
    conn.commit()
    return conn


def bump(key, n=1):
    """计数器 +n。"""
    with _lock, _db() as c:
        c.execute("UPDATE stats SET v=v+? WHERE k=?", (n, key))


def _norm(text):
    """归一化文本，避免同题因空白/标点差异导致缓存 miss。"""
    t = (text or "").strip().lower()
    t = re.sub(r"[\s\u3000]+", "", t)
    return t.translate(str.maketrans("", "", "，。！？；：、（）【】《》""''"))


def qhash_of(title, options, qtype):
    raw = f"{qtype}\n{_norm(title)}\n{_norm(options)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cache_get(h):
    with _lock, _db() as c:
        row = c.execute("SELECT answer FROM answers WHERE qhash=?", (h,)).fetchone()
        if row:
            c.execute("UPDATE answers SET hits=hits+1, updated=? WHERE qhash=?",
                      (int(time.time()), h))
            return row[0]
    return None


def cache_put(h, title, answer):
    now = int(time.time())
    with _lock, _db() as c:
        c.execute("""INSERT INTO answers(qhash,title,answer,model,hits,created,updated)
                     VALUES(?,?,?,?,0,?,?)
                     ON CONFLICT(qhash) DO UPDATE SET
                       answer=excluded.answer, model=excluded.model,
                       updated=excluded.updated""",
                  (h, title, answer, config.MODEL, now, now))


def get_stats():
    with _lock, _db() as c:
        st = dict(c.execute("SELECT k,v FROM stats").fetchall())
        st["cached"] = c.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
        return st


# 允许的排序。做成白名单而不是把前端传来的字符串直接拼进 SQL：
# ORDER BY 后面没法用参数占位符，只能拼字符串，不校验就是注入口子。
_SORTS = {
    "updated": "updated DESC",
    "hits": "hits DESC, updated DESC",
    "short": "LENGTH(title) ASC, updated DESC",
}


def list_answers(keyword="", limit=100, offset=0, sort="updated", only_unused=False):
    """列出缓存题目。

    参数：
      keyword     关键词，同时匹配题干和答案
      sort        updated（最近更新）/ hits（命中最多）/ short（题干最短）
      only_unused 只看命中次数为 0 的

    「题干最短」不是给用户浏览用的，是排查工具：
    排出来一串十几个字的答案，多半是模型答得太潦草或者被截断了。

    返回 (过滤后条数, 当前页列表)。过滤后条数和全库条数是两回事，
    页面上「共 N 道」显示前者，清空按钮要用后者，所以两个都返回。
    """
    kw = (keyword or "").strip()
    conds, args = [], []
    if kw:
        # LIKE 里的 % 和 _ 是通配符，用户输入里带的要转义，否则搜 "50%" 会命中一切
        esc = kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conds.append("(title LIKE ? ESCAPE '\\' OR answer LIKE ? ESCAPE '\\')")
        args += [f"%{esc}%", f"%{esc}%"]
    if only_unused:
        # hits 是"被复用次数"。0 表示存下来之后从来没被第二道题命中过，
        # 这类多半是课件误抓或者答案有问题，正好拿来清
        conds.append("hits = 0")

    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    order = _SORTS.get(sort, _SORTS["updated"])

    with _lock, _db() as c:
        total = c.execute(f"SELECT COUNT(*) FROM answers {where}", tuple(args)).fetchone()[0]
        all_total = c.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
        rows = c.execute(
            f"""SELECT qhash, title, answer, hits, updated FROM answers {where}
                ORDER BY {order} LIMIT ? OFFSET ?""",
            tuple(args) + (int(limit), int(offset))
        ).fetchall()

    items = [{"qhash": h, "title": t, "answer": a, "hits": n, "updated": u}
             for h, t, a, n, u in rows]
    return total, all_total, items


def delete_answer(qhash):
    """删一条缓存，返回实际删掉的条数（0 表示这条本来就不存在）。"""
    if not qhash:
        return 0
    with _lock, _db() as c:
        n = c.execute("DELETE FROM answers WHERE qhash=?", (qhash,)).rowcount
    return n


def clear_answers():
    """清空全部答案缓存，返回删掉的条数。

    只动 answers 表，stats 里的累计查询、调用次数这些历史计数保留——
    清缓存不代表之前没查过题，把计数一起抹掉会让排查失去依据。
    """
    with _lock, _db() as c:
        n = c.execute("DELETE FROM answers").rowcount
    return n


def db_info():
    """数据库文件本身的信息。

    页面要显示它，是为了解决一个反复踩的坑：
    项目里可能存在多个 cache.db（换目录、旧副本），看到"0"时
    先确认你看的是不是服务真正在用的那个文件。
    """
    p = Path(config.DB_PATH)
    exists = p.exists()
    return {
        "path": str(p),
        "exists": exists,
        "size": p.stat().st_size if exists else 0,
        "mtime": int(p.stat().st_mtime) if exists else 0,
    }
