/**
 * memory-inject — DSH 宿主插件：会话创建时把记忆注入 agent 收件箱。
 *
 * 渲染器与 WorkBuddy 完全共用：hooks/session-start.py --dry --cwd <cwd>。
 * 本文件只负责三件事：找到 cwd、跑渲染器、把结果经 agent.inject() 送进会话。
 *
 * 为什么走 agent.inject()：DSH 的 session log 是唯一事实源，注入必须记为
 * `agent/inbox/spliced` 事件，fork / resume / replay 才复现得出来。直接改
 * system prompt 会绕开这个机制。
 *
 * 时序：`agent/created` 是 serial 派发，监听器返回的 Promise 会被 await，
 * 所以这里可以 await 子进程：注入一定赶在第一步之前进入收件箱。
 */
import { randomUUID } from 'node:crypto'
import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN_ROOT = join(HERE, '..', '..')

/** 子进程服务：`ctx.subprocess`（@deepseek-ai/dsh-subprocess）。 */
export const inject = ['subprocess']

const DEFAULTS = {
  enabled: true,
  /** 依次尝试；含路径分隔符的当绝对路径检查存在性，其余交给 resolveExecutable。 */
  python: ['python', 'py', 'E:\\python\\python.exe'],
  /** 留空 = 自动找：先 PLUGIN_ROOT/hooks，再 sourceRoot/hooks。 */
  renderer: '',
  /** 插件源码目录。装成真实副本时 PLUGIN_ROOT 会指向 node_modules，靠它兜底。 */
  sourceRoot: 'C:\\Users\\<user>\\.workbuddy-ai\\plugins\\marketplaces\\local-moxie\\plugins\\memory',
  /** 让渲染器把身份解析成 <defaultAgent>-dsh（默认 moxie-dsh）。 */
  harness: 'dsh',
  /** 覆盖 agent id；留空表示由 MEMORY_HARNESS + config.defaultAgent 推导。 */
  agentName: '',
  timeoutMs: 10000,
  /** 子 agent 默认不注入：每个 subagent 多一份记忆就是实付 token。 */
  injectSubagents: false,
  /** 渲染器工作目录，只影响相对路径解析，与 --cwd 无关。 */
  cwd: PLUGIN_ROOT,
}

/**
 * 渲染器的候选路径。DSH 用 pnpm 装本地目录时通常留软链，PLUGIN_ROOT 就是源码目录；
 * 但若被复制进 node_modules，PLUGIN_ROOT 会变成 `…/node_modules/@local`，
 * 所以再退一步用 sourceRoot。
 */
function rendererCandidates(cfg) {
  const list = []
  if (cfg.renderer !== '') list.push(cfg.renderer)
  list.push(join(PLUGIN_ROOT, 'hooks', 'session-start.py'))
  if (cfg.sourceRoot !== '') list.push(join(cfg.sourceRoot, 'hooks', 'session-start.py'))
  return [...new Set(list)]
}

/**
 * 校正用户配置。非法值一律 warn 后回落默认——宁可注入一次内容不对的记忆，
 * 也不要在 agent 创建阶段抛异常（监听器失败会让整个 agent 回滚）。
 */
function normalize(ctx, raw) {
  const cfg = { ...DEFAULTS }
  const warn = (msg) => ctx.logger.warn(`memory-inject: ${msg}`)
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
    if (raw !== undefined) warn('config 不是对象，全部用默认值')
    return cfg
  }

  const bool = (key) => {
    if (raw[key] === undefined) return
    if (typeof raw[key] === 'boolean') cfg[key] = raw[key]
    else warn(`config.${key} 应为 boolean，忽略`)
  }
  const text = (key) => {
    if (raw[key] === undefined) return
    if (typeof raw[key] === 'string') cfg[key] = raw[key].trim()
    else warn(`config.${key} 应为 string，忽略`)
  }
  const posInt = (key) => {
    if (raw[key] === undefined) return
    if (Number.isSafeInteger(raw[key]) && raw[key] > 0) cfg[key] = raw[key]
    else warn(`config.${key} 应为正整数，忽略`)
  }

  bool('enabled')
  bool('injectSubagents')
  text('renderer')
  text('sourceRoot')
  text('harness')
  text('agentName')
  text('cwd')
  posInt('timeoutMs')

  if (raw.python !== undefined) {
    const list = Array.isArray(raw.python) ? raw.python : [raw.python]
    const kept = list.filter((item) => typeof item === 'string' && item.trim() !== '').map((item) => item.trim())
    if (kept.length === 0) warn('config.python 没有可用的候选，用默认值')
    else cfg.python = kept
  }

  if (cfg.python.length === 0) cfg.python = DEFAULTS.python
  return cfg
}

/**
 * 插件实例内的告警去重：这几个失败每次会话都会发生，不节流会把日志刷爆。
 */
const warned = new Set()
function warnOnce(ctx, key, message) {
  if (warned.has(key)) return
  warned.add(key)
  ctx.logger.warn(`memory-inject: ${message}`)
}

async function resolvePython(ctx, cfg) {
  for (const candidate of cfg.python) {
    if (/[\\/]/.test(candidate)) {
      if (existsSync(candidate)) return candidate
      continue
    }
    try {
      const resolved = await ctx.subprocess.resolveExecutable(candidate)
      if (typeof resolved === 'string' && resolved !== '') return resolved
    } catch {
      // 继续试下一个候选
    }
  }
  return null
}

async function render(ctx, cfg, cwd) {
  const candidates = rendererCandidates(cfg)
  const renderer = candidates.find((path) => existsSync(path))
  if (renderer === undefined) {
    warnOnce(ctx, 'renderer', `找不到渲染器，试过：${candidates.join(' | ')}`)
    return null
  }
  const python = await resolvePython(ctx, cfg)
  if (python === null) {
    warnOnce(ctx, 'python', `找不到 python（试过：${cfg.python.join(', ')}）`)
    return null
  }

  const env = {
    MEMORY_HARNESS: cfg.harness,
    // Windows 控制台默认 cp936，不强制这两项中文会变成乱码或 UnicodeEncodeError
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
  }
  if (cfg.agentName !== '') env.MEMORY_AGENT = cfg.agentName

  const handle = ctx.subprocess.spawn({
    argv: [python, renderer, '--dry', '--cwd', cwd],
    cwd: existsSync(cfg.cwd) ? cfg.cwd : PLUGIN_ROOT,
    env,
    stdio: {
      stdin: 'ignore',
      stdout: { maxBytes: 512 * 1024 },
      stderr: { maxBytes: 128 * 1024 },
    },
    graceMs: 3000,
  })

  // subprocess 的 graceMs 是「终止后到强杀」的宽限，不是超时，得自己掐表
  const timer = setTimeout(() => {
    void Promise.resolve(handle.terminate()).catch(() => {})
  }, cfg.timeoutMs)

  let outcome
  try {
    outcome = await handle.done
  } finally {
    clearTimeout(timer)
  }

  const stdout = handle.collected.stdout?.readFrom(0)?.text ?? ''
  const stderr = handle.collected.stderr?.readFrom(0)?.text ?? ''
  if (stderr.trim() !== '') ctx.logger.info(`memory-inject: 渲染器 stderr: ${stderr.trim()}`)
  if (outcome.exitCode !== 0) {
    warnOnce(ctx, `exit-${String(outcome.exitCode)}`, `渲染器退出码 ${String(outcome.exitCode)}，本次不注入`)
    return null
  }

  const text = stdout.trim()
  if (text === '') {
    warnOnce(ctx, 'empty', '渲染器没有输出，本次不注入')
    return null
  }
  if (!text.startsWith('<memory-context>')) {
    warnOnce(ctx, 'marker', '渲染器输出缺少 <memory-context> 头，仍然注入')
  }
  return text
}

/**
 * 构造注入消息。抽成导出函数，好让 smoke 直接按宿主的准入规则校验它。
 *
 * source.kind 必须是「生产者自有的 kind」：session format v4 只拒绝空串与退役的 'plugin'
 * （宿主 lib/types/message-sources.js 的 source()：kind 非空且 !== 'plugin' 才放行；
 *   V3 的包装写法是 { kind:'plugin', plugin:'<包名>' }，宿主迁移器会翻成 'plugin:<包名>' 或实名）。
 * 用 'plugin' 会在 Session.append 的准入阶段抛 SessionFormatError，把整个 agent 创建带崩。
 */
export function injectedMessage(text) {
  return {
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: 'memory-inject' },
  }
}

async function injectFor(ctx, cfg, done, event) {
  const agent = event?.agent
  if (!agent || typeof agent.inject !== 'function') return
  if (done.has(agent)) return
  done.add(agent)

  const header = agent.session?.header ?? {}
  if (!cfg.injectSubagents && header.origin === 'subagent') return

  const cwd = typeof header.cwd === 'string' && header.cwd !== '' ? header.cwd : process.cwd()
  const text = await render(ctx, cfg, cwd)
  if (text === null) return

  // 必须 await：inject 的失败可能以 rejected promise 的形式逃出同步 try/catch。
  await agent.inject(injectedMessage(text))
}

export function apply(ctx, config) {
  const cfg = normalize(ctx, config)
  if (!cfg.enabled) {
    ctx.logger.info('memory-inject: disabled by config')
    return
  }
  const done = new WeakSet()
  ctx.on('agent/created', async (event) => {
    // serial 派发会 await 这个 Promise；抛出会让整个 agent 创建回滚，所以必须吞
    try {
      await injectFor(ctx, cfg, done, event)
    } catch (error) {
      ctx.logger.warn(`memory-inject: 注入失败：${String(error)}`)
    }
  })
}
