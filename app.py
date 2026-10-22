"""OCS 网课助手 · 自建 AI 题库（精简版）

只用 Python 标准库，无需安装任何第三方包。

功能：
  - 对接 OCS 网课助手的题库接口，自动答选择/判断/填空题
  - 答案缓存在本地 SQLite，同题不重复花钱
  - 自带一个网页控制台，可查看统计、手动测试

用法：
  1. 复制 .env.example 为 .env，填入 DEEPSEEK_API_KEY
  2. 双击 启动.bat（或 python app.py）
  3. OCS 脚本设置里，题库地址填 http://127.0.0.1:8000/query

协议说明（与完整版一致）：
  请求  GET/POST  title=题目 & options=选项 & type=题型 & token=令牌
  响应  {"code":1,"data":{"question":"题目","answer":"答案"},"message":"请求成功"}
        code=0 表示未答上，OCS 会回退到其他题库，不会中断
"""

import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent


def _load_env():
    """读取 .env，支持 注释(#)/空行/KEY=VALUE。"""
    cfg = {}
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return cfg
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


_env = _load_env()


def _cfg(key, default=""):
    # 环境变量优先，方便临时覆盖
    return os.environ.get(key) or _env.get(key) or default


API_KEY = _cfg("DEEPSEEK_API_KEY")
MODEL = _cfg("DEEPSEEK_MODEL", "deepseek-chat")
BASE_URL = _cfg("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
PORT = int(_cfg("PORT", "8000") or 8000)
TOKEN = _cfg("API_TOKEN")
DB_PATH = BASE_DIR / "cache.db"
# OCS 题型 -> 中文名
TYPES = {"single": "单选题", "multiple": "多选题", "judgement": "判断题",
         "completion": "填空题", "unknown": "未知题型"}
# DeepSeek 偶尔返回数字题型码
NUM_TYPES = {1: "single", 2: "multiple", 3: "judgement", 4: "completion"}

SYSTEM_PROMPT = """你是一个答题助手，负责回答网课题目。请严格按以下要求输出：

1. 【最重要】如果题目给出了选项，你的答案必须是某个选项的原文照抄，禁止同义改写、禁止概括、禁止自创表述。
   例如选项是「爱因斯坦的相对论」，即使你认为答案只是「相对论」，也必须输出「爱因斯坦的相对论」。
   这样做的原因是接收方会拿你的答案与选项做字符串比对，改写会导致匹配失败。

2. 只输出答案本身，不要任何解释、前缀、标点或多余文字。
3. 单选题：输出正确选项的完整文字（含选项里的所有修饰成分）。
4. 多选题：所有正确选项的完整文字，用 # 连接（例如"选项一#选项三"）。
5. 判断题：只输出 正确 或 错误。
6. 填空题：多个空的答案用 # 连接，按空出现的顺序。
7. 如果无法确定，给出你认为最可能的一个答案，不要输出"不确定"。

注意：你必须从给定的选项中选择，不能自创选项。"""

# ---------------------------------------------------------------------------
# 缓存（SQLite）
# ---------------------------------------------------------------------------

_lock = threading.Lock()


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
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


def _bump(key, n=1):
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
                  (h, title, answer, MODEL, now, now))


def get_stats():
    with _lock, _db() as c:
        st = dict(c.execute("SELECT k,v FROM stats").fetchall())
        st["cached"] = c.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
        return st


# ---------------------------------------------------------------------------
# 调用 DeepSeek（urllib，零依赖）
# ---------------------------------------------------------------------------

def _post_chat(payload, timeout=90):
    """POST 到 OpenAI 兼容接口，返回解析后的 JSON。"""
    url = f"{BASE_URL}/chat/completions"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
        # 强制直连，避免本机代理软件拦截 localhost/API 请求
        "User-Agent": "ocs-tiku/1.0",
    })
    # 关键：清掉代理环境变量，否则公司网络/代理软件会把请求截走
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
              "https_proxy", "all_proxy"):
        os.environ.pop(k, None)

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _build_prompt(title, options, qtype):
    parts = [f"【题型】{TYPES.get(qtype, '未知题型')}", f"【题目】{title.strip()}"]
    if options and options.strip():
        parts.append("【选项】（答案必须从下面原样照抄，禁止改写或概括）")
        parts.append(options.strip())
    parts.append("【你的答案】")
    return "\n".join(parts)


def _clean(raw, qtype):
    """清理模型输出，去掉常见废话。"""
    ans = (raw or "").strip()
    ans = re.sub(r"^[`*\"'\s]+|[`*\"'\s]+$", "", ans)          # 去 markdown/引号
    ans = re.sub(r"^(最终)?答案(是|为)?[:：]?\s*", "", ans)      # 去"答案是："
    ans = re.sub(r"^(正确选项|选项)(是|为)?[:：]?\s*", "", ans)  # 去"正确选项："
    ans = re.sub(r"^(应)?选(择)?[:：]?\s*", "", ans)            # 去"选"
    ans = ans.strip().strip("。.;；，,")

    if qtype == "judgement":
        yes = re.search(r"(正确|对|是|true|√|✓)", ans, re.I)
        no = re.search(r"(错误|错|否|false|×|✗)", ans, re.I)
        if yes and not no:
            return "正确"
        if no:
            return "错误"

    if "#" in ans:
        ans = re.sub(r"\s*[;；,，]\s*", "#", ans)
    ans = ans.splitlines()[0].strip()
    return ans[:500]


def _norm_for_match(s):
    """与 OCS 的 clearString/normalizeString 对齐的归一化。

    OCS 在匹配前会去掉标点空白并转小写，只保留中英数字，
    所以这里做同样处理，确保我们的判断与它的实际行为一致。
    """
    s = (s or "").strip().lower()
    s = s.replace("，", "").replace("。", "")
    s = re.sub(r"[\s\u3000]+", "", s)
    s = re.sub(r"[^\w\u4e00-\u9fff]", "", s)
    return s


def _similarity(a, b):
    """Dice 系数（二元组重合度），与 string-similarity 的思路一致。

    OCS 用 npm 包 string-similarity，其核心就是比较两个字符串的
    二元组（bigram）重合比例。这里复现同样量纲，用于自检能否过 0.6 阈值。
    """
    a, b = _norm_for_match(a), _norm_for_match(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    def bigrams(s):
        return {s[i:i + 2] for i in range(len(s) - 1)} or {s}

    ba, bb = bigrams(a), bigrams(b)
    inter = len(ba & bb)
    return 2.0 * inter / (len(ba) + len(bb))


def _align_answer_to_options(answer, options):
    """把模型答案改造成 OCS 更容易匹配上的形态。

    OCS resolveSingle 的匹配规则（源码 resolveSingle / answerNormalizedMatch）：
      阶段1 归一化后 答案===选项，或 答案.includes(选项) —— 注意方向是"答案包含选项"
      阶段2 相似度 > 0.6
      阶段3 答案是单个 A/B/C 字母

    常见失败场景：选项是「爱因斯坦的相对论」，模型答「相对论」。
    此时 答案⊉选项，相似度约 0.33 < 0.6，OCS 直接丢弃。

    本函数按优先级尝试：
      1. 答案已能精确匹配某选项          -> 原样返回
      2. 答案是某选项的子串              -> 返回该选项原文（补全上下文）
      3. 相似度已 > 0.6                  -> 原样返回
      4. 能找到包含答案的选项且整体不太长 -> 返回选项原文
      5. 都不行                          -> 返回选项标号（走 OCS 阶段3兜底）
    """
    if not answer or not options:
        return answer

    # options 可能是 "A. 甲\nB. 乙" 这样的整串，也可能是已拆好的列表，统一成列表
    raw_list = options if isinstance(options, (list, tuple)) else str(options).splitlines()

    # 拆出选项正文与标号
    opt_texts, opt_labels = [], []
    seq = 0  # 无标号选项按出现顺序补 A/B/C
    for raw in raw_list:
        line = (raw or "").strip()
        if not line:
            continue
        m = re.match(r"^\s*([A-Za-z])\s*[.、．:：)）]\s*(.+)$", line)
        if m:
            opt_labels.append(m.group(1).upper())
            opt_texts.append(m.group(2).strip())
        else:
            opt_labels.append(chr(ord("A") + seq))
            opt_texts.append(line)
        seq += 1

    if not opt_texts:
        return answer

    ans_n = _norm_for_match(answer)

    # 1. 已能精确匹配 -> 不用动
    for t in opt_texts:
        if _norm_for_match(t) == ans_n:
            return answer

    # 2. 答案是某选项的子串 -> 返回选项原文（补全上下文，满足"答案⊇选项"）
    subs = [(i, t) for i, t in enumerate(opt_texts) if ans_n and ans_n in _norm_for_match(t)]
    if len(subs) == 1:
        return subs[0][1]

    # 3. 相似度已过阈值 -> 原样返回
    best = max((_similarity(answer, t) for t in opt_texts), default=0)
    if best > 0.6:
        return answer

    # 4. 找包含答案的选项，且选项不算太长（避免选到一个泛泛的长选项）
    cands = [(i, t) for i, t in enumerate(opt_texts)
             if ans_n and ans_n in _norm_for_match(t) and len(t) <= len(answer) * 4 + 12]
    if cands:
        # 取最短的那个，最贴近答案本身
        cands.sort(key=lambda x: len(x[1]))
        return cands[0][1]

    # 5. 兜底：返回标号，让 OCS 走阶段3（纯字母答案）
    # 只有当模型答案与某选项有部分重合时才这么做，否则宁可保持原样
    for i, t in enumerate(opt_texts):
        tn = _norm_for_match(t)
        if ans_n and tn and (ans_n in tn or tn in ans_n):
            return opt_labels[i]

    # 6. 最后兜底：答案与所有选项字面都不沾边（模型做了同义改写/概括）。
    # 此时任何返回都无法让 OCS 的阶段1/2 命中，但返回"字符重合度最高"的选项
    # 至少还有机会：OCS 阶段2 用 Dice 系数，部分重合仍可能 > 0.6。
    # 若最高相似度也极低，则保持原答案，让 OCS 自行跳过（总比乱选好）。
    scored = [(i, _similarity(answer, t)) for i, t in enumerate(opt_texts)]
    if scored:
        scored.sort(key=lambda x: -x[1])
        bi, bs = scored[0]
        if bs > 0.25:
            return opt_texts[bi]

    return answer


def ask(title, options, qtype):
    """调大模型答题，返回清洗后的答案字符串。"""
    if not API_KEY:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY，请编辑 .env 文件")

    r = _post_chat({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(title, options, qtype)},
        ],
        "temperature": 0.1,
        "max_tokens": 512,
    })
    content = r["choices"][0]["message"].get("content") or ""
    ans = _clean(content, qtype)

    # 把答案对齐到选项形态，提升 OCS 侧的匹配成功率
    if qtype in ("single", "multiple", "judgement") and options:
        parts = [p.strip() for p in ans.split("#") if p.strip()]
        if parts:
            aligned = [_align_answer_to_options(p, options) for p in parts]
            ans = "#".join(aligned)

    return ans


# ---------------------------------------------------------------------------
# 核心：处理一次查询
# ---------------------------------------------------------------------------

# 典型非题目文本特征：课件 PPT 正文、章节说明、提示语。
# OCS 的元素选择器在某些页面会抓到这些内容并当作题目传给题库，
# 服务端若不识别就会拿着大段课件去问模型，既浪费额度又答非所问。
_NON_QUESTION_HINTS = [
    "本章", "本节", "教学目标", "学习目标", "教学重点", "教学难点",
    "知识点", "课件", "演示", "如下图所示", "见图", "见表",
    "请同学们", "大家看", "接下来", "首先", "然后", "最后",
    "谢谢观看", "谢谢大家", "thank you", "thanks",
    "目录", "前言", "绪论", "引言", "概述", "小结", "总结",
    "思考题", "练习题", "讨论题",   # 这些虽近似题目但页面标题区常作为栏目标题出现
    "主讲人", "讲师", "作者简介", "学校", "学院", "专业",
    "点击", "下载", "上传", "登录", "注册", "首页", "上一页", "下一页",
]

# 填空题的合法结尾特征。
# 注意要包含中文句号「。」：题目常写成「资本主义生产的实质是___。」
_COMPLETION_TAILS = ("___", "……", "......", "。", "．", ".", "？", "?", ":", "：")


def _looks_like_question(title, qtype, options=""):
    """判断一段文本是否像题目。

    OCS 会原样透传页面选择的标题文本（answerer-env.ts 的 env.title = params.title），
    遇到课件页/说明页时会把大段 PPT 正文当题目传过来。这里在服务端拦一道，
    避免拿课件去问模型——既省钱也不会给出莫名其妙的答案。
    """
    t = (title or "").strip()
    if not t:
        return False, "题目为空"

    # 选择题/判断题必须带选项，否则几乎可以断定是误抓的页面元素
    # （如目录项「4.1 条件语句」、章节标题等）
    opt_lines = [x for x in (options or "").splitlines() if x.strip()]
    if qtype in ("single", "judgement", "multiple") and len(opt_lines) < 2:
        return False, "选择/判断题缺少选项，疑似误抓的页面元素"

    # 超长文本：真实题目通常很短，几十字以上基本是正文段落
    if len(t) > 120:
        return False, f"文本过长({len(t)}字)，疑似课件正文"

    # 多行文本：题目一般是单行
    lines = [x for x in t.splitlines() if x.strip()]
    if len(lines) > 3:
        return False, f"行数过多({len(lines)}行)，疑似课件正文"

    # 命中课件/说明类关键词
    low = t.lower()
    for kw in _NON_QUESTION_HINTS:
        if kw in low:
            return False, f"命中非题目关键词「{kw}」"

    # 填空题必须以空或标点收尾；选择/判断题不强制
    if qtype == "completion":
        if not any(t.rstrip().endswith(m) for m in _COMPLETION_TAILS):
            return False, "填空题格式不符"

    return True, ""


def handle_query(title, options, qtype_raw, token):
    """返回 (http状态码, 响应dict)。"""
    if TOKEN and token != TOKEN:
        return 401, {"code": 0, "data": None, "message": "token 无效"}

    title = (title or "").strip()
    if not title:
        return 200, {"code": 0, "data": None, "message": "题目为空"}

    qtype = (qtype_raw or "").strip().lower()
    if qtype.isdigit():
        qtype = NUM_TYPES.get(int(qtype), "unknown")
    if qtype not in TYPES:
        qtype = "unknown"

    _bump("queries")

    # 0. 非题目拦截：OCS 在课件页/说明页会把大段正文当题目传过来，
    #    这里先挡掉，避免浪费额度又答非所问。返回 code=0 让 OCS 回退其他题库。
    ok, why = _looks_like_question(title, qtype, options)
    if not ok:
        _bump("skipped")
        return 200, {"code": 0, "data": None,
                     "message": f"非题目内容，已跳过（{why}）"}

    h = qhash_of(title, options, qtype)

    # 1. 缓存
    hit = cache_get(h)
    if hit:
        _bump("cache_hits")
        return 200, {"code": 1, "data": {"question": title, "answer": hit},
                     "message": "缓存命中"}

    # 2. 大模型
    try:
        answer = ask(title, options, qtype)
    except Exception as e:
        _bump("errors")
        return 200, {"code": 0, "data": None, "message": f"模型调用失败: {e}"}

    if not answer:
        _bump("errors")
        return 200, {"code": 0, "data": None, "message": "模型未返回有效答案"}

    cache_put(h, title, answer)
    _bump("api_calls")
    return 200, {"code": 1, "data": {"question": title, "answer": answer},
                 "message": "请求成功"}


# ---------------------------------------------------------------------------
# 网页控制台
# ---------------------------------------------------------------------------

PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OCS 自建 AI 题库</title><style>
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;
max-width:600px;margin:0 auto;padding:56px 20px 80px;background:#fafafa;color:#1a1a1a;
line-height:1.65;-webkit-font-smoothing:antialiased}
h1{font-size:19px;font-weight:600;margin:0;letter-spacing:-.2px}
.sub{color:#8a8a8a;font-size:13px;margin:6px 0 0}
header{display:flex;align-items:center;justify-content:space-between;
padding-bottom:20px;border-bottom:1px solid #ebebeb;margin-bottom:32px}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:7px;
vertical-align:1px}
.dot.on{background:#22c55e}.dot.off{background:#d1d5db}
.st{font-size:12px;color:#8a8a8a}
section{margin-bottom:36px}
h2{font-size:12px;font-weight:600;color:#8a8a8a;text-transform:uppercase;
letter-spacing:.8px;margin:0 0 14px}
.row{display:flex;gap:12px}
.stat{flex:1;background:#fff;border:1px solid #ebebeb;border-radius:10px;padding:14px 16px}
.stat .k{font-size:11px;color:#a0a0a0;margin-bottom:5px}
.stat .v{font-size:21px;font-weight:600;letter-spacing:-.5px}
.box{background:#fff;border:1px solid #ebebeb;border-radius:10px;padding:18px}
label{display:block;font-size:13px;color:#555;margin:0 0 6px;font-weight:500}
input,textarea,select{width:100%;background:#fff;color:#1a1a1a;
border:1px solid #e0e0e0;border-radius:8px;padding:9px 11px;font-size:14px;
font-family:inherit;transition:border-color .15s}
input:focus,textarea:focus,select:focus{outline:none;border-color:#1a1a1a}
textarea{min-height:84px;resize:vertical;line-height:1.6}
button{background:#1a1a1a;color:#fff;border:0;border-radius:8px;
padding:9px 18px;font-size:13px;font-weight:500;cursor:pointer;
transition:opacity .15s}
button:hover{opacity:.82}
button:disabled{opacity:.4;cursor:default}
button.ghost{background:#fff;color:#1a1a1a;border:1px solid #e0e0e0}
.mt{margin-top:14px}
.hint{font-size:12px;color:#a0a0a0;margin:8px 0 0;line-height:1.6}
.hint a{color:#555}
code{background:#f0f0f0;padding:2px 6px;border-radius:5px;font-size:12.5px;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.copy{display:flex;align-items:center;gap:8px;margin-top:4px}
.copy code{flex:1;padding:8px 10px}
#out{margin-top:14px;padding:13px 15px;border-radius:8px;background:#f5f5f5;
font-size:14px;white-space:pre-wrap;line-height:1.6}
#out.err{background:#fef2f2;color:#991b1b}
#out.good{background:#f0fdf4;color:#166534}
.msg{font-size:13px;margin:12px 0 0;padding:10px 12px;border-radius:7px;display:none}
.msg.ok{background:#f0fdf4;color:#166534;display:block}
.msg.no{background:#fef2f2;color:#991b1b;display:block}
footer{color:#b0b0b0;font-size:12px;text-align:center;padding-top:28px;
border-top:1px solid #ebebeb}
</style></head><body>

<header>
  <div>
    <h1>OCS 自建 AI 题库</h1>
    <div class="sub">自建题库 · 替代 OCS 默认收费题库</div>
  </div>
  <div class="st"><span class="dot __DOTCLASS__"></span>__STATUS__</div>
</header>

<section>
  <h2>运行数据</h2>
  <div class="row">
    <div class="stat"><div class="k">累计查询</div><div class="v">__QUERIES__</div></div>
    <div class="stat"><div class="k">缓存命中</div><div class="v">__HITS__</div></div>
    <div class="stat"><div class="k">实际调用</div><div class="v">__CALLS__</div></div>
  </div>
  <div class="row mt">
    <div class="stat"><div class="k">已缓存题目</div><div class="v">__CACHED__</div></div>
    <div class="stat"><div class="k">非题目跳过</div><div class="v">__SKIPPED__</div></div>
    <div class="stat"><div class="k">失败次数</div><div class="v">__ERRORS__</div></div>
  </div>
  <div class="row mt">
    <div class="stat"><div class="k">当前模型</div><div class="v" style="font-size:14px;padding-top:4px">__MODEL__</div></div>
    <div class="stat"><div class="k">服务端口</div><div class="v" style="font-size:14px;padding-top:4px">__PORT__</div></div>
    <div class="stat"><div class="k">接口令牌</div><div class="v" style="font-size:14px;padding-top:4px">__TOKENSTATE__</div></div>
  </div>
</section>

<section>
  <h2>API 设置</h2>
  <div class="box">
    <label>DeepSeek API Key</label>
    <input id="key" type="password" placeholder="sk-..." value="__MASKEDKEY__"
           autocomplete="off" spellcheck="false">
    <div class="hint" id="keyhint">__KEYHINT__</div>
    <div class="mt"><button onclick="saveKey()">保存并测试</button></div>
    <div class="msg" id="kmsg"></div>
    <div class="hint" style="margin-top:14px">
      <a href="javascript:adv()" id="advlink">高级选项</a>
    </div>
    <div id="adv" style="display:none;margin-top:12px">
      <label>API 地址</label>
      <input id="base" value="__BASEURL__" spellcheck="false">
      <div class="hint">一般无需修改。使用本地模型（Ollama/LM Studio）或第三方中转时才改。</div>
      <label class="mt">访问令牌（可选）</label>
      <input id="tk" value="__TOKENVAL__" placeholder="留空表示不启用" spellcheck="false">
      <div class="hint">设置后，OCS 题库配置里也要填同一个值。防止同局域网他人盗用额度。</div>
    </div>
  </div>
</section>

<section>
  <h2>接入 OCS</h2>
  <div class="box">
    <label>题库地址</label>
    <div class="copy">
      <code id="url">http://127.0.0.1:__PORT__/query</code>
      <button class="ghost" onclick="cp('url')">复制</button>
    </div>
    __TOKENHINT__
    <div class="hint">在 OCS 网课助手的设置中，把上述地址填入题库配置。</div>

    <label class="mt">题库配置（JSON）</label>
    <div class="copy">
      <code id="cfg" style="white-space:pre;overflow-x:auto">__OCSCONFIG__</code>
      <button class="ghost" onclick="cp('cfg')">复制</button>
    </div>
    <div class="hint">
      在 OCS 设置 → 题库配置 → 「题库配置填写/修改区」中粘贴上面这段 JSON，然后保存配置。<br>
      必须用 <code>GM_xmlhttpRequest</code> 请求类型，否则浏览器同源策略会拦截本地请求。
    </div>
  </div>
</section>
</section>

<section>
  <h2>手动测试</h2>
  <div class="box">
    <label>题型</label>
    <select id="t">
      <option value="single">单选题</option>
      <option value="multiple">多选题</option>
      <option value="judgement">判断题</option>
      <option value="completion">填空题</option>
    </select>
    <label class="mt">题目</label>
    <textarea id="q" placeholder="例如：鸦片战争发生在哪一年？"></textarea>
    <label class="mt">选项（每行一个，判断题和填空题可留空）</label>
    <textarea id="o" placeholder="A. 1840年&#10;B. 1842年&#10;C. 1839年"></textarea>
    <div class="mt"><button onclick="go()">提交测试</button></div>
    <div id="out"></div>
  </div>
</section>

<footer>答案缓存于本地 SQLite · 同题不重复计费</footer>

<script>
function cp(id){
  const t=document.getElementById(id).textContent;
  navigator.clipboard.writeText(t).then(()=>{
    const b=event.target, o=b.textContent;
    b.textContent='已复制';setTimeout(()=>b.textContent=o,1200);
  });
}

function adv(){
  const d=document.getElementById('adv');
  const open=d.style.display==='none';
  d.style.display=open?'block':'none';
  document.getElementById('advlink').textContent=open?'收起高级选项':'高级选项';
}

async function saveKey(){
  const inp=document.getElementById('key'), msg=document.getElementById('kmsg');
  const v=inp.value.trim();
  if(!v){ msg.className='msg no'; msg.textContent='请输入 API Key'; return; }
  const b=event.target; b.disabled=true; b.textContent='保存中...';
  msg.className='msg';
  const payload={api_key:v};
  const advOpen=document.getElementById('adv').style.display!=='none';
  if(advOpen){
    payload.base_url=document.getElementById('base').value.trim();
    payload.token=document.getElementById('tk').value.trim();
  }
  try{
    const r=await fetch('/api/config',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload)});
    const j=await r.json();
    if(j.code===1){
      msg.className='msg ok';
      msg.textContent=j.tested
        ? '已保存，并测试通过。可以开始用了。'
        : '已保存。注意：连通性测试未通过，请检查 Key 或 API 地址是否正确。';
      if(j.tested) setTimeout(()=>location.reload(),1500);
    }else{
      msg.className='msg no'; msg.textContent='保存失败：'+j.message;
    }
  }catch(e){ msg.className='msg no'; msg.textContent='请求失败：'+e; }
  finally{ b.disabled=false; b.textContent='保存并测试'; }
}

async function go(){
  const out=document.getElementById('out');
  out.className=''; out.textContent='思考中...';
  const p=new URLSearchParams({title:document.getElementById('q').value,
    options:document.getElementById('o').value, type:document.getElementById('t').value});
  try{
    const r=await fetch('/query?'+p);
    const j=await r.json();
    if(j.code===1){ out.className='good'; out.textContent='答案：'+j.data.answer; }
    else{ out.className='err'; out.textContent='未答上：'+j.message; }
  }catch(e){ out.className='err'; out.textContent='请求失败：'+e; }
}
</script>
</body></html>"""


def _mask_key(k):
    """脱敏显示 Key，只留首尾各几位。"""
    if not k:
        return ""
    if len(k) <= 10:
        return k[:3] + "***"
    return k[:6] + "***" + k[-4:]


def ocs_config_json():
    """生成可直接粘贴到 OCS「题库配置填写/修改区」的 JSON。

    要点：
      - type 用 GM_xmlhttpRequest，否则浏览器同源策略会拦截 127.0.0.1 的请求
      - data 用 ${title}/${options}/${type} 占位符，由 OCS 替换成真实题目
      - handler 解析本服务返回的 {code,data:{question,answer}} 结构
      - 多选答案已由服务端用 # 连接，符合 OCS 对多选题的格式要求
    """
    # token 直接拼进 URL 查询串。
    # OCS 的 resolvePlaceHolder 只替换 ${xxx} 占位符，静态值原样保留，
    # 所以这样写能正确传过去；且比放在 data 里更直观，不易被误改。
    url = f"http://127.0.0.1:{PORT}/query"
    if TOKEN:
        url += "?token=" + TOKEN

    cfg = [{
        "url": url,
        "name": "本地AI题库",
        "method": "get",
        # OCS 源码 answer.wrapper.handler.ts:154 实际读的是 responseType，
        # contentType 只是历史遗留的兜底字段。这里显式写 responseType 更稳妥。
        "responseType": "json",
        "type": "GM_xmlhttpRequest",
        "data": {
            "title": "${title}",
            "options": "${options}",
            "type": "${type}",
        },
        "handler": "return (res)=> res.code === 1 ? [res.data.question, res.data.answer] : undefined",
    }]
    return json.dumps(cfg, ensure_ascii=False, indent=2)


def render_page():
    s = get_stats()
    if API_KEY:
        dot, status = "on", "已就绪"
        keyhint = ('当前已配置 <code>%s</code>，留空则保持不变。'
                   % _mask_key(API_KEY))
    else:
        dot, status = "off", "未配置 API Key"
        keyhint = ('还没有 Key。去 <a href="https://platform.deepseek.com" '
                   'target="_blank">platform.deepseek.com</a> 注册充值后，'
                   '把 Key 粘贴到这里保存即可。')

    tokenhint = ""
    if TOKEN:
        tokenhint = ('<label class="mt">访问令牌</label><div class="copy">'
                     '<code id="tk">%s</code>'
                     '<button class="ghost" onclick="cp(\'tk\')">复制</button>'
                     '</div>' % TOKEN)

    # 注意：局部变量名不要叫 html，会和 html 模块冲突
    page = (PAGE
            .replace("__DOTCLASS__", dot)
            .replace("__STATUS__", status)
            .replace("__MODEL__", MODEL)
            .replace("__QUERIES__", str(s.get("queries", 0)))
            .replace("__HITS__", str(s.get("cache_hits", 0)))
            .replace("__CALLS__", str(s.get("api_calls", 0)))
            .replace("__CACHED__", str(s.get("cached", 0)))
            .replace("__SKIPPED__", str(s.get("skipped", 0)))
            .replace("__ERRORS__", str(s.get("errors", 0)))
            .replace("__PORT__", str(PORT))
            .replace("__TOKENSTATE__", "已启用" if TOKEN else "未启用")
            .replace("__MASKEDKEY__", _mask_key(API_KEY))
            .replace("__KEYHINT__", keyhint)
            .replace("__TOKENHINT__", tokenhint)
            .replace("__BASEURL__", BASE_URL)
            .replace("__TOKENVAL__", TOKEN)
            # JSON 配置要 HTML 转义，否则 < > & 会破坏页面结构
            .replace("__OCSCONFIG__", html.escape(ocs_config_json())))
    return page.encode("utf-8")


# ---------------------------------------------------------------------------
# 配置热更新（网页上直接填 Key）
# ---------------------------------------------------------------------------

def save_config(api_key=None, token=None, base_url=None):
    """把配置写回 .env，保持其它行不变。

    返回 (成功与否, 消息, 是否测试通过)。
    """
    global API_KEY, TOKEN, BASE_URL

    env_file = BASE_DIR / ".env"
    lines = []
    if env_file.exists():
        lines = env_file.read_text(encoding="utf-8").splitlines()

    def _set(key, value):
        """在行列表里设置 key=value，不存在则追加。"""
        pat = re.compile(r"^\s*%s\s*=" % re.escape(key))
        for i, ln in enumerate(lines):
            if pat.match(ln):
                lines[i] = f"{key}={value}"
                return
        lines.append(f"{key}={value}")

    if api_key is not None:
        _set("DEEPSEEK_API_KEY", api_key)
    if token is not None:
        _set("API_TOKEN", token)
    if base_url is not None:
        _set("DEEPSEEK_BASE_URL", base_url)

    try:
        # 先备份，写坏了还能救回来
        if env_file.exists():
            bak = BASE_DIR / ".env.bak"
            bak.write_text(env_file.read_text(encoding="utf-8"), encoding="utf-8")
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        return False, f"写入 .env 失败: {e}", False

    # 立即生效，无需重启
    if api_key is not None:
        API_KEY = api_key
    if token is not None:
        TOKEN = token
    if base_url is not None:
        BASE_URL = base_url.rstrip("/")

    # 顺手测一下新 Key 能不能用
    tested = False
    if api_key:
        try:
            ask("测试题", "A.甲\nB.乙", "single")
            tested = True
        except Exception:
            pass

    return True, "已保存", tested


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    def _send(self, code, obj, ctype="application/json; charset=utf-8"):
        body = obj if isinstance(obj, bytes) else \
            json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _query_params(self):
        u = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(u.query).items()}
        if self.command == "POST" and "application/json" in \
                (self.headers.get("Content-Type") or ""):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                try:
                    params.update(json.loads(self.rfile.read(n).decode("utf-8")))
                except Exception:
                    pass
        return params

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, render_page(), "text/html; charset=utf-8")
        elif path == "/health":
            self._send(200, {"status": "ok", "model": MODEL, "has_key": bool(API_KEY)})
        elif path == "/stats":
            self._send(200, get_stats())
        elif path == "/query":
            p = self._query_params()
            code, body = handle_query(p.get("title", ""), p.get("options", ""),
                                      p.get("type", ""), p.get("token", ""))
            self._send(code, body)
        else:
            self._send(404, {"code": 0, "message": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/query":
            p = self._query_params()
            code, body = handle_query(p.get("title", ""), p.get("options", ""),
                                      p.get("type", ""), p.get("token", ""))
            self._send(code, body)
        elif path == "/api/config":
            self._handle_config()
        else:
            self._send(404, {"code": 0, "message": "not found"})

    def _handle_config(self):
        """网页上保存 Key / 令牌。"""
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 64 * 1024:
            self._send(400, {"code": 0, "message": "请求体异常"})
            return
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            self._send(400, {"code": 0, "message": "JSON 解析失败"})
            return

        key = (data.get("api_key") or "").strip()
        token = (data.get("token") or "").strip()
        base_url = (data.get("base_url") or "").strip()

        # 防呆：Key 有明显格式问题直接拦下
        if key and not re.fullmatch(r"[A-Za-z0-9_\-]{8,128}", key):
            self._send(200, {"code": 0, "message":
                             "Key 格式不对，应是一串字母数字（通常以 sk- 开头），"
                             "且不要加引号"})
            return

        # 防呆：地址必须是 http(s)
        if base_url and not base_url.startswith(("http://", "https://")):
            self._send(200, {"code": 0, "message":
                             "API 地址必须以 http:// 或 https:// 开头"})
            return

        ok, msg, tested = save_config(
            api_key=key if key else None,
            token=token if token else None,
            base_url=base_url if base_url else None,
        )
        if not ok:
            self._send(200, {"code": 0, "message": msg})
        else:
            self._send(200, {"code": 1, "message": msg, "tested": tested})


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def main():
    # 输出重定向到文件/管道时 Python 会缓冲 print，导致启动横幅延迟甚至丢失。
    # 这里强制行缓冲，保证用户立刻看到状态。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    print("=" * 58)
    print("  OCS 自建 AI 题库（精简版 · 零依赖）")
    print("=" * 58)
    if not API_KEY:
        print("  [!] 未配置 DEEPSEEK_API_KEY —— 答题不可用")
        print("      请编辑 .env（参考 .env.example）后重启")
    else:
        print(f"  [OK] 模型      : {MODEL}")
        print(f"  [OK] API 地址  : {BASE_URL}")
    print(f"  [OK] 端口      : {PORT}")
    if TOKEN:
        print("  [OK] 接口令牌  : 已启用")
    else:
        print("  [!] 接口令牌  : 未启用")
        print("      服务监听 0.0.0.0，同局域网他人可盗用额度，建议设置 API_TOKEN")
    print()
    print(f"  控制台       http://127.0.0.1:{PORT}/    （可在此直接填写 API Key）")
    print(f"  OCS 题库地址 http://127.0.0.1:{PORT}/query")
    print()
    print("  按 Ctrl+C 停止")
    print("=" * 58)
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n[已停止]")
    except OSError as e:
        print(f"\n[启动失败] {e}")
        if "10048" in str(e) or "address" in str(e).lower():
            print(f"端口 {PORT} 被占用。请先结束占用该端口的程序，")
            print("或修改 .env 里的 PORT 后重试。")
        input("\n按回车退出...")


if __name__ == "__main__":
    main()
