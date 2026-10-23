"""非题目识别：把课件正文从真实题目里筛出来。

为什么需要这一层：
OCS 的 answerer-env.ts 里就一句 `env.title = params.title`，
标题文本原样透传，不做任何过滤。课件页/说明页上选择器抓到的
PPT 正文会被整段当题目传过来，服务端若不识别，就是拿大段课件去问模型，
既浪费额度又答非所问。
"""

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


def looks_like_question(title, qtype, options=""):
    """判断一段文本是否像题目。返回 (是否像, 不像的原因)。"""
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
