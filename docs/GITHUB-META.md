# GitHub 仓库信息（复制即用）

## 仓库名

```
astrbot_plugin_token_panel
```

（与插件 id 一致，安装时目录名必须相同。）

## 中文简介（AstrBot 插件市场 / 仓库描述）

> AstrBot 的会话上下文可视化面板：token 进度、压缩承接、置顶、联网搜索、推理通知与事件日志，双主题。

## English about（< 350 chars，填仓库右上角 About）

```
Context-aware dashboard plugin for AstrBot: per-session token usage, context
compression & handoff, pinned sessions, built-in web search, reasoning
notifications, TTS and a unified event log. Two themes, MIT.
```

## Topics（GitHub 标签，建议 12–15 个）

```
astrbot          astrbot-plugin   llm              context-window
token-usage      dashboard        onebot           qq-bot
web-search       playwright       sqlite           python
mit-license      tts              prompt-caching
```

## 一句话定位（用于 Issue 模板/README 顶部）

中文：看得见 token 用量，也管得住上下文。
English: See your context, own your context.

## Release 标题建议

```
v1.10.2 — Unified event log
```

## 仓库设置建议

| 项 | 建议值 |
| --- | --- |
| Description | 中文简介（见上），勾选 About 里的 "Use repository description" |
| Website | 留空，或填插件市场页 |
| Topics | 见上 |
| License | MIT（仓库里已有 LICENSE 文件即可识别） |
| Issues | 开启，使用 `.github/ISSUE_TEMPLATE` 里的两个模板 |
| Discussions | 可选，用于使用问答 |
| Releases | 建议每个版本打 tag 并附 `dist/*.zip` |

## 上传前检查清单

- [ ] `settings.json`、`*.db`、`*.jsonl`、`carry_backups/` 未被提交（见 `.gitignore`）
- [ ] 仓库里没有本机绝对路径（`docs/` 已做脱敏）
- [ ] `plugin.json` 的 `repo` 字段填成真实地址
- [ ] README 截图路径存在（`assets/screenshots/*.jpg`）
- [ ] 版本号在 `plugin.json`、`metadata.yaml`、`main.py` 三处一致