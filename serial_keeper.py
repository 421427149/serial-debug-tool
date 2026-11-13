#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""serial_keeper.py —— 串口实例守护: 板子插上就有, 拔掉自动收, 线挪了自动跟。

背景: hub 只聚合"已在跑的 serial_debug 实例", 所以过去每接一块新板/每次挪线,
都要人(或 AI)手动起 tmux 实例——本守护补上这最后一环:

    * /dev/serial/by-path 出现新适配器(名字以 -port0 结尾的 CH340 类单口线,
      CH344Q 四合一/H503 命令口不带此后缀, 天然排除) → 自动 tmux 起实例
      (内置 while 自愈循环, agent 口在 8321..8400 里挑空闲);
    * 适配器消失(板子断电/线拔) → 宽限期 GRACE_S 后自动清掉对应 tmux 会话,
      释放 agent 口; 适配器回来(冷启动完上电) → 自动重新起实例;
    * 已被任何 serial_debug 进程占用的口不重复接管(pgrep 按参数匹配,
      不依赖 hub 存活); 8321 兼容门与人工会话(serial-t536 等)天然不冲突——
      它们占着的口 keeper 不会碰。

用法:
    tmux new-session -d -s serial-keeper "python3 serial_keeper.py"   # 常驻
    python3 serial_keeper.py --once        # 单轮检查(调试用)
    python3 serial_keeper.py --status      # 打印当前管理视图

波特率默认 1500000(RK 调试口); 个别口不同(如 T536 治具 115200)写 keeper.json:
    {"1.4.2": {"baud": 115200}, "deny": ["1.1"]}
"""

import argparse
import json
import os
import re
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
KEEPER_JSON = os.path.join(HERE, "keeper.json")
DEFAULT_BAUD = 1500000
PORT_MIN, PORT_MAX = 8321, 8400
IGNORE_AGENT_PORTS = {8321}       # 兼容门(socat)独占, 永不分配
GRACE_S = 120                     # 适配器消失 N 秒后才清实例(给断电放电/重启留时间)
POLL_S = 3.0
PREFIX = "serial-auto-"           # keeper 名下的 tmux 会话前缀(只管理自己生的)


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15)


def tmux_sessions():
    r = sh(["tmux", "list-sessions", "-F", "#{session_name}"])
    return [] if r.returncode else [x for x in r.stdout.splitlines() if x]


def _split_chain(name):
    """by-path 文件名 → (链路段, 该适配器内的接口号或 None)。

    "…usb-0:1.4.1:1.0-port0" → ("1.4.1", None)     单口 CH340(键沿用旧式, 兼容既有配置)
    "…usb-0:1.3.3:1.2"       → ("1.3.3", "1.2")    多口 CH344Q(需带接口号才唯一)
    """
    m = re.search(r"usb-([\d.:]+)", name)
    if not m:
        return None, None
    parts = m.group(1).split(":")            # ["0", "1.4.1", "1.0"]
    if len(parts) < 2:
        return m.group(1), None
    if name.endswith("-port0"):
        return parts[1], None
    return parts[1], (parts[2] if len(parts) > 2 else None)


def bypath_chains(cfg=None):
    """可管理的串口: 默认只收单口适配器(-port0)。

    多口适配器(CH344Q 四合一扩展坞)不满足单口规则会被漏掉, 需在 keeper.json 的
    "manage" 里显式列出(键形如 "1.3.3:1.2"), 才会接管——避免误把 H503 命令口
    等非板卡调试口当控制台打开。
    """
    allow = set((cfg or {}).get("manage") or [])
    out = {}
    try:
        for name in os.listdir("/dev/serial/by-path"):
            chain, iface = _split_chain(name)
            if not chain:
                continue
            if iface is None:
                out[chain] = name                      # 单口: 老规则
            elif f"{chain}:{iface}" in allow:
                out[f"{chain}:{iface}"] = name         # 多口: 仅白名单
    except OSError:
        pass
    return out


def running_targets():
    """当前所有 serial_debug 进程的 -p 参数集合(pgrep 按参数匹配)。"""
    r = sh(["pgrep", "-af", "serial_debug.py"])
    targets = set()
    for line in r.stdout.splitlines():
        m = re.search(r"-p\s+(\S+)", line)
        if m:
            targets.add(m.group(1))
    return targets


def device_in_use(by_path):
    """真实设备节点是否已被别的进程占用(fuser 查 fd 持有者)。

    只比对 -p 参数会漏: 另一个实例可能用易失名(/dev/ttyUSBx)打开同一个设备,
    参数对不上但 fd 确实被占——不查就会两个实例抢一个口(实测踩过)。
    """
    real = os.path.realpath(by_path)
    if not os.path.exists(real):
        return False
    r = sh(["fuser", "-s", real])      # -s 静默; rc=0 表示有进程持有
    return r.returncode == 0


def used_agent_ports():
    ports = set(IGNORE_AGENT_PORTS)
    r = sh(["pgrep", "-af", "agent-http"])
    for line in r.stdout.splitlines():
        m = re.search(r"--agent-http\s+(\d+)", line)
        if m:
            ports.add(int(m.group(1)))
    return ports


def load_cfg():
    try:
        with open(KEEPER_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def sanitize(name):
    return name.replace(".", "_").replace(":", "_")


def spawn(chain, filename, baud):
    used = used_agent_ports()
    free = [p for p in range(PORT_MIN, PORT_MAX + 1) if p not in used]
    if not free:
        return None, f"agent 端口耗尽({PORT_MIN}..{PORT_MAX})"
    agent = free[0]
    session = PREFIX + sanitize(chain)
    # 文件名来自内核命名(无空格/引号), 仍加双引号作防御; 波特率强制整数
    cmd = (f"while true; do python3 serial_debug.py "
           f'-p "/dev/serial/by-path/{filename}" -b {int(baud)} '
           f"--agent-http {agent} --no-color; sleep 2; done")
    r = sh(["tmux", "new-session", "-d", "-s", session, "-c", HERE, cmd])
    if r.returncode:
        return None, r.stderr.strip()
    return agent, None


def kill_session(session):
    sh(["tmux", "kill-session", "-t", session])


def cycle(cfg, quiet=False):
    chains = bypath_chains(cfg)
    targets = running_targets()
    mine = {s[len(PREFIX):]: s for s in tmux_sessions() if s.startswith(PREFIX)}
    now = time.time()
    log = []

    # 1) 现有会话: 端口还在 → 续命; 消失超宽限 → 清理
    for chain, session in sorted(mine.items()):
        if chain in chains:
            continue
        gone_for = now - KEEPER_GONE.setdefault(chain, now)
        if gone_for > GRACE_S:
            kill_session(session)
            KEEPER_GONE.pop(chain, None)
            log.append(f"{chain}: 适配器消失超{GRACE_S}s, 清理会话 {session}")
        # 宽限期内不动作(等冷启动/放电把适配器带回来)

    # 2) 口上有实例在跑(不管谁起的) → 不碰
    # 3) 新适配器且无人管 → 自动起实例
    for chain, filename in sorted(chains.items()):
        KEEPER_GONE.pop(chain, None)
        full = f"/dev/serial/by-path/{filename}"
        if full in targets or f"/dev/{filename}" in targets:
            continue
        if chain in mine:
            continue                      # 自己的会话还在(进程也许在重启间隙)
        if device_in_use(full):
            continue                      # 已被别的实例用易失名占着, 不抢
        if chain in cfg.get("deny", []):
            continue
        baud = (cfg.get(chain) or {}).get("baud", DEFAULT_BAUD)
        agent, err = spawn(chain, filename, baud)
        if agent:
            log.append(f"{chain}: 新适配器 → 自动起实例 @{baud} (agent:{agent}, {session_name(chain)})")
        else:
            log.append(f"{chain}: 起实例失败: {err}")
    return log


def session_name(chain):
    return PREFIX + sanitize(chain)


KEEPER_GONE = {}      # chain -> 首次发现消失的时刻


def status():
    cfg = load_cfg()
    chains = bypath_chains(cfg)
    targets = running_targets()
    mine = [s for s in tmux_sessions() if s.startswith(PREFIX)]
    print(f"by-path 单口适配器: {sorted(chains)}")
    print(f"keeper 名下会话:   {sorted(mine)}")
    for chain, filename in sorted(chains.items()):
        full = f"/dev/serial/by-path/{filename}"
        mark = "✔有实例" if full in targets else "✘无实例(将自动起)"
        print(f"  {chain:<10} {filename[:44]:<46} {mark}")


def main():
    ap = argparse.ArgumentParser(description="串口实例守护: 插上即有, 拔掉即收")
    ap.add_argument("--once", action="store_true", help="跑一轮检查后退出")
    ap.add_argument("--status", action="store_true", help="打印管理视图后退出")
    args = ap.parse_args()
    if args.status:
        status()
        return
    if args.once:
        for line in cycle(load_cfg()):
            print(time.strftime("%H:%M:%S"), line, flush=True)
        return
    print(f"[keeper] 串口实例守护启动 (轮询{POLL_S}s, 宽限{GRACE_S}s, 默认波特率{DEFAULT_BAUD})",
          flush=True)
    while True:
        try:
            for line in cycle(load_cfg()):
                print(time.strftime("%H:%M:%S"), line, flush=True)
        except Exception as e:
            print(time.strftime("%H:%M:%S"), f"[异常] {e}", flush=True)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
