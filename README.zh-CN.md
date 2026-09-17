[English](README.md) | 简体中文

# pi-context-prune

**上下文 token −63%，六轮 A/B 实测，被裁掉的原文随时一句话取回。**

写这个是因为：长会话里模型花在反复重读自己工具输出上的钱，比花在思考上的还多。

> **来历。** 本项目是 [championswimmer/pi-context-prune](https://github.com/championswimmer/pi-context-prune)（MIT）的自维护分支，基于上游 1.4.0。上游的架构、命令与工具设计均来自原作者，本分支在其上修复了若干在弱模型、错误重试与用户停止场景下会失效的行为，并按自己的版本号发布。
>
> npm 包名 [`@syansunyang/pi-context-prune`](https://www.npmjs.com/package/@syansunyang/pi-context-prune)，从 1.5.0 起。未加作用域的 `pi-context-prune` 仍属上游。

[Pi coding-agent](https://github.com/badlogic/pi-mono) 扩展：把已完成的工具调用批次交给摘要模型压缩，之后的请求里用摘要替代原始工具输出，原文留在会话索引里，模型随时可用 `context_tree_query` 取回。

## 解决什么问题

长会话里每一次工具调用都往上下文里堆一份输出。绝大多数输出在第一次读完之后就不再需要原文，但它们会一直占着窗口，直到触发原生压缩把整段历史重写掉。

本扩展的做法是：在模型明确用完这批结果之后，把它们换成一份简短摘要，原文不删、只是不再进入后续请求。需要细节时模型自己去取。

## 安装

```bash
pi install npm:@syansunyang/pi-context-prune
```

只给当前项目装：

```bash
pi install -l npm:@syansunyang/pi-context-prune
```

也可以从 GitHub 按标签安装（`dist/` 已入库，安装时不执行构建）：

```bash
pi install git:github.com/syansunyang-collab/pi-context-prune@v1.5.1
```

## 快速开始

装完在 Pi 里依次执行：

```
/pruner on
/pruner model <provider/model-id>:low
/pruner max-chars 8000
```

第二条是关键。摘要默认用当前会话的模型，那样不省钱，应换成一个便宜的小模型。第三条决定摘要模型能看到每条工具结果的前多少字符，上游默认 2000，实测 8000 对质量更友好。

看状态与收益：

```
/pruner status
/pruner stats
```

## 触发模式

`/pruner prune-on <mode>` 设置，默认 `agent-message`。

| 模式 | 触发时机 | 适用 |
|---|---|---|
| `agent-message` | 模型给出不带工具调用的回复后 | 默认。任务边界清晰，被裁的是已经用完的结果 |
| `every-turn` | 每个带工具结果的回合结束后 | 省得最多，但会频繁打断前缀缓存 |
| `on-context-tag` | 模型调用 `context_checkpoint` 时 | 由模型自己划分阶段 |
| `agentic-auto` | 模型调用 `context_prune` 时 | 完全交给模型决定 |
| `manual` | 只在 `/pruner now` 时 | 手动控制 |

裁剪会改变请求前缀，因此每次裁剪都会让服务端的前缀缓存失效一次。裁得越频繁，省下的窗口越多，缓存命中越少。`agent-message` 是这两者之间的默认折中。

## 配置

配置文件在 `~/.pi/agent/context-prune/settings.json`，路径按用户主目录写死，不随 `PI_CODING_AGENT_DIR` 变化。也可用 `/pruner settings` 打开交互面板。

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | 总开关，装完需要手动打开 |
| `summarizerModel` | `default` | 摘要模型，`default` 表示跟随会话模型 |
| `summarizerThinking` | `default` | 摘要模型的推理档位 |
| `pruneOn` | `agent-message` | 触发模式 |
| `batchingMode` | `turn` | 批次划分，`turn` 或 `agent-message` |
| `summarizerMaxCharsPerResult` | `2000` | 每条工具结果送进摘要模型的字符上限，`0` 表示不截断 |
| `showPruneStatusLine` | `true` | 底部状态行 |
| `notifySkipped` | `true` | 跳过时提示 |

## 工具与命令

- `context_tree_query`：按工具调用 id 取回被裁剪的原文。模型在需要细节时自行调用，不需要人工干预。
- `context_prune`：仅 `agentic-auto` 模式下注册，让模型自己决定何时裁剪。
- `/pruner tree`：浏览已裁剪的调用树，`Ctrl-O` 打开某条摘要。
- `/pruner now`：立即处理待裁剪队列，带进度显示。
- `/pruner help`：完整命令列表。

## 相对上游的改动

| 版本 | 改动 | 修复的故障 |
|---|---|---|
| 1.5.0 | 被裁剪的工具结果留一条简短的桩，不再整条删除 | 删除后工具调用不配对。pi-ai 会补一条 `No result provided` 的**报错**结果，绕过该转换层的 provider 则把不配对的调用原样发出。两种情况下模型都会认为工具坏了，能力弱的模型直接罢工 |
| 1.5.0 | 以 `error` / `aborted` 结束的回合不算最终回复 | provider 返回 503 的空回复触发裁剪，自动重试时当前任务刚读的结果已经被裁掉 |
| 1.5.0 | 索引、统计与 frontier 在 `context` 钩子里按需重建 | `/reload` 后若别的扩展在 `session_start` 里触发新回合，首个请求完全不裁剪 |
| 1.5.0 | 新增 `summarizerMaxCharsPerResult` 与 `/pruner max-chars`；摘要请求带 OpenCode 会话头 | 上游固定 2000 字符上限，长结果被截断后摘要质量下降 |
| 1.5.1 | 钩子里的摘要调用绑定本轮停止信号，并加 180 秒截止 | Pi 的停止要等会话空闲，而摘要在钩子里被 await，摘要期间点停止无效 |
| 1.5.1 | 构建与打包检查脚本可在 Windows 下运行 | `rm -rf` 与直接 spawn `npm` 使 `npm publish` 在 Windows 上失败 |

完整记录见 [CHANGELOG.md](CHANGELOG.md)。英文 README 末尾的 Follow-up ideas 一节是上游的路线图，不是本分支的计划。
`PRUNING.md` 与 `.agents/` 下的开发笔记均出自上游，本分支原样保留。

## 实测数据

模型 `openai-codex/gpt-5.6-luna`，推理档 medium。每轮在同一个会话里连续解 17 道 SWE-bench Verified 的 Django 题，工作树是仓外的单提交副本、无历史、禁网，事后扫描会话确认没有越界读取。token 口径包含摘要模型的调用。

| 组 | 轮数 | token 中位数 | 解题均值 /17 | 原生压缩次数 |
|---|---|---|---|---|
| 不裁剪 | 6 | 26.0M | 12.7 | 6 |
| 裁剪，上限 2000 | 6 | 9.6M（−63%） | 11.3 | 0 |
| 裁剪，上限 8000 | 6 | 9.7M（−63%） | 11.7 | 0 |

token 降幅每一轮都在 53% 到 72% 之间，不裁剪的一组每轮都撞上原生压缩阈值，裁剪组一次都没有。

质量上要说实话：medium 档下裁剪组平均每 17 题少解约 1 题，差距集中在三道题上，其余 14 题各组表现一致。逐会话核对后，丢分不是因为信息缺失，被裁掉的内容与这几题无关；裁剪组在首次编辑前的调查步数明显偏少，像是行为层面的影响。同样这批题把推理档提到 high 跑一轮，三组为 14 / 14 / 13，之前一直丢的三题各组全部解出，token 降幅不变。

实用结论：模型在任务上游刃有余时，裁剪基本是白赚的；模型本就吃紧时，预留一点质量损失，优先用更大的截断上限，并考虑提高推理档位。

方法、逐轮原始数据与采集脚本见 [`bench/`](bench/README.md)。

## 已知限制

- 摘要模型的调用本身要花钱，`/pruner stats` 会累计。省下的窗口与这部分开销需要自己权衡。
- 摘要的延迟发生在触发边界上。1.5.1 起，钩子里发起的摘要绑定本轮停止信号并在 180 秒后放弃，因此不会挡住停止，放弃的批次留到下次触发。
- 每次裁剪都会让服务端前缀缓存失效一次。
- 配置路径按用户主目录写死，同一台机器上多个 agent 目录共用一份配置。
- 摘要质量取决于所选模型，太小的模型会丢掉关键标识符。

## 许可证

MIT。版权归上游作者与本分支各自的贡献者，见 [LICENSE](LICENSE)。
