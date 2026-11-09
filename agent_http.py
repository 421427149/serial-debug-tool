#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent_http.py —— 为 serial_debug.py 增加本地 AI Agent 通道(HTTP API)。

设计目标("真实 + 全面", 与屏显互不依赖, 人看终端的同时 AI 也能看同一份流):
    * AI 读的不是会丢的内存缓冲, 而是带绝对偏移的会话捕获文件(磁盘账本):
      读过的不过期、带 offset 可任意重读, 洪水刷屏下 AI 侧不丢字节;
    * 对账自查: /api/info 暴露 rx_total(串口实收)与 capture_bytes(已落盘),
      两者恒等即无静默丢失; 对不上说明驱动层/磁盘层出过问题, 必现形;
    * 原始字节为真身: 读接口同时返回原始字节 HEX 与按当前编码解码的文本,
      文本附 decode_errors 计数, 解码失真不隐瞒;
    * 事件入流: note() 的每条系统消息与每次发送(带 来源=human/agent、
      单调时间戳、原始字节 b64)都成为带递增 seq 的事件, AI 能还原对话现场
      (谁在什么时刻发了什么、串口何时开断、波特率何时切换);
    * API 面与 QSerial 的 AI Agent 服务兼容(单会话映射为固定 Tab 0),
      其 stdio MCP 桥 mcp_bridge.py 可原样复用(本目录有一份拷贝)。

用法(经 serial_debug.py):
    python serial_debug.py -p /dev/ttyUSB0 -b 1500000 --agent-http          # 端口 8321
    python serial_debug.py --agent-http 9000 --agent-token <令牌>

安全: 默认只绑 127.0.0.1; 设置令牌后所有请求须带 X-Agent-Token 头。
send/open/close 会真实操作设备, 请仅在可信环境开启。
"""

import base64
import json
import queue
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from serial_debug import NEWLINES   # 发送行尾符与主工具同一份定义

MAX_BODY_BYTES = 1024 * 1024   # 请求体上限, 防异常客户端撑爆内存
DEFAULT_READ_BYTES = 64 * 1024
MAX_READ_BYTES = 4 * 1024 * 1024
EVENT_RING = 100_000           # 内存事件环上限(超出淘汰并计数, 不静默)
MAX_EVENTS_READ = 5000
MARSHAL_WAIT_S = 2.5           # 等主循环执行 open/close/send 的超时


class AgentApiError(Exception):
    """携带 HTTP 状态码的业务错误, 由协议层转为 JSON 错误响应。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _fmt_hex(data):
    return " ".join(f"{b:02X}" for b in data)


class AgentChannel:
    """AI 通道: 读路径=会话捕获文件(磁盘账本) + 事件环; 写路径=命令队列交主线程执行。"""

    def __init__(self, tool, port=8321, host="127.0.0.1", token=""):
        self.tool = tool
        self.host = host
        self.port = int(port)
        self.token = token or ""
        self.bound_port = None
        self.started_at = time.time()
        self.server = None
        self._serve_thread = None
        self._lock = threading.Lock()      # 保护事件环/seq/cursor
        self._events = deque(maxlen=EVENT_RING)
        self._evicted = 0                  # 因环满被淘汰的事件数(AI 可据此发现缺口)
        self._next_seq = 1
        self._cursor = 0                   # 不带 offset 的 /receive 从这里续读(可被 /api/info 观察)
        self._cmd_q = queue.Queue()
        self._rd_lock = threading.Lock()
        self._rdf = None                   # 捕获文件的独立只读句柄(追加写不妨碍并发读)

    # ------------------------------------------------------------ 挂钩(serial_debug 调用)

    def note_event(self, msg):
        """系统消息入流(note() 的每条提示, 开关串口/波特率切换/异常断开等都在其中)。"""
        self._emit({"kind": "note", "text": str(msg)})

    def tx_event(self, data, source=None):
        """发送入流: 来源缺省按线程判定(主线程=人工键入, 其他线程=agent 通道)。"""
        if source is None:
            source = "human" if threading.current_thread() is threading.main_thread() else "agent"
        self._emit({"kind": "tx", "source": source, "n": len(data),
                    "b64": base64.b64encode(bytes(data)).decode("ascii")})

    def exit_event(self):
        self._emit({"kind": "exit"})

    def drain_cmds(self):
        """主循环每帧调用: 把 AI 请求的操作放到主线程执行(串口对象只在主线程动)。

        测试环境没有主循环时也可手动调用; 每帧最多执行 16 条, 防单帧被灌爆。
        """
        t = self.tool
        for _ in range(16):
            try:
                item = self._cmd_q.get_nowait()
            except queue.Empty:
                return
            try:
                if item[0] == "cmd":
                    t.exec_cmd(item[1])
                elif item[0] == "send":
                    t.serial_write(item[1], source="agent")
            except Exception as e:
                self.note_event(f"agent 命令执行失败: {e}")

    # ------------------------------------------------------------ 事件

    def _emit(self, detail):
        with self._lock:
            seq = self._next_seq
            self._next_seq += 1
            if len(self._events) == self._events.maxlen:
                self._evicted += 1
            self._events.append({"seq": seq, "t": round(time.time() - self.started_at, 6), **detail})

    def events_after(self, after_seq=0, max_records=500):
        """返回 seq > after_seq 的事件; first_seq/evicted 让 AI 能自查是否错过事件。"""
        max_records = max(1, min(int(max_records), MAX_EVENTS_READ))
        with self._lock:
            snapshot = list(self._events)
            first = snapshot[0]["seq"] if snapshot else self._next_seq
            evicted, nxt = self._evicted, self._next_seq
        selected = [e for e in snapshot if e["seq"] > after_seq][:max_records]
        return {"events": selected, "first_seq": first, "next_seq": nxt,
                "evicted": evicted, "total_events": nxt - 1}

    # ------------------------------------------------------------ 读路径(磁盘账本)

    def _snapshot_capture(self):
        """刷盘后快照账本状态; 返回 (end_offset, path, capture_on)。"""
        t = self.tool
        with t.log_lock:
            if t.capturef is not None:
                try:
                    t.capturef.flush()   # 读之前先落盘, 文件内容与账面一致
                except Exception:
                    pass
            return t.capture_bytes, t.capture_path, t.capture_on

    def _read_raw(self, offset, max_bytes):
        end, path, cap_on = self._snapshot_capture()
        if not cap_on:
            raise AgentApiError(503, "会话捕获已失效(创建失败或被 --no-capture 关闭), 账本不完整")
        if offset < 0:
            raise AgentApiError(400, "offset 不能为负")
        if offset > end:
            raise AgentApiError(400, f"offset {offset} 超出已落盘范围 0..{end}")
        if offset == end or path is None:
            return b"", end
        need = min(max_bytes, end - offset)
        with self._rd_lock:
            try:
                if self._rdf is None:
                    self._rdf = open(path, "rb")
                self._rdf.seek(offset)
                data = self._rdf.read(need)
            except Exception:
                try:   # 句柄可能被外因破坏(如磁盘重插), 重开一次再试
                    if self._rdf is not None:
                        self._rdf.close()
                except Exception:
                    pass
                self._rdf = None
                raise AgentApiError(503, "读取会话捕获文件失败")
        return data, offset + len(data)

    def receive(self, offset=None, max_bytes=DEFAULT_READ_BYTES, tail=False, wait_ms=0):
        """读接收流。带 offset: 绝对位置随机读(不动 cursor, 可反复回看);
        不带 offset: 从服务端 cursor 续读并前移(QSerial 桥的消费式轮询兼容语义);
        tail=True(仅不带 offset 时生效, 带 offset 时忽略): 跳过积压取最新窗口——
        起点 = max(cursor, 账末-max_bytes), 起点落在行中则顺延到下一行首, cursor
        前移到窗口末尾; 跳过的字节计入返回值 skipped, 仍在账本可带 offset 回看。
        wait_ms>0(仅带 offset 时生效): 长轮询——无新数据时服务端小睡等待至多
        wait_ms, 数据一到立即返回, 把浏览器终端的回显延迟从轮询间隔降到几十 ms。"""
        max_bytes = max(1, min(int(max_bytes), MAX_READ_BYTES))
        with self._lock:
            cur = self._cursor
        off = cur if offset is None else int(offset)
        skipped = 0
        if tail and offset is None:
            end = self._snapshot_capture()[0]
            off = max(cur, end - max_bytes)
            if off >= end and end > 0:
                off = max(0, end - max_bytes)   # 游标已在账末: 仍给尾窗, 否则读空
            skipped = max(0, off - cur)
        data, next_off = self._read_raw(off, max_bytes)
        if not data and offset is not None and int(wait_ms) > 0:
            deadline = time.monotonic() + min(int(wait_ms), 5000) / 1000.0
            while not data and time.monotonic() < deadline:
                time.sleep(0.03)
                data, next_off = self._read_raw(off, max_bytes)
        if skipped and data:
            nl = data.find(b"\n")   # 尾窗起点多半在行中, 半行开头无信息价值, 顺延到行首
            if nl >= 0:
                off += nl + 1
                skipped += nl + 1
                data = data[nl + 1:]
        if offset is None:
            with self._lock:
                self._cursor = next_off
                cur_after = self._cursor
        else:
            cur_after = cur
        t = self.tool
        text = data.decode(t.enc, errors="replace")
        with t.stat_lock:
            rx_total, tx_total = t.rx_total, t.tx_total
        end = self._snapshot_capture()[0]   # 读后账面末尾, AI 可据此判断是否还有后续
        return {"offset": off, "next_offset": next_off, "bytes": len(data),
                "skipped": skipped,
                "hex": _fmt_hex(data), "text": text,
                "decode_errors": text.count("\ufffd"),
                "encoding": t.enc, "cursor": cur_after,
                "rx_total": rx_total, "tx_total": tx_total, "capture_bytes": end}

    # ------------------------------------------------------------ 写路径(经主线程)

    def _submit(self, item):
        self._cmd_q.put(item)

    def _wait(self, predicate, timeout=MARSHAL_WAIT_S):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def api_open(self, body):
        t = self.tool
        if t.ser is not None:
            raise AgentApiError(409, "串口已打开")
        port = body.get("port")
        baud = body.get("baudrate")
        if port is not None:
            if not isinstance(port, str) or not port.strip():
                raise AgentApiError(400, "port 必须是非空字符串")
            self._submit(("cmd", "/port " + port.strip()))
        if baud is not None:
            if not isinstance(baud, int) or isinstance(baud, bool) or not 300 <= baud <= 4_000_000:
                raise AgentApiError(400, "baudrate 必须是 300–4000000 的整数")
            self._submit(("cmd", f"/baud {baud}"))
        self._submit(("cmd", "/open"))
        if not self._wait(lambda: t.ser is not None):
            raise AgentApiError(503, "打开超时(主循环未运行或设备打开失败, 见事件流)")
        return {"index": 0, "open": True}

    def api_close(self):
        t = self.tool
        if t.ser is None:
            raise AgentApiError(409, "串口未打开")
        self._submit(("cmd", "/close"))
        if not self._wait(lambda: t.ser is None):
            raise AgentApiError(503, "关闭超时(主循环未运行?)")
        return {"index": 0, "open": False}

    def api_send(self, body):
        t = self.tool
        has_text, has_hex = "text" in body, "hex" in body
        if has_text == has_hex:
            raise AgentApiError(400, "text 与 hex 必须提供且只能提供其一")
        if t.ser is None:
            raise AgentApiError(409, "串口未打开")
        if has_text:
            if not isinstance(body["text"], str) or not body["text"]:
                raise AgentApiError(400, "text 必须是非空字符串")
            data = body["text"].encode(t.enc, errors="replace")
            if body.get("nl", True):   # 浏览器键击直通模式按字符发送, 不自动加行尾
                data += NEWLINES[t.nl]
        else:
            hx = "".join(body["hex"].split()).lower() if isinstance(body["hex"], str) else ""
            if not hx or len(hx) % 2 or not all(c in "0123456789abcdef" for c in hx):
                raise AgentApiError(400, "hex 应为偶数个十六进制字符, 如 \"01 A2\"")
            data = bytes.fromhex(hx)
        if len(data) > MAX_BODY_BYTES:
            raise AgentApiError(400, "发送内容过大")
        with t.stat_lock:
            tx0 = t.tx_total
        self._submit(("send", data))
        if not self._wait(lambda: t.tx_total > tx0):
            raise AgentApiError(503, "发送未被主循环消费(未运行?)")
        return {"index": 0, "sent": True, "bytes": len(data)}

    # ------------------------------------------------------------ 状态

    def info(self):
        t = self.tool
        with t.stat_lock:
            rx, tx = t.rx_total, t.tx_total
        with t.pend_lock:
            pend = len(t.pending)
        cap_bytes, cap_path, cap_on = self._snapshot_capture()
        with self._lock:
            cur, nxt, evicted = self._cursor, self._next_seq, self._evicted
        return {
            "name": "serial_debug", "version": "1.2+agent",
            "host": self.host, "port": self.bound_port, "token_required": bool(self.token),
            "uptime_s": round(time.time() - self.started_at, 3),
            "raw_send": True,
            "port": t.port, "baud": t.baud, "open": t.ser is not None,
            "bits": t.bits, "parity": t.parity, "stopbits": t.stopbits, "flow": t.flow,
            "encoding": t.enc, "newline": t.nl, "raw_mode": t.raw,
            "rx_total": rx, "tx_total": tx, "capture_bytes": cap_bytes,
            "capture_path": cap_path, "capture_ok": cap_on,
            "capture_consistent": (rx == cap_bytes),
            "display_pending_bytes": pend,
            "display_hidden_bytes": t.hidden_total + t.hidden_mark,
            "read_cursor": cur, "unread_bytes": max(0, cap_bytes - cur),
            "next_event_seq": nxt, "total_events": nxt - 1, "events_evicted": evicted,
        }

    def tab_entry(self):
        t = self.tool
        with t.stat_lock:
            rx, tx = t.rx_total, t.tx_total
        with self._lock:
            cur = self._cursor
        cap_bytes = t.capture_bytes
        return {"index": 0, "title": t.port or "未设置", "port": t.port,
                "open": t.ser is not None, "baud": t.baud, "encoding": t.enc,
                "rx_count": rx, "tx_count": tx,
                "pending_bytes": max(0, cap_bytes - cur)}

    # ------------------------------------------------------------ 生命周期

    def start(self):
        self.server = ThreadingHTTPServer((self.host, self.port), _Handler)
        self.server.agent = self
        self.server.daemon_threads = True
        self.bound_port = self.server.server_address[1]
        self._serve_thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        self._serve_thread.start()

    def stop(self):
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
            self.server = None
        with self._rd_lock:
            if self._rdf is not None:
                try:
                    self._rdf.close()
                except Exception:
                    pass
                self._rdf = None


class _Handler(BaseHTTPRequestHandler):
    """把 /api/ 请求路由到 AgentChannel; 令牌校验、请求体限制、JSON 编解码在此完成。"""

    server_version = "SerialDebugAgent/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, _fmt, *_args):   # 控制台工具, 静默访问日志
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        try:
            ch = self.server.agent
            if ch.token and self.headers.get("X-Agent-Token") != ch.token:
                raise AgentApiError(401, "访问令牌缺失或不匹配")
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            data = self._route(ch, method,
                               [s for s in parts.path.split("/") if s], query)
            self._send_json(200, {"ok": True, "data": data})
        except AgentApiError as e:
            self._send_json(e.status, {"ok": False, "error": e.message})
        except Exception as e:   # 后端意外异常也回 JSON, 不给客户端挂起
            self._send_json(500, {"ok": False, "error": f"内部错误: {e}"})

    def _route(self, ch, method, seg, query):
        if len(seg) < 2 or seg[0] != "api":
            raise AgentApiError(404, "未知路径")
        r = seg[1]
        if r == "status" and len(seg) == 2:
            self._need(method, "GET")
            t = ch.tool
            return {"name": "serial_debug", "version": "1.2+agent",
                    "token_required": bool(ch.token), "open": t.ser is not None,
                    "port": t.port, "tab_count": 1}
        if r == "ports" and len(seg) == 2:
            self._need(method, "GET")
            from serial.tools import list_ports
            return {"ports": [p.device for p in list_ports.comports()]}
        if r == "info" and len(seg) == 2:
            self._need(method, "GET")
            return ch.info()
        if r == "events" and len(seg) == 2:
            self._need(method, "GET")
            after = self._int_arg(query, "after_seq", 0)
            mx = self._int_arg(query, "max", 500)
            return ch.events_after(after, mx)
        if r == "tabs":
            return self._route_tabs(ch, method, seg, query)
        raise AgentApiError(404, "未知路径")

    def _route_tabs(self, ch, method, seg, query):
        if len(seg) == 2:
            if method == "GET":
                return {"tabs": [ch.tab_entry()]}
            if method == "POST":
                self._read_body()
                raise AgentApiError(409, "本工具为单会话, 固定 Tab 0, 无需新建")
            raise AgentApiError(405, "仅支持 GET/POST")
        if seg[2] != "0":
            raise AgentApiError(404, f"Tab {seg[2]} 不存在(本工具固定 Tab 0)")
        action = seg[3] if len(seg) > 3 else None
        if action == "open":
            self._need(method, "POST")
            return ch.api_open(self._read_body())
        if action == "close":
            self._need(method, "POST")
            self._read_body()
            return ch.api_close()
        if action == "send":
            self._need(method, "POST")
            return ch.api_send(self._read_body())
        if action == "receive":
            self._need(method, "GET")
            offset = self._int_arg(query, "offset", None)
            mx = self._int_arg(query, "max_bytes", DEFAULT_READ_BYTES)
            wait = self._int_arg(query, "wait_ms", 0)
            return ch.receive(offset, mx, self._bool_arg(query, "tail", False), wait)
        raise AgentApiError(404, "未知操作")

    # ------------------------------------------------------------ 小工具

    @staticmethod
    def _need(method, want):
        if method != want:
            raise AgentApiError(405, f"仅支持 {want}")

    @staticmethod
    def _int_arg(query, name, default):
        if name not in query:
            return default
        try:
            v = int(query[name][0])
        except ValueError:
            raise AgentApiError(400, f"{name} 必须是整数")
        if v < 0:
            raise AgentApiError(400, f"{name} 不能为负")
        return v

    @staticmethod
    def _bool_arg(query, name, default):
        if name not in query:
            return default
        v = query[name][0].strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("", "0", "false", "no", "off"):
            return False
        raise AgentApiError(400, f"{name} 必须是布尔值(1/true/0/false)")

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > MAX_BODY_BYTES:
            raise AgentApiError(400, "请求体过大")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AgentApiError(400, "请求体必须是 UTF-8 JSON")
        if not isinstance(body, dict):
            raise AgentApiError(400, "请求体必须是 JSON 对象")
        return body

    def _send_json(self, status, payload):
        try:
            blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
        except (BrokenPipeError, ConnectionResetError):
            pass


def attach(tool, port=8321, host="127.0.0.1", token=""):
    """给 SerialTool 挂上 AI 通道并启动 HTTP 服务, 返回通道对象。

    绑定失败抛 OSError, 由调用方决定降级方式。
    """
    ch = AgentChannel(tool, port=port, host=host, token=token)
    tool.agent = ch
    ch.start()
    tool.note(f"AI Agent 通道已开启: http://{host}:{ch.bound_port}/api/"
              f" (令牌{'已设置' if token else '未设置'}, /api/info 为对账入口)")
    return ch
