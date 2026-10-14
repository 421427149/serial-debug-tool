# -*- coding: utf-8 -*-
"""洪水压测: loop:// 虚拟串口满速灌数据, 验证不崩溃、账目守恒、日志完整、省略路径生效。"""
import os
import sys
import time
import threading

import serial_debug as sd


class NullIO:  # 静音屏显输出
    def write(self, s):
        pass

    def flush(self):
        pass


real = sys.stdout
t = sd.SerialTool(port="loop://", baud=115200, budget=512, color=False)  # 小预算逼出"省略"路径
t.open_port()
assert t.ser is not None, "loop:// 打开失败"
sys.stdout = NullIO()

t._cmd_log("on", "flood_test.log")

written = [0]


def writer():  # 满速灌数据 ~6 秒(loop:// 背压速率 ~140KB/s ≈ 1.5M 波特率线速, 即真实洪水场景)
    data = b"THE QUICK BROWN FOX JUMPS OVER THE LAZY DOG 0123456789\n" * 64
    end = time.time() + 6.0
    while time.time() < end:
        try:
            t.ser.write(data)
            written[0] += len(data)
        except Exception as e:
            real.write(f"writer error: {e}\n")
            break


th = threading.Thread(target=writer)
th.start()

# 以 20Hz 节流刷新(与真实主循环一致)
endt = time.time() + 7.0
while time.time() < endt:
    time.sleep(0.05)
    t.flush_display()
    t.drain_notices()
th.join()

# 放开预算, 排空剩余缓冲
t.budget = 1 << 26
for _ in range(300):
    if not t.pending:
        break
    time.sleep(0.02)
    t.flush_display()

with t.pend_lock:
    pend = len(t.pending)
rx, shown = t.rx_total, t.shown
hidden = t.hidden_total + t.hidden_mark
t.exec_cmd("/log off")
log_path = t._resolve_log_path("flood_test.log")
logsize = os.path.getsize(log_path)
cap_bytes = t.capture_bytes            # 会话捕获应与接收总量一致
t.exec_cmd("/save saved_session.log")  # 洪水结束后任意时刻都可完整导出
save_path = t._resolve_log_path("saved_session.log")
sys.stdout = real
t.close_all()
savesize = os.path.getsize(save_path)

print(f"灌入数据   written = {written[0]:>13,} 字节")
print(f"接收总数   rx      = {rx:>13,} 字节")
print(f"已屏显     shown   = {shown:>13,} 字节")
print(f"已省略     hidden  = {hidden:>13,} 字节")
print(f"剩余缓冲   pending = {pend:>13,} 字节")
print(f"日志大小   log     = {logsize:>13,} 字节  ({log_path})")
print(f"/save 导出 saved   = {savesize:>13,} 字节")

ok = True
if rx != written[0]:
    print("!! 传输有丢失(接收 != 灌入)"); ok = False
if rx != shown + hidden + pend:
    print("!! 字节账目不守恒"); ok = False
if logsize != rx:
    print("!! 日志不完整(应有每一字节)"); ok = False
if cap_bytes != rx:
    print("!! 会话捕获字节数与接收总数不一致"); ok = False
if savesize != rx:
    print("!! /save 导出不完整"); ok = False
if not os.path.dirname(t.capture_path or "").endswith("logs"):
    print(f"!! 捕获文件不在 logs/ 文件夹: {t.capture_path}"); ok = False
if not os.path.dirname(save_path).endswith("logs"):
    print(f"!! /save 结果不在 logs/ 文件夹: {save_path}"); ok = False
if hidden == 0:
    print("!! 未触发省略路径, 测试覆盖不足"); ok = False
if rx < 200_000:
    print("!! 洪水量太小, 测试无效"); ok = False
os.remove(log_path)
os.remove(save_path)


def rm_quiet(pattern):  # Windows 下新文件可能被杀毒/索引短暂锁定, 重试清理
    for _ in range(5):
        try:
            import glob
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
print("FLOOD TEST OK" if ok else "FLOOD TEST FAILED")
sys.exit(0 if ok else 1)
