# 国金证券 政策与战略 · 门户网站

国金证券政策与战略组的内部研究门户。纯静态单页站点（无构建、无框架、无后端），
数据与页面分离：页面是 `index.html` 一个文件，政策条目存放在 `data/policy/*.json`，
由 `scripts/` 下的 Python 脚本抓取、补写、校验后提交。

线上地址：<https://liulijia123456.github.io/sinolink-portal/>
仓库：`git@github.com:liulijia123456/sinolink-portal.git`（GitHub Pages，部署分支 `main`）

---

## 1. 架构总览

```
                ┌──────────────────────────────────────────┐
                │  index.html（690 KB，单文件）             │
                │  ├─ 内联 CSS（按视图分作用域）              │
                │  │   ├─ 基础样式（深海军蓝 + 纸感暖白双区）  │
                │  │   ├─ #v2-portal    首页（#columns 等）    │
                │  │   └─ #v3-us-board  美国板块（#view-us）   │
                │  ├─ 3 个 <script>（视图路由 / 自动更新 / 装饰）│
                │  └─ 2 个内嵌 JSON 数据块                  │
                │      ├─ id="election-data"（选举动态）     │
                │      └─ id="us-cal-data"（大事日历）       │
                └───────────────┬──────────────────────────┘
                                │ fetch（同源相对路径）
                ┌───────────────▼──────────────────────────┐
                │  data/policy/（政策三板块，外置 JSON）      │
                │  ├─ {board}-items.json     条目主体        │
                │  ├─ {board}-overview.json  周/月研判        │
                │  └─ manifest.json          计数与时间戳     │
                └───────────────▲──────────────────────────┘
                                │ 读写（portal_store.py 存储层）
                ┌───────────────┴──────────────────────────┐
                │  scripts/（9 个 Python 脚本，标准库即可运行）│
                └───────────────▲──────────────────────────┘
                                │ 本地自动化 / GitHub Actions
                        git push → Pages 自动重建
```

三条关键设计约束，改动前务必了解：

1. **页面不读 HTML 里的数据块**。政策条目全部走 `fetch('data/policy/…')`，
   因此**必须用 HTTP 服务预览**，双击打开 `file://` 会因跨域/协议限制拿不到数据。
2. **数据只由脚本写**。`brief` / `title_cn` / `domain` 都有各自归属的写入通道，
   手工编辑 JSON 极易造成 `count` 与 `manifest` 脱节、编码错乱、字段被覆盖。
3. **两套数据通道并存**：`data/policy/` 是权威存量（全量历史，页面主列表读它）；
   `updates.json` 是增量清单（供页面「内置自动更新模块」静默拉取），两者不互相同步。

---

## 2. 目录结构

| 路径 | 说明 |
| --- | --- |
| `index.html` | 全站唯一页面：门户首页 + 4 个子视图（`us` / `eu` / `board` / `election`）+ 全部样式与脚本 |
| `data/policy/` | 政策三板块数据（`whitehouse` / `congress` / `agencies`），已入库 |
| `scripts/` | 抓取、简报、校验、日历维护等维护工具 |
| `logs/` | `fetch.log` / `brief.log`（运行时产物，已 gitignore） |
| `scripts/.congress_api_key` | 国会 API 密钥（**已 gitignore，绝不入库**） |
| `github自动更新部署包.zip` | 早期部署包（含 Actions 工作流，见 §9） |
| `.workbuddy/` | 本地自动化的工作记忆（当前未被 gitignore，勿提交） |

### 页面视图与 hash 路由

| hash | 视图 | 内容 |
| --- | --- | --- |
| （空） | 门户首页 | 研究专栏四卡 + 覆盖范围 + 说明 + 页脚 |
| `#us` | 美国板块 | 三个政策子板块卡 + 选举动态卡 + 大事日历 |
| `#board-whitehouse` | 子板块 | 白宫政策条目列表（筛选 / 分页 / 研判区） |
| `#board-congress` | 子板块 | 国会政策条目列表 |
| `#board-agencies` | 子板块 | 独立机构政策条目列表 |
| `#election` | 选举动态 | 中期选举 / 2028 总统大选两栏 + 民调卡 |
| `#eu` | 欧洲板块 | 欧盟 / 英国 / 法国 / 俄罗斯四卡（**纯静态，暂无数据管线**） |

首页四张专栏卡中，`tone-us`（美国政治）与 `tone-eu`（欧洲政治）已接内容；
`tone-geo`（地缘局势分析）与 `tone-hot`（热点事件分析）的「进入板块」按钮
**没有 `data-enter` 属性**，目前是占位卡，点击无跳转。

---

## 3. 本地开发

### 3.1 预览（必须走 HTTP）

```bash
cd /Users/silentecho/Desktop/门户/sinolink-portal
python3 -m http.server 8000
# 打开 http://localhost:8000/index.html
```

必须从**仓库根目录**起服务：页面里写的是相对路径 `data/policy/…`，
服务根目录错位会导致三个板块全部空白。

### 3.2 环境要求

- Python 3.8+，**仅用标准库**（`json` / `urllib` / `logging`），无第三方依赖，无需虚拟环境。
- 国会 API：优先读环境变量 `CONGRESS_API_KEY`，其次 `scripts/.congress_api_key`，
  都没有则回落 `DEMO_KEY`（限流严重，仅够调试）。申请：<https://api.congress.gov/sign-up/>

```bash
export CONGRESS_API_KEY=你的密钥          # 推荐：不落盘
# 或写入 scripts/.congress_api_key（已 gitignore）
```

### 3.3 改页面的注意事项

- `data-page-node-id="…"` 属性由上游可视化编辑器生成，**不要删除或批量重排**，
  否则后续可视化改版会产生大范围无意义 diff。
  新增元素时**一律不要自己加这个属性**（会与编辑器生成的 id 冲突）。
- 内联 SVG（国徽、欧盟旗、欧洲地图、地球仪）是人工简化/正算的路径数据，
  改尺寸请改 `viewBox` 与容器，不要动 `<path d="…">`。
- 全站配色走 CSS 变量（`--bg` / `--card` / `--accent` / `--gold` …，浅色区带
  `-light` 后缀），改主题只改 `:root`，不要散改具体规则。
- 站点**无构建流程**：改完 `index.html` 直接刷新即可，不需要打包。

### 3.4 版面设计系统（v2 首页 / v3 美国板块）

全站现在共用一套「报告式编辑排版」语言：

| 要素 | 取值 |
| --- | --- |
| 墨色底 | `--v2-ink` / `--us-ink`：`#0b1724` |
| 纸面底 | `--v2-paper` / `--us-paper`：`#f2eee6` |
| 铜金（索引与刻度） | `#a8823c` / `#c9a86a` |
| 蓝（数据与交互） | `#2f6fb8` |
| 中文标题 | 宋体：Songti SC → STSong → Source Han Serif SC → Georgia |
| 标签 / 读数 | 等宽：SF Mono → Menlo → Consolas |

**两色分工是硬规矩**：蓝色只承担数据与交互（链接、徽标、选中态、图表），
铜金只承担索引与刻度（编号、规则线、序号）。二者不混用，避免版面失去层级。

样式按作用域分块，互不干扰：

| 样式块 | 作用域 | 用途 |
| --- | --- | --- |
| `<style id="v2-portal">` | `#columns` / `#coverage` / `#notes` / `.about` | 首页页头、板块标题、覆盖领域、研报列表 |
| `<style id="v3-us-board">` | `#view-us` 全部 | 美国政治板块：档案抬头、读数带、日历控制台、板块卡 |

改美国板块时注意：

- 新样式一律写在 `#view-us` 作用域内，**不要**改 `.subhead` / `.card` / `.col-head`
  等全局规则 —— 欧洲板块、条目列表、选举动态共用这些类，改动会波及四视图。
- 新增 DOM 元素时不要带 `data-page-node-id`。
- 抬头为单列排版（返回链接 → 标题 → 英文副标 → 说明）：`min-height: 470px` + `align-content: center`，
  纵向留白 96/104px，与首页大抬头（156/84、高 520）同一量级；≤700px 取消 min-height、留白 46/54px；
  日历控制台为单列（时钟 → 工具条 → 月历 → 图例）。以下内容已按需求删除，
  其作用域样式与脚本一并清除，不留死代码：「档案信息栏」`.ush-rail`、
  「栏目行 SECTION 02」`.us-eyebrow`、「未来 30 天议程侧栏」`.uc-agenda`
  与 `renderAgenda()` / `window.usSyncAgenda()`、以及整条「读数带」`.us-stats`
  （原四格，先删「政策条目累计」一格，后整条移除）与 `renderStats()`。
- 美国板块保留一个对外接口：`window.usCalApi.jumpToDate(key)`
  （跳月 + 选中日 + 展开当日安排），供控制台直接调用。
- 板块卡徽标（`data-us-count`）与脚注（`data-us-foot`）的数值来自真实数据，不是硬编码，
  由 `renderCardMeta()` 回填：白宫 / 国会 / 独立机构读 `data/policy/manifest.json`
  （`fetch`，`file://` 下静默失败、徽标保持占位），选举动态条数读内嵌 `election-data`。
- 双时区时钟带（`.uc-clock`）为「刻度带」：约 54px 高，两栏各占一半，
  栏内**三点定位**——标签靠左、时间居中、日期靠右，两半因此落在同一条节奏上。
  标签前一根 3×15px 竖色条，颜色即归属。
  **底色为毛玻璃**：`rgba(255,255,255,.5)` 半透明底 + `backdrop-filter: blur(16px) saturate(1.3)`，
  叠左右各一抹极淡的蓝/金径向晕与一层自上而下的白色高光，内侧 1px 亮边。
  浅底上用深色字：蓝 `#1c5a96` / 金 `#8a6420`，次层靠透明度递减（日期 .9 / 秒位 .8 / 时令 .78）。
  中缝为深色发丝线 + 白色小珠呼吸灯（浅底上白色必须带一圈细描边与投影才看得见）。
  字阶：时间 28px / 秒位 12.5px / 标签 10.5px / 日期 10px。
  两个动效都写在伪元素上，不加 DOM：
  `::before` 是 1px 旋转渐变描边（`@property --ucc-ang` 驱动 conic 渐变 + `mask-composite` 挖空，
  14s 一圈，渐变走「蓝 → 金」把两半连起来）；`::after` 是贴下沿的极淡色温呼吸
  （左冷蓝 / 右暖金两团，`blur(18px)`，7.2s 一次）；中点 `.ucc-node` 是 4.6s 白色呼吸灯。
  `prefers-reduced-motion: reduce` 下三者全部定格。
  `@property` 不可用的旧浏览器降级为静止渐变描边，不影响可读性。
  DOM 里原有的四角框 `.ucc-corner` 与秒进度条 `.ucc-track` 由样式隐藏（节点保留，脚本继续写入）；
  秒位仍由 `renderUsClock()` 每秒刷新。≤860px 两行堆叠时，中缝转为横线，呼吸灯落在两行之间，
  每行成为「标签左 / 时间右」。**注意**：`.ucc-block` 的基类含 `flex-direction: column`，
  作用域内必须显式写 `flex-direction: row`，否则三点定位会退化成竖排。
- 抬头右侧有一幅**北美轮廓**：以美国为主体（本土 48 州 + 阿拉斯加 + 夏威夷），
  加拿大、墨西哥、中美洲与加勒比群岛只作为地理背景、故意更细更淡
  （`.usm-neighbor`，描边 .26 / 1px）。加拿大含北极群岛（巴芬、维多利亚、班克斯等，
  仅略去北纬 73° 以北的埃尔斯米尔一带），画布 660×470，
  按「全部内容」求包围盒统一缩放，因此美加墨的相对大小是真实的。只描边不填充，
  作为地理底纹压在文字之下（`z-index` 介于网格与 `.wrap` 之间）。
  尺寸按**高度**适配（`height: min(86%, 330px); width: auto`），避免在较矮的抬头里被裁掉；
  ≤1100px 文字铺满时隐藏。路径由 `scripts/gen_us_map.py` 从 Natural Earth 110m 生成
  （Albers 投影 + Douglas–Peucker 简化，美国约 350 点；加拿大 / 墨西哥另出 `.usm-neighbor`）。
  **三块用同一投影、同一比例，按真实地理方位摆放**：阿拉斯加在西北（本土左上）、
  夏威夷在西南（本土左下、尺度很小）、本土居中偏东；阿留申群岛按 `CLIP_LON` 整块略去，
  避免地图被拉到西经 180° 之外。要调方位就改脚本顶部的投影参数与 `CLIP_LON`，重跑即可。
  该 `<svg>` 是手工新增节点，**不带 `data-page-node-id`**；编辑器重存有可能把它吃掉，改完要核对。

---

## 4. 数据模型

### 4.1 `data/policy/{board}-items.json`

```json
{
  "schema": 1,
  "board": "whitehouse",
  "updated_at": "2026-10-24 14:00",
  "count": 150,
  "items": [
    {
      "date": "2026-10-26",
      "title": "英文原标题",
      "title_cn": "中文标题",
      "url": "https://www.federalregister.gov/documents/…",
      "source": "联邦公报",
      "kind": "规则",
      "agency": "联邦通信委员会 FCC",
      "domain": "电信/媒体",
      "brief": { "at": "2026-10-24", "tier": "note", "body": "…" }
    }
  ]
}
```

硬性要求（`validate_data.py` 会拦）：

- `items` 按 `date` **严格倒序**；`url` 全板块唯一；`date` 为 `YYYY-MM-DD`。
- `count` 必须等于 `len(items)`；`schema` 为 `1`；`board` 与文件名一致。
- `domain` 必须在固定词表内（见 §6.3）；`title_cn` 与 `domain` 缺失在 `--strict` 下拦截发布。

### 4.2 `data/policy/{board}-overview.json`

周/月研判区数据，`weekly` 与 `monthly` 各含
`{tag, period, updated_at, stats, expanded, body, note}`。
由撰写端直接维护，脚本不改写。

### 4.3 `data/policy/manifest.json`

```json
{ "schema": 1, "built_at": "…", "source_fetched_at": "…",
  "boards": { "whitehouse": {"count": 150, "updated_at": "…"}, … } }
```

**存在性即开关**：`manifest.json` 在，`portal_store.load_policy()` 走 JSON 形态；
不存在则回落到读 `index.html` 里的旧数据块。因此删掉它会让整站回退到历史形态。

### 4.4 内嵌数据块（在 `index.html` 里）

| id | 结构 | 维护方式 |
| --- | --- | --- |
| `election-data` | `{updated_at, map, sections, gallup_update}` | 手工编辑（无脚本） |
| `us-cal-data` | `{updated_at, source_note, events[]}` | `cal_update.py merge` 只追加 |

日历事件字段：`d`（必填，起始日）、`cat`（`econ/pol/dip/hol`）、`title`（必填）、
`d2`（多日结束日）、`t`（美东时刻）、`s`（格内短标题）、`note`（地点/机构/来源）、
`replaces_title`（把同名旧条目标「已调整」但不删除）。

---

## 5. 脚本清单

| 脚本 | 用途 | 常用命令 |
| --- | --- | --- |
| `fetch_policy.py` | 抓取三板块政策条目并合并入库（纯增量，无留置上限） | `python3 scripts/fetch_policy.py --days 10 [--dry-run]` |
| `brief.py` | 生成简报任务队列 / 合并简报 | `python3 scripts/brief.py queue --limit 8`<br>`python3 scripts/brief.py merge scripts/briefs.json` |
| `edit_items.py` | 受控补写 `title_cn` / `domain` | `python3 scripts/edit_items.py patch.json [--overwrite] [--dry-run]` |
| `validate_data.py` | 发布前机器体检 | `python3 scripts/validate_data.py [--strict]` |
| `cal_update.py` | 大事日历安全增量合并 | `python3 scripts/cal_update.py merge cand.json [--dry-run]`<br>`python3 scripts/cal_update.py check` |
| `gen_updates_json.py` | 生成 `updates.json` 增量清单 | `python3 scripts/gen_updates_json.py --days 10 --out updates.json` |
| `fix_congress_urls.py` | 修正决议类条目的错误 URL | `python3 scripts/fix_congress_urls.py [--dry-run]` |
| `migrate_to_json.py` | 一次性迁移/回灌（非常规工具） | `python3 scripts/migrate_to_json.py --source index.html --out data/policy` |
| `portal_store.py` | 存储层（被上面脚本 import） | — |

### 5.1 `fetch_policy.py` — 数据入口

数据来源全部为美国政府官方接口：

- **白宫政策**：联邦公报总统文件（行政令/备忘录/公告）+ EOP 组成机构
  （`WHITEHOUSE_AGENCY_SLUGS`：USTR、OMB、OSTP、CEQ、ONDCP、NSC、NEC）
  + 内阁各部（`CABINET_DEPT_SLUGS`，**只收 Rule / Proposed Rule**，不收 Notice）。
- **国会政策**：`api.congress.gov/v3/bill`，参众两院法案与决议最新动态。
- **独立机构政策**：联邦公报按机构过滤，`AGENCY_SLUGS` 当前为美联储 + FCC。

要增删机构或扩大范围，改这三个字典即可（slug 取自联邦公报 agency 字段）。

关键常量：

```python
LOOKBACK_DAYS = 10
HTTP_TIMEOUT, HTTP_RETRIES = 20, 3
```

退出码：`0` 全部成功 / `1` 部分成功（成功板块已回写）/ `2` 全部失败（保留旧数据）。

> 注意：`DEPT_NOTICE_EXTRA` 为「几乎不发规则」的部门（如国防部，实测近 30 天 84 条
> 全为 Notice）单独放宽到通告，并用关键词剔除程序性条目。新增此类部门前先查实际发文构成。

### 5.2 `brief.py` — 简报分层与合并

两层简报，按「市场相关性」分类，避免长简报覆盖率被新增量稀释：

| 层级 | 篇幅 | 结构 | 判据 |
| --- | --- | --- | --- |
| `full` 长简报 | 800–2000 字 | 四段：`【政策背景】`/`【核心内容】`/`【要点提炼】`/`对金融市场的预期影响` | 白宫直接行为（行政令/备忘录/公告）；终局规则 + 高相关领域；法案已过委员会门槛 + 高相关领域 |
| `note` 要点速览 | 150–300 字 | 3–5 条要点，无段头 | 拟议规则、程序性通告、决议、礼仪类、低相关领域、仅提出未审议的法案 |

流程：

```bash
# 1) 出队列（含官方摘要素材，标注每条该写 full 还是 note）
python3 scripts/brief.py queue --limit 8
#    输出 scripts/brief_queue.md（已 gitignore）

# 2) 按队列撰写，写成 { "<条目url>": {"at","tier","body"} } 的 JSON
# 3) 合并入库
python3 scripts/brief.py merge scripts/briefs.json
```

合并保护：已有 `full` 不会被 `note` 覆盖（除非 `--force`）。
**`briefs.json` 的键必须是数据文件里的真实 `url`**，差一个后缀就会静默漏合并——
合并后务必用 `validate_data.py` 的简报计数复核。

### 5.3 其余脚本要点

- `edit_items.py`：模型只「判断」，脚本只「写入」。`title_cn` / `domain` 默认只补空值，
  `--overwrite` 才覆盖；`domain` 必须落在词表内，否则退出码 1。`brief` 不归它管。
- `cal_update.py`：键为 `(d, d2, title)`，命中即跳过，**绝无删除分支**；
  写回前用 `(d, title)` 键集合做兜底断言；只改 `us-cal-data` 一个数据块。
  需要「待定 → 官方确认」精化时加 `--update-notes`。
- `fix_congress_urls.py`：congress.gov 对法案与四类决议用不同路径段，早期统一写成
  `-bill` 曾导致跳错。脚本按 `kind` 推候选类型、查官方标题、`title` 比对一致才改。
- `gen_updates_json.py`：把新增条目导出为前端自动更新模块消费的清单，**只写 `updates.json`**，
  不碰 `data/policy/`。缺 `CONGRESS_API_KEY` 时跳过国会板块，不失败。

---

## 6. 数据纪律（最容易踩的四类坑）

### 6.1 留置策略：纯增量累积，无留置上限

2026-10-25 起取消分板块留置上限（此前为白宫 300 / 国会 400 / 机构 200）。
`fetch_policy.py: merge_items()` 现在**只按 url 去重与覆盖，绝不截断**，
历史条目永久保留。

理由：政策文本是研究底稿，过期不等于无价值；截断会连带丢掉已写好的简报、
译名与领域标签（已付出的加工成本），且删除动作是静默的——历史被清空时
不会有任何报错，只有事后回看才发现。

**代价与应对**：

- JSON 体积随时间线性增长。当前三板块 gzip 合计约 106 KB（原始 386 KB）。
  按日均新增 30 条外推：一年后约 3.7 MB，两年后约 7.3 MB。
- 页面 `fetch` 的是整个 JSON，**每次打开都要全量传输**。
- 渲染侧无压力：前端已有分页（`boardPageSize` 默认 20 条 / 页，`board-pager` 组件），
  每页只渲染当前页。
- 因此若日后需要控制传输体积，正确做法是**在页面端只渲染前 N 条**，
  而不是在存储端删数据——存储端删数据即违背本策略。
- `validate_data.py` 不做条数上限校验（存储层不截断，校验层就不该报「超过上限」）。

### 6.2 「抓取条数」不等于「净增条数」

`fetch.log` 里若标注 dry-run，该轮不落库。读日志判断增量前，先核对
「旧 N 条」与 manifest 的分板块计数是否一致，否则会把重复条目误读为新增。
commit message 的净增数按实际入库量填写。

### 6.3 领域词表固定（17 项）

```
科技/AI · 贸易/关税 · 能源/环境 · 医疗/医药 · 国防/安全 · 财政/税收
金融/保险 · 电信/媒体 · 农业/食品 · 教育/劳动 · 住房 · 交通
司法/执法 · 社会保障 · 产业/企业 · 政府运作 · 其他
```

新增标签必须同步改 `validate_data.py: DOMAINS` **和** 定时任务的 prompt 约定，
否则校验直接报错退出。

### 6.4 简报质量告警的处理口径

`note` 参考区间 100–900 字，`full` 除四段段头齐全外 400–4000 字。
区间外只告警不拦截；但**旧条目的历史告警不要为了清零而硬改正文**，
应记录为遗留项并在下一轮新增时收紧。

---

## 7. 更新标准流程（SOP）

2026-10-25 起按板块拆分为 4 个独立任务，各任务时段错开、互不阻塞
（详见 §9 定时任务分工）。各任务共用的守门环节：**校验 → 提交 → 推送**。

**政策条目日更**（每日 08:00）：

| 环节 | 动作 | 命令 |
| --- | --- | --- |
| A 抓取 | 拉取三板块条目 | `python3 scripts/fetch_policy.py --days 10` |
| B 补字段 | 新增条目的中文标题与领域 | `edit_items.py patch.json` |
| C 简报 | 出队列 → 撰写 → 合并 | `brief.py queue` → `brief.py merge` |
| E 校验 | 发布前体检 | `validate_data.py --strict` |
| F 提交 | 校验通过才推 | `git add -A && git commit && git push` |

**大事日历日更**（每日 07:00）：

| 环节 | 动作 | 命令 |
| --- | --- | --- |
| A 体检 | 读现状与缺口 | `cal_update.py check` |
| B 预填 | 补齐未来 4 个月官方日程 | 写 `scripts/cal_candidates.json` |
| C 核实 | 检索临时节点与改期 | 官方源核验，不确定的列为「待定」 |
| D 合并 | 只追加不删除 | `cal_update.py merge cand.json --update-notes` |

**选举动态日更**（每日 10:00）：检索 → 追加 `election-data` 的 items（倒序插入）
→ 核对评级/席位/民调（确有变化才改）→ 提交。

**政策研判周更**（周六 09:00）：读最近 7 天条目 → 撰写三个板块的 `weekly`
（每月 1 日另写 `monthly`）→ 直接编辑 `*-overview.json` → 提交。

自检要点：

- 开工先看 `manifest.json: built_at` 是否为当日，避免同日重复抓取。
- D 环节的选举板块当前**只有 `.ele-table` 的 CSS 定义、没有实际表格实例**，
  表格类更新无落点，改版前不要按旧结构写数据。
- 日历候选必须有官方来源核验（BLS/BEA/FOMC 日程、白宫、USTR 等），
  不确定的写进 `note` 而不要臆造日期。

---

## 8. 校验与故障排查

```bash
python3 scripts/validate_data.py            # 常规：结构硬错误 + 内容告警
python3 scripts/validate_data.py --strict   # 严格：新增条目缺 title_cn / domain 即失败
```

退出码：`0` 通过 / `1` 结构错误 / `2` 严格模式内容缺失。

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 三个板块全空白 | 预览未走 HTTP，或服务根目录不在仓库根 | 从仓库根起 `http.server` |
| `count 与实际不符` | 手工删改过 items | 改 `count`，或回滚该文件 |
| `条目未按日期倒序` | 新条目追加到尾部 | 按 `date` 倒序重排后重跑校验 |
| `出现词表外的领域标签` | 新造标签 | 改回词表值，或同步扩词表 |
| `url 重复` | 同一文件被两次抓取合并 | 去重保留信息更全的一条 |
| 简报合并后条数没变 | `briefs.json` 的键 url 不匹配 | 用数据文件里的真实 url 逐条复核 |
| 国会板块 0 条 | `DEMO_KEY` 限流 | 配置正式 key |

---

## 9. 定时任务分工

2026-10-25 起，原单一「周更」任务拆为 4 个按板块划分的任务。每个任务只负责
一个数据区块，改动文件互不重叠，时段错开，互不阻塞。

| 任务 | 频率 | 负责区块 | 改动文件 |
| --- | --- | --- | --- |
| 大事日历日更 | 每日 07:00 | `us-cal-data` | `index.html`（日历块） |
| 政策条目日更 | 每日 08:00 | 三个政策子板块 | `data/policy/*-items.json` |
| 选举动态日更 | 每日 10:00 | `election-data` | `index.html`（选举块） |
| 政策研判周更 | 周六 09:00 | 周/月研判 | `data/policy/*-overview.json` |

**分工原则**：

- **按数据区块拆，不按步骤拆**。抓取与译名/简报同属政策板块，合成一个任务；
  日历与选举各有独立的数据源与核实逻辑，各自成任务。
- **发布守门内嵌在各任务**，不做独立发布任务：每个任务结束前自行
  `validate_data.py --strict` → `git commit` → `git push`，校验不通过就不提交。
- **日历日更靠规则化预填，不靠每日检索**。FOMC、BLS、BEA、OPM 联邦假日
  都提前一年公布全年日程，日更的核心是滚动补齐未来 4 个月窗口 + 核实临时变更。
  官方源：`federalreserve.gov/monetarypolicy/fomccalendars.htm`、
  `bls.gov/schedule/{年}/home.htm`、`bea.gov/news/schedule`、
  `opm.gov/.../federal-holidays/holidays.ics`。
- **`monthly` 研判只在每月 1 日更新**，其余周六只更新 `weekly`。
- **选举动态临近选举日（2026-11-03）后需改版**（结果版图、第 120 届国会构成），
  届时先确认方案再改。

原「国金门户 · 美国政策周更抓取」任务已 PAUSED，其职责由上述 4 个任务覆盖。

---

## 10. 部署与已知缺口

**部署**：`main` 分支 push 后 GitHub Pages 自动重建，无需其他动作。
回滚用 `git revert <sha>`（优先）或 `git checkout <sha> -- <file>`，
`data/policy/` 与 `index.html` 可独立回滚。

以下为**已知缺口**，按优先级排列，是后续迭代的起点：

1. **`.github/workflows/weekly-update.yml` 未入库**。工作流（每周六 UTC 01:00
   跑 `gen_updates_json.py` 并提交 `updates.json`）目前只存在于
   `github自动更新部署包.zip` 里。当前周更实际由本地自动化执行并 push，
   云端定时任务并未生效。若要恢复云端兜底，需把工作流放回 `.github/workflows/`
   并在仓库 Secrets 配置 `CONGRESS_API_KEY`。
2. **`updates.json` 未入库**。页面自动更新模块的首选源是 `./updates.json`，
   缺失时静默降级到备源绝对地址再失败，功能实际处于「静默无更新」状态。
3. **`scripts/gen_europe_map.py` 缺失**。`index.html` 的欧洲地图注释引用了它，
   仓库中无此文件，地图轮廓目前只能手工维护。
4. **地缘局势 / 热点事件两个专栏无数据管线**，按钮无 `data-enter`，是占位。
5. **`.workbuddy/` 已加入 `.gitignore`**（2026-10-25 补），工作记忆与任务归档不再入库。
6. **`github自动更新部署包.zip`（263 KB）仍被版本控制跟踪**，内容已被仓库文件超越，
   建议归档到 `docs/` 或移出仓库。
7. **欧洲板块无数据源**：四张卡片为纯静态描述，接入抓取前需先定义
   `data/eu/*.json` 的 schema 与来源（欧盟机构官网 / 各国议会 / 选举委员会）。

---

## 11. 编码约定

- 脚本一律 `#!/usr/bin/env python3` + `# -*- coding: utf-8 -*-`，
  模块 docstring 写清用途、用法、退出码；只依赖标准库。
- 所有写盘走 `portal_store.save_policy()` 的原子写（临时文件 + `os.replace`），
  不要直接 `open(..., 'w')` 覆盖数据文件。
- 时间戳统一北京时间 `+08:00`（`TZ8`），日历事件的 `t` 字段是**美东时刻**，
  两者不要混用。
- 页面文案为简体中文书面语，标题用「动宾/偏正」短语（如「修订《联邦水污染控制法》
  第404条湿地许可范围」），不写口语化摘要、不堆修饰词。
- commit message 用中文，写清「改了什么 + 为什么」，数据类提交带日期与净增数，
  例如：`数据周更 2026-10-24：白宫 +0 / 国会 +0 / 独立机构 +0`。
