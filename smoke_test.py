# -*- coding: utf-8 -*-
"""命令/输入编辑冒烟测试: 覆盖参数解析、错误输入、历史、行编辑, 确保不抛异常。"""
import glob
import os
import sys
import time

import serial_debug as sd

fail = []


def check(name, fn):
    try:
        fn()
        print(f"  ok  {name}")
    except Exception as e:
        print(f"FAIL  {name}: {type(e).__name__}: {e}")
        fail.append(name)


t = sd.SerialTool(port="loop://", baud=115200, color=False)
t.open_port()

# 非法/边界命令一律走 note() 提示, 不得抛异常
for cmd in ("/info", "/baud abc", "/baud 1500000", "/baud 115200", "/nl xxx", "/nl crlf",
            "/enc gbk", "/enc bad_enc!", "/cfg bits=8 parity=E stop=2 flow=rtscts",
            "/cfg foo=bar", "/cfg", "/hex", "/hex", "/hexsend", "/hexsend", "/ts", "/pause", "/resume",
            "/log on t1.log", "/log on t2.log", "/log off", "/log off", "/log", "/dtr 1",
            "/rts 0", "/dtr x", "/port", "/send 不存在_的文件.bin", "/help",
            "/unknown_cmd", "/", "/q2", "/clear", "/save"):  # 此时无数据, /save 应只提示
    check(f"exec {cmd}", lambda c=cmd: t.exec_cmd(c))

check("baud applied", lambda: (_ for _ in ()).throw(AssertionError) if t.baud != 115200 else None)
check("nl applied", lambda: (_ for _ in ()).throw(AssertionError) if t.nl != "crlf" else None)
check("cfg applied", lambda: (_ for _ in ()).throw(AssertionError)
      if not (t.parity == "E" and t.stopbits == 2.0 and t.rtscts) else None)
t.exec_cmd("/cfg flow=none")  # loop:// 无法响应硬件流控, 关掉以免后续发送阻塞
check("reopen after cfg", lambda: t.exec_cmd("/open"))
check("port alive", lambda: (_ for _ in ()).throw(AssertionError) if t.ser is None else None)

# 行编辑 + 发送 + 历史(纯字符路径, 不涉及 msvcrt 扩展键)
def type_text(s):
    for ch in s:
        t.handle_key(ch)


check("type", lambda: type_text("hello"))
check("backspace", lambda: type_text("\x08\x08"))
check("buf now 'hel'", lambda: (_ for _ in ()).throw(AssertionError) if t.inp != "hel" else None)
check("enter sends", lambda: t.handle_key("\r"))


def _assert_tx():
    # "hel" 3 字节 + crlf 2 字节 = 5
    if t.tx_total != 5:
        raise AssertionError(f"tx_total={t.tx_total}, 应为 5")


check("tx bytes hel+crlf=5", _assert_tx)
check("history up", lambda: t._hist_prev())
check("hist recalled", lambda: (_ for _ in ()).throw(AssertionError) if t.inp != "hel" else None)
check("hist next clears", lambda: t._hist_next())
check("hist empty", lambda: (_ for _ in ()).throw(AssertionError) if t.inp != "" else None)

# 方向键映射回归: ↓ 曾误绑 _hist_prev, 行模式下只能上翻不能回退
check("↑ key recalls hel", lambda: t.handle_key("\x00H"))
check("↑ key got hel", lambda: (_ for _ in ()).throw(AssertionError) if t.inp != "hel" else None)
check("↓ key returns empty", lambda: t.handle_key("\x00P"))
check("↓ key cleared", lambda: (_ for _ in ()).throw(AssertionError) if t.inp != "" else None)

# HEX 发送
check("hexsend on", lambda: t.exec_cmd("/hexsend"))
check("type hex", lambda: type_text("01 A0 FF"))
check("send hex", lambda: t.handle_key("\r"))
check("hex bad", lambda: (type_text("0G"), t.handle_key("\r")))  # 应提示而不崩
check("hexsend off", lambda: t.exec_cmd("/hexsend"))

# 文本/GBK 混合发送
t.exec_cmd("/enc gbk")
check("send cjk gbk", lambda: t.send_line("中文AB"))
t.exec_cmd("/enc utf-8")

# /save: 导出本次会话全部接收数据(loop:// 会把发送内容环回)
time.sleep(0.4)  # 等接收线程收完环回数据
check("capture has data", lambda: (_ for _ in ()).throw(AssertionError) if t.capture_bytes == 0 else None)
check("capture in logs dir", lambda: (_ for _ in ()).throw(AssertionError)
      if not os.path.dirname(t.capture_path or "").endswith("logs") else None)
check("/save with spaces in name", lambda: t.exec_cmd("/save smoke saved log.log"))
saved = t._resolve_log_path("smoke saved log.log")
check("save size == capture", lambda: (_ for _ in ()).throw(AssertionError)
      if os.path.getsize(saved) != t.capture_bytes else None)
check("save in logs dir", lambda: (_ for _ in ()).throw(AssertionError)
      if not os.path.dirname(saved).endswith("logs") else None)

# Tab 本地命令补全
def _complete_case(inp, expect):
    def f():
        t.inp = inp
        t.inp_pos = len(inp)
        t.handle_key("\t")
        if t.inp != expect:
            raise AssertionError(f"{inp!r} -> {t.inp!r}, 期望 {expect!r}")
    return f


check("complete /ba -> /baud", _complete_case("/ba", "/baud "))
check("complete /l -> /log", _complete_case("/l", "/log "))
check("complete /hexs -> /hexsend", _complete_case("/hexs", "/hexsend "))
check("complete /baud 15 -> 1500000", _complete_case("/baud 15", "/baud 1500000"))
check("complete /nl l -> lf", _complete_case("/nl l", "/nl lf"))
check("complete /nl cr ambiguous stays", _complete_case("/nl cr", "/nl cr"))  # cr/crlf 两可, 仅提示
check("complete multi /h stays", _complete_case("/h", "/h"))  # 多候选只提示不改写


def _raw_smoke():
    t.exec_cmd("/raw on")
    if not t.raw:
        raise AssertionError("raw 未开启")
    tx0 = t.tx_total
    for ch in "if":        # 普通字符透传
        t.handle_key(ch)
    t.handle_key("\t")     # Tab 透传
    t.handle_key("\x08")   # 退格 -> 0x7f
    t.handle_key("\x03")   # Ctrl+C -> 0x03 + 本地 ^C 反馈
    if t.tx_total != tx0 + 5:
        raise AssertionError(f"raw 透传 {t.tx_total - tx0} 字节, 应为 5")
    if not t._raw_cc_hinted:
        raise AssertionError("Ctrl+C 未被本地检测/提示")
    if not t.raw:
        raise AssertionError("Ctrl+C 意外退出了交互模式")
    t.handle_key("\x11")   # Ctrl+Q 回到行模式
    if t.raw:
        raise AssertionError("Ctrl+Q 未退出 raw")


check("raw mode passthrough", _raw_smoke)
check("line mode after raw", lambda: (_ for _ in ()).throw(AssertionError) if t.raw else None)


# 行模式 Ctrl+C: 转发 ^C 给设备 + 2 秒内连按两次退出 (放在最后, 会置 running=False)
def _line_cc():
    tx0 = t.tx_total
    t.handle_key("\x03")
    if t.tx_total != tx0 + 1:
        raise AssertionError("行模式 Ctrl+C 未转发 ^C 给设备")
    if t.running:
        pass  # 第一按只转发, 不退出
    time.sleep(0.35)  # 越过 0.3s 去抖窗口, 仍在 2s 退出窗口内
    t.handle_key("\x03")
    if t.running:
        raise AssertionError("行模式连按 Ctrl+C 未退出程序")


check("line-mode Ctrl+C forwards & double-press exits", _line_cc)

# POSIX 键盘转义序列解析(纯字节逻辑, 在 Windows 上也可自测; Linux 终端按键靠它)
def _esc_case(buf, want_key, want_used):
    def f():
        key, used = sd._match_esc_seq(buf)
        if key != want_key or used != want_used:
            raise AssertionError(f"{buf!r} -> ({key!r}, {used}), 期望 ({want_key!r}, {want_used})")
    return f


NEED = sd._NEED_MORE
check("esc ↑", _esc_case(b"\x1b[A", "\x00H", 3))
check("esc ↓", _esc_case(b"\x1b[B", "\x00P", 3))
check("esc →", _esc_case(b"\x1b[C", "\x00M", 3))
check("esc ←", _esc_case(b"\x1b[D", "\x00K", 3))
check("esc Del", _esc_case(b"\x1b[3~", "\x00S", 4))
check("esc Ins", _esc_case(b"\x1b[2~", "\x00R", 4))
check("esc Home [H", _esc_case(b"\x1b[H", "\x00G", 3))
check("esc Home [1~", _esc_case(b"\x1b[1~", "\x00G", 4))
check("esc End SS3", _esc_case(b"\x1bOF", "\x00O", 3))
check("esc PgUp", _esc_case(b"\x1b[5~", "\x00I", 4))
check("esc PgDn", _esc_case(b"\x1b[6~", "\x00Q", 4))
check("esc lone ESC waits", _esc_case(b"\x1b", NEED, 0))
check("esc half [ waits", _esc_case(b"\x1b[", NEED, 0))
check("esc half [1; waits", _esc_case(b"\x1b[1;", NEED, 0))
check("esc half SS3 waits", _esc_case(b"\x1bO", NEED, 0))
check("esc lone ESC + char", _esc_case(b"\x1ba", "\x1b", 1))
check("esc unknown Shift-Tab swallowed", _esc_case(b"\x1b[Z", None, 3))
check("esc unknown Shift-arr swallowed", _esc_case(b"\x1b[1;2A", None, 6))
check("esc unknown Ctrl-Del swallowed", _esc_case(b"\x1b[3;5~", None, 6))
check("esc mouse SGR swallowed", _esc_case(b"\x1b[<0;33;12M", None, 11))
check("esc unknown SS3 swallowed", _esc_case(b"\x1b[X", None, 3))

t.close_all()
for f in ("t1.log", "t2.log", "smoke saved log.log"):
    p = t._resolve_log_path(f)
    if os.path.exists(p):
        os.remove(p)


def rm_quiet(pattern):  # Windows 下新文件可能被杀毒/索引短暂锁定, 重试清理
    for _ in range(5):
        try:
            for f in glob.glob(pattern):
                os.remove(f)
            return
        except PermissionError:
            time.sleep(0.3)


rm_quiet(os.path.join("logs", "serial_capture_*.part"))  # close_all 会把 .part 转正
rm_quiet(os.path.join("logs", "serial_capture_*.log"))
rm_quiet("serial_capture_*.part")  # 兼容旧位置
rm_quiet("serial_capture_*.log")
import shutil
shutil.rmtree("logs", ignore_errors=True)  # 测试环境收尾: 清掉统一日志文件夹

print("SMOKE TEST OK" if not fail else f"SMOKE TEST FAILED: {fail}")
sys.exit(1 if fail else 0)
