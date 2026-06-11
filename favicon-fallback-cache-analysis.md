# SearXNG 站点图标缓存刷新机制澄清报告

## 一、失败结果的缓存键粒度

### 1.1 主键结构

失败标记 `FALLBACK_ICON` 的缓存键粒度由 `blob_map` 表的主键决定。在 [cache.py:L270-L275](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L270-L275) 中：

```sql
CREATE TABLE IF NOT EXISTS blob_map (
    m_time     INTEGER DEFAULT (strftime('%s', 'now')),
    sha256     TEXT,
    resolver   TEXT,
    authority  TEXT,
    PRIMARY KEY (resolver, authority))
```

**缓存键是 `(resolver, authority)` 二元组**，即：
- `resolver`：用户选择的 favicon 解析器名称（如 `duckduckgo`、`google`、`allesedv`、`yandex`）
- `authority`：域名（如 `github.com`、`www.baidu.com`）

### 1.2 粒度含义

| 场景 | 是否共享缓存 |
|------|-------------|
| 同一域名 + 不同 resolver | ❌ 各自独立缓存 |
| 同一 resolver + 不同子域名 | ❌ 各自独立缓存（`github.com` vs `gist.github.com`） |
| 同一 resolver + 同一域名的不同 URL 路径 | ✅ 共享缓存（只看 authority，不看 path） |

### 1.3 失败标记的存储

当抓取失败（`data=None`）时，在 [cache.py:L359-L360](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L359-L360) 写入：

```python
if data is None:
    sha256 = FALLBACK_ICON  # 特殊标记 b"FALLBACK_ICON"
```

此时 `blob_map` 表中该 `(resolver, authority)` 对应的 `sha256` 字段存储的是字符串 `"FALLBACK_ICON"`，而非真实文件的哈希值。`blobs` 表中不会有对应记录。

### 1.4 读取时的失败判定

在 [cache.py:L320-L336](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L320-L336) 的 `__call__` 方法中：

```python
sql = "SELECT sha256 FROM blob_map WHERE resolver = ? AND authority = ?"
res = self.DB.execute(sql, (resolver, authority)).fetchone()
if res is None:
    return None  # 未命中

sha256 = res[0]
if sha256 == FALLBACK_ICON:
    return (None, None)  # 命中失败缓存
```

---

## 二、HOLD_TIME 超时后读取路径是否会立即重新抓取

**答案：不会立即重新抓取。**

### 2.1 读取路径不做超时检查

`__call__` 方法（[cache.py:L320-L336](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L320-L336)）的 SQL 查询非常简单：

```sql
SELECT sha256 FROM blob_map WHERE resolver = ? AND authority = ?
```

**完全没有检查 `m_time` 字段是否超过 `HOLD_TIME`。** 只要 `(resolver, authority)` 记录存在，就直接返回缓存结果——包括失败标记 `FALLBACK_ICON`。

### 2.2 HOLD_TIME 的作用时机

`HOLD_TIME` 只在 `maintenance()` 方法中使用，用于 DELETE 过期记录。在 [cache.py:L396-L400](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L396-L400)：

```sql
DELETE FROM blob_map
WHERE cast(m_time as integer) < cast(strftime('%s', 'now') as integer) - {self.cfg.HOLD_TIME}
```

### 2.3 maintenance 的触发条件

`maintenance()` 只有在以下两种情况才会执行：

1. **写入时自动触发**：调用 `cache.set()` 时，如果 `MAINTENANCE_MODE == "auto"` 且当前时间超过 `next_maintenance_time`（[cache.py:L340-L342](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L340-L342)）
2. **手动触发**：显式调用 `maintenance()` 方法（如 CLI 命令 `python -m searx.favicons cache maintenance`）

### 2.4 完整的超时刷新链条

```
1. 记录 m_time 超过 HOLD_TIME
        │
        ▼  读取路径 __call__ 仍命中缓存（不检查时间）
2. 用户搜索触发 favicon_url() → cache.CACHE()
        │  返回 (None, None) 失败标记，前端显示占位 SVG
        │
        ▼  只有当某次写入操作触发 maintenance 时
3. cache.set() 被调用 → 检查到 next_maintenance_time 已过 → 执行 maintenance()
        │
        ▼  maintenance 执行 DELETE
4. 删除所有 m_time < now - HOLD_TIME 的 blob_map 条目
        │
        ▼
5. 下一次读取时 __call__ 返回 None（未命中）
        │
        ▼
6. search_favicon() 调用 resolver 重新抓取
        │
        ▼
7. 新结果（成功或失败）被 set() 回缓存，m_time 刷新为当前时间
```

### 2.5 "续期"机制

在 [cache.py:L306-L310](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L306-L310) 的 UPSERT 语句中：

```sql
INSERT INTO blob_map (sha256, resolver, authority) VALUES (?, ?, ?)
    ON CONFLICT DO UPDATE
   SET sha256=excluded.sha256, m_time=strftime('%s', 'now')
```

**每次 `set()` 都会刷新 `m_time` 为当前时间。** 这意味着：

- 如果一个域名反复抓取失败，每次失败时 `set(FALLBACK_ICON)` 都会将 `m_time` 刷新为"现在"
- 这会导致该失败记录的 HOLD_TIME 被不断"续期"，永远不会被 maintenance 清理
- 实际效果是：一个持续失败的域名，其失败缓存会被**永久保留**（只要每隔 30 天内至少有一次访问触发重新抓取并再次失败）

---

## 三、刷新频率的完整控制逻辑

### 3.1 各时间参数的实际意义

| 参数 | 位置 | 作用 |
|------|------|------|
| `HOLD_TIME`（30天） | [cache.py:L109](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L109) | maintenance 时清理多久以前的"死"记录（没有被续期的） |
| `MAINTENANCE_PERIOD`（1小时） | [cache.py:L124](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L124) | 自动 maintenance 的最小间隔 |
| `max_age`（7天） | [proxy.py:L47](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/proxy.py#L47) | 浏览器端 HTTP 缓存时长 |
| `resolver_timeout`（来自 outgoing.request_timeout） | [proxy.py:L57](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/proxy.py#L57) | 单次 resolver 请求超时 |

### 3.2 典型时间线示例（默认配置）

以 `example.com` 域名 + `duckduckgo` resolver 为例：

```
Day 0:  首次访问 → 未命中 → 抓取失败 → set(FALLBACK_ICON)
        m_time = Day 0, sha256 = "FALLBACK_ICON"

Day 15: 再次访问 → 命中失败缓存 → 直接返回 (None, None)
        不触发重新抓取，不触发 maintenance

Day 29: 再次访问 → 仍命中失败缓存 → 直接返回
        不触发重新抓取

Day 31: 访问 → 仍命中失败缓存（__call__ 不检查 m_time！）
        此时恰好有其他域名的 set() 触发了 maintenance
        → maintenance 发现 m_time(Day 0) < now - 30days
        → 删除该 (duckduckgo, example.com) 记录

Day 31 + ε: 再访问 → 未命中 → 重新抓取 → 仍失败 → set(FALLBACK_ICON)
        m_time 被刷新为 Day 31

Day 61:  maintenance 再次运行，该记录 m_time(Day 31) 仍未过期，保留
...
```

### 3.3 边界情况

1. **MAINTENANCE_MODE = "off"**：永远不会自动清理，所有记录（包括失败标记）永久保留，永不重新抓取。

2. **SQLite 版本 < 3.35**：降级为 `FaviconCacheNull`，完全不缓存，每次都重新抓取。

3. **BLOB 超过 BLOB_MAX_BYTES（20KB）**：不写入缓存，每次都重新抓取。

---

## 四、关键源码索引

| 功能 | 文件位置 |
|------|---------|
| blob_map 表主键定义 | [cache.py:L270-L275](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L270-L275) |
| 缓存读取（无超时检查） | [cache.py:L320-L336](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L320-L336) |
| 缓存写入（m_time 续期） | [cache.py:L306-L310](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L306-L310) |
| 失败标记写入 | [cache.py:L359-L360](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L359-L360) |
| maintenance 触发点 | [cache.py:L340-L342](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L340-L342) |
| HOLD_TIME 清理逻辑 | [cache.py:L396-L400](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L396-L400) |
| next_maintenance_time 计算 | [cache.py:L376-L379](file:///d:/fz/0601-1/solo-dogfeeding/code/13-searxng/searx/favicons/cache.py#L376-L379) |
