#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rigctl.py —— 控制组合(rig)一条命令操控: T536 治具 GPIO 控受控板电源。

背景: "通过一个板子控制开发板"(rk-board-access §1D)的整条链——鉴别串口、进治具
控制台、发 GPIO 控电命令、回受控板验证——AI 每次现推导要十几个回合, 组合一多
还会认错对象。本工具把 rigs.json 登记的组合做成一等公民, 一条命令机械执行:

    rigctl status              # 所有组合: 控制台/受控串口/USB 判活 一行一结论
    rigctl rig-A status        # 单个组合同上
    rigctl rig-A on            # 开机(4 路 GPIO 全写 1, 幂等)
    rigctl rig-A off           # 关机(全写 0)
    rigctl rig-A coldboot      # 关机 → 放电 → 开机(冷启动, 全程约 discharge_s+5s)
    rigctl rig-A enter-loader  # 板型核对 → reboot loader → 轮询USB判定模式(全自动)
    rigctl rig-A bind          # 重探测受控板: 关电盯哪个串口灭→开电盯哪个醒,
                               # 结论自动写回 rigs.json 的 target(换板/挪线后跑这个)
    rigctl --selftest          # 链路归一化自检, 不碰设备

设计约定:
    * 控制通道走 serial_debug 实例的 agent HTTP(经 hub 发现, 不抢串口);
      治具控制台实例必须在线(本机约定 1.4.2@115200 → 8324);
    * 受控板是**槽位不是常量**: target 随接线/刷机而变, bind 重探测一次落盘;
    * bind 会真的断电再上电受控板(这就是探测原理), 请在可接受时才跑;
    * 身份坐标系与 boards.json/adbq 同源(by-path 链路段), 两套组合并存时
      各挂各的 hub 分支, 物理上串不了。
"""

import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RIGS_DEFAULT = os.path.join(HERE, "rigs.json")
HUB_DEFAULT = os.environ.get("RIGCTL_HUB", "http://127.0.0.1:8300")
READ_TAIL_BYTES = 4096          # 回读尾窗大小(足够覆盖命令回显)
SETTLE_S = 0.8                  # 发命令后等回显落账的时间
BIND_WATCH_S = 6                # bind 每阶段盯账本活动的秒数
# URLError(含 HTTPError 超类)/OSError(含 socket 超时)/HTTPException(非 HTTP 对端)
NET_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException)


class RigError(SystemExit):
    """带退出码的业务错误, 消息前缀 [rigctl]。"""


# ---------------------------------------------------------------- 身份归一化

def hub_chain(port_str):
    """串口标识 → hub 链路段。三种写法都认:
    pci-...-usb-0:1.4.2:1.0-port0 → "1.4.2"; "1.4.2" → 原样; "ttyUSB3" → None。
    与 adbq.hub_chain_of_key / agent_hub._jack_of 同一规则。"""
    if not port_str:
        return None
    m = re.search(r"usb-([\d.:]+)", port_str)
    if m:
        segs = m.group(1).split(":")
        return segs[1] if len(segs) > 1 else segs[0]
    if re.fullmatch(r"\d+(\.\d+)*", port_str.strip()):
        return port_str.strip()
    return None


def serial_node(port_str):
    """串口标识 → 当前设备节点名(ttyUSBn): by-path 解析软链, ttyUSBx 原样。
    实例无论用 by-path 还是 ttyUSBx 启动都能跟登记键对上——防止"登记键是
    by-path、实例却报 ttyUSBn"(或反之)造成的假性失配。"""
    if not port_str:
        return None
    if port_str.startswith("/dev/"):
        port_str = port_str[5:]
    if port_str.startswith("tty"):
        return port_str
    ch = hub_chain(port_str)
    if not ch:
        return os.path.basename(port_str)
    try:
        for entry in os.listdir("/dev/serial/by-path"):
            if hub_chain(entry) == ch:
                return os.path.basename(os.path.realpath(f"/dev/serial/by-path/{entry}"))
    except OSError:
        pass
    return None


def port_matches(port_str, console_key):
    """实例上报的 port 是否就是登记的 console 键: 链路段相等 或 设备节点相等。"""
    if not port_str:
        return False
    ch, ck = hub_chain(port_str), hub_chain(console_key)
    if ch and ck:
        return ch == ck
    np, nk = serial_node(port_str), serial_node(console_key)
    return bool(np and nk and np == nk)


# ---------------------------------------------------------------- rigs 登记

def load_rigs(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise RigError(2, f"rigs.json 读取失败: {e}")
    return {k: v for k, v in data.items() if not k.startswith("_")}


def save_rigs(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)          # 原子替换, 半写状态不会污染登记表


def console_chain_of_rig(rig, role):
    return rig.get(role, {}).get("console")


# ---------------------------------------------------------------- hub / 实例

def _api(base, method, path, body=None, timeout=10):
    req = urllib.request.Request(base + path,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def hub_tabs():
    try:
        return _api(HUB_DEFAULT, "GET", "/api/tabs")["data"]["tabs"]
    except NET_ERRORS as e:
        raise RigError(2, f"hub({HUB_DEFAULT}) 不可达: {e}; 实例必须经 hub 登记(先起 agent_hub.py)")


def find_instance(console_key):
    """按 console 键在 hub Tab 里找实例, 返回 (agent_port, tab) 或 None。"""
    for tab in hub_tabs():
        if port_matches(tab.get("port"), console_key):
            return tab["agent_port"], tab
    return None


def send_and_read(agent_port, cmd, settle=SETTLE_S):
    """经实例向控制台发一行命令, 读回尾窗文本(含回显)。"""
    base = f"http://127.0.0.1:{agent_port}"
    try:
        _api(base, "POST", "/api/tabs/0/send", {"text": cmd})
        time.sleep(settle)
        return _api(base, "GET",
                    f"/api/tabs/0/receive?max_bytes={READ_TAIL_BYTES}&tail=1")["data"]["text"]
    except NET_ERRORS as e:
        raise RigError(3, f"实例 {agent_port} 读写失败: {e}")


def power_cmdline(rig, value):
    """生成 4 路 GPIO 同值操作的 sysfs 一行命令(保持型, 带 RC 哨兵)。"""
    p = rig["controller"]["power"]
    gs = " ".join(str(g) for g in p["gpios"])
    return (f"for g in {gs}; do echo $g > /sys/class/gpio/export 2>/dev/null; "
            f"echo out > /sys/class/gpio/gpio$g/direction 2>/dev/null; "
            f"echo {value} > /sys/class/gpio/gpio$g/value; done; "
            f"echo __PWR_RC=$?__")


# ---------------------------------------------------------------- 动作

# ------------------------- H503 双协议(A5 二进制=现固件 / JSON V3.5=新固件)

# A5: 4字节帧 A5+通道+状态+校验(前三和&0xFF), 回帧 A5+通道+错误码+校验; 探活=截断3字节帧。
# JSON V3.5(协议源=tqtools/QSerial 仓 power_board_protocol.py): 行式JSON,
#   {"ch":名,"st":0/1} 控制, {"qr":名} 查询, 回带 code(0成功); 通道名大小写敏感。
A5_CH_BY_NAME = {"USB": 0x01, "5V2": 0x02, "5V3": 0x03, "12V4": 0x04, "12V5": 0x05}
JSON_V35_CHANNELS = ("USB0", "USB1", "5V2", "5V3", "12V4", "12V5", "RST_USB",
                     "PB3", "PB4", "PB5", "PB6", "PB7", "PB8", "PB12", "PB13",
                     "PA8", "PA9", "PA10", "PA15")
H503_ACK = {0: "成功", 1: "长度错", 2: "校验错", 3: "状态错"}


def _h503_xact(cmd_port, frame, timeout=1.5):
    """独占打开命令口发一帧读回帧(CDC 无波特率意义), 用完即关。"""
    import serial
    with serial.Serial(cmd_port, timeout=timeout) as s:
        s.reset_input_buffer()
        s.write(frame)
        return s.read(64)


def h503_detect(cmd_port):
    """双协议探测: 先 JSON(新固件会回 JSON 行), 再 A5(截断帧应答 a50101a7)。"""
    try:
        reply = _h503_xact(cmd_port, b'{"qr":"USB0"}\n')
        line = reply.split(b"\n")[0].strip()
        if line.startswith(b"{"):
            return "json-v3.5", line.decode("utf-8", "replace")
    except (OSError, ValueError):
        return None, "命令口打不开"
    try:
        reply = _h503_xact(cmd_port, bytes([0xA5, 0x01, 0x00]))
    except (OSError, ValueError) as e:
        return None, f"命令口读写失败: {e}"
    if reply[:3].hex() == "a50101":
        return "a5-bin", f"探活应答 {reply.hex()}"
    return None, f"未知应答 {reply.hex()!r}"


def h503_power(cmd_port, channel, state, protocol="a5-bin"):
    """按协议发电源命令。channel 用通道名(5V2/12V4/USB...), A5 内部译为 0x0X。"""
    if channel is None:
        raise RigError(2, "电源通道未绑定: 先跑 bind 差分或人工确认后填 rigs.json")
    if protocol == "json-v3.5":
        import json as _json
        cmd = _json.dumps({"ch": channel, "st": int(state)}, separators=(",", ":"))
        reply = _h503_xact(cmd_port, cmd.encode() + b"\n")
        try:
            resp = _json.loads(reply.split(b"\n")[0].strip().decode("utf-8", "replace"))
        except ValueError:
            raise RigError(4, f"回帧非 JSON: {reply[:64]!r}")
        code = resp.get("code")
        if code != 0:
            raise RigError(4, f"设备回 code={code}: {resp}")
        return f"JSON 回帧 {resp}"
    ch = A5_CH_BY_NAME.get(channel)
    if ch is None:
        try:
            ch = int(str(channel), 16) if isinstance(channel, str) else int(channel)
        except ValueError:
            raise RigError(2, f"未知通道名 {channel!r}(A5 协议可用: {list(A5_CH_BY_NAME)})")
    body = bytes([0xA5, ch, int(state) & 1])
    reply = _h503_xact(cmd_port, body + bytes([sum(body) & 0xFF]))
    if len(reply) < 4 or reply[0] != 0xA5 or reply[1] != ch:
        raise RigError(4, f"回帧异常: {reply.hex()!r}")
    if reply[2] != 0:
        raise RigError(4, f"回帧错误码 {reply[2]}({H503_ACK.get(reply[2], '?')})")
    return f"回帧 {reply.hex()} 成功"


def act_power(rig_name, rig, value, label):
    ctrl = rig.get("controller") or {}
    if str(ctrl.get("power", {}).get("method", "")).startswith("h503"):
        proto, _ = h503_detect(ctrl["cmd_port"])
        if not proto:
            raise RigError(3, "H503 命令口双协议探测均失败")
        # 上电顺序纪律: H503 的 USB 口供电先开再开目标板通道
        usb_ch = ctrl.get("usb_port_channel")
        if value == ctrl["power"].get("on_value", 1) and usb_ch is not None:
            h503_power(ctrl["cmd_port"], usb_ch, ctrl["power"].get("on_value", 1), proto)
        msg = h503_power(ctrl["cmd_port"], ctrl["power"].get("channel"), value, proto)
        print(f"{rig_name}: {label} 完成({proto}, 通道 {ctrl['power'].get('channel')}, {msg})")
        return
    hit = find_instance(console_chain_of_rig(rig, "controller"))
    if not hit:
        raise RigError(3, f"{rig_name} 的控制器控制台({console_chain_of_rig(rig, 'controller')}) "
                          "没有在线实例; 先起: serial_debug.py -p <by-path> -b 115200 --agent-http <port>")
    agent_port, _tab = hit
    out = send_and_read(agent_port, power_cmdline(rig, value))
    m = re.search(r"__PWR_RC=(\d+)__", out)
    if not m:
        raise RigError(4, f"控制台无 {label} 哨兵回显(尾窗: {out[-120:]!r}); "
                          "检查控制台是否被占用/登录态")
    if m.group(1) != "0":
        raise RigError(4, f"{label} 命令 rc={m.group(1)} 非零, GPIO 写入可能失败")
    print(f"{rig_name}: {label} 完成(GPIO 全写 {value}, 经实例 {agent_port})")


def _lsusb():
    try:
        return subprocess.run(["lsusb"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


USB_PID_MODE = {"0006": "运行态(adb gadget)", "350b": "Loader 下载态", "320b": "MaskROM"}


def usb_mode():
    """主机侧 2207 设备的 PID → 模式判定(无 upgrade_tool 时按 PID 映射, 见闸门口诀)。"""
    pids = re.findall(r"ID\s+2207:([0-9a-f]{4})", _lsusb(), re.I)
    if not pids:
        return None
    pid = pids[0].lower()
    return f"2207:{pid} → {USB_PID_MODE.get(pid, '未知PID')}"


def act_enter_loader(rig_name, rig):
    """全自动进 Loader: 板型核对 → reboot loader → 轮询 USB 枚举判定模式。

    依据本 SDK rk3588s.dtsi 的 reboot-mode 节点: mode-loader=BOOT_BL_DOWNLOAD,
    `reboot loader` 是内核正式支持路径; MaskROM 无软件入口(mode-maskrom 不存在)。
    """
    thit = find_instance(console_chain_of_rig(rig, "target"))
    if not thit:
        raise RigError(3, "受控板控制台无在线实例, 无法下发 reboot loader")
    ap = thit[0]
    model = send_and_read(ap, "cat /proc/device-tree/model", settle=1.0)
    print(f"{rig_name}: 板型核对: {(model or '').strip()[-80:]!r}")
    cmd = (rig.get("target") or {}).get("enter_loader_cmd")
    if cmd:
        print(f"{rig_name}: 用固件自带入口 {cmd!r}(init 横幅记载)...")
        send_and_read(ap, cmd, settle=1.0)
    else:
        send_and_read(ap, "reboot loader", settle=1.0)
    print(f"{rig_name}: 已下发, 盯 USB 枚举判定模式(至多 45s; loader 态串口静默属正常)...")
    for _ in range(24):
        time.sleep(2)
        mode = usb_mode_for(rig)
        if mode:
            break
    if not mode:
        print(f"{rig_name}: 45s 未见本板枚举(adb 序列号/350b/320b 均无) —— "
              f"下载线未接主机时 Loader 态不可见, 属预期; 烧录需接下载线+upgrade_tool/RKDevTool")
        return
    print(f"{rig_name}: USB {mode}")
    if "Loader" in mode:
        print(f"{rig_name}: 已在 Loader 态; 烧录后复位: python3 rigctl.py {rig_name} coldboot")


def _adb_devices_l():
    try:
        return subprocess.run(["adb", "devices", "-l"], capture_output=True, text=True,
                              timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def usb_mode_for(rig):
    """判定受控板下载口模式——以 adb 序列号认主, 杜绝总线上其他 2207 设备污染判定
    (2026-11-08 实训: SSI_17 设备占 2207:0006, 曾让 rig-A 被误判为运行态)。
    运行态: adb devices -l 按序列号匹配(链路一并报出); Loader/MaskROM: adb 不列,
    扫 lsusb 的 350b/320b(需下载线接主机才可见, 无 upgrade_tool 时按 PID 口诀)。"""
    adb_serial = (rig.get("target") or {}).get("adb_serial", "")
    if adb_serial:
        for line in _adb_devices_l().splitlines():
            if adb_serial in line:
                m = re.search(r"usb:\d+-([\d.]+)", line)
                chain = m.group(1) if m else "?"
                return f"运行态(adb@链路{chain}, 序列号匹配)"
    pids = re.findall(r"ID\s+2207:([0-9a-f]{4})", _lsusb(), re.I)
    download = [p.lower() for p in pids if p.lower() in ("350b", "320b")]
    if download:
        pid = download[0]
        return f"2207:{pid} → {USB_PID_MODE.get(pid)}"
    if pids:
        return None   # 总线上有别的 2207 设备, 但都不是本板 → 视为未见
    return None


def _diff_snapshot():
    """观察源快照: adb 序列号 + lsusb VID:PID + hub 各实例账本字节数。"""
    devs = {}
    for line in _adb_devices_l().splitlines():
        m = re.match(r"^(\S+)\s+\S+\s+usb:(\d+-[\d.]+)", line)
        if m:
            devs[f"adb:{m.group(1)}"] = "在线"
    for line in _lsusb().splitlines():
        m = re.search(r"ID\s+([0-9a-f]{4}):([0-9a-f]{4})", line, re.I)
        if m:
            devs[f"usb:{m.group(1).lower()}:{m.group(2).lower()}"] = "枚举"
    for t in hub_tabs():
        try:
            cb = _api(f"http://127.0.0.1:{t['agent_port']}", "GET", "/api/info",
                      timeout=5)["capture_bytes"]
            devs[f"tty:{t['agent_port']}({t['title']})"] = cb
        except Exception:
            pass
    return devs


def act_bind_h503(rig_name, rig, rigs_path, all_rigs, confirmed=False):
    """H503 通道↔板绑定: 断电差分法(金标准, skill 固化的 2026-10-27 事故对策)。

    ⚠ 破坏性: 逐路断电观察, 且 H503 无状态查询命令——扫完各路保持 OFF,
    原上电状态不可恢复(需人工按需重开)。故必须 --yes 显式确认。
    """
    ctrl = rig["controller"]
    if not confirmed:
        raise RigError(2, f"差分会逐路断电且结束后保持全 OFF(原状态不可恢复); "
                          f"确认接好线后跑: rigctl.py {rig_name} bind --yes")
    proto, _ = h503_detect(ctrl["cmd_port"])
    if not proto:
        raise RigError(3, "H503 命令口双协议探测均失败")
    binding = None
    for name in ("USB", "5V2", "5V3", "12V4", "12V5"):   # USB=板载USB2/USB3口供电
        a = _diff_snapshot()
        h503_power(ctrl["cmd_port"], name, 0, proto)
        time.sleep(5)
        b = _diff_snapshot()
        gone = [k for k, v in a.items() if k not in b and not isinstance(v, int)]
        gone_tty = [k for k in a if k not in b and k.startswith("tty:")]
        print(f"通道 {name} 断电后消失: {gone + gone_tty or '(无变化→空通道)'}")
        if gone or gone_tty:
            binding = name
            print(f"{rig_name}: 绑定命中 → 通道 {name} (记录进 rigs.json)")
            for k in gone:
                if k.startswith("adb:"):
                    rig["target"]["adb_serial"] = k[4:]
            for k in gone_tty:
                ap = k.split("(")[0][4:]
                tab = next((t for t in hub_tabs() if str(t["agent_port"]) == ap), None)
                if tab:
                    rig["target"]["console"] = hub_chain(tab["port"]) or tab["port"]
                    rig["target"]["baud"] = tab["baud"]
            rig["target"]["name"] = f"通道{name}目标板"
            rig["target"]["bound_at"] = time.strftime("%Y-%m-%d %H:%M")
            rig["target"]["bound_by"] = "rigctl bind 差分"
            break
        # 空通道保持 OFF, 继续下一路
    ctrl["power"]["channel"] = binding
    save_rigs(rigs_path, all_rigs)
    if binding is None:
        print(f"{rig_name}: 0x02..0x05 全部无差分 —— 没探到目标板; "
              f"确认目标板电源线接的是 H503 哪路(5V2/5V3/12V4/12V5), 或直接人工填 channel")
    else:
        print(f"{rig_name}: 现在可 status/on/off/coldboot 全套操作; "
              f"注意各路当前为 OFF(差分副作用), 按需 python3 rigctl.py {rig_name} on")


def usb_alive(target):
    vidpid = (target.get("alive_check") or {}).get("usb_vid_pid")
    if not vidpid:
        return None
    try:
        out = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    prefix = vidpid.lower().replace(":", "")
    for line in out.splitlines():
        m = re.search(r"ID\s+([0-9a-f]{4}):([0-9a-f]{4})", line, re.I)
        if m and (m.group(1) + m.group(2)).startswith(prefix.split(":")[0][:4]):
            if f"{m.group(1)}:{m.group(2)}" == vidpid.lower():
                return line.strip()
    return False


def act_status(rig_name, rig):
    parts = []
    ctrl = rig.get("controller") or {}
    if str(ctrl.get("power", {}).get("method", "")).startswith("h503"):
        proto, msg = h503_detect(ctrl["cmd_port"])
        ch = ctrl["power"].get("channel")
        parts.append(f"控制器{ctrl.get('name','H503')} "
                     f"{'✔'+proto+'('+msg+')' if proto else '✘双协议探测失败('+msg+')'}"
                     + (f" 通道{ch}" if ch is not None else " 通道未绑定"))
        thit = find_instance(console_chain_of_rig(rig, "target"))
        tgt = rig.get("target") or {}
        if thit:
            parts.append(f"受控{tgt.get('name','?')} 串口 ✔(实例{thit[0]})")
        else:
            parts.append(f"受控{tgt.get('name','?')} 串口 ✘ 无实例")
        mode = usb_mode_for(rig)
        parts.append(f"USB {mode}" if mode else "USB 未见本板(未接下载线/未枚举)")
        print(f"{rig_name}: " + " | ".join(parts))
        return
    hit = find_instance(console_chain_of_rig(rig, "controller"))
    if hit:
        _ap, tab = hit
        parts.append(f"控制器{rig['controller']['name']} ✔(实例{hit[0]} @{tab['baud']} open={tab['open']})")
    else:
        parts.append(f"控制器控制台 ✘ 无实例")
    thit = find_instance(console_chain_of_rig(rig, "target"))
    tgt = rig["target"]
    if thit:
        _ap, tab = thit
        parts.append(f"受控{tgt['name']} 串口 ✔(实例{thit[0]} rx={tab['rx_count']})")
    else:
        parts.append(f"受控{tgt['name']} 串口 ✘ 无实例")
    mode = usb_mode_for(rig)
    if mode:
        parts.append(f"USB {mode} ✔")
    else:
        parts.append("USB 未见本板(下载线未接主机/未枚举)")
    print(f"{rig_name}: " + " | ".join(parts))


def _capture_grow_map(candidates, seconds):
    """轮询各实例 capture_bytes, 返回 {agent_port: 是否有增长}。"""
    marks = {}
    for ap in candidates:
        try:
            marks[ap] = _api(f"http://127.0.0.1:{ap}", "GET", "/api/info", timeout=5)["capture_bytes"]
        except NET_ERRORS:
            marks[ap] = None
    time.sleep(seconds)
    grew = {}
    for ap in candidates:
        try:
            now = _api(f"http://127.0.0.1:{ap}", "GET", "/api/info", timeout=5)["capture_bytes"]
        except NET_ERRORS:
            now = None
        grew[ap] = (marks[ap] is not None and now is not None and now > marks[ap])
    return grew


def act_bind(rig_name, rig, rigs_path, all_rigs):
    """重探测受控槽位: 关电→盯谁灭→开电→盯谁醒→写回 target。"""
    ctrl = find_instance(console_chain_of_rig(rig, "controller"))
    if not ctrl:
        raise RigError(3, "控制器控制台无在线实例, 无法 bind")
    ctrl_ap = ctrl[0]
    # 候选 = hub 里除控制器自己外的全部实例(受控板控制台必然插在主机上)
    cands = [t["agent_port"] for t in hub_tabs() if t["agent_port"] != ctrl_ap]
    if not cands:
        raise RigError(3, "hub 里没有候选实例(受控板串口实例必须在线)")
    print(f"{rig_name}: bind 开始, 候选实例 {cands}(会真实断电再上电受控板)")
    act_power(rig_name, rig, rig["controller"]["power"]["off_value"], "关电")
    dark = {ap for ap, g in _capture_grow_map(cands, BIND_WATCH_S).items() if not g}
    act_power(rig_name, rig, rig["controller"]["power"]["on_value"], "开电")
    boot = {ap for ap, g in _capture_grow_map(cands, BIND_WATCH_S).items() if g}
    hits = dark & boot if dark else boot
    if len(hits) != 1:
        raise RigError(4, f"探测不唯一: 灭={sorted(dark)} 醒={sorted(boot)}; "
                          "把受控板串口实例起好、其余口静默后重试, 或手改 rigs.json")
    ap = hits.pop()
    tab = next(t for t in hub_tabs() if t["agent_port"] == ap)
    old = console_chain_of_rig(rig, "target")
    new_chain = hub_chain(tab["port"])
    if old and old != new_chain:
        rig.setdefault("_history", []).append(
            {"target": f"{rig['target'].get('name', '?')} console@{old}", "until": time.strftime("%Y-%m-%d %H:%M"),
             "note": "bind 重探测迁移"})
    rig["target"]["console"] = new_chain
    rig["target"]["baud"] = tab["baud"]
    rig["target"]["bound_at"] = time.strftime("%Y-%m-%d")
    rig["target"]["bound_by"] = f"rigctl bind(实例{ap})"
    save_rigs(rigs_path, all_rigs)
    print(f"{rig_name}: 受控槽位 → {tab['port']}(@{tab['baud']}, 实例{ap}), 已写回 rigs.json")


# ---------------------------------------------------------------- 入口

def selftest():
    assert hub_chain("pci-0000:0c:00.3-usb-0:1.4.2:1.0-port0") == "1.4.2"
    assert hub_chain("1.4.2") == "1.4.2"
    assert hub_chain("ttyUSB3") is None
    assert port_matches("pci-0000:0c:00.3-usb-0:1.4.2:1.0-port0", "1.4.2")
    assert port_matches("/dev/ttyUSB3", "ttyUSB3")
    assert not port_matches("pci-0000:0c:00.3-usb-0:1.4.4:1.0-port0", "1.4.2")
    # 节点解析: 实例报 ttyUSBn、登记键是 by-path 链路(或反之)也必须对上
    n = serial_node("1.4.4")
    assert n and n.startswith("ttyUSB"), f"1.4.4 解析失败: {n}"
    assert port_matches("/dev/ttyUSB3", "1.4.4") == (n == "ttyUSB3"), "节点匹配异常"
    print(f"selftest OK: 链路归一化/节点解析(1.4.4→{n})/匹配 全过")


def main():
    args = sys.argv[1:]
    rigs_path = os.environ.get("RIGCTL_RIGS", RIGS_DEFAULT)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return
    if args[0] == "--selftest":
        selftest()
        return
    rigs = load_rigs(rigs_path)
    if not args:
        raise RigError(2, f"没有组合登记: {rigs_path}")
    name = args[0] if args[0] in rigs else None
    action = args[1] if name else args[0]
    if name is None:
        # 无组合名: 只支持 status 广播
        if action != "status":
            raise RigError(2, f"未知组合 {args[0]!r}(现有: {', '.join(rigs)})")
        for n, r in rigs.items():
            act_status(n, r)
        return
    if len({n for n in rigs if n.startswith(name)}) > 1 and name not in rigs:
        raise RigError(2, f"组合前缀 {name!r} 歧义")
    if name not in rigs:
        raise RigError(2, f"没有组合 {name!r}(现有: {', '.join(rigs)})")
    rig = rigs[name]
    action = args[1] if len(args) > 1 else "status"
    if action == "status":
        act_status(name, rig)
    elif action == "on":
        act_power(name, rig, rig["controller"]["power"]["on_value"], "开机")
    elif action == "off":
        act_power(name, rig, rig["controller"]["power"]["off_value"], "关机")
    elif action == "coldboot":
        p = rig["controller"]["power"]
        act_power(name, rig, p["off_value"], "关机")
        wait = int(p.get("discharge_s", 20))
        print(f"{name}: 放电等待 {wait}s ...")
        time.sleep(wait)
        act_power(name, rig, p["on_value"], "开机")
        print(f"{name}: 冷启动已触发, 盯受控串口看 boot 洪流")
    elif action == "bind":
        if str((rig.get("controller") or {}).get("power", {}).get("method", "")).startswith("h503"):
            act_bind_h503(name, rig, rigs_path, rigs, confirmed="--yes" in args)
        else:
            act_bind(name, rig, rigs_path, rigs)
    elif action == "enter-loader":
        act_enter_loader(name, rig)
    else:
        raise RigError(2, f"未知动作 {action!r}(status/on/off/coldboot/bind)")


if __name__ == "__main__":
    try:
        main()
    except RigError as e:
        print(f"[rigctl] {e}", file=sys.stderr)
        sys.exit(e.code)
