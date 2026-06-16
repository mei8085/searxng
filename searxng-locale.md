# SearXNG 翻译加载到模板渲染流程分析

本文档按照代码执行顺序，详细分析 SearXNG 项目中语言资源文件的发现与编译、用户语言偏好与请求语言的匹配规则、模板中翻译函数对回退语言的处理，以及新增语言时需要关注的关键点。

---

## 一、语言资源文件的发现与编译流程

### 1.1 翻译资源目录结构

翻译文件位于 `searx/translations/` 目录，采用标准的 gettext 目录结构：

```
searx/translations/
├── messages.pot              # 翻译模板（POT 文件）
├── en/
│   └── LC_MESSAGES/
│       ├── messages.po       # 可编辑的翻译文本
│       └── messages.mo       # 编译后的二进制翻译文件
├── zh_Hans_CN/
│   └── LC_MESSAGES/
│       ├── messages.po
│       └── messages.mo
├── zh_Hant_TW/
│   └── LC_MESSAGES/
│       ├── messages.po
│       └── messages.mo
├── fr/
├── de/
...
```

每个语言目录使用下划线命名（如 `zh_Hans_CN`），与 babel 内部要求一致。SearXNG 对外暴露时使用连字符（如 `zh-CN`）。

### 1.2 翻译字符串的提取（Extract）

翻译字符串的提取规则由 [babel.cfg](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/babel.cfg) 配置：

```ini
[extractors]
searxng_msg = searx.babel_extract.extract
[python: **.py]                          # 从 Python 源码提取 gettext() / _() 调用
[jinja2: **/templates/**.html]           # 从 Jinja2 模板提取 {{ _('...') }}
[searxng_msg: **/searxng.msg]            # 从 searxng.msg 提取常量字符串
```

#### 1.2.1 Python 源码中的翻译

所有 Python 文件中通过 `gettext()` 或 `_()` 标记的字符串都会被提取，例如在 [webutils.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/webutils.py#L36-L67) 中：

```python
from flask_babel import gettext

timeout_text = gettext('timeout')
parsing_error_text = gettext('parsing error')
exception_classname_to_text = {
    None: gettext('unexpected crash'),
    'timeout': timeout_text,
    ...
}
```

#### 1.2.2 Jinja2 模板中的翻译

模板文件通过 `{{ _('字符串') }}` 调用翻译函数，例如在 [base.html](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/templates/simple/base.html#L47-L72) 中：

```jinja2
<span>{{ _('About') }}</span>
<span>{{ _('Donate') }}</span>
{{ _('Powered by') }}
{{ _('Source code') }}
```

#### 1.2.3 searxng.msg 常量翻译

[searx/searxng.msg](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/searxng.msg) 是一个特殊的 Python 文件，用于集中定义需要翻译的常量名称（如分类名、天气条件等）。自定义提取器在 [babel_extract.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/babel_extract.py#L30-L58) 中实现：

```python
def extract(fileobj, keywords, comment_tags, options):
    namespace = {}
    exec(fileobj.read(), {}, namespace)  # 执行 searxng.msg
    
    for obj_name in namespace['__all__']:
        obj = namespace[obj_name]
        if isinstance(obj, list):
            for msg in obj:
                yield 0, '_', msg, [f"{obj_name}"]
        elif isinstance(obj, dict):
            for k, msg in obj.items():
                yield 0, '_', msg, [f"{obj_name}['{k}']"]
```

提取后的所有字符串汇总生成 `messages.pot` 模板文件。

### 1.3 翻译文件的编译（Compile）

`.po` 文本文件需要编译为 `.mo` 二进制文件供运行时高效加载。这通常通过 Babel 的 `pybabel compile` 命令完成。

### 1.4 语言元数据的生成（locales.json）

翻译目录发现与语言元数据生成由 [update_locales.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searxng_extra/update/update_locales.py) 完成，通过 `./manage data.locales` 触发（见 [lib_sxng_data.sh](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/utils/lib_sxng_data.sh#L64-L72)）。

生成流程按以下顺序遍历，构建 `LOCALE_NAMES` 和 `RTL_LOCALES`：

1. **ADDITIONAL_TRANSLATIONS**（[locales.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/locales.py#L64-L71) 中定义的 babel 不支持的语言）
   - 如 `dv`（Dhivehi）、`oc`（Occitan）、`szl`（Silesian）、`pap`（Papiamento）
   
2. **LOCALE_BEST_MATCH**（[locales.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/locales.py#L73-L83) 中的映射键）
   - 如 `zh-HK` → `zh-Hant-TW`、`nl-BE` → `nl` 等

3. **翻译目录扫描**：通过 `get_translation_locales()`（[locales.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/locales.py#L123-L139)）遍历 `searx/translations/` 目录

```python
def get_translation_locales() -> list[str]:
    for folder in (Path(searx_dir) / 'translations').iterdir():
        if not folder.is_dir():
            continue
        if not (folder / 'LC_MESSAGES').is_dir():
            continue
        tr_locales.append(folder.name)  # 收集如 'zh_Hans_CN', 'en', 'fr' 等
    return sorted(tr_locales)
```

最终生成的 [locales.json](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/data/locales.json) 结构：

```json
{
  "LOCALE_NAMES": {
    "en": "English",
    "zh-CN": "中文, 中国 (Chinese, China)",
    "fa-IR": "فارسی, ایران (Persian, Iran)",
    ...
  },
  "RTL_LOCALES": ["ar", "fa-IR", "he", "dv", ...]
}
```

### 1.5 搜索语言/区域列表（sxng_locales.py）

用于搜索功能的语言/区域列表由 `./manage data.traits` 生成，存储在 [sxng_locales.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/sxng_locales.py) 中。这是一个静态元组，包含语言标签、本地化名称、英文名称和国旗 emoji。

---

## 二、用户语言偏好与请求语言的匹配规则

### 2.1 应用初始化阶段

应用启动时，[webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/webapp.py#L1363-L1409) 的 `init()` 函数被调用：

```python
def init():
    locales_initialize()  # 关键：初始化语言环境
    ...
```

`locales_initialize()`（[locales.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/locales.py#L142-L150)）执行两个关键操作：

1. **Monkey patch**：用自定义的 `get_translations()` 替换 `flask_babel.get_translations`
2. **加载元数据**：从 `searx/data/locales.json` 加载 `LOCALE_NAMES` 和 `RTL_LOCALES`

```python
def locales_initialize():
    flask_babel.get_translations = get_translations  # 替换默认的翻译加载
    LOCALE_NAMES.update(data.LOCALES["LOCALE_NAMES"])
    RTL_LOCALES.update(data.LOCALES["RTL_LOCALES"])
```

同时，Flask-Babel 被初始化并绑定 locale 选择器（[webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/webapp.py#L155-L161)）：

```python
def get_locale():
    locale = localeselector()
    return locale

babel = Babel(app, locale_selector=get_locale)
```

### 2.2 请求处理阶段：语言匹配优先级

每个请求到达时，`@app.before_request` 装饰的 `pre_request()`（[webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/webapp.py#L459-L519)）按以下优先级确定用户语言：

#### 第 1 步：初始化 Preferences 对象

```python
client_pref = ClientPref.from_http_request(sxng_request)
preferences = Preferences(themes, ..., engines, ..., client_pref)
```

#### 第 2 步：解析 Cookie 中的用户偏好

```python
preferences.parse_dict(sxng_request.cookies)
```

从 Cookie 中读取 `locale` 字段（UI 语言）和 `language` 字段（搜索语言）。

#### 第 3 步：解析表单/URL 参数中的偏好

```python
if sxng_request.form.get('preferences'):
    preferences.parse_encoded_data(...)  # 解析 base64 编码的偏好
else:
    preferences.parse_dict(sxng_request.form)  # 解析普通表单字段
```

#### 第 4 步：回退到浏览器 Accept-Language 头

如果用户既没有 Cookie 也没有表单参数，则使用浏览器语言：

```python
# 搜索语言
if not preferences.get_value("language"):
    language = _get_browser_language(sxng_request, settings['search']['languages'])
    preferences.parse_dict({"language": language})

# UI locale
if not preferences.get_value("locale"):
    locale = _get_browser_language(sxng_request, LOCALE_NAMES.keys())
    preferences.parse_dict({"locale": locale})
```

`_get_browser_language()`（[webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/webapp.py#L164-L167)）的工作原理：

```python
def _get_browser_language(req, lang_list):
    client = ClientPref.from_http_request(req)
    locale = match_locale(client.locale_tag, lang_list, fallback='en')
    return locale
```

`ClientPref.from_http_request()`（[preferences.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/preferences.py#L358-L386)）解析 `Accept-Language` 头，按质量因子 `q` 排序，选择最高优先级的语言。

### 2.3 localeselector()：locale 标准化处理

`localeselector()`（[locales.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/locales.py#L86-L107)）是 Flask-Babel 实际使用的 locale 选择器，执行以下关键转换：

```python
def localeselector():
    locale: str = 'en'  # 最终兜底
    if has_request_context():
        value = sxng_request.preferences.get_value('locale')
        if value:
            locale = value

    # 1. 标记 babel 不原生支持的语言，设置 use-translation 标记
    if locale in ADDITIONAL_TRANSLATIONS:
        sxng_request.form['use-translation'] = locale

    # 2. LOCALE_BEST_MATCH 映射：将不完整/别名映射到存在的翻译
    locale = LOCALE_BEST_MATCH.get(locale, locale)
    # 例如：zh-HK → zh-Hant-TW, nl-BE → nl, dv → si, oc → fr-FR

    if locale == '':
        locale = 'en'  # 空值兜底

    # 3. 格式转换：连字符 → 下划线（babel 内部要求）
    locale = locale.replace('-', '_')
    # 例如：zh-CN → zh_CN, pt-BR → pt_BR
    return locale
```

### 2.4 match_locale()：智能语言匹配

`match_locale()`（[locales.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/locales.py#L372-L418)）实现了复杂的语言匹配算法，当精确匹配失败时进行多级近似匹配：

**匹配规则（按优先级）：**

1. **精确匹配**：直接查找 `searxng_locale` 是否在 `locale_tag_list` 中
2. **语言+书写系统匹配**：如 `zh-Hans` → `zh-CN`, `zh-Hant` → `zh-TW`
3. **按地域优先匹配（First approximation）**：用户选择了带地域的 locale（如 `fr-BE`），优先匹配该地域的任何官方语言
   - 例如：`fr-BE` 在只有 `nl-BE` 可选时，匹配到 `nl-BE`
4. **按语言+人口比例匹配（Second approximation）**：只选了语言没选地域时，找该语言为官方语言、且人口比例最高的地域
   - 例如：`fr` → `fr-FR`，`es` → `es-ES`
   - 特殊：`en` → `en-US`

---

## 三、模板中翻译函数对回退语言的处理

### 3.1 翻译加载机制

#### 3.1.1 标准路径：flask_babel 默认行为

对于 babel 支持的语言，Flask-Babel 会根据 `locale_selector` 返回的 locale（如 `zh_CN`），在 `translations/zh_CN/LC_MESSAGES/messages.mo` 中加载翻译。

#### 3.1.2 特殊路径：ADDITIONAL_TRANSLATIONS 的 Monkey Patch

对于 babel 不原生支持的语言（如 `dv` Dhivehi、`oc` Occitan），通过 monkey patch 的 `get_translations()`（[locales.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/locales.py#L110-L117)）处理：

```python
def get_translations():
    if has_request_context():
        use_translation = sxng_request.form.get('use-translation')
        if use_translation in ADDITIONAL_TRANSLATIONS:
            # 直接从翻译目录加载，绕过 babel 的 locale 验证
            babel_ext = flask_babel.current_app.extensions['babel']
            return Translations.load(babel_ext.translation_directories[0], use_translation)
    return _flask_babel_get_translations()  # 回退到标准 flask_babel 行为
```

**关键点**：`use-translation` 标记是在 `localeselector()` 中设置到 `sxng_request.form` 的，这是一个"暗号"传递机制。

### 3.2 翻译查找与回退链

当模板中调用 `{{ _('Search for...') }}` 时，实际调用路径：

```
模板 {{ _('text') }}
  → flask_babel.gettext('text')
    → flask_babel.get_translations()  （被 monkey patch 为我们的版本）
      → babel.support.Translations.gettext('text')
        → 查找当前语言的 .mo 文件中的 msgid
          → 找到：返回 msgstr
          → 找不到：触发回退机制
```

#### 3.2.1 Babel 的层级回退

Babel 的 `Translations` 类支持 fallback 链。当某个 locale 的翻译不存在时，会依次尝试更通用的 locale：

```
zh_Hans_CN → zh_Hans → zh → (fallback 到 en/原文)
pt_BR → pt → (fallback)
fr_CA → fr → (fallback)
```

#### 3.2.2 最终回退：返回 msgid（英文原文）

如果所有层级都找不到翻译，babel 会原样返回 `msgid`，也就是代码中写的原始字符串（通常是英文）。

这就是为什么 [messages.po](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/translations/en/LC_MESSAGES/messages.po#L21-L79) 的英文翻译 `msgstr` 是空的——因为 `msgid` 本身就是英文，空 `msgstr` 意味着直接使用 `msgid`。

### 3.3 模板渲染时的翻译注入

[render()](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/webapp.py#L389-L456) 函数统一处理所有模板渲染，注入 i18n 相关的上下文变量：

```python
def render(template_name: str, **kwargs):
    # i18n 上下文变量
    kwargs['sxng_locales'] = [l for l in sxng_locales if l[0] in settings['search']['languages']]
    locale = sxng_request.preferences.get_value('locale')
    kwargs['locale_rfc5646'] = _get_locale_rfc5646(locale)  # 用于 <html lang="...">
    if locale in RTL_LOCALES:
        kwargs['rtl'] = True  # 标记 RTL 语言，渲染时切换方向样式
    
    # 客户端 JS 需要的翻译
    client_settings['translations'] = get_translations()
    # get_translations() 返回：
    # {
    #   'no_item_found': gettext('No item found'),
    #   'Source': gettext('Source'),
    #   'error_loading_next_page': gettext('Error loading the next page'),
    # }
```

### 3.4 引擎描述的翻译回退

`/engine_descriptions.json` 端点（[webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/webapp.py#L1074-L1097)）展示了另一种翻译回退模式：

```python
def engine_descriptions():
    sxng_ui_lang_tag = get_locale().replace("_", "-")
    sxng_ui_lang_tag = LOCALE_BEST_MATCH.get(sxng_ui_lang_tag, sxng_ui_lang_tag)

    result = ENGINE_DESCRIPTIONS['en'].copy()  # 先加载英文（作为兜底）
    if sxng_ui_lang_tag != 'en':
        # 用目标语言覆盖已有条目（只覆盖有翻译的）
        for engine, description in ENGINE_DESCRIPTIONS.get(sxng_ui_lang_tag, {}).items():
            result[engine] = description
    
    # 处理引用型描述（如 description = 'google:en' 表示引用英文的 google 描述）
    for engine, description in result.items():
        if len(description) == 2 and description[1] == 'ref':
            ref_engine, ref_lang = description[0].split(':')
            description = ENGINE_DESCRIPTIONS[ref_lang][ref_engine]
```

**回退策略**：英文打底 → 当前语言覆盖 → 引用解析 → settings.yml 自定义覆盖。

---

## 四、新增语言时需要关注的点

### 4.1 翻译资源准备清单

| 步骤 | 操作 | 文件/命令 |
|------|------|-----------|
| 1 | 创建翻译目录（使用下划线命名） | `searx/translations/<lang_code>/LC_MESSAGES/` |
| 2 | 复制 messages.pot 为 messages.po | `cp translations/messages.pot translations/<lang>/LC_MESSAGES/messages.po` |
| 3 | 填写 .po 文件中的 msgstr | 翻译所有 msgid |
| 4 | 编译为 .mo 文件 | `pybabel compile -d searx/translations -l <lang>` |
| 5 | 运行 locales 数据更新 | `./manage data.locales` |
| 6 | 更新搜索语言/区域（可选） | `./manage data.traits`（修改 engine traits 时） |

### 4.2 特殊情况处理

#### 4.2.1 语言不被 python-babel 原生支持

如果目标语言（如 Dhivehi `dv`、Occitan `oc`）在 babel 中不存在，需要在 [locales.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/locales.py) 中添加两处配置：

**ADDITIONAL_TRANSLATIONS**（第 64-71 行）：注册语言及其显示名称
```python
ADDITIONAL_TRANSLATIONS = {
    "dv": "ދިވެހި (Dhivehi)",       # 新语言：显示名
    "oc": "Occitan",
    "szl": "Ślōnski (Silesian)",
    "pap": "Papiamento",
    # 新增你的语言
}
```

**LOCALE_BEST_MATCH**（第 73-83 行）：设置用于 babel 功能（如日期格式化、RTL 判断）的替代语言
```python
LOCALE_BEST_MATCH = {
    "dv": "si",           # Dhivehi 使用僧伽罗语的 babel 数据作为近似
    "oc": 'fr-FR',        # Occitan 使用法语
    "szl": "pl",          # Silesian 使用波兰语
    "pap": "pt-BR",       # Papiamento 使用巴西葡萄牙语
    "zh-HK": "zh-Hant-TW",  # 香港繁体使用台湾繁体翻译
    # 新增映射
}
```

#### 4.2.2 语言区域变体映射

如果要支持某种区域变体但只维护一份翻译，使用 `LOCALE_BEST_MATCH` 指向已有翻译：

```python
LOCALE_BEST_MATCH = {
    "zh-HK": "zh-Hant-TW",   # 香港用户看到繁体中文（使用台湾翻译）
    "nl-BE": "nl",           # 比利时荷兰语使用标准荷兰语翻译
}
```

#### 4.2.3 RTL（从右到左）语言

RTL 检测是通过 babel 的 `locale.text_direction == 'rtl'` 自动判断的，配置在 `update_locales.py` 中完成。但如果是 `ADDITIONAL_TRANSLATIONS` 中的语言，RTL 判断使用 `LOCALE_BEST_MATCH` 中映射的语言的方向。

例如：`dv`（Dhivehi）是 RTL 语言，`LOCALE_BEST_MATCH["dv"] = "si"`（僧伽罗语），但僧伽罗语是 LTR...如果方向判断错误，可能需要手动在代码中处理，或选择合适的 babel 映射语言。

### 4.3 配置 settings.yml

新增的语言需要在实例的 `settings.yml` 中启用才能出现在 UI 中：

```yaml
search:
  languages:  # 搜索语言列表
    - all
    - auto
    - en
    - zh
    - zh-CN
    - zh-TW
    # ... 新增你的语言代码（使用连字符格式，与 sxng_locales.py 中一致）

ui:
  default_locale: 'en'  # 默认 UI 语言可改为新增语言
```

UI locale 选项由 `LOCALE_NAMES.keys()` 决定（来自 locales.json），只要翻译目录存在并运行了 `data.locales`，就会自动出现在偏好设置的下拉列表中。

### 4.4 搜索语言 vs UI locale 的区别

SearXNG 中有两套独立的语言设置，容易混淆：

| 设置项 | 存储字段 | 用途 | 来源列表 |
|--------|----------|------|----------|
| 搜索语言 | `language` | 搜索引擎查询时使用的语言/区域过滤 | `sxng_locales.py`（`./manage data.traits` 生成） |
| UI 界面语言 | `locale` | 模板翻译、日期格式化、文字方向 | `locales.json`（`./manage data.locales` 生成） |

用户在偏好设置页面看到的两个下拉框分别对应这两套设置。新增语言时，如果该语言也需要作为搜索过滤选项，需要确保它出现在 `sxng_locales.py` 中（通常需要引擎支持该语言的 `fetch_traits` 数据）。

### 4.5 语言代码命名规范

| 场景 | 命名格式 | 示例 |
|------|----------|------|
| 翻译目录名 | 下划线，babel 格式 | `zh_Hans_CN`, `pt_BR`, `en` |
| LOCALE_NAMES 键 | 连字符，SearXNG 格式 | `zh-CN`, `pt-BR`, `en` |
| LOCALE_BEST_MATCH 键/值 | 连字符，SearXNG 格式 | `zh-HK` → `zh-Hant-TW` |
| sxng_locales 标签 | 连字符，BCP 47 | `zh-CN`, `zh-TW`, `zh-HK` |
| localeselector() 返回 | 下划线，传递给 babel | `zh_Hans_CN`, `pt_BR` |
| Flask 请求中 locale | 连字符，SearXNG 格式 | `zh-CN`（从 preferences 读出时） |

**记忆口诀**：目录/内部给 babel 用下划线，对外/配置/URL 用连字符。

### 4.6 完整新增语言流程示例（以 Esperanto `eo` 为例）

1. **确认 babel 支持**：Esperanto（`eo`）在 babel 中存在，无需添加到 `ADDITIONAL_TRANSLATIONS`

2. **创建翻译目录和文件**：
   ```
   searx/translations/eo/LC_MESSAGES/
   ├── messages.po   (从 messages.pot 复制并翻译)
   └── messages.mo   (编译后生成)
   ```

3. **编译翻译**：
   ```bash
   pybabel compile -d searx/translations -l eo
   ```

4. **更新 locales 数据**：
   ```bash
   ./manage data.locales
   ```
   这会自动将 `eo` 添加到 `locales.json` 的 `LOCALE_NAMES` 中，显示名为 `Esperanto`。

5. **（可选）如果有搜索引擎支持 Esperanto，更新 traits**：
   ```bash
   ./manage data.traits
   ```

6. **在 settings.yml 中启用**：
   ```yaml
   search:
     languages:
       - ...（原有）
       - eo
   ui:
     default_locale: ''  # 或设为 'eo'
   ```

7. **验证**：重启服务，进入 `/preferences`，在 UI 语言下拉中选择 Esperanto，界面应切换语言。

---

## 附录：核心文件索引

| 文件 | 作用 |
|------|------|
| [searx/locales.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/locales.py) | locale 选择、匹配算法、翻译 monkey patch、常量配置 |
| [searx/webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/webapp.py) | 应用初始化、请求处理、Flask-Babel 绑定、模板渲染 |
| [searx/preferences.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/preferences.py) | Preferences 类、ClientPref（Accept-Language 解析） |
| [searx/data/__init__.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/data/__init__.py) | locales.json 懒加载 |
| [searx/sxng_locales.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/sxng_locales.py) | 搜索语言/区域元组（自动生成，请勿手动修改） |
| [searxng_extra/update/update_locales.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searxng_extra/update/update_locales.py) | 生成 locales.json 的脚本 |
| [searx/babel_extract.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/babel_extract.py) | searxng.msg 自定义提取器 |
| [searx/searxng.msg](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/searxng.msg) | 分类名等常量的翻译定义 |
| [utils/lib_sxng_data.sh](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/utils/lib_sxng_data.sh) | ./manage data.* 命令的 shell 实现 |
| [babel.cfg](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/babel.cfg) | Babel 提取规则配置 |
| [searx/data/locales.json](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/data/locales.json) | LOCALE_NAMES 和 RTL_LOCALES 数据（自动生成） |
| [searx/translations/](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/searx/translations) | 翻译资源根目录 |
| [tests/unit/test_locales.py](file:///d:/fz/0601-1/solo-dogfeeding/code/100-searxng/tests/unit/test_locales.py) | 语言匹配算法测试用例 |
