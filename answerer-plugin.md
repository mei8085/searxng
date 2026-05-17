# SearXNG 直接答案插件（Answerer / Plugin）完整链路

## 一、体系概述

SearXNG 中存在两套"直接给出答案"的功能通过两种独立的机制实现：

| 机制 | 位置 | 基类 | 示例 |
|------|------|------|------|
| **Answerer** | `searx/answerers/` | `Answerer` | `random.py`, `statistics.py` |
| **Plugin** | `searx/plugins/` | `Plugin` | `calculator.py`, `hash_plugin.py` |

两套机制共享相同的结果类型体系（`BaseAnswer` 及其子类），最终都渲染在同一个答案区域。

---

## 二、查询解析阶段的命中条件

### 2.1 Answerer 命中条件

**代码位置**：`searx/answerers/_core.py:143-164

```python
def ask(self, query: str) -> list[BaseAnswer]:
    keyword = None
    for keyword in query.split():
        if keyword:
            break
    if not keyword or keyword not in self:
        return results
    for answerer in self[keyword]:
        ...
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

**代码位置**：`searx/plugins/_core.py:282-306

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

---

## 三、搜索流程与优先级关系

### 3.1 整体调用顺序

**代码位置**：`searx/search/__init__.py:174-179, 201-209

```python
# Search.search() 流程：
def search(self) -> ResultContainer:
    self.start_time = default_timer()
    if not self.search_external_bang():      # 1. External Bang
        if not self.search_answerers():  # 2. Answerers
            self.search_standard()            # 3. 标准搜索引擎
    return self.result_container

# SearchWithPlugins.search() 扩展：
def search(self) -> ResultContainer:
    if searx.plugins.STORAGE.pre_search(self.request, self):
        super().search()                    # 调用上面的 Search.search()
    searx.plugins.STORAGE.post_search(self.request, self)  # 4. Plugin post_search
    self.result_container.close()
    return self.result_container
```

**优先级从高到低**：

| 阶段 | 组件 | 优先级 | 说明 |
|------|------|--------|------|
| 1 | External Bang | 最高 | 命中后直接跳转，终止整个搜索流程 |
| 2 | Answerer | 高 | 命中后**跳过标准搜索**，不调用搜索引擎 |
| 3 | 标准搜索引擎 | 中 | Answerer 未命中时执行 |
| 4 | Plugin post_search | 低 | **总是**在标准搜索后执行 |

**关键行为**：
- Answerer 命中 → 跳过 `search_standard()`，不会发起网络请求
- Plugin post_search **总是**执行，无论是否有 Answerer 结果

---

## 四、返回结构与结果类型

### 4.1 结果类型体系

**代码位置**：`searx/result_types/answer.py

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

### 4.2 结果去重与存储

**代码位置**：`searx/results.py:62, 99-100

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

---

## 五、界面模板与渲染

### 5.1 模板层级结构

**渲染入口**：`searx/templates/simple/results.html:23-25

```html
{%- if answers -%}
  {%- include 'simple/elements/answers.html' -%}
{%- endif %}
```

**答案区域模板**：`searx/templates/simple/elements/answers.html

```html
<div id="answers" role="complementary" aria-labelledby="answers-title">
  <h4 class="title" id="answers-title">{{ _('Answers') }} : </h4>
  {%- for answer in answers -%}
    <div class="answer">
      {%- include ("simple/" + (answer.template or "answer/legacy.html") -%}
    </div>
  {%- endfor -%}
</div>
```

### 5.2 子模板示例

**legacy.html**（默认模板，适用于简单文本答案：
```html
<span>{{ answer.answer }}</span>
{%- if answer.url -%}
  <a href="{{ answer.url }}" class="answer-url">...</a>
{% endif -%}
```

### 5.3 渲染位置

答案区域在搜索结果页面的**最上方**，位于：
1. 侧边栏（infoboxes、suggestions）之前
2. 主搜索结果列表（urls）之前

---

## 六、结果拼接完整链路

### 6.1 Answerer 结果链路

```
查询 "random string"
    ↓
SearchWithPlugins.search()
    ↓
pre_search() 钩子
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
post_search() 钩子（但不影响
    ↓
result_container.close()
    ↓
模板渲染：answers.html → 按 template 渲染
```

### 6.2 Plugin 结果链路

```
查询 "sha256 hello world"
    ↓
SearchWithPlugins.search()
    ↓
pre_search() 钩子
    ↓
search_external_bang() → 无命中
    ↓
search_answerers() → 无命中（没有 answerer 匹配 "sha256"）
    ↓
search_standard() → 调用搜索引擎
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

### 6.3 两者共存场景

当查询同时命中 Answerer 和 Plugin 时：
- Answerer 结果先加入 answers 集合
- Plugin 结果后加入 answers 集合
- 两者都会被渲染，按 template 排序显示

---

## 七、开发建议

### 7.1 选择 Answerer 还是 Plugin？

**使用 Answerer 当：
- 答案可以纯离线计算，无需网络请求
- 希望命中后应该终止搜索引擎查询（节省资源）
- 答案是"确定的、计算型的

**使用 Plugin 当：
- 需要在搜索引擎结果基础上补充答案
- 答案需要结合搜索结果

### 7.2 结果构造示例

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
