# SearXNG 主题切换与静态资源组装、压缩、分发完整脉络

本文档按代码执行顺序，拆解主题枚举定义 → 资源打包工具链 → 静态文件分发中间件的三层协同关系。

---

## 第一层：主题枚举与偏好体系

### 1.1 主题目录枚举（运行时动态发现）

可用主题列表并非硬编码枚举，而是运行时扫描模板目录得到：

**入口函数**：[webutils.py#L177-L179](file:///d:/fz/0601-2/solo-dogfeeding/code/8-searxng/searx/webutils.py#L177-L179)

```python
def get_themes(templates_path):
    """Returns available themes list."""
    return os.listdir(templates_path)
```

在 [webapp.py#L131-L134](file:///d:/fz/0601-2/solo-dogfeeding/code/8-searxng/searx/webapp.py#L131-L134) 中于进程启动时一次性扫描：

```python
default_theme = settings['ui']['default_theme']
templates_path = settings['ui']['templates_path']
themes = get_themes(templates_path)   # 结果：['simple']（当前仅此一套）
result_templates = get_result_templates(templates_path)
```

### 1.2 系统默认主题与样式枚举

**定义位置**：[settings_defaults.py#L25-L26](file:///d:/fz/0601-2/solo-dogfeeding/code/8-searxng/searx/settings_defaults.py#L25-L26) 与 [settings_defaults.py#L232-L247](file:///d:/fz/0601-2/solo-dogfeeding/code/8-searxng/searx/settings_defaults.py#L232-L247)

```python
SIMPLE_STYLE = ('auto', 'light', 'dark', 'black')   # 四种子样式（CSS类切换）

SCHEMA: dict[str, t.Any] = {
    'ui': {
        'static_path': SettingsDirectoryValue(str, os.path.join(searx_dir, 'static')),
        'templates_path': SettingsDirectoryValue(str, os.path.join(searx_dir, 'templates')),
        'default_theme': SettingsValue(str, 'simple'),
        'default_locale': SettingsValue(str, ''),
        'theme_args': {
            'simple_style': SettingsValue(SIMPLE_STYLE, 'auto'),  # 默认auto模式
        },
        ...
    },
}
```

### 1.3 用户偏好中的主题设置项

**定义位置**：[preferences.py#L451-L469](file:///d:/fz/0601-2/solo-dogfeeding/code/8-searxng/searx/preferences.py#L451-L469)

```python
'theme': EnumStringSetting(
    get_setting("ui.default_theme"),
    locked="theme" in self.cfg.lock,
    choices=themes,   # 来自 get_themes() 的动态列表
),
'simple_style': EnumStringSetting(
    get_setting("ui.theme_args.simple_style"),
    locked="simple_style" in self.cfg.lock,
    choices=["", "auto", "light", "dark", "black"],
),
```

这两个 `EnumStringSetting` 对象通过 cookie 持久化，在 `pre_request` 钩子中解析并挂载到 `sxng_request.preferences`。

---

## 第二层：资源打包工具链（构建时）

### 2.1 构建命令入口链

Shell 调用链路（自顶向下）：

```
Makefile themes.all
  → ./manage themes.all
    → lib_sxng_themes.sh: themes.all()
      → lib_sxng_vite.sh: vite.simple.build()
        → 1. templates.simple.pygments()  [生成pygments.less]
        → 2. node.env()                  [确保npm依赖]
        → 3. cd client/simple && npm run build
```

**关键脚本**：
- [lib_sxng_themes.sh](file:///d:/fz/0601-2/solo-dogfeeding/code/8-searxng/utils/lib_sxng_themes.sh)
- [lib_sxng_vite.sh#L32-L45](file:///d:/fz/0601-2/solo-dogfeeding/code/8-searxng/utils/lib_sxng_vite.sh#L32-L45)

### 2.2 npm build 两阶段流水线

[client/simple/package.json#L8-L12](file:///d:/fz/0601-2/solo-dogfeeding/code/8-searxng/client/simple/package.json#L8-L12)

```json
"scripts": {
  "build": "npm run build:icons && npm run build:vite",
  "build:icons": "node theme_icons.ts",
  "build:vite": "vite build"
}
```

#### 阶段一：build:icons → 生成 Jinja SVG 图标目录

执行脚本：[client/simple/theme_icons.ts](file:///d:/fz/0601-2/solo-dogfeeding/code/8-searxng/client/simple/theme_icons.ts)

- 从 `node_modules/ionicons/dist/svg/` 和 `src/svg/ionicons/` 收集 30+ 个 SVG
- 通过 SVGO 优化（去 namespace、加 aria-hidden、去 title）
- 调用 [jinja_svg_catalog.ts: jinja_svg_sets()](file:///d:/fz/0601-2/solo-dogfeeding/code/8-searxng/client/simple/tools/jinja_svg_catalog.ts#L98-L118) 使用 Edge.js 模板引擎
- 输出：`searx/templates/simple/icons.html`，包含 `icon()` / `icon_small()` / `icon_big()` 三个 Jinja macro

#### 阶段二：build:vite → 编译压缩所有前端资源

Vite 配置：[client/simple/vite.config.ts](file:///d:/fz/0601-2/solo-dogfeeding/code/8-searxng/client/simple/vite.config.ts)

##### 2.2.1 编译目标与浏览器兼容

```typescript
build: {
  target: browserslistToEsbuild(manifest.browserslist),  // "baseline 2022, not dead"
  outDir: resolve("../../", "searx/static/themes/simple/"),  // 输出到 Python static 目录
  manifest: "manifest.json",   // 生成资源映射清单
  emptyOutDir: true,
  sourcemap: true,
}
```

##### 2.2.2 入口点定义（Rolldown multi-entry）

```typescript
rolldownOptions: {
  input: {
    core:    `${PATH.src}/js/index.ts`,           // 主JS入口
    ltr:     `${PATH.src}/less/style-ltr.less`,   // LTR 样式
    rtl:     `${PATH.src}/less/style-rtl.less`,   // RTL 样式
    rss:     `${PATH.src}/less/rss.less`,         // RSS 样式
  },
  output: {
    entryFileNames: "sxng-[name].min.js",         // sxng-core.min.js
    chunkFileNames: "chunk/[hash].min.js",        // 动态导入分包
    assetFileNames: ({ names }) => {
      switch (name?.split(".").pop()) {
        case "css": return "sxng-[name].min[extname]";  // sxng-ltr.min.css 等
        default:    return "sxng-[name][extname]";      // 其他资源
      }
    },
  }
}
```

##### 2.2.3 JS 动态分包与按需加载

主入口 [src/js/index.ts](file:///d:/fz/0601-2/solo-dogfeeding/code/8-searxng/client/simple/src/js/index.ts) 仅 3 行：

```typescript
void import.meta.glob(["./*.ts", "./util/**/.ts"], { eager: true });
```

配合 Vite 的 `dynamicImports` 机制，实际功能模块被编译为独立 chunk（参见 `manifest.json`）：

| 功能模块 | chunk 文件 | 触发加载时机 |
|---|---|---|
| search / keyboard / autocomplete | 各独立 hash.js | 搜索页 endpoint |
| results / preferences | 各独立 hash.js | 对应页面 |
| Calculator / InfiniteScroll / MapView | 各独立 hash.js | 插件启用时 |
| MapView | 附带 sxng-mapview.min.css | 启用地图插件时 |

##### 2.2.4 LightningCSS 压缩与主题变量

CSS 处理器使用 `lightningcss`，目标浏览器由 `browserslistToTargets()` 转换。

**主题切换的核心 CSS 机制**在 [src/less/definitions.less](file:///d:/fz/0601-2/solo-dogfeeding/code/8-searxng/client/simple/src/less/definitions.less)：

```less
:root { /* 亮色主题变量 ~100 个 CSS custom properties */ }

.dark-themes() { /* 暗色主题变量覆盖 */ }
.black-themes() { /* 纯黑主题变量覆盖 */ }

// auto 模式：跟随系统
@media (prefers-color-scheme: dark) {
  :root.theme-auto { .dark-themes(); }
}
// 手动暗色
:root.theme-dark { .dark-themes(); }
// 手动纯黑
:root.theme-black { .dark-themes(); .black-themes(); }
```

**运行时切换**是纯 CSS 变量方案——只需在 `<html>` 标签切换 `theme-xxx` class，无需重新加载任何样式文件。

##### 2.2.5 自定义 Vite 插件（SVG 优化与转码）

[tools/plg.ts](file:///d:/fz/0601-2/solo-dogfeeding/code/8-searxng/client/simple/tools/plg.ts) 定义两个 `writeBundle` 钩子插件：

- **plg_svg2svg**：用 SVGO 优化 SVG，输出到 `img/` 目录
  - `empty_favicon.svg`、`select-dark.svg`、`select-light.svg`
  - 品牌 logo：`searxng.svg`、`img_load_error.svg`、`favicon.svg`
  - 模板目录：`searxng-wordmark.min.svg`

- **plg_svg2png**：用 Sharp 将 SVG 光栅化为 PNG
  - `favicon.png`（默认尺寸）
  - `searxng.png`（默认尺寸）
  - `512.png` / `192.png`（PWA 图标）

### 2.3 构建产物结构（最终输出）

```
searx/static/themes/simple/
├── sxng-core.min.js + .map        # 主JS (~300KB)
├── sxng-ltr.min.css               # 左到右样式
├── sxng-rtl.min.css               # 右到左样式
├── sxng-rss.min.css               # RSS样式
├── sxng-mapview.min.css           # 地图插件样式
├── chunk/
│   ├── [hash1].min.js + .map      # autocomplete
│   ├── [hash2].min.js + .map      # keyboard
│   ├── ...                        # 共 9 个动态 chunk
├── img/
│   ├── favicon.png / favicon.svg
│   ├── searxng.png / searxng.svg
│   ├── 192.png / 512.png          # PWA
│   ├── empty_favicon.svg
│   ├── select-dark.svg / select-light.svg
│   └── img_load_error.svg
└── manifest.json                  # Vite 生成的资源清单
```

---

## 第三层：静态文件分发中间件（运行时）

### 3.1 WhiteNoise 中间件挂载

**位置**：[webapp.py#L1385-L1401](file:///d:/fz/0601-2/solo-dogfeeding/code/8-searxng/searx/webapp.py#L1385-L1401)

```python
def static_headers(headers: Headers, _path: str, _url: str) -> None:
    headers['Cache-Control'] = 'public, max-age=30, stale-while-revalidate=60'
    for header, value in settings['server']['default_http_headers'].items():
        headers[header] = str(value)

app.wsgi_app = ProxyFix(app.wsgi_app)
app.wsgi_app = WhiteNoise(
    app.wsgi_app,
    root=settings['ui']['static_path'],    # searx/static/
    prefix="static",                        # URL 前缀 /static/
    max_age=None,
    allow_all_origins=False,
    add_headers_function=static_headers,
)
```

**关键点**：WhiteNoise 接管所有 `/static/*` 请求，绕过 Flask 路由层，直接从磁盘高效发送（支持 gzip/brotli 预压缩文件、HTTP 范围请求、长缓存）。

### 3.2 custom_url_for：主题资源路径重写

**位置**：[webapp.py#L256-L294](file:///d:/fz/0601-2/solo-dogfeeding/code/8-searxng/searx/webapp.py#L256-L294)

模板中写 `url_for('static', filename='sxng-core.min.js')` 并不能直接命中磁盘，因为真实路径是 `static/themes/<theme_name>/sxng-core.min.js`。`custom_url_for` 做了两层转发：

```python
def custom_url_for(endpoint: str, **values):
    global _STATIC_FILES
    if not _STATIC_FILES:
        _STATIC_FILES = webutils.get_static_file_list()  # 一次性扫描

    if endpoint == "static" and values.get("filename"):
        arg_filename = values["filename"]
        if arg_filename not in _STATIC_FILES:
            # 不在全局静态列表中 → 拼上当前主题路径
            theme_name = sxng_request.preferences.get_value("theme")
            theme_filename = f"themes/{theme_name}/{arg_filename}"
            if theme_filename in _STATIC_FILES:
                values["filename"] = theme_filename   # 重写 filename

        app_prefix = url_for("index")
        return f"{app_prefix}static/{values['filename']}"  # 直接构造最终URL
    ...
```

**辅助函数** [webutils.py#L182-L197](file:///d:/fz/0601-2/solo-dogfeeding/code/8-searxng/searx/webutils.py#L182-L197) 递归列出 `searx/static/` 下所有文件，作为白名单校验。

### 3.3 模板渲染时的资源注入

**基础模板**：[templates/simple/base.html](file:///d:/fz/0601-2/solo-dogfeeding/code/8-searxng/searx/templates/simple/base.html)

#### `<html>` 标签主题 class 绑定（行2）：

```html
<html class="no-js theme-{{ preferences.get_value('simple_style') or 'auto' }} ..." ...>
```

这里直接把 `simple_style` 偏好值渲染为 CSS 类名 `theme-auto` / `theme-light` / `theme-dark` / `theme-black`，**直接命中 definitions.less 的选择器**。

#### 样式资源加载（行15-19）：

```html
{% if rtl %}
<link rel="stylesheet" href="{{ url_for('static', filename='sxng-rtl.min.css') }}">
{% else %}
<link rel="stylesheet" href="{{ url_for('static', filename='sxng-ltr.min.css') }}">
{% endif %}
```

通过 `rtl` 变量（由 locale 是否在 RTL_LOCALES 判断）选择左/右样式。经 `custom_url_for` 转换后，实际请求路径是：
- `/static/themes/simple/sxng-ltr.min.css`（或 rtl）

#### 主脚本加载（行13）：

```html
<script type="module" src="{{ url_for('static', filename='sxng-core.min.js') }}"
        client_settings="{{ client_settings }}"></script>
```

`client_settings` 是一个 Base64 编码的 JSON 对象，包含（见 [webapp.py#L366-L384](file:///d:/fz/0601-2/solo-dogfeeding/code/8-searxng/searx/webapp.py#L366-L384)）：

```python
{
  'theme_static_path': custom_url_for('static', filename='themes/simple'),
  'theme': req_pref.get_value('theme'),
  'autocomplete': ..., 'hotkeys': ..., 'plugins': ...,
  # ... 其余运行时配置
}
```

JS 端通过读取当前 `<script>` 标签的 `client_settings` 属性获得运行时上下文，用于动态 chunk 的 baseURL 拼接。

### 3.4 偏好保存与主题切换循环

**完整请求处理链**（`@app.before_request` [webapp.py#L457-L517](file:///d:/fz/0601-2/solo-dogfeeding/code/8-searxng/searx/webapp.py#L457-L517)）：

```
HTTP 请求到达
  ↓
pre_request() 钩子
  ├─ 创建 Preferences 对象（注入 themes 枚举）
  ├─ preferences.parse_dict(cookies) → 从 cookie 读 theme / simple_style
  ├─ preferences.parse_dict(form)    → 从表单覆盖
  └─ sxng_request.preferences = prefs
  ↓
路由处理函数
  ↓
render() [webapp.py#L387-L454]
  ├─ get_client_settings() → 打包 theme / theme_static_path 等
  ├─ kwargs['url_for'] = custom_url_for   ← 模板内 url_for 被覆盖
  ├─ kwargs['theme'] = prefs.get_value('theme')
  └─ render_template(f"{theme}/base.html子模板", ...)
     → base.html <html> 上注入 theme-xxx class
     → base.html 通过 custom_url_for 引用 sxng-*.min.css/js
  ↓
响应返回浏览器
  ↓
浏览器
  ├─ CSS 命中 :root.theme-xxx → 应用对应 CSS 变量
  ├─ JS sxng-core.min.js 执行 → 读取 client_settings
  └─ 需要时动态 import() 拉取 chunk/*.min.js
```

**偏好保存**：在 `/preferences` POST 提交时（[webapp.py#L871-L878](file:///d:/fz/0601-2/solo-dogfeeding/code/8-searxng/searx/webapp.py#L871-L878)）调用 `preferences.save(resp)`，将 `theme` 和 `simple_style` 作为 cookie 写入（`max_age=5年`）。

---

## 三层协同关系全景图

```
┌──────────────────────────────────────────────────────────────────────┐
│                       【构建时：Build-time】                         │
│                                                                      │
│  Makefile → manage → lib_sxng_vite.sh                                │
│       │                                                              │
│       ├── Phase 1: node theme_icons.ts                              │
│       │     └─ ionicons SVG → SVGO → Jinja macros (icons.html)      │
│       │                                                              │
│       └── Phase 2: vite build (vite.config.ts)                      │
│             ├─ Entry: index.ts → sxng-core.min.js + 9 chunk/*.js    │
│             ├─ Entry: style-ltr.less → sxng-ltr.min.css             │
│             │        (内嵌 :root.theme-{auto,light,dark,black})     │
│             ├─ Entry: style-rtl.less → sxng-rtl.min.css             │
│             ├─ Entry: rss.less → sxng-rss.min.css                   │
│             ├─ Plugin: plg_svg2svg → img/*.svg (优化)                │
│             ├─ Plugin: plg_svg2png → img/*.png (PWA/品牌)           │
│             └─ manifest.json (资源映射清单)                          │
│                                                                      │
│                      ↓ 产物落地到 searx/static/themes/simple/       │
└──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       【运行时：Run-time】                           │
│                                                                      │
│  进程启动 (webapp.py init)                                           │
│       ├── get_themes(templates/) → ['simple'] (偏好可用选项)         │
│       ├── WhiteNoise(root=searx/static/, prefix=static/)             │
│       │   └─ 静态文件高效分发 + gzip/brotli + 缓存头                 │
│       └── custom_url_for = 覆盖 Jinja 原生 url_for('static')         │
│                                                                      │
│  HTTP 请求 → pre_request()                                           │
│       ├── sxng_request.preferences = Preferences(...)                │
│       │   ├─ 'theme' EnumSetting (choices=themes)                   │
│       │   └─ 'simple_style' EnumSetting (auto/light/dark/black)     │
│       └─ 从 cookies / form 解析主题值                                │
│                                                                      │
│  render(template) → render_template(f"{theme}/xxx.html")            │
│       ├── <html class="theme-{simple_style}">  ← 触发 CSS 变量切换   │
│       ├── url_for('static', filename='sxng-core.min.js')            │
│       │     └─ custom_url_for → /static/themes/simple/sxng-...      │
│       ├── sxng-core.min.js → 读 client_settings(Base64 JSON)        │
│       └── 动态 import() → 按需加载 chunk/[hash].min.js              │
│                                                                      │
│  用户切换主题 → POST /preferences → save() → 写入cookie              │
│       └── 下次请求: pre_request 读 cookie → 重新渲染 <html class>    │
│           (无需重新下载 CSS，仅切换 class 触发 CSS 变量重算)          │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 关键设计洞察

1. **"主题"两层分离**：
   - **Theme（目录级）**：`simple` 是唯一主题，通过模板目录切换（预留多主题扩展点）
   - **Style（CSS 类级）**：`auto/light/dark/black` 是同一 CSS 文件内的类切换，换肤零网络开销

2. **资源引用解耦**：模板只写 `sxng-core.min.js` 这种短名，由 `custom_url_for` 在运行时拼上当前主题目录路径，解耦模板与磁盘真实布局。

3. **预构建 + 缓存分发**：Vite 构建时完成所有 hash 指纹化、压缩、sourcemap；运行时 WhiteNoise 直接接管静态请求，不经过 Flask 路由，性能接近纯文件服务器。

4. **JS 动态分块**：功能模块（search/preferences/calculator...）全部按需动态 import，首屏仅需加载核心 js，其余按 endpoint 和插件启用状态懒加载。
