# SearxNG 搜索结果渲染上下文深度分析（严谨补证版 v2）

## 概述

本文档对 SearxNG 后端搜索结果渲染上下文进行严谨化分析，重点修正和补充：
- **pre_search 短路语义修正**：与源码执行语义完全对齐
- **post_search 插件全覆盖**：纳入 hash_plugin，明确各插件在命中条件下的行为
- **图像结果 parsed_url 判定修正**：区分 hash 豁免与模板字段必需性
- **异常传播链路**：结果对象抛错后在处理器层被捕获并转成引擎异常信号的完整链路

---

## 1. 即时答案短路机制的边界修正

### 1.1 完整执行流程与短路边界

**文件位置**：`searx/search/__init__.py:201-209`

```python
class SearchWithPlugins(Search):
    def search(self) -> ResultContainer:
        if searx.plugins.STORAGE.pre_search(self.request, self):  # 前置插件
            super().search()                                       # 内部执行：bang → answerers → search_standard
        
        searx.plugins.STORAGE.post_search(self.request, self)      # 后置插件（始终执行！）
        self.result_container.close()
        
        return self.result_container
```

**源码语义的精确解读**：
- `pre_search` 返回 `True` → 继续执行 `super().search()`
- `pre_search` 返回 `False` → **跳过整个 `super().search()`**（包括 bang、answerers 和常规引擎）
- **无论如何**，`post_search` 和 `result_container.close()` 始终执行

**短路边界的精确定义**：

| 阶段 | 是否可被短路 | 短路条件 | 始终执行 |
|------|-------------|----------|---------|
| `pre_search` 插件 | ❌ 否 | - | ✅ 始终执行 |
| 外部 bang 检查 | ✅ 是 | `pre_search` 未阻止 + 匹配到 bang 语法 | ❌ |
| Answerers 内部答案 | ✅ 是 | `pre_search` 未阻止 + `search_answerers()` 返回非空 | ❌ |
| 常规引擎检索 | ✅ 是 | 前三者任一阻止或命中 | ❌ |
| `post_search` 插件 | ❌ 否 | - | ✅ 始终执行 |
| `result_container.close()` | ❌ 否 | - | ✅ 始终执行 |

**pre_search 短路的代码证据**：`searx/search/__init__.py:203-204`
```python
if searx.plugins.STORAGE.pre_search(self.request, self):  # 返回 True 才进入
    super().search()                                      # 返回 False 则整个跳过
```

### 1.2 Answerers 短路的代码证据

**文件位置**：`searx/search/__init__.py:174-179`

```python
def search(self) -> ResultContainer:
    self.start_time = default_timer()
    if not self.search_external_bang():      # 第1层：外部 bang
        if not self.search_answerers():      # 第2层：内部 answerers
            self.search_standard()           # 第3层：常规引擎（仅当前两层均未命中时执行）
    return self.result_container
```

**关键逻辑**：
- `search_answerers()` 返回非空列表时，`not search_answerers()` 为 `False`
- `if` 条件不满足，**直接跳过 `search_standard()`**
- 但 `post_search` 插件在 `super().search()` 返回后仍会执行

### 1.3 post_search 插件全覆盖分析

**文件位置**：`searx/plugins/_core.py:282-306`

```python
def post_search(self, request: SXNG_Request, search: "SearchWithPlugins") -> None:
    keyword = None
    for keyword in search.search_query.query.split():
        if keyword:
            break
    
    for plugin in [p for p in self.plugin_list if p.id in search.user_plugins]:
        if plugin.keywords:
            # 有关键词的插件：仅当查询首词匹配时执行
            if keyword and keyword not in plugin.keywords:
                continue
        try:
            results = plugin.post_search(request=request, search=search) or []
        except Exception:
            continue
        
        # 通过 extend 向结果容器添加新结果
        search.result_container.extend(f"plugin: {plugin.id}", results)
```

**所有 post_search 插件的完整清单**：

| 插件 | 关键词 | 命中条件 | 新增结果类型 |
|------|--------|---------|-------------|
| `unit_converter` | 无关键词 | 查询含 "in", "to", "=", "->" 等转换关键词 | ✅ `Answer`（即时答案） |
| `tor_check` | `["tor", "tor check"]` | 查询首词为 "tor" 或 "tor" 且 pageno=1 | ✅ `Answer`（即时答案） |
| `time_zone` | `["time", "date", "tz", "timezone"]` | 查询首词匹配且 pageno=1 | ✅ `Answer`（即时答案） |
| `self_info` | `["ip", "user-agent"]` | 查询首词为 "ip" 或 "user-agent" 且 pageno=1 | ✅ `Answer`（即时答案） |
| `hash_plugin` | `["md5", "sha1", "sha224", "sha256", "sha384", "sha512"]` | 查询首词为 hash 算法名且 pageno=1 | ✅ `Answer`（即时答案） |

**hash_plugin 证据**：`searx/plugins/hash_plugin.py:41-66`
```python
def post_search(self, request, search) -> EngineResults:
    results = EngineResults()
    if search.search_query.pageno > 1:
        return results
    
    m = self.parser_re.match(search.search_query.query)  # 匹配 "md5 xxx" 格式
    if not m:
        return results
    
    # ... hash 计算 ...
    results.add(results.types.Answer(answer=answer))  # 新增即时答案
    return results
```

**边界修正结论**：
> 即时答案的短路仅限定在**常规引擎检索阶段**（`search_standard()`）。`post_search` 插件始终执行，上述 5 个插件在命中条件下均可通过 `result_container.extend()` 向上下文中添加新的答案或结果，改写最终的渲染上下文。

---

## 2. 结果对象构建层的字段约束与异常链路

### 2.1 对象失效的硬约束：hash 计算

**文件位置**：`searx/result_types/_base.py:405-418` (MainResult)

```python
def __hash__(self) -> int:
    if not self.parsed_url:
        raise ValueError(f"missing a value in field 'parsed_url': {self}")
    
    url = self.parsed_url
    return hash(
        f"{self.template}"
        + f"|{url.netloc}|{url.path}|{url.params}|{url.query}|{url.fragment}"
        + f"|{self.img_src}"
    )
```

**文件位置**：`searx/result_types/_base.py:521-547` (LegacyResult)

```python
def __hash__(self) -> int:
    if self.template == "images.html":
        # 图像结果特殊 hash：不依赖 parsed_url，仅需要 url 和 img_src
        return hash(f"{self.template}|{self.url}|{self.img_src}")
    
    if not any(cls in self for cls in ["suggestion", "correction", "infobox", ...]):
        # 普通 URL 结果
        if not self.parsed_url:
            raise ValueError(f"missing a value in field 'parsed_url': {self}")
        # ... hash 计算
```

**hash 调用时机**：`searx/results.py:173-183`

```python
def _merge_main_result(self, result: MainResult | LegacyResult, position: int):
    result_hash = hash(result)  # 这里触发 hash 计算
    # ... 后续合并逻辑
```

### 2.2 引擎阶段异常传播完整链路

**关键发现**：`ResultContainer.extend()` 调用 `_merge_main_result()` 时**没有捕获** `ValueError` 异常，异常会向上传播到 processor 层被捕获。

**引擎阶段异常链路**（仅发生在搜索执行阶段）：

```
引擎返回结果列表
    ↓
OnlineProcessor.search() [try 块内]
    ↓
_extend_container_basic()
    ↓
ResultContainer.extend()
    ↓
_merge_main_result()
    ↓
hash(result) → 抛出 ValueError (parsed_url 为 None 且非图像结果)
    ↓
异常向上传播
    ↓
OnlineProcessor.search() 的 except Exception 捕获 [online.py:280-282]
    ↓
handle_exception(result_container, e)
    ├─ result_container.add_unresponsive_engine(engine_name, "ValueError")
    ├─ metrics 计数：counter_inc error
    └─ count_exception 记录异常
    ↓
结果未加入 main_results_map，引擎被标记为异常
```

**代码证据**：`searx/search/processors/online.py:280-282`
```python
except Exception as e:  # pylint: disable=broad-except
    self.handle_exception(result_container, e)  # 转成引擎异常信号
    self.logger.exception("exception : {0}".format(e))
```

**handle_exception 实现**：`searx/search/processors/abstract.py:165-191`
```python
def handle_exception(self, result_container, exception_or_message, suspend=False):
    if isinstance(exception_or_message, BaseException):
        error_message = module_name + exception_class.__qualname__  # 如 "builtins.ValueError"
    else:
        error_message = exception_or_message
    
    result_container.add_unresponsive_engine(self.engine.name, error_message)
    counter_inc('engine', self.engine.name, 'search', 'count', 'error')
    # ... 可选挂起引擎
```

**引擎阶段异常的最终表现**：
- 异常结果被丢弃，不进入渲染上下文
- 该引擎在结果页底部显示为 "unresponsive"，错误信息为 "ValueError"
- 不影响其他引擎的结果，页面正常渲染

### 2.3 引擎阶段异常 vs 模板渲染阶段异常对比

SearxNG 存在两个完全独立的异常捕获边界，图像结果的 `parsed_url.netloc` 属于**模板渲染阶段异常**，**不会**被 processor 捕获链捕获。

| 对比维度 | 引擎阶段异常 | 模板渲染阶段异常 |
|---------|-------------|----------------|
| **触发位置** | `_merge_main_result()` 中的 `hash(result)` 调用 | Jinja2 模板渲染时的字段访问，如 `result.parsed_url.netloc` |
| **捕获位置** | `OnlineProcessor.search()` 的 `except Exception` | Flask 框架的全局错误处理器 |
| **典型场景** | 普通 URL 结果 `parsed_url = None` 触发 hash 抛错 | 图像结果 `parsed_url = None`，模板第 6 行访问 `.netloc` 时抛 `AttributeError` |
| **异常类型** | `ValueError` (hash 计算) | `AttributeError` (None.netloc) |
| **对结果的影响** | 单个结果被丢弃，引擎标记为异常 | 整个页面渲染失败，返回 500 错误 |
| **用户可见性** | 部分引擎结果缺失，页面其他部分正常 | 全站错误页，无搜索结果 |
| **豁免机制** | 图像结果 hash 不依赖 parsed_url | 无，模板异常会穿透到 Flask 层 |

**图像结果的特殊异常路径**：
```
图像结果返回 (url = "", img_src = "xxx", template = "images.html")
    ↓
normalize_result_fields() → parsed_url = None (因为 url 为空)
    ↓
_merge_main_result() → hash(result)
    ↓
LegacyResult.__hash__ 检测到 template == "images.html"
    ↓
使用 hash(f"images.html|{url}|{img_src}") → 成功，不抛错
    ↓
结果成功加入 main_results_map
    ↓
... 搜索流程完成 ...
    ↓
webapp.py 调用 render(results.html)
    ↓
Jinja2 模板引擎遍历 results
    ↓
渲染 images.html 模板第 6 行：{{ result.parsed_url.netloc }}
    ↓
parsed_url 为 None → AttributeError: 'NoneType' object has no attribute 'netloc'
    ↓
异常向上传播到 Flask 框架
    ↓
返回 500 Internal Server Error 页面
```

**关键结论**：图像结果的 hash 豁免仅保护了**搜索执行阶段**，但无法保护**模板渲染阶段**。`parsed_url` 为 None 的图像结果会成功进入结果容器，但在渲染时导致整个页面崩溃。

### 2.3 URL 字段规范化流程

**文件位置**：`searx/result_types/_base.py:38-57`

```python
def _normalize_url_fields(result: "Result | LegacyResult"):
    if result.url and not result.parsed_url:
        if not isinstance(result.url, str):
            log.debug('result: invalid URL: %s', str(result))
            result.url = ""              # 清空无效 URL
            result.parsed_url = None      # 设为 None → 后续 hash 失败
        else:
            result.parsed_url = urllib.parse.urlparse(result.url)
    
    if result.parsed_url:
        result.parsed_url = result.parsed_url._replace(scheme=result.parsed_url.scheme or "http")
        result.url = result.parsed_url.geturl()
```

**规范化后状态矩阵**：

| 原始 url | 原始 parsed_url | 规范化后 url | 规范化后 parsed_url | 后续 hash 结果 | 异常传播结果 |
|---------|----------------|-------------|--------------------|--------------|-------------|
| 有效字符串 | None | 规范化后的 URL | ParseResult 对象 | ✅ 成功 | 正常合并 |
| 非字符串（如 int） | None | `""` | `None` | ❌ ValueError | 引擎标记为异常 |
| `None` / `""` | None | 不变 | `None` | ❌ ValueError（非图像） | 引擎标记为异常 |
| 任意值 | 已设置 | 规范化后的 URL | 补全 scheme 后的 ParseResult | ✅ 成功 | 正常合并 |

---

## 3. 字段缺失的三级行为分类

### 3.1 判定准则（可复核）

| 行为类型 | 判定依据 | 代码位置 | 结果 |
|---------|---------|---------|------|
| **对象失效** | hash 计算抛出未捕获异常，向上传播到 processor 被捕获为引擎异常 | `MainResult.__hash__()` 抛出 `ValueError` + `except Exception as e` | 结果被丢弃，引擎标记为 unresponsive |
| **显示占位** | 模板中有条件判断，字段为空时显示占位符或跳过 | `{% if result.field %}...{% else %}&nbsp;{% endif %}` | 页面显示空白占位或提示文本 |
| **渲染降级** | 核心增强字段缺失，模板使用备用值或隐藏部分功能 | 如 `{{ result.thumbnail_src or result.img_src }}` | 功能减弱但结果正常显示 |

### 3.2 对象失效场景清单

**硬失效条件**：`parsed_url` 为 `None` 且调用 `hash()`

| 结果类型 | 触发条件 | 失效路径 |
|---------|---------|---------|
| 普通 URL 结果（default.html 等） | `url` 缺失 / 类型无效 / 解析失败 → `parsed_url = None` | `_merge_main_result` → `hash(result)` → `ValueError` → processor 捕获 → 引擎标记异常 |
| 视频结果（videos.html） | 同上 | 同上 |
| 图像结果（images.html） | ✅ hash 豁免：hash 不依赖 `parsed_url`，但 `parsed_url` 仍为模板必需字段 | hash 成功，但模板消费 `parsed_url.netloc` 时可能异常* |
| 信息盒（infobox） | ✅ 豁免：走独立分支，不经过 `_merge_main_result` | 永不失效 |
| 建议/纠正 | ✅ 豁免：走独立分支 | 永不失效 |

*注：图像结果的 `parsed_url` 如果为 None，虽然 hash 计算不会抛错，但模板第 6 行消费 `result.parsed_url.netloc` 时会抛出 `AttributeError`，该异常同样会被 processor 层捕获为引擎异常。

### 3.3 图像结果 parsed_url 的双重判定修正

**之前的矛盾**：hash 豁免 ≠ 模板字段可缺失

**修正后的精确判定**：

| 层面 | parsed_url 状态 | 结果 |
|------|----------------|------|
| hash 计算层 | None | ✅ 豁免，不抛 ValueError |
| 模板消费层（第 6 行） | None | ❌ `result.parsed_url.netloc` 抛出 AttributeError |
| 最终结果 | None | ❌ 异常传播到 processor，引擎标记为异常 |

**图像结果实际必需字段**：
- `url` → 用于解析 `parsed_url`，缺失或无效则最终异常
- `img_src` → hash 必需，缺失则 hash 抛 ValueError
- `parsed_url` → 模板必需，缺失则模板抛 AttributeError

**图像结果 hash 豁免证据**：`searx/result_types/_base.py:527-530`
```python
if self.template == "images.html":
    # 仅使用 url 和 img_src，不依赖 parsed_url
    return hash(f"{self.template}|{self.url}|{self.img_src}")
```

**但模板仍消费 parsed_url**：`searx/templates/simple/result_templates/images.html:6`
```jinja
<span class="url">{{ result.parsed_url.netloc }}</span>
```

### 3.4 显示占位场景清单

模板中明确使用条件判断，字段为空时显示占位：

#### 图像结果 (images.html)

| 字段 | 模板代码 | 空值表现 |
|------|---------|---------|
| `resolution` | `{% if result.resolution %}<span>{{ result.resolution }}</span>{% endif %}` | 不显示分辨率标签 |
| `content` | `{% if result.content %}{{ result.content|safe }}{% else %}&nbsp;{% endif %}` | 详情面板显示 `&nbsp;` |
| `author` | `{% if result.author %}...{% else %}&nbsp;{% endif %}` | 详情面板显示 `&nbsp;` |
| `resolution`（详情） | `{% if result.resolution %}...{% else %}&nbsp;{% endif %}` | 详情面板显示 `&nbsp;` |
| `img_format` | `{% if result.img_format %}...{% else %}&nbsp;{% endif %}` | 详情面板显示 `&nbsp;` |
| `filesize` | `{% if result.filesize %}...{% else %}&nbsp;{% endif %}` | 详情面板显示 `&nbsp;` |
| `source` | `{% if result.source %}...{% else %}&nbsp;{% endif %}` | 详情面板显示 `&nbsp;` |

#### 视频结果 (videos.html)

| 字段 | 模板代码 | 空值表现 |
|------|---------|---------|
| `publishedDate` / `pubdate` | `result_sub_header` 宏中的条件判断 | 不显示发布日期 |
| `length`（无缩略图时） | `result_sub_header` 宏中的条件判断 | 不显示播放时长 |
| `views` | `result_sub_header` 宏中的条件判断 | 不显示观看次数 |
| `author` | `result_sub_header` 宏中的条件判断 | 不显示上传者 |
| `content` | `{% if result.content %}...{% else %}<p class="content empty_element">...</p>{% endif %}` | 显示 "This site did not provide any description." |

### 3.5 渲染降级场景清单

核心增强字段缺失，使用备用值或隐藏功能：

#### 图像结果

| 字段 | 降级逻辑 | 降级表现 |
|------|---------|---------|
| `thumbnail_src` | `{{ result.thumbnail_src or result.img_src }}` | 使用原图作为缩略图，可能加载较慢 |

#### 视频结果

| 字段 | 降级逻辑 | 降级表现 |
|------|---------|---------|
| `thumbnail` | 宏 `result_header` 中 `{% if result.thumbnail %}...{% endif %}` | 不显示视频缩略图，仅文字链接 |
| `iframe_src` | `{% if result.iframe_src %}<p class="altlink">...show video...</p>{% endif %}` | 不显示内嵌播放按钮，需跳转外部网站 |

---

## 4. 单模板渲染与分组包裹机制

### 4.1 only_template 检测逻辑

**文件位置**：`searx/templates/simple/results.html:15-19`

```jinja
{% if results and results|map(attribute='template')|unique|list|count == 1 %}
  {% set only_template = 'only_template_' + results[0]['template']|default('default')|replace('.html', '') %}
{% else %}
  {% set only_template = '' %}
{% endif %}
```

**触发条件**：结果列表非空，且所有结果的 `template` 字段值完全相同。

### 4.2 跳过分组包裹的逻辑

**文件位置**：`searx/templates/simple/results.html:65-71`

```jinja
<div id="urls" role="main">
{% for result in results %}
    {% if result.open_group and not only_template %}   {# 关键判断 #}
        <div class="template_group_{{ result['template']|replace('.html', '') }}">
    {% endif %}
    {% include get_result_template('simple', result['template']) %}
    {% if result.close_group and not only_template %}  {# 关键判断 #}
        </div>
    {% endif %}
{% endfor %}
</div>
```

**行为**：
- 多模板混合：正常分组，每个模板类型被 `<div class="template_group_*">` 包裹
- 单模板（`only_template` 非空）：**完全跳过**分组 div，每个结果直接渲染

### 4.3 only_template 的 CSS 布局影响

**文件位置**：`client/simple/src/less/style.less:1022-1053`

```less
#main_results div#results.only_template_images {
  display: grid;
  grid-template:
    "corrections" min-content
    "answers" min-content
    "sidebar" min-content
    "urls" 1fr
    "pagination" min-content
    / 100%;
  gap: 0;

  #urls {
    margin: 0;
    display: flex;    /* 图片专用：flex 瀑布流布局 */
    flex-wrap: wrap;
  }
}
```

**布局差异矩阵**：

| 场景 | 布局方式 | #urls 容器样式 |
|------|---------|--------------|
| 混合模板 | 标准双栏网格 | `grid-template-columns: main sidebar` |
| `only_template_images` | 单列网格 + flex 瀑布流 | `display: flex; flex-wrap: wrap` |
| `only_template_videos` | 标准双栏网格 | 与默认相同 |
| 其他单模板 | 标准双栏网格 | 与默认相同 |

---

## 5. 图像结果 (images.html) 字段全对齐

### 5.1 模板实际消费字段（逐行）

**文件位置**：`searx/templates/simple/result_templates/images.html`

| 行号 | 字段 | 用途 | 行为类型 | 必需/可选 | 缺失后果 |
|------|------|------|---------|----------|---------|
| 1 | `result.category` | CSS 类名 | 显示占位 | 可选 | 不显示 category CSS 类 |
| 2 | `result.img_src` | 图片链接目标 | 对象失效* | **必需** | 图片链接失效，hash 计算抛 ValueError |
| 3 | `result.thumbnail_src` | 缩略图 src（优先） | 渲染降级 | 可选 | 使用原图作为缩略图 |
| 3 | `result.img_src` | 缩略图 src（降级） | 对象失效* | **必需** | 同上 |
| 3 | `result.title` | 图片 alt 属性 | 显示占位 | **必需** | 无 alt 属性，可访问性问题 |
| 4 | `result.resolution` | 分辨率标签 | 显示占位 | 可选 | 不显示分辨率标签 |
| 5 | `result.title` | 图片标题显示 | 显示占位 | **必需** | 无标题文本 |
| 6 | `result.parsed_url.netloc` | 来源域名 | 对象失效 | **必需** | 抛 AttributeError，引擎标记异常 |
| 12 | `result.img_src` | 详情面板原图链接 | 对象失效* | **必需** | 详情面板图片失效 |
| 13 | `result.img_src` | 详情面板原图 src | 对象失效* | **必需** | 详情面板无图片 |
| 13 | `result.title` | 详情面板 alt 属性 | 显示占位 | **必需** | 无 alt 属性 |
| 16 | `result.title` | 详情面板标题 | 显示占位 | **必需** | 详情面板无标题 |
| 17 | `result.content` | 详情面板描述 | 显示占位 | 可选 | 详情面板显示 `&nbsp;` |
| 19 | `result.author` | 详情面板作者 | 显示占位 | 可选 | 详情面板显示 `&nbsp;` |
| 20 | `result.resolution` | 详情面板分辨率 | 显示占位 | 可选 | 详情面板显示 `&nbsp;` |
| 21 | `result.img_format` | 详情面板格式 | 显示占位 | 可选 | 详情面板显示 `&nbsp;` |
| 22 | `result.filesize` | 详情面板文件大小 | 显示占位 | 可选 | 详情面板显示 `&nbsp;` |
| 23 | `result.source` | 详情面板来源 | 显示占位 | 可选 | 详情面板显示 `&nbsp;` |
| 24 | `result.engine` | 详情面板引擎 | 显示占位 | **必需** | 无来源引擎信息 |
| 25 | `result.url` | 详情面板来源链接 | 对象失效** | **必需** | 来源链接失效，且用于解析 parsed_url |

*注：`img_src` 缺失会导致 hash 计算抛 ValueError，被 processor 捕获为引擎异常。

**注：`url` 缺失会导致：
1. hash 计算使用空字符串（可能与其他结果碰撞）
2. `parsed_url` 为 None，模板第 6 行抛 AttributeError
3. 最终同样被捕获为引擎异常

### 5.2 必需字段清单（对象可用的最低要求）

| 字段 | 缺失后果 |
|------|---------|
| `img_src` | hash 抛 ValueError → 引擎标记异常 |
| `url` | parsed_url 为 None → 模板抛 AttributeError → 引擎标记异常 |
| `title` | 无标题和 alt 属性，结果可用但用户体验差 |
| `engine` | 无来源引擎信息 |

### 5.3 引擎附带字段与渲染影响

| 字段 | Google Images | Bing Images | 对渲染的影响 |
|------|--------------|-------------|-------------|
| `thumbnail_src` | ✅ | ✅ | 有则加载更快的缩略图，无则用原图 |
| `resolution` | ✅ | ✅ | 显示分辨率标签，用户体验更好 |
| `content` | ✅ | ✅ | 详情面板显示描述 |
| `author` | ✅ | ❌ | 详情面板显示作者 |
| `img_format` | ❌ | ❌ | 详情面板显示格式信息 |
| `filesize` | ❌ | ❌ | 详情面板显示文件大小 |
| `source` | ✅ | ✅ | 详情面板显示来源网站 |

---

## 6. 视频结果 (videos.html) 字段全对齐

### 6.1 宏展开后的完整字段消费链

**文件位置**：`searx/templates/simple/result_templates/videos.html`

```jinja
{% from 'simple/macros.html' import iframe, result_header, result_sub_header, result_sub_footer, result_footer with context %}

{{ result_header(result, favicons, image_proxify) }}
{{ result_sub_header(result) }}
{% if result.iframe_src %}<p class="altlink">... {{ _('show video') }}</a></p>{% endif %}
{% if result.content %}<p class="content">{{ result.content|safe }}</p>
{% else %}<p class="content empty_element">...</p>{% endif %}
{{ result_sub_footer(result) }}
{% if result.iframe_src %}<div id="result-video-{{ index }}" class="embedded-video invisible">{{ iframe(result.iframe_src) }}</div>{% endif %}
{{ result_footer(result) }}
```

### 6.2 各宏的字段消费明细

#### 宏 1: result_header (`searx/templates/simple/macros.html:21-35`)

| 字段 | 用途 | 行为类型 | 必需/可选 | 缺失后果 |
|------|------|---------|----------|---------|
| `result.url` | 标题链接目标 | 对象失效 | **必需** | hash 抛 ValueError → 引擎标记异常 |
| `result.parsed_url` | 域名显示 + favicon | 对象失效 | **必需** | hash 抛 ValueError → 引擎标记异常 |
| `result.thumbnail` | 视频缩略图 | 渲染降级 | 可选 | 不显示缩略图，仅文字链接 |
| `result.length` | 缩略图上的时长标签 | 显示占位 | 可选 | 不显示时长 |
| `result.title` | 结果标题 | 显示占位 | **必需** | 无标题文本 |
| `result.template` | CSS 类名 `result-videos` | 显示占位 | **必需** | 无正确 CSS 类 |
| `result.category` | CSS 类名 `category-*` | 显示占位 | 可选 | 无 category CSS 类 |

#### 宏 2: result_sub_header (`searx/templates/simple/macros.html:38-45`)

| 字段 | 用途 | 行为类型 | 必需/可选 | 缺失后果 |
|------|------|---------|----------|---------|
| `result.publishedDate` / `result.pubdate` | 发布日期 | 显示占位 | 可选 | 不显示发布日期 |
| `result.length` | 播放时长（无缩略图时） | 显示占位 | 可选 | 不显示播放时长 |
| `result.views` | 观看次数 | 显示占位 | 可选 | 不显示观看次数 |
| `result.author` | 上传者 | 显示占位 | 可选 | 不显示上传者 |
| `result.metadata` | 元数据高亮 | 显示占位 | 可选 | 不显示元数据 |

#### 宏 3: result_sub_footer (`searx/templates/simple/macros.html:48-54`)

| 字段 | 用途 | 行为类型 | 必需/可选 | 缺失后果 |
|------|------|---------|----------|---------|
| `result.engines` | 来源引擎列表 | 显示占位 | **必需**（系统填充） | 无来源引擎列表 |
| `result.url` | 缓存链接 | 对象失效 | **必需** | 无缓存链接 |

#### 宏 4: iframe (`searx/templates/simple/macros.html:72-79`)

| 字段 | 用途 | 行为类型 | 必需/可选 | 缺失后果 |
|------|------|---------|----------|---------|
| `result.parsed_url.hostname` | YouTube 特殊权限处理 | 显示占位 | 可选 | 无 YouTube 特殊处理 |

#### 模板自身消费字段

| 字段 | 用途 | 行为类型 | 必需/可选 | 缺失后果 |
|------|------|---------|----------|---------|
| `result.iframe_src` | 嵌入式播放器 URL | 渲染降级 | 可选 | 不显示内嵌播放按钮 |
| `result.content` | 视频描述 | 显示占位 | 可选 | 显示 "This site did not provide any description." |

### 6.3 必需字段清单

**系统保证的字段**（引擎无需关心）：
- `template` - 默认为 `"default.html"`，视频引擎设为 `"videos.html"`
- `engine` - 系统自动填充引擎名
- `engines` - 系统自动维护的引擎集合
- `parsed_url` - 系统从 `url` 自动解析（但解析失败会导致对象失效）

**引擎必需提供**：
- `url` - 视频源页面链接（缺失导致 hash 抛 ValueError）
- `title` - 视频标题

### 6.4 引擎附带字段与渲染影响

| 字段 | Bing Videos | YouTube API | 对渲染的影响 |
|------|------------|-------------|-------------|
| `thumbnail` | ✅ | ✅ | 显示视频缩略图，大幅提升视觉效果 |
| `length` | ✅ | ✅ | 缩略图上显示时长标签 |
| `iframe_src` | ❌ | ✅ | 可直接嵌入播放，无需跳转 |
| `content` | ✅ | ✅ | 视频描述文本 |
| `views` | ❌ | ✅ | 显示观看次数 |
| `author` | ❌ | ✅ | 显示上传者 |
| `publishedDate` | ❌ | ✅ | 显示发布日期 |

### 6.5 渲染行为差异矩阵

| 场景 | 表现 |
|------|------|
| 有 `thumbnail` + `length` | 显示带时长标签的缩略图，视觉效果好 |
| 只有 `thumbnail` | 显示缩略图但无时长 |
| 无 `thumbnail` | 不显示缩略图，仅文字链接 |
| 有 `iframe_src` | 显示 "show video" 按钮，可展开内嵌播放器 |
| 无 `iframe_src` | 不显示播放按钮，需跳转外部网站 |
| 有 `content` | 显示视频描述 |
| 无 `content` | 显示 "This site did not provide any description." |

---

## 7. 结果类型模型层

### 7.1 类型层次结构

**文件位置**：`searx/result_types/_base.py`

```
Result (msgspec.Struct)
├── MainResult (主结果区)
│   ├── template: str = "default.html"
│   ├── title, content, img_src, thumbnail
│   ├── iframe_src, audio_src
│   ├── publishedDate, length, views, author
│   └── 排序字段: engines, score, priority, category
└── BaseAnswer (答案区)
    ├── Answer (简单文本)
    ├── Translations (翻译列表)
    └── WeatherAnswer (天气数据)
```

### 7.2 ResultContainer 核心字段

**文件位置**：`searx/results.py:53-81`

| 字段 | 类型 | 描述 |
|------|------|------|
| `main_results_map` | `dict[int, MainResult \| LegacyResult]` | 主结果（按 hash 去重） |
| `infoboxes` | `list[LegacyResult]` | 信息盒列表 |
| `suggestions` | `set[str]` | 搜索建议 |
| `answers` | `AnswerSet` | 即时答案 |
| `corrections` | `set[str]` | 拼写纠正 |
| `unresponsive_engines` | `set[UnresponsiveEngine]` | 无响应引擎 |
| `timings` | `list[Timing]` | 性能计时 |
| `redirect_url` | `str \| None` | 外部跳转 URL |
| `paging` | `bool` | 是否支持分页 |

### 7.3 传递给模板的完整上下文字段

**文件位置**：`searx/webapp.py:756-784`

| 字段 | 类型 | 来源 | 描述 |
|------|------|------|------|
| `results` | `list` | `ResultContainer.get_ordered_results()` | 排序后的主结果列表 |
| `q` | `str` | `sxng_request.form['q']` | 用户查询字符串 |
| `selected_categories` | `list` | `search_query.categories` | 选中的搜索分类 |
| `pageno` | `int` | `search_query.pageno` | 当前页码 |
| `time_range` | `str` | `search_query.time_range` | 时间范围过滤器 |
| `number_of_results` | `str` | `ResultContainer.number_of_results` | 结果总数（格式化后） |
| `suggestions` | `list[dict]` | `ResultContainer.suggestions` | 搜索建议列表，含 URL |
| `answers` | `AnswerSet` | `ResultContainer.answers` | 即时答案集合 |
| `corrections` | `list[dict]` | `ResultContainer.corrections` | 拼写纠正列表，含 URL |
| `infoboxes` | `list[LegacyResult]` | `ResultContainer.infoboxes` | 信息盒列表 |
| `engine_data` | `dict` | `ResultContainer.engine_data` | 引擎额外数据 |
| `paging` | `bool` | `ResultContainer.paging` | 是否支持分页 |
| `unresponsive_engines` | `list` | `ResultContainer.unresponsive_engines` | 无响应引擎（已翻译） |
| `current_locale` | `str` | 用户偏好 | 当前 UI 区域设置 |
| `current_language` | `str` | 检测/选择的语言 | 当前搜索语言 |
| `search_language` | `str` | 匹配后的语言 | 实际搜索使用的语言 |
| `timeout_limit` | `float \| None` | 请求参数 | 超时限制 |
| `timings` | `list[tuple]` | `ResultContainer.get_timings()` | 引擎响应时间对 |
| `max_response_time` | `float \| None` | 计算值 | 最大响应时间 |

---

## 8. 关键流程总结

```
用户查询
    ↓
SearchQuery 构建
    ↓
pre_search 插件 → 返回 False 则跳过整个 super().search()
    ↓
Search.search() （仅当 pre_search 返回 True 时执行）
    ├─ external_bang 检查 → 命中则设置 redirect_url 并终止
    ├─ answerers 调用 → 命中则短路，不调用 search_standard()
    └─ search_standard() → 并行调用各搜索引擎
        ↓
OnlineProcessor.search() [try 块内]
    ├─ 引擎返回结果列表
    ├─ _extend_container_basic()
    │   └─ ResultContainer.extend()
    │       ├─ normalize_result_fields() → URL/文本/日期规范化
    │       ├─ on_result 插件钩子 → 可修改或丢弃结果
    │       └─ _merge_main_result
    │           └─ hash(result) → 可能抛 ValueError
    └─ 异常被 except Exception 捕获 → handle_exception() → 引擎标记异常
        ↓
post_search 插件（始终执行！）→ 5 个插件可通过 extend() 添加新结果
    ↓
result_container.close() → 计算分数
    ↓
webapp.py 后处理
    ├─ 标题/内容高亮
    ├─ 模板分组标记 (open_group/close_group)
    └─ 建议/纠正 URL 构建
        ↓
render(results.html)
    ↓
only_template 检测
    ├─ 单模板 → 设置 only_template 类，跳过分组 div
    └─ 多模板 → 正常分组包裹
        ↓
模板分发
    ├─ 主结果区 → result_templates/*.html
    ├─ 侧边栏 → elements/infobox.html
    └─ 答案区 → answer/*.html
```

---

## 9. 可复核的判定准则速查表

### 对象失效判定
- ✅ 复核代码：`MainResult.__hash__()` 检查 `parsed_url` 是否为 `None`
- ✅ 复核代码：`_merge_main_result()` 调用 `hash(result)`，无异常捕获
- ✅ 复核代码：`OnlineProcessor.search()` 的 `except Exception as e` 捕获异常
- ✅ 复核代码：`handle_exception()` 调用 `add_unresponsive_engine()` 标记引擎
- ❌ 豁免：图像结果 hash 不依赖 `parsed_url`，但模板仍消费 `parsed_url.netloc`
- ❌ 豁免：信息盒、建议、纠正走独立分支，不经过 `_merge_main_result`

### 显示占位判定
- ✅ 复核代码：模板中 `{% if result.field %}...{% else %}&nbsp;{% endif %}` 模式
- ✅ 复核代码：`_normalize_text_fields()` 强制转换永不失效

### 渲染降级判定
- ✅ 复核代码：模板中 `{{ result.field_a or result.field_b }}` 模式
- ✅ 复核代码：条件渲染整个功能块 `{% if result.field %}...{% endif %}`
- ✅ 复核代码：`_normalize_date_fields()` 异常时降级为 None

### pre_search 短路语义判定
- ✅ 复核代码：`searx/search/__init__.py:203-204` 的 `if` 条件结构
- ✅ 复核代码：`pre_search` 返回 `False` 时，`super().search()` 整行不执行
- ✅ 复核代码：`post_search` 和 `close()` 在 `if` 块之外，始终执行

---

## 10. 扩展阅读

- 结果类型文档：`docs/dev/result_types/`
- 引擎开发文档：`docs/dev/engines/`
- 插件开发文档：`docs/dev/plugins/`
- 模板目录：`searx/templates/simple/`
- 样式目录：`client/simple/src/less/`
- 规范化逻辑：`searx/result_types/_base.py`
- 插件核心：`searx/plugins/_core.py`
- 处理器异常处理：`searx/search/processors/abstract.py:165-191`
- Online 处理器搜索：`searx/search/processors/online.py:239-282`
