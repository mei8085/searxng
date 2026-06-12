# SearXNG 搜索结果内容协商与序列化机制分析

## 一、概述

SearXNG 支持多种搜索结果输出格式（HTML、JSON、CSV、RSS），实现了"同一份搜索数据，多种序列化方式"的内容协商机制。与传统基于 HTTP `Accept` 头的内容协商不同，SearXNG 采用了**查询参数驱动**的简洁实现方式。

---

## 二、输出格式定义与配置

### 2.1 支持的输出格式

输出格式在 [settings_defaults.py](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/settings_defaults.py#L22-L22) 中硬编码定义：

```python
OUTPUT_FORMATS = ['html', 'csv', 'json', 'rss']
```

### 2.2 配置层验证

在 [settings_defaults.py](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/settings_defaults.py#L207-L207) 的 SCHEMA 中，`search.formats` 配置项使用 `SettingSublistValue` 进行验证，确保管理员配置的格式都在 `OUTPUT_FORMATS` 列表中：

```python
'search': {
    'formats': SettingsValue(list, OUTPUT_FORMATS),
    ...
}
```

这意味着：
- 默认启用所有 4 种格式
- 管理员可以在 `settings.yml` 中限制可用格式
- 非法格式会在启动时被拒绝

---

## 三、内容协商机制

### 3.1 协商入口

内容协商的核心逻辑位于 [webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/webapp.py#L619-L785) 的 `search()` 视图函数中。

### 3.2 协商决策流程

**决策依据优先级**（从高到低）：

1. **查询参数 `format`** - 最高优先级
2. **配置白名单** - `settings['search']['formats']`
3. **硬编码默认值** - `'html'`

**代码实现**（[webapp.py#L628-L634](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/webapp.py#L628-L634)）：

```python
# 1. 从请求参数获取 format，默认为 'html'
output_format = sxng_request.form.get('format', 'html')

# 2. 验证是否在硬编码支持列表中
if output_format not in OUTPUT_FORMATS:
    output_format = 'html'

# 3. 验证是否在管理员配置的白名单中
if output_format not in settings['search']['formats']:
    flask.abort(403)
```

### 3.3 关键设计特点

| 特点 | 说明 |
|------|------|
| **无 Accept 头解析** | 完全不依赖 HTTP `Accept` 头，简化了实现 |
| **显式参数驱动** | 用户必须通过 `?format=json` 显式指定格式 |
| **双重验证** | 既验证硬编码支持列表，又验证管理员配置 |
| **优雅降级** | 非法格式自动回退到 `html` |

> **注意**：与 RESTful API 常见的内容协商不同，SearXNG 不解析 `Accept: application/json` 这样的请求头。

---

## 四、序列化器实现

SearXNG 为每种格式实现了独立的序列化逻辑，分为两类：

### 4.1 无模板序列化（JSON、CSV）

#### 4.1.1 JSON 序列化器

**位置**：[webutils.py#L162-L175](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/webutils.py#L162-L175)

```python
def get_json_response(sq: "SearchQuery", rc: "ResultContainer") -> str:
    data = {
        'query': sq.query,
        'number_of_results': rc.number_of_results,
        'results': [_.as_dict() for _ in rc.get_ordered_results()],
        'answers': [_.as_dict() for _ in rc.answers],
        'corrections': list(rc.corrections),
        'infoboxes': rc.infoboxes,
        'suggestions': list(rc.suggestions),
        'unresponsive_engines': get_translated_errors(rc.unresponsive_engines),
    }
    response = json.dumps(data, cls=JSONEncoder)
    return response
```

**自定义 JSONEncoder**（[webutils.py#L149-L159](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/webutils.py#L149-L159)）处理特殊类型：
- `msgspec.Struct` → 转换为内置类型
- `datetime` → ISO 格式字符串
- `timedelta` → 总秒数
- `set` → 列表

#### 4.1.2 CSV 序列化器

**位置**：[webutils.py#L113-L146](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/webutils.py#L113-L146)

```python
def write_csv_response(csv: CSVWriter, rc: "ResultContainer") -> None:
    keys = ('title', 'url', 'content', 'host', 'engine', 'score', 'type')
    csv.writerow(keys)

    # 写入搜索结果
    for res in rc.get_ordered_results():
        row = res.as_dict()
        row['host'] = row['parsed_url'].netloc
        row['type'] = 'result'
        csv.writerow([row.get(key, '') for key in keys])

    # 写入答案
    for a in rc.answers:
        ...

    # 写入建议和纠错
    for a in rc.suggestions:
        ...
    for a in rc.corrections:
        ...
```

**CSVWriter 类**（[webutils.py#L85-L110](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/webutils.py#L85-L110)）提供编码处理，确保 UTF-8 兼容性。

### 4.2 模板驱动序列化（RSS、HTML）

#### 4.2.1 RSS 序列化器

使用 Jinja2 模板 [opensearch_response_rss.xml](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/templates/simple/opensearch_response_rss.xml)：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" ...>
  <channel>
    <title>SearXNG search: {{ q|e }}</title>
    ...
    {% for r in results %}
    <item>
      <title>{{ r.title }}</title>
      <link>{{ r.url }}</link>
      <description>{{ r.content }}</description>
    </item>
    {% endfor %}
  </channel>
</rss>
```

**调用位置**：[webapp.py#L722-L729](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/webapp.py#L722-L729)

```python
if output_format == 'rss':
    response_rss = render(
        'opensearch_response_rss.xml',
        results=results,
        q=sxng_request.form['q'],
        number_of_results=result_container.number_of_results,
    )
    return Response(response_rss, mimetype='text/xml')
```

#### 4.2.2 HTML 序列化器

使用 Jinja2 模板 [results.html](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/templates/simple/results.html)，这是最复杂的序列化路径：

**HTML 特有的预处理**（[webapp.py#L701-L718](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/webapp.py#L701-L718)）：

```python
for result in results:
    if output_format == 'html':
        # 关键词高亮
        if 'content' in result and result['content']:
            result['content'] = highlight_content(escape(result['content'][:1024]), search_query.query)
        if 'title' in result and result['title']:
            result['title'] = highlight_content(escape(result['title'] or ''), search_query.query)

    # 分组边界检测（用于模板分组渲染）
    if current_template != result.template:
        result.open_group = True
        if previous_result:
            previous_result.close_group = True
```

---

## 五、完整数据流程

### 5.1 数据流全景图

```
HTTP 请求
    ↓
[webapp.py:pre_request] 预处理（解析偏好、合并表单参数）
    ↓
[webapp.py:search]
    ├─→ 内容协商：确定 output_format
    ├─→ [webadapter.py:get_search_query_from_webapp] 解析搜索查询
    ├─→ [search/__init__.py:SearchWithPlugins.search] 执行搜索
    │    ├─→ external_bang 检测
    │    ├─→ answerers 问答
    │    └─→ 多引擎并发搜索
    │        └─→ [results.py:ResultContainer] 收集、合并、去重、排序
    │
    └─→ 序列化分发
         ├─→ format=json → [webutils.py:get_json_response] → JSON Response
         ├─→ format=csv  → [webutils.py:write_csv_response]  → CSV Response
         ├─→ format=rss  → Jinja2 模板渲染                 → XML Response
         └─→ format=html → 结果预处理 + Jinja2 模板渲染     → HTML Response
```

### 5.2 核心数据结构：ResultContainer

[ResultContainer](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/results.py#L53-L294) 是所有序列化器共享的数据源：

| 字段 | 类型 | 说明 |
|------|------|------|
| `main_results_map` | `dict` | 主结果映射（URL 哈希去重） |
| `infoboxes` | `list` | 信息框结果 |
| `suggestions` | `set` | 搜索建议 |
| `answers` | `AnswerSet` | 直接答案 |
| `corrections` | `set` | 拼写纠错 |
| `unresponsive_engines` | `set` | 无响应引擎 |
| `timings` | `list` | 引擎响应时间 |

**关键方法**：
- `get_ordered_results()` - 返回排序后的结果列表
- `number_of_results` - 结果总数估计
- `get_timings()` - 获取性能数据

### 5.3 Result 类型系统

所有结果都继承自 [Result](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/result_types/_base.py#L228-L337) 基类（基于 `msgspec.Struct`）：

```python
class Result(msgspec.Struct, kw_only=True):
    url: str | None = None
    engine: str | None = ""
    parsed_url: urllib.parse.ParseResult | None = None

    def as_dict(self):
        return {f: getattr(self, f) for f in self.__struct_fields__}
```

`as_dict()` 方法是序列化的关键桥梁——所有格式的序列化最终都依赖这个方法将强类型对象转换为字典。

---

## 六、错误处理的内容协商

错误响应也遵循相同的格式协商机制，在 [index_error](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/webapp.py#L553-L579) 函数中实现：

```python
def index_error(output_format: str, error_message: str):
    if output_format == 'json':
        return Response(json.dumps({'error': error_message}), mimetype='application/json')
    if output_format == 'csv':
        response = Response('', mimetype='application/csv')
        cont_disp = 'attachment;Filename=searx.csv'
        response.headers.add('Content-Disposition', cont_disp)
        return response
    if output_format == 'rss':
        # 使用 RSS 模板渲染错误
        response_rss = render(
            'opensearch_response_rss.xml',
            results=[], error_message=error_message, ...
        )
        return Response(response_rss, mimetype='text/xml')
    # HTML 错误
    sxng_request.errors.append(gettext('search error'))
    return render('index.html', ...)
```

---

## 七、架构设计分析

### 7.1 设计优点

1. **关注点分离**：搜索执行与序列化完全解耦，`ResultContainer` 是中间契约
2. **单一数据源**：所有格式共享同一份 `ResultContainer` 数据，保证一致性
3. **可扩展性**：新增格式只需添加新的序列化分支和可选模板
4. **防御式编程**：双重验证（硬编码列表 + 配置白名单）确保安全

### 7.2 可改进点

1. **缺失标准内容协商**：不支持 `Accept` 头，不符合 RESTful 最佳实践
2. **分支式扩展**：当前使用 `if-elif` 分支选择序列化器，可改为注册表模式：

```python
# 建议的改进方向：注册表模式
SERIALIZERS = {
    'json': json_serializer,
    'csv': csv_serializer,
    'rss': rss_serializer,
    'html': html_serializer,
}

# 替代多个 if-elif
serializer = SERIALIZERS.get(output_format, SERIALIZERS['html'])
return serializer(search_query, result_container)
```

3. **MIME 类型响应头**：部分响应的 `Content-Type` 可以更精确：
   - JSON: `application/json` ✓
   - CSV: `application/csv`（标准应为 `text/csv`）
   - RSS: `text/xml`（标准应为 `application/rss+xml`）

### 7.3 扩展新格式的步骤

如需新增格式（如 XML、YAML），需要：

1. 在 `OUTPUT_FORMATS` 中添加新格式名
2. 在 `webapp.py:search()` 中添加新的序列化分支
3. 实现对应的序列化函数（或模板）
4. 设置正确的 MIME 类型

---

## 八、总结

SearXNG 的内容协商与序列化机制采用了**简单直接**的设计哲学：

- **协商依据**：查询参数 `format`（而非 HTTP Accept 头）
- **决策流程**：参数获取 → 硬编码验证 → 配置白名单验证 → 回退策略
- **序列化模式**：无模板序列化（JSON/CSV）+ 模板驱动序列化（RSS/HTML）
- **数据契约**：`ResultContainer` 作为所有格式的共享数据源
- **类型桥梁**：`Result.as_dict()` 实现强类型到序列化格式的转换

这套机制虽然不符合严格的 RESTful 内容协商规范，但在搜索引擎场景下具有实现简单、调试方便、用户可控等优点，是一个务实的工程选择。
