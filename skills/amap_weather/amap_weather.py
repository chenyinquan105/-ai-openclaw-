"""
amap_weather.py —— 高德地图实时天气技能
==========================================
替代 weather_extractor 的 Mock 伪随机天气，
直接调用高德地图天气 API 获取真实实时天气数据。

物理契约:
  get_real_time_weather(adcode) -> dict

API Key 来源: 环境变量 AMAP_API_KEY
"""

import os
import time
import json
import hashlib
from pathlib import Path
from typing import Optional
import requests


# ======================================================================
# 高德天气文字 -> 项目内部天气枚举映射
# ======================================================================
_WEATHER_TEXT_MAP = {
    "晴":   {"en": "sunny",        "outdoor": True,  "walking_penalty": 1.0},
    "少云": {"en": "sunny",        "outdoor": True,  "walking_penalty": 1.0},
    "多云": {"en": "cloudy",       "outdoor": True,  "walking_penalty": 1.0},
    "阴":   {"en": "overcast",     "outdoor": True,  "walking_penalty": 0.9},
    "小雨": {"en": "light_rain",   "outdoor": False, "walking_penalty": 0.8},
    "阵雨": {"en": "light_rain",   "outdoor": False, "walking_penalty": 0.8},
    "雷阵雨": {"en": "light_rain", "outdoor": False, "walking_penalty": 0.7},
    "中雨": {"en": "moderate_rain","outdoor": False, "walking_penalty": 0.6},
    "大雨": {"en": "heavy_rain",   "outdoor": False, "walking_penalty": 0.4},
    "暴雨": {"en": "storm",        "outdoor": False, "walking_penalty": 0.2, "alert": "暴雨预警！"},
    "大暴雨": {"en": "storm",      "outdoor": False, "walking_penalty": 0.1, "alert": "大暴雨预警！"},
    "小雪": {"en": "light_snow",   "outdoor": True,  "walking_penalty": 0.7},
    "中雪": {"en": "heavy_snow",   "outdoor": False, "walking_penalty": 0.4},
    "大雪": {"en": "heavy_snow",   "outdoor": False, "walking_penalty": 0.3},
    "暴雪": {"en": "heavy_snow",   "outdoor": False, "walking_penalty": 0.2, "alert": "暴雪预警！"},
    "雾":   {"en": "overcast",     "outdoor": True,  "walking_penalty": 0.9},
    "霾":   {"en": "overcast",     "outdoor": False, "walking_penalty": 0.8},
    "扬沙": {"en": "overcast",     "outdoor": False, "walking_penalty": 0.7},
    "浮尘": {"en": "overcast",     "outdoor": False, "walking_penalty": 0.8},
}

# 默认映射（兜底）
_DEFAULT_WEATHER_META = {"en": "cloudy", "outdoor": True, "walking_penalty": 1.0}


# ======================================================================
# AmapWeatherClient
# ======================================================================
class AmapWeatherClient:
    """高德天气 API 客户端（复用 amap_poi 的缓存+限流模式）"""

    BASE_URL = "https://restapi.amap.com/v3"

    def __init__(self, api_key: str = None, cache_ttl: int = 1800):
        """
        参数:
            api_key: 高德 API Key，默认读取环境变量 AMAP_API_KEY
            cache_ttl: 缓存有效期（秒），天气默认 30 分钟
        """
        self.api_key = api_key or os.getenv("AMAP_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "AMAP_API_KEY 未设置。请去 https://lbs.amap.com/ 注册获取 Key，"
                "然后写入 .env 文件: AMAP_API_KEY=你的key"
            )
        self.cache_dir = Path(__file__).parent.parent.parent / "cache" / "amap"
        self.cache_ttl = cache_ttl
        self._last_request = 0

    # ------------------------------------------------------------------
    # 内部工具（与 amap_poi.py 完全一致）
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
    # 天气 API
    # ------------------------------------------------------------------

    def get_real_time_weather(self, adcode: str = "110000") -> dict:
        """
        获取指定城市的实时天气。

        参数:
            adcode: 城市编码，如 110000=北京，310000=上海

        返回:
            dict: 与 weather_extractor 兼容的天气数据结构
        """
        data = self._call("weather/weatherInfo", {
            "city": adcode,
            "extensions": "base"
        })

        lives = data.get("lives", [])
        if not lives:
            raise RuntimeError(f"无天气数据: adcode={adcode}")

        return self._normalize_live(lives[0])

    def _normalize_live(self, live: dict) -> dict:
        """
        将高德实时天气 liveness 数据转换为项目兼容格式。

        高德 live 字段:
            province, city, adcode, weather, temperature,
            winddirection, windpower, humidity, reporttime
        """
        weather_text = live.get("weather", "多云")
        meta = _WEATHER_TEXT_MAP.get(weather_text, _DEFAULT_WEATHER_META)

        temp_c = int(live.get("temperature", 20))
        humidity = int(live.get("humidity", 50))
        windpower = live.get("windpower", "≤3")

        # 风力文字转数值（粗略估算）
        wind_kmh = self._estimate_wind(windpower)

        # 构建活动建议
        outdoor_friendly = meta["outdoor"]
        walking_penalty = meta.get("walking_penalty", 1.0)

        if "暴" in weather_text:
            reason = f"{weather_text}天气，户外活动存在安全风险"
            suggestions = ["建议取消户外活动", "如必须出行请选择打车", "步行路段需格外小心"]
            transport_advice = "极端天气，强烈建议打车出行，避免步行"
            traffic_risk = "高"
        elif "雨" in weather_text:
            reason = f"{weather_text}将持续，路面湿滑"
            suggestions = ["建议带伞", "户外活动建议改期", "步行路段注意防滑"]
            transport_advice = "雨天路滑，建议减少步行路段"
            traffic_risk = "中等" if walking_penalty <= 0.6 else "低"
        elif "雪" in weather_text:
            reason = "降雪天气，路面可能结冰"
            suggestions = ["注意保暖", "步行注意防滑", "建议公共交通出行"]
            transport_advice = "雪天路滑，建议乘地铁出行"
            traffic_risk = "中等" if walking_penalty > 0.5 else "高"
        elif weather_text == "晴":
            reason = "天气晴朗，适合户外活动"
            suggestions = ["注意防晒", "适合户外运动", "建议步行或骑行"]
            transport_advice = "天气晴好，步行舒适"
            traffic_risk = "低"
        else:
            reason = f"当前{weather_text}，适合外出活动"
            suggestions = ["适合外出", "注意天气变化"]
            transport_advice = "出行正常"
            traffic_risk = "低"

        alternative = "推荐室内活动：电影院/商场/咖啡馆" if not outdoor_friendly else None

        return {
            "status": "SUCCESS",
            "weather": {
                "condition": weather_text,
                "condition_en": meta["en"],
                "temperature_c": temp_c,
                "humidity": humidity,
                "wind_kmh": wind_kmh,
                "uv_index": 0,  # 高德 base 接口不含 UV 指数
                "hourly": [],   # base 接口不含逐小时预报
                "alert": meta.get("alert"),
            },
            "activity_advice": {
                "outdoor": "推荐" if outdoor_friendly else "不推荐",
                "reason": reason,
                "suggestions": suggestions,
                "alternative": alternative,
            },
            "transport_impact": {
                "walking_penalty": walking_penalty,
                "traffic_risk": traffic_risk,
                "advice": transport_advice,
            },
            "source": "amap_realtime",
        }

    @staticmethod
    def _estimate_wind(windpower: str) -> int:
        """高德风力文字 -> 风速 km/h 估算"""
        mapping = {
            "≤3": 10,
            "1": 5, "2": 10, "3": 18,
            "4": 28, "5": 38, "6": 50,
            "7": 62, "8": 75, "9": 88,
            "10": 105, "11": 118, "12": 135,
        }
        return mapping.get(windpower.strip(), 10)


# ======================================================================
# 模块级单例
# ======================================================================
_client: Optional[AmapWeatherClient] = None


def _get_client() -> AmapWeatherClient:
    global _client
    if _client is None:
        _client = AmapWeatherClient()
    return _client


def get_real_time_weather(adcode: str = "110000") -> dict:
    """模块级便捷函数: 获取指定城市实时天气"""
    return _get_client().get_real_time_weather(adcode=adcode)
