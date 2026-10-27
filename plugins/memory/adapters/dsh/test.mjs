#!/usr/bin/env node
/**
 * test.mjs — 用假宿主验证 index.js 的三条接线。
 *
 * smoke.mjs 验「清单和渲染器对不对」，这里验「事件来了会不会真的去调 core」：
 * agent/created → 注入；tool/call(read) → track；turn/end → digest。
 *
 * 不启动 DSH，也不需要真的 python 跑完——ctx.subprocess 被换成记录器，
 * 只断言 argv / env / cwd 对不对。
 *
 * 用法：node test.mjs；退出码 0 = 全过。
 */
import { apply, injectedMessage, readPaths, reminderMessage } from './index.js'

const results = []
async function check(name, fn) {
  try {
    const detail = await fn()
    results.push({ ok: true, name, detail })
  } catch (error) {
    results.push({ ok: false, name, detail: String(error?.stack ?? error?.message ?? error) })
  }
}

function must(condition, message) {
  if (!condition) throw new Error(message)
}

/** 造一个假宿主：记录 spawn 调用，on 存下处理器。 */
function fakeContext({ stdout = '', exitCode = 0 } = {}) {
  const handlers = new Map()
  const calls = []
  const warnings = []
  const routes = []

  const ctx = {
    logger: {
      info: () => {},
      warn: (message) => warnings.push(message),
    },
    on: (name, handler) => {
      if (!handlers.has(name)) handlers.set(name, [])
      handlers.get(name).push(handler)
    },
    /**
     * 宿主在依赖就绪时回调一次。自检里就当依赖已就绪——真宿主没就绪时
     * 不会回调，但那样代码路径根本没跑，测了也没意义。
     */
    inject: (_names, callback) => {
      try {
        callback(ctx)
      } catch (error) {
        warnings.push(`inject: ${String(error?.message ?? error)}`)
      }
    },
    get: () => undefined,
    /** Cordis 的 ctx.effect：立即跑一次，返回值当清理函数。 */
    effect: (fn) => {
      const dispose = fn()
      return typeof dispose === 'function' ? dispose : () => {}
    },
    /** 内置设置页的路由表挂这里，方便断言注册了几条。 */
    connection: {
      fetch: {
        register: (route) => {
          routes.push(route)
          return () => {}
        },
      },
    },
    subprocess: {
      spawn: (spec) => {
        calls.push(spec)
        return {
          done: Promise.resolve({ exitCode }),
          terminate: () => {},
          collected: {
            stdout: { readFrom: () => ({ text: stdout }) },
            stderr: { readFrom: () => ({ text: '' }) },
          },
        }
      },
    },
  }

  return {
    ctx,
    calls,
    warnings,
    routes,
    async fire(name, ...args) {
      for (const handler of handlers.get(name) ?? []) await handler(...args)
    },
  }
}

function fakeAgent({ id = 's-1', cwd = 'C:\\work\\ws', origin = '' } = {}) {
  const injected = []
  const session = { header: { id, cwd, origin } }
  const agent = { session, inject: async (message) => { injected.push(message) } }
  return { agent, session, injected }
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 60))

// ---------------------------------------------------------------- 接线

await check('agent/created → 注入一条 <memory-context>', async () => {
  const host = fakeContext({ stdout: '<memory-context>\n# 长期记忆\n</memory-context>\n' })
  apply(host.ctx, {})
  const { agent, injected } = fakeAgent()
  await host.fire('agent/created', { agent })
  must(injected.length === 1, `注入 ${injected.length} 次`)
  const [message] = injected
  must(message.role === 'user', 'role 不是 user')
  must(message.source.kind === 'memory-inject', `source.kind=${message.source.kind}`)
  must(message.content[0].text.startsWith('<memory-context>'), '正文不是记忆块')
  const call = host.calls.at(-1)
  must(call.argv.at(-3) === '--dry', `渲染器参数不对：${call.argv.join(' ')}`)
  must(call.env.MEMORY_HARNESS === 'dsh', 'HEAP_MEMORY_HARNESS 未设置'.replace('HEAP_', 'MEMORY_'))
  return call.argv.slice(0, 2).join(' ')
})

await check('设置页 → 往宿主注册路由', async () => {
  const host = fakeContext()
  apply(host.ctx, {})
  must(host.routes.length >= 9, `注册了 ${host.routes.length} 条`)
  const paths = host.routes.map((route) => route.path)
  for (const need of ['/api/memory/ui', '/api/memory/style.css', '/api/memory/overview']) {
    must(paths.includes(need), `缺路由 ${need}`)
  }
  return `${String(host.routes.length)} 条`
})

await check('tool/call(read) → track 带 --paths', async () => {
  const host = fakeContext()
  apply(host.ctx, {})
  const { agent, session } = fakeAgent()
  await host.fire('agent/created', { agent })
  host.calls.length = 0

  await host.fire('session/event', session, {
    surfaceOp: 'append',
    type: 'tool/call',
    data: { turn: 1, step: 1, callId: 'c1', name: 'read', arguments: JSON.stringify({ file_path: 'C:\\Users\\<user>\\.memory\\shared\\focus.md' }) },
  })
  await settle()

  must(host.calls.length === 1, `spawn ${host.calls.length} 次`)
  const argv = host.calls[0].argv
  must(argv.includes('track'), `子命令不是 track：${argv.join(' ')}`)
  const paths = argv[argv.indexOf('--paths') + 1]
  must(paths.includes('focus.md'), `--paths 不含目标文件：${paths}`)
  must(host.calls[0].env.MEMORY_HARNESS === 'dsh', 'track 没带 harness 环境变量')
  return paths
})

await check('tool/call(非读工具) → 不 spawn', async () => {
  const host = fakeContext()
  apply(host.ctx, {})
  const { agent, session } = fakeAgent()
  await host.fire('agent/created', { agent })
  host.calls.length = 0

  await host.fire('session/event', session, {
    surfaceOp: 'append',
    type: 'tool/call',
    data: { turn: 1, step: 1, callId: 'c2', name: 'bash', arguments: JSON.stringify({ command: 'ls' }) },
  })
  await settle()
  must(host.calls.length === 0, `不该 spawn，却 spawn 了 ${host.calls.length} 次`)
  return 'ok'
})

await check('重放事件（surfaceOp 不是 append）→ 不 spawn', async () => {
  const host = fakeContext()
  apply(host.ctx, {})
  const { agent, session } = fakeAgent()
  await host.fire('agent/created', { agent })
  host.calls.length = 0

  await host.fire('session/event', session, {
    surfaceOp: 'replay',
    type: 'tool/call',
    data: { turn: 1, step: 1, callId: 'c3', name: 'read', arguments: JSON.stringify({ file_path: 'x.md' }) },
  })
  await settle()
  must(host.calls.length === 0, '重放事件也触发了 track')
  return 'ok'
})

await check('turn/end 且 core 返回 should=true → 注入提醒', async () => {
  const host = fakeContext({ stdout: '{"should":true,"text":"写点东西","kind":"stale","code":""}', exitCode: 2 })
  apply(host.ctx, {})
  const { agent, session, injected } = fakeAgent()
  await host.fire('agent/created', { agent })
  injected.length = 0
  host.calls.length = 0

  await host.fire('session/event', session, {
    surfaceOp: 'append',
    type: 'turn/end',
    data: { turn: 4, reason: 'stop' },
  })
  await settle()

  must(host.calls.length === 1, `spawn ${host.calls.length} 次`)
  const argv = host.calls[0].argv
  must(argv.includes('digest'), `子命令不是 digest：${argv.join(' ')}`)
  must(argv[argv.indexOf('--turns') + 1] === '4', '--turns 没传真实轮数')
  must(argv.includes('--json'), 'digest 没带 --json')
  must(injected.length === 1, `提醒注入 ${injected.length} 次`)
  must(injected[0].source.kind === 'memory-digest', 'source.kind 不是 memory-digest')
  return injected[0].content[0].text
})

await check('turn/end 且 core 返回 should=false → 不注入', async () => {
  const host = fakeContext({ stdout: '{"should":false,"text":""}', exitCode: 0 })
  apply(host.ctx, {})
  const { agent, session, injected } = fakeAgent()
  await host.fire('agent/created', { agent })
  injected.length = 0

  await host.fire('session/event', session, {
    surfaceOp: 'append',
    type: 'turn/end',
    data: { turn: 2, reason: 'stop' },
  })
  await settle()
  must(injected.length === 0, 'should=false 也注入了提醒')
  return 'ok'
})

await check('子 agent 默认不注入记忆', async () => {
  const host = fakeContext({ stdout: '<memory-context>x</memory-context>' })
  apply(host.ctx, {})
  const { agent, injected } = fakeAgent({ origin: 'subagent' })
  await host.fire('agent/created', { agent })
  must(injected.length === 0, '子 agent 被注入了')
  return 'ok'
})

// ---------------------------------------------------------------- 纯函数

await check('readPaths 认 read 与 ops_read_files', () => {
  const one = readPaths('read', JSON.stringify({ file_path: 'a.md' }))
  must(one.length === 1 && one[0] === 'a.md', `read 解析失败：${JSON.stringify(one)}`)
  const many = readPaths('ops_read_files', JSON.stringify({ files: [{ path: 'b.md' }, { path: 'c.md' }] }))
  must(many.length === 2, `ops_read_files 解析失败：${JSON.stringify(many)}`)
  must(readPaths('bash', JSON.stringify({ command: 'ls' })).length === 0, 'bash 不该被当成读')
  return `${one.length}+${many.length}`
})

await check('消息工厂的 source.kind 不是退役的 plugin', () => {
  for (const [label, make] of [['注入', injectedMessage], ['提醒', reminderMessage]]) {
    const message = make('x')
    must(message.source.kind !== '' && message.source.kind !== 'plugin', `${label} 的 kind 非法`)
    must(Array.isArray(message.content) && message.content[0].type === 'text', `${label} 正文不是 text`)
  }
  return 'ok'
})

// ---------------------------------------------------------------- 报表

const failures = results.filter((item) => !item.ok)
for (const item of results) {
  console.log(`${item.ok ? 'PASS' : 'FAIL'}  ${item.name}${item.ok ? ` — ${item.detail}` : `\n      ${item.detail}`}`)
}
console.log(`\n${results.length - failures.length}/${results.length} 通过`)
process.exit(failures.length === 0 ? 0 : 1)
