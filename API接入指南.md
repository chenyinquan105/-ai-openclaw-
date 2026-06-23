# 真实POI & 天气API接入指南

> 更新时间：2026-06-23
> 目的：将项目从mock数据迁移到真实全国POI+天气数据，全程合法合规

---

## 一、POI API 选型对比

| 维度 | 高德地图 | 百度地图 | 腾讯地图 | 美团地图 | 天地图 |
|------|---------|---------|---------|---------|--------|
| **免费额度** | 5000次/日(个人)<br>30000次/日(企业) | 5000次/日(个人)<br>10万次/日(企业) | 10000次/日 | 需申请(较严格) | 3000次/日 |
| **POI覆盖** | ⭐⭐⭐⭐⭐ 最全 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐(餐饮有优势) | ⭐⭐⭐ |
| **坐标系** | GCJ-02 | BD-09(需转换) | GCJ-02 | GCJ-02 | CGCS2000 |
| **注册门槛** | 手机号+支付宝认证 | 手机号+实名 | QQ/微信登录 | 企业资质为主 | 手机号 |
| **天气API** | ✅ 自带 | ✅ 自带 | ❌ 无 | ❌ 无 | ❌ 无 |
| **路径规划** | ✅ 驾车/公交/步行/骑行/电动车 | ✅ 驾车/公交/步行/骑行 | ✅ 驾车/公交/步行/骑行 | ❌ | ✅ 基础 |
| **合规性** | 甲级测绘资质 | 甲级测绘资质 | 甲级测绘资质 | 甲级测绘资质 | 国家官方 |
| **MCP Server** | ✅ 官方提供(15+工具) | ❌ 无 | ❌ 无 | ❌ 无 | ❌ 无 |

### 🏆 推荐方案

```
第一优先：高德地图 API
理由：POI覆盖最全 + 免费自带天气API + 官方MCP Server + 注册最简单 + GCJ-02标准坐标
```

---

## 二、高德地图 API 注册与接入（POI + 天气一揽子）

### 2.1 注册步骤（5分钟）

```
1. 访问 https://lbs.amap.com/
2. 右上角「注册」→ 手机号注册
3. 进入「控制台」→「应用管理」→「我的应用」
4. 点击「创建新应用」→ 名称填 "Meituan_Spatial_Butler"
5. 应用类型选择「Web服务」 ← 关键！
6. 点击「添加 Key」→ 复制保存生成的 Key
```

### 2.2 核心 API 端点

```python
# ===== POI 搜索 =====
# 关键字搜索
GET https://restapi.amap.com/v3/place/text?parameters
  参数: key, keywords, city, types, offset, page, extensions

# 周边搜索
GET https://restapi.amap.com/v3/place/around?parameters
  参数: key, location(lng,lat), radius, keywords, types

# POI详情
GET https://restapi.amap.com/v3/place/detail?parameters
  参数: key, id

# 输入提示（模糊搜索）
GET https://restapi.amap.com/v3/assistant/inputtips?parameters
  参数: key, keywords, city

# ===== 地理编码 =====
# 地址→坐标
GET https://restapi.amap.com/v3/geocode/geo?parameters
  参数: key, address, city

# 坐标→地址（逆地理编码）
GET https://restapi.amap.com/v3/geocode/regeo?parameters
  参数: key, location

# ===== 路径规划 =====
# 驾车
GET https://restapi.amap.com/v3/direction/driving?parameters
# 公交
GET https://restapi.amap.com/v3/direction/transit/integrated?parameters
# 步行
GET https://restapi.amap.com/v3/direction/walking?parameters
# 骑行
GET https://restapi.amap.com/v3/direction/bicycling?parameters

# ===== 天气 =====
# 实时天气
GET https://restapi.amap.com/v3/weather/weatherInfo?parameters
  参数: key, city(adcode), extensions=base

# 预报天气（4天）
GET https://restapi.amap.com/v3/weather/weatherInfo?parameters
  参数: key, city, extensions=all
```

### 2.3 Python 接入代码示例

```python
import requests
import time
import hashlib
from functools import lru_cache

class AmapClient:
    """高德地图 API 客户端 — 替代现有 mock 数据层"""
    
    BASE_URL = "https://restapi.amap.com/v3"
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._last_request = 0
    
    def _rate_limit(self):
        """限流：每次请求间隔 >= 0.2s，避免触发QPS限制"""
        elapsed = time.time() - self._last_request
        if elapsed < 0.2:
            time.sleep(0.2 - elapsed)
        self._last_request = time.time()
    
    def _get(self, endpoint: str, params: dict) -> dict:
        self._rate_limit()
        params["key"] = self.api_key
        resp = requests.get(f"{self.BASE_URL}/{endpoint}", params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data["status"] != "1":
            raise Exception(f"高德API错误: {data.get('info', 'unknown')}")
        return data
    
    # --- POI 搜索（替代 generic_poi_searcher 的 mock 数据库）---
    
    def search_poi(self, keywords: str, city: str = "北京", 
                   category: str = None, offset: int = 25):
        """
        关键字搜索POI
        category 可选值见高德POI分类编码：
        https://lbs.amap.com/api/webservice/download
        例如：050000=餐饮，060000=购物，080000=生活服务
        """
        params = {
            "keywords": keywords,
            "city": city,
            "offset": offset,
            "extensions": "all"  # 返回额外信息（评分、电话等）
        }
        if category:
            params["types"] = category
        
        data = self._get("place/text", params)
        return data["pois"]  # 列表，每个含 id,name,location,address,
                             # biz_ext(评分), tel, distance 等
    
    def search_nearby(self, lng: float, lat: float, radius: int = 3000,
                      keywords: str = "", category: str = ""):
        """周边搜索”（替换 Haversine 距离计算）"""
        params = {
            "location": f"{lng},{lat}",
            "radius": radius,
            "keywords": keywords,
            "offset": 25,
            "extensions": "all"
        }
        if category:
            params["types"] = category
        
        data = self._get("place/around", params)
        return data["pois"]
    
    # --- 天气（替代 weather_extractor 的伪随机模拟）---
    
    def get_weather(self, adcode: str = "110000", forecast: bool = False):
        """
        获取实时/预报天气
        adcode: 城市编码，110000=北京市，310000=上海市
        全国adcode表: https://lbs.amap.com/api/webservice/download
        """
        params = {
            "city": adcode,
            "extensions": "all" if forecast else "base"
        }
        data = self._get("weather/weatherInfo", params)
        return data["lives" if not forecast else "forecasts"]
    
    # --- 地理编码 ---
    
    def geocode(self, address: str, city: str = "北京"):
        """地址→坐标"""
        data = self._get("geocode/geo", {"address": address, "city": city})
        geocodes = data.get("geocodes", [])
        if geocodes:
            loc = geocodes[0]["location"].split(",")
            return {"lng": float(loc[0]), "lat": float(loc[1]), 
                    "formatted_address": geocodes[0].get("formatted_address", "")}
        return None
    
    def reverse_geocode(self, lng: float, lat: float):
        """坐标→地址"""
        data = self._get("geocode/regeo", {"location": f"{lng},{lat}"})
        regeo = data.get("regeocode", {})
        return regeo.get("formatted_address", "")
    
    # --- 路径规划（替代 route_planner 的贪心算法）---
    
    def plan_driving(self, origin: str, destination: str, waypoints: list = None):
        """驾车路径规划"""
        params = {"origin": origin, "destination": destination, 
                  "strategy": "0"}  # 0=速度优先
        if waypoints:
            params["waypoints"] = ";".join(waypoints)
        return self._get("direction/driving", params)
    
    def plan_transit(self, origin: str, destination: str, city: str = "北京"):
        """公交路径规划"""
        return self._get("direction/transit/integrated", 
                        {"origin": origin, "destination": destination, "city": city})
    
    def plan_walking(self, origin: str, destination: str):
        """步行路径规划"""
        return self._get("direction/walking", 
                        {"origin": origin, "destination": destination})
    
    # --- 输入提示（模糊搜索）---
    
    def input_tips(self, keywords: str, city: str = "北京"):
        """输入提示 — 用户打字时实时提示，减少输入步长"""
        data = self._get("assistant/inputtips", 
                        {"keywords": keywords, "city": city})
        return data.get("tips", [])
    
    # --- IP定位 ---
    
    def ip_location(self, ip: str = ""):
        """IP定位 — 获取用户当前城市"""
        params = {}
        if ip:
            params["ip"] = ip
        return self._get("ip", params)
```

### 2.4 替换现有模块的迁移路径

```
现有模块                           →  高德API替代
──────────────────────────────────────────────────
skills/generic_poi_searcher/      →  AmapClient.search_poi()
  14个mock商户                      AmapClient.search_nearby()
  Haversine距离计算                  API自带真实距离

skills/weather_extractor/         →  AmapClient.get_weather()
  MD5伪随机天气                     真实实时天气+4天预报

skills/route_planner/             →  AmapClient.plan_driving/walking/transit()
  贪心最近邻算法                    高德真实路径规划引擎

server.py 坐标计算                 →  AmapClient.geocode/reverse_geocode()
  (39.93, 116.45) 固定中心          动态地址↔坐标转换
```

---

## 三、天气 API 备选方案

高德自带天气已覆盖基础需求，如需更丰富的气象数据，可选：

### 3.1 和风天气（推荐备选）

| 维度 | 详情 |
|------|------|
| **免费额度** | 5万次/月（天气环境类），约1666次/天 |
| **注册** | https://console.qweather.com/ → 创建应用 → 选「免费开发版」→ Web API Key |
| **优势** | 分钟级降水预报、空气质量、灾害预警、15天预报、生活指数 |
| **劣势** | 2025年3月后改为月额度制，超出收费 |

```python
# 和风天气接入示例
QWEATHER_KEY = "your_key"
# 实时天气
url = f"https://devapi.qweather.com/v7/weather/now?location=101010100&key={QWEATHER_KEY}"
# 7天预报
url = f"https://devapi.qweather.com/v7/weather/7d?location=101010100&key={QWEATHER_KEY}"
# 分钟级降水（未来2小时逐分钟）
url = f"https://devapi.qweather.com/v7/minutely/5m?location=116.41,39.92&key={QWEATHER_KEY}"
```

### 3.2 天气方案对比

| 维度 | 高德内置天气 | 和风天气 |
|------|------------|---------|
| 免费额度 | 同POI配额共享 | 5万次/月独立 |
| 实时天气 | ✅ | ✅ |
| 4天预报 | ✅ | ❌（但有7天/15天） |
| 分钟级降水 | ❌ | ✅ |
| 空气质量 | ❌ | ✅ |
| 灾害预警 | ❌ | ✅ |
| 生活指数 | ❌ | ✅（紫外线/穿衣/洗车等） |
| 接入成本 | 0（同一个Key） | 需单独注册 |

### 🏆 推荐

```
阶段1（当前）：高德内置天气 — 零额外成本，覆盖基础需求
阶段2（后期）：+和风天气 — 分钟级降水、灾害预警等高级气象能力
```

---

## 四、合规性清单

### 4.1 必须做的

| # | 事项 | 说明 |
|---|------|------|
| 1 | **注册开发者账号** | 高德要求手机号+支付宝实名认证，合规使用 |
| 2 | **遵守API调用限制** | 个人5000次/日，不要用脚本刷接口 |
| 3 | **展示高德Logo** | 使用高德数据的地图页面必须展示高德Logo和copyright |
| 4 | **不存储地图数据** | 不得将API返回的数据建库长期存储（仅可缓存短期） |
| 5 | **不二次分发** | API数据仅用于自己产品，不得作为数据源卖给第三方 |
| 6 | **使用GCJ-02坐标** | 国内地图产品必须使用国测局加密坐标，不得直接展示WGS-84 |
| 7 | **备案ICP** | 如果上线公网，网站需要ICP备案 |

### 4.2 绝对不能做的

| # | 禁止行为 | 后果 |
|---|---------|------|
| 1 | 爬取高德/百度地图瓦片或POI数据 | 违反服务条款，可能面临法律诉讼 |
| 2 | 绕过API限制批量采集 | 封号+法律风险 |
| 3 | 在中国境内使用未经审核的国外地图（Google Maps、OSM等）展示中国区域 | 违反《测绘法》 |
| 4 | 将免费API数据用于大型商业产品而不付费 | 服务条款违规 |

### 4.3 数据缓存策略（合规且节省配额）

```python
import json
import time
from pathlib import Path

class CachedAmapClient(AmapClient):
    """带本地缓存的API客户端 — 相同查询24h内不重复请求"""
    
    CACHE_DIR = Path("./cache/amap")
    TTL = 86400  # 24小时
    
    def _cache_key(self, endpoint: str, params: dict) -> str:
        raw = f"{endpoint}:{json.dumps(params, sort_keys=True)}"
        return hashlib.md5(raw.encode()).hexdigest()
    
    def _get(self, endpoint: str, params: dict) -> dict:
        # 天气类查询: TTL=30分钟; POI查询: TTL=24小时
        ttl = 1800 if "weather" in endpoint else self.TTL
        
        key = self._cache_key(endpoint, params)
        cache_file = self.CACHE_DIR / key
        
        if cache_file.exists():
            cached = json.loads(cache_file.read_text())
            if time.time() - cached["ts"] < ttl:
                return cached["data"]
        
        data = super()._get(endpoint, params)
        
        self.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"ts": time.time(), "data": data}))
        return data
```

---

## 五、费用预估

### 5.1 免费额度够用吗？

```
假设产品日活：100人
每人平均查询次数：10次/天
总日调用量：1000次/天
高德个人免费额度：5000次/天
余量：4000次/天 → 绰绰有余 ✓

假设产品日活：1000人
总日调用量：10000次/天
需升级：企业认证（30000次/日）或购买付费套餐

和风天气：5万次/月 ≈ 1666次/天
按100人日活，每人查2次天气 = 200次/天 → 绰绰有余 ✓
```

### 5.2 如需付费

| 平台 | 套餐 | 价格 | 额度 |
|------|------|------|------|
| 高德 | 企业认证 | 免费 | 30万次/日 |
| 高德 | API付费套餐 | 联系商务 | 按需定制 |
| 和风 | 基础版 | ¥0.0007/次 | 超出免费5万次后 |
| 和风 | 专业版 | 联系商务 | 更高QPS+SLA |

---

## 六、替代方案：如果不想接入高德

| 方案 | 适用场景 | 成本 |
|------|---------|------|
| **百度地图 API** | 需要百度生态联动 | 免费5000次/日，企业10万次/日 |
| **腾讯地图 API** | 微信小程序项目 | 免费10000次/日 |
| **美团地图 API** | 餐饮POI优先，且能通过企业审核 | 需企业资质 |
| **天地图 API** | 政府/国企项目，要求最高合规性 | 免费3000次/日 |
| **Nominatim + Overpass** | 海外项目或个人学习 | 免费但有rate limit |
| **自建POI库** | 大型商业项目，长期成本优化 | 初始投入高 |

---

## 七、推荐实施顺序

```
第1步（今天）：注册高德开发者 → 创建应用 → 获取API Key → 写入.env
第2步（1天）：  实现 AmapClient 基础类 → 替换 weather_extractor（最简单）
第3步（2天）：  用 AmapClient.search_poi() 替换 generic_poi_searcher mock数据
第4步（2天）：  用 AmapClient.plan_driving/transit/walking 替换 route_planner
第5步（1天）：  添加缓存层 CachedAmapClient → 前端适配新数据格式
第6步（后续）： 需要高级气象时接入和风天气
```

> 📌 **关键原则**：高德API Key 写入 `.env` 文件，不要提交到 git。在 `.gitignore` 中确认 `.env` 已加入。
