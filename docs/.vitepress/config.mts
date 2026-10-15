import { defineConfig } from 'vitepress'

const base = process.env.DOCS_BASE || '/telegram-group-manager/'

export default defineConfig({
  lang: 'zh-CN',
  title: 'GroupGuard 群卫士',
  description: 'Telegram 中文群管理机器人使用文档：安装部署、违禁词、智能反垃圾、防刷屏与成员服务。',
  base,
  cleanUrls: false,
  lastUpdated: true,
  srcExclude: ['function-audit-*.md'],
  head: [['link', { rel: 'icon', type: 'image/svg+xml', href: `${base}logo.svg` }]],
  sitemap: { hostname: process.env.DOCS_SITE_URL || 'https://slobys.github.io/telegram-group-manager/' },
  markdown: {
    lineNumbers: true,
    codeCopyButtonTitle: '复制命令',
    anchor: {
      // Keep the GitHub-style chapter links used by the existing handbooks,
      // including headings with Chinese commas and parentheses.
      slugify(text) {
        return text.toLowerCase().normalize('NFKC').trim()
          .replace(/[^\p{L}\p{N}\p{M}_ -]/gu, '').replace(/ /g, '-')
      },
    },
  },
  themeConfig: {
    logo: { src: '/logo.svg', width: 28, height: 28, alt: 'GroupGuard 群卫士' },
    siteTitle: '群卫士 · 使用文档',
    nav: [
      { text: '快速开始', link: '/guide/getting-started' },
      { text: '功能导航', link: '/guide/features' },
      { text: '部署维护', link: '/server-one-click' },
      { text: '常见问题', link: '/guide/faq' },
    ],
    sidebar: [
      { text: '开始使用', items: [
        { text: '快速开始', link: '/guide/getting-started' },
        { text: '全部功能导航', link: '/guide/features' },
        { text: '中文 / English 切换', link: '/guide/languages' },
        { text: '权限与按钮操作', link: '/guide/permissions' },
        { text: '常见问题', link: '/guide/faq' },
      ] },
      { text: '群消息防护', collapsed: false, items: [
        { text: '违禁词与匹配方式', link: '/moderation/keywords' },
        { text: '启用分类词库', link: '/moderation/word-packs' },
        { text: '两字词、同音与拼音变体', link: '/moderation/variants' },
        { text: '导入、导出与精确例外', link: '/moderation/import-export' },
        { text: '智能反垃圾与链接', link: '/moderation/anti-spam' },
        { text: '个人防刷屏与图片', link: '/moderation/anti-flood' },
        { text: '跨账号重复消息', link: '/cross-account-flood' },
        { text: '警告、禁言与封禁', link: '/moderation/penalties' },
        { text: '图片 OCR', link: '/moderation/ocr' },
      ] },
      { text: '入群与成员管理', collapsed: false, items: [
        { text: '新人验证', link: '/members/verification' },
        { text: '集中入群防护', link: '/members/raid' },
        { text: '昵称、机器人与手动管理', link: '/members/management' },
      ] },
      { text: '群运营', collapsed: false, items: [
        { text: '欢迎消息与群规', link: '/operations/welcome' },
        { text: '自动回复', link: '/operations/replies' },
        { text: '定时公告', link: '/operations/schedules' },
        { text: '统计、排行与管理日志', link: '/operations/statistics' },
      ] },
      { text: '成员服务', collapsed: false, items: [
        { text: '成员按钮入口', link: '/community/menu' },
        { text: '签到积分与转赠', link: '/community/points' },
        { text: '商城与订单', link: '/community/shop' },
        { text: '免费抽奖', link: '/community/raffle' },
      ] },
      { text: '服务器维护', collapsed: true, items: [
        { text: '安装部署详细教程', link: '/server-one-click' },
        { text: 'gg 菜单、升级与备份', link: '/maintenance/server' },
        { text: '服务器迁移：A → B', link: '/maintenance/migration' },
        { text: '只读网页看板', link: '/maintenance/dashboard' },
        { text: '故障排查', link: '/maintenance/troubleshooting' },
        { text: '管理按钮延迟排查', link: '/slow-buttons' },
      ] },
      { text: '完整参考', collapsed: true, items: [
        { text: '群内命令速查', link: '/commands' },
        { text: '按钮操作完整说明', link: '/button-management' },
        { text: '词库来源与技术说明', link: '/word-library' },
        { text: '如何维护文档站', link: '/reference/documentation' },
      ] },
    ],
    search: {
      provider: 'local',
      options: {
        miniSearch: {
          options: {
            // Index overlapping Chinese pairs: a query such as “刷屏” must
            // also find “跨账号刷屏”, without an external search service.
            tokenize(text) {
              const parts = text.toLowerCase().match(/[\p{Script=Han}]+|[\p{Letter}\p{Number}_]+/gu) || []
              return [...new Set(parts.flatMap(part => {
                if (!/\p{Script=Han}/u.test(part)) return [part]
                const chars = [...part]
                return chars.length < 2 ? chars : chars.slice(0, -1).map((char, i) => char + chars[i + 1])
              }))]
            },
          },
          // Rank shared pairs instead of requiring an exact continuous phrase:
          // “跨账号刷屏” should find “跨账号防刷屏与惩罚” as well.
          searchOptions: { fuzzy: false, prefix: true, combineWith: 'OR' },
        },
        locales: {
          root: {
            translations: {
              button: { buttonText: '搜索文档', buttonAriaLabel: '搜索使用文档' },
              modal: {
                noResultsText: '没有找到结果，请试试功能名称或更短的关键词',
                resetButtonTitle: '清空搜索',
                backButtonTitle: '关闭搜索',
                displayDetails: '显示详细内容',
                footer: { selectText: '打开', navigateText: '切换', closeText: '关闭' },
              },
            },
          },
        },
      },
    },
    outline: { level: [2, 3], label: '本页目录' },
    docFooter: { prev: '上一篇', next: '下一篇' },
    lastUpdated: { text: '最后更新', formatOptions: { dateStyle: 'medium' } },
    editLink: { pattern: 'https://github.com/slobys/telegram-group-manager/edit/main/docs/:path', text: '在 GitHub 上编辑本页' },
    socialLinks: [{ icon: 'github', link: 'https://github.com/slobys/telegram-group-manager', ariaLabel: 'GroupGuard GitHub 仓库' }],
    footer: { message: 'GroupGuard 群卫士 · 文档随 main 分支更新', copyright: '请先在测试群验证，再启用正式群处罚。' },
    returnToTopLabel: '返回顶部',
    sidebarMenuLabel: '功能目录',
    darkModeSwitchLabel: '外观',
    darkModeSwitchTitle: '切换到深色模式',
    lightModeSwitchTitle: '切换到浅色模式',
    skipToContentLabel: '跳转到正文',
    notFound: { title: '页面未找到', quote: '页面可能已移动，请使用功能目录或搜索文档。', linkLabel: '返回文档首页', linkText: '返回首页' },
  },
})
