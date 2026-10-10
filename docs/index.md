---
layout: home
title: 使用文档
description: 从安装机器人到配置每一项群管理功能，按步骤使用 GroupGuard 群卫士。
hero:
  name: GroupGuard 群卫士
  text: 群管理，从这里开始
  tagline: 安装、配置、调优与日常维护。找到功能，照着步骤操作。
  image:
    src: /logo.svg
    alt: GroupGuard 群卫士盾牌标志
    width: 160
    height: 180
  actions:
    - theme: brand
      text: 第一次使用
      link: /guide/getting-started
    - theme: alt
      text: 查找功能用法
      link: /guide/features
    - theme: alt
      text: 安装到服务器
      link: /server-one-click
features:
  - title: 违禁词与分类词库
    details: 添加关键词、启用13类词库，了解两字词、同音、繁简和精确例外。
    link: /moderation/keywords
    linkText: 配置违禁词
  - title: 智能反垃圾
    details: 配置链接、伪装网址、批量提及和域名白名单，先观察再处罚。
    link: /moderation/anti-spam
    linkText: 配置反垃圾
  - title: 个人与跨账号刷屏
    details: 调整频率、重复消息和图片规则，清理多账号同内容刷屏。
    link: /moderation/anti-flood
    linkText: 配置防刷屏
  - title: 新人与成员管理
    details: 新人验证、集中入群保护、昵称限制及警告、禁言、移出操作。
    link: /members/verification
    linkText: 管理群成员
  - title: 群运营与成员服务
    details: 欢迎语、群规、自动回复、定时公告、签到积分、商城与免费抽奖。
    link: /guide/features#群运营与成员服务
    linkText: 浏览运营功能
  - title: 部署与日常维护
    details: 一键安装、gg中文菜单、更新备份、只读看板和常见故障排查。
    link: /maintenance/server
    linkText: 维护机器人
---

## 从你的任务开始

| 你想做什么 | 从这里开始 |
| --- | --- |
| 第一次部署并加入群组 | [快速开始](guide/getting-started.md) |
| 更新后找不到新功能，或不知道词库怎么开启 | [启用分类词库](moderation/word-packs.md) |
| 广告用两个字、同音字或拆字绕过规则 | [中文变体识别](moderation/variants.md) |
| 多个账号发送完全一样的广告 | [跨账号重复检测](cross-account-flood.md) |
| 误删了正常聊天，想知道为什么 | [防护记录与统计](operations/statistics.md) |
| 安装不停重复、按钮很慢或机器人没有响应 | [故障排查](maintenance/troubleshooting.md) |

::: tip 先确认当前群的设置
分类词库、中文变体和跨账号检测默认关闭。更新程序不会替你启用；在机器人私聊中选择群组后配置。新规则建议先观察，再启用处罚。
:::
