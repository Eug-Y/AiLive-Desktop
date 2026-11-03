"""Competition demo preset for the AI digital-human application track.

The preset is intentionally synthetic and clearly labelled as a demo. It gives
the judging video a repeatable starting point without changing real merchant
data or implying commercial results.
"""

from typing import Dict, List


DEMO_PRODUCT: Dict = {
    "id": "competition-demo-digital-human",
    "name": "AI数字人应用（参赛演示）",
    "category": "regular",
    "platform_product_id": "DEMO-OPC-2026",
    "price": "演示不报价",
    "coupon": "演示不启用优惠",
    "commission_rate": "",
    "commission_amount": "",
    "selling_points": [
        "把商品资料、脚本、播报、互动和复盘放在同一条工作流中",
        "高频问题优先走本地规则，减少不必要的大模型调用",
        "超出资料范围的问题提示人工确认，不强行编造",
        "支持人工插话、打断、暂停自动内容和紧急停止",
    ],
    "target_audience": "需要低成本验证数字人直播和内容运营的小微商家团队",
    "usage": "本场仅演示软件链路，不代表真实商品、价格、客户、收入或转化结果",
    "notes": "仅用于赛事录屏和本地彩排。正式使用前必须替换为已授权、已核对的真实资料。",
    "forbidden_terms": [
        "保证成交",
        "无人值守",
        "第一名",
        "绝对有效",
    ],
    "verified": True,
    "material_authorized": True,
    "enabled": True,
    "play_duration": 180,
}


DEMO_SCRIPTS: List[str] = [
    "大家好，这里是 AI 数字人应用的参赛演示。",
    "它把商品资料、直播脚本、数字人播报、互动应答和运营复盘串成一条可核验的工作流。",
    "高频、低风险的问题先由本地规则回答，减少等待和不必要的大模型调用。",
    "复杂问题才进入受控的大模型兜底，超出资料范围或涉及敏感承诺时，系统会提示人工确认。",
    "运营人员可以随时插话、打断、暂停自动内容，必要时一键紧急停止。",
    "本场数据全部是演示数据，正式经营结果必须通过真实彩排、试用和客户记录验证。",
]


def apply_demo_preset() -> Dict:
    """Load a repeatable, synthetic competition scenario into the local studio."""
    from server.live_studio import live_studio_manager
    from server.sales_catalog import product_catalog

    product = product_catalog.upsert(DEMO_PRODUCT)
    product_catalog.set_current(product["id"])
    live_studio_manager.update_settings(
        {
            "platform": "other",
            "title": "AI数字人应用 · 参赛演示",
            "ai_label": "AI数字人 · 参赛演示 · 真人值守",
            "layout": "contain",
            "script_gap_seconds": 6,
            "repeat_scripts": False,
        }
    )
    live_studio_manager.set_scripts(DEMO_SCRIPTS, repeat=False)
    live_studio_manager.update_workflow(
        {
            "reset_session": True,
            "permissions_confirmed": True,
            "product_card_matched": True,
            "platform_product_attached": True,
            "audio_video_checked": False,
            "private_rehearsal_done": False,
            "human_supervisor_ready": True,
        }
    )
    return {
        "product": product,
        "scripts": list(DEMO_SCRIPTS),
        "notice": "已载入合成参赛演示数据。请勿将其当作真实客户、收入或经营结果。",
    }
