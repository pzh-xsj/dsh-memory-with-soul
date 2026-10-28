# adapters —— 一个中轴，每个 harness 一个薄壳

记忆系统的**中轴**是 `core/render.py`（渲染器）+ `~/.memory/config.json`（注入表）。
本目录只放**把中轴接到某个 harness 上**的薄壳，每个 100–200 行，不重复渲染逻辑。

```
~/.memory/config.json          注入表（priority / maxChars / tailLines）
        │
core/render.py                渲染器：读 config → 拼 <memory-context> → stdout
        │
        │
   adapters/dsh/            SessionStart hook 的 stdout 直接进上下文
```

## 契约：适配器只干四件事

| 动词 | 时机 | 干什么 |
| --- | --- | --- |
| **inject** | 会话开始 | 跑渲染器，把输出塞进模型能看见的第一处 |
| track | 读记忆库文件后 | 写访问戳记（`touch_access`），供 checkup 判「读过没」 |
| flush | 会话结束 | 落一条日志尾巴（`extraction_state` 判提炼） |
| probe | 随时 | 回答「我接上了吗、注入几字符、谁在读」 |

**第一版只实现 inject。** track / flush / probe 在 DSH 侧是 P1：
- track 需要 `tools/result` 监听 + 路径匹配
- flush 需要 `turn/end` 或会话结束事件
- probe 需要 `command.register`（做一个 `/memory-status`）

写之前先看 `dsh 的 practices.md` 两条硬规矩：注入必须走 `agent.inject()`（写进 session log，可重建）；
per-agent 的东西注册在 `agent/created` 拿到的 `agent.ctx` 上。

## 各 harness 怎么接

| harness | 注入点 | 身份来源 |
| --- | --- | --- |
| dsh (DeepSeek Harness) | bundle 监听 `agent/created` → `agent.inject(createUserMessage)` | `MEMORY_AGENT` > `defaultAgent`（0.6.6 起 agent_id 不再拼 `-dsh`） |

**0.6.5 起只剩 DSH 一个宿主。** WorkBuddy 的钩子（`hooks/hooks.json`、`post-read.py`、`stop.py`）与它的两层清单已删——WB 从 10-27 起冻结，目标是卸掉，不再维护第二个适配器。注入表仍然只有 `config.json` 一处定义，接第三个 harness 只需写 `adapters/<name>/`。

## adapters/dsh

```
adapters/dsh/
  index.js           apply(ctx, config)：agent/created → 渲染 → inject
  cordis.patch.yml   - insert: [{ id: memory-inject, name: 'dsh-memory-with-soul' }]
  locale/{zh,en}.json  meta.title / meta.description
  icon.svg           952 B
  smoke.mjs          19 项自检，不需要装进 DSH 就能跑
```

### 可调项（写在用户的 `cordis.patch.yml` 里，同 id 整行覆盖）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 关掉注入，不动其他 |
| `python` | `['python','python3','py']` | 按顺序试；含路径分隔符的当绝对路径 |
| `renderer` | 空 | 留空自动找 `core/render.py`；`sourceRoot` 只是兜底 |
| `sourceRoot` | 空 | 插件被复制进 node_modules（而非软链）时用它兜底找渲染器 |
| `harness` | `dsh` | 只进日志与注入头（0.6.6 起不再拼进 agent id） |
| `agentName` | 空 | 直接指定 agent id，留空则用 `config.defaultAgent` |
| `timeoutMs` | `10000` | 超时先 `terminate()` 再强杀 |
| `injectSubagents` | `false` | 子 agent 不该吃掉一份上下文（靠 `session.header.origin === 'subagent'` 判，**不能用 parentSession，fork 也带**） |
| `cwd` | 插件根 | 只影响相对路径解析 |

### 装

包根是一整个 npm 包，装它就等于装 Python 侧 + 设置页 + skills：

```bash
dsh plugin --profile desktop add dsh-memory-with-soul
```

开发时也可以让 profile 直接指向本仓库（`"dsh-memory-with-soul": "link:E:/path/to/memory-plugin"`，
见根目录 `README.md`）。装完重启一次 DSH 才会挂上。

### 验

```bash
cd <插件根>/adapters/dsh     # 包根是 <插件根>，adapters/dsh 只是它的子目录
node smoke.mjs          # 19/19 通过 = 清单、模块、渲染器、路由、左侧栏五关都过
```

真装进 DSH 之后再看两处：

1. `~/.memory/.state/session-start-<agent>.json` 里 `harness: dsh`、`agent` 与你设的 id 一致
2. 新会话里**没有**重复的手抄人格块——注入接管以后，`AGENTS.md` 只该留「人格摘要 + 路由 + 故障提示」，
   否则同一个人格吃两遍 token

### 故障特征

| 现象 | 去哪看 |
| --- | --- |
| 新会话没有记忆块 | 日志里找 `记忆注入` 的 warn：渲染器缺失 / 找不到 python / 非零退出 / 空输出 |
| 有块但 agent 名不对 | `MEMORY_AGENT` / `agentName` 与实际不符 |
| 会话建不起来 | 监听器抛了异常——`agent/created` 是 serial 派发，**抛错会回滚整个 agent 创建** |
| 块重复两遍 | 旧的手抄 `AGENTS.md` 没收缩（Step 0b） |

## 加一个新 harness

1. 在 `adapters/<name>/` 建壳，只调用 `core/render.py`，**不复制渲染逻辑**；
2. 身份：先设 `<NAME>_HARNESS` 等价物（只影响日志与注入头；agent id 由 `config.defaultAgent` 决定）；
3. 注入点必须是「模型能看见、且能重建」的位置——抄进 system prompt 不算；
4. 写一个不需要装进该 harness 的 smoke，覆盖「有项目册 / 无项目册 / 路径不存在」三种 cwd；
5. 在 README 的「各 harness 怎么接」表里加一行。

_（wb 那份已随 0.6.5 删除；要接新 harness 见上一节五步。）_
