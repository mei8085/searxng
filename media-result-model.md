# 多媒体结果建模与渲染链路分析

本文档详细梳理 SearXNG 中图片与视频两类多媒体结果从数据建模、引擎适配、结果处理到模板渲染的完整链路。

## 1. 数据模型层

### 1.1 核心模型定义

**文件**: `searx/result_types/_base.py`

SearXNG 使用 `msgspec.Struct` 作为结果类型的基类，多媒体结果目前主要通过 `MainResult` 类进行建模，尚未定义专门的 `ImageResult` 或 `VideoResult` 子类。

#### Result 基类 (`searx/result_types/_base.py:228`)

```python
class Result(msgspec.Struct, kw_only=True):
    url: str | None = None           # 结果关联的链接
    engine: str | None = ""          # 来源引擎名称
    parsed_url: ParseResult | None = None  # 解析后的URL对象
```

#### MainResult 主结果类 (`searx/result_types/_base.py:339`)

继承自 `Result`，包含多媒体专属字段：

```python
class MainResult(Result):
    template: str = "default.html"   # 渲染模板名称
    
    # 通用字段
    title: str = ""                  # 标题
    content: str = ""                # 描述内容
    
    # --- 多媒体专属字段 ---
    img_src: str = ""                # 图片/视频原图URL
    thumbnail: str = ""              # 缩略图URL (视频结果)
    thumbnail_src: str = ""          # 缩略图URL (图片结果)
    iframe_src: str = ""             # 视频嵌入iframe URL
    audio_src: str = ""              # 音频源URL
    
    # 元数据字段
    publishedDate: datetime | None = None  # 发布日期
    length: timedelta | None = None        # 视频时长
    views: str = ""                        # 观看次数
    author: str = ""                       # 作者
    metadata: str = ""                     # 元数据
```

#### LegacyResult 兼容类 (`searx/result_types/_base.py:428`)

为了向后兼容，旧版引擎返回的字典会被包装为 `LegacyResult`，它模拟了 `Result` 和 `MainResult` 的字段接口。

### 1.2 多媒体字段分类

| 字段 | 类型 | 适用类型 | 说明 |
|------|------|----------|------|
| `img_src` | str | 图片/视频 | 原始图片/视频缩略图URL |
| `thumbnail` | str | 视频 | 视频缩略图URL |
| `thumbnail_src` | str | 图片 | 图片缩略图URL |
| `iframe_src` | str | 视频 | 视频嵌入播放器URL |
| `resolution` | str | 图片 | 图片分辨率 (如 "1920 x 1080") |
| `img_format` | str | 图片 | 图片格式 (如 "JPG", "PNG") |
| `filesize` | str | 图片 | 文件大小 (如 "2.3 MB") |
| `length` | timedelta/str | 视频 | 视频时长 |
| `views` | str | 视频 | 观看次数 |
| `author` | str | 图片/视频 | 作者/创作者 |
| `source` | str | 图片 | 来源网站 |

---

## 2. 引擎适配层

各搜索引擎适配层负责将不同来源的原始数据归一化为统一的结果格式。

### 2.1 图片引擎适配

图片引擎统一设置 `template: "images.html"` 标识。

#### Bing 图片引擎 (`searx/engines/bing_images.py:88-99`)

```python
results.append({
    "template": "images.html",
    "url": metadata["purl"],              # 来源页面URL
    "thumbnail_src": metadata["turl"],    # 缩略图URL
    "img_src": metadata["murl"],          # 原图URL
    "content": metadata.get("desc"),      # 描述
    "title": title,                       # 标题
    "source": source,                     # 来源
    "resolution": img_format[0],          # 分辨率
    "img_format": img_format[1] if len(img_format) >= 2 else None,
})
```

#### Google 图片引擎 (`searx/engines/google_images.py:92-101`)

```python
result_item = {
    'url': item["result"]["referrer_url"],
    'title': item["result"]["page_title"],
    'content': item["text_in_grid"]["snippet"],
    'source': item["result"]["site_title"],
    'resolution': f'{item["original_image"]["width"]} x {item["original_image"]["height"]}',
    'img_src': item["original_image"]["url"],
    'thumbnail_src': item["thumbnail"]["url"],
    'template': 'images.html',
}
```

#### Wallhaven 图片引擎 (`searx/engines/wallhaven.py:73-85`)

```python
results.append({
    'template': 'images.html',
    'title': '',
    'content': f"{result['category']} / {result['purity']}",
    'url': result['url'],
    'img_src': result['path'],
    'thumbnail_src': result['thumbs']['small'],
    'resolution': result['resolution'].replace('x', ' x '),
    'publishedDate': datetime.strptime(result['created_at'], '%Y-%m-%d %H:%M:%S'),
    'img_format': result['file_type'],
    'filesize': humanize_bytes(result['file_size']),
})
```

### 2.2 视频引擎适配

视频引擎统一设置 `template: "videos.html"` 标识。

#### YouTube 视频引擎 (`searx/engines/youtube_noapi.py:79-89`)

```python
results.append({
    'url': base_youtube_url + section['videoId'],
    'title': ' '.join(x['text'] for x in section['title']['runs']),
    'content': content,
    'author': section['ownerText']['runs'][0]['text'],
    'length': section['lengthText']['simpleText'],
    'template': 'videos.html',
    'iframe_src': 'https://www.youtube-nocookie.com/embed/' + section['videoId'],
    'thumbnail': section['thumbnail']['thumbnails'][-1]['url'],
})
```

#### Dailymotion 视频引擎 (`searx/engines/dailymotion.py:175-188`)

```python
item = {
    "template": "videos.html",
    "url": url,
    "title": title,
    "content": content,
    "publishedDate": publishedDate,
    "length": length,
    "thumbnail": thumbnail,
}
if res["allow_embed"]:
    item["iframe_src"] = iframe_src.format(video_id=res["id"])
```

#### Bilibili 视频引擎 (`searx/engines/bilibili.py:83-94`)

```python
results.append({
    "title": title,
    "url": url,
    "content": description,
    "author": author,
    "publishedDate": formatted_date,
    "length": duration,
    "thumbnail": thumbnail,
    "iframe_src": iframe_url,
    "template": "videos.html",
})
```

### 2.3 归一化处理要点

1. **模板标识**: 通过 `template` 字段指定渲染模板
2. **URL字段**: 区分 `url` (来源页面)、`img_src` (媒体资源)、`thumbnail_src`/`thumbnail` (缩略图)
3. **URL过滤**: `_filter_urls` 函数统一处理所有URL字段，支持代理、过滤等操作
   - 处理字段包括: `["url", "iframe_src", "audio_src", "img_src", "thumbnail_src", "thumbnail"]`

---

## 3. 结果处理层

**文件**: `searx/results.py`

### 3.1 ResultContainer 结果容器

负责收集、合并、排序来自不同引擎的结果：

```python
class ResultContainer:
    main_results_map: dict[int, MainResult | LegacyResult]  # 主结果哈希映射
    infoboxes: list[LegacyResult]                          # 信息框
    suggestions: set[str]                                  # 搜索建议
    answers: AnswerSet                                     # 答案
    corrections: set[str]                                  # 拼写校正
```

### 3.2 结果合并逻辑 (`searx/results.py:157-188`)

```python
def _merge_main_result(self, result: MainResult | LegacyResult, position: int):
    result_hash = hash(result)
    merged = self.main_results_map.get(result_hash)
    if not merged:
        result.positions = [position]
        self.main_results_map[result_hash] = result
        return
    merge_two_main_results(merged, result)
    merged.positions.append(position)
```

### 3.3 哈希计算

图片结果的哈希值用于去重 (`searx/result_types/_base.py:527-530`)：

```python
if self.template == "images.html":
    return hash(f"{self.template}|{self.url}|{self.img_src}")
```

普通结果的哈希值包含 `template`、`parsed_url` 和 `img_src`。

### 3.4 结果排序 (`searx/results.py:197-253`)

`get_ordered_results()` 方法：
1. 按 `score` 降序排序
2. 按类别和模板分组，同类结果聚合显示
3. 分组键: `f"{res.category}:{res.template}:{'img_src' if (res.thumbnail or res.img_src) else ''}"`

---

## 4. 模板分发与渲染层

### 4.1 模板分发机制

**文件**: `searx/webapp.py:247-251`

```python
def get_result_template(theme_name: str, template_name: str):
    themed_path = theme_name + '/result_templates/' + template_name
    if themed_path in result_templates:
        return themed_path
    return 'result_templates/' + template_name
```

**调用位置**: `searx/templates/simple/results.html:69`

```jinja2
{% include get_result_template('simple', result['template']) %}
```

### 4.2 图片结果模板

**文件**: `searx/templates/simple/result_templates/images.html`

#### 结构说明

```html
<article class="result result-images">
    <!-- 缩略图链接 -->
    <a href="{{ result.img_src }}">
        <img src="{{ image_proxify(result.thumbnail_src or result.img_src) }}">
        {% if result.resolution %}<span class="image_resolution">{{ result.resolution }}</span>{% endif %}
        <span class="title">{{ result.title }}</span>
        <span class="source">{{ result.parsed_url.netloc }}</span>
    </a>
    
    <!-- 详情面板 -->
    <div class="detail">
        <img data-src="{{ image_proxify(result.img_src) }}">
        <div class="result-images-labels">
            <h4>{{ result.title }}</h4>
            <p class="result-content">{{ result.content }}</p>
            <p class="result-author">{% if result.author %}Author: {{ result.author }}{% endif %}</p>
            <p class="result-resolution">{% if result.resolution %}Resolution: {{ result.resolution }}{% endif %}</p>
            <p class="result-format">{% if result.img_format %}Format: {{ result.img_format }}{% endif %}</p>
            <p class="result-filesize">{% if result.filesize %}Filesize: {{ result.filesize }}{% endif %}</p>
            <p class="result-source">{% if result.source %}Source: {{ result.source }}{% endif %}</p>
            <p class="result-engine">Engine: {{ result.engine }}</p>
            <p class="result-url">View source: <a href="{{ result.url }}">{{ result.url }}</a></p>
        </div>
    </div>
</article>
```

#### 图片模板专属字段消费

| 模板字段 | 数据源字段 | 说明 |
|----------|------------|------|
| 主图链接 | `result.img_src` | 点击缩略图跳转到原图 |
| 缩略图 | `result.thumbnail_src` 或 `result.img_src` | 列表显示的小图 |
| 分辨率 | `result.resolution` | 显示在缩略图右上角 |
| 标题 | `result.title` | 缩略图下方标题 |
| 来源域名 | `result.parsed_url.netloc` | 缩略图下方来源 |
| 详情图 | `result.img_src` | 详情面板大图 |
| 作者 | `result.author` | 详情面板元数据 |
| 格式 | `result.img_format` | 详情面板元数据 |
| 文件大小 | `result.filesize` | 详情面板元数据 |
| 来源 | `result.source` | 详情面板元数据 |

### 4.3 视频结果模板

**文件**: `searx/templates/simple/result_templates/videos.html`

#### 结构说明

```jinja2
{{ result_header(result, favicons, image_proxify) }}
{{ result_sub_header(result) }}

{% if result.iframe_src %}
<p class="altlink">
    <a class="btn-collapse collapsed media-loader disabled_if_nojs" 
       data-target="#result-video-{{ index }}">
        {{ icon_small('film') }} {{ _('show video') }}
    </a>
</p>
{% endif %}

{% if result.content %}
  <p class="content">{{ result.content|safe }}</p>
{% else %}
  <p class="content empty_element">{{ _('This site did not provide any description.')|safe }}</p>
{% endif %}

{{ result_sub_footer(result) }}

{% if result.iframe_src %}
<div id="result-video-{{ index }}" class="embedded-video invisible">
  {{ iframe(result.iframe_src) }}
</div>
{% endif %}

{{ result_footer(result) }}
```

#### 视频模板依赖的宏

**文件**: `searx/templates/simple/macros.html`

##### result_header 宏 (`macros.html:21-35`)

```jinja2
{% macro result_header(result, favicons, image_proxify) -%}
<article class="result result-videos category-videos">
  <a href="{{ result.url }}" class="url_header">...</a>
  {% if result.thumbnail %}
    <a href="{{ result.url }}" class="thumbnail_link">
      <img class="thumbnail" src="{{ image_proxify(result.thumbnail) }}" loading="lazy">
      {% if result.length %}<span class="thumbnail_length">{{ result.length }}</span>{% endif %}
    </a>
  {% endif %}
  <h3><a href="{{ result.url }}">{{ result.title|safe }}</a></h3>
{%- endmacro %}
```

##### result_sub_header 宏 (`macros.html:38-45`)

```jinja2
{% macro result_sub_header(result) -%}
  {% if result.publishedDate %}<time class="published_date">{{ result.publishedDate }}</time>{% endif %}
  {% if result.length and not result.thumbnail %}<div class="result_length">Length: {{ result.length }}</div>{% endif %}
  {% if result.views %}<div class="result_views">Views: {{ result.views }}</div>{% endif %}
  {% if result.author %}<div class="result_author">Author: {{ result.author }}</div>{% endif %}
{%- endmacro %}
```

##### iframe 宏 (`macros.html:72-79`)

```jinja2
{% macro iframe(iframe_src) -%}
  <iframe data-src="{{iframe_src}}" frameborder="0" allowfullscreen
    {% if result.parsed_url.hostname in ("www.youtube.com", ) %}
    allow="picture-in-picture" referrerpolicy="origin"
    {%- endif -%}>
  </iframe>
{%- endmacro %}
```

#### 视频模板专属字段消费

| 模板位置 | 数据源字段 | 说明 |
|----------|------------|------|
| 缩略图 | `result.thumbnail` | 左侧视频缩略图 |
| 时长 | `result.length` | 缩略图右下角时长标签 |
| 标题 | `result.title` | 结果标题 |
| 描述 | `result.content` | 结果描述 |
| 发布日期 | `result.publishedDate` | 子头部元数据 |
| 观看次数 | `result.views` | 子头部元数据 |
| 作者 | `result.author` | 子头部元数据 |
| 嵌入播放器 | `result.iframe_src` | 可折叠的视频播放器 |

---

## 5. 完整链路图

```
引擎适配层 (Engine Adapters)
    │
    ▼
[bing_images.py] ──┐
[google_images.py] ─┤
[pexels.py]        ├─► 归一化为 dict / LegacyResult
[unsplash.py]      │    ├─ template: "images.html"
[wallhaven.py]    ─┘    ├─ img_src, thumbnail_src
                         └─ resolution, img_format, filesize...

[bing_videos.py]  ──┐
[youtube_noapi.py] ─┤
[dailymotion.py]    ├─► 归一化为 dict / LegacyResult
[vimeo.py]          │    ├─ template: "videos.html"
[bilibili.py]      ─┘    ├─ thumbnail, iframe_src
                         └─ length, views, author...
    │
    ▼
结果处理层 (ResultContainer)
    │
    ├─ extend() - 收集结果，调用 normalize_result_fields()
    ├─ _merge_main_result() - 按哈希去重合并
    │   └─ hash() - 计算结果哈希 (template + url + img_src)
    └─ get_ordered_results() - 按分数排序 + 按类别分组
    │
    ▼
模板分发层 (Template Dispatching)
    │
    ├─ get_result_template('simple', result['template'])
    │   ├─ "images.html" → simple/result_templates/images.html
    │   └─ "videos.html" → simple/result_templates/videos.html
    │
    ▼
模板渲染层 (Template Rendering)
    │
    ├─ images.html
    │   ├─ 缩略图网格布局
    │   ├─ 点击展开详情面板
    │   └─ 消费: img_src, thumbnail_src, resolution, img_format...
    │
    └─ videos.html
        ├─ 标题 + 缩略图 + 描述布局
        ├─ 可折叠嵌入播放器
        └─ 消费: thumbnail, iframe_src, length, author...
```

---

## 6. 关键设计要点

### 6.1 字段复用设计
- 图片和视频共享 `MainResult` 类，通过 `template` 字段区分渲染方式
- 图片使用 `thumbnail_src`，视频使用 `thumbnail`（历史原因）
- 两者都使用 `img_src` 存储主要媒体资源URL

### 6.2 URL统一处理
所有URL字段经过 `_filter_urls()` 统一处理：
```python
url_fields = ["url", "iframe_src", "audio_src", "img_src", "thumbnail_src", "thumbnail"]
```
支持图片代理、URL重写、安全过滤等横切关注点。

### 6.3 模板驱动渲染
- 引擎只需要设置 `template` 字段，无需关心具体渲染逻辑
- 新增结果类型只需添加新模板文件，无需修改核心分发逻辑
- 宏 `macros.html` 提供可复用组件，减少模板重复代码

### 6.4 渐进式类型迁移
当前处于从 `LegacyResult` (dict) 向 `MainResult` (msgspec.Struct) 迁移阶段：
- 新引擎推荐使用 `EngineResults` 包装器
- 旧引擎返回 dict 会自动转换为 `LegacyResult`
- 未来可能引入专门的 `ImageResult` 和 `VideoResult` 子类
