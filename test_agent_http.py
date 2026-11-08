#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI Agent 通道测试: loop:// 虚拟串口上验证"真实 + 全面"承诺与两种观察模式。

覆盖:
    T1 双端对账: 人工灌入 -> AI 经 offset 账本读回, 字节级一致, rx_total==capture_bytes;
    T2 真实性:   随机位置重读与首读字节一致(读过不过期, 可复查);
    T3 双观察:   人工键入(human)与 AI 发送(agent)在事件流里来源可辨、载荷可对;
    T4 AI 单独看: 经 HTTP 开/关串口、发指令, 无人工参与走通整个闭环;
    T5 令牌:     无令牌 401, 带令牌 200;
    T6 桥接:     QSerial 的 mcp_bridge.py 原样对本工具完成 status/read/send/list_tabs;
    T7 尾部优先: 洪水积压下默认 tail 读直接拿到尾部窗口(应答不被展示截断埋掉),
                 skipped 账目相符, cursor 一次跳到末尾, 带 offset 可回看被跳过的积压。
"""
import json
import sys
import threading
import time
import urllib.request

import serial_debug as sd
import agent_http


class NullIO:  # 静音屏显输出
    def write(self, s):
        pass

    def flush(self):
        pass


real = sys.stdout
fails = []


def check(name, cond, detail=""):
    if cond:
        real.write(f"  ok  {name}\n")
    else:
        real.write(f"FAIL  {name}  {detail}\n")
        fails.append(name)


def http(method, base, path, body=None, token=None, timeout=5.0):
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Agent-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


# ---------------------------------------------------------------- 搭台
t = sd.SerialTool(port="loop://", baud=115200, color=False)
t.open_port()
assert t.ser is not None, "loop:// 打开失败"
sys.stdout = NullIO()
agent_http.attach(t, port=0, token="")   # 端口 0 = 内核分配临时端口, 测试专用
BASE = f"http://127.0.0.1:{t.agent.bound_port}"

stop_drain = threading.Event()


def drain_loop():  # 测试环境没有主循环, 用线程代打 drain_cmds(等价 run_console 每帧调用)
    while not stop_drain.is_set():
        t.agent.drain_cmds()
        time.sleep(0.01)


threading.Thread(target=drain_loop, daemon=True).start()

# ---------------------------------------------------------------- T1 双端对账 + T2 真实性
PATTERN = b"THE QUICK BROWN FOX JUMPS OVER THE LAZY DOG 0123456789abcdef\n" * 4096  # 249,856 字节
written = [0]


def writer():
    for i in range(0, len(PATTERN), 4096):
        try:
            t.ser.write(PATTERN[i:i + 4096])
            written[0] += min(4096, len(PATTERN) - i)
        except Exception:
            break


th = threading.Thread(target=writer)
th.start()

collected = bytearray()
reread_probe = None          # 中途在固定位置做一次随机复读, 存首读结果
deadline = time.time() + 60
while time.time() < deadline:
    st, resp = http("GET", BASE, "/api/tabs/0/receive?max_bytes=65536")
    assert st == 200, resp
    d = resp["data"]
    if reread_probe is None and d["next_offset"] > 2048:
        st2, r1 = http("GET", BASE, "/api/tabs/0/receive?offset=1024&max_bytes=1024")
        reread_probe = (r1["data"], d["offset"])   # 首读样本 + 当时光标
    if d["bytes"]:
        off = d["offset"]
        assert off == len(collected), f"cursor 语义错位: offset={off} 已收={len(collected)}"
        collected += bytes.fromhex(d["hex"].replace(" ", ""))
    if d["next_offset"] >= written[0] and not th.is_alive():
        th.join()
        st, tail = http("GET", BASE, "/api/tabs/0/receive?max_bytes=65536")
        if tail["data"]["bytes"]:
            collected += bytes.fromhex(tail["data"]["hex"].replace(" ", ""))
        if tail["data"]["next_offset"] >= written[0]:
            break
    time.sleep(0.01)

check("T1 全量读回", bytes(collected) == PATTERN,
      f"AI 读回 {len(collected)} / 灌入 {len(PATTERN)}")
check("T1 文本视图与 HEX 视图一致", d["decode_errors"] == 0)

st, resp = http("GET", BASE, "/api/info")
info = resp["data"]
check("T1 对账恒等 rx==capture", info["rx_total"] == info["capture_bytes"] == written[0],
      f"rx={info['rx_total']} cap={info['capture_bytes']} written={written[0]}")
check("T1 capture_consistent 标志", info["capture_consistent"] is True)

if reread_probe:
    probe, _ = reread_probe
    expect = PATTERN[1024:2048]
    got = bytes.fromhex(probe["hex"].replace(" ", ""))
    check("T2 随机重读与原数据一致(可复查)", got == expect and probe["offset"] == 1024)
else:
    check("T2 随机重读未触发", False)

# ---------------------------------------------------------------- T3 双观察: 来源可辨
n_ev = info["next_event_seq"]
t.serial_write(b"human-typed\n")   # 主线程调用 = 人工键入(同真实主循环路径)
time.sleep(0.2)
st, resp = http("GET", BASE, "/api/events?after_seq=0&max=5000")
evs = resp["data"]["events"]
tx_human = [e for e in evs if e.get("kind") == "tx" and e.get("source") == "human"]
import base64 as b64mod
check("T3 人工键入入流且 source=human",
      any(b64mod.b64decode(e["b64"]) == b"human-typed\n" for e in tx_human),
      f"human tx 事件数={len(tx_human)}")

st, resp = http("POST", BASE, "/api/tabs/0/send", {"text": "agent-says"})
check("T3 AI 发送成功", st == 200 and resp["data"]["sent"] and resp["data"]["bytes"] == len(b"agent-says\n"),
      json.dumps(resp, ensure_ascii=False))
time.sleep(0.2)
st, resp = http("GET", BASE, "/api/events?after_seq=0&max=5000")
evs = resp["data"]["events"]
tx_agent = [e for e in evs if e.get("kind") == "tx" and e.get("source") == "agent"]
import base64 as b64mod
check("T3 AI 发送入流且 source=agent + 载荷含行尾",
      any(b64mod.b64decode(e["b64"]) == b"agent-says\n" for e in tx_agent),
      f"agent tx 事件数={len(tx_agent)}")

# ---------------------------------------------------------------- T4 AI 单独看: 开关串口闭环
st, resp = http("POST", BASE, "/api/tabs/0/close", {})
check("T4 HTTP 关串口", st == 200 and resp["data"]["open"] is False, json.dumps(resp))
st, resp = http("POST", BASE, "/api/tabs/0/close", {})
check("T4 重复关闭返回 409", st == 409)
st, resp = http("POST", BASE, "/api/tabs/0/open", {"baudrate": 921600})
check("T4 HTTP 开串口(带波特率覆盖)", st == 200 and resp["data"]["open"] is True and t.baud == 921600,
      json.dumps(resp))
st, resp = http("POST", BASE, "/api/tabs/0/send", {"hex": "01 A2 FF"})
check("T4 HEX 发送", st == 200 and resp["data"]["bytes"] == 3, json.dumps(resp))

# ---------------------------------------------------------------- T5 令牌
t2 = sd.SerialTool(port=None, color=False)
agent_http.attach(t2, port=0, token="sekrit")
BASE2 = f"http://127.0.0.1:{t2.agent.bound_port}"
st, resp = http("GET", BASE2, "/api/status")
check("T5 无令牌 401", st == 401)
st, resp = http("GET", BASE2, "/api/status", token="wrong")
check("T5 错令牌 401", st == 401)
st, resp = http("GET", BASE2, "/api/status", token="sekrit")
check("T5 对令牌 200", st == 200 and resp["data"]["token_required"] is True)

# ---------------------------------------------------------------- T6 QSerial 桥原样复用
from mcp_bridge import McpBridge
br = McpBridge(BASE)
r = br.call_tool("qserial_status", {})
check("T6 桥 status", not r["isError"] and json.loads(r["content"][0]["text"])["name"] == "serial_debug")
r = br.call_tool("qserial_list_tabs", {})
tabs = json.loads(r["content"][0]["text"])["tabs"]
check("T6 桥 list_tabs 单 Tab", not r["isError"] and len(tabs) == 1 and tabs[0]["index"] == 0)
tx_before = t.tx_total
r = br.call_tool("qserial_send", {"index": 0, "text": "via-mcp-bridge"})
sent = json.loads(r["content"][0]["text"])
check("T6 桥 send", not r["isError"] and sent["sent"] is True, r["content"][0]["text"])
r = br.call_tool("qserial_read", {"index": 0, "max_bytes": 65536})
rd = json.loads(r["content"][0]["text"])
check("T6 桥 read 返回 text/next_offset", not r["isError"] and "next_offset" in rd, r["content"][0]["text"])
r = br.call_tool("qserial_create_tab", {})
check("T6 create_tab 明确拒绝(单会话)", r["isError"] and "Tab 0" in r["content"][0]["text"])

# ---------------------------------------------------------------- T7 尾部优先(tail)读
LINE = b"SPAMLINE 0123456789abcdef 0123456789abcdef 0123456\n"   # 48 字节
flood = LINE * 4000 + b"TAILMARK-7f3a READY\n"                     # ~192KB 洪水 + 尾部应答标记
st, resp = http("GET", BASE, "/api/tabs/0/receive?max_bytes=4194304")   # 先排空(HTTP 默认 FIFO), 固定 cursor 起点
pre_end = resp["data"]["next_offset"]
def writer2():   # loop:// 缓冲有限, 必须像 T1 一样分块写, 否则写超时
    for i in range(0, len(flood), 4096):
        try:
            t.ser.write(flood[i:i + 4096])
        except Exception:
            break


wr2 = threading.Thread(target=writer2)
wr2.start()
deadline = time.time() + 30
while time.time() < deadline:
    st, resp = http("GET", BASE, "/api/info")
    if resp["data"]["capture_bytes"] >= pre_end + len(flood):
        break
    time.sleep(0.02)
check("T7 洪水全部落盘", resp["data"]["capture_bytes"] == pre_end + len(flood),
      f"capture={resp['data']['capture_bytes']} 期望={pre_end + len(flood)}")
r = br.call_tool("qserial_read", {"index": 0, "max_bytes": 1024})   # MCP 默认 tail=1
d7 = json.loads(r["content"][0]["text"])
check("T7 默认读拿到尾部窗口含应答标记",
      not r["isError"] and 900 < d7["bytes"] <= 1024 and "TAILMARK-7f3a" in d7["text"],
      r["content"][0]["text"][:200])
check("T7 窗口起点对齐行首(无半行开头)", d7["text"].startswith("SPAMLINE") or d7["text"].startswith("TAILMARK"))
check("T7 skipped 账目相符", d7["skipped"] == len(flood) - d7["bytes"],
      f"skipped={d7['skipped']} flood={len(flood)} bytes={d7['bytes']}")
check("T7 cursor 一次跳到窗口尾", d7["cursor"] == d7["next_offset"] == d7["capture_bytes"],
      f"cursor={d7['cursor']} next={d7['next_offset']} cap={d7['capture_bytes']}")
st, resp = http("GET", BASE, "/api/tabs")
check("T7 消费后 pending 归零", resp["data"]["tabs"][0]["pending_bytes"] == 0)
st, resp = http("GET", BASE, f"/api/tabs/0/receive?offset={d7['offset']}&max_bytes={d7['bytes']}")
d7b = resp["data"]
check("T7 带 offset 回看窗口内容一致(缓存按需)", d7b["hex"] == d7["hex"] and d7b["cursor"] == d7["cursor"])
r = br.call_tool("qserial_read", {"index": 0, "tail": False, "max_bytes": 4096})
d7c = json.loads(r["content"][0]["text"])
check("T7 tail=false 退回 FIFO 且已无积压", not r["isError"] and d7c["bytes"] == 0 and d7c["skipped"] == 0)
r = br.call_tool("qserial_read", {"index": 0, "offset": d7["offset"], "max_bytes": 32})
d7d = json.loads(r["content"][0]["text"])
check("T7 桥 offset 透传回看(不动游标)",
      not r["isError"] and d7d["offset"] == d7["offset"] and d7d["bytes"] == 32 and d7d["cursor"] == d7["cursor"])

# ---------------------------------------------------------------- 收尾
stop_drain.set()
time.sleep(0.1)
sys.stdout = real
t.close_all()
t2.close_all()
import shutil
shutil.rmtree("logs", ignore_errors=True)

print(f"\nAI AGENT TEST {'OK' if not fails else 'FAILED'}  ({len(fails)} 项失败)")
sys.exit(0 if not fails else 1)
