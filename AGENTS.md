# Amadeus QQ Bot 开发与验收约定

本文适用于 `new_bot/` 及其全部子目录，是开发者与代码代理共同遵循的项目约定。它记录当前真实入口、架构边界、权限模型、专项工作流和验收标准。若文档与可验证的代码行为不一致，应以事实为准，并在同一任务中同步修正文档。

项目运行于 Windows，主要技术栈为 NoneBot 2、OneBot V11、NapCat、SQLite、Playwright Chromium 和多供应商 AI 路由。

## 1. 一页式工作流

所有改动遵循同一条闭环：

**确认范围 → 建立基线 → 分层定位 → 定义契约 → 最小实现 → 回归测试 → 分级验证 → 对齐运行状态 → 收尾交付**

1. **确认范围**：阅读本文件、`README.md`、`docs/customization.md` 和 `docs/deferred-issues.md`；确认用户要的是解释、诊断、实现还是持续运行。
2. **建立基线**：检查现有修改、相关配置是否存在、机器人进程与 8080 监听、最近日志和数据库健康状态。只记录必要的状态，不输出凭据值。
3. **分层定位**：用 `rg --files`、`rg -n` 串起 matcher、`CommandSpec`、service、repository、数据库、调度任务、日志和测试；按“用户输出 → 健康状态 → 外部响应 → 解析 → 缓存 → 展示”逐层确认，不凭文件名或单个状态码猜根因。
4. **定义契约**：写清参数、输出、权限、用户/群作用域、功能开关、AI 可调用性、外部失败、空结果、重试和幂等行为。
5. **最小实现**：只修改根因所在层。持久化进入 `repositories`，业务逻辑进入 `services`，OneBot 协议适配留在 `plugins`；matcher 保持轻量。
6. **补回归测试**：覆盖根因和边界，而不只断言最终文案。真实事件形状、时间边界、权限隔离、鉴权失败、外部空结果、重试次数和错误结果都应有测试。
7. **同步事实来源**：命令变化更新 `CommandSpec`；模型变化更新 `config/ai_routes.toml`；角色事实更新 `persona/`；运行方式或外部认证变化同步 README；未完成的真实验收写入延后事项。
8. **分级验证**：依次执行静态检查、单元测试、专项探针、真实 refresh、启动冒烟和真实 QQ 验收。后一级不能用前一级的结果代替。
9. **对齐运行状态**：代码修复后检查生产缓存、`source_health`、会话/token 文件和最终用户文案；测试库或临时探针成功不等于生产状态已经恢复。
10. **收尾交付**：确认未泄露凭据、未覆盖用户数据、未遗留进程或临时目录，并分别说明自动化验证、外部探测、启动验证、真实状态变更和待人工验收项。

诊断任务默认先确定根因并报告；只有用户明确要求修复，或请求本身已经包含实现，才修改代码。不要把“单元测试通过”写成“真实 QQ 或外部服务已验证”。

## 2. 开始工作前

- 工作目录：仓库根目录。
- 生产式本地启动：设置 `NAPCAT_DIR` 与 `QQ_UIN` 后运行 `run-windows.cmd`；也可单独执行 `uv run amadeus-bot`。
- 开发启动：在项目目录执行 `nb run`；需要热重载时才使用 `nb run --reload`。
- 保留用户已有修改，不执行破坏性重置、大范围清理或无关重写。
- 启动前检查 8080 端口和现有 Python/NapCat 进程，避免运行两个机器人实例。
- 修改数据库、备份、会话文件或调度逻辑前，先确认机器人是否运行，并评估并发读写风险。

项目已经是 nb-cli 项目。不要再次运行 `nb create`，也不要新建第二套 `bot.py` 入口。

## 3. 不可破坏的原则

1. **单一事实来源**：命令帮助来自 `CommandSpec`，AI 路由来自 `config/ai_routes.toml`，角色事实来自 `persona/`。不要维护会漂移的复制版本。
2. **权限落实到代码**：帮助元数据不能代替 matcher/service 中的检查。SUPERUSER 能力不得进入 AI 工具。
3. **外部结果必须真实**：HTTP 200 可能仍是登录页、错误页或业务失败；必须检查最终 URL、页面特征、数据形状和合理条目数。
4. **凭据永不外泄**：密码、Cookie、Bearer token、API key 和完整敏感请求参数不得进入聊天、补丁、测试快照或日志。
5. **作用域严格隔离**：群数据、用户数据和全局数据必须存放在正确位置；临时会话私聊不能访问群作用域功能。
6. **错误与空结果分开**：鉴权失败、页面变化和解析失败不能显示成“暂无数据”；只有成功请求得到空集合时才能报告业务为空。
7. **显示问题先核对编码**：PowerShell 捕获、终端编码和字体可能制造乱码假象。必要时检查原始字节、`ascii()`、Unicode 转义、数据库内容和最终图片。
8. **重要行为可追溯**：外部调用、AI 工具、调度投递和诊断操作应留下结构化状态或可读日志。

## 4. 项目结构与数据作用域

- `src/amadeus_bot/domain/`：权限、命令、消息和 AI 领域模型。
- `src/amadeus_bot/repositories/`：SQLite 初始化、迁移和数据访问。
- `src/amadeus_bot/services/`：DDL、课程、权限、记忆、AI、渲染、统计、校园源和问题快照等业务逻辑。
- `src/amadeus_bot/adapters/`：AI 等外部供应商适配。
- `src/amadeus_bot/plugins/`：NoneBot 命令、监听器和调度入口。
- `src/amadeus_bot/persona/`：Amadeus 角色事实、语言风格和运行边界。
- `config/ai_routes.toml`：AI 任务路由、回退、温度和 token 上限。
- `data/core.sqlite3`：权限、开关、审计、订阅、校园缓存和 AI 用量。
- `data/users/<QQ号>/user.sqlite3`：跨群共享的用户记忆、DDL、课程和个人设置。
- `data/groups/<群号>/group.sqlite3`：wife、群语录等群作用域数据。
- `data/cache/render/`：帮助和长文本图片缓存。
- `logs/`：活动日志、规范化消息和运行日志。
- `issues/`：`/issue` 生成的本地问题快照和人工放入的复现材料。
- `backups/`：由 `/data backup` 管理的数据备份。
- `scripts/`：AI、渲染、校园源和凭据配置探针。
- `tests/`：仓储、服务、权限、配置、协议边界和 AI 工具回归测试。

群内关系不得写入全局用户库；跨群记忆不得拆入各群数据库。展示跨群记忆时，不披露来源群、其他参与者或不必要的原话。

## 5. 启动、配置与插件发现

### 5.1 唯一启动入口

`pyproject.toml` 是 nb-cli 插件发现入口：

```toml
[tool.nonebot]
plugin_dirs = ["src/amadeus_bot/plugins"]
builtin_plugins = []
```

`amadeus-bot` / `python -m amadeus_bot` 是备用打包入口。新增或删除业务插件时，必须同步检查 `app.py` 的备用加载列表。

`plugin_dirs` 会把目录中的 Python 模块都视作插件候选。协议无关的公共代码应放入 `services` 或其他普通包，不要堆进 `plugins`。

### 5.2 启动成功标准

“8080 正在监听”只能证明 Web 服务启动，不能证明机器人可用。启动冒烟必须同时确认：

- `help`、`chat`、`scheduler`、`message_logger`、`issue_report` 等业务插件成功加载；
- OneBot V11 适配器已加载；
- 数据库初始化和帮助图片预热完成；
- 出现 `Application startup complete`；
- 没有配置、调度器或渲染初始化错误；
- Ctrl+C 后出现完整的应用关闭日志，且没有残留机器人进程。

### 5.3 `.env` 与凭据

NoneBot Config 中存在字段，不代表 `os.getenv()` 一定能读取。项目统一通过 `amadeus_bot.settings.load_project_env()` 加载 `.env`：

- `app.create_app()` 在备用入口初始化前加载；
- `AppPaths.discover()` 覆盖 nb-cli 自动发现和独立脚本场景；
- 操作系统环境变量优先，`.env` 默认不覆盖它们。

不要在单个插件中重复调用 `load_dotenv()`。

`.env` 只保存路径和非敏感配置。API key、校园 Cookie、第二课堂 token 和密码文件放入 `secrets/` 或配置指定的受保护路径。`secrets/`、`.env*`、`data/`、`logs/` 和本地数据库不得提交。

Cookie 必须保留原始请求头顺序，不能用字典表示可能同名、不同 Path 的 Cookie。探针只输出状态、模型、条目数和脱敏错误，不打印完整请求头、token 或响应正文。

## 6. 分层、OneBot 与作用域

### 6.1 分层规则

- matcher 只处理事件适配、参数拆解、权限/功能开关和输出。
- 时间解析、外部访问、课程解析、权限边界等复用逻辑进入 `services`。
- SQL、迁移和数据查询进入 `repositories`。
- service 不得反向依赖 plugin。
- 数据库迁移必须幂等，并与初始化代码和测试同时更新。

不要复制时间解析、权限判断、回复目标提取、长文本渲染、数据主体选择或外部错误处理。

### 6.2 OneBot 事件规则

- 只有 `message_type == "group"` 才是群聊。NapCat 临时会话私聊可能带辅助 `group_id`，不能据此进入群作用域。
- OneBot V11 会把 reply 段从 `event.message` 取出，并把源消息放入 `event.reply`。统一使用 `services/event_utils.py` 的 `reply_message_id()`。
- NapCat `get_msg` 对单文件消息可能返回裸字典，对多段消息返回列表。统一使用 `onebot_message()` 规范化，不要直接 `Message(detail["message"])`。
- 文字、at、图片、mface、语音、文件、poke 和消息表情回应是不同消息段或事件。新增处理前先核对真实事件形状。
- 事件辅助函数放在 `services/event_utils.py`；`plugins/common.py` 只做兼容性重导出。

## 7. 命令、权限与 AI 委托

权限语义以 `domain/permissions.py` 为准：

- `EVERYONE`：普通用户可调用。
- `MEMBER`：由 SUPERUSER 维护的额外受信任角色，不等于任意群成员。
- `SUPERUSER`：开发者管理权限。
- `SELF_OR_SUPERUSER`：用户只能管理本人数据；SUPERUSER 可用明确参数选择数据主体。
- `OWNER_OR_MEMBER`：资源所有者或 MEMBER 可管理；使用前必须确认 service 已实现真实所有权校验。

新增或修改命令时：

1. 在 `CommandSpec` 写明命令名、别名、说明、完整用法、权限、功能开关、示例、注意事项和 AI 可调用性。
2. 在 matcher/service 中执行真实权限检查，不能只依赖 `CommandSpec`。
3. `ai_callable=True` 仅表示可成为候选；还必须在 `services/tools.py` 显式注册 schema 和执行器。
4. `ai_delegate_member=True` 只允许当前请求上下文中的明确白名单能力，不得让 AI 获得 SUPERUSER 权限。
5. AI 只能代表当前请求者操作当前请求者的数据，不能通过参数切换成无关用户。
6. 记忆查看、修改和删除仍由用户申请、SUPERUSER 人工处理，AI 不直接操作长期记忆。

## 8. 专项工作流

### 8.1 AI 与角色

- 按 `AITask` 区分聊天、工具规划、主动接话、总结、统计分析、记忆候选、视觉和复杂推理。
- 优先使用任务配置中的低成本模型；高成本模型只用于复杂任务或回退。
- 主动接话先经过确定性规则和限频，边界情况再调用低成本 `proactive_gate`。
- 调整模型后运行 `scripts/probe_ai.py`；涉及工具调用时再运行 `scripts/probe_tool_call.py`。这些探针会产生真实调用和少量费用。
- 角色事实以 `persona/canon.md` 为准，语言风格看 `style.md`，权限和工具边界看 `runtime.md`。不确定的剧情保持未知。

### 8.2 校园服务

校园功能仅用于查询、缓存、订阅、课程导入和推送：

- **信息门户**：校内通知。列表必须用文字发送，确保详情链接可点击；历史页面存在 UTF-8/GB18030 双编码，登录控件可能位于 `#loginIframe`。
- **教务系统**：班级课表查询、个人课表导入和会话保活。`import-file` 支持 CSV，以及教务系统直接下载的 `.xls/.xlsx`；自制或另存表格不保证可解析。
- **第二课堂**：只读活动列表和订阅，默认功能开关开启。Bearer token 缺失或返回 401/403 时，可用已配置的密码文件自动续签并仅重试一次。只有接口成功且返回空列表时，才能回复“当前第二课堂没有活动”；鉴权、续签或解析失败必须明确报告数据源错误。

修改校园服务时：

1. 检查环境变量是否经统一加载器生效，只报告配置和凭据文件存在/缺失，不输出值、长度可推断内容或完整错误响应。
2. 先看 `source_health` 与最近日志，再用 `scripts/probe_campus_sources.py` 做脱敏探测；区分配置未加载、凭据过期、接口变化、响应形状变化、解析失败和缓存未更新。
3. 使用项目 Playwright Chromium。信息门户和第二课堂按现有配置可在会话失效时短暂使用有界面模式；不得改用系统 Edge/Chrome 绕过项目约束。
4. 第二课堂先调用官方账号密码会话接口；服务端要求网页验证时，使用官方网站登录页完成同一流程。兼容启动参数只用于消除 Playwright 默认自动化标记造成的“不支持该浏览器”，不得绕过真实验证码或安全拦截。
5. 第二课堂续签成功后原子替换 token 文件，再以新 token 重试原只读请求一次。新 token 缺失、未变化、仍返回 401/403 或登录失败时立即停止，不循环弹窗或反复提交密码。
6. 自动登录遇到交互验证码、页面变化、账号密码错误或连续失败时停止重试并通知 SUPERUSER；错误信息不得包含密码、Cookie、token 或带敏感参数的完整 URL。
7. 门户统一使用 `decode_portal_html()`；不要根据终端直接显示的中文判断编码。
8. 同时验证最终 URL、登录状态、HTTP/业务状态、响应形状、字段含义和合理条目数；HTTP 200 本身不是成功证据。
9. 当前活动刷新代表“当前列表”快照；成功刷新后应清理已不在快照中的旧活动。失败时不得清空最后一次有效缓存。
10. 探针默认使用临时数据库，但第二课堂探针可能续签配置指向的真实 token 文件。运行前确认凭据路径和机器人进程状态；探针成功后仍需对生产 repository 执行真实 refresh，并检查 `source_health`、缓存和最终消息。
11. 第二课堂真实活动字段只在确有活动时与学生端对照；未决事项写入 `docs/deferred-issues.md`。永远不实现或探测报名、签到、退选接口。

第二课堂 401 的推荐修复闭环为：

**日志确认 401 → 脱敏探针确认服务端错误 → 只读检查公开前端认证契约 → 为续签与单次重试补测试 → 实现 API/网页验证两级续签 → 真实探针更新 token → 刷新生产缓存和健康状态 → 启动冒烟 → 清理临时进程与目录**

公开前端研究只读取页面和静态脚本，不读取用户浏览器配置、历史会话或既有存储；认证请求体、响应形状和 token 保存规则必须有公开脚本或真实脱敏响应作为证据，不能靠猜测实现。

### 8.3 DDL 与课程

- DDL 使用北京时间，支持“2分钟后”“明天下午三点”“23:59”和 ISO 时间。
- `reminder=at` / `--remind at` 表示到截止时间提醒；`30m`、`1小时` 表示提前量；`off` 仅在用户明确要求不提醒时使用。
- 省略提醒时默认提前 1 小时；创建时距截止不足 1 小时则不设置默认提醒。
- 调度修改必须检查时区、重复投递、进程重启、无 Bot 连接和正常关闭。
- 课程 `import-class` 使用精确班级号查询；不能依赖教务站当前失效的联想搜索接口。
- 课程文件导入先生成预览 token，用户确认后才写入数据库；`.xls/.xlsx` 必须来自教务系统直接下载的个人课表。
- 课程提醒依赖 `AMADEUS_SEMESTER_START` 和节次时间配置，并通过私聊发送。

### 8.4 Stick、消息输出与渲染

- 成功贴表情后保持静默，不再额外发送“已贴表情”。
- `/stick list [起始ID]` 每次展示 20 个连续 ID 与对应 QQ 表情消息；不再使用会错误推断表情含义的 probe 流程。
- 只有在目标 NapCat/QQ 版本人工确认后，才把名称、别名和 `verified` 状态写入 `data/shared/emoji_ids.json`。
- 短消息直接发送；帮助、长列表和大段 AI 输出按统一阈值渲染为图片。信息门户列表是文字输出的明确例外，因为链接必须可点击。
- `COMMAND_START` 同时包含 `"/"` 和空前缀。一个 `on_command()` 已覆盖有斜杠和无斜杠形式；不要叠加 `on_fullmatch()` 制造重复回复。
- 帮助只发送图片。启动时预热各权限总览和命令详情；冷缓存渲染失败只返回简短错误，不回退发送整页文字。
- 图片只使用项目 Playwright Chromium；渲染缓存采用内容寻址，内容变化应自然生成新键，不随意清空整个缓存目录。
- 首次部署 Chromium 后运行 `scripts/probe_chromium_render.py`。关闭清理必须幂等，Playwright 已断开时不能让正常停机失败。

## 9. 日志与问题快照

- `logs/runtime/runtime.log`：简化后的框架运行日志；启动阶段保留插件加载信息。
- `logs/activity/YYYY-MM-DD.log`：面向人阅读；同名 `.jsonl` 保存结构化活动记录。
- `logs/messages/<群号>/`、`logs/private/<QQ号>/`：统计使用的规范化原始消息，不能被 `/log` 摘要替代。
- 活动日志应覆盖入站、通知、Bot 输出、AI 选择/结果、工具开始/成功/失败和错误。
- 图片、小文件和表情只记录必要的安全路径、文件 ID 或 URL；凭据和完整敏感参数永不写日志。
- OneBot API 后处理钩子只记录必要字段。AI 工具必须同时记录 `started` 与最终 `success/failed`。
- 调整控制台过滤器后必须真实启动，确认重要异常未被过滤、标签受当前 Loguru 支持、Ctrl+C 能完整关闭。

`/issue [说明]` 仅限 SUPERUSER：

- 回复一条消息时，以被回复消息为时间锚点；不回复时，以命令本身为锚点。
- 快照写入 `issues/<时间-消息ID>/`，包含命令消息、被回复消息、附近规范化消息、同作用域活动日志和同时间段运行日志。
- 快照用于本地复现，不下载或复制附件内容，也不得把凭据写入快照。

## 10. 验证阶梯

### 10.1 离线检查

```powershell
uv lock --check
.\.venv\Scripts\ruff.exe check --no-cache .
.\.venv\Scripts\ruff.exe format --check --no-cache .\src .\scripts .\tests
.\.venv\Scripts\pytest.exe -q -p no:cacheprovider
```

受限环境不能写系统临时目录时，先在项目内创建专用父目录，再给 pytest 使用独立子目录；不要把项目根、`data/`、`logs/` 或用户数据目录作为 `--basetemp`。部分 Windows ACL 会拒绝点号开头的目录，优先使用 `test-tmp/`：

```powershell
.\.venv\Scripts\pytest.exe -q -p no:cacheprovider --basetemp .\test-tmp\pytest-current
```

测试结束后只清理本次创建且已解析确认位于项目内的临时目录；不要用模糊通配符或未校验变量递归删除。

依赖发生变化时先更新锁文件，再检查安装环境：

```powershell
uv lock
uv sync --extra dev
```

### 10.2 启动冒烟

```powershell
nb run
```

按第 5.2 节检查插件、适配器、数据库、帮助预热和 8080 监听，然后使用 Ctrl+C 正常关闭。不要把冒烟实例遗留在后台。历史日志不应为“清爽输出”而删除；以新时间戳判断本次启动结果。

### 10.3 按需探测

```powershell
.\.venv\Scripts\python.exe .\scripts\probe_ai.py
.\.venv\Scripts\python.exe .\scripts\probe_tool_call.py
.\.venv\Scripts\python.exe .\scripts\probe_chromium_render.py
.\.venv\Scripts\python.exe .\scripts\probe_campus_sources.py
.\.venv\Scripts\python.exe .\scripts\probe_campus_sources.py --activity-only
```

只运行与改动相关的探针。`--activity-only` 用于缩短第二课堂认证与活动列表的真实验证路径；它使用临时数据库，但可能更新配置的 token 文件。失败时区分代码缺陷、凭据失效、网页验证、外部服务变化和执行环境限制。

### 10.4 真实 QQ 验收

自动化和启动冒烟通过后，再由开发者运行 `run-windows.cmd` 验证：

- `help`、`/help`、中文别名和无斜杠命令；
- 帮助命令一次只发送一张图片，冷缓存也不先发文字版；
- 私聊、群聊、@、回复、主动接话和临时会话隔离；
- 图片、单文件消息、文件下载、贴表情、戳一戳和广播；
- 门户文字链接、第二课堂空结果/错误结果、课程 `.xls` 导入；
- “两分钟后”等 DDL 相对时间和到时提醒；
- `/issue` 回复模式与无回复模式；
- NapCat 重连、失败提示、调度投递和正常关闭。

空命令前缀会让普通文本进入命令候选。新增无斜杠命令时，要检查是否误匹配自然语言前缀。

## 11. 数据与高风险操作

- SQLite 表结构变化必须同时更新初始化/迁移代码和测试，且迁移可重复执行。
- `/data backup` 只备份 `data/`；restore 必须预检、确认，并保留恢复前快照。
- 不手工删除生产数据库表，不用测试命令覆盖真实 `data/users` 或 `data/groups`。
- 测试不得写入真实用户库、群库、生产缓存或凭据文件。
- 外部写操作、广播、恢复、他人数据和隐私操作必须执行代码级权限检查。
- wife 按北京时间每日 0 点换日。
- 群统计和总结窗口为最近 1–24 小时，默认 1 小时；确定性计数与 AI 结论必须明确分开。

## 12. 暂缓与人工验收事项

- MATLAB 功能暂不实现。
- 重启和关闭命令暂不实现。
- 第二课堂真实活动字段仍需在确有活动时与学生端对照。
- QQ 表情 ID 需要在目标 NapCat/QQ 版本逐批人工验证。
- `persona/canon.md` 仍需开发者审核。

详细状态统一维护在 `docs/deferred-issues.md`；完成后更新或移除对应条目。

## 13. 交付前检查清单

- [ ] 改动范围与用户请求一致，没有夹带无关重构。
- [ ] 启动入口仍是 nb-cli `plugin_dirs`，备用入口的插件列表已同步。
- [ ] `.env` 自定义字段经统一加载器生效，且未输出凭据。
- [ ] 命令参数、输出、权限、作用域、功能开关和 AI 调用信息已同步到 `CommandSpec`。
- [ ] 群作用域依据 `message_type`；reply 和 `get_msg` 数据通过统一事件辅助函数处理。
- [ ] 权限在执行路径中检查，而不只写在帮助元数据中。
- [ ] AI 工具只暴露白名单，不能切换数据主体或执行 SUPERUSER 操作。
- [ ] 外部空结果、鉴权失败和解析失败有不同且可理解的行为。
- [ ] 外部认证重试有明确上限；第二课堂续签不会循环提交密码、绕过验证码或泄露 token。
- [ ] 数据迁移、缓存替换、调度、重复执行和关闭行为有测试或明确人工验收记录。
- [ ] 帮助只由一个 matcher 响应，长文本不会同时发送文字和图片；门户链接仍可点击。
- [ ] 日志覆盖关键状态且不包含凭据；必要时已用 `/issue` 验证快照内容。
- [ ] `uv lock --check`、Ruff、格式检查、pytest、启动冒烟及相关探针通过。
- [ ] 外部服务修复后已对齐生产缓存/健康状态；临时探针成功没有被误报为生产恢复。
- [ ] 没有遗留测试进程、临时机器人实例或不必要的真实数据改动。
- [ ] 交付说明区分自动验证、启动验证、外部探测和待人工验收。
