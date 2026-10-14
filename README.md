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

## 测试

```bash
python flood_test.py   # 洪水压测：无损、账目守恒、日志完整、省略路径生效
python smoke_test.py   # 命令与输入编辑冒烟测试
```
