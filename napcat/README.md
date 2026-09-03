# NapCat 接入

NapCat 是第三方 QQ / OneBot V11 运行时，本仓库不再分发其 EXE、DLL、QQ 文件或账号配置。请从
[NapCatQQ 官方仓库](https://github.com/NapNeko/NapCatQQ)获取与你的 QQ 版本匹配的发行包，并保留其许可证与更新机制。

在 NapCat WebUI 中为当前账号启用 OneBot V11 反向 WebSocket：

- URL：`ws://127.0.0.1:8080/onebot/v11/`
- 消息格式：`array`
- 心跳：`30000` 毫秒
- 重连：`10000` 毫秒
- Token：如需设置，应同时通过本机环境配置给 NoneBot，绝不能提交到 Git

`onebot11.example.json` 是脱敏结构示例。NapCat 的真实配置文件名通常含 QQ 号，且 WebUI 配置中可能包含自动登录账号和访问令牌，因此真实 `config/` 必须留在外部运行目录。

Windows 启动示例：

```powershell
$env:NAPCAT_DIR = 'D:\Path\To\NapCat.Shell'
$env:QQ_UIN = '你的QQ号'
.\run-windows.cmd
```

本项目不会自动下载、修改或提交 NapCat 本体。
