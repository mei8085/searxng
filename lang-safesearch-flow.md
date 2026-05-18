# SearXNG 语言偏好与安全搜索等级完整流向

## 一、整体流程概览

```
用户请求
    ↓
[入口参数解析] webadapter.py
    ├─ parse_lang()            # 语言偏好解析（保留用户选择，包括 auto）
    └─ parse_safesearch()      # 安全搜索解析
    ↓
[auto 语言替换] webadapter.py
    └─ query_lang == 'auto' ? 用客户端语言替换 : 保留原值
    ↓
[SearchQuery 封装] search/models.py
    ↓
[分发参数构建] search/processors/abstract.py
    ├─ get_params()         # 构建基础请求参数
    └─ 各引擎 request()     # 引擎特定格式适配
    ↓
[引擎执行]
    ├─ 支持 → 正常请求
    └─ 不支持 → 兜底/跳过
    ↓
[结果合并] results.py
    ├─ extend()             # 合并结果
    └─ add_unresponsive_engine() # 按 display_error_messages 决定是否记录
    ↓
返回给用户
```

---

## 二、入口参数解析

### 2.1 语言偏好解析 (`parse_lang` - webadapter.py:55-72)

**优先级从高到低：**

1. **查询语法**：`raw_text_query.languages`（如 `:zh` 语法）
2. **表单参数**：`form['language']`
3. **用户偏好**：`preferences.get_value('language')`（来自 cookies）
4. **默认值**：`settings['search']['default_lang']`（默认为 `"auto"`）

**自动语言选择的两阶段处理：**
- **阶段一（参数解析）**：`parse_lang()` 先完整保留用户的选择结果（包括 `"auto"`），存入 `selected_locale` 变量，用于 UI 展示
- **阶段二（查询组装）**：在 `get_search_query_from_webapp()` 中检查，若 `query_lang == 'auto'`，才替换为 `preferences.client.locale_tag`（从 `Accept-Language` 头解析），若仍无则回退到 `"all"`

**锁定机制：** 如果 `is_locked('language')` 为 `True`，直接使用偏好值，忽略其他输入

**代码位置：**
- 参数解析：[webadapter.py:55-72](searx/webadapter.py#L55-L72)
- auto 替换：[webadapter.py:263-267](searx/webadapter.py#L263-L267)

### 2.2 安全搜索等级解析 (`parse_safesearch` - webadapter.py:75-92)

**优先级从高到低：**

1. **表单参数**：`form['safesearch']`
2. **用户偏好**：`preferences.get_value('safesearch')`（来自 cookies）
3. **默认值**：`settings['search']['safe_search']`（默认为 `0`）

**安全搜索等级定义：**
- `0`: None（不过滤）
- `1`: Moderate（适度过滤）
- `2`: Strict（严格过滤）

**锁定机制：** 如果 `is_locked('safesearch')` 为 `True`，直接使用偏好值

**代码位置：** [webadapter.py:75-92](searx/webadapter.py#L75-L92)

### 2.3 默认值、Cookies、用户设置覆盖关系

```
settings.yml 默认值
    ↓ （服务启动时加载）
Preferences 初始化
    ↓ （用户首次访问）
Cookie 解析 → Preferences.key_value_settings
    ↓ （每次请求）
表单参数/查询语法覆盖
    ↓
最终 SearchQuery.lang / SearchQuery.safesearch
```

**关键配置：**
- `settings.yml` 中 `search.default_lang`：默认语言
- `settings.yml` 中 `search.safe_search`：默认安全搜索等级
- `settings.yml` 中 `preferences.lock`：可锁定 language/safesearch 等设置

---

## 三、向各引擎分发时的格式适配

### 3.1 基础参数构建 (`EngineProcessor.get_params` - search/processors/abstract.py:233-279)

所有引擎共享的参数构建逻辑：

```python
params: RequestParams = {
    "query": search_query.query,
    "category": engine_category,
    "pageno": search_query.pageno,
    "safesearch": search_query.safesearch,  # 直接传递数值 0/1/2
    "time_range": search_query.time_range,
    "engine_data": search_query.engine_data.get(self.engine.name, {}),
    "searxng_locale": search_query.lang,    # 原始语言字符串
}
```

**代码位置：** [search/processors/abstract.py:257-277](searx/search/processors/abstract.py#L257-L277)

### 3.2 语言格式适配

#### 3.2.1 Traits 系统

每个引擎通过 `EngineTraits` 存储语言映射：

- `traits.languages`: SearXNG 语言 → 引擎语言 映射
- `traits.regions`: SearXNG 区域 → 引擎区域 映射
- `traits.all_locale`: `"all"` 对应的引擎值

**核心适配方法：**
```python
# 获取引擎语言
traits.get_language(searxng_locale, default)

# 获取引擎区域
traits.get_region(searxng_locale, default)
```

**代码位置：** [enginelib/traits.py:87-117](searx/enginelib/traits.py#L87-L117)

#### 3.2.2 智能语言匹配 (`get_engine_locale` - locales.py:218-317)

当没有 1:1 映射时，按以下规则匹配：

1. **直接匹配**：检查是否有精确匹配
2. **语言标签匹配**：尝试匹配语言部分（如 `zh-HK` → `zh_Hant`）
3. **区域优先**：按区域的官方语言匹配
4. **语言优先**：按语言在其他区域的官方状态匹配

**代码位置：** [locales.py:218-317](searx/locales.py#L218-L317)

#### 3.2.3 引擎实现示例

**Google 引擎** ([engines/google.py:101-278](searx/engines/google.py#L101-L278))：
```python
eng_lang = eng_traits.get_language(sxng_locale, "lang_en")
country = eng_traits.get_region(sxng_locale, eng_traits.all_locale)

# 构建 Google 特定参数
ret_val["params"]["hl"] = f"{lang_code}-{country}"  # 界面语言
ret_val["params"]["lr"] = eng_lang                    # 结果语言限制
ret_val["params"]["cr"] = "country" + country          # 国家限制
```

**DuckDuckGo 引擎** ([engines/duckduckgo.py:361-443](searx/engines/duckduckgo.py#L361-L443))：
```python
eng_region = traits.get_region(params["searxng_locale"], traits.all_locale)
data["kl"] = eng_region  # DDG 的区域参数
```

### 3.3 安全搜索格式适配

各引擎通过 `safesearch_map` 将 0/1/2 映射为引擎特定值：

| 引擎 | safesearch_map | 实现方式 | 示例 |
|------|---------------|----------|------|
| Google | `{0: "off", 1: "medium", 2: "high"}` | URL 参数 | `safe=medium` |
| Yahoo | `{0: "p", 1: "i", 2: "r"}` | URL 参数 | - |
| **Brave** | **`{0: "off", 1: "moderate", 2: "strict"}`** | **Cookie 写入** | **`safesearch=strict`** |
| Wallhaven | `{0: "111", 1: "110", 2: "100"}` | URL 参数 | - |
| XPath 引擎 | 可配置 | URL 参数拼接 | `&filter=moderate` |

**Google 引擎示例** ([engines/google.py:340-341](searx/engines/google.py#L340-L341))：
```python
if params["safesearch"]:
    query_url += "&" + urlencode({"safe": filter_mapping[params["safesearch"]]})
```

**Brave 引擎示例** ([engines/brave.py:183,221](searx/engines/brave.py#L183-L221))：
```python
safesearch_map = {2: "strict", 1: "moderate", 0: "off"}  # 等级到字符串映射

# 写入 cookie，而非 URL 参数
params["cookies"]["safesearch"] = safesearch_map.get(params["safesearch"], "off")
```

**XPath 引擎通用处理** ([engines/xpath.py:236-239](searx/engines/xpath.py#L236-L239))：
```python
safe_search_val = params.get('safesearch')
if safe_search_val is not None:
    safe_search = safe_search_map[safe_search_val]
```

---

## 四、引擎不支持时的兜底机制

### 4.1 语言不支持的兜底

1. **`get_language()` / `get_region()` 返回 `None`**：
   - 使用 `default` 参数（通常为 `traits.all_locale`）
   - 若 `default` 也为 `None`，则使用引擎自身默认逻辑

2. **XPATH 引擎简化处理** ([engines/xpath.py:227-229](searx/engines/xpath.py#L227-L229))：
   ```python
   lang = lang_all  # 默认 'en'
   if params['language'] != 'all':
       lang = params['language'][:2]  # 只取前两位
   ```

### 4.2 安全搜索不支持的兜底

1. **引擎声明 `safesearch = False`**：
   - 不传递安全搜索参数
   - 由搜索引擎自身默认策略处理

2. **部分引擎仅部分支持**：
   - DuckDuckGo：`safesearch = True` 但用户无法选择，结果由 DDG 过滤
   - Google Scholar：`safesearch = False`，不支持安全搜索

### 4.3 引擎完全跳过机制

在 `EngineProcessor.get_params()` 中，以下情况返回 `None`，引擎被跳过：
- 分页不支持但请求页码 > 1
- 时间范围不支持但请求了时间范围
- 超过最大页数限制

**代码位置：** [search/processors/abstract.py:244-255](searx/search/processors/abstract.py#L244-L255)

---

## 五、结果合并阶段的回收

### 5.1 结果合并 (`ResultContainer.extend` - results.py:83-158)

```python
def extend(self, engine_name: str | None, results: list[Result | LegacyResult]):
    for result in list(results):
        # 结果标准化处理
        result.engine = result.engine or engine_name
        result.normalize_result_fields()
        
        # 按类型分发
        if isinstance(result, BaseAnswer):
            self.answers.add(result)
        elif isinstance(result, MainResult):
            self._merge_main_result(result, main_count)
        # ... 其他类型处理
```

**关键点：**
- 结果中不直接携带 `lang` 或 `safesearch` 信息
- 仅记录来源引擎名称
- 重复结果通过 `_merge_main_result()` 合并

**代码位置：** [results.py:83-158](searx/results.py#L83-L158)

### 5.2 不响应引擎记录 (`add_unresponsive_engine` - results.py:274-282)

```python
def add_unresponsive_engine(self, engine_name: str, error_type: str, suspended: bool = False):
    if searx.engines.engines[engine_name].display_error_messages:
        self.unresponsive_engines.add(
            UnresponsiveEngine(engine_name, error_type, suspended)
        )
```

**记录条件：**
- 是否记录取决于引擎配置的 `display_error_messages` 开关（默认为 `True`）
- 若 `display_error_messages: False`，即使引擎出错也不会添加到 `unresponsive_engines` 列表中

**记录的信息：**
- 引擎名称
- 错误类型（如 timeout、HTTP error 等）
- 是否被临时挂起

**代码位置：** [results.py:274-280](searx/results.py#L274-L280)

### 5.3 结果返回

最终返回给用户的结果包含：
- 合并后的搜索结果列表
- 建议、答案、信息框等
- 各引擎响应时间统计
- 不响应引擎列表（用于 UI 提示，受 `display_error_messages` 控制）

---

## 六、关键文件索引

| 功能模块 | 文件路径 | 关键函数/类 |
|---------|---------|------------|
| 入口参数解析 | `searx/webadapter.py` | `parse_lang`, `parse_safesearch`, `get_search_query_from_webapp` |
| 用户偏好管理 | `searx/preferences.py` | `Preferences`, `MapSetting`, `SearchLanguageSetting` |
| 搜索查询模型 | `searx/search/models.py` | `SearchQuery`, `EngineRef` |
| 处理器抽象 | `searx/search/processors/abstract.py` | `EngineProcessor.get_params` |
| 引擎 Traits | `searx/enginelib/traits.py` | `EngineTraits`, `get_language`, `get_region` |
| 语言匹配 | `searx/locales.py` | `get_engine_locale` |
| 结果合并 | `searx/results.py` | `ResultContainer.extend`, `add_unresponsive_engine` |
| 引擎配置默认值 | `searx/engines/__init__.py` | `display_error_messages` 默认值定义 |
| Google 引擎 | `searx/engines/google.py` | `get_google_info`, `request` |
| DuckDuckGo 引擎 | `searx/engines/duckduckgo.py` | `request` |
| XPath 引擎 | `searx/engines/xpath.py` | `request` |

---

## 七、配置示例

### settings.yml 相关配置

```yaml
search:
  safe_search: 0        # 默认安全搜索等级: 0=None, 1=Moderate, 2=Strict
  default_lang: "auto"  # 默认语言: auto=检测浏览器语言

preferences:
  lock:
    - language          # 锁定语言设置，用户无法修改
    - safesearch        # 锁定安全搜索设置，用户无法修改
```

### 引擎 safesearch 配置示例

```yaml
- name: google
  engine: google
  safesearch: true
  display_error_messages: true  # 是否在 UI 显示该引擎的错误信息

- name: xpath-example
  engine: xpath
  safesearch: true
  safe_search_map:
    0: '&filter=none'
    1: '&filter=moderate'
    2: '&filter=strict'

- name: silent-engine
  engine: some_engine
  display_error_messages: false  # 静默失败，不显示给用户
```

### display_error_messages 说明

| 配置值 | 行为 |
|--------|------|
| `true`（默认） | 引擎出错时，错误信息会记录到 `unresponsive_engines` 并在 UI 展示 |
| `false` | 引擎出错时，仅内部记录日志，但不会在 UI 展示给用户 |

**代码默认值定义位置：** [engines/__init__.py:41](searx/engines/__init__.py#L41)
