"""Rule-layer reading of what users commonly ask for beyond device type, budget and capacities.

Three things are read from one message, deterministically:

* ``stated_constraints``  requirements the catalogue can enforce (maker, chip, platform, store, Wi-Fi,
  laptop screen ...). They are stored in ``hard_constraints`` and checked by validation.
* ``budget_amount``       budgets written as "2.5k", "一万", a range, or in another currency.
* ``unsupported_requests`` wishes nothing in the catalogue or planner can act on (weight, delivery,
  a monitor ...), so the reply can say so instead of silently dropping them.
"""
from __future__ import annotations

import re

from backend.attributes import GPU_CHIP, LABELS, active, gpu_model

NEG = (r"(?:avoid|no|not|never|don'?t want|do not want|without|exclude|except|anything but|hate|dislike|don'?t like|do not like|"
       r"not a fan of|stay away from|不要|别用|避开|除了|不用|不喜欢|讨厌)\s*(?:any(?:thing)?\s+|an?\s+|from\s+|the\s+)?")
# A feature the user says they can do without: "I don't need Wi-Fi", "no need for OLED", "不需要无线".
NO_NEED = r"(?:(?:don'?t|do not|doesn'?t|does not|no) need(?: for)?|no need for|不需要)\s*(?:any\s+|an?\s+|the\s+)?"
NOT_NEEDED = r"\s*(?:is|are)?\s*(?:not (?:needed|necessary|required|important)|unnecessary|isn'?t (?:needed|necessary))"
# The words that say which part of a desktop a maker is wanted for ("an ASUS motherboard", "Corsair RAM").
PART_WORDS = {
    "motherboard": r"motherboard|mainboard|mobo|主板", "ram": r"ram|memory|内存", "ssd": r"ssd|nvme|固态(?:硬盘)?",
    "psu": r"psu|power supply|电源", "case": r"case|chassis|机箱", "cooler": r"cooler|aio|散热器?",
    "gpu": r"graphics card|video card|gpu|显卡",
}
BRAND_ALIASES = {
    "asus": "asus", "rog": "asus", "华硕": "asus", "msi": "msi", "微星": "msi", "gigabyte": "gigabyte", "技嘉": "gigabyte",
    "asrock": "asrock", "corsair": "corsair", "lian li": "lian li", "cooler master": "cooler master", "nzxt": "nzxt",
    "deepcool": "deepcool", "thermalright": "thermalright", "silverstone": "silverstone", "fractal": "fractal design",
    "phanteks": "phanteks", "thermaltake": "thermaltake", "seasonic": "seasonic", "kingston": "kingston",
    "samsung": "samsung", "三星": "samsung", "western digital": "wd", "crucial": "crucial", "sandisk": "sandisk",
    "kioxia": "kioxia", "lexar": "lexar", "adata": "adata", "g.skill": "g.skill", "xfx": "xfx",
    "lenovo": "lenovo", "thinkpad": "lenovo", "联想": "lenovo", "dell": "dell", "戴尔": "dell", "hp": "hp", "惠普": "hp",
    "acer": "acer", "宏碁": "acer", "apple": "apple", "macbook": "apple", "苹果": "apple", "razer": "razer",
    "huawei": "huawei", "华为": "huawei", "microsoft": "microsoft", "surface": "microsoft",
}
LAPTOP_BRANDS = {"asus", "msi", "gigabyte", "lenovo", "dell", "hp", "acer", "apple", "razer", "huawei", "microsoft", "samsung"}
FOREIGN_CURRENCY = re.compile(r"\busd\b|us\$|\brmb\b|\bcny\b|人民币|美元|美金|\bmyr\b|ringgit|马币|\beur\b|欧元|\beuros?\b|\byuan\b|"
                              r"日元|\bjpy\b|港币|\bhkd\b|£|€|¥|\bpounds?\b|\brupees?\b|\binr\b")
BUDGET_WORD = r"(?:预算|budget|spend|around|about|roughly|under|up to|max(?:imum)?|within|below|s\$|sgd|\$)"
CHINESE_DIGIT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
PRODUCT_MESSAGE = re.compile(r"^\s*i want to (?:keep|buy) the ", re.I)      # sent by the lock and choice buttons
OWNERSHIP = re.compile(r"already (?:have|own|got|has)|\bi (?:have|own)\b|i've got|\bre-?use\b|\bexisting\b|"
                       r"\bmy (?:old|current|own)\b|已有|已经有|我有|现有|旧的", re.I)


# Amounts that are not the overall maximum: "S$800 on the graphics card", "spend at least S$2,500".
NOT_THE_BUDGET = re.compile(
    r"(?:s\$|sgd|\$)?\s*\d[\d,]*\s+(?:on|for)\s+(?:the\s+|an?\s+)?(?:graphics card|gpu|cpu|processor|case|monitor|motherboard|ram|memory|ssd|psu|power supply|cooler)"
    r"|(?:at least|no less than|minimum of)\s+(?:s\$|sgd|\$)\s*\d[\d,]*")


def shorthand(lower: str) -> str:
    """Chinese-style capacity shorthand: "内存32G，硬盘2T" means 32GB and 2TB. Not applied to "8C/16T"."""
    if not re.search(r"[一-鿿]", lower):
        return lower
    lower = re.sub(r"(?<![/\d])(\d{1,4})\s*g(?![a-z])", r"\1gb", lower)
    return re.sub(r"(?<![/\d])(\d{1,2})\s*t(?![a-z])", r"\1tb", lower)


def budget_amount(lower: str, normalized: str) -> tuple[int | None, bool]:
    """(amount, foreign) for budgets the plain patterns miss: a range (its upper end), "2.5k", "一万", "3千".
    ``foreign`` is True when the message names a currency other than the Singapore dollar."""
    foreign = bool(FOREIGN_CURRENCY.search(lower)) and not re.search(r"新币|新加坡元|\bsgd\b|s\$", lower)
    spread = re.search(rf"(?:between|from|{BUDGET_WORD})[^.;\d]{{0,20}}?(?:s\$|sgd|\$)?\s*([1-9]\d{{2,5}})\s*"
                       r"(?:-|–|—|~|to|and|到|至)\s*(?:s\$|sgd|\$)?\s*([1-9]\d{2,5})(?!\s*(?:gb|tb|w\b|hz|mhz))", normalized)
    if spread:
        return max(int(spread.group(1)), int(spread.group(2))), foreign
    thousands = BUDGET_IN_K.search(normalized)
    if thousands:
        return int(float(thousands.group(1) or thousands.group(2)) * 1000), foreign
    if re.search(r"预算|价位|以内|左右|块|元|新币", lower):
        # "一万五" is 15,000 and "三千五" is 3,500: a trailing digit counts in the next unit down.
        chinese = re.search(r"(\d+(?:\.\d+)?|[一二两三四五六七八九十])\s*([万千])\s*(?:([一二两三四五六七八九\d])(?![\d百万])千?)?", lower)
        if chinese:
            unit = 10000 if chinese.group(2) == "万" else 1000
            number = CHINESE_DIGIT.get(chinese.group(1)) or float(chinese.group(1))
            tail = chinese.group(3)
            extra = (CHINESE_DIGIT.get(tail) or int(tail)) * unit // 10 if tail else 0
            return int(number * unit) + extra, foreign
    return None, foreign


def loose_budget(normalized: str) -> int | None:
    """An amount with only a weak cue ("I can spend around 2000", "I have 2500 to spend", "1500 euros"),
    tried after every explicit budget wording has failed."""
    loose = re.search(r"(?:spend(?:ing)?|around|roughly|approximately|afford|~)\s*(?:up to\s+|about\s+)?(?:s\$|sgd|\$)?\s*([1-9]\d{2,5})"
                      r"(?!\d|\.\d|\s*(?:gb|tb|w\b|hz|mhz|mm|p\b|x|k\b|ti\b|xt\b|super|rpm|inch))", normalized) \
        or re.search(r"(?<![\w$.])([1-9]\d{2,5})\s+(?:to spend|budget\b|max\b|maximum\b|tops\b|all in\b)", normalized) \
        or re.search(r"([1-9]\d{2,5})\s*(?:euros?|eur|pounds|ringgit|myr|hkd|yuan|yen|jpy|cny)\b", normalized)
    if loose:
        return int(loose.group(1))
    bare = re.search(r"(?<![\w.$])(\d(?:\.\d)?)\s*k\b(?!\s*(?:resolution|monitor|display|video|hdr|@|hz))", normalized)
    if bare and float(bare.group(1)) not in (2, 4, 8) and float(bare.group(1)) >= 0.5:
        return int(float(bare.group(1)) * 1000)
    return None


# "budget 2.5k", "3k budget": thousands, unless the k belongs to a resolution ("4k gaming").
BUDGET_IN_K = re.compile(rf"{BUDGET_WORD}[^.;\d]{{0,20}}?(\d+(?:\.\d+)?)\s*k\b(?!\s*(?:resolution|gaming|monitor|display|video|hdr|@))"
                         r"|(?<![\w.])(\d+(?:\.\d+)?)\s*k\s+(?:budget|max\b|maximum|sgd|dollars|to spend|limit)")


def _negated(pattern: str, lower: str) -> bool:
    return bool(re.search(NEG + f"(?:{pattern})", lower))


def _declined(pattern: str, lower: str) -> bool:
    """The feature is named only to say it is not wanted or not needed."""
    return bool(re.search(rf"(?:{NEG}|{NO_NEED})(?:{pattern})|(?:{pattern}){NOT_NEEDED}", lower))


def _alias(alias: str) -> str:
    return re.escape(alias) if re.search(r"[^\x00-\x7f]", alias) else rf"\b{re.escape(alias)}\b"


_FILLER = r"(?:(?!(?:in|for|and|or|with|the|an?|to|is|but|on|of)\b)[a-z0-9.+-]+\s+){0,3}?"      # "ASUS ROG Strix <part>"


def _part_brands(lower: str) -> dict[str, list[str]]:
    """Makers wanted for one part of a desktop: "an ASUS motherboard", "RAM from Corsair", "三星的固态"."""
    out: dict[str, list[str]] = {}
    others = "|".join(_alias(a) for a in BRAND_ALIASES)
    for alias, brand in BRAND_ALIASES.items():
        name = _alias(alias)
        if _negated(name, lower):
            continue
        for category, words in PART_WORDS.items():
            if re.search(rf"{name}(?:\s*(?:,|/|or|或)\s*(?:{others}))*\s*(?:的)?\s*{_FILLER}(?:{words})(?![a-z])|"
                         rf"(?:{words})\s+(?:from|by|made by)\s+(?:(?:{others})\s*(?:,|/|or|或)\s*)*{name}", lower) \
                    and brand not in out.setdefault(category, []):
                out[category].append(brand)
    return {category: brands for category, brands in out.items() if brands}


def _brands(lower: str) -> tuple[list[str], list[str]]:
    """(wanted, unwanted) makers named in the message."""
    wanted, unwanted = [], []
    for alias, brand in BRAND_ALIASES.items():
        pattern = _alias(alias)
        if not re.search(pattern, lower):
            continue
        target = unwanted if _negated(pattern, lower) else wanted
        if brand not in target:
            target.append(brand)
    return wanted, unwanted


def removals(lower: str) -> list[str]:
    """Constraint keys the message asks to drop: "Remove the graphics card brand requirement."."""
    def dropped(topic: str) -> bool:
        return bool(re.search(rf"(?:remove|drop|forget|ignore|cancel)\s+(?:the\s+|my\s+)?{topic}\s+(?:requirement|preference|restriction)|"
                              rf"any\s+{topic}\s+(?:is|will be|would be)\s+(?:fine|ok|okay)|"
                              rf"{topic}\s+(?:does not|doesn'?t)\s+matter|no\s+{topic}\s+preference", lower))
    return [key for key, (topic, _) in LABELS.items() if dropped(re.escape(topic.casefold()))]


# A chip named without its family ("at least a 5070", "a 9070 XT or better"): GeForce 20-50 series, Radeon 6000-9000.
BARE_CHIP = re.compile(r"(?<![a-z0-9$.,])(?:([2-5]0[5-9]0)|([67][6-9]00|90[67]0))(?:\s*(ti\s*super|ti|super|xtx|xt))?(?![a-z0-9]|\.\d)")
EITHER = r"\s*(?:or|vs\.?|/|或者?|还是)\s*(?:an?\s+)?"


def stated_constraints(text: str, device_type: str | None) -> dict:
    """Hard constraints named in one message. A value of None means "this is no longer required"."""
    lower, found = shorthand(text.casefold()), {}
    if PRODUCT_MESSAGE.search(text):       # a product name is full of such words; it is not a new requirement
        return found
    desktop = device_type in ("desktop", "compare", None)
    laptop = device_type in ("laptop", "compare")

    nvidia, radeon = r"nvidia|geforce|n卡|英伟达", r"radeon|a卡|amd\s*(?:的)?\s*(?:graphics|gpu|video|card|显卡)"
    if re.search(rf"(?:nvidia|geforce){EITHER}(?:amd|radeon)|(?:amd|radeon){EITHER}(?:nvidia|geforce)", lower):
        found["gpu_vendor"] = None            # "NVIDIA or AMD, I don't mind" asks for neither
    elif _negated(nvidia, lower):
        found["gpu_vendor"] = "amd"
    elif re.search(nvidia, lower):
        found["gpu_vendor"] = "nvidia"
    elif _negated(radeon, lower):
        found["gpu_vendor"] = "nvidia"
    elif re.search(radeon, lower):
        found["gpu_vendor"] = "amd"

    chips = [(m, m.group(0)) for m in GPU_CHIP.finditer(text)]
    chips += [(m, f"{'rtx' if m.group(1) else 'rx'} {m.group(0)}") for m in BARE_CHIP.finditer(lower)]
    for match, name in chips:
        before, after = lower[max(0, match.start() - 28):match.start()], lower[match.end():match.end() + 22]
        at_least = re.search(r"(?:at least|minimum|no (?:lower|less|worse) than|不低于|至少|起码)\s*(?:an?\s+|the\s+)?$", before)
        or_better = re.match(r"\s*(?:or (?:better|above|higher|newer|more)|and (?:above|up)|\+|以上|起步|及以上|或更高|或以上)", after)
        if at_least or or_better:
            found["minimum_gpu_model"] = gpu_model(name)
            found["gpu_vendor"] = found["minimum_gpu_model"]["vendor"]
            break

    if re.search(r"(?:don'?t|do not|no) need (?:for )?(?:an? |any )?(?:dedicated |discrete )?(?:graphics card|gpu|video card)|"
                 r"(?:without|no) (?:an? )?(?:dedicated |discrete )?(?:graphics card|gpu|video card)|"
                 r"integrated graphics (?:is|are|will be) (?:fine|enough|ok|okay)|不需要(?:独立)?显卡|不要(?:独立)?显卡|核显就(?:行|够|可以)", lower):
        if desktop and not any(found.values()):
            found.update(no_graphics_card=True, gpu_vendor=None, minimum_gpu_model=None, minimum_gpu_memory_gb=None)
    elif found.get("gpu_vendor") or found.get("minimum_gpu_model"):
        found["no_graphics_card"] = None

    intel = r"intel|英特尔|酷睿|\bi[3579]\b|\bcore\s?(?:i[3579]|ultra)"
    amd_cpu = r"amd\s*(?:的)?\s*(?:cpu|processor|处理器|平台|platform|build|system|setup)|ryzen|锐龙|\bam[45]\b"
    # "No AMD" with no part named: the processor is the part every build has.
    bare_amd = r"amd(?!\s*(?:的)?\s*(?:cpu|processor|处理器|平台|platform|graphics|gpu|video|card|显卡|radeon|ryzen))"
    if re.search(rf"intel{EITHER}amd|amd{EITHER}intel", lower):
        found["cpu_maker"] = None
    elif _negated(intel, lower):
        found["cpu_maker"] = "amd"
    elif re.search(intel, lower) and not re.search(r"intel\s+arc", lower):
        found["cpu_maker"] = "intel"
    elif _negated(amd_cpu, lower):
        found["cpu_maker"] = "intel"
    elif re.search(amd_cpu, lower):
        found["cpu_maker"] = "amd"
    elif _negated(bare_amd, lower) and "gpu_vendor" not in found:
        found["cpu_maker"] = "intel"

    tier = re.search(r"\bi([3579])\b|\bcore\s?i([3579])|\bultra\s?([3579])\b|ryzen\s?([3579])\b|锐龙\s?([3579])", lower)
    if tier:          # "an i7", "Ryzen 7 9800X3D": that class or a higher one
        found["minimum_cpu_tier"] = int(next(g for g in tier.groups() if g))
    socket = re.search(r"\b(am[45])\b|lga\s?-?(1700|1851)\b", lower)
    if socket:
        found["socket"] = socket.group(1).upper() if socket.group(1) else f"LGA{socket.group(2)}"
    memory = re.search(r"\bddr\s?([45])\b", lower)
    if memory:
        found["memory_type"] = f"DDR{memory.group(1)}"
    watts = (re.search(r"(?:psu|power supply|电源)[^.;\d]{0,30}?(\d{3,4})\s*w\b", lower)
             or re.search(r"(\d{3,4})\s*w(?:atts?)?\b(?:\s+[a-z0-9+]+){0,3}?\s*(?:psu|power supply|电源)", lower))
    if watts and not OWNERSHIP.search(text) and 300 <= int(watts.group(1)) <= 2200:
        found["minimum_psu_watts"] = int(watts.group(1))
    if re.search(r"mini[- ]?itx|\bitx\b", lower):
        found["maximum_form_factor"] = "Mini-ITX"
    elif re.search(r"micro[- ]?atx|\bm-?atx\b", lower):
        found["maximum_form_factor"] = "Micro-ATX"
    wifi = r"wi-?fi|wireless (?:lan|network|card|internet)|无线网|无线上网"
    if _declined(wifi, lower):
        found["wifi"] = None
    elif re.search(wifi, lower) and desktop:
        found["wifi"] = True
    liquid, air = r"liquid[- ]cool\w*|water[- ]?cool\w*|\baio\b|水冷", r"air[- ]cool\w*|风冷"
    if _declined(liquid, lower):
        found["cooler_kind"] = "air"
    elif re.search(liquid, lower):
        found["cooler_kind"] = "liquid"
    elif _declined(air, lower):
        found["cooler_kind"] = "liquid"
    elif re.search(air, lower):
        found["cooler_kind"] = "air"

    store = re.search(r"(?:only|just)\s+(?:from|at|in)\s+(dynacore|vii\s?pc)|(dynacore|vii\s?pc)\s+only|只(?:要|在|从|买)?\s*(dynacore|vii\s?pc)", lower)
    if store:
        found["stores"] = ["vii pc" if "vii" in next(g for g in store.groups() if g) else "dynacore"]
    wanted, unwanted = _brands(lower)
    if unwanted:
        found["excluded_brands"] = unwanted
    if re.search(r"any brand|no brand preference|brand (?:does not|doesn'?t) matter|不限品牌|品牌无所谓", lower):
        found.update(excluded_brands=None, laptop_brands=None)

    parts = _part_brands(lower)
    if parts:
        found["part_brands"] = parts
    if laptop:
        named_for_a_part = {brand for brands in parts.values() for brand in brands}      # "with a Samsung SSD"
        makers = [b for b in wanted if b in LAPTOP_BRANDS and b not in named_for_a_part]
        if makers:
            found["laptop_brands"] = makers
        inch = r"\s*(?:-?\s*inch(?:es)?\b|\"|”|英寸|寸)"
        span = re.search(rf"(\d{{2}})\s*(?:or|to|-|–|~|到|或)\s*(\d{{2}}(?:\.\d)?){inch}", lower)
        size = re.search(rf"(\d{{2}}(?:\.\d)?){inch}", lower)
        if span and 10 <= int(span.group(1)) <= float(span.group(2)) < 20:
            found["laptop_screen_inches"] = [float(span.group(1)), int(float(span.group(2))) + 0.99]
        elif size and 10 <= float(size.group(1)) < 20:
            inches = int(float(size.group(1)))
            before, after = lower[max(0, size.start() - 26):size.start()], lower[size.end():size.end() + 18]
            if re.search(r"(?:at least|minimum|no smaller than|(?:bigger|larger) than|至少|不小于)\s*(?:an?\s+)?$", before) \
                    or re.match(r"\s*(?:screen\s+)?(?:or (?:larger|bigger|more|above)|and (?:above|up)|\+|以上)", after):
                found["laptop_screen_inches"] = [float(inches), 19.99]
            elif re.search(r"(?:under|at most|no (?:bigger|larger) than|smaller than|below|up to|不超过|最大)\s*(?:an?\s+)?$", before) \
                    or re.match(r"\s*(?:screen\s+)?(?:or (?:smaller|less|below)|以下|以内)", after):
                found["laptop_screen_inches"] = [10.0, inches + 0.99]
            else:
                found["laptop_screen_inches"] = [float(inches), inches + 0.99]
        if _declined(r"\boled\b", lower):
            found["laptop_oled"] = None
        elif re.search(r"\boled\b", lower):
            found["laptop_oled"] = True
        dedicated = r"(?:dedicated|discrete) (?:graphics(?: card)?|gpu)|独显|独立显卡"
        if _declined(dedicated, lower):
            found["laptop_dedicated_gpu"] = None
        elif re.search(dedicated, lower):
            found["laptop_dedicated_gpu"] = True
    for key in removals(lower):
        found[key] = None
    return found


SOFT_PREFERENCES = {
    "80 Plus Gold power supply": r"80\s*(?:plus|\+)\s*gold|金牌电源|\bgold\b[a-z -]{0,12}(?:psu|power supply)",
    "lowest price": r"cheapest|as cheap as possible|lowest price|最便宜|越便宜越好",
    "NVMe SSD": r"\bnvme\b",
    "room to upgrade later": r"future[- ]?proof|upgrad(?:e|able|ability)|以后升级|方便升级",
    "4K": r"\b4k\b|2160p", "1440p": r"\b1440p\b|\b2k\b|\bqhd\b", "1080p": r"\b1080p\b|\bfhd\b",
    "high refresh rate": r"\b(?:1[2-9]\d|2[0-9]\d|360)\s*hz\b|高刷",
    "tempered glass side panel": r"glass (?:side )?panel|侧透",
}
NO_RGB = re.compile(r"(?:no|without|not?|zero|don'?t (?:want|need|like)|do not (?:want|need|like)|no need for|not a fan of|hate|dislike|不要|不喜欢|没有|无)\s*(?:any\s+)?(?:rgb|灯效|灯光|灯\b|灯$|lighting|lights)", re.I)

# topic -> pattern. Things no listing field and no planning step can honour.
UNSUPPORTED = {
    "a monitor": r"\bmonitor\b|\bdisplay\b|显示器",
    "a keyboard, mouse or other peripherals": r"keyboard|\bmouse\b|headset|speakers?\b|webcam|键盘|鼠标|耳机",
    "an operating system or software licence": r"windows(?: 1[01])?|operating system|\bos\b|office licen[cs]e|系统|正版",
    "delivery or assembly": r"deliver|shipping|ship it|this week|tomorrow|today|assembl|送货|配送|装好",
    "warranty terms": r"warranty|保修",
    "a number of processor cores": r"\d+\s*-?\s*cores?\b|\d+\s*threads?\b|\d+\s*核",
    "a weight limit": r"\d(?:\.\d)?\s*kg\b|weigh|公斤|重量",
    "battery life": r"battery|续航|电池",
    "a second drive": r"hard dis[ck]|\bhdd\b|(?:two|2)\s+(?:\d+\s*[gt]b\s+)?(?:drives|ssds)|second drive|second ssd|机械硬盘|两块硬盘",
    "a minimum spend": r"(?:spend|use|cost)\s+at least|at least\s+(?:s\$|\$)\s*\d|no less than\s+(?:s\$|\$)",
    "a budget for a single part": r"(?:s\$|\$)\s*\d[\d,]*\s+(?:on|for)\s+the\s+(?:graphics card|gpu|cpu|processor|case|monitor)",
    "the number of options": r"\b(?:two|three|four|five|\d)\s+(?:options|choices|builds|alternatives)\b|几个方案",
    "used or refurbished parts": r"second[- ]hand|pre-?owned|\bused\b(?!\s+(?:for|to|in|with|as|by|mainly|mostly|only|primarily))|refurbished|二手",
    "Bluetooth": r"bluetooth|蓝牙",
    "a touchscreen": r"touch\s?screen|触屏|触摸屏",
    "particular ports": r"thunderbolt|usb-?c\b|\bhdmi\b|displayport|\bports?\b|接口",
    "a motherboard chipset": r"\b(?:a[56]20|b[4-8][56]0|x[5-8]70|h[6-8]10|z[6-8]90)[em]?\b",
}


def unsupported_requests(text: str, device_type: str | None) -> list[str]:
    """Wishes in the message that the recommendation cannot honour, in words for the reply."""
    lower = text.casefold()
    if PRODUCT_MESSAGE.search(text):
        return []
    found = [topic for topic, pattern in UNSUPPORTED.items() if re.search(pattern, lower)]
    if device_type == "laptop":          # a laptop has its own screen and keyboard
        found = [t for t in found if t not in ("a monitor",)]
    if re.search(r"without (?:a )?monitor|no monitor|不包含显示器", lower) and "a monitor" in found:
        found.remove("a monitor")
    return found


def inapplicable(text: str, requirements: dict) -> list[str]:
    """Requirements named in this message that do not apply to the chosen device ("an 850W power supply"
    for a laptop), in words for the reply."""
    device = requirements.get("device_type")
    live = active(requirements)
    return [f"{LABELS[key][1](value)} for a {device}" for key, value in stated_constraints(text, device).items()
            if value and key in LABELS and key not in live]
