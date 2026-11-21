---
name: change-walkthrough
description: Write a one-page walkthrough of a large change so someone else can review it or pick it up: what changed and why, the key flows and where to look first, how to verify, and where to continue. Give it a commit range, a branch or a merge request; it reads the log and the diff and writes Markdown (optionally a single static HTML page) into the task's output directory, then lists the files in the checkpoint's artifacts. Use when asked to explain, hand over or summarize a big change, branch or MR for a colleague. Not for reviewing code for bugs (that is a review) and not for a changelog entry.
---

# 大改动的一页走读

## 什么时候用

要把一批大改动（几十个提交、几千行以上，或一个很大的分支、合并请求）讲给**另一个人**：让他能给意见，或者接着做。讲的人不是读者，所以读者要拿到的是判断，不是提交列表。只回答“这批改动是什么”，不查 bug、不评价写得好不好；要审查另做。

## 读者要在一页里拿到的四件事

1. **改了什么、为什么改**：先一句话结论，再按“用户或调用方会看到的变化”分几条，不按文件列。动机来自提交说明、关联的需求或讨论；找不到原因就写“没有找到原因”，不替作者编。
2. **关键流程和重点看哪里**：两三条主线（一次请求怎么走、数据怎么流、状态怎么变），每条写入口文件、经过哪几个模块、**最该看的一两处**（改了不变量、改了公共接口、迁移或兼容代码、没有测试覆盖的分支）。
3. **怎么验证**：哪些测试、命令、页面能证明它对；哪些**你跑过**、哪些只是**该跑**，分开写。没跑过的不要写成通过了。
4. **从哪里接上**：没做完的、已知问题、刻意没做的、下一步最小的一件事，以及接手前要知道的坑（开关、迁移顺序、临时代码）。

## 怎么做

1. **确定范围**：用户给的提交区间（`A..B`）、分支（和它的基线，通常是主干，`git merge-base` 取分叉点）或合并请求（有对应命令行工具就读它的描述和评论；读不到就请用户贴描述）。范围不明时问一句，不猜。
2. **读证据，只读不改**：`git log --reverse --format='%h %an %s' <范围>` 看顺序和意图；`git diff --stat <范围>` 和按目录汇总的 `git diff --dirstat` 看分量；挑改动最大、最靠近入口的文件读 `git diff <范围> -- <路径>`。大改动别读全文：先按目录分组，再顺着一条主线读。顺手查测试增删（`git diff --stat -- '*test*'`）。
3. **每个结论落到证据上**：提到的文件、函数、提交写出来（`路径:行号` 或短哈希），读者能自己点开；推断的内容标明“推断”。不要把没读过的文件说成读过。
4. **写成一页**：Markdown，正文 600 到 900 字，最多一屏半，开头一句话结论，之后按上面四件事分四节，每节 3 到 6 条短句；不放整段代码，不放提交清单（要放就只放关键的 3 到 5 个，附一句话）。不写“本次改动旨在”这类空话，不写作者的功劳。读者用什么语言，就用什么语言写。
5. **可选 HTML**：用户要网页版时，再写一个**单文件静态页**：内联 CSS，无脚本，无外链字体或图片，同一份内容，窄屏也能读；表格和代码用等宽字体。用户没要就只出 Markdown。
6. **写到哪里**：写到这个任务的输出位置：先看当前工作区的 `AGENTS.md` 或存储约定规定产物放哪，没规定就用任务给的输出目录，不写进被讲解的仓库、Bridge 的源码仓库或服务状态目录。文件名带范围，例如 `walkthrough-<分支或短哈希>.md`（和 `.html`）。已有同名文件先看看是不是上次的，别无声覆盖。
7. **登记产物**：把写出的文件的**绝对路径**放进本轮检查点的 `artifacts`，回复里说写在哪、一句话讲清这批改动、哪些没验证。登记只说明文件准备好了，发送由任务卡处理，不要自己往飞书发。

## 规则

- 不改被讲解的仓库：不提交、不切分支、不整理；要切到某个提交看内容，用 `git show <提交>:<路径>` 或临时的 worktree，用完删掉。
- 不写密钥、令牌、账号和内部地址；diff 里碰到了也不抄进走读。
- 没有的东西就说没有：没有测试、没有说明、读不到合并请求，都直接写出来，这本身就是读者要的信息。
- 范围太大一页放不下时，先写这一页的“总览”，并列出建议分几次讲的几块，每块一句话；不要把一页写成三页。
