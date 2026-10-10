---
search: false
---
# 如何维护文档站

文档源码保存在同一仓库的 `docs/`，网站由VitePress构建并通过GitHub Pages发布。机器人部署不需要Node.js，也不需要运行文档服务。

## 本地预览与检查

维护者需要Node.js 22或更高版本，在仓库根目录运行：

```bash
npm ci
npm run docs:dev
```

打开终端打印的本地URL。正式构建与链接检查：

```bash
npm run docs:build
npm run docs:check
npm run docs:preview
```

`docs:check` 检查生成的页面、站内链接、图片/脚本资源与章节锚点。请先构建，避免检查旧产物。

## 新增一个功能页面

1. 在对应目录新增Markdown文件。
2. 按“按钮入口 → 操作步骤 → 默认值与限制 → 示例 → 常见问题”编写。
3. 核对实现中的实际按钮、默认值与权限；模拟示意应明确标注，不能当作真实Telegram截图。
4. 在 `.vitepress/config.mts` 侧边栏和[功能导航](../guide/features.md)加入链接。
5. 构建检查后提交。main分支文档变更会自动部署；PR只构建检查，不发布。

原有完整手册保持原文件名和GitHub链接。功能检查报告留在源码仓库，不作为用户教程发布。

## 发布地址与域名

默认地址：<https://slobys.github.io/telegram-group-manager/>。GitHub仓库Settings → Pages的构建来源为GitHub Actions。

绑定自定义域名时，需要设置GitHub Pages域名和DNS，并将配置中的 `DOCS_BASE` 改为 `/`、`DOCS_SITE_URL` 改为实际域名，或修改默认值后重新构建。当前不预设任何自定义域名。

网站无需API密钥或搜索服务账户。中文本地搜索索引随构建生成，普通读者只需浏览器。

## 依赖与机器人隔离

VitePress是仅供文档构建的开发依赖；使用稳定版1.6.4，并通过override固定已修复安全问题的Vite 6.4.3，构建兼容性由本地及CI验证。以后升级VitePress时，应重新检查此override是否仍需要。

`node_modules` 和构建产物不提交Git，也排除出机器人Docker构建上下文。网站发布不会更新你的机器人实例；机器人仍通过 `gg update` 升级。
