# SKILL.md — generic_poi_searcher

## Skill Identity

- **Skill ID:** `generic_poi_searcher`
- **Skill 名称:** 通用空间商户检索器
- **实现文件:** `generic_poi_searcher.py`
- **核心函数:** `search_poi_matrix(center_coord, categories, radius_meters, min_rating) -> dict`
- **数据源:** 本地 Mock 字典（高德/美团 POI 数据模拟，安全隔离，无外部 API 调用）
- **运行时模式:** 纯内存计算（Haversine 球面距离 + 评分/品类过滤），无副作用
- **合法 category 值:**
  `"hair"`, `"pet"`, `"cafe"`, `"gym"`, `"restaurant"`, `"japanese"`, `"hotpot"`, `"cinema"`, `"laundry"`
- **human_needed 语义:**
  - `True` — 需人在场的服务（理发、咖啡、健身、堂食、影院等）
  - `False` — 可放下即走的服务（宠物洗护、干洗等）

---

## 语义描述触发（意图描述）

当用户完成 **品类选择** 后，需要检索附近商户列表的场景。触发条件包括但不限于以下意图：

1. 用户选择品类后自动拉取附近商户 → 调用本技能
2. 用户指定品类 + 位置范围 + 最低评分 → 调用本技能
3. 用户要求推荐「附近某某类店铺」 → 调用本技能
4. 用户需要知道某个品类下哪些商户符合距离/评分条件 → 调用本技能

**不触发场景：**
- 用户尚未确定品类时不应调用
- 仅需商户详情（单个商户信息）时不适用此技能

---

## 输入协议（Input Protocol）

调用 `search_poi_matrix(center_coord, categories, radius_meters, min_rating)` 时，传入参数必须严格满足以下 JSON Schema：

```json
{
  "type": "object",
  "required": ["center_coord", "categories", "radius_meters", "min_rating"],
  "properties": {
    "center_coord": {
      "type": "string",
      "description": "搜索中心点坐标，格式为 \"lat,lng\"，lat 和 lng 均为十进制浮点数",
      "examples": ["39.93,116.45", "31.23,121.47"]
    },
    "categories": {
      "type": "array",
      "items": {
        "type": "string",
        "enum": ["hair", "pet", "cafe", "gym", "restaurant", "japanese", "hotpot", "cinema", "laundry"]
      },
      "minItems": 1,
      "description": "需要检索的品类编码列表。支持同时检索多个品类"
    },
    "radius_meters": {
      "type": "integer",
      "minimum": 1,
      "description": "搜索半径，单位：米。表示以 center_coord 为圆心、radius_meters 为半径的圆形搜索范围"
    },
    "min_rating": {
      "type": "number",
      "minimum": 0,
      "maximum": 5,
      "description": "最低评分阈值。仅返回评分 ≥ min_rating 的商户"
    }
  }
}
```

---

## 输出协议（Output Protocol）

函数返回值的 JSON Schema 如下：

```json
{
  "type": "object",
  "required": ["status", "message"],
  "properties": {
    "status": {
      "type": "string",
      "enum": ["SUCCESS", "ERROR"],
      "description": "调用结果状态。SUCCESS 表示正常返回；ERROR 表示入参校验失败"
    },
    "message": {
      "type": "string",
      "description": "当 status 为 ERROR 时，此处为错误描述。当 status 为 SUCCESS 时，此字段可为空字符串"
    },
    "search_results": {
      "type": "object",
      "description": "仅当 status 为 SUCCESS 时存在。键为品类编码（与入参 categories 一一对应），值为该品类下的商户列表",
      "additionalProperties": {
        "type": "array",
        "items": {
          "type": "object",
          "required": ["shop_id", "name", "rating", "lat", "lng", "coord", "category", "category_alias", "human_needed", "phone", "address"],
          "properties": {
            "shop_id": {
              "type": "string",
              "description": "商户唯一标识"
            },
            "name": {
              "type": "string",
              "description": "商户名称"
            },
            "rating": {
              "type": "number",
              "description": "商户评分（0.0 ~ 5.0）"
            },
            "lat": {
              "type": "number",
              "description": "商户纬度"
            },
            "lng": {
              "type": "number",
              "description": "商户经度"
            },
            "coord": {
              "type": "string",
              "description": "商户坐标 \"lat,lng\" 字符串"
            },
            "category": {
              "type": "string",
              "enum": ["hair", "pet", "cafe", "gym", "restaurant", "japanese", "hotpot", "cinema", "laundry"],
              "description": "品类英文编码"
            },
            "category_alias": {
              "type": "string",
              "description": "品类中文别名，如 \"美发\"、\"宠物店\"、\"咖啡馆\""
            },
            "human_needed": {
              "type": "boolean",
              "description": "True 表示需人在场；False 表示可放下即走"
            },
            "phone": {
              "type": "string",
              "description": "联系电话"
            },
            "address": {
              "type": "string",
              "description": "详细地址"
            },
            "signature_dishes": {
              "type": "array",
              "items": {
                "type": "object",
                "properties": {
                  "name": {"type": "string"},
                  "price": {"type": "string"},
                  "image_url": {"type": "string"}
                }
              },
              "description": "招牌菜品/服务列表"
            },
            "top_comments": {
              "type": "array",
              "items": {
                "type": "object",
                "properties": {
                  "user": {"type": "string"},
                  "text": {"type": "string"},
                  "rating": {"type": "number"}
                }
              },
              "description": "精选用户评价列表"
            }
          }
        }
      }
    }
  }
}
```

---

## 少样本示例（Few-Shot Examples）

### 示例 1：搜索三里屯附近评分 4.0+ 的美发、宠物店、咖啡馆

**输入：**

```python
search_poi_matrix(
    center_coord="39.93,116.45",
    categories=["hair", "pet", "cafe"],
    radius_meters=1500,
    min_rating=4.0
)
```

**输出（截取摘要）：**

```json
{
  "status": "SUCCESS",
  "message": "",
  "search_results": {
    "hair": [
      {
        "shop_id": "shop_hair_01",
        "name": "沙宣三里屯店",
        "rating": 4.8,
        "lat": 39.934,
        "lng": 116.453,
        "coord": "39.934,116.453",
        "category": "hair",
        "category_alias": "美发",
        "human_needed": true,
        "phone": "010-6416-8888",
        "address": "三里屯太古里南区S1-12",
        "signature_dishes": [
          {"name": "沙宣剪发套餐", "price": "¥268", "image_url": ""},
          {"name": "造型烫发", "price": "¥588", "image_url": ""}
        ],
        "top_comments": [
          {"user": "Lisa", "text": "剪发很细致，环境不错", "rating": 4.8},
          {"user": "Tony老师", "text": "造型师专业，推荐", "rating": 4.7}
        ]
      },
      {
        "shop_id": "shop_hair_02",
        "name": "托尼形象设计",
        "rating": 4.6,
        "lat": 39.935,
        "lng": 116.45,
        "coord": "39.935,116.45",
        "category": "hair",
        "category_alias": "美发",
        "human_needed": true,
        "phone": "010-6417-1234",
        "address": "三里屯路33号3层3018",
        "signature_dishes": [
          {"name": "男士精剪", "price": "¥158", "image_url": ""},
          {"name": "日系染发", "price": "¥398", "image_url": ""}
        ],
        "top_comments": [
          {"user": "王大锤", "text": "性价比很高", "rating": 4.6},
          {"user": "小美", "text": "染发颜色很正", "rating": 4.5}
        ]
      }
    ],
    "pet": [
      {
        "shop_id": "shop_pet_01",
        "name": "酷迪宠物三里屯店",
        "rating": 4.9,
        "lat": 39.933,
        "lng": 116.454,
        "coord": "39.933,116.454",
        "category": "pet",
        "category_alias": "宠物店",
        "human_needed": false,
        "phone": "010-6415-6666",
        "address": "三里屯SOHO 5号商场B1",
        "signature_dishes": [
          {"name": "宠物精洗", "price": "¥88", "image_url": ""},
          {"name": "宠物美容", "price": "¥198", "image_url": ""}
        ],
        "top_comments": [
          {"user": "狗爸", "text": "洗得很干净，服务好", "rating": 4.9},
          {"user": "猫奴一号", "text": "猫咪洗澡很温柔", "rating": 4.8}
        ]
      }
    ],
    "cafe": [
      {
        "shop_id": "shop_cafe_01",
        "name": "星巴克臻选三里屯",
        "rating": 4.4,
        "lat": 39.932,
        "lng": 116.455,
        "coord": "39.932,116.455",
        "category": "cafe",
        "category_alias": "咖啡馆",
        "human_needed": true,
        "phone": "010-6418-3333",
        "address": "三里屯太古里南区1层",
        "signature_dishes": [
          {"name": "拿铁咖啡", "price": "¥38", "image_url": ""},
          {"name": "抹茶星冰乐", "price": "¥42", "image_url": ""}
        ],
        "top_comments": [
          {"user": "咖啡控", "text": "环境很好，适合办公", "rating": 4.5},
          {"user": "周末逛逛", "text": "位置好找，咖啡不错", "rating": 4.4}
        ]
      },
      {
        "shop_id": "shop_cafe_05",
        "name": "瑞幸咖啡三里屯SOHO",
        "rating": 4.2,
        "lat": 39.933,
        "lng": 116.452,
        "coord": "39.933,116.452",
        "category": "cafe",
        "category_alias": "咖啡馆",
        "human_needed": true,
        "phone": "010-6418-0005",
        "address": "三里屯SOHO 1号楼1层",
        "signature_dishes": [
          {"name": "生椰拿铁", "price": "¥22", "image_url": ""},
          {"name": "厚乳拿铁", "price": "¥24", "image_url": ""}
        ],
        "top_comments": [
          {"user": "上班族", "text": "性价比高，出杯快", "rating": 4.3},
          {"user": "小困", "text": "推荐生椰拿铁", "rating": 4.2}
        ]
      },
      {
        "shop_id": "shop_cafe_06",
        "name": "蜜雪冰城三里屯店",
        "rating": 4.0,
        "lat": 39.934,
        "lng": 116.451,
        "coord": "39.934,116.451",
        "category": "cafe",
        "category_alias": "水吧",
        "human_needed": true,
        "phone": "010-6418-0006",
        "address": "三里屯路甲9号",
        "signature_dishes": [
          {"name": "柠檬水", "price": "¥6", "image_url": ""},
          {"name": "冰淇淋", "price": "¥3", "image_url": ""}
        ],
        "top_comments": [
          {"user": "学生党", "text": "便宜又好喝", "rating": 4.2},
          {"user": "冬天也要吃", "text": "冰淇淋yyds", "rating": 4.0}
        ]
      }
    ]
  }
}
```

### 示例 2：入参校验失败 —— 无效坐标格式

**输入：**

```python
search_poi_matrix(
    center_coord="invalid",
    categories=["cafe"],
    radius_meters=1000,
    min_rating=4.0
)
```

**输出：**

```json
{
  "status": "ERROR",
  "message": "center_coord 格式必须为 'lat,lng'，实际收到: 'invalid'",
  "search_results": {}
}
```

**说明：** 当输入参数不合法时，返回 `status: "ERROR"` 并提供明确错误信息，此时 `search_results` 应为空对象。调用方应据此提示用户修正输入。
