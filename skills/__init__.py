"""
skills/__init__.py —— OpenClaw Skill 注册表与框架集成适配层
==============================================================
赛事要求：基于 OpenClaw 框架开发本地生活服务技能。
本模块将原有的独立 Flask 应用改造为真正的 OpenClaw 集成，
使每个 Skill 通过标准接口注册到 OpenClaw 的 skill 系统。

架构设计：
- 每个 Skill 暴露 register() 函数返回 SkillManifest
- OpenClawToolBridge 将 Skill 函数包装为标准 OpenClaw tool 描述
- 支持 sessions_spawn 异步分发（长链路排程）和同步内联调用
"""

import json
from typing import Dict, Any, Callable, List, Optional


class SkillManifest:
    """Skill 元数据描述符，对齐 OpenClaw skill 注册规范"""
    def __init__(
        self,
        skill_id: str,
        name: str,
        description: str,
        entry_fn: Callable,
        input_schema: dict,
        output_schema: dict,
        triggers: List[str] = None,
        dependencies: List[str] = None,
        is_long_running: bool = False,
    ):
        self.skill_id = skill_id
        self.name = name
        self.description = description
        self.entry_fn = entry_fn
        self.input_schema = input_schema
        self.output_schema = output_schema
        self.triggers = triggers or []
        self.dependencies = dependencies or []
        self.is_long_running = is_long_running

    def to_openclaw_tool(self) -> dict:
        """将 Skill 描述转换为 OpenClaw function tool 格式"""
        return {
            "type": "function",
            "function": {
                "name": self.skill_id,
                "description": self.description,
                "parameters": self.input_schema,
            }
        }

    def to_dict(self) -> dict:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "triggers": self.triggers,
            "dependencies": self.dependencies,
            "is_long_running": self.is_long_running,
        }


class OpenClawToolBridge:
    """
    OpenClaw 框架工具桥接器。
    将原 Flask 中的 /api/* 端点调用方式改造为 OpenClaw tool 调用方式，
    使 Agent 可以通过 tool_call 直接调用各 Skill。
    """
    def __init__(self):
        self._registry: Dict[str, SkillManifest] = {}

    def register(self, manifest: SkillManifest):
        """注册一个 Skill 到全局桥接器"""
        self._registry[manifest.skill_id] = manifest

    def get_tool(self, skill_id: str) -> Optional[SkillManifest]:
        return self._registry.get(skill_id)

    def list_tools(self) -> List[dict]:
        """返回所有已注册 Skill 的 OpenClaw tool 描述"""
        return [m.to_openclaw_tool() for m in self._registry.values()]

    def list_skills(self) -> List[dict]:
        """返回所有已注册 Skill 的元数据摘要"""
        return [m.to_dict() for m in self._registry.values()]

    def execute(self, skill_id: str, params: dict) -> dict:
        """通过 skill_id 路由调用对应 Skill，返回标准化结果"""
        manifest = self._registry.get(skill_id)
        if not manifest:
            return {"status": "ERROR", "message": f"未知 Skill: {skill_id}"}
        try:
            result = manifest.entry_fn(**params)
            # 标准化包装
            if isinstance(result, dict):
                return result
            return {"status": "SUCCESS", "data": result}
        except TypeError as e:
            return {"status": "ERROR", "message": f"参数不匹配: {str(e)}"}
        except Exception as e:
            return {"status": "ERROR", "message": f"执行异常: {str(e)}"}


# ======================================================================
# 全局单例
# ======================================================================
_bridge = OpenClawToolBridge()


def get_bridge() -> OpenClawToolBridge:
    """获取全局 Skill 桥接器实例"""
    return _bridge


def register_skill(manifest: SkillManifest):
    """便捷注册函数"""
    _bridge.register(manifest)


# ======================================================================
# 自动注册所有已有 Skill
# ======================================================================

def _auto_register_all():
    """启动时自动扫描并注册所有 Skill"""
    # --- generic_poi_searcher ---
    try:
        from skills.generic_poi_searcher.generic_poi_searcher import search_poi_matrix
        register_skill(SkillManifest(
            skill_id="search_poi",
            name="通用空间商户检索器",
            description="根据中心坐标、品类、半径和最低评分检索附近商户列表。支持 hair/pet/cafe/gym/restaurant/japanese/hotpot/cinema/laundry 品类。",
            entry_fn=search_poi_matrix,
            input_schema={
                "type": "object",
                "required": ["center_coord", "categories", "radius_meters", "min_rating"],
                "properties": {
                    "center_coord": {"type": "string", "description": "中心坐标 lat,lng"},
                    "categories": {"type": "array", "items": {"type": "string"}, "description": "品类列表"},
                    "radius_meters": {"type": "integer", "description": "搜索半径(米)"},
                    "min_rating": {"type": "number", "description": "最低评分"},
                }
            },
            output_schema={"type": "object"},
            triggers=["搜索商户", "附近有什么", "推荐店铺", "找店"],
            is_long_running=False,
        ))
    except ImportError:
        pass

    # --- concurrent_pipeline_scheduler ---
    try:
        from skills.concurrent_pipeline_scheduler.concurrent_pipeline_scheduler import solve_concurrent_timeline
        register_skill(SkillManifest(
            skill_id="solve_timeline",
            name="多任务并发排程引擎",
            description="根据任务列表、空间矩阵和当前时间，计算最优执行时间线，处理时间冲突，生成包含交通时间的完整行程计划。",
            entry_fn=solve_concurrent_timeline,
            input_schema={
                "type": "object",
                "required": ["task_list", "spatial_matrix", "current_time_str"],
                "properties": {
                    "task_list": {"type": "array", "description": "任务列表"},
                    "spatial_matrix": {"type": "object", "description": "空间位置矩阵"},
                    "current_time_str": {"type": "string", "description": "当前时间 HH:MM"},
                    "user_confirmed_tasks": {"type": "array", "items": {"type": "string"}},
                    "user_rejected_tasks": {"type": "array", "items": {"type": "string"}},
                }
            },
            output_schema={"type": "object"},
            triggers=["排程", "规划路线", "安排顺序", "时间冲突", "赶得上吗"],
            is_long_running=True,
        ))
    except ImportError:
        pass

    # --- destination_anti_pitfall ---
    try:
        from skills.destination_anti_pitfall.destination_anti_pitfall import execute_anti_pitfall_skill
        register_skill(SkillManifest(
            skill_id="anti_pitfall",
            name="目的地防踩坑",
            description="对用户行程节点进行防呆检查和风险提示，包括出门物品提醒、品类体感提示、交通按钮渲染。",
            entry_fn=execute_anti_pitfall_skill,
            input_schema={
                "type": "object",
                "required": ["trip_id", "pipeline_nodes", "environmental_context"],
                "properties": {
                    "trip_id": {"type": "string"},
                    "pipeline_nodes": {"type": "array"},
                    "environmental_context": {"type": "object"},
                    "transport": {"type": "string"},
                }
            },
            output_schema={"type": "object"},
            triggers=["防踩坑", "出门检查", "风险提示", "体感提示"],
            is_long_running=False,
        ))
    except ImportError:
        pass

    # --- anomaly_sensor_skill ---
    try:
        from skills.anomaly_sensor_skill.anomaly_sensor_skill import execute_anomaly_sensor_skill
        register_skill(SkillManifest(
            skill_id="anomaly_sensor",
            name="异常传感器",
            description="接收环境异常事件（关店/天气/交通），执行拓扑污染分析，生成打断级弹窗触发器驱动 Plan B 容灾。",
            entry_fn=execute_anomaly_sensor_skill,
            input_schema={
                "type": "object",
                "required": ["pipeline_nodes", "environmental_context"],
                "properties": {
                    "pipeline_nodes": {"type": "array"},
                    "environmental_context": {"type": "object"},
                }
            },
            output_schema={"type": "object"},
            triggers=["异常检测", "关店通知", "天气突变", "交通管制"],
            dependencies=["virtual_pipeline_mutate"],
            is_long_running=False,
        ))
    except ImportError:
        pass

    # --- time_master ---
    try:
        from skills.time_master.time_master import get_master

        def _time_offset(session_id: str, delta_minutes: int) -> dict:
            return get_master().offset(session_id, delta_minutes)

        def _time_jump(session_id: str, target_time: str) -> dict:
            return get_master().jump(session_id, target_time)

        def _time_set_speed(session_id: str, speed: float) -> dict:
            return get_master().set_speed(session_id, speed)

        def _time_start(session_id: str, speed: float = 60.0) -> dict:
            return get_master().start_auto_tick(session_id, speed)

        def _time_stop(session_id: str) -> dict:
            return get_master().stop_auto_tick(session_id)

        register_skill(SkillManifest(
            skill_id="time_offset",
            name="虚拟时钟快进",
            description="将虚拟时钟快进指定分钟数，触发沿途排程节点。",
            entry_fn=_time_offset,
            input_schema={
                "type": "object",
                "required": ["session_id", "delta_minutes"],
                "properties": {
                    "session_id": {"type": "string"},
                    "delta_minutes": {"type": "integer"},
                }
            },
            output_schema={"type": "object"},
            is_long_running=False,
        ))

        register_skill(SkillManifest(
            skill_id="time_jump",
            name="虚拟时钟跳转",
            description="将虚拟时钟跳转到指定时间 HH:MM，触发沿途排程节点。",
            entry_fn=_time_jump,
            input_schema={
                "type": "object",
                "required": ["session_id", "target_time"],
                "properties": {
                    "session_id": {"type": "string"},
                    "target_time": {"type": "string", "description": "HH:MM 格式"},
                }
            },
            output_schema={"type": "object"},
            is_long_running=False,
        ))
    except ImportError:
        pass

    # --- task_reminder_skill ---
    try:
        from skills.task_reminder_skill.task_reminder_skill import process_reminder_pipeline, handle_user_action as _reminder_handle_action
        from skills.time_master.time_master import get_master as _get_tm

        def _reminder_pipeline(session_id: str, triggered_events: list) -> list:
            tm = _get_tm()
            return process_reminder_pipeline(session_id, [], triggered_events, tm)

        def _reminder_user_action(session_id: str, user_input: str, current_time: str) -> dict:
            tm = _get_tm()
            return _reminder_handle_action(session_id, user_input, current_time, tm)

        register_skill(SkillManifest(
            skill_id="reminder_pipeline",
            name="服药/喝水提醒管线",
            description="处理虚拟时钟触发的健康提醒事件，驱动响铃→确认→吞服→闭环状态机。",
            entry_fn=_reminder_pipeline,
            input_schema={
                "type": "object",
                "required": ["session_id", "triggered_events"],
                "properties": {
                    "session_id": {"type": "string"},
                    "triggered_events": {"type": "array"},
                }
            },
            output_schema={"type": "array"},
            is_long_running=False,
        ))

        register_skill(SkillManifest(
            skill_id="reminder_user_action",
            name="提醒用户交互",
            description="处理用户对服药提醒的响应：1=去拿药，2=延后，'我已吞服药片'=确认吞服。",
            entry_fn=_reminder_user_action,
            input_schema={
                "type": "object",
                "required": ["session_id", "user_input", "current_time"],
                "properties": {
                    "session_id": {"type": "string"},
                    "user_input": {"type": "string", "description": "1/2/我已吞服药片/吃了"},
                    "current_time": {"type": "string"},
                }
            },
            output_schema={"type": "object"},
            is_long_running=False,
        ))
    except ImportError:
        pass

    # --- amap_poi (高德真实 POI 检索) ---
    try:
        from skills.amap_poi import search_poi as _amap_search_poi
        from skills.amap_poi import search_nearby as _amap_search_nearby
        from skills.amap_poi import fuzzy_search as _amap_fuzzy_search
        from skills.amap_poi import get_poi_detail as _amap_get_poi_detail
        from skills.amap_poi import geocode as _amap_geocode

        register_skill(SkillManifest(
            skill_id="amap_search_poi",
            name="高德关键字POI搜索",
            description="通过关键字+城市+品类搜索真实高德地图POI数据，返回商户名/评分/距离/地址/人均。支持 hair/pet/cafe/gym/restaurant/japanese/hotpot/cinema/laundry 品类。",
            entry_fn=_amap_search_poi,
            input_schema={
                "type": "object",
                "required": ["keywords"],
                "properties": {
                    "keywords": {"type": "string", "description": "搜索关键字，如「川菜」「咖啡馆」"},
                    "city": {"type": "string", "description": "城市名，默认「北京」"},
                    "category": {"type": "string", "description": "品类编码，如 hair/pet/cafe 等"},
                    "offset": {"type": "integer", "description": "返回条数，默认20"},
                }
            },
            output_schema={"type": "object"},
            triggers=["搜一下", "搜索商户", "找一下附近的", "有什么餐厅", "帮我找"],
        ))

        register_skill(SkillManifest(
            skill_id="amap_search_nearby",
            name="高德周边POI搜索",
            description="根据经纬度坐标搜索周边指定半径内的POI商户，支持品类过滤和最低评分过滤。",
            entry_fn=_amap_search_nearby,
            input_schema={
                "type": "object",
                "required": ["lng", "lat"],
                "properties": {
                    "lng": {"type": "number", "description": "中心点经度"},
                    "lat": {"type": "number", "description": "中心点纬度"},
                    "radius": {"type": "integer", "description": "搜索半径(米)，默认3000"},
                    "keywords": {"type": "string", "description": "搜索关键字，可选"},
                    "category": {"type": "string", "description": "品类编码，可选"},
                    "min_rating": {"type": "number", "description": "最低评分，默认0"},
                }
            },
            output_schema={"type": "object"},
            triggers=["附近有什么", "周边搜索", "周围的店", "距离我最近的"],
        ))

        register_skill(SkillManifest(
            skill_id="amap_fuzzy_search",
            name="高德模糊搜索/输入提示",
            description="根据用户输入的模糊关键词（如「有变形金刚的游乐园」）返回匹配的POI候选项列表，用于输入自动补全和语义消歧。",
            entry_fn=_amap_fuzzy_search,
            input_schema={
                "type": "object",
                "required": ["keywords"],
                "properties": {
                    "keywords": {"type": "string", "description": "模糊搜索关键词"},
                    "city": {"type": "string", "description": "城市名，默认「北京」"},
                }
            },
            output_schema={"type": "array"},
            triggers=["模糊搜索", "输入提示", "你说的那个是什么", "有XX的YY"],
        ))

        register_skill(SkillManifest(
            skill_id="amap_get_poi_detail",
            name="高德POI详情查询",
            description="根据POI ID获取单个商户的详细信息，包括完整地址/电话/评分/营业时间等。",
            entry_fn=_amap_get_poi_detail,
            input_schema={
                "type": "object",
                "required": ["poi_id"],
                "properties": {
                    "poi_id": {"type": "string", "description": "POI唯一标识"},
                }
            },
            output_schema={"type": "object"},
            triggers=["查一下这家店", "商户详情", "了解更多"],
        ))

        register_skill(SkillManifest(
            skill_id="amap_geocode",
            name="高德地理编码",
            description="将文本地址（如「三里屯太古里」「北京市朝阳区工体北路」）转换为经纬度坐标，是路径规划的前置能力。",
            entry_fn=_amap_geocode,
            input_schema={
                "type": "object",
                "required": ["address"],
                "properties": {
                    "address": {"type": "string", "description": "地址文本"},
                    "city": {"type": "string", "description": "城市名，默认「北京」"},
                }
            },
            output_schema={"type": "object"},
            triggers=["地址转坐标", "这个地方在哪", "帮我查一下坐标", "经纬度"],
        ))
    except ImportError:
        pass

    print(f"[OpenClaw Bridge] 已注册 {len(_bridge._registry)} 个 Skill")


# 启动时自动注册
_auto_register_all()
