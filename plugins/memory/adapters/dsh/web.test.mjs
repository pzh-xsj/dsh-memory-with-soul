/**
 * 内置设置页的回归测试。
 *
 * 分两层：
 * 1. **入口层**——用假 cordis ctx 跑 `apply()`，确认三条监听挂上、十条路由注册、
 *    启动日志打出页面地址。这一层不碰 Python，跑得快。
 * 2. **接线层**——真起 `python ui/api.py` 打每一条路由，确认路径拼对、参数转对、
 *    错误翻成 4xx。这一层要 Python，约 3 秒。
 *
 * 没覆盖的：DSH 真机（浏览器带 cookie 访问 `/api/memory/ui`）。那需要重启宿主。
 *
 * 跑法：`node adapters/dsh/web.test.mjs`
 */
import { spawn } from 'node:child_process'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = resolve(HERE, '..', '..')

const { API_PREFIX, pageUrl, registerMemoryUi } = await import(`file:///${HERE}/web.js`)
const mod = await import(`file:///${HERE}/index.js`)

const results = []
const check = (name, ok, detail = '') => {
  results.push({ name, ok, detail })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail === '' ? '' : `  — ${detail}`}`)
}

/** 假 cordis ctx：effect 立即执行，inject 视为依赖就绪。 */
function makeCtx() {
  const routes = []
  const listeners = new Map()
  const logs = []
  const ctx = {
    routes,
    listeners,
    logs,
    logger: { info: (m) => logs.push(`info ${m}`), warn: (m) => logs.push(`warn ${m}`), debug: () => {} },
    effect: (cb) => cb(),
    on(name, handler) {
      const list = listeners.get(name) ?? []
      list.push(handler)
      listeners.set(name, list)
      return () => {}
    },
    get: (name) => (name === 'webServer' ? { port: 57525 } : undefined),
    inject: (_deps, callback) => {
      callback(ctx)
      return () => {}
    },
    connection: {
      fetch: {
        register(route) {
          if (routes.some((item) => item.path === route.path)) throw new Error(`重复注册 ${route.path}`)
          routes.push(route)
          return () => {}
        },
      },
    },
    subprocess: {
      resolveExecutable: async () => undefined,
      spawn: () => {
        throw new Error('这一层不该起子进程')
      },
    },
  }
  return ctx
}

// ---------------------------------------------------------------- 1. 入口层

const ctx = makeCtx()
let threw
try {
  mod.apply(ctx, { injectBudgetChars: 12000 })
} catch (error) {
  threw = String(error)
}

check('apply 不抛错', threw === undefined, threw ?? '')
check('挂了 agent/created', (ctx.listeners.get('agent/created')?.length ?? 0) === 1)
check('挂了 agent/disposed', (ctx.listeners.get('agent/disposed')?.length ?? 0) === 1)
check('挂了 session/event', (ctx.listeners.get('session/event')?.length ?? 0) === 2, String(ctx.listeners.get('session/event')?.length))
check('注册了 10 条路由', ctx.routes.length === 10, String(ctx.routes.length))
check('路由都在 /api 下', ctx.routes.every((r) => r.path.startsWith('/api/')))
check('启动日志打出页面地址', ctx.logs.some((l) => l.includes('/api/memory/ui')), ctx.logs.find((l) => l.includes('/api/memory/ui')) ?? '无')
check('前缀是 /api/memory', API_PREFIX === '/api/memory', API_PREFIX)
check('页面地址拼法', pageUrl(57525) === 'http://127.0.0.1:57525/api/memory/ui', pageUrl(57525))

// ---------------------------------------------------------------- 2. 接线层

// 接线层要真的起 Python，所以换一个不带「禁止起子进程」的 ctx 重新注册。
const wireCtx = makeCtx()
wireCtx.subprocess = { spawn: () => { throw new Error('接线层用 exec，不用 subprocess') } }

/** 真跑 `python ui/api.py`，与插件运行时同一条路。 */
function exec(args) {
  return new Promise((done) => {
    const child = spawn('python', [join(PLUGIN, 'ui', 'api.py'), ...args], {
      cwd: PLUGIN,
      env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
    })
    let stdout = ''
    let stderr = ''
    child.stdout.on('data', (d) => { stdout += d })
    child.stderr.on('data', (d) => { stderr += d })
    child.on('close', (code) => done({ code, stdout, stderr }))
  })
}

registerMemoryUi(wireCtx, { pagesDir: join(PLUGIN, 'ui'), exec, logger: wireCtx.logger })
const routes = new Map(wireCtx.routes.map((r) => [r.path, r]))

// 页面与样式
const page = await routes.get(`${API_PREFIX}/ui`).fetch(new Request(`http://127.0.0.1:1${API_PREFIX}/ui`))
const html = await page.text()
check('页面 200', page.status === 200, String(page.status))
check('注入了 API_BASE', html.includes('window.__MEMORY_API_BASE__="/api/memory"'))
check('注入了 EMBEDDED', html.includes('window.__MEMORY_EMBEDDED__=true'))
check('页面有五个页签', ['概览', '记忆', '人格', '体检', '设置'].every((t) => html.includes(t)))

const css = await routes.get(`${API_PREFIX}/style.css`).fetch(new Request(`http://127.0.0.1:1${API_PREFIX}/style.css`))
check('样式表 200', css.status === 200, String(css.status))
check('样式 content-type 对', (css.headers.get('content-type') ?? '').includes('text/css'))

// 数据接口：每条都要通
const get = async (key, query = '') => {
  const res = await routes.get(`${API_PREFIX}/${key}`).fetch(new Request(`http://127.0.0.1:1${API_PREFIX}/${key}${query}`))
  return { status: res.status, body: await res.json() }
}

const overview = await get('overview', `?cwd=${encodeURIComponent(process.cwd())}`)
check('overview 200', overview.status === 200, String(overview.status))
check('overview 带 root', typeof overview.body.root === 'string' && overview.body.root !== '', overview.body.root)
check('overview 带注入预算', Number.isFinite(overview.body.injected?.chars), String(overview.body.injected?.chars))

const tree = await get('tree')
check('tree 有文件', Array.isArray(tree.body.files) && tree.body.files.length > 0, String(tree.body.files?.length))

const checkup = await get('checkup')
check('checkup 有 problems', Array.isArray(checkup.body.problems), String(checkup.body.problems?.length))

const config = await get('config')
check('config 有注入预算', Number.isFinite(config.body.config?.inject?.maxChars), String(config.body.config?.inject?.maxChars))

// 错误翻译与越界
const missingFile = await get('file', '?path=shared/__nope__.md')
check('不存在的文件 → 404', missingFile.status === 404, `${missingFile.status} ${missingFile.body.error ?? ''}`)

const escape = await get('file', `?path=${encodeURIComponent('../../../../etc/passwd')}`)
check('越界路径被拒', escape.status === 400 || escape.status === 404, String(escape.status))

// 写接口的冲突检测（SOUL 用过期 mtime）
const soul = await routes.get(`${API_PREFIX}/soul`).fetch(new Request(`http://127.0.0.1:1${API_PREFIX}/soul`, {
  method: 'POST',
  body: JSON.stringify({ agent: 'moxie-dsh', text: 'x', mtime: 1 }),
}))
check('SOUL 过期 mtime → 409', soul.status === 409, String(soul.status))

const post = async (key, payload) => {
  const res = await routes.get(`${API_PREFIX}/${key}`).fetch(new Request(`http://127.0.0.1:1${API_PREFIX}/${key}`, {
    method: 'POST',
    body: JSON.stringify(payload),
  }))
  return { status: res.status, body: await res.json() }
}

const persona = await post('persona', { agent: 'moxie-dsh', file: 'IDENTITY.md', text: 'x', mtime: 1 })
check('persona 过期 mtime → 409', persona.status === 409, String(persona.status))

const personaBad = await post('persona', { agent: 'moxie-dsh', file: '../../config.json', text: 'x' })
check('persona 只认 SOUL/IDENTITY', personaBad.status === 400, `${personaBad.status} ${personaBad.body.error ?? ''}`)

const fileStale = await post('file', { path: 'shared/focus.md', text: 'x', mtime: 1 })
check('file 过期 mtime → 409', fileStale.status === 409, String(fileStale.status))

const fileType = await post('file', { path: 'config.json', text: '{}' })
check('file 只允许写 .md', fileType.status === 400, `${fileType.status} ${fileType.body.error ?? ''}`)

const fileEscape = await post('file', { path: '../../../../etc/passwd', text: 'x' })
check('写越界路径被拒', fileEscape.status === 400 || fileEscape.status === 404, String(fileEscape.status))

// methods 是声明，宿主据此分发；handler 自己不拒方法，所以断言声明
check('tree 只声明 GET', String(routes.get(`${API_PREFIX}/tree`).methods) === 'GET', String(routes.get(`${API_PREFIX}/tree`).methods))
check('soul 只声明 POST', String(routes.get(`${API_PREFIX}/soul`).methods) === 'POST')
check('config 声明 GET,POST', String(routes.get(`${API_PREFIX}/config`).methods) === 'GET,POST')
check('file 声明 GET,POST', String(routes.get(`${API_PREFIX}/file`).methods) === 'GET,POST', String(routes.get(`${API_PREFIX}/file`).methods))
check('persona 只声明 POST', String(routes.get(`${API_PREFIX}/persona`).methods) === 'POST')
check('全部路由都带 requestBody', [...routes.values()].every((r) => r.requestBody === 'buffered'))

const failed = results.filter((r) => !r.ok)
console.log(`\n${results.length - failed.length}/${results.length} 通过`)
process.exit(failed.length === 0 ? 0 : 1)
