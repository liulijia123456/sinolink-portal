# 设计进度

> 本文件**只记「为什么这么改」**，不记「谁改了什么」。
> 谁改了什么看 `git log`，线上是什么样看部署地址，这两者不会过期，本文件会。
>
> 查看自动汇总：`python3 scripts/design_check.py progress`

---

## 1. 当前状态

| 项 | 值 |
| --- | --- |
| 线上地址 | <https://liulijia123456.github.io/sinolink-portal/> |
| 设计系统 | v2 首页 / v3 美国板块 / v4 政党，三套并行，令牌值相同、前缀不同 |
| 页面规模 | 14665 行，1.35 MB，单文件无构建 |
| 最近更新 | 以 `design_check.py progress` 输出为准，此处不维护 |

**已知未完成**（不是设计问题，是功能缺口）：欧洲板块纯静态无管线、政党板块用示例数据、
`updates.json` 未入库、仓库无 CI 兜底。

---

## 2. 进行中

改动开始时可在此加一行，完成后删除。格式：`- [平台] 目标 · 开始时间`

```
（当前无进行中的设计改动）
```

> 这一栏是唯一需要手工维护的部分，作用是让其他平台知道「有人正在改，先别动同一处」。
> 不写也不会出错，只是撞车概率高一点。

---

## 3. 决策备注（只追加，倒序）

只记**非显然**的决定。显而易见的改动（调字号、改间距）不必写。

| 日期 | 位置 | 平台 | 为什么这么改 |
| --- | --- | --- | --- |
| 2026-11-09 | 协作机制 | WorkBuddy | 取消区块认领，改为「共享编辑 + 小步提交」：认领制的协调成本高于冲突成本，页面设计单次改动小、回退便宜，没必要分工 |
| 2026-11-09 | 协作机制 | WorkBuddy | 进度不靠手工台账，改为 git log + 自动生成：手工台账在日更节奏下必然失效 |
| 2026-11-09 | `tokens` | WorkBuddy | 铜金值在 v2/v3/v4 三处重复定义，改色需同步 6 行，已在 `DESIGN.md` §2.3 列清单 |

---

## 4. 线上核对方法

push 后约 40 秒生效，CDN 缓存最长约 10 分钟。核对时带时间戳绕过缓存：

```bash
curl -s "https://liulijia123456.github.io/sinolink-portal/data/policy/manifest.json?v=$(date +%s)" | head -5
```

页面本身刷新时用 `Cmd+Shift+R` 强制重载。

---

## 5. 常用命令

```bash
git pull --rebase --autostash                 # 动手前
python3 scripts/design_check.py progress      # 看别人改了什么
python3 scripts/design_check.py verify        # 越界检查
python3 scripts/design_check.py regions       # 打印当前区块边界
python3 -m http.server 8000                   # 本地预览
```
