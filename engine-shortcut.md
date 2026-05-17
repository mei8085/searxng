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

使用正则表达式 `(\s+)` 进行分割，保留空白字符作为分割标记。

### 3.2 解析顺序

`RawTextQuery.PARSER_CLASSES` 定义了解析器的执行顺序：

| 解析器 | 前缀 | 功能 |
|--------|------|------|
| TimeoutParser | `<` | 设置超时时间 |
| LanguageParser | `:` | 设置语言 |
| ExternalBangParser | `!!` | 外部 Bang 跳转 |
| BangParser | `!` | 引擎/分类快捷前缀 |
| FeelingLuckyParser | `!!` | 手气不错（重定向到第一个结果） |

### 3.3 BangParser 切分规则

`BangParser` 在 `searx/query.py:178-238` 中实现，核心逻辑：

1. **识别条件**：`raw_value[0] == '!'` 且不是 `!!`
2. **值规范化**：`value = raw_value[1:].replace('-', ' ').replace('_', ' ').lower()`
3. **匹配优先级**：
   - 第一步：匹配 `engine_shortcuts` 字典中的快捷词
   - 第二步：匹配 `engines` 字典中的引擎名称
   - 第三步：匹配 `categories` 字典中的分类名称

### 3.4 快捷词匹配示例

| 用户输入 | 匹配过程 | 结果 |
|----------|----------|------|
| `!g python` | `g` → 匹配 `engine_shortcuts['g']` → `google` | 使用 google 引擎搜索 "python" |
| `!wp python` | `wp` → 匹配 `engine_shortcuts['wp']` → `wikipedia` | 使用 wikipedia 引擎搜索 "python" |
| `!images cat` | `images` → 匹配 `categories['images']` | 使用 images 分类下所有引擎搜索 "cat" |

## 4. 与默认配置的协同方式

### 4.1 分类协同

在 `searx/webadapter.py:269-276` 中实现：

```python
if not is_locked('categories') and raw_text_query.specific:
    query_engineref_list = raw_text_query.enginerefs
else:
    query_engineref_list = parse_generic(preferences, form, disabled_engines)
```

**关键逻辑**：
- 当 `raw_text_query.specific == True`（即用户使用了 `!` 前缀），且分类未被锁定时，使用快捷前缀指定的引擎
- 否则使用表单或偏好设置中的分类

### 4.2 语言协同

在 `searx/webadapter.py:55-72` 中实现：

```python
def parse_lang(preferences, form, raw_text_query):
    if len(raw_text_query.languages):
        query_lang = raw_text_query.languages[-1]  # 使用查询中的语言前缀
    elif 'language' in form:
        query_lang = form.get('language')           # 使用表单参数
    else:
        query_lang = preferences.get_value('language')  # 使用用户偏好
```

**优先级顺序**：查询前缀 `:语言` > 表单参数 > 用户偏好设置

### 4.3 搜索安全级别协同

在 `searx/webadapter.py:75-92` 中实现：

```python
def parse_safesearch(preferences, form):
    if is_locked('safesearch'):
        return preferences.get_value('safesearch')
    if 'safesearch' in form:
        query_safesearch = form.get('safesearch')
    else:
        query_safesearch = preferences.get_value('safesearch')
```

**注意**：搜索安全级别（safesearch）**不通过查询前缀**设置，只能通过表单参数或用户偏好设置。

### 4.4 超时设置协同

在 `searx/webadapter.py:104-114` 中实现：

```python
def parse_timeout(form, raw_text_query):
    timeout_limit = raw_text_query.timeout_limit
    if timeout_limit is None:
        timeout_limit = form.get('timeout_limit')
```

**优先级顺序**：查询前缀 `<超时时间>` > 表单参数

## 5. 快捷词与配置冲突时的回退策略

### 5.1 配置加载时的冲突检测

在 `searx/engines/__init__.py:251-260` 中实现：

```python
def register_engine(engine):
    if engine.name in engines:
        logger.error('Engine config error: ambiguous name: {0}'.format(engine.name))
        sys.exit(1)
    engines[engine.name] = engine

    if engine.shortcut in engine_shortcuts:
        logger.error('Engine config error: ambiguous shortcut: {0}'.format(engine.shortcut))
        sys.exit(1)
    engine_shortcuts[engine.shortcut] = engine.name
```

**策略**：配置加载时检测到重复的快捷词，直接报错并退出程序（`sys.exit(1)`），属于**严格失败**策略。

### 5.2 查询解析时的回退策略

在 `searx/query.py:193-214` 的 `BangParser._parse()` 中实现：

```python
def _parse(self, value):
    # 1. 尝试匹配快捷词
    if value in engine_shortcuts:
        value = engine_shortcuts[value]
    
    # 2. 尝试匹配引擎名称
    if value in engines:
        self.raw_text_query.enginerefs.append(EngineRef(value, 'none'))
        return True
    
    # 3. 尝试匹配分类名称
    if value in categories:
        self.raw_text_query.enginerefs.extend(...)
        return True
    
    # 4. 都不匹配时返回 False
    return False
```

**回退逻辑**：
- 如果快捷词、引擎名、分类名**都不匹配**，`BangParser.__call__()` 返回 `False`
- 此时该前缀被视为普通查询文本的一部分，保留在 `user_query_parts` 中
- 不会报错，继续使用默认分类进行搜索

### 5.3 禁用引擎的回退策略

在 `searx/query.py:207-211` 中实现：

```python
self.raw_text_query.enginerefs.extend(
    EngineRef(engine.name, value)
    for engine in categories[value]
    if (engine.name, value) not in self.raw_text_query.disabled_engines
)
```

**策略**：分类匹配时自动过滤掉用户禁用的引擎，如果分类下所有引擎都被禁用，则该分类无效。

### 5.4 验证阶段的回退

在 `searx/webadapter.py:21-45` 中实现：

```python
def validate_engineref_list(engineref_list, preferences):
    valid = []
    unknown = []
    no_token = []
    for engineref in engineref_list:
        if engineref.name not in engines:
            unknown.append(engineref)
            continue
        engine = engines[engineref.name]
        if not preferences.validate_token(engine):
            no_token.append(engineref)
            continue
        valid.append(engineref)
    return valid, unknown, no_token
```

**策略**：
- 未知引擎：放入 `unknown` 列表，不参与搜索
- Token 验证失败：放入 `no_token` 列表，不参与搜索
- 只有 `valid` 列表中的引擎会实际执行搜索

## 6. 完整解析流程图

```
用户输入查询字符串
        ↓
webapp.py: search() 路由接收请求
        ↓
webadapter.py: get_search_query_from_webapp()
        ↓
query.py: RawTextQuery.__init__()
        ↓
query.py: RawTextQuery._parse_query()
        ├─ 按空白字符分割查询
        ├─ 遍历每个 query_part
        │   ├─ TimeoutParser (<) → 超时设置
        │   ├─ LanguageParser (:) → 语言设置
        │   ├─ ExternalBangParser (!!) → 外部跳转
        │   ├─ BangParser (!) → 引擎/分类快捷
        │   │   ├─ 匹配 engine_shortcuts
        │   │   ├─ 匹配 engines
        │   │   ├─ 匹配 categories
        │   │   └─ 都不匹配 → 作为普通文本
        │   └─ FeelingLuckyParser (!!) → 手气不错
        └─ 分离为 query_parts (特殊前缀) 和 user_query_parts (实际查询)
        ↓
webadapter.py: 组装 SearchQuery
        ├─ 语言：查询前缀 > 表单 > 偏好
        ├─ 引擎：快捷前缀指定 > 表单/分类偏好
        ├─ 超时：查询前缀 > 表单
        └─ 安全级别：表单 > 偏好（锁定时忽略）
        ↓
执行搜索
```

## 7. 关键数据结构

### 7.1 EngineRef

在 `searx/search/models.py:8-24` 中定义：

```python
class EngineRef:
    __slots__ = 'name', 'category'
    def __init__(self, name: str, category: str):
        self.name = name        # 引擎名称
        self.category = category  # 分类名称（'none' 表示直接指定引擎）
```

### 7.2 engine_shortcuts 字典

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

## 8. 总结

SearXNG 的引擎快捷前缀解析系统采用**分层解析、严格配置、宽松查询**的策略：

1. **配置阶段**：严格检查，重复快捷词直接报错退出
2. **查询阶段**：宽松处理，未识别的前缀作为普通文本保留
3. **协同机制**：查询前缀优先级最高，其次是表单参数，最后是用户偏好
4. **回退策略**：多层验证确保只有有效且可用的引擎参与搜索

该设计既保证了配置的正确性，又提供了良好的用户体验，使用户可以通过简单的前缀语法快速切换搜索范围。
