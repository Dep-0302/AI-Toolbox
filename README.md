# AI-Toolbox

本地、只读的 AI 能力检索工作台。它在前台观察本机已有的 `Skill / Plugin / MCP / CLI / SDK` 入口，以及你主动选择的收藏目录；不会安装、启用或执行这些能力。

> Local, observe-only inventory for AI capabilities. No cloud account is required.

当前公开版本：`0.3.2`

## 主要能力

- 按 Codex、Claude、Hermes、WorkBuddy 等宿主观察 Skill 与工具入口。
- 区分“发现、安装、宿主绑定和可用性”，不把文件存在误报为已经可调用。
- 只读索引一个本地收藏目录，支持 ZIP 安全预算、符号链接拒绝跟随和失败关闭。
- 在独立的“项目 Skill”工作台中查看项目、固定 Skill 入口、受限元数据投影与分层证据。
- 通过 macOS 原生文件夹选择器配置一个本机项目观察根，并可持续监测根内或根外的具体项目。
- 在 `127.0.0.1` 提供 React 工作台，并把运行快照限制在项目内的 `generated/`。
- 提供搜索、筛选、收藏、详情、健康检查和手动刷新。

公开仓库不包含任何作者机器上的能力清单、收藏包名、真实项目登记、项目关联、绝对路径、生成快照、浏览器决策导出、内部审查记录或第三方宿主图标。公开项目 Registry 与关联 Registry 是空模板；首次运行看到空数据是正常现象。

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

浏览器打开 `http://127.0.0.1:4791`。macOS 也可以双击 `打开AI-Toolbox工作台.command`；入口会按 `package-lock.json` 准备依赖、构建页面，并且只复用源码目录、API 与构建身份均匹配的本地服务。

## 本地来源与临时选择

默认收藏根是 `~/AI-Toolbox-Collection`。也可以在页面里通过原生文件夹选择器临时选择另一个安全目录，或在启动前指定：

```bash
AI_TOOLBOX_COLLECTION_ROOT="/absolute/path/to/your/collection" npm run workbench
```

收藏目录的临时切换只保存在当前本地服务会话；重启服务或点击“恢复默认”后回到 `~/AI-Toolbox-Collection`。页面不接受手填路径，只接收本地服务签发的短时、单次选择令牌。

“项目 Skill”公开版默认不绑定任何项目观察根。首次进入时可通过原生文件夹选择器配置一个本机观察根；配置只写入被 Git 忽略的 `generated/project-skills/local-root.json`，不会进入公开版本。公开项目 Registry 和人工关联 Registry 默认均为空；观察根下尚未登记的一级目录只列为未分类候选，不读取其中的 Skill 内容。

你也可以在“项目 Skill”页面选择一个具体项目文件夹。确认后只读取该项目批准的 Skill 入口，并把监测记录保存在被 Git 忽略的 `generated/project-skills/local-projects.json`；服务重启后仍可选择。移除监测只删除这条本机记录，不删除或修改项目文件夹，不创建人工关联，也不把项目写入跟踪的 Registry。同一路径若已经是登记项目，只复用原登记项，不会产生重复项目。

## 项目 Skill 的证据边界

- 只观察批准入口中的 `SKILL.md` frontmatter，字段限于名称、说明、版本、作者、许可证和 `agent_created`。
- 可选读取同一 Skill 直属 `agents/openai.yaml` 中的 `policy.allow_implicit_invocation` 声明；这仍只是声明证据。
- 不读取 `SKILL.md` 正文、`scripts/`、`references/`、凭据、宿主配置正文、会话或日志。
- 文件发现、项目绑定、宿主可用、调用资格和实际使用分层显示，不从低层证据自动推断更高层状态。
- 手动项目观察的可重建结果与本机选择状态只写入仓库内忽略的 `generated/project-skills/`；不会写回被观察项目。

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
- 项目观察只允许用户明确选择的具体目录；个人主目录、桌面、系统目录、敏感目录、过宽目录与路径身份变化都会被拒绝。
- 不解压归档到磁盘；归档条目数、大小、展开体积和压缩比均有限额。
- HTTP 只监听 loopback，并校验 Host、Origin 与写请求 CSRF Token。
- 不包含遥测、模型调用、后台监听、自启动或远程服务。

更完整的数据边界见 [PRIVACY.md](PRIVACY.md)，安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

## 自定义元数据

公开版提供空的 `registry/chinese_metadata.json`、`registry/collection_taxonomy.json`、项目 Registry 与项目关联 Registry，避免分发私人资产或项目关系。无需修改 Registry，也可通过页面选择观察根和具体项目；这些选择只保存在本机忽略文件中。若你在自己的私有分支中直接维护跟踪文件，请不要把含个人路径的改动、`generated/` 或浏览器决策导出提交到公开仓库。

`registry/scan_config.json` 只定义通用宿主根和资源预算；`registry/scenario_taxonomy.json`、`collection-workbench/rules.json` 是可修改的展示与分类规则。

## 许可证与商标

代码以 [MIT License](LICENSE) 发布。依赖与归属见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。Codex、Claude、Antigravity、Hermes、WorkBuddy 及其他产品名可能是其各自所有者的商标；本项目与这些厂商没有隶属、授权或背书关系，详见 [TRADEMARKS.md](TRADEMARKS.md)。
