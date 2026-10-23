"""配置：读取 .env、暴露运行时配置、写回配置。

【重要】其他模块一律用 `config.API_KEY` 这种属性访问，
不要写 `from .config import API_KEY`。

原因：网页上保存 Key 之后需要立即生效（热更新），
`from ... import` 拿到的只是 import 那一刻的快照，
后面 `config.API_KEY = 新值` 那个快照不会跟着变，服务会继续用旧 Key。
"""

import os
import re
from pathlib import Path

# 项目根目录：server/config.py 的上两层
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"
DB_PATH = BASE_DIR / "cache.db"

# OCS 题型 -> 中文名
TYPES = {"single": "单选题", "multiple": "多选题", "judgement": "判断题",
         "completion": "填空题", "unknown": "未知题型"}

# OCS 偶尔把题型传成数字码
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


def _load_env():
    """读取 .env，支持 注释(#)/空行/KEY=VALUE。"""
    cfg = {}
    if not ENV_FILE.exists():
        return cfg
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


_env = _load_env()


def cfg(key, default=""):
    """环境变量优先，方便临时覆盖（测试时不用改 .env）。"""
    return os.environ.get(key) or _env.get(key) or default


API_KEY = cfg("DEEPSEEK_API_KEY")
MODEL = cfg("DEEPSEEK_MODEL", "deepseek-chat")
BASE_URL = cfg("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
PORT = int(cfg("PORT", "8000") or 8000)
TOKEN = cfg("API_TOKEN")


def save_config(api_key=None, token=None, base_url=None):
    """把配置写回 .env，保持其它行不变。

    只负责落盘 + 更新全局变量，不负责测试连通性。
    测试放在 api 层做，避免 config 反向依赖 llm（会形成循环导入）。

    返回 (成功与否, 消息)。
    """
    global API_KEY, TOKEN, BASE_URL

    lines = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

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
        if ENV_FILE.exists():
            bak = BASE_DIR / ".env.bak"
            bak.write_text(ENV_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        return False, f"写入 .env 失败: {e}"

    # 立即生效，无需重启
    if api_key is not None:
        API_KEY = api_key
    if token is not None:
        TOKEN = token
    if base_url is not None:
        BASE_URL = base_url.rstrip("/")

    return True, "已保存"
