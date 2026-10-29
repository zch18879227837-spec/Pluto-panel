# 发布指引：AstrBot 插件市场 + GitHub 仓库

> 本文只讲「怎么把 v1.3.1 发出去」，不涉及任何代码改动。
> 全部命令与脚本都是**模板**，不含任何账号、token、密码。

---

## 发布前必读

> ⚠️ **提醒 1：图标已就绪**
> `logo.png`、`assets/icon.png`、`assets/banner.png` 都已生成并在 `plugin.json` / `metadata.yaml` 里配好，本次发布**不需要再做任何图片处理**。

> ⚠️ **提醒 2：本指引不会自动登录你的账号**
> 下面的 git 命令与 bat 脚本只在本机执行，上传到 GitHub 用的是你本机已配置好的 git 凭证。
> 如果本机没配过，需要你先自己设置：`git config --global user.name`、`user.email`，以及 GitHub 的个人访问 token（PAT）或 SSH key。
> 本仓库不会、也不应该存放任何 token。

> ⚠️ **提醒 3：插件市场的「简介」要手动复制粘贴**
> `plugin.json` 里的 `description` 只给插件内部和仓库读，**不会自动填充插件市场的网页表单**。
> 提交时你需要登录自己的 AstrBot 账号，把本文末尾「市场提交文案」里的内容手动复制进表单。

> ⚠️ **提醒 4：zip 根目录必须是插件文件夹本身**
> 压缩包第一层必须是 `astrbot_plugin_token_panel/`，**不能**再套一层 `PLUTO-panel-v1.3.1/astrbot_plugin_token_panel/`，否则市场安装会报「找不到 metadata.yaml」。

---

## 一、AstrBot 插件市场打包

### 1. 目录结构（必须是这个样子）

```text
astrbot_plugin_token_panel/
├── main.py
├── metadata.yaml
├── plugin.json
├── _conf_schema.json
├── logo.png
├── README.md
├── LICENSE
├── assets/
│   ├── icon.png
│   └── banner.png
├── views/
│   └── dashboard/
│       └── index.html
├── .astrbot-plugin/
│   └── i18n/
│       └── zh-CN.json
└── .github/                ← 可不打包，不影响安装
```

**不要打包**：`__pycache__/`、`data/`、`*.log`、`settings.json`、`session_meta.json`、`compress_log.json`、`carry_backups/`。

### 2. 打包步骤（任选一种）

**方式 A：双击脚本（推荐）**

```text
tools\打包插件市场zip.bat
```

脚本会用到插件所在目录，自动在 `dist\` 下生成 `astrbot_plugin_token_panel.zip`，并校验根目录层级。

**方式 B：手动右键压缩（最稳）**

1. 在资源管理器里进入 `AstrBot/data/plugins/`。
2. 右键 `astrbot_plugin_token_panel` 文件夹 → 「压缩为 ZIP 文件」（Windows 11）或「发送到 → 压缩(zipped)文件夹」。
3. 把生成的 zip 改名成 `astrbot_plugin_token_panel.zip`。
4. **检查**：双击打开 zip，第一层应该直接看到 `main.py`、`metadata.yaml`，而不是又一个文件夹。

**方式 C：PowerShell 一行命令**

```powershell
cd "D:\dafeiyu\AstrBot\data\plugins"
Compress-Archive -Path "astrbot_plugin_token_panel" -DestinationPath "$env:TEMP\astrbot_plugin_token_panel.zip" -Force
```

### 3. 提交到插件市场

1. 打开 <https://plugins.astrbot.app/>（或 AstrBot 文档里的市场入口），登录你的 AstrBot 账号。
2. 进入「提交插件 / 上传」页面。
3. 上传上一步的 zip。
4. 表单里的「简介」「描述」「标签」按下方文案手动填写。
5. 提交后等待审核；审核通过前可以先用「手动安装」自测。

### 4. 市场提交文案（复制粘贴用）

**插件名称**

```text
PLUTO panel（普鲁托面板）
```

**一句话简介（≤60 字）**

```text
把每个会话的 Token 用量变成看得见、管得住的东西：可视化面板 + 上下文统计 / 压缩 / 承接。
```

**完整描述**

```text
PLUTO panel 是 AstrBot 的「上下文管家」。后台「扩展功能」内置独立面板：按私聊 / 群聊分组展示每个会话的模型窗口上限、已用 / 剩余 Token 与占用百分比，进度条按正常 / 预警 / 高危三色区分；到阈值自动在 QQ 里播报一次（带节流），也可在面板里一键清空会话。

内置上下文能力：
• /ctx 读取当前模型的真实窗口上限（Provider 配置 → models.dev 元数据），不再写死 128000
• /ctx_compress 把较早历史压成摘要，会话 ID 不变，压缩前自动备份
• /ctx_continue 另开新会话承接，旧会话原样保留，可随时切回
• /ctx_auto 请求内自动压缩，只影响本次请求、不写库
• /token_* 指令查看与播报 Token 用量

特点：全程本地运行、不联网、不改 AstrBot 核心；两套主题（Claude Code / DeepSeek 蓝调）× 两套画布（象牙白 / 吸黑）；全部指令带独立前缀，不与 /ctx 等原生指令冲突。
```

**标签**

```text
工具, 管理, token, context, dashboard
```

---

## 二、上传到 GitHub

仓库建议名：`astrbot_plugin_token_panel`（与插件标识一致，便于市场校验）。

### 方案①：命令行推送（推荐，可重复）

**首次上传**

```bash
cd "D:\dafeiyu\AstrBot\data\plugins\astrbot_plugin_token_panel"

git init
git add .
git commit -m "feat: PLUTO panel v1.3.1 - Token 面板与上下文压缩承接"
git branch -M main
git remote add origin https://github.com/<你的用户名>/astrbot_plugin_token_panel.git
git push -u origin main
```

**如果本机还没配过 git 身份**

```bash
git config --global user.name "Charon"
git config --global user.email "you@example.com"
```

**如果推送需要认证**（HTTPS）

1. GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens。
2. 权限至少勾选该仓库的 `Contents: Read and write`。
3. 推送时用户名填 GitHub 用户名，密码处粘贴 token（**不要把 token 写进任何文件**）。
   或者改用 SSH：

```bash
git remote set-url origin git@github.com:<你的用户名>/astrbot_plugin_token_panel.git
```

**后续更新**

```bash
git add .
git commit -m "chore: 发布 v1.3.1"
git tag v1.3.1
git push origin main --tags
```

### 方案②：网页拖拽上传（不想碰命令行时）

1. GitHub → New repository → 名称 `astrbot_plugin_token_panel` → Public → **不要**勾选 Add README（避免冲突）。
2. 创建后进入仓库，点「uploading an existing file」。
3. 把 `astrbot_plugin_token_panel` 文件夹**里面的全部文件与子文件夹**拖进去（`main.py`、`metadata.yaml`、`plugin.json`、`logo.png`、`assets/`、`views/`、`.github/`、`README.md`、`LICENSE`、`.gitignore` 等）。
   - 注意：网页拖拽**不会**带上以 `.` 开头的文件夹（如 `.github/`、`.astrbot-plugin/`），需要单独上传或改用方案①。
4. 填写 commit 信息 → Commit changes。
5. 补传 `.github/` 与 `.astrbot-plugin/`：进入对应目录层级，用「Add file → Upload files」逐层上传。
6. 上传完成后到仓库 Settings → Social preview 上传 `assets/banner.png`（1280×640）。

### 方案③：Windows 一键推送脚本

```text
tools\push-github.bat
```

运行后会依次问你：仓库 URL（可直接回车用默认值）、提交信息；脚本只做 `git init / add / commit / branch / remote / push`，**不含任何账号密码**，认证仍走你本机的 git 凭证。

---

## 三、GitHub 仓库 About（手动填写）

仓库首页右侧「About → 编辑」，粘贴：

**Description（一句话）**

```text
AstrBot 的 Token 用量面板与上下文管家：可视化会话占用，内置 /ctx 统计、压缩与承接，本地运行不联网。
```

**Topics（逐个添加）**

```text
astrbot  astrbot-plugin  token  context  llm  dashboard  onebot  python
```

**Website**：可留空，或填插件市场页面地址。

---

## 四、发布检查清单

- [ ] `metadata.yaml` 里 `name` 仍是 `astrbot_plugin_token_panel`，`version` 为 `1.3.1`，`author` 为 `Charon`
- [ ] `plugin.json` 里 `id` 未改动，`icon` / `banner` 指向 `assets/` 下的文件
- [ ] zip 第一层是 `astrbot_plugin_token_panel/`
- [ ] 仓库里没有 `data/`、`*.log`、`settings.json`、`carry_backups/`、`__pycache__/`
- [ ] `LICENSE` 年份与作者正确（2026 / Charon）
- [ ] README 顶部封面图能正常显示
- [ ] GitHub 仓库 About 已填 Description 与 Topics
- [ ] GitHub Social preview 已上传 `assets/banner.png`
- [ ] 市场表单的简介与描述是手动粘贴的（不会自动填充）

---

## 五、常见问题

**Q：市场提示「插件标识不合法 / 找不到 metadata.yaml」？**
A：九成是 zip 多套了一层文件夹。解压后第一层必须直接是 `main.py` 和 `metadata.yaml`。

**Q：网页上传后 `.github/` 不见了？**
A：GitHub 网页拖拽会忽略以 `.` 开头的目录，逐个手动上传，或改用 git 命令行。

**Q：推送时提示 `remote: Permission to ... denied`？**
A：token 没勾 `Contents: Read and write`，或账号不是该仓库所有者。

**Q：市场审核没过，说描述太简单？**
A：把上面「完整描述」整段贴进去，并补充截图（面板数据页 + 压缩承接页各一张）。

**Q：能同时发布到 GitHub 和插件市场吗？**
A：可以，两者互不影响。市场通常要求仓库为 Public 且 `metadata.yaml` 规范。

---

版本 v1.3.1 · 作者 Charon · MIT