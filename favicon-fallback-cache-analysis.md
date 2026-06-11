# SearXNG 站点图标缓存机制完整分析报告

---

## 一、整体链路总览

SearXNG 的 favicon 系统分为 **三个阶段** 逐层处理：

```
阶段一：模板渲染
    ↓ favicon_url(netloc) 被 jinja 调用
阶段二：URL 生成（三级分流）
    ├─ 命中失败缓存 → 直接输出占位图 Data URL
    ├─ 命中成功缓存 → 直接输出真实图 Base64 Data URL
    └─ 未命中缓存   → 输出 /favicon_proxy URL（交给浏览器异步请求）
    ↓ 若走 /favicon_proxy
阶段三：代理端点处理（search_favicon）
    ├─ 先再查一次缓存
    ├─ 未命中则调用 resolver 外部抓取
    ├─ 结果写入缓存（无论成功失败）
    └─ 失败则兜底返回 empty_favicon.svg 静态文件
```

---

## 二、阶段一：模板渲染入口

### 2.1 模板中的调用点

搜索结果页渲染时，[macros.html:L21-L26](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/templates/simple/macros.html#L21-L26) 的 `result_header` 宏中：

```jinja
{%- if favicon_resolver != "" %}
<div class="favicon"><img loading="lazy" src="{{ favicon_url(result.parsed_url.netloc) }}"></div>
{%- endif -%}
```

- `favicon_resolver` 来自模板上下文，若为空字符串则**完全不渲染 favicon 区域**
- `result.parsed_url.netloc` 是域名（含端口），作为 authority 传入
- `favicon_url` 函数由 [webapp.py:L437](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/webapp.py#L437) 注入模板

### 2.2 前端 CSS 视觉兜底

即使 `<img>` 的 `src` 完全无效（例如返回空字符串或加载失败），CSS 仍提供视觉占位。在 [search.less:L375-L382](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/client/simple/src/less/search.less#L375-L382)：

```less
.favicon img {
  height: 1.5rem;
  width: 1.5rem;
  border-radius: 10%;
  background-color: var(--color-favicon-background-color);  /* #ddd 灰底 */
  border: 1px solid var(--color-favicon-border-color);       /* #ccc 边框 */
  display: flex;
}
```

显示效果：始终有一个 24×24 的灰色圆角方块，里面才是 `<img>` 内容。

---

## 三、阶段二：URL 生成（三级分流）

### 3.1 favicon_url 函数的完整决策树

核心逻辑位于 [proxy.py:L195-L237](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/proxy.py#L195-L237)。以下是逐行拆解：

```python
def favicon_url(authority: str) -> str:
    # Step 1: 获取当前用户选的 resolver
    resolver = sxng_request.preferences.get_value('favicon_resolver')
    if not resolver or resolver not in CFG.resolver_map.keys():
        return ""  # resolver 无效 → 返回空字符串（前端仍显示 CSS 灰方块）

    # Step 2: 查一次缓存
    data_mime = cache.CACHE(resolver, authority)
    #   data_mime 可能有三种值：
    #   - (None, None)   → 曾抓过，但失败了（FALLBACK_ICON 标记）
    #   - (data, mime)   → 曾抓过，成功了（真实 BLOB）
    #   - None           → 从未抓过，或者记录已过期被 maintenance 删除

    # Step 3-A: 命中失败缓存 → 直接返回占位图 Data URL
    if data_mime == (None, None):
        theme = sxng_request.preferences.get_value("theme")
        return CFG.favicon_data_url(theme=theme)   # ← 注意：这里不调用 set()！

    # Step 3-B: 命中成功缓存 → 返回 Base64 Data URL
    if data_mime is not None:
        data, mime = data_mime
        return f"data:{mime};base64,{str(base64.b64encode(data), 'utf-8')}"  # ← 也不调用 set()！

    # Step 3-C: 完全未命中 → 返回 /favicon_proxy URL，交给浏览器异步请求
    h = new_hmac(CFG.secret_key, authority.encode())
    proxy_url = flask.url_for('favicon_proxy')
    query = urllib.parse.urlencode({"authority": authority, "h": h})
    return f"{proxy_url}?{query}"   # ← 也不调用 set()！
```

### 3.2 关键修正：favicon_url 中**没有任何 set() 调用**

**之前的错误**：认为"每次访问命中失败缓存都会续期 `m_time`"。

**正确结论**：`favicon_url()` 只是**只读**地查缓存，三种分支都不会调用 `cache.set()`。因此：

- 命中失败缓存 → 直接返回占位图 Data URL，**不刷新 `m_time`，不续期**
- 命中成功缓存 → 直接返回 Base64 Data URL，**不刷新 `m_time`，不续期**
- 未命中缓存 → 返回代理 URL，**不刷新 `m_time`，不续期**

### 3.3 失败缓存命中时如何"直接跳转到占位图"

当 `cache.CACHE()` 返回 `(None, None)` 时（[proxy.py:L225-L228](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/proxy.py#L225-L228)）：

```python
if data_mime == (None, None):
    theme = sxng_request.preferences.get_value("theme")
    return CFG.favicon_data_url(theme=theme)
```

`favicon_data_url` 在 [proxy.py:L93-L109](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/proxy.py#L93-L109) 中实现：

```python
def favicon_data_url(self, **replacements):
    cache_key = ", ".join(f"{x}:{replacements[x]}" for x in sorted(list(replacements.keys()), key=str))
    data_url = DEFAULT_FAVICON_URL.get(cache_key)
    if data_url is not None:
        return data_url  # 进程内有缓存就直接返回

    fav, mimetype = CFG.favicon(**replacements)
    # 读文件：themes/simple/img/empty_favicon.svg
    with fav.open("r", encoding="utf-8") as f:
        data_url = f.read()
    data_url = urllib.parse.quote(data_url)
    data_url = f"data:{mimetype};utf8,{data_url}"
    DEFAULT_FAVICON_URL[cache_key] = data_url
    return data_url
```

最终返回给模板的是：`data:image/svg+xml;utf8,%3Csvg%20xmlns...`（即 [empty_favicon.svg](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/client/simple/src/svg/empty_favicon.svg) 的 URL 编码版本）。

**这条链路的特点**：
- 不需要浏览器发起额外 HTTP 请求（Data URL 内联在 HTML 中）
- 不需要访问 `/favicon_proxy` 端点
- **不触发外部 resolver 抓取**
- **不触发 `cache.set()`，所以不会续期 `m_time`**

---

## 四、阶段三：/favicon_proxy 代理端点处理

当缓存完全未命中时（返回 `/favicon_proxy?...` URL），浏览器会异步发起 GET 请求。路由注册在 [webapp.py:L999](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/webapp.py#L999)。

### 4.1 favicon_proxy 端点处理流程

代码位于 [proxy.py:L112-L156](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/proxy.py#L112-L156)：

```python
def favicon_proxy():
    authority = sxng_request.args.get('authority')
    if not authority or "/" in authority:
        return '', 400   # 非法请求

    # HMAC 校验：防止任意域名探测
    if not is_hmac_of(CFG.secret_key, authority.encode(), sxng_request.args.get('h', '')):
        return '', 400

    resolver = sxng_request.preferences.get_value('favicon_resolver')
    if not resolver or resolver not in CFG.resolver_map.keys():
        return "", 400

    # 核心：调用 search_favicon 查缓存+抓取
    data, mime = search_favicon(resolver, authority)

    if data is not None and mime is not None:
        # 成功：返回图片二进制 + Cache-Control
        resp = flask.Response(data, mimetype=mime)
        resp.headers['Cache-Control'] = f"max-age={CFG.max_age}"  # 默认 7 天
        return resp

    # 失败兜底：从静态目录发送 empty_favicon.svg
    theme = sxng_request.preferences.get_value("theme")
    fav, mimetype = CFG.favicon(theme=theme)
    return flask.send_from_directory(fav.parent, fav.name, mimetype=mimetype)
```

### 4.2 search_favicon 的缓存 + 抓取逻辑

代码位于 [proxy.py:L159-L192](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/proxy.py#L159-L192)：

```python
def search_favicon(resolver: str, authority: str) -> tuple[None | bytes, None | str]:
    data, mime = (None, None)

    func = CFG.get_resolver(resolver)
    if func is None:
        return data, mime

    # 再查一次缓存（因为从 favicon_url 到浏览器请求 proxy 之间可能有其他进程写入了）
    data_mime = cache.CACHE(resolver, authority)
    if data_mime is not None:          # ← 包括 (None, None) 失败标记
        return data_mime                # ← 直接返回，不调用 set()！

    # 只有缓存真正未命中，才会走到以下逻辑
    try:
        data, mime = func(authority, timeout=CFG.resolver_timeout)
        if data is None or mime is None:
            data, mime = (None, None)
    except (HTTPError, SearxEngineResponseException):
        pass

    # 写缓存：无论成功还是失败都写入
    cache.CACHE.set(resolver, authority, mime, data)   # ← 唯一可能触发 set() 的地方
    return data, mime
```

### 4.3 关键结论：唯一会写入缓存的时机

**只有**当缓存真正未命中（`cache.CACHE()` 返回 `None`）并走完 resolver 抓取流程后，才会调用 `cache.set()`。

这意味着：
- 如果已经有失败缓存 `(None, None)` → search_favicon 直接返回，**不会续期**
- 如果已经有成功缓存 → search_favicon 直接返回，**不会续期**
- 只有缓存中完全没有记录（首次访问，或 maintenance 已删除过期记录）时，才调用 `set()` 并刷新 `m_time`

---

## 五、缓存键粒度

### 5.1 blob_map 表的主键结构

在 [cache.py:L270-L275](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L270-L275) 中：

```sql
CREATE TABLE IF NOT EXISTS blob_map (
    m_time     INTEGER DEFAULT (strftime('%s', 'now')),
    sha256     TEXT,
    resolver   TEXT,
    authority  TEXT,
    PRIMARY KEY (resolver, authority))
```

**缓存键 = `(resolver, authority)` 二元组**：

| 维度 | 说明 |
|------|------|
| `resolver` | 用户选定的解析器名：`duckduckgo` / `google` / `allesedv` / `yandex`。不同 resolver 之间缓存完全隔离。 |
| `authority` | 域名（含子域名、含端口），即 URL 的 netloc。`www.github.com` 和 `github.com` 和 `gist.github.com` 是三条独立记录。 |

**缓存共享矩阵**：

| 场景 | 是否共享同一条缓存 |
|------|------------------|
| 同一 resolver + 完全相同域名 | ✅ 共享 |
| 同一 resolver + 相同域名不同路径 | ✅ 共享（authority 不包含 path） |
| 同一 resolver + 不同子域名 | ❌ 独立（`a.com` vs `b.a.com`） |
| 同一域名 + 不同 resolver | ❌ 独立（`duckduckgo:a.com` vs `google:a.com`） |
| 同一域名 + HTTP 与 HTTPS | ✅ 共享（authority 不包含 scheme） |

### 5.2 失败标记的存储方式

当抓取失败时，[cache.py:L359-L360](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L359-L360)：

```python
if data is None:
    sha256 = FALLBACK_ICON  # 常量值 = b"FALLBACK_ICON"
```

- `blob_map` 表中：`sha256` 字段写入字符串 `"FALLBACK_ICON"`
- `blobs` 表中：**不插入任何记录**（[cache.py:L365-L366](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L365-L366) 判断后跳过）

读取时的失败判定在 [cache.py:L320-L336](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L320-L336)：

```python
def __call__(self, resolver: str, authority: str):
    sql = "SELECT sha256 FROM blob_map WHERE resolver = ? AND authority = ?"
    res = self.DB.execute(sql, (resolver, authority)).fetchone()
    if res is None:
        return None                     # ← 未命中（完全无记录）
    sha256 = res[0]
    if sha256 == FALLBACK_ICON:
        return (None, None)             # ← 命中失败缓存
    # 否则从 blobs 表查真实数据
    sql = "SELECT data, mime FROM blobs WHERE sha256 = ?"
    ...
```

---

## 六、HOLD_TIME 超时与缓存真正过期的完整链条

### 6.1 读取路径不做超时检查

`cache.CACHE()`（`__call__` 方法）的查询 SQL **完全没有 `m_time` 条件**：

```sql
SELECT sha256 FROM blob_map WHERE resolver = ? AND authority = ?
```

即使 `m_time` 已经是十年前的，只要记录还在表中，就会被当作有效缓存返回——包括失败标记。

### 6.2 HOLD_TIME 只在 maintenance 中生效

`HOLD_TIME`（默认 30 天）只出现在 [cache.py:L396-L400](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L396-L400) 的 maintenance 函数中：

```sql
DELETE FROM blob_map
WHERE cast(m_time as integer) < cast(strftime('%s', 'now') as integer) - {self.cfg.HOLD_TIME}
```

### 6.3 maintenance 的触发条件

`maintenance()` 被调用的唯一自动触发点在 `cache.set()` 入口处，[cache.py:L340-L342](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L340-L342)：

```python
if self.cfg.MAINTENANCE_MODE == "auto" and int(time.time()) > self.next_maintenance_time:
    self.maintenance()
```

即必须**同时满足**：
1. 有一次 `set()` 被调用（即有某个域名缓存未命中，触发了外部抓取）
2. `MAINTENANCE_MODE` 是 `"auto"`
3. 当前时间已经超过 `LAST_MAINTENANCE.m_time + MAINTENANCE_PERIOD`（默认 1 小时）

如果服务器长时间闲置、没有新域名被抓取（即没有 `set()` 调用），即使所有缓存都超过 HOLD_TIME 一万年，也**不会触发 maintenance，不会删除任何记录**。

### 6.4 缓存从"写入"到"真正过期并触发重抓"的完整时间线

以下为默认配置（HOLD_TIME=30天，MAINTENANCE_PERIOD=1小时）的典型场景：

```
Day 0, 09:00
  事件：用户第一次搜索包含 example.com 的结果
  流程：
    - favicon_url() 查缓存 → cache.CACHE() 返回 None（无记录）
    - 返回 /favicon_proxy URL，浏览器异步请求
    - search_favicon() 查缓存 → 还是 None
    - 调用 duckduckgo resolver → 返回 404 → data=None
    - cache.set(duckduckgo, example.com, None, None)
      → blob_map 写入：m_time=Day0 09:00, sha256="FALLBACK_ICON"
      → 同时：检查 maintenance 条件 → 是首次，触发 maintenance
      → maintenance：无过期记录可删，记录 LAST_MAINTENANCE=Day0 09:00

Day 10
  事件：用户再次搜索 example.com
  流程：
    - favicon_url() 查缓存 → 返回 (None, None) 失败标记
    - 直接返回 empty_favicon.svg 的 Data URL
    - 不调用 /favicon_proxy，不调用 set()
    - m_time 仍为 Day0 09:00，未续期

Day 29
  事件：用户再次搜索 example.com
  流程：同上，仍命中失败缓存，m_time 不变

Day 31, 08:00
  事件：用户再次搜索 example.com
  注意：m_time(Day0) 距今已 31 天 > HOLD_TIME(30天)
  但：
    - cache.CACHE() 不检查时间
    - 记录仍在 blob_map 表中
    - 所以仍命中失败缓存，返回占位图 Data URL
    - **不会重新抓取，也不会续期**

Day 31, 10:00
  事件：用户搜索一个全新域名 new-domain-xyz.com（从未抓过）
  流程：
    - favicon_url() → None → /favicon_proxy
    - search_favicon() → None → resolver 抓取
    - cache.set(duckduckgo, "new-domain-xyz.com", mime, data)
      → 进入 set() 函数
      → 检查 maintenance：now(Day31 10:00) > LAST_MAINTENANCE(Day0 09:00) + 1h ? 是
      → 触发 maintenance()
         → 执行 DELETE：WHERE m_time < Day31 10:00 - 30天 = Day1 10:00
         → example.com 的记录 m_time=Day0 09:00 < Day1 10:00 → 被删除！
         → new-domain-xyz.com 的记录 m_time=Day31 10:00 → 保留
      → LAST_MAINTENANCE 更新为 Day31 10:00

Day 31, 11:00
  事件：用户再次搜索 example.com
  流程：
    - favicon_url() 查缓存
    - cache.CACHE()：SELECT 无记录 → 返回 None（因为 Day31 10:00 被 maintenance 删了）
    - 返回 /favicon_proxy URL
    - 浏览器请求 /favicon_proxy
    - search_favicon() 查缓存 → None
    - 调用 duckduckgo resolver → 重新抓取 → 仍失败
    - cache.set(duckduckgo, example.com, None, None)
      → 新写入记录，m_time=Day31 11:00
      → （这次 set() 检查 maintenance：距上次仅 1h，不触发）

  结果：example.com 的失败缓存终于被"刷新"了，m_time 续期为 Day31 11:00
  前提：恰好有另一个新域名触发了 maintenance 先删除了旧记录
```

### 6.5 续期的真实含义（修正之前的错误）

**之前的错误表述**："一个持续失败的域名，只要每隔 30 天内至少有一次访问触发重新抓取并再次失败，失败缓存就会被永久保留。"

**正确表述**：

1. **普通访问不会续期**：命中失败缓存时，`favicon_url()` 和 `search_favicon()` 都只读不写，`m_time` 不会被刷新。

2. **续期只能发生在以下链条之后**：
   - maintenance 删除了过期的失败记录（因为某**其他域名**的 `set()` 触发了 maintenance）
   - 下一次访问缓存未命中（返回 None）
   - 触发 `/favicon_proxy` → `search_favicon` → resolver 重新抓取
   - 再次失败 → `set()` 写入新的 FALLBACK_ICON → `m_time` 被刷新为当前时间

3. **如果长时间没有新域名触发 maintenance**：所有记录（包括失败的）都会长期存在，超过 HOLD_TIME 也不会被删除，读取时仍当作有效缓存。

4. **最极端的不续期场景**：只有一个固定域名反复访问，永远不访问其他新域名。每次访问都命中失败缓存，不触发 `set()` → 永远不会触发 maintenance（因为没有新 `set()`）→ 记录永远不会被删 → 永远不会重新抓取 → `m_time` 永远保持 Day 0 的值。

---

## 七、四个时间参数的完整对照

| 参数 | 默认值 | 位置 | 作用与触发时机 |
|------|--------|------|--------------|
| `HOLD_TIME` | 30 天 | [cache.py:L109](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L109) | maintenance 运行时，删除 `m_time < now - HOLD_TIME` 的 blob_map 记录。**读取路径不检查此值**。 |
| `MAINTENANCE_PERIOD` | 1 小时 | [cache.py:L124](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L124) | 两次自动 maintenance 之间的最小间隔。只有 `set()` 被调用时才会检查是否已超过该间隔。 |
| `max_age` | 7 天 | [proxy.py:L47](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/proxy.py#L47) | 浏览器端 HTTP `Cache-Control: max-age`，仅在 `/favicon_proxy` 成功返回真实图时设置。Data URL 方式不受影响（每次渲染都重新输出）。 |
| `resolver_timeout` | 来自 `outgoing.request_timeout` | [proxy.py:L57](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/proxy.py#L57) | 单次外部 resolver 请求的超时时间，被 `HTTPError` 捕获后视为失败。 |

---

## 八、边界情况汇总

| 场景 | 行为 |
|------|------|
| `MAINTENANCE_MODE = "off"` | 永远不自动 maintenance。所有记录永久存在；失败记录永远不会被清；除非手动运行 CLI `cache maintenance`。 |
| SQLite < 3.35 | 降级为 `FaviconCacheNull`：完全不缓存，每次都走 `/favicon_proxy` → 每次都重新抓取。 |
| BLOB > `BLOB_MAX_BYTES`（20KB） | `set()` 直接返回 False，不写入缓存。下次访问仍未命中 → 每次都重新抓取。 |
| 用户切换 resolver | 缓存键包含 resolver，所以新 resolver 下是全新记录 → 立即触发重新抓取，不受旧 resolver 下失败缓存影响。 |
| 多进程环境 | maintenance 通过 SQLite 行锁 + `LAST_MAINTENANCE` 属性时间判断避免重复运行，但并发时可能有轻微竞争。 |

---

## 九、关键源码索引

| 功能 | 位置 |
|------|------|
| 模板渲染入口：`<img src="{{ favicon_url(...) }}">` | [macros.html:L24-L26](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/templates/simple/macros.html#L24-L26) |
| favicon_url：三级分流（失败/成功/未命中），**全程不调用 set()** | [proxy.py:L195-L237](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/proxy.py#L195-L237) |
| 失败缓存命中 → 返回占位图 Data URL | [proxy.py:L225-L228](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/proxy.py#L225-L228) |
| favicon_data_url：Data URL 生成 + 进程内缓存 | [proxy.py:L93-L109](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/proxy.py#L93-L109) |
| /favicon_proxy 端点 | [proxy.py:L112-L156](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/proxy.py#L112-L156) |
| search_favicon：**唯一可能触发 set() 的地方** | [proxy.py:L159-L192](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/proxy.py#L159-L192) |
| cache 读取：不检查 m_time，三种返回值 | [cache.py:L320-L336](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L320-L336) |
| cache 写入：唯一会触发 maintenance 的入口 | [cache.py:L338-L373](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L338-L373) |
| 写入时 UPSERT 刷新 m_time | [cache.py:L306-L310](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L306-L310) |
| FALLBACK_ICON 标记写入 | [cache.py:L359-L360](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L359-L360) |
| blob_map 主键 = (resolver, authority) | [cache.py:L270-L275](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L270-L275) |
| maintenance 触发检查 | [cache.py:L340-L342](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L340-L342) |
| HOLD_TIME 清理 SQL | [cache.py:L396-L400](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L396-L400) |
| CSS 灰方块兜底样式 | [search.less:L375-L382](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/client/simple/src/less/search.less#L375-L382) |
| empty_favicon.svg 占位图文件 | [empty_favicon.svg](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/client/simple/src/svg/empty_favicon.svg) |
