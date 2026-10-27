# dsh-memory-inject

DSH 侧的记忆系统适配层。三件事：

1. **注入**——会话创建时把 `~/.memory` 渲染出的人格、偏好、项目册入口和最近日志，经 `agent.inject()` 放进新会话；
2. **戳记**——会话里 `read` / `ops_read_files` 读到的记忆库文件，把访问时间记到今天（体检据此判断哪些文件没人读）；
3. **提炼提醒**——回合结束时问一次 core，命中就往会话里塞一句提醒，同一段时间内不重复提醒。

**完整契约、可调项、故障排查在上一级：** [../README.md](../README.md)

## 装

Web 界面 → 左侧栏 **插件** → **添加插件** 旁的箭头 → **选择添加插件方式** → **安装第三方插件** →
「包名或地址」填本目录的绝对路径（`…\plugins\memory\adapters\dsh`）→ **安装** → 完成页点 **立即启用**。

绝对路径必须是这个目录本身，不能是它的父目录，也不能用相对路径。

## 验

```bash
node smoke.mjs          # 14/14：清单、模块加载、v4 来源准入、渲染器四关、静态接线
node test.mjs           # 9/9：用假宿主验三条接线（注入 / track / digest）
```

`test.mjs` 不启动 DSH，也不需要 python 真的跑完——`ctx.subprocess` 被换成记录器，只断言
`argv`、`env`、`cwd` 对不对。改了 `index.js` 的事件处理，先跑它。

装好后开一个新会话，注入块的首行应为 `# 长期记忆（agent: moxie-dsh，harness: dsh，库：…）`，
并在会话里读一个记忆库文件后，`~/.memory/.state/access-moxie-dsh.json` 的当天计数会增加。

## 结构

| 文件 | 作用 |
| --- | --- |
| `index.js` | 三条接线：`agent/created` → 渲染器 → `agent.inject()`；`session/event` 的 `tool/call` → `core/memory.py track`；`turn/end` → `core/memory.py digest` |
| `cordis.patch.yml` | 挂载行 `- insert: [{ id: memory-inject, … }]`，可调项都写在它的注释里 |
| `locale/{zh,en}.json` | 界面上的插件标题与描述 |
| `icon.svg` | 插件图标（952 B） |
| `smoke.mjs` | 不装进 DSH 也能跑的自检（清单 + 渲染器） |
| `test.mjs` | 假宿主下的接线测试（事件 → 子进程参数） |

## 身份

`agent` id 不在插件里写死：插件只设环境变量 `MEMORY_HARNESS`（默认 `dsh`），core 用
`<config.defaultAgent>-<harness>` 推出 `moxie-dsh`。要换 id，在 `cordis.patch.yml` 里设 `agentName`。

## 依赖

Python 3.10+（本机 `E:\python\python.exe`）。插件启动时按 `python` → `py` → `E:\python\python.exe`
顺序探测；全找不到时只警告一次，本会话不注入，其余功能不受影响。
