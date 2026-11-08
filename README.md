# 串口调试工具（serial_debug.py）

跨平台（Windows / Linux）控制台串口调试终端，专为**高波特率 + 大数据量刷屏**场景设计（如 1500000 波特率的
嵌入式 Linux 控制台、Bootloader 刷机日志等），刷屏洪水时不卡顿、不丢键、不崩溃。两个平台的按键行为、
命令、日志机制完全一致。

## 安装

```bash
pip install pyserial        # Windows; Linux 用 pip3 install pyserial
```

## 启动

**双击 `串口调试.bat` 即可**（自动定位 Python，缺 pyserial 时自动安装；程序退出后窗口
会保留，方便查看日志保存路径，按任意键关闭）。

命令行方式（等价）：

```bash
python serial_debug.py                      # 交互式选择串口和波特率
python serial_debug.py -p COM5              # 指定串口, 默认 115200
python serial_debug.py -p COM5 -b 1500000   # 1.5M 波特率
python serial_debug.py --list               # 仅列出可用串口
```

### Linux / macOS

```bash
bash 串口调试.sh                            # 等价 Windows 的 bat: 自动装 pyserial、退出后暂停
python3 serial_debug.py -p /dev/ttyUSB0 -b 1500000
python3 serial_debug.py --list              # 列出 /dev/ttyUSB* /dev/ttyACM* /dev/ttyS*
```

> 提示 Permission denied 时，把用户加入 dialout 组后**重新登录**即可：
> `sudo usermod -aG dialout $USER`（或临时 `sudo chmod 666 /dev/ttyUSB0`）。
> 1.5M/3M 波特率取决于串口驱动（CH340/FT232/CP210x 均支持）。

启动器也支持透传参数：`串口调试.bat -p COM19 -b 1500000`，可为其创建桌面快捷方式并在
"目标"末尾加上固定参数，实现"一键连指定串口"。

> 若通过窗口右上角 ✕ 直接关闭，退出提示不会显示；会话捕获文件会以
> `logs/serial_capture_*.part` 保留，把扩展名改成 `.log` 即可使用（最多丢最后 50ms 数据）。

其他参数：`--hex`（HEX 显示）、`--ts`（时间戳）、`--nl crlf`（行尾符）、
`--log FILE`（启动即落盘）、`--no-capture`（关闭会话捕获）、`--no-color`、
`--budget N`（每 50ms 最多打印字节数）。

## 保存日志

所有自动生成的日志统一存放在**脚本目录下的 `logs/` 文件夹**（首次写日志时自动创建；
目录不可写时自动退回当前目录）。规则：`/save`、`/log` 给**裸文件名**会自动放进 `logs/`；
**明确写了路径**的（如 `D:\data\a.log`）按指定位置存放。

- **`/save [文件名]`**：把**本次会话（打开串口以来）收到的全部原始数据**导出为正式日志，
  包括已被屏幕省略的部分；不带文件名则自动命名为 `serial_日期_时间.log`。
- 即使从头到尾没用过 `/save`，退出程序时捕获文件也会自动转正为
  `logs/serial_capture_日期_时间.log` 并提示路径，数据不会静默丢失。
- `/log on FILE` 与 `/save` 互不干扰：前者是"从此刻开始记录"，后者是"回溯保存整个会话"。
- 捕获数据只含**收到的**数据（与 /log 一致）；不需要磁盘自动记录时用 `--no-capture` 关闭。

## Tab 补全（类似 MobaXterm 连 Linux 的体验）

MobaXterm 里的 Tab 补全其实是**设备端 shell 在做**（收到 `\t` 后自己补全并回显）。
串口工具只要把按键原样透传，设备就会给你一模一样的体验：

- **交互透传模式**（`/raw` 或 Ctrl+Q 切换，`--raw` 启动即进入）：按键直达设备，
  Tab 补全命令/文件名、↑↓ 翻历史、退格、Ctrl+C 中断，全部和真实终端一致。
- 行模式下输入非 `/` 内容直接按 **Tab**：自动带上已输入字符进入交互模式并触发设备补全
  （如输入 `ifcon` 后按 Tab → 设备补全成 `ifconfig`）。
- 行模式下输入 `/` 开头内容按 **Tab**：本地补全工具命令与常用参数
  （如 `/ba` → `/baud`、`/baud 15` → `/baud 1500000`，多候选列出提示）。

两种模式可随时用 **Ctrl+Q** 切换。**Ctrl+C 在两种模式下都会把 `^C` 发给设备**用于中断
远端程序，并在本地显示暗灰色 `^C` 标记确认按键已发出。区别只在本地行为：

- **行模式**：2 秒内连按两次 Ctrl+C 退出程序。
- **交互模式**：永不因 Ctrl+C 退出（防误触），退出走 Ctrl+Q → `/q`。

> **按键通路修复**：Windows 控制台默认（`ENABLE_PROCESSED_INPUT`）会把 Ctrl+C 拦截成本地
> SIGINT——按键根本到不了程序，`^C` 自然也发不到设备，表现为"Ctrl+C 无效"。工具在进入
> 终端主循环时清除该标志，让 `^C` 以普通按键 `\x03` 到达；stdin 不是控制台时（如在
> Git Bash 里启动）自动改用 `CONIN$` 打开真实控制台，退出程序时恢复原有控制台模式。
> Linux 终端同理：进入主循环时用 termios 关掉 `ISIG`（连同行编辑/回显），Ctrl+C 同样
> 以 `\x03` 到达，行为与 Windows 完全一致；stdin 被管道/重定向时自动改开 `/dev/tty`。
> （启动时的交互选口阶段不动终端模式，该阶段的 `input()` 依赖行编辑；此阶段按 Ctrl+C
> 可直接退出程序，属正常行为。）

> 注意：`udhcpc: broadcasting discover` 这类刷屏来自**后台守护进程**，Ctrl+C 的 SIGINT
> 只发给前台进程组，守护进程收不到——MobaXterm/PuTTY 里同样如此。停掉它请在刷屏中
> 直接输入 `killall udhcpc` 回车（工具输入不受刷屏影响）。

## 抗洪水设计

| 措施 | 说明 |
|---|---|
| 收发线程分离 | 接收线程只做「串口 → 内存缓冲 + 日志」，主线程专职键盘与显示，互不阻塞 |
| 显示节流 | 主循环 20 帧/秒，每帧最多打印 `budget` 字节（默认 64KB ≈ 1.2MB/s 上限） |
| 自动省略 | 屏显积压超过 512KB 时按整行省略并计数提示，保证行完整性 |
| 数据不丢 | 被省略的数据只是不上屏；`/log` 落盘和会话捕获都是原始字节流，一字节不少 |
| 会话捕获 | 收到第一包数据起，全部接收数据自动落盘到 `serial_capture_*.part`，随时可完整导出 |
| 异常不崩 | 串口拔出/占用/超时只提示，`/open` 即可重连；Ctrl+C 连按两次才退出 |
| 大驱动缓冲 | 打开串口时申请 2MB 接收缓冲，给接收线程争取时间 |

压测（`python flood_test.py`，loop:// 虚拟串口 ~140KB/s ≈ 1.5M 波特率线速）：
灌入 802,560 字节 → 接收 802,560（无损）、屏显 335,018 + 智能省略 467,542（账目守恒）、
日志 802,560（完整）。

## 常用命令

进入程序后输入 `/help` 查看。输入以 `/` 开头才当作命令，其余内容按当前编码原样发送。

```
/baud 1500000        切波特率          /hex           HEX 显示接收
/port                列出串口          /hexsend       HEX 发送（如 01 A0 FF）
/open /close         开关串口          /ts            行首时间戳
/log on              原始数据落盘      /pause /resume 暂停/恢复屏显（后台仍接收）
/save [名字]         导出本次会话全部数据   /nl crlf   发送行尾符
/send 文件            发送文件          /enc gbk       收发编码
/dtr 0  /rts 1       控制电平          /enc gbk       收发编码
/info                查看统计          /q             退出
```

## 快捷键

- `Ctrl+C`：转发 `^C` 中断设备端程序；行模式下同时清空输入，输入为空时 2 秒内连按两次退出
- `Esc` / `Ctrl+U`：清行
- `↑` / `↓`：发送历史
- `←` / `→` / `Delete`：行内编辑

## AI Agent 通道（人机双观察）

`--agent-http` 开启本地 HTTP 服务后，AI 与人**看同一份串口流但互不依赖**：你照常用终端，
AI 通过接口读同一份数据；你不在场时 AI 也能独立完成"开串口→发指令→读响应"的完整闭环。

```bash
python serial_debug.py -p /dev/ttyUSB0 -b 1500000 --agent-http            # 端口默认 8321
python serial_debug.py --agent-http 9000 --agent-token 我的令牌           # 加令牌
```

AI 侧的信息保真按"**真实 + 全面**"设计，每条可验证：

| 保证 | 机制 |
|---|---|
| 不丢（全面） | AI 读的是**会话捕获文件**（磁盘账本）而非内存缓冲：读过的不过期、可任意重读，1.5M 洪水刷屏下 AI 侧同样零丢失 |
| 可自查 | `GET /api/info` 暴露 `rx_total`（串口实收）与 `capture_bytes`（已落盘），恒等即无静默丢失；不等说明驱动/磁盘层出过问题，必现形 |
| 不失真（真实） | 原始字节 HEX 是真身，text 只是按当前编码的派生视图并附 `decode_errors` 计数 |
| 可还原现场 | 事件流带递增 seq：每条系统消息（开关串口/切波特率/异常断开）与每次发送（来源 human/agent、时间戳、载荷 b64）都入流 |
| 可复查 | `?offset=N` 绝对位置随机读，不动游标；不带 offset 按 `read_cursor` 续读（游标值在 /api/info 可见，AI 重启后可续接） |
| 看最新 | `?tail=1` 跳过积压直接取最新 `max_bytes` 尾窗（起点对齐行首），跳过字节计入 `skipped` 且仍在账本可 `offset` 回看——洪水刷屏下应答不会被"最旧优先 + 展示截断"埋掉 |

接口（与 QSerial 的 AI Agent 服务同构，单会话固定 Tab 0）：

```
GET  /api/status                          GET  /api/info          ← 对账入口
GET  /api/ports                           GET  /api/events?after_seq=N
GET  /api/tabs                            GET  /api/tabs/0/receive?offset=&max_bytes=&tail=
POST /api/tabs/0/open   {"port":"","baudrate":0}   (参数可选)
POST /api/tabs/0/close  {}                POST /api/tabs/0/send   {"text":"AT"} 或 {"hex":"01 A2"}
```

MCP（Model Context Protocol）接入：本目录的 `mcp_bridge.py` 原样取自
[tqtools/QSerial](git@192.168.1.7:tqtools/QSerial.git)（MIT），只依赖标准库，把
`qserial_status/list_ports/list_tabs/open_port/close_port/send/read` 八个工具映射到上述
HTTP API；`qserial_read` 在桥侧默认 `tail=1`（AI 要的是应答不是积压），可用
`tail=false` 退回 FIFO、带 `offset` 回看历史。ZCode 等客户端配置：

```json
{ "mcpServers": { "serialdev": {
    "command": "python3",
    "args": ["/path/to/串口工具/mcp_bridge.py", "--port", "8321", "--token", "我的令牌"] } } }
```

### 多板聚合（agent_hub.py）——一个 MCP 入口看所有板子

一个 serial_debug 实例只管一个串口（固定 Tab 0），多块板子如果一实例配一个 MCP 入口，
板子多了入口会爆炸。`agent_hub.py` 把 127.0.0.1 扫描区间（默认 8321..8400）内所有
serial_debug 实例自动发现、聚合成 QSerial 风格的**多 Tab API**，`mcp_bridge.py` 零改动：

```bash
nohup python3 agent_hub.py >> logs/hub.log 2>&1 &      # 网关监听 8300(须在扫描区间外)
# 每块板子各起一个实例(终端/tmux 里, 人要看日志), 出现后自动进 Tab 列表:
tmux new-session -d -s serial-usb1 "python3 serial_debug.py -p /dev/ttyUSB1 -b 1500000 --agent-http 8322 --no-color"
# ZCode/Claude 只配一个入口, 指向网关:
{ "args": [".../mcp_bridge.py", "--port", "8300", "--timeout", "60"] }
```

**防认错板子三板斧**（v1.1）：

1. **稳定寻址**：实例用 `/dev/serial/by-path/...`（物理插口）启动而非 `ttyUSBx`——重插、
   重枚举换号不受影响（劣质适配器序列号是假的/缺失，by-id 不可靠）；
2. **板名映射 + 自动命名**：Tab 名优先取 `boards.json` 人工登记（`{"name": "TQ3588-A",
   "note": "..."}`，按串口路径全串/basename 匹配，保存即生效不用重启网关）；没登记的板
   由取证签名**自动派生**板名（主机名 > SoC > dtb 名，如 `root@TQ3588` → 板名 `TQ3588`）；
   两块同型号撞名时全部加 `@物理插口` 后缀（如 `TQ3588@1.2` / `TQ3588@1.4.4`），标题
   恒唯一；静默板不猜名，`hint` 为空即「未知且静默」；
3. **被动取证**：hub 读各实例账本**尾部 64KB**（offset 绝对读，不发字节、不动游标），
   提取出现最多的 `root@主机名` / `RK35xx` / `xxx.dtb` / `U-Boot` 签名附在 Tab 的
   `hint` 字段——没登记的板子 AI 也能自证身份，静默口 hint 为空（AI 至少知道它静默）。

语义与边界：Tab 序号 = 实例按 agent 端口升序的位置，**每次请求实时解析**（实例退出后
序号前移，AI 用前先 `list_tabs` 对齐，每项带 `agent_port` 可精确定位）；网关无状态不缓存，
实例上下线立即反映；`POST /api/tabs` 回 409 附启动命令提示（hub 不代开进程——实例必须
活在终端里供人观察/手工键入）；子实例响应包络字节级透传（含 409/400 业务错误）；实例的
`X-Agent-Token` 令牌经网关原样转发；监听端口落在扫描区间内会拒绝启动（防自己发现自己）。

### 控制组合一条命令（rigctl.py）——治具控板自动化

「通过一块控制板（T536 治具 + 4 路继电器）控制开发板电源」的整条链——鉴别串口、进治具
控制台、发 GPIO 控电、回受控板验证——AI 每次现推导要十几个回合还会认错对象。
`rigs.json` 把组合登记成一等公民：**controller=治具常量**（控制台/波特率/GPIO/放电时长，
永不因换板而变），**target=受控槽位**（换板/挪线是常态，重探测即更新，旧绑定进 `_history`）。

```bash
rigctl status              # 所有组合一行一结论: 控制台✔/受控串口✔/USB 判活✔
rigctl rig-A on|off        # 控电(4 路 GPIO sysfs 同值, 幂等; off 真断电)
rigctl rig-A coldboot      # 关机→放电→开机(冷启动), 全程约 discharge_s+5s
rigctl rig-A bind          # 受控槽位重探测: 关电盯谁灭→开电盯谁醒→写回 rigs.json
                           # (会真实断电再上电受控板, 换板/挪线后跑一次)
rigctl --selftest          # 链路归一化/节点解析自检
```

要点：身份坐标系与 boards.json/adbq 同源（by-path 链路段），控制走 serial_debug 实例的
agent HTTP（不抢串口）；实例用 by-path 还是 ttyUSBx 启动都能对上（rigctl 会把链路解析成
当前设备节点再比对）；两套组合并存时各挂各的 hub 分支，物理上串不了；`on` 幂等安全，
`off/coldboot/bind` 会真操作电源，跑前想清楚。

### AI 活动审计与围观（hub_audit_view.py）

hub 对每个经网关的 AI 读写落一行审计（`logs/hub_audit.jsonl`：时间/板名/动作/字节数/
发送内容/接收尾部预览）——这是**强制卡口**：AI 只要走 hub（唯一推荐路径），它读到的
串口内容必然先经过这里。人实时围观：

```bash
tmux new-session -d -s serial-audit "python3 hub_audit_view.py -n 20"   # 常驻镜像窗口
tmux attach -t serial-audit        # 随时进去看 AI 正在读什么发什么, Ctrl+B D 退出
python3 hub_audit_view.py -n 100   # 或一次性回放最近 100 条
```

注意：AI 直连实例端口(832x)可绕开审计——AGENTS.md 已将其列为违规；确需封死可加防火墙规则。

### adb 按板名操作（adbq.py）

RK 板的 USB adb 序列号大量重复甚至为空，多板同插时 `adb -s 序列号` 会打到错的板上。
`adbq` 复用上面第 ① 层的身份体系：`adb devices -l` 自带 `usb:1-x.y.z` 物理链路，与
boards.json 里 by-path 键的 hub 链路段是同一个东西——**板名映射一份通吃串口和 adb**。

```bash
adbq boards                    # 列在线 adb 设备 + 板名解析(链路对不上的标 ?)
adbq TQ3588 shell ls /         # 按板名执行 adb 子命令, 自动解析成 adb -s <对的那台>
adbq TQ3588 -n shell ls /      # -n 只打印解析后的命令不执行
adbq --selftest                # 内嵌自检(链路解析/重复序列号消歧/前缀歧义)
```

边界：只有 boards.json 的 **by-path 键**参与 adb 解析（ttyUSBx 键不含链路信息）；
板名支持唯一前缀；**不提供不带板名的广播**（多板时 `adb install` 这类广播正是事故源）；
Debian 源的旧 adb(29.x) 碰新 adbd 可能 unauthorized，那是 adb 版本问题，换 platform-tools。

安全与边界：默认只绑 127.0.0.1，`send/open/close` 会**真实操作设备**，跨机暴露请设令牌并
自担风险；`--no-capture` 与 `--agent-http` 同用时捕获被强制开启（AI 账本靠它）。驱动层
FIFO 溢出发生在任何用户态工具之前，工具无法补回，但对账数字会让它现形——承诺形式是
"**不静默丢失**"：每个字节要么交到你和 AI 手里，要么明确报出丢了多少。

测试：`python test_agent_http.py`（对账/复读/双观察来源/AI 独立闭环/令牌/桥接 20 项）。

## 测试

```bash
python flood_test.py   # 洪水压测：无损、账目守恒、日志完整、省略路径生效
python smoke_test.py   # 命令与输入编辑冒烟测试
```
