"""
generic_poi_searcher.py —— 通用空间商户检索器
=================================================
物理契约 (API Contract):
  search_poi_matrix(center_coord: str, categories: list, radius_meters: int, min_rating: float) -> dict

本模块运行在安全隔离模式：使用本地 Mock 字典模拟高德/美团 POI 数据。
禁止调用任何真实三方 API。
"""

import math
import json
from typing import List

# ======================================================================
# Mock 数据库 —— 三里屯虚拟商户字典
# 硬编码 10+ 家不同品类、不同评分、不同经纬度的商户
# ======================================================================
_MOCK_POI_DB: List[dict] = [
    # ===================================================================
    # 距离梯度 ~200m~500m（三里屯太古里核心区）
    # ===================================================================
    # --- hair (美发) 需人在场 ---
    {"shop_id": "shop_hair_01", "name": "沙宣三里屯店", "rating": 4.8, "lat": 39.934, "lng": 116.453, "category": "hair", "category_alias": "美发", "human_needed": True, "phone": "010-6416-8888", "address": "三里屯太古里南区S1-12", "signature_dishes": [{"name": "沙宣剪发套餐", "price": "¥268", "image_url": ""}, {"name": "造型烫发", "price": "¥588", "image_url": ""}], "top_comments": [{"user": "Lisa", "text": "剪发很细致，环境不错", "rating": 4.8}, {"user": "Tony老师", "text": "造型师专业，推荐", "rating": 4.7}]},
    {"shop_id": "shop_hair_02", "name": "托尼形象设计", "rating": 4.6, "lat": 39.935, "lng": 116.450, "category": "hair", "category_alias": "美发", "human_needed": True, "phone": "010-6417-1234", "address": "三里屯路33号3层3018", "signature_dishes": [{"name": "男士精剪", "price": "¥158", "image_url": ""}, {"name": "日系染发", "price": "¥398", "image_url": ""}], "top_comments": [{"user": "王大锤", "text": "性价比很高", "rating": 4.6}, {"user": "小美", "text": "染发颜色很正", "rating": 4.5}]},
    # --- pet (宠物) 可丢下后台做 ---
    {"shop_id": "shop_pet_01", "name": "酷迪宠物三里屯店", "rating": 4.9, "lat": 39.933, "lng": 116.454, "category": "pet", "category_alias": "宠物店", "human_needed": False, "phone": "010-6415-6666", "address": "三里屯SOHO 5号商场B1", "signature_dishes": [{"name": "宠物精洗", "price": "¥88", "image_url": ""}, {"name": "宠物美容", "price": "¥198", "image_url": ""}], "top_comments": [{"user": "狗爸", "text": "洗得很干净，服务好", "rating": 4.9}, {"user": "猫奴一号", "text": "猫咪洗澡很温柔", "rating": 4.8}]},
    {"shop_id": "shop_pet_02", "name": "宠物家朝阳店", "rating": 4.5, "lat": 39.936, "lng": 116.448, "category": "pet", "category_alias": "宠物店", "human_needed": False, "phone": "010-6415-7777", "address": "朝阳北路甲2号", "signature_dishes": [{"name": "宠物基础洗护", "price": "¥68", "image_url": ""}], "top_comments": [{"user": "柯基主人", "text": "价格实惠，服务热情", "rating": 4.5}]},
    # --- cafe (咖啡/水吧) 需人在场 ---
    {"shop_id": "shop_cafe_01", "name": "星巴克臻选三里屯", "rating": 4.4, "lat": 39.932, "lng": 116.455, "category": "cafe", "category_alias": "咖啡馆", "human_needed": True, "phone": "010-6418-3333", "address": "三里屯太古里南区1层", "signature_dishes": [{"name": "拿铁咖啡", "price": "¥38", "image_url": ""}, {"name": "抹茶星冰乐", "price": "¥42", "image_url": ""}], "top_comments": [{"user": "咖啡控", "text": "环境很好，适合办公", "rating": 4.5}, {"user": "周末逛逛", "text": "位置好找，咖啡不错", "rating": 4.4}]},
    {"shop_id": "shop_cafe_05", "name": "瑞幸咖啡三里屯SOHO", "rating": 4.2, "lat": 39.933, "lng": 116.452, "category": "cafe", "category_alias": "咖啡馆", "human_needed": True, "phone": "010-6418-0005", "address": "三里屯SOHO 1号楼1层", "signature_dishes": [{"name": "生椰拿铁", "price": "¥22", "image_url": ""}, {"name": "厚乳拿铁", "price": "¥24", "image_url": ""}], "top_comments": [{"user": "上班族", "text": "性价比高，出杯快", "rating": 4.3}, {"user": "小困", "text": "推荐生椰拿铁", "rating": 4.2}]},
    {"shop_id": "shop_cafe_06", "name": "蜜雪冰城三里屯店", "rating": 4.0, "lat": 39.934, "lng": 116.451, "category": "cafe", "category_alias": "水吧", "human_needed": True, "phone": "010-6418-0006", "address": "三里屯路甲9号", "signature_dishes": [{"name": "柠檬水", "price": "¥6", "image_url": ""}, {"name": "冰淇淋", "price": "¥3", "image_url": ""}], "top_comments": [{"user": "学生党", "text": "便宜又好喝", "rating": 4.2}, {"user": "冬天也要吃", "text": "冰淇淋yyds", "rating": 4.0}]},
    {"shop_id": "shop_cafe_07", "name": "茶百道三里屯店", "rating": 4.1, "lat": 39.935, "lng": 116.450, "category": "cafe", "category_alias": "水吧", "human_needed": True, "phone": "010-6418-0007", "address": "三里屯路35号", "signature_dishes": [{"name": "杨枝甘露", "price": "¥24", "image_url": ""}, {"name": "豆乳玉麒麟", "price": "¥18", "image_url": ""}], "top_comments": [{"user": "奶茶控", "text": "杨枝甘露yyds", "rating": 4.3}, {"user": "小甜", "text": "豆乳系列好喝", "rating": 4.1}]},
    # --- gym (健身) 需人在场 ---
    {"shop_id": "shop_gym_01", "name": "超级猩猩三里屯", "rating": 4.8, "lat": 39.932, "lng": 116.446, "category": "gym", "category_alias": "健身房", "human_needed": True, "phone": "010-6411-7777", "address": "三里屯太古里北区B1", "signature_dishes": [{"name": "单次团课体验", "price": "¥99", "image_url": ""}, {"name": "私教体验课", "price": "¥299", "image_url": ""}], "top_comments": [{"user": "健身达人", "text": "课程丰富，教练专业", "rating": 4.9}, {"user": "小白一枚", "text": "第一次去体验很好", "rating": 4.7}]},

    # ===================================================================
    # 距离梯度 ~500m~900m（工体北路 / 新东路沿线）
    # ===================================================================
    # --- hair (美发) 需人在场 ---
    {"shop_id": "shop_hair_03", "name": "丝颂烫染专门店", "rating": 4.0, "lat": 39.931, "lng": 116.452, "category": "hair", "category_alias": "美发", "human_needed": True, "phone": "010-6417-0003", "address": "工体北路甲5号", "signature_dishes": [{"name": "烫发套餐", "price": "¥388", "image_url": ""}], "top_comments": [{"user": "烫发小妹", "text": "烫发效果不错", "rating": 4.1}]},
    {"shop_id": "shop_hair_04", "name": "木北造型工体店", "rating": 4.3, "lat": 39.939, "lng": 116.446, "category": "hair", "category_alias": "美发", "human_needed": True, "phone": "010-6417-0004", "address": "工体北路8号", "signature_dishes": [{"name": "精剪+造型", "price": "¥198", "image_url": ""}], "top_comments": [{"user": "职场丽人", "text": "服务好，效果满意", "rating": 4.4}]},
    # --- pet (宠物) 可丢下后台做 ---
    {"shop_id": "shop_pet_03", "name": "爱派宠物生活馆", "rating": 4.0, "lat": 39.930, "lng": 116.458, "category": "pet", "category_alias": "宠物店", "human_needed": False, "phone": "010-6415-0003", "address": "新东路12号", "signature_dishes": [{"name": "宠物SPA", "price": "¥158", "image_url": ""}], "top_comments": [{"user": "布偶妈妈", "text": "猫咪做SPA很乖", "rating": 4.2}]},
    # --- restaurant (餐饮) 需人在场 ---
    {"shop_id": "shop_rest_01", "name": "海底捞三里屯店", "rating": 4.7, "lat": 39.936, "lng": 116.449, "category": "hotpot", "category_alias": "火锅", "human_needed": True, "phone": "010-5819-8888", "address": "工体北路甲2号", "signature_dishes": [{"name": "经典麻辣锅底", "price": "¥88", "image_url": ""}, {"name": "招牌虾滑", "price": "¥58", "image_url": ""}, {"name": "毛肚", "price": "¥68", "image_url": ""}], "top_comments": [{"user": "火锅达人", "text": "海底捞服务还是一如既往的好", "rating": 4.9}, {"user": "朋友聚餐", "text": "排队久但值得", "rating": 4.6}]},
    {"shop_id": "shop_rest_02", "name": "鼎泰丰太古里店", "rating": 4.5, "lat": 39.937, "lng": 116.451, "category": "restaurant", "category_alias": "餐厅", "human_needed": True, "phone": "010-5819-0002", "address": "三里屯太古里北区B1", "signature_dishes": [{"name": "小笼包", "price": "¥58", "image_url": ""}, {"name": "红油抄手", "price": "¥45", "image_url": ""}], "top_comments": [{"user": "小笼包控", "text": "皮薄馅大，汤汁鲜美", "rating": 4.6}, {"user": "家庭聚餐", "text": "老少皆宜的选择", "rating": 4.5}]},
    {"shop_id": "shop_rest_03", "name": "麦当劳三里屯站", "rating": 3.8, "lat": 39.938, "lng": 116.443, "category": "restaurant", "category_alias": "餐厅", "human_needed": True, "phone": "010-5819-0003", "address": "工体北路1号", "signature_dishes": [{"name": "巨无霸套餐", "price": "¥38", "image_url": ""}, {"name": "麦辣鸡腿堡", "price": "¥22", "image_url": ""}], "top_comments": [{"user": "快餐党", "text": "方便快捷，标准出品", "rating": 3.9}]},
    # --- cafe (咖啡) 需人在场 ---
    {"shop_id": "shop_cafe_02", "name": "瑞幸咖啡三里屯店", "rating": 4.1, "lat": 39.935, "lng": 116.447, "category": "cafe", "category_alias": "咖啡馆", "human_needed": True, "phone": "010-6418-0002", "address": "工体北路甲3号", "signature_dishes": [{"name": "生椰拿铁", "price": "¥22", "image_url": ""}], "top_comments": [{"user": "打工人", "text": "每天一杯", "rating": 4.2}]},
    # --- gym (健身) 需人在场 ---
    {"shop_id": "shop_gym_02", "name": "乐刻健身工体店", "rating": 4.2, "lat": 39.940, "lng": 116.441, "category": "gym", "category_alias": "健身房", "human_needed": True, "phone": "010-6411-0002", "address": "工体北路11号", "signature_dishes": [{"name": "月卡体验", "price": "¥199", "image_url": ""}], "top_comments": [{"user": "自律的我", "text": "24小时开放，方便", "rating": 4.3}]},

    # ===================================================================
    # 距离梯度 ~1000m（东四十条桥 / 工体北路北段）
    # ===================================================================
    {"shop_id": "shop_hair_05", "name": "东田造型东四十条店", "rating": 4.5, "lat": 39.942, "lng": 116.444, "category": "hair", "category_alias": "美发", "human_needed": True, "phone": "010-6417-0005", "address": "东四十条甲33号", "signature_dishes": [{"name": "明星造型设计", "price": "¥688", "image_url": ""}], "top_comments": [{"user": "时尚博主", "text": "明星造型师果然不一样", "rating": 4.6}]},
    {"shop_id": "shop_rest_04", "name": "金鼎轩东直门店", "rating": 4.3, "lat": 39.943, "lng": 116.441, "category": "restaurant", "category_alias": "餐厅", "human_needed": True, "phone": "010-5819-0004", "address": "东直门外大街2号", "signature_dishes": [{"name": "虾饺皇", "price": "¥38", "image_url": ""}, {"name": "烧卖", "price": "¥32", "image_url": ""}], "top_comments": [{"user": "早茶爱好者", "text": "广式早茶正宗", "rating": 4.4}]},
    {"shop_id": "shop_japanese_01", "name": "鮨然日料居酒屋", "rating": 4.7, "lat": 39.942, "lng": 116.442, "category": "japanese", "category_alias": "日料", "human_needed": True, "phone": "010-5819-0005", "address": "东四十条甲55号", "signature_dishes": [{"name": "刺身拼盘", "price": "¥188", "image_url": ""}, {"name": "鳗鱼饭", "price": "¥88", "image_url": ""}], "top_comments": [{"user": "日料控", "text": "食材新鲜，口感一流", "rating": 4.8}, {"user": "深夜食堂", "text": "氛围很好", "rating": 4.6}]},
    {"shop_id": "shop_hotpot_01", "name": "楠火锅工体店", "rating": 4.6, "lat": 39.941, "lng": 116.443, "category": "hotpot", "category_alias": "火锅", "human_needed": True, "phone": "010-6419-2222", "address": "工体北路4号院", "signature_dishes": [{"name": "卤校长招牌锅", "price": "¥98", "image_url": ""}, {"name": "卤味拼盘", "price": "¥68", "image_url": ""}], "top_comments": [{"user": "辣妹子", "text": "重庆味道，很正宗", "rating": 4.8}, {"user": "吃货一枚", "text": "卤味一绝", "rating": 4.6}]},
    # --- laundry (干洗) 可丢下后台做 ---
    {"shop_id": "shop_laundry_01", "name": "福奈特洗衣东四十条店", "rating": 4.5, "lat": 39.941, "lng": 116.442, "category": "laundry", "category_alias": "干洗店", "human_needed": False, "phone": "010-6412-9999", "address": "东四十条甲22号", "signature_dishes": [{"name": "干洗西服套装", "price": "¥59", "image_url": ""}, {"name": "羽绒服清洗", "price": "¥79", "image_url": ""}], "top_comments": [{"user": "白领小张", "text": "洗得干净，取送方便", "rating": 4.5}, {"user": "王姐", "text": "价格合理", "rating": 4.4}]},

    # ===================================================================
    # 距离梯度 ~2000m（东直门外大街 / 东直门地铁站周边）
    # ===================================================================
    {"shop_id": "shop_pet_04", "name": "宠物家东直门店", "rating": 4.2, "lat": 39.948, "lng": 116.432, "category": "pet", "category_alias": "宠物店", "human_needed": False, "phone": "010-6415-0004", "address": "东直门外大街18号", "signature_dishes": [{"name": "宠物洗澡套餐", "price": "¥78", "image_url": ""}], "top_comments": [{"user": "金毛主人", "text": "大狗也能洗，不错", "rating": 4.3}]},
    {"shop_id": "shop_cafe_03", "name": "皮爷咖啡东直门店", "rating": 4.5, "lat": 39.947, "lng": 116.431, "category": "cafe", "category_alias": "咖啡馆", "human_needed": True, "phone": "010-6418-0003", "address": "东直门外大街6号", "signature_dishes": [{"name": "拿铁", "price": "¥35", "image_url": ""}, {"name": "手冲咖啡", "price": "¥48", "image_url": ""}], "top_comments": [{"user": "咖啡发烧友", "text": "手冲一流", "rating": 4.6}]},
    {"shop_id": "shop_gym_03", "name": "乐刻健身东直门店", "rating": 4.0, "lat": 39.946, "lng": 116.433, "category": "gym", "category_alias": "健身房", "human_needed": True, "phone": "010-6411-0003", "address": "东直门外大街12号", "signature_dishes": [{"name": "周卡体验", "price": "¥69", "image_url": ""}], "top_comments": [{"user": "试试看", "text": "周卡划算", "rating": 4.1}]},
    {"shop_id": "shop_japanese_02", "name": "鸟贵族烧鸟三里屯店", "rating": 4.4, "lat": 39.948, "lng": 116.430, "category": "japanese", "category_alias": "日料", "human_needed": True, "phone": "010-5819-0006", "address": "三里屯路甲8号", "signature_dishes": [{"name": "烧鸟拼盘", "price": "¥68", "image_url": ""}, {"name": "清酒", "price": "¥38", "image_url": ""}], "top_comments": [{"user": "酒鬼", "text": "烧鸟配清酒绝了", "rating": 4.5}]},
    {"shop_id": "shop_cinema_01", "name": "保利国际影城朝阳", "rating": 4.6, "lat": 39.947, "lng": 116.434, "category": "cinema", "category_alias": "电影院", "human_needed": True, "phone": "010-6500-1111", "address": "朝阳门外大街8号", "signature_dishes": [{"name": "IMAX电影票", "price": "¥129", "image_url": ""}, {"name": "双人爆米花套餐", "price": "¥45", "image_url": ""}], "top_comments": [{"user": "影迷小李", "text": "IMAX效果震撼", "rating": 4.7}, {"user": "周末休闲", "text": "卫生干净，座椅舒服", "rating": 4.5}]},

    # ===================================================================
    # 距离梯度 ~3000m（雍和宫 / 安定门 / 簋街周边）
    # ===================================================================
    {"shop_id": "shop_rest_05", "name": "花家怡园簋街店", "rating": 4.6, "lat": 39.951, "lng": 116.428, "category": "restaurant", "category_alias": "餐厅", "human_needed": True, "phone": "010-5819-0007", "address": "簋街甲13号", "signature_dishes": [{"name": "宫保虾球", "price": "¥68", "image_url": ""}, {"name": "鱼头泡饼", "price": "¥88", "image_url": ""}], "top_comments": [{"user": "老北京", "text": "宫保虾球必点", "rating": 4.7}]},
    {"shop_id": "shop_cafe_04", "name": "Manner咖啡雍和宫", "rating": 4.3, "lat": 39.950, "lng": 116.426, "category": "cafe", "category_alias": "咖啡馆", "human_needed": True, "phone": "010-6418-0004", "address": "雍和宫大街15号", "signature_dishes": [{"name": "燕麦拿铁", "price": "¥28", "image_url": ""}, {"name": "flat white", "price": "¥30", "image_url": ""}], "top_comments": [{"user": "咖啡自由", "text": "自带杯减5元，环保", "rating": 4.4}]},
    {"shop_id": "shop_hotpot_02", "name": "卤校长老火锅三里屯店", "rating": 4.5, "lat": 39.950, "lng": 116.429, "category": "hotpot", "category_alias": "火锅", "human_needed": True, "phone": "010-6419-0002", "address": "簋街甲5号", "signature_dishes": [{"name": "卤校长麻辣锅", "price": "¥88", "image_url": ""}, {"name": "校长鲜鸭血", "price": "¥18", "image_url": ""}], "top_comments": [{"user": "火锅重度", "text": "辣锅够味", "rating": 4.6}]},
    {"shop_id": "shop_cinema_02", "name": "万达影城CBD店", "rating": 4.7, "lat": 39.951, "lng": 116.432, "category": "cinema", "category_alias": "电影院", "human_needed": True, "phone": "010-6500-0002", "address": "朝阳区建国路87号", "signature_dishes": [{"name": "MX4D电影票", "price": "¥149", "image_url": ""}], "top_comments": [{"user": "电影迷", "text": "4D体验很新奇", "rating": 4.7}]},
    # --- laundry (干洗) 可丢下后台做 ---
    {"shop_id": "shop_laundry_02", "name": "象王洗染东直门店", "rating": 4.3, "lat": 39.949, "lng": 116.431, "category": "laundry", "category_alias": "干洗店", "human_needed": False, "phone": "010-6412-0002", "address": "东直门外大街20号", "signature_dishes": [{"name": "衣物洗护套餐", "price": "¥49", "image_url": ""}], "top_comments": [{"user": "懒人福音", "text": "上门取送太方便了", "rating": 4.4}]},
    {"shop_id": "shop_hair_06", "name": "文峰美发雍和宫店", "rating": 3.9, "lat": 39.951, "lng": 116.426, "category": "hair", "category_alias": "美发", "human_needed": True, "phone": "010-6417-0006", "address": "雍和宫大街22号", "signature_dishes": [{"name": "基础洗剪吹", "price": "¥68", "image_url": ""}], "top_comments": [{"user": "普通顾客", "text": "基础服务还行", "rating": 3.8}]}]


# ======================================================================
# 工具函数
# ======================================================================

def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """返回两点之间的球面距离（单位：公里）。"""
    R = 6371.0  # 地球平均半径，km
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def _parse_coord(center_coord: str) -> tuple:
    """解析 "lat,lng" 字符串为 (lat, lng) 浮点数元组。"""
    parts = center_coord.strip().split(",")
    if len(parts) != 2:
        raise ValueError(f"center_coord 格式必须为 'lat,lng'，实际收到: {center_coord!r}")
    try:
        lat = float(parts[0].strip())
        lng = float(parts[1].strip())
    except ValueError:
        raise ValueError(f"center_coord 中包含非法数值: {center_coord!r}")
    return lat, lng


# ======================================================================
# 核心主函数（物理契约入口）
# ======================================================================

def search_poi_matrix(
    center_coord: str,
    categories: List[str],
    radius_meters: int,
    min_rating: float,
    price_level: str = None,
    dietary_restrictions: List[str] = None,
) -> dict:
    """
    通用空间商户检索器。

    参数:
        center_coord (str): 中心点坐标 "lat,lng"，如 "39.93,116.45"
        categories   (list): 品类字符串数组，如 ["hair", "pet"]
        radius_meters (int): 搜索半径（米）
        min_rating  (float): 最低评分过滤
        price_level  (str): 消费预算过滤，可选 "经济"/"中端"/"高端", None=不过滤
            - 经济: signature_dishes[0].price 平均 < 50
            - 中端: 50-200
            - 高端: > 200
        dietary_restrictions (list): 忌口关键词列表，如 ["羊肉","内脏","香菜"]。
            会过滤掉 signature_dishes 和 top_comments 中包含忌口词的店铺。

    返回:
        dict: 符合物理契约的出参字典，可直接 json.dumps。
    """
    # ---------- 入参合法性校验 ----------
    if not isinstance(center_coord, str) or not center_coord:
        return {"status": "ERROR", "message": "center_coord 必须是非空字符串"}
    if not isinstance(categories, list) or len(categories) == 0:
        return {"status": "ERROR", "message": "categories 必须是非空数组"}
    if not isinstance(radius_meters, int) or radius_meters <= 0:
        return {"status": "ERROR", "message": "radius_meters 必须是正整数"}
    if not isinstance(min_rating, (int, float)):
        return {"status": "ERROR", "message": "min_rating 必须是数字"}
    if price_level is not None and price_level not in ("经济", "中端", "高端"):
        return {"status": "ERROR", "message": f"price_level 必须是 '经济'/'中端'/'高端'/None，实际收到: {price_level!r}"}

    try:
        center_lat, center_lng = _parse_coord(center_coord)
    except ValueError as e:
        return {"status": "ERROR", "message": str(e)}

    # ---------- 价格水平判定工具 ----------
    import re as _price_re

    def _extract_avg_price(shop: dict) -> float:
        """从 signature_dishes[0].price 提取 ¥数字，无法提取返回无穷大"""
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

    # ---------- 核心过滤 ----------
    result_map: dict = {cat: [] for cat in categories}

    for shop in _MOCK_POI_DB:
        # 1) 品类过滤（支持 category 英文编码 + category_alias 中文别名双匹配）
        shop_cats = [shop["category"], shop.get("category_alias", "")]
        matched_cat = next((c for c in categories if c in shop_cats), None)
        if matched_cat is None:
            continue

        # 2) 评分过滤
        if shop["rating"] < min_rating:
            continue

        # 3) 距离过滤（球面 Haversine 公式）
        dist_km = _haversine_km(center_lat, center_lng, shop["lat"], shop["lng"])
        dist_m = dist_km * 1000.0
        if dist_m > radius_meters:
            continue

        # 4) 价格水平过滤
        if price_level:
            avg_price = _extract_avg_price(shop)
            if not _price_in_level(avg_price, price_level):
                continue

        # 5) 膳食忌口过滤
        if dietary_restrictions:
            # 检查 signature_dishes 中的菜品名
            sig_texts = [d.get("name", "") for d in shop.get("signature_dishes", [])]
            # 检查 top_comments 中的评论文本
            cmt_texts = [c.get("text", "") for c in shop.get("top_comments", [])]
            all_text = " ".join(sig_texts + cmt_texts)
            if any(kw in all_text for kw in dietary_restrictions):
                continue

        # 构造出参条目
        entry = {
            "shop_id": shop["shop_id"],
            "name": shop["name"],
            "rating": shop["rating"],
            "lat": shop["lat"],
            "lng": shop["lng"],
            "coord": f"{shop['lat']},{shop['lng']}",
            "category": shop["category"],
            "category_alias": shop.get("category_alias", ""),
            "human_needed": shop.get("human_needed", True),
            "phone": shop.get("phone", ""),
            "address": shop.get("address", ""),
            "signature_dishes": shop.get("signature_dishes", []),
            "top_comments": shop.get("top_comments", []),
        }
        result_map[matched_cat].append(entry)

    return {
        "status": "SUCCESS",
        "search_results": result_map,
    }


# ======================================================================
# __main__ 简单自测入口（可选）
# ======================================================================
if __name__ == "__main__":
    demo_result = search_poi_matrix(
        center_coord="39.93,116.45",
        categories=["hair", "pet", "cafe"],
        radius_meters=1500,
        min_rating=4.0,
    )
    print(json.dumps(demo_result, ensure_ascii=False, indent=2))
