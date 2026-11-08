#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent_hub.py —— serial_debug 多实例聚合网关(多板子 × 一个 AI Agent 入口)。

背景: serial_debug.py 一个进程只管一个串口, agent_http.py 的 API 面固定单 Tab;
要同时看多块板子, 只能一个实例配一个 MCP 入口, 板子多了入口爆炸。本网关把
127.0.0.1 上所有 serial_debug 实例自动发现并聚合成 QSerial 风格的多 Tab API,
mcp_bridge.py 不用改, ZCode 只配一个 MCP 入口就能看所有板子:

    ZCode --MCP--> mcp_bridge.py --port 8300 --> agent_hub.py(本文件)
        |-- 8321 serial_debug(ttyUSB0)   人看终端1
        |-- 8322 serial_debug(ttyUSB1)   人看终端2
        `-- ... 扫描区间内所有实例

设计约定:
    * Tab 序号 = 扫描到的实例按 agent 端口升序排列的位置(每次请求实时解析,
      实例退出后序号会前移, AI 用前先 list_tabs 即可, 每项带 agent_port 可定位);
    * 响应包络与 agent_http.py 完全一致({"ok":..,"data"/"error"}), 桥零适配;
    * 网关无状态不缓存: 每次 list/status 现场并行探测, 实例上下线立即反映;
    * 不代开进程: POST /api/tabs 回 409 并附启动命令提示(实例必须活在终端/
      tmux 里, 人要看日志、要能手工键入, headless 没有意义);
    * X-Agent-Token 透传: 实例若设了令牌, 调用方带对应令牌经网关原样转发。

用法:
    python3 agent_hub.py                       # 监听 8300, 扫描 8321..8400
    python3 agent_hub.py --listen 8301 --scan-min 8400 --scan-max 8500
    python3 agent_hub.py --token <令牌>        # 网关自身要求令牌

常驻运行(nohup, 日志进 logs/):
    nohup python3 agent_hub.py >> logs/hub.log 2>&1 &
"""

import argparse
import http.client
import json
import os
import re
import threading
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HUB_VERSION = "1.2"
MAX_BODY_BYTES = 1024 * 1024          # 与 agent_http.py 同限, 转发体不做放大
PROBE_WORKERS = 16                    # 扫描区间并行探测线程数
INSTANCE_NAME = "serial_debug"        # /api/status 名字非此者不收编(避免误收别的 HTTP 服务)
HINT_SCAN_BYTES = 64 * 1024           # 被动取证每次回看账本尾部的字节数
HINT_TIMEOUT = 5.0                    # 取证请求超时秒数
# 板名映射文件按"上报的串口路径(或其 basename)"匹配, 值形如 {"name": "TQ3588", "note": "..."}
BOARDS_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boards.json")
# 被动取证签名(只读账本, 绝不发字节): 每类取出现最多的一条, 拼成 hint 给 AI 对照
_HINT_PATTERNS = (
    ("host", re.compile(r"(?:root|admin)@([A-Za-z0-9][A-Za-z0-9_.-]{2,30})")),
    ("soc", re.compile(r"(?:Rockchip[ _]?RK|r[Kk])(35\d{2})")),
    ("dtb", re.compile(r"\b([A-Za-z0-9_-]{3,40}\.dtb)\b")),
    ("uboot", re.compile(r"(U-Boot 20\d\d[\w.+-]{2,20})")),
)
# 探测/转发要捕获的网络异常: URLError(含 HTTPError 超类)/OSError(含 socket 超时)/
# HTTPException(端口上蹲着非 HTTP 服务时 BadStatusLine 等, 它不是 OSError 子类)
_NET_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException)


class AgentApiError(Exception):
    """携带 HTTP 状态码的业务错误, 由协议层转为 JSON 错误响应(与 agent_http.py 同构)。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Instance:
    """一个已被发现的 serial_debug 实例(= 聚合后的一个 Tab)。"""

    __slots__ = ("agent_port", "status")

    def __init__(self, agent_port, status):
        self.agent_port = agent_port
        self.status = status            # 其 /api/status 的 data 字段

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.agent_port}"

    def fetch_tabs(self, timeout):
        """取该实例的 Tab 表(单实例固定 1 个 Tab 0), 失败抛 URLError/OSError。"""
        return _request_json("GET", self.base_url + "/api/tabs", timeout=timeout)

    def forward(self, method, path_and_query, body, token, timeout):
        """把 /api/tabs/0/<action> 请求原样转发给实例, 返回 (状态码, 响应体字节)。"""
        request = urllib.request.Request(self.base_url + path_and_query,
                                         data=body, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json; charset=utf-8")
        if token:
            request.add_header("X-Agent-Token", token)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:   # 实例的业务错误(409/400..)原样透传
            return error.code, error.read()


class Hub:
    """发现与聚合逻辑; 协议路由在 _Handler, 状态只在本类。"""

    def __init__(self, host, listen_port, scan_min, scan_max,
                 probe_timeout=1.0, forward_timeout=30.0, token="", boards_path=None):
        if scan_min <= listen_port <= scan_max:
            raise SystemExit(f"[ERROR] 监听端口 {listen_port} 落在扫描区间 "
                             f"[{scan_min},{scan_max}] 内, 会自己发现自己, 请换端口")
        self.host = host
        self.listen_port = listen_port
        self.scan_min = scan_min
        self.scan_max = scan_max
        self.probe_timeout = probe_timeout
        self.forward_timeout = forward_timeout
        self.token = token or ""
        self.boards_path = boards_path
        self._boards_cache = (None, {})   # (mtime, 映射表), mtime 变了才重读(免重启)
        self._hint_cache = {}             # agent_port -> (capture_bytes, hint)
        self._pool = ThreadPoolExecutor(PROBE_WORKERS)
        self._log_lock = threading.Lock()
        self._last_seen = ()            # 上次打印过的实例端口集合, 变化时记一行

    # ------------------------------------------------------------ 发现

    def discover(self):
        """并行探测扫描区间, 返回按端口升序的实例列表。"""
        ports = range(self.scan_min, self.scan_max + 1)
        found = [inst for inst in self._pool.map(self._probe, ports) if inst]
        self._log_change(found)
        return found

    def _probe(self, port):
        try:
            payload = _request_json("GET", f"http://127.0.0.1:{port}/api/status",
                                    timeout=self.probe_timeout)
        except (*_NET_ERRORS, ValueError):
            return None
        data = payload.get("data") or {}
        # 认 serial_debug 的 status 面; 名字不符的 HTTP 服务(别的工具)不收编
        if payload.get("ok") and data.get("name") == INSTANCE_NAME:
            return Instance(port, data)
        return None

    def instance_at(self, index):
        """按聚合 Tab 序号取实例; 越界报 404(提示先 list_tabs 对齐序号)。"""
        found = self.discover()
        if not 0 <= index < len(found):
            hint = ",".join(f"{i.agent_port}({i.status.get('port')})" for i in found) or "无实例在线"
            raise AgentApiError(404, f"Tab {index} 不存在(当前在线: {hint}); "
                                     f"实例增减后序号会变, 请先 GET /api/tabs 对齐")
        return found[index]

    def _log_change(self, found):
        """实例集合变化时向 stdout 记一行(nohup 重定向即为网关日志)。"""
        now = tuple(i.agent_port for i in found)
        with self._log_lock:
            if now != self._last_seen:
                self._last_seen = now
                detail = ", ".join(f"{i.agent_port}->{i.status.get('port')}" for i in found)
                print(f"[hub] 实例集合更新: [{detail or '空'}]", flush=True)

    # ------------------------------------------------------------ 聚合 API

    def status(self):
        found = self.discover()
        return {"name": "serial_hub", "version": HUB_VERSION,
                "agent_for": f"{INSTANCE_NAME} 1.1+agent",
                "listen_port": self.listen_port,
                "scan_range": [self.scan_min, self.scan_max],
                "tab_count": len(found),
                "open": any(i.status.get("open") for i in found),
                "instances": [{"agent_port": i.agent_port, **{
                    k: i.status.get(k) for k in ("port", "open", "tab_count")}}
                    for i in found]}

    def list_tabs(self):
        """并行取各实例 Tab 表, 重排 index 为聚合序号并附 agent_port/板名/取证 hint。"""
        found = self.discover()
        entries = []
        for index, inst in enumerate(found):
            try:
                payload = inst.fetch_tabs(self.probe_timeout)
            except (*_NET_ERRORS, ValueError) as error:
                raise AgentApiError(502, f"实例 {inst.agent_port} 取 Tab 失败: {error}")
            if not payload.get("ok"):
                raise AgentApiError(502, f"实例 {inst.agent_port} 返回错误: "
                                         f"{payload.get('error', '未知')}")
            tabs = payload.get("data", {}).get("tabs") or []
            if not tabs:
                raise AgentApiError(502, f"实例 {inst.agent_port} 未返回 Tab")
            entry = dict(tabs[0])
            entry["index"] = index            # 单实例 Tab 0 -> 聚合序号
            entry["agent_port"] = inst.agent_port
            # ② 板名: boards.json 人工映射优先(权威, 可带备注)
            board = self._board_of(entry.get("port"))
            entry["board"] = board.get("name") if board else None
            if board and board.get("note"):
                entry["board_note"] = board["note"]
            # ③ 被动取证: 只读账本尾部提取签名(零字节发送)
            entry["hint"], parts = self._hint_of(inst)
            entry["board_auto"] = self._auto_name(parts)   # 未登记板自动派生板名
            entries.append(entry)
        self._finalize_titles(entries)
        return {"tabs": entries}

    def _finalize_titles(self, entries):
        """定标题: 人工名 > 自动名 > 原串口名; 同名撞车一律加物理插口后缀。

        两块同型号板(如两台 TQ3588)会派生出同名, 撞车时全部加 @插口 短后缀,
        保证 Tab 标题两两不同——AI 认板子的最后防线。
        """
        names = [e.get("board") or e.get("board_auto") for e in entries]
        duplicate = {n for n in names if n and names.count(n) > 1}
        for entry, name in zip(entries, names):
            if not name:
                continue
            entry["title"] = name if name not in duplicate else f"{name}@{self._jack_of(entry.get('port'))}"
            entry["name_source"] = "boards.json" if entry.get("board") else "hint自动"

    @staticmethod
    def _jack_of(port):
        """物理插口短名: by-path 提 hub 链路段(如 1.4.4), 其余取 basename(如 ttyUSB3)。"""
        m = re.search(r"usb-([0-9][\d.:]*)", port or "")
        if m:
            segs = m.group(1).split(":")
            return segs[1] if len(segs) > 1 else segs[0]
        return os.path.basename(port or "") or "?"

    # ------------------------------------------------------------ 板名映射 / 被动取证

    def _boards(self):
        """读 boards.json; mtime 没变用缓存, 改完文件即生效不用重启网关。"""
        if not self.boards_path or not os.path.isfile(self.boards_path):
            return {}
        mtime = os.stat(self.boards_path).st_mtime
        if self._boards_cache[0] != mtime:
            try:
                with open(self.boards_path, encoding="utf-8") as f:
                    mapping = json.load(f)
            except (OSError, ValueError) as e:
                print(f"[hub] boards.json 读取失败(沿用旧表): {e}", flush=True)
                mapping = self._boards_cache[1]
            self._boards_cache = (mtime, mapping if isinstance(mapping, dict) else {})
        return self._boards_cache[1]

    def _board_of(self, port):
        """按上报串口路径全串/basename 两级匹配板名条目(by-path 与 ttyUSBx 写法都认)。"""
        if not port:
            return None
        mapping = self._boards()
        entry = mapping.get(port) or mapping.get(os.path.basename(port))
        return entry if isinstance(entry, dict) else None

    def _hint_of(self, inst):
        """被动取证: 回看实例账本尾部提取 host/SoC/dtb/U-Boot 签名。

        绝不发字节、不动实例读游标(offset 绝对读); 账本字数没涨就沿用上次结论,
        任何失败都返回 (None, {}) 不阻塞 Tab 列表——取证是兜底不是依赖。
        返回 (hint 串, {签名类型: 值}) 两份, 后者供自动命名用。
        """
        try:
            payload = _request_json("GET", inst.base_url + "/api/info",
                                    timeout=self.probe_timeout)
            cap = (payload.get("data") or {}).get("capture_bytes") or 0
        except (*_NET_ERRORS, ValueError):
            return None, {}
        cached = self._hint_cache.get(inst.agent_port)
        if cached and cached[0] == cap:
            return cached[1], cached[2]
        if cap <= 0:
            self._hint_cache[inst.agent_port] = (cap, None, {})
            return None, {}
        offset = max(0, cap - HINT_SCAN_BYTES)
        try:
            payload = _request_json(
                "GET",
                f"{inst.base_url}/api/tabs/0/receive?offset={offset}&max_bytes={HINT_SCAN_BYTES}",
                timeout=HINT_TIMEOUT)
            text = (payload.get("data") or {}).get("text") or ""
        except (*_NET_ERRORS, ValueError):
            return None, {}
        parts = {}
        for key, pattern in _HINT_PATTERNS:
            hits = pattern.findall(text)
            if hits:
                parts[key] = Counter(hits).most_common(1)[0][0]
        shown = {k: ("RK" + v if k == "soc" else v) for k, v in parts.items()}
        hint = " ".join(f"{k}:{v}" for k, v in shown.items()) or None
        self._hint_cache[inst.agent_port] = (cap, hint, parts)
        return hint, parts

    @staticmethod
    def _auto_name(parts):
        """从取证签名派生板名: 主机名 > SoC > dtb 名(去扩展名); 静默板返回 None 不猜。"""
        if parts.get("host"):
            return parts["host"]
        if parts.get("soc"):
            return "RK" + parts["soc"]
        if parts.get("dtb", "").endswith(".dtb"):
            return parts["dtb"][:-4]
        return None

    def proxy(self, index, action, method, query, body, token):
        """/api/tabs/{i}/{action} -> 实例 /api/tabs/0/{action}, 字节级透传。

        返回 (状态码, 响应体字节): 实例回的已是完整 {"ok":..} 包络(含业务错误),
        原样上送, 网关不重打包——大块 receive 不多吃一遍 JSON 序列化。
        """
        if action not in ("open", "close", "send", "receive"):
            raise AgentApiError(404, f"未知操作: {action}")
        inst = self.instance_at(index)
        path = f"/api/tabs/0/{action}"
        if query:
            path += "?" + query
        try:
            status, payload = inst.forward(method, path, body, token, self.forward_timeout)
        except _NET_ERRORS as error:
            raise AgentApiError(502, f"实例 {inst.agent_port} 无响应(可能刚退出): {error}")
        if not payload:   # 异常对端回空体, 补一个合法错误包络
            payload = json.dumps({"ok": False, "error": f"实例 {inst.agent_port} 返回空响应"},
                                 ensure_ascii=False).encode("utf-8")
        return status, payload

    def refuse_create(self):
        raise AgentApiError(
            409, "hub 只聚合已启动的 serial_debug 实例, 不代开进程; 新板子请运行: "
                 "python3 serial_debug.py -p /dev/serial/by-path/<物理口路径> "
                 "(ttyUSBx 会因重插变号, by-path 稳定) -b <波特率> --agent-http "
                 f"<{self.scan_min}..{self.scan_max} 的空闲端口>, "
                 "在终端/tmux 里启动(人要看日志), 出现后自动进 Tab 列表; "
                 f"板名登记: {self.boards_path or 'boards.json'}")


def _request_json(method, url, body=None, timeout=5.0):
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class _Handler(BaseHTTPRequestHandler):
    """网关协议层: 令牌校验、路由分发、JSON 编解码(面与 agent_http.py 保持一致)。"""

    server_version = f"SerialAgentHub/{HUB_VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, _fmt, *_args):   # 常驻网关, 静默逐请求日志(集合变化由 hub 记)
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        hub = self.server.hub
        try:
            if hub.token and self.headers.get("X-Agent-Token") != hub.token:
                raise AgentApiError(401, "访问令牌缺失或不匹配")
            parts = urlsplit(self.path)
            seg = [s for s in parts.path.split("/") if s]
            status, raw, payload = self._route(hub, method, seg, parts.query)
            if raw:   # 实例回的完整包络, 字节级透传
                self._send_bytes(status, payload)
            else:
                self._send_json(status, {"ok": True, "data": payload})
        except AgentApiError as e:
            self._send_json(e.status, {"ok": False, "error": e.message})
        except Exception as e:   # 网关自身意外也回 JSON, 不给客户端挂起
            self._send_json(500, {"ok": False, "error": f"网关内部错误: {e}"})

    def _route(self, hub, method, seg, query):
        """返回 (状态码, 是否原始包络, 数据对象或字节)。"""
        if len(seg) < 2 or seg[0] != "api":
            raise AgentApiError(404, "未知路径")
        if seg[1] == "status" and len(seg) == 2:
            self._need(method, "GET")
            return 200, False, hub.status()
        if seg[1] == "ports" and len(seg) == 2:
            self._need(method, "GET")
            return 200, False, {"ports": _list_serial_ports()}
        if seg[1] == "info" and len(seg) == 2:
            self._need(method, "GET")
            return 200, False, hub.status()      # 网关无自有账本, info 即聚合状态
        if seg[1] == "tabs":
            return self._route_tabs(hub, method, seg, query)
        raise AgentApiError(404, "未知路径")

    def _route_tabs(self, hub, method, seg, query):
        if len(seg) == 2:
            if method == "GET":
                return 200, False, hub.list_tabs()
            if method == "POST":
                self._read_body()
                hub.refuse_create()      # 必抛 409(附新板子上线方法)
            raise AgentApiError(405, "仅支持 GET/POST")
        if len(seg) != 4 or not seg[2].isdigit():
            raise AgentApiError(404, "路径应为 /api/tabs/{index}/{action}")
        body = self._read_body() if method == "POST" else None
        token = self.headers.get("X-Agent-Token", "")
        status, payload = hub.proxy(int(seg[2]), seg[3], method, query, body, token)
        return status, True, payload

    @staticmethod
    def _need(method, want):
        if method != want:
            raise AgentApiError(405, f"仅支持 {want}")

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            raise AgentApiError(400, f"请求体应为 0..{MAX_BODY_BYTES} 字节")
        return self.rfile.read(length) if length else b""

    def _send_json(self, status, payload):
        self._send_bytes(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _send_bytes(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _list_serial_ports():
    try:
        from serial.tools import list_ports
        return [p.device for p in list_ports.comports()]
    except ImportError:
        return []            # 网关主机没装 pyserial 时不阻塞 status/tabs, 只少了枚举


def main():
    parser = argparse.ArgumentParser(description="serial_debug 多实例聚合网关")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址, 默认 127.0.0.1")
    parser.add_argument("--listen", type=int, default=8300, metavar="PORT",
                        help="网关监听端口, 默认 8300(须在扫描区间外)")
    parser.add_argument("--scan-min", type=int, default=8321, metavar="PORT",
                        help="实例扫描起始端口(含), 默认 8321")
    parser.add_argument("--scan-max", type=int, default=8400, metavar="PORT",
                        help="实例扫描结束端口(含), 默认 8400")
    parser.add_argument("--probe-timeout", type=float, default=1.0,
                        help="实例探测超时秒数, 默认 1.0")
    parser.add_argument("--forward-timeout", type=float, default=30.0,
                        help="转发请求超时秒数(大块读取要留足), 默认 30")
    parser.add_argument("--token", default="", help="网关访问令牌, 非空时请求须带 X-Agent-Token")
    parser.add_argument("--boards", default=BOARDS_DEFAULT, metavar="FILE",
                        help=f"板名映射 JSON, 默认 {BOARDS_DEFAULT}")
    args = parser.parse_args()

    hub = Hub(args.host, args.listen, args.scan_min, args.scan_max,
              probe_timeout=args.probe_timeout, forward_timeout=args.forward_timeout,
              token=args.token, boards_path=args.boards)
    server = ThreadingHTTPServer((args.host, args.listen), _Handler)
    server.hub = hub
    server.daemon_threads = True
    print(f"[hub] serial_debug 聚合网关 v{HUB_VERSION} 监听 "
          f"http://{args.host}:{args.listen} (扫描 {args.scan_min}..{args.scan_max})", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        hub._pool.shutdown(wait=False)
        server.server_close()


if __name__ == "__main__":
    main()
