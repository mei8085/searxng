# SearXNG 搜索结果站点图标回退与刷新链路

## 结论先看

搜索结果里的站点图标不是简单地“抓不到就换默认图”。实际链路分成三段：

1. 模板阶段先看服务端缓存。
2. 缓存没命中时再走 `/favicon_proxy` 向外部 resolver 抓图。
3. 一旦确认该域名没有可用图标，就把“失败结果”也写进缓存，后续直接回退到默认占位图，不再反复访问外部服务。

命中缓存后的刷新频率也分两层控制：

- 服务端 SQLite 缓存控制“多久重新向外部 resolver 询问一次”。
- 浏览器 HTTP 缓存只控制通过 `/favicon_proxy` 成功返回的真实图标多久不用再请求服务端。

## 参与模块

- `searx/favicons/__init__.py`：加载配置，初始化缓存和代理。
- `searx/favicons/proxy.py`：模板用的 `favicon_url()`、运行时的 `/favicon_proxy` 和 `search_favicon()`。
- `searx/favicons/cache.py`：SQLite 缓存、失败哨兵值、定期清理。
- `searx/favicons/resolvers.py`：向外部 favicon 服务抓图。
- `searx/templates/simple/macros.html`：结果列表里 `<img>` 的渲染入口。
- `client/simple/src/less/search.less`：图标区域的视觉兜底样式。

## 一、结果页上图标是怎么决定的

结果模板会调用 `favicon_url(result.parsed_url.netloc)`。这个函数不会无条件返回代理地址，而是先查缓存，再按缓存状态分三路返回：

### 1. 已知失败：直接返回默认占位图的 Data URL

当缓存里已经记着这个域名“没有可用 favicon”时，`favicon_url()` 直接返回主题里的 `empty_favicon.svg`，而且是内联的 Data URL。

这意味着：

- 结果页渲染时不再请求 `/favicon_proxy`
- 也不会再次访问外部 resolver
- 页面总能拿到一个合法图片地址

### 2. 已知成功：直接返回真实图标的 Data URL

如果缓存里已经有真实 favicon 的二进制内容，`favicon_url()` 会把它编码成 `data:<mime>;base64,...` 后直接塞进页面。

这条路径也不会再访问 `/favicon_proxy`。

### 3. 完全未命中：返回 `/favicon_proxy?...` 地址

只有缓存里既没有成功图，也没有失败哨兵时，模板才会生成带 HMAC 的 `/favicon_proxy?authority=...&h=...` 地址，让浏览器后续发起一次真正的 HTTP 请求。

## 二、抓取失败时按什么顺序回退

### 第 1 层：resolver 自己先判定“这次抓取算不算成功”

默认有 4 个 resolver：

- `allesedv`：即使 HTTP 200，也会把 `image/gif` 当成占位结果，不算成功。
- `duckduckgo`：只有 HTTP 200 才算成功。
- `google`：只有 HTTP 200 才算成功。
- `yandex`：除了 HTTP 200，还要求响应体大于 70 字节，用来排除 1x1 占位图。

也就是说，是否“抓到了真实 favicon”，第一关不是统一规则，而是各 resolver 各自判断。

### 第 2 层：`search_favicon()` 吞掉抓取异常，并把失败写进缓存

`search_favicon()` 会先查缓存；未命中时才真正调用 resolver。

这里有两个关键点：

- resolver 抛出网络或响应异常时，函数不会把异常继续抛给上层，而是保留 `(None, None)`。
- 无论是“明确没图”还是“请求异常后拿不到图”，最后都会调用 `cache.CACHE.set(...)`。

因此，“失败”不是一次性的瞬时状态，而会被正式记入缓存。

### 第 3 层：缓存把失败记成哨兵值

在 SQLite 缓存实现里，`data is None` 时不会写真实 BLOB，而是把 `sha256` 记成 `FALLBACK_ICON`。

后面再次查询这个 `(resolver, authority)`：

- 如果根本查不到记录，表示“从没试过”
- 如果查到的是 `FALLBACK_ICON`，表示“试过了，但没有可用 favicon”
- 如果查到真实 BLOB，表示“之前抓到过”

这个区分是整个回退链路的核心，因为它避免了对失败域名重复请求外部服务。

### 第 4 层：模板阶段先吃掉“失败缓存”

`favicon_url()` 看到缓存命中 `(None, None)` 时，不会再走代理，而是直接返回默认占位图的 Data URL。

所以在“失败结果已经缓存”的常见场景下，回退顺序其实是：

1. 模板查缓存
2. 发现是失败哨兵
3. 直接给默认占位图
4. 页面渲染结束

连 `/favicon_proxy` 都不会再进。

### 第 5 层：首次抓取失败时，`/favicon_proxy` 再兜一次默认图

只有首次未命中缓存时，浏览器才会请求 `/favicon_proxy`。

如果这次抓图最终仍得到 `(None, None)`，`favicon_proxy()` 不会返回 404，而是从主题静态目录发送 `empty_favicon.svg`。

因此首次失败时的回退顺序是：

1. 模板未命中缓存，输出 `/favicon_proxy?...`
2. 浏览器请求代理端点
3. 代理触发 `search_favicon()`
4. resolver 失败或异常
5. 失败结果写入缓存
6. 代理返回默认 `empty_favicon.svg`

### 第 6 层：CSS 再给一层视觉兜底

结果列表里的 favicon 区域本身带固定尺寸、背景色和边框。即使图片内容不可见，外层仍会保留一个灰底小方块，不至于让布局塌掉。

这层不是业务回退，只是视觉兜底。

## 三、命中缓存时刷新频率怎么控制

### 1. 服务端缓存：控制多久重新向外部 resolver 询问

SQLite 缓存默认参数：

- `HOLD_TIME = 30 天`
- `MAINTENANCE_PERIOD = 1 小时`
- `MAINTENANCE_MODE = auto`

`cache.CACHE.set()` 每次写缓存前都会检查是否到达下一次维护时间；到了就触发 `maintenance()`。

维护会做两件和刷新相关的事：

- 删除 `m_time` 早于当前时间减 `HOLD_TIME` 的映射
- 清掉这些映射失效后残留的孤儿 BLOB

这直接决定了“多久允许重新抓一次”：

- 成功缓存：30 天内可直接复用
- 失败缓存：30 天内也直接复用默认图，不再重复抓取
- 30 天过后：维护把记录删掉，下一次访问才重新向 resolver 询问

所以对失败域名来说，默认刷新频率不是分钟级，也不是按页面访问次数，而是“最长缓存 30 天，到期后下一次请求再试”。

### 2. 浏览器缓存：只作用于通过代理成功返回的真实图标

`/favicon_proxy` 在成功返回真实 favicon 时，会显式加：

`Cache-Control: max-age=604800`

也就是默认 7 天。

这层只影响“浏览器要不要再次向 SearXNG 服务端请求同一个代理 URL”，不影响服务端 SQLite 里那份 30 天缓存。

实际效果是：

- 浏览器 7 天内可直接复用代理返回的图标响应
- 7 天后浏览器可能重新请求服务端
- 但服务端大概率仍能从自己的 SQLite 缓存直接取出图标，不需要再访问外部 resolver

### 3. Data URL 命中路径没有单独的 HTTP 缓存往返

当模板阶段已经命中缓存时，无论命中的是：

- 真实 favicon 的 base64 Data URL
- 默认占位图的 SVG Data URL

浏览器拿到的都是 HTML 内联内容，不会再对 `/favicon_proxy` 发请求。

这条路径下谈“刷新频率”时，真正起作用的是服务端那份缓存记录什么时候过期，而不是额外的 HTTP 缓存头。

### 4. 一个容易漏掉的边界：大于 20 KB 的图不会写入服务端缓存

`BLOB_MAX_BYTES` 默认是 20 KB。若 resolver 抓到的图标超过这个上限，`cache.CACHE.set()` 会直接拒绝写缓存。

后果是：

- 这次请求仍然可以把真实图标返回给浏览器
- 但服务端不会记住它
- 浏览器的 7 天 HTTP 缓存过期后，再访问时又会重新走代理和外部抓取

所以“命中缓存后的刷新频率”只适用于能成功进入服务端缓存的图标；过大的图标只享受浏览器侧缓存。

## 四、完整链路压缩成一句话

SearXNG 会先用模板阶段的缓存判断把 favicon 分成“已有成功图”“已知失败”“完全未见过”三类：前两类直接内联返回，只有第三类才请求代理；首次抓取失败后会把失败结果写成缓存哨兵，后续 30 天都直接回退到默认占位图，不再访问外部 resolver；若首次抓取成功且图标可缓存，则浏览器侧默认 7 天不必再向服务端请求，服务端侧默认 30 天不必再向外部服务重新抓取。

## 五、关键文件索引

- `searx/favicons/proxy.py`
- `searx/favicons/cache.py`
- `searx/favicons/resolvers.py`
- `searx/favicons/__init__.py`
- `searx/templates/simple/macros.html`
- `client/simple/src/less/search.less`
