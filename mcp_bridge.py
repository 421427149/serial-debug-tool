"""QSerial MCP stdio 桥接：把 MCP 工具调用转发到 QSerial 内置 HTTP 服务。

由 MCP 客户端（如 ZCode、Claude Desktop）作为子进程启动，通过标准输入输出按
newline-delimited JSON-RPC 2.0 通信；本脚本不直接操作串口，串口能力全部经
HTTP API 访问。用法示例：

    python src/main/mcp_bridge.py --port 8321 --token <令牌>
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROTOCOL_VERSION = "2024-11-05"
BRIDGE_VERSION = "1.0"

_TAB_INDEX_SCHEMA = {
    "type": "integer",
    "minimum": 0,
    "description": "工作 Tab 序号，先调用 qserial_list_tabs 获取",
}

TOOL_DEFINITIONS = [
    {
        "name": "qserial_status",
        "description": "获取 QSerial 服务状态与应用版本。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "qserial_list_ports",
        "description": "列出本机当前可用的串口。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "qserial_list_tabs",
        "description": "列出全部工作 Tab，包括 index、串口号、开关状态、收发计数和待读取字节数。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "qserial_create_tab",
        "description": "在 QSerial 主栏新建一个工作 Tab，返回新 Tab 的 index。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "qserial_open_port",
        "description": "打开指定 Tab 的串口，可选覆盖串口参数；返回最终开关状态。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "index": _TAB_INDEX_SCHEMA,
                "port": {"type": "string", "description": "串口号，如 COM3；省略时使用界面当前选择"},
                "baudrate": {"type": "integer", "description": "波特率，默认使用界面当前值"},
                "parity": {"type": "string", "enum": ["None", "Even", "Odd", "Mark", "Space"]},
                "bytesize": {"type": "integer", "enum": [5, 6, 7, 8]},
                "stopbits": {"type": "number", "enum": [1, 1.5, 2]},
                "flow_control": {"type": "string", "enum": ["None", "Hardware", "Software"]},
            },
            "required": ["index"],
        },
    },
    {
        "name": "qserial_close_port",
        "description": "关闭指定 Tab 的串口，返回最终开关状态。",
        "inputSchema": {
            "type": "object",
            "properties": {"index": _TAB_INDEX_SCHEMA},
            "required": ["index"],
        },
    },
    {
        "name": "qserial_send",
        "description": "向指定 Tab 发送数据。text 与 hex 必须二选一；TEXT 的换行按该 Tab 发送配置转换，HEX 为十六进制字符串（如 01 A2）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "index": _TAB_INDEX_SCHEMA,
                "text": {"type": "string", "description": "TEXT 模式发送内容"},
                "hex": {"type": "string", "description": "HEX 模式发送内容"},
            },
            "required": ["index"],
        },
    },
    {
        "name": "qserial_read",
        "description": "读取指定 Tab 的接收数据。默认 tail 模式：积压刷屏时直接取最新 max_bytes 尾部窗口（起点对齐行首），跳过的积压字节计入 skipped 且仍在磁盘账本，可带 offset 回看；tail=false 改为自上次读取位置 FIFO 续读；带 offset 为绝对位置回看（不消费、不动游标、可反复读）。返回 text/hex 双格式。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "index": _TAB_INDEX_SCHEMA,
                "max_bytes": {"type": "integer", "description": "本次最多读取的字节数，默认 65536"},
                "tail": {"type": "boolean", "description": "默认 true：跳过积压只取最新尾部窗口（积压可带 offset 回看）；false：自上次读取位置 FIFO 续读。带 offset 时本参数被忽略"},
                "offset": {"type": "integer", "description": "磁盘账本绝对偏移，回看历史段（不消费、不动游标，可反复读），来自此前读返回的 offset/next_offset"},
            },
            "required": ["index"],
        },
    },
]


class McpBridge:
    """处理 stdin/stdout 的 JSON-RPC 消息并转发到 QSerial HTTP 服务。"""

    def __init__(self, base_url, token="", timeout=10.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # ---- HTTP 转发 ----

    def _request(self, method, path, body=None):
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
            method=method,
        )
        request.add_header("Accept", "application/json")
        if body is not None:
            request.add_header("Content-Type", "application/json; charset=utf-8")
        if self.token:
            request.add_header("X-Agent-Token", self.token)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not payload.get("ok"):
            raise RuntimeError(payload.get("error", "QSerial 返回未知错误"))
        return payload.get("data")

    # ---- 工具实现 ----

    @staticmethod
    def _require_index(arguments):
        index = arguments.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ValueError("index 必须是非负整数")
        return index

    @staticmethod
    def _open_arguments(arguments):
        body = {}
        for key in ("port", "baudrate", "parity", "bytesize", "stopbits", "flow_control"):
            if arguments.get(key) is not None:
                body[key] = arguments[key]
        return body

    def call_tool(self, name, arguments):
        """执行工具并返回 MCP 工具结果（content/isError）。"""
        arguments = arguments or {}
        try:
            if name == "qserial_status":
                data = self._request("GET", "/api/status")
            elif name == "qserial_list_ports":
                data = self._request("GET", "/api/ports")
            elif name == "qserial_list_tabs":
                data = self._request("GET", "/api/tabs")
            elif name == "qserial_create_tab":
                data = self._request("POST", "/api/tabs", {})
            elif name == "qserial_open_port":
                index = self._require_index(arguments)
                data = self._request("POST", f"/api/tabs/{index}/open", self._open_arguments(arguments))
            elif name == "qserial_close_port":
                index = self._require_index(arguments)
                data = self._request("POST", f"/api/tabs/{index}/close", {})
            elif name == "qserial_send":
                index = self._require_index(arguments)
                has_text, has_hex = "text" in arguments, "hex" in arguments
                if has_text == has_hex:
                    raise ValueError("text 与 hex 必须提供且只能提供其一")
                body = {"text": arguments["text"]} if has_text else {"hex": arguments["hex"]}
                data = self._request("POST", f"/api/tabs/{index}/send", body)
            elif name == "qserial_read":
                index = self._require_index(arguments)
                params = []
                if arguments.get("max_bytes") is not None:
                    params.append(f"max_bytes={int(arguments['max_bytes'])}")
                if arguments.get("offset") is not None:
                    params.append(f"offset={int(arguments['offset'])}")
                elif arguments.get("tail") is not False:   # MCP 侧默认尾部优先(AI 要的是应答不是积压)
                    params.append("tail=1")
                path = f"/api/tabs/{index}/receive"
                if params:
                    path += "?" + "&".join(params)
                data = self._request("GET", path)
            else:
                return self._tool_error(f"未知工具: {name}")
        except ValueError as error:
            return self._tool_error(str(error))
        except urllib.error.HTTPError as error:
            try:
                payload = json.loads(error.read().decode("utf-8"))
                message = payload.get("error", str(error))
            except (UnicodeDecodeError, json.JSONDecodeError):
                message = str(error)
            return self._tool_error(f"QSerial 返回 {error.code}: {message}")
        except (urllib.error.URLError, OSError) as error:
            return self._tool_error(f"无法连接 QSerial 服务（GUI 版请在视图菜单开启 AI Agent 服务；serial_debug.py 请加 --agent-http 参数启动）: {error}")
        except RuntimeError as error:
            return self._tool_error(str(error))
        return {
            "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
            "isError": False,
        }

    @staticmethod
    def _tool_error(message):
        return {"content": [{"type": "text", "text": message}], "isError": True}

    # ---- JSON-RPC 处理 ----

    def handle_message(self, message):
        """处理一条已解析的消息；返回响应对象，通知类消息返回 None。"""
        method = message.get("method")
        message_id = message.get("id")
        if method is None:
            return None
        if method == "initialize":
            requested = message.get("params", {}).get("protocolVersion")
            result = {
                "protocolVersion": requested if isinstance(requested, str) and requested else PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "qserial-mcp", "version": BRIDGE_VERSION},
            }
            return self._success(message_id, result)
        if method == "ping":
            return self._success(message_id, {})
        if method == "tools/list":
            return self._success(message_id, {"tools": TOOL_DEFINITIONS})
        if method == "tools/call":
            params = message.get("params", {})
            result = self.call_tool(params.get("name"), params.get("arguments"))
            return self._success(message_id, result)
        if method.startswith("notifications/"):
            return None
        return self._error(message_id, -32601, f"未知方法: {method}")

    @staticmethod
    def _success(message_id, result):
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    @staticmethod
    def _error(message_id, code, message):
        return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}

    def handle_line(self, line):
        """解析并处理一行输入；返回响应文本或 None。"""
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            print(f"忽略无法解析的输入: {error}", file=sys.stderr)
            return None
        if not isinstance(message, dict):
            print("忽略非对象 JSON-RPC 消息", file=sys.stderr)
            return None
        response = self.handle_message(message)
        if response is None:
            return None
        return json.dumps(response, ensure_ascii=False)

    def serve(self, input_stream, output_stream):
        """逐行读取请求并写出响应，直到输入结束。"""
        for line in input_stream:
            line = line.strip()
            if not line:
                continue
            response = self.handle_line(line)
            if response is not None:
                output_stream.write(response + "\n")
                output_stream.flush()


def _detect_base_url(args):
    if args.url:
        return args.url
    return f"http://127.0.0.1:{args.port}"


def main():
    parser = argparse.ArgumentParser(description="QSerial MCP stdio 桥接")
    parser.add_argument("--url", default="", help="QSerial HTTP 服务地址，默认 http://127.0.0.1:<port>")
    parser.add_argument("--port", type=int, default=8321, help="QSerial HTTP 服务端口，默认 8321")
    parser.add_argument("--token", default="", help="访问令牌，与 QSerial agent_service.token 一致")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP 请求超时秒数")
    args = parser.parse_args()
    # MCP stdio 协议固定 UTF-8；Windows 管道默认跟随区域设置（GBK），必须显式指定。
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8")
    McpBridge(_detect_base_url(args), token=args.token, timeout=args.timeout).serve(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
