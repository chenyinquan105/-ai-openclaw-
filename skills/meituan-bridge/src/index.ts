import { Type } from "typebox";
import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";

const DEFAULT_BACKEND = "http://localhost:5000";

async function fetchJson(base: string, path: string, method = "GET", body?: unknown) {
  const url = `${base}${path}`;
  const opts: RequestInit = {
    method,
    headers: { "Content-Type": "application/json" },
  };
  if (body !== undefined) {
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Backend ${res.status}: ${text.slice(0, 300)}`);
  }
  return res.json();
}

export default defineToolPlugin({
  id: "meituan-bridge",
  name: "美团AI 时空沙盒技能包",
  description:
    "美团本地生活全天候数字管家 — 18个核心技能通过 HTTP Bridge 注册为 OpenClaw Agent Tool（搜索/排程/防踩坑/异常/提醒/时钟/偏好/路径规划/排队监控/天气/高德POI）",
  configSchema: Type.Object({
    backendUrl: Type.Optional(
      Type.String({ description: "Python Flask 后端地址，默认 http://localhost:5000" }),
    ),
  }),

  tools: (tool) => [
    // ── 1. POI搜索（仅搜索，不排程） ──
    tool({
      name: "meituan_search_poi",
      label: "POI搜索",
      description:
        "根据用户需求搜索美团本地生活商户，返回匹配的店铺列表。**注意：这只是搜索，不会自动生成行程或修改已有计划。** 如果用户只是想了解附近有什么、有没有某类店铺（如「附近有啥好吃的」「有没有川菜馆」），用这个工具。如果用户明确要求安排行程/加到计划中，才用排程工具。",
      parameters: Type.Object({
        query: Type.String({
          description: "用户原始需求文本，例如「帮我找附近的川菜馆，人均100以内」",
        }),
      }),
      async execute({ query }, config) {
        const base = config.backendUrl ?? DEFAULT_BACKEND;
        return fetchJson(base, "/api/start", "POST", { text: query });
      },
    }),

    // ── 2. 虚拟时钟状态 ──
    tool({
      name: "meituan_clock_status",
      label: "虚拟时钟状态",
      description: "查询当前虚拟时钟的时间、速度倍率、是否在运行。",
      parameters: Type.Object({}),
      async execute(_params, config) {
        const base = config.backendUrl ?? DEFAULT_BACKEND;
        return fetchJson(base, "/api/clock/status");
      },
    }),

    // ── 3. 虚拟时钟快进 ──
    tool({
      name: "meituan_clock_forward",
      label: "虚拟时钟快进",
      description: "快进虚拟时钟指定分钟数，触发该时段内的提醒/异常事件。",
      parameters: Type.Object({
        minutes: Type.Number({ description: "快进分钟数", default: 15 }),
      }),
      async execute({ minutes }, config) {
        const base = config.backendUrl ?? DEFAULT_BACKEND;
        return fetchJson(base, "/api/clock/offset", "POST", { minutes });
      },
    }),

    // ── 4. 虚拟时钟跳转 ──
    tool({
      name: "meituan_clock_jump",
      label: "虚拟时钟跳转",
      description: "将虚拟时钟跳转到指定时刻（HH:MM 格式）。",
      parameters: Type.Object({
        time: Type.String({ description: "目标时刻，格式 HH:MM，例如「14:30」" }),
      }),
      async execute({ time }, config) {
        const base = config.backendUrl ?? DEFAULT_BACKEND;
        return fetchJson(base, "/api/clock/jump", "POST", { time });
      },
    }),

    // ── 5. 虚拟时钟事件 ──
    tool({
      name: "meituan_clock_events",
      label: "虚拟时钟事件",
      description: "获取虚拟时钟推进过程中触发的提醒和异常事件。",
      parameters: Type.Object({}),
      async execute(_params, config) {
        const base = config.backendUrl ?? DEFAULT_BACKEND;
        return fetchJson(base, "/api/clock/events");
      },
    }),

    // ── 6. 偏好读取 ──
    tool({
      name: "meituan_preference_read",
      label: "读取管家偏好",
      description:
        "读取用户长期偏好：口味（辣度/忌口/菜系）、通勤（步行距离/交通方式）、预算（价格档位/评分门槛）、健康作息（喝水间隔/吃药时间）。",
      parameters: Type.Object({}),
      async execute(_params, config) {
        const base = config.backendUrl ?? DEFAULT_BACKEND;
        return fetchJson(base, "/api/profile/get");
      },
    }),

    // ── 7. 偏好更新 ──
    tool({
      name: "meituan_preference_update",
      label: "更新管家偏好",
      description:
        "更新用户偏好中的指定维度，支持口味、通勤、预算、健康作息四个维度的部分更新。",
      parameters: Type.Object({
        updates: Type.Record(Type.String(), Type.Unknown(), {
          description:
            '要更新的偏好键值对，例如 {"taste": {"taste_tolerance": "中辣"}} 或 {"budget": {"price_level": "高端"}}',
        }),
      }),
      async execute({ updates }, config) {
        const base = config.backendUrl ?? DEFAULT_BACKEND;
        return fetchJson(base, "/api/profile/set", "POST", { updates });
      },
    }),

    // ── 8. 提醒添加 ──
    tool({
      name: "meituan_reminder_add",
      label: "添加生活提醒",
      description: "添加喝水、吃药等生活健康提醒任务。",
      parameters: Type.Object({
        task_type: Type.String({ description: "提醒类型：water 或 medicine" }),
        label: Type.String({ description: "提醒显示文本，例如「喝杯水吧💧」" }),
        time: Type.String({ description: "提醒时刻，格式 HH:MM，例如「14:30」" }),
      }),
      async execute({ task_type, label, time }, config) {
        const base = config.backendUrl ?? DEFAULT_BACKEND;
        return fetchJson(base, "/api/reminder/add_task", "POST", {
          task_type,
          label,
          time,
        });
      },
    }),

    // ── 9. 提醒删除 ──
    tool({
      name: "meituan_reminder_remove",
      label: "删除生活提醒",
      description: "删除指定提醒任务。",
      parameters: Type.Object({
        task_id: Type.String({ description: "要删除的提醒任务 ID" }),
      }),
      async execute({ task_id }, config) {
        const base = config.backendUrl ?? DEFAULT_BACKEND;
        return fetchJson(base, "/api/reminder/remove_task", "POST", { task_id });
      },
    }),

    // ── 10. 异常事件注入 ──
    tool({
      name: "meituan_anomaly_inject",
      label: "注入异常事件",
      description:
        "动态注入异常事件（店铺停电/排队满/天气预警/交通管制），触发异常传感器的 Plan B 容灾管线。",
      parameters: Type.Object({
        event_class: Type.String({
          description:
            "事件类型：STORE_CLOSURE | QUEUE_FULL | WEATHER_EVENT | TRAFFIC_CONTROL",
        }),
        shop_name: Type.Optional(
          Type.String({ description: "受影响的店铺名称（不填则随机选一家）" }),
        ),
      }),
      async execute({ event_class, shop_name }, config) {
        const base = config.backendUrl ?? DEFAULT_BACKEND;
        return fetchJson(base, "/api/anomaly/inject", "POST", {
          event_class,
          shop_name,
        });
      },
    }),

    // ── 11. 防踩坑检查 ──
    tool({
      name: "meituan_pitfall_check",
      label: "防踩坑检查",
      description:
        "对当前排程管线做防踩坑检查，包括步行距离评估、交通模式建议、身份一致性校验（如宠物店→洗猫时确认是猫还是狗）。",
      parameters: Type.Object({
        pipeline_nodes: Type.Array(Type.Record(Type.String(), Type.Unknown()), {
          description:
            "排程节点列表，每个节点包含 shop_name/transport/location 等字段",
        }),
        walking_tolerance_meters: Type.Optional(
          Type.Number({
            description: "步行容忍距离（米），默认 800",
            default: 800,
          }),
        ),
      }),
      async execute({ pipeline_nodes, walking_tolerance_meters }, config) {
        const base = config.backendUrl ?? DEFAULT_BACKEND;
        return fetchJson(base, "/api/pitfall/check", "POST", {
          pipeline_nodes,
          walking_tolerance_meters: walking_tolerance_meters ?? 800,
        });
      },
    }),

    // ── 12. 路径规划 ──
    tool({
      name: "meituan_route_plan",
      label: "路径规划",
      description:
        "多节点路径规划：给定起点+途经点+交通偏好+天气，计算最优访问顺序与接驳方式（步行/打车/地铁）。",
      parameters: Type.Object({
        start_coord: Type.Optional(
          Type.String({ description: "出发坐标 lat,lng，默认三里屯" }),
        ),
        waypoints: Type.Array(
          Type.Object({
            id: Type.String(),
            name: Type.String(),
            coord: Type.String({ description: "坐标 lat,lng" }),
            duration_minutes: Type.Number({ description: "活动耗时（分钟）" }),
          }),
          { description: "途经点列表" },
        ),
        transport_preference: Type.Optional(
          Type.String({ description: "步行优先/打车优先/地铁优先" }),
        ),
        walking_tolerance_meters: Type.Optional(
          Type.Number({ description: "步行容忍距离（米）" }),
        ),
        weather_condition: Type.Optional(
          Type.String({ description: "天气状况，影响步行容忍距离" }),
        ),
      }),
      async execute(params, config) {
        const base = config.backendUrl ?? DEFAULT_BACKEND;
        return fetchJson(base, "/api/route/plan", "POST", params);
      },
    }),

    // ── 13. 排队监控 ──
    tool({
      name: "meituan_queue_monitor",
      label: "排队监控",
      description:
        "餐厅排队监控：排号入队、查询排号状态、批量轮询所有排队（≤5桌时自动提醒叫车）。",
      parameters: Type.Object({
        action: Type.String({ description: "enqueue 排号 | query 查询 | poll_all 轮询全部" }),
        shop_id: Type.Optional(Type.String({ description: "店铺 ID（enqueue/query 时必填）" })),
        shop_name: Type.Optional(Type.String({ description: "店铺名称（enqueue 时填写）" })),
        queue_id: Type.Optional(Type.String({ description: "排号 ID（query 时必填）" })),
        party_size: Type.Optional(Type.Number({ description: "用餐人数", default: 2 })),
      }),
      async execute(params, config) {
        const base = config.backendUrl ?? DEFAULT_BACKEND;
        const action = params.action;
        const body: Record<string, unknown> = { ...params };
        delete body.action;
        return fetchJson(base, `/api/queue/${action}`, "POST", body);
      },
    }),

    // ── 14. 天气查询 ──
    tool({
      name: "meituan_weather",
      label: "天气查询",
      description:
        "查询指定坐标+日期的天气状况，返回天气数据、活动建议（户外/室内）、交通影响（步行惩罚系数）。",
      parameters: Type.Object({
        coord: Type.Optional(
          Type.String({ description: "坐标 lat,lng，默认三里屯" }),
        ),
        date: Type.Optional(
          Type.String({ description: "日期 YYYY-MM-DD，默认今天" }),
        ),
      }),
      async execute(params, config) {
        const base = config.backendUrl ?? DEFAULT_BACKEND;
        return fetchJson(base, "/api/weather", "POST", params);
      },
    }),

    // ── 15. 高德POI关键字搜索 ──
    tool({
      name: "meituan_poi_search",
      label: "高德POI搜索",
      description:
        "通过关键字+城市+品类搜索真实高德地图POI数据，返回商户名/评分/距离/地址/人均。",
      parameters: Type.Object({
        keywords: Type.String({ description: "搜索关键字，如「川菜」「咖啡馆」" }),
        city: Type.Optional(Type.String({ description: "城市名，默认北京" })),
        category: Type.Optional(Type.String({ description: "品类编码：hair/pet/cafe/gym/restaurant/japanese/hotpot/cinema/laundry" })),
        offset: Type.Optional(Type.Number({ description: "返回条数，默认10" })),
      }),
      async execute({ keywords, city, category, offset }, config) {
        const base = config.backendUrl ?? DEFAULT_BACKEND;
        return fetchJson(base, "/api/poi/search", "POST", { keywords, city, category, offset });
      },
    }),

    // ── 16. 高德周边POI搜索 ──
    tool({
      name: "meituan_poi_nearby",
      label: "高德周边搜索",
      description:
        "根据经纬度搜索周边指定半径内的POI商户，支持品类过滤和最低评分过滤。",
      parameters: Type.Object({
        lng: Type.Number({ description: "中心点经度" }),
        lat: Type.Number({ description: "中心点纬度" }),
        radius: Type.Optional(Type.Number({ description: "搜索半径(米)，默认3000" })),
        keywords: Type.Optional(Type.String({ description: "搜索关键字" })),
        category: Type.Optional(Type.String({ description: "品类编码" })),
        min_rating: Type.Optional(Type.Number({ description: "最低评分，默认0（不过滤）" })),
      }),
      async execute({ lng, lat, radius, keywords, category, min_rating }, config) {
        const base = config.backendUrl ?? DEFAULT_BACKEND;
        return fetchJson(base, "/api/poi/nearby", "POST", { lng, lat, radius, keywords, category, min_rating });
      },
    }),

    // ── 17. 高德模糊搜索 ──
    tool({
      name: "meituan_poi_fuzzy",
      label: "高德模糊搜索",
      description:
        "根据用户输入的模糊关键词（如「有变形金刚的游乐园」）返回匹配的POI候选项列表，用于输入自动补全和语义消歧。",
      parameters: Type.Object({
        keywords: Type.String({ description: "模糊搜索关键词" }),
        city: Type.Optional(Type.String({ description: "城市名，默认北京" })),
      }),
      async execute({ keywords, city }, config) {
        const base = config.backendUrl ?? DEFAULT_BACKEND;
        return fetchJson(base, "/api/poi/fuzzy", "POST", { keywords, city });
      },
    }),

    // ── 18. 高德地理编码 ──
    tool({
      name: "meituan_poi_geocode",
      label: "高德地理编码",
      description:
        "将文本地址（如「三里屯太古里」）转换为经纬度坐标，或反向将经纬度转换为地址。是路径规划的前置能力。",
      parameters: Type.Object({
        action: Type.String({ description: "geocode=地址转坐标 | reverse=坐标转地址" }),
        address: Type.Optional(Type.String({ description: "地址文本（action=geocode时必填）" })),
        lng: Type.Optional(Type.Number({ description: "经度（action=reverse时必填）" })),
        lat: Type.Optional(Type.Number({ description: "纬度（action=reverse时必填）" })),
        city: Type.Optional(Type.String({ description: "城市名，默认北京" })),
      }),
      async execute({ action, address, lng, lat, city }, config) {
        const base = config.backendUrl ?? DEFAULT_BACKEND;
        return fetchJson(base, "/api/poi/geocode", "POST", { action, address, lng, lat, city });
      },
    }),
  ],
});
