# 头像的来源

私聊卡片的头像行用这里的图片（`covey/core/avatars.py` 管分配，`covey/feishu/avatars.py` 管上传）。运行时不访问外网。话题没有图片头像：它的 T 号就是头像（`covey/cards/ui.py` 的 `topic_badge`）。

| 文件 | 用在 | 来源和许可 |
| --- | --- | --- |
| `butler.png` | 管家 | 自绘原创，随本仓库的许可：靛色渐变圆底上一只系领结的白色鹌鹑（换图只换这个文件；换了内容飞书那边会重新上传） |
| `G-*.png` | 专项负责人（6 个） | DiceBear「Moods」，CC0 |
| `M-*.png` | 专项负责人（6 个） | DiceBear「Clay」，CC0 |
| `sea-*.png` | 专项负责人（8 个） | 自绘原创，随本仓库的许可 |

网页复用同一套图片（`web/scripts/sync-avatars.mjs` 把这个目录拷进 `web/client/src/cv/avatars/`，那份不入库）；专项使用同步条目里的 `avatar` 名称，不在浏览器重新分配。

原来的管家头像（由 Gemini 生成的 logo：奶油底两只叠着的鹌鹑）现在是网页的品牌标志，在 `web/client/src/cv/brand/covey.png`（浏览器标签页图标 `web/client/public/favicon.png` 由它缩成），不再当头像用。
