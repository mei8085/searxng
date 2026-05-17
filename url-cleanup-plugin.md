# URL 清理插件链路系统梳理

## 一、插件注册形式

SearXNG 的 URL 清理类插件基于统一的插件框架实现，核心注册机制如下：

### 1.1 插件基类与接口

所有 URL 清理插件均继承自 `Plugin` 抽象基类（`searx/plugins/_core.py:68），通过实现 `on_result` 钩子介入搜索结果处理流程。插件必须实现以下核心要素：

- **类定义**：继承 `Plugin` 类，定义唯一 `id` 标识
- **配置加载**：在 `settings.yml` 中声明插件的完整类路径和激活状态
- **钩子实现**：在 `on_result` 方法中调用 `result.filter_urls()` 遍历所有 URL 字段

### 1.2 注册流程

```python
# settings.yml 配置示例:
plugins:
  searx.plugins.tracker_url_remover.SXNGPlugin:
    active: true
  searx.plugins.hostnames.SXNGPlugin:
    active: true
```

注册链路：
1. 应用启动时，`PluginStorage.load_settings()` 解析配置（`searx/plugins/_core.py:212`）
2. 通过 `importlib.import_module` 动态导入插件类
3. 实例化插件对象并调用 `register()` 注册到 `plugin_list` 集合
4. 调用 `init()` 方法进行初始化，返回 `False` 则插件不激活

### 1.3 现有 URL 清理类插件清单

| 插件 ID | 类路径 | 功能定位 |
|--------|--------|----------|
| `tracker_url_remover` | `searx.plugins.tracker_url_remover.SXNGPlugin` | 移除 URL 追踪参数 |
| `hostnames` | `searx.plugins.hostnames.SXNGPlugin` | 主机名重写、结果移除、优先级调整 |
| `oa_doi_rewrite` | `searx.plugins.oa_doi_rewrite.SXNGPlugin` | DOI 重定向到开放获取版本 |

## 二、命中匹配规则的维度

### 2.1 tracker_url_remover 插件

**数据源**：ClearURLs 规则库（`searx/data/tracker_patterns.py:31-36）

匹配维度：

```python
# 规则结构 (tracker_patterns.py:22)
RuleType = tuple[str, list[str], list[str]]
# 即: (url_regexp, url_ignore, del_args)
```

1. **URL 模式匹配** (`url_regexp) - 正则表达式匹配整个 URL
2. **例外排除** (`url_ignore`) - 匹配此正则的 URL 跳过处理
3. **参数删除** (`del_args`) - 匹配此正则的查询参数名被移除

```python
# 匹配流程 (tracker_patterns.py:121-171)
for rule in self.rules():
    if re.match(rule[url_regexp], new_url):       # 1. URL 匹配
        if re.match(exception, new_url):              # 2. 例外检查
            continue
        for name, val in query_args:
            if re.match(pattern, name):            # 3. 参数名匹配
                query_args.remove((name, val))       # 4. 删除参数
```

### 2.2 hostnames 插件

匹配维度基于**主机名正则匹配**（`hostnames.py:126-143`）：

1. **REMOVE 规则**：主机名匹配正则，匹配则移除整个结果
2. **REPLACE 规则**：主机名匹配正则，匹配则替换主机名部分
3. **PRIORITY 规则**：匹配则调整结果优先级

```python
# 主机名匹配逻辑
for pattern in REMOVE:
    if pattern.search(result.parsed_url.netloc):
        return False  # 移除结果

for pattern, replacement in REPLACE.items():
    if pattern.search(url_src_parsed.netloc):
        new_url = url_src_parsed._replace(netloc=pattern.sub(replacement, ...))
```

### 2.3 oa_doi_rewrite 插件

匹配维度基于**DOI 模式匹配**（`oa_doi_rewrite.py:70-81）：

```python
regex = re.compile(r'10\.\d{4,9}/[^\s]+')

def extract_doi(url):
    m = regex.search(url.path)          # 1. 从 URL 路径提取
    for _, v in parse_qsl(url.query):   # 2. 从查询参数提取
        m = regex.search(v)
```

## 三、被改写字段的范围

所有 URL 清理插件通过 `result.filter_urls() 方法统一处理 URL 字段。在 `searx/result_types/_base.py:111-216` 中定义了完整的字段遍历逻辑。

### 3.1 主 URL 字段（一级字段）

```python
url_fields = ["url", "iframe_src", "audio_src", "img_src", "thumbnail_src", "thumbnail"]
```

| 字段名 | 用途说明 |
|--------|----------|
| `url` | 结果主链接 |
| `iframe_src` | iframe 嵌入源 |
| `audio_src` | 音频源 URL |
| `img_src` | 图片源 URL |
| `thumbnail_src` | 缩略图源 URL |
| `thumbnail` | 缩略图 URL（兼容字段） |

### 3.2 Infobox 嵌套字段

```python
# Infobox URLs 列表字段
infobox_urls: list[dict[str, str]] = getattr(result, "urls", [])
# 每个 item 中的 "url" 字段

# Infobox 属性中的图片源
infobox_attributes: list[dict[str, t.Any]] = getattr(result, "attributes", [])
# 每个 item["image"]["src"] 字段
```

### 3.3 各插件字段覆盖范围

| 插件 | 处理字段 | 说明 |
|------|----------|------|
| tracker_url_remover | 所有 URL 字段 | 遍历全部字段，移除追踪参数 |
| hostnames | 所有 URL 字段 + 主 URL 额外处理 | 1. 先检查主 URL 决定是否移除结果<br>2. 再遍历所有 URL 字段进行重写 |
| oa_doi_rewrite | 仅 `url` 字段 | `if field_name != "url": return True`，只处理主链接 |

### 3.4 字段改写后的同步机制

当 `url` 字段被修改时，会自动同步 `parsed_url` 字段（`_base.py:140-145`）：

```python
if field_name == "url":
    if not new_url:
        result.parsed_url = None
    elif isinstance(new_url, str):
        result.parsed_url = urllib.parse.urlparse(new_url)
```

## 四、插件执行顺序与互相覆盖问题

### 4.1 执行顺序的不确定性

**核心问题**：`PluginStorage.plugin_list` 是一个 `set` 集合（`_core.py:195`），集合的迭代顺序是不确定的。

```python
class PluginStorage:
    plugin_list: set[Plugin]  # 无序集合
```

在 `on_result` 钩子调用时（`_core.py:267-280`），插件按集合迭代顺序执行：

```python
def on_result(self, request, search, result):
    ret = True
    for plugin in [p for p in self.plugin_list if p.id in search.user_plugins]:
        ret = bool(plugin.on_result(request=request, search=search, result=result)
        if not ret:
            break
    return ret
```

### 4.2 互相覆盖的风险场景

当多个插件修改同一个 URL 字段时，**后执行的插件会覆盖先执行插件的修改**。

**风险场景示例：

1. **tracker_url_remover 与 hostnames 冲突：
   - 先 hostnames 先执行：youtube.com → invidious.example.com
   - tracker_url_remover 后执行：可能无法匹配 invidious 的规则（因为规则是针对 youtube.com 的）

2. **hostnames 与 oa_doi_rewrite 冲突：
   - 先 hostnames 把 doi.org → 其他域名
   - 后 oa_doi_rewrite 无法识别 DOI 路径

3. **多个插件修改同一个 URL：
   - 插件 A 修改 url 为 A'
   - 插件 B 基于原始 url 修改为 B'
   - 最终结果取决于执行顺序

### 4.3 推荐的执行顺序安排

为避免互相覆盖，应遵循以下原则：

#### 原则 1：**先清理参数，后重写域名

推荐顺序：
```
tracker_url_remover → hostnames → oa_doi_rewrite
```

原因：
- tracker_url_remover 基于原始域名匹配追踪参数
- hostnames 基于清理后的 URL 重写域名
- oa_doi_rewrite 最后处理 DOI 重定向

#### 原则 2：**移除类插件优先执行

如果有插件会移除结果（如 hostnames 的 REMOVE 规则），应优先执行，避免无效处理即将被移除的结果。

#### 原则 3：**在 filter_func 中避免假设原始 URL

插件的 filter_func 签名为：
```python
def filter_url_field(result, field_name, url_src) -> bool | str:
```

`url_src` 是**当前字段的值（可能已被之前插件修改过）。如果插件需要基于原始 URL 匹配，应在 `on_result` 中保存原始值。

#### 原则 4：**幂等性设计

每个插件的 filter_func 应设计为幂等的，多次调用结果一致。

### 4.4 执行顺序的控制方法

**当前代码限制**：SearXNG 插件框架不支持显式的插件优先级配置。

**可行的控制方案：

方案 A：**配置文件顺序控制

在 `settings.yml` 中按期望的执行顺序声明插件（依赖 Python 3.7+ dict 有序特性）：
```yaml
plugins:
  searx.plugins.tracker_url_remover.SXNGPlugin:  # 1. 先清理参数
    active: true
  searx.plugins.hostnames.SXNGPlugin:            # 2. 再重写域名
    active: true
  searx.plugins.oa_doi_rewrite.SXNGPlugin:  # 3. 最后 DOI 重定向
    active: false
```

方案 B：**单一组合插件**

创建一个组合插件，内部按顺序调用多个清理逻辑：
```python
class URLCleanupPipeline(Plugin):
    def on_result(self, request, search, result):
        result.filter_urls(self._clean_trackers)
        result.filter_urls(self._rewrite_hostnames)
        result.filter_urls(self._rewrite_doi)
```

方案 C：**在 filter_func 中检查前置条件

每个插件的 filter_func 检查字段是否已被修改：
```python
def filter_url_field(result, field_name, url_src):
    # 检查 url_src 是否已被其他插件修改
    original_url = result.get("original_url")
    if original_url and original_url != url_src:
        return True  # 跳过已修改的 URL
```

## 五、完整链路时序图

```
搜索请求
    ↓
SearchWithPlugins.search()
    ↓
pre_search 钩子（所有插件）
    ↓
引擎搜索，产生结果
    ↓
结果进入 ResultContainer
    ↓
on_result 钩子（按 plugin_list 顺序）
    ├─→ tracker_url_remover.on_result()
    │     └─→ result.filter_urls(filter_url_field)
    │           ├─→ 遍历 url, iframe_src, img_src, ...
    │           ├─→ 遍历 infobox urls
    │           └─→ 遍历 infobox attributes
    │           └─→ TRACKER_PATTERNS.clean_url()
    │
    ├─→ hostnames.on_result()
    │     ├─→ 检查主 URL 是否匹配 REMOVE
    │     └─→ result.filter_urls(filter_url_field)
    │           └─→ 主机名重写
    │
    └─→ oa_doi_rewrite.on_result()
          └─→ result.filter_urls(filter_url_field)
                └─→ 仅处理 url 字段
    ↓
post_search 钩子（所有插件）
    ↓
结果返回
```

## 六、关键代码位置参考

| 功能 | 文件位置 |
|------|--------|
| 插件基类 | `searx/plugins/_core.py:68-175` |
| filter_urls 实现 | `searx/result_types/_base.py:111-216` |
| tracker_url_remover | `searx/plugins/tracker_url_remover.py:24-58` |
| 追踪模式数据库 | `searx/data/tracker_patterns.py:26-176` |
| hostnames 插件 | `searx/plugins/hostnames.py:110-200` |
| oa_doi_rewrite | `searx/plugins/oa_doi_rewrite.py:45-89` |
| 插件执行调度 | `searx/plugins/_core.py:267-280` |
| 搜索结果钩子 | `searx/search/__init__.py:198-199` |
