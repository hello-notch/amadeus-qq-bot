# Amadeus QQ Bot

新版机器人采用 NapCat + NoneBot 2 + OneBot V11。设计方案中的本期功能已经全部接入；明确暂缓的重启/关闭、MATLAB，以及需要开发者提供有效校园登录会话的在线抓取除外。

## 当前能力

- SQLite 权限、功能开关、忽略规则、审计和推荐池。
- 统一命令注册表与按权限过滤的帮助图片。
- 大段文本图片渲染及内容寻址缓存。
- 多供应商 AI 路由、按供应商选择 Chat Completions/Responses 协议、可持久化的 `/model` 模型切换、任务回退和 token/延迟统计。
- `help`/`/help`、角色聊天、主动接话、视觉描述缓存、wife、贴表情、戳一戳和安全 Markdown。
- DDL、课程表（班级/CSV/XLSX/手动）、推荐池与 DDL 临时候选、真实私聊提醒调度。
- 群统计、群总结、语录、跨群用户记忆、隐私退出及开发者记忆处理申请。
- `qa`、`ql`、`qf`、`qs`、`qr`、`qe`、`qd` 是语录各子命令的简写；语录删除仅限 SUPERUSER。普通 `help` 不展示管理员命令，管理员使用 `help superuser` 查看全部命令。`memory` 和 `privacy` 命令暂限 SUPERUSER。
- 回复群内文字、图片或语音消息后使用 `/quote add [名称]` 收藏；语音文件保存在对应群的 `data/groups/<群号>/media/quotes/`，展示时发送 QQ 语音消息段。每个媒体限 10 MiB；NapCat 语音段无法直接下载时使用 `get_record` 转为 MP3 保存。语音收发仍需在真实 QQ/NapCat 环境验收。
- `ql/qf` 仅展示语录 ID 和名称，不展示文字内容、图片或语音；`qs <id>` 或 `qr` 才发送语录原文与媒体。SUPERUSER 使用 `qd <id>` 直接删除，无需二次确认或确认码。
- 信息门户、第二课堂只读查询/订阅/每日推送、数据源健康状态和管理员导入降级方案。
- 权限成员管理、功能开关、日志诊断、备份/保留策略、健康和 AI 配额命令。
- AI 可代表当前请求者调用 DDL、课程、校园查询/订阅、统计上下文、wife、群成员身份/最近消息查询、多目标戳一戳和多消息贴表情等白名单工具；SUPERUSER 工具不进入 AI 注册表。
- 群聊主动搭话默认开启，仅在相关话题或提问通过判定后接话，单群最多 10 分钟两次且至少间隔 60 秒；管理员可用 `/feature disable proactive_chat <群号>` 关闭。
- 聊天可读取当前、被回复或同一发送者紧邻上一条的图片；文件正文仅支持 QQ HTTPS 链接提供的 UTF-8 文本、Markdown、CSV、JSON 和 ICS（最多 256 KiB，提交给模型时截取前 6000 字）。其他文件只显示名称，不声称已读内容。

## 本地启动

修改功能后的本地会话测试可启动不连接 QQ/NapCat 的模拟器：

```powershell
.\.venv\Scripts\python.exe -m amadeus_bot.simulator
```

打开 `http://127.0.0.1:18765`，选择模拟群聊或模拟私聊，并切换测试管理员、小林、小周发送消息。支持回复、@、表情、图片消息段和戳一戳事件；输出图片仅预览模拟数据目录内的渲染结果。模拟器使用 `.test-tmp/simulator/` 内的独立数据、日志、备份及问题快照，Bot 占用 18080 端口，网页占用 18765 端口；可用 `SIMULATOR_BOT_PORT`、`SIMULATOR_UI_PORT` 调整。关闭时 Ctrl+C。校园查询及手动刷新可读取项目配置的会话文件，抓取结果只写入模拟数据目录；模拟器不会自动续登或改写真实 Cookie/token，过期时须先在正常运行环境更新会话。AI 默认禁用；启动前设置 `SIMULATOR_ENABLE_AI=true` 可使用项目已配置的 API key 进行真实调用，可能产生费用。未知 OneBot API 明确失败。模拟结果不能代替最终 NapCat/QQ 验收。

项目通过 `pyproject.toml` 的 `tool.nonebot.plugin_dirs` 让 nb-cli 自动发现插件，因而可直接运行：

```powershell
nb run
```

长期运行默认不启用 `--reload`；开发时可手动使用 `nb run --reload`。

也可以使用项目打包入口：

```powershell
uv sync --extra dev
Copy-Item -LiteralPath .env.example -Destination .env
uv run amadeus-bot
```

帮助图片只使用与项目 Playwright 版本匹配的 Chromium，不会调用系统 Edge 或 Chrome。首次部署需要执行 `uv run playwright install chromium`。

NapCat 的 OneBot V11 反向 WebSocket 地址应指向 NoneBot 配置的地址。各供应商 API key 默认从受保护且被 Git 忽略的 `secrets/apikey.txt` 读取；文件按 `apikey=...`、`url=...` 成对配置，可保存多组凭据。

NapCat 是独立的第三方运行时，不随本仓库提交二进制文件。参照
[`napcat/README.md`](napcat/README.md) 下载官方发行包并复制脱敏配置模板；Windows 下可在设置
`NAPCAT_DIR` 与 `QQ_UIN` 后运行 `run-windows.cmd` 同时启动两端。启动后 NapCat 与 NoneBot
日志显示在同一个托盘控制台中；关闭或最小化窗口只会隐藏到系统托盘，单击托盘图标恢复，
右键选择 `Exit completely` 才会结束两个进程。直接双击 `run-windows-hidden.vbs` 可避免启动时短暂出现命令行窗口。

启动时会显式加载项目 `.env`，使 NoneBot 配置和使用 `os.getenv` 的业务服务读取同一组值；操作系统中已经存在的环境变量优先。

暂不阻塞基础运行的后续验收事项见 [`AGENTS.md`](AGENTS.md) 第 12 节。
模型路由见 `config/ai_routes.toml`；帮助、推荐池、数据库和日志的开发约定见
[`AGENTS.md`](AGENTS.md)。

## 额外配置

- `AMADEUS_PORTAL_COOKIE_FILE`：信息门户专用受保护 Cookie 请求头文件路径。
- `AMADEUS_ACTIVITY_TOKEN_FILE` 与 `AMADEUS_ACTIVITY_LIST_ENDPOINT=/api/v1/activity`：第二课堂只读 Bearer token 文件与学生端活动列表。自动续登失败时可由 SUPERUSER 回复 JSON/CSV 使用 `/activity import-file`。
- `AMADEUS_JWGL_COOKIE_FILE`：教务系统专用受保护 Cookie 请求头文件路径。原始请求头会保留同名 Cookie 的顺序，例如教务系统可能同时发送两个不同 Path 的 `JSESSIONID`。
- `AMADEUS_SEMESTER_START=YYYY-MM-DD`：本学期第 1 周周一，用于本周课表和课程提醒；当前安卓邮学伴的第 4 周为 2026-09-21 起，因此本地设置为 2026-08-31。
- `AMADEUS_CAMPUS_PUSH_HOUR=8`：校园每日推送小时。

这些文件只保存可失效的短期会话副本，不保存账号密码，也不得提交；登录失效后由开发者刷新。配置脚本会尝试把 `secrets/` 的 Windows ACL 限制为当前用户和 SYSTEM。
机器人默认每 300 秒访问一次信息门户和教务系统以保持闲置会话，并检查第二课堂；可用 `AMADEUS_CAMPUS_REFRESH_SECONDS` 调整（最低 60 秒）。首次失败和恢复时会私聊 `SUPERUSER`，不会把凭据写入消息或日志。
若配置 `AMADEUS_PASSWORD_FILE`，会话失效时机器人会使用 Playwright Chromium 打开真实登录页，读取文件第一行账号和第二行密码完成登录，更新原始 Cookie 请求头并重试一次。凭据内容不会写入日志；遇到验证码或登录策略变化时停止重试并报告失败。

信息门户、第二课堂和教务续登默认都使用无头 Playwright Chromium，不会显示浏览器窗口。若学校网站临时拒绝无头浏览器，可仅在排查期间把对应的 `AMADEUS_PORTAL_BROWSER_HEADLESS`、`AMADEUS_ACTIVITY_BROWSER_HEADLESS` 或 `AMADEUS_CAMPUS_BROWSER_HEADLESS` 设为 `false`；此时续登会短暂显示浏览器窗口。三者都只使用 Playwright 安装的 Chromium，不调用系统 Edge 或 Chrome。

### 配置校园登录凭据

不要把 Cookie 或 token 发到聊天，也不要把它们直接写入 `.env`。运行：

```powershell
.\.venv\Scripts\python.exe .\scripts\configure_campus_secrets.py
```

脚本使用隐藏输入，将凭据保存到已被 Git 忽略的 `secrets/`，并输出一行不含凭据的 `.env` 路径配置。

- 信息门户：在已登录的 `my.bupt.edu.cn` 页面打开开发者工具 → Network，刷新页面，选择发往 `my.bupt.edu.cn` 而不是 CAS 登录页的请求，在 Request Headers 中复制完整 `Cookie` 值，选择脚本选项 1。
- 教务系统：在已登录的 `jwgl.bupt.edu.cn/jsxsd` 页面以同样方式复制请求的完整 `Cookie` 值，选择选项 2。
- 第二课堂：在已登录的 `dekt.bupt.edu.cn` 页面打开 Network，筛选 `api/v1`，选择加载活动列表的 GET 请求，复制 `Authorization: Bearer ...` 的值，选择选项 3。同时把该请求 URL 中域名后的只读路径写成 `AMADEUS_ACTIVITY_LIST_ENDPOINT=/api/v1/...`。

信息门户和教务 Cookie 到期后可重新运行脚本覆盖对应文件。第二课堂 token 缺失或到期时，如果已配置 `AMADEUS_PASSWORD_FILE`，机器人会通过官方网站的登录页自动续签并原子更新 token 文件；遇到交互验证码或登录策略变化时停止重试并报告失败，也可重新运行脚本手工更新。机器人只读取这些凭据进行查询，不实现报名、签到或退选请求。

`/course` 和 `/course show` 显示本周课表图片，连续节次合为一个课程矩形。`/course import <班级号>` 导入班级课表，回复教务课表文件后发送 `/course import` 导入文件；成功解析后会清空并替换本人原课表（包括手动添加的课程），失败时保留原课表，不需二次确认。按班级号导入可能包含多余课程，请用 `/course list` 核对；确有不修读的课程时用 `/course delete <ID>` 删除，没有则无需操作。班级课表冲突课程不会进入课表或提醒，使用 `/course conflicts [页码]` 查看课程名、教师和 ID。可回复机器人发出的冲突提示图片，仅发送一个或多个 ID；也可发送 `/course choose <ID...>`。每位用户只保留最新一张冲突提示图片的消息 ID，回复有效期为 24 小时；新图片发出后旧图片不能再用于选课。多个 ID 可用空格、中英文逗号分隔，同一批次互相冲突则整体拒绝。`/course delete <ID...>` 批量删除，`/course delete all` 清空本人课表及待选择课程。`/portal` 显示最近 10 条通知图片，`/portal 2` 至 `/portal 5` 查看后续页，`/portal <wbnewsid>` 才以文字发送可点击的详情链接。`/activity` 显示已抓取活动图片；`/health` 显示门户、第二课堂最近抓取状态与北京时间更新记录。

## 安全约束

- `apikey.txt`、`.env*`、`data/`、`logs/`、`issues/` 和 NapCat 账号配置禁止提交。
- SUPERUSER 命令不会注册为 AI 工具。
- AI 的 MEMBER 委托能力只对显式白名单工具和当前请求生效。
- 第二课堂只读查询与推送，永不自动报名、签到或退选。
