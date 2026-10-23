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


def list_answers(keyword="", limit=100, offset=0):
    """列出缓存题目，按最近命中时间倒序。

    给控制台的「缓存题目」列表用。关键词同时匹配题干和答案——
    经常是「我记得答案里有共同富裕」这种模糊记忆，只搜题干找不到。

    返回 (总条数, 当前页列表)。总条数是过滤后的总数，不是表的总数，
    这样"共 N 道"在搜索时显示的是搜索结果数，不是全库数。
    """
    kw = (keyword or "").strip()
    where, args = "", ()
    if kw:
        # LIKE 里的 % 和 _ 是通配符，用户输入里带的要转义，否则搜 "50%" 会命中一切
        esc = kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where = "WHERE title LIKE ? ESCAPE '\\' OR answer LIKE ? ESCAPE '\\'"
        args = (f"%{esc}%", f"%{esc}%")

    with _lock, _db() as c:
        total = c.execute(f"SELECT COUNT(*) FROM answers {where}", args).fetchone()[0]
        rows = c.execute(
            f"""SELECT title, answer, hits, updated FROM answers {where}
                ORDER BY updated DESC LIMIT ? OFFSET ?""",
            args + (int(limit), int(offset))
        ).fetchall()

    items = [{"title": t, "answer": a, "hits": h, "updated": u}
             for t, a, h, u in rows]
    return total, items


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
