# SearXNG 插件 Hook 执行顺序报告

## 一、整体生命周期概览

SearXNG 插件系统通过三个核心 Hook 介入搜索请求流程，按照执行顺序排列：

```
应用启动
    ↓
[初始化阶段] Plugin.init() → 仅在应用启动时执行一次
    ↓
用户请求到达
    ↓
[请求预处理] 确定 user_plugins 列表（用户启用的插件）
    ↓
[搜索前] pre_search() → 返回 False 可跳过引擎搜索
    ↓
[引擎搜索] 多线程并发请求各搜索引擎（pre_search 返回 True 时才执行）
    ↓
[结果逐个处理] on_result() → 每个结果触发一次，可过滤
    ↓
[搜索后] post_search() → 始终执行，可追加结果
    ↓
返回响应给用户
```

> **重要修正**：`post_search` 无论 `pre_search` 返回什么都会执行。

---

## 二、插件遍历顺序的重要说明

**核心事实**：`PluginStorage.plugin_list` 是 **`set[Plugin]`** 类型（见 `searx/plugins/_core.py:195`）。

```python
# searx/plugins/_core.py:195
plugin_list: set[Plugin]
```

**关键结论**：
- `set` 是无序集合，**遍历顺序不保证稳定**，也不保证与配置加载顺序一致
- Python 3.7+ 的 `set` 在 CPython 实现中可能表现出某种插入顺序，但这是**实现细节**，不是语言规范
- 不同 Python 版本、不同运行实例间，插件遍历顺序可能不同
- **不要依赖插件的执行顺序来实现业务逻辑**

**对短路逻辑的影响**：
- `pre_search` 的短路优先级**不可预测**：如果多个插件都可能返回 `False`，哪个先触发是不确定的
- `on_result` 的过滤优先级**不可预测**：如果多个插件都可能过滤同一结果，哪个先生效是不确定的

---

## 三、各阶段详细说明

### 阶段 1：初始化阶段

**触发时机**：Web 应用启动时执行，仅运行一次。

**调用链路**：
1. `searx/webapp.py:1377` → `searx.plugins.initialize(app)`
2. `searx/plugins/__init__.py:107-109` → `STORAGE.load_settings()` + `STORAGE.init(app)`
3. `searx/plugins/_core.py:244-251` → 遍历所有插件调用 `plugin.init(app)`

**核心代码**：
```python
# searx/plugins/_core.py:129-138
def init(self, app: flask.Flask) -> bool:
    """返回 True 表示插件激活，False 表示停用该插件"""
    return True
```

**行为特点**：
- 每个插件的 `init()` 仅在应用启动时调用一次
- 返回值决定插件是否被加入可用列表（返回 False 会被移除）
- 可用于初始化数据库连接、加载资源等一次性操作

**异常影响**：
- `load_settings` 阶段：导入模块异常会被捕获并记录日志，但如果插件类不存在（`cls is None`）会抛出 `ValueError`，**导致应用启动失败**
- `init` 方法调用：`PluginStorage.init()` 中没有异常捕获，如果插件的 `init()` 抛出异常，**会导致应用启动失败**
- 只有插件 `init()` 返回 `False` 是安全的（仅停用该插件，不影响启动）

---

### 阶段 2：请求预处理（确定 user_plugins）

**触发时机**：每个 HTTP 请求到达时，在 `before_request` 中执行。

**调用链路**：
1. `searx/webapp.py:459` → `pre_request()`
2. `searx/webapp.py:512-518` → 根据用户偏好构建 `sxng_request.user_plugins`

**核心代码**：
```python
# searx/webapp.py:512-518
sxng_request.user_plugins = []
allowed_plugins = preferences.plugins.get_enabled()
disabled_plugins = preferences.plugins.get_disabled()
for plugin in searx.plugins.STORAGE:
    if (plugin.id not in disabled_plugins) or plugin.id in allowed_plugins:
        sxng_request.user_plugins.append(plugin.id)
```

**行为特点**：
- 每个请求都会重新计算用户启用的插件列表
- 基于用户 Cookie 中的偏好设置
- 后续所有 Hook 只会调用此列表中的插件

---

### 阶段 3：搜索前 Hook（pre_search）

**触发时机**：搜索开始前执行。

**调用链路**：
1. `searx/search/__init__.py:201-204` → `SearchWithPlugins.search()`
2. `searx/plugins/_core.py:253-265` → `PluginStorage.pre_search()`

**核心代码**：
```python
# searx/search/__init__.py:201-206
def search(self) -> ResultContainer:
    if searx.plugins.STORAGE.pre_search(self.request, self):
        super().search()  # 只有 pre_search 返回 True 才执行引擎搜索
    
    searx.plugins.STORAGE.post_search(self.request, self)  # 始终执行！
    ...
```

```python
# searx/plugins/_core.py:253-265
def pre_search(self, request, search):
    ret = True
    for plugin in [p for p in self.plugin_list if p.id in search.user_plugins]:
        try:
            ret = bool(plugin.pre_search(request=request, search=search))
        except Exception:
            plugin.log.exception("Exception while calling pre_search")
            continue
        if not ret:
            break  # 第一个返回 False 的插件终止后续调用
    return ret
```

**行为特点**：
- 遍历 `self.plugin_list`（set 类型），**顺序不保证稳定**
- 任何插件返回 `False` 会立即终止循环，**跳过引擎搜索**，但 `post_search` 仍会执行
- 单个插件抛出异常仅记录日志，不影响其他插件和主流程（搜索继续）
- 可用于：查询重写、权限检查、提前返回答案等

**关于短路的重要说明**：
> ⚠️ **不要依赖执行顺序实现短路优先级**。如果有多个插件可能返回 `False`，哪个插件先触发短路是不确定的。如果需要确定的优先级，应在单个插件内实现逻辑判断，或通过其他机制协调。

---

### 阶段 4：结果逐个处理 Hook（on_result）

**触发时机**：每个搜索引擎返回结果后，逐个结果处理时执行。

**调用链路**：
1. 各引擎处理器将结果添加到 `ResultContainer`
2. `searx/search/__init__.py:188` → `self.result_container.on_result = self._on_result`
3. `searx/search/__init__.py:198-199` → `_on_result()` 调用插件链
4. `searx/plugins/_core.py:267-280` → `PluginStorage.on_result()`

**核心代码**：
```python
# searx/plugins/_core.py:267-280
def on_result(self, request, search, result):
    ret = True
    for plugin in [p for p in self.plugin_list if p.id in search.user_plugins]:
        try:
            ret = bool(plugin.on_result(request=request, search=search, result=result))
        except Exception:
            plugin.log.exception("Exception while calling on_result")
            continue
        if not ret:
            break  # 第一个返回 False 的插件标记该结果被过滤
    return ret
```

**行为特点**：
- 每个结果对象都会触发一次完整的插件链遍历
- 遍历 `self.plugin_list`（set 类型），**顺序不保证稳定**
- 任何插件返回 `False` 会立即终止循环，该结果被**丢弃**
- 单个插件抛出异常仅记录日志，该结果继续传递给后续插件
- 可用于：结果过滤、URL 重写、内容修改、字段补充等
- 插件可以直接修改 `result` 对象的属性

**关于结果过滤的重要说明**：
> ⚠️ **不要依赖执行顺序实现过滤优先级**。如果有多个插件都可能过滤同一结果，哪个插件先生效是不确定的。如果需要确定的过滤顺序，应在单个插件内实现组合多个过滤逻辑，或确保各插件的过滤条件互斥。

---

### 阶段 5：搜索后 Hook（post_search）

**触发时机**：所有引擎搜索完成且结果处理完毕后执行（或 pre_search 返回 False 后立即执行）。

**调用链路**：
1. `searx/search/__init__.py:206` → `searx.plugins.STORAGE.post_search()`
2. `searx/plugins/_core.py:282-306` → `PluginStorage.post_search()`

**核心代码**：
```python
# searx/plugins/_core.py:282-306
def post_search(self, request, search):
    keyword = None
    for keyword in search.search_query.query.split():
        if keyword:
            break

    for plugin in [p for p in self.plugin_list if p.id in search.user_plugins]:
        if plugin.keywords:
            # 有关键词的插件：只有 keyword 存在且不匹配时才跳过
            if keyword and keyword not in plugin.keywords:
                continue
        try:
            results = plugin.post_search(request=request, search=search) or []
        except Exception:
            plugin.log.exception("Exception while calling post_search")
            continue
        
        search.result_container.extend(f"plugin: {plugin.id}", results)
```

**行为特点**：
- **始终执行**：无论 `pre_search` 返回 `True` 还是 `False`，`post_search` 都会被调用
- 遍历 `self.plugin_list`（set 类型），**顺序不保证稳定**
- **关键词逻辑**：
  - 当查询为空时，`keyword = None`，`if keyword and ...` 条件为 `False`，不会执行 `continue`
  - 因此**空查询时，有关键词的插件也会运行**
  - 只有当查询非空且首词不在插件关键词列表中时，才会跳过该插件
- 返回值会被追加到最终结果列表中
- 单个插件抛出异常仅记录日志，不影响其他插件
- 可用于：添加自定义答案、补充 infobox、结果重排序等

**关于结果追加的重要说明**：
> ⚠️ **不要依赖执行顺序控制结果追加顺序**。多个插件返回的结果追加顺序是不确定的。如果需要确定的结果顺序，应在单个插件内统一处理，或在返回结果后进行排序。

---

## 四、异常处理机制

| Hook 类型 | 异常捕获位置 | 对主流程影响 | 对后续插件影响 |
|----------|-------------|-------------|---------------|
| `init()` | **无捕获** | **致命**：插件 `init()` 抛出异常会导致应用启动失败 | 后续插件不会初始化 |
| `pre_search()` | `_core.py:259-261` | 无影响（搜索继续） | 当前插件被跳过，继续执行下一个 |
| `on_result()` | `_core.py:273-275` | 无影响（当前结果继续处理） | 当前插件被跳过，继续执行下一个 |
| `post_search()` | `_core.py:301-303` | 无影响（结果正常返回） | 当前插件被跳过，继续执行下一个 |

**关键设计原则**：
- 初始化阶段异常是致命的（会导致启动失败）
- 请求处理阶段的单个插件故障不会导致整个搜索失败
- 异常仅记录日志，不向上层抛出
- 仅 `pre_search` 返回 `False` 能跳过引擎搜索（异常不会）

---

## 五、完整时序图

```
SearchWithPlugins.search()
        │
        ├─→ pre_search() 遍历插件链（顺序不稳定！）
        │     ├─ plugin_X.pre_search() → True
        │     ├─ plugin_Y.pre_search() → True
        │     └─ plugin_Z.pre_search() → False → 终止，后续插件不执行
        │
        ├─ 条件分支：
        │     ├─ 若 pre_search 返回 True：
        │     │     ├─→ super().search()
        │     │     │     ├─ search_external_bang()
        │     │     │     ├─ search_answerers()
        │     │     │     └─ search_standard()
        │     │     │           └─ 多线程并发请求引擎
        │     │     │                 └─ 每个结果返回时触发 on_result 链（顺序不稳定！）
        │     │     │                       ├─ plugin_X.on_result(result1)
        │     │     │                       ├─ plugin_Y.on_result(result1)
        │     │     │                       └─ ...
        │     │     └─ post_search() 执行（顺序不稳定！）
        │     │
        │     └─ 若 pre_search 返回 False：
        │           └─ post_search() 仍执行 ←────── 关键点！
        │
        └─ result_container.close()
```

---

## 六、关键注意事项

1. **plugin_list 是 set 类型**：遍历顺序不保证稳定，不要依赖执行顺序
2. **短路逻辑**：`pre_search` 和 `on_result` 支持短路，但由于顺序不稳定，短路优先级不可预测
3. **post_search 始终执行**：无论 `pre_search` 返回什么，`post_search` 都会被调用
4. **空查询时关键词插件仍运行**：查询为空时，有关键词限制的插件不会被跳过
5. **初始化异常致命**：插件 `init()` 抛出异常会导致应用启动失败
6. **线程安全**：`on_result` 在多线程环境下被调用，插件需保证线程安全
7. **结果修改**：`on_result` 中修改 `result` 对象会直接影响最终输出
8. **性能影响**：`on_result` 对每个结果都执行，注意避免耗时操作
9. **插件顺序不可控**：多个插件的结果追加顺序不可控，必要时在单个插件内统一处理排序
