# AGENTS.md — ZOps 使用须知（AI 动手前读完本文件）

**ZOps（Z 操作台）= 给 AI 用的多板调试操作台**：串口观察、烧录/进 Loader、上下电。
本体是抗洪水串口终端 `serial_debug.py`；`agent_hub.py`（网关，8300）把它聚合成多板
统一入口；`rigctl.py` 管电源/救砖，`serial_keeper.py` 管实例生命周期。

细节文档在 README.md，本文件只写 AI 的行动契约；与 README 冲突时以本文件为准。

## 工具选型（按目的）

| 你要做什么 | 用什么 |
|---|---|
| 看板子串口输出 / 发命令 | MCP `zops_*` 工具（入口 `serialdev` → hub:8300）。旧名 `qserial_*` 仍作别名照收 |
| ZOps 网关不可用 / 脚本化 | `curl http://127.0.0.1:8300/api/...`（API 面 = README「AI Agent 通道」节）|
| adb 操作板子 | `python3 adbq.py <板名> <adb子命令>`；**禁 adb 广播** |
| 查谁是谁 | `curl http://127.0.0.1:8300/api/tabs`，看 title/board/hint 字段 |
| 受控板电源/冷启动/进Loader | **一键接口** `POST /api/rigs/<组合名>/<on\|off\|coldboot\|enter-loader>`（人网页按钮走的就是它；动作异步执行，结果落在 `GET /api/rigs` 的 `last` 字段）；或命令行 `python3 rigctl.py <组合名> <同款动作>` |
| **内核已 panic / 卡死要救砖** | `python3 rigctl.py <组合名> catch-uboot`（冷启动+连发 Ctrl+C 抢 U-Boot 3 秒倒计时+菜单选 `[3] USB update images` 进 Loader）。**内核死了 in-system 的 `enter-loader` 必然无效**，只有 U-Boot 这条路；`--menu` 只停在菜单不选 |
| 看组合(控制台/受控/USB)状态 | `GET /api/rigs`（控制器✔/受控✔/USB 模式一屏全览，AI 先看这个再动手）|

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

## 新板子上线 = 插线即可（serial_keeper 守护全自动）

**串口插上就有、拔掉自动收、线挪了自动跟**——`serial_keeper.py`（tmux serial-keeper 常驻）
盯 /dev/serial/by-path：新单口适配器（-port0 结尾）几秒内自动起实例（默认 1500000，
特例写 keeper.json，如 {"1.4.2":{"baud":115200}}）；消失超 120s 自动清理释放 agent 口，
回来自动重起。hub 自动发现 → 网页自动显示（hint 自动命名）。**AI 不要再手动起实例**，
仅两种情况动手：板子输出全静默想换波特率（改 keeper.json 后 kill 对应 serial-auto-* 会话
即自动重起）、要给口起正式板名（改 boards.json）。

人工/特殊实例照旧不受管：serial-t536(8324)、serial-3588(8325)、serial-compat(8321 兼容门)、
serial-audit(审计镜像)。hub 常驻检查: `curl -s http://127.0.0.1:8300/api/status`。
改 ZCode 的 MCP 配置需重启会话才生效。

## 自检（改码后必跑对应项再提交）

```bash
python3 test_agent_http.py        # 20 项, 自包含不碰真设备
python3 adbq.py --selftest
python3 rigctl.py --selftest
python3 test_ui_smoke.py          # 改 UI_HTML 后必跑: JS语法/重复定义/翻译器行为
python3 serial_keeper.py --status # keeper 管理视图(实例缺失会自动补, 无需手动起)
```

提交信息按天嵌规范：`(等级)[type][uart] 描述`，描述含原因+解决方式。

## 许可（别搞错）

本项目是**专有软件（Proprietary）· 保留所有权利**，不是开源、也不是"源码可见许可"。
不要把它描述为开源，不要建议把它分发到公开仓库、公开分享给第三方、或并入对外产品。
对外协作（如发给客户/供应商）前必须由著作权人书面授权。
第三方组件（`webui/xterm.js` 等）仍为 MIT，引用它们时保留其版权声明。
`a673302` 及更早的提交历史为 MIT，不追溯。
