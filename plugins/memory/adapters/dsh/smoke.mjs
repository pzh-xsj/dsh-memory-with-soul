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
const verbose = process.argv.includes('--verbose')

const checks = []
function check(name, fn) {
  try {
    const detail = fn()
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

// ---------------------------------------------------------------- 报表

let failed = 0
for (const item of checks) {
  const mark = item.ok ? 'PASS' : 'FAIL'
  if (!item.ok) failed += 1
  console.log(`${mark}  ${item.name}${item.ok ? ` — ${item.detail}` : `\n      ${item.detail}`}`)
}
console.log(`\n${checks.length - failed}/${checks.length} 通过`)
process.exit(failed === 0 ? 0 : 1)
