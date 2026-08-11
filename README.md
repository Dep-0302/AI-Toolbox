# AI-Toolbox

本地、只读的 AI 能力检索工作台。它在前台观察本机已有的 `Skill / Plugin / MCP / CLI / SDK` 入口，以及你主动选择的收藏目录；不会安装、启用或执行这些能力。

> Local, observe-only inventory for AI capabilities. No cloud account is required.

## 主要能力

- 按 Codex、Claude、Hermes、WorkBuddy 等宿主观察 Skill 与工具入口。
- 区分“发现、安装、宿主绑定和可用性”，不把文件存在误报为已经可调用。
- 只读索引一个本地收藏目录，支持 ZIP 安全预算、符号链接拒绝跟随和失败关闭。
- 在 `127.0.0.1` 提供 React 工作台，并把运行快照限制在项目内的 `generated/`。
- 提供搜索、筛选、收藏、详情、健康检查和手动刷新。

公开仓库不包含任何作者机器上的能力清单、收藏包名、绝对路径、生成快照、内部审查记录或第三方宿主图标。首次运行看到空数据是正常现象。

## 环境要求

- Node.js `>=20.19`
- Python `>=3.9`
- macOS、Linux；Windows 尚未作为正式支持平台验证

## 快速开始

```bash
git clone https://github.com/Dep-0302/AI-Toolbox.git
cd AI-Toolbox
npm ci --ignore-scripts
npm run workbench
```

浏览器打开 `http://127.0.0.1:4791`。macOS 也可以双击 `打开AI-Toolbox工作台.command`；入口会按 `package-lock.json` 准备依赖、构建页面并启动或复用同一公开版本的本地服务。

默认收藏目录是 `~/AI-Toolbox-Collection`。也可以在页面里通过原生文件夹选择器临时选择其他目录，或在启动前指定：

```bash
AI_TOOLBOX_COLLECTION_ROOT="/absolute/path/to/your/collection" npm run workbench
```

临时选择结果只保存在当前服务内存；浏览器不会把手填路径发送给服务。

## 常用命令

```bash
npm run check              # 前端测试、构建、Python 测试与候选索引测试
npm run scan               # 观察宿主入口，写 generated/snapshot.json
npm run scan:collections   # 观察默认收藏目录
npm run build              # 构建前端
npm start                  # 启动已构建的本地服务
```

## 安全与隐私边界

- 不执行扫描到的 Skill、Plugin、MCP、CLI 或 SDK。
- 不读取宿主凭据、Token、会话、浏览器资料、日志或任意配置正文。
- 不跟随收藏目录中的符号链接；授权根或祖先存在符号链接时失败关闭。
- 不解压归档到磁盘；归档条目数、大小、展开体积和压缩比均有限额。
- HTTP 只监听 loopback，并校验 Host、Origin 与写请求 CSRF Token。
- 不包含遥测、模型调用、后台监听、自启动或远程服务。

更完整的数据边界见 [PRIVACY.md](PRIVACY.md)，安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

## 自定义元数据

公开版提供空的 `registry/chinese_metadata.json` 与 `registry/collection_taxonomy.json`，避免分发私人资产清单。你可以在自己的分支中维护本地覆盖，但请使用 `*.local.json` 或其他已忽略文件保存含个人路径的数据，不要提交 `generated/`。

`registry/scan_config.json` 只定义通用宿主根和资源预算；`registry/scenario_taxonomy.json`、`collection-workbench/rules.json` 是可修改的展示与分类规则。

## 许可证与商标

代码以 [MIT License](LICENSE) 发布。依赖与归属见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。Codex、Claude、Antigravity、Hermes、WorkBuddy 及其他产品名可能是其各自所有者的商标；本项目与这些厂商没有隶属、授权或背书关系，详见 [TRADEMARKS.md](TRADEMARKS.md)。
