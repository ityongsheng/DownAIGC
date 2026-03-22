"""
text_humanizer.py — 代码级反 AIGC 后处理器

核心策略（基于 AIGC 检测原理）：
1. 提升突发性（Burstiness）— 强制打散句子长度分布
2. 注入人类噪声 — 随机插入学术口语化表达
3. 打破 Transformer token 分布 — 低频同义词替换
4. 结构扰动 — 随机调整语序

这些操作是**确定性代码**执行的，不依赖 LLM，
因此不会携带 Transformer 的写作风格指纹。
"""

import re
import random


# ────────────────────── 同义词替换词典 ──────────────────────

# 高频 AI 词汇 → 低频人类学术替代词（多个选项）
SYNONYM_MAP: dict[str, list[str]] = {
    "进行了": ["开展了", "实施了", "着手进行了", "落实了"],
    "进行": ["加以", "予以", "施行", "展开"],
    "实现了": ["达成了", "完成了", "落地了", "兑现了"],
    "提出了": ["给出了", "提了", "构想了", "初步形成了"],
    "采用了": ["选用了", "沿用了", "引入了", "借用了"],
    "利用": ["借助", "凭借", "依托", "运用"],
    "基于": ["立足于", "依托", "以…为基础", "根植于"],
    "通过": ["经由", "借由", "凭借", "依靠"],
    "对于": ["就", "针对", "关于", "面对"],
    "同时": ["与此同时", "在此过程中", "伴随其间", "另外"],
    "因此": ["故而", "由此", "正因如此", "这使得"],
    "然而": ["但", "不过", "话虽如此", "尽管如此"],
    "此外": ["除此之外", "另需指出的是", "补充而言", "顺带一提"],
    "显著": ["明显", "可观", "相当程度上", "颇为突出"],
    "有效": ["切实", "较为理想", "具有实用价值", "行之有效"],
    "具有": ["带有", "兼具", "含有", "拥有"],
    "表明": ["说明", "印证了", "佐证了", "揭示出"],
    "验证": ["检验", "核实", "证实", "印证"],
    "分析": ["剖析", "解读", "审视", "考察"],
    "研究": ["探究", "考察", "审视", "探讨"],
    "方法": ["手段", "路径", "途径", "思路"],
    "模型": ["模型结构", "算法框架", "计算模型", "建模方案"],
    "数据": ["数据集", "样本", "观测值", "实验数据"],
    "结果": ["产出", "输出", "实验发现", "所得"],
    "实验": ["测试", "试验", "验证性实验", "对照实验"],
    "性能": ["表现", "效能", "运行指标", "效果"],
    "优化": ["改良", "调优", "提升", "改进"],
    "提高": ["提升", "改善", "拉高", "增强"],
    "降低": ["压低", "缩减", "削减", "拉低"],
    "算法": ["算法方案", "计算策略", "求解方法", "处理逻辑"],
    "框架": ["体系", "架构", "整体方案", "系统结构"],
    "本文": ["本研究", "笔者", "本项工作", "此次研究"],
    "综上所述": ["概言之", "总括以上讨论", "回顾上述内容"],
    "值得注意的是": ["需要强调的一点", "不应忽视的是", "一个细节在于"],
    "总的来说": ["归纳而言", "从整体来看", "综合来看"],
}

# 人类化插入短语（模拟真实本科生写作的犹豫和思考）
HUMAN_INSERTIONS = [
    "笔者认为",
    "据此推断",
    "从实践角度看",
    "这一点在后续章节中将进一步讨论",
    "某种程度上",
    "客观而言",
    "就目前的研究现状来看",
    "有必要指出",
    "不难发现",
    "换言之",
    "坦率地讲",
    "在一定条件下",
    "应当承认",
    "事实上",
    "从某种意义上看",
    "这并非偶然",
    "值得深思的是",
    "从学理视角审视",
    "毋庸赘述",
    "退一步说",
]

# 句首替换——打破 AI 喜欢用的固定句首
SENTENCE_STARTERS_AI = [
    "随着", "为了", "由于", "鉴于", "考虑到",
]

SENTENCE_STARTERS_HUMAN = [
    "伴随", "出于", "源于", "立足于", "着眼于",
    "回溯", "审视", "反观", "注意到", "不难发现",
]


# ────────────────────── 核心函数 ──────────────────────

def split_into_sentences(text: str) -> list[str]:
    """将文本切分为句子列表"""
    # 按中文/英文句末标点切分，保留标点
    parts = re.split(r'((?<=[。！？!?])\s*)', text)
    sentences = []
    current = ""
    for part in parts:
        current += part
        if re.search(r'[。！？!?]\s*$', current):
            sentences.append(current.strip())
            current = ""
    if current.strip():
        sentences.append(current.strip())
    return [s for s in sentences if s]


def boost_burstiness(text: str, intensity: float = 0.3) -> str:
    """
    提升突发性：随机拆分长句 / 合并短句，使句子长度分布更不均匀。

    intensity: 0.0~1.0，控制操作的概率
    """
    sentences = split_into_sentences(text)
    if len(sentences) <= 1:
        return text

    result = []
    i = 0
    while i < len(sentences):
        s = sentences[i]
        s_len = len(s)

        # 长句（> 60 字）→ 有概率在逗号处拆分
        if s_len > 60 and random.random() < intensity:
            # 找到靠中间的逗号/分号位置
            comma_positions = [m.start() for m in re.finditer(r'[，；,;]', s)]
            if comma_positions:
                # 选离中点最近的逗号
                mid = len(s) // 2
                best_pos = min(comma_positions, key=lambda p: abs(p - mid))
                # 只有两半都 > 15 字才拆
                if best_pos > 15 and (len(s) - best_pos) > 15:
                    part1 = s[:best_pos] + "。"
                    part2 = s[best_pos + 1:].strip()
                    # 确保第二部分首字大写或正常
                    result.append(part1)
                    result.append(part2)
                    i += 1
                    continue

        # 短句（< 20 字）+ 下一句也短 → 有概率合并
        if (s_len < 20 and i + 1 < len(sentences)
                and len(sentences[i + 1]) < 25
                and random.random() < intensity):
            next_s = sentences[i + 1]
            # 把第一句的句号换成逗号，合并
            merged = re.sub(r'[。！？]$', '，', s) + next_s
            result.append(merged)
            i += 2
            continue

        result.append(s)
        i += 1

    return "".join(result)


def replace_synonyms(text: str, replacement_rate: float = 0.25) -> str:
    """
    随机替换高频 AI 词汇为低频同义词。

    replacement_rate: 每个匹配的替换概率
    """
    result = text
    for ai_word, alternatives in SYNONYM_MAP.items():
        if ai_word in result and random.random() < replacement_rate:
            # 只替换第一个出现的（避免过度替换导致不自然）
            replacement = random.choice(alternatives)
            result = result.replace(ai_word, replacement, 1)
    return result


def inject_human_noise(text: str, injection_rate: float = 0.15) -> str:
    """
    随机在句间插入人类化表达。

    每个句子间有 injection_rate 的概率被插入一个短语。
    """
    sentences = split_into_sentences(text)
    if len(sentences) <= 2:
        return text

    result = []
    for i, s in enumerate(sentences):
        result.append(s)
        # 不在第一句和最后一句后插入
        if 0 < i < len(sentences) - 2 and random.random() < injection_rate:
            insertion = random.choice(HUMAN_INSERTIONS)
            # 把插入短语附加到下一句的句首
            # 这里只是标记，在组装时处理
            if i + 1 < len(sentences):
                next_s = sentences[i + 1]
                # 在下一句前面加上插入语
                sentences[i + 1] = f"{insertion}，{next_s[0].lower() if next_s else ''}{next_s[1:] if len(next_s) > 1 else ''}"

    return "".join(sentences)


def vary_sentence_starters(text: str) -> str:
    """替换 AI 常用的句首词"""
    result = text
    for ai_starter in SENTENCE_STARTERS_AI:
        # 只匹配句首的
        pattern = rf'(?<=[。！？\n])\s*{ai_starter}'
        if re.search(pattern, result):
            if random.random() < 0.4:
                replacement = random.choice(SENTENCE_STARTERS_HUMAN)
                result = re.sub(pattern, replacement, result, count=1)
    return result


def add_structural_variation(text: str) -> str:
    """
    增加结构变化：
    - 偶尔将"A，B，C" 列举改为不同格式
    - 在部分句子中调整定语/状语位置
    """
    # 把"A、B、C和D" 偶尔改为 "A 与 B，还有 C 以及 D"
    def vary_enumeration(match):
        items = match.group(0)
        if random.random() < 0.3:
            items = items.replace("、", "与", 1)
            if "、" in items:
                items = items.replace("、", "及", 1)
        return items

    result = re.sub(r'[\u4e00-\u9fff]+(?:、[\u4e00-\u9fff]+){2,}', vary_enumeration, text)
    return result


# ────────────────────── 主入口 ──────────────────────

def humanize_text(
    text: str,
    burstiness_intensity: float = 0.3,
    synonym_rate: float = 0.25,
    noise_rate: float = 0.12,
) -> str:
    """
    对 LLM 重写后的文本进行代码级人类化后处理。

    这是一个确定性（带随机种子）的代码处理流程，
    不依赖 LLM，因此不会携带 Transformer 写作指纹。

    处理顺序：
    1. 同义词替换（打破 AI 词汇偏好）
    2. 句首变化（打破 AI 句式固化）
    3. 突发性提升（打散句子长度分布）
    4. 人类噪声注入（模拟真实学术写作）
    5. 结构变化（列举方式多样化）
    """
    if not text or len(text.strip()) < 20:
        return text

    result = text.strip()

    # Step 1: 同义词替换
    result = replace_synonyms(result, synonym_rate)

    # Step 2: 句首变化
    result = vary_sentence_starters(result)

    # Step 3: 突发性提升
    result = boost_burstiness(result, burstiness_intensity)

    # Step 4: 人类噪声注入
    result = inject_human_noise(result, noise_rate)

    # Step 5: 结构变化
    result = add_structural_variation(result)

    return result
