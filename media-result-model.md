# 多媒体结果建模与渲染链路分析

本文档详细梳理 SearXNG 中图片与视频两类多媒体结果从数据建模、引擎适配、结果处理到模板渲染的完整链路。

---

## 1. 数据模型层：字段三级分类

### 1.1 主结果结构体 (MainResult) 字段

**文件**: `searx/result_types/_base.py:339-426`

`MainResult` 是所有主结果的基类，使用 `msgspec.Struct` 强类型定义。以下是多媒体相关字段：

| 字段 | 类型 | 默认值 | 适用类型 | 说明 |
|------|------|--------|----------|------|
| `template` | `str` | `"default.html"` | 图片/视频 | 渲染模板标识，多媒体分别为 `"images.html"` / `"videos.html"` |
| `title` | `str` | `""` | 图片/视频 | 结果标题 |
| `content` | `str` | `""` | 图片/视频 | 结果描述内容 |
| `url` | `str` | `None` | 图片/视频 | 来源页面URL |
| `img_src` | `str` | `""` | 图片/视频 | 原始图片/视频资源URL |
| `thumbnail` | `str` | `""` | 视频 | 视频缩略图URL |
| `iframe_src` | `str` | `""` | 视频 | 视频嵌入播放器URL |
| `audio_src` | `str` | `""` | 音频 | 音频源URL |
| `publishedDate` | `datetime` | `None` | 图片/视频 | 发布日期（datetime 对象，用于模板显示文本） |
| `pubdate` | `str` | `""` | 图片/视频 | 发布日期字符串（格式 `%Y-%m-%d %H:%M:%S%z`，用于 `<time datetime="">` 属性） |
| `length` | `timedelta` | `None` | 视频 | 视频时长 |
| `views` | `str` | `""` | 视频 | 观看次数（人性化格式） |
| `author` | `str` | `""` | 图片/视频 | 作者/创作者 |
| `metadata` | `str` | `""` | 视频 | 其他元数据 |
| `engine` | `str` | `""` | 图片/视频 | 来源引擎名称 |
| `engines` | `set[str]` | `set()` | 图片/视频 | 所有找到该结果的引擎集合 |
| `parsed_url` | `ParseResult` | `None` | 图片/视频 | 解析后的URL对象 |
| `category` | `str` | `""` | 图片/视频 | 结果类别（从引擎继承） |
| `score` | `float` | `0` | 图片/视频 | 结果排序分数 |

> **注意**: `thumbnail_src`、`resolution`、`img_format`、`filesize`、`source` 等字段**未在 `MainResult` 中定义**，它们属于引擎侧扩展字段，仅通过 `LegacyResult` 兼容层支持。

---

### 1.2 兼容层 (LegacyResult) 字段支持

**文件**: `searx/result_types/_base.py:428-574`

`LegacyResult` 继承自 `dict`，为向后兼容而存在，支持任意键值对。它模拟了 `MainResult` 的核心字段，同时允许引擎传递额外字段。

#### 明确模拟的字段 (`__init__` 中初始化):

```python
# Result 类字段
self["url"] = self.get("url")
self["template"] = self.get("template", "default.html")
self["engine"] = self.get("engine", "")
self["parsed_url"] = self.get("parsed_url")

# MainResult 类字段
self["title"] = self.get("title", "")
self["content"] = self.get("content", "")
self["img_src"] = self.get("img_src", "")
self["thumbnail"] = self.get("thumbnail", "")
self["priority"] = self.get("priority", "")
self["engines"] = self.get("engines", set())
self["positions"] = self.get("positions", "")
self["score"] = self.get("score", 0)
self["category"] = self.get("category", "")
self["publishedDate"] = self.get("publishedDate")
self["pubdate"] = self.get("pubdate", "")
```

#### 扩展字段支持（通过 dict 特性动态支持）:

| 字段 | 适用类型 | 说明 | 消费位置 |
|------|----------|------|----------|
| `thumbnail_src` | 图片 | 图片缩略图URL | 图片模板缩略图显示 |
| `resolution` | 图片 | 图片分辨率 (如 "1920 x 1080") | 图片模板详情面板 |
| `img_format` | 图片 | 图片格式 (如 "JPG", "image/png") | 图片模板详情面板 |
| `filesize` | 图片 | 文件大小 (如 "2.3 MB") | 图片模板详情面板 |
| `source` | 图片 | 来源网站名称 | 图片模板详情面板 |
| `duration` | 视频 | 视频时长（部分引擎使用） | 与 `length` 类似 |

#### URL 字段统一处理 (`_filter_urls`):

**文件**: `searx/result_types/_base.py:111-216`

所有 URL 字段经过统一过滤处理，支持图片代理、URL 重写等横切操作：

```python
url_fields = ["url", "iframe_src", "audio_src", "img_src", "thumbnail_src", "thumbnail"]
```

---

### 1.3 引擎侧返回字段汇总

各引擎根据自身 API 能力返回不同字段，以下是所有引擎实际使用的字段汇总：

#### 图片引擎字段使用情况

| 字段 | Bing | Google | Flickr | Pexels | Unsplash | Wallhaven | Wikicommons | Brave |
|------|------|--------|--------|--------|----------|-----------|-------------|-------|
| `template` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `url` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `title` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `content` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | - |
| `img_src` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `thumbnail_src` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `author` | - | - | ✅ | ✅ | - | - | - | - |
| `source` | ✅ | ✅ | - | - | - | - | - | ✅ |
| `resolution` | ✅ | ✅ | - | ✅ | - | ✅ | ✅ | - |
| `img_format` | ✅ | - | - | - | - | ✅ | ✅ | - |
| `filesize` | - | - | - | - | - | ✅ | ✅ | - |
| `publishedDate` | - | - | - | - | - | ✅ | - | - |

#### 视频引擎字段使用情况

| 字段 | YouTube | Dailymotion | Bilibili | Vimeo | Odysee | Piped | Peertube | Invidious | Brave | Wikicommons |
|------|---------|-------------|----------|-------|--------|-------|----------|-----------|-------|-------------|
| `template` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `url` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `title` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `content` | ✅ | ✅ | ✅ | - | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `thumbnail` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | - |
| `iframe_src` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `author` | ✅ | - | ✅ | - | ✅ | - | ✅ | ✅ | - | - |
| `length` | ✅ | ✅ | ✅ | - | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `views` | - | - | - | - | - | ✅ | ✅ | ✅ | - | - |
| `publishedDate` | - | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | - |
| `metadata` | - | - | - | - | - | - | ✅ | - | - | - |

---

## 2. 引擎适配层：归一化处理

### 2.1 图片引擎归一化

#### 核心归一化模式

所有图片引擎通过设置 `template: "images.html"` 标识为图片结果，统一返回以下核心字段：

```python
{
    "template": "images.html",
    "url": "<来源页面URL>",
    "img_src": "<原始图片URL>",
    "thumbnail_src": "<缩略图URL>",
    "title": "<标题>",
    "content": "<描述>",
    # 可选扩展字段
    "author": "<作者>",
    "source": "<来源网站>",
    "resolution": "<分辨率>",
    "img_format": "<图片格式>",
    "filesize": "<文件大小>",
}
```

#### 各引擎原始字段映射

| 引擎 | 原始URL字段 | 原始缩略图字段 | 原始标题字段 | 原始描述字段 |
|------|------------|---------------|-------------|-------------|
| **Bing Images** | `metadata["purl"]` | `metadata["turl"]` | `result.xpath(...)` | `metadata["desc"]` |
| **Google Images** | `item["result"]["referrer_url"]` | `item["thumbnail"]["url"]` | `item["result"]["page_title"]` | `item["text_in_grid"]["snippet"]` |
| **Flickr** | `build_flickr_url(owner, id)` | `photo["url_n"]` / `photo["url_z"]` | `photo["title"]` | `photo["description"]["_content"]` |
| **Pexels** | `f"{base_url}/photo/{slug}-{id}/"` | `attrs["image"]["small"]` | `attrs["title"]` | `attrs["description"]` |
| **Unsplash** | `result["links"]["html"]` | `result["urls"]["thumb"]` | `result["alt_description"]` | `result["description"]` |
| **Wallhaven** | `result["url"]` | `result["thumbs"]["small"]` | `""` | `f"{category} / {purity}"` |
| **Wikicommons** | `imageinfo["descriptionurl"]` | `imageinfo["thumburl"]` | `title` | `html_to_text(snippet)` |
| **Brave** | `result["url"]` | `result["thumbnail"]["src"]` | `result["title"]` | - |

### 2.2 视频引擎归一化

#### 核心归一化模式

所有视频引擎通过设置 `template: "videos.html"` 标识为视频结果，统一返回以下核心字段：

```python
{
    "template": "videos.html",
    "url": "<视频页面URL>",
    "title": "<视频标题>",
    "content": "<视频描述>",
    "thumbnail": "<视频缩略图URL>",
    "iframe_src": "<嵌入播放器URL>",
    # 可选扩展字段
    "author": "<作者/频道>",
    "length": "<视频时长>",
    "views": "<观看次数>",
    "publishedDate": "<发布日期>",
}
```

#### 各引擎原始字段映射

| 引擎 | 原始URL字段 | 原始缩略图字段 | 原始iframe字段 | 原始时长字段 |
|------|------------|---------------|----------------|-------------|
| **YouTube** | `base_youtube_url + videoId` | `thumbnail["thumbnails"][-1]["url"]` | `youtube-nocookie.com/embed/{videoId}` | `lengthText["simpleText"]` |
| **Dailymotion** | `res["url"]` | `res["thumbnail_360_url"]` | `dailymotion.com/embed/video/{id}` | 秒数格式化 |
| **Bilibili** | `item["arcurl"]` | `item["pic"]` | `player.bilibili.com/player.html?aid={aid}` | `parse_duration_string(duration)` |
| **Vimeo** | `base_url + videoid` | `pictures["sizes"][-1]["link"]` | `player.vimeo.com/video/{videoid}` | - |
| **Odysee** | `https://odysee.com/{name}:{claimId}` | CDN 优化 URL | `odysee.com/$/embed/{name}:{claimId}` | 秒数格式化 |
| **Piped** | `frontend_url + url` | `result["thumbnail"]` | `frontend_url/embed{url}` | `timedelta(seconds=duration)` |
| **Peertube** | `result["url"]` | `thumbnailUrl` / `previewUrl` | `result["embedUrl"]` | `timedelta(seconds=duration)` |
| **Invidious** | `base_url/watch?v={videoId}` | `videoThumbnails` 中匹配 | `base_url/embed/{videoId}` | 秒数格式化 |
| **Wikicommons** | `imageinfo["descriptionurl"]` | - | `media_url` | `timedelta(seconds=duration)` |

---

## 3. 结果处理层：合并与排序

### 3.1 日期字段标准化处理

**文件**: `searx/result_types/_base.py:219-225`

`_normalize_date_fields()` 函数统一处理发布日期字段，为模板渲染准备双格式：

```python
def _normalize_date_fields(result: "MainResult | LegacyResult"):
    if result.publishedDate:
        try:
            result.pubdate = result.publishedDate.strftime('%Y-%m-%d %H:%M:%S%z')
        except ValueError:
            result.publishedDate = None
```

| 字段 | 类型 | 生成时机 | 用途 |
|------|------|----------|------|
| `publishedDate` | `datetime` | 引擎侧返回 | 模板显示文本（Jinja2 自动格式化） |
| `pubdate` | `str` | `normalize_result_fields()` 中生成 | `<time datetime="">` 属性值（ISO 格式） |

> **重要**: 引擎只需返回 `publishedDate` (datetime 对象)，`pubdate` 由系统自动派生。模板中两者配合使用：
> ```jinja2
> <time datetime="{{ result.pubdate }}">{{ result.publishedDate }}</time>
> ```

---

### 3.2 哈希去重策略

哈希去重逻辑在 `Result`、`MainResult`、`LegacyResult` 中各有不同实现：

#### 3.2.1 Result 基类哈希 (`searx/result_types/_base.py:288-299`)

```python
def __hash__(self) -> int:
    return id(self)
```

- 基类默认使用对象身份 ID 作为哈希值
- 子类可覆盖此方法实现内容去重

#### 3.2.2 MainResult 哈希 (`searx/result_types/_base.py:405-418`)

```python
def __hash__(self) -> int:
    if not self.parsed_url:
        raise ValueError(f"missing a value in field 'parsed_url': {self}")
    url = self.parsed_url
    return hash(
        f"{self.template}"
        + f"|{url.netloc}|{url.path}|{url.params}|{url.query}|{url.fragment}"
        + f"|{self.img_src}"
    )
```

| 哈希因子 | 说明 |
|----------|------|
| `template` | 模板名称，区分结果类型 |
| `parsed_url` (netloc+path+params+query+fragment) | 不包含 scheme 的 URL，用于匹配同源结果 |
| `img_src` | 图片/视频资源 URL，区分同一页面的不同多媒体 |

> **注意**: `MainResult` **没有**图片专用分支，所有结果统一使用此算法，要求 `parsed_url` 必须存在。

#### 3.2.3 LegacyResult 哈希 (`searx/result_types/_base.py:521-547`)

```python
def __hash__(self) -> int:
    if "answer" in self:
        return hash(self["answer"])

    if self.template == "images.html":
        # 图片结果专用分支：因图片结果的 parsed_url 可能为空
        return hash(f"{self.template}|{self.url}|{self.img_src}")

    if not any(cls in self for cls in ["suggestion", "correction", "infobox", ...]):
        # 普通 URL 结果分支（含视频）
        if not self.parsed_url:
            raise ValueError(...)
        url = self.parsed_url
        return hash(
            f"{self.template}"
            + f"|{url.netloc}|{url.path}|{url.params}|{url.query}|{url.fragment}"
            + f"|{self.img_src}"
        )

    return id(self)
```

| 分支 | 触发条件 | 哈希因子 |
|------|----------|----------|
| **答案结果** | 包含 `"answer"` 键 | `answer` 字段内容 |
| **图片结果** | `template == "images.html"` | `template` + `url` + `img_src` |
| **普通结果**（含视频） | 非特殊类型且有 `parsed_url` | `template` + `parsed_url`（无 scheme） + `img_src` |
| **特殊结果** | 建议/校正/信息框等 | 对象 ID（不去重） |

> **关键区别**: 图片结果在 `LegacyResult` 中使用 `url` 而非 `parsed_url`，因为图片引擎的 `parsed_url` 可能为空。

---

### 3.3 结果分组排序

**文件**: `searx/results.py:197-253`

`get_ordered_results()` 方法按以下规则处理：

1. **分数排序**: 按 `score` 降序排列
2. **分组聚合**: 同类结果聚合显示，分组键为：
   ```python
   category = f"{res.category}:{res.template}:{'img_src' if (res.thumbnail or res.img_src) else ''}"
   ```
3. **分组约束**: 每组最多 8 个结果，组间最大距离 20 个位置

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

---

### 4.2 图片模板字段消费详情

**文件**: `searx/templates/simple/result_templates/images.html`

#### 缩略图区域消费

| 字段 | 消费位置 | 触发条件 | 渲染效果 |
|------|----------|----------|----------|
| `img_src` | 第2行: `<a href="{{ result.img_src }}">` | 始终渲染 | 点击跳转到原图 |
| `thumbnail_src` / `img_src` | 第3行: `src="{% if result.thumbnail_src %}...{% else %}...{% endif %}"` | 始终渲染，优先使用 `thumbnail_src` | 显示 200x200 缩略图 |
| `resolution` | 第4行: `{%- if result.resolution %} <span class="image_resolution">{{ result.resolution }}</span> {%- endif -%}` | **仅当 `result.resolution` 非空时显示** | 缩略图右上角显示分辨率标签 |
| `title` | 第5行: `<span class="title">{{ result.title|striptags }}</span>` | 始终渲染 | 缩略图下方显示标题 |
| `parsed_url.netloc` | 第6行: `<span class="source">{{- result.parsed_url.netloc -}}</span>` | 始终渲染 | 缩略图下方显示来源域名 |

#### 详情面板消费

| 字段 | 消费位置 | 触发条件 | 渲染效果 |
|------|----------|----------|----------|
| `img_src` | 第13行: `data-src="{{ image_proxify(result.img_src) }}"` | 点击缩略图时加载 | 详情面板显示大图 |
| `title` | 第16行: `<h4>{{ result.title|striptags }}</h4>` | 始终渲染 | 详情标题 |
| `content` | 第17行: `{%- if result.content %}...{% else %}&nbsp;{% endif -%}` | 非空显示，否则占位 | 详情描述 |
| `author` | 第19行: `{%- if result.author %}<span>Author:</span>{{ result.author }}{% else %}&nbsp;{% endif -%}` | 非空显示，否则占位 | 作者信息 |
| `resolution` | 第20行: `{%- if result.resolution %}<span>Resolution:</span>{{ result.resolution }}{% else %}&nbsp;{% endif -%}` | 非空显示，否则占位 | 分辨率信息 |
| `img_format` | 第21行: `{%- if result.img_format %}<span>Format:</span>{{ result.img_format }}{% else %}&nbsp;{% endif -%}` | 非空显示，否则占位 | 格式信息 |
| `filesize` | 第22行: `{%- if result.filesize %}<span>Filesize:</span>{{ result.filesize}}{% else %}&nbsp;{% endif -%}` | 非空显示，否则占位 | 文件大小信息 |
| `source` | 第23行: `{%- if result.source %}<span>Source:</span>{{ result.source }}{% else %}&nbsp;{% endif -%}` | 非空显示，否则占位 | 来源网站信息 |
| `engine` | 第24行: `<span>Engine:</span>{{ result.engine }}` | 始终渲染 | 来源引擎名称 |
| `url` | 第25行: `<a href="{{ result.url }}">{{ result.url }}</a>` | 始终渲染 | 查看源链接 |

---

### 4.3 视频模板字段消费详情

**文件**: `searx/templates/simple/result_templates/videos.html`

视频模板依赖 `macros.html` 中的宏进行渲染。

#### result_header 宏消费 (`macros.html:21-35`)

| 字段 | 消费位置 | 触发条件 | 渲染效果 |
|------|----------|----------|----------|
| `url` | 第23行: `<a href="{{ result.url }}" class="url_header">` | 始终渲染 | 标题链接和域名显示 |
| `parsed_url` | 第28-30行: `get_pretty_url(result.parsed_url)` | 始终渲染 | 美化后的域名显示 |
| `thumbnail` | 第33行: `{%- if result.thumbnail %}...{%- endif -%}` | **仅当 `result.thumbnail` 非空时渲染** | 左侧显示视频缩略图 |
| `length` | 第33行: `{%- if result.length -%}<span class="thumbnail_length">{{ result.length }}</span>{%- endif -%}` | **仅当同时有 `thumbnail` 和 `length` 时显示** | 缩略图右下角显示时长标签 |
| `title` | 第34行: `<h3>{{ result_link(result.url, result.title|safe) }}</h3>` | 始终渲染 | 结果标题链接 |

#### result_sub_header 宏消费 (`macros.html:38-45`)

| 字段 | 消费位置 | 触发条件 | 渲染效果 |
|------|----------|----------|----------|
| `publishedDate` + `pubdate` | 第39行: `<time class="published_date" datetime="{{ result.pubdate }}" >{{ result.publishedDate }}</time>` | **`publishedDate` 非空时显示** | 发布日期（双格式：`pubdate` 用于 `datetime` 属性，`publishedDate` 用于显示文本） |
| `length` | 第41行: `{% if result.length and not result.thumbnail %}<div class="result_length">Length: {{ result.length }}</div>{% endif %}` | **有 `length` 但无 `thumbnail` 时显示** | （已显示在缩略图上则不重复显示） |
| `views` | 第42行: `{% if result.views %}<div class="result_views">Views: {{ result.views }}</div>{% endif %}` | **非空时显示** | 观看次数 |
| `author` | 第43行: `{% if result.author %}<div class="result_author">Author: {{ result.author }}</div>{% endif %}` | **非空时显示** | 作者/频道 |
| `metadata` | 第44行: `{% if result.metadata %}<div class="highlight">{{ result.metadata }}</div>{% endif %}` | **非空时显示** | 其他元数据 |

#### 视频模板主体消费

| 字段 | 消费位置 | 触发条件 | 渲染效果 |
|------|----------|----------|----------|
| `iframe_src` | 第5-7行: `{% if result.iframe_src -%}<p class="altlink">...{{ _('show video') }}</a></p>{%- endif %}` | **非空时显示** | 显示"显示视频"折叠按钮 |
| `content` | 第8-16行: `{% if result.content %}...{% else %}...{% endif %}` | 非空显示，否则显示默认提示 | 视频描述 |
| `iframe_src` | 第19-23行: `{% if result.iframe_src -%}<div id="result-video-{{ index }}" class="embedded-video invisible">...</div>{%- endif %}` | **非空时渲染（初始隐藏）** | 可折叠的嵌入播放器 |

#### iframe 宏消费 (`macros.html:72-79`)

| 字段 | 消费位置 | 触发条件 | 渲染效果 |
|------|----------|----------|----------|
| `iframe_src` | 第73行: `<iframe data-src="{{iframe_src}}" ...>` | 点击"显示视频"按钮后加载 | 嵌入视频播放器 |
| `parsed_url.hostname` | 第74行: `{% if result.parsed_url.hostname in ("www.youtube.com", ) %}` | YouTube 域名时启用画中画 | 特殊平台优化 |

---

## 5. 完整链路图

```
引擎适配层 (Engine Adapters)
    │
    ▼
图片引擎组:
  [bing_images.py] ──┐
  [google_images.py] ─┤
  [flickr.py]         ├─► 归一化为 dict / LegacyResult
  [pexels.py]         │    ├─ template: "images.html"
  [unsplash.py]       │    ├─ img_src, thumbnail_src
  [wallhaven.py]      │    └─ resolution, img_format, filesize, author, source...
  [wikicommons.py]   ─┘
    │
    ▼
视频引擎组:
  [youtube_noapi.py] ─┐
  [dailymotion.py]    ─┤
  [bilibili.py]       ├─► 归一化为 dict / LegacyResult
  [vimeo.py]          │    ├─ template: "videos.html"
  [odysee.py]         │    ├─ thumbnail, iframe_src
  [piped.py]          │    └─ length, views, author, publishedDate...
  [peertube.py]       │
  [invidious.py]     ─┘
    │
    ▼
结果处理层 (ResultContainer)
    │
    ├─ extend() - 收集结果
    │   ├─ LegacyResult 包装
    │   └─ normalize_result_fields() - 字段标准化
    │       ├─ _normalize_url_fields() - URL 解析与标准化
    │       ├─ _normalize_text_fields() - 文本清理
    │       └─ _normalize_date_fields() - 生成 pubdate 字符串
    │
    ├─ _merge_main_result() - 按哈希去重合并
    │   └─ hash() - 计算结果哈希
    │       ├─ MainResult: template|parsed_url|img_src (通用算法)
    │       └─ LegacyResult:
    │           ├─ 图片: template|url|img_src
    │           └─ 视频/普通: template|parsed_url|img_src
    │
    └─ get_ordered_results() - 排序 + 分组
        ├─ 按 score 降序
        └─ 按 category:template:has_img 分组
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
    │   ├─ 缩略图网格 (消费: img_src, thumbnail_src, resolution, title)
    │   └─ 详情面板 (消费: img_src, title, content, author, resolution, img_format, filesize, source)
    │
    └─ videos.html
        ├─ result_header 宏 (消费: url, thumbnail, length, title)
        ├─ result_sub_header 宏 (消费: publishedDate, pubdate, views, author, metadata)
        ├─ 描述内容 (消费: content)
        └─ 嵌入播放器 (消费: iframe_src)
```

---

## 6. 关键设计要点

### 6.1 字段分层设计

| 层级 | 定义位置 | 特性 |
|------|----------|------|
| **MainResult 结构体字段** | `searx/result_types/_base.py:339` | 强类型、编译时检查、核心功能必需 |
| **LegacyResult 模拟字段** | `searx/result_types/_base.py:470-490` | 动态初始化、提供默认值、向后兼容 |
| **引擎扩展字段** | 各引擎 `response()` 函数 | 动态 dict 特性、模板专用、无需核心层感知 |

### 6.2 URL 统一处理架构

所有 URL 字段经过 `_filter_urls()` 统一处理，支持：
- **图片代理**: `image_proxify()` 函数将 URL 转换为代理 URL
- **URL 过滤**: 插件可注册过滤器修改或删除 URL
- **安全清洗**: 自动补全 scheme、标准化路径

```python
url_fields = ["url", "iframe_src", "audio_src", "img_src", "thumbnail_src", "thumbnail"]
```

### 6.3 模板驱动渲染优势

- **关注点分离**: 引擎只需关注数据获取与归一化，无需关心 UI 实现
- **可扩展性**: 新增结果类型只需添加模板文件，无需修改核心分发逻辑
- **可复用性**: `macros.html` 提供通用组件（`result_header`, `iframe` 等），减少模板重复

### 6.4 渐进式类型迁移路径

当前处于从 `LegacyResult` (dict) 向 `MainResult` (msgspec.Struct) 迁移阶段：

1. **现状**: 绝大多数引擎返回 dict，自动包装为 `LegacyResult`
2. **过渡**: 新引擎可使用 `EngineResults` 包装器，选择 `LegacyResult` 或 `MainResult`
3. **未来**: 引入 `ImageResult` 和 `VideoResult` 子类，将 `resolution`、`iframe_src` 等字段纳入强类型定义

### 6.5 哈希去重设计权衡

| 类型 | 哈希算法 | 适用场景 | 优缺点 |
|------|----------|----------|--------|
| **MainResult** | `template + parsed_url + img_src` | 类型安全的新引擎 | 强一致要求，不允许 `parsed_url` 为空 |
| **LegacyResult** (图片) | `template + url + img_src` | 图片引擎 | 兼容 `parsed_url` 可能为空的情况 |
| **LegacyResult** (视频) | `template + parsed_url + img_src` | 视频/普通引擎 | 与 MainResult 算法一致 |

> **迁移注意**: 当图片引擎迁移到 `MainResult` 时，需要确保 `parsed_url` 字段被正确填充，否则哈希计算会抛出异常。

---

## 7. 引擎归一化映射对照矩阵

### 7.1 图片引擎映射矩阵

| 引擎 | 模板 | URL | 原图 | 缩略图 | 标题 | 描述 | 作者 | 来源 | 分辨率 | 格式 | 大小 | 日期 |
|------|------|-----|------|--------|------|------|------|------|--------|------|------|------|
| **Bing Images** | ✅ | `purl` | `murl` | `turl` | ✅ xpath | `desc` | - | ✅ xpath | ✅ | ✅ | - | - |
| **Google Images** | ✅ | `referrer_url` | `original_image.url` | `thumbnail.url` | `page_title` | `snippet` | ✅ | `site_title` | ✅ | - | - | ✅ |
| **Flickr** | ✅ | 构造 URL | `url_o`/`url_z` | `url_n`/`url_z` | ✅ | `description._content` | `ownername` | - | - | - | - | - |
| **Pexels** | ✅ | 构造 URL | `image.download_link` | `image.small` | ✅ | ✅ | `user.username` | - | ✅ | - | - | - |
| **Unsplash** | ✅ | `links.html` | `urls.regular` | `urls.thumb` | `alt_description` | `description` | - | - | - | - | - | - |
| **Wallhaven** | ✅ | ✅ | `path` | `thumbs.small` | "" | `category/purity` | - | - | ✅ | ✅ | ✅ | ✅ |
| **Wikicommons** | ✅ | `descriptionurl` | ✅ | `thumburl` | ✅ | ✅ | - | - | ✅ | ✅ | ✅ | - |
| **Brave** | ✅ | ✅ | `properties.url` | `thumbnail.src` | ✅ | - | - | ✅ | - | - | - | - |

### 7.2 视频引擎映射矩阵

| 引擎 | 模板 | URL | 缩略图 | 嵌入帧 | 标题 | 描述 | 作者 | 时长 | 观看 | 日期 |
|------|------|-----|--------|--------|------|------|------|------|------|------|
| **YouTube** | ✅ | 构造 URL | ✅ | 构造 URL | ✅ | ✅ | ✅ | ✅ | - | - |
| **Dailymotion** | ✅ | ✅ | ✅ | 构造 URL | ✅ | ✅ | - | ✅ | - | ✅ |
| **Bilibili** | ✅ | `arcurl` | `pic` | 构造 URL | ✅ | ✅ | ✅ | ✅ | - | ✅ |
| **Vimeo** | ✅ | 构造 URL | ✅ | 构造 URL | ✅ | - | - | - | - | ✅ |
| **Odysee** | ✅ | 构造 URL | CDN URL | 构造 URL | ✅ | ✅ | ✅ | ✅ | - | ✅ |
| **Piped** | ✅ | 构造 URL | ✅ | 构造 URL | ✅ | ✅ | - | ✅ | ✅ | ✅ |
| **Peertube** | ✅ | ✅ | ✅ | `embedUrl` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Invidious** | ✅ | 构造 URL | ✅ | 构造 URL | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Wikicommons** | ✅ | `descriptionurl` | - | ✅ | ✅ | ✅ | - | ✅ | - | - |

---

## 8. 代码引用索引

| 模块 | 关键文件 | 行号 | 说明 |
|------|----------|------|------|
| 数据模型 | `searx/result_types/_base.py` | 228 | `Result` 基类定义 |
| 数据模型 | `searx/result_types/_base.py` | 339 | `MainResult` 主结果类定义 |
| 数据模型 | `searx/result_types/_base.py` | 428 | `LegacyResult` 兼容类定义 |
| 日期处理 | `searx/result_types/_base.py` | 219 | `_normalize_date_fields` 日期标准化 |
| URL 处理 | `searx/result_types/_base.py` | 111 | `_filter_urls` URL 统一过滤 |
| MainResult 哈希 | `searx/result_types/_base.py` | 405 | `MainResult.__hash__` 通用哈希算法 |
| LegacyResult 哈希 | `searx/result_types/_base.py` | 521 | `LegacyResult.__hash__` 含图片专用分支 |
| 结果容器 | `searx/results.py` | 53 | `ResultContainer` 结果收集合并 |
| 结果排序 | `searx/results.py` | 197 | `get_ordered_results` 排序分组 |
| 模板分发 | `searx/webapp.py` | 247 | `get_result_template` 模板选择 |
| 图片模板 | `searx/templates/simple/result_templates/images.html` | 1 | 图片结果渲染 |
| 视频模板 | `searx/templates/simple/result_templates/videos.html` | 1 | 视频结果渲染 |
| 模板宏 | `searx/templates/simple/macros.html` | 21 | `result_header` 等宏定义 |
| 模板宏 | `searx/templates/simple/macros.html` | 39 | `result_sub_header` 发布日期双格式消费 |
