# dsh-memory-with-soul

长期记忆库插件，给 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（DSH）用。

一句话：**让 AI 记住你**——会话开始时把记忆库注入上下文，读文件时记下它读过什么，回合结束时提醒它把结论写回文件。

- 纯 Markdown + git，不依赖任何数据库或云服务
- 记忆库放在 `~/.memory/`，可以单独 `git` 版本化、手动编辑、换机器带走
- 人格（角色卡）是一份 `SOUL.md`，写文件而不写配置
- 设置页挂在 DSH 侧边栏「记忆库」，七个页签

## 装之前

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| DSH | `>=0.1.0-rc.1 <0.3.0` | 宿主，插件以 bundle 形式挂进 profile |
| Node.js | `>=22` | 适配器与内置设置页的路由 |
| **Python** | **`>=3.10`** | **硬依赖**：核心逻辑（`core/memory.py`）是 Python，不是可选项 |

Python 不在 `PATH` 里也能用：在 `cordis.patch.yml` 的 `python` 候选里写绝对路径（见下面的配置表）。

## 安装

```bash
# 1. 装插件（profile 名按你的实际写；默认叫 desktop）
dsh plugin --profile desktop add dsh-memory-with-soul

# 2. 第一次用：建记忆库（不建就只有空人格，插件本身照常工作）
python ~/.dsh/profiles/desktop/node_modules/dsh-memory-with-soul/core/memory.py init
```

装好、重启 DSH 后，侧边栏会多出「记忆库」入口；新会话开头会出现一段 `<memory-context>`（里面是记忆库内容）。

Windows 用户注意：上面第 2 步的路径用 `%USERPROFILE%\.dsh\profiles\desktop\node_modules\dsh-memory-with-soul\core\memory.py`，或直接用设置页里的路径提示。

## 它做了什么

插件挂在 DSH 的三个生命周期点上：

| 时机 | 动作 | 落到哪 |
| --- | --- | --- |
| 会话开始 | 读 `~/.memory/config.json` 的注入表，把记忆拼成 `<memory-context>` 注入 | `core/render.py` 输出 → `agent.inject()` |
| 读文件（`read` / `ops_read_files`） | 记下被读的记忆库文件 | `core/memory.py track` |
| 回合结束 | 判断这一回合有没有「该提炼但没写日志」 | `core/memory.py digest` |

另外，插件加载时会把你装的 `skills/` 复制进 `~/.dsh/skills/`（复制，不是软链——版本升级后不会指向旧路径；内容变了下次加载自动刷新）。

## 记忆库长什么样

```
~/.memory/
├── config.json          # 注入表：哪些文件、多大预算、什么顺序
├── index.md             # 总览：东西在哪
├── README.md            # 给 AI 看的协议（保存/提炼/遗忘的规矩）
├── roles/<role>/SOUL.md # 角色卡：人格
├── playbooks/           # 跨项目方法论
├── topics/              # 单文件主题
├── user/                # 用户档案与偏好
├── agents/              # agent 注册表（每个 agent 一个目录）
├── archive/             # 归档（设置页只读）
└── .state/              # 运行时状态（gitignore）
```

项目级记忆在**各工作区**的 `.memory/`（`log/<agent>/YYYY-MM-DD.md` 就是每日日志），插件按 cwd 向上找 `_meta.md` 定位。

## 配置

装完后在 profile 的 `cordis.patch.yml` 里覆盖 `memory-inject` 那一行的 `config`（整行覆盖，不深合并）：

| 键 | 默认 | 作用 |
| --- | --- | --- |
| `enabled` | `true` | `false` 关掉插件 |
| `python` | `["python","python3","py"]` | 候选解释器，按顺序试；含路径分隔符的按绝对路径检查 |
| `renderer` | 空 | 渲染器路径，留空自动找 `core/render.py` |
| `harness` | `dsh` | 环境变量 `MEMORY_HARNESS` 的值，只影响日志与注入头 |
| `agentName` | 空 | 直接指定 agent id，留空按 `config.defaultAgent` |
| `timeoutMs` | `10000` | 渲染器超时（毫秒） |
| `injectSubagents` | `false` | 子 agent 也注入（每份记忆都是实付 token，默认省） |
| `trackAccess` | `true` | 记录读过哪些记忆文件 |
| `digest` | `true` | 回合结束判断要不要提醒提炼 |
| `installSkills` | `true` | 加载时把 `skills/` 复制进 `~/.dsh/skills/` |
| `webUi` | `true` | 挂内置设置页（`/api/memory/ui`） |
| `cwd` | 包根 | 渲染器工作目录，只影响相对路径解析 |

## 自检

```bash
node adapters/dsh/smoke.mjs       # 清单与渲染器（19 项）
node adapters/dsh/test.mjs        # 三条接线（11 项）
node adapters/dsh/web.test.mjs    # 内置设置页路由（47 项）
python ui/serve.py --self-test    # 独立设置页与核心逻辑（68 项）
python core/memory.py checkup     # 真库体检
```

## 已知边界

- 设置页只读写**总册**；项目册有自己的页签，日志落在项目册里。
- `archive/` 只读：归档要回答「当时为什么这么定」，改了就查不回去了。
- 角色卡与 agent 是两件事：`config.roles` 决定「谁在跑」用哪张卡，`defaultRole` 只做兜底（兜底能读、不能写）。
- DSH 是唯一支持的宿主。旧版 WorkBuddy 适配器已在 0.6.5 删除。

## English

A long-term memory plugin for DeepSeek Harness (DSH).

**What it does:** injects a Markdown memory library at session start, records which memory files the agent reads, and reminds it at turn end to write conclusions back. Plain Markdown + git — no database, no cloud.

**Requires:** DSH `>=0.1.0-rc.1 <0.3.0`, Node.js `>=22`, and **Python 3.10+** (the core is Python, not optional).

```bash
dsh plugin --profile desktop add dsh-memory-with-soul
python ~/.dsh/profiles/desktop/node_modules/dsh-memory-with-soul/core/memory.py init
```

Restart DSH afterwards: a "Memory" entry appears in the sidebar, and new sessions start with a `<memory-context>` block. The plugin also copies its skills into `~/.dsh/skills/` on load.

Configuration lives in the `memory-inject` row of your profile's `cordis.patch.yml`. See the Chinese section above for the full key table.

## License

MIT
