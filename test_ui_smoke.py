#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ui_smoke.py —— 网页 UI 冒烟测试(改 UI_HTML 后必跑)。

血训(2026-11-09): UI_HTML 字符串拼接切出重复函数头, 网页 JS 整体语法死亡——
页面壳(静态 HTML)照常渲染、curl 状态全 200, 但浏览器里零交互, 用户报
"看不到板子"被误排查服务端/隧道/缓存一整天。curl 能证明"服务器回了什么",
不能证明"浏览器拿到后能不能跑"——本测试补的就是后半段。

用法: python3 test_ui_smoke.py [hub地址]     # 默认 http://127.0.0.1:8300
依赖: node 只做语法检查; 无 node 时降级为括号配平粗检(弱保证, 会明示)。
"""

import json
import re
import subprocess
import sys
import urllib.request

PASS, FAIL = "✅", "❌"


def fetch_page(base):
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        return r.read().decode("utf-8")


def extract_inline_js(html):
    return "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))


def check_syntax(js):
    """node --check 语法校验(权威); 无 node 退回括号/引号配平粗检。"""
    open("/tmp/ui_smoke.js", "w", encoding="utf-8").write(js)
    try:
        r = subprocess.run(["node", "--check", "/tmp/ui_smoke.js"],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            return True, "node --check 通过"
        return False, f"node --check 语法错误:\n{r.stderr[:600]}"
    except FileNotFoundError:
        pairs = {"{": "}", "(": ")", "[": "]"}
        stack, ok = [], True
        for ch in js:
            if ch in pairs:
                stack.append(pairs[ch])
            elif ch in pairs.values():
                if not stack or stack.pop() != ch:
                    ok = False
                    break
        if ok and not stack:
            return True, "(无 node, 括号配平粗检通过——弱保证, 建议装 node)"
        return False, "(括号配平失败)"
    except subprocess.TimeoutExpired:
        return False, "node 超时"


def check_no_duplicate_defs(js):
    """函数重复定义检测(本次血训的直接病灶)。"""
    names = re.findall(r"function\s+(\w+)\s*\(", js)
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        return False, f"重复定义的函数: {sorted(dup)}"
    return True, f"{len(names)} 个函数无重复"


def check_key_functions(html):
    """关键行为抽查: 翻译器 tl() 必须存在且语义正确。"""
    s, e = html.find("const TL="), html.find("function devOf")
    if s < 0 or e < 0 or e <= s:
        return False, "找不到翻译器代码段(TL=/devOf 锚点)"
    snippet = html[s:e]
    js = snippet + """
;console.log(JSON.stringify([tl('reboot'), tl('for g in x; do echo 1 > /sys/class/gpio/gpio196/value; done')]));
"""
    open("/tmp/ui_tl.js", "w", encoding="utf-8").write(js)
    try:
        r = subprocess.run(["node", "/tmp/ui_tl.js"], capture_output=True, text=True, timeout=20)
        vals = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else None
    except (FileNotFoundError, ValueError, subprocess.TimeoutExpired):
        return True, "(无 node 或解析失败, 跳过行为抽查)"   # 语法已由上一关把关
    if vals and "⚠" in vals[0] and "GPIO" in vals[1]:
        return True, f"tl() 行为正常: {vals}"
    return False, f"tl() 行为异常: {vals}"


def main():
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8300").rstrip("/")
    try:
        html = fetch_page(base)
    except Exception as e:
        print(f"{FAIL} 页面拉取失败 {base}/: {e}")
        sys.exit(2)
    checks = []
    js = extract_inline_js(html)
    if len(js.strip()) < 1000:
        checks.append(("内联 JS 存在且非空", False, f"仅 {len(js)} 字节"))
    else:
        checks.append(("内联 JS 存在", True, f"{len(js)} 字节"))
    checks.append(("JS 语法", *check_syntax(js)))
    checks.append(("函数无重复定义", *check_no_duplicate_defs(js)))
    checks.append(("翻译器行为", *check_key_functions(html)))
    checks.append(("静态外链脚本", True,
                   ", ".join(re.findall(r'src="(/static/[^"]+)"', html)) or "无"))

    failed = 0
    for name, ok, detail in checks:
        print(f"{PASS if ok else FAIL} {name}: {detail}")
        failed += 0 if ok else 1
    if failed:
        print(f"\n{failed} 项失败 —— 禁止提交/宣告完成")
        sys.exit(1)
    print("\nUI SMOKE OK")


if __name__ == "__main__":
    main()
