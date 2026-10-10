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

from backend.attributes import GPU_CHIP, LABELS, gpu_model

NEG = r"(?:avoid|no|not|never|don'?t want|do not want|without|exclude|except|anything but|不要|别用|避开|除了|不用)\s*(?:any(?:thing)?\s+|an?\s+|from\s+|the\s+)?"
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
    thousands = re.search(rf"{BUDGET_WORD}[^.;\d]{{0,20}}?(\d+(?:\.\d+)?)\s*k\b(?!\s*(?:resolution|gaming|monitor|display|video|hdr|@))",
                          normalized)
    if thousands:
        return int(float(thousands.group(1)) * 1000), foreign
    if re.search(r"预算|价位|以内|左右|块|元|新币", lower):
        chinese = re.search(r"(\d+(?:\.\d+)?|[一二两三四五六七八九十])\s*([万千])", lower)
        if chinese:
            number = CHINESE_DIGIT.get(chinese.group(1)) or float(chinese.group(1))
            return int(number * (10000 if chinese.group(2) == "万" else 1000)), foreign
    return None, foreign


def _negated(pattern: str, lower: str) -> bool:
    return bool(re.search(NEG + f"(?:{pattern})", lower))


def _brands(lower: str) -> tuple[list[str], list[str]]:
    """(wanted, unwanted) makers named in the message."""
    wanted, unwanted = [], []
    for alias, brand in BRAND_ALIASES.items():
        pattern = re.escape(alias) if re.search(r"[^\x00-\x7f]", alias) else rf"\b{re.escape(alias)}\b"
        if not re.search(pattern, lower):
            continue
        target = unwanted if _negated(pattern, lower) else wanted
        if brand not in target:
            target.append(brand)
    return wanted, unwanted


def removals(lower: str) -> list[str]:
    """Constraint keys the message asks to drop: "Remove the graphics card brand requirement."."""
    return [key for key, (topic, _) in LABELS.items()
            if re.search(rf"(?:remove|drop|forget|ignore|cancel)\s+(?:the\s+|my\s+)?{re.escape(topic.casefold())}\s+requirement", lower)]


def stated_constraints(text: str, device_type: str | None) -> dict:
    """Hard constraints named in one message. A value of None means "this is no longer required"."""
    lower, found = shorthand(text.casefold()), {}
    if PRODUCT_MESSAGE.search(text):       # a product name is full of such words; it is not a new requirement
        return found
    desktop = device_type in ("desktop", "compare", None)
    laptop = device_type in ("laptop", "compare")

    nvidia, radeon = r"nvidia|geforce|n卡|英伟达", r"radeon|a卡|amd\s*(?:的)?\s*(?:graphics|gpu|video|card|显卡)"
    if _negated(nvidia, lower):
        found["gpu_vendor"] = "amd"
    elif re.search(nvidia, lower):
        found["gpu_vendor"] = "nvidia"
    elif _negated(radeon, lower):
        found["gpu_vendor"] = "nvidia"
    elif re.search(radeon, lower):
        found["gpu_vendor"] = "amd"

    for match in GPU_CHIP.finditer(text):
        before, after = lower[max(0, match.start() - 28):match.start()], lower[match.end():match.end() + 22]
        at_least = re.search(r"(?:at least|minimum|no (?:lower|less|worse) than|不低于|至少|起码)\s*(?:an?\s+|the\s+)?$", before)
        or_better = re.match(r"\s*(?:or (?:better|above|higher|newer|more)|and (?:above|up)|\+|以上|起步|及以上|或更高|或以上)", after)
        if at_least or or_better:
            found["minimum_gpu_model"] = gpu_model(match.group(0))
            found["gpu_vendor"] = found["minimum_gpu_model"]["vendor"]
            break

    if re.search(r"(?:don'?t|do not|no) need (?:an? |any )?(?:dedicated |discrete )?(?:graphics card|gpu|video card)|"
                 r"(?:without|no) (?:an? )?(?:dedicated |discrete )?(?:graphics card|gpu|video card)|"
                 r"integrated graphics (?:is|are|will be) (?:fine|enough|ok|okay)|不需要(?:独立)?显卡|不要(?:独立)?显卡|核显就(?:行|够|可以)", lower):
        if desktop and not found:
            found.update(no_graphics_card=True, gpu_vendor=None, minimum_gpu_model=None, minimum_gpu_memory_gb=None)
    elif found.get("gpu_vendor") or found.get("minimum_gpu_model"):
        found["no_graphics_card"] = None

    intel = r"intel|英特尔|酷睿"
    amd_cpu = r"amd\s*(?:的)?\s*(?:cpu|processor|处理器|平台|platform)|ryzen|锐龙|\bam[45]\b"
    if _negated(intel, lower):
        found["cpu_maker"] = "amd"
    elif re.search(intel, lower) and not re.search(r"intel\s+arc", lower):
        found["cpu_maker"] = "intel"
    elif _negated(r"amd\s*(?:的)?\s*(?:cpu|processor|处理器)|ryzen|锐龙", lower):
        found["cpu_maker"] = "intel"
    elif re.search(amd_cpu, lower):
        found["cpu_maker"] = "amd"

    socket = re.search(r"\b(am[45])\b|lga\s?-?(1700|1851)\b", lower)
    if socket:
        found["socket"] = socket.group(1).upper() if socket.group(1) else f"LGA{socket.group(2)}"
    memory = re.search(r"\bddr\s?([45])\b", lower)
    if memory:
        found["memory_type"] = f"DDR{memory.group(1)}"
    watts = (re.search(r"(?:psu|power supply|电源)[^.;\d]{0,30}?(\d{3,4})\s*w\b", lower)
             or re.search(r"(\d{3,4})\s*w(?:atts?)?\s*(?:or more\s+)?(?:psu|power supply|电源)", lower))
    if watts and not OWNERSHIP.search(text) and 300 <= int(watts.group(1)) <= 2200:
        found["minimum_psu_watts"] = int(watts.group(1))
    if re.search(r"mini[- ]?itx|\bitx\b", lower):
        found["maximum_form_factor"] = "Mini-ITX"
    elif re.search(r"micro[- ]?atx|\bm-?atx\b", lower):
        found["maximum_form_factor"] = "Micro-ATX"
    if re.search(r"wi-?fi|wireless (?:lan|network|card|internet)|无线网|无线上网", lower) and desktop:
        found["wifi"] = True
    if re.search(r"liquid[- ]cool|water[- ]?cool|\baio\b|水冷", lower):
        found["cooler_kind"] = "liquid"
    elif re.search(r"air[- ]cool|风冷", lower):
        found["cooler_kind"] = "air"

    store = re.search(r"(?:only|just)\s+(?:from|at|in)\s+(dynacore|vii\s?pc)|(dynacore|vii\s?pc)\s+only|只(?:要|在|从|买)?\s*(dynacore|vii\s?pc)", lower)
    if store:
        found["stores"] = ["vii pc" if "vii" in next(g for g in store.groups() if g) else "dynacore"]
    wanted, unwanted = _brands(lower)
    if unwanted:
        found["excluded_brands"] = unwanted
    if re.search(r"any brand|no brand preference|brand (?:does not|doesn'?t) matter|不限品牌|品牌无所谓", lower):
        found.update(excluded_brands=None, laptop_brands=None)

    if laptop:
        makers = [b for b in wanted if b in LAPTOP_BRANDS]
        if makers:
            found["laptop_brands"] = makers
        size = re.search(r"(\d{2}(?:\.\d)?)\s*(?:-?\s*inch(?:es)?\b|\"|”|英寸|寸)", lower)
        if size and 10 <= float(size.group(1)) < 20:
            inches = float(size.group(1))
            found["laptop_screen_inches"] = [float(int(inches)), int(inches) + 0.99]
        if re.search(r"\boled\b", lower):
            found["laptop_oled"] = True
        if re.search(r"dedicated (?:graphics|gpu)|discrete (?:graphics|gpu)|独显|独立显卡", lower):
            found["laptop_dedicated_gpu"] = True
    for key in removals(lower):
        found[key] = None
    return found


SOFT_PREFERENCES = {
    "80 Plus Gold power supply": r"80\s*(?:plus|\+)\s*gold|金牌电源",
    "NVMe SSD": r"\bnvme\b",
    "room to upgrade later": r"future[- ]?proof|upgrad(?:e|able|ability)|以后升级|方便升级",
    "4K": r"\b4k\b|2160p", "1440p": r"\b1440p\b|\b2k\b|\bqhd\b", "1080p": r"\b1080p\b|\bfhd\b",
    "high refresh rate": r"\b(?:1[2-9]\d|2[0-9]\d|360)\s*hz\b|高刷",
    "tempered glass side panel": r"glass (?:side )?panel|侧透",
}
NO_RGB = re.compile(r"(?:no|without|not?|zero|don'?t want|do not want|不要|没有|无)\s*(?:any\s+)?(?:rgb|灯效|灯光|灯\b|灯$|lighting|lights)", re.I)

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
    "a second drive": r"hard dis[ck]|\bhdd\b|two drives|second drive|second ssd|机械硬盘|两块硬盘",
    "a minimum spend": r"(?:spend|use|cost)\s+at least|at least\s+(?:s\$|\$)\s*\d|no less than\s+(?:s\$|\$)",
    "a budget for a single part": r"(?:s\$|\$)\s*\d[\d,]*\s+(?:on|for)\s+the\s+(?:graphics card|gpu|cpu|processor|case|monitor)",
    "the number of options": r"\b(?:two|three|four|five|\d)\s+(?:options|choices|builds|alternatives)\b|几个方案",
    "used or refurbished parts": r"second[- ]hand|\bused\b|refurbished|二手",
    "Bluetooth": r"bluetooth|蓝牙",
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
