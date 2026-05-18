# SearXNG 引擎快捷前缀解析路径分析报告

## 1. 概述

引擎快捷前缀（Engine Shortcut）是 SearXNG 中允许用户通过简单的前缀标记快速指定特定搜索引擎或分类的功能。用户可以在查询文本中使用 `!前缀` 语法来选择特定的搜索引擎或分类，而无需通过界面手动选择。

## 2. 查询入口处理位置

### 2.1 主要处理流程入口

查询处理的主入口位于 `searx/webapp.py:619-656` 的 `search()` 函数中：

```python
@app.route('/search', methods=['GET', 'POST'])
def search():
    # ...
    search_query, raw_text_query, _, _, selected_locale = get_search_query_from_webapp(
        sxng_request.preferences, sxng_request.form
    )
```

### 2.2 查询解析核心流程

1. **webapp.py** 调用 `get_search_query_from_webapp()` 函数
2. **webadapter.py:221-300** 中的 `get_search_query_from_webapp()` 创建 `RawTextQuery` 实例
3. **query.py:250-349** 中的 `RawTextQuery.__init__()` 调用 `_parse_query()` 进行实际解析

### 2.3 自动补全入口

自动补全功能在 `searx/webapp.py:812-832` 中也使用了相同的解析逻辑：

```python
def autocompleter():
    raw_text_query = RawTextQuery(sxng_request.form.get('q', ''), disabled_engines)
```

## 3. 用户输入切分规则

### 3.1 切分机制

在 `searx/query.py:280-309` 的 `_parse_query()` 方法中实现：

```python
raw_query_parts = re.split(r'(\s+)', self.query)
```

使用正则表达式 `(\s+)` 进行分割，保留空白字符作为分割标记。每个非空白的 `query_part` 依次经过解析器链处理。

### 3.2 解析器执行顺序

`RawTextQuery.PARSER_CLASSES` 定义了解析器的执行顺序，每个解析器通过 `check()` 方法判断是否处理当前 `query_part`：

| 解析器 | 前缀 | check() 条件 | 功能 |
|--------|------|-------------|------|
| TimeoutParser | `<` | `raw_value[0] == '<'` | 设置超时时间 |
| LanguageParser | `:` | `raw_value[0] == ':'` | 设置语言 |
| ExternalBangParser | `!!` | `raw_value.startswith('!!') and len(raw_value) > 2` | 外部 Bang 跳转 |
| BangParser | `!` | `raw_value[0] == '!' and not raw_value.startswith('!!')` | 引擎/分类快捷前缀 |
| FeelingLuckyParser | `!!` | `raw_value == '!!'` | 手气不错（重定向到第一个结果） |

### 3.3 BangParser 匹配逻辑

`BangParser` 在 `searx/query.py:178-238` 中实现，核心匹配流程：

1. **识别条件**：`raw_value[0] == '!'` 且不是 `!!` 开头
2. **值规范化**：`value = raw_value[1:].replace('-', ' ').replace('_', ' ').lower()`
3. **三级匹配优先级**：
   - **第一步**：匹配 `engine_shortcuts` 字典中的快捷词（如 `g` → `google`）
   - **第二步**：匹配 `engines` 字典中的引擎名称（如 `google`）
   - **第三步**：匹配 `categories` 字典中的分类名称（如 `images`）

### 3.4 快捷词匹配示例

| 用户输入 | 匹配过程 | 结果 |
|----------|----------|------|
| `!g python` | `g` → 匹配 `engine_shortcuts['g']` → `google` | 使用 google 引擎搜索 "python" |
| `!wp python` | `wp` → 匹配 `engine_shortcuts['wp']` → `wikipedia` | 使用 wikipedia 引擎搜索 "python" |
| `!images cat` | `images` → 匹配 `categories['images']` | 使用 images 分类下所有引擎搜索 "cat" |
| `!unknown test` | 三级匹配都失败 → 返回 `False` | `!unknown` 作为普通文本，搜索 "!unknown test" |

## 4. 与默认配置的协同方式

### 4.1 分类/引擎协同

在 `searx/webadapter.py:269-276` 中实现：

```python
if not is_locked('categories') and raw_text_query.specific:
    query_engineref_list = raw_text_query.enginerefs
else:
    query_engineref_list = parse_generic(preferences, form, disabled_engines)
```

**决策逻辑（完整真值表）**：

| is_locked('categories') | raw_text_query.specific | 行为 |
|------------------------|------------------------|------|
| False | True | 使用 `raw_text_query.enginerefs`（即使为空也不回退） |
| False | False | 调用 `parse_generic()`，按表单→偏好→general 回退 |
| True | True | **忽略 enginerefs**，调用 `parse_generic()`（锁定分类优先级最高） |
| True | False | 调用 `parse_generic()` |

> **关键澄清（两层行为区分）**：
> - **引擎选择层**：categories 锁定时，`!` 前缀**不会决定最终的引擎集合**，`enginerefs` 被忽略
> - **文本剥离层**：categories 锁定时，若 `!` 前缀被解析命中（返回 True），**仍会从 `user_query_parts` 剥离**，不会出现在最终搜索文本中
> - 这两层行为是独立的：解析命中的前缀总是被剥离，但只有 categories 未锁定时才会影响引擎选择

### 4.2 语言协同

在 `searx/webadapter.py:55-72` 中实现完整的优先级链：

```python
def parse_lang(preferences, form, raw_text_query):
    if is_locked('language'):
        # 锁定场景：强制使用管理员配置，忽略所有用户输入
        return preferences.get_value('language')
    
    # 未锁定场景：按优先级选择
    if len(raw_text_query.languages):
        query_lang = raw_text_query.languages[-1]  # 优先级1: 查询前缀 :语言
    elif 'language' in form:
        query_lang = form.get('language')           # 优先级2: 表单参数
    else:
        query_lang = preferences.get_value('language')  # 优先级3: 用户偏好
```

**语言优先级完整表**：

| 场景 | 优先级顺序 |
|------|-----------|
| 语言已锁定 | 1. 管理员锁定配置（忽略查询前缀、表单参数、用户偏好） |
| 语言未锁定 | 1. 查询前缀 `:语言` → 2. 表单参数 → 3. 用户偏好 → 4. 系统默认 |

> **与 `!` 前缀相同的两层行为**：
> - **语言选择层**：language 锁定时，`:` 前缀不会决定最终语言
> - **文本剥离层**：language 锁定时，若 `:` 前缀被解析命中，仍会从 `user_query_parts` 剥离

### 4.3 搜索安全级别协同

在 `searx/webadapter.py:75-92` 中实现：

```python
def parse_safesearch(preferences, form):
    if is_locked('safesearch'):
        # 锁定场景：强制使用管理员配置
        return preferences.get_value('safesearch')
    
    if 'safesearch' in form:
        query_safesearch = form.get('safesearch')  # 优先级1: 表单参数
    else:
        query_safesearch = preferences.get_value('safesearch')  # 优先级2: 用户偏好
```

**关键特性**：
- 搜索安全级别（safesearch）**不支持通过查询前缀**设置
- 只能通过表单参数或用户偏好设置
- 管理员锁定时完全忽略用户输入

### 4.4 超时设置协同

在 `searx/webadapter.py:104-114` 中实现：

```python
def parse_timeout(form, raw_text_query):
    timeout_limit = raw_text_query.timeout_limit  # 优先级1: 查询前缀 <超时
    if timeout_limit is None:
        timeout_limit = form.get('timeout_limit')  # 优先级2: 表单参数
```

**超时优先级**：查询前缀 `<超时时间>` > 表单参数 > 系统默认

> 注意：超时设置**没有锁定机制**，用户总是可以通过查询前缀或表单参数修改。

### 4.5 默认分类回退到 general 的触发条件

在 `searx/webadapter.py:135-155` 中定义了完整的分类回退链：

```python
def get_selected_categories(preferences, form):
    selected_categories = []
    
    # 第1层：尝试从表单参数解析（仅当分类未锁定时）
    if not is_locked('categories') and form is not None:
        for name, value in form.items():
            parse_category_form(selected_categories, name, value)
    
    # 第2层：表单解析为空时，使用用户偏好（cookie 中存储的分类选择）
    if not selected_categories:
        cookie_categories = preferences.get_value('categories')
        for ccateg in cookie_categories:
            selected_categories.append(ccateg)
    
    # 第3层：用户偏好也为空时，强制回退到 general
    if not selected_categories:
        selected_categories = ['general']
    
    return selected_categories
```

**回退到 general 的完整触发条件**：

| 条件 | 说明 |
|------|------|
| 1. 进入 parse_generic() 路径 | 即 `is_locked('categories') == True` **或** `raw_text_query.specific == False` |
| 2. 表单中未指定任何分类参数 | 无 `categories` 参数，也无 `category_xxx=on` 参数 |
| 3. 用户偏好中也未设置任何分类 | cookie 为空或未设置 |

> **重要补充**：
> - 当 categories 未锁定且用户使用 `!` 快捷前缀时（`specific == True`），**完全绕过** `get_selected_categories()`，直接使用 `raw_text_query.enginerefs`（即使为空也不回退）
> - 当 categories 被锁定时，无论用户是否使用 `!` 前缀，都会进入 `parse_generic()`，可能回退到 general
> - 无论 categories 是否锁定，只要 `!` 前缀被解析命中，就会从搜索文本中剥离

## 5. 快捷词冲突与回退策略

### 5.1 配置阶段 vs 查询阶段：处理差异对比

| 维度 | 配置阶段（系统启动时） | 查询阶段（用户搜索时） |
|------|----------------------|----------------------|
| **触发时机** | `load_engines()` 加载 `settings.yml` 时 | 用户输入查询，`RawTextQuery` 解析时 |
| **冲突类型** | 多个引擎定义了相同的 `shortcut` 字段 | 用户输入的 `!xxx` 无法匹配任何快捷词/引擎/分类 |
| **处理策略** | 严格失败 | 宽松回退 |
| **行为** | 打印错误日志 + `sys.exit(1)` 终止程序 | 视为普通文本，继续搜索 |
| **代码位置** | `searx/engines/__init__.py:251-260` | `searx/query.py:298-306` |

### 5.2 配置阶段：严格冲突检测

在 `searx/engines/__init__.py:251-260` 的 `register_engine()` 中实现：

```python
def register_engine(engine):
    # 检查引擎名称冲突
    if engine.name in engines:
        logger.error('Engine config error: ambiguous name: {0}'.format(engine.name))
        sys.exit(1)
    engines[engine.name] = engine

    # 检查快捷词冲突
    if engine.shortcut in engine_shortcuts:
        logger.error('Engine config error: ambiguous shortcut: {0}'.format(engine.shortcut))
        sys.exit(1)
    engine_shortcuts[engine.shortcut] = engine.name
```

**设计意图**：配置错误是严重问题，必须在系统启动时暴露，避免运行时出现不可预测的行为。

### 5.3 查询阶段：分类快捷词命中但引擎不可用的行为

**关键结论（仅适用于 categories 未锁定场景）**：分类快捷词命中后，若该分类下引擎因禁用或校验不可用导致 `enginerefs` 为空，**不会回退到默认分类**，而是执行一次空搜索。

**代码证据链（categories 未锁定时）**：

1. **第一步：`BangParser._parse()` 匹配分类成功，但 `enginerefs` 为空**
   ```python
   # query.py:203-212
   if value in categories:
       self.raw_text_query.enginerefs.extend(
           EngineRef(engine.name, value)
           for engine in categories[value]
           if (engine.name, value) not in self.raw_text_query.disabled_engines
       )
       return True  # ⚠️ 即使 enginerefs 为空也返回 True！
   ```

2. **第二步：`specific` 被设置为 True**
   ```python
   # query.py:187-188
   if found and raw_value[0] == '!':
       self.raw_text_query.specific = True  # ⚠️ 即使 enginerefs 为空也设置
   ```

3. **第三步：webadapter 直接使用空的 enginerefs**
   ```python
   # webadapter.py:269-272
   if not is_locked('categories') and raw_text_query.specific:
       query_engineref_list = raw_text_query.enginerefs  # ⚠️ 可能是空列表！
   ```

4. **第四步：validate 后 valid 仍然为空**
   ```python
   # webadapter.py:279-281
   query_engineref_list, _, _ = validate_engineref_list(
       query_engineref_list, preferences
   )  # ⚠️ 如果所有引擎都验证失败，valid 为空
   ```

5. **第五步：搜索阶段无引擎可用**
   ```python
   # search/__init__.py:86
   for engineref in self.search_query.engineref_list:
       # 空列表，循环不执行
       ...
   requests = []  # 保持为空
   ```

**最终结果**：返回一个没有搜索结果的页面，不触发任何回退机制。

> **categories 锁定场景的差异**：
> - 如果 categories 被锁定，即使 `specific == True`，也会走 `parse_generic()` 路径
> - 此时空的 `raw_text_query.enginerefs` 会被忽略，使用管理员配置的分类
> - 但 `!` 前缀已从搜索文本中剥离，不影响最终查询文本

### 5.4 查询阶段：快捷词命中失败的回退链路

这是最常见的回退逻辑，在 `searx/query.py:292-306` 中实现：

```python
for i, query_part in enumerate(raw_query_parts):
    if query_part.isspace() or query_part == '':
        continue
    
    special_part = False
    for parser_class in RawTextQuery.PARSER_CLASSES:
        if parser_class.check(query_part):
            # 调用解析器，返回 True 表示成功解析，False 表示解析失败
            special_part = parser_class(self, i == autocomplete_index)(query_part)
            break
    
    # 核心分流逻辑
    qlist = self.query_parts if special_part else self.user_query_parts
    qlist.append(query_part)
```

**完整回退链路（以 `!unknown test` 为例）**：

```
用户输入: "!unknown test"
    ↓
re.split(r'(\s+)', query) → ['!unknown', ' ', 'test']
    ↓
处理 "!unknown":
    BangParser.check("!unknown") → True（以 ! 开头）
    ↓
    BangParser._parse("unknown"):
        1. "unknown" in engine_shortcuts? → 否
        2. "unknown" in engines? → 否
        3. "unknown" in categories? → 否
        return False
    ↓
    special_part = False
    ↓
    加入 user_query_parts → user_query_parts = ["!unknown"]
    ↓
处理 "test":
    所有解析器 check() 都返回 False
    ↓
    special_part = False
    ↓
    加入 user_query_parts → user_query_parts = ["!unknown", "test"]
    ↓
最终:
    query_parts = []（无特殊前缀被识别）
    user_query_parts = ["!unknown", "test"]
    getQuery() → "!unknown test"
    specific = False（未设置任何引擎）
    ↓
webadapter 中:
    raw_text_query.specific == False → 调用 parse_generic()
    ↓
    使用默认分类（通常是 general）进行搜索
```

**回退链路关键点**：
1. `BangParser._parse()` 返回 `False` 表示解析失败
2. `special_part = False` 导致该 `query_part` 被加入 `user_query_parts` 而非 `query_parts`
3. `raw_text_query.specific` 保持 `False`（只有 `BangParser` 成功解析时才会设为 `True`）
4. 最终使用默认分类进行搜索，用户输入的 `!unknown` 作为查询文本的一部分

### 5.5 validate 后 valid 为空的后续行为

**关键结论**：`validate_engineref_list()` 返回的 `valid` 为空时，系统不会回退到默认分类，而是静默返回空结果。

**完整代码路径**：

```
webadapter.py: get_search_query_from_webapp()
    ↓
query_engineref_list = raw_text_query.enginerefs  # 可能为空（categories 未锁定时）
    ↓ 或者
query_engineref_list = parse_generic(...)  # 可能返回空（极端情况）
    ↓
query_engineref_list = deduplicate_engineref_list(query_engineref_list)
    ↓
query_engineref_list, _, _ = validate_engineref_list(query_engineref_list, preferences)
    ↓ valid = []（空列表）
    ↓
SearchQuery(query, query_engineref_list, ...)  # engineref_list = []
    ↓
search/__init__.py: Search.search()
    ↓
search_standard()
    ↓
_get_requests()
    ↓
for engineref in self.search_query.engineref_list:
    # 空列表，循环体不执行
    ↓
requests = []
    ↓
if requests:  # False
    search_multiple_requests(requests)  # 不执行
    ↓
返回空的 ResultContainer
    ↓
webapp.py 渲染结果页面
    ↓
显示 "无结果" 页面
```

**行为总结**：
- 系统不会崩溃或抛出异常
- 不会自动回退到默认分类
- 不会有任何日志警告（静默失败）
- 用户看到一个没有搜索结果的页面

### 5.6 禁用引擎的过滤策略

在 `searx/query.py:207-211` 中实现：

```python
self.raw_text_query.enginerefs.extend(
    EngineRef(engine.name, value)
    for engine in categories[value]
    if (engine.name, value) not in self.raw_text_query.disabled_engines
)
```

**策略**：分类匹配时自动过滤掉用户禁用的引擎。如果分类下所有引擎都被禁用，则 `enginerefs` 为空。后续行为取决于 categories 是否锁定：
- **未锁定**：使用空列表搜索，返回空结果（不回退）
- **已锁定**：忽略空列表，使用管理员配置的分类

## 6. categories 锁定场景下的完整行为分析

### 6.1 两层行为对比表

| 行为层面 | categories 未锁定 | categories 已锁定 |
|---------|-----------------|-----------------|
| **引擎选择** | `!` 前缀命中 → 使用 `enginerefs` | `!` 前缀命中 → 忽略 `enginerefs`，使用管理员配置 |
| **文本剥离** | 命中 → 从 `user_query_parts` 剥离 | 命中 → 仍从 `user_query_parts` 剥离（与锁定无关） |
| `specific` 值 | 命中 → `True` | 命中 → `True`（但被忽略） |
| 最终查询文本 | 不含 `!` 前缀 | 不含 `!` 前缀（与未锁定相同） |

### 6.2 示例对比

**场景**：categories 被锁定为 `['general']`，用户输入 `!g python`

```
categories 未锁定时：
    BangParser 命中 "g" → specific = True
    enginerefs = [EngineRef('google', 'none')]
    引擎选择：使用 google 引擎
    最终搜索文本："python"

categories 已锁定时：
    BangParser 命中 "g" → specific = True（RawTextQuery 内部行为）
    enginerefs = [EngineRef('google', 'none')]（RawTextQuery 内部行为）
    webadapter 检测到 is_locked('categories') == True
    → 忽略 enginerefs，调用 parse_generic()
    → 使用管理员配置的 general 分类
    最终搜索文本："python"（!g 已被剥离）
```

**关键差异**：两种场景下最终搜索文本都是 "python"，但使用的引擎集合不同。

## 7. 完整解析流程图

```
用户输入查询字符串
    ↓
webapp.py: search() 路由接收请求
    ↓
webadapter.py: get_search_query_from_webapp()
    ├─ 检查是否锁定分类/语言/安全级别
    └─ 创建 RawTextQuery 实例
        ↓
query.py: RawTextQuery.__init__()
    ↓
query.py: RawTextQuery._parse_query()
    ├─ re.split(r'(\s+)', self.query) 分割查询
    └─ 遍历每个非空白 query_part:
        ├─ TimeoutParser.check(part)?
        │   ├─ 是 → 解析成功? → special_part = True/False
        │   └─ 否 → 继续下一个解析器
        ├─ LanguageParser.check(part)?
        │   ├─ 是 → 解析成功? → special_part = True/False
        │   └─ 否 → 继续下一个解析器
        ├─ ExternalBangParser.check(part)?
        │   ├─ 是 → 解析成功? → special_part = True/False
        │   └─ 否 → 继续下一个解析器
        ├─ BangParser.check(part)?
        │   ├─ 是 → 匹配 engine_shortcuts?
        │   │       ├─ 是 → 转换为引擎名 → 检查 engines
        │   │       └─ 否 → 直接检查 engines
        │   │           ├─ 是 → 加入 enginerefs → special_part = True
        │   │           └─ 否 → 检查 categories
        │   │               ├─ 是 → 加入 enginerefs（过滤禁用引擎）→ special_part = True
        │   │               └─ 否 → special_part = False
        │   └─ 否 → 继续下一个解析器
        ├─ FeelingLuckyParser.check(part)?
        │   ├─ 是 → 解析成功 → special_part = True
        │   └─ 否 → 所有解析器都不匹配
        └─ 分流（与 categories 是否锁定无关!）:
            special_part == True → 加入 query_parts（特殊前缀，从搜索文本剥离）
            special_part == False → 加入 user_query_parts（普通文本）
    ↓
webadapter.py: 选择引擎列表（categories 锁定只影响这一层!）
    ├─ categories 未锁定 AND specific == True?
    │   ├─ 是 → 使用 raw_text_query.enginerefs（可能为空，不回退）
    │   └─ 否 → parse_generic() → 表单分类 → 用户偏好 → general
    ├─ 语言: 锁定? → 管理员配置 → 否则: 查询前缀 > 表单 > 偏好
    ├─ 超时: 查询前缀 > 表单 > 默认
    └─ 安全级别: 锁定? → 管理员配置 → 否则: 表单 > 偏好
    ↓
validate_engineref_list() → 过滤未知引擎和 Token 验证失败的引擎
    ↓
执行搜索（engineref_list 为空时返回空结果）
```

## 8. 关键数据结构

### 8.1 EngineRef

在 `searx/search/models.py:8-24` 中定义：

```python
class EngineRef:
    __slots__ = 'name', 'category'
    def __init__(self, name: str, category: str):
        self.name = name        # 引擎名称
        self.category = category  # 分类名称（'none' 表示直接指定引擎）
```

### 8.2 engine_shortcuts 字典

在 `searx/engines/__init__.py:58-66` 中定义：

```python
engine_shortcuts = {}
# 映射关系：engine_shortcuts[shortcut] = engine_name
```

默认快捷词在 `settings.yml` 中配置，例如：
- `360so` → `360search`
- `g` → `google`
- `wp` → `wikipedia`
- `ddg` → `duckduckgo`

### 8.3 RawTextQuery 关键属性

| 属性 | 类型 | 含义 |
|------|------|------|
| `query_parts` | list | 成功解析的特殊前缀列表（如 `['!g', ':en']`） |
| `user_query_parts` | list | 实际搜索的文本部分列表（如 `['python', 'tutorial']`） |
| `enginerefs` | list[EngineRef] | 解析出的引擎引用列表（可能为空） |
| `languages` | list | 解析出的语言列表 |
| `specific` | bool | 是否通过 `!` 前缀指定了特定引擎/分类（与 enginerefs 是否为空无关） |
| `timeout_limit` | float/None | 解析出的超时限制 |

## 9. 总结

### 9.1 核心设计原则

SearXNG 的引擎快捷前缀解析系统采用**配置阶段严格、查询阶段宽松、管理员配置优先级最高、解析与执行分离**的设计哲学：

1. **配置阶段**：零容忍策略
   - 重复的快捷词或引擎名被视为严重配置错误
   - 系统启动时直接终止，强制管理员修正

2. **查询阶段**：最大可用性策略
   - 无法识别的前缀不报错，作为普通文本继续搜索
   - 但识别了前缀但引擎不可用时，**不做回退**，直接返回空结果（仅 categories 未锁定时）

3. **管理员锁定**：最高优先级
   - 任何配置项被锁定后，完全忽略用户的查询前缀和表单参数（在执行层）
   - 但**解析层**不受锁定影响，前缀仍会被正常解析和剥离
   - categories 锁定时，`!` 前缀在**引擎选择层**失效，但在**文本剥离层**仍生效
   - language 锁定时，`:` 前缀在**语言选择层**失效，但在**文本剥离层**仍生效

4. **解析与执行分离**：
   - `RawTextQuery` 只负责解析，不关心配置是否锁定
   - 锁定逻辑在 `webadapter.py` 的执行层生效
   - 这种分离确保了解析逻辑的简洁性和一致性

5. **优先级设计**：
   - 管理员锁定配置 > 查询前缀 > 表单参数 > 用户偏好 > 系统默认

### 9.2 关键行为澄清

| 场景 | 实际行为 | 常见误解 |
|------|---------|---------|
| `!分类` 但分类下引擎全被禁用（categories 未锁定） | 返回空结果，不回退 | ❌ 误以为会回退到 general |
| `!分类` 但分类下引擎全被禁用（categories 已锁定） | 忽略 `!` 前缀，使用管理员配置的分类搜索 | ❌ 误以为会使用 `!` 指定的分类 |
| `!引擎` 但引擎 Token 验证失败 | 返回空结果，不回退 | ❌ 误以为会尝试其他引擎 |
| validate 后 valid 为空 | 返回空结果，静默失败 | ❌ 误以为会有错误提示或回退 |
| `!unknown` 无法匹配 | 作为普通文本搜索 | ✅ 正确 |
| categories 已锁定时使用 `!g` | 忽略 `!g` 选择引擎，但 `!g` 会从搜索文本中剥离 | ❌ 误以为 `!` 前缀完全失效（文本也保留） |
| language 已锁定时使用 `:en` | 忽略 `:en` 选择语言，但 `:en` 会从搜索文本中剥离 | ❌ 误以为 `:` 前缀完全失效（文本也保留） |

### 9.3 多层回退机制（仅限特定场景）

- **快捷词匹配失败时**：快捷词 → 引擎名 → 分类名 → 普通文本
- **无快捷词且 categories 未锁定时**：表单分类 → 用户偏好分类 → general 分类
- **categories 已锁定时**：管理员配置分类（忽略所有用户输入，但文本仍被剥离）
- **引擎验证失败时**：静默过滤，不影响整体搜索（但可能导致空结果）

这种设计在配置正确性、用户体验和管理员控制权之间做了权衡：对于用户输入错误保持宽容，对于已识别的用户意图保持忠实，同时确保管理员的锁定配置具有最高优先级。
