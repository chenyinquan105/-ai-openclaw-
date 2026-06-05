"""
anomaly_sensor_skill —— 异常传感器 Skill 核心引擎
======================================================
"""

import time
from typing import Dict, Any

def execute_anomaly_sensor_skill(input_payload: dict) -> dict:
    """
    全通用异常感应核心 Skill 实现。
    完全基于数据驱动及协议泛化，通过拓扑分析动态生成打断级弹窗契约。
    """
    # 初始化标准输出容器，严格对齐 OpenClaw 协议标准
    output_response = {
        "status": "SUCCESS",
        "localized_insights": [],
        "intent_triggers": []
    }

    try:
        # 1. 入参边界校验与防御
        if not isinstance(input_payload, dict):
            raise ValueError("输入载荷必须为标准 Key-Value 字典结构")

        pipeline_nodes = input_payload.get("pipeline_nodes", [])
        env_context = input_payload.get("environmental_context", {})
        active_anomalies = env_context.get("active_anomalies", [])

        # 如果当前时空网格中没有检测到任何后端 Webhook 异常，直接丝滑放行
        if not active_anomalies:
            return output_response

        # 2. 建立节点索引映射表 (拓扑快速对齐)
        # 将节点列表转化为 ID 到实体的映射，避免在循环中重复执行 O(N) 查找
        node_indexer = {node["node_id"]: node for node in pipeline_nodes if "node_id" in node}

        # 3. 遍历异动流，执行泛化拓扑污染评估
        for anomaly in active_anomalies:
            anomaly_id = anomaly.get("anomaly_id", f"anom_unk_{int(time.time())}")
            anomaly_class = anomaly.get("anomaly_class", "UNKNOWN_DISRUPTION")
            target_node_id = anomaly.get("target_node_id")
            description = anomaly.get("description", "时空环境发生不平稳异动")
            impact_min = anomaly.get("impact_duration_minutes", 0)

            # 防御性跳过：target_node_id 为空时无法定位受灾节点
            if not target_node_id:
                continue

            # 解析安全流控指令域 (Directives)
            directives = anomaly.get("fallback_directives", {})
            action_required = directives.get("action_required", "POSTPONE_NODE")
            attribute_filter = directives.get("attribute_filter", {})

            # 拓扑锁定：寻找当前管线中被波及的物理实体
            corrupted_node = node_indexer.get(target_node_id)
            node_name = corrupted_node["node_name"] if corrupted_node else f"Node_{target_node_id}"

            # 4. 纯泛化组装局部空间体感指引 (Localized Insights)
            # 通过变量直接填充模板，绝无特定业务硬编码
            insight_item = {
                "associated_node_id": target_node_id,
                "title": f"⚠️ 空间管线拓扑污染告警 [{anomaly_class}]",
                "content": f"检测到目标节点 [{node_name}] 遭遇环境突变。该时空异动预计引发管线整体延误约 {impact_min} 分钟。"
            }
            output_response["localized_insights"].append(insight_item)

            # 5. 纯泛化构建阻断级动作反射触发器 (Intent Triggers)
            # 将策略解耦，把原子控制变量(Mutation Manifest)无损打包透传给 main.py 的 Hook
            trigger_item = {
                "trigger_id": f"tg_hook_{anomaly_id}",
                "ui_manifest": {
                    "component_type": "dialog",  # 声明为高亮打断级 Dialog 弹窗
                    "prompt_text": f"系统已捕获非平稳时空阻断，建议立刻激活安全容灾预案：执行 [{action_required}] 规避物理损耗。",
                    "confirm_label": "执行 Plan B"
                },
                "action_reflection": {
                    "target_tools": [
                        "virtual_pipeline_mutate"  # 反射调用底层的通用管线变异器
                    ],
                    "parameter_mapping": {
                        "execute_intercept_hook": True,  # 唤醒 main.py 拦截钩子的物理硬标记
                        "corrupted_node_id": target_node_id,  # 受灾目标 ID
                        "mutation_directive": action_required,  # 变异策略指令 (如 SWAP_NODE / BYPASS_NODE)
                        "dynamic_filter": attribute_filter,  # Plan B 清洗平替店时的过滤条件标签组
                        "delta_delay_minutes": impact_min  # 降级所需的绝对增时量化
                    }
                }
            }
            output_response["intent_triggers"].append(trigger_item)

    except Exception as err:
        # 工业级运行时防断裂保护：一旦解析引发阻断性崩溃，自动切入降级 ERROR 状态
        output_response["status"] = "ERROR"
        output_response["localized_insights"] = [{
            "associated_node_id": "SYSTEM_CRITICAL",
            "title": "❌ Sensor Pipeline Crash",
            "content": f"异常传感器在泛化计算时遭遇阻断错误: {str(err)}"
        }]
        output_response["intent_triggers"] = []

    return output_response
