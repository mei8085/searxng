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

关键理解：**未知语言不是直接应用默认值，而是先逐个跳过、再进入匹配兜底**。`ClientPref.from_http_request()` 遇到 `UnknownLocaleError` 时执行 `continue`（跳过该 tag），只有所有 tag 都跳过后 `pairs` 才为空，`locale` 才变成 `None`，随后由 `match_locale()` 的 `fallback='en'` 接住。

```
浏览器 Accept-Language: "xyz-AB, qwe-CD;q=0.9"
(两种语言 babel 不识别，SearXNG 也没有翻译)
  │
  ▼
━━━━━━━━━━━━━━━━ 阶段 A：pre_request 请求处理（webapp.py）━━━━━━━━━━━━━━━━
  A1. ClientPref.from_http_request()
      ├─ 解析 Accept-Language 头
      ├─ babel.Locale.parse('xyz-AB') → UnknownLocaleError → continue（跳过）
      ├─ babel.Locale.parse('qwe-CD') → UnknownLocaleError → continue（跳过）
      ├─ pairs 为空 → locale = None
      └─ 返回 ClientPref(locale=None)   ← 不是直接设 'en'！
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
      └─ 再次解析，结果同 A1：locale = None
  B2. client.locale_tag
      └─ locale 为 None → 返回 None
  B3. match_locale(searxng_locale=None, locale_tag_list, fallback='en')
      ├─ if not searxng_locale → True（None 是 falsy）
      └─ 直接返回 fallback='en' ← 兜底触发点
  A7. preferences.parse_dict({"locale": "en"})
      └─ 将匹配结果写入 Preferences，供后续使用
  │
  ▼
━━━━━━━━━━━━━━━━ 阶段 C：localeselector Babel 选择器（locales.py）━━━━━━━━━━━━━━━━
  注意：此阶段不在 pre_request 中主动调用，而是在首次 gettext() 时由
  Flask-Babel 的 get_locale() 按需触发（lazy evaluation）
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
    # 注意：这里不会直接兜底到 'en'，未知语言会被跳过
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

A1 中的 [ClientPref.from_http_request()](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/preferences.py#L359-L387) **逐个尝试解析，跳过不认识的 tag**，而不是整体兜底：

```python
@classmethod
def from_http_request(cls, http_request: SXNG_Request):
    al_header = http_request.headers.get("Accept-Language")
    if not al_header:
        return cls(locale=None)       # 无请求头 → locale=None

    pairs: list[tuple[babel.Locale, float]] = []
    for l in al_header.split(','):
        lang, qvalue = [_.strip() for _ in (l.split(';') + ['q=1',])[:2]]
        try:
            qvalue = float(qvalue.split('=')[-1])
            locale = babel.Locale.parse(lang, sep='-')   # 尝试解析
        except (ValueError, babel.core.UnknownLocaleError):
            continue    # ← 关键：跳过该 tag，不中断，继续尝试下一个
        pairs.append((locale, qvalue))

    locale = None
    if pairs:
        pairs.sort(reverse=True, key=lambda x: x[1])
        locale = pairs[0][0]          # 取 q 值最高的那个 Locale 对象
    # pairs 为空（所有 tag 都跳过了）→ locale 保持 None
    return cls(locale=locale)
```

这意味着：如果 `Accept-Language: "xyz-AB, en;q=0.5"`，`xyz-AB` 会被跳过，但 `en` 仍能被解析，`pairs` 不为空，最终 `locale = Locale('en')`。**只有所有 tag 都无法解析时**，`locale` 才为 `None`。

[ClientPref.locale_tag](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/preferences.py#L350-L357) 属性将 `locale=None` 转换为 `None` 返回：

```python
@property
def locale_tag(self):
    if self.locale is None:
        return None
    tag = self.locale.language
    if self.locale.territory:
        tag += '-' + self.locale.territory
    return tag
```

#### 阶段 B 代码追踪：match_locale 语言匹配

从 `_get_browser_language()` 进入 [locales.py:match_locale()](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/locales.py#L372-L418)：

```python
def _get_browser_language(req, lang_list):
    # 再次解析（结果同 A1），client_pref.locale_tag 可能为 None
    client = ClientPref.from_http_request(req)
    # 保险 ②：fallback='en' 作为 match_locale 的最后参数
    locale = match_locale(client.locale_tag, lang_list, fallback='en')
    return locale
```

`match_locale` 内部两级前置判断，当 `searxng_locale` 为 `None`（来自阶段 A 的"全部跳过"结果）时直接命中第一级：

```python
def match_locale(searxng_locale, locale_tag_list, fallback=None):
    # 判断 1：searxng_locale 为 None（falsy）→ 直接返回 fallback
    if not searxng_locale:
        return fallback

    # 判断 2：如果 babel 不认识这个 locale tag → 直接返回 fallback
    locale = get_locale(searxng_locale)
    if locale is None:
        return fallback

    # （正常匹配：构造 engine_locales 字典 → 调用 get_engine_locale）
    ...
    return get_engine_locale(searxng_locale, engine_locales, default=fallback)
```

当 `Accept-Language` 中有部分语言能解析（如 `"xyz-AB, zh-CN;q=0.5"`），`ClientPref.from_http_request()` 只跳过 `xyz-AB`，保留 `zh-CN`。此时 `client.locale_tag = 'zh-CN'`，`match_locale('zh-CN', ...)` 进入正常的 `get_engine_locale` 匹配流程，**不会触发兜底**。

#### 阶段 C 代码追踪：localeselector Babel 选择器

阶段 A+B 的结果已写入 `sxng_request.preferences`。Flask-Babel 的 `locale_selector` 回调**不在 pre_request 中主动调用**，而是在首次调用 `gettext()` 时由 Flask-Babel 内部的 `get_locale()` 按需触发（lazy evaluation），且结果缓存到 `ctx.babel_locale`，同一请求内只计算一次。

Flask-Babel `get_locale()` 源码核心逻辑：

```python
def get_locale():
    ctx = _get_current_context()
    locale = getattr(ctx, 'babel_locale', None)
    if locale is None:
        # 首次调用 → 触发 locale_selector 回调
        babel = get_babel()
        if babel.locale_selector is None:
            locale = babel.instance.default_locale
        else:
            rv = babel.locale_selector()     # ← 调用 SearXNG 注册的 localeselector()
            if rv is None:
                locale = babel.instance.default_locale
            else:
                locale = Locale.parse(rv)
        ctx.babel_locale = locale            # ← 缓存结果，后续不再触发
    return locale
```

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

### 4.2 前端翻译字典全链路：服务端生成 → 注入页面 → 浏览器解码使用

前端 TypeScript 代码运行在浏览器沙箱中，无法直接调用 Python 侧的 `gettext()`。SearXNG 的方案是**在服务器端渲染时，把前端需要的少量翻译词条打包成 JSON，通过 HTML 的自定义属性注入页面**。整条链路分为三个严格有序的阶段，每个阶段的时机不能错位：

```
服务端进程：Flask 请求响应周期内
  │
  ├── 阶段 ①：render() 内首次 gettext() 调用时，触发 localeselector 确定语言
  │
  ├── 阶段 ②：render() 第一行，get_client_settings() 生成翻译字典
  │       └─ 打包为 base64 → 塞进模板变量 client_settings
  │
  └── Jinja2 渲染 → base.html 输出：
          <script src="sxng-core.min.js" client_settings="eyJ0cmFuc...">
              ↑                                    ↑
              │                                    │
           阶段 ③：浏览器侧                       base64 编码后的
           toolkit.ts 在此处                      JSON，包含
           读取属性并解码                          translations 字典
```

#### 阶段 ①：服务端生成时机 — 首次 gettext() 调用时确定语言

路由函数（如 `index()`、`search()`、`preferences()`）执行时，Flask 的 `before_request` 钩子 `pre_request()` 已经完成，`Preferences` 已从 Cookie/Accept-Language 中加载完毕。

紧接着 Flask-Babel 会在**第一次调用任何翻译函数时**，通过注册的 `locale_selector` 回调触发 `localeselector()` 并返回 locale tag。这个回调时机非常关键——必须早于 `render()` 内的 `gettext()` 调用，否则翻译字典会拿到英文原文。

SearXNG 的时序保证是天然正确的，因为：
- `pre_request` 把 locale 写入了 `sxng_request.preferences`
- `localeselector()` 从 preferences 中读取值，只要后续调用 `gettext()` 时 babel 才会延迟加载 `.mo` 文件
- 因此在 `render()` 内部第一次调用 `gettext('No item found')` 时，babel 会完成 locale 解析并加载正确的 `.mo` 文件

#### 阶段 ②：塞入页面 — render() 打包，模板输出到 HTML 属性

[webapp.py:render()](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/webapp.py#L387-L406) 执行第一行调用 `get_client_settings()`，首次 `gettext()` 在此触发 localeselector 确定语言，后续翻译直接使用缓存：

```python
def render(template_name: str, **kwargs):
    # ┌─ 步骤 1：收集所有客户端设置（含 3 条翻译）──────────────────┐
    client_settings = get_client_settings()                        │
    #                                                                │
    # ┌─ 步骤 2：整体打包为 base64 字符串 ────────────────────────┐ │
    # │ 输入：{"theme": "simple", "translations": {"no_item_found"│ │
    # │        : "无结果", ...}, ...}                             │ │
    # │ 过程：json.dumps → UTF-8 bytes → base64 bytes → str      │ │
    # │ 输出："eyJ0aGVtZSI6InNpbXBsZSIsInRyYW5zbGF0aW9ucyI6...    │ │
    kwargs['client_settings'] = base64.b64encode(                  │ │
        json.dumps(client_settings).encode('utf-8')                │ │
    ).decode('ascii')                                              │ │
    # └────────────────────────────────────────────────────────────┘ │
    #                                                                │
    # ┌─ 步骤 3：同时展开字典，模板内直接访问各个字段 ────────────┐ │
    # │ 例如模板内可写 {{ theme }}、{{ autocomplete }} 等         │ │
    kwargs.update(client_settings)                                 │ │
    # └────────────────────────────────────────────────────────────┘ │
    # └─────────────────────────────────────────────────────────────┘
    ...
    # 步骤 4：Jinja2 渲染 → client_settings 字符串被写入 HTML 属性
    result = render_template('simple/' + template_name, **kwargs)
    return result
```

`get_client_settings()` → `get_translations()` 收集前端 TS 代码确实需要的 3 条翻译：

```python
def get_translations():
    return {
        # 被 autocomplete.ts：搜索建议下拉框显示 "无匹配结果"
        'no_item_found': gettext('No item found'),

        # 被 preferences.ts：引擎描述后拼 "来源: Wikipedia"
        'Source': gettext('Source'),

        # 被 InfiniteScroll.ts：无限滚动加载失败提示
        'error_loading_next_page': gettext('Error loading the next page'),
    }
```

**为什么只有 3 条**？绝大多数界面文案都在 Jinja 模板中用 `{{ _('...') }}` 渲染（在服务端已经翻译完了），只有用户交互过程中动态生成的 DOM 节点才需要浏览器侧的翻译。打包过多词条会增大每个页面的 HTML 体积。

模板 [base.html](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/searx/templates/simple/base.html#L13) 中把 base64 字符串写入自定义属性：

```html
<script type="module"
        src="{{ url_for('static', filename='sxng-core.min.js') }}"
        client_settings="{{ client_settings }}"></script>
```

**为什么用 base64 编码而不是直接内联 `<script>` 标签**：
1. **CSP 规避**：SearXNG 默认启用严格的内容安全策略，内联 `<script>` 需要 nonce 或 hash 值。而自定义 HTML 属性不受此限制。
2. **引号安全**：`gettext()` 返回的翻译可能包含单引号、双引号甚至 HTML 字符实体。直接写 `const s = {{ translations|tojson }}` 会因为引号嵌套破坏页面结构。base64 输出只有 `[A-Za-z0-9+/=]` 字符，天然无转义问题。
3. **体积恒定**：base64 编码后的长度是原数据的 4/3，固定可预测，不影响 HTML 解析器的字符计数。

#### 阶段 ③：浏览器解码使用 — toolkit.ts 模块顶层一次性执行

浏览器按顺序解析 HTML 文档：

1. 解析到 `<script type="module" src="sxng-core.min.js" client_settings="...">`
2. 此时 `client_settings` **属性本身已经在 DOM 树上**，但 JS 模块代码还没有开始执行
3. 浏览器启动 ES 模块加载流程，下载并解析 `sxng-core.min.js`
4. 模块内部的依赖图按拓扑序执行，`toolkit.ts` 是最底层依赖，最先执行

[toolkit.ts](file:///d:/fz/0601-2/solo-dogfeeding/code/7-searxng/client/simple/src/js/toolkit.ts#L61-L71) 在**模块顶层作用域**立即执行 `getSettings()`：

```typescript
// ── 模块加载时立即执行，且只执行一次 ──
const getSettings = (): Settings => {
  // 从 DOM 中取属性值（此时 <script> 标签已解析完毕，一定存在）
  const attr = document.querySelector("script[client_settings]")
    ?.getAttribute("client_settings");

  if (!attr) return {};              // 属性不存在 → 返回空对象兜底

  try {
    return JSON.parse(atob(attr));   // 逆过程：base64 → UTF-8 bytes → JSON 对象
  } catch (error) {
    console.error("Failed to load client_settings:", error);
    return {};                       // 解析失败 → 空对象兜底
  }
};

// 导出单例：所有 TS 模块 import { settings } 时拿到的都是同一个对象引用
export const settings: Settings = getSettings();
```

`settings` 对象的类型定义（toolkit.ts 第 5-22 行）与 Python 端 `get_client_settings()` 返回结构一一对应：

```typescript
// synced with searx/webapp.py get_client_settings  ← 注释注明需要和 Python 端同步
type Settings = {
  plugins?: string[];
  autocomplete?: string;
  ...
  translations?: Record<string, string>;  // ← 翻译字典在这里
  ...
};
```

各业务模块直接使用 `settings.translations`，并附带空值兜底（防止解析失败时页面报错）：

```typescript
// ── autocomplete.ts：搜索输入框建议无结果时 ──
const emptyEl = document.createElement("div");
emptyEl.textContent =
  settings.translations?.no_item_found ?? "No results found";
  // 如果 translations 不存在，用英文原文兜底

// ── InfiniteScroll.ts：无限滚动下一页请求失败 ──
errorEl.textContent =
  settings.translations?.error_loading_next_page ?? "Error loading next page";

// ── preferences.ts：动态插入引擎描述的来源标签 ──
const sourceHtml =
  ` (<i>${settings.translations?.Source}:&nbsp;${source}</i>)`;
```

整条链路的不变式：**只要 Python 端能翻译出某个词条，浏览器端就能拿到对应的译文**。如果中间任何环节失败（base64 损坏、JSON 格式异常、translations 键缺失），代码都会回退到英文原文——这就是 `??` 运算符和 `msgid` 英语原文设计的天然兜底。

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
