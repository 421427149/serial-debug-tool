#!/bin/bash
# 串口调试工具 Linux/macOS 启动器(等价于 Windows 的 串口调试.bat):
# 自动定位 python3, 缺 pyserial 时自动安装, 参数原样透传。
# 用法: bash 串口调试.sh [-p /dev/ttyUSB0 -b 1500000 ...]
#       从文件管理器双击打开时退出后窗口保留; 在终端里不想停顿可:
#       SERIAL_DEBUG_NOPAUSE=1 bash 串口调试.sh ...
cd "$(dirname "$0")" || exit 1

PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
        PY="$c"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "[ERROR] 未找到 python3, 请先安装 (如: sudo apt install python3 python3-pip)"
    [ -t 0 ] && read -r -p "按回车关闭..."
    exit 1
fi

"$PY" -c "import serial" >/dev/null 2>&1 || {
    echo "pyserial 未安装, 正在自动安装..."
    "$PY" -m pip install --user pyserial || "$PY" -m pip install pyserial || {
        echo "[ERROR] 自动安装失败, 请手动执行: $PY -m pip install pyserial"
        [ -t 0 ] && read -r -p "按回车关闭..."
        exit 1
    }
}

"$PY" serial_debug.py "$@"
code=$?
[ "$code" -ne 0 ] && echo "[ERROR] 串口工具退出码: $code"

# 双击/从桌面打开时(终端随脚本退出而关闭)停一下, 方便查看日志保存路径
if [ -t 0 ] && [ -t 1 ] && [ "${SERIAL_DEBUG_NOPAUSE:-0}" != "1" ]; then
    read -r -p "按回车关闭..."
fi
exit $code
