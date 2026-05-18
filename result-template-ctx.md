# SearxNG 搜索结果渲染上下文分析

## 概述

本文档深入分析 SearxNG 后端搜索结果在交付前端模板之前的渲染上下文准备过程，包括上下文字段、模型层来源、模板分发机制，以及图像、视频、信息盒、即时答案等特殊结果类型的处理逻辑。

---

## 1. 核心数据模型层

### 1.1 结果容器 (ResultContainer)

**文件位置**：`searx/results.py:53`

ResultContainer 是所有搜索结果的聚合容器，负责收集、排序、去重和合并结果。

**核心字段**：

| 字段 | 类型 | 描述 |
|------|------|------|
| `main_results_map` | `dict[int, MainResult \| LegacyResult]` | 主结果映射表，以结果哈希为键 |
| `infoboxes` | `list[LegacyResult]` | 信息盒结果列表 |
| `suggestions` | `set[str]` | 搜索建议集合 |
| `answers` | `AnswerSet` | 即时答案集合 |
| `corrections` | `set[str]` | 拼写纠正集合 |
| `unresponsive_engines` | `set[UnresponsiveEngine]` | 无响应引擎集合 |
| `timings` | `list[Timing]` | 引擎响应时间记录 |
| `redirect_url` | `str \| None` | 外部跳转 URL（用于 bang 语法） |
| `paging` | `bool` | 是否支持分页 |
| `engine_data` | `dict[str, dict[str, str]]` | 引擎额外数据 |

**关键方法**：
- `extend(engine_name, results)`: 添加引擎返回的结果
- `get_ordered_results()`: 返回排序后的主结果列表
- `close()`: 关闭容器并计算结果分数

### 1.2 结果类型层次结构

**文件位置**：`searx/result_types/_base.py`

```
Result (msgspec.Struct)
├── MainResult
│   └── 主结果区显示的所有结果类型
└── BaseAnswer
    ├── Answer (简单文本答案)
    ├── Translations (翻译结果)
    └── WeatherAnswer (天气答案)
```

#### Result 基类字段 (`searx/result_types/_base.py:228`)

| 字段 | 类型 | 描述 |
|------|------|------|
| `url` | `str \| None` | 结果关联的链接 |
| `engine` | `str` | 来源引擎名称 |
| `parsed_url` | `urllib.parse.ParseResult \| None` | 解析后的 URL 对象 |

#### MainResult 扩展字段 (`searx/result_types/_base.py:339`)

| 字段 | 类型 | 描述 |
|------|------|------|
| `template` | `str` | 渲染模板名称，默认为 `"default.html"` |
| `title` | `str` | 结果标题 |
| `content` | `str` | 结果描述/摘要 |
| `img_src` | `str` | 图像 URL |
| `thumbnail` | `str` | 缩略图 URL |
| `iframe_src` | `str` | 嵌入式 iframe URL |
| `audio_src` | `str` | 音频 URL |
| `publishedDate` | `datetime \| None` | 发布日期 |
| `length` | `timedelta \| None` | 播放时长 |
| `views` | `str` | 观看次数 |
| `author` | `str` | 作者 |
| `metadata` | `str` | 元数据 |
| `priority` | `"" \| "high" \| "low"` | 结果优先级 |
| `engines` | `set[str]` | 找到该结果的所有引擎 |
| `score` | `float` | 结果排序分数 |
| `category` | `str` | 结果分类 |
| `open_group` / `close_group` | `bool` | 模板分组标记 |

#### LegacyResult (`searx/result_types/_base.py:428`)

用于向后兼容的字典包装类，支持传统引擎返回的未类型化字典。

### 1.3 答案类型

**文件位置**：`searx/result_types/answer.py`

| 类型 | 模板 | 字段 |
|------|------|------|
| `Answer` | `answer/legacy.html` | `answer` (答案文本) |
| `Translations` | `answer/translations.html` | `translations` (翻译列表) |
| `WeatherAnswer` | `answer/weather.html` | `current`, `forecasts`, `service` |

---

## 2. 渲染上下文准备流程

### 2.1 搜索处理流程

**文件位置**：`searx/webapp.py:620-785`

```
1. 获取搜索参数 → 构建 SearchQuery
2. 执行搜索 → SearchWithPlugins.search()
   ├─ 检查外部 bang 跳转
   ├─ 调用 answerers (内部答案引擎)
   └─ 并行调用各搜索引擎
3. 结果收集到 ResultContainer
4. 结果后处理
   ├─ 高亮搜索词
   ├─ 设置模板分组标记 (open_group/close_group)
   └─ 构建建议/纠正 URL
5. 调用 render() 传递上下文
```

### 2.2 传递给模板的完整上下文字段

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

### 2.3 结果后处理细节

**高亮处理** (`searx/webapp.py:703-706`)：
```python
result['content'] = highlight_content(escape(result['content'][:1024]), search_query.query)
result['title'] = highlight_content(escape(result['title'] or ''), search_query.query)
```

**模板分组标记** (`searx/webapp.py:708-718`)：
- 当 `result.template` 变化时，设置 `open_group = True`
- 前一个结果设置 `close_group = True`
- 用于前端将相同模板的结果分组显示

---

## 3. 模板分发机制

### 3.1 模板路径解析

**文件位置**：`searx/webutils.py:201-209` 和 `searx/webapp.py:247-251`

```python
def get_result_template(theme_name: str, template_name: str):
    themed_path = theme_name + '/result_templates/' + template_name
    if themed_path in result_templates:
        return themed_path
    return 'result_templates/' + template_name
```

**模板发现**：系统启动时扫描 `templates_path` 下所有 `result_templates` 目录中的 HTML 文件。

### 3.2 模板调用逻辑

**文件位置**：`searx/templates/simple/results.html:65-71`

```jinja
{% for result in results %}
    {% if result.open_group and not only_template %}
        <div class="template_group_{{ result['template']|replace('.html', '') }}">
    {% endif %}
    {% include get_result_template('simple', result['template']) %}
    {% if result.close_group and not only_template %}
        </div>
    {% endif %}
{% endfor %}
```

### 3.3 可用的结果模板

**目录位置**：`searx/templates/simple/result_templates/`

| 模板文件 | 用途 | 典型来源引擎 |
|----------|------|-------------|
| `default.html` | 默认网页结果 | 通用搜索引擎 |
| `images.html` | 图片搜索结果 | google_images, bing_images 等 |
| `videos.html` | 视频搜索结果 | bing_videos, youtube_api 等 |
| `code.html` | 代码片段结果 | github, stackexchange 等 |
| `keyvalue.html` | 键值对表格 | 词典、单位转换等 |
| `paper.html` | 学术论文结果 | google_scholar, arxiv 等 |
| `file.html` | 文件下载结果 | ftp 搜索引擎等 |
| `torrent.html` | BT 种子结果 | 1337x, piratebay 等 |
| `packages.html` | 软件包结果 | 包管理引擎 |
| `products.html` | 商品结果 | 电商引擎 |
| `map.html` | 地图结果 | openstreetmap 等 |

---

## 4. 特殊结果类型深度剖析

### 4.1 图像结果 (images.html)

**模板位置**：`searx/templates/simple/result_templates/images.html`

**上下文字段**：

| 字段 | 来源 | 描述 |
|------|------|------|
| `img_src` | 引擎返回 | 原图 URL |
| `thumbnail_src` | 引擎返回 | 缩略图 URL |
| `title` | 引擎返回 | 图片标题 |
| `content` | 引擎返回 | 图片描述 |
| `resolution` | 引擎返回 | 分辨率 (如 "1920 x 1080") |
| `author` | 引擎返回 | 作者 |
| `img_format` | 引擎返回 | 图片格式 |
| `filesize` | 引擎返回 | 文件大小 |
| `source` | 引擎返回 | 来源网站 |
| `parsed_url.netloc` | 解析 | 来源域名 |

**引擎示例** - Google Images (`searx/engines/google_images.py:91-100`)：
```python
result_item = {
    'url': item["result"]["referrer_url"],
    'title': item["result"]["page_title"],
    'content': item["text_in_grid"]["snippet"],
    'resolution': f'{item["original_image"]["width"]} x {item["original_image"]["height"]}',
    'img_src': item["original_image"]["url"],
    'thumbnail_src': item["thumbnail"]["url"],
    'template': 'images.html',
}
```

**渲染特点**：
- 缩略图点击后显示详情面板
- 支持滑动浏览
- 图片通过 `image_proxify()` 代理访问

### 4.2 视频结果 (videos.html)

**模板位置**：`searx/templates/simple/result_templates/videos.html`

**上下文字段**：

| 字段 | 来源 | 描述 |
|------|------|------|
| `title` | 引擎返回 | 视频标题 |
| `content` | 引擎返回 | 视频描述 |
| `thumbnail` | 引擎返回 | 视频缩略图 |
| `iframe_src` | 引擎返回 | 嵌入播放器 URL |
| `length` | 引擎返回 | 播放时长 |
| `views` | 引擎返回 | 观看次数 |
| `author` | 引擎返回 | 上传者 |

**引擎示例** - Bing Videos (`searx/engines/bing_videos.py:85-94`)：
```python
results.append({
    "url": metadata["murl"],
    "thumbnail": thumbnail,
    "title": metadata.get("vt", ""),
    "content": info,
    "length": metadata["du"],
    "template": "videos.html",
})
```

**渲染特点**：
- 可折叠的嵌入式播放器
- 点击 "show video" 展开 iframe
- 使用宏 `result_header`, `result_sub_header` 等统一布局

### 4.3 信息盒 (Infobox)

**模板位置**：`searx/templates/simple/elements/infobox.html`

**渲染位置**：侧边栏 (`#sidebar` 内)

**上下文字段**：

| 字段 | 来源 | 描述 |
|------|------|------|
| `infobox` | 引擎返回 | 信息盒标题 |
| `id` | 引擎返回 | 唯一标识符 (用于去重) |
| `content` | 引擎返回 | 主体内容 (HTML) |
| `img_src` | 引擎返回 | 主图 URL |
| `attributes` | `list[dict]` | 属性列表，每项含 `label`, `value`, `image` |
| `urls` | `list[dict]` | 相关链接列表，每项含 `title`, `url`, `entity` |
| `relatedTopics` | `list[dict]` | 相关主题，含搜索建议 |
| `engine` | 系统 | 来源引擎 |
| `engines` | 系统 | 所有提供该信息的引擎 |

**引擎示例** - Wikipedia (`searx/engines/wikipedia.py:197-207`)：
```python
results.append({
    "infobox": title,
    "id": wikipedia_link,
    "content": api_result.get("extract", ""),
    "img_src": api_result.get("thumbnail", {}).get("source"),
    "urls": [{"title": "Wikipedia", "url": wikipedia_link}],
})
```

**合并逻辑** (`searx/results.py:297-354`)：
- 按 `id` 字段去重合并
- 权重高的引擎内容优先
- 合并 `urls`, `attributes`, `img_src`, `content` 等字段

### 4.4 即时答案 (Answers)

**模板位置**：`searx/templates/simple/elements/answers.html`

**渲染位置**：结果区域顶部 (`#answers`)

**答案类型**：

| 类型 | 模板 | 字段示例 |
|------|------|---------|
| `Answer` | `answer/legacy.html` | `answer`: 简单文本答案 |
| `Translations` | `answer/translations.html` | `translations`: 翻译列表 |
| `WeatherAnswer` | `answer/weather.html` | `current`, `forecasts`: 天气数据 |

**模板分发** (`searx/templates/simple/elements/answers.html:3-7`)：
```jinja
{%- for answer in answers -%}
  <div class="answer">
    {%- include ("simple/" + (answer.template or "answer/legacy.html")) -%}
  </div>
{%- endfor -%}
```

**传统答案示例** (DuckDuckGo)：
```python
# 遗留 dict 格式（已弃用，推荐使用 Answer 类）
result = {
    "answer": "42",
    "url": "https://example.com/answer",
}
```

**新类型示例**：
```python
from searx.result_types.answer import Answer

result = Answer(
    answer="The answer to life, the universe, and everything",
    url="https://en.wikipedia.org/wiki/42_(number)",
)
```

---

## 5. 结果排序与分组算法

### 5.1 分数计算

**文件位置**：`searx/results.py:17-38`

```python
def calculate_score(result, priority):
    weight = 1.0
    for result_engine in result['engines']:
        weight *= float(searx.engines.engines[result_engine].weight)
    weight *= len(result['positions'])
    
    score = 0
    for position in result['positions']:
        if priority == 'low':
            continue
        if priority == 'high':
            score += weight
        else:
            score += weight / position
    return score
```

**影响因素**：
1. 引擎权重 (`engine.weight`)
2. 结果出现次数 (`len(result['positions'])`)
3. 在各引擎中的排名位置
4. 优先级标记 (`priority`)

### 5.2 分组排序

**文件位置**：`searx/results.py:210-253`

**算法逻辑**：
1. 第一遍：按 `score` 降序排序
2. 第二遍：按 `category:template:has_image` 分组
   - 每组最多 8 个结果
   - 组间最大距离 20 个位置
   - 相同类型结果尽量聚集显示

---

## 6. 插件对上下文的影响

### 6.1 结果过滤器

插件可以通过 `on_result` 钩子修改或过滤结果：

```python
# 示例：hostnames 插件设置优先级
def on_result(request, search, result):
    if is_high_priority(result.url):
        result.priority = 'high'
    return True  # 保留结果，返回 False 则丢弃
```

### 6.2 URL 过滤

通过 `Result.filter_urls()` 方法，插件可以处理所有 URL 字段：

```python
def on_result(request, search, result):
    def filter_func(result, field_name, url):
        if is_tracker_url(url):
            return False  # 移除该 URL
        return proxify(url)  # 替换为代理 URL
    result.filter_urls(filter_func)
    return True
```

---

## 7. 关键流程总结

```
用户查询
    ↓
SearchQuery 构建 (webadapter.py)
    ↓
SearchWithPlugins.search() (search/__init__.py)
    ├─ answerers 处理 (answerers/)
    ├─ 外部 bang 检查 (external_bang.py)
    └─ 引擎并行搜索 (processors/)
        ↓
ResultContainer 收集与合并 (results.py)
    ├─ 结果去重 (hash based)
    ├─ 信息盒合并
    ├─ 分数计算
    └─ 排序与分组
        ↓
webapp.py 后处理
    ├─ 标题/内容高亮
    ├─ 模板分组标记
    ├─ 建议/纠正 URL 构建
    └─ 翻译错误信息
        ↓
render(results.html) 传递上下文
    ↓
模板分发 (get_result_template)
    ├─ 主结果区 → result_templates/*.html
    ├─ 侧边栏 → elements/infobox.html
    └─ 答案区 → answer/*.html
```

---

## 8. 扩展阅读

- 结果类型文档：`docs/dev/result_types/`
- 引擎开发文档：`docs/dev/engines/`
- 插件开发文档：`docs/dev/plugins/`
- 模板目录：`searx/templates/simple/`
