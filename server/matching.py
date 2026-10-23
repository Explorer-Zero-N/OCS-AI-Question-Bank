"""答案匹配：归一化、相似度、把模型答案改造成 OCS 认得的形态。

这里没有任何 IO，全是纯函数，方便单独测试。

背景（读 OCS 源码得出，不是猜的）：
  packages/core/src/core/worker/resolvers/single.ts 的 resolveSingle
  拿了答案要过四阶段，任一阶段不过就丢弃这道题：

    阶段1  归一化后 答案 === 选项，或 答案.includes(选项)   ← 方向单向！
    阶段2  Dice 相似度 > 0.6
    阶段3  答案是单个 A/B/C 字母
    阶段4  多片段合并后重试
"""

import re


def norm_for_match(s):
    """与 OCS 的 clearString/normalizeString 对齐的归一化。

    OCS 在匹配前会去掉标点空白并转小写，只保留中英数字，
    所以这里做同样处理，确保我们的判断与它的实际行为一致。
    """
    s = (s or "").strip().lower()
    s = s.replace("，", "").replace("。", "")
    s = re.sub(r"[\s\u3000]+", "", s)
    s = re.sub(r"[^\w\u4e00-\u9fff]", "", s)
    return s


def similarity(a, b):
    """Dice 系数（二元组重合度），与 string-similarity 的思路一致。

    OCS 用 npm 包 string-similarity，其核心就是比较两个字符串的
    二元组（bigram）重合比例。这里复现同样量纲，用于自检能否过 0.6 阈值。
    """
    a, b = norm_for_match(a), norm_for_match(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    def bigrams(s):
        return {s[i:i + 2] for i in range(len(s) - 1)} or {s}

    ba, bb = bigrams(a), bigrams(b)
    inter = len(ba & bb)
    return 2.0 * inter / (len(ba) + len(bb))


# 选项正文外面爱包的反引号、引号，比对前先剥掉。
# 典型：注释符号这道题的选项写的是 "A. ``#``"，答案是 "#"，
# 不剥的话两个字符串永远不相等。
_WRAP = " \t\r\n\u3000`'\"“”‘’"


def trim_wrap(s):
    return (s or "").strip().strip(_WRAP)


def split_options(options):
    """把 "A. 甲\nB. 乙" 拆成 ([标号], [正文])。没有标号的按顺序补 A/B/C。"""
    labels, texts, seq = [], [], 0
    raw_list = options if isinstance(options, (list, tuple)) else str(options or "").splitlines()
    for raw in raw_list:
        line = (raw or "").strip()
        if not line:
            continue
        m = re.match(r"^\s*([A-Za-z])\s*[.、．:：)）]\s*(.+)$", line)
        if m:
            labels.append(m.group(1).upper())
            texts.append(m.group(2).strip())
        else:
            labels.append(chr(ord("A") + seq))
            texts.append(line)
        seq += 1
    return labels, texts


def align_one(part, options):
    """把**一段**答案对齐到某个选项的原文，对不上返回 None。

    为什么改成"对不上就返回 None"：

    OCS 的 resolveSingle / resolveMultiple 里，匹配不上的片段不会报错，
    只是被静默忽略；但它的相似度兜底（> 0.6）会把片段对到一个
    **错误** 的选项上点下去。结果就是页面上点了几项、
    答题面板的状态却对不上——用户看到的是"明明都选了，还显示未选"。

    所以这里宁可返回 None 让调用方丢掉这一段，也不交一个"差不多"的答案。

    尝试顺序（和 OCS 的阶段1/阶段2 对齐）：
      1. 原文相等（含 #、// 这类符号答案）
      2. 归一化后相等
      3. 答案是选项的子串，且只命中一个选项
      4. 相似度 > 0.6
    """
    _, texts = split_options(options)
    if not texts:
        return None
    p = trim_wrap(part)
    if not p:
        return None

    # 1. 原文相等
    for t in texts:
        if trim_wrap(t) == p:
            return t

    pn = norm_for_match(p)
    if pn:
        # 2. 归一化相等
        for t in texts:
            if norm_for_match(t) == pn:
                return t
        # 3. 答案是选项的子串，且只命中一个（命中多个说明有歧义，交给相似度判）
        subs = [t for t in texts if pn in norm_for_match(t)]
        if len(subs) == 1:
            return subs[0]

    # 4. 相似度
    best_i, best = -1, 0.0
    for i, t in enumerate(texts):
        s = similarity(p, t)
        if s > best:
            best, best_i = s, i
    if best > 0.6:
        return texts[best_i]

    return None


def options_in_text(text, options):
    """一段长答案里**同时包含**多个选项原文时，把这些选项按顺序挑出来。

    用在哪：模型有时不用 # 分段，而是把几个正确选项抄成一句话。
    OCS 拿到这种答案是靠"答案⊇选项"逐个命中的，我们这边得先把它们拆开，
    否则整段话会被当成一段，相似度匹配只会命中其中一个。
    """
    tn = norm_for_match(text)
    if not tn:
        return []
    _, texts = split_options(options)
    return [t for t in texts if norm_for_match(t) and norm_for_match(t) in tn]


def best_option(text, options):
    """一段都对不上时的最后退路：取字符重合度最高的选项。

    阈值 0.35 是折中：太低会把完全无关的选项点上去（静默答错），
    太高又等于放弃这道题。0.35 大约相当于"至少有一小半二元组相同"。
    """
    _, texts = split_options(options)
    best_i, best = -1, 0.0
    for i, t in enumerate(texts):
        s = similarity(text, t)
        if s > best:
            best, best_i = s, i
    if best_i >= 0 and best > 0.35:
        return texts[best_i]
    return None
