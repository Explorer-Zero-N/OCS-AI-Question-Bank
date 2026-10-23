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
    for k in ("queries", "cache_hits", "api_calls", "errors", "skipped"):
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
