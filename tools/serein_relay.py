"""Expose one private WeChat memory window to a loopback SSH forward."""
import argparse
from datetime import datetime
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Serein:
    def __init__(self, core, token_file):
        dest = urlsplit(core)
        if (dest.scheme != "http" or dest.hostname != "127.0.0.1" or not dest.port
                or dest.netloc != f"127.0.0.1:{dest.port}" or dest.path not in ("", "/") or dest.query or dest.fragment):
            raise ValueError("Serein 核心地址应为本机回环地址")
        self.core = core.rstrip("/")
        self.token_file = token_file
        self.http = build_opener(ProxyHandler({}), NoRedirect())

    def call(self, path, data=None):
        request = Request(self.core + path, data=json.dumps(data).encode() if data is not None else None,
                          headers={"Authorization": "Bearer " + self.token_file.read_text().strip(),
                                   "Content-Type": "application/json"})
        with self.http.open(request, timeout=10) as response:
            return json.load(response)


def relay(core, window, path, data):
    if data.get("window") != window:
        raise PermissionError
    if path == "/health":
        health = core.call("/health")
        return {"ok": health.get("status") in ("ok", "ready", "healthy") and not health.get("read_only")}
    if path == "/recall":
        query, ids = data.get("query"), data.get("delivered_ids")
        if (not isinstance(query, str) or not 0 < len(query) <= 16000 or not isinstance(ids, list)
                or len(ids) > 100 or not all(isinstance(i, str) and len(i) <= 300 for i in ids)):
            raise ValueError
        row = core.call("/api/hook/recall", {"session_id": window, "query": query, "max_notes": 2,
                                             "delivered_ids": ids, "max_context_chars": 2400})
        if not row.get("ok") or not isinstance(row.get("additional_context"), str) or not isinstance(row.get("recalled_ids"), list):
            raise RuntimeError
        return {"ok": True, "note": row["additional_context"], "ids": row["recalled_ids"]}
    if path != "/archive":
        raise LookupError
    events = data.get("events")
    if not isinstance(events, list) or not 1 <= len(events) <= 8:
        raise ValueError
    originals = []
    for e in events:
        if (not isinstance(e, dict) or e.get("role") not in ("user", "assistant")
                or not isinstance(e.get("text"), str) or not e["text"].strip()
                or not isinstance(e.get("source_event_id"), str) or not re.fullmatch("[a-f0-9]{64}", e["source_event_id"])
                or not isinstance(e.get("created_at"), str) or not datetime.fromisoformat(e["created_at"]).tzinfo):
            raise ValueError
        originals.append({k: e[k] for k in ("source_event_id", "role", "text", "created_at")})
    row = core.call("/api/ingest-raw", {"source": "wechat_voice_bridge", "client": "wechat",
                                       "session_id": window, "conversation_id": window, "events": originals})
    ids = [e["source_event_id"] for e in originals]
    if (not row.get("ok") or row.get("rejected") != 0 or
            [i.get("source_event_id") for i in row.get("items", []) if i.get("status") in ("inserted", "duplicate")] != ids):
        raise RuntimeError
    return {"ok": True, "ids": ids}


def server(pair, core, port):
    if not re.fullmatch(r"wx-private-[a-f0-9]{64}", pair["window"]) or len(pair["token"]) < 32:
        raise ValueError("无效的私聊配对文件")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, data):
            raw = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + pair["token"]).encode()):
                return self.reply(403, {"ok": False})
            try:
                length = int(self.headers.get("Content-Length", 0))
                if self.headers.get_content_type() != "application/json" or not 0 < length <= 2*1024*1024:
                    raise ValueError
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError
                result = relay(core, pair["window"], self.path, data)
            except PermissionError:
                return self.reply(403, {"ok": False})
            except (ValueError, TypeError):
                return self.reply(400, {"ok": False})
            except LookupError:
                return self.reply(404, {"ok": False})
            except (OSError, RuntimeError):
                return self.reply(502, {"ok": False})
            return self.reply(200, result)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", type=Path, required=True)
    parser.add_argument("--core", default="http://127.0.0.1:28218")
    parser.add_argument("--core-token-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=28229)
    args = parser.parse_args()
    server(json.loads(args.pair.read_text()), Serein(args.core, args.core_token_file), args.port).serve_forever()
