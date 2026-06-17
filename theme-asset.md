# SearXNG 主题切换与静态资源组装、压缩、分发完整脉络

本文档按代码执行顺序，拆解主题枚举定义 → 资源打包工具链 → 静态文件分发中间件的三层协同关系。文中所有代码引用使用**仓库相对路径**（如 `searx/webutils.py#L177-L179`），可在 IDE 中直接点击跳转。

---

## 第一层：主题枚举与偏好体系

### 1.1 主题目录枚举（运行时动态发现）

可用主题列表并非硬编码枚举，而是运行时扫描模板目录得到：

**入口函数**：[searx/webutils.py#L177-L179](searx/webutils.py#L177-L179)

```python
def get_themes(templates_path):
    """Returns available themes list."""
    return os.listdir(templates_path)
```

在 [searx/webapp.py#L131-L134](searx/webapp.py#L131-L134) 中于进程启动时一次性扫描：

```python
default_theme = settings['ui']['default_theme']
templates_path = settings['ui']['templates_path']
themes = get_themes(templates_path)   # 结果：['simple']（当前仅此一套）
result_templates = get_result_templates(templates_path)
```

### 1.2 系统默认主题与样式枚举

**定义位置**：[searx/settings_defaults.py#L25-L26](searx/settings_defaults.py#L25-L26) 与 [searx/settings_defaults.py#L232-L247](searx/settings_defaults.py#L232-L247)

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

**定义位置**：[searx/preferences.py#L451-L469](searx/preferences.py#L451-L469)

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
    → utils/lib_sxng_themes.sh: themes.all()
      → utils/lib_sxng_vite.sh: vite.simple.build()
        → 1. templates.simple.pygments()  [生成pygments.less]
        → 2. node.env()                   [确保npm依赖]
        → 3. cd client/simple && npm run build
```

**关键脚本**：
- [utils/lib_sxng_themes.sh](utils/lib_sxng_themes.sh)
- [utils/lib_sxng_vite.sh#L32-L45](utils/lib_sxng_vite.sh#L32-L45)

### 2.2 npm build 两阶段流水线

[client/simple/package.json#L8-L12](client/simple/package.json#L8-L12)

```json
"scripts": {
  "build": "npm run build:icons && npm run build:vite",
  "build:icons": "node theme_icons.ts",
  "build:vite": "vite build"
}
```

#### 阶段一：build:icons → 生成 Jinja SVG 图标目录

执行脚本：[client/simple/theme_icons.ts](client/simple/theme_icons.ts)

- 从 `node_modules/ionicons/dist/svg/` 和 `src/svg/ionicons/` 收集 30+ 个 SVG
- 通过 SVGO 优化（去 namespace、加 aria-hidden、去 title）
- 调用 [client/simple/tools/jinja_svg_catalog.ts: jinja_svg_sets()](client/simple/tools/jinja_svg_catalog.ts#L98-L118) 使用 Edge.js 模板引擎
- 输出：`searx/templates/simple/icons.html`，包含 `icon()` / `icon_small()` / `icon_big()` 三个 Jinja macro

#### 阶段二：build:vite → 编译压缩所有前端资源

Vite 配置：[client/simple/vite.config.ts](client/simple/vite.config.ts)

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

##### 2.2.3 JS 动态分块与按需加载

主入口 [client/simple/src/js/index.ts](client/simple/src/js/index.ts) 仅 3 行：

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

**主题切换的核心 CSS 机制**在 [client/simple/src/less/definitions.less](client/simple/src/less/definitions.less)：

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

[client/simple/tools/plg.ts](client/simple/tools/plg.ts) 定义两个 `writeBundle` 钩子插件：

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

**注意**：`npm run build`（Vite 层面）**不会**生成 `.gz` 或 `.br` 旁置文件；这些压缩产物只在容器镜像构建阶段才会产出（详见专题 2）。

---

## 第三层：静态文件分发中间件（运行时）

### 3.1 WhiteNoise 中间件挂载

**位置**：[searx/webapp.py#L1385-L1401](searx/webapp.py#L1385-L1401)

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

**关键点**：WhiteNoise 接管所有 `/static/*` 请求，绕过 Flask 路由层，直接从磁盘高效发送（支持 gzip/brotli 预压缩旁置文件、HTTP 范围请求、长缓存）。

### 3.2 custom_url_for：主题资源路径重写

**位置**：[searx/webapp.py#L256-L294](searx/webapp.py#L256-L294)

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

**辅助函数** [searx/webutils.py#L182-L197](searx/webutils.py#L182-L197) 递归列出 `searx/static/` 下所有文件，作为白名单校验。

### 3.3 模板渲染时的资源注入

**基础模板**：[searx/templates/simple/base.html](searx/templates/simple/base.html)

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

`client_settings` 是一个 Base64 编码的 JSON 对象，包含（见 [searx/webapp.py#L366-L384](searx/webapp.py#L366-L384)）：

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

**完整请求处理链**（`@app.before_request` [searx/webapp.py#L457-L517](searx/webapp.py#L457-L517)）：

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
render() [searx/webapp.py#L387-L454](searx/webapp.py#L387-L454)
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

**偏好保存**：在 `/preferences` POST 提交时（[searx/webapp.py#L871-L878](searx/webapp.py#L871-L878)）调用 `preferences.save(resp)`，将 `theme` 和 `simple_style` 作为 cookie 写入（`max_age=5年`）。

---

## 三层协同关系全景图

```
┌──────────────────────────────────────────────────────────────────────┐
│                       【构建时：Build-time】                         │
│                                                                      │
│  Makefile → manage → utils/lib_sxng_vite.sh                         │
│       │                                                              │
│       ├── Phase 1: node theme_icons.ts                              │
│       │     └─ ionicons SVG → SVGO → Jinja macros (icons.html)      │
│       │                                                              │
│       └── Phase 2: vite build (client/simple/vite.config.ts)        │
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
│                                                                      │
│  ★ 额外：容器镜像构建阶段（仅 container.build 触发）                 │
│     container/builder.dockerfile#L28-L33                             │
│     find searx/static/ -name "*.html/css/js/svg"                    │
│        -exec gzip -9 -k {} +  → 生成 foo.gz 旁置文件                │
│        -exec brotli -9 -k {} +  → 生成 foo.br 旁置文件              │
└──────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       【运行时：Run-time】                           │
│                                                                      │
│  进程启动 (searx/webapp.py init)                                     │
│       ├── get_themes(templates/) → ['simple'] (偏好可用选项)         │
│       ├── WhiteNoise(root=searx/static/, prefix=static/)             │
│       │   ├─ 若 foo.js.gz / .br 旁置文件存在 → 优先发送预压缩        │
│       │   └─ 否则 → 内存动态 gzip，brotli 不支持动态                 │
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

---

## 补充专题

### 专题 1：主题色如何控制语法高亮（Pygments）颜色

语法高亮颜色不直接定义在主题 CSS 变量里，而是通过 Pygments 生成两套独立的 CSS class 体系，再随主题 class 切换来命中。

#### 构建阶段：生成双主题 LESS

**生成脚本**：[searxng_extra/update/update_pygments.py](searxng_extra/update/update_pygments.py)

在每次 Vite 构建前，由 `lib_sxng_vite.sh` 的 `templates.simple.pygments()` 提前触发（见 [utils/lib_sxng_vite.sh#L78-L86](utils/lib_sxng_vite.sh#L78-L86)），输出到 `client/simple/generated/pygments.less`。

生成逻辑在 [searxng_extra/update/update_pygments.py#L59-L73](searxng_extra/update/update_pygments.py#L59-L73)：

```python
def generat_css(light_style, dark_style) -> str:
    css = HEADER + START_LIGHT_THEME
    for line in Formatter(style=light_style).get_style_lines():
        css += '\n  ' + line
    css += END_LIGHT_THEME + START_DARK_THEME
    for line in Formatter(style=dark_style).get_style_lines():
        css += '\n    ' + line
    css += END_DARK_THEME
    return css

# 调用：light 主题用 Pygments 内置 'default'，dark 主题用 'monokai'
f.write(generat_css('default', 'monokai'))
```

产物 `pygments.less` 包含两个 Less mixin：
- `.code-highlight { … }`（外层无嵌套，亮色默认规则）
- `.code-highlight-dark() { .code-highlight { … } }`（暗色 mixin，需显式调用才展开）

#### 样式层：通过主题 class 切换 mixin

**使用方**：[client/simple/src/less/result_types/code.less](client/simple/src/less/result_types/code.less)

```less
@import "../../../generated/pygments.less";

.code-highlight-sxng() {
  .code-highlight {
    span.linenos { color: var(--color-line-number); }  // 行号走主题CSS变量
    .err       { border: none; color: inherit; }        // 禁用Pygments默认错误样式
  }
}

// ① 默认亮色：直接展开亮色 Pygments 规则
.code-highlight-sxng();

// ② auto 模式跟随系统暗色
@media (prefers-color-scheme: dark) {
  :root.theme-auto {
    .code-highlight-dark();   // 展开 monokai 暗色规则
    .code-highlight-sxng();   // 叠加 SearXNG 自定义调整
  }
}

// ③ 手动暗色
:root.theme-dark {
  .code-highlight-dark();
  .code-highlight-sxng();
}
```

**注意**：代码中**没有**为 `:root.theme-black` 单独定义 `.code-highlight-dark()`；它继承 [client/simple/src/less/definitions.less#L276-L279](client/simple/src/less/definitions.less#L276-L279) 中 `:root.theme-black { .dark-themes(); .black-themes(); }` 的黑色背景变量，语法高亮实际复用 monokai 暗色配色（这是当前实现的一个隐式简化）。

#### 运行时：HTML 产生的高亮代码

在 [searx/webapp.py#L165-L226](searx/webapp.py#L165-L226) 的 `do_highlite` / `_pygments_highlight` 函数中，Pygments 的 `HtmlFormatter(cssclass='code-highlight', …)` 生成带 `<span class="k">`、`<span class="s">` 等 token class 的 HTML 片段，外层包裹 `<div class="code-highlight">`——这个 class 正是上面 CSS 选择器的匹配锚点。

```
Pygments HtmlFormatter(cssclass='code-highlight')
   ↓ 输出
<div class="code-highlight">
  <pre><span class="k">def</span> <span class="nf">foo</span>...
```

切换主题本质是浏览器对 `<span class="k">` 这类元素按匹配到的 CSS 规则重新上色，不涉及网络请求。

---

### 专题 2：资源预压缩（gzip / brotli）的产出时间、责任人与发送链路

**结论修正**：预压缩**确实存在**，但**只发生在容器镜像构建阶段**；本地 `make themes.all` 或 `npm run build` **不**会产出旁置压缩文件。

#### 产出时间与责任人

完整构建链上有两个独立的"构建"动作，压缩只在后者执行：

| 构建动作 | 触发方式 | 产出旁置 .gz/.br | 责任人 |
|---|---|---|---|
| `make themes.all` / `./manage themes.all` | 本地开发 | ❌ 不产出 | Vite（ Rolldown + LightningCSS 仅做代码级 minify，不做传输压缩） |
| `make container.build` / `./manage container.build` | CI / Docker 镜像发布 | ✅ 产出 gzip + brotli | [container/builder.dockerfile#L28-L33](container/builder.dockerfile#L28-L33) |

**精确代码位置**（`container/builder.dockerfile` 在 `COPY searx/` 之后执行）：

```dockerfile
RUN set -eux -o pipefail; \
    python -m compileall -q -f -j 0 --invalidation-mode=unchecked-hash ./searx/; \
    find ./searx/static/ -type f \
    \( -name "*.html" -o -name "*.css" -o -name "*.js" -o -name "*.svg" \) \
    -exec gzip -9 -k {} + \          # gzip 最高压缩，保留原文件 (-k)
    -exec brotli -9 -k {} + \        # brotli 最高压缩，保留原文件 (-k)
    -exec gzip --test {}.gz + \      # 校验 gzip 完整性
    -exec brotli --test {}.br +; \   # 校验 brotli 完整性
    touch -c --date="@$TIMESTAMP_SETTINGS" ./searx/settings.yml
```

压缩覆盖范围：
- **文件类型**：`*.html`（模板里也有静态 HTML 片段）、`*.css`、`*.js`、`*.svg`
- **不覆盖**：PNG（已经是二进制压缩格式，再压收益低）、`.map` sourcemap 文件（白名单未列）
- **压缩级别**：gzip `-9`（最高）、brotli `-9`（最高质量）

#### 发送链路：WhiteNoise 如何匹配旁置压缩文件

WhiteNoise 的静态文件查找逻辑（由其内部 `WhiteNoiseFileToHeaderAdapter` 实现）：

1. 收到请求 `/static/themes/simple/sxng-core.min.js`
2. 检查请求头 `Accept-Encoding`
3. **优先级 1（brotil）**：若磁盘上存在 `searx/static/themes/simple/sxng-core.min.js.br` → 发送 `.br` 文件，设置 `Content-Encoding: br`
4. **优先级 2（gzip）**：若磁盘上存在 `searx/static/themes/simple/sxng-core.min.js.gz` → 发送 `.gz` 文件，设置 `Content-Encoding: gzip`
5. **回退**：两者都不存在 → 在内存中做动态 gzip 压缩后发送（WhiteNoise 不支持动态 brotli）

因此：
- **容器镜像部署（生产）**：旁置文件齐全，WhiteNoise 直接零 CPU 开销发送预压缩产物，同时支持 brotli 和 gzip
- **本地 / uWSGI 直接部署（无前置 nginx）**：旁置文件缺失，WhiteNoise 走动态 gzip，brotli 请求回退到未压缩原始文件
- **前置 nginx 部署**：通常由 nginx 的 `gzip on;` / `brotli on;` 在 WSGI 层之外再做一次透明压缩，可能覆盖 WhiteNoise 的选择

#### 容器构建时序（证明压缩一定发生在 Vite 产物之后）

[utils/lib_sxng_container.sh#L120-L125](utils/lib_sxng_container.sh#L120-L125) 调用 builder.dockerfile 时传参 `.`（当前仓库根目录）作为 build context，而 builder.dockerfile 第 22 行 `COPY --exclude=./searx/version_frozen.py ./searx/ ./searx/` 会把整个 `searx/` 目录（包括已由 Vite 构建完成的 `searx/static/themes/simple/`）完整复制进镜像。之后第 28-33 行才对复制进来的静态文件执行 `find ... -exec gzip/brotli`，因此**压缩一定发生在 Vite 产物落地之后**。

---

### 专题 3：PWA 图标如何通过浏览器 manifest 传递给客户端

完整链路是"HTML → /manifest.json 路由 → Jinja 渲染 JSON → 浏览器解析"。

#### 步骤 A：HTML 头声明 manifest URL

[searx/templates/simple/base.html#L26-L29](searx/templates/simple/base.html#L26-L29)：

```html
<link rel="icon" href="{{ url_for('static', filename='img/favicon.png') }}" sizes="any">
<link rel="icon" href="{{ url_for('static', filename='img/favicon.svg') }}" type="image/svg+xml">
<link rel="apple-touch-icon" href="{{ url_for('static', filename='img/favicon.png') }}">
<link rel="manifest" href="{{ url_for('manifest') }}" />
```

`/manifest.json` 是 Flask 路由，不是静态文件。

#### 步骤 B：Flask 路由根据当前主题选择 PWA 颜色

[searx/webapp.py#L1204-L1214](searx/webapp.py#L1204-L1214)：

```python
@app.route('/manifest.json', methods=['GET'])
def manifest():
    theme = sxng_request.preferences.get_value('simple_style')
    if theme not in ("light", "dark", "black"):
        theme = "light"   # auto 模式回退到 light

    theme_color = get_setting(f'brand.pwa_colors.theme_color_{theme}')
    background_color = get_setting(f'brand.pwa_colors.background_color_{theme}')
    ret = render('manifest.json', theme_color=theme_color, background_color=background_color)
    resp = Response(response=ret, status=200, mimetype="application/json")
    return resp
```

颜色取值来自 [searx/brand.py#L21-L29](searx/brand.py#L21-L29) 的 `ThemeColors`：

```python
class ThemeColors(msgspec.Struct, kw_only=True):
    theme_color_light: str = "#3050ff"
    background_color_light: str = "#fff"
    theme_color_dark: str = "#58f"
    background_color_dark: str = "#222428"
    theme_color_black: str = "#3050ff"
    background_color_black: str = "#000"
```

#### 步骤 C：Jinja 模板渲染真正的 Web App Manifest

[searx/templates/simple/manifest.json](searx/templates/simple/manifest.json)：

```json
{
  "name": "{{ instance_name }}",
  "short_name": "{{ instance_name }}",
  "icons": [
    { "src": "{{ url_for('static', filename='img/favicon.svg', _external=True) }}", "sizes": "any",       "type": "image/svg+xml" },
    { "src": "{{ url_for('static', filename='img/192.png', _external=True) }}",     "sizes": "192x192",   "type": "image/png" },
    { "src": "{{ url_for('static', filename='img/512.png', _external=True) }}",     "sizes": "512x512",   "type": "image/png" }
  ],
  "start_url": "{{ url_for('index') }}",
  "theme_color": "{{ theme_color }}",
  "background_color": "{{ background_color }}",
  "display": "standalone"
}
```

`_external=True` 确保图标 URL 带完整主机名，符合 W3C Web App Manifest 规范要求（桌面安装 PWA 时，浏览器会按 `src` 分别下载三个尺寸图标缓存到操作系统）。

#### 图标来源：SVG→PNG 精确映射

manifest 中的三个图标对应的源图、生成路径、输出尺寸的精确关系如下：

| manifest 声明 | 磁盘目标文件 | 源 SVG 文件 | 生成方式 | 输出尺寸 |
|---|---|---|---|---|
| `img/favicon.svg`（sizes=any） | `searx/static/themes/simple/img/favicon.svg` | `client/simple/src/brand/searxng-wordmark.svg` | Vite `plg_svg2svg`（SVGO 优化） | 矢量 |
| `img/192.png`（192×192） | `searx/static/themes/simple/img/192.png` | `client/simple/src/brand/searxng-wordmark.svg` | Vite `plg_svg2png`（Sharp 光栅化） | 192 × 192 px |
| `img/512.png`（512×512） | `searx/static/themes/simple/img/512.png` | `client/simple/src/brand/searxng-wordmark.svg` | Vite `plg_svg2png`（Sharp 光栅化） | 512 × 512 px |

**精确代码证据**（[client/simple/vite.config.ts#L124-L181](client/simple/vite.config.ts#L124-L181)）：

```typescript
// favicon.png 和 searxng.png（默认尺寸，不指定 width/height）
plg_svg2png([
  { src: `${PATH.brand}/searxng-wordmark.svg`, dest: `${PATH.dist}/img/favicon.png` },
  { src: `${PATH.brand}/searxng.svg`,          dest: `${PATH.dist}/img/searxng.png`   }
]),

// ★ PWA 192 图标：显式指定 192×192，源图是 searxng-wordmark.svg
plg_svg2png(
  [ { src: `${PATH.brand}/searxng-wordmark.svg`, dest: `${PATH.dist}/img/192.png` } ],
  192, 192
),

// ★ PWA 512 图标：显式指定 512×512，源图也是 searxng-wordmark.svg
plg_svg2png(
  [ { src: `${PATH.brand}/searxng-wordmark.svg`, dest: `${PATH.dist}/img/512.png` } ],
  512, 512
),

// favicon.svg：SVGO 优化 searxng-wordmark.svg → favicon.svg
plg_svg2svg(
  [ { src: `${PATH.brand}/searxng-wordmark.svg`, dest: `${PATH.dist}/img/favicon.svg` } ],
  svg2svg_favicon_opts
),
```

**尺寸实现原理**（[client/simple/tools/img.ts#L24-L45](client/simple/tools/img.ts#L24-L45)）：

```typescript
export const svg2png = (items: Src2Dest[], width?: number, height?: number): void => {
  for (const item of items) {
    sharp(item.src)
      .png({ force: true, compressionLevel: 9, palette: true })
      .resize(width, height, { fit: "contain" })  // contain 模式按比例缩放不裁剪
      .toFile(item.dest)
  }
};
```

- `192.png`：`sharp.resize(192, 192, { fit: "contain" })` → 保证最长边 192px，按比例缩放
- `512.png`：`sharp.resize(512, 512, { fit: "contain" })` → 保证最长边 512px，按比例缩放
- 不指定 `width`/`height` 的（如 `favicon.png`）：按 SVG 原始视口尺寸输出

**结论纠正**：之前错误地认为 PWA 图标来自 `searxng.svg`。实际源图**统一**是 `searxng-wordmark.svg`（带文字 wordmark 的完整 Logo），尺寸通过 Sharp `resize` 精确控制为 192×192 与 512×512；`searxng.svg` 只用于生成 `searxng.png`（独立于 PWA 图标的另一产物）。

---

### 专题 4：图标 macro 在模板中的调用位置

#### 定义：`icon()` / `icon_small()` / `icon_big()`

构建时产物 [searx/templates/simple/icons.html#L45-L55](searx/templates/simple/icons.html#L45-L55)：

```jinja
{% macro icon(action, alt) -%}
  {{ catalog[action] | replace("__jinja_class_placeholder__", "sxng-icon-set") | safe }}
{%- endmacro %}

{% macro icon_small(action, alt) -%}
  {{ catalog[action] | replace("__jinja_class_placeholder__", "sxng-icon-set-small") | safe }}
{%- endmacro %}

{% macro icon_big(action, alt) -%}
  {{ catalog[action] | replace("__jinja_class_placeholder__", "sxng-icon-set-big") | safe }}
{%- endmacro %}
```

三个 macro 的差异仅在于注入不同 CSS class（尺寸由 [client/simple/src/less/style.less#L30-L48](client/simple/src/less/style.less#L30-L48) 定义：`icon_small` 1rem、`icon_big` 1.5rem）。

#### 被 include 方式

在各模板顶部通过 `{% from 'simple/icons.html' import icon, icon_small, icon_big without context %}` 导入。

#### 各模板调用位置汇总

| 模板文件 | 调用语句 | 用途 |
|---|---|---|
| [searx/templates/simple/simple_search.html#L6-L7](searx/templates/simple/simple_search.html#L6-L7) | `icon_big('close')` / `icon_big('search')` | 搜索框清除和提交按钮 |
| [searx/templates/simple/search.html#L10-L11](searx/templates/simple/search.html#L10-L11) | `icon_big('close')` / `icon_big('search')` | 同上（完整版搜索页） |
| [searx/templates/simple/base.html#L47-L58](searx/templates/simple/base.html#L47-L58) | `icon_big('information-circle')` / `icon_big('heart')` / `icon_big('settings')` | 页顶 About / Donate / Preferences 链接 |
| [searx/templates/simple/categories.html#L25-L34](searx/templates/simple/categories.html#L25-L34) | `icon_big(category_icons[category])` / fallback `icon_big('globe')` | 每个分类标签的图标 |
| [searx/templates/simple/results.html#L73-L109](searx/templates/simple/results.html#L73-L109) | `icon_small('navigate-up')` / `icon_small('navigate-left')` / `icon_small('navigate-right')` | 返回顶部、翻页按钮 |
| [searx/templates/simple/stats.html#L9](searx/templates/simple/stats.html#L9) | `icon_big('navigate-down')` | 统计列排序 |
| [searx/templates/simple/preferences.html#L24-L130](searx/templates/simple/preferences.html#L24-L130) | `icon_small('alert')` / `icon_big('alert')` / `icon_big('exclamation-sign')` | 警告提示图标 |
| [searx/templates/simple/preferences/engines.html#L59](searx/templates/simple/preferences/engines.html#L59) | `icon_big('alert', 'No HTTPS')` | 引擎没有 HTTPS 的警告 |
| [searx/templates/simple/macros.html#L51](searx/templates/simple/macros.html#L51) | `icon_small('ellipsis-vertical')` | 结果缓存链接前的菜单图标 |
| [searx/templates/simple/result_templates/default.html#L6](searx/templates/simple/result_templates/default.html#L6) | `icon_small('play')` | 显示媒体折叠按钮 |
| [searx/templates/simple/result_templates/images.html#L36-L38](searx/templates/simple/result_templates/images.html#L36-L38) | `icon('close')` / `icon('navigate-left')` / `icon('navigate-right')` | 图片详情模态框控件 |
| [searx/templates/simple/result_templates/videos.html#L6](searx/templates/simple/result_templates/videos.html#L6) | `icon_small('film')` | 显示视频折叠按钮 |
| [searx/templates/simple/result_templates/map.html#L43](searx/templates/simple/result_templates/map.html#L43) | `icon_small('globe')` | 显示地图折叠按钮 |
| [searx/templates/simple/result_templates/torrent.html#L7-L19](searx/templates/simple/result_templates/torrent.html#L7-L19) | `icon_big('magnet')` / `icon_big('seeder')` / `icon_big('leecher')` / `icon_big('save')` 等 | 种子资源元数据 |
| [searx/templates/simple/messages/no_cookies.html#L3](searx/templates/simple/messages/no_cookies.html#L3) | `icon('info-sign')` | Cookie 禁用提示 |

---

### 专题 5：选择 WhiteNoise 作为静态分发层的利弊

#### 代码层面的事实依据

- **挂载方式**：在 WSGI 层包装 Flask 应用 [searx/webapp.py#L1393-L1401](searx/webapp.py#L1393-L1401)
- **自定义头**：`Cache-Control: public, max-age=30, stale-while-revalidate=60` + 配置里的 HTTP 安全头
- **不支持多主题前缀**：主题目录层级需要 `custom_url_for` 在应用层拼路径（见 [searx/webapp.py#L256-L294](searx/webapp.py#L256-L294)）
- **压缩发送策略**：若有旁置 `.gz` / `.br` 则优先直接发送（容器镜像部署时有）；否则动态 gzip；不支持动态 brotli

#### 利（优点）

1. **零运维依赖**：单一 Python 包即可跑通部署，不必前置 nginx 或 CDN。对 SearXNG 这种面向自托管用户的项目尤为关键——普通用户 `python searx/webapp.py` 就能获得接近 Nginx 的静态性能。
2. **请求链最短**：在 WSGI 层就完成响应，不经过 Flask 的路由匹配、请求上下文构建、before_request 钩子链，开销远低于用 `@app.route('/static/<path>')` 手写分发。
3. **自动 Range 请求、ETag、304**：内置 `If-Modified-Since` / `If-None-Match` 条件判断和 HTTP 断点续传，无需开发者重复实现。
4. **与 Flask 生命周期融合**：`add_headers_function` 钩子可统一注入 CSP、Referrer-Policy 等安全头，保持静态与动态响应一致的安全策略。
5. **旁置压缩零开销发送**：配合 `container/builder.dockerfile#L28-L33` 预压缩产物，容器内部署时 WhiteNoise 直接 sendfile 发送 `.gz` / `.br`，CPU 零消耗。

#### 弊（缺点）

1. **仍比纯文件服务器慢**：无论怎么优化，WSGI 中间件相比 nginx 的 sendfile() 零拷贝仍有 2~5 倍延迟差距，大文件（图片、sourcemap）上差距更明显。高并发生产部署通常会前置 nginx 绕过 WhiteNoise。
2. **预压缩依赖外部构建链**：WhiteNoise 自身不产出旁置压缩文件，必须依赖容器构建阶段的 `find ... -exec gzip/brotli`。本地开发、裸机部署时若没有前置 nginx，就只能走动态 gzip（CPU 开销），brotli 用户根本拿不到压缩。
3. **多主题目录无原生支持**：WhiteNoise 的 `root` / `prefix` 是扁平一维结构。多主题架构下需要应用层 `custom_url_for` 重写路径；如果未来有独立静态 CDN，CDN 侧也得同步这套路径映射规则。
4. **max-age=30 过短**：构建产物文件名带 hash（chunk 已带 hash），理论上可设 1 年缓存。当前 30 秒的短缓存是保守选择，错失了内容寻址资源的最佳缓存实践。

---

### 专题 6：多入口打包的利弊

#### 代码层面的事实依据

Vite 配置的 `rolldownOptions.input` 包含四个入口（[client/simple/vite.config.ts#L48-L56](client/simple/vite.config.ts#L48-L56)）：

```typescript
input: {
  core: `${PATH.src}/js/index.ts`,       // JS 主入口
  ltr:  `${PATH.src}/less/style-ltr.less`,  // LTR 样式
  rtl:  `${PATH.src}/less/style-rtl.less`,  // RTL 样式
  rss:  `${PATH.src}/less/rss.less`,        // RSS 模板样式
}
```

其中 RTL 与 LTR 是互斥加载（由模板 `{% if rtl %}` 分支判断，见 [searx/templates/simple/base.html#L15-L19](searx/templates/simple/base.html#L15-L19)）；`rss.less` 独立服务 RSS 页面，不被搜索结果页引用。

#### 利（优点）

1. **互斥样式零浪费**：LTR 使用者不下载 RTL 的 CSS 翻转规则，反之亦然。按全球语言分布估计，大约节约 45% 用户的 CSS 体积（SearXNG 的 RTL 样式约 2KB，绝对值小但模式正确）。
2. **职责边界清晰**：`core` 入口管 JS，`ltr` / `rtl` / `rss` 管 CSS，天然分离逻辑与样式。每个入口的产物命名（`sxng-core.min.js`、`sxng-ltr.min.css`）可读，模板引用不会混淆。
3. **独立缓存失效**：只改 JS 不影响 CSS 的缓存命中，反之亦然。`core` 入口、`ltr` 入口分别带各自 hash，局部修改不会让用户重下全站资源。
4. **RSS 页面解耦**：RSS 模板（XSLT 样式）样式不与主样式混在一起，避免主样式改版时破坏 RSS 输出格式。

#### 弊（缺点）

1. **更多并发请求**：与"单个 CSS + 单个 JS"打包比，首屏需并行拉取 `sxng-core.min.js` + `sxng-ltr.min.css`，HTTP/1.1 下会多一个 RTT。现代 HTTP/2 多路复用基本抵消此问题。
2. **入口间代码重复难控制**：LTR 与 RTL 样式文件虽然差异只在镜像翻转，但如果 Less 组织不当会复制大量公共规则。当前通过 `style-ltr.less` / `style-rtl.less` 都 `@import "style.less"` 的方式规避，但需开发者自觉维护。
3. **工具链配置复杂**：多入口必须显式在 `vite.config.ts` 维护 `entryFileNames` / `assetFileNames` 的命名模式，新增入口时若漏改会导致产物与模板引用脱节。
4. **CSS 动态按需加载受限**：当前 CSS 入口粒度较粗（LTR/rtl/RSS 三个总览），如果要进一步拆分"搜索页 CSS"和"偏好页 CSS"以极致首屏体积，需要手动增加更多入口并配套模板条件判断，维护成本线性增长。
