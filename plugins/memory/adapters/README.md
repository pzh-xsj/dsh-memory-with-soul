# adapters —— 一个中轴，每个 harness 一个薄壳

记忆系统的**中轴**是 `hooks/session-start.py`（渲染器）+ `~/.memory/config.json`（注入表）。
本目录只放**把中轴接到某个 harness 上**的薄壳，每个 100–200 行，不重复渲染逻辑。

```
~/.memory/config.json          注入表（priority / maxChars / tailLines）
        │
hooks/session-start.py         渲染器：读 config → 拼 <memory-context> → stdout
        │
   ┌────┴─────────────────────────┐
adapters/wb/                  adapters/dsh/
（还没搬，仍在           SessionStart hook 的 stdout 直接进上下文
 hooks/hooks.json 里）
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
| wb (WorkBuddy / CodeBuddy) | `hooks/hooks.json` 的 SessionStart，**stdout 直接进上下文** | `payload.agent_type` → `config.defaultAgent` |
| dsh (DeepSeek Harness) | bundle 监听 `agent/created` → `agent.inject(createUserMessage)` | `MEMORY_HARNESS=dsh` → `<defaultAgent>-dsh` |

两边共用同一份 `config.json`，所以「注入什么、注多少」永远只有一处定义。

## adapters/dsh

```
adapters/dsh/
  index.js           apply(ctx, config)：agent/created → 渲染 → inject
  package.json       @local/dsh-memory-inject，dsh.bundle.patch 指向 cordis.patch.yml
  cordis.patch.yml   - insert: [{ id: memory-inject, name: '@local/dsh-memory-inject' }]
  locale/{zh,en}.json  meta.title / meta.description
  icon.svg           952 B
  smoke.mjs          9 项自检，不需要装进 DSH 就能跑
```

### 可调项（写在用户的 `cordis.patch.yml` 里，同 id 整行覆盖）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 关掉注入，不动其他 |
| `python` | `['python','py','E:\python\python.exe']` | 按顺序试；含路径分隔符的当绝对路径 |
| `renderer` | 空 | 留空自动找：先「插件根/hooks/」，再「sourceRoot/hooks/」 |
| `sourceRoot` | 本仓库的 `plugins/memory` | 插件被复制进 node_modules（而非软链）时用它兜底找渲染器 |
| `harness` | `dsh` | 决定 agent id 后缀 |
| `agentName` | 空 | 直接指定 agent id，留空则 `<config.defaultAgent>-<harness>` |
| `timeoutMs` | `10000` | 超时先 `terminate()` 再强杀 |
| `injectSubagents` | `false` | 子 agent 不该吃掉一份上下文（靠 `session.header.origin === 'subagent'` 判，**不能用 parentSession，fork 也带**） |
| `cwd` | 插件根 | 只影响相对路径解析 |

### 装（必须由人工点，命令行装不了）

1. Web 左侧栏点 **插件**
2. **添加插件** 旁的箭头 → **选择添加插件方式** → **安装第三方插件**
3. 「包名或地址」填绝对路径（相对路径 Host 直接拒）：
   ```
   C:\Users\<user>\.workbuddy-ai\plugins\marketplaces\local-moxie\plugins\memory\adapters\dsh
   ```
4. **安装** → pnpm 跑完 → 完成页点 **立即启用**（不点就是装好了但关着）
5. 提示 **请重启 DSH 使更改生效** 就重启一次

- 装完看 `application` / `warnings`：`overridden` = 被高层覆盖，`restart-required` = 还没生效。
- **不要**手改 `~/.dsh/profiles/desktop/package.json` 或 `cordis.patch.yml`，也不要跑 pnpm。

### 验

```bash
cd "C:/Users/<user>/.workbuddy-ai/plugins/marketplaces/local-moxie/plugins/memory/adapters/dsh"
node smoke.mjs          # 9/9 通过 = 清单、模块、渲染器三关都过
```

真装进 DSH 之后再看两处：

1. `~/.memory/.state/session-start-moxie-dsh.json` 里 `harness: dsh`、`agent: moxie-dsh`
2. 新会话里**没有** `~/.dsh/AGENTS.md` 那份手抄块——装上注入后它必须收缩成「人格 + 路由 + 故障提示」，
   否则同一个人格吃两遍 token（这就是 Step 0b，等安装成功再做）

### 故障特征

| 现象 | 去哪看 |
| --- | --- |
| 新会话没有记忆块 | 日志里找 `记忆注入` 的 warn：渲染器缺失 / 找不到 python / 非零退出 / 空输出 |
| 有块但 agent 名是 `moxie-unknown` | `MEMORY_HARNESS` 没传进去（`inject` 里少了 `subprocess`） |
| 会话建不起来 | 监听器抛了异常——`agent/created` 是 serial 派发，**抛错会回滚整个 agent 创建** |
| 块重复两遍 | 旧的手抄 `AGENTS.md` 没收缩（Step 0b） |

## 加一个新 harness

1. 在 `adapters/<name>/` 建壳，只调用 `hooks/session-start.py`，**不复制渲染逻辑**；
2. 身份：先设 `<NAME>_HARNESS` 等价物，让 `_state.py` 推出 `<defaultAgent>-<name>`；
3. 注入点必须是「模型能看见、且能重建」的位置——抄进 system prompt 不算；
4. 写一个不需要装进该 harness 的 smoke，覆盖「有项目册 / 无项目册 / 路径不存在」三种 cwd；
5. 在 README 的「各 harness 怎么接」表里加一行。

_（wb 那份还在 `hooks/hooks.json` 里没搬过来：`plugin.json` 的 `"hooks": "./hooks/hooks.json"` 指着真实路径，搬动会断。等 dsh 这条跑顺了再收。）_
