# 部署说明：把 CCMessage（agent-bridge）装到你自己的项目

照着从上往下做就能用。每一步后面都有「怎么确认成功」。功能介绍见 [README](README.md)。

---

## 0. 先看适不适合

适合：同一台电脑上，同一个项目里，**同时开着 Claude Code 和 / 或 Codex 的多个会话**，想让它们互相发消息、派活、交接。

| 你的情况 | 能用到什么 |
|---|---|
| 只有 Claude Code（桌面端或终端） | Claude 会话之间互发消息、派活、闲时自动叫醒、网页 / 手机看会话 |
| 只有 Codex 桌面端 | Codex 会话之间互发消息、派活（Codex 被叫醒要靠桌面端开着） |
| 两个都有 | 全部功能：Claude ↔ Codex 互派活、交接、记忆互通、信件动画 |

**不适合**：跨电脑协作（信箱是本机文件）；只用 `claude -p` / `codex exec` 这种跑一次就退出的无界面方式（没法闲着等消息）。

---

## 1. 准备环境（每台电脑一次）

需要：

- **Python 3.11 或更新**（用到标准库 `tomllib`）。装的时候勾上「Add to PATH」。
- **git**
- **Claude Code**：桌面端，或较新版本的命令行（闲时自动叫醒用到 Stop 钩子的 `asyncRewake`；老版本没有这个能力时，闲着的 Claude 会话要手动挂 `listen` 才收得到）
- **Codex 桌面端**（要用 Codex 的话）
- 可选：`pip install qrcode`（手机扫码配对；不装就只显示配对链接）

取代码：

    git clone https://github.com/helefee/CCMessage.git
    cd CCMessage

检查这台电脑：

    python -m agent_bridge doctor

✅ 成功：最后一行是「结论：可以用」。标 ✗ 的是必需项，按提示补；标「（可选）」的不影响使用。

> **Windows 小提示**：命令里的 `python` 如果打开的是微软商店，说明 PATH 里没有真的 Python；装 python.org 的安装包并勾选 PATH，或在「应用执行别名」里关掉 python 的商店别名。

---

## 2. 先在临时目录里试一遍（不碰任何真实项目）

    python -m agent_bridge selftest

它会在临时目录建一个空项目、装进去、跑 7 项自检、再卸载删掉。

✅ 成功：7 行都是 ✓，最后是「卸载：…删除 .msgbus/」。

---

## 3. 启动管理服务

    python -m agent_bridge            # Windows 也可以双击 start.cmd；macOS / Linux 用 ./start.sh

浏览器会自动打开 http://127.0.0.1:8765/ 。只听本机，别人访问不到。

- 换端口：`python -m agent_bridge --port 9000`
- 不自动开浏览器：`--no-browser`
- 已经开着再启动一次：会直接帮你打开浏览器，不会起第二个

> 服务只是管理窗口。**关掉它，会话之间照样能互发消息、派活**；只有网页界面、手机端、桌面信件动画要它开着。

想开机自动启动（Windows）：

    schtasks /Create /SC ONLOGON /TN CCMessage /TR "\"<CCMessage 所在目录>\start.cmd\" --no-browser"

---

## 4. 装进你的项目

**网页里**：右上角 ⋯ →「＋ 添加项目」→ 填项目目录的绝对路径（下拉里有最近用过的目录）→ 顶上点「一键安装」。

**或命令行**：

    python -m agent_bridge install <你的项目目录>

它会做这些事（改之前都备份到 `<项目>/.msgbus/bak/`）：

| 动作 | 位置 | 说明 |
|---|---|---|
| 放总线程序 | `<项目>/.msgbus/bus.py` | 会话里用的就是它 |
| 加 Claude 钩子 4 个 | `<项目>/.claude/settings.local.json` | **合并**，你原有的权限、别的钩子都保留；个人配置，一般不进版本库 |
| 加 Codex 钩子 3 个 | `<项目>/.codex/hooks.json` | 只补缺的，已有的一字不动 |
| 不让它们进版本库 | `.git/info/exclude` | 只在 git 仓里做；不改 `.gitignore`；已被 git 跟踪的文件不加 |
| 自检 | — | 7 项 |

✅ 成功：自检 7 项全 ✓；网页顶上「Claude 钩子 4/4」「Codex 钩子 3/3」是绿的。

> 钩子命令里写的是**安装时那个 Python 的绝对路径**。以后换了 Python 版本或挪了位置，回网页点一次「升级 / 修复」就会重写。

---

## 5. ☠ Codex 要人工点一次「信任」

Codex 对新加的钩子有一道信任闸：没点信任的钩子**不会运行**（`codex exec` 下也静默跳过）。

1. 在 Codex 桌面端打开这个项目
2. 进钩子管理（输入 `/hooks`）
3. 把 3 个 msgbus 钩子（SessionStart / UserPromptSubmit / PostToolUse）设为**信任**

✅ 成功：网页顶上「Codex 信任 3/3」变绿。

> 有些切换模型 / 服务商的工具（比如 CC Switch）会整份重写 `~/.codex/config.toml`，把信任记录冲掉。界面顶上变回「Codex 信任 0/3」时，再点一次就好。没信任时 Codex 仍能被叫醒、用 `inbox` 收消息，只是看不到自动注入的全文。

---

## 6. 让会话接上

- **新开的会话**：开会话时会自动看到一段「消息总线」用法说明 —— 看到就说明接上了。
- **已经开着的 Claude 会话**：一般不用重开就能接上；没接上就重开一次。「闲时自动叫醒」要等它结束一轮之后才挂上。
- **会话要在项目目录（或它的子目录）里开**。项目以外的会话总线不理。
- **Cursor**：不用另装。Cursor 3.x 会照跑 `.claude/settings.json` 里的钩子，在项目里开的 Agent 对话自动接上（显示成 `cursor:xxxxxx`）。
  没接上就检查 Cursor 设置里第三方钩子（Claude 兼容）是不是被关了。闲着的 Cursor 叫不醒，见 README「Cursor 会话」。

验收（在任意一个会话里让它跑）：

    python .msgbus/bus.py who          # 列出在线的会话
    python .msgbus/bus.py codex-status # Codex 开没开、各 Codex 对话忙闲 / 归谁

再从网页底部以「用户」身份群发一句话，看各会话能不能收到。

---

## 7. （推荐）告诉会话什么时候该派活

两种写法，选一种：

- **只给总线看**：把 [examples/rules.md](examples/rules.md) 复制成 `<项目>/.msgbus/rules.md` 按项目改（4000 字以内，整份原样注入）。会话开始时注入它，代替缺省规则。
- **写进项目说明**：把 [examples/AGENTS-snippet.md](examples/AGENTS-snippet.md) 贴进项目的 `AGENTS.md` 或 `CLAUDE.md`（Codex 读 `AGENTS.md`，Claude 读 `CLAUDE.md`）。

不写也行，缺省规则是：可并行的独立单元、换模型交叉复核、耗时验证、调研适合派；动共享资源、两边改同一批文件、要用户拍板的、几分钟能做完的不派。

---

## 8. （可选）手机端

电脑网页 ⋯ →「📱 手机端（扫码）」→ 手机连同一个 Wi-Fi 扫码。第一次时 Windows 防火墙可能问 Python 能否联网，选「允许（专用网络）」。

⚠ 能打开这个页面的设备就能给你电脑上有完整权限的 AI 会话派活。配对令牌就是这道门：只在需要时开、不用了点「关闭手机访问」、丢了手机点「移除」。

---

## 9. 更新到新版本

    cd CCMessage
    git pull

然后：

1. 重启管理服务（关掉再 `python -m agent_bridge`）
2. 网页里每个项目点一次「升级 / 修复」（或 `python -m agent_bridge install <项目目录>`）—— 把新版 `bus.py` 同步进项目、补上新版要的钩子，再自检

项目里的钩子配置已经齐的不会重写，Codex 信任不会失效；**新版多了钩子时**才会补，Codex 新补的那几个要再信任一次。

---

## 10. 卸载

网页「卸载」，或：

    python -m agent_bridge uninstall <项目目录>          # 去掉两边的 msgbus 钩子（先备份），保留消息记录
    python -m agent_bridge uninstall <项目目录> --purge  # 连 .msgbus/ 一起删

彻底不用了：再删掉 `~/.agent-bridge/`（登记的项目、设置、配对过的手机）和 CCMessage 目录。

---

## 11. 数据都在哪

| 位置 | 内容 | 能删吗 |
|---|---|---|
| CCMessage 目录 | 程序本体 | 卸载后可删 |
| `~/.agent-bridge/`（可用 `AGENT_BRIDGE_HOME` 改） | 登记的项目、设置、配对的手机、`claude-sync` 备份 | 删了等于重置 |
| `<项目>/.msgbus/` | 信箱 `messages.jsonl`、在线记录、读到位置、交接导出、`workers/`（新开的 Codex 对话）、备份、`rules.md` | 删了就是清空这个项目的消息历史 |
| `<项目>/.claude/settings.local.json`、`<项目>/.codex/hooks.json` | 钩子 | 用「卸载」去掉，别手删一半 |

---

## 12. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 网页「Codex 信任 0/3」 | 没点信任，或被切换工具冲掉了 → 第 5 步再点一次 |
| Codex 收不到派的活 | Codex 桌面端没开（没开时 `task --to codex` 会直接拒绝、退出码 3，这件活应留给 Claude）；或那个线程在 Codex 里被删了（总线会自动清掉它的登记） |
| 闲着的 Claude 会话收不到消息 | 它还没结束过一轮（Stop 待命钩子没挂上）；或 Claude Code 版本太老没有 `asyncRewake` → 让它跑 `python .msgbus/bus.py listen --once` 挂在后台 |
| 已经开着的会话看不到用法说明 | 说明只在开会话时注入一次；新会话才看得到。消息照常能收 |
| 多个 Claude 会话都往 Codex 派活 | 不会乱：Codex 对话归第一次派活的会话用、手上有活不接新活、没空闲就新开一个（见 README「会话里怎么用」） |
| 新开的 Codex 对话交不了活、报只读 | 项目没配 Codex 沙箱时总线会给「工作区可写」；你项目 `.codex/config.toml` 里要是写了 `sandbox_mode = "read-only"`，就会这样 —— 改配置，或设环境变量 `MSGBUS_CODEX_SANDBOX=workspace-write` |
| 手机连不上 | 不在同一局域网；防火墙拦了 Python；或「关闭手机访问」了 → 电脑上重新点「手机端」 |
| 切换 Claude 账号后会话列表少了 | 对话没丢，只是桌面端列表按账号分 → `python -m agent_bridge claude-sync`，再**重启 Claude 桌面端** |
| 换了 Python / 挪了目录后钩子报错 | 钩子里写着安装时的 Python 绝对路径 → 网页点「升级 / 修复」 |
| 端口 8765 被占 | `python -m agent_bridge --port 9000` |
| 想看钩子有没有出错 | `<项目>/.msgbus/hook-errors.log`（钩子出错只记这里，不会卡住会话） |

---

## 13. 平台说明

- **Windows**：全部功能。
- **macOS / Linux**：除信件桌面动画、按窗口位置摆放会话列表外都能用（这两样用了 Win32 接口，其他系统自动跳过；网页信件动画照常有）。Claude 桌面端数据目录按系统自动找，Codex 命令行按 `CODEX_CLI_PATH` → PATH 里的 `codex` → macOS 桌面端自带的 顺序找。
