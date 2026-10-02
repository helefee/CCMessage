# CCMessage（agent-bridge）

仓库：https://github.com/helefee/CCMessage ・ 许可证：[MIT](LICENSE) ・ **部署到自己的项目：看 [DEPLOY.md](DEPLOY.md)**

让同一个项目里同时开着的 **Claude Code** 会话和 **Codex** 会话互相发消息、互相派活、互相交接，
带一个本地网页管理界面（手机扫码也能用）。

- **互发消息**：会话里一条命令发给别的会话；对方正在干活就在下一步看到，闲着也会被叫醒。
- **派活**：带任务号的任务，对方做完用 `done` 交回结果，结果自动回到派活的会话。任意一边都能当主会话。
- **会话视图**：网页里直接看每个 Claude / Codex 会话的对话记录，随时回复、派活。
- **交接**：Codex 线程一键导出成交接文件交给 Claude 接着做（Claude → Codex 由 Codex 桌面端自动导入）。
- **手机端**：电脑上出一次性二维码，手机扫码配对后能看、能回、能派活。
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
    python -m agent_bridge            # 或双击 start.cmd（Windows）/ ./start.sh

浏览器打开 http://127.0.0.1:8765/ 。右上角 ⋯ →「添加项目」选项目目录 →「一键安装」。

不开界面也行：

    python -m agent_bridge install <项目目录>      # 装进项目 + 自检
    python -m agent_bridge selftest                # 在临时目录里装一份跑完整自检（不碰任何真实项目）

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

**缺省是简洁模式**：只有左右两栏会话列表和中间的对话 / 输入框；状态标签、标签页、筛选、任务看板、图例都收起来，出问题（没装好、Codex 暂停）时顶上才冒一行提示。
⋯ 菜单「完整模式」切回全部功能（各设备自己记住）。

**会话头像**：每条会话前面有一个小头像，身形（8 种）和颜色（10 种）按会话号定，同一个会话永远长一样，一眼能认出是哪条线。
**活跃的会动**（呼吸、眨眼、眼神漂移），待命的静止「专注」脸，空闲的静止「犯困」脸；右下角的小圆点仍是状态色。
系统设了「减少动态效果」时一律不动。界面里其余图标都是内嵌 SVG（[Lucide](https://lucide.dev)，ISC 许可）；Claude / Codex / Cursor 三个标识是按意思画的简化图形，不是官方商标原图。头像来自 [bloub](https://github.com/helefee/bloub)（MIT，© 2026 Jérémy Perret，许可证见 `licenses/bloub-MIT.txt`）：
`agent_bridge/bloub-icons.json` 是用它自己的引擎导出的静态路径与 idle 动画帧（`tools/gen_bloub_icons.ts`，约 84KB），网页里用 CSS 播放，不带它的程序。

电脑上是三栏：**Claude 会话列表和 Codex 会话列表分在左右两侧，跟着两个桌面窗口在屏幕上的左右位置摆**
（Claude 窗口在左，Claude 列表就在左；把窗口换边，网页几秒内跟着换），中间是会话内容 / 总线消息，任务看板在右栏下面。
找不到窗口（没开、最小化、非 Windows）时缺省 Claude 在左、Codex 在右。手机上是底部导航，不分左右。

**主 / 辅 标志**：按最近 24 小时的派活关系给会话标「主」（派活的一方，行首一条蓝线）和「辅」（接别人派的活的一方，或被某个会话认领的 Codex 对话），
行下写着「派出 N 件在做」「替 X 干活（N 件没交）」这类说明；两样都有时按现在还没回来的活判。会话视图顶部、标签页上也带标志。

**手动设主 / 辅**：会话视图顶上「主 / 辅 / 自动」三选一，或点会话列表里的标志循环切换（没有身份的行显示淡色「设」）；手动设的带虚线框、优先于自动判断，选「自动」回到按派活关系判断。改了之后总线会给那个会话发一条说明（主会话负责统筹派活、辅会话负责接活干活），它下次开会话时注入里也带着；命令行 `bus.py set-role <会话> 主|辅|自动`（`--quiet` 只改标记不通知）。

**刷新**：顶栏 ⟳ 把会话、消息、任务和当前打开的会话记录一起重读；两侧会话列表标题上的 ⟳ 同样；会话视图顶上的 ⟳ 从头重读这个会话的记录。

**网页信件动画**：新消息到了，网页里有一封信从发件会话那一行沿弧线飞到收件会话那一行（群发落在那一栏的标题上），落地那一行闪一下；
手机上没有两侧栏，就从 Claude / Codex 所在的那一侧屏幕边飞进飞出。⋯ 菜单「网页信件动画」开关（手机也能用，只影响这台设备）；
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
它会当作经总线转来的话，风险大的事可能还要你在 Claude 里确认。

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
手机扫码后拿到 30 天的设备令牌；局域网来的请求没有有效令牌一律拒绝；手机只能看、发消息、派活，
安装 / 卸载 / 配对 / 设置只认电脑本机。对话框里能逐台移除手机、关闭手机访问。
第一次打开时 Windows 防火墙可能问 Python 能否联网：选「允许（专用网络）」。

⚠ 能打开这个页面的设备就能给你电脑上有完整权限的 AI 会话派活 —— 配对令牌就是这道门，别关掉它。

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

## 切换 Claude 账号后会话「不见了」

Claude 桌面端的会话列表按「账号 / 组织」分文件夹记（`<Claude 数据目录>/claude-code-sessions/<账号>/<组织>/local_*.json`），
只显示当前登录的那个；对话内容本身在 `~/.claude/projects/`，本地、不分账号，切账号不会丢。

    python -m agent_bridge claude-sync --list      # 各账号 / 组织下有多少会话
    python -m agent_bridge claude-sync --dry-run   # 看看会补进来哪些
    python -m agent_bridge claude-sync             # 补齐到当前账号（最近有会话在写的那个文件夹）

或界面 ⋯ →「🔄 同步 Claude 会话到当前账号」。只补缺的，不覆盖、不删除；对话记录已不在的跳过；写之前整个目录备份到
`~/.agent-bridge/claude-sync-bak/`。**补完要重启 Claude 桌面端**才会出现在列表里（它只在启动时读一次）。
会话里绑过的 PR、远程控制等是跟原账号走的，换账号后可能不可用；对话本身能接着聊。

## 数据放哪

- 程序本体：本仓目录。
- 每个用户的数据：`~/.agent-bridge/`（登记的项目、设置、配对过的手机；可用 `AGENT_BRIDGE_HOME` 改）。
- 每个项目的：`<项目>/.msgbus/`（信箱 `messages.jsonl`、在线记录、读到位置、交接导出、备份、可选 `rules.md`）。

## 卸载

界面「卸载」或 `python -m agent_bridge uninstall <项目目录> [--purge]`：去掉两边的 msgbus 钩子（改前备份），`--purge` 连消息记录一起删。

## 已知限制

- 信件动画、按窗口定位只在 Windows 上有。
- 「Codex 开没开」按 `codex … app-server` 进程判断（Windows 用 WMI，其他系统用 `pgrep`）。
- Claude 没有外部注入用户输入的接口，网页里对 Claude 的「回复」是经总线送达。
- 读会话记录依赖 Claude Code / Codex 本地记录文件的格式，它们升级改格式时可能要跟着改。
