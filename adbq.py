#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""adbq.py —— 按板名操作 adb 的包装器(多板防串台)。

背景: RK 板的 USB adb 序列号大量重复甚至为空, 多板同插时 `adb -s 序列号` 失效,
照着 -s 打就会打到错的板上(串口侧"认错板子"的 adb 版)。本工具用与串口防线同一套
身份体系: `adb devices -l` 每台设备自带 `usb:1-x.y.z` 物理链路, 与 boards.json 里
by-path 键的 hub 链路段(如 1.4.4)是同一个东西——板名映射一份通吃串口/adb 两界。

用法:
    adbq boards                      # 列在线 adb 设备 + 板名解析结果(先看这个)
    adbq TQ3588 shell ls /           # 按板名执行任意 adb 子命令(自动解析成 adb -s)
    adbq TQ3588 -n shell ls /        # 只打印解析后的命令, 不执行
    adbq --selftest                  # 内嵌自检(解析器, 不碰真设备)

约定与边界:
    * boards.json 里只有 by-path 键(含 "usb-")能参与 adb 解析; ttyUSBx 过渡键不行
      (它不携带物理链路信息, 这也是尽快迁 by-path 的又一个理由);
    * 板名可写全名也可写前缀(唯一匹配即可, 如 TQ3 匹配 TQ3588);
    * 不带板名直接跑 adb 的"广播"语义(如 adb install)在多板时是事故源, 本工具
      强制要求板名, 不提供广播;
    * adb 版本旧(如 Debian 源 29.x)遇到新 adbd 可能 unauthorized, 与本工具无关,
      需换 platform-tools。
"""

import json
import os
import re
import subprocess
import sys

BOARDS_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boards.json")
ADB = os.environ.get("ADBQ_ADB", "adb")
DEVICE_STATES = ("device", "offline", "unauthorized", "recovery", "sideload", "bootloader", "host", "authorizing")


def hub_chain_of_usb(usb_prop):
    """adb 的 usb 属性("1-1.4.4" = 总线1, 链路1.4.4) -> 链路段 "1.4.4"。"""
    if not usb_prop:
        return None
    m = re.match(r"^\d+-(.+)$", usb_prop.strip())
    return m.group(1) if m else None


def hub_chain_of_key(key):
    """boards.json 的 by-path 键(pci-...-usb-0:1.4.4:1.0-port0) -> 链路段 "1.4.4"。

    与 agent_hub.Hub._jack_of 同一规则: 取 "usb-" 后第一个冒号段(=hub 链)。
    非 by-path 键(ttyUSBx 等)返回 None, 不参与 adb 解析。
    """
    m = re.search(r"usb-([\d.:]+)", key)
    if not m:
        return None
    segs = m.group(1).split(":")
    return segs[1] if len(segs) > 1 else segs[0]


def load_chain_names(boards_path):
    """boards.json -> {链路段: 板名}; 同名多键(过渡键+by-path 键)后者覆盖前者即可。"""
    try:
        with open(boards_path, encoding="utf-8") as f:
            mapping = json.load(f)
    except (OSError, ValueError) as e:
        print(f"[adbq] boards.json 读取失败: {e}", file=sys.stderr)
        return {}
    chain_names = {}
    for key, entry in mapping.items():
        chain = hub_chain_of_key(key)
        name = entry.get("name") if isinstance(entry, dict) else None
        if chain and name:
            chain_names[chain] = name
    return chain_names


def adb_devices():
    """adb devices -l -> [{serial, state, usb, model, product, chain}]。"""
    try:
        out = subprocess.run([ADB, "devices", "-l"], capture_output=True, text=True,
                             timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[adbq] adb 不可用({e}), 检查 PATH 或 ADBQ_ADB 环境变量", file=sys.stderr)
        sys.exit(2)
    devices = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] not in DEVICE_STATES:
            continue
        props = {}
        for token in parts[2:]:
            if ":" in token:
                k, _, v = token.partition(":")
                props[k] = v
        devices.append({"serial": parts[0], "state": parts[1],
                        "usb": props.get("usb"), "model": props.get("model"),
                        "product": props.get("product"),
                        "chain": hub_chain_of_usb(props.get("usb"))})
    return devices


def resolve(board, devices, chain_names):
    """板名(或唯一前缀) -> 目标设备; 解析不出/不唯一时给出可执行的报错。"""
    names = sorted({n for n in chain_names.values() if n})
    candidates = [n for n in names if n.startswith(board)]
    if not candidates:
        raise SystemExit(f"[adbq] boards.json 里没有板名 {board!r}(现有: {', '.join(names) or '无'})")
    if len(candidates) > 1:
        raise SystemExit(f"[adbq] 前缀 {board!r} 歧义: {candidates}, 请写全名")
    target_name = candidates[0]
    chains = [c for c, n in chain_names.items() if n == target_name]
    hits = [d for d in devices if d["chain"] in chains]
    if not hits:
        online = ", ".join(f"{d['chain'] or '?'}({d['serial'][:12]})" for d in devices) or "无设备在线"
        raise SystemExit(f"[adbq] 板 {target_name} 不在线(在线: {online}); "
                         "usb 链路对不上时先跑 adbq boards 核对")
    if len(hits) > 1:
        serials = [d["serial"] for d in hits]
        raise SystemExit(f"[adbq] 链路 {chains} 竟命中多台(序列号 {serials}), "
                         "物理插口异常, 请人工核对 adbq boards")
    return target_name, hits[0]


def cmd_boards(boards_path):
    devices = adb_devices()
    chain_names = load_chain_names(boards_path)
    if not devices:
        print("(无 adb 设备在线)")
        return
    for d in devices:
        name = chain_names.get(d["chain"]) or "?"
        print(f"{name:<16} state={d['state']:<12} chain={d['chain'] or '?':<8} "
              f"serial={d['serial'][:16]:<16} model={d['model'] or '-'} "
              f"usb={d['usb'] or '-'}")
    if any(chain_names.get(d["chain"]) is None for d in devices if d["chain"]):
        print("(带 ? 的设备: boards.json 无此链路的 by-path 键, 补一行即可)")


def selftest():
    """内嵌自检: 解析器逻辑(重复序列号/链路映射/前缀歧义), 不碰真设备。"""
    assert hub_chain_of_usb("1-1.4.4") == "1.4.4"
    assert hub_chain_of_usb(None) is None
    assert hub_chain_of_usb("2-1") == "1"
    key = "pci-0000:0c:00.3-usb-0:1.4.4:1.0-port0"
    assert hub_chain_of_key(key) == "1.4.4", hub_chain_of_key(key)
    assert hub_chain_of_key("ttyUSB0") is None
    chain_names = {"1.2": "TQ3588", "1.4.4": "TQ3588B", "1.4.1": "TQ3562"}
    # 两台同序列号的 RK 板, 靠链路区分——正是本工具的存在意义
    devices = [{"serial": "20080411", "state": "device", "usb": "1-1.4.4",
                "model": None, "product": None, "chain": "1.4.4"},
               {"serial": "20080411", "state": "device", "usb": "1-1.4.1",
                "model": None, "product": None, "chain": "1.4.1"}]
    name, dev = resolve("TQ3562", devices, chain_names)
    assert (name, dev["serial"], dev["chain"]) == ("TQ3562", "20080411", "1.4.1")
    name, dev = resolve("TQ3588B", devices, chain_names)
    assert dev["chain"] == "1.4.4", "同序列号必须仍能选中对的板"
    try:
        resolve("TQ3588", devices, chain_names)   # TQ3588 是 TQ3588B 的前缀但也是全名?
        raise AssertionError("前缀歧义未报错")
    except SystemExit as e:
        assert "歧义" in str(e), e
    print("selftest OK: 链路解析/重复序列号消歧/前缀歧义 全部通过")


def main():
    args = sys.argv[1:]
    boards_path = os.environ.get("ADBQ_BOARDS", BOARDS_DEFAULT)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return
    if args[0] == "--selftest":
        selftest()
        return
    if args[0] == "boards":
        cmd_boards(boards_path)
        return
    dry = False
    if args[0] == "-n":
        dry = True
        args = args[1:]
    if len(args) < 2:
        print(__doc__.strip())
        sys.exit(2)
    board, adb_args = args[0], args[1:]
    if adb_args and adb_args[0] == "-n":     # 板名后再接 -n 也认(与用法示例一致)
        dry = True
        adb_args = adb_args[1:]
    devices = adb_devices()
    chain_names = load_chain_names(boards_path)
    name, dev = resolve(board, devices, chain_names)
    cmd = [ADB, "-s", dev["serial"], *adb_args]
    if dry:
        print(" ".join(cmd))
        return
    sys.exit(subprocess.run(cmd).returncode)


if __name__ == "__main__":
    main()
