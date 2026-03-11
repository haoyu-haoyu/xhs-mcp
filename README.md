# XHS-MCP 小红书智能查询分析系统

一个通用的小红书 (Xiaohongshu / Little Red Book) MCP Server，让 [Claude Code](https://claude.ai/claude-code) 能够搜索、阅读和分析小红书上的任意内容。

## 它能做什么

通过自然语言指令驱动，例如：

```
"帮我看看小红书上关于伦敦租房的经验帖，总结要注意什么"
"分析一下小红书上对 iPhone 17 的真实评价"
"这个小红书账号是真人还是营销号？帮我查一下"
"小红书上最近关于 KCL CSC 奖学金有什么新讨论"
```

Claude Code 会自动规划查询策略、调用 MCP 工具采集数据、分析内容并输出结论。

## 架构

```
用户自然语言指令
  │
  ▼
Claude Code (读取 Skill → 规划策略)
  │
  ├── 调用 ──→  xhs-mcp Server (本地运行)
  │              ├─ xhs_search      搜索笔记
  │              ├─ xhs_detail      笔记详情 + 评论
  │              ├─ xhs_creator     作者主页 + 历史帖子
  │              ├─ xhs_login       登录管理
  │              └─ xhs_status      服务状态
  │              │
  │              └─ 底层: Playwright 签名 + httpx 请求
  │
  ├── 自身能力 ──→  内容分析、分类、总结、判断
  │
  └── 输出 ──→  终端展示 / 结构化报告
```

## 前置要求

- Python 3.10+
- [Claude Code CLI](https://claude.ai/claude-code)
- Chromium 浏览器（Playwright 会自动安装）

## 安装

```bash
# 1. 克隆仓库
git clone https://github.com/haoyu-haoyu/xhs-mcp.git
cd xhs-mcp

# 2. 安装 Python 依赖
pip install mcp playwright httpx tenacity

# 3. 安装 Playwright 浏览器
playwright install chromium
```

## 配置 Claude Code

在项目根目录或 `~/.claude/` 下创建 `.mcp.json`：

```json
{
  "mcpServers": {
    "xhs": {
      "command": "python3",
      "args": ["<你的路径>/xhs-mcp/server.py"],
      "cwd": "<你的路径>/xhs-mcp",
      "env": {}
    }
  }
}
```

将 `<你的路径>` 替换为实际路径。

### 安装 Skill（可选但推荐）

将 `SKILL.md` 复制到 Claude Code 的 commands 目录：

```bash
mkdir -p ~/.claude/commands
cp SKILL.md ~/.claude/commands/xhs.md
```

之后在 Claude Code 中输入 `/xhs` 即可激活小红书分析能力。

## 首次使用：登录

小红书 API 需要有效的 Cookie。首次使用时需要扫码登录：

1. 在 Claude Code 中说：`"帮我登录小红书"` 或调用 `xhs_login(action="qrcode")`
2. 会弹出浏览器窗口显示二维码
3. 用小红书 App 扫码确认
4. Cookie 自动保存，有效期约 7-30 天

也可以手动导入 Cookie 字符串（从浏览器开发者工具复制）：

```
xhs_login(action="cookie_str", cookie_str="你的cookie字符串")
```

## MCP 工具说明

### `xhs_search` — 搜索笔记

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `keywords` | `list[str]` | *必填* | 搜索关键词列表 |
| `sort` | `str` | `"general"` | `general` / `time_descending` / `popularity_descending` |
| `page` | `int` | `1` | 页码 |
| `note_type` | `int` | `0` | `0`=全部 `1`=图文 `2`=视频 |
| `force_refresh` | `bool` | `false` | 绕过缓存 |

返回包含 `note_id`、`xsec_token`、标题、摘要、互动数据、作者信息的列表。

### `xhs_detail` — 获取笔记详情与评论

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `note_ids` | `list[str]` | *必填* | 笔记 ID 列表（来自搜索结果） |
| `xsec_tokens` | `list[str]` | *必填* | 安全令牌列表（来自搜索结果） |
| `get_comments` | `bool` | `false` | 是否获取评论 |
| `comment_count` | `int` | `20` | 每条笔记获取评论数 |
| `force_refresh` | `bool` | `false` | 绕过缓存 |

返回完整正文、图片列表、话题标签、IP 属地，以及评论（含子评论）。

### `xhs_creator` — 查看作者主页

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `user_ids` | `list[str]` | *必填* | 用户 ID 列表 |
| `note_count` | `int` | `5` | 获取最近几条帖子 |
| `force_refresh` | `bool` | `false` | 绕过缓存 |

返回个人简介、粉丝数、获赞与收藏数、IP 属地、认证标签、近期发布的帖子。

### `xhs_login` — 登录管理

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `action` | `str` | `"check"` | `check` / `qrcode` / `cookie_str` |
| `cookie_str` | `str` | `""` | `cookie_str` 动作时传入 |

### `xhs_status` — 服务状态

无参数。返回浏览器连接状态、Cookie 有效性、缓存条目数和大小。

## 缓存策略

| 数据类型 | TTL | 说明 |
|----------|-----|------|
| 搜索结果 | 15 分钟 | 按关键词 + 排序 + 页码组合缓存 |
| 笔记详情 | 24 小时 | 按 note_id 单条缓存 |
| 作者信息 | 7 天 | 按 user_id 单条缓存 |

所有工具支持 `force_refresh: true` 绕过缓存获取最新数据。缓存文件存储在 `cache/` 目录，不会被上传到 Git。

## 项目结构

```
xhs-mcp/
├── server.py              # MCP Server 入口，注册工具和调度
├── xhs/
│   ├── client.py          # XHS API 客户端（签名 + 请求）
│   ├── sign.py            # 请求签名（X-S, X-T, x-S-Common）
│   ├── browser.py         # Playwright 浏览器管理
│   ├── login.py           # 登录流程（QR 码 / Cookie 导入）
│   ├── cache.py           # 文件缓存系统
│   └── models.py          # 数据模型与异常定义
├── config/
│   └── settings.py        # 配置常量
├── libs/
│   └── stealth.min.js     # 反无头浏览器检测脚本
├── SKILL.md               # Claude Code Skill 定义
└── pyproject.toml
```

## 技术细节

- **签名机制**：通过 Playwright 注入页面环境，调用小红书前端的 `window._webmsxyw` 函数生成签名头（X-S, X-T），再用 Python 构建 x-S-Common 和 X-B3-Traceid
- **反检测**：注入 `stealth.min.js` 防止无头浏览器被识别
- **请求频率**：内置 2-5 秒随机间隔，多关键词搜索 5-10 秒间隔
- **错误处理**：所有错误返回结构化 JSON，不会导致 MCP Server 崩溃。支持 3 次自动重试

## 致谢

核心采集能力提取并重构自 [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)，感谢原作者的开源贡献。

## 许可

仅限非商业学习研究使用 (Non-Commercial Learning Use Only)。

本项目依赖小红书的非公开 API，请遵守小红书的服务条款。使用者需对自己的使用行为负责。
