#!/usr/bin/env node
/**
 * smoke.mjs — 不装进 DSH 也能跑的自检。
 *
 * 覆盖三件事：
 *   1. bundle 清单与资产是否成形（package.json / patch / icon / locale）；
 *   2. index.js 是否可被 ESM 加载并导出 apply + inject；
 *   3. 渲染器在三种 cwd（有项目册 / 无项目册 / 路径不存在）下是否都能出块、不崩。
 *
 * 用法：node smoke.mjs [--verbose]
 * 退出码 0 = 全绿。
 */
import { spawnSync } from 'node:child_process'
import { existsSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN_ROOT = resolve(HERE, '..', '..')
const RENDERER = join(PLUGIN_ROOT, 'hooks', 'session-start.py')
const API_CLIENT = join(PLUGIN_ROOT, 'ui', 'api.py')
const verbose = process.argv.includes('--verbose')

const checks = []
function check(name, fn) {
  try {
    const detail = fn()
    if (detail !== null && typeof detail?.then === 'function') {
      // 异步检查（要 import 新模块）单独收，否则 detail 会记成 [object Promise]
      checks.push({ ok: true, name, detail: '…', pending: detail })
      return
    }
    checks.push({ ok: true, name, detail })
  } catch (error) {
    checks.push({ ok: false, name, detail: String(error?.message ?? error) })
  }
}

function must(condition, message) {
  if (!condition) throw new Error(message)
}

// ---------------------------------------------------------------- 1. 清单

const manifest = JSON.parse(readFileSync(join(HERE, 'package.json'), 'utf8'))

check('package.json: type=module + dsh.bundle.patch', () => {
  must(manifest.type === 'module', 'type 必须是 module')
  must(manifest.dsh?.bundle?.patch === './cordis.patch.yml', 'dsh.bundle.patch 必须指向 ./cordis.patch.yml')
  must(manifest.private === true, '本地 bundle 应 private:true')
  return manifest.name
})

check('cordis.patch.yml: 插入行的 name 等于包名', () => {
  const patchPath = join(HERE, manifest.dsh.bundle.patch)
  must(existsSync(patchPath), `patch 不存在：${patchPath}`)
  const yaml = readFileSync(patchPath, 'utf8')
  must(/^\s*-\s*insert:/m.test(yaml), 'patch 里没有 insert:')
  must(yaml.includes(manifest.name), `patch 没有引用包名 ${manifest.name}`)
  must(/id:\s*memory-inject/.test(yaml), 'patch 的行 id 应为 memory-inject')
  return 'ok'
})

check('exports 覆盖 ./locale/*.json 与 ./icon', () => {
  must(manifest.exports?.['./locale/*.json'] === './locale/*.json', '缺 ./locale/*.json 导出')
  must(manifest.exports?.['./icon'] === './icon.svg', '缺 ./icon 导出')
  return 'ok'
})

check('icon.svg 有效且 ≤256 KiB', () => {
  const iconPath = join(HERE, 'icon.svg')
  must(existsSync(iconPath), 'icon 不存在')
  const size = statSync(iconPath).size
  must(size <= 256 * 1024, `icon 过大：${size}B`)
  const head = readFileSync(iconPath, 'utf8').trimStart()
  must(head.startsWith('<svg'), 'icon 不是 SVG')
  must(head.includes('</svg>'), 'icon 没有闭合')
  return `${size}B`
})

check('locale/*.json 都有 meta.title 与 meta.description', () => {
  for (const locale of ['zh', 'en']) {
    const data = JSON.parse(readFileSync(join(HERE, 'locale', `${locale}.json`), 'utf8'))
    must(typeof data.meta?.title === 'string' && data.meta.title !== '', `${locale}: 缺 meta.title`)
    must(typeof data.meta?.description === 'string' && data.meta.description !== '', `${locale}: 缺 meta.description`)
  }
  return 'zh + en'
})

// ---------------------------------------------------------------- 2. 模块

const mod = await import(pathToFileURL(join(HERE, 'index.js')).href).catch((error) => ({ __error: error }))

check('index.js 可加载并导出 apply / inject', () => {
  must(!mod.__error, `加载失败：${mod.__error}`)
  must(typeof mod.apply === 'function', '没有导出 apply(ctx, config)')
  must(Array.isArray(mod.inject) && mod.inject.includes('subprocess'), "inject 必须声明 'subprocess'")
  return `inject=${mod.inject.join(',')}`
})

// 宿主在 Session.append 的准入阶段校验 source，不合规就抛 SessionFormatError，
// 而 agent/created 是 serial 派发 —— 一次抛错会让整个 agent 创建回滚（表现为「本轮运行失败」）。
// 规则来自宿主 lib/types/message-sources.js 的 source()：kind 必须是非空字符串且 !== 'plugin'。
check('注入消息通过宿主 v4 来源准入', () => {
  must(typeof mod.injectedMessage === 'function', '没有导出 injectedMessage(text)')
  const message = mod.injectedMessage('<memory-context>smoke</memory-context>')
  const source = message?.source
  must(source !== null && typeof source === 'object', '消息缺 source')
  must(typeof source.kind === 'string' && source.kind.length > 0, 'source.kind 必须是非空字符串')
  must(source.kind !== 'plugin', "source.kind 不能用退役的 'plugin'（v4 准入会直接抛错）")
  must(message.role === 'user', "role 必须是 'user'（inbox 只路由 user 角色）")
  must(Array.isArray(message.content) && message.content[0]?.type === 'text', 'content 必须是 text 块')
  must(typeof message.id === 'string' && message.id.length > 0, '消息要有 id')
  return `kind=${source.kind}`
})

// ---------------------------------------------------------------- 3. 渲染器

function findPython() {
  const override = process.env.MEMORY_PYTHON
  const candidates = override ? [override] : ['python', 'py', 'E:\\python\\python.exe']
  for (const candidate of candidates) {
    const probe = spawnSync(candidate, ['-V'], { encoding: 'utf8' })
    if (probe.status === 0) return candidate
  }
  return null
}

const python = findPython()

function runRenderer(cwd) {
  const result = spawnSync(python, [RENDERER, '--dry', '--cwd', cwd], {
    encoding: 'utf8',
    env: { ...process.env, MEMORY_HARNESS: 'dsh', PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
    timeout: 30000,
  })
  if (result.error) throw new Error(`spawn 失败：${result.error.message}`)
  if (result.status !== 0) throw new Error(`退出码 ${result.status}；stderr=${(result.stderr ?? '').trim().slice(0, 300)}`)
  const out = (result.stdout ?? '').trim()
  if (verbose && result.stderr.trim() !== '') console.error(`[stderr] ${result.stderr.trim()}`)
  must(out.startsWith('<memory-context>'), '输出没有以 <memory-context> 开头')
  must(out.endsWith('</memory-context>'), '输出没有以 </memory-context> 结尾')
  must(out.includes('# 长期记忆'), '输出缺少「# 长期记忆」标题行')
  must(out.length < 20000, `输出异常大：${out.length} 字符`)
  return out
}

const cases = [
  ['有项目册', process.env.MEMORY_SMOKE_CWD ?? 'C:\\work\\ws'],
  ['无项目册', 'C:\\Users\\<user>'],
  ['路径不存在', 'C:\\__memory_smoke_missing__'],
]

if (python === null) {
  checks.push({ ok: false, name: '渲染器', detail: '找不到 python；用 MEMORY_PYTHON 指定' })
} else {
  for (const [label, cwd] of cases) {
    check(`渲染器 / ${label}（${cwd}）`, () => {
      const out = runRenderer(cwd)
      const agent = /agent:\s*([a-z0-9-]+)/.exec(out)?.[1] ?? '?'
      const hasProject = out.includes('本项目册根：（无')
      return `${out.length} 字符，agent=${agent}，项目册=${hasProject ? '无' : '有'}`
    })
  }
}

// ---------------------------------------------------------------- 静态检查
//
// 不启动 DSH，只读源码：这些是「改了 index.js 就会悄悄失效」的地方，
// 靠肉眼检查不靠谱。

const INDEX_JS = join(HERE, 'index.js')
const PACKAGE_JSON = join(HERE, 'package.json')

check('清单 / name 与 patch 一致', () => {
  const pkg = JSON.parse(readFileSync(PACKAGE_JSON, 'utf8'))
  const patch = readFileSync(join(HERE, 'cordis.patch.yml'), 'utf8')
  const used = /name:\s*'([^']+)'/.exec(patch)?.[1] ?? ''
  must(used !== '', 'patch 里没找到 name')
  must(used === pkg.name, `patch 用 ${used}，清单是 ${pkg.name}`)
  return pkg.name
})

check('清单 / 必填字段', () => {
  const pkg = JSON.parse(readFileSync(PACKAGE_JSON, 'utf8'))
  for (const key of ['name', 'version', 'engines', 'dsh']) must(key in pkg, `缺少 ${key}`)
  must(pkg.engines.node !== undefined, '缺 engines.node')
  must(pkg.dsh?.engines?.dsh !== undefined, '缺 dsh.engines.dsh')
  must(pkg.dsh?.bundle?.patch === './cordis.patch.yml', 'dsh.bundle.patch 应指向 cordis.patch.yml')
  return `${pkg.name}@${pkg.version}`
})

check('入口 / 导出 apply、注入 subprocess', () => {
  const src = readFileSync(INDEX_JS, 'utf8')
  must(/export\s+const\s+inject\s*=/.test(src), '缺 `export const inject`')
  must(/export\s+function\s+apply\s*\(/.test(src), '缺 `export function apply`')
  must(/inject\s*=\s*\['subprocess'\]/.test(src), 'inject 里没有 subprocess')
  must(/ctx\.on\(\s*'session\/event'/.test(src), '没挂 session/event（track 与 digest 都靠它）')
  must(src.includes("'track'"), '没调用 core 的 track 子命令')
  must(src.includes("'digest'"), '没调用 core 的 digest 子命令')
  return 'ok'
})

check('渲染器 / --dry 不写状态', () => {
  const src = readFileSync(RENDERER, 'utf8')
  const main = src.slice(src.lastIndexOf('def main('))
  must(main.includes('"--dry" in argv'), 'main 里没解析 --dry')
  must(/if not dry:/.test(main), 'main 没有 dry 分支（dry 时仍会写状态）')
  return 'ok'
})

check('内置设置页 / web.js 路由表合法', async () => {
  const web = await import(pathToFileURL(join(HERE, 'web.js')).href)
  must(typeof web.registerMemoryUi === 'function', 'web.js 没导出 registerMemoryUi')
  must(web.API_PREFIX === '/api/memory', `前缀不是 /api/memory：${web.API_PREFIX}`)

  // 真注册一遍，顺手验证「路径落在 /api 下 + 不重复」
  const registered = []
  const ctx = {
    effect: (callback) => callback(),
    connection: {
      fetch: {
        register: (route) => {
          if (registered.some((item) => item.path === route.path)) {
            throw new Error(`重复注册 ${route.path}`)
          }
          registered.push(route)
          return () => {}
        },
      },
    },
    logger: { warn: () => {}, info: () => {} },
  }
  web.registerMemoryUi(ctx, { pagesDir: join(HERE, '..', '..', 'ui'), exec: async () => null, logger: ctx.logger })

  must(registered.length === 9, `注册了 ${String(registered.length)} 条，期望 9`)
  for (const route of registered) {
    must(route.path.startsWith('/api/'), `路由不在 /api 下：${route.path}`)
    const segments = route.path.slice('/api/'.length).split('/')
    must(segments.every((seg) => /^[A-Za-z0-9_$.-]+$/.test(seg)), `路径段不合法：${route.path}`)
    must(Array.isArray(route.methods) && route.methods.length > 0, `${route.path} 没有 methods`)
    must(route.requestBody === 'buffered', `${route.path} 缺 requestBody`)
    must(typeof route.fetch === 'function', `${route.path} 没有 fetch`)
  }
  return `${String(registered.length)} 条`
})

check('内置设置页 / api.py 与 serve.py 共用实现', () => {
  const client = readFileSync(API_CLIENT, 'utf8')
  must(/import serve/.test(client), 'api.py 没复用 ui/serve.py')
  must(/read_body\(argv\)/.test(client), 'api.py 没按 --body-file 读请求体')
  return 'ok'
})

check('左侧栏入口 / 浏览器半边已声明', () => {
  // 宿主按 exports['./client'] 找浏览器入口；少了这一条，左侧栏永远不会有
  // 「记忆库」，而服务端一半照常工作——最难查的那种半死。
  const pkg = JSON.parse(readFileSync(join(HERE, 'package.json'), 'utf8'))
  const entry = pkg.exports?.['./client']
  const file = typeof entry === 'string' ? entry : entry?.default
  must(typeof file === 'string', "package.json 缺 exports['./client']")
  const bundle = readFileSync(join(HERE, file), 'utf8')
  must(/__ModuleLoader__\.load\(/.test(bundle), 'client 入口不是 __ModuleLoader__.load 形态')
  must(
    !/require\(['"](node:)?(fs|path|child_process|os)['"]\)/.test(bundle),
    'client 入口 require 了宿主没有的 node 内置模块',
  )
  must(pkg.dsh?.client?.platform === 'web', 'package.json 缺 dsh.client.platform')
  must(pkg.files?.includes('client.js'), 'files 里没有 client.js，打包会漏')
  // 侧栏行与 main 面板靠同一个 id 连线：id 变了 key 没变，点进去就是空白。
  must(/name:\s*'main'/.test(bundle) && /key:\s*PANEL_ID/.test(bundle), '没注册 main 面板，或 key 没跟 id 共用')
  must(/name:\s*'sidebar\.panellist'/.test(bundle) && /id:\s*PANEL_ID/.test(bundle), '没注册 sidebar.panellist，或 id 没跟面板共用一个常量')
  must(/settings\.section/.test(bundle), 'client 入口没注册 settings.section')
  return file
})

// ---------------------------------------------------------------- 报表

// 异步检查跑完再统计
for (const item of checks) {
  if (item.pending === undefined) continue
  try {
    item.detail = await item.pending
    item.ok = true
  } catch (error) {
    item.ok = false
    item.detail = String(error?.message ?? error)
  }
  delete item.pending
}

let failed = 0
for (const item of checks) {
  const mark = item.ok ? 'PASS' : 'FAIL'
  if (!item.ok) failed += 1
  console.log(`${mark}  ${item.name}${item.ok ? ` — ${item.detail}` : `\n      ${item.detail}`}`)
}
console.log(`\n${checks.length - failed}/${checks.length} 通过`)
process.exit(failed === 0 ? 0 : 1)
