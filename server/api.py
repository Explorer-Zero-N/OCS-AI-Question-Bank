"""HTTP 层：路由 + 静态文件。

这一层只做两件事：
  1. 把 HTTP 请求翻成函数调用（/query、/api/*）
  2. 把 web/ 目录下的静态文件发出去

它不生成任何 HTML，页面长什么样完全由 web/ 决定。
"""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import config, llm, service, store

WEB_DIR = config.BASE_DIR / "web"

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def mask_key(k):
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
    # 所以这样写能正确传过去；且比放在 data 里更直观，不易被误改
    # （GET 请求时 data 里的同名参数会覆盖 URL 上的）。
    url = f"http://127.0.0.1:{config.PORT}/query"
    if config.TOKEN:
        url += "?token=" + config.TOKEN

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


def status_payload():
    """控制台页面要的全部数据，一次拿完。"""
    s = store.get_stats()
    return {
        "code": 1,
        "ready": bool(config.API_KEY),
        "has_key": bool(config.API_KEY),
        "masked_key": mask_key(config.API_KEY),
        "model": config.MODEL,
        "port": config.PORT,
        "base_url": config.BASE_URL,
        "token_enabled": bool(config.TOKEN),
        "token": config.TOKEN,
        "stats": {
            "queries": s.get("queries", 0),
            "cache_hits": s.get("cache_hits", 0),
            "api_calls": s.get("api_calls", 0),
            "cached": s.get("cached", 0),
            "skipped": s.get("skipped", 0),
            "empty": s.get("empty", 0),
            "errors": s.get("errors", 0),
        },
        "db": store.db_info(),
        "recent": service.get_recent(),
        "ocs": {
            "url": f"http://127.0.0.1:{config.PORT}/query",
            "config": ocs_config_json(),
        },
    }


def _resolve_static(path):
    """把 URL 路径映射到 web/ 下的真实文件，映射不到返回 None。

    只允许发 web/ 目录内的普通文件，`..` 之类的穿越会被挡掉。
    """
    rel = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
    try:
        p = (WEB_DIR / rel).resolve()
    except (OSError, ValueError):
        return None
    if p != WEB_DIR and WEB_DIR not in p.parents:
        return None
    if not p.is_file():
        return None
    return p


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

    def _send_static(self, path):
        p = _resolve_static(path)
        if not p:
            self._send(404, {"code": 0, "message": "not found"})
            return
        ctype = _MIME.get(p.suffix.lower(), "application/octet-stream")
        self._send(200, p.read_bytes(), ctype)

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

    def _route_query(self):
        p = self._query_params()
        code, body = service.handle_query(p.get("title", ""), p.get("options", ""),
                                          p.get("type", ""), p.get("token", ""))
        self._send(code, body)

    def _route_cache(self):
        """缓存题目列表。支持 q（关键词）和 limit。

        翻页没做，因为一个学期的课最多几百道题，一次 100 条够翻；
        真需要再加 offset，store.list_answers 已经留好了参数。
        """
        p = self._query_params()
        try:
            limit = int(p.get("limit") or 100)
        except ValueError:
            limit = 100
        limit = max(1, min(limit, 500))
        total, items = store.list_answers(p.get("q", ""), limit)
        self._send(200, {"code": 1, "total": total, "items": items})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/query":
            self._route_query()
        elif path == "/health":
            self._send(200, {"status": "ok", "model": config.MODEL,
                             "has_key": bool(config.API_KEY)})
        elif path == "/stats":
            self._send(200, store.get_stats())
        elif path == "/api/status":
            self._send(200, status_payload())
        elif path == "/api/cache":
            self._route_cache()
        else:
            self._send_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/query":
            self._route_query()
        elif path == "/api/config":
            self._handle_config()
        else:
            self._send(404, {"code": 0, "message": "not found"})

    def _handle_config(self):
        """网页上保存 / 清除配置。"""
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 64 * 1024:
            self._send(400, {"code": 0, "message": "请求体异常"})
            return
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            self._send(400, {"code": 0, "message": "JSON 解析失败"})
            return

        # 清除 Key 走独立标志。
        # 不能靠"传了空字符串"来判断：save_config 用 None 表示"这个字段不改"，
        # 空串和"没传"在 JSON 里都是空值，靠猜意图迟早两边理解不一致。
        if data.get("clear_key"):
            ok, msg = config.save_config(api_key="")
            if not ok:
                self._send(200, {"code": 0, "message": msg})
                return
            # 没 Key 就不用测连通性了
            self._send(200, {"code": 1, "message": "已清除", "cleared": True,
                             "tested": False})
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

        ok, msg = config.save_config(
            api_key=key if key else None,
            token=token if token else None,
            base_url=base_url if base_url else None,
        )
        if not ok:
            self._send(200, {"code": 0, "message": msg})
            return

        # 顺手测一下新 Key 能不能用（放在这层做，config 不依赖 llm）
        tested = False
        if key:
            try:
                llm.ask("测试题", "A.甲\nB.乙", "single")
                tested = True
            except Exception:
                pass

        self._send(200, {"code": 1, "message": msg, "tested": tested})


def make_server(host="0.0.0.0", port=None):
    return ThreadingHTTPServer((host, port or config.PORT), Handler)
