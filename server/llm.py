"""模型调用：拼 prompt、发请求、清洗回复、把答案对齐到选项。

只用标准库 urllib，不引入 openai 之类的 SDK——这是"零依赖"的根基。
"""

import json
import os
import re
import urllib.request

from . import config
from .matching import align_answer_to_options


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


def build_prompt(title, options, qtype):
    parts = [f"【题型】{config.TYPES.get(qtype, '未知题型')}", f"【题目】{title.strip()}"]
    if options and options.strip():
        parts.append("【选项】（答案必须从下面原样照抄，禁止改写或概括）")
        parts.append(options.strip())
    parts.append("【你的答案】")
    return "\n".join(parts)


def clean(raw, qtype):
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


def ask(title, options, qtype):
    """调大模型答题，返回清洗后的答案字符串。"""
    if not config.API_KEY:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY，请编辑 .env 文件")

    r = _post_chat({
        "model": config.MODEL,
        "messages": [
            {"role": "system", "content": config.SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(title, options, qtype)},
        ],
        "temperature": 0.1,
        "max_tokens": 512,
    })
    content = r["choices"][0]["message"].get("content") or ""
    ans = clean(content, qtype)

    # 把答案对齐到选项形态，提升 OCS 侧的匹配成功率
    if qtype in ("single", "multiple", "judgement") and options:
        parts = [p.strip() for p in ans.split("#") if p.strip()]
        if parts:
            aligned = [align_answer_to_options(p, options) for p in parts]
            ans = "#".join(aligned)

    return ans
