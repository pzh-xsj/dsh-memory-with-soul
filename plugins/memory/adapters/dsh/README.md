# dsh-memory-inject

会话创建时把 `~/.memory` 渲染出的人格、偏好、项目册入口和最近日志，经 `agent.inject()` 放进新会话的收件箱。

**完整契约、可调项、故障排查在上一级：** [../README.md](../README.md)

## 装

Web 界面 → 左侧栏 **插件** → **添加插件** 旁的箭头 → **选择添加插件方式** → **安装第三方插件** →
「包名或地址」填本目录的绝对路径（`…\plugins\memory\adapters\dsh`）→ **安装** → 完成页点 **立即启用**。

绝对路径必须是这个目录本身，不能是它的父目录，也不能用相对路径。

## 验

```bash
node smoke.mjs          # 10/10 通过 = 清单、模块、v4 来源准入、渲染器四关都过
```

装好后开一个新会话，注入块的首行应为 `# 长期记忆（agent: moxie-dsh，harness: dsh，库：…）`。

## 结构

| 文件 | 作用 |
| --- | --- |
| `index.js` | 监听 `agent/created` → 跑 `hooks/session-start.py --dry --cwd <会话 cwd>` → `agent.inject()` |
| `cordis.patch.yml` | 挂载行 `- insert: [{ id: memory-inject, … }]`，可调项都写在它的注释里 |
| `locale/{zh,en}.json` | 界面上的插件标题与描述 |
| `icon.svg` | 插件图标（952 B） |
| `smoke.mjs` | 不装进 DSH 也能跑的自检 |
