# -*- coding: utf-8 -*-
"""常见卡点 · 手工预设(MVP:按题型/模块各预设 3 个,不做动态生成)。
每个预设挂一个 point_id(对应方法论知识点,卡点沉淀靠它泛化到推荐)、可读名 point_label、
以及点击后发给重讲分支的预设问法 question。

匹配优先级:先按二级题型(question_type 子串)→ 再按一级模块(module)→ 兜底通用。
point_id 必须是真实存在的方法论条目 ID(见 method_kb.all_entries())。"""

# 按二级题型(用子串匹配 category_l2)
_BY_TYPE = {
    "图形": [
        {"point_id": "tx_router", "point_label": "图形推理·判型",
         "question": "我不知道这题该从哪个维度入手(数量?位置?样式?),判型口诀怎么用?"},
        {"point_id": "tx_03_shuliang_mian", "point_label": "图形推理·数面",
         "question": "数量类我老是数不准,封闭面/线/点到底怎么数才不漏不重?"},
        {"point_id": "tx_11_kongjian_zhezhihe", "point_label": "图形推理·折纸盒",
         "question": "立体折叠/空间重构我完全没空间感,有没有不靠想象的排除法?"},
    ],
    "定义": [
        {"point_id": "pd_dingyi_panduan", "point_label": "定义判断·拆要件",
         "question": "定义里哪些是必须满足的关键要件?我分不清主体/方式/对象。"},
        {"point_id": "pd_dingyi_panduan", "point_label": "定义判断·问法",
         "question": "题目问的是「属于」还是「不属于」,我老是看反,怎么定位问法?"},
        {"point_id": "pd_dingyi_panduan", "point_label": "定义判断·排干扰",
         "question": "两个选项看起来都沾边,怎么用要件清单把干扰项排掉?"},
    ],
    "类比": [
        {"point_id": "pd_leibi_tuili", "point_label": "类比推理·找关系",
         "question": "题干两个词的关系我说不清(并列?包含?对应?),怎么先定准关系?"},
        {"point_id": "pd_leibi_tuili", "point_label": "类比推理·二级辨析",
         "question": "好几个选项关系都「像」,怎么用二级特征(属性/功能/顺序)再细分?"},
        {"point_id": "pd_leibi_tuili", "point_label": "类比推理·造句",
         "question": "我不会用造句法把抽象关系具体化,能演示一下吗?"},
    ],
    "逻辑": [
        {"point_id": "pd_luoji_panduan", "point_label": "逻辑判断·翻译",
         "question": "「如果…就…」「只有…才…」我老翻译反,推出关系到底怎么写?"},
        {"point_id": "pd_luoji_panduan", "point_label": "逻辑判断·加强削弱",
         "question": "加强/削弱题我分不清哪个选项最直接,力度怎么比较?"},
        {"point_id": "pd_luoji_panduan", "point_label": "逻辑判断·真假",
         "question": "真假话题型我找不到突破口,矛盾关系怎么用?"},
    ],
    "逻辑填空": [
        {"point_id": "yy_luoji_tiankong", "point_label": "逻辑填空·找呼应",
         "question": "空格该填什么我全靠语感,怎么从上下文找提示词/呼应点?"},
        {"point_id": "yy_luoji_tiankong", "point_label": "逻辑填空·辨词",
         "question": "两个近义词意思差不多,怎么辨析感情色彩/搭配/语义轻重?"},
        {"point_id": "yy_luoji_tiankong", "point_label": "逻辑填空·关联词",
         "question": "关联词(转折/因果/递进)我看不出来,怎么靠它定方向?"},
    ],
    "片段": [
        {"point_id": "yy_pianduan_yuedu", "point_label": "片段阅读·找主旨",
         "question": "我抓不住这段话的重点句,主旨到底在哪一句?"},
        {"point_id": "yy_pianduan_yuedu", "point_label": "片段阅读·辨结构",
         "question": "总分/分总/转折结构我看不出来,怎么靠行文脉络定位重点?"},
        {"point_id": "yy_pianduan_yuedu", "point_label": "片段阅读·排干扰",
         "question": "选项里有「偷换/扩大/片面」的干扰项,怎么识别和排除?"},
    ],
    "数学": [
        {"point_id": "sl_shuxue_yunsuan", "point_label": "数学运算·选方法",
         "question": "这题该用方程、赋值还是代入?我不知道选哪种最快。"},
        {"point_id": "sl_shuxue_yunsuan", "point_label": "数学运算·设未知",
         "question": "我不会设未知数/找等量关系,列式总卡住怎么办?"},
        {"point_id": "sl_shuxue_yunsuan", "point_label": "数学运算·特值法",
         "question": "什么时候能用特值/赋值法?怎么赋才不出错?"},
    ],
    "资料": [
        {"point_id": "zl_router", "point_label": "资料分析·定公式",
         "question": "看到问法我不知道该套哪个公式(增长率?比重?基期?)。"},
        {"point_id": "zl_f01_zengzhanglv", "point_label": "资料分析·基期现期",
         "question": "「哪一年/基期还是现期」我老是绕晕,怎么快速看清问的是哪个时期?"},
        {"point_id": "zl_f01_zengzhanglv", "point_label": "资料分析·估算凑整",
         "question": "算式列出来了但数太大,怎么用首数法/特征数字估算凑整?"},
    ],
    "常识": [
        {"point_id": "cs_changshi_panduan", "point_label": "常识判断·排除法",
         "question": "这题我没背过对应知识点,能不能用排除法/常识逻辑缩小范围?"},
        {"point_id": "cs_changshi_panduan", "point_label": "常识判断·辨易混",
         "question": "几个选项涉及的概念我容易混,怎么抓关键区别?"},
        {"point_id": "cs_changshi_panduan", "point_label": "常识判断·定考点",
         "question": "这题到底考哪个领域(政治/法律/科技/人文)的什么点?"},
    ],
}

# 按一级模块兜底(二级没命中时用)
_BY_MODULE = {
    "判断推理": _BY_TYPE["逻辑"],
    "言语理解": _BY_TYPE["逻辑填空"],
    "数量关系": _BY_TYPE["数学"],
    "资料分析": _BY_TYPE["资料"],
    "常识判断": _BY_TYPE["常识"],
}

# 完全兜底(模块也没命中):通用三连
_GENERIC = [
    {"point_id": "", "point_label": "判型·这是什么题",
     "question": "我没看出这是什么题型、考什么,怎么判断?"},
    {"point_id": "", "point_label": "下手·第一步看什么",
     "question": "我不知道第一步该看什么、从哪下手。"},
    {"point_id": "", "point_label": "排查·选项怎么比",
     "question": "我不知道选项之间按什么标准去比较和排除。"},
]


def presets_for(module: str, question_type: str):
    """返回该题型最高频的 3 个卡点预设。"""
    qt = question_type or ""
    # 长 key 先匹配:'逻辑填空' 要早于 '逻辑' 命中,避免被更短的子串抢先
    for key in sorted(_BY_TYPE, key=len, reverse=True):
        if key in qt:
            return _BY_TYPE[key]
    if module in _BY_MODULE:
        return _BY_MODULE[module]
    return _GENERIC
