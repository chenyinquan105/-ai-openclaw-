"""
destination_anti_pitfall —— 目的地防踩坑 Skill
=============================================

输入接口（Input Protocol）
-------------------------
{
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "OpenClaw_Skill_Input_Protocol",
    "type": "object",
    "required": ["trip_id", "pipeline_nodes", "environmental_context"],
    "properties": {
        "trip_id": {
            "type": "string",
            "description": "本次行程实例的唯一分布式追踪ID"
        },
        "current_node_index": {
            "type": "integer",
            "description": "当前执行到了第几个空间节点"
        },
        "pipeline_nodes": {
            "type": "array",
            "description": "用户本次行程中所有已被调度的空间实体节点序列",
            "items": {
                "type": "object",
                "required": ["node_id", "node_name", "category", "coordinate"],
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": "POI实体的唯一业务ID"
                    },
                    "node_name": {
                        "type": "string",
                        "description": "POI实体的物理名称（如：XX火锅店、YY电影院）"
                    },
                    "category": {
                        "type": "string",
                        "description": "泛化标准品类标签，与上游 generic_poi_searcher 品类体系对齐。合法值: hair, pet, cafe, gym, restaurant, cinema, laundry, japanese, hotpot"
                    },
                    "coordinate": {
                        "type": "string",
                        "description": "纬度,经度 字符串"
                    },
                    "attributes": {
                        "type": "object",
                        "description": "动态扩展属性域，例如是否需要人在场、预计停留时间等",
                        "additionalProperties": true
                    }
                }
            }
        },
        "environmental_context": {
            "type": "object",
            "required": ["timestamp", "weather_summary"],
            "properties": {
                "timestamp": {
                    "type": "integer",
                    "description": "当前触发时间戳"
                },
                "weather_summary": {
                    "type": "string",
                    "description": "当前物理天气特征简述"
                },
                "client_platform": {
                    "type": "string",
                    "description": "终端渠道，如 WECHAT, TELEGRAM, SLACK"
                }
            }
        }
    }
}

输出接口（Output Protocol）
--------------------------
{
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "OpenClaw_Skill_Output_Protocol",
    "type": "object",
    "required": ["status", "global_reminders", "localized_insights", "intent_triggers"],
    "properties": {
        "status": { "type": "string", "enum": ["SUCCESS", "ERROR"] },
        "global_reminders": {
            "type": "array",
            "description": "全局物理强防呆提醒域，无论目的地为何处均强制高亮置顶渲染",
            "items": {
                "type": "object",
                "required": ["reminder_type", "display_text"],
                "properties": {
                    "reminder_type": { "type": "string", "description": "提醒类型（如：items_check, security, weather）" },
                    "display_text": { "type": "string", "description": "对用户展示的文案，支持 Markdown 格式" }
                }
            }
        },
        "localized_insights": {
            "type": "array",
            "description": "局部空间节点体感指引域，按节点动态绑定",
            "items": {
                "type": "object",
                "required": ["associated_node_id", "title", "content"],
                "properties": {
                    "associated_node_id": { "type": "string", "description": "对应输入接口中的 node_id，用于前端绑定卡片" },
                    "title": { "type": "string", "description": "提示小标题" },
                    "content": { "type": "string", "description": "针对该特定节点深度优化的防坑/舒适度文本" }
                }
            }
        },
        "intent_triggers": {
            "type": "array",
            "description": "动态交互动作触发域。框架根据此域在前端渲染交互组件，并在触发时反射调用工具链",
            "items": {
                "type": "object",
                "required": ["trigger_id", "ui_manifest", "action_reflection"],
                "properties": {
                    "trigger_id": { "type": "string", "description": "触发器唯一标识" },
                    "ui_manifest": {
                        "type": "object",
                        "required": ["component_type", "prompt_text", "confirm_label"],
                        "properties": {
                            "component_type": { "type": "string", "description": "UI组件样式（如 standard_button, dialog）" },
                            "prompt_text": { "type": "string", "description": "引导互动文案" },
                            "confirm_label": { "type": "string", "description": "动作确认按钮上显示的文本" }
                        }
                    },
                    "action_reflection": {
                        "type": "object",
                        "required": ["target_tools", "parameter_mapping"],
                        "properties": {
                            "target_tools": {
                                "type": "array",
                                "items": { "type": "string" },
                                "description": "需要被触发的虚拟动作标识符列表。由虚拟后台事件总线统一注册，当前预注册动作: virtual_call_taxi, virtual_queue, virtual_weather_notice, virtual_delay_warning。不在注册表中的标识符将被静默忽略。"
                            },
                            "parameter_mapping": {
                                "type": "object",
                                "description": "运行时参数透传路由表，框架在此动态注入输入上下文中的真实变量值",
                                "additionalProperties": true
                            }
                        }
                    }
                }
            }
        }
    }
}
"""

import json
import time
from typing import List, Dict, Any

# ======================================================================
# 核心架构：全泛化抽象规则注册中心 (Data-Driven Registry)
# ======================================================================

# 全局强制前置防呆规则集
_GLOBAL_REMINDER_RULES = [
    {
        "reminder_type": "items_check",
        "display_text": "出门前请确认是否带齐：钥匙、手机、充电宝、身份证。"
    }
]

# 针对合法 Category 的泛化体感提示与控制流行为映射
# 规则内部严禁包含任何"线上排号"文本，聚焦环境体感与特定防御装备
_CATEGORY_BEHAVIOR_MATRIX = {
    "cinema": {
        "title": "影厅环境防冻与装备指南",
        "abstract_template": "影厅内冷气通常极足（约20°C），静坐极易受凉，强烈建议顺手带件薄外套。另外，部分特效厅不提供免费3D眼镜，建议包里自带。",
        "registered_actions": []
    },
    "hotpot": {
        "title": "重装织物防味警示",
        "abstract_template": "吃火锅极易留下一身浓重风味。落座请立刻让服务员提供防护罩衣，强烈建议包里随身准备【口香糖】与【除味清新剂】及时解围。",
        "registered_actions": ["FOOD_DELIVERY_FLOW"]
    },
    "restaurant": {
        "title": "空间油烟与织物除味提示",
        "abstract_template": "部分餐厅通风较差易沾染明显油烟味，出门前强烈建议随身准备【口香糖】与【织物清新剂】以备不时之需。",
        "registered_actions": ["FOOD_DELIVERY_FLOW"]
    },
    "japanese": {
        "title": "烟熏环境与口气清新管理",
        "abstract_template": "部分烧鸟/居酒屋等密闭烟熏味较重，出门前强烈建议在随身包中塞入【口香糖】与【除味清新剂】。",
        "registered_actions": ["FOOD_DELIVERY_FLOW"]
    },
    "hair": {
        "title": "消费透明度物理防御",
        "abstract_template": "开剪前务必与发型师明确『洗剪吹』最终一口价，警惕中途推销任何高价药水、头皮护理或会员卡卡项。",
        "registered_actions": []
    },
    "pet": {
        "title": "后台托管时效确认",
        "abstract_template": "宠物洗澡通常需要 1.5 至 2 小时，交付后无需现场死守。放下宠物后请务必与店员精确对齐预计接回的时间。",
        "registered_actions": []
    },
    "gym": {
        "title": "运动装备及卫生防御",
        "abstract_template": "请检查行囊，确认带齐了干净的运动鞋、换洗衣物以及水杯。部分健身房不提供免费毛巾，建议自备。",
        "registered_actions": []
    },
    "laundry": {
        "title": "资产交付前置清空核对",
        "abstract_template": "交付衣物前，请务必当场仔细掏空并核对所有衣袋，避免钥匙、硬币或重要小纸条遗留丢失。",
        "registered_actions": []
    },
    "cafe": {
        "title": "移动办公舒适度与环保提示",
        "abstract_template": "如需长时间移动办公，请注意部分座位可能缺少电源插座。多数店铺自带杯可享受减免，建议随身携带保温杯。",
        "registered_actions": []
    }
}

# 平台注册的虚拟底层工具别名定义（对齐事件总线，防止瞎填）
_VALID_TARGET_TOOLS = {"virtual_call_taxi", "virtual_queue", "virtual_weather_notice", "virtual_delay_warning", "virtual_navigation"}


# ======================================================================
# 核心执行引擎 (Skill Implementation)
# ======================================================================

def execute_anti_pitfall_skill(input_payload: dict, client=None, model: str = "deepseek-chat") -> dict:
    """
    OpenClaw 目的地防踩坑核心 Skill 逻辑实现。
    完全基于数据驱动及协议泛化，绝不包含实例硬编码。
    """
    output_response = {
        "status": "SUCCESS",
        "global_reminders": [],
        "localized_insights": [],
        "intent_triggers": []
    }

    try:
        if "pipeline_nodes" not in input_payload or "environmental_context" not in input_payload:
            raise ValueError("Input payload misses required root keys: 'pipeline_nodes' or 'environmental_context'")

        pipeline_nodes = input_payload["pipeline_nodes"]
        env_context = input_payload["environmental_context"]
        weather_summary = env_context.get("weather_summary", "未知天气")
        user_transport = input_payload.get("transport", "步行")

        # 2. 构建全局物理强防呆提醒（100% 触发）
        for rule in _GLOBAL_REMINDER_RULES:
            output_response["global_reminders"].append({
                "reminder_type": rule["reminder_type"],
                "display_text": rule["display_text"]
            })

        # 3. 遍历行程节点，利用规则矩阵进行抽象扫描并由大模型动态实例化
        activated_action_flows = set()

        for node in pipeline_nodes:
            node_id = node.get("node_id")
            node_name = node.get("node_name")
            category = node.get("category")
            coordinate = node.get("coordinate")

            behavior_rule = _CATEGORY_BEHAVIOR_MATRIX.get(category, {
                "title": "出行前置提示",
                "abstract_template": "即将前往新空间，请注意保管好随身物品，保持机警。",
                "registered_actions": []
            })

            for action_flow in behavior_rule["registered_actions"]:
                activated_action_flows.add((action_flow, node_name, coordinate, category))

            # 直接使用规则矩阵中的 abstract_template，无需 LLM 润色
            final_content = behavior_rule["abstract_template"]

            # 填充局部空间节点体感指引域
            # 步行距离评估：超过 walking_tolerance 时自动建议打车
        walking_tolerance = input_payload.get("walking_tolerance_meters", 800)
        if node.get("distance_meters", 0) > walking_tolerance and user_transport == "步行":
            final_content += f"\n\n🚕 距 {node_name} 步行约 {node.get('distance_meters', 0)}m，超过您的步行容忍距离({walking_tolerance}m)，建议打车前往。"
            if not any(t.get("trigger_id", "").startswith(f"trig_taxi_{node_id}") for t in output_response["intent_triggers"]):
                output_response["intent_triggers"].append({
                    "trigger_id": f"trig_taxi_{node_id}_{int(time.time())}",
                    "ui_manifest": {
                        "component_type": "standard_button",
                        "prompt_text": f"前往 [{node_name}] 步行距离({node.get('distance_meters', 0)}m)较远，是否一键叫车？",
                        "confirm_label": "叫车",
                        "cancel_label": "不需要",
                    },
                    "action_reflection": {
                        "target_tools": ["virtual_call_taxi"],
                        "parameter_mapping": {
                            "taxi_target_name": node_name,
                            "taxi_target_coord": coordinate,
                            "request_timestamp": int(time.time()),
                        },
                    },
                })

        output_response["localized_insights"].append({
                "associated_node_id": node_id,
                "title": behavior_rule["title"],
                "content": final_content
            })

        # 4. 动态构建全通用的动作反射触发域
        # 判断行程中是否包含餐饮品类
        food_categories = {"hotpot", "restaurant", "japanese"}
        has_food = any(n.get("category") in food_categories for n in pipeline_nodes)
        # 取第一个非全局强防呆节点作为导航/叫车目标
        first_node = pipeline_nodes[0] if pipeline_nodes else {}
        first_name = first_node.get("node_name", "")
        first_coord = first_node.get("coordinate", "")

        is_taxi_mode = (user_transport == "打车")

        for flow_type, ref_node_name, ref_coordinate, ref_category in activated_action_flows:
            if flow_type == "FOOD_DELIVERY_FLOW":
                if is_taxi_mode:
                    output_response["intent_triggers"].append({
                        "trigger_id": f"trig_flow_{int(time.time())}_{ref_category}",
                        "ui_manifest": {
                            "component_type": "standard_button",
                            "prompt_text": f"行程包含餐饮节点 [{ref_node_name}]，是否需要为您一键餐厅排号和叫车？",
                            "confirm_label": "执行",
                            "cancel_label": "不需要"
                        },
                        "action_reflection": {
                            "target_tools": ["virtual_call_taxi", "virtual_queue"],
                            "parameter_mapping": {
                                "taxi_target_name": ref_node_name,
                                "taxi_target_coord": ref_coordinate,
                                "queue_shop_category": ref_category,
                                "request_timestamp": int(time.time())
                            }
                        }
                    })
                else:
                    output_response["intent_triggers"].append({
                        "trigger_id": f"trig_nav_flow_{int(time.time())}_{ref_category}",
                        "ui_manifest": {
                            "component_type": "standard_button",
                            "prompt_text": f"行程包含餐饮节点 [{ref_node_name}]，是否需要为您导航前往？",
                            "confirm_label": "执行",
                            "cancel_label": "不需要"
                        },
                        "action_reflection": {
                            "target_tools": ["virtual_navigation"],
                            "parameter_mapping": {
                                "nav_target_name": ref_node_name,
                                "nav_target_coord": ref_coordinate,
                                "nav_transport": user_transport,
                                "request_timestamp": int(time.time())
                            }
                        }
                    })

        # 没有餐饮节点时，弹单按钮导航/叫车
        if not output_response["intent_triggers"] and first_name:
            if is_taxi_mode:
                output_response["intent_triggers"].append({
                    "trigger_id": f"trig_taxi_{int(time.time())}",
                    "ui_manifest": {
                        "component_type": "standard_button",
                        "prompt_text": f"是否需要为您一键叫车前往 [{first_name}]？",
                        "confirm_label": "执行",
                        "cancel_label": "不需要"
                    },
                    "action_reflection": {
                        "target_tools": ["virtual_call_taxi"],
                        "parameter_mapping": {
                            "taxi_target_name": first_name,
                            "taxi_target_coord": first_coord,
                            "request_timestamp": int(time.time())
                        }
                    }
                })
            else:
                output_response["intent_triggers"].append({
                    "trigger_id": f"trig_nav_{int(time.time())}",
                    "ui_manifest": {
                        "component_type": "standard_button",
                        "prompt_text": f"是否需要为您导航前往 [{first_name}]？",
                        "confirm_label": "执行",
                        "cancel_label": "不需要"
                    },
                    "action_reflection": {
                        "target_tools": ["virtual_navigation"],
                        "parameter_mapping": {
                            "nav_target_name": first_name,
                            "nav_target_coord": first_coord,
                            "nav_transport": user_transport,
                            "request_timestamp": int(time.time())
                        }
                    }
                })

    except Exception as err:
        output_response["status"] = "ERROR"
        output_response["global_reminders"].append({
            "reminder_type": "security",
            "display_text": f"❌ OpenClaw Pipeline 运行时引发了致命阻断错误: {str(err)}"
        })

    return output_response


# ======================================================================
# 前端/IM 终端通用通用协议渲染层 (Framework Renderer)
# ======================================================================

def get_pending_triggers(skill_output: dict) -> list:
    """
    全通用的触发器提取器。
    返回 intent_triggers 列表，由前端/上层负责渲染和执行。
    不再包含 print/input 终端交互逻辑。
    """
    if skill_output.get("status") != "SUCCESS":
        return []
    return skill_output.get("intent_triggers", [])


def dispatch_reflection(trigger: dict) -> dict:
    """
    反射动作执行器。
    接收一个 intent_trigger 对象，执行其 action_reflection 中的虚拟动作。
    返回执行结果字典。
    """
    result = {"status": "SUCCESS", "executed_tools": []}

    reflection = trigger.get("action_reflection", {})
    target_tools = reflection.get("target_tools", [])
    params = reflection.get("parameter_mapping", {})

    for tool_name in target_tools:
        if tool_name not in _VALID_TARGET_TOOLS:
            result["status"] = "WARNING"
            result["executed_tools"].append({
                "tool": tool_name,
                "status": "SKIPPED",
                "reason": f"'{tool_name}' 不在已注册的虚拟动作列表中"
            })
            continue

        if tool_name == "virtual_call_taxi":
            result["executed_tools"].append({
                "tool": "virtual_call_taxi",
                "status": "SUCCESS",
                "params": {
                    "destination": params.get("taxi_target_name", ""),
                    "coord": params.get("taxi_target_coord", "")
                }
            })
        elif tool_name == "virtual_queue":
            result["executed_tools"].append({
                "tool": "virtual_queue",
                "status": "SUCCESS",
                "params": {
                    "shop_category": params.get("queue_shop_category", "")
                }
            })
        elif tool_name == "virtual_weather_notice":
            result["executed_tools"].append({
                "tool": "virtual_weather_notice",
                "status": "SUCCESS",
                "params": {}
            })
        elif tool_name == "virtual_delay_warning":
            result["executed_tools"].append({
                "tool": "virtual_delay_warning",
                "status": "SUCCESS",
                "params": {
                    "delay_minutes": params.get("delay_minutes", 0)
                }
            })

    return result
