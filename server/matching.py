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


def align_answer_to_options(answer, options):
    """把模型答案改造成 OCS 更容易匹配上的形态。

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

    ans_n = norm_for_match(answer)

    # 1. 已能精确匹配 -> 不用动
    for t in opt_texts:
        if norm_for_match(t) == ans_n:
            return answer

    # 2. 答案是某选项的子串 -> 返回选项原文（补全上下文，满足"答案⊇选项"）
    subs = [(i, t) for i, t in enumerate(opt_texts) if ans_n and ans_n in norm_for_match(t)]
    if len(subs) == 1:
        return subs[0][1]

    # 3. 相似度已过阈值 -> 原样返回
    best = max((similarity(answer, t) for t in opt_texts), default=0)
    if best > 0.6:
        return answer

    # 4. 找包含答案的选项，且选项不算太长（避免选到一个泛泛的长选项）
    cands = [(i, t) for i, t in enumerate(opt_texts)
             if ans_n and ans_n in norm_for_match(t) and len(t) <= len(answer) * 4 + 12]
    if cands:
        # 取最短的那个，最贴近答案本身
        cands.sort(key=lambda x: len(x[1]))
        return cands[0][1]

    # 5. 兜底：返回标号，让 OCS 走阶段3（纯字母答案）
    # 只有当模型答案与某选项有部分重合时才这么做，否则宁可保持原样
    for i, t in enumerate(opt_texts):
        tn = norm_for_match(t)
        if ans_n and tn and (ans_n in tn or tn in ans_n):
            return opt_labels[i]

    # 6. 最后兜底：答案与所有选项字面都不沾边（模型做了同义改写/概括）。
    # 此时任何返回都无法让 OCS 的阶段1/2 命中，但返回"字符重合度最高"的选项
    # 至少还有机会：OCS 阶段2 用 Dice 系数，部分重合仍可能 > 0.6。
    # 若最高相似度也极低，则保持原答案，让 OCS 自行跳过（总比乱选好）。
    scored = [(i, similarity(answer, t)) for i, t in enumerate(opt_texts)]
    if scored:
        scored.sort(key=lambda x: -x[1])
        bi, bs = scored[0]
        if bs > 0.25:
            return opt_texts[bi]

    return answer
