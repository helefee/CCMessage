# CCMessage（agent-bridge）

仓库：https://github.com/helefee/CCMessage ・ 许可证：[MIT](LICENSE) ・ **部署到自己的项目：看 [DEPLOY.md](DEPLOY.md)**

让同一个项目里同时开着的 **Claude Code** 会话和 **Codex** 会话互相发消息、互相派活、互相交接，
带一个本地网页管理界面（手机扫码也能用）。

- **互发消息**：会话里一条命令发给别的会话；对方正在干活就在下一步看到，闲着也会被叫醒。
- **派活**：带任务号的任务，对方做完用 `done` 交回结果，结果自动回到派活的会话。任意一边都能当主会话。
- **会话视图**：网页里直接看每个 Claude / Codex 会话的对话记录，随时发消息给它。
- **交接**：Codex 线程一键导出成交接文件交给 Claude 接着做（Claude → Codex 由 Codex 桌面端自动导入）。
- **手机端**：电脑上出一次性二维码，手机扫码配对后能看、能发消息。
- **信件动画**（Windows）：Claude ↔ Codex 之间每来一条消息，桌面上有一封信从发件窗口飞到收件窗口。

只用 Python 标准库（二维码是可选的 `qrcode` 包）。

## 要求

- Python 3.11+、git
- Claude Code（桌面端或命令行）和 / 或 Codex（桌面端最完整：能被叫醒、能收「回复」）
- 可选：`pip install qrcode`（手机扫码配对）

检查这台电脑能不能用：

    python -m agent_bridge doctor

## 安装与启动

    git clone https://github.com/helefee/CCMessage.git
    cd CCMessage
    python -m agent_bridge            # 或双击 start.cmd（Windows）/ ./run.sh（Linux / macOS）

浏览器打开 http://127.0.0.1:8765/ 。右上角 ⋯ →「添加项目」选项目目录 →「一键安装」。

不开界面也行：

    python -m agent_bridge install <项目目录>      # 装进项目 + 自检
    python -m agent_bridge selftest                # 在临时目录里装一份跑完整自检（不碰任何真实项目）

### Windows 安装版（独立窗口，不用装 Python）

下载 `AgentBridge-Setup-<版本>.exe`（Release 里有；GitHub Actions 每次构建的 Artifacts 里也有），双击装就行：

- 装到 `%LOCALAPPDATA%\Programs\AgentBridge`，**只装给当前用户、不要管理员**；开始菜单里有「会话消息总线」，桌面图标可选；
  控制面板 / 设置 →「应用」里能卸载；再装新版本直接覆盖升级（正在跑的会先自动关掉）；
- 里面是 Nuitka 编译好的程序（`AgentBridge.exe`）+ 一个单独编译的总线程序 `bus\bus.exe`，**没有 .py 源码，电脑上不用装 Python**；
  窗口用系统自带的 WebView2（Edge 内核）；
- 窗口标题「会话消息总线」，大小、位置会记住；只开一个，再双击就把已有的窗口提到前面；
- 本机 8765 上已经有服务在跑（比如 start.cmd 开的）就直接连上它，关窗口不影响它；没有就在窗口进程里起服务，**关窗口 = 退出服务**；
- 浏览器照样能打开同一个地址 http://127.0.0.1:8765/ ，⋯ 菜单里多一个「在浏览器中打开」；手机端照旧；
- 命令行也能用：`AgentBridge.exe --port 8799 --no-browser`、`AgentBridge.exe doctor`、`AgentBridge.exe selftest`、`AgentBridge.exe install <项目目录>`（它是窗口程序，PowerShell 不等它、也接不到输出，在 PowerShell 里前面加 `cmd /c`）；
- 出错日志：`~/.agent-bridge/desktop.log`。

**钩子用 bus.exe**：用安装版给项目「安装 / 升级 / 修复」总线时，Claude 和 Codex 的钩子都改成
`"%LOCALAPPDATA%/Programs/AgentBridge/bus/bus.exe" --dir <项目里的总线目录> hook claude|codex`，每轮启动很快、不依赖 Python。
项目里的总线目录（`.msgbus` 或 `.temp/msgbus`）照旧放消息、收件箱、在线记录，格式不变；原来的 `bus.py` 留着没动（源码方式还能用）。
Codex 的钩子改了之后要在 Codex 的 `/hooks` 里重新信任一次；平时（不点安装 / 升级 / 修复）Codex 钩子一字不动，只有跑不起来了（比如写的 Python 已经不在）才改。
卸载安装版后用了 bus.exe 的项目钩子就跑不起来了 —— 再装回来，或者用源码方式的「升级 / 修复」改回 Python。
`AgentBridge.exe doctor`（源码方式 `python -m agent_bridge doctor`）会列出每个项目的钩子用的是什么。

**检查更新**：设置（齿轮）里有「版本 x.y.z · 检查更新」和「自动检查更新」（默认开，启动后、之后最多每 6 小时查一次 GitHub Releases 最新版）。
有新版本时右下角出一张小卡片（带更新说明），点「立即更新」：下载安装包（有进度）→ 对一遍 Release 里的 `.sha256` → 关掉服务 → 静默装新版（当前用户、不弹 UAC）→ 自动重新打开。
没网 / GitHub 连不上只是提示一下，不影响使用。源码方式和 Linux 只提示有新版本，用 `git pull` 更新。
（仓库是公开的，查更新不用登录；万一以后改成私有仓库，未登录的查询会失败 —— 界面会说明，可以设环境变量 `AGENT_BRIDGE_GITHUB_TOKEN` 给一个只读 token，程序里不内置任何密钥。）

自己构建（要一个 64 位的系统 Python 3.11、网络、[Inno Setup](https://jrsoftware.org/isdl.php) 6.5+ 或 7；没有 Visual Studio 时 Nuitka 会自己下 MinGW64）：

    build_desktop.cmd            # dist\AgentBridge\（编译好的文件夹）+ dist\AgentBridge-Setup-<版本>.exe（+ .sha256），中间跑 bus.exe 测速和总线自检
    build_desktop.cmd -Zip       # 另外打成 dist\AgentBridge-<版本>-windows-x64.zip（免安装，解压就能用）
    build_desktop.cmd -NoInstaller -NoTest

版本号只在 `agent_bridge/__init__.py` 的 `__version__` 里改；打 `v<版本>` 标签时 Actions 会核对两者一致，把安装包、`.sha256` 和 Linux 包挂到 Release 上。

源码方式也能开窗口：`pip install pywebview` 后 `python -m agent_bridge desktop`；源码方式（start.cmd）的钩子照旧用 Python 跑项目里的 `bus.py`。

### Linux

管理界面、手机端、会话记录、交接、Claude Code 命令行（`~/.claude`）和 Codex（`~/.codex`）都能用；
桌面信件动画（Win32）、「同步 Claude 会话到当前账号」（Claude 桌面端，Linux 上没有）会自动隐藏。

    git clone https://github.com/helefee/CCMessage.git && cd CCMessage
    ./install.sh                     # 建 .venv、装 qrcode、跑 doctor（要 Python 3.11+；Debian / Ubuntu 先 sudo apt install python3-venv）
    ./run.sh                         # 打开 http://127.0.0.1:8765/
    ./run.sh install ~/code/myproj   # 不开界面也能把总线装进项目

做成开机 / 登录后自动跑的 systemd 用户服务：

    ./install.sh --service                     # ~/.config/systemd/user/agent-bridge.service，已 enable --now
    systemctl --user status agent-bridge       # 看状态；日志 journalctl --user -u agent-bridge -f
    sudo loginctl enable-linger $USER          # 想不登录也一直跑
    ./install.sh --uninstall-service           # 删掉服务（.venv 和数据不动）

**在服务器上跑、从别的机器访问**：管理界面永远只听 127.0.0.1，不会开到局域网。
要从别的设备用，开一个只认配对设备的监听，跟手机端是同一套设备令牌：

    ./run.sh --no-browser --host 0.0.0.0       # 局域网监听在 端口+1（8766），可用 --lan-port 改
    ./install.sh --service --host 0.0.0.0      # 服务也一样
    ./run.sh pair                              # 出一个一次性配对链接 + 终端二维码（3 分钟、只能用一次）

没配对的请求一律 401；配对过的设备也只能看、发消息，安装 / 卸载 / 配对 / 设置只能在本机做。
真要远程管理，用 SSH 端口转发：`ssh -L 8765:127.0.0.1:8765 服务器`，再在自己电脑上开 http://127.0.0.1:8765/ 。

钩子用 `.venv` 背后的系统 python3（`/usr/bin/python3`），删 / 重建 `.venv` 不影响钩子。

可选的桌面窗口模式（GTK WebKit）：

    sudo apt install python3-gi gir1.2-webkit2-4.1              # Fedora：python3-gobject webkit2gtk4.1
    rm -rf .venv && python3 -m venv --system-site-packages .venv && ./install.sh --desktop
    ./run.sh desktop

**装进项目会做什么**（改前都备份到项目的 `.msgbus/bak/`）：

1. 放 `bus.py` 到 `<项目>/.msgbus/`；
2. Claude：往 `.claude/settings.local.json` 合并 4 个钩子（SessionStart / UserPromptSubmit / PostToolUse / Stop），原有配置保留；
3. Codex：往 `.codex/hooks.json` 补 3 个钩子（已有的一字不动）；
4. 是 git 仓就把上面这些加进 `.git/info/exclude`（不改 `.gitignore`、不进版本库）；
5. 跑自检。

**☠ Codex 还要人工一步**：在 Codex 里打开这个项目，进钩子管理（`/hooks`）把 3 个 msgbus 钩子设为信任。
没信任时 Codex 仍能被叫醒、用 `inbox` 收消息，只是看不到自动注入的全文。
某些切换模型的工具会重写 `~/.codex/config.toml`、把信任记录冲掉，界面顶部会显示「Codex 信任 0/3」，再点一次就好。

## 界面布局

**界面只有一个模式**：顶栏是项目、状态标签、主题按钮；
两侧是 Claude / Codex 会话列表，中间是总线消息或打开的会话。出问题（没装好、Codex 暂停）时顶上冒一行提示。
没有顶部标签页：点列表里的会话就在中间打开，标题栏最左边的 ‹ 或左上角「会话消息总线」回到总线消息。

**输入框只有一个「发送」**：网页上发的都是普通消息（总线消息页发给选中的对象，会话视图里发给这个会话）。网页不再派活，也没有任务看板了；
派活是会话之间的事（`bus.py task` / `done` 照旧）：它们派的任务、交回的结果照样出现在总线消息流里，任务卡片上标「已完成 / 没做成」并带上交回的结果，
结果卡片上点「回应任务 #…」跳回那条任务。消息流上方的筛选只剩「全部 / 消息 / 任务」（任务 = 任务和结果）加搜索。

**会话列表按项目分文件夹**（像 Claude Code 桌面版侧栏）：当前项目在最上面、默认展开，其它已装总线的项目折起，开合各设备自己记住；
文件夹里最近有动静的排前面，以前后台新开的、标了「别派」的收在文件夹底下默认折起的「其它 N 个」里。
总线消息只看当前项目；点其它项目里的会话会先切到那个项目再打开。

**主题**：浅色 / 深色 / 跟随系统（缺省），顶栏的主题按钮点一下换一种，「设置」里也能直接选；跟随系统时系统一换网页跟着换，各设备自己记住。

**设置**（顶栏齿轮，⋯ 菜单里也有「设置…」，手机也能用）：主题、状态说明位置、网页信件动画（这几样只影响这台设备，存在浏览器里），电脑上还有桌面信件动画的总开关。⋯ 菜单里只留操作（手机配对、添加项目、会话管理、同步、安装 / 修复、自检、试放桌面信件动画…）。

**状态说明**（待命 / 活跃 / 空闲，带当前项目在线会话的个数）：缺省「贴窗口底部」—— 每个会话栏最下面一条，不跟着列表滚，只数这一栏的会话；也可以选「顶部栏」（顶栏一份，Claude + Codex 合计；窄窗口只剩圆点和数字，鼠标停上去看说明；手机上点顶栏的状态圆点看）或「隐藏」。

**Codex 会话用 Codex 桌面版的样子**：Codex 那一栏和打开的 Codex 会话是黑白灰、无衬线字，命令一行行写「运行 `命令`」（带退出码、耗时），改文件写「编辑 文件 +N −M」，点开看输出 / diff，头像换成 Codex 的标；
Claude（和 Cursor）会话照旧是 Claude Code 桌面版的样子。

**字体**：英文字母和数字用 [Inter](https://rsms.me/inter/)，代码用 [JetBrains Mono](https://www.jetbrains.com/lp/mono/)（都是 SIL OFL 1.1，许可证在 `agent_bridge/fonts/`），
只含拉丁字符子集（约 270KB），由本机服务从 `/fonts/` 发给网页，不装进系统；中文一律用微软雅黑 UI。计数、时间、编号用等宽数字对齐。
改了 server.py 之后要重开一次（start.cmd）才会发字体；没重开时网页照常能用，只是退回 Segoe UI / Consolas。

**会话头像**：每条会话前面有一个小头像，身形（8 种）和颜色（10 种）按会话号定，同一个会话永远长一样，一眼能认出是哪条线。
**活跃的会动**（呼吸、眨眼、眼神漂移），待命的静止「专注」脸，空闲的静止「犯困」脸；右下角的小圆点仍是状态色。
系统设了「减少动态效果」时一律不动。界面里其余图标都是内嵌 SVG（[Lucide](https://lucide.dev)，ISC 许可）；Claude / Codex（OpenAI）/ Cursor 用各自的官方 logo（路径取自 [Simple Icons](https://simpleicons.org)，商标归各公司，这里只在本机工具里作识别用）。头像来自 [bloub](https://github.com/helefee/bloub)（MIT，© 2026 Jérémy Perret，许可证见 `licenses/bloub-MIT.txt`）：
`agent_bridge/bloub-icons.json` 是用它自己的引擎导出的静态路径与 idle 动画帧（`tools/gen_bloub_icons.ts`，约 84KB），网页里用 CSS 播放，不带它的程序。

电脑上是三栏：**Claude 会话列表和 Codex 会话列表分在左右两侧，跟着两个桌面窗口在屏幕上的左右位置摆**
（Claude 窗口在左，Claude 列表就在左；把窗口换边，网页几秒内跟着换），中间是会话内容 / 总线消息。
找不到窗口（没开、最小化、非 Windows）时缺省 Claude 在左、Codex 在右。手机上是底部导航，不分左右。

**网页上没有主 / 辅**：会话列表、会话视图里不再显示「主 / 辅」标志、图例和说明，也不能手动设。总线本身没改：`bus.py` 仍按派活关系自动判断（`set-role` 命令也还在），`task --to claude` 自动挑人时只挑总线在桌面端新开的 Claude 会话和用命令行设成「辅」的，你自己开的 Claude 会话要派活就写 `--to claude:前6位`。

**子任务**：Claude 会话里派出去的子代理（Task / Agent 工具）在会话视图里单独画成卡片，跟 Claude Code 桌面版差不多：
类型、说明、状态（转圈 = 在跑 / ✓ 完成 / ✗ 失败 / 没动静了）、用时、「N 次工具调用」；同一轮里一起派的几个归成「并行 N 个子任务」一组。
点开看提示词、子代理自己的过程（工具调用照样折叠，在跑的跟着刷新）和交回来的结果。子代理的记录在
`~/.claude/projects/<项目>/<会话号>/subagents/agent-<子代理号>.jsonl`，靠旁边 `.meta.json` 的 `toolUseId` 跟主记录里那次调用对上；
会话列表里不会把它们当成单独的会话。Codex 的子代理暂时不画。

**刷新**：顶栏 ⟳ 把会话、消息和当前打开的会话记录一起重读；两侧会话列表标题上的 ⟳ 同样；会话视图顶上的 ⟳ 从头重读这个会话的记录。

**网页信件动画**：新消息到了，网页里有一封信从发件会话那一行沿弧线飞到收件会话那一行（群发落在那一栏的标题上），落地那一行闪一下；
手机上没有两侧栏，就从 Claude / Codex 所在的那一侧屏幕边飞进飞出。「设置」里的「网页信件动画」开关（手机也能用，只影响这台设备）；
「桌面信件动画」是另一回事（Windows 桌面上窗口之间飞，只认本机）。

## 会话里怎么用

装好后每个会话开始时会自动看到一段用法说明。常用命令（在项目目录里）：

    python .msgbus/bus.py who                               # 谁在线
    python .msgbus/bus.py send --to <对象> "正文"             # 发消息
    python .msgbus/bus.py task --to <对象> "做什么、交回什么"   # 派活（--wait 秒数 原地等结果）
    python .msgbus/bus.py done <任务号> "结果"                # 交活（--fail 表示没做成）
    python .msgbus/bus.py tasks / wait <任务号> / inbox       # 看任务 / 等结果 / 手动收
    python .msgbus/bus.py codex-status                      # Codex 开没开（开着才交给 Codex）

`--to`：`all` | `claude` | `codex`（派活时总线挑，见下）| `codex:new`（强制新开）| 会话名片段 | `claude:前6位` | `codex:末6位`。

**派活只用对方开着的对话**：`task --to codex` 挑你开着的、空闲的 Codex 对话；`task --to claude` 挑你设成「辅」的、闲着的 Claude 会话
（用户自己的主线会话不挑）。都没有就拒绝（退出码 3）——想让另一边接活，就提前在那边开好对话放着（Claude 那边设成「辅」）。

**桌面端自动新开（实验，缺省关）**：设 `MSGBUS_CODEX_DESKTOP_NEW=1` / `MSGBUS_CLAUDE_DESKTOP_NEW=1` 才开。
☠ 2026-09-30 实测都不可靠：Codex 桌面版停在某个老对话时，深链没切到新对话，回车把活发进了老对话；
Claude 桌面端（`claude://code/new?q=…&folder=…`）新会话页和预填都出来了，模拟回车却发不出去。下面是它的做法，留作记录——
总线用 `codex://threads/new?path=…&prompt=…` 深链让桌面版新开一个对话并预填一句话（完整说明写在 `<总线>/workers/<任务号>.prompt.md`，
深链里只放摘要 + 文件路径），再把桌面版拉到前台模拟按回车发出去。你能在桌面版里看着它干活；认出新对话后记到派活方名下，之后优先复用。

- **会抢焦点，所以有两道闸**：你 5 秒没碰键盘鼠标才动手（`MSGBUS_DESKTOP_IDLE_SECS`），最多等 10 分钟（`MSGBUS_DESKTOP_IDLE_WAIT`，秒）；
  按回车前一刻再核一次「前台还是桌面版、这期间你没动过」，有一样不对就不按。
- 没发出去（一直等不到你空闲 / 窗口被挡 / 90 秒内没认出新对话）→ 私信派活方，任务保持未完成、**不自动改走后台**
  （桌面版里可能留着一条预填好的草稿，你按回车就发；免得变成两份）。
- 认新对话只认派活之后新建、总线没登记过的对话（老对话和它的子线程里也会有任务号，09-30 误判过一次）。
- 要后台无界面跑（`codex exec`，桌面版里看不到）：派活写 `--to codex:bg`。
- 某个对话不想让总线用：`bus.py codex-ignore codex:<末6位>`（`--undo` 取消）。

**Codex 额度用完 / 服务报错时自动暂停**：总线看 Codex 最近的对话记录和后台日志，最后一次出现 503 / 429 / 额度耗尽这类报错、之后没再成功回复过，
就暂停往 Codex 派活 30 分钟（`MSGBUS_CODEX_PAUSE_MIN` 可改），期间 `task --to codex` 拒绝、活留给 Claude；到点自动再试。
`bus.py codex-resume` 立即恢复（之前的报错不再算），`codex-pause --minutes N` 手动暂停。网页顶上会冒一行提示，带「恢复」按钮。

**多个 Claude 会话同时往 Codex 派活不会乱**：

- **谁派的活归谁用**：Codex 对话第一次接了哪个 Claude 会话的活，就记在它名下；别的会话不会往里派
  （原主 6 小时没动静、或 `bus.py release codex:<末6位>` 后放开）。
- **手上有活没交的不接新活**（避免两件活挤一个对话、或半路插进正在做的活）。
- `task --to codex` 挑的顺序：自己名下空闲的 → 没人认领的空闲对话 → **在桌面版里新开一个**（见上）。
  `--to codex:bg` 才用后台 `codex exec` 跑（派来的活就是它的第一句），之后派给它的活用 `codex exec resume` 接着做，上下文接得上；
  没用 `done` 交活就拿它最后一句回复兜底交回。新开的同时最多 4 个（`MSGBUS_MAX_NEW_CODEX`）。
  沙箱：项目 `.codex/config.toml` 配了 `sandbox_mode` 就照项目的，没配给「工作区可写」（`MSGBUS_CODEX_SANDBOX` 可改）。
- 每件活开头都带一段隔离说明（这件活独立、先核工作目录 / 分支 / worktree、做完 `done`）。
- `bus.py codex-status` 列出各 Codex 对话忙闲 / 归谁、这次会交给谁；网页会话列表里也标「归 X」「总线新开」。
- 硬指定别人名下或正忙的对话会被拒；确实要插队加 `--force`。
Codex 会话号前几位是时间戳，所以用**末** 6 位。

**共享资源排队（`lock`）**：部署测试服这类「同一时间只能一个会话动」的事，别群发「我要上了 / 我上完了」
（每条群发都会把所有会话叫醒白跑一轮），改成在总线里登记：

    python .msgbus/bus.py lock take 测试服 "要做什么"   # 空着就拿到；被占着自动排队（退出码 4），--wait 秒数 原地等
    python .msgbus/bus.py lock done 测试服 "结果"       # 交还；只私信排第一的那个会话「轮到你了」
    python .msgbus/bus.py lock show                     # 谁在用、谁在排、最近记录（不写资源名就列出全部）
    python .msgbus/bus.py lock leave 测试服             # 不排了

资源名随便起（缺省「副服」）。占用超过 180 分钟没交还算过期、别人可接手（`--ttl 分钟` 改）；
交还后排第一的 10 分钟没来接，后面的也能拿。

**占号登记（`claim`）**：并行开发时「我占了哪个卡号 / 要动哪些文件 / PR 开了、合了」也别群发，登记在总线里，要的人自己查：

    python .msgbus/bus.py claim list [关键字] [--all]                      # 已占的号（--all 连最近释放的）
    python .msgbus/bus.py claim check <文件或号>                           # 现在谁要动它
    python .msgbus/bus.py claim add <号> "做什么" --files a.lua,b.lua --branch feat/x   # 占号；--pr 编号 补 PR
    python .msgbus/bus.py claim done <号> --pr 编号 "结果"                 # 释放

撞号直接拒（系列号也算：占了 `KH` 别人不能占 `KHa`）；要动的文件和别人登记的重叠，只私信那一个会话。

**公告（`notice`）**：真要人人都知道、又不急的（新规矩、工具更新），用 `notice "正文"` 代替 `send --to all`：
它不排 `codex queue`、不让待命中的 Claude 醒来，各会话下次提交消息或调用工具时顺带看到；等到有正经消息把它叫醒时，公告也一并带上。

**派活模板**：[examples/派活模板/](examples/派活模板/) 有通用骨架（调研 / 盘点 / 实现独立单元 / 交叉复核 / 耗时验证 + 通用规矩 + 收活检查）。
复制到 `<项目>/.msgbus/派活模板/`，把通用规矩里「不许碰什么」「产物放哪」改成项目的；派活时一句话写「按 .msgbus/派活模板/调研.md 做，对象=…，交回目录=…」。

**项目自己的派活规则**：写在 `<项目>/.msgbus/rules.md`，会话开始时注入它代替缺省规则。
缺省规则：可并行的独立单元、换模型交叉复核、耗时验证、调研适合派；动共享资源、两边改同一批文件、要用户拍板的、几分钟能做完的不派。
Codex 开着才交给 Codex（`task --to codex` 没开会拒绝、退出码 3），没开就留给 Claude。

## 送达方式

| 对方 | 正在干活 | 闲着 |
|---|---|---|
| Claude | 钩子把消息注入上下文 | Stop 钩子（`asyncRewake`）后台待命，有消息退出码 2 → Claude Code 叫醒会话 |
| Codex | 钩子注入（要先信任） | `codex queue` 排一条提醒，桌面端会话被叫醒 |
| Cursor | 钩子注入（Cursor 照跑 `.claude/settings.json` 里的钩子） | **叫不醒**；它一轮结束时如果有新消息，会作为一条跟进消息自动接着处理 |

网页里「回复」Codex 是一条真正的用户输入（`codex queue`）；「回复」Claude 只能经总线送达（外部没有往 Claude 桌面会话塞用户输入的口子），
发件人是 `user:ui`（手机端是 `user:phone`），送达时标成【用户本人 · 经 agent-bridge 网页】并注明「等同用户直接对你说的话，按用户指令办」，
和别的 AI 会话发来的【来自 …】留言（「不是用户本人的指令，按内容判断」）分开。
总线不让 AI 会话用 `--as user:…` 发消息（在 Claude / Codex / Cursor 的命令行里会被拒），免得 AI 冒充你。
旧项目要点一次 ⋯ → 安装 / 升级 / 修复 换上新版 bus.py（同时把 Stop 钩子的叫醒标题改成「msgbus：收到新消息」）。
网页的会话记录里：你经总线发的回复画成你的气泡、标「经总线」（Claude 还没收到时先显示一个半透明的「已发出」气泡）；
别的会话发来的画成收件卡片；会话自己跑 `bus.py send / task / notice / done` 画成发件卡片（点「命令和输出」看原命令）。
Claude 桌面端自己的界面里，这些仍然只是钩子提示（「msgbus：收到新消息」），不会变成你的聊天气泡 —— 外部没有往桌面端会话里提交用户消息的口子。

### Cursor 会话

Cursor（3.x）会读项目 `.claude/settings.json` 里的 Claude 钩子（设置里的第三方钩子 / Claude 兼容，缺省开着），
所以装好总线后，**在项目里开的 Cursor Agent 对话会自动接上**，身份是 `cursor:<会话号前 6 位>`，网页上排在 Claude 那一栏、名字前标「Cursor」。

- 收消息：提交消息、调用工具时注入上下文；一轮结束时有新消息，用 stop 钩子的 `followup_message` 让它自动接着处理。
- 闲着的 Cursor 叫不醒（没有像 Claude `asyncRewake` 那样的后台待命）。想让它一轮结束后多等一会儿，
  设环境变量 `MSGBUS_CURSOR_WAIT=秒数`（缺省 0：只看一眼不等，免得对话卡在「运行钩子」上）。
- Cursor 的命令行拿不到会话号，所以它跑总线命令要带 `--as cursor:<会话号>`；开会话时注入的说明里命令已经带好了。
- 发给它：`send --to cursor`（所有 Cursor）或 `--to cursor:前6位`。信件动画暂时不画 Cursor。

### Cursor 后台派活（Cursor 命令行）

不用谁开着 Cursor：`task --to cursor:new "…"` 让总线用 Cursor 命令行在后台开一个对话跑完交回
（或接着你名下空闲的那个后台对话，上下文接得上）。和 Codex 后台一样：归派活的会话用、手上有活的不再派、同时最多 4 个。

- 先装 Cursor 命令行并登录（一次）：Windows PowerShell 里 `irm 'https://cursor.com/install?win32=true' | iex`，
  macOS / Linux `curl https://cursor.com/install -fsS | bash`；然后 `agent login`。程序装在 `~/.local/bin/agent`。
  ☠ 别的工具也可能叫 `agent`（比如 Grok），总线只认 `cursor-agent` 或 `~/.local/bin` 下的 `agent`；装在别处就设 `MSGBUS_CURSOR_AGENT=<完整路径>`。
- 跑法：`agent -p --output-format stream-json --force --workspace <项目> [--resume <对话>] "<一句话>"`。
  任务说明写进 `<总线>/workers/<任务号>.prompt.md`，只给它一句话让它去读（多行说明经 Windows 的 .cmd 包装会被截断）。
  `--force` 表示它跑命令不再逐条问你 —— 这就是「后台」的意思，派活时写清楚不许碰什么。
- 它用环境变量 `MSGBUS_AS=cursor:<对话>` 认身份，`done` 交活；没交就拿它最后一句回复兜底交回。日志在 `<总线>/workers/<任务号>.log`。
- 后台开的对话在 Cursor 桌面端里大概率看不到（和 Codex 后台开的一样）。

## 手机端

电脑界面 ⋯ →「📱 手机端（扫码）」：打开局域网监听、出一次性配对二维码（3 分钟、只能用一次）。
手机扫码后拿到 30 天的设备令牌；局域网来的请求没有有效令牌一律拒绝；手机只能看、发消息，
安装 / 卸载 / 配对 / 设置只认电脑本机。对话框里能逐台移除手机、关闭手机访问。
第一次打开时 Windows 防火墙可能问 Python 能否联网：选「允许（专用网络）」。

⚠ 能打开这个页面的设备就能给你电脑上有完整权限的 AI 会话下指令 —— 配对令牌就是这道门，别关掉它。

## Claude ↔ Codex 交接

- **Claude → Codex**：Codex 桌面端每次启动会自动导入 `~/.claude/projects/` 下的 Claude Code 会话（只有对话文字、是快照；Claude 的记忆不带）。
- **Codex → Claude**：会话视图里 Codex 会话顶上「🤝 交给 Claude」，或

      python -m agent_bridge export <Codex 线程末几位>

  导出交接 Markdown 到 `<项目>/.msgbus/exports/`：接手说明、导出时现查的 git 状态、最后几轮、完整对话与命令。
- **把 Codex 对话同步进 Claude 桌面端列表**（和 Codex 自动导入 Claude 会话反过来）：界面 ⋯ →「📥 把 Codex 对话同步进 Claude…」，或

      python -m agent_bridge codex-sync [--dry-run] [--days 14] [--cwd 项目目录]

  - 只同步你在 Codex 桌面端里自己开的对话；从 Claude 导进 Codex 的、后台 exec 开的、子代理都不同步（免得来回套娃）。
  - 每个 Codex 对话对应一个固定的 Claude 会话，标题前带「（Codex）」：Codex 那边又聊了就更新；你在 Claude 里接着聊过的不再覆盖。
  - 只带对话文字，Codex 跑过的命令折成一行说明；接着做之前让 Claude 先核一下 git / 文件现状。
  - 顺手在 Codex 的导入记录里登记「这份就是原来那个对话」，Codex 不会再把它导回去成重复的。写之前整目录备份到 `~/.agent-bridge/codex-sync-bak/`。
  - Claude 桌面端只在启动时读列表：同步完要**重启 Claude 桌面端**。
  - 这靠的是本机 Claude / Codex 的文件格式（没公开的），它们升级改格式后可能要跟着改。

## 记忆互通

Claude Code 的记忆按项目存在 `~/.claude/projects/<项目>/memory/`（不分账号，切账号不会丢）；Codex 的记忆是全局的，在 `~/.codex/memories/`。
两套互相看不到，Codex 自动导入 Claude 会话时也不带记忆。所以装了总线的项目，开会话时会顺带注入对方记忆的位置：

- **Codex 开会话**：「本项目还有 Claude Code 积累的记忆（N 条），索引在 …/MEMORY.md，开工前先读索引，相关条目再读正文」
- **Claude 开会话**：「Codex 那边的记忆在 …/memory_summary.md、…/MEMORY.md，接手 Codex 做过的活时先翻一眼」

只给位置和提示，不把整份记忆塞进上下文；都叮嘱了「冲突以代码 / 文件现状为准、别改对方的记忆」。

## 会话管理（借鉴 cc-switch）

界面 ⋯ →「会话管理」，四页：

- **会话**：Claude（桌面端登记 + 命令行 / 子代理开的）和 Codex 的会话放在一张表里，每条标出当前账号下**看不看得见**，
  看不见的写明原因（在别的账号里 / 没登记 / 在别的供应商桶 / 别的 ChatGPT 账号建的 / 已归档 / 记录文件已不在）。
  按标题、第一句话、目录、会话号筛；「全文搜」在会话记录正文里找（限时 20 秒）。每条可**复制恢复命令**
  （`claude --resume <会话>` / `codex resume <线程>`，带 `cd` 到原目录）、**导出 Markdown**。
  勾选多条可**归档 / 取消归档 / 删除**：删除是挪进 `~/.agent-bridge/trash/<批次>/`（会话记录、子代理目录、各账号里的登记、Codex 库里那一行一起挪），可整批还原。
- **同步与还原**：Claude 换号补齐、Codex 归桶（见下），以及历次备份 / 账本的撤回。
- **回收站**：按批次还原。
- **排队**：等桌面端退出后再做的活，可取消。

**桌面端开着时不直接改它的数据。** 两个桌面端都把会话登记放在内存里、退出时写回磁盘，开着时改的东西会在它退出那一下被盖回去
（2026-10-05 实测：Claude 同步写完 44 秒后桌面端退出，开着的 10 个会话被写回了旧版本）。所以所有改动都先看对应桌面端开没开：
没开就立刻做；开着就排队，起一个独立的后台进程等它**完全退出**（托盘里也要退）再做，做完自动把它重新打开。

    python -m agent_bridge jobs                     # 排队的活

## 切换 Claude 账号后会话「不见了」

Claude 桌面端的会话列表按「账号 / 组织」分文件夹记（`<Claude 数据目录>/claude-code-sessions/<账号>/<组织>/local_*.json`），
只显示当前登录的那个；对话内容本身在 `~/.claude/projects/`，本地、不分账号，切账号不会丢。

**桌面端和命令行（Claude Code CLI）是两套登录，可以登的不是同一个号。** 桌面端的号从它自己的 Local Storage
（`<Claude 数据目录>/Local Storage/leveldb`）里认，命令行的号看 `~/.claude.json` 的 `oauthAccount`。会话列表是给桌面端看的，所以缺省按桌面端的号算。
命令行自己（`claude --resume`）直接读 `~/.claude/projects/`，不看这份按账号分的列表；补到 CLI 的号，只有桌面端也登这个号时才看得到。

    python -m agent_bridge claude-sync --list      # 各账号 / 组织下有多少会话，标出桌面端 / CLI 当前是哪个
    python -m agent_bridge claude-sync --dry-run   # 看看会补进来哪些（缺省两边都看：--source both）
    python -m agent_bridge claude-sync [--source desktop|cli]   # 补齐到桌面端（缺省）或 CLI 当前登录的那个号，一次只能选一边
    python -m agent_bridge claude-sync --backups   # 历次备份
    python -m agent_bridge claude-sync --restore <备份>   # 撤回某次同步

或界面 ⋯ →「同步 Claude 会话到当前账号」（可以勾「桌面端」「命令行（CLI）」分别预览、分别补齐）/「会话管理 → 同步与还原」（只按桌面端的号）。

- 同一个会话在几个旧账号里都有登记（换过不止一次号）时，取**最后活动时间最新**的那份；当前账号里已有、但别处版本更新的，一并刷新
  （保留当前账号里的收藏 / 归档 / 点开时间，去掉旧账号被停用时留下的报错）。只取第一份会补进旧版本，桌面端按里面记的「最后一条回复」读，看到的就不是最新的。
- 在当前账号里删过的（文件夹里的 `deleted_<会话号>` 墓碑）不复活；归档状态两处都认（登记里的 `isArchived` 和 `archived-sessions.idx`）。
- 写之前整个目录备份到 `~/.agent-bridge/claude-sync-bak/<时间>/`，旁边 `ledger.json` 记这次补了哪些、刷新了哪些。撤回：补进来的挪进回收站，刷新过的换回原样；
  之后你自己新开的会话不动。没有账本的老备份只补回现在缺的，不覆盖。
- 会话里绑过的 PR、远程控制等是跟原账号走的，换账号后可能不可用；对话本身能接着聊。

## Codex 换号 / 换供应商后老对话「不见了」

Codex 的对话列表按两样东西筛：建对话时的供应商 `model_provider`（rollout 文件第一行 `session_meta` + `state_*.sqlite` 的 `threads` 表），
和建对话的账号 `creator_account_id`。`config.toml` 的 `model_provider` 一改（哪怕只是 `openai` → `OpenAI` 大小写），老对话就全落进别的桶。

    python -m agent_bridge codex-unify --dry-run    # 看有多少不在当前桶
    python -m agent_bridge codex-unify [--no-account]   # 归到当前供应商（和当前 ChatGPT 账号）
    python -m agent_bridge codex-unify --list       # 历次账本
    python -m agent_bridge codex-unify --restore <账本>

- 写之前 state 库整份在线备份、每个要改的 rollout 文件原样备份，账本记每条线程原来的值，都在 `~/.agent-bridge/codex-unify-bak/<时间>/`；
- 改文件前后各核一次大小和修改时间，中途被写过就跳过；改完把修改时间还原，列表顺序不变；`creator_account_id` 为空的老对话不动；
- 还原：线程字段改回原值；文件之后没动过就整份换回备份，动过（又聊过）就只把那一行的供应商改回去，新内容留着；
- 归桶不改归档状态：已归档的要在「会话」页取消归档才会出现在列表里。

## 数据放哪

- 程序本体：本仓目录。
- 每个用户的数据：`~/.agent-bridge/`（登记的项目、设置、配对过的手机；可用 `AGENT_BRIDGE_HOME` 改）。
- 每个项目的：`<项目>/.msgbus/`（信箱 `messages.jsonl`、在线记录、读到位置、交接导出、备份、可选 `rules.md`）。

## 卸载

界面「卸载」或 `python -m agent_bridge uninstall <项目目录> [--purge]`：去掉两边的 msgbus 钩子（改前备份），`--purge` 连消息记录一起删。

## 已知限制

- 信件动画、按窗口定位只在 Windows 上有；「同步 Claude 会话到当前账号」只在装了 Claude 桌面端的电脑上有。
- 桌面版窗口要 WebView2（Win10 / 11 都自带；极少数精简系统要装 Microsoft Edge WebView2 Runtime）。
- 「Codex 开没开」按 `codex … app-server` 进程判断（Windows 用 WMI，其他系统用 `pgrep`）。
- Claude 没有外部注入用户输入的接口，网页里对 Claude 的「回复」是经总线送达，靠文字标注「用户本人」让 Claude 认；
  Claude 依然可能对高风险操作再跟你确认一次。`claude --resume <会话> -p` 能交成真正的用户输入，但它是另起一个无界面进程往同一个会话写，
  和桌面端开着的会话抢着写、桌面端也看不到这一轮，所以没用。
- 升级总线前就已经闲着待命的 Claude 会话，后台跑的还是旧版 bus.py，下一条消息仍是旧措辞；它干完下一轮就换成新版。
- 读会话记录依赖 Claude Code / Codex 本地记录文件的格式，它们升级改格式时可能要跟着改。
