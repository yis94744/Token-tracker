import json
import re
import os
import sys
import time
import threading
import ssl
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ===== 配置 =====
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8890
TARGET_HOST = "api.deepseek.com"
DEFAULT_API_KEY = "sk-b8081e92e69d4c14b3755bff2551cb78"  # 默认 API Key
DATA_FILE = Path(__file__).parent / "token-usage.json"
WEB_DIR = Path(__file__).parent

# ===== 数据管理 =====
data_lock = threading.Lock()

def load_data():
    with data_lock:
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except:
            return []

def save_data(data):
    with data_lock:
        DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def log_usage(model, prompt_tokens, completion_tokens, total_tokens):
    today = time.strftime("%Y-%m-%d")
    record = {
        "date": today,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "api": f"DeepSeek-{model}" if model else "DeepSeek",
        "model": model or "unknown",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
    records = load_data()
    records.append(record)
    save_data(records)
    print(f"[记录] {record['api']} | prompt={prompt_tokens} completion={completion_tokens} total={total_tokens}")
    return record

# ===== HTTP 代理处理器 =====
class ProxyHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {args[0]}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/data" or path == "/data/":
            records = load_data()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps(records, ensure_ascii=False).encode("utf-8"))
            return

        if path == "/stats" or path == "/stats/":
            records = load_data()
            today = time.strftime("%Y-%m-%d")
            from datetime import datetime, timedelta
            ws = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
            ms = today[:8] + "01"
            stats = {
                "today": sum(r.get("total_tokens", 0) for r in records if r.get("date") == today),
                "week": sum(r.get("total_tokens", 0) for r in records if r.get("date", "") >= ws),
                "month": sum(r.get("total_tokens", 0) for r in records if r.get("date", "") >= ms),
                "total": sum(r.get("total_tokens", 0) for r in records),
                "record_count": len(records),
                "api_count": len(set(r.get("api", "") for r in records)),
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps(stats, ensure_ascii=False).encode("utf-8"))
            return

        html_path = WEB_DIR / "index.html"
        if html_path.exists():
            content = html_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # 自动注入中文回复指令
        try:
            body_json = json.loads(body)
            if "messages" in body_json:
                messages = body_json["messages"]
                has_system = messages and messages[0].get("role") == "system"
                lang_instr = "请始终使用中文进行回复。"
                if has_system:
                    existing = messages[0].get("content", "")
                    if "中文" not in existing and "Chinese" not in existing:
                        messages[0]["content"] = lang_instr + " " + existing
                else:
                    messages.insert(0, {"role": "system", "content": lang_instr})
                body = json.dumps(body_json, ensure_ascii=False).encode("utf-8")
        except (json.JSONDecodeError, KeyError, TypeError):
            pass  # 非 JSON 请求体则原样转发

        target_url = f"https://{TARGET_HOST}{self.path}"
        req = urllib.request.Request(target_url, data=body, method="POST")

        # 复制请求头
        has_auth = False
        for key, value in self.headers.items():
            kl = key.lower()
            if kl in ("host", "connection", "proxy-connection", "transfer-encoding"):
                continue
            if kl == "authorization":
                has_auth = True
            req.add_header(key, value)

        # 如果客户端没传 Authorization，自动注入默认 API Key
        if not has_auth and DEFAULT_API_KEY:
            req.add_header("Authorization", f"Bearer {DEFAULT_API_KEY}")
            print(f"[{time.strftime('%H:%M:%S')}] [代理] 自动注入 API Key")

        # 创建忽略 SSL 验证的上下文（信任 DeepSeek 证书，但避免本地代理的 SSL 问题）
        ssl_ctx = ssl.create_default_context()

        try:
            with urllib.request.urlopen(req, timeout=120, context=ssl_ctx) as resp:
                resp_body = resp.read()
                resp_headers = dict(resp.headers)

                # 提取 token 用量
                if "/chat/completions" in self.path or "/completions" in self.path:
                    resp_text = resp_body.decode("utf-8", errors="ignore")
                    try:
                        if resp_text.strip().startswith("data:"):
                            # 流式响应 (SSE): 从最后一个包含 usage 的 chunk 提取
                            chunks = re.findall(r'data:\s*(\{.*?\})\s*(?:\n|$)', resp_text, re.DOTALL)
                            for chunk in reversed(chunks):
                                chunk = chunk.strip()
                                if chunk == "[DONE]":
                                    continue
                                try:
                                    chunk_json = json.loads(chunk)
                                    usage = chunk_json.get("usage", {})
                                    if usage:
                                        model = chunk_json.get("model", "")
                                        log_usage(
                                            model=model,
                                            prompt_tokens=usage.get("prompt_tokens", 0),
                                            completion_tokens=usage.get("completion_tokens", 0),
                                            total_tokens=usage.get("total_tokens", 0),
                                        )
                                        break
                                except json.JSONDecodeError:
                                    continue
                        else:
                            # 非流式响应: 直接解析 JSON
                            body_json = json.loads(resp_body)
                            usage = body_json.get("usage", {})
                            model = body_json.get("model", "")
                            if usage:
                                log_usage(
                                    model=model,
                                    prompt_tokens=usage.get("prompt_tokens", 0),
                                    completion_tokens=usage.get("completion_tokens", 0),
                                    total_tokens=usage.get("total_tokens", 0),
                                )
                    except Exception:
                        pass

                self.send_response(resp.status)
                for key, value in resp_headers.items():
                    if key.lower() in ("transfer-encoding", "connection", "content-encoding"):
                        continue
                    self.send_header(key, value)
                self._cors()
                self.end_headers()
                self.wfile.write(resp_body)

        except urllib.error.HTTPError as e:
            err_body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(err_body)
            print(f"[错误] DeepSeek 返回 {e.code}: {err_body.decode()[:200]}")

        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
            print(f"[错误] 代理异常: {e}")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

# ===== 启动 =====
def main():
    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    print("=" * 60)
    print("  DeepSeek API Token 用量自动记录器")
    print("=" * 60)
    print(f"  代理地址:     http://localhost:{LISTEN_PORT}")
    print(f"  统计面板:     http://localhost:{LISTEN_PORT}")
    print(f"  数据接口:     http://localhost:{LISTEN_PORT}/stats")
    print(f"  数据文件:     {DATA_FILE}")
    print(f"  目标后端:     https://{TARGET_HOST}")
    print(f"  API Key:      {DEFAULT_API_KEY[:12]}*** (自动注入)")
    print("=" * 60)
    print()
    print("所有发往 http://localhost:8890 的请求会自动转发到 DeepSeek")
    print("Token 用量自动记录，打开 http://localhost:8890 查看")
    print("按 Ctrl+C 停止服务")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        server.shutdown()

if __name__ == "__main__":
    main()
