#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hub_audit_view.py —— 人围观 AI 串口活动的实时镜像(读 agent_hub 审计账)。

AI 经 hub 网关的每一次串口读写都会被审计进 logs/hub_audit.jsonl; 本工具把它
变成人能盯的一行一条的实时流——AI 读到了什么、发了什么, 你同步看到:

    python3 hub_audit_view.py            # 跟踪最新(默认, 相当于 tail -f)
    python3 hub_audit_view.py -n 100     # 先回放最近 100 条再进入跟踪
    python3 hub_audit_view.py --no-follow  # 只打印现有内容即退出

常驻围观(推荐, 开个 tmux 窗口):
    tmux new-session -d -s serial-audit "python3 hub_audit_view.py -n 20"
    tmux attach -t serial-audit          # 随时进去看, Ctrl+B D 退出
"""

import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT_DEFAULT = os.path.join(HERE, "logs", "hub_audit.jsonl")
POLL_S = 0.5
# 串口流里的 ANSI 转义码(方向键/颜色/bracketed-paste)若原样打印, 会被人的终端当成
# 光标控制执行, 画面直接乱掉——显示层必须清洗; 审计 JSONL 里保留原始证据不清洗。
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-_]")


def clean(s):
    s = ANSI_RE.sub("", s or "")
    s = s.replace("\r", "").replace("\n", " ⏎ ")
    return "".join(ch if ch.isprintable() else "·" for ch in s).strip()


def fmt(rec):
    """一条审计记录 → 人读一行(已清洗转义码)。"""
    head = f"{rec.get('t', '?'):<8} [{rec.get('board', '?')}@{rec.get('dev', '?')}]"
    act = rec.get("action")
    if act == "receive":
        body = clean(rec.get("preview"))[:160]
        return f"{head} AI读 {rec.get('bytes')}B | {body}"
    if act == "send":
        body = clean(rec.get("out"))[:120]
        return f"{head} AI发 ➤ {body}"
    return f"{head} {act} (status={rec.get('status')})"


def load_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def main():
    ap = argparse.ArgumentParser(description="AI 串口活动实时镜像")
    ap.add_argument("--file", default=AUDIT_DEFAULT, help=f"审计文件, 默认 {AUDIT_DEFAULT}")
    ap.add_argument("-n", type=int, default=20, help="启动时先回放最近 N 条, 默认 20")
    ap.add_argument("--no-follow", action="store_true", help="打印现有内容后退出, 不跟踪")
    ap.add_argument("--min-bytes", type=int, default=16,
                    help="AI读 少于该字节数的碎读默认不显示(洪水期刷屏), 0=全显示")
    args = ap.parse_args()

    if not os.path.isfile(args.file):
        print(f"(审计文件还没有: {args.file} —— AI 一经 hub 读写即生成)")
        if args.no_follow:
            return
    printed = 0     # 审计只追加, 行号即游标
    try:
        while True:
            records = load_lines(args.file)
            if printed == 0:
                printed = max(0, len(records) - max(0, args.n))
            for rec in records[printed:]:
                if (rec.get("action") == "receive"
                        and (rec.get("bytes") or 0) < args.min_bytes):
                    continue     # 碎读不显示但游标照常推进
                print(fmt(rec), flush=True)
            printed = len(records)
            if args.no_follow:
                return
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
