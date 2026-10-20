# Windows 与酒馆兼容说明

本分支基于 [上游 f2c51b8](https://github.com/chrysoljq/aistudio-api/commit/f2c51b816910007fe143e14626bba5cbac7cb0de)，保留已确认需要的兼容修改，并修复登录完成检测误判。

## 使用方式

1. 在 Windows 上安装 Python 3.11 或更新版本，双击本目录的 `start.bat`。
2. 服务地址为 `http://127.0.0.1:8080`；在该页面管理账号。
3. 酒馆使用 OpenAI 兼容接口，API 地址填写 `http://127.0.0.1:8080/v1`。
4. 直接从酒馆网页发送请求的插件，也可使用这个 API 地址。插件如果自行添加 `/v1`，则只填写服务地址。

当前 `master` 副本位于 `E:\游戏文档\酒馆\antigravity\aistudio-api-master\new_fork_master`，使用该目录中的 `start.bat` 启动。相邻的 `new_fork` 是 `beta` 副本，`new_fork_debug` 是调试副本。原 C 盘目录已迁移，账号和未提交修改一并保留。

原目录在 C 盘改名为 `C:\Users\noz\.codex\worktrees\aistudio-upstream-compat\new_fork-migrated-backup`，只作恢复备份；其中的启动脚本和 Git 关联文件已改名禁用，日常使用 E 盘目录。

另一个程序正在占用 8080 端口时，应先关闭该程序，再启动此副本。每个副本在自己的 `data/accounts` 下保存账号和浏览器资料。

## 保留的兼容

| 内容 | 实际行为 |
| --- | --- |
| Windows 启动 | 提供双击启动脚本；启用 Python UTF-8；禁用 Cloakbrowser 自动更新，与此前启动方式一致 |
| Windows 文件与终端 | 缺少 `termios` 时仍可导入登录服务；配置和账号文件按 UTF-8 读取；保存图片前创建临时目录 |
| 酒馆思考显示 | OpenAI 完整回复和流式回复都使用 `reasoning_content` 输出思考内容 |
| 网页与桌面跨域访问 | 默认允许本地 HTTP/HTTPS 网页、Tauri/Electron 等本地桌面来源和 `null` 来源；允许请求携带 Authorization、Content-Type 等头 |
| 过滤分类容错 | Gemini 请求中未知的内容过滤分类会被跳过；支持的分类和阈值仍正常处理，无效阈值仍报错 |
| 等待时间 | 普通请求保持 120 秒；流式请求默认为 900 秒（15 分钟） |

跨域允许网页读取 API 回复，不会免除 API 密钥校验，也不参与 Google 登录。

如酒馆网页使用其他地址，在本目录的 `.env` 中设置来源，例如：

```dotenv
AISTUDIO_CORS_ORIGINS=http://192.168.1.10:8000,https://tavern.example.com
```

只填写网页来源，即协议、主机和端口；不要填写 API 路径。修改后重启服务。默认放行 `null`、HTTP/HTTPS 的 `localhost`、`127.0.0.1`、`[::1]`、`tauri.localhost`，以及 `tauri://localhost`、`app://localhost`、`electron://localhost`、`capacitor://localhost`、`ionic://localhost`，包括各自的不同端口。

## 模型默认配置

`config.yaml` 的配置值采用已确认可用的版本：

| 模型类别 | 默认工具 | 过滤参数 |
| --- | --- | --- |
| Gemma 文本 | Google 搜索 | 四个分类均为 4，即 BLOCK_NONE |
| Gemini 文本 | 不额外添加搜索 | 四个分类均为 4，即 BLOCK_NONE |
| 生图 | 不额外添加网页或图片搜索 | 不发送这组过滤参数；保留 image_only 和 MINIMAL 等原有图片配置 |

客户端可以显式传入工具或生成参数。参数合并仍使用上游规则，包括 Gemini 默认内置工具的合并与去重、空工具数组禁用工具。

## 核心流程与验证边界

登录完成检测仅接受主窗口进入 AI Studio、出现可见聊天输入框，并具有 Google 登录凭据；验证码子页面不会触发账号保存或关闭登录窗口。保存账号不会直接将它标记为激活，网页随后调用启用接口。启用成功后才更新激活记录；失败时恢复原账号，恢复也失败则清除激活记录。网页分别显示账号保存和启用的结果，并在账号管理页每 5 秒更新状态；状态读取失败时显示“未知”。

生成请求、浏览器预热、Cookie 注入和账号切换按同一个浏览器操作锁顺序执行，避免一条请求处理中途换账号。初始化失败的浏览器会关闭，请求模板使用 BrowserSession 已有的缓存，随切号或页面重建清除，避免另一层缓存保留原账号的请求信息。`session.py` 的 Windows UTF-8 读取，以及 `chat_service.py` 的图片目录创建和未知过滤分类跳过仍然保留。其余请求发送、回复解析及工具历史转换沿用上游。

AI Studio 页面加载先等待页面主体，再检查网页内部功能和可见、可用的聊天输入框，不再等待所有网络请求停止。正常跳转或导航等待超时时，会继续检查最终页面；登录失效或页面一直未就绪则明确报错。Chromium 准备认证资料后统一加载一次聊天页，页面及请求拦截准备成功后才输出 `profile hit`。

每个浏览器资料目录中的 `.aistudio-fingerprint-seed` 保存该账号使用的浏览器识别编号。搬迁时保留原编号，之后随浏览器资料一起移动，避免仅因换目录就改变浏览器身份。

上游自带的两处工具测试仍然检查旧行为，本分支将它们调整为当前上游的工具结果文本标记、默认工具合并，并补充相关兼容验证。自动化验证命令：

```powershell
python -X utf8 -m pytest -q
```

这些自动化测试检查本地接口与请求处理，不代表 Google 一定接受某份已保存的登录状态。若服务已启动、浏览器却跳转到 Google 登录页，应在此副本中重新登录；不要覆盖其他副本的账号资料来反复尝试。

流式等待时间已按用户要求改为 15 分钟。需要调整时，在 `.env` 中设置 `AISTUDIO_TIMEOUT_STREAM`（秒）并重启服务；普通请求的 `AISTUDIO_TIMEOUT_REPLAY` 仍为 120 秒。

## 多副本使用

双击哪个目录中的 `start.bat`，就会启动哪一份副本。启动脚本会显示完整项目目录，方便核对。

默认各副本使用自己的 `data/accounts`，但 `AISTUDIO_ACCOUNTS_DIR` 或 `AISTUDIO_AUTH_FILE` 可以指定其他目录。如果多份副本指向同一账号目录，账号资料的改动会相互影响。

使用同一 Python 环境的副本会共用依赖库，CloakBrowser 默认也会共用用户目录下 `.cloakbrowser` 中的浏览器程序。修改共用依赖或更新浏览器程序，可能影响其他副本下次启动。相同 HTTP 端口只能同时由一份服务占用。

上传仓库时只提交程序、配置示例和测试；实际 `.env`、`data/` 中的账号资料及诊断备份均由忽略规则排除，应保留在本机。
