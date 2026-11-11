# ZOps · Z 操作台

**给 AI 用的多板调试操作台**——串口观察、烧录/进 Loader、上下电，一块屏管多块板。

| 能力 | 做什么 | 主要文件 |
|---|---|---|
| **看** | 抗洪水串口终端；经网关聚合多块板 → 浏览器真终端 + AI 共用同一份流 | `serial_debug.py`、`agent_hub.py` |
| **控** | 治具控电（继电器 GPIO / H503 电源板）、冷启动、进 Loader、内核已死时抓 U-Boot 救砖 | `rigctl.py` |
| **认** | 物理插口/板名登记 + 账本签名自动取证 + 按板名操作 adb——杜绝"认错板子" | `boards.json`、`rigs.json`、`adbq.py` |
| **自动** | 串口实例守护：插上即有、拔掉即收；网关自动发现实例并给 AI 一个入口（MCP `zops_*`） | `serial_keeper.py` |

平台：Windows / Linux。终端本体专为**高波特率 + 大数据量刷屏**设计（1500000 波特率下不卡顿、
不丢键、不崩溃，两平台按键行为与日志机制完全一致）。

> **AI 要动手，先读 [`AGENTS.md`](AGENTS.md)**——那是行动契约（工具选型、铁律、登记表、自检清单）。
> 本 README 是给人看的使用手册。

---

## 目录

- [快速开始](#快速开始)
- [组件一览](#组件一览)
- [一、串口终端本体（serial_debug.py）](#一串口终端本体serial_debugpy)
- [二、多板聚合与网页终端（agent_hub.py）](#二多板聚合与网页终端agent_hubpy)
- [三、串口实例守护（serial_keeper.py）](#三串口实例守护serial_keeperpy)
- [四、控电与救砖（rigctl.py）](#四控电与救砖rigctlpy)
- [五、按板名操作 adb（adbq.py）](#五按板名操作adbqpy)
- [六、AI 接入](#六ai-接入)
- [故障排查](#故障排查)
- [测试](#测试)
- [安全与边界](#安全与边界)

---

## 快速开始

```bash
pip install pyserial                        # Windows; Linux 用 pip3 install pyserial

bash start_zops.sh                          # 起网关(8300) + 串口实例守护, 自动建 logs/
# 浏览器打开 http://<主机IP>:8300/           ← 网页终端 + rig 状态条 + 电源按钮
```

只想要单块板的传统串口终端（不起网关）：

```bash
bash 串口调试.sh                            # Linux/macOS, 等价 Windows 双击 串口调试.bat
python serial_debug.py -p /dev/ttyUSB0 -b 1500000 --agent-http 8321
```

---

## 组件一览

| 文件 | 职责 | 何时用到 |
|---|---|---|
| `serial_debug.py` | 抗洪水串口终端（本体，单实例管一个串口） | 人盯日志、手工键命令 |
| `agent_hub.py` | 网关：聚合多实例为多 Tab API + 网页终端 + AI 审计（默认 8300） | 多板同时看；AI 统一入口 |
| `serial_keeper.py` | 实例守护：盯 `/dev/serial/by-path`，插上自动起实例、拔掉自动收 | 常驻，无需人工干预 |
| `rigctl.py` | 控电/冷启动/进 Loader/救砖；组合登记在 `rigs.json` | 上下电、烧录前 |
| `adbq.py` | 按板名操作 adb（绕开 RK 重复序列号） | adb 调试多板 |
| `mcp_bridge.py` | MCP stdio 桥（工具 `zops_*`，旧名 `qserial_*` 作别名） | AI 接串口 |
| `hub_audit_view.py` | AI 读写活动的实时镜像（人围观 AI） | 想盯着 AI 在干什么 |
| `agent_http.py` | 给 serial_debug 挂 HTTP 通道（由 `--agent-http` 加载） | 内部模块 |
| `boards.json` / `rigs.json` | 板名映射 / 控电组合登记（改文件即生效） | 认板、换板 |

---

## 一、串口终端本体（serial_debug.py）

### 启动

**Windows 双击 `串口调试.bat` 即可**（自动定位 Python，缺 pyserial 时自动安装；程序退出后窗口
会保留，方便查看日志保存路径，按任意键关闭）。

Linux/macOS 用 `bash 串口调试.sh`（等价：自动装 pyserial、退出后暂停）。

```bash
python serial_debug.py                      # 交互式选择串口和波特率
python serial_debug.py -p COM5              # 指定串口, 默认 115200
python serial_debug.py -p COM5 -b 1500000   # 1.5M 波特率
python serial_debug.py --list               # 仅列出可用串口
```

启动器支持透传参数：`串口调试.bat -p COM19 -b 1500000`，可给快捷方式固定参数实现"一键连指定串口"。

其他参数：`--hex`（HEX 显示）、`--hexsend`、`--ts`（时间戳）、`--nl crlf`（行尾符）、
`--enc gbk`、`--flow rtscts`、`--log FILE`（启动即落盘）、`--no-capture`（关闭会话捕获）、
`--no-color`、`--budget N`（每 50ms 最多打印字节数）、`--no-auto-reopen`（断开后不自动重连）。

> 提示 Permission denied：`sudo usermod -aG dialout $USER` 后**重新登录**（或临时
> `sudo chmod 666 /dev/ttyUSB0`）。1.5M/3M 波特率取决于驱动（CH340/FT232/CP210x 均支持）。

### 常用命令

进入程序后 `/help` 查看。输入以 `/` 开头才当命令，其余按当前编码原样发送。

```
/baud 1500000        切波特率          /hex           HEX 显示接收
/port                列出串口          /hexsend       HEX 发送（如 01 A0 FF）
/open /close         开关串口          /ts            行首时间戳
/log on              原始数据落盘      /pause /resume 暂停/恢复屏显（后台仍接收）
/save [名字]         导出本次会话全部数据   /nl crlf   发送行尾符
/send 文件            发送文件          /enc gbk       收发编码
/dtr 0  /rts 1       控制电平          /info          查看统计
/q                   退出
```

### 快捷键

- `Ctrl+C`：转发 `^C` 中断设备端程序；行模式下同时清空输入，输入为空时 2 秒内连按两次退出
- `Esc` / `Ctrl+U`：清行　　`↑`/`↓`：发送历史　　`←`/`→`/`Delete`：行内编辑
- `Ctrl+Q`：行模式 ↔ 交互透传模式切换

### Tab 补全（类 MobaXterm 体验）

MobaXterm 的 Tab 补全其实是**设备端 shell 在做**（收到 `\t` 自己补全回显），工具只要原样透传：

- **交互透传模式**（`/raw` 或 Ctrl+Q，`--raw` 启动即进入）：按键直达设备，Tab 补全、↑↓ 历史、
  退格、Ctrl+C 全部与真实终端一致；
- 行模式下输入非 `/` 内容按 **Tab**：自动进入交互模式并触发设备补全（`ifcon` → `ifconfig`）；
- 行模式下 `/` 开头按 **Tab**：本地补全工具命令与参数（`/ba` → `/baud`）。

> **Ctrl+C 通路修复**：Windows 控制台默认把 Ctrl+C 拦成本地 SIGINT（按键到不了程序）；
> 工具进入主循环时清除 `ENABLE_PROCESSED_INPUT`，stdin 非控制台时改用 `CONIN$`。Linux 同理
> 关掉 `ISIG`，stdin 被重定向时改开 `/dev/tty`。两平台行为完全一致。
>
> `udhcpc: broadcasting discover` 这类刷屏来自后台守护进程——Ctrl+C 只发前台进程组，守护进程
> 收不到（PuTTY/MobaXterm 同样如此）。停它请直接输 `killall udhcpc` 回车。

### 保存日志

日志统一放脚本目录 `logs/`（首次写时自动创建，不可写则退回当前目录）。`/save`、`/log` 给裸文件名
会自动进 `logs/`，写了明确路径则按指定位置。

- **`/save [文件名]`**：把**本次会话收到的全部原始数据**导出（含屏幕省略部分），不带名字自动命名
  `serial_日期_时间.log`；
- 从没用过 `/save` 也不会丢：退出时捕获文件自动转正为 `logs/serial_capture_*.log` 并提示路径；
- `/log on FILE`（从此刻开始记录）与 `/save`（回溯整个会话）互不干扰；只含**收到**的数据；
- 右上角 ✕ 强关时，`.part` 文件保留，改扩展名为 `.log` 即可用（最多丢最后 50ms）。

### 抗洪水设计

| 措施 | 说明 |
|---|---|
| 收发线程分离 | 接收线程只做「串口 → 内存缓冲 + 日志」，主线程专职键盘与显示 |
| 显示节流 | 主循环 20 帧/秒，每帧最多打印 `budget` 字节（默认 64KB ≈ 1.2MB/s 上限） |
| 自动省略 | 屏显积压超 512KB 时按整行省略并计数提示，保证行完整性 |
| 数据不丢 | 被省略的只是不上屏；`/log` 落盘与会话捕获都是原始字节流，一字节不少 |
| 会话捕获 | 收到第一包起全部自动落盘 `serial_capture_*.part`，随时可完整导出 |
| 异常不崩 | 串口拔出/占用/超时只提示并**自动重连**；Ctrl+C 连按两次才退出 |
| 大驱动缓冲 | 打开串口时申请 2MB 接收缓冲 |

压测（`python flood_test.py`，loop:// 虚拟串口 ~140KB/s ≈ 1.5M 线速）：灌入 802,560 字节 →
接收 802,560（无损）、屏显 335,018 + 智能省略 467,542（账目守恒）、日志 802,560（完整）。

---

## 二、多板聚合与网页终端（agent_hub.py）

一个 serial_debug 实例只管一个串口（固定 Tab 0）；板子多了若一实例配一个 MCP 入口，入口会爆炸。
网关把扫描区间（默认 8321..8400，排除兼容门端口）内的实例自动发现、聚合成多 Tab API，
`mcp_bridge.py` 零改动即可指向它。

```bash
python agent_hub.py --host 0.0.0.0            # 监听 8300(须在扫描区间外)
python agent_hub.py --ignore-ports 8321       # 扫描时忽略转发器端口(默认已含 8321)
```

### 浏览器终端（xterm.js，零 CDN 依赖）

```bash
# 网关已监听内网, 浏览器直开(免隧道):
http://<主机IP>:8300/
# 或走 SSH 隧道后开 http://127.0.0.1:8300/ :
ssh -N -L 8300:127.0.0.1:8300 <user>@<主机IP>
```

- 按板切换标签页；颜色/转义序列正确渲染（`ls` 彩色输出不乱码）；
- **光标直接在终端里打字**：实例 ≥ `1.2+agent` 自动**键击直通**（按键原样发给板子，
  回显/行编辑/↑↓ 历史/Ctrl-C 由板子 shell 处理，与真串口终端同手感）；旧实例自动降级**行模式**；
- AI 经网关的发送以橙色 `⟦AI➤⟧` 行注入同一画面，长组合命令按 `;` 折行，并附一行灰色
  **人话翻译**（危险动作标红 ⚠）——本地规则库，零延迟；
- 拉流走绝对 offset 增量读，**不消费 AI 的读游标**：人看和 AI 读互不干扰；
- 实例重启导致账本清零时自动重新同步（画面上提示「串口通道已重连」）。

**rig 状态条**（终端区上方一行，5s 自刷）：每套组合的控制器/受控串口/USB 模式三盏灯 +
**电源一键按钮**（开机 / 关机 / 冷启动 / 进Loader / 救砖进Loader，危险动作带确认框）。
点「受控」芯片直接跳转该板终端页；动作结果回显在状态条末行。人点按钮与 AI 调接口
走**同一条通道**（见 [AI 接入](#六ai-接入)）。双击状态条可收起以腾出终端空间。

### 防认错板子三板斧

1. **稳定寻址**：实例用 `/dev/serial/by-path/...`（物理插口）而非 `ttyUSBx`——重插/重枚举换号
   不受影响（劣质适配器序列号是假的或缺失，`by-id` 不可靠）；
2. **板名映射 + 自动命名**：优先取 `boards.json` 人工登记（`{"name": "TQ3588-A", "note": "..."}`，
   按串口路径全串/basename 匹配，**保存即生效**不用重启网关）；没登记的由取证签名自动派生
   （主机名 > SoC > dtb 名，如 `root@TQ3588` → `TQ3588`）；同型号撞名自动加 `@物理插口`
   后缀（`TQ3588@1.2` / `TQ3588@1.4.4`），标题恒唯一；静默板不猜名；
3. **被动取证**：读各实例账本**尾部 64KB**（offset 绝对读，**不发一个字节**、不动游标），
   提取出现最多的 `root@主机名` / `RK35xx` / `xxx.dtb` / `U-Boot` 签名放进 Tab 的 `hint` 字段。

### 语义与边界

Tab 序号 = 实例按 agent 端口升序的位置，**每次请求实时解析**（实例退出后序号前移，AI 用前先
`list_tabs` 对齐，每项带 `agent_port` 可精确定位）。网关无状态不缓存（发现结果有 1.5s TTL），
实例上下线立即反映。`POST /api/tabs` 回 409 并附启动命令提示——**网关不代开进程**，实例必须
活在终端/tmux 里供人观察与手工键入。子实例响应包络字节级透传（含 409/400 业务错误）；
实例的 `X-Agent-Token` 原样转发；监听端口落在扫描区间内会拒绝启动（防自己发现自己）。

---

## 三、串口实例守护（serial_keeper.py）

补上"最后一环"：不必再人工起实例。

```bash
tmux new-session -d -s serial-keeper "python3 serial_keeper.py"   # 常驻(或由 start_zops.sh 起)
python3 serial_keeper.py --status     # 管理视图: 哪些口有实例 / 哪些会自动起
python3 serial_keeper.py --once       # 单轮检查(调试)
```

- 盯 `/dev/serial/by-path`：新出现的**单口适配器**（名字以 `-port0` 结尾，CH340 类）几秒内自动起
  实例（`serial-auto-*` 会话，内含 while 自愈循环，agent 口在 8321..8400 里挑空闲）；
- 适配器消失 → **宽限 120s** 后自动清理（给断电放电/重启留时间），回来自动重起；
- 已被任何实例占用的口不碰（`pgrep` 按参数 + `fuser` 查真实 fd 占用，防"易失名实例"抢同一口）；
- CH344Q 四合一 / H503 命令口不带 `-port0` 后缀，天然排除，不会误起空实例；
- 波特率默认 1500000，特例写 `keeper.json`：

```json
{ "1.4.2": { "baud": 115200 }, "deny": ["1.1"] }
```

---

## 四、控电与救砖（rigctl.py）

`rigs.json` 把"控制组合"做成一等公民：**controller = 治具常量**（控制台/波特率/GPIO/放电时长，
永不因换板而变），**target = 受控槽位**（换板/挪线是常态，重探测即更新，旧绑定进 `_history`）。

```bash
python3 rigctl.py status                  # 所有组合: 控制器 / 受控串口 / USB 判活
python3 rigctl.py rig-A on|off            # 控电(幂等; off 真断电)
python3 rigctl.py rig-A coldboot          # 关机 → 放电(discharge_s) → 开机
python3 rigctl.py rig-A enter-loader      # 系统活着: 板型核对 → loader 命令 → 轮询 USB 判模式
python3 rigctl.py rig-A catch-uboot       # 内核已死: 冷启动抢 U-Boot 菜单 → 选[3]进 Loader
python3 rigctl.py rig-A bind              # 受控槽位重探测(关电盯谁灭→开电盯谁醒), 写回 rigs.json
python3 rigctl.py --selftest
```

### 两套电源方案

| 方案 | 通道 | 要点 |
|---|---|---|
| **治具 GPIO**（如 T536） | 治具调试串口 shell → 写 `/sys/class/gpio` | 必须用 **sysfs**（`gpioset` exit 模式设完即复位）；4 路同值；GPIO 释放态（治具重启）= 断电 |
| **H503 电源板** | USB CDC 命令口 | **双协议自适应**：现固件 A5 二进制 4 字节帧（回帧校验）、新固件 JSON 行协议（`{"ch":"5V2","st":1}`、`{"qr":...}` 查询）；通道名 `USB`/`5V2`/`5V3`/`12V4`/`12V5`；**裸 ON 无边沿不生效**（必须 OFF → 放电 ≥25s → ON）；板载 USB 口供电要先开 |

### 进 Loader 与救砖（重要区别）

| 板子状态 | 用什么 | 原理 |
|---|---|---|
| 系统正常（有 shell） | `enter-loader` | 在板内执行固件自带 `loader` 命令（或 `reboot loader`，需 dtsi 支持 `mode-loader`） |
| **内核 panic / 卡死** | **`catch-uboot`** | 板内没有 shell，in-system 命令必然无效——唯一入口是 U-Boot 的 **3 秒 autoboot 倒计时**（`Hit key to stop autoboot('CTRL+C')`）：冷启动后密集发 Ctrl+C 抢到 **U-Boot 菜单**，选 `[3] USB update images` 进 Loader（`lsusb` 见 `2207:350b`） |
| 砖到 loader 也没了 | 只能硬件 | MaskROM 无软件入口（dtsi 无 `mode-maskrom`），需按键/短接 D+/DM |

> `catch-uboot` 成功与否取决于串口恢复速度：目标板供电的 CH340 在断电期间会掉线，上电后需重新枚举。
> 一次没抓到就再跑一次。加 `--menu` 只停在菜单不自动选。

---

## 五、按板名操作 adb（adbq.py）

RK 板的 USB adb 序列号大量重复甚至为空，多板同插时 `adb -s 序列号` 会打到错的板上。
`adbq` 复用同一套身份体系：`adb devices -l` 自带 `usb:1-x.y.z` 物理链路，与 `boards.json` 里
by-path 键的 hub 链路段是同一个东西——**板名映射一份通吃串口和 adb**。

```bash
adbq boards                    # 列在线 adb 设备 + 板名解析(链路对不上的标 ?)
adbq TQ3588 shell ls /         # 按板名执行 adb 子命令, 自动解析成 adb -s <对的那台>
adbq TQ3588 -n shell ls /      # -n 只打印解析后的命令不执行
adbq --selftest
```

边界：只有 `boards.json` 的 **by-path 键**参与解析（`ttyUSBx` 键不含链路信息）；板名支持唯一前缀；
**不提供不带板名的广播**（多板时 `adb install` 这类广播正是事故源）；Debian 源的旧 adb(29.x) 碰新
adbd 可能 unauthorized——那是 adb 版本问题，换 platform-tools。

---

## 六、AI 接入

### Agent HTTP 通道（单实例）

`--agent-http` 开启本地 HTTP 服务后，AI 与人**看同一份串口流但互不依赖**：你照常用终端，AI 通过
接口读同一份数据；你不在场时 AI 也能独立完成"开串口 → 发指令 → 读响应"的完整闭环。

```bash
python serial_debug.py -p /dev/ttyUSB0 -b 1500000 --agent-http            # 端口默认 8321
python serial_debug.py --agent-http 9000 --agent-token 我的令牌
```

**数据保真按"真实 + 全面"设计，每条可验证**：

| 保证 | 机制 |
|---|---|
| 不丢（全面） | AI 读的是**会话捕获文件**（磁盘账本）而非内存缓冲：读过不过期、可任意重读，洪水下零丢失 |
| 可自查 | `GET /api/info` 暴露 `rx_total`（实收）与 `capture_bytes`（已落盘），恒等即无静默丢失 |
| 不失真（真实） | 原始字节 HEX 是真身，text 只是按当前编码的派生视图并附 `decode_errors` 计数 |
| 可还原现场 | 事件流带递增 seq：系统消息与每次发送（来源 human/agent、时间戳、载荷 b64）都入流 |
| 可复查 | `?offset=N` 绝对位置随机读，不动游标；不带 offset 按 `read_cursor` 续读 |
| 看最新 | `?tail=1` 取最新 `max_bytes` 尾窗（起点对齐行首），跳过字节计入 `skipped` 且仍可 offset 回看 |
| 低延迟 | `?wait_ms=N` 长轮询：无数据时服务端挂着等，数据一到立即返回（网页终端用它把回显压到几十 ms） |

**API 一览**（单实例；网关把 `/api/tabs/0/*` 映射为 `/api/tabs/<序号>/*`）：

```
GET  /api/status                          GET  /api/info          ← 对账入口
GET  /api/ports                           GET  /api/events?after_seq=N
GET  /api/tabs                            GET  /api/tabs/0/receive?offset=&max_bytes=&tail=&wait_ms=
POST /api/tabs/0/open   {"port":"","baudrate":0}   (参数可选)
POST /api/tabs/0/close  {}                POST /api/tabs/0/send   {"text":"AT"} 或 {"hex":"01 A2"}
                                          POST /api/tabs/0/send   {"text":"x","nl":false}  ← 键击直通不加行尾
```

网关额外提供：

```
GET  /api/rigs                            ← 各组合控制器/受控/USB 状态 + 最近动作输出
POST /api/rigs/<组合名>/<on|off|coldboot|enter-loader|catch-uboot>
GET  /api/audit?n=30                      ← AI 活动审计(最近 n 条)
```

### MCP 接入

`mcp_bridge.py` 源自 [tqtools/QSerial](git@192.168.1.7:tqtools/QSerial.git)（MIT），只依赖标准库，
把八个工具映射到上述 API。工具名为 `zops_status/list_ports/list_tabs/open_port/close_port/send/read/
create_tab`；**旧名 `qserial_*` 仍作别名照收**（在线会话不必重连）。`zops_read` 在桥侧默认 `tail=1`
（AI 要的是应答不是积压），可 `tail=false` 退回 FIFO、带 `offset` 回看。

```json
{ "mcpServers": { "serialdev": {
    "command": "python3",
    "args": ["/path/to/串口工具/mcp_bridge.py", "--port", "8300", "--timeout", "60"] } } }
```

> 指向 **8300（网关）** 即可看到所有板子；指向单个实例端口（如 8321）只看那一块。
> 改 MCP 配置后需重启会话才生效。

### AI 活动审计与围观

网关对每个 AI 读写落一行审计（`logs/hub_audit.jsonl`：时间/板名/动作/字节数/发送内容/接收尾部
预览，带 `src` 区分网页与 AI）——这是**强制卡口**：AI 只要走网关，读到的内容必然先经过这里。

```bash
tmux new-session -d -s serial-audit "python3 hub_audit_view.py -n 20"
tmux attach -t serial-audit        # 实时围观 AI 在读什么发什么, Ctrl+B D 退出
python3 hub_audit_view.py -n 100   # 一次性回放最近 100 条
```

AI 直连实例端口（832x）可绕开审计——`AGENTS.md` 已将其列为违规；确需封死可加防火墙规则。

---

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| **板子认错 / 串口张冠李戴** | `ttyUSBx` 会因重插重枚举换号 | 实例一律用 `by-path` 启动；认名字看 Tab 的 `title`/`board`/`hint` |
| **串口零输出（板子像没反应）** | ① 波特率配错（治具常 115200，RK 调试口 1500000）② 线被挪 ③ 适配器消失 ④ 系统没起来 | 先换波特率试；`rigctl status` 看链路；发个回车探活；别只凭一个通道下结论 |
| **串口突然变哑 / 实例报"连接中断"** | 板子重启时 CH340 掉线重枚举（目标板供电的适配器会陪葬） | 已内置**自动重连**；只要进程不退出账本就连续。进程真死了由 keeper 2s 内补起 |
| **网页空白 / 状态条一直"加载中"** | ① 页面缓存旧壳 ② 网关多实例抢答 ③ 前端 JS 语法错误 | `Ctrl+F5`；确认只有一个 `agent_hub.py` 进程；跑 `python3 test_ui_smoke.py` |
| **网页数据停在某处不刷新** | 板子重启 → 账本清零 → 网页持旧 offset 无限重试 | 已内置自动重同步（见「串口通道已重连」）；仍卡则刷新页面 |
| **控电点了没反应** | 断电的 20 秒串口必然静止，看起来像无响应；H503 还需先有边沿 | 看 `last` 回显是否 `完成`；`rigctl rig-A status` 确认控制器在线 |
| **`logs/` 目录消失** | 外部清理 | `bash start_zops.sh`（会自动 `mkdir -p logs`） |
| **进 Loader 没反应（系统已死）** | 内核没有 shell，in-system 命令必然无效 | 用 `rigctl rig-A catch-uboot` |
| **`2207:0006` 但不是我那块板** | 总线上其它 RK 设备冒名 | `rigctl status` 按 **adb 序列号**认主，不信类型级匹配 |

---

## 测试

```bash
python flood_test.py            # 洪水压测：无损、账目守恒、日志完整、省略路径生效
python smoke_test.py            # 命令与输入编辑冒烟测试
python test_agent_http.py       # Agent 通道 20 项：对账/复读/双观察来源/AI 独立闭环/令牌/桥接
python test_ui_smoke.py         # 网页冒烟：JS 语法(node --check)/函数重复定义/翻译器行为
python rigctl.py --selftest     # 链路归一化 / 节点解析 / 匹配
python adbq.py --selftest       # 链路解析 / 重复序列号消歧 / 前缀歧义
```

> 改 `agent_hub.py` 里的 `UI_HTML` 后**必须**跑 `test_ui_smoke.py`：curl 只能证明服务器回了什么，
> 证明不了浏览器拿到后能不能跑（历史上吃过"页面壳正常渲染但 JS 整体语法死亡"的亏）。

---

## 安全与边界

- 网关与实例默认只绑 `127.0.0.1`；`--host 0.0.0.0` 会暴露到内网（实验室便利，挂开放网络前请加令牌）。
- `send` / `open` / `close` / `off` / `coldboot` / `enter-loader` / `catch-uboot` / `bind` 都会
  **真实操作设备**（后几个会真断电）。跨机暴露请设 `--token` 并自担风险。
- `--no-capture` 与 `--agent-http` 同用时捕获被强制开启（AI 账本靠它）。
- 驱动层 FIFO 溢出发生在任何用户态工具之前，工具无法补回，但对账数字会让它现形——承诺形式是
  **"不静默丢失"**：每个字节要么交到你和 AI 手里，要么明确报出丢了多少。

## License

**PolyForm Noncommercial License 1.0.0** — 见 [`LICENSE`](LICENSE)。

| 用途 | 是否允许 |
|---|---|
| 个人学习、研究、实验、业余项目（无商业目的） | ✅ 免费 |
| 学校 / 公益 / 公共研究 / 政府机构使用 | ✅ 免费 |
| **公司内部业务流程中使用**（含给客户做项目、产线调试、内部工具链） | ❌ **需另行授权** |
| 集成进对外销售的产品 / 提供商业服务 | ❌ 需另行授权 |
| 修改、二次开发、再分发（非商业用途） | ✅ 允许，须保留版权声明与许可全文 |
| 专利授权 | ✅ 随许可一并授予（但发起专利诉讼即自动终止） |

**要点**：
- 这是**源码可见（source-available）**许可，**不是 OSI 定义的开源许可**——限制使用领域
  的许可不满足开源定义，请勿对外称"开源"。
- **许可变更不追溯**：本仓库此前以 MIT 发布的提交（截至 `a673302`）仍然且永远以 MIT 授权，
  任何人已获得的那些版本可以合法商用。非商业限制只对本提交及之后的版本生效。
- 商业授权、公司内部使用授权：联系仓库所有者。

第三方组件（均已 vendored，不联网加载，仍按原许可授权）：

- **xterm.js / xterm.css / addon-fit**（`webui/`）— MIT，Copyright (c) 2014 The xterm.js authors，
  全文见 [`webui/LICENSE-xterm.txt`](webui/LICENSE-xterm.txt)
- **MCP 桥的协议形态**源自 [tqtools/QSerial](git@192.168.1.7:tqtools/QSerial.git)（MIT）；
  本仓库内的实现已独立演化（多板聚合、`zops_*` 工具名、审计、网页终端等均为新增）
