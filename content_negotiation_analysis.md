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

在 [settings_defaults.py](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/settings_defaults.py#L207-L207) 的 SCHEMA 中，`search.formats` 配置项的默认值即为 `OUTPUT_FORMATS`：

```python
'search': {
    'formats': SettingsValue(list, OUTPUT_FORMATS),
    ...
}
```

这意味着：
- 默认启用所有 4 种格式
- 管理员可以在 `settings.yml` 中限制可用格式
- 配置值为 `list` 类型，不做子项校验（不同于 `SettingSublistValue`），完全信任管理员输入

---

## 三、格式决策的三道关卡

内容协商的核心逻辑位于 [webapp.py](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/webapp.py#L619-L785) 的 `search()` 视图函数。格式决策经历三道关卡，每道关卡的语义截然不同：

### 关卡一：参数提取与默认值

```python
output_format = sxng_request.form.get('format', 'html')
```

未携带 `format` 参数的请求统一视为 HTML。这是最常见的用户浏览场景——浏览器地址栏直接搜索，无显式格式声明。

### 关卡二：非法格式回落

```python
if output_format not in OUTPUT_FORMATS:
    output_format = 'html'
```

当 `format` 参数值不在代码硬编码的 `OUTPUT_FORMATS` 列表中时，**静默回落**为 HTML，而非报错。这是一种容错设计：无论是用户手误（`?format=xlsx`）、爬虫探测还是遗留参数，都不会被打断，而是以最通用的 HTML 形式返回结果。

**关键语义**：回落仅发生在"代码不认识这个格式名"时，回落的结果是"给你能看的东西"。

### 关卡三：配置禁用拦截

```python
if output_format not in settings['search']['formats']:
    flask.abort(403)
```

当格式名合法但被管理员在 `settings.yml` 中排除时，**直接拒绝**，返回 HTTP 403 Forbidden。这不是容错，而是访问控制。

**关键语义**：拦截发生在"代码认识这个格式名，但实例策略不允许使用"时，拒绝的结果是"你无权以这种方式获取数据"。

### 两道关卡的语义差异

| 维度 | 关卡二（非法格式回落） | 关卡三（配置禁用拦截） |
|------|----------------------|----------------------|
| 触发条件 | 格式名不在硬编码列表中 | 格式名合法但不在配置白名单中 |
| 设计意图 | 容错：给用户一个可用的响应 | 控制：禁止该格式的访问 |
| 响应行为 | 静默回落为 HTML | 返回 403 中断请求 |
| 安全含义 | 无——格式本身不存在，无法利用 | 有——格式存在但被策略禁用，必须阻止绕过 |
| 典型场景 | `?format=yaml`、`?format=undefined` | 管理员仅开放 HTML，禁止 API 式批量抓取 |

**一个微妙之处**：关卡二的回落发生在关卡三之前。这意味着如果管理员配置了 `formats: [html]`，请求 `?format=yaml` 会先被关卡二回落为 `html`，然后通过关卡三（`html` 在白名单中），最终正常返回 HTML 页面——而非 403。只有合法但被禁用的格式（如 `?format=json`）才会触发 403。换言之，**"写错了"得到宽容，"写对了但没权限"被严格拦截**。

---

## 四、结果页面的格式下载入口

### 4.1 入口的数据来源

HTML 结果页面侧栏中的"Download results"区域，其可用格式按钮不是硬编码的，而是由配置白名单动态生成。数据源在 [render()](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/webapp.py#L420-L420) 函数中注入模板上下文：

```python
kwargs['search_formats'] = [x for x in settings['search']['formats'] if x != 'html']
```

`search_formats` 列表是配置白名单减去 `html` 后的结果——因为用户已经在 HTML 页面上了，无需再提供"下载为 HTML"的入口。

### 4.2 入口的渲染逻辑

在 [results.html](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/templates/simple/results.html#L54-L56) 中，仅当 `search_formats` 非空时才渲染下载入口：

```jinja2
{%- if search_formats -%}
  {%- include 'simple/elements/apis.html' -%}
{%- endif -%}
```

[apis.html](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/templates/simple/elements/apis.html) 模板为每个可用格式生成一个表单提交按钮，表单中携带完整的搜索上下文（查询词、分类、页码、语言、时间范围、安全搜索等），并通过隐藏字段 `format` 指定目标格式：

```jinja2
{%- for output_type in search_formats -%}
  <form method="{{ method or 'POST' }}" action="{{ url_for('search') }}">
    <input type="hidden" name="q" value="{{ q|e }}">
    ...
    <input type="hidden" name="format" value="{{ output_type }}">
    <input type="submit" role="link" value="{{ output_type }}">
  </form>
{%- endfor -%}
```

### 4.3 RSS 的双重入口

除了"Download results"侧栏，RSS 格式还有一个额外入口：HTML 页面 `<head>` 中的 `<link rel="alternate">` 标签，供浏览器和 RSS 阅读器自动发现订阅源（[results.html#L11](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/templates/simple/results.html#L11)）：

```jinja2
<link rel="alternate" type="application/rss+xml" title="Searx search: {{ q|e }}"
  href="{{ url_for('search', _external=True) }}?q={{ q|urlencode }}&amp;format=rss&amp;...">
```

**注意**：这个 RSS 自动发现链接是硬编码在模板中的，不受 `search_formats` 变量控制。即使用户无法通过侧栏按钮点击到 RSS（如管理员禁用了 RSS），浏览器仍可能通过 `<link>` 标签发现该端点——但此时直接访问会被关卡三拦截返回 403。

---

## 五、完整链路：从请求到格式输出

### 5.1 请求级链路

```
用户请求 /search?q=test&format=json
    │
    ├─ ① 参数提取：format='json'，缺省='html'
    │
    ├─ ② 关卡二：'json' ∈ OUTPUT_FORMATS → 通过，无回落
    │
    ├─ ③ 关卡三：'json' ∈ settings['search']['formats']?
    │    ├─ 是 → 继续搜索
    │    └─ 否 → abort(403)，链路终止
    │
    ├─ ④ 执行搜索，得到 ResultContainer
    │
    └─ ⑤ 按 output_format 分发到对应序列化器
         └─ JSON → webutils.get_json_response() → Response(mimetype='application/json')
```

### 5.2 页面级链路（下载入口与关卡的联动）

```
管理员配置 settings.yml: search.formats = [html]
    │
    ├─ 启动时：SCHEMA 验证，settings['search']['formats'] = ['html']
    │
    ├─ 渲染 HTML 结果页时：
    │    render() 计算 search_formats = ['html'] - ['html'] = []
    │    results.html: {% if search_formats %} → False → 不渲染 apis.html
    │    侧栏无"Download results"入口
    │
    └─ 直接请求 /search?q=test&format=json：
         关卡三：'json' ∉ ['html'] → abort(403)
```

```
管理员配置 settings.yml: search.formats = [html, csv, json, rss]
    │
    ├─ 启动时：settings['search']['formats'] = ['html', 'csv', 'json', 'rss']
    │
    ├─ 渲染 HTML 结果页时：
    │    render() 计算 search_formats = ['csv', 'json', 'rss']
    │    results.html: {% if search_formats %} → True → 渲染 apis.html
    │    侧栏显示三个按钮：csv | json | rss
    │
    └─ 点击 json 按钮 → POST /search, format=json
         关卡三：'json' ∈ ['html','csv','json','rss'] → 通过 → 正常返回 JSON
```

### 5.3 非法格式与配置禁用的差异化处理链路

```
请求 /search?q=test&format=yaml
    │
    ├─ ② 关卡二：'yaml' ∉ OUTPUT_FORMATS → output_format 回落为 'html'
    │
    ├─ ③ 关卡三：'html' ∈ settings['search']['formats'] → 通过
    │    （即使配置只允许 html，也能通过）
    │
    └─ ⑤ 序列化：按 HTML 输出，用户看到正常搜索结果页

请求 /search?q=test&format=json（管理员禁用了 json）
    │
    ├─ ② 关卡二：'json' ∈ OUTPUT_FORMATS → 通过，不回落
    │
    ├─ ③ 关卡三：'json' ∉ settings['search']['formats'] → abort(403)
    │
    └─ 链路终止，用户看到 403 错误页
```

---

## 六、序列化器实现

SearXNG 为每种格式实现了独立的序列化逻辑，分为两类：

### 6.1 无模板序列化（JSON、CSV）

#### JSON 序列化器

[webutils.py#get_json_response](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/webutils.py#L162-L175) 将 `ResultContainer` 中的各类结果组装为字典，再通过自定义 [JSONEncoder](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/webutils.py#L149-L159) 序列化。`JSONEncoder` 处理 `msgspec.Struct` → 内置类型、`datetime` → ISO 字符串、`timedelta` → 秒数、`set` → 列表等特殊转换。

#### CSV 序列化器

[webutils.py#write_csv_response](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/webutils.py#L113-L146) 将结果按行写入 CSV，固定列模式为 `(title, url, content, host, engine, score, type)`，通过 `type` 列区分 `result`、`answer`、`suggestion`、`correction` 四种行类型。[CSVWriter](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/webutils.py#L85-L110) 封装了增量编码，确保 UTF-8 兼容。

### 6.2 模板驱动序列化（RSS、HTML）

#### RSS 序列化器

使用 Jinja2 模板 [opensearch_response_rss.xml](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/templates/simple/opensearch_response_rss.xml) 渲染，遵循 OpenSearch RSS 规范，输出 `text/xml`。

#### HTML 序列化器

使用 Jinja2 模板 [results.html](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/templates/simple/results.html)，是最复杂的序列化路径。HTML 路径独有关键词高亮预处理（[webapp.py#L702-L706](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/webapp.py#L702-L706)），以及按模板分组的 open/close_group 标记逻辑。

---

## 七、错误处理的内容协商

错误响应也遵循相同的格式协商机制，在 [index_error](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/webapp.py#L553-L579) 函数中实现。但需注意：由于关卡二的存在，`index_error` 收到的 `output_format` 参数永远不会是非法值——非法值已被回落为 `html`。因此 `index_error` 只需处理四种合法格式各自的错误表现形态。

---

## 八、Result 类型系统与序列化桥梁

所有结果都继承自 [Result](file:///d:/fz/0601-1/solo-dogfeeding/code/40-searxng/searx/result_types/_base.py#L228-L337) 基类（基于 `msgspec.Struct`）。`as_dict()` 方法是序列化的关键桥梁——所有格式的序列化最终都依赖这个方法将强类型对象转换为字典，JSON 序列化器的 `_.as_dict()` 和 CSV 序列化器的 `res.as_dict()` 均出自此处。

---

## 九、架构设计分析

### 9.1 设计优点

1. **关注点分离**：搜索执行与序列化完全解耦，`ResultContainer` 是中间契约
2. **单一数据源**：所有格式共享同一份 `ResultContainer` 数据，保证一致性
3. **可扩展性**：新增格式只需添加新的序列化分支和可选模板
4. **防御式编程**：双重验证（硬编码列表 + 配置白名单）确保安全
5. **入口与关卡联动**：页面下载入口由配置白名单动态生成，不可能出现"按钮指向被禁格式"的矛盾

### 9.2 可改进点

1. **RSS 自动发现链接不受配置控制**：`<link rel="alternate">` 硬编码在模板中，不受 `search_formats` 约束，可能导致用户发现一个 403 端点
2. **分支式扩展**：当前使用 `if-elif` 分支选择序列化器，可改为注册表模式
3. **MIME 类型响应头**：部分响应的 `Content-Type` 可以更精确（CSV 应为 `text/csv`，RSS 应为 `application/rss+xml`）

---

## 十、总结

SearXNG 的内容协商与序列化机制采用了**简单直接**的设计哲学，其核心链路由三个环节构成：

- **格式决策**：参数提取（默认 HTML）→ 非法格式回落（容错，静默回 HTML）→ 配置禁用拦截（访问控制，403 中断）
- **入口联动**：页面下载入口由配置白名单动态生成（`search_formats = 配置白名单 - html`），确保可见入口与可用端点始终一致
- **序列化分发**：无模板序列化（JSON/CSV）+ 模板驱动序列化（RSS/HTML），共享 `ResultContainer` 数据源

这套机制虽然不符合严格的 RESTful 内容协商规范，但在搜索引擎场景下具有实现简单、调试方便、用户可控等优点，是一个务实的工程选择。
