#!/bin/bash
# ZOps 启动脚本：确保目录 → 起网关( hub ) → 起串口实例守护( keeper )
# 用法: bash start_zops.sh [--restart]
#   无参数  只补缺失的服务(已在跑的不动)
#   --restart  先停后起(网关 + keeper)
set -u
cd "$(dirname "$0")" || exit 1
mkdir -p logs

PY=""
for c in python3 python; do command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }; done
[ -z "$PY" ] && { echo "[ERROR] 找不到 python3"; exit 1; }

kill_by_port() {
    local port=$1
    local pid
    pid=$(ss -tlnp 2>/dev/null | grep ":$port " | grep -oP 'pid=\K[0-9]+' | head -1)
    [ -n "$pid" ] && kill "$pid" 2>/dev/null && echo "  已停旧进程 pid=$pid (:$port)"
}

if [ "${1:-}" = "--restart" ]; then
    echo "[ZOps] 重启网关与守护..."
    for p in $(pgrep -f "agent_hub.py" 2>/dev/null); do kill "$p" 2>/dev/null; done
    for p in $(pgrep -f "serial_keeper.py" 2>/dev/null); do kill "$p" 2>/dev/null; done
    sleep 1
fi

# 网关：聚合所有 serial_debug 实例 → 一个 MCP 入口 + 网页终端
if ! ss -tln 2>/dev/null | grep -q ":8300 "; then
    nohup "$PY" agent_hub.py --host 0.0.0.0 --ignore-ports 8321 >> logs/hub.log 2>&1 &
    sleep 1.5
    echo "[ZOps] 网关已启动 → http://127.0.0.1:8300/  (pid $!)"
else
    echo "[ZOps] 网关已在运行 (:8300)"
fi

# 守护：新适配器自动起实例 / 消失自动清
if ! pgrep -f "serial_keeper.py" >/dev/null 2>&1; then
    tmux new-session -d -s serial-keeper -c "$PWD" "$PY serial_keeper.py" 2>/dev/null \
        && echo "[ZOps] 实例守护已启动 (tmux serial-keeper)" \
        || echo "[WARN] tmux 起守护失败, keeper 可不常驻, 手动跑: python3 serial_keeper.py"
else
    echo "[ZOps] 实例守护已在运行"
fi

echo
echo "--- 状态 ---"
curl -s --max-time 5 http://127.0.0.1:8300/api/status 2>/dev/null | "$PY" -c "
import json,sys
try:
    d=json.load(sys.stdin)['data']
    print(f\"网关 {d['name']} v{d['version']} | 在线板子 {d['tab_count']}\")
    for i in d.get('instances', []):
        print(f\"   {i.get('agent_port')} -> {i.get('port')} open={i.get('open')}\")
except Exception:
    print('(网关无响应)')" 2>/dev/null || echo "(网关无响应)"
