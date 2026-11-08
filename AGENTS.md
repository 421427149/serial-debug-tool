# AGENTS.md — AI 使用须知（动手前读完本文件）

本仓库 = 抗洪水串口调试终端 + 多板 AI 观察控制体系。细节文档在 README.md，本文件只写
AI 的行动契约；与 README 冲突时以本文件为准。

## 工具选型（按目的）

| 你要做什么 | 用什么 |
|---|---|
| 看板子串口输出 / 发命令 | MCP `qserial_*` 工具（入口 `serialdev` → hub:8300）|
| hub MCP 不可用 / 脚本化 | `curl http://127.0.0.1:8300/api/...`（API 面 = README「AI Agent 通道」节）|
| adb 操作板子 | `python3 adbq.py <板名> <adb子命令>`；**禁 adb 广播** |
| 查谁是谁 | `curl http://127.0.0.1:8300/api/tabs`，看 title/board/hint 字段 |
| 受控板电源 / 冷启动 | `python3 rigctl.py <组合名> status\|on\|off\|coldboot\|bind` |

## 铁律

1. **动手前先 list_tabs**：Tab 序号随实例上下线而变，上次会话记的 index 作废。
2. **title 是板名，hint 是证据**：title 来自 boards.json 或账本签名自动派生
   （`name_source` 区分人工/推断）；hint 是账本尾部 `root@主机名`/SoC 签名。
   两者矛盾 = 接线变过，先核对再操作。
3. **禁止背 ttyUSBx 编号**：重插即重编号（实测一天变两次）。寻址只用板名或
   by-path 链路段（如 `1.4.2`）。
4. **`send`/`open`/`close`/`rigctl off|coldboot|bind` 都是真实设备操作**。off/coldboot/bind
   会真断电；除非任务明确要求，不要对在跑的板子主动断电重启。`rigctl on` 幂等安全。
5. **发完命令立即读，禁止 sleep 等待**：读默认 tail 模式，应答就在最新尾窗；没读全
   紧接着补读一次。等异步事件（如重启完成）就先干别的，回头查 `pending_bytes`。
6. **串口静默先查波特率再下结论**：T536 治具控制台 115200，RK 调试口 1500000，配错
   = 全程零字节。静默 ≠ 线断 ≠ 没电。
7. **实例账本疑死先做活性采样**：两次 `/api/info` 采 `capture_bytes`，不涨 = 大概率
   设备重插后旧实例持死 fd，重启实例（用 by-path）而不是对着冻死账本下结论。
8. **串口读写一律走 hub（8300），禁止直连实例端口**：网关对每次 AI 读写做审计
   （`logs/hub_audit.jsonl`，主人用 `hub_audit_view.py` 实时围观）；直连 832x =
   绕开审计，等同私读。

## 登记表（事实源；改文件即生效，hub/实例不用重启）

- `boards.json`：物理插口 → 板名映射。认出新板改一行 `name`；`note` 写证据。
- `rigs.json`：控电组合。**controller = 治具常量，不要动**；target = 受控槽位，
  换板/挪线后跑 `rigctl <组合名> bind`（会真实断电再上电一次）自动重探测回填，
  旧绑定进 `_history`。

## 新板子上线 / 起新实例

```bash
# 每块板一个实例, 在 tmux 里起(人可随时 attach 看日志), hub 自动发现:
tmux new-session -d -s serial-<名> "python3 serial_debug.py \
  -p /dev/serial/by-path/<物理口路径> -b <波特率> --agent-http <8321..8400空闲口> --no-color"
# hub 常驻(已被 ZCode MCP 依赖, 检查: curl -s http://127.0.0.1:8300/api/status):
nohup python3 agent_hub.py >> logs/hub.log 2>&1 &
```

实例约定：8324=TQ536治具(tmux serial-t536)、8325=TQ3588(tmux serial-3588)。
主人围观 AI 串口活动：`tmux attach -t serial-audit`（实时镜像，已常驻）。
改 ZCode 的 MCP 配置需重启会话才生效。

## 自检（改码后必跑对应项再提交）

```bash
python3 test_agent_http.py        # 20 项, 自包含不碰真设备
python3 adbq.py --selftest
python3 rigctl.py --selftest
```

提交信息按天嵌规范：`(等级)[type][uart] 描述`，描述含原因+解决方式。
