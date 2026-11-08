# GEMINI.md

本项目的协作约定统一写在 `AGENTS.md`，设计令牌在 `DESIGN.md`，进度备注在 `docs/design/PROGRESS.md`。
请先读这三份，再改 `index.html`。

## 关键事实

- 纯静态单页，**无构建流程**。`index.html` 一个文件 1.35 MB、约 14665 行。
- 本地预览必须走 HTTP：`python3 -m http.server 8000`。`file://` 下政策板块全部空白。
- 脚本仅用 Python 标准库，不需要安装任何依赖。

## 三条红线

1. **不碰内嵌 JSON 数据块**：`id="election-data"` 与 `id="us-cal-data"` 由定时任务每日写入。
2. **不跑格式化工具**：会重排整文件，产生上万行噪声 diff。
3. **不动公用样式类**：`.card` `.subhead` `.col-head` `.wrap` `.section` 被多个视图复用。

## 工作流

同一台电脑上多个 AI 工具共用这个目录，没有副本要合并。唯一要守的是
**同一时刻只让一个工具改 `index.html`**（同时编辑会静默覆盖，git 检测不到）。

```bash
git log -5 --oneline                       # 上一个 AI 改了什么
python3 scripts/design_check.py progress   # 已提交改动 + 工作区残留
# 改样式与 DOM，单次 ≤150 行 diff
python3 scripts/design_check.py verify     # 越界检查，非 0 必须回退
git commit -m "design(<位置>): <说明>"    # 本地存档即可
```

`git push` 只是异地备份，不必每次推。

位置标签：`us` `columns` `coverage` `notes` `about` `eu` `board` `election` `party` `tokens`
（完整锚点表见 `AGENTS.md` §2）。
