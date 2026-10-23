"""启动入口。

双击 启动.bat，或者命令行 `python run.py`。

这里只负责：打印启动横幅、拉起 HTTP 服务、处理端口占用这类启动期错误。
业务逻辑在 server/ 里。
"""

import sys

from server import api, config, store


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
    if not config.API_KEY:
        print("  [!] 未配置 DEEPSEEK_API_KEY —— 答题不可用")
        print("      请编辑 .env（参考 .env.example）后重启")
    else:
        print(f"  [OK] 模型      : {config.MODEL}")
        print(f"  [OK] API 地址  : {config.BASE_URL}")
    print(f"  [OK] 端口      : {config.PORT}")

    # 先把数据库路径和已有记录数打出来。
    # 出现"统计一直是 0"时，第一步要能确认：服务用的是哪个库、里面本来有没有数据。
    # 项目里可能存在多个 cache.db（换过目录、留了旧副本），看错库是最常见的误判来源。
    db = store.db_info()
    existed = db["exists"]
    cached = store.get_stats().get("cached", 0)
    print(f"  [OK] 数据库    : {db['path']}")
    if existed:
        print(f"       已有 {cached} 道题的缓存，统计沿用上次的记录")
    else:
        print("       本次新建空库（第一次运行会出现这种情况）")

    if config.TOKEN:
        print("  [OK] 接口令牌  : 已启用")
    else:
        print("  [!] 接口令牌  : 未启用")
        print("      服务监听 0.0.0.0，同局域网他人可盗用额度，建议设置 API_TOKEN")
    print()
    print(f"  控制台       http://127.0.0.1:{config.PORT}/    （可在此直接填写 API Key）")
    print(f"  OCS 题库地址 http://127.0.0.1:{config.PORT}/query")
    print()
    print("  按 Ctrl+C 停止")
    print("=" * 58)
    try:
        api.make_server().serve_forever()
    except KeyboardInterrupt:
        print("\n[已停止]")
    except OSError as e:
        print(f"\n[启动失败] {e}")
        if "10048" in str(e) or "address" in str(e).lower():
            print(f"端口 {config.PORT} 被占用。请先结束占用该端口的程序，")
            print("或修改 .env 里的 PORT 后重试。")
        input("\n按回车退出...")


if __name__ == "__main__":
    main()
