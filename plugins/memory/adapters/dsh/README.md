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
node smoke.mjs          # 17/17：清单、模块加载、v4 来源准入、渲染器四关、路由表、左侧栏
node test.mjs           # 10/10：用假宿主验三条接线（注入 / track / digest）
node web.test.mjs       # 39/39：内置设置页的十条路由（入口层 + 真起 python 打每条路由）
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
| `web.js` | 把 `ui/settings.html` 和十条 `/api/memory/*` 路由挂进宿主端口，页面不用另起服务 |
| `client.js` | 浏览器半边：`main` 面板 + 左侧栏 `sidebar.panellist` 行 + 设置分区，内容都是 iframe 装 `/api/memory/ui` |
| `locale/{zh,en}.json` | 界面上的插件标题与描述 |
| `icon.svg` | 插件图标（952 B） |
| `smoke.mjs` | 不装进 DSH 也能跑的自检（清单 + 渲染器 + 路由表 + 左侧栏声明） |
| `test.mjs` | 假宿主下的接线测试（事件 → 子进程参数） |
| `web.test.mjs` | 设置页回归测试（入口层假 ctx + 接线层真起 `python ui/api.py`） |

## 设置页

三处入口，同一份 `ui/settings.html`：主界面面板（`main`）、左侧栏行、设置分区。面板里带 `?embed=1`，
页面据此加 `body.embed`——只收起品牌和标题（外壳已经有了），**不收起「连接本地目录…」**。
这条 CSS 曾经是 `display:none`，结果嵌进宿主以后连接入口整个消失，用户只能猜。

写入有两条通道，目录授权降级成兜底：

| 通道 | 端点 | 用途 |
| --- | --- | --- |
| 服务端 | `POST /api/persona`（只认 `SOUL.md` / `IDENTITY.md`） | 人格页三张卡（身份 / 称呼 / 人格）整份回写 |
| 服务端 | `POST /api/file`（只认 `.md`，不收 `.state` / `.git`） | 记忆页自由文本编辑 |
| 浏览器目录授权 | File System Access API | 没连服务端时的兜底 |

两条服务端通道都带 mtime 冲突检测，写前备份到 `.state/ui-backups/`。
人格页按 `## ` 分节编辑：`SOUL.md` 是 原则 / 沟通（含 `### 提问`、`### 语气`）/ 禁区 / 记忆，
`IDENTITY.md` 的 `## 称呼` 单拎成一张卡。节名是页面分卡片的依据，改名前先看 `parseSoul()`。

## 身份

`agent` id 不在插件里写死：插件只设环境变量 `MEMORY_HARNESS`（默认 `dsh`），core 用
`<config.defaultAgent>-<harness>` 推出 `moxie-dsh`。要换 id，在 `cordis.patch.yml` 里设 `agentName`。

## 依赖

Python 3.10+（本机 `E:\python\python.exe`）。插件启动时按 `python` → `py` → `E:\python\python.exe`
顺序探测；全找不到时只警告一次，本会话不注入，其余功能不受影响。
