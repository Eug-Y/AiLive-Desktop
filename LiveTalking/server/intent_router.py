"""直播评论意图路由引擎（三级回应架构的第 1 级 T1）。

链路定位::

    评论行 → [T1 本引擎] 关键词/意图匹配 → 预置合规话术 → 立刻播报（不调用大模型）
           → [T2] 未命中 → 大模型兜底
           → [T3] 高风险/高价值 → 真人接管

设计目标
- 把高频重复问题的响应延迟从「秒级」压到「百毫秒级」（省掉 LLM 往返）
- 高频问题的答案只能来自已核对商品资料，不给模型自由发挥的空间
- 规则可由运营在 data/intent_rules.json 里增删，不必改代码

注意：关键词刻意使用「具体的多字词」而非单字，中文没有词边界，
      单字关键词（如「活」「鲜」）会大量误命中。
"""

import json
import os
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional

from server.sales_catalog import local_flavor, sales_compliance
from utils.logger import logger


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CUSTOM_RULES_FILE = PROJECT_ROOT / "data" / "intent_rules.json"

# 模板占位符：{name} {price} {coupon} {points} {audience} {usage}
_PLACEHOLDER = re.compile(r"\{(\w+)\}")

# ─── 内置意图规则 ──────────────────────────────────────────────────────
# 字段说明：
#   id         意图标识
#   label      中文名（用于界面展示与复盘统计）
#   category   口头禅分类（见 sales_catalog.DEFAULT_FLAVOR_PHRASES），空串表示不加
#   keywords   命中词（使用多字词避免误命中）
#   templates  话术模板，占位符缺失时会自动跳过该条
#   no_product 没有已核对商品时的兜底话术（必须只做合规引导）
INTENT_RULES: List[Dict] = [
    {
        "id": "price",
        "label": "问价格",
        "category": "trust",
        "keywords": [
            "多少钱一斤", "多少钱一箱", "多少钱一包", "多少钱", "什么价格", "什么价位",
            "什么价", "怎么卖", "几多钱", "多少米", "贵不贵", "划算吗", "价格多少",
            "一斤多少", "卖多少钱", "多少钱啊",
        ],
        "templates": [
            "{name}，{price}，以商品卡为准。",
            "价格是{price}，看商品卡最准。",
            "问价格的朋友，{name}，{price}。",
        ],
        "no_product": "价格以商品卡为准，我只报核对过的。",
    },
    {
        "id": "spec",
        "label": "问规格重量",
        "category": "trust",
        "keywords": [
            "几斤", "多重", "多少斤", "多少克", "多少只", "几只", "一斤几个",
            "一箱多少", "规格", "分量", "净重", "毛重", "多少条", "一斤几只",
        ],
        # 排除词：避免「主播多大了」这类问人的话被当成问规格
        "exclude": ["主播", "老板", "小姐姐", "几岁", "你多大", "年龄"],
        "templates": [
            "{name}规格看商品卡，别买错档。",
            "克重和数量商品卡都写了，照着看。",
        ],
        "no_product": "规格重量看商品卡，看清档位再拍。",
    },
    {
        "id": "fresh",
        "label": "问鲜活度",
        "category": "fresh",
        "keywords": [
            "活的吗", "活的么", "是活的", "保活", "会不会死", "新鲜吗", "新不新鲜",
            "新鲜不新鲜", "鲜不鲜", "死的吗", "冰鲜", "冰冻的", "冷冻的", "怎么保鲜", "保鲜",
        ],
        "templates": [
            "{name}保鲜方式商品卡有写。",
            "保鲜和运输看商品卡，有问题找客服。",
        ],
        "no_product": "保鲜方式看商品卡，有问题找客服。",
    },
    {
        "id": "shipping_fee",
        "label": "问邮费",
        "category": "trust",
        "keywords": [
            "包邮吗", "包邮么", "包不包邮", "运费", "邮费", "快递费", "要运费", "免运费",
        ],
        "templates": [
            "运费以结算页为准，下单能看到。",
        ],
        "no_product": "运费以结算页为准，下单就能看到。",
    },
    {
        "id": "shipping_time",
        "label": "问发货时效",
        "category": "trust",
        "keywords": [
            "几天到", "几天能到", "几天到货", "几天可以到", "多久到", "多久能到",
            "多久能收到", "什么时候到", "什么时候发", "什么时候发货", "多久发货",
            "发货时间", "几天发货", "发什么快递", "什么快递", "怎么发货", "顺丰", "冷链", "次日达",
        ],
        "templates": [
            "发货时效看商品卡，急的先看清。",
            "走什么快递商品卡都标了，看商品卡。",
        ],
        "no_product": "发货时效看商品卡，看清楚再拍。",
    },
    {
        "id": "origin",
        "label": "问产地",
        "category": "trust",
        "keywords": [
            "哪里产的", "哪里发货", "哪里发", "产地", "是不是连江", "本地", "正宗",
            "哪里的", "什么产地",
        ],
        "templates": [
            "{name}产地以商品卡标注为准。",
        ],
        "no_product": "产地以商品卡标注为准。",
    },
    {
        "id": "cook",
        "label": "问吃法做法",
        "category": "",
        "keywords": [
            "怎么做", "怎么吃", "做法", "清蒸", "煮多久", "怎么做好吃", "吃法",
            "炖汤", "怎么弄", "怎么处理", "要不要泡",
        ],
        # 排除词：「退款怎么弄」「怎么处理售后」是售后问题，不是做法问题
        "exclude": ["退款", "退货", "售后", "赔偿", "包赔", "能退", "坏了", "死了", "死的", "坏的"],
        "templates": [
            "做法：{usage}。",
            "{name}的吃法，{usage}。",
        ],
        "no_product": "具体做法看商品卡说明。",
    },
    {
        "id": "discount",
        "label": "问优惠",
        "category": "urge",
        "keywords": [
            "有优惠", "有没有优惠", "有券", "优惠券", "便宜点", "能便宜", "打折", "打个折",
            "满减", "什么活动", "有活动", "有折扣",
        ],
        "templates": [
            "现在{coupon}，以商品卡为准。",
            "{name}的优惠是{coupon}，看领券页。",
        ],
        "no_product": "优惠以商品卡领券页为准。",
    },
    {
        "id": "stock",
        "label": "问库存",
        "category": "urge",
        "keywords": [
            "还有吗", "还有货", "还有没有", "库存", "卖完了", "补货", "还有多少", "没货",
        ],
        "templates": [
            "库存看商品卡，能拍就是有货。",
            "还有没有货，看商品卡上的库存。",
        ],
        "no_product": "库存看商品卡，能拍就是有货。",
    },
    {
        "id": "aftersale",
        "label": "问售后",
        "category": "trust",
        "keywords": [
            "坏了怎么办", "死了怎么办", "不新鲜怎么办", "售后", "退款", "退货",
            "包赔", "赔吗", "坏了能退", "死了能退", "能退吗", "怎么赔",
        ],
        "templates": [
            "售后按平台规则走，有问题找客服。",
            "{name}的售后说明商品卡有写，及时找客服。",
        ],
        "no_product": "售后按平台规则走，有问题找客服。",
    },
    {
        "id": "follow",
        "label": "关注互动",
        "category": "greeting",
        "keywords": [
            "点关注", "点个关注", "加关注", "关注一下", "加粉丝团", "进粉丝团", "点个赞", "扣1",
            "扣个1", "扣一",
        ],
        "templates": [
            "谢谢支持！关注了开播有提醒。",
            "看到扣1的朋友了，{name}看商品卡。",
        ],
        "no_product": "谢谢支持！关注了开播有提醒。",
    },
]


# ─── 口语归一与语序无关的模式匹配（T1 第二级）──────────────────────────
# 引入原因：2026-10-27 留出集实测发现，纯关键词对「换个说法的同一问题」泛化只有
# 50%（28 条里 14 条未命中），典型失败是「一斤几钱」「走的哪家快递」「这个要怎么煮」
# 「关注你了」。这些不是新意图，而是同一意图的口语变体。
# 处理方式分两层：
#   1) SYNONYM_MAP：把方言/口语说法归一成规则里已有的词，再做关键词匹配；
#   2) PATTERN_RULES：按「疑问词 + 对象」的结构做正则，避免穷举所有句子。
# 两条都不放宽「排除词」逻辑；模式匹配只在关键词全未命中时启用，避免抢已有规则。
SYNONYM_MAP = (
    (re.compile(r"几多钱|几钱|多少米|多少大米|几块|多少钱啊"), "多少钱"),
    (re.compile(r"哪家快递|什么快递|哪个快递"), "什么快递"),
    (re.compile(r"多久能发|什么时候能发|几天能发|多久发"), "多久发货"),
    (re.compile(r"能不能谈|还能谈|可以谈|有得谈|少点|再少|低点"), "便宜点"),
    (re.compile(r"现捞|刚捞|刚上岸"), "活的"),
    (re.compile(r"坏掉|坏掉不|变质"), "会不会死"),
)

PATTERN_RULES = (
    # 顺序即优先级：越具体的意图越靠前
    ("shipping_fee", re.compile(r"(?:运费|邮费|快递费|包邮|免运|运费险)")),
    ("aftersale", re.compile(r"(?:售后|退款|退货|包赔|赔|坏了|死的|不新鲜)")),
    ("shipping_time", re.compile(r"(?:多久|几天|什么时候|第二天|次日|隔天)[^,]?(?:发|到|送到|收到)")),
    ("price", re.compile(r"(?:多少钱|什么价|怎么卖|贵不贵|划算|便宜|谈价|打几折)")),
    ("spec", re.compile(r"(?:多少|几)(?:斤|克|只|条|个|包|袋|盒|箱|串|份)|多重|净重|毛重|规格")),
    ("fresh", re.compile(r"(?:活|鲜|冻|死|保活|坏)")),
    ("origin", re.compile(r"(?:产地|原产|哪里|哪儿)[^,]?(?:产|发|来)|直发|连江|福建")),
    ("cook", re.compile(r"(?:怎么|如何)(?:煮|做|吃|蒸|炒|炖|弄|处理|泡|洗)|做法|吃法|要不要泡")),
    ("discount", re.compile(r"(?:优惠|活动|券|折扣|满减|打折|便宜)")),
    ("stock", re.compile(r"(?:库存|还有货|能拍|拍到|卖完|补货|没货|下架)")),
    ("follow", re.compile(r"(?:关注|粉丝团|点赞|扣1|扣个|扣一)")),
)


def normalize_comment(text: str) -> str:
    """归一化评论文本：统一标点、去掉空白、转小写，并做口语同义归一。"""
    text = str(text or "").strip().lower()
    text = text.translate(str.maketrans({
        "，": ",", "。": ".", "！": "!", "？": "?", "：": ":", "；": ";",
        "、": ",", "　": " ",
        "“": '"', "”": '"', "‘": "'", "’": "'",
    }))
    text = re.sub(r"\s+", "", text)
    for pattern, canonical in SYNONYM_MAP:
        text = pattern.sub(canonical, text)
    return text



class IntentRouter:
    """关键词意图路由：把评论行映射到合规的预置话术。"""

    def __init__(self, state_file: Path = CUSTOM_RULES_FILE):
        self._state_file = Path(state_file)
        self._lock = threading.RLock()
        self._custom_rules: List[Dict] = []
        self._disabled: set = set()
        self._hit_counter: Dict[str, int] = {}
        self._load()

    # ── 配置持久化 ──────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._state_file.exists():
            return
        try:
            saved = json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Failed to load custom intent rules")
            return
        if not isinstance(saved, dict):
            return
        self._disabled = {str(item) for item in saved.get("disabled", []) if str(item).strip()}
        rules = saved.get("rules")
        if isinstance(rules, list):
            self._custom_rules = [self._normalize_rule(item) for item in rules[:60]]
            self._custom_rules = [rule for rule in self._custom_rules if rule]

    def _persist(self) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        temp = self._state_file.with_suffix(".tmp")
        temp.write_text(
            json.dumps(
                {"disabled": sorted(self._disabled), "rules": self._custom_rules},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temp, self._state_file)

    @staticmethod
    def _normalize_rule(params) -> Optional[Dict]:
        params = params if isinstance(params, dict) else {}
        rule_id = str(params.get("id", "")).strip()[:40]
        keywords = [
            str(item).strip()
            for item in (params.get("keywords") or [])
            if str(item).strip()
        ][:40]
        if not rule_id or not keywords:
            return None
        templates = [
            str(item).strip()
            for item in (params.get("templates") or [])
            if str(item).strip()
        ][:10]
        return {
            "id": rule_id,
            "label": str(params.get("label", rule_id)).strip()[:30] or rule_id,
            "category": str(params.get("category", "")).strip()[:24],
            "keywords": keywords,
            "exclude": [
                str(item).strip()
                for item in (params.get("exclude") or [])
                if str(item).strip()
            ][:20],
            "templates": templates,
            "no_product": str(params.get("no_product", "")).strip()[:200],
            "custom": True,
        }

    def set_disabled(self, intent_ids) -> Dict:
        """停用若干意图（内置与自定义都可停用）。"""
        values = intent_ids if isinstance(intent_ids, (list, tuple, set)) else []
        with self._lock:
            self._disabled = {str(item).strip() for item in values if str(item).strip()}
            self._persist()
        return self.status()

    def upsert_rule(self, params: Dict) -> Dict:
        rule = self._normalize_rule(params)
        if not rule:
            raise ValueError("自定义意图需要同时提供 id 和 keywords")
        with self._lock:
            existing = next(
                (item for item in self._custom_rules if item["id"] == rule["id"]), None
            )
            if existing:
                self._custom_rules[self._custom_rules.index(existing)] = rule
            else:
                if len(self._custom_rules) >= 60:
                    raise ValueError("自定义意图最多 60 条")
                self._custom_rules.append(rule)
            self._persist()
        return self.status()

    def delete_rule(self, rule_id: str) -> Dict:
        rule_id = str(rule_id or "").strip()
        with self._lock:
            before = len(self._custom_rules)
            self._custom_rules = [r for r in self._custom_rules if r["id"] != rule_id]
            if len(self._custom_rules) == before:
                raise ValueError("自定义意图不存在")
            self._persist()
        return self.status()

    # ── 匹配 ────────────────────────────────────────────────────────

    def active_rules(self) -> List[Dict]:
        """自定义规则优先于内置规则。"""
        with self._lock:
            custom = list(self._custom_rules)
            disabled = set(self._disabled)
        builtin = [
            {**rule, "custom": False}
            for rule in INTENT_RULES
            if rule["id"] not in disabled
        ]
        return [rule for rule in custom if rule["id"] not in disabled] + builtin

    @staticmethod
    def _render(template: str, context: Dict) -> str:
        needed = _PLACEHOLDER.findall(template)
        if any(not context.get(name) for name in needed):
            return ""
        try:
            return template.format(**context)
        except (KeyError, IndexError):
            return ""

    @staticmethod
    def _speech_trim(text: str, limit: int) -> str:
        """把字段裁到适合口播的长度，尽量在标点处断开。"""
        text = str(text or "").strip()
        if len(text) <= limit:
            return text
        window = text[:limit]
        for mark in ("，", "。", "；", "、", ",", ".", ";", " "):
            cut = window.rfind(mark)
            if cut >= limit // 2:
                return window[:cut].strip()
        return window.strip()

    @classmethod
    def _build_context(cls, product: Optional[Dict]) -> Dict:
        """构造话术模板的填充字段。

        注意：这里对口播字段做了截断。商品资料里的 usage/audience 最长可达
        300 字，直接口播会让单条回应长达几十秒，把吞吐拖垮。
        详细内容交给商品卡，口播只给要点。
        """
        product = product or {}
        points = product.get("selling_points") or []
        return {
            "name": cls._speech_trim(product.get("name", ""), 16),
            "price": cls._speech_trim(product.get("price", ""), 16),
            "coupon": cls._speech_trim(product.get("coupon", ""), 14),
            "points": cls._speech_trim("；".join(str(item) for item in points[:3]), 24),
            "audience": cls._speech_trim(product.get("target_audience", ""), 18),
            "usage": cls._speech_trim(product.get("usage", ""), 22),
        }

    def match(self, text: str, product: Optional[Dict] = None,
              use_flavor: bool = True) -> Optional[Dict]:
        """匹配一条评论行。

        返回 ``{"id","label","response","keyword","category"}``；未命中返回 ``None``。
        多个规则命中时，命中词更长的规则胜出（更具体者优先）。
        """
        normalized = normalize_comment(text)
        if len(normalized) < 2:
            return None

        best_rule: Optional[Dict] = None
        best_keyword = ""
        for rule in self.active_rules():
            # 排除词优先：命中排除词的规则整条跳过，避免「主播多大了」被当成问规格
            excluded = rule.get("exclude") or []
            if any(
                normalize_comment(word) and normalize_comment(word) in normalized
                for word in excluded
            ):
                continue
            for keyword in rule.get("keywords") or []:
                keyword_norm = normalize_comment(keyword)
                if keyword_norm and keyword_norm in normalized:
                    if len(keyword_norm) > len(best_keyword):
                        best_rule, best_keyword = rule, keyword_norm

        # 关键词全未命中时，启用「疑问词+对象」的模式匹配（口语变体兜底）
        if best_rule is None:
            by_id = {rule["id"]: rule for rule in self.active_rules()}
            for intent_id, pattern in PATTERN_RULES:
                rule = by_id.get(intent_id)
                if not rule:
                    continue
                excluded = rule.get("exclude") or []
                if any(
                    normalize_comment(word) and normalize_comment(word) in normalized
                    for word in excluded
                ):
                    continue
                found = pattern.search(normalized)
                if found:
                    best_rule, best_keyword = rule, found.group(0)
                    break

        if best_rule is None:
            return None

        context = self._build_context(product)
        response = ""
        for template in best_rule.get("templates") or []:
            response = self._render(template, context)
            if response:
                break
        if not response:
            response = self._render(best_rule.get("no_product", "") or "", context)
        if not response:
            response = "这个问题请以直播间商品卡和客服说明为准。"

        # 统一走合规过滤：命中风险表述时自动降级为安全兜底话术
        response = sales_compliance.safe_response(response, product)

        category = str(best_rule.get("category", "") or "")
        if use_flavor and category:
            response = local_flavor.decorate(response, category)

        with self._lock:
            self._hit_counter[best_rule["id"]] = self._hit_counter.get(best_rule["id"], 0) + 1

        return {
            "id": best_rule["id"],
            "label": best_rule.get("label", best_rule["id"]),
            "response": response,
            "keyword": best_keyword,
            "category": category,
        }

    def match_lines(self, lines, product: Optional[Dict] = None,
                    use_flavor: bool = True) -> Optional[Dict]:
        """在多行 OCR 结果中找第一条能命中的评论。

        先长文本后短文本：较长的行信息量更大，命中更可靠。
        """
        candidates = [str(line).strip() for line in (lines or []) if str(line).strip()]
        candidates.sort(key=len, reverse=True)
        for line in candidates:
            hit = self.match(line, product, use_flavor=use_flavor)
            if hit:
                hit["source_text"] = line
                return hit
        return None

    # ── 话术清点（供音频预热与人工复核）──────────────────────────────

    def collect_phrases(self, products=None, use_flavor: bool = True) -> List[str]:
        """列出所有**可能被实际播报**的最终话术，含口头禅变体。

        用于 TTS 音频预热：把这份清单全部合成进缓存后，
        高频问题即可在断网状态下零延迟播报。
        """
        items = [(None, self._build_context(None))]
        for product in (products or []):
            items.append((product, self._build_context(product)))
        flavor_map = local_flavor.status()["categories"]

        phrases: List[str] = []

        def add(text: str) -> None:
            text = str(text or "").strip()
            if text and text not in phrases:
                phrases.append(text)

        for rule in self.active_rules():
            category = str(rule.get("category", "") or "")
            flavor_variants = (flavor_map.get(category) or []) if (use_flavor and category) else []
            for product, context in items:
                rendered = [
                    self._render(template, context)
                    for template in (rule.get("templates") or [])
                ]
                rendered = [text for text in rendered if text]
                if not rendered:
                    fallback = self._render(rule.get("no_product", "") or "", context)
                    if fallback:
                        rendered = [fallback]
                for text in rendered:
                    text = sales_compliance.safe_response(text, product)
                    add(text)
                    for phrase in flavor_variants:
                        if phrase and phrase not in text and len(phrase) <= 10:
                            add(f"{phrase}，{text}")
        return phrases

    # ── 状态 ────────────────────────────────────────────────────────

    def reset_stats(self) -> None:
        with self._lock:
            self._hit_counter = {}

    def status(self) -> Dict:
        with self._lock:
            hits = dict(self._hit_counter)
            custom_count = len(self._custom_rules)
            disabled = sorted(self._disabled)
        rules = self.active_rules()
        return {
            "enabled_rules": len(rules),
            "builtin_rules": len(INTENT_RULES),
            "custom_rules": custom_count,
            "disabled": disabled,
            "hit_counts": hits,
            "total_hits": sum(hits.values()),
            "intents": [
                {"id": rule["id"], "label": rule.get("label", rule["id"]),
                 "keywords": len(rule.get("keywords") or []),
                 "custom": bool(rule.get("custom"))}
                for rule in rules
            ],
            "flavor": local_flavor.status(),
        }


intent_router = IntentRouter()