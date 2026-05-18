# Answerer 执行路径分析

本文档详细说明 SearXNG 中本地 answerer 的工作机制，包括注册形态、命中判定、与缓存和插件的执行先后关系，以及结果合并展示逻辑。

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

### 1.3 实现示例

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

## 二、命中判定

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

## 三、执行先后关系

### 3.1 整体搜索流程

搜索入口在 `Search.search()` (`searx/search/__init__.py:174-179`)：

```python
def search(self) -> ResultContainer:
    self.start_time = default_timer()
    if not self.search_external_bang():
        if not self.search_answerers():
            self.search_standard()
    return self.result_container
```

**执行顺序**：

| 阶段 | 方法 | 说明 | 短路逻辑 |
|------|------|------|----------|
| 1 | `search_external_bang()` | 检查外部 bang 重定向 | 返回 `True` 则终止后续流程 |
| 2 | `search_answerers()` | 调用本地 answerer | 返回 `True`（有结果）则跳过标准搜索 |
| 3 | `search_standard()` | 传统搜索引擎检索 | 仅当前面都未命中时执行 |

### 3.2 与插件的关系

插件执行通过 `SearchWithPlugins.search()` (`searx/search/__init__.py:201-209`) 包装：

```python
def search(self) -> ResultContainer:
    if searx.plugins.STORAGE.pre_search(self.request, self):
        super().search()  # 包含 answerer 流程
    
    searx.plugins.STORAGE.post_search(self.request, self)
    self.result_container.close()
    return self.result_container
```

**插件与 Answerer 时序**：

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

关键点：
- `pre_search` 在 answerer 之前执行，可通过返回 `False` 阻止整个搜索
- `on_result` 钩子在 answerer 结果添加时被调用（见下文）
- `post_search` 在所有搜索完成后执行

### 3.3 与缓存的关系

**全局搜索流程无缓存层**：SearXNG 的搜索主流程没有内置的查询结果缓存。

**缓存使用场景**：
1. **Answerer 内部缓存**：各 answerer 可自主使用缓存。例如天气 answerer 内部使用 `WEATHER_DATA_CACHE` 缓存地理位置数据 (`searx/weather.py:181-188`)
2. **引擎级缓存**：部分在线引擎可能有自己的缓存机制
3. **HTTP 请求头**：在线处理器设置 `Cache-Control: no-cache` (`searx/search/processors/online.py:145`)，避免 HTTP 层缓存

**Answerer 与缓存的时序**：
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

## 四、结果合并展示

### 4.1 结果添加流程

Answerer 结果通过 `search_answerers()` 添加到结果容器 (`searx/search/__init__.py:71-75`)：

```python
def search_answerers(self):
    results = searx.answerers.STORAGE.ask(self.search_query.query)
    self.result_container.extend(None, results)
    return bool(results)
```

### 4.2 结果分类存储

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

关键点：
- Answer 结果被 `on_result` 插件钩子过滤
- 命中的 Answer 存入 `self.answers`（`AnswerSet` 类型），而非主结果列表
- `AnswerSet` 自动去重（基于 hash）并按 template 排序

### 4.3 AnswerSet 机制

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

### 4.4 展示逻辑

在 web 应用的 `search()` 视图中 (`searx/webapp.py:620-729`)：

1. 结果通过 `result_container.get_ordered_results()` 获取**主搜索结果**
2. Answer 结果通过 `result_container.answers` 单独访问
3. 模板中分别渲染：
   - 主结果：`results.html` 模板
   - Answer：`answers.html` 模板（位于搜索结果顶部）

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

## 五、完整执行路径图

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
   ├─→ search_answerers()
   │     ├─→ AnswerStorage.ask(query)
   │     │     ├─ 提取第一个关键词
   │     │     └─ 调用匹配的 Answerer.answer()
   │     ├─→ ResultContainer.extend(None, results)
   │     │     ├─ on_result 插件过滤
   │     │     └─ 存入 answers 集合（去重）
   │     └─(有结果)──→ 跳过标准搜索
   │     ↓(无结果)
   └─→ search_standard()
         └─ 多引擎并发检索 → 主结果列表
   ↓
plugins.post_search()
   ↓
result_container.close()
   ↓
模板渲染
   ├─ answers 区域（顶部展示）
   └─ 主结果列表
```

## 六、关键设计特点

1. **短路优化**：Answerer 命中后跳过传统搜索，显著降低延迟和网络请求
2. **关键词驱动**：仅匹配查询首词，判定逻辑简单高效
3. **结果隔离**：Answer 与传统搜索结果分开展示，避免干扰
4. **插件协同**：完整接入插件生命周期，支持过滤和扩展
5. **无全局缓存**：搜索流程无缓存层，Answerer 按需自主缓存
6. **可扩展性**：通过新增 `searx/answerers/` 下的模块即可扩展 answerer
