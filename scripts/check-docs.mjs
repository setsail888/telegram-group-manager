import { readFile, readdir, stat } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const project = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const root = path.join(project, 'docs/.vitepress/dist')
const base = process.env.DOCS_BASE || '/telegram-group-manager/'
const origin = 'https://docs-check.invalid'
const problems = new Set()
const cache = new Map()
const decode = text => text.replace(/&amp;/g, '&').replace(/&quot;/g, '"')
  .replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>')

async function exists(file) {
  try { return (await stat(file)).isFile() } catch { return false }
}

async function htmlPages(directory) {
  const pages = []
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const target = path.join(directory, entry.name)
    if (entry.isDirectory()) pages.push(...await htmlPages(target))
    else if (entry.name.endsWith('.html')) pages.push(target)
  }
  return pages
}

async function contents(file) {
  if (!cache.has(file)) cache.set(file, await readFile(file, 'utf8'))
  return cache.get(file)
}

if (!await exists(path.join(root, 'index.html'))) {
  throw new Error('尚未生成文档，请先运行 npm run docs:build')
}

let links = 0
const pages = await htmlPages(root)
for (const file of pages) {
  const relative = path.relative(root, file).split(path.sep).join('/')
  const html = await contents(file)
  for (const match of html.matchAll(/<(?:a|link|script|img|source)\b[^>]*>/g)) {
    const element = match[0]
    if (element.startsWith('<img') && !/\balt=/.test(element)) {
      problems.add(`${relative}: 图片缺少alt文本`)
    }
    for (const attr of element.matchAll(/\b(?:href|src)="([^"]*)"/g)) {
      const href = decode(attr[1])
      if (!href || /^(?:data:|mailto:|tel:|javascript:)/.test(href)) continue
      const url = new URL(href, origin + base + relative)
      if (url.origin !== origin) continue
      links++
      if (!url.pathname.startsWith(base)) {
        problems.add(`${relative}: 链接超出部署子路径 ${href}`)
        continue
      }
      const requested = decodeURIComponent(url.pathname.slice(base.length))
      let target = path.resolve(root, requested || 'index.html')
      if (!target.startsWith(root + path.sep) && target !== root) {
        problems.add(`${relative}: 非法本地路径 ${href}`)
        continue
      }
      if (!await exists(target)) {
        const candidates = [target + '.html', path.join(target, 'index.html')]
        for (const candidate of candidates) {
          if (await exists(candidate)) { target = candidate; break }
        }
      }
      if (!await exists(target)) {
        problems.add(`${relative}: 目标不存在 ${href}`)
      } else if (url.hash && target.endsWith('.html')) {
        const anchor = decodeURIComponent(url.hash.slice(1))
        const ids = [...(await contents(target)).matchAll(/\bid="([^"]*)"/g)].map(item => decode(item[1]))
        if (anchor && !ids.includes(anchor)) problems.add(`${relative}: 章节不存在 ${href}`)
      }
    }
  }
}

if (problems.size) {
  for (const problem of problems) console.error(problem)
  throw new Error(`文档检查失败：${problems.size}项问题`)
}
console.log(`文档检查通过：${pages.length}个HTML页面，${links}处站内链接与资源，包含章节锚点和图片替代文本。`)
