# Answerer 执行路径分析

本文档严格按照 SearXNG 代码契约说明本地 answerer 的工作机制，包括注册形态、命中判定、短路逻辑、缓存边界、插件执行顺序，以及结果展示规则。

## 一、注册形态

### 1.1 核心类结构

Answerer 体系基于抽象基类 `Answerer` 构建，定义在 `searx/answerers/_core.py:43-55`：

```python
class Answerer(abc.ABC):
    keywords: list[str]
    
    @abc.abstractmethod
    def answer(self, query: str) -> list[BaseAnswer]:
        ...
    
    @abc.abstractmethod
    def info(self) -> AnswererInfo:
        ...
```

**接口契约**：
- `answer()` 方法返回值类型声明为 `list[BaseAnswer]`
- 按照代码契约，answerer 应仅返回 `BaseAnswer` 及其子类（如 `Answer`、`Translations`、`WeatherAnswer`）

### 1.2 注册方式

Answerer 通过 `AnswerStorage` 单例进行管理，注册流程如下：

1. **自动加载**：在 `searx/answerers/__init__.py:47-48`，模块加载时自动执行：
   ```python
   STORAGE: AnswerStorage = AnswerStorage()
   STORAGE.load_builtins()
   ```

2. **内置 Answerer 发现**：`AnswerStorage.load_builtins()` (`searx/answerers/_core.py:99-119`) 遍历 `searx/answerers/` 目录：
   - 对每个 `.py` 文件（非下划线开头），尝试注册 `searx.answerers.{name}.SXNGAnswerer` 类
   - 兼容旧模式：如果是目录且包含 `answerer.py`，用 `ModuleAnswerer` 包装后注册

3. **注册存储**：`AnswerStorage.register()` (`searx/answerers/_core.py:135-141`) 将 answerer 按关键词索引：
   ```python
   def register(self, answerer: Answerer):
       self.answerer_list.add(answerer)
       for _kw in answerer.keywords:
           self[_kw] = self.get(_kw, [])
           self[_kw].append(answerer)
   ```

### 1.3 标准实现示例

以 `random.py` 为例 (`searx/answerers/random.py:50-79`)：

```python
class SXNGAnswerer(Answerer):
    keywords = ["random"]
    
    def info(self):
        return AnswererInfo(
            name=gettext("Random value generator"),
            description=gettext("Generate different random values"),
            keywords=self.keywords,
            examples=[f"random {x}" for x in self.random_types],
        )
    
    def answer(self, query: str) -> list[BaseAnswer]:
        parts = query.split()
        if len(parts) != 2 or parts[1] not in self.random_types:
            return []
        return [Answer(answer=self.random_types[parts[1]]())]
```

## 二、命中判定与短路逻辑

### 2.1 判定流程

命中判定发生在 `AnswerStorage.ask()` 方法中 (`searx/answerers/_core.py:143-164`)：

```python
def ask(self, query: str) -> list[BaseAnswer]:
    results = []
    keyword = None
    
    # 提取查询的第一个词作为关键词
    for keyword in query.split():
        if keyword:
            break
    
    # 检查关键词是否在注册的 answerer 中
    if not keyword or keyword not in self:
        return results
    
    # 调用所有匹配的 answerer
    for answerer in self[keyword]:
        for answer in answerer.answer(query):
            answer.engine = f"answerer: {keyword}"
            results.append(answer)
    
    return results
```

### 2.2 判定规则

1. **关键词匹配**：仅检查查询字符串的**第一个词**是否与注册的 `keywords` 匹配
2. **多 Answerer 支持**：同一关键词可注册多个 answerer，全部按顺序执行
3. **空结果过滤**：answerer 返回空列表表示未命中，不影响其他 answerer
4. **Engine 标记**：命中的 answer 会被标记 `engine = "answerer: {keyword}"`

### 2.3 核心短路逻辑

**命中 answerer 后，常规搜索引擎检索会被完全跳过**。

搜索入口在 `Search.search()` (`searx/search/__init__.py:174-179`)：

```python
def search(self) -> ResultContainer:
    self.start_time = default_timer()
    if not self.search_external_bang():
        if not self.search_answerers():  # 只要有结果就返回 True
            self.search_standard()       # answerer 命中则这行不会执行
    return self.result_container
```

**执行顺序与短路规则**：

| 阶段 | 方法 | 说明 | 短路逻辑 |
|------|------|------|----------|
| 1 | `search_external_bang()` | 检查外部 bang 重定向 | 返回 `True` 则终止后续流程 |
| 2 | `search_answerers()` | 调用本地 answerer | 返回 `True`（有结果）则**跳过标准搜索** |
| 3 | `search_standard()` | 传统搜索引擎检索 | 仅当前面都未命中时执行 |

> **标准契约结论**：默认情况下，answerer 命中后不会发起任何网络请求到外部搜索引擎，也不会有传统搜索结果。answerer 结果与传统检索结果是**互斥**的，不存在并行合并。

## 三、插件执行顺序

### 3.1 插件包装层

插件执行通过 `SearchWithPlugins.search()` (`searx/search/__init__.py:201-209`) 包装：

```python
def search(self) -> ResultContainer:
    if searx.plugins.STORAGE.pre_search(self.request, self):
        super().search()  # 包含 answerer 流程
    
    searx.plugins.STORAGE.post_search(self.request, self)
    self.result_container.close()
    return self.result_container
```

### 3.2 插件与 Answerer 时序

```
pre_search 插件钩子
    ↓
search_external_bang()
    ↓
search_answerers() → Answerer.answer()
    ↓ (未命中才执行)
search_standard()
    ↓
post_search 插件钩子
    ↓
result_container.close()
```

关键点（代码契约）：
- `pre_search` 在 answerer 之前执行，可通过返回 `False` 阻止整个搜索
- `on_result` 钩子在 answerer 结果添加到容器时被调用
- `post_search` 在所有搜索完成后执行，**是框架契约内唯一可追加结果的扩展点**

## 四、缓存边界

### 4.1 主搜索链路无缓存

**SearXNG 的主搜索流程没有内置的查询结果缓存**。每次用户发起搜索：
1. 不会检查是否有相同查询的历史结果
2. 不会缓存本次搜索结果供后续使用
3. `search()` 方法每次都会完整执行判定逻辑

### 4.2 Answerer 局部缓存

Answerer 只能在各自的实现内部自主使用缓存，例如：

1. **天气 answerer**：内部使用 `WEATHER_DATA_CACHE` 缓存地理位置查询结果 (`searx/weather.py:181-188`)
2. **其他 answerer**：可根据需要自行引入缓存机制

**Answerer 内部缓存时序**：
```
Answerer.answer(query)
    ↓
[可选] 检查内部缓存
    ↓ (未命中)
执行业务逻辑
    ↓
[可选] 写入内部缓存
    ↓
返回 Answer 结果
```

### 4.3 其他缓存场景

1. **引擎级缓存**：部分在线引擎可能有自己的缓存机制（与 answerer 无关）
2. **HTTP 请求头**：在线处理器设置 `Cache-Control: no-cache` (`searx/search/processors/online.py:145`)，避免 HTTP 层缓存

> **缓存边界结论**：缓存是 answerer 内部实现细节，主搜索链路不感知、不干预。不同 answerer 之间缓存独立，互不影响。

## 五、结果展示与契约边界

### 5.1 结果添加流程

Answerer 结果通过 `search_answerers()` 添加到结果容器 (`searx/search/__init__.py:71-75`)：

```python
def search_answerers(self):
    results = searx.answerers.STORAGE.ask(self.search_query.query)
    self.result_container.extend(None, results)
    return bool(results)
```

### 5.2 结果分类存储

在 `ResultContainer.extend()` 中 (`searx/results.py:83-152`)，根据结果类型分发：

```python
for result in list(results):
    if isinstance(result, Result):
        result.engine = result.engine or engine_name
        result.normalize_result_fields()
        
        if not self.on_result(result):  # 调用插件 on_result 钩子
            continue
        
        if isinstance(result, BaseAnswer):
            self.answers.add(result)  # Answer 结果进入 answers 集合
        elif isinstance(result, MainResult):
            self._merge_main_result(result, main_count)
```

**契约边界**：
- Answer 结果被 `on_result` 插件钩子过滤
- 命中的 Answer 存入 `self.answers`（`AnswerSet` 类型），而非主结果列表
- `AnswerSet` 自动去重（基于 hash）并按 template 排序

### 5.3 AnswerSet 机制

`AnswerSet` 定义在 `searx/result_types/answer.py:46-75`：

```python
class AnswerSet:
    def add(self, answer: BaseAnswer) -> None:
        a_hash = hash(answer)
        for i in self._answerlist:
            if hash(i) == a_hash:
                return  # 去重
        self._answerlist.append(answer)
    
    def __iter__(self):
        self._answerlist.sort(key=lambda answer: answer.template)
        yield from self._answerlist
```

### 5.4 展示逻辑

在 web 应用的 `search()` 视图中 (`searx/webapp.py:620-729`)：

1. 主结果通过 `result_container.get_ordered_results()` 获取
2. Answer 结果通过 `result_container.answers` 单独访问
3. 模板中分别渲染：
   - Answer：`answers.html` 模板（位于搜索结果顶部）
   - 主结果：`results.html` 模板

**HTML 输出结构**：
```html
<!-- Answer 区域（顶部） -->
<div class="answers">
    {% for answer in result_container.answers %}
        {% include get_result_template(theme, answer.template) %}
    {% endfor %}
</div>

<!-- 传统搜索结果 -->
<div class="results">
    {% for result in results %}
        {% include get_result_template(theme, result.template) %}
    {% endfor %}
</div>
```

### 5.5 契约内并存场景：插件追加

按照代码契约，**唯一标准的扩展路径**是通过 `SearchWithPlugins` 的 `post_search` 钩子追加结果：

| 路径类型 | 场景 | 触发方式 | 契约合规性 |
|----------|------|----------|------------|
| **标准路径** | answerer 命中 → 仅显示 answers | 默认流程 | ✅ 标准 |
| **扩展路径** | answerer 命中 + post_search 插件追加 MainResult | 自定义插件实现 `post_search()` 方法，调用 `result_container.extend()` 追加结果 | ✅ 框架契约内 |
| **非标准路径** | answerer 直接返回 MainResult 类型 | answerer 的 `answer()` 方法违反接口声明，返回非 `BaseAnswer` 类型 | ⚠️ 不符合接口契约，不保证兼容 |

> **设计意图**：SearXNG 设计 answerer 短路机制是为了性能优化。如果业务需要同时展示 answer 和传统搜索结果，**必须通过插件机制在 `post_search` 中实现**，这是框架契约内唯一支持的方式。

## 六、完整执行路径图（按契约分层）

### 6.1 标准路径（Answerer 命中 → 短路 → 仅 Answers）

```
用户查询
   ↓
webapp.search() 视图
   ↓
SearchWithPlugins.search()
   ↓
plugins.pre_search() ──(返回False)──→ 终止搜索
   ↓
Search.search()
   ├─→ search_external_bang() ──(命中)──→ 重定向
   │     ↓(未命中)
   └─→ search_answerers()
         ├─→ AnswerStorage.ask(query)
         │     ├─ 提取第一个关键词
         │     └─ 调用匹配的 Answerer.answer()  [返回 list[BaseAnswer]]
         ├─→ ResultContainer.extend(None, results)
         │     ├─ on_result 插件过滤
         │     └─ 存入 answers 集合（去重）
         └─(有结果)──→ 短路：跳过 search_standard()
   ↓
plugins.post_search()  [标准路径下不追加结果]
   ↓
result_container.close()
   ↓
模板渲染
   └─ 仅显示 answers 区域（顶部）
```

### 6.2 扩展路径（Answerer 命中 + post_search 插件追加）

```
用户查询
   ↓
webapp.search() 视图
   ↓
SearchWithPlugins.search()
   ↓
plugins.pre_search()
   ↓
Search.search()
   └─→ search_answerers() 命中
         └─→ 存入 answers 集合  [仅 BaseAnswer 类型]
   ↓
plugins.post_search()
   └─→ 插件主动调用 result_container.extend(engine_name, [MainResult...])
         └─→ 主结果存入 main_results_map
   ↓
result_container.close()
   ↓
模板渲染
   ├─ answers 区域（顶部）
   └─ 主结果列表（插件追加的结果）
```

### 6.3 非标准路径（Answerer 违反接口契约）

```
用户查询
   ↓
SearchWithPlugins.search()
   ↓
Search.search()
   └─→ search_answerers()
         └─→ Answerer.answer()  [违反契约：返回 MainResult 而非 BaseAnswer]
               └─→ ResultContainer.extend() 中进入 _merge_main_result 分支
   ↓
plugins.post_search()
   ↓
result_container.close()
   ↓
模板渲染
   ├─ answers 区域（可能为空，取决于 answerer 返回类型）
   └─ 主结果列表（含 answerer 返回的非标准结果）
```

> ⚠️ **非标准路径说明**：此路径依赖 `ResultContainer.extend()` 对 `Result` 基类的宽泛处理，不属于 answerer 接口契约。answerer 实现应严格返回 `list[BaseAnswer]`，否则可能在未来版本中出现兼容性问题。

## 七、关键契约总结

| 层级 | 结论 | 代码依据 |
|------|------|----------|
| **标准契约** | answerer 命中后短路，不执行传统检索 | `searx/search/__init__.py:176-178` |
| **标准契约** | answerer 接口返回类型为 `list[BaseAnswer]` | `searx/answerers/_core.py:50` |
| **标准契约** | answerer 结果存入 `answers` 集合，与主结果分离 | `searx/results.py:99-100` |
| **扩展契约** | `post_search` 是框架内唯一可追加结果的合法扩展点 | `searx/search/__init__.py:206` |
| **缓存契约** | 主搜索链路无缓存，answerer 缓存为内部实现 | 主流程无缓存调用 |
| **非标准** | answerer 返回 `MainResult` 可工作但违反接口 | 依赖 `ResultContainer.extend()` 的宽松处理 |

## 八、设计原则

1. **短路优化优先**：Answerer 命中后跳过传统搜索，显著降低延迟和网络请求
2. **接口契约明确**：Answerer 应严格返回 `BaseAnswer` 类型结果
3. **结果隔离展示**：Answer 与传统搜索结果分开展示，避免干扰
4. **插件扩展唯一**：`post_search` 是框架契约内追加结果的唯一合法路径
5. **缓存边界清晰**：主搜索链路不做查询结果缓存，Answerer 按需自主实现局部缓存
6. **可扩展性**：通过新增 `searx/answerers/` 下的模块即可扩展 answerer
7. **互斥默认，扩展灵活**：默认 answerer 与传统检索互斥，特殊需求通过插件机制实现
