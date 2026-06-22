"""
Render 部署专用：通过 PORT 环境变量监听，绑定 0.0.0.0。
公开浏览入口统一走 GitHub Pages，Render 仅承载交互式接口。
启动命令：python render_server.py
"""
import os
import sys

# 让 server.py 能找到 config
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 覆盖 server.py 中的 PORT 和监听地址
import server as _srv
from http.server import ThreadingHTTPServer

PORT = int(os.environ.get("PORT", 8765))

def main():
    print(f"Starting server on 0.0.0.0:{PORT}")
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), _srv.Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
