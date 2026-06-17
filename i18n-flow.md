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

### 2.5 特殊语言的翻译加载（猴子补丁 + 双轨制兜底）

python-babel 的 `Locale.parse()` 基于 CLDR 数据库识别语言代码，对于 Dhivehi (`dv`)、Occitan (`oc`)、Silesian (`szl`)、Papiamento (`pap`) 这 4 种小语种，babel 的 locale 数据库中没有完整的元数据（缺少月份名称、日期格式、数字格式等），直接使用会抛出 `babel.core.UnknownLocaleError`。

SearXNG 采用"**翻译真实加载 + 格式近似兜底**"的双轨制方案完整绕过：

```
用户选择 locale = "dv" (Dhivehi 迪维希语)
  │
  ├── ① localeselector() 阶段
  │     ├── 标记 sxng_request.form['use-translation'] = 'dv'   ← 真实翻译目标
  │     └── LOCALE_BEST_MATCH['dv'] = 'si'                      ← babel 近似格式用
  │         返回给 babel 的 locale = 'si' (Sinhala 僧伽罗语)
  │
  ├── ② babel 内部初始化阶段
  │     └── 用 'si' 构建 babel.Locale → 成功获取日期/数字格式化器
  │         （月份、星期、千位分隔符等均走僧伽罗语格式，近似可用）
  │
  └── ③ 首次调用 gettext() 时，猴子补丁 get_translations() 触发
        └── Translations.load(..., 'dv')
              └── 直接读取 translations/dv/LC_MESSAGES/messages.mo
                  界面文案显示真实的迪维希语翻译
```

**核心代码拆解**：

[locales.py:ADDITIONAL_TRANSLATIONS](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/locales.py#L64-L71) 定义了这 4 种特殊语言：

```python
ADDITIONAL_TRANSLATIONS = {
    "dv": "ދިވެހި (Dhivehi)",
    "oc": "Occitan",
    "szl": "Ślōnski (Silesian)",
    "pap": "Papiamento",
}
```

[locales.py:LOCALE_BEST_MATCH](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/locales.py#L73-L80) 为每种特殊语言分配一个 babel 能识别的近似语言，只负责日期/数字格式化：

```python
LOCALE_BEST_MATCH = {
    "dv":  "si",     # Dhivehi  → Sinhala（书写系统接近）
    "oc":  "fr-FR",  # Occitan  → 法国法语（地理邻近）
    "szl": "pl",     # Silesian → 波兰语（西斯拉夫语支）
    "pap": "pt-BR",  # Papiamento → 巴西葡萄牙语（受葡语影响深）
    ...
}
```

猴子补丁的实现 [get_translations()](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/locales.py#L110-L117)：

```python
def get_translations():
    """Monkey patch of flask_babel.get_translations"""
    if has_request_context():
        use_translation = sxng_request.form.get('use-translation')
        if use_translation in ADDITIONAL_TRANSLATIONS:
            # 用 Translations.load() 绕过 babel 的 Locale.parse 校验，
            # 直接按 GNU gettext 标准目录结构读取 .mo 文件
            babel_ext = flask_babel.current_app.extensions['babel']
            return Translations.load(babel_ext.translation_directories[0], use_translation)
    return _flask_babel_get_translations()  # 其余 80+ 种语言走 babel 默认逻辑
```

**为什么不用 locale 别名或 fork babel**：修改 babel 上游的 CLDR 数据维护成本极高，且这些小语种的翻译志愿者人数很少。SearXNG 的方案把复杂度隔离在应用层——`Translations.load()` 本身只关心目录结构和 .mo 文件格式，不校验 locale 是否在 babel 数据库中，因此可以无缝工作。

这些特殊语言也会被纳入 [locales.json](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/data/locales.json) 的预生成流程——在 [searxng_extra/update/update_locales.py](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searxng_extra/update/update_locales.py#L35-L47) 中，先用近似语言的 `babel.Locale` 判断 RTL 方向，然后把真实的翻译名称写入 `LOCALE_NAMES`。

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

#### 为什么界面语言和搜索语言要分开成两个独立维度？

这两个维度的分离是 SearXNG 作为元搜索引擎的**核心设计抉择**，而非技术巧合，原因有三：

**1. 语义目标不同**

- **界面语言 (`locale`)** 是 UI/UX 概念：按钮、菜单、错误提示、偏好设置页的文案显示。它的选择集合由"是否有社区志愿者完成了 PO 翻译"决定，目前约 65 种（见 [LOCALE_NAMES](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/data/locales.json) 的 key 数）。
- **搜索语言 (`language`)** 是信息检索概念：作为参数传给 Google、DuckDuckGo、Wikipedia 等几十种后端引擎，限定返回结果的语言。它的选择集合由"各搜索引擎在其 traits 中声明了哪些 locale tag"决定，覆盖约 100+ 种语言/地区组合（见 [sxng_locales.py](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/sxng_locales.py)）。

两个集合既不相等也不互相包含——例如搜索语言支持 `is`（冰岛语）但界面没有冰岛语翻译；反过来界面有 `oc`（奥克语）翻译，但大多数搜索引擎根本不支持奥克语检索。

**2. 典型使用场景要求独立控制**

真实世界大量用户需要两者不同：
- 旅居德国的中国开发者：界面用英文（`locale=en`）避免德语不熟练，但搜索结果限定中文（`language=zh-CN`）
- 研究北欧历史的法国学者：界面用法语（`locale=fr`），搜索语言设为瑞典语、丹麦语、挪威语多语言
- 不丹本地人：界面用英语（唯一有翻译的选项），但搜索语言指定 `dz`（宗喀语）

如果把两个维度合并成一个 "language" 设置，就会强制用户在"看得懂界面"和"搜得到结果"之间二选一。

**3. 底层处理链路完全不同**

两者在代码中走完全独立的管道：

```
locale (界面语言)                          language (搜索语言)
  │                                            │
  ├─ LOCALE_NAMES 校验                         ├─ sxng_locales / 引擎 traits 校验
  ├─ babel Locale.parse()                      ├─ SearchLanguageSetting 正则匹配
  ├─ gettext() 查 .mo 文件翻译                 ├─ parse_lang() → query_lang
  ├─ Jinja2 模板 {{ _('...') }} 渲染           ├─ 各 engine.fetch_traits() 映射为引擎私有格式
  ├─ 影响 <html lang>、RTL 布局                └─ 影响搜索请求 HTTP 参数 (hl=、lang= 等)
  └─ 写入 Cookie 'preferences'                 └─ 每次搜索可通过 ":lang-xx" Bang 临时覆盖
```

在 [webadapter.py:parse_lang()](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/webadapter.py#L55-L72) 中可以看到搜索语言甚至支持通过查询语法临时覆盖（`:lang-de` Bang），而界面语言必须通过偏好设置页修改并写入 Cookie。

### 3.2 默认语言兜底全链路：请求处理 → 语言匹配 → Babel 选择器

当用户从未设置过偏好（Cookie 为空），且浏览器 `Accept-Language` 请求头中的语言又全部不被支持时，SearXNG 通过三个阶段串联的多级兜底，最终落到英语 `'en'`。整个过程是嵌套保护结构：上一层失败才触发下一层，每层都有独立保险。

```
浏览器 Accept-Language: "xyz-AB, qwe-CD;q=0.9"
(两种语言 babel 不识别，SearXNG 也没有翻译)
  │
  ▼
━━━━━━━━━━━━━━━━ 阶段 A：pre_request 请求处理（webapp.py）━━━━━━━━━━━━━━━━
  A1. ClientPref.from_http_request()
      ├─ 解析 Accept-Language 头
      ├─ babel.Locale.parse('xyz-AB') → UnknownLocaleError ✗
      ├─ babel.Locale.parse('qwe-CD') → UnknownLocaleError ✗
      └─ try/except 兜底: locale = babel.Locale.default → 'en' ← 保险 ①
  A2. Preferences 初始化
      └─ 从 settings_defaults.py 取默认值: locale = '' (空字符串)
  A3. 从 Cookie 加载 preferences.parse_dict(cookies)
      └─ 首访无 Cookie → locale 仍为 ''
  A4. 从 GET/POST 合并 → 无 locale 参数 → locale 仍为 ''
  A5. preferences.parse_dict(form) → 无 locale 值
  A6. 判断: if not preferences.get_value("locale") → True（空字符串）
      └─ 触发 _get_browser_language(req, LOCALE_NAMES.keys()) ← 进入阶段 B
  │
  ▼
━━━━━━━━━━━━━━━━ 阶段 B：match_locale 语言匹配（locales.py）━━━━━━━━━━━━━━━━
  B1. ClientPref.from_http_request(req)
      └─ 再次解析，但结果已在保险 ① 中设为 'en'
  B2. match_locale(searxng_locale='en', locale_tag_list, fallback='en')
      ├─ if not searxng_locale → False（'en' 非空）
      ├─ locale = get_locale('en') → babel.Locale('en') ✓
      ├─ build_engine_locales(LOCALE_NAMES.keys)
      │     包含 'en' → 'en', 'en-US' → 'en-US', ...
      ├─ get_engine_locale('en', engine_locales, default='en')
      │     ├─ engine_locales.get('en') → 'en' 直接命中 ✓
      │     └─ return 'en' ← 匹配成功
      └─ 返回 'en'
  B3. 若阶段 B 全部失败（get_locale 返回 None）
      └─ match_locale 参数 fallback='en' 直接返回 ← 保险 ②
  A7. preferences.parse_dict({"locale": "en"})
      └─ 将匹配结果写入 Preferences，供后续使用
  │
  ▼
━━━━━━━━━━━━━━━━ 阶段 C：localeselector Babel 选择器（locales.py）━━━━━━━━━━━━━━━━
  C1. locale 变量声明：locale: str = 'en' ← 保险 ③（初始值就是 'en'）
  C2. has_request_context() → True
  C3. value = preferences.get_value('locale') → 'en'（阶段 B 的结果）
  C4. if value: locale = value → locale = 'en' ✓
  C5. if locale in ADDITIONAL_TRANSLATIONS → False（'en' 是 babel 原生语言）
  C6. LOCALE_BEST_MATCH.get('en', 'en') → 'en'
  C7. if locale == '' → False，跳过
      但如果 preferences 加载异常导致值为 ''，会再设为 'en' ← 保险 ④
  C8. locale.replace('-', '_') → 'en'
  C9. 返回 'en' 给 Flask-Babel
  │
  ▼
翻译结果：加载 translations/en/LC_MESSAGES/messages.mo，页面显示英语
若 en 的 .mo 文件也不存在，gettext 返回 msgid 原文（即英文源码文案）
```

#### 阶段 A 代码追踪：pre_request 请求处理

正常优先级链路（`FORM → Cookie → URL → Accept-Language → settings → 'en'`）的所有前置合并都在 [pre_request()](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/webapp.py#L457-L518) 中完成，它是 Flask 的 `before_request` 钩子，每个请求必走：

```python
@app.before_request
def pre_request():
    # ── A1：从 Accept-Language 解析浏览器偏好 ──
    # 保险 ①：即使请求头全不支持，from_http_request 也会兜底 'en'
    client_pref = ClientPref.from_http_request(sxng_request)

    # ── A2：构造 Preferences ──
    # settings_defaults.py 中 ui.default_locale 默认是空字符串 ''
    preferences = Preferences(themes, categories, engines, plugins, client_pref)
    sxng_request.preferences = preferences

    # ── A3-A5：从 Cookie → Form → GET 参数 逐层合并 ──
    preferences.parse_dict(sxng_request.cookies)          # A3
    sxng_request.form = dict(sxng_request.form.items())   # A4
    for k, v in sxng_request.args.items():
        if k not in sxng_request.form:
            sxng_request.form[k] = v
    if sxng_request.form.get('preferences'):              # A5
        preferences.parse_encoded_data(sxng_request.form['preferences'])
    else:
        preferences.parse_dict(sxng_request.form)

    # ── 搜索语言同名兜底逻辑 ──
    if not preferences.get_value("language"):
        language = _get_browser_language(sxng_request, settings['search']['languages'])
        preferences.parse_dict({"language": language})

    # ── A6：locale 仍为空 → 进入阶段 B ──
    if not preferences.get_value("locale"):
        locale = _get_browser_language(sxng_request, LOCALE_NAMES.keys())
        preferences.parse_dict({"locale": locale})
```

A1 中的 [ClientPref.from_http_request()](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/preferences.py#L359-L387) 有最外层异常捕获：

```python
@classmethod
def from_http_request(cls, http_request: SXNG_Request):
    try:
        al_header = http_request.headers.get("Accept-Language")
        for l in al_header.split(','):
            lang, qvalue = ...  # 按 ";" 分离 q 值
            locale = babel.Locale.parse(lang, sep='-')  # 解析每个语言 tag
            pairs.append((locale, qvalue))
        pairs.sort(reverse=True, key=lambda x: x[1])
        locale = pairs[0][0]          # 取 q 值最高的那个 Locale 对象
    except Exception:
        locale = babel.Locale.default  # ← 保险 ①：babel 全局默认 = 'en'
    return cls(locale=locale)
```

#### 阶段 B 代码追踪：match_locale 语言匹配

从 `_get_browser_language()` 进入 [locales.py:match_locale()](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/locales.py#L372-L418)：

```python
def _get_browser_language(req, lang_list):
    # 再次解析，但阶段 A1 的结果已缓存了兜底逻辑
    client = ClientPref.from_http_request(req)
    # 保险 ②：fallback='en' 作为 match_locale 的最后参数
    locale = match_locale(client.locale_tag, lang_list, fallback='en')
    return locale
```

`match_locale` 内部两级前置判断，任一命中就直接返回 `fallback`：

```python
def match_locale(searxng_locale, locale_tag_list, fallback=None):
    # 判断 1：如果传入空字符串 → 直接返回 fallback（不往下走任何匹配逻辑）
    if not searxng_locale:
        return fallback

    # 判断 2：如果 babel 不认识这个 locale tag → 直接返回 fallback
    locale = get_locale(searxng_locale)
    if locale is None:
        return fallback

    # （正常匹配：构造 engine_locales 字典 → 调用 get_engine_locale）
    ...
    # get_engine_locale 内部所有匹配规则都不命中时 → 再返回 default=fallback
    return get_engine_locale(searxng_locale, engine_locales, default=fallback)
```

#### 阶段 C 代码追踪：localeselector Babel 选择器

阶段 A+B 的结果已写入 `sxng_request.preferences`。当 Flask-Babel 在首次调用 `gettext()` 前需要确定 locale 时，触发注册的 `locale_selector` 回调：

注册位置：[webapp.py:L154-L160](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/webapp.py#L154-L160)

```python
def get_locale():
    locale = localeselector()
    return locale

babel = Babel(app, locale_selector=get_locale)
```

核心处理函数 [localeselector()](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/locales.py#L86-L107)：

```python
def localeselector():
    # 保险 ③：变量初始值就是 'en'，即使整个函数体异常中断也不会崩溃
    locale: str = 'en'

    if has_request_context():
        value: str = sxng_request.preferences.get_value('locale')
        # 只有 value 非空字符串时才覆盖初始值 'en'
        if value:
            locale = value

    # ── 小语种双轨制标记（不影响 'en'）──
    if locale in ADDITIONAL_TRANSLATIONS:
        sxng_request.form['use-translation'] = locale

    # ── LOCALE_BEST_MATCH 近似语言回退（'en' 不在表中，直接取原值）──
    locale = LOCALE_BEST_MATCH.get(locale, locale)

    # 保险 ④：如果 preferences 加载异常导致值仍为空字符串，再次硬兜底
    if locale == '':
        locale = 'en'

    # 连字符转下划线（babel 内部格式）：'en' → 'en'，'zh-CN' → 'zh_CN'
    locale = locale.replace('-', '_')
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

#### 为什么选英语做最终兜底？

因为所有翻译的 `messages.pot` 模板文件是以英语 msgid 为锚点生成的。举个例子，模板中的 `msgid "About"`，如果目标语言的 `.mo` 文件里没有对应翻译条目，gettext 会直接返回 msgid 原文（即英语的 `"About"`），不会出现空字符串或翻译键泄漏到页面。这是 GNU gettext 的标准设计——英语天然是所有语言的零配置兜底。

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

### 4.2 前端 JS 翻译字典的注入时机与全链路

前端 TypeScript 代码运行在浏览器沙箱中，无法直接调用 Python 侧的 `gettext()`。SearXNG 的方案是**在服务器端渲染时，把前端需要的少量翻译词条打包成 JSON，通过 HTML 的自定义属性注入页面**。整个流程分三个精确时间点：

```
时间点 1: 路由处理函数内部，调用 render() 之前
时间点 2: render() 函数内部，get_client_settings() 执行时
时间点 3: 浏览器加载 HTML，解析到 <script client_settings="..."> 时
```

#### 时间点 1：Babel 已激活，翻译环境就绪

路由函数（如 `index()`、`search()`、`preferences()`）执行时，Flask 的 `before_request` 钩子 `pre_request()` 已经完成，`Preferences` 已从 Cookie/Accept-Language 中加载完毕。紧接着 Flask-Babel 的 `locale_selector` 回调也已经触发并返回了 locale tag。此时：

- `flask_babel.gettext()` 内部调用 `get_translations()` 已能正确加载对应语言的 `.mo` 文件
- 在此之后任何位置调用 `gettext('...')` 都会返回正确语言的翻译

#### 时间点 2：render() 内打包翻译字典

[webapp.py:render()](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/webapp.py#L387-L406) 执行第一行就调用 `get_client_settings()`，此时翻译环境已就绪，所以每个 `gettext()` 调用都能拿到正确译文：

```python
def render(template_name: str, **kwargs):
    # ┌─ 在 render_template 之前调用，确保翻译环境已激活 ─┐
    client_settings = get_client_settings()
    #                                                    │
    # 打包为 base64，避免 JSON 中的引号破坏 HTML 属性     │
    kwargs['client_settings'] = base64.b64encode(        │
        json.dumps(client_settings).encode()             │
    ).decode()                                           │
    #                                                    │
    kwargs.update(client_settings)  # 同时展开字典，使模板也能访问
    # └─────────────────────────────────────────────────┘
    ...
```

`get_client_settings()` → `get_translations()` 收集前端 TS 代码确实需要的 3 条翻译：

```python
def get_translations():
    return {
        'no_item_found': gettext('No item found'),
        #  被 autocomplete.ts 用在下拉框无搜索建议时显示
        'Source': gettext('Source'),
        #  被 preferences.ts 拼在引擎描述后面："Source: Wikipedia"
        'error_loading_next_page': gettext('Error loading the next page'),
        #  被 InfiniteScroll.ts 用在无限滚动加载失败时
    }
```

**为什么只有 3 条**？绝大多数界面文案都在 Jinja 模板中用 `{{ _('...') }}` 渲染，只有用户交互过程中动态生成的 DOM 节点（自动补全下拉框、无限滚动错误提示、偏好设置里动态插入的引擎来源）需要在浏览器侧调用翻译。打包过多词条会增大每个页面的 HTML 体积。

#### 时间点 3：浏览器解析 HTML，前端 TS 提取翻译字典

[base.html](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/templates/simple/base.html#L13) 中 `<script>` 标签携带注入的属性：

```html
<script type="module" src="{{ url_for('static', filename='sxng-core.min.js') }}"
        client_settings="{{ client_settings }}"></script>
```

前端 [toolkit.ts](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/client/simple/src/js/toolkit.ts#L61-L71) 在模块加载时立即解析：

```typescript
// toolkit.ts 模块顶层作用域，页面加载时立即执行一次
const getSettings = (): Settings => {
  const attr = document.querySelector("script[client_settings]")
    ?.getAttribute("client_settings");
  if (!attr) return {};
  try {
    return JSON.parse(atob(attr));  // base64 解码 → JSON 解析
  } catch (error) {
    console.error("Failed to load client_settings:", error);
    return {};
  }
};

// 模块导出的单例，整个前端代码共享这一份 settings
export const settings: Settings = getSettings();
```

各 TS 模块通过 `settings.translations?.xxx` 访问翻译，同时附带空值兜底（防止 `client_settings` 解析失败时出现 undefined）：

```typescript
// autocomplete.ts
textContent: settings.translations?.no_item_found ?? "No results found"
// InfiniteScroll.ts
textContent: settings.translations?.error_loading_next_page ?? "Error loading next page"
// preferences.ts
` (<i>${settings.translations?.Source}:&nbsp;${source}</i>)`
```

**为什么用 base64 编码而不是直接内联 `<script>` 标签**：这是 SearXNG 的 CSP（内容安全策略）设计——内联 `<script>` 需要 nonce 或 hash，而自定义属性携带 JSON + base64 编码能天然避开 CSP 对脚本注入的限制，同时避免 JSON 中的引号、换行符破坏 HTML 属性结构。

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
