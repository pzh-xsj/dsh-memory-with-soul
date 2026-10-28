/**
 * 记忆库 —— DSH 主界面左侧栏一级页面（浏览器半边）。
 *
 * 三处注册合成一个入口：
 *   1. `main` 面板（key `memory`）：内容区的整页；
 *   2. `sidebar.panellist` 行（id `memory`）：左侧栏那一行，点它宿主自己
 *      调 `layout.selectPanel('memory')`——id 与面板 key 相同才会对上；
 *   3. `settings.section`：设置里也留一份，习惯从设置点的人不至于找不到。
 *
 * 内容仍是 iframe 装 `/api/memory/ui`：那一页 1400 行、判定全在
 * `core/memory.py`，用 React 重写就是第二个真相源——上一版设置页正是
 * 这么烂掉的。iframe 只负责显示，逻辑一行都不搬。
 *
 * 加载约定（宿主 `@deepseek-ai/dsh-client-modules` 定的）：
 *   - 入口走 package.json 的 `exports['./client']`；
 *   - 文件必须是 `window.__ModuleLoader__.load({ id, factory })` 形态；
 *   - factory 的 `require` 只能拿宿主共享模块表（react / react-dom /
 *     @deepseek-ai/dsh-client-ui-primitives），拿不到 node 内置模块；
 *   - 导出 `apply(ctx)` 与 `inject`（服务名数组），按 Cordis 生命周期挂载。
 */

window.__ModuleLoader__.load({
  id: '@local/dsh-memory-inject',
  factory: (require) => {
    const { createElement: h } = require('react')

    /** 面板 key 与侧栏行 id 必须一致，宿主靠它把两半接起来。 */
    const PANEL_ID = 'memory'
    /** 设置分区的 id，与面板 id 不冲突（宿主按 slot 分开记）。 */
    const SECTION_ID = 'memory'

    const LABEL = () => '记忆库'
    /** 页面地址：相对路径在 `dsh-app://app/` 下解析，天然同源、带登录态。 */
    const PAGE_URL = '/api/memory/ui?embed=1'

    const FRAME_STYLE = {
      display: 'block',
      width: '100%',
      height: '100%',
      border: '0',
      background: '#12141a',
      colorScheme: 'dark',
    }

    /** 铺满宿主给的内容区；外壳自己带圆角与边距，这里不再叠一层。 */
    function Frame({ radius = '0' }) {
      return h(
        'div',
        { style: { width: '100%', height: '100%', minHeight: '360px', overflow: 'hidden', borderRadius: radius } },
        h('iframe', { title: '记忆库', src: PAGE_URL, style: FRAME_STYLE }),
      )
    }

    function MainPanel() {
      return h(Frame, { radius: '0' })
    }

    function SettingsSection() {
      return h(
        'div',
        { style: { width: '100%', height: 'calc(100vh - 200px)', minHeight: '420px' } },
        h('iframe', { title: '记忆库', src: PAGE_URL, style: FRAME_STYLE }),
      )
    }

    /** 侧栏图标：16px 线稿，跟着宿主的 `owner.size` 走。 */
    function SidebarIcon(owner = {}) {
      const size = owner.size ?? 16
      return h(
        'svg',
        {
          width: size,
          height: size,
          viewBox: '0 0 16 16',
          fill: 'none',
          stroke: 'currentColor',
          strokeWidth: 1.4,
          strokeLinecap: 'round',
          strokeLinejoin: 'round',
          'aria-hidden': 'true',
        },
        h('path', { d: 'M2.5 3.2h5.2a1 1 0 0 1 1 1v8.6H3.5a1 1 0 0 1-1-1V3.2Z' }),
        h('path', { d: 'M8.7 4.2h3.8a1 1 0 0 1 1 1v7.6H9.7' }),
        h('path', { d: 'M2.5 11.8h5.2' }),
        h('path', { d: 'M10.4 6.6h1.6M10.4 8.8h1.6' }),
      )
    }

    /** `slots` 是宿主 slots 服务；不声明会在 ctx.slots 处抛错。 */
    const inject = ['slots']

    function apply(ctx) {
      // 三个 slot 都要等外壳把 declaration 登记到 ledger 之后才注册。
      // 侧栏行与 main 面板共用 id，宿主据此把点击接到面板上。
      const seat = (name, options, component) => {
        if (typeof ctx.slots.inject === 'function') {
          ctx.slots.inject(name, () => ctx.slots.register(options, component))
          return
        }
        // 老宿主没有 inject：直接注册，失败由宿主自己报。
        ctx.slots.register(options, component)
      }

      seat('main', { name: 'main', key: PANEL_ID }, MainPanel)
      seat('sidebar.panellist', { name: 'sidebar.panellist', id: PANEL_ID, order: 120, label: LABEL }, SidebarIcon)
      seat('settings.section', { name: 'settings.section', id: SECTION_ID, order: 120, label: LABEL }, SettingsSection)
    }

    return { name: 'memory', inject, apply }
  },
})
