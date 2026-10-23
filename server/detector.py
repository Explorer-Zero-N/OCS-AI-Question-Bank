"""非题目识别：把课件正文从真实题目里筛出来。

为什么需要这一层：
OCS 的 answerer-env.ts 里就一句 `env.title = params.title`，
标题文本原样透传，不做任何过滤。课件页/说明页上选择器抓到的
PPT 正文会被整段当题目传过来，服务端若不识别，就是拿大段课件去问模型，
既浪费额度又答非所问。
"""

import re

# 典型非题目文本特征：课件 PPT 正文、章节说明、提示语。
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

# 题面里的空位（____ 这种）。不用括号：括号在多选题里太常见。
_BLANK_RE = re.compile(r"[_＿]{2,}")

# 简答/论述类题目的开头。这类题没有空位，也不一定带标点，
# 但它们确实是题目——OCS 把简答题也归到 completion 类型。
_ASK_RE = re.compile(r"^(请|简述|概述|论述|试述|说明|解释|分析|谈谈|如何|怎样|为什么|"
                     r"什么|列举|举例|比较|阐述)")

# 简答题的典型措辞。OCS 把简答和填空都归到 completion，
# 只能从题面用词把两者分开：填空要短答案，简答要成段论述。
_SHORT_ANSWER_WORDS = (
    "简述", "概述", "论述", "试述", "说明", "解释", "阐述", "分析",
    "比较", "谈谈", "举例", "列举", "区别", "联系", "关系", "意义",
    "作用", "特点", "内涵", "原则", "经验", "要求", "如何", "怎样", "为什么",
)

# 带选项时的硬上限。比无选项时宽松得多——因为"有 A/B/C/D 选项"本身
# 就几乎只可能是题目，PPT 正文不会给自己编四个选项。
_MAX_LEN_WITH_OPTIONS = 200
_MAX_LINES_WITH_OPTIONS = 6

# 无选项时的上限。判据变弱了，这里就得收紧。
_MAX_LEN = 120
_MAX_LINES = 3


def looks_like_question(title, qtype, options=""):
    """判断一段文本是否像题目。返回 (是否像, 不像的原因)。

    判定分三档，越往后判据越弱、规则越严：

      一档  带了 ≥2 个选项 —— 只看长度和行数的硬上限。
            关键词黑名单在这里**完全不生效**。
            原因：真题题干里出现「总结」「概述」「首先」这类词太常见，
            「毛泽东在《〈共产党人〉发刊词》中总结的……三大法宝包括（）」
            就是一道真题，却因为一个「总结」被一票否决，OCS 显示「共 0 个结果」。

      二档  填空题且结尾符合题面特征 —— 直接放行，同样不看关键词。
            填空题没有选项可用，靠「___ / 。 / ？」这个形态特征判断。

      三档  其余情况（无选项的短标题、说明文字）—— 走完整规则：
            长度、行数、关键词黑名单。
            PPT 的栏目标题（「本章小结」「概述」）都落在这一档。
    """
    t = (title or "").strip()
    if not t:
        return False, "题目为空"

    # 选择题/判断题必须带选项，否则几乎可以断定是误抓的页面元素
    # （如目录项「4.1 条件语句」、章节标题等）
    opt_lines = [x for x in (options or "").splitlines() if x.strip()]
    if qtype in ("single", "judgement", "multiple") and len(opt_lines) < 2:
        return False, "选择/判断题缺少选项，疑似误抓的页面元素"

    lines = [x for x in t.splitlines() if x.strip()]

    # ---- 一档：有选项，基本就是题目 ----
    if len(opt_lines) >= 2:
        if len(t) > _MAX_LEN_WITH_OPTIONS:
            return False, f"文本过长({len(t)}字)，疑似课件正文"
        if len(lines) > _MAX_LINES_WITH_OPTIONS:
            return False, f"行数过多({len(lines)}行)，疑似课件正文"
        return True, ""

    # ---- 二档：填空题 / 简答题 ----
    # OCS 把简答题也归到 completion。所以判据不能只看"结尾是不是标点"——
    # 「简述新民主主义革命的三大法宝及其相互关系」没有空位也没有标点，
    # 按原规则会被判成非题目，简答题连答题机会都没有。
    if qtype == "completion":
        if len(t) > 300:
            return False, f"文本过长({len(t)}字)，疑似课件正文"
        has_blank = bool(_BLANK_RE.search(t))
        ends_ok = any(t.rstrip().endswith(m) for m in _COMPLETION_TAILS)
        is_ask = bool(_ASK_RE.match(t))
        if has_blank or ends_ok or is_ask:
            return True, ""
        return False, "填空题格式不符"

    # ---- 三档：无选项，判据最弱，全套规则都上 ----
    if len(t) > _MAX_LEN:
        return False, f"文本过长({len(t)}字)，疑似课件正文"

    if len(lines) > _MAX_LINES:
        return False, f"行数过多({len(lines)}行)，疑似课件正文"

    low = t.lower()
    for kw in _NON_QUESTION_HINTS:
        if kw in low:
            return False, f"命中非题目关键词「{kw}」"

    return True, ""


def is_short_answer(title, qtype):
    """这条题面是不是简答/论述题（而不是填空）。

    为什么要分：OCS 把两者都归进 completion，都落成一个多行输入框，
    但要求完全不同——
      填空     「社会主义的根本任务是____」-> 要一个词
      简答     「简述三大法宝及其相互关系」-> 要成段、要答到每一层要求

    之前不分，简答题也被"只输出答案本身"约束着，模型只能挤出一串关键词，
    「及其相互关系」这种要求直接被吞掉。

    判定：必须是 completion、题面没有下划线空位、且带简答措辞。
    没有空位是硬条件——「在 Python 中，使用______符号」这种带空位的，
    哪怕措辞像简答，也是填空。
    """
    if qtype != "completion":
        return False
    t = (title or "").strip()
    if not t or len(t) < 6:
        return False
    if _BLANK_RE.search(t):
        return False
    if _ASK_RE.match(t):
        return True
    return any(w in t for w in _SHORT_ANSWER_WORDS)
