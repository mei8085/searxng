# SearXNG 元搜索引擎多语言 (i18n) 流程全链路分析

本文档沿着代码梳理 SearXNG 的多语言实现：从**翻译资源加载** → **语言协商策略** → **模板上下文注入** → **最终页面渲染** 的完整串联路径。

---

## 一、整体架构概览

```
┌─────────────────────────────────────────────────────────────────────┐
│                         应用启动阶段 (init)                         │
│  1. searx/__init__.py: init_settings() → 加载配置                   │
│  2. webapp.py: Babel(app, locale_selector=get_locale)              │
│  3. locales.py: locales_initialize() → 猴子补丁 + 加载元数据        │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        请求进入阶段 (before_request)                │
│  pre_request():                                                      │
│  1. ClientPref.from_http_request() → 解析 Accept-Language          │
│  2. Preferences 实例化 → 从 Cookie / Form 加载用户偏好             │
│  3. 若 locale 未设置 → _get_browser_language() 匹配浏览器语言      │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     Babel 语言选择回调 (locale_selector)            │
│  localeselector():                                                   │
│  1. 从 preferences 取 locale 值                                     │
│  2. 处理 ADDITIONAL_TRANSLATIONS（babel 不支持的语言）              │
│  3. LOCALE_BEST_MATCH 回退映射                                      │
│  4. hyphen → underscore（zh-CN → zh_CN）                           │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       翻译资源加载 (get_translations)               │
│  flask_babel.get_translations 被猴子补丁替换:                       │
│  - 普通语言: 走 babel 默认 → 读取 translations/{lang}/LC_MESSAGES   │
│  - 附加语言(dv/oc/szl/pap): 直接 Translations.load() 加载          │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      模板渲染阶段 (render 函数)                     │
│  render() 注入 i18n 上下文:                                         │
│  1. kwargs['sxng_locales'] → 语言下拉选项列表                       │
│  2. kwargs['locale_rfc5646'] → <html lang=""> 属性值               │
│  3. kwargs['rtl'] → 是否 RTL 布局                                   │
│  4. kwargs['current_language'] → 当前搜索语言                       │
│  5. kwargs['translations'] → 前端 JS 用的翻译字典                   │
│  6. Flask-Babel 自动注入 _() / gettext() 到 Jinja2 环境            │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        模板中翻译使用                               │
│  {{ _('About') }}          ← 普通文案翻译                          │
│  lang="{{ locale_rfc5646 }}" ← HTML lang 属性                      │
│  dir="rtl"                 ← RTL 方向                               │
│  {% for l in sxng_locales %}  ← 语言列表遍历                       │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 二、翻译资源加载

### 2.1 翻译文件物理结构

翻译资源位于 [searx/translations/](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/translations) 目录，采用标准 GNU gettext 目录布局：

```
searx/translations/
├── messages.pot                    # 翻译模板（pot）
├── en/LC_MESSAGES/
│   ├── messages.po                 # 可编辑的 PO 文件
│   └── messages.mo                 # 编译后的二进制 MO 文件
├── zh_Hans_CN/LC_MESSAGES/
│   ├── messages.po
│   └── messages.mo
├── zh_Hant_TW/LC_MESSAGES/
├── fr/LC_MESSAGES/
├── de/LC_MESSAGES/
├── dv/LC_MESSAGES/                 # 附加语言: Dhivehi
├── oc/LC_MESSAGES/                 # 附加语言: Occitan
├── szl/LC_MESSAGES/                # 附加语言: Silesian
├── pap/LC_MESSAGES/                # 附加语言: Papiamento
└── ... (共约 80+ 种语言)
```

**关键点**:
- 语言目录名使用 **下划线分隔**（如 `zh_Hans_CN`），这是 babel 内部标准格式
- 页面层展示给用户的是 **连字符分隔**（如 `zh-CN`），在 `localeselector()` 中转换

### 2.2 语言元数据 (locales.json)

[searx/data/locales.json](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/data/locales.json) 是预生成的数据文件，存储两类关键信息：

| 字段 | 类型 | 说明 |
|------|------|------|
| `LOCALE_NAMES` | `dict[str, str]` | locale_tag → 本地化语言名称（如 `"zh-CN": "中文 (中国)"`） |
| `RTL_LOCALES` | `list[str]` | 从右到左书写的语言列表（如 `ar`, `he`, `fa-IR`） |

通过 [searx/data/__init__.py](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/data/__init__.py#L81-L95) 的**懒加载机制**按需读取：

```python
def __getattr__(name: str) -> t.Any:
    # 首次访问 searx.data.LOCALES 时触发
    with open(data_dir / data_json_files[name], encoding='utf-8') as f:
        lazy_globals[name] = json.load(f)
    return lazy_globals[name]
```

### 2.3 翻译目录列表扫描

[searx/locales.py](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/locales.py#L123-L139) 中的 `get_translation_locales()` 扫描所有可用翻译目录：

```python
def get_translation_locales() -> list[str]:
    for folder in (Path(searx_dir) / 'translations').iterdir():
        if folder.is_dir() and (folder / 'LC_MESSAGES').is_dir():
            tr_locales.append(folder.name)  # 收集如 'zh_Hans_CN', 'en', 'dv' 等
    _TR_LOCALES = sorted(tr_locales)
    return _TR_LOCALES
```

### 2.4 初始化入口：locales_initialize()

在 [webapp.py:init()](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/webapp.py#L1349-L1383) 中调用 [locales_initialize()](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/locales.py#L142-L150)：

```python
def locales_initialize():
    # 1. 猴子补丁: 替换 flask_babel 默认的翻译加载器
    flask_babel.get_translations = get_translations

    # 2. 加载语言元数据（触发懒加载 searx.data.LOCALES）
    LOCALE_NAMES.update(data.LOCALES["LOCALE_NAMES"])
    RTL_LOCALES.update(data.LOCALES["RTL_LOCALES"])
```

### 2.5 特殊语言的翻译加载（猴子补丁）

babel 本身不支持某些小语种（Dhivehi、Occitan、Silesian、Papiamento），通过自定义 [get_translations()](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/locales.py#L110-L117) 绕过：

```python
def get_translations():
    """Monkey patch of flask_babel.get_translations"""
    if has_request_context():
        use_translation = sxng_request.form.get('use-translation')
        if use_translation in ADDITIONAL_TRANSLATIONS:
            # 直接从磁盘加载 babel 不识别的语言 MO 文件
            babel_ext = flask_babel.current_app.extensions['babel']
            return Translations.load(babel_ext.translation_directories[0], use_translation)
    return _flask_babel_get_translations()  # 其余情况走默认逻辑
```

---

## 三、语言协商策略

### 3.1 核心语言配置概念

SearXNG 将"语言"分为两个独立维度，存储在 `Preferences` 中：

| 配置项 | Key | 作用域 | 说明 |
|--------|-----|--------|------|
| 搜索语言 | `language` | 搜索请求 | 传给各搜索引擎，限定搜索结果语言 |
| 界面语言 | `locale` | UI 渲染 | 决定模板文案、日期/数字格式化、RTL 布局 |

定义位置：[preferences.py:L407-L422](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/preferences.py#L407-L422)

```python
'language': SearchLanguageSetting(      # 搜索语言
    get_setting("search.default_lang"),
    choices=get_setting("search.languages") + [""],
),
'locale': EnumStringSetting(            # 界面语言
    get_setting("ui.default_locale"),
    choices=list(LOCALE_NAMES.keys()) + [""],
),
```

### 3.2 语言来源优先级（协商链路）

每个请求到达时，按照以下优先级从高到低确定最终 locale：

```
优先级 1:  FORM 参数           (?locale=zh-CN 提交)
优先级 2:  Cookie 存储         (用户偏好已保存)
优先级 3:  URL 参数            (?preferences=xxx 编码偏好)
优先级 4:  Accept-Language 头   (浏览器自动发送)
优先级 5:  settings 默认值      (ui.default_locale)
兜底:       'en'
```

**协商流程代码路径**:

1. **[pre_request() 中间件](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/webapp.py#L457-L518)** — Flask `before_request` 钩子，请求进入后第一时间执行：

```python
@app.before_request
def pre_request():
    # Step A: 从 Accept-Language 解析浏览器偏好
    client_pref = ClientPref.from_http_request(sxng_request)

    # Step B: 构造 Preferences 对象
    preferences = Preferences(themes, categories, engines, plugins, client_pref)
    sxng_request.preferences = preferences

    # Step C: 从 Cookie 加载
    preferences.parse_dict(sxng_request.cookies)

    # Step D: 从 GET/POST 合并（优先级高于 Cookie）
    sxng_request.form = dict(sxng_request.form.items())
    for k, v in sxng_request.args.items():
        if k not in sxng_request.form:
            sxng_request.form[k] = v

    # Step E: 支持通过 URL 参数压缩编码传递偏好
    if sxng_request.form.get('preferences'):
        preferences.parse_encoded_data(sxng_request.form['preferences'])
    else:
        preferences.parse_dict(sxng_request.form)

    # Step F: 若 language 仍为空，匹配浏览器 Accept-Language
    if not preferences.get_value("language"):
        language = _get_browser_language(sxng_request, settings['search']['languages'])
        preferences.parse_dict({"language": language})

    # Step G: 若 locale 仍为空，匹配浏览器 Accept-Language
    if not preferences.get_value("locale"):
        locale = _get_browser_language(sxng_request, LOCALE_NAMES.keys())
        preferences.parse_dict({"locale": locale})
```

2. **[ClientPref.from_http_request()](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/preferences.py#L359-L387)** — 解析 HTTP `Accept-Language` 请求头：

```python
@classmethod
def from_http_request(cls, http_request: SXNG_Request):
    al_header = http_request.headers.get("Accept-Language")
    # 例: "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7"

    pairs: list[tuple[babel.Locale, float]] = []
    for l in al_header.split(','):
        lang, qvalue = ...  # 解析 q 值
        locale = babel.Locale.parse(lang, sep='-')
        pairs.append((locale, qvalue))

    # 按 q 值排序，取最高优先级
    pairs.sort(reverse=True, key=lambda x: x[1])
    locale = pairs[0][0]  # 取浏览器最偏好的语言
    return cls(locale=locale)
```

3. **[match_locale()](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/locales.py#L372-L418)** — 将浏览器语言匹配到实际可用列表：

```python
# _get_browser_language 内部调用
def _get_browser_language(req, lang_list):
    client = ClientPref.from_http_request(req)
    locale = match_locale(client.locale_tag, lang_list, fallback='en')
    return locale
```

### 3.3 Babel locale_selector 回调

在 [webapp.py:L154-L160](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/webapp.py#L154-L160) 中，将选择器注册到 Flask-Babel：

```python
def get_locale():
    locale = localeselector()
    return locale

babel = Babel(app, locale_selector=get_locale)
```

核心处理函数 [localeselector()](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/locales.py#L86-L107)：

```python
def localeselector():
    locale: str = 'en'
    if has_request_context():
        value: str = sxng_request.preferences.get_value('locale')
        if value:
            locale = value

    # 附加语言标记（供 get_translations 猴子补丁使用）
    if locale in ADDITIONAL_TRANSLATIONS:
        sxng_request.form['use-translation'] = locale

    # babel 不支持的 locale → 回退到近似语言
    locale = LOCALE_BEST_MATCH.get(locale, locale)
    # 例: "zh-HK" → "zh-Hant-TW", "nl-BE" → "nl"

    if locale == '':
        locale = 'en'

    # 连字符转下划线 (babel 内部格式)
    locale = locale.replace('-', '_')
    # 例: "zh-CN" → "zh_CN", "zh-Hans-CN" → "zh_Hans_CN"
    return locale
```

**LOCALE_BEST_MATCH 映射表**（[locales.py:L73-L80](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/locales.py#L73-L80)）：

| 用户选择 | 回退到 | 原因 |
|----------|--------|------|
| `dv` | `si` | Dhivehi 用僧伽罗语近似处理日期格式化 |
| `oc` | `fr-FR` | Occitan 用法语地区格式 |
| `szl` | `pl` | Silesian 用波兰语格式 |
| `nl-BE` | `nl` | 比利时荷兰语 → 标准荷兰语 |
| `zh-HK` | `zh-Hant-TW` | 香港 → 繁体台湾翻译 |
| `pap` | `pt-BR` | Papiamento → 巴西葡萄牙语 |

---

## 四、模板上下文注入

### 4.1 自定义 render() 函数

SearXNG 不直接使用 Flask 的 `render_template()`，而是封装了 [render()](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/webapp.py#L387-L454) 函数，在调用模板前集中注入所有 i18n 相关上下文：

```python
def render(template_name: str, **kwargs):
    # ── 1. 客户端设置（含前端 JS 翻译字典） ──
    client_settings = get_client_settings()
    kwargs['client_settings'] = base64.b64encode(json.dumps(client_settings)...)
    kwargs['preferences'] = sxng_request.preferences
    kwargs.update(client_settings)  # 包含 translations 字典

    # ── 2. 注入 i18n 上下文变量 ──

    # (a) 搜索语言下拉列表（过滤掉配置中禁用的语言）
    kwargs['sxng_locales'] = [
        l for l in sxng_locales
        if l[0] in settings['search']['languages']
    ]

    # (b) <html lang="zh-CN"> 属性值（RFC5646 格式，不含 script 子标签）
    locale = sxng_request.preferences.get_value('locale')
    kwargs['locale_rfc5646'] = _get_locale_rfc5646(locale)
    # 例: "zh-Hans-CN" → "zh-CN"（去掉 script 部分，兼容 Chrom* 语言检测）

    # (c) RTL 布局开关
    if locale in RTL_LOCALES and 'rtl' not in kwargs:
        kwargs['rtl'] = True

    # (d) 当前选择的搜索语言
    if 'current_language' not in kwargs:
        kwargs['current_language'] = parse_lang(...)

    # ── 3. 最终交给 Flask 渲染 ──
    result = render_template(
        '{}/{}'.format(kwargs['theme'], template_name),
        **kwargs
    )
    return result
```

### 4.2 前端 JS 翻译字典

`get_client_settings()` → `get_translations()` 收集少量供前端 TypeScript 代码直接使用的翻译：

```python
def get_translations():
    return {
        'no_item_found': gettext('No item found'),          # 自动补全无结果
        'Source': gettext('Source'),                        # 引擎描述来源
        'error_loading_next_page': gettext('Error loading the next page'),  # 无限滚动
    }
```

最终以 base64 编码形式注入到 `<script client_settings="...">` 属性，由前端 `loader.ts` 解析。

### 4.3 Flask-Babel 自动注入的翻译函数

通过 `Babel(app, locale_selector=...)` 初始化时，Flask-Babel 会自动向 Jinja2 环境注入以下全局函数：

| 模板中使用 | 等价 Python 代码 | 作用 |
|-----------|-----------------|------|
| `_('text')` | `flask_babel.gettext('text')` | 单数翻译（最常用） |
| `gettext('text')` | 同上 | 显式写法 |
| `ngettext(s, p, n)` | `flask_babel.ngettext` | 复数翻译 |
| `pgettext(c, t)` | `flask_babel.pgettext` | 带上下文的翻译 |

无需在 `render()` 中手动添加，Jinja 模板中可直接调用 `{{ _('About') }}`。

---

## 五、模板层翻译使用示例

### 5.1 base.html — 全局语言属性与公用文案

[searx/templates/simple/base.html](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/templates/simple/base.html)

```html
<!-- 使用 locale_rfc5646 + rtl 注入值 -->
<html lang="{{ locale_rfc5646 }}" {% if rtl %} dir="rtl"{% endif %}>
<head>
  <!-- 条件加载 LTR / RTL 样式 -->
  {% if rtl %}
  <link rel="stylesheet" href="{{ url_for('static', filename='sxng-rtl.min.css') }}">
  {% else %}
  <link rel="stylesheet" href="{{ url_for('static', filename='sxng-ltr.min.css') }}">
  {% endif %}
</head>
<body>
  <!-- 使用 _() 翻译普通文案 -->
  <a href="{{ url_for('info', pagename='about') }}">
    {{ icon_big('information-circle') }}
    <span>{{ _('About') }}</span>
  </a>
  <a href="{{ url_for('preferences') }}">
    <span>{{ _('Preferences') }}</span>
  </a>

  <footer>
    <p>
    {{ _('Powered by') }} <a href="...">SearXNG</a>
    — {{ _('a privacy-respecting, open metasearch engine') }}
    </p>
  </footer>
</body>
</html>
```

### 5.2 ui_locale.html — 界面语言切换下拉框

[searx/templates/simple/preferences/ui_locale.html](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/templates/simple/preferences/ui_locale.html)

```html
<fieldset>
  <legend>{{ _('Interface language') }}</legend>
  <div class="value">
    <!-- locales 变量 = LOCALE_NAMES 字典 (render 中注入) -->
    <select name='locale'>
      {% for locale_id, locale_name in locales.items() | sort %}
        <option value="{{ locale_id }}"
                {% if locale_id == current_locale %}selected{% endif %}>
          {{ locale_name }}
        </option>
      {% endfor %}
    </select>
  </div>
  <div class="description">
    {{ _('Change the language of the layout') }}
  </div>
</fieldset>
```

用户提交选择后，流程：
1. POST 到 `/preferences` → `preferences.parse_form()` 解析 `locale` 字段
2. 验证 `locale` 值是否在 `LOCALE_NAMES.keys()` 中（EnumStringSetting 校验）
3. 通过 `preferences.save(resp)` 写入 Cookie（5 年有效期）
4. 后续请求的 `pre_request()` 从 Cookie 恢复该值

---

## 六、关键文件索引

| 文件 | 作用 |
|------|------|
| [searx/locales.py](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/locales.py) | 语言选择核心逻辑：`localeselector()`、翻译加载补丁、语言匹配算法 |
| [searx/webapp.py](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/webapp.py) | Flask 应用入口：`pre_request` 中间件、`render()` 上下文注入、`Babel` 初始化 |
| [searx/preferences.py](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/preferences.py) | 偏好存储：`Preferences`、`ClientPref`（Accept-Language 解析）、Cookie 序列化 |
| [searx/sxng_locales.py](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/sxng_locales.py) | 预生成的搜索语言/区域元组列表（含国旗 emoji） |
| [searx/data/__init__.py](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/data/__init__.py) | 懒加载 `locales.json` 等数据文件 |
| [searx/data/locales.json](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/data/locales.json) | `LOCALE_NAMES` 与 `RTL_LOCALES` 数据 |
| [searx/translations/](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/translations) | gettext 翻译文件（.mo / .po），80+ 语言 |
| [searx/templates/simple/base.html](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/templates/simple/base.html) | 顶层模板：使用 `_()` 翻译、`locale_rfc5646`、`rtl` |
| [searx/templates/simple/preferences/ui_locale.html](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/templates/simple/preferences/ui_locale.html) | 界面语言切换下拉框模板 |

---

## 七、一次完整请求的调用栈时序（以首页为例）

```
浏览器 GET /
  │
  ├── Flask before_request 触发
  │     └── pre_request()
  │           ├── ClientPref.from_http_request()  ── 解析 Accept-Language
  │           ├── Preferences(...) 实例化           ── 从 settings 取 default_locale
  │           ├── preferences.parse_dict(cookies)   ── 从 Cookie 恢复 locale
  │           ├── 合并 GET/POST form
  │           ├── preferences.parse_dict(form)
  │           └── [若 locale 空] _get_browser_language()
  │                     └── match_locale()           ── 浏览器语言 → 可用列表
  │
  ├── Flask-Babel locale_selector 触发
  │     └── get_locale()
  │           └── localeselector()
  │                 ├── sxng_request.preferences.get_value('locale')
  │                 ├── 附加语言 → sxng_request.form['use-translation'] = dv
  │                 ├── LOCALE_BEST_MATCH 回退
  │                 └── zh-CN → zh_CN (返回给 babel)
  │
  ├── flask_babel.get_translations 触发（首次翻译）
  │     └── get_translations() [猴子补丁版本]
  │           └── [若 use-translation]
  │                 └── Translations.load(translations/dv)
  │           └── [否则]
  │                 └── 读取 translations/zh_CN/LC_MESSAGES/messages.mo
  │
  ├── 路由 index() 执行
  │     └── render('index.html', ...)
  │           ├── get_client_settings()
  │           │     └── get_translations()  # 注意！这是另一个同名函数
  │           │           ├── gettext('No item found')     ← 触发 babel 翻译
  │           │           ├── gettext('Source')
  │           │           └── gettext('Error loading ...')
  │           ├── 注入 sxng_locales, locale_rfc5646, rtl, current_language
  │           └── render_template('simple/index.html', **kwargs)
  │
  ├── Jinja2 模板渲染
  │     ├── 访问 {{ locale_rfc5646 }}  → "zh-CN"
  │     ├── 访问 {% if rtl %}           → False（中文不是 RTL）
  │     ├── {{ _('About') }}            → flask_babel.gettext
  │     │     └── 查 Translations 字典  → "关于"
  │     ├── {{ _('Preferences') }}      → "偏好设置"
  │     └── ... 逐模板渲染继承链 (base.html → index.html)
  │
  └── 返回 HTML 响应
        └── 浏览器显示：中文界面，<html lang="zh-CN">
```
