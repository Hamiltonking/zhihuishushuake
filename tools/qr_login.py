#!/usr/bin/env python3
"""ZHS 网页扫码登录

启动 `zhs login` 子进程，同时在本地起一个网页，把二维码放大显示在浏览器里。
二维码过期时 zhs 会自动重新获取，页面每 1.5 秒自动换图，浏览器里直接扫即可。

用法（在仓库根目录）：
    .venv/bin/python tools/qr_login.py
    .venv/bin/python tools/qr_login.py --port 8765 --no-browser
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "personal"
QR = DATA / "qrcode.png"
LOG_DIR = DATA / "logs"
ZHS_BIN = Path(sys.executable).with_name("zhs")

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ZHS 扫码登录</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;
      justify-content:center;gap:18px;background:#111318;color:#e6e6e6;
      font:15px/1.6 system-ui,"Noto Sans CJK SC",sans-serif}
 h1{font-size:19px;font-weight:600;margin:0;letter-spacing:.5px}
 .card{background:#fff;padding:14px;border-radius:14px;box-shadow:0 10px 40px #0008}
 img{display:block;width:380px;height:380px;image-rendering:pixelated}
 #st{font-size:14px;color:#8b93a1;min-height:22px}
 #st.ok{color:#3ddc84;font-weight:600}
 #st.warn{color:#ffb020}
 .hint{font-size:12.5px;color:#666e7d;text-align:center;max-width:420px}
 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#3ddc84;
      margin-right:6px;animation:p 1.4s infinite}
 @keyframes p{50%{opacity:.25}}
</style></head>
<body>
 <h1>智慧树扫码登录</h1>
 <div class="card"><img id="qr" alt="登录二维码" src="/qr.png"></div>
 <div id="st"><span class="dot"></span>等待扫码…</div>
 <div class="hint">用手机智慧树 APP「扫一扫」扫描上方二维码，并在手机上确认登录。<br>
 二维码过期后自动换新，本页面无需手动刷新。</div>
<script>
const img = document.getElementById('qr');
const st  = document.getElementById('st');
let done = false;

// 每秒换一次图（新 URL 绕开浏览器缓存），加载完成再替换，避免闪烁
setInterval(() => {
  if (done) return;
  const tmp = new Image();
  tmp.onload = () => { img.src = tmp.src; };
  tmp.src = '/qr.png?t=' + Date.now();
}, 1500);

async function tick() {
  if (done) return;
  try {
    const r = await fetch('/status?t=' + Date.now());
    const d = await r.json();
    if (d.state === 'success') {
      done = true; st.className = 'ok'; st.textContent = '✅ 登录成功，Cookie 已保存，可以关掉本页了';
    } else if (d.state === 'scanned') {
      st.className = 'warn'; st.textContent = '📱 已扫码，请在手机上点「确认登录」';
    } else if (d.state === 'expired') {
      st.className = 'warn'; st.textContent = '二维码已过期，正在换新…';
    } else if (d.state === 'error') {
      done = true; st.className = 'warn'; st.textContent = '❌ 登录中断：' + (d.msg || '请查看终端输出');
    } else {
      st.innerHTML = '<span class="dot"></span>等待扫码…';
    }
  } catch (e) {}
  setTimeout(tick, 1000);
}
tick();
</script>
</body></html>
"""


def _tail_log() -> str:
    """读取当天日志末尾，用于判断扫码状态"""
    day = datetime.now().strftime("%Y-%m-%d")
    log = LOG_DIR / f"zhs_{day}.log"
    try:
        with open(log, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8192))
            return f.read().decode("utf-8", "ignore")
    except OSError:
        return ""


def _status() -> dict[str, str]:
    tail = _tail_log()
    if "登录成功" in tail or "Cookie 已保存" in tail:
        return {"state": "success"}
    if "One-time code received" in tail or "QR Scanned" in tail:
        return {"state": "scanned"}
    if "QR code expired" in tail:
        return {"state": "expired"}
    if "登录失败" in tail or "扫码登录失败" in tail or "轮询超时" in tail:
        return {"state": "error", "msg": "终端里有错误详情"}
    return {"state": "waiting"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # 静音访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/qr.png":
            try:
                data = QR.read_bytes()
            except OSError:
                self._send(404, b"qr not ready", "text/plain")
                return
            self._send(200, data, "image/png")
        elif path == "/status":
            self._send(200, json.dumps(_status()).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")


def _raise_interrupt(signum: int, frame: object) -> None:
    """把 SIGTERM 转成 KeyboardInterrupt，走统一的清理分支"""
    raise KeyboardInterrupt


def main() -> int:
    ap = argparse.ArgumentParser(description="ZHS 网页扫码登录")
    ap.add_argument("--port", type=int, default=8765, help="监听端口，0 表示随机（默认 8765）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    if not ZHS_BIN.exists():
        print(f"找不到 zhs 可执行文件: {ZHS_BIN}\n请用 venv 里的 python 运行本脚本", file=sys.stderr)
        return 1

    # 清掉上次的二维码与状态日志，避免误判
    QR.unlink(missing_ok=True)

    # Ctrl+C / kill 时一并结束子进程，避免 zhs login 变成孤儿进程继续轮询
    signal.signal(signal.SIGTERM, _raise_interrupt)

    print("正在启动 zhs login …", flush=True)
    proc = subprocess.Popen([str(ZHS_BIN), "login"], cwd=ROOT)

    # 等二维码文件出现（最多 20 秒）
    deadline = time.time() + 20
    while time.time() < deadline and not QR.exists():
        if proc.poll() is not None:
            print("zhs login 已退出，请检查上方输出", file=sys.stderr)
            return 1
        time.sleep(0.3)
    if not QR.exists():
        print("等待二维码超时", file=sys.stderr)
        proc.terminate()
        return 1

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"\n  在浏览器打开： {url}\n  （用手机智慧树 APP 扫描网页上的二维码）\n", flush=True)
    if not args.no_browser:
        webbrowser.open(url)

    try:
        code = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        code = 130
    finally:
        httpd.shutdown()

    if code == 0:
        print("登录成功，Cookie 已保存。可以关掉浏览器页面了。")
    else:
        print(f"登录未完成（退出码 {code}），请查看上方 zhs 输出。", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
