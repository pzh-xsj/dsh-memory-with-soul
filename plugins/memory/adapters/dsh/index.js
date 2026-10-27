/**
 * memory-inject — DSH 宿主插件：把记忆系统接进 DSH 的三个生命周期点。
 *
 *   1. 会话开始（agent/created）→ 注入 `<memory-context>`
 *   2. 工具调用（session/event tool/call）→ 记录记忆库文件被读过
 *   3. 回合结束（session/event turn/end）→ 判断要不要提醒提炼
 *
 * 三个渲染/判定都只调 `core/memory.py`，本文件不实现业务逻辑：
 *   注入   → hooks/session-start.py --dry --cwd <cwd>
 *   戳记   → core/memory.py track --paths ...
 *   提醒   → core/memory.py digest --agent ... --turns N --session ... --json
 *
 * 为什么注入走 agent.inject()：DSH 的 session log 是唯一事实源，注入必须记为
 * `agent/inbox/spliced` 事件，fork / resume / replay 才复现得出来。直接改
 * system prompt 会绕开这个机制。
 *
 * 时序：`agent/created` 是 serial 派发，监听器返回的 Promise 会被 await，
 * 所以这里可以 await 子进程：注入一定赶在第一步之前进入收件箱。
 *
 * 事件形状（宿主 lib/session 的类型表）：
 *   'tool/call': { turn, step, callId, name, arguments: string }
 *   'turn/end' : { turn, reason }
 * session/event 的 payload 是 (session, event)；重放/恢复的事件带 surfaceOp，
 * 只处理 surfaceOp === 'append' 的实时事件，否则 resume 一次就重跑一遍提醒。
 */
import { randomUUID } from 'node:crypto'
import { existsSync } from 'node:fs'
import { delimiter, dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { pageUrl, registerMemoryUi } from './web.js'

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
  /** 记录记忆库文件被读过（对应 WB 的 PostToolUse hook）。 */
  trackAccess: true,
  /** 回合结束时判断要不要提醒提炼（对应 WB 的 Stop hook）。 */
  digest: true,
  digestSubagents: false,
  /** 把设置页挂到 DSH 自己的端口下（/api/memory/ui）。 */
  webUi: true,
  /** 渲染器工作目录，只影响相对路径解析，与 --cwd 无关。 */
  cwd: PLUGIN_ROOT,
}

/** 读类工具名。DSH 内置的是 read / ops_read_files，斜杠写法兼容其他实现。 */
const READ_TOOLS = new Set(['read', 'ops_read_files', 'Read', 'ops.read'])

/** track 子进程的并发上限：一次会话读十几个文件时不该起十几个 python。 */
const MAX_CONCURRENT_CORE = 2

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

/** core/memory.py 的候选路径，找法与渲染器一致。 */
function coreCandidates(cfg) {
  const list = [join(PLUGIN_ROOT, 'core', 'memory.py')]
  if (cfg.sourceRoot !== '') list.push(join(cfg.sourceRoot, 'core', 'memory.py'))
  return [...new Set(list)]
}

/** `ui/api.py` 的候选路径：内置设置页每个请求都跑它一次。 */
function apiCandidates(cfg) {
  const list = [join(PLUGIN_ROOT, 'ui', 'api.py')]
  if (cfg.sourceRoot !== '') list.push(join(cfg.sourceRoot, 'ui', 'api.py'))
  return [...new Set(list)]
}

/** `ui/` 目录：内置设置页要从这里读 html 与 css。 */
function pagesDirOf(cfg) {
  const list = [join(PLUGIN_ROOT, 'ui')]
  if (cfg.sourceRoot !== '') list.push(join(cfg.sourceRoot, 'ui'))
  return list.find((path) => existsSync(join(path, 'settings.html')))
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
  bool('trackAccess')
  bool('digest')
  bool('digestSubagents')
  bool('webUi')
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

let cachedPython
async function resolvePython(ctx, cfg) {
  if (cachedPython !== undefined) return cachedPython
  for (const candidate of cfg.python) {
    if (/[\\/]/.test(candidate)) {
      if (existsSync(candidate)) {
        cachedPython = candidate
        return cachedPython
      }
      continue
    }
    try {
      const resolved = await ctx.subprocess.resolveExecutable(candidate)
      if (typeof resolved === 'string' && resolved !== '') {
        cachedPython = resolved
        return cachedPython
      }
    } catch {
      // 继续试下一个候选
    }
  }
  cachedPython = null
  return null
}

/**
 * 跑一次 core/memory.py。返回 `{ code, stdout, stderr }`；拿不到 python 或脚本时返回 null。
 *
 * 三个生命周期点共用它：注入走渲染器（session-start.py），其余两个走 CLI 子命令。
 */
async function run(ctx, cfg, script, args, options = {}) {
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
    argv: [python, script, ...args],
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
  const timeout = options.timeoutMs ?? cfg.timeoutMs
  const timer = setTimeout(() => {
    void Promise.resolve(handle.terminate()).catch(() => {})
  }, timeout)

  let outcome
  try {
    outcome = await handle.done
  } finally {
    clearTimeout(timer)
  }

  return {
    code: outcome.exitCode ?? 0,
    stdout: handle.collected.stdout?.readFrom(0)?.text ?? '',
    stderr: handle.collected.stderr?.readFrom(0)?.text ?? '',
  }
}

async function render(ctx, cfg, cwd) {
  const candidates = rendererCandidates(cfg)
  const renderer = candidates.find((path) => existsSync(path))
  if (renderer === undefined) {
    warnOnce(ctx, 'renderer', `找不到渲染器，试过：${candidates.join(' | ')}`)
    return null
  }

  const result = await run(ctx, cfg, renderer, ['--dry', '--cwd', cwd])
  if (result === null) return null
  if (result.stderr.trim() !== '') ctx.logger.info(`memory-inject: 渲染器 stderr: ${result.stderr.trim()}`)
  if (result.code !== 0) {
    warnOnce(ctx, `exit-${String(result.code)}`, `渲染器退出码 ${String(result.code)}，本次不注入`)
    return null
  }

  const text = result.stdout.trim()
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
 * 跑一个 CLI 子命令。找不到 core/memory.py 时静默跳过（只 toast 一次）。
 */
async function runCore(ctx, cfg, args, options = {}) {
  const candidate = coreCandidates(cfg).find((path) => existsSync(path))
  if (candidate === undefined) {
    warnOnce(ctx, 'core', '找不到 core/memory.py，记忆的戳记与提醒不可用')
    return null
  }
  return run(ctx, cfg, candidate, args, options)
}

/**
 * 跑一次 `ui/api.py`：内置设置页的每个接口请求都走这里。
 *
 * 超时给到 15s——overview 要跑一遍渲染器，比 track 重得多。
 */
async function runApi(ctx, cfg, args) {
  const candidate = apiCandidates(cfg).find((path) => existsSync(path))
  if (candidate === undefined) {
    warnOnce(ctx, 'api', '找不到 ui/api.py，内置设置页的接口不可用')
    return null
  }
  return run(ctx, cfg, candidate, args, { timeoutMs: Math.min(cfg.timeoutMs, 15000) })
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

/** 提炼提醒的消息。kind 与注入分开，方便在会话里一眼看出是哪一类。 */
export function reminderMessage(text) {
  return {
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: 'memory-digest' },
  }
}

/**
 * 从 tool/call 的参数里抽出「读了哪些文件」。
 *
 * DSH 内置工具：`read` 收 `{ file_path }`，`ops_read_files` 收 `{ files: [{ path, offset, limit }] }`。
 * 参数是 JSON 字符串，解析失败就当没读过——戳记不是关键路径，不值得抛错。
 */
export function readPaths(name, argsJson) {
  if (!READ_TOOLS.has(name)) return []
  let args
  try {
    args = typeof argsJson === 'string' ? JSON.parse(argsJson) : argsJson
  } catch {
    return []
  }
  if (args === null || typeof args !== 'object') return []

  const out = []
  for (const key of ['file_path', 'path', 'filePath']) {
    if (typeof args[key] === 'string' && args[key] !== '') out.push(args[key])
  }
  if (Array.isArray(args.files)) {
    for (const item of args.files) {
      if (item && typeof item === 'object' && typeof item.path === 'string' && item.path !== '') out.push(item.path)
    }
  }
  return [...new Set(out)]
}

/** session -> agent。session/event 只给 session，要发提醒得先找回 agent。 */
const sessionAgents = new WeakMap()

function agentOf(session) {
  return sessionAgents.get(session)
}

function isSubagent(agent) {
  const origin = agent.session?.header?.origin
  return origin === 'subagent' || origin === 'subagent-deep'
}

/** 简单并发闸：track 不该因为一次会话读了很多文件而堆几十个子进程。 */
function createLimiter(max) {
  let running = 0
  const queue = []
  const pump = () => {
    while (running < max && queue.length > 0) {
      const job = queue.shift()
      running += 1
      Promise.resolve()
        .then(job)
        .catch(() => {})
        .finally(() => {
          running -= 1
          pump()
        })
    }
  }
  return (job) => {
    queue.push(job)
    pump()
  }
}

async function injectMemory(ctx, cfg, agent, done) {
  if (!agent || typeof agent.inject !== 'function') return
  if (done.has(agent)) return
  done.add(agent)
  if (!cfg.injectSubagents && isSubagent(agent)) return

  const header = agent.session?.header ?? {}
  const cwd = typeof header.cwd === 'string' && header.cwd !== '' ? header.cwd : process.cwd()
  const text = await render(ctx, cfg, cwd)
  if (text === null) return

  // 必须 await：inject 的失败可能以 rejected promise 的形式逃出同步 try/catch。
  await agent.inject(injectedMessage(text))
}

function sessionIdOf(agent) {
  const id = agent.session?.header?.id
  return typeof id === 'string' && id !== '' ? id : 'unknown'
}

export function apply(ctx, config) {
  const cfg = normalize(ctx, config)
  if (!cfg.enabled) {
    ctx.logger.info('memory-inject: disabled by config')
    return
  }
  const injected = new WeakSet()
  const enqueue = createLimiter(MAX_CONCURRENT_CORE)

  ctx.on('agent/created', async (event) => {
    // serial 派发会 await 这个 Promise；抛出会让整个 agent 创建回滚，所以必须吞
    try {
      const agent = event?.agent
      if (agent?.session) sessionAgents.set(agent.session, agent)
      await injectMemory(ctx, cfg, agent, injected)
    } catch (error) {
      ctx.logger.warn(`memory-inject: 注入失败：${String(error)}`)
    }
  })

  ctx.on('agent/disposed', (event) => {
    const session = event?.agent?.session
    if (session) sessionAgents.delete(session)
  })

  // 访问戳记：记忆库里的子文件不在注入清单里，没这个记录就永远不知道哪些被读过。
  if (cfg.trackAccess) {
    ctx.on('session/event', (session, event) => {
      if (event.surfaceOp !== 'append') return
      if (event.type !== 'tool/call') return
      const agent = agentOf(session)
      if (agent === undefined) return
      if (!cfg.injectSubagents && isSubagent(agent)) return

      const paths = readPaths(event.data?.name, event.data?.arguments)
      if (paths.length === 0) return

      enqueue(async () => {
        await runCore(ctx, cfg, ['track', '--paths', paths.join(delimiter), '--agent', cfg.agentName || ''], {
          timeoutMs: Math.min(cfg.timeoutMs, 5000),
        })
      })
    })
  }

  // 提炼提醒：回合结束时问一次 core，命中就往会话里塞一条提醒。
  if (cfg.digest) {
    ctx.on('session/event', (session, event) => {
      if (event.surfaceOp !== 'append') return
      if (event.type !== 'turn/end') return
      const agent = agentOf(session)
      if (agent === undefined || typeof agent.inject !== 'function') return
      if (!cfg.digestSubagents && isSubagent(agent)) return

      const header = agent.session?.header ?? {}
      const cwd = typeof header.cwd === 'string' && header.cwd !== '' ? header.cwd : process.cwd()
      const turns = Number(event.data?.turn ?? 0)

      enqueue(async () => {
        try {
          const result = await runCore(ctx, cfg, [
            'digest',
            '--agent', cfg.agentName !== '' ? cfg.agentName : '',
            '--cwd', cwd,
            '--turns', String(Number.isFinite(turns) ? turns : 0),
            '--session', sessionIdOf(agent),
            '--json',
          ])
          if (result === null || result.code !== 2) return
          const parsed = JSON.parse(result.stdout)
          if (parsed?.should !== true || typeof parsed.text !== 'string' || parsed.text === '') return
          await agent.inject(reminderMessage(parsed.text))
        } catch (error) {
          ctx.logger.warn(`memory-inject: 提炼提醒失败：${String(error)}`)
        }
      })
    })
  }

  // 设置页：挂到宿主自己的端口下，不再起 127.0.0.1:8765。
  // 注册要等 connection 就绪；`ctx.inject` 在依赖到位时回调一次。
  if (cfg.webUi) {
    const pagesDir = pagesDirOf(cfg)
    if (pagesDir === undefined) {
      warnOnce(ctx, 'pages', '找不到 ui/settings.html，内置设置页不注册')
    } else {
      ctx.inject(['connection'], (inner) => {
        try {
          registerMemoryUi(inner, {
            pagesDir,
            exec: (args) => runApi(ctx, cfg, args),
            logger: ctx.logger,
          })
          const port = ctx.get('webServer')?.port
          ctx.logger.info(`memory-ui: 设置页 ${pageUrl(port)}`)
        } catch (error) {
          // 注册失败不该拖垮插件：注入、戳记、提醒都还该正常工作
          ctx.logger.warn(`memory-ui: 注册失败：${String(error)}`)
        }
      })
    }
  }
}
