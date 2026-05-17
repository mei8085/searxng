# SearXNG 直接答案插件（Answerer / Plugin / Offline Processor）完整链路

## 一、体系概述

SearXNG 中存在三套"直接给出答案"的机制：

| 机制 | 位置 | 基类 | 示例 | 特点 |
|------|------|------|------|------|
| **Answerer** | `searx/answerers/` | `Answerer` | `random.py`, `statistics.py` | 纯离线计算，命中后跳过搜索 |
| **Plugin** | `searx/plugins/` | `Plugin` | `calculator.py`, `hash_plugin.py` | 后置处理，总是执行 |
| **Offline Processor** | `searx/search/processors/offline.py` | `OfflineProcessor` | `demo_offline.py`, `sqlite.py` | 作为特殊引擎类型，参与标准搜索流程 |

三套机制共享相同的结果类型体系（`BaseAnswer` 及其子类），最终都渲染在同一个答案区域。

---

## 二、查询解析阶段的命中条件

### 2.1 Answerer 命中条件

**代码位置**：`searx/answerers/_core.py:143-164`

```python
def ask(self, query: str) -> list[BaseAnswer]:
    keyword = None
    for keyword in query.split():
        if keyword:
            break
    if not keyword or keyword not in self:
        return results
    for answerer in self[keyword]:
        for answer in answerer.answer(query):
            answer.engine = f"answerer: {keyword}"
            results.append(answer)
    return results
```

**命中规则**：
- 取查询字符串的**第一个非空单词**作为关键字
- 该关键字必须存在于 `Answerer.keywords` 列表中
- 匹配成功后调用 `answerer.answer(query)` 处理整个查询

**示例**（random answerer）：
```python
class SXNGAnswerer(Answerer):
    keywords = ["random"]  # 只有第一个词是 random 才命中
```

### 2.2 Plugin 命中条件

**代码位置**：`searx/plugins/_core.py:282-306`

```python
def post_search(self, request: SXNG_Request, search: "SearchWithPlugins") -> None:
    keyword = None
    for keyword in search.search_query.query.split():
        if keyword:
            break
    for plugin in [p for p in self.plugin_list if p.id in search.user_plugins]:
        if plugin.keywords:
            if keyword and keyword not in plugin.keywords:
                continue
        results = plugin.post_search(request=request, search=search) or []
        search.result_container.extend(f"plugin: {plugin.id}", results)
```

**命中规则**：
- 取查询字符串的**第一个非空单词**作为关键字
- 如果插件定义了 `Plugin.keywords`，则关键字必须在列表中
- 如果插件**没有**定义 `keywords`，则对所有查询都执行
- 插件必须在用户启用列表中（`plugin.id in search.user_plugins`）

**示例**（hash plugin）：
```python
class SXNGPlugin(Plugin):
    keywords = ["md5", "sha1", "sha224", "sha256", "sha384", "sha512"]
```

**示例**（calculator plugin）：
```python
class SXNGPlugin(Plugin):
    id = "calculator"
    # 没有定义 keywords，对所有查询执行（内部再做二次判断）
```

### 2.3 Offline Processor 命中条件

**代码位置**：`searx/search/processors/offline.py:11-31`

```python
class OfflineProcessor(EngineProcessor):
    """Processor class used by ``offline`` engines."""
    engine_type: str = "offline"

    def search(
        self,
        query: str,
        params: RequestParams,
        result_container: "ResultContainer",
        start_time: float,
        timeout_limit: float,
    ):
        try:
            search_results = self.engine.search(query, params)
            self.extend_container(result_container, start_time, search_results)
        except Exception as e:
            self.handle_exception(result_container, e)
```

**命中规则**：
- 不是基于关键字匹配，而是基于**引擎配置**
- 引擎定义 `engine_type = "offline"`
- 该引擎在用户选择的分类和引擎列表中
- 在 `search_standard()` 阶段与 online 引擎**并行**执行

**示例**（demo_offline engine）：
```python
engine_type = "offline"
categories = ["general"]

def search(query: str, params: "RequestParams") -> EngineResults:
    res = EngineResults()
    # 本地计算，不发起网络请求
    ...
    return res
```

---

## 三、搜索流程与优先级关系

### 3.1 整体调用顺序

**代码位置**：`searx/search/__init__.py:174-179, 201-209`

```python
# Search.search() 流程：
def search(self) -> ResultContainer:
    self.start_time = default_timer()
    if not self.search_external_bang():      # 1. External Bang
        if not self.search_answerers():  # 2. Answerers
            self.search_standard()            # 3. 标准搜索（含 Offline Processor）
    return self.result_container

# SearchWithPlugins.search() 扩展：
def search(self) -> ResultContainer:
    if searx.plugins.STORAGE.pre_search(self.request, self):  # 0. Plugin pre_search
        super().search()                                          # 调用上面的 Search.search()
    searx.plugins.STORAGE.post_search(self.request, self)     # 4. Plugin post_search
    self.result_container.close()
    return self.result_container
```

### 3.2 search_standard 内部流程

**代码位置**：`searx/search/__init__.py:160-171`

```python
def search_standard(self):
    requests, self.actual_timeout = self._get_requests()  # 收集所有引擎请求（包括 offline）
    if requests:
        self.search_multiple_requests(requests)              # 多线程并行执行所有 processor
    return True
```

`search_multiple_requests` 会为每个引擎启动一个线程，调用 `PROCESSORS[engine_name].search()`，
`PROCESSORS` 根据 `engine_type` 选择对应的处理器（`OfflineProcessor` 或 `OnlineProcessor` 等）。

### 3.3 优先级总表（从高到低）

| 阶段 | 组件 | 优先级 | 说明 |
|------|------|--------|------|
| 0 | Plugin pre_search | 最先 | 返回 false 则终止整个搜索 |
| 1 | External Bang | 最高 | 命中后直接跳转，终止整个搜索流程 |
| 2 | Answerer | 高 | 命中后**跳过标准搜索**，不调用任何引擎（包括 offline） |
| 3 | Offline Processor | 中 | 作为标准搜索的一部分，与 online 引擎并行执行 |
| 4 | Online Processor | 中 | 标准网络搜索 |
| 5 | Plugin post_search | 最低 | **总是**在所有搜索后执行 |

**关键行为**：
- `pre_search` 返回 `False` → 终止整个流程，直接跳到 `post_search`
- Answerer 命中 → 跳过 `search_standard()`，Offline Processor **不会**执行
- Plugin `post_search` **总是**执行，无论前面发生了什么

### 3.4 三分支执行情况对照表

| 分支场景 | pre_search 结果 | search_standard 执行 | Offline Processor 执行 | post_search 执行 | 网络请求 |
|----------|----------------|---------------------|------------------------|-----------------|----------|
| **pre_search 返回 False** | False | ❌ 不执行 | ❌ 不执行 | ✅ 总是执行 | ❌ 无 |
| **Answerer 命中** | True | ❌ 不执行 | ❌ 不执行 | ✅ 总是执行 | ❌ 无 |
| **Answerer 未命中（正常流程）** | True | ✅ 执行 | ✅ 并行执行 | ✅ 总是执行 | ✅ 有（online 引擎） |

---

## 四、Plugin Hook 执行时序详解

### 4.1 完整执行流图

```
SearchWithPlugins.search()
    │
    ├─ pre_search()
    │   ├─ 遍历所有启用的 plugin
    │   ├─ 只要有一个 plugin 返回 False → 停止，pre_search 整体返回 False
    │   └─ 全部返回 True → pre_search 整体返回 True
    │
    ├─ pre_search 返回 False?
    │   ├─ 是 → 跳过 super().search()，直接进入 post_search
    │   └─ 否 → 执行 super().search()
    │           ├─ search_external_bang() → 命中则返回
    │           ├─ search_answerers() → 命中则跳过 search_standard
    │           └─ search_standard() → 未命中 answerer 时执行
    │               ├─ _get_requests() → 收集所有引擎（含 offline）
    │               └─ search_multiple_requests() → 并行执行所有 processor
    │
    └─ post_search()
        ├─ 遍历所有启用的 plugin
        ├─ 每个 plugin 的 post_search 都被调用
        └─ 返回的结果被添加到 result_container
```

### 4.2 分支 1：pre_search 返回 False

**代码位置**：`searx/plugins/_core.py:253-265`

```python
def pre_search(self, request: SXNG_Request, search: "SearchWithPlugins") -> bool:
    ret = True
    for plugin in [p for p in self.plugin_list if p.id in search.user_plugins]:
        try:
            ret = bool(plugin.pre_search(request=request, search=search))
        except Exception:
            plugin.log.exception("Exception while calling pre_search")
            continue
        if not ret:
            break  # 第一个 False 就终止
    return ret
```

**执行流程**：
```
查询 "test query"
    ↓
SearchWithPlugins.search()
    ↓
pre_search() 钩子
    ├─ plugin_A.pre_search() → 返回 True
    ├─ plugin_B.pre_search() → 返回 False
    └─ pre_search 整体返回 False
    ↓
跳过 super().search()
    ↓
post_search() 钩子（仍会执行！）
    ├─ plugin_A.post_search()
    └─ plugin_B.post_search()
    ↓
result_container.close()
    ↓
返回结果（可能为空）
```

**关键点**：
- pre_search 返回 False 只跳过 `super().search()`，**不跳过** post_search
- post_search 中的插件仍可以添加结果
- 整个流程中不会发起任何网络请求，也不会调用 answerer

### 4.3 分支 2：Answerer 命中

**执行流程**：
```
查询 "random string"
    ↓
SearchWithPlugins.search()
    ↓
pre_search() 钩子 → 全部返回 True
    ↓
super().search()
    ├─ search_external_bang() → 无命中
    ├─ search_answerers()
    │   ├─ 取第一个词 "random"
    │   ├─ 匹配 Answerer.keywords = ["random"]
    │   ├─ 调用 random.answer("random string")
    │   ├─ 返回 [Answer(answer="a1b2c3...")]
    │   ├─ result_container.extend(None, results)
    │   └─ 返回 True → 跳过 search_standard()
    └─ search_standard() → 被跳过！
        └─ Offline Processor → 不执行！
        └─ Online Processor → 不执行！
    ↓
post_search() 钩子（仍会执行！）
    ├─ 所有启用的 plugin 都调用 post_search
    └─ plugin 可以继续添加结果到 answers 集合
    ↓
result_container.close()
    ↓
模板渲染：answers.html → 渲染所有 Answer
```

**关键点**：
- Answerer 命中会跳过 `search_standard()`，Offline Processor **不会**执行
- post_search **仍会执行**，插件可以继续补充结果
- 整个流程中不会发起任何网络请求

### 4.4 分支 3：正常搜索流程（无 Answerer 命中）

**执行流程**：
```
查询 "sha256 hello world"
    ↓
SearchWithPlugins.search()
    ↓
pre_search() 钩子 → 全部返回 True
    ↓
super().search()
    ├─ search_external_bang() → 无命中
    ├─ search_answerers() → 无命中（没有 answerer 匹配 "sha256"）
    └─ search_standard()
        ├─ _get_requests() → 收集所有选中的引擎
        │   ├─ online 引擎（google, bing, ...）
        │   └─ offline 引擎（如果配置了）
        └─ search_multiple_requests() → 多线程并行执行
            ├─ OnlineProcessor.search() → 网络请求
            ├─ OfflineProcessor.search() → 本地计算（如果有）
            └─ 结果都添加到 result_container
    ↓
post_search() 钩子
    ├─ 取第一个词 "sha256"
    ├─ 遍历启用的 plugins
    │   ├─ hash_plugin: keywords 包含 "sha256" → 命中
    │   │   ├─ 调用 hash_plugin.post_search()
    │   │   └─ 添加 Answer 到结果集
    │   └─ 其他插件按 keywords 匹配
    └─ 结果合并到 result_container
    ↓
result_container.close()
    ↓
模板渲染：answers.html + 主搜索结果
```

---

## 五、返回结构与结果类型

### 5.1 结果类型体系

**代码位置**：`searx/result_types/answer.py`

所有直接答案都继承自 `BaseAnswer`：

```python
class BaseAnswer(Result, kw_only=True):
    """所有答案类型的基类"""

class Answer(BaseAnswer, kw_only=True):
    template: str = "answer/legacy.html"  # 指定渲染模板
    answer: str                           # 答案文本

class Translations(BaseAnswer, kw_only=True):
    template: str = "answer/translations.html"
    translations: list[Translations.Item]

class WeatherAnswer(BaseAnswer, kw_only=True):
    template: str = "answer/weather.html"
    current: WeatherAnswer.Item
```

### 5.2 结果去重与存储

**代码位置**：`searx/results.py:62, 99-100`

```python
class ResultContainer:
    answers: AnswerSet

    def extend(self, engine_name: str | None, results: list[Result | LegacyResult]):
        for result in list(results):
            if isinstance(result, BaseAnswer):
                self.answers.add(result)  # 加入 AnswerSet，自动去重
```

**AnswerSet 去重机制**：
```python
class AnswerSet:
    def add(self, answer: BaseAnswer) -> None:
        a_hash = hash(answer)
        for i in self._answerlist:
            if hash(i) == a_hash:
                return  # 重复则跳过
        self._answerlist.append(answer)
```

### 5.3 不同来源的结果标记

| 来源 | engine 字段标记 | 代码位置 |
|------|----------------|----------|
| Answerer | `answerer: {keyword}` | `searx/answerers/_core.py:161` |
| Plugin | `plugin: {plugin_id}` | `searx/plugins/_core.py:305` |
| Offline Engine | 引擎配置的 `name` | `searx/results.py:94` |
| Online Engine | 引擎配置的 `name` | `searx/results.py:94` |

---

## 六、界面模板与渲染

### 6.1 模板层级结构

**渲染入口**：`searx/templates/simple/results.html:23-25`

```html
{%- if answers -%}
  {%- include 'simple/elements/answers.html' -%}
{%- endif %}
```

**答案区域模板**：`searx/templates/simple/elements/answers.html`

```html
<div id="answers" role="complementary" aria-labelledby="answers-title">
  <h4 class="title" id="answers-title">{{ _('Answers') }} : </h4>
  {%- for answer in answers -%}
    <div class="answer">
      {%- include ("simple/" + (answer.template or "answer/legacy.html")) -%}
    </div>
  {%- endfor -%}
</div>
```

### 6.2 子模板示例

**legacy.html**（默认模板，适用于简单文本答案）：
```html
<span>{{ answer.answer }}</span>
{%- if answer.url -%}
  <a href="{{ answer.url }}" class="answer-url">...</a>
{% endif -%}
```

### 6.3 渲染位置

答案区域在搜索结果页面的**最上方**，位于：
1. 侧边栏（infoboxes、suggestions）之前
2. 主搜索结果列表（urls）之前

---

## 七、结果拼接完整链路

### 7.1 Answerer 结果链路

```
查询 "random string"
    ↓
SearchWithPlugins.search()
    ↓
pre_search() 钩子 → 全部返回 True
    ↓
search_external_bang() → 无命中
    ↓
search_answerers()
    ├─ 取第一个词 "random"
    ├─ 匹配 Answerer.keywords = ["random"]
    ├─ 调用 random.answer("random string")
    │   └─ 返回 [Answer(answer="a1b2c3...")]
    ├─ result_container.extend(None, results)
    │   └─ 加入 answers 集合
    └─ 返回 True → 跳过 search_standard()
    ↓
post_search() 钩子（不影响已有结果，但可以追加）
    ↓
result_container.close()
    ↓
模板渲染：answers.html → 按 template 渲染
```

### 7.2 Plugin 结果链路

```
查询 "sha256 hello world"
    ↓
SearchWithPlugins.search()
    ↓
pre_search() 钩子 → 全部返回 True
    ↓
search_external_bang() → 无命中
    ↓
search_answerers() → 无命中（没有 answerer 匹配 "sha256"）
    ↓
search_standard() → 调用所有选中的引擎
    ├─ Online Processor → 网络搜索结果
    └─ Offline Processor → 本地计算结果（如果配置了）
    ↓
post_search() 钩子
    ├─ 取第一个词 "sha256"
    ├─ 遍历启用的 plugins
    │   ├─ hash_plugin: keywords = ["md5", "sha1", ..., "sha256", ...]
    │   ├─ "sha256" 在 keywords 中 → 命中
    │   ├─ 调用 hash_plugin.post_search()
    │   │   └─ 返回 EngineResults()
    │   │      └─ Answer(answer="sha256 hash digest: b94d27b9...")
    │   └─ result_container.extend("plugin: hash_plugin", results)
    │       └─ 加入 answers 集合
    └─ 其他插件无命中则跳过
    ↓
result_container.close()
    ↓
模板渲染：answers.html → 按 template 渲染
```

### 7.3 Offline Processor 结果链路

```
查询 "test query"（已配置 demo_offline 引擎）
    ↓
SearchWithPlugins.search()
    ↓
pre_search() 钩子 → 全部返回 True
    ↓
search_external_bang() → 无命中
    ↓
search_answerers() → 无命中
    ↓
search_standard()
    ├─ _get_requests()
    │   └─ 收集到 demo_offline 引擎（engine_type = "offline"）
    └─ search_multiple_requests()
        └─ OfflineProcessor.search()
            ├─ 调用 demo_offline.search(query, params)
            ├─ 返回 EngineResults（可能包含 Answer 或其他结果类型）
            └─ extend_container() → 结果加入 result_container
    ↓
post_search() 钩子
    ↓
result_container.close()
    ↓
模板渲染：如果是 Answer 类型 → 渲染在 answers.html
         如果是其他类型 → 渲染在对应位置
```

### 7.4 多来源共存场景

当查询同时产生多个来源的答案时：
1. Answerer 结果先加入 answers 集合
2. Offline Processor 结果在 search_standard 中加入
3. Plugin 结果在 post_search 中最后加入
4. 所有结果按 template 字段排序后渲染

**最终 answers 集合的顺序**：按 `answer.template` 字符串排序，与加入顺序无关。

---

## 八、开发建议

### 8.1 选择 Answerer、Plugin 还是 Offline Engine？

**使用 Answerer 当：**
- 答案可以纯离线计算，无需网络请求
- 希望命中后**终止**搜索引擎查询（节省资源）
- 答案是"确定的、计算型的"
- 不需要用户手动启用（始终可用）

**使用 Plugin 当：**
- 需要在搜索引擎结果基础上补充答案
- 答案需要结合搜索结果
- 需要用户可配置启用/禁用
- 希望无论是否有 answerer 结果都能执行

**使用 Offline Engine 当：**
- 作为引擎体系的一部分，需要分类管理
- 需要与 online 引擎并行执行
- 需要利用引擎的缓存、超时等基础设施
- 用户可以像选择普通引擎一样选择是否启用

### 8.2 结果构造示例

**Answerer 示例**：
```python
from searx.answerers import Answerer, AnswererInfo
from searx.result_types import Answer

class MyAnswerer(Answerer):
    keywords = ["calc"]
    
    def answer(self, query: str) -> list[BaseAnswer]:
        # 解析 query，计算结果
        return [Answer(answer="结果")]
```

**Plugin 示例**：
```python
from searx.plugins import Plugin, PluginInfo
from searx.result_types import Answer, EngineResults

class MyPlugin(Plugin):
    keywords = ["mycmd"]
    
    def post_search(self, request, search):
        results = EngineResults()
        results.add(results.types.Answer(answer="结果"))
        return results
```

**Offline Engine 示例**：
```python
engine_type = "offline"
categories = ["general"]

def search(query: str, params: RequestParams) -> EngineResults:
    res = EngineResults()
    res.add(res.types.Answer(answer="本地计算结果"))
    return res
```
