# CLAUDE.md

本项目的协作约定**统一写在 `AGENTS.md`**，请先完整读它，再动手。

## 必读顺序

1. `AGENTS.md` —— 三条禁区、可编辑区域、同步协议、提交规范
2. `DESIGN.md` —— 颜色/字体/尺寸令牌，两色分工硬规矩
3. `docs/design/PROGRESS.md` —— 为什么这么改（可选，判断进度请看 git log）

## Claude Code 的三条额外提醒

**1. 不要用格式化工具。**
不要执行 `prettier`、`npx prettier --write`、`eslint --fix`、`html-beautify`，
也不要用 Edit 工具"顺手"重排整段缩进。`index.html` 是 1.35 MB 单文件，
一次全量重排会产生上万行 diff，直接冲掉当日定时任务写入的数据。

**2. 改前先跑自检，改后必须再跑。**

```bash
python3 scripts/design_check.py progress   # 看别人刚改了什么
# ... 改动 ...
python3 scripts/design_check.py verify     # 越界检查，退出码非 0 不许提交
```

**3. 预览必须走 HTTP。**
`file://` 打开 `index.html` 时，四个政策板块会全部空白——这是 `fetch()` 的跨源限制，
不是你的改动造成的问题。正确方式：

```bash
python3 -m http.server 8000
# http://localhost:8000/index.html
```

## 不要做的事

- 不要引入构建工具或把文件拆成组件（会打断定时任务对内嵌 JSON 块的写入）
- 不要新增或删除 `data-page-node-id` 属性（上游可视化编辑器的绑定锚）
- 不要改基础样式块（`index.html` 第 10–1625 行）的公用类
- 不要碰 `election-data` / `us-cal-data` 两个 JSON 块
- 不要把密钥、`logs/`、`briefs.json` 提交入库

## 定时任务的存在感

每日 07:00（日历）与 10:00（选举）会直接写 `index.html`。
若你的提交落在这两个时点前后 10 分钟内，`git push` 可能被拒，
`git pull --rebase` 后重试即可，**不要 force push**。
