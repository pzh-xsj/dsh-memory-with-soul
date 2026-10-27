/**
 * memory-ui — 把设置页挂到 DSH 宿主自己的端口上。
 *
 * 之前是 `python ui/serve.py` 起 127.0.0.1:8765，然后让人手动打开。内置之后
 * 页面和接口都走 DSH 的 `/api` 前缀，浏览器带着宿主的 cookie 直接访问，
 * 不用额外进程、不用额外端口。
 *
 * 路由规则（宿主 `dsh-client-connection` 的 `registerFetchRoute`）：
 *
 *   - **精确匹配**：`fetchRoutes.get(url.pathname)`，不是前缀。所以每个端点
 *     单独注册一条，`/api/memory/xxx` 不能靠一条通配吃掉。
 *   - **必须落在 `/api/` 下**：`assertFetchRoute` 用 `endpointFromPath('/api', path)`
 *     校验，且每段要匹配 `^[A-Za-z0-9_$.-]+$`。
 *   - **只有 owner 能读**：`peer.principal.kind !== 'owner'` 直接 403。浏览器带
 *     cookie 的请求会被认成 owner peer，所以从 DSH 页面点进去就能用。
 *   - **精确路径不能重复注册**，重复会抛错并让插件加载失败——只在 apply 里
 *     注册一次。
 *
 * 数据不在 JS 里重算：每个请求起一次 `python ui/api.py <端点>`，结果原样透传。
 * 慢一点（一次 Python 启动约 150ms），但保住「判定只有一份」这条底线。
 */
import { readFile, unlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

/** 接口前缀。页面里的 `API_BASE` 由本文件注入，两处必须一致。 */
export const API_PREFIX = '/api/memory'

/** 静态：设置页本体与样式表。 */
const ASSETS = [
  { path: `${API_PREFIX}/ui`, file: 'settings.html', type: 'text/html; charset=utf-8' },
  { path: `${API_PREFIX}/style.css`, file: 'style.css', type: 'text/css; charset=utf-8' },
]

/**
 * 接口表。`query` 列出的查询参数原样转成 `--key value`；`write` 决定要不要
 * 加 `--write`（同一条路由 GET 读、POST 写）。
 */
const ENDPOINTS = [
  { path: `${API_PREFIX}/overview`, api: 'overview', methods: ['GET'], query: ['cwd', 'agent'] },
  { path: `${API_PREFIX}/tree`, api: 'tree', methods: ['GET'], query: [] },
  { path: `${API_PREFIX}/file`, api: 'file', methods: ['GET', 'POST'], query: ['path'], write: true },
  { path: `${API_PREFIX}/config`, api: 'config', methods: ['GET', 'POST'], query: [], write: true },
  // GET 体检；POST = 执行修复（body: {kinds:[...], dry?:bool}）。同一条路径两种语义，
  // 在 `--write` 上分叉——不新增路由，省掉一条「要不要带 all」的歧义。
  { path: `${API_PREFIX}/checkup`, api: 'checkup', methods: ['GET', 'POST'], query: ['all'], write: true },
  { path: `${API_PREFIX}/project-meta`, api: 'project-meta', methods: ['GET', 'POST'], query: ['path'], write: true },
  { path: `${API_PREFIX}/soul`, api: 'soul', methods: ['POST'], query: [], write: true },
  { path: `${API_PREFIX}/persona`, api: 'persona', methods: ['POST'], query: [], write: true },
]

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
  })
}

/**
 * 把 `python ui/api.py` 的一行输出翻成 HTTP 响应。
 *
 * 子进程退出码只表示「跑完了」，真正的状态在 JSON 的 `status` 里——这样
 * 400/404/409 这类业务错误不用挤在 0–255 的退出码空间里。
 */
function toResponse(result) {
  if (result === null) return json(503, { ok: false, error: '找不到 python，接口不可用' })
  const text = String(result.stdout ?? '').trim()
  if (text === '') {
    const detail = String(result.stderr ?? '').trim().slice(0, 400)
    return json(500, { ok: false, error: detail === '' ? '接口没有输出' : detail })
  }
  let parsed
  try {
    parsed = JSON.parse(text)
  } catch {
    return json(500, { ok: false, error: `接口输出不是 JSON：${text.slice(0, 200)}` })
  }
  const status = Number.isInteger(parsed?.status) ? parsed.status : 200
  if (parsed?.ok === true) return json(status, { ok: true, ...parsed.value })
  const body = { ok: false, error: String(parsed?.error ?? '未知错误') }
  if (typeof parsed?.code === 'string' && parsed.code !== '') body.code = parsed.code
  return json(status, body)
}

function pageWithBase(html) {
  const inject = `<script>window.__MEMORY_API_BASE__=${JSON.stringify(API_PREFIX)};`
    + 'window.__MEMORY_EMBEDDED__=true;</script>'
  return html.includes('</head>') ? html.replace('</head>', `${inject}</head>`) : inject + html
}

/**
 * 注册全部路由。
 *
 * @param ctx 宿主上下文（要有 `connection` 与 `effect`）
 * @param options.pagesDir   `ui/` 目录绝对路径
 * @param options.exec       `(args, body) => Promise<{code,stdout,stderr}|null>`，
 *                           由 index.js 提供（它手里有 python 解析与子进程封装）
 * @param options.logger     宿主 logger
 */
export function registerMemoryUi(ctx, options) {
  const { pagesDir, exec, logger } = options

  for (const asset of ASSETS) {
    ctx.effect(() => ctx.connection.fetch.register({
      path: asset.path,
      methods: ['GET'],
      requestBody: 'buffered',
      fetch: async () => {
        try {
          const raw = await readFile(join(pagesDir, asset.file), 'utf8')
          const body = asset.file.endsWith('.html') ? pageWithBase(raw) : raw
          return new Response(body, {
            headers: { 'content-type': asset.type, 'cache-control': 'no-store' },
          })
        } catch (error) {
          logger.warn(`memory-ui: 读不到 ${asset.file}：${String(error)}`)
          return json(500, { ok: false, error: `读不到 ${asset.file}` })
        }
      },
    }), `memory-ui: ${asset.path}`)
  }

  for (const endpoint of ENDPOINTS) {
    ctx.effect(() => ctx.connection.fetch.register({
      path: endpoint.path,
      methods: endpoint.methods,
      requestBody: 'buffered',
      fetch: async (request) => {
        const url = new URL(request.url)
        const args = [endpoint.api]
        for (const key of endpoint.query) {
          const value = url.searchParams.get(key)
          if (value !== null && value !== '') args.push(`--${key}`, value)
        }

        let body
        if (request.method === 'POST' && endpoint.write === true) {
          args.push('--write')
          const raw = await request.text()
          if (raw.trim() !== '') {
            body = join(tmpdir(), `memory-ui-${Date.now()}-${Math.random().toString(36).slice(2)}.json`)
            await writeFile(body, raw, 'utf8')
            args.push('--body-file', body)
          }
        }

        try {
          return toResponse(await exec(args, body))
        } catch (error) {
          logger.warn(`memory-ui: ${endpoint.api} 失败：${String(error)}`)
          return json(500, { ok: false, error: String(error) })
        } finally {
          if (body !== undefined) await unlink(body).catch(() => {})
        }
      },
    }), `memory-ui: ${endpoint.path}`)
  }
}

/** 设置页的完整地址，日志与 README 里用。 */
export function pageUrl(port) {
  return `http://127.0.0.1:${String(port ?? 0)}${API_PREFIX}/ui`
}
