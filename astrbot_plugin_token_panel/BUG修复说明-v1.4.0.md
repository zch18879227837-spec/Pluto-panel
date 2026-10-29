# v1.4.0 修复补丁：抖音浏览记录重复与统计错位

> 版本号不变（仍为 1.4.0），只修 bug，不加功能。

## 一、问题表现

面板「最近浏览」里同一个视频出现两条以上；顶部写着「5 个视频」，列表实际只有 3 条且带重复；切换会话时数字对不上。

## 二、三个根因

| 位置 | 问题 | 后果 |
| --- | --- | --- |
| `worker/douyin_worker.py` | 去重键用的是卡片整段 `innerText`（含点赞数、作者、时间），同一个视频第二次抓到时只要点赞数变了就算「新视频」 | 同一次浏览里同一视频被存两条 |
| `worker/douyin_worker.py` | 风控检测把页面里常驻的隐藏 `captcha` 容器也算命中 | 一进页面就误判风控，0 视频退出 |
| `main.py::_dy_run_once` | 顶部数字来自 `douyin_state.json` 的**累加值**（每次 +本次条数），列表来自日志条目 | 一旦有重复条目或中断，数字虚高且与列表对不上 |

## 三、改动清单（3 个文件）

### 1. `worker/douyin_worker.py`

- 卡片解析新增 `vid`：读 `data-aweme-id`（缺失时退回元素 `id`），只接受 8 位以上纯数字。
- 新增 `_card_key(card)`：优先 `vid:<视频ID>`，抓不到 ID 时退回 `raw:<整段文本 sha1 前16位>`——不再用标题去重。
- 收集循环改用 `_card_key` 判重（`fresh` 过滤与写库前各判一次），`items` 改为以该键为 key 的字典；每条记录存 `video_id / title / duration / watched_at / dwell`，不再存点赞数。
- `CHECK_RISK` 重写：候选验证框必须 `offsetParent !== null` 且尺寸 > 80×80 才算命中；文字特征改为「点击…形状 / 形状相同 / 拖动…滑块 / 滑动验证 / 请完成安全验证 / 安全验证失败」——登录弹窗里的「验证码登录」不再触发。

### 2. `main.py`

- 新增 `_dy_dedupe(items)`：入库前按 `video_id` 再去一次重（无 ID 时用 标题+时长）。
- 新增 `_dy_watch_minutes(items)`：会话观看时长 = 各视频 `dwell` 秒数累加 ÷ 60，四舍五入 1 位小数。
- 新增 `_dy_stats_from_log()`：今日统计**每次都从日志条目现算**，跳过 `ok=false` 与 `risk=true` 的条目；不再读状态文件里的旧累加值。
- `_dy_state()`：读完状态文件后立刻用日志统计覆盖 `sessions / videos / minutes`。
- `_dy_run_once()`：
  - 生成 `session_id`（`YYYYMMDD-HHMMSS-随机4位`）；
  - 子进程结果先 `_dy_dedupe` 再计数；
  - 先写日志、再统计、最后存状态（顺序保证数字与列表同源）；
  - 日志条目新增 `session_id`、`watch_minutes`，`items` 上限 12 → 30。
- `api_dy_overview()`：新增 `sessions[]`（每条含 `session_id / ts / mode / ok / risk / count / minutes / watch_minutes / items`），全部经 `_dy_dedupe`。

### 3. `views/dashboard/index.html`

- 「最近浏览」卡片头部新增会话下拉 `#dy-session`（复用 `.mini-select` 样式）。
- 新增 `dyDedupe / dyNormalize / dyCurrent / dyFillSessions / dyRenderStats`；`applyDy` 先归一化会话列表，再按选中会话渲染。
- `renderDyLog(session)` 改为渲染**单个会话**：条数取前端二次去重后的长度，标题最多显示 8 条。
- `bindDy` 绑定下拉 `change`：切换会话时只重算统计与列表，不重新请求。
- 顶部「今日累计」改为「N 次 · 本次 X 个视频 · Y 分钟」，X/Y 来自当前选中会话，与列表条数一一对应。

## 四、没有动的部分

Token 估算公式、压缩/承接流程与摘要模板、指令名与权限、配置项、数据表结构、风控限额（每日次数/免打扰/单次时长与条数）、主题与布局、日志保留规则（只存标题、时长、时间）。

## 五、验证

| 用例 | 结果 |
| --- | --- |
| worker 单独跑 4 条 | `ok=true risk=false`，4 条记录 `video_id` 全不同 |
| 插件内手动浏览两轮 | 每轮 8 条、`count == unique video_id 数`，无重复标题 |
| 顶部数字 vs 列表 | 「本次 8 个视频」↔ 列表 8 条，一致 |
| 切换会话 | 21:15 会话显示 8 条 2.5 分钟；21:09 会话显示 8 条 2.4 分钟，互不干扰 |
| 误判风控 | 页面 21 张卡片正常进入浏览，`risk=false`（修复前 0.1 分钟即退出） |
| 控制台 | 象牙白 / 吸黑两套主题下 0 报错 |