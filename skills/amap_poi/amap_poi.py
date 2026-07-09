"""
amap_poi.py —— 高德地图 POI 搜索技能
========================================
替代 generic_poi_searcher 的 Mock 数据库，
直接调用高德地图 Web 服务 API 获取真实全国 POI 数据。

物理契约:
  search_poi(keywords, city, category, offset) -> list[dict]
  search_nearby(lng, lat, radius, keywords, category) -> list[dict]
  get_poi_detail(poi_id) -> dict
  fuzzy_search(keywords, city) -> list[dict]

API Key 来源: 环境变量 AMAP_API_KEY
"""

import os
import time
import json
import hashlib
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

# 预置商户数据（demo 加速用，懒加载）
_PRE_CACHED = None

def _get_pre_cached():
    global _PRE_CACHED
    if _PRE_CACHED is None:
        try:
            from skills.amap_poi.pre_cached_shops import PRE_CACHED_SHOPS
            _PRE_CACHED = PRE_CACHED_SHOPS
        except ImportError:
            _PRE_CACHED = {}
    return _PRE_CACHED


# ======================================================================
# 高德 POI 分类编码（与项目现有品类映射）
# ======================================================================
CATEGORY_CODE_MAP = {
    "hair":        "071400",   # 美容美发 → 美发
    "pet":         "080600",   # 生活服务 → 宠物服务
    "cafe":        "050500",   # 餐饮 → 咖啡厅/茶饮
    "gym":         "080800",   # 生活服务 → 运动健身
    "restaurant":  "050100",   # 餐饮 → 中餐厅（泛指）
    "japanese":    "050200",   # 餐饮 → 外国餐厅 → 日料
    "hotpot":      "050100",   # 餐饮 → 火锅（中餐厅子类）
    "cinema":      "060400",   # 购物 → 电影院
    "laundry":     "080600",   # 生活服务 → 洗衣店
    "hotel":       "100000",   # 住宿服务 → 酒店
    "scenic":      "110000",   # 风景名胜 → 景点
    "breakfast":   "050100",   # 餐饮 → 早餐（中餐厅子类）
    "shopping":    "060000",   # 购物 → 商场
    "market":      "060000",   # 购物 → 菜市场/农贸市场
}

# 高德 POI 大类编码（用于周边搜索的宽松匹配）
CATEGORY_BROAD_MAP = {
    "hair":        "071400",
    "pet":         "080600",
    "cafe":        "050500|050300",   # 咖啡厅 + 茶饮
    "gym":         "080800",
    "restaurant":  "050000",          # 餐饮全部
    "japanese":    "050200",
    "hotpot":      "050100",
    "cinema":      "060400",
    "laundry":     "080600",
    "hotel":       "100000",          # 住宿服务全部
    "scenic":      "110000|140000",   # 风景名胜 + 公共设施
    "breakfast":   "050100",          # 餐饮 → 早餐
    "shopping":    "060000",          # 购物全部
    "market":      "060000",          # 购物全部（菜市场）
}

# 搜索关键词映射（传给高德 API 收紧结果）
CATEGORY_KEYWORD_MAP = {
    "hair":        "美发|理发|剪发|造型|发廊|沙龙",
    "pet":         "宠物|宠物店|宠物医院|猫|狗",
    "cafe":        "咖啡|奶茶|茶饮",
    "gym":         "健身|瑜伽|游泳",
    "restaurant":  "餐厅|中餐|快餐",
    "japanese":    "日料|日式|居酒屋|寿司",
    "hotpot":      "火锅|串串|麻辣烫",
    "cinema":      "电影院|影城",
    "laundry":     "洗衣|干洗",
    "hotel":       "酒店|宾馆|民宿|旅馆|住宿",
    "scenic":      "景点|公园|博物馆|景区|名胜|古迹|寺庙",
    "breakfast":   "早餐|早点|豆浆|油条|包子",
    "shopping":    "商场|购物|步行街|百货|商圈",
    "market":      "菜市场|农贸市场|菜场|生鲜|买菜|赶集",
}

# 客户端名称黑名单（API 分类不精确时的兜底过滤）
CATEGORY_NAME_BLACKLIST = {
    "hair":        ["SPA", "spa", "按摩", "养生", "足道", "足疗", "推拿", "中医", "采耳"],
    "cafe":        ["披萨", "pizza", "汉堡", "火锅", "烧烤", "烤肉", "麻辣烫", "串串",
                    "面馆", "饺子", "包子", "拉面", "卤味"],
    "pet":         ["剧场", "影院", "影城", "电影", "酒店", "KTV", "剧院"],
    "scenic":      ["酒店", "宾馆", "旅馆", "民宿", "饭店", "餐厅", "厕所", "卫生间"],
    "hotel":       ["饭店", "餐厅", "洗浴", "KTV", "网吧", "棋牌"],
}


class AmapPOIClient:
    """高德地图 POI API 客户端"""

    BASE_URL = "https://restapi.amap.com/v3"

    def __init__(self, api_key: str = None, cache_ttl: int = 86400):
        self.api_key = api_key or os.getenv("AMAP_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "AMAP_API_KEY 未设置。请去 https://lbs.amap.com/ 注册获取 Key，"
                "然后写入 .env 文件: AMAP_API_KEY=你的key"
            )
        self.cache_dir = Path(__file__).parent.parent.parent / "cache" / "amap"
        self.cache_ttl = cache_ttl  # POI 缓存 24 小时
        self._last_request = 0

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _rate_limit(self):
        """限流：保证每次请求间隔 >= 200ms，避免触发 QPS 限制"""
        elapsed = time.time() - self._last_request
        if elapsed < 0.2:
            time.sleep(0.2 - elapsed)
        self._last_request = time.time()

    def _cache_key(self, endpoint: str, params: dict) -> str:
        raw = f"{endpoint}:{json.dumps(params, sort_keys=True, ensure_ascii=False)}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _cache_get(self, key: str) -> Optional[dict]:
        f = self.cache_dir / key
        if f.exists():
            try:
                data = json.loads(f.read_text())
                if time.time() - data["ts"] < self.cache_ttl:
                    return data["payload"]
            except (json.JSONDecodeError, KeyError):
                pass
        return None

    def _cache_set(self, key: str, payload: dict):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        f = self.cache_dir / key
        f.write_text(json.dumps({"ts": time.time(), "payload": payload}, ensure_ascii=False))

    def _call(self, endpoint: str, params: dict) -> dict:
        """带限流、缓存、错误处理的 API 调用"""
        params = {k: v for k, v in params.items() if v is not None}
        params["key"] = self.api_key

        # 读缓存
        ck = self._cache_key(endpoint, params)
        cached = self._cache_get(ck)
        if cached is not None:
            return cached

        self._rate_limit()
        try:
            resp = requests.get(
                f"{self.BASE_URL}/{endpoint}",
                params=params,
                timeout=10,
                headers={"User-Agent": "MeituanSpatialButler/1.0"}
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            raise RuntimeError(f"高德API网络错误: {e}")
        except json.JSONDecodeError:
            raise RuntimeError("高德API返回非JSON数据")

        if data.get("status") != "1":
            raise RuntimeError(
                f"高德API错误 (code={data.get('infocode')}): {data.get('info', 'unknown')}"
            )

        # 写缓存
        self._cache_set(ck, data)
        return data

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def search_poi(
        self,
        keywords: str,
        city: str = "北京",
        category: str = None,
        offset: int = 20,
        page: int = 1,
    ) -> dict:
        """
        关键字搜索 POI —— 替代 generic_poi_searcher.search_poi_matrix()

        返回格式与现有代码兼容:
        {
          "count": 总条数,
          "shops": [{shop_id, name, rating, lat, lng, category, category_alias,
                     human_needed, phone, address, signature_dishes, top_comments, distance}]
        }
        """
        params = {
            "keywords": keywords,
            "city": city,
            "offset": offset,
            "page": page,
            "extensions": "all",
        }
        if category:
            params["types"] = CATEGORY_CODE_MAP.get(category, category)

        data = self._call("place/text", params)
        pois = data.get("pois", [])

        return {
            "count": int(data.get("count", 0)),
            "shops": [self._normalize_poi(p) for p in pois],
        }

    def search_nearby(
        self,
        lng: float,
        lat: float,
        radius: int = 3000,
        keywords: str = "",
        category: str = None,
        min_rating: float = 0,
        offset: int = 25,
    ) -> dict:
        """
        周边搜索 —— 替代 Haversine 距离计算 + 本地过滤

        参数:
          lng, lat: 中心点坐标 (GCJ-02)
          radius: 搜索半径(米)
          keywords: 搜索关键词（可选，空字符串=不限制）
          category: 项目内部品类编码（如 "hair","pet"）
          min_rating: 最低评分过滤
        """
        params = {
            "location": f"{lng},{lat}",
            "radius": radius,
            "offset": offset,
            "extensions": "all",
        }
        # 关键词优先：品类 → 关键词映射，比 types 分类编码精准得多
        effective_keywords = keywords
        if not effective_keywords and category:
            effective_keywords = CATEGORY_KEYWORD_MAP.get(category, "")
        if effective_keywords:
            params["keywords"] = effective_keywords
        # types 仅在没有关键词时作为兜底
        elif category:
            params["types"] = CATEGORY_BROAD_MAP.get(category, category)

        data = self._call("place/around", params)
        pois = data.get("pois", [])

        shops = [self._normalize_poi(p) for p in pois]
        if min_rating > 0:
            rated = [s for s in shops if s.get("rating", 0) >= min_rating]
            # 如果评分过滤后为空但原始结果全为0分（公共设施），保留全部
            if rated or all(s.get("rating", 0) == 0 for s in shops):
                shops = rated if rated else shops
            else:
                shops = rated

        # 客户端黑名单过滤（API 分类不精确的兜底）
        if category and category in CATEGORY_NAME_BLACKLIST:
            blacklist = CATEGORY_NAME_BLACKLIST[category]
            shops = [
                s for s in shops
                if not any(kw in s.get("name", "") for kw in blacklist)
            ]

        return {
            "count": len(shops),
            "shops": shops,
        }

    def get_poi_detail(self, poi_id: str) -> dict:
        """获取单个 POI 详细信息"""
        data = self._call("place/detail", {"id": poi_id})
        pois = data.get("pois", [])
        if not pois:
            raise ValueError(f"POI不存在: {poi_id}")
        return self._normalize_poi(pois[0])

    def fuzzy_search(self, keywords: str, city: str = "北京") -> list[dict]:
        """
        输入提示（模糊搜索）—— 用户打字时实时提示

        返回: [{name, address, location, adcode}]
        """
        data = self._call("assistant/inputtips", {"keywords": keywords, "city": city})
        tips = data.get("tips", [])
        return [
            {
                "name": t.get("name", ""),
                "address": t.get("address", ""),
                "location": t.get("location", ""),
                "district": t.get("district", ""),
            }
            for t in tips
            if t.get("location")  # 过滤掉无坐标的行政区划提示
        ]

    def geocode(self, address: str, city: str = "北京") -> Optional[dict]:
        """地址 → 坐标"""
        data = self._call("geocode/geo", {"address": address, "city": city})
        geocodes = data.get("geocodes", [])
        if not geocodes:
            return None
        g = geocodes[0]
        loc = g["location"].split(",")
        return {
            "lng": float(loc[0]),
            "lat": float(loc[1]),
            "formatted_address": g.get("formatted_address", address),
            "adcode": g.get("adcode", ""),
        }

    def reverse_geocode(self, lng: float, lat: float) -> dict:
        """坐标 → 地址"""
        data = self._call("geocode/regeo", {"location": f"{lng},{lat}", "extensions": "base"})
        regeo = data.get("regeocode", {})
        comp = regeo.get("addressComponent", {})
        return {
            "formatted_address": regeo.get("formatted_address", ""),
            "city": comp.get("city", []),
            "district": comp.get("district", ""),
            "street": comp.get("streetNumber", {}).get("street", ""),
            "adcode": comp.get("adcode", ""),
        }

    # ------------------------------------------------------------------
    # 数据标准化 —— 将高德格式转换为项目内部统一格式
    # ------------------------------------------------------------------

    def _normalize_poi(self, poi: dict) -> dict:
        """
        将高德 API 返回的 POI 转换为项目兼容格式

        高德字段 → 项目字段:
          id → shop_id
          name → name
          biz_ext.rating → rating
          location (lng,lat) → lng, lat
          type → category (尝试映射)
          biz_ext.cost → signature_dishes[0].price 估算
          tel → phone
          address → address
          distance → distance
          deep_info → 额外信息
        """
        loc = poi.get("location", "0,0").split(",")
        lng = float(loc[0]) if len(loc) > 0 else 0
        lat = float(loc[1]) if len(loc) > 1 else 0

        biz_ext = poi.get("biz_ext", {}) or {}
        rating_str = biz_ext.get("rating", "0")
        try:
            rating = float(rating_str)
        except (ValueError, TypeError):
            rating = 0.0

        # 高德 type 编码 → 项目品类
        amap_type = poi.get("type", "")
        category, category_alias = self._map_amap_type_to_category(amap_type)

        # 人均消费
        cost_str = biz_ext.get("cost", "")
        avg_price = ""
        try:
            avg_price = f"¥{int(float(cost_str))}" if cost_str else ""
        except (ValueError, TypeError):
            pass

        # 照片
        photos = poi.get("photos", [])
        photo_url = photos[0].get("url", "") if photos else ""

        # 距离
        distance_str = poi.get("distance", "0")
        try:
            distance = int(float(distance_str))
        except (ValueError, TypeError):
            distance = 0

        # ── 营业时间（deep_info.opentime）──
        deep_info = poi.get("deep_info", {}) or {}
        opentime = deep_info.get("opentime", "") or ""
        # 常见格式: "10:00-22:00", "周一至周五 09:00-18:00; 周六,周日 10:00-20:00"
        if not opentime:
            opentime = "未知"

        return {
            "shop_id": poi.get("id", ""),
            "name": poi.get("name", ""),
            "rating": rating,
            "lng": lng,
            "lat": lat,
            "category": category,
            "category_alias": category_alias,
            "human_needed": self._infer_human_needed(category),
            "phone": poi.get("tel", "") or "",
            "address": poi.get("address", ""),
            "signature_dishes": (
                [{"name": poi.get("name", ""), "price": avg_price, "image_url": photo_url}]
                if avg_price else []
            ),
            "top_comments": [],
            "distance": distance,
            "pname": poi.get("pname", ""),        # 省份
            "cityname": poi.get("cityname", ""),   # 城市
            "adname": poi.get("adname", ""),       # 区县
            "business_area": poi.get("business_area", ""),
            "photos": photos,
            "opentime": opentime,                   # 营业时间
        }

    def _map_amap_type_to_category(self, amap_type: str) -> tuple:
        """高德分类编码 → (内部品类码, 中文名)"""
        type_prefix = amap_type[:6] if len(amap_type) >= 6 else amap_type
        type_broad = amap_type[:3] + "000" if len(amap_type) >= 3 else amap_type

        mapping = {
            "071400": ("hair", "美发"),
            "080600": ("pet", "宠物店"),
            "050500": ("cafe", "咖啡馆"),
            "050300": ("cafe", "茶饮"),
            "080800": ("gym", "健身房"),
            "050100": ("hotpot" if "火锅" in amap_type else "restaurant", "餐厅"),
            "050200": ("japanese", "日料"),
            "060400": ("cinema", "影院"),
        }
        for code, (cat, alias) in mapping.items():
            if amap_type.startswith(code):
                return (cat, alias)

        # 大类兜底
        if amap_type.startswith("05"):
            return ("restaurant", "餐饮")
        if amap_type.startswith("06"):
            return ("shopping", "购物")
        if amap_type.startswith("07"):
            return ("hair", "生活服务")
        if amap_type.startswith("08"):
            return ("laundry", "生活服务")
        if amap_type.startswith("10"):
            return ("hotel", "酒店")
        if amap_type.startswith("11"):
            return ("scenic", "景点")
        if amap_type.startswith("14"):
            return ("scenic", "公共设施")
        return ("restaurant", "其他")

    def _infer_human_needed(self, category: str) -> bool:
        """根据品类推断是否需要人在场"""
        drop_and_go = {"pet", "laundry"}
        return category not in drop_and_go


# ======================================================================
# 模块级便捷函数（兼容旧 skill 调用方式）
# ======================================================================

_client: Optional[AmapPOIClient] = None

def _get_client() -> AmapPOIClient:
    global _client
    if _client is None:
        _client = AmapPOIClient()
    return _client

def search_poi(keywords: str, city: str = "北京", category: str = None) -> dict:
    """关键字搜索POI（兼容旧接口）"""
    return _get_client().search_poi(keywords=keywords, city=city, category=category)

def search_nearby(lng: float, lat: float, radius: int = 3000,
                  keywords: str = "", category: str = None,
                  min_rating: float = 0) -> dict:
    """周边搜索（兼容旧接口）"""
    return _get_client().search_nearby(
        lng=lng, lat=lat, radius=radius,
        keywords=keywords, category=category, min_rating=min_rating
    )

def get_poi_detail(poi_id: str) -> dict:
    """POI详情"""
    return _get_client().get_poi_detail(poi_id)

def fuzzy_search(keywords: str, city: str = "北京") -> list[dict]:
    """模糊搜索提示"""
    return _get_client().fuzzy_search(keywords, city)

def geocode(address: str, city: str = "北京") -> Optional[dict]:
    """地址 → 坐标（模块级便捷函数）"""
    return _get_client().geocode(address, city)

def reverse_geocode(lng: float, lat: float) -> dict:
    """坐标 → 地址（模块级便捷函数）"""
    return _get_client().reverse_geocode(lng, lat)


# ======================================================================
# 桥接函数：兼容 generic_poi_searcher.search_poi_matrix() 接口
# ======================================================================
import re as _price_re

def _extract_avg_price(shop: dict) -> float:
    """从 signature_dishes[0].price 提取 ¥数字"""
    dishes = shop.get("signature_dishes", [])
    if not dishes:
        return float("inf")
    prices = []
    for d in dishes:
        raw = d.get("price", "")
        m = _price_re.search(r"[¥￥]?\s*(\d+)", raw)
        if m:
            prices.append(float(m.group(1)))
    return sum(prices) / len(prices) if prices else float("inf")

def _price_in_level(avg: float, level: str) -> bool:
    if level == "经济":
        return avg < 50
    elif level == "中端":
        return 50 <= avg <= 200
    elif level == "高端":
        return avg > 200
    return True

def search_poi_matrix(
    center_coord: str,
    categories: list,
    radius_meters: int,
    min_rating: float,
    price_level: str = None,
    dietary_restrictions: list = None,
    keywords: str = None,
) -> dict:
    """
    通用空间商户检索器 —— 桥接到高德 API。
    接口完全兼容 generic_poi_searcher.search_poi_matrix()。

    参数:
        center_coord (str): 中心点坐标 "lat,lng"
        categories   (list): 品类数组 ["hair","pet","cafe"]
        radius_meters (int): 搜索半径(米)，上限 50000
        min_rating  (float): 最低评分过滤
        price_level  (str): 可选 "经济"/"中端"/"高端"
        dietary_restrictions (list): 忌口关键词列表
        keywords     (str): 模糊语义关键词，直接传给高德 API（如 "有变形金刚的游乐园"）

    返回:
        {"status":"SUCCESS","search_results":{cat1:[...],cat2:[...]}}
    """
    # 入参校验（与旧接口一致）
    if not isinstance(center_coord, str) or not center_coord:
        return {"status": "ERROR", "message": "center_coord 必须是非空字符串"}
    if not isinstance(categories, list) or len(categories) == 0:
        return {"status": "ERROR", "message": "categories 必须是非空数组"}
    if not isinstance(radius_meters, int) or radius_meters <= 0:
        return {"status": "ERROR", "message": "radius_meters 必须是正整数"}
    if not isinstance(min_rating, (int, float)):
        return {"status": "ERROR", "message": "min_rating 必须是数字"}
    if price_level is not None and price_level not in ("经济", "中端", "高端"):
        return {"status": "ERROR", "message": f"price_level 必须是 '经济'/'中端'/'高端'/None"}

    # 解析坐标
    parts = center_coord.strip().split(",")
    if len(parts) != 2:
        return {"status": "ERROR", "message": f"center_coord 格式必须为 'lat,lng'"}
    try:
        lat = float(parts[0].strip())
        lng = float(parts[1].strip())
    except ValueError:
        return {"status": "ERROR", "message": f"center_coord 中包含非法数值: {center_coord!r}"}

    # ── Demo 加速：预置数据命中 → 0 网络延迟 ──
    use_pre_cached = os.getenv("USE_PRE_CACHED", "true").lower() == "true"
    pre = {}
    pre_hits = []
    if use_pre_cached:
        pre = _get_pre_cached()
        pre_hits = [c for c in categories if c in pre]
        # 全部品类命中预置 → 直接返回
        if len(pre_hits) == len(categories):
            print(f"[pre-cached] ✅ 全部命中 {categories}，0 网络延迟", flush=True)
            # 补齐 coord 字段（预置数据只有 lat/lng）
            results = {}
            for c in categories:
                shops = []
                for s in pre[c]:
                    s = dict(s)
                    if "coord" not in s:
                        s["coord"] = f"{s.get('lat', 0)},{s.get('lng', 0)}"
                    shops.append(s)
                results[c] = shops
            return {"status": "SUCCESS", "search_results": results}
        # 部分命中 → 只搜未命中的品类
        if pre_hits:
            print(f"[pre-cached] ⚡ 命中 {pre_hits}，剩余 {[c for c in categories if c not in pre]} 走API", flush=True)
            categories = [c for c in categories if c not in pre]

    client = _get_client()
    result_map: dict = {cat: [] for cat in categories}
    # 合并预置数据到结果（补齐 coord 字段）
    for cat in pre_hits:
        shops = []
        for s in pre[cat]:
            s = dict(s)
            if "coord" not in s:
                s["coord"] = f"{s.get('lat', 0)},{s.get('lng', 0)}"
            shops.append(s)
        result_map[cat] = shops

    # ── 并行搜索：多个品类同时调高德 API，不等上一个返回 ──
    def _search_one_cat(cat: str):
        """搜索单个品类，失败返回空列表"""
        try:
            search_kw = keywords if keywords else ""
            resp = client.search_nearby(
                lng=lng, lat=lat,
                radius=min(radius_meters, 50000),
                keywords=search_kw,
                category=cat if not search_kw else None,
                min_rating=min_rating,
            )
            return (cat, resp.get("shops", []))
        except RuntimeError as e:
            print(f"[amap_poi] 品类 '{cat}' 搜索失败: {e}")
            return (cat, [])

    # 单品类直接搜，多品类并行
    if len(categories) == 1:
        results = [_search_one_cat(categories[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(len(categories), 5)) as executor:
            futures = {executor.submit(_search_one_cat, cat): cat for cat in categories}
            results = []
            for future in as_completed(futures):
                results.append(future.result())

    for cat, shops in results:
        for shop in shops:
            # 客户端侧价格过滤（高德 API 不返回价格，仅做兜底）
            if price_level:
                avg_price = _extract_avg_price(shop)
                if not _price_in_level(avg_price, price_level):
                    continue

            # 客户端侧忌口过滤
            if dietary_restrictions:
                sig_texts = [d.get("name", "") for d in shop.get("signature_dishes", [])]
                cmt_texts = [c.get("text", "") for c in shop.get("top_comments", [])]
                all_text = " ".join(sig_texts + cmt_texts)
                if any(kw in all_text for kw in dietary_restrictions):
                    continue

            entry = {
                "shop_id": shop["shop_id"],
                "name": shop["name"],
                "rating": shop["rating"],
                "lat": shop["lat"],
                "lng": shop["lng"],
                "coord": f"{shop['lat']},{shop['lng']}",
                "category": shop.get("category", cat),
                "category_alias": shop.get("category_alias", ""),
                "human_needed": shop.get("human_needed", True),
                "phone": shop.get("phone", ""),
                "address": shop.get("address", ""),
                "distance": shop.get("distance", 0),
                "signature_dishes": shop.get("signature_dishes", []),
                "top_comments": shop.get("top_comments", []),
                "opentime": shop.get("opentime", "未知"),
            }
            result_map[cat].append(entry)

    return {
        "status": "SUCCESS",
        "search_results": result_map,
    }


# ======================================================================
# 自检
# ======================================================================
if __name__ == "__main__":
    client = AmapPOIClient()
    print("=== 高德POI技能自检 ===\n")

    # 测试1: 关键字搜索
    print("1. 搜索'三里屯 理发':")
    r = client.search_poi("理发", city="北京", category="hair", offset=3)
    for s in r["shops"]:
        print(f"   {s['name']} | ⭐{s['rating']} | {s['distance']}m | {s['address']}")

    # 测试2: 周边搜索
    print("\n2. 三里屯太古里周边3km咖啡馆:")
    r = client.search_nearby(116.455, 39.932, radius=3000, category="cafe", min_rating=4.0)
    for s in r["shops"][:5]:
        print(f"   {s['name']} | ⭐{s['rating']} | {s['distance']}m")

    # 测试3: 模糊搜索
    print("\n3. 输入提示'变形金刚':")
    tips = client.fuzzy_search("变形金刚", city="北京")
    for t in tips[:3]:
        print(f"   {t['name']} — {t['address']}")

    print("\n✅ 自检完成")
