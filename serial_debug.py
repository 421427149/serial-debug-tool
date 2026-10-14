#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
serial_debug.py —— 抗洪水串口调试终端（Windows / Linux 通用）

用法:
    python serial_debug.py                      # 交互式选择串口和波特率
    python serial_debug.py -p COM5              # Windows 指定串口, 默认 115200
    python serial_debug.py -p /dev/ttyUSB0      # Linux 指定串口
    python serial_debug.py -p COM5 -b 1500000   # 指定串口和 1.5M 波特率
    python serial_debug.py --list               # 仅列出可用串口

设计要点（高吞吐不卡死）:
    * 独立接收线程只负责"读串口 -> 入缓冲/写日志", 几乎不做别的事;
    * 主线程 20 次/秒批量刷新屏幕, 每次最多打印 budget 字节, 保证打字永不丢键;
    * 屏显落后超过阈值时按整行省略并计数提示, 原始数据完整写入日志不丢失;
    * 串口拔出/被占用等异常只提示并允许 /open 重连, 程序不崩溃;
    * 键盘抽象: Windows msvcrt / Linux termios raw, 两个平台同一套逻辑键与快捷键。

常用命令: 进程序后输入 /help 查看。
"""

import argparse
import codecs
import os
import re
import shutil
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("缺少 pyserial, 请先安装:  pip install pyserial")
    sys.exit(1)

IS_WIN = os.name == "nt"
if IS_WIN:
    import msvcrt
else:
    import select
    import termios
    msvcrt = None

# ---------------------------------------------------------------- 常量

NEWLINES = {"none": b"", "lf": b"\n", "cr": b"\r", "crlf": b"\r\n"}

TICK = 0.05                 # 屏幕刷新周期 50ms (20 FPS)
DEFAULT_BUDGET = 64 * 1024  # 每个周期最多打印的字节数 (~1.2 MB/s 上限)
DROP_HI = 512 * 1024        # 屏显积压超过该值 -> 按整行省略
PEND_CAP = 8 * 1024 * 1024  # 内存中待显示缓冲上限(溢出丢弃但计数, 日志不受影响)

PARITY_MAP = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN,
              "O": serial.PARITY_ODD, "M": serial.PARITY_MARK,
              "S": serial.PARITY_SPACE}

# 本地命令补全候选(行模式下按 Tab)
COMMAND_LIST = ("/baud", "/cfg", "/clear", "/close", "/dtr", "/enc", "/exit", "/h", "/help",
                "/hex", "/hexsend", "/info", "/log", "/nl", "/open", "/pause", "/port",
                "/q", "/quit", "/raw", "/reopen", "/resume", "/rts", "/save", "/send", "/ts")

# 第二个参数的常用取值(如 /baud <Tab> 提示可选波特率)
VALUE_HINTS = {
    "baud": ["115200", "921600", "1500000", "3000000"],
    "nl": ["none", "lf", "cr", "crlf"],
    "enc": ["utf-8", "gbk", "ascii"],
    "log": ["on", "off"],
    "raw": ["on", "off"],
    "dtr": ["0", "1"],
    "rts": ["0", "1"],
}

# 交互模式下扩展键 -> ANSI 转义序列(设备端 shell 识别, 用于↑↓历史/移动光标)
RAW_KEYMAP = {"H": b"\x1b[A", "P": b"\x1b[B", "M": b"\x1b[C", "K": b"\x1b[D",   # ↑↓→←
              "G": b"\x1b[H", "O": b"\x1b[F",                                   # Home End
              "S": b"\x1b[3~", "R": b"\x1b[2~",                                 # Del Ins
              "I": b"\x1b[5~", "Q": b"\x1b[6~"}                                 # PgUp PgDn


def enable_vt():
    """让老版 conhost 也支持 ANSI 转义(Win10+ / Windows Terminal 天然支持)。"""
    if not IS_WIN:
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            k32.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def disable_ctrlc_signal():
    """清掉控制台 ENABLE_PROCESSED_INPUT(0x1), 让 Ctrl+C 作为普通键 \x03 进入输入缓冲。

    默认模式下控制台会拦截 ^C 转成 SIGINT, getwch 永远收不到, ^C 也就发不到设备。
    stdin 可能不是控制台(Git Bash/mintty、管道、重定向), 此时用 CONIN$ 拿真实控制台。
    返回 (句柄, 原模式), 供退出时恢复; 失败返回 (None, 0), 不影响其他功能。
    """
    if not IS_WIN:
        return None, 0
    try:
        import ctypes
        k32 = ctypes.windll.kernel32

        def _try(h):
            if h in (None, 0, -1):
                return None
            m = ctypes.c_uint32()
            if not k32.GetConsoleMode(h, ctypes.byref(m)):
                return None
            if not k32.SetConsoleMode(h, m.value & ~0x0001):
                return None
            return (h, m.value)

        r = _try(k32.GetStdHandle(-10))  # STD_INPUT_HANDLE
        if r is None:                    # stdin 是管道/重定向 -> 直接开控制台输入设备
            r = _try(k32.CreateFileW("CONIN$", 0x80000000 | 0x40000000, 3, None, 3, 0, None))
        return r if r else (None, 0)
    except Exception:
        return None, 0


def restore_console_mode(con):
    h, old = con
    if not h:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleMode(h, old)
    except Exception:
        pass


# ---------------------------------------------------------------- 键盘抽象
# 统一逻辑键约定: 普通键 = 单字符; 扩展键(方向键等) = "\x00"+扩展码(如 "\x00H"=↑)。
# Windows msvcrt 原生就是 (\x00/\xe0 + 码) 两段式, 这里把 Linux 终端转义序列也归一成
# 同一种逻辑键, 上层 handle_key/_raw_key 完全不分平台。

_NEED_MORE = object()   # 转义序列/多字节字符尚未收全, 需要更多字节

# Linux 终端转义序列 -> 扩展码(与 RAW_KEYMAP 及 handle_key 使用的字母一致)
_ESC_SEQS = {
    b"\x1b[A": "H", b"\x1b[B": "P", b"\x1b[C": "M", b"\x1b[D": "K",   # ↑↓→←
    b"\x1b[H": "G", b"\x1b[F": "O", b"\x1b[1~": "G", b"\x1b[7~": "G",  # Home
    b"\x1b[4~": "O", b"\x1b[8~": "O",                                  # End
    b"\x1b[2~": "R", b"\x1b[3~": "S",                                  # Ins Del
    b"\x1b[5~": "I", b"\x1b[6~": "Q",                                  # PgUp PgDn
    b"\x1bOA": "H", b"\x1bOB": "P", b"\x1bOC": "M", b"\x1bOD": "K",   # SS3 应用模式
    b"\x1bOH": "G", b"\x1bOF": "O",
}
_ESC_SEQS_SORTED = sorted(_ESC_SEQS.items(), key=lambda kv: -len(kv[0]))


def _match_esc_seq(buf):
    """解析 buf 开头的终端转义序列(纯字节逻辑, 与平台无关, 便于自测)。

    buf 以 \x1b 开头。返回 (key, consumed):
      key == _NEED_MORE: 序列未收全, 等待更多字节(consumed=0);
      key 为 "\x00"+码: 已知扩展键; key 为 "\x1b": 孤立的 Esc 键;
      key 为 None: 不认识的序列(如 Shift+方向、鼠标上报), 整体吞掉不产生按键。
      consumed 为消耗的字节数。
    """
    if len(buf) < 2:
        return _NEED_MORE, 0
    if buf[1:2] == b"[" or buf[1:2] == b"O":
        for seq, code in _ESC_SEQS_SORTED:
            if buf.startswith(seq):
                return "\x00" + code, len(seq)
        if any(s.startswith(buf) for s in _ESC_SEQS):
            return _NEED_MORE, 0          # 已知序列的半截(如只有 \x1b[ )
        # 未知序列(如 \x1b[1;2A、\x1b[<0;33;12M 鼠标): 吞到 CSI 终止符(0x40-0x7E)为止
        i = 2
        while i < len(buf) and not (0x40 <= buf[i] <= 0x7E):
            i += 1
        if i >= len(buf):
            if buf[1:2] == b"O":
                if len(buf) >= 3:
                    return None, 3        # SS3 未知组合, 定长 3 字节
                return _NEED_MORE, 0
            return _NEED_MORE, 0
        return None, i + 1
    return "\x1b", 1                      # Esc + 其他字符: 孤立 Esc 先交付


if IS_WIN:
    class _Keyboard:
        """Windows 控制台键盘: msvcrt 事件级读取。
        Ctrl+C 以 \x03 到达依赖 disable_ctrlc_signal 清 ENABLE_PROCESSED_INPUT。"""

        def __init__(self):
            self.ok = False
            self._con = (None, 0)

        def open(self):
            self._con = disable_ctrlc_signal()
            self.ok = self._con[0] is not None
            return self.ok

        def close(self):
            restore_console_mode(self._con)
            self.ok = False

        @staticmethod
        def poll_key():
            """非阻塞取一个逻辑键; 无按键返回 None。扩展键两段式合成一次交付。"""
            if not msvcrt.kbhit():
                return None
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                ch += msvcrt.getwch()
            return ch
else:
    class _Keyboard:
        """Linux/POSIX 终端键盘: termios raw + select 非阻塞读。
        关 ISIG 让 Ctrl+C 以 \x03 到达(同 Windows 清 ENABLE_PROCESSED_INPUT);
        不动 OPOST, 终端仍负责输出的换行翻译。stdin 被管道/重定向时退回 /dev/tty
        (同 Windows 的 CONIN$ 思路)。"""

        def __init__(self):
            self.ok = False
            self.fd = -1
            self._old = None
            self._buf = b""
            self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")

        def open(self):
            try:
                if sys.stdin.isatty():
                    self.fd = sys.stdin.fileno()
                else:
                    self.fd = os.open("/dev/tty", os.O_RDWR | os.O_NONBLOCK)
                self._old = termios.tcgetattr(self.fd)
                new = termios.tcgetattr(self.fd)
                new[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INLCR |
                            termios.IGNCR | termios.ISTRIP | termios.PARMRK |
                            termios.IXON)
                new[3] &= ~(termios.ICANON | termios.ECHO | termios.ECHONL |
                            termios.ISIG | termios.IEXTEN)
                new[6][termios.VMIN] = 0
                new[6][termios.VTIME] = 0
                termios.tcsetattr(self.fd, termios.TCSANOW, new)
                self.ok = True
            except Exception:
                self.ok = False
            return self.ok

        def close(self):
            if self._old is not None and self.fd >= 0:
                try:
                    termios.tcsetattr(self.fd, termios.TCSANOW, self._old)
                except Exception:
                    pass
            if self.fd >= 0:
                try:
                    os.close(self.fd)
                except Exception:
                    pass
                self.fd = -1
            self.ok = False

        def poll_key(self):
            """非阻塞取一个逻辑键; 无按键返回 None。"""
            while True:
                k = self._parse_one()
                if k is not _NEED_MORE:
                    return k
                # 需要更多字节: 半截转义序列短等一下(6ms), 其余不等待
                wait = 0.006 if self._buf.startswith(b"\x1b") else 0.0
                try:
                    r, _, _ = select.select([self.fd], [], [], wait)
                except Exception:
                    return None
                if not r:
                    if self._buf.startswith(b"\x1b"):
                        # 等不到序列后续 -> 就是用户按下的孤立 Esc 键
                        self._buf = self._buf[1:]
                        return "\x1b"
                    return None    # UTF-8 多字节字符没收全, 等下一轮
                try:
                    data = os.read(self.fd, 512)
                except OSError:
                    return None
                if not data:
                    self.ok = False  # 终端断开(EOF): 键盘不可用
                    return None
                self._buf += data

        def _parse_one(self):
            buf = self._buf
            if not buf:
                return _NEED_MORE
            if buf[0] == 0x1B:
                k, used = _match_esc_seq(buf)
                if k is not _NEED_MORE:
                    self._buf = buf[used:]
                return k
            ch = self._dec.decode(buf[:1])
            self._buf = buf[1:]
            if not ch:
                return _NEED_MORE  # 多字节字符(如中文)没收全
            return "\x08" if ch == "\x7f" else ch  # Linux 退格是 DEL, 对齐 Windows


def fmt_bytes(n):
    return f"{n:,}"


def _lcp(words):
    """一组字符串的最长公共前缀。"""
    p = words[0] if words else ""
    for w in words:
        while not w.startswith(p):
            p = p[:-1]
    return p


# ---------------------------------------------------------------- 主工具类

class SerialTool:
    def __init__(self, port=None, baud=115200, bits=8, parity="N", stop=1,
                 flow="none", nl="lf", enc="utf-8", hex_mode=False,
                 hexsend=False, ts=False, color=True, budget=DEFAULT_BUDGET,
                 log_path=None, capture=True, raw=False):
        self.port = port
        self.baud = baud
        self.bits = bits
        self.parity = parity
        self.stopbits = stop
        self.flow = flow
        self.xonxoff = flow == "xonxoff"
        self.rtscts = flow == "rtscts"
        self.nl = nl if nl in NEWLINES else "lf"
        self.enc = enc
        self.hex_mode = hex_mode
        self.hexsend = hexsend
        self.ts = ts
        self.pause = False
        self.raw = raw              # 交互透传模式: 按键直达设备, 行编辑由设备端 shell 完成
        self.color = color
        self.budget = budget if budget and budget > 0 else (1 << 30)
        self.dtr_on = True
        self.rts_on = True

        self.ser = None            # 当前串口对象(仅主线程创建/关闭)
        self.running = True
        self.stop = threading.Event()
        self.ser_lock = threading.Lock()
        self.pend_lock = threading.Lock()
        self.stat_lock = threading.Lock()
        self.log_lock = threading.Lock()

        self.pending = bytearray() # 待显示缓冲(接收线程写, 主线程读)
        self.hidden_mark = 0       # 已被省略未提示的屏显字节数
        self.rx_total = 0
        self.tx_total = 0
        self.shown = 0
        self.hidden_total = 0
        self.hex_off = 0
        self.log_path = log_path
        self.logf = None
        self.log_bytes = 0

        # 会话捕获: 收到第一包数据起就把全部原始数据落盘(临时 .part 文件),
        # 因此无论何时 /save 都能导出"这次打开串口以来"的完整日志, 包括已被
        # 屏幕省略的部分。不想要可用 --no-capture 关闭。
        # 所有自动生成的日志统一放在 logs/ 文件夹(懒创建)。
        self.capture_on = capture
        self._capture_name = datetime.now().strftime("serial_capture_%Y%m%d_%H%M%S_%f.part")
        self.capture_path = None     # 收到首包数据时确定(位于 logs/ 文件夹)
        self.capturef = None         # 惰性打开: 收到第一包数据时才创建文件
        self.capture_bytes = 0
        self.capture_saved_to = None
        self.log_dir = ""            # 统一日志文件夹, 首次需要时创建
        self._log_dir_checked = False

        self.notices = deque(maxlen=128)
        self.inp = ""              # 当前输入行
        self.inp_pos = 0
        self.hist = []
        self.hist_i = 0
        self._cc_seen = 0.0        # Ctrl+C 去抖
        self._cc_arm = 0.0         # 退出确认
        self._raw_cc_hinted = False  # 交互模式首次 Ctrl+C 的说明只提示一次
        self.kbd = _Keyboard()     # 平台键盘封装(Windows: msvcrt / Linux: termios raw)。
        # open() 要等 run_console 开始才调用: 交互选口阶段的 input() 依赖终端行编辑,
        # 提前关行编辑会导致按回车无响应。

        self.decoder = codecs.getincrementaldecoder(self.enc)(errors="replace")
        self.line_tail = ""        # 时间戳模式下的半行缓冲

        self._rx = threading.Thread(target=self._rx_loop, daemon=True)
        self._rx.start()

    # ------------------------------------------------------------ 控制台小工具

    def _c(self, code, s):
        return f"\x1b[{code}m{s}\x1b[0m" if self.color else s

    def note(self, msg):
        self.notices.append(msg)

    def draw_input(self):
        """在行首重绘用户正在输入的内容(仅 ANSI 模式)。"""
        if not self.color or not self.inp:
            return
        back = len(self.inp) - self.inp_pos
        sys.stdout.write("\r\x1b[36m>\x1b[0m " + self.inp + "\x1b[K")
        if back > 0:
            sys.stdout.write(f"\x1b[{back}D")
        sys.stdout.flush()

    # ------------------------------------------------------------ 串口开关

    def open_port(self):
        if not self.port:
            self.note("未设置串口, 先用 /port 串口名")
            return
        if self.ser:
            self.close_port()
        try:
            s = serial.serial_for_url(
                self.port, baudrate=self.baud,
                bytesize=self.bits, parity=PARITY_MAP.get(self.parity, "N"),
                stopbits=self.stopbits,
                timeout=TICK, write_timeout=2.0,
                xonxoff=self.xonxoff, rtscts=self.rtscts)
        except Exception as e:
            self.note(f"打开 {self.port} 失败: {e}")
            return
        try:  # 加大驱动缓冲, 给接收线程争取时间(主要在 Windows 生效)
            s.set_buffer_size(rx_size=2 * 1024 * 1024, tx_size=64 * 1024)
        except Exception:
            pass
        try:
            s.dtr = self.dtr_on
            s.rts = self.rts_on
        except Exception:
            pass
        with self.ser_lock:
            self.ser = s
        self.note(f"已打开 {self.port} @ {self.baud}")

    def close_port(self):
        with self.ser_lock:
            s, self.ser = self.ser, None
        if s:
            try:
                s.close()
            except Exception:
                pass
            self.note("串口已关闭")

    def mark_dead(self, reason):
        with self.ser_lock:
            s, self.ser = self.ser, None
        if s:
            try:
                s.close()
            except Exception:
                pass
        self.note(reason + " (输入 /open 可重连)")

    # ------------------------------------------------------------ 接收线程

    def _rx_loop(self):
        """只做一件事: 把串口数据搬进内存缓冲和日志文件, 保证不因刷屏而丢数据。"""
        while not self.stop.is_set():
            with self.ser_lock:
                s = self.ser
            if s is None:
                self.stop.wait(0.05)
                continue
            try:
                n = s.in_waiting
                data = s.read(1) if n == 0 else s.read(min(n, 65536))
            except Exception:
                self.mark_dead("串口连接中断(被拔出或被占用)")
                continue
            if not data:
                continue
            with self.stat_lock:
                self.rx_total += len(data)
            with self.pend_lock:
                self.pending += data
                if len(self.pending) > PEND_CAP:  # 长时间暂停等极端情况, 保内存
                    drop = len(self.pending) - PEND_CAP // 2
                    del self.pending[:drop]
                    self.hidden_mark += drop
            with self.log_lock:  # 捕获+日志检查与写入原子化, /log off、/save 不会打架
                if self.capture_on and self.capturef is None:
                    try:
                        d = self._ensure_log_dir()
                        self.capture_path = os.path.join(d, self._capture_name) if d else self._capture_name
                        self.capturef = open(self.capture_path, "ab")
                    except Exception as e:
                        self.capture_on = False
                        self.note(f"会话捕获文件创建失败, /save 不可用: {e}")
                if self.capturef is not None:
                    try:
                        self.capturef.write(data)
                        self.capture_bytes += len(data)
                    except Exception:
                        pass
                if self.logf is not None:
                    try:
                        self.logf.write(data)
                        self.log_bytes += len(data)
                    except Exception:
                        pass

    # ------------------------------------------------------------ 发送

    def serial_write(self, data):
        with self.ser_lock:
            s = self.ser
        if s is None:
            self.note("串口未打开, 用 /open")
            return False
        try:
            s.write(data)
            s.flush()
        except Exception as e:
            self.mark_dead(f"发送失败: {e}")
            return False
        with self.stat_lock:
            self.tx_total += len(data)
        return True

    def send_line(self, line):
        if self.hexsend:
            hx = re.sub(r"\s+", "", line).lower()
            if not hx or len(hx) % 2 or not re.fullmatch(r"[0-9a-f]+", hx):
                self.note("HEX 格式不合法, 应为偶数个十六进制字符, 如: 01A0FF")
                return
            self.serial_write(bytes.fromhex(hx))
        else:
            self.serial_write(line.encode(self.enc, errors="replace") + NEWLINES[self.nl])

    # ------------------------------------------------------------ 屏显渲染

    def _render_text(self, chunk):
        txt = self.decoder.decode(chunk)
        if not self.ts:
            return txt
        s = self.line_tail + txt
        self.line_tail = ""
        if not s:
            return ""
        parts = s.split("\n")
        tail = parts.pop()  # 最后一段是未完成的半行
        if len(tail) > 4096:  # 超长无换行也强制输出, 防止无限增长
            parts.append(tail)
            tail = ""
        else:
            self.line_tail = tail
        if not parts:
            return ""
        stamp = time.strftime("%H:%M:%S")
        if self.color:
            return "".join(f"\x1b[90m[{stamp}]\x1b[0m {ln}\n" for ln in parts)
        return "".join(f"[{stamp}] {ln}\n" for ln in parts)

    def _render_hex(self, chunk):
        out = []
        off = self.hex_off
        for i in range(0, len(chunk), 16):
            seg = chunk[i:i + 16]
            hexs = " ".join(f"{b:02X}" for b in seg)
            text = "".join(chr(b) if 32 <= b < 127 else "." for b in seg)
            out.append(f"{off + i:08X}  {hexs:<47}  |{text}|\n")
        self.hex_off += len(chunk)
        return "".join(out)

    def flush_display(self):
        """每个刷新周期调用一次: 按预算取缓冲打印, 落后太多则整行省略。"""
        if self.pause:
            return
        out = []
        if self.hidden_mark:
            hint = "，完整数据已写入日志" if self.logf else ""
            out.append(self._c("33", f"\n[…速率过快, 省略 {fmt_bytes(self.hidden_mark)} 字节屏显{hint}…]\n"))
            self.hidden_total += self.hidden_mark
            self.hidden_mark = 0
        with self.pend_lock:
            L = len(self.pending)
            if L > DROP_HI:  # 显示速度跟不上: 丢弃到最近整行为止, 保住行完整性
                cut = self.pending.rfind(b"\n", 0, L - 65536)
                drop = (cut + 1) if cut != -1 else L
                self.hidden_mark += drop
                del self.pending[:drop]
            chunk = bytes(self.pending[:self.budget])
            del self.pending[:self.budget]
        if chunk:
            self.shown += len(chunk)
            out.append(self._render_hex(chunk) if self.hex_mode else self._render_text(chunk))
        if out:
            sys.stdout.write("".join(out))
            self.draw_input()
            sys.stdout.flush()

    # ------------------------------------------------------------ 消息

    def flush_capture(self):
        """把会话捕获缓冲刷到磁盘(每显示帧调用一次)。"""
        if self.capturef is not None:
            with self.log_lock:
                try:
                    self.capturef.flush()
                except Exception:
                    pass

    def drain_notices(self):
        n = 0
        while self.notices and n < 8:
            msg = self.notices.popleft()
            n += 1
            sys.stdout.write(self._c("33", f"* {msg}") + "\n")
        if n:
            sys.stdout.flush()
            self.draw_input()

    # ------------------------------------------------------------ 键盘

    def clear_input(self):
        if self.color:
            sys.stdout.write("\r\x1b[K")
            sys.stdout.flush()
        self.inp = ""
        self.inp_pos = 0

    def _hist_prev(self):
        if not self.hist:
            return
        self.hist_i = max(0, self.hist_i - 1)
        self.inp = self.hist[self.hist_i]
        self.inp_pos = len(self.inp)
        self.draw_input()

    def _hist_next(self):
        if not self.hist:
            return
        self.hist_i = min(len(self.hist), self.hist_i + 1)
        self.inp = self.hist[self.hist_i] if self.hist_i < len(self.hist) else ""
        self.inp_pos = len(self.inp)
        self.draw_input()

    def on_ctrl_c(self):
        now = time.time()
        if now - self._cc_seen < 0.3:  # 信号与按键双路触发时去重
            return
        self._cc_seen = now
        # Ctrl+C 的含义是"中断": 无论哪种模式都把 ^C 转发给设备(与真实终端一致),
        # 本地同时回显暗灰色 ^C, 按键可感知
        if self.serial_write(b"\x03"):
            sys.stdout.write(self._c("90", "^C"))
            sys.stdout.flush()
        if self.inp:
            self.clear_input()
            return
        if now - self._cc_arm < 2.0:
            self.running = False
        else:
            self._cc_arm = now
            self.note("已转发 ^C 给设备; 2 秒内再按一次 Ctrl+C 退出程序 (或 /q)")

    def handle_key(self, ch):
        if ch == "\x11":  # Ctrl+Q: 行模式 <-> 交互透传模式
            if self.raw:
                self.raw = False
                self.note("已回到行模式(本地行编辑, 回车整行发送)")
            else:
                self._enter_raw()
            return
        if self.raw:
            self._raw_key(ch)
            return
        if ch.startswith("\x00"):   # 扩展键(方向键/Home/End/Del 等), 统一为 "\x00"+码
            k = ch[1:2]
            if k == "H":            # ↑ 历史
                self._hist_prev()
            elif k == "P":          # ↓ 历史
                self._hist_next()
            elif k == "Q":          # PgDn 同 ↓ 历史
                self._hist_next()
            elif k == "K":          # ←
                if self.inp_pos > 0:
                    self.inp_pos -= 1
                    self.draw_input()
            elif k == "M":          # →
                if self.inp_pos < len(self.inp):
                    self.inp_pos += 1
                    self.draw_input()
            elif k == "S":          # Delete
                if self.inp_pos < len(self.inp):
                    self.inp = self.inp[:self.inp_pos] + self.inp[self.inp_pos + 1:]
                    self.draw_input()
            # H/G=Home, F/O=End, 其余忽略
            return
        if ch == "\r":
            self.on_enter()
        elif ch == "\x03":  # Ctrl+C
            self.on_ctrl_c()
        elif ch in ("\x1b", "\x15"):  # Esc / Ctrl+U 清行
            self.clear_input()
        elif ch == "\x08":  # Backspace
            if self.inp_pos > 0:
                p = self.inp_pos - 1
                self.inp = self.inp[:p] + self.inp[p + 1:]
                self.inp_pos = p
                self.draw_input()
        elif ch == "\x7f":  # Ctrl+Backspace -> 全清
            self.clear_input()
        elif ch == "\t":
            if self.inp.startswith("/"):
                self._complete_local()   # 本地命令补全
            else:
                # 交给设备端 shell 补全(命令/文件名), 自动进入交互透传模式
                self._enter_raw(self.inp.encode(self.enc, errors="replace") + b"\t")
        elif ch >= " ":
            if len(self.inp) < 8192:
                self.inp = self.inp[:self.inp_pos] + ch + self.inp[self.inp_pos:]
                self.inp_pos += 1
                self.draw_input()
        # 其余控制字符忽略

    # -------------------------------------------------------- 交互透传模式

    def _enter_raw(self, prefix=b""):
        """进入交互透传模式; prefix 是进入前要先发给设备的字节(如已输入的内容+Tab)。"""
        self.inp = ""
        self.inp_pos = 0
        self.raw = True
        if self.color:
            sys.stdout.write("\r\x1b[K")  # 清掉本地输入行, 让设备回显接管
            sys.stdout.flush()
        if prefix:
            self.serial_write(prefix)
        self.note("交互模式: 按键直达设备, Tab补全/↑↓历史/退格由设备处理; Ctrl+Q 返回行模式")

    def _raw_key(self, ch):
        if ch.startswith("\x00"):   # 扩展键 -> ANSI 转义序列(设备端 shell 的历史/移动)
            seq = RAW_KEYMAP.get(ch[1:2])
            if seq:
                self.serial_write(seq)
            return
        if ch == "\x03":            # Ctrl+C: 透传 ^C 给设备 + 本地立即回显, 按键可感知
            self.serial_write(b"\x03")
            self._mark_sent_cc()
            return
        if ch == "\x08":            # 退格: 多数 shell 期望 DEL(0x7f)
            self.serial_write(b"\x7f")
            return
        # 其余全部原样透传: 回车\r、Tab补全、Ctrl+D、Esc、中文...
        self.serial_write(ch.encode(self.enc, errors="replace"))

    def _mark_sent_cc(self):
        """交互模式 Ctrl+C 的本地检测反馈: 即使设备不回显/洪水刷屏, 也能确认已发出。"""
        sys.stdout.write(self._c("90", "^C"))  # 暗灰色 ^C 标记, 与设备回显区分
        sys.stdout.flush()
        if not self._raw_cc_hinted:
            self._raw_cc_hinted = True
            self.note("已向设备发送 ^C (交互模式下 Ctrl+C 不会退出程序); "
                      "退出程序请先 Ctrl+Q 回行模式, 再输入 /q")

    def _complete_local(self):
        """行模式下按 Tab: 补全本地命令与常用参数。"""
        if self.inp_pos != len(self.inp) or not self.inp:
            return  # 简化: 仅在行尾补全
        buf = self.inp
        m = re.match(r"^(/\S*)(\s+)(\S*)$", buf)
        if m:  # 已有命令 -> 补全第二个参数
            opts = VALUE_HINTS.get(m.group(1).lower()[1:])
            if not opts:
                return
            tail = m.group(3)
            cands = [o for o in opts if o.startswith(tail)]
            if len(cands) == 1:
                self.inp = m.group(1) + m.group(2) + cands[0]
                self.inp_pos = len(self.inp)
                self.draw_input()
            elif len(cands) > 1:
                self.note("  " + "  ".join(cands))
            return
        cands = [c for c in COMMAND_LIST if c.startswith(buf.lower())]
        if not cands:
            return
        if len(cands) == 1:
            self.inp = cands[0] + " "
            self.inp_pos = len(self.inp)
            self.draw_input()
            return
        lcp = _lcp(cands)
        if len(lcp) > len(buf):  # 先补到公共前缀
            self.inp = lcp
            self.inp_pos = len(self.inp)
            self.draw_input()
        self.note("  " + "  ".join(cands))

    def on_enter(self):
        line = self.inp
        self.clear_input()
        if self.color:
            sys.stdout.write("\r\x1b[K")
        sys.stdout.write(self._c("36", ">>> " + line) + "\n")
        sys.stdout.flush()
        if line.startswith("/"):
            if line and (not self.hist or self.hist[-1] != line):
                self.hist.append(line)
            self.hist_i = len(self.hist)
            self.exec_cmd(line)
        elif line:
            if not self.hist or self.hist[-1] != line:
                self.hist.append(line)
            self.hist_i = len(self.hist)
            self.send_line(line)
        else:
            nl = NEWLINES[self.nl]
            if nl:
                self.serial_write(nl)

    # ------------------------------------------------------------ 命令

    HELP = """\
命令(以 / 开头才会当作命令, 其余内容按编码原样发送):
  /port [串口名]          不带参数: 列出可用串口; 带参数: 切换串口
                          (Windows 如 COM5, Linux 如 /dev/ttyUSB0)
  /baud <N>               设置波特率, 如 115200 / 1500000 / 921600
  /open /close /reopen    打开 / 关闭 / 重开串口
  /cfg bits=8 parity=N stop=1 flow=none   线路参数(parity: N E O, flow: none rtscts xonxoff)
  /nl none|lf|cr|crlf     发送行尾符 (默认 lf)
  /enc utf-8|gbk|ascii    收发编码
  /raw [on|off]           交互透传模式: 按键直达设备, Tab补全命令/文件名、↑↓历史、
                          退格等全部由设备端 shell 处理(同 MobaXterm 效果);
                          Ctrl+Q 快速切换; 行模式下按 Tab(非/命令)自动进入
  /hex                    十六进制显示接收数据
  /hexsend                发送模式 文本<->HEX (HEX 例: 01 A0 FF)
  /ts                     行首时间戳开关
  /pause /resume          暂停/恢复屏幕显示(后台仍接收并记录)
  /clear                  清屏
  /log on|off [文件]      原始数据落盘, 洪水刷屏也不丢数据(裸文件名存入 logs/ 文件夹)
  /save [文件名]          保存本次会话(打开串口以来)收到的全部原始数据,
                          含已被屏幕省略的部分; 缺省自动命名并存入 logs/ 文件夹
  /send <文件>            发送整个文件内容
  /dtr 0|1  /rts 0|1      控制 DTR/RTS 电平
  /info                   查看当前配置与统计
  /q 或 /exit             退出
快捷键: Tab 本地命令补全(行模式)/设备补全(交互模式)  Ctrl+Q 行模式⇄交互模式
        Ctrl+C 两种模式都转发 ^C 中断设备; 行模式下 2 秒内连按两次退出程序
        (注意: 后台守护进程如 udhcpc 收不到 ^C, 需 killall 停止)
        Esc/Ctrl+U 清行  ↑↓ 历史  ←→ 移动  Delete 删除 (均指行模式)"""

    def exec_cmd(self, line):
        parts = line.split()
        if not parts:
            return
        cmd = parts[0][1:].lower()
        arg = parts[1] if len(parts) > 1 else ""
        arg2 = parts[2] if len(parts) > 2 else ""
        rest = line[len(parts[0]):].strip()  # 原始剩余部分(支持含空格的路径)

        if cmd in ("h", "help", "?"):
            print(self.HELP)
        elif cmd == "port":
            if not arg:
                for p in list_ports.comports():
                    print(f"  {p.device}    {p.description}")
            else:
                self.port = arg
                if self.ser:
                    self.open_port()
                else:
                    self.note(f"串口已设为 {arg}, 输入 /open 打开")
        elif cmd == "baud":
            if arg.isdigit() and int(arg) >= 300:
                self.baud = int(arg)
                if self.ser:
                    try:
                        self.ser.baudrate = self.baud
                        self.note(f"波特率已切换为 {self.baud}")
                    except Exception as e:
                        self.note(f"切换失败: {e}, 可试 /reopen")
                else:
                    self.note(f"波特率已设为 {self.baud}")
            else:
                self.note(f"用法: /baud 115200 (当前 {self.baud})")
        elif cmd == "open":
            self.open_port()
        elif cmd == "close":
            self.close_port()
        elif cmd == "reopen":
            self.open_port()
        elif cmd == "cfg":
            self._cmd_cfg(parts[1:])
        elif cmd == "nl":
            if arg in NEWLINES:
                self.nl = arg
                self.note(f"发送行尾符: {arg}")
            else:
                self.note(f"用法: /nl none|lf|cr|crlf (当前 {self.nl})")
        elif cmd == "enc":
            if arg:
                try:
                    "".encode(arg)
                    self.enc = arg
                    self.decoder = codecs.getincrementaldecoder(arg)(errors="replace")
                    self.note(f"编码: {arg}")
                except Exception:
                    self.note(f"未知编码: {arg}")
            else:
                self.note(f"当前编码: {self.enc}")
        elif cmd == "hex":
            self.hex_mode = not self.hex_mode
            self.hex_off = 0
            self.note(f"十六进制显示: {'开' if self.hex_mode else '关'}")
        elif cmd == "hexsend":
            self.hexsend = not self.hexsend
            self.note(f"发送模式: {'HEX' if self.hexsend else '文本'}")
        elif cmd == "ts":
            self.ts = not self.ts
            self.line_tail = ""
            self.note(f"时间戳: {'开' if self.ts else '关'}")
        elif cmd == "pause":
            self.pause = True
            self.note("屏显已暂停(后台仍接收), /resume 恢复")
        elif cmd == "resume":
            self.pause = False
            self.note("屏显已恢复")
        elif cmd == "clear":
            sys.stdout.write("\x1b[2J\x1b[1;1H" if self.color else "\n" * 3)
            sys.stdout.flush()
        elif cmd == "log":
            self._cmd_log(arg, arg2)
        elif cmd == "save":
            self._cmd_save(rest)
        elif cmd == "raw":
            self._cmd_raw(arg)
        elif cmd == "send":
            self._cmd_send(rest)
        elif cmd == "dtr":
            self._set_line("dtr", arg)
        elif cmd == "rts":
            self._set_line("rts", arg)
        elif cmd == "info":
            self._cmd_info()
        elif cmd in ("q", "quit", "exit"):
            self.running = False
        else:
            self.note(f"未知命令: /{cmd}  (输入 /help 查看)")

    def _cmd_cfg(self, kvs):
        if not kvs:
            self.note(f"当前: bits={self.bits} parity={self.parity} stop={self.stopbits} flow={self.flow}")
            return
        for kv in kvs:
            if "=" not in kv:
                self.note(f"忽略非法参数 {kv} (应为 k=v)")
                continue
            k, v = kv.split("=", 1)
            k = k.lower()
            try:
                if k == "bits" and v.isdigit() and int(v) in (5, 6, 7, 8):
                    self.bits = int(v)
                elif k == "parity" and v.upper() in ("N", "E", "O"):
                    self.parity = v.upper()
                elif k == "stop" and v in ("1", "1.5", "2"):
                    self.stopbits = float(v)
                elif k == "flow" and v in ("none", "rtscts", "xonxoff"):
                    self.flow = v
                    self.xonxoff = v == "xonxoff"
                    self.rtscts = v == "rtscts"
                else:
                    self.note(f"忽略非法参数 {kv}")
            except Exception as e:
                self.note(f"参数 {kv} 错误: {e}")
        if self.ser:  # 在线热应用
            try:
                s = self.ser
                s.bytesize = self.bits
                s.parity = PARITY_MAP.get(self.parity, "N")
                s.stopbits = self.stopbits
                s.xonxoff = self.xonxoff
                s.rtscts = self.rtscts
            except Exception as e:
                self.note(f"在线应用失败: {e}, 可试 /reopen")
        self.note(f"线路参数: bits={self.bits} parity={self.parity} stop={self.stopbits} flow={self.flow}")

    def _cmd_log(self, arg, arg2):
        if arg == "on":
            if self.logf:
                self.note(f"日志已在记录: {self.log_path} (先 /log off)")
                return
            path = self._resolve_log_path(arg2 or datetime.now().strftime("serial_%Y%m%d_%H%M%S.log"))
            try:
                self.logf = open(path, "ab")
            except Exception as e:
                self.note(f"无法创建日志文件: {e}")
                return
            self.log_path = path
            self.log_bytes = 0
            self.note(f"开始记录原始数据到 {path} (收发洪水量大也不丢)")
        elif arg == "off":
            with self.log_lock:
                f, self.logf = self.logf, None
            if f:
                f.close()
                self.note(f"日志已停止: {self.log_path} (共 {fmt_bytes(self.log_bytes)} 字节)")
            else:
                self.note("日志未在记录")
        else:
            state = f"{self.log_path} (已写 {fmt_bytes(self.log_bytes)} 字节)" if self.logf else "关闭"
            self.note(f"日志: {state}   用法: /log on [文件名] | /log off")

    def _ensure_log_dir(self):
        """统一日志文件夹: <脚本目录>/logs (只读等异常则退回 <当前目录>/logs)。懒创建。"""
        if self._log_dir_checked:
            return self.log_dir
        self._log_dir_checked = True
        for base in (os.path.dirname(os.path.abspath(__file__)), os.getcwd()):
            d = os.path.join(base, "logs")
            try:
                os.makedirs(d, exist_ok=True)
                self.log_dir = d
                break
            except OSError:
                continue
        return self.log_dir

    def _resolve_log_path(self, name):
        """日志路径规则: 裸文件名放进统一 logs/ 文件夹; 用户写了路径的尊重原样。"""
        if not name:
            return name
        if os.path.dirname(name):
            return name
        d = self._ensure_log_dir()
        return os.path.join(d, name) if d else name

    def _cmd_save(self, path):
        """导出本次会话(打开串口以来)收到的全部原始数据, 含被屏幕省略的部分。"""
        if not self.capture_on:
            self.note("会话捕获未启用(--no-capture), 无法保存")
            return
        with self.log_lock:  # 锁内快照: 接收线程此刻暂停写盘, 拷贝内容一致
            if self.capturef is not None:
                try:
                    self.capturef.flush()
                except Exception:
                    pass
            if self.capture_bytes == 0:
                self.note("本次会话还没有收到任何数据")
                return
            dst = self._resolve_log_path(
                path or datetime.now().strftime("serial_%Y%m%d_%H%M%S.log"))
            try:
                shutil.copyfile(self.capture_path, dst)
            except Exception as e:
                self.note(f"保存失败: {e}")
                return
        self.capture_saved_to = dst
        self.note(f"已保存本次会话全部接收数据 ({fmt_bytes(self.capture_bytes)} 字节) -> {dst}")

    def _cmd_raw(self, arg):
        """交互透传模式开关: 按键直达设备, 由设备端 shell 完成 Tab 补全/历史/行编辑。"""
        if arg == "on" and not self.raw:
            self._enter_raw()
        elif arg == "off" and self.raw:
            self.raw = False
            self.note("已回到行模式(本地行编辑, 回车整行发送)")
        elif arg in ("on", "off"):
            self.note(f"已处于{'交互' if self.raw else '行'}模式")
        elif self.raw:
            self.raw = False
            self.note("已回到行模式(本地行编辑, 回车整行发送)")
        else:
            self._enter_raw()

    def _cmd_send(self, path):
        if not path:
            self.note("用法: /send 文件路径")
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception as e:
            self.note(f"读取文件失败: {e}")
            return
        ok = True
        for i in range(0, len(data), 4096):
            if not self.serial_write(data[i:i + 4096]):
                ok = False
                break
        if ok:
            self.note(f"已发送文件 {path} ({fmt_bytes(len(data))} 字节)")

    def _set_line(self, name, arg):
        if arg not in ("0", "1"):
            self.note(f"用法: /{name} 0|1")
            return
        val = arg == "1"
        setattr(self, name + "_on", val)
        with self.ser_lock:
            s = self.ser
        if s:
            try:
                setattr(s, name, val)
                self.note(f"{name.upper()} 已设为 {val}")
            except Exception as e:
                self.note(f"设置失败: {e}")
        else:
            self.note(f"{name.upper()} 已设为 {val} (串口未打开, 打开时生效)")

    def _cmd_info(self):
        state = "打开" if self.ser else "关闭"
        log = f"{self.log_path} (已写 {fmt_bytes(self.log_bytes)})" if self.logf else "关闭"
        if self.capture_on:
            if self.capture_bytes or self.capture_path:
                cap = f"{self.capture_path or self._capture_name} (已收 {fmt_bytes(self.capture_bytes)}, /save 可导出)"
            else:
                cap = f"待首包数据 (将存为 {self._capture_name})"
        else:
            cap = "关闭(--no-capture)"
        with self.pend_lock:
            pend = len(self.pending)
        with self.stat_lock:
            rx, tx = self.rx_total, self.tx_total
        print(f"""\
  串口: {self.port or '未设置'}   状态: {state}   波特率: {self.baud}
  线路: bits={self.bits} parity={self.parity} stop={self.stopbits} flow={self.flow}
  发送: 行尾={self.nl} 编码={self.enc} HEX发送={'开' if self.hexsend else '关'}
  模式: {'交互透传(设备负责行编辑)' if self.raw else '行模式(本地行编辑)'}
  显示: HEX={'开' if self.hex_mode else '关'} 时间戳={'开' if self.ts else '关'} 暂停={'是' if self.pause else '否'} 预算={fmt_bytes(self.budget)} B/50ms
  日志: {log}
  日志目录: {self._ensure_log_dir() or '当前目录(创建 logs/ 失败)'}
  会话捕获: {cap}
  统计: 接收 {fmt_bytes(rx)} | 屏显 {fmt_bytes(self.shown)} | 省略 {fmt_bytes(self.hidden_total + self.hidden_mark)} | 发送 {fmt_bytes(tx)}
  待显示缓冲: {fmt_bytes(pend)} 字节""")

    # ------------------------------------------------------------ 主循环

    def run_console(self):
        # 进入主循环才接管终端(Windows: 清 ENABLE_PROCESSED_INPUT; Linux: termios raw),
        # 让 Ctrl+C 以按键 \x03 到达程序——否则被终端拦截成 SIGINT, ^C 永远发不到设备
        # ("Ctrl+C 无效"的根因)。此后键盘只走 poll_key(非阻塞事件读取), 不依赖行编辑。
        if not self.kbd.open():
            signal.signal(signal.SIGINT, signal.SIG_DFL)  # 无键盘时至少 Ctrl+C 能终止
            self.note("键盘输入不可用(标准输入不是终端且无控制终端), 只能查看接收数据")
        self.note("已启动, 输入 /help 查看命令, /info 查看配置")
        next_tick = time.perf_counter()
        while self.running:
            now = time.perf_counter()
            if now >= next_tick:
                try:
                    self.flush_display()
                except Exception as e:
                    self.note(f"显示异常(已忽略): {e}")
                self.flush_capture()  # 定期落盘, 程序被强杀最多丢 50ms 数据
                next_tick = now + TICK
            self.drain_notices()
            if self.kbd.ok:
                n = 0
                try:
                    while n < 48:  # 限制每轮处理按键数, 保证刷新节奏
                        k = self.kbd.poll_key()
                        if k is None:
                            break
                        self.handle_key(k)
                        n += 1
                        if not self.running:
                            break
                except Exception as e:
                    self.note(f"输入异常(已忽略): {e}")
            time.sleep(0.004)

    def close_all(self):
        self.kbd.close()  # 先恢复终端(行编辑/回显), 后面的退出提示才能正常显示
        self.stop.set()
        with self.ser_lock:
            s, self.ser = self.ser, None
        if s:
            try:
                s.close()
            except Exception:
                pass
        with self.log_lock:
            f, self.logf = self.logf, None
            c, self.capturef = self.capturef, None
        for h in (f, c):
            if h:
                try:
                    h.close()
                except Exception:
                    pass
        if f:
            print(f"日志已保存: {self.log_path} ({fmt_bytes(self.log_bytes)} 字节)")
        if self.capture_on and self.capture_bytes > 0:
            final = None
            target = self.capture_path[:-5] + ".log"  # .part -> .log
            for i in range(5):  # Windows 下杀毒/索引可能短暂锁定新文件, 重试几次
                try:
                    os.replace(self.capture_path, target)
                    final = target
                    break
                except OSError:
                    time.sleep(0.2 * (i + 1))
            if final:
                print(f"本次会话接收数据已自动保存: {final} ({fmt_bytes(self.capture_bytes)} 字节)"
                      + ("" if self.capture_saved_to else " (此前未 /save, 即为完整日志)"))
            else:
                print(f"会话捕获文件(可直接改名使用): {self.capture_path} "
                      f"({fmt_bytes(self.capture_bytes)} 字节)")
        elif self.capture_on and self.capture_path and os.path.exists(self.capture_path):
            try:  # 没收到任何数据时的空捕获文件, 清理掉
                os.remove(self.capture_path)
            except Exception:
                pass


# ---------------------------------------------------------------- 交互式选口

def choose_interactive(t):
    print(__doc__.strip().split("\n")[0])
    print("-" * 60)
    while True:
        ports = list_ports.comports()
        if ports:
            for i, p in enumerate(ports, 1):
                print(f"  [{i}] {p.device:<10} {p.description}")
        else:
            print("  (未发现串口设备, 可手动输入名称)")
        s = input("请输入串口编号或名称(如 COM5 或 /dev/ttyUSB0, 回车刷新, q 退出): ").strip()
        if s.lower() in ("q", "quit", "exit"):
            sys.exit(0)
        if not s:
            continue
        if s.isdigit() and 1 <= int(s) <= len(ports):
            t.port = ports[int(s) - 1].device
        else:
            t.port = s
        break
    print("波特率:  [1] 115200   [2] 1500000   [3] 921600   [4] 自定义")
    b = input("选择 [1]: ").strip() or "1"
    if b == "2":
        t.baud = 1500000
    elif b == "3":
        t.baud = 921600
    elif b == "4":
        c = input("自定义波特率: ").strip()
        if c.isdigit() and int(c) >= 300:
            t.baud = int(c)
    print("-" * 60)


# ---------------------------------------------------------------- 入口

def main():
    ap = argparse.ArgumentParser(description="抗洪水串口调试终端")
    ap.add_argument("-p", "--port", help="串口名, 如 COM5 / /dev/ttyUSB0 (缺省进入交互选择)")
    ap.add_argument("-b", "--baud", type=int, default=115200,
                    help="波特率, 默认 115200, 常用 115200 / 1500000")
    ap.add_argument("--list", action="store_true", help="列出可用串口后退出")
    ap.add_argument("--hex", action="store_true", help="十六进制显示接收数据")
    ap.add_argument("--hexsend", action="store_true", help="HEX 方式发送")
    ap.add_argument("--ts", action="store_true", help="接收行显示时间戳")
    ap.add_argument("--nl", default="lf", choices=list(NEWLINES), help="发送行尾符, 默认 lf")
    ap.add_argument("--enc", default="utf-8", help="收发编码, 默认 utf-8")
    ap.add_argument("--flow", default="none", choices=("none", "rtscts", "xonxoff"), help="流控")
    ap.add_argument("--log", metavar="FILE", help="启动即把原始数据记录到文件")
    ap.add_argument("--raw", action="store_true",
                    help="启动即进入交互透传模式(Tab补全/方向键历史由设备处理, Ctrl+Q 返回)")
    ap.add_argument("--no-capture", action="store_true",
                    help="关闭会话捕获(默认自动记录全部接收数据供 /save 导出)")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                    help="每 50ms 最多打印的字节数, 默认 65536")
    ap.add_argument("--no-color", action="store_true", help="关闭 ANSI 颜色/重绘")
    args = ap.parse_args()

    if args.list:
        ports = list_ports.comports()
        for p in ports:
            print(f"{p.device:<12} {p.description}")
        if not ports:
            print("(未发现串口设备)")
        return

    enable_vt()
    signal.signal(signal.SIGINT, lambda *_: None)  # Ctrl+C 统一由键盘处理, 防止半路抛异常
    if not IS_WIN:  # 终端断开/被 kill 时也走 finally, 保证 termios 设置被恢复
        signal.signal(signal.SIGHUP, lambda *_: sys.exit(0))
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    t = SerialTool(port=args.port, baud=args.baud, nl=args.nl, enc=args.enc,
                   flow=args.flow, hex_mode=args.hex, hexsend=args.hexsend,
                   ts=args.ts, color=not args.no_color, budget=args.budget,
                   log_path=args.log, capture=not args.no_capture)
    try:
        if not args.port:
            choose_interactive(t)
        if args.log and not t.logf:
            t.exec_cmd("/log on " + args.log)
        if t.port:
            t.open_port()
        if args.raw:
            t.raw = True
            t.note("交互模式: 按键直达设备, Tab补全/↑↓历史/退格由设备处理; Ctrl+Q 返回行模式")
        t.run_console()
    except KeyboardInterrupt:
        pass
    finally:
        t.close_all()
        print("已退出。")


if __name__ == "__main__":
    main()
