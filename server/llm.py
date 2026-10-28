"""模型调用：拼 prompt、发请求、清洗回复、把答案对齐到选项。

只用标准库 urllib，不引入 openai 之类的 SDK——这是"零依赖"的根基。
"""

import json
import os
import re
import urllib.request

from . import config
from .detector import is_short_answer
from .matching import (align_one, best_option, norm_for_match,
                       options_in_text, split_options, trim_wrap)


def _post_chat(payload, timeout=90):
    """POST 到 OpenAI 兼容接口，返回解析后的 JSON。"""
    url = f"{config.BASE_URL}/chat/completions"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.API_KEY}",
        "User-Agent": "ocs-tiku/1.0",
    })
    # 关键：清掉代理环境变量，否则本机代理软件会把请求截走导致超时。
    # 这是实测踩过的坑——curl 正常、Python 全超时，就是它在作怪。
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
              "https_proxy", "all_proxy"):
        os.environ.pop(k, None)

    # 再显式指定空 ProxyHandler，双重保险
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_prompt(title, options, qtype, long_mode=False, blanks=0):
    tname = "简答题" if long_mode else config.TYPES.get(qtype, "未知题型")
    parts = [f"【题型】{tname}", f"【题目】{title.strip()}"]
    if options and options.strip():
        parts.append("【选项】（答案必须从下面原样照抄，禁止改写或概括）")
        parts.append(options.strip())
    elif long_mode:
        parts.append("【提醒】这道题有多个要点，逐条答完；不要出现 # ; | 这三个符号。")
    elif qtype == "completion" and blanks >= 2:
        # 空位数必须写进 prompt。
        # 只写"多个空用 # 连接"不够——模型不知道有几个空，
        # 就会把两个答案连成一句 "and or"，接收方切不开，一个空都填不上。
        parts.append(f"【注意】这道题有 {blanks} 个空。按空位先后输出 {blanks} 个答案，"
                     f"用 # 连接，顺序不能乱；不要写序号，不要解释。")
    parts.append("【你的答案】")
    return "\n".join(parts)


# markdown 列表符号：行首的 "- "、"* "、"1. "、"（1）"
_MD_LIST_RE = re.compile(r"(?m)^\s*(?:[-*•·]|\d+\s*[.、)）]|[（(]\d+[）)])\s*")


def clean(raw, qtype, long_mode=False, blanks=0):
    """清理模型输出，去掉常见废话。

    两种处理方式，差别很大：

      简答     保留多行并合并成一段，只去前缀和列表符号。
               原来无条件取 splitlines()[0]，模型分三行写的答案
               只有第一行活下来——这是"简答答得太简单"最直接的元凶。

      选择/填空 答案本来就该是一小段，取首行、去标点、把分隔符统一成 #。
               只有多空填空例外：模型常常一个空写一行，取首行会把
               后面几个空整段丢掉，所以改把换行折成 #。
    """
    ans = (raw or "").strip()
    ans = re.sub(r"^[`*\"'\s]+|[`*\"'\s]+$", "", ans)          # 去 markdown/引号

    if long_mode:
        ans = re.sub(r"^(最终)?答案(是|为)?[:：]?\s*", "", ans)
        ans = re.sub(r"^(答|解析|回答)\s*[:：]\s*", "", ans)
        ans = _MD_LIST_RE.sub("", ans)
        ans = re.sub(r"\s*[\r\n]+\s*", "", ans)                # 多行并成一段
        ans = re.sub(r"[ \t\u3000]{2,}", " ", ans)
        return ans.strip()[:800]

    ans = re.sub(r"^(最终)?答案(是|为)?[:：]?\s*", "", ans)      # 去"答案是："
    ans = re.sub(r"^(正确选项|选项)(是|为)?[:：]?\s*", "", ans)  # 去"正确选项："
    ans = re.sub(r"^(应)?选\s*[：:]\s*", "", ans)               # 去"选择："
    ans = re.sub(r"^(应)?选(择)?\s*([A-Da-d])\s*$", r"\3", ans)  # "选B" -> "B"
    ans = ans.strip().strip("。.;；，,")

    if qtype == "judgement":
        yes = re.search(r"(正确|对|是|true|√|✓)", ans, re.I)
        no = re.search(r"(错误|错|否|false|×|✗)", ans, re.I)
        if yes and not no:
            return "正确"
        if no:
            return "错误"

    # 只有选择/多选才把分隔符统一成 #。
    #
    # 原来这里是无条件执行的，而且连中文逗号一起换成 #。
    # 结果是答案里只要出现一个 #（比如"# 是注释符"），
    # 整篇逗号全变成 #，再由 join_as_text 补成顿号，产出
    # 「我们必须坚持而不能离开社会主义、第二、……」
    # 这种句子。库里有 3 条是这么坏掉的。
    if qtype in ("single", "multiple"):
        ans = re.sub(r"\s*[;；|]\s*", "#", ans)
        ans = ans.splitlines()[0].strip() if ans else ans
        return ans[:500]

    # 填空：一行就够，多了说明模型在解释
    ans = _MD_LIST_RE.sub("", ans)
    if blanks >= 2:
        # 多空填空：一个空写一行是模型的常见习惯（"and\nor"）。
        # 原来这里无条件取首行，第二个空直接被丢掉，
        # 交出去只剩一段，接收方按空位数一比就判不通过。
        ans = re.sub(r"\s*[\r\n]+\s*", "#", ans).strip("#").strip()
        return ans[:500]
    ans = ans.splitlines()[0].strip() if ans else ans
    return ans[:500]


def dedupe_parts(ans):
    """去掉 # 分隔答案里的重复分段（多选专用）。

    为什么需要：OCS 拿到多选答案后是"逐个点选项"。同一段出现两次，
    它会把同一个选项点两下——第一次选中，第二次取消选中，状态对不上，
    页面就卡在「等待顺序答题中」不动了。

    实测出现过这种情况：模型对齐到选项之后，两两变成同一个选项，
    最终产出 5 段、其中 2 组重复。

    只对多选调用。填空题的多空答案允许相同（两个空都填「人民」是合法的），
    去重会把第二个空的答案吃掉。
    """
    if "#" not in (ans or ""):
        return ans
    seen, uniq = set(), []
    for p in ans.split("#"):
        p = p.strip()
        if not p:
            continue
        k = norm_for_match(p)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p)
    return "#".join(uniq)


def _split_options(options):
    """兼容旧调用点，实现已收归 matching.split_options。"""
    return split_options(options)


# OCS 的 splitAnswer 默认分隔符：=== # --- ### | ; ；
# 答案里出现这些字符，OCS 就会把它切成多段。
_SEP_CHARS = "#|;；"


def _is_separator_only(s):
    """整个答案就是一串分隔符（典型：注释符号这道题的答案是 `#`）。

    为什么要单独判：OCS 拿到答案先 splitAnswer，而 "#".split("#")
    得到两个空串、过滤后是空数组，于是这道题直接被判成没答上。
    所以这种答案必须换成选项标号发出去。
    """
    core = trim_wrap(s)
    return bool(core) and all(ch in _SEP_CHARS for ch in core)


def _label_for_symbol(ans, options):
    """符号答案 -> 选项标号：先找正文完全相同的，再找正文里包含这个符号的。"""
    labels, texts = split_options(options)
    core = trim_wrap(ans)
    if not core:
        return None
    for i, t in enumerate(texts):
        if trim_wrap(t) == core:
            return labels[i]
    for i, t in enumerate(texts):
        if core in trim_wrap(t):
            return labels[i]
    return None


def _resolve_by_options(ans, options, qtype):
    """把答案改造成「若干条选项原文」，用 # 连接。

    这里是本次修复"选项都点了面板却显示未选"的关键。

    改动前：每一段都丢给 align_answer_to_options，而这个函数保证
    **总能返回点东西**——实在对不上就挑字符重合度最高的选项。
    于是模型答偏时，OCS 会被喂一个"差不多"的选项，点下去是错的；
    多选里还会出现几段对到同一个选项，OCS 点两下（选中又取消），
    页面和面板的状态就此对不上。

    改动后：对不上的段直接丢掉。少点一项只是没答全，
    点错一项是把对的也带偏了。
    """
    ans = (ans or "").strip()
    labels, texts = split_options(options)

    # 模型有时把标号和正文一起写：「A. #」「B. 甲」。
    # 不先剥掉的话，下面按分隔符切分会把 "#" 切走、只剩 "A."，
    # 最后发出去一个既不是标号也不是选项正文的东西。
    m = re.match(r"^\s*([A-Da-d])\s*[.、．:：)）]\s*(.+)$", ans)
    if m:
        lb, rest = m.group(1).upper(), m.group(2).strip()
        if _is_separator_only(rest):
            return _label_for_symbol(rest, options) or (lb if lb in labels else ans)
        if lb in labels:
            # 以正文为准，不以标号为准：模型数错标号是常事，
            # 「D. //」里 // 其实对应 B，按正文走才对。
            aligned = align_one(rest, options)
            if aligned:
                return aligned

    # 1. 答案本身就是分隔符（#、;、|）：换成选项标号
    if _is_separator_only(ans):
        lb = _label_for_symbol(ans, options)
        if lb:
            return lb

    parts = [p.strip() for p in re.split(r"[#|;；]", ans or "") if p.strip()]

    picked = []
    for p in parts:
        # 一段长答案里同时含多个选项原文（模型没分段，把正确项抄成一句话）
        if qtype == "multiple":
            hits = options_in_text(p, options)
            if len(hits) >= 2:
                for h in hits:
                    if h not in picked:
                        picked.append(h)
                continue
        t = align_one(p, options)
        if t and t not in picked:
            picked.append(t)

    if qtype == "single" and len(picked) > 1:
        # 单选只能有一个答案。多出来的段是模型自己发散出来的，
        # 留着会让 OCS 点两个选项（单选是 radio，后点的把先点的顶掉）。
        picked = picked[:1]
    if texts and len(picked) > len(texts):
        picked = picked[:len(texts)]

    if not picked:
        b = best_option(ans, options)
        if not b:
            return ans
        picked = [b]

    out = []
    for t in picked:
        # 选中的选项正文如果本身就是分隔符，同样换成标号
        out.append(_label_for_symbol(t, options) if _is_separator_only(t) else t)
    return "#".join(out)


# 先判「否」再判「是」。
# 顺序不能反："不正确" 里含 "正确"，"不对" 里含 "对"，
# 先判是的话这两种都会被判成"正确"。
_JUDGE_NO = re.compile(r"(错误|错的?|不对|不正确|不是|否|×|✗|false|^f$|^0$)", re.I)
_JUDGE_YES = re.compile(r"(正确|对的?|是的|√|✓|true|^t$|^1$)", re.I)


def _judge_word(s):
    s = (s or "").strip()
    if not s:
        return None
    if _JUDGE_NO.search(s):
        return "错误"
    if _JUDGE_YES.search(s):
        return "正确"
    return None


def normalize_judgement(ans, options=""):
    """把判断题答案统一成「正确」或「错误」。

    为什么必须这样：OCS 的 resolveJudgement（resolvers/judgement.ts）会拿答案
    去和 CORRECT_WORDS / INCORRECT_WORDS 做**精确相等**比对，
    比对不上就直接 return { finish: false }，页面上显示「无法选择」。

    所以答案写成选项标号 "B" 是没用的——"B" 不等于任何一个正确/错误词，
    OCS 会认为这道题没答上。实测 13 道判断题里有 4 道踩了这个坑。

    处理顺序：
      1. 答案里有「对/错」类词 -> 直接用
      2. 答案里只有选项标号（"B"、"选B"）-> 取出该选项的正文，再判一次
      3. 答案和某个选项正文一致 -> 判该选项
      4. 都判不出来 -> 原样返回（宁可 OCS 跳过，也不瞎猜）
    """
    r = _judge_word(ans)
    if r:
        return r
    if not options:
        return ans

    labels, texts = _split_options(options)
    if not texts:
        return ans

    # 2. 答案里带选项标号
    m = re.search(r"(?:^|[^A-Za-z])([A-Da-d])(?:$|[^A-Za-z])", ans or "")
    if m:
        lb = m.group(1).upper()
        if lb in labels:
            r = _judge_word(texts[labels.index(lb)])
            if r:
                return r

    # 3. 答案与某个选项正文相同
    for t in texts:
        if norm_for_match(t) == norm_for_match(ans):
            r = _judge_word(t)
            if r:
                return r

    return ans


# 题面里的「空位」只用下划线判断，不用括号。
# 因为括号在选择/多选题里太常见（「……包括（）」），拿它当空位会把多选题误判成填空。
_BLANK_RE = re.compile(r"[_＿]{2,}")


def count_blanks(title):
    """数题面里有几个空位（____ 这种）。"""
    return len(_BLANK_RE.findall(title or ""))


# 填空的另一种空位写法：「（  ）」。
# 只对填空题生效——选择题题干里的括号太常见（「……包括（）」），
# 拿它数空位会把单选题误判成多空题。
_PAREN_BLANK_RE = re.compile(r"[（(]\s*[）)]")


def blank_count(title, qtype):
    """这道题要填几个空。

    先数下划线；一个都没有、题型又是填空时，再数全角括号。
    只用 count_blanks 会漏掉「（ ）」写法的空位，把两空题当成单空题，
    结果只填第一个空、第二个空空着。
    """
    n = count_blanks(title)
    if n == 0 and qtype == "completion":
        n = len(_PAREN_BLANK_RE.findall(title or ""))
    return n


# 多空答案的切分顺序，从最该用的开始试。
# 只有前两种是接收方（OCS）认的分隔符，后两种纯粹是"模型可能这么写"的兜底：
# 分割符只有 ['===','#','---','###','|',';','；']，顿号、逗号、空格都不算。
_PART_SPLITTERS = (
    r"[#|;；]",            # 照 prompt 写的标准格式
    r"\s*[\r\n]+\s*",      # 一个空写一行
    r"\s*[、,，/／]\s*",    # 顿号、逗号、斜杠
    r"\s+",                # 英文答案 "and or" 只剩空格可用
)

# 段首序号：1. / 1、/ (1) / ① / a.
_LABEL_RE = re.compile(
    r"^\s*(?:[（(]\d+[）)]|\d+\s*[.、．)）]|[①-⑳]|[A-Za-z]\s*[.、．)）])\s*")


def _fit_blanks(ans, n):
    """把多空填空的答案切成正好 n 段，用 # 连接。

    为什么必须"正好"：接收方拿答案先按分隔符切段，再和页面上输入框的个数比。
    段数不等于输入框个数就直接判不通过，**一个空都不填**——
    不是"填不满"，是完全不动，页面上看起来就是没有反应。

    所以「切不齐」和「没答」是一回事。模型不听话时答案常长成
    `and or` / `and、or` / `and\\nor`，这里按常见写法逐个试，切出正好 n 段就交。

    都切不出来就原样返回。宁可让接收方跳过这道题，也不硬凑一个段数出来
    ——凑出来的第二段一定是错的，填进去比空着更糟。
    """
    ans = (ans or "").strip()
    if n < 2 or not ans:
        return ans

    longer = None
    for pat in _PART_SPLITTERS:
        parts = [_LABEL_RE.sub("", p).strip() for p in re.split(pat, ans)]
        parts = [p for p in parts if p]
        if len(parts) == n:
            return "#".join(parts)
        if longer is None and len(parts) > n:
            # 切多了：模型在答案后面补了一句解释。取前 n 段。
            longer = parts
    if longer:
        return "#".join(longer[:n])
    return ans


def join_as_text(ans):
    """把 # 分段连成一段文字，供简答/单空填空使用。

    连接符按长度选：短片段（「统一战线」「武装斗争」）用顿号，
    长句子用逗号。原来一律用顿号，接到长句上就变成
    「我们必须坚持而不能离开社会主义、第二、我国的……」这种病句。
    """
    parts = [p.strip() for p in (ans or "").split("#") if p.strip()]
    out = ""
    for p in parts:
        if out and not re.search(r"[。！？：；]$", out):
            # 10 字以上基本是一个短句了，再接顿号读不通
            out += "，" if (len(out) > 10 or len(p) > 10) else "、"
        out += p
    return out


def tidy_long_answer(ans):
    """把简答答案洗成一段干净的纯文本。

    两件事：

    1. 去掉所有会被 OCS 当分隔符的字符。
       splitAnswer 的默认分隔符是 ['===','#','---','###','|',';','；']，
       简答答案里出现一个 # 或分号，就会被切成多段，
       OCS 当成多个空去顺序填，页面卡在「等待顺序答题中」。
       井号换成全角（看起来一样，但不再是分隔符），分号换句号。

    2. 收拾模型爱留的排版垃圾：连续标点、行首编号。
    """
    s = (ans or "").strip()
    if not s:
        return s
    s = s.replace("#", "＃")
    s = re.sub(r"\s*\|\s*", "，", s)
    s = re.sub(r"[;；]", "。", s)
    s = re.sub(r"([。，、])\1+", r"\1", s)      # 「。。」->「。」
    s = re.sub(r"，{2,}", "，", s)
    s = s.strip().strip("，、；")
    return s


def should_split(title, qtype, ans):
    """这条答案能不能用 # 分隔。

    为什么要判断：OCS 见到 # 就当"多个空"，进入顺序填写模式，
    挨个找输入框往里填。简答题页面通常只有一两个输入框，
    填到第二段就找不到了，于是这道题卡住不动，
    后面排队的题目全部显示「等待顺序答题中」，看起来像没答上。

    规则（**先看题面，不看答案**）：
      多选            -> 必须分段，这是 OCS 要求的格式
      填空且 ≥2 个空  -> 分段，且段数必须等于空位数
      其余（简答等）  -> 连成一段

    原来第一句是 `if "#" not in ans: return False`，先看答案有没有 #。
    多空填空的答案如果本来就没带 #（"and or"），就会一路返回 False，
    最后被 join_as_text 当成一段交出去——题面明明两个空却只给一段，
    接收方段数对不上，一个空都填不上。
    """
    if qtype == "multiple":
        return True
    if qtype == "completion" and blank_count(title, qtype) >= 2:
        return True
    return False


def ask(title, options, qtype):
    """调大模型答题，返回清洗后的答案字符串。"""
    if not config.API_KEY:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY，请编辑 .env 文件")

    # 空位数先算出来，prompt、清洗、分段三处都要用。
    blanks = blank_count(title, qtype)

    # 简答题走另一套 prompt 和更长的输出上限。
    # 原来不分题型，简答也被"只输出答案本身、不要任何解释"约束，
    # 结果就是几个关键词，题目里"及其相互关系"这类要求全被吞掉。
    long_mode = is_short_answer(title, qtype)
    if blanks >= 2:
        # 多空填空必须走 # 分隔协议，不能被判成简答。
        # 括号写法的空位（「（ ）」）不在 is_short_answer 的排除条件里，
        # 不在这里拦一下，就会走简答分支把 # 换成全角 ＃，段数全废。
        long_mode = False

    r = _post_chat({
        "model": config.MODEL,
        "messages": [
            {"role": "system",
             "content": config.SYSTEM_PROMPT_LONG if long_mode else config.SYSTEM_PROMPT},
            {"role": "user",
             "content": build_prompt(title, options, qtype, long_mode, blanks)},
        ],
        "temperature": 0.1,
        "max_tokens": 800 if long_mode else 512,
    })
    content = r["choices"][0]["message"].get("content") or ""
    ans = clean(content, qtype, long_mode, blanks)

    if long_mode:
        # 简答不参与任何 # 协议：它是往一个 textarea 里填一段话
        return tidy_long_answer(ans)

    if qtype == "judgement":
        # 判断题不走选项对齐：它要的是"正确/错误"两个词，
        # 对齐是给多选/单选准备的文本匹配，两者目标不同。
        return normalize_judgement(ans, options)

    if qtype in ("single", "multiple") and options:
        ans = _resolve_by_options(ans, options, qtype)

    # 对齐之后再查重。顺序不能反：是两个不同的表述被对齐到同一个选项
    # 才产生重复，对齐之前还看不出来。
    if qtype == "multiple":
        ans = dedupe_parts(ans)

    # 多空填空：段数必须正好等于空位数，差一段就等于没答。
    if qtype == "completion" and blanks >= 2:
        return _fit_blanks(ans, blanks)

    # 最后一步：不是多选题、题面也没有多个空位的，把 # 分段连成一段。
    # 典型就是单空填空——分段只会让 OCS 卡在顺序填写里。
    if not should_split(title, qtype, ans):
        ans = join_as_text(ans)

    return ans
