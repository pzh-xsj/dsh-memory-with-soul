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
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
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
check('注册了 11 条路由', ctx.routes.length === 11, String(ctx.routes.length))
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
check('页面有七个页签', ['概览', '项目册', '记忆', '人格', '用户', '体检', '设置'].every((t) => html.includes(t)))

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

// 0.6.3 起人格写在角色卡上：临时造一张卡 + 一条映射，收尾还原——真库不该留自检垃圾
const MEM_ROOT = process.env.MEMORY_DIR ?? join(homedir(), '.memory')
const CFG_FILE = join(MEM_ROOT, 'config.json')
const CFG_ORIG = existsSync(CFG_FILE) ? readFileSync(CFG_FILE, 'utf8') : null
const WIRE_ROLE = 'zz-wire'
const WIRE_DIR = join(MEM_ROOT, 'roles', WIRE_ROLE)
mkdirSync(WIRE_DIR, { recursive: true })
writeFileSync(join(WIRE_DIR, 'SOUL.md'), '---\nname: 线测\n---\n\n# SOUL.md\n\n原文\n')
{
  const cfg = CFG_ORIG === null ? {} : JSON.parse(CFG_ORIG)
  cfg.roles = { ...(cfg.roles ?? {}), [WIRE_ROLE]: WIRE_ROLE }
  writeFileSync(CFG_FILE, JSON.stringify(cfg, null, 2) + '\n')
}

// 写接口的冲突检测（SOUL 用过期 mtime）
const soul = await routes.get(`${API_PREFIX}/soul`).fetch(new Request(`http://127.0.0.1:1${API_PREFIX}/soul`, {
  method: 'POST',
  body: JSON.stringify({ agent: WIRE_ROLE, text: 'x', mtime: 1 }),
}))
check('SOUL 过期 mtime → 409', soul.status === 409, String(soul.status))

const post = async (key, payload) => {
  const res = await routes.get(`${API_PREFIX}/${key}`).fetch(new Request(`http://127.0.0.1:1${API_PREFIX}/${key}`, {
    method: 'POST',
    body: JSON.stringify(payload),
  }))
  return { status: res.status, body: await res.json() }
}

const persona = await post('persona', { agent: WIRE_ROLE, file: 'SOUL.md', text: 'x', mtime: 1 })
check('persona 过期 mtime → 409', persona.status === 409, String(persona.status))

// 一键修与顺序：POST 挂在同一条路由上，写通道只认白名单动作
const fixBogus = await post('checkup', { kinds: ['bogus'] })
check('修复杂项 → 400', fixBogus.status === 400, `${fixBogus.status} ${fixBogus.body.error ?? ''}`)

const orderPaths = (config.body.config?.inject?.order ?? []).map((e) => e.path)
const orderShort = await post('config', { order: orderPaths.slice(0, -1) })
check('顺序少一条 → 400', orderShort.status === 400, `${orderShort.status}`)

const personaBad = await post('persona', { agent: WIRE_ROLE, file: '../../config.json', text: 'x' })
check('persona 只认 SOUL.md', personaBad.status === 400, `${personaBad.status} ${personaBad.body.error ?? ''}`)

const personaOld = await post('persona', { agent: WIRE_ROLE, file: 'IDENTITY.md', text: 'x' })
check('persona 拒收已退役的 IDENTITY.md', personaOld.status === 400, `${personaOld.status}`)

// 没配角色的 agent 一律拒写（404）：静默写回旧路径会让迁移看起来没做
const personaNoRole = await post('persona', { agent: 'zz-norole', file: 'SOUL.md', text: 'x' })
check('没配角色的 agent 拒写人格', personaNoRole.status === 404, `${personaNoRole.status}`)

const fileStale = await post('file', { path: 'shared/focus.md', text: 'x', mtime: 1 })
check('file 过期 mtime → 409', fileStale.status === 409, String(fileStale.status))

const fileType = await post('file', { path: 'config.json', text: '{}' })
check('file 只允许写 .md', fileType.status === 400, `${fileType.status} ${fileType.body.error ?? ''}`)

const fileEscape = await post('file', { path: '../../../../etc/passwd', text: 'x' })
check('写越界路径被拒', fileEscape.status === 400 || fileEscape.status === 404, String(fileEscape.status))

// 领域：GET 列目录，POST 只能新建；只验拒绝路径，不往真库添文件
const playbook = await get('playbook')
const kindList = playbook.body.kinds ?? []
check('playbook 列出领域与文件状态',
  playbook.status === 200 && Array.isArray(kindList) && kindList.every((k) => 'kind' in k && 'has_method' in k),
  String(kindList.length))

const pbBad = await post('playbook', { kind: '../evil', title: 'x', summary: 'y', when: 'z' })
check('领域名不合法 → 400', pbBad.status === 400, `${pbBad.status} ${pbBad.body.error ?? ''}`)

const pbEmpty = await post('playbook', { kind: 'zz-selftest', title: '自检', summary: '', when: '自检时' })
check('领域空说明 → 400', pbEmpty.status === 400, `${pbEmpty.status}`)

const existing = kindList.find((k) => k.has_method)
if (existing) {
  const pbDup = await post('playbook', { kind: existing.kind, title: '覆盖试试', summary: 'y', when: 'z' })
  check('已有 method.md → 409（不覆盖）', pbDup.status === 409, `${pbDup.status}`)
}

// methods 是声明，宿主据此分发；handler 自己不拒方法，所以断言声明
check('tree 只声明 GET', String(routes.get(`${API_PREFIX}/tree`).methods) === 'GET', String(routes.get(`${API_PREFIX}/tree`).methods))
check('soul 只声明 POST', String(routes.get(`${API_PREFIX}/soul`).methods) === 'POST')
check('config 声明 GET,POST', String(routes.get(`${API_PREFIX}/config`).methods) === 'GET,POST')
check('checkup 声明 GET,POST', String(routes.get(`${API_PREFIX}/checkup`).methods) === 'GET,POST', String(routes.get(`${API_PREFIX}/checkup`).methods))
check('file 声明 GET,POST', String(routes.get(`${API_PREFIX}/file`).methods) === 'GET,POST', String(routes.get(`${API_PREFIX}/file`).methods))
check('playbook 声明 GET,POST', String(routes.get(`${API_PREFIX}/playbook`).methods) === 'GET,POST', String(routes.get(`${API_PREFIX}/playbook`).methods))
check('persona 只声明 POST', String(routes.get(`${API_PREFIX}/persona`).methods) === 'POST')
check('全部路由都带 requestBody', [...routes.values()].every((r) => r.requestBody === 'buffered'))

// 角色卡 fixture 收尾：还原 config、删掉临时卡
rmSync(WIRE_DIR, { recursive: true, force: true })
if (CFG_ORIG === null) rmSync(CFG_FILE, { force: true })
else writeFileSync(CFG_FILE, CFG_ORIG)

const failed = results.filter((r) => !r.ok)
console.log(`\n${results.length - failed.length}/${results.length} 通过`)
process.exit(failed.length === 0 ? 0 : 1)
