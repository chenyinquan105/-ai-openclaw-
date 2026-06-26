# amap_poi — 高德地图真实 POI 检索 Skill

## Skill Identity

| Field | Value |
|-------|-------|
| **Skill ID** | `amap_poi` |
| **Module** | `skills.amap_poi.amap_poi` |
| **Entry Class** | `AmapPOIClient` (模块级便捷函数提供无状态调用) |
| **Dependencies** | `requests` (HTTP), 环境变量 `AMAP_API_KEY` |
| **Domain** | 真实 POI 搜索 / 地理编码 / 周边检索 |
| **Data Source** | 高德地图 Web 服务 API (真实全国数据, 非 Mock) |
| **Rate Limit** | 内置限流 + 文件缓存 (默认 TTL 86400s) |

## 语义描述触发（意图描述）

当用户或上游系统需要 **搜索真实商户**、**周边检索**、**模糊语义匹配 POI**、**地址↔坐标转换** 时，调用本 Skill。

典型触发场景：

- "帮我找三里屯附近的川菜馆" → `search_poi(keywords="川菜", city="北京")`
- "搜一下周围 3km 内评分 4.5+ 的日料" → `search_nearby(lng, lat, radius=3000, category="japanese", min_rating=4.5)`
- "有变形金刚的游乐园" → `fuzzy_search(keywords="变形金刚 游乐园")`
- "查一下这家店的详细信息" → `get_poi_detail(poi_id="B0FFF...")`
- "三里屯太古里的坐标是多少" → `geocode(address="三里屯太古里")`
- "这个经纬度是什么地方" → `reverse_geocode(lng=116.455, lat=39.932)`

**不触发场景：**
- 仅需 Mock 数据/沙盒演练 → 使用 `generic_poi_searcher` 的 `search_poi_matrix`
- 排程/时间冲突解决 → 使用 `concurrent_pipeline_scheduler`
- 路径规划 → 使用 `route_planner`

**与 `generic_poi_searcher` 的关系：**
- `generic_poi_searcher` — Mock 14 家三里屯商户，无外部依赖，沙盒安全
- `amap_poi` — 真实高德 API，全国数据，需要 API Key
- `search_poi_matrix()` 桥接函数提供与 `generic_poi_searcher` 完全兼容的接口

## 输入协议（Input Protocol）

### 函数签名一览

| 函数 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `search_poi` | `keywords: str, city: str, category: str?, offset: int?` | `dict` | 关键字搜索，返回 `{"count": N, "shops": [...]}` |
| `search_nearby` | `lng: float, lat: float, radius: int, keywords: str?, category: str?, min_rating: float?` | `dict` | 周边搜索，支持评分过滤 |
| `fuzzy_search` | `keywords: str, city: str?` | `list[dict]` | 输入提示/模糊匹配 |
| `get_poi_detail` | `poi_id: str` | `dict` | POI 详情查询 |
| `geocode` | `address: str, city: str?` | `dict` | 地址 → 坐标 |
| `reverse_geocode` | `lng: float, lat: float` | `dict` | 坐标 → 地址 |
| `search_poi_matrix` | `center_coord, categories, radius_meters, min_rating, ...` | `dict` | 桥接函数，兼容 `generic_poi_searcher` 接口 |

### 合法 category 值

| 品类编码 | 中文 | 高德分类码 |
|---------|------|----------|
| `hair` | 美发 | 071400 |
| `pet` | 宠物服务 | 080600 |
| `cafe` | 咖啡馆/茶饮 | 050500 |
| `gym` | 运动健身 | 080800 |
| `restaurant` | 中餐厅 | 050100 |
| `japanese` | 日料 | 050200 |
| `hotpot` | 火锅 | 050100 |
| `cinema` | 电影院 | 060400 |
| `laundry` | 洗衣店 | 080600 |

### search_poi —— 关键字搜索

```json
{
  "type": "object",
  "required": ["keywords"],
  "properties": {
    "keywords": {"type": "string", "description": "搜索关键字，如「川菜」「咖啡馆」「宠物店」"},
    "city": {"type": "string", "description": "城市名，默认「北京」"},
    "category": {"type": "string", "description": "品类编码，可选。传入后使用高德分类码精确匹配"},
    "offset": {"type": "integer", "description": "返回条数，默认 20，上限 25"}
  }
}
```

### search_nearby —— 周边搜索

```json
{
  "type": "object",
  "required": ["lng", "lat"],
  "properties": {
    "lng": {"type": "number", "description": "中心点经度"},
    "lat": {"type": "number", "description": "中心点纬度"},
    "radius": {"type": "integer", "description": "搜索半径(米)，默认 3000，上限 50000"},
    "keywords": {"type": "string", "description": "搜索关键字，可选"},
    "category": {"type": "string", "description": "品类编码，可选"},
    "min_rating": {"type": "number", "description": "最低评分过滤，默认 0（不过滤）"}
  }
}
```

### geocode —— 地址转坐标

```json
{
  "type": "object",
  "required": ["address"],
  "properties": {
    "address": {"type": "string", "description": "地址文本，如「三里屯太古里」「北京市朝阳区工人体育场北路」"},
    "city": {"type": "string", "description": "城市名，默认「北京」"}
  }
}
```

### reverse_geocode —— 坐标转地址

```json
{
  "type": "object",
  "required": ["lng", "lat"],
  "properties": {
    "lng": {"type": "number", "description": "经度"},
    "lat": {"type": "number", "description": "纬度"}
  }
}
```

## 输出协议（Output Protocol）

### search_poi / search_nearby 返回格式

```json
{
  "count": 5,
  "shops": [
    {
      "shop_id": "B0FFF...",
      "name": "海底捞火锅(三里屯店)",
      "rating": 4.6,
      "lng": 116.455,
      "lat": 39.932,
      "coord": "39.932,116.455",
      "category": "hotpot",
      "category_alias": "火锅",
      "human_needed": true,
      "phone": "010-6418-xxxx",
      "address": "三里屯太古里南区3层",
      "signature_dishes": [
        {"name": "招牌虾滑", "price": "¥58", "image_url": ""}
      ],
      "top_comments": [
        {"user": "吃货小王", "text": "服务好，食材新鲜", "rating": 4.8}
      ]
    }
  ]
}
```

### geocode 返回格式

```json
{
  "lng": 116.455,
  "lat": 39.932,
  "formatted_address": "北京市朝阳区三里屯太古里",
  "adcode": "110105"
}
```

### reverse_geocode 返回格式

```json
{
  "formatted_address": "北京市朝阳区三里屯路19号",
  "city": "北京市",
  "district": "朝阳区",
  "street": "三里屯路",
  "adcode": "110105"
}
```

### search_poi_matrix 返回格式

与 `generic_poi_searcher.search_poi_matrix()` 完全兼容：

```json
{
  "status": "SUCCESS",
  "message": "",
  "search_results": {
    "cafe": [{ "shop_id": "...", "name": "...", ... }],
    "japanese": [{ "shop_id": "...", "name": "...", ... }]
  }
}
```

错误时返回 `{"status": "ERROR", "message": "错误描述"}`。

## 少样本示例（Few-Shot Examples）

### 示例 1：关键字搜索川菜

**场景：** 用户说"帮我找北京的川菜馆"

```python
from skills.amap_poi import search_poi

result = search_poi(keywords="川菜", city="北京")
```

**输出（截取）：**

```json
{
  "count": 20,
  "shops": [
    {
      "shop_id": "B0FFFGUHJE",
      "name": "眉州东坡酒楼(三里屯店)",
      "rating": 4.5,
      "lng": 116.456,
      "lat": 39.931,
      "coord": "39.931,116.456",
      "category": "restaurant",
      "category_alias": "中餐厅",
      "human_needed": true,
      "phone": "010-6417-1666",
      "address": "朝阳区三里屯太古里北区",
      "signature_dishes": [
        {"name": "东坡肘子", "price": "¥88", "image_url": ""}
      ],
      "top_comments": []
    }
  ]
}
```

**说明：** `search_poi` 直接调用高德关键字搜索 API，返回真实商户数据。结果已归一化为项目统一格式。

---

### 示例 2：周边搜索 + 评分过滤

**场景：** 在三里屯太古里周边 3km 搜索评分 4.0+ 的咖啡馆

```python
from skills.amap_poi import search_nearby

# 三里屯太古里坐标
result = search_nearby(
    lng=116.455,
    lat=39.932,
    radius=3000,
    category="cafe",
    min_rating=4.0
)
```

**说明：** `search_nearby` 调用高德周边搜索 API，按品类精确匹配，`min_rating` 参数在返回结果中做客户端过滤。

---

### 示例 3：地理编码 —— 地址转坐标

**场景：** 排程引擎需要「三里屯太古里」的经纬度来做路径规划

```python
from skills.amap_poi.amap_poi import AmapPOIClient

client = AmapPOIClient()
coord = client.geocode("三里屯太古里", city="北京")
# → {"lng": 116.455, "lat": 39.932, "formatted_address": "北京市朝阳区三里屯太古里", "adcode": "110105"}
```

**说明：** 地理编码是路径规划的前置依赖。`route_planner` 需要起终点坐标时，先调 `geocode` 获取经纬度。
