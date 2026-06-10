# -*- coding: utf-8 -*-
"""
第一步：把行测PDF切成一道道独立的题，存成 questions.json
这一步不需要任何 API，纯本地运行。先跑通这步、确认切题正确再往下走。
用法：python step1_parse.py 你的试卷.pdf
"""
import pdfplumber, re, json, sys

# 图形题特征：含这些固定话术，且没有实质内容
GRAPHIC_MARKERS = ["填入问号处", "呈现一定的规律", "从所给的四个选项"]

def parse_pdf(path):
    # 1. 提取全文
    full = ""
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            full += (page.extract_text() or "") + "\n"

    # 2. 砍掉申论/材料处理题部分（结构不同，第一版不处理）
    for marker in ["五、材料处理题", "材料处理题", "（一）根据下列材料"]:
        idx = full.find(marker)
        if idx != -1:
            # 注意：资料分析(一)也用这个引导语，这里只砍申论；
            # 资料分析题第一版也跳过，所以遇到"（一）根据下列材料"就截断
            full = full[:idx]
            break

    # 3. 按行首的"数字."切题
    pattern = re.compile(r'(?m)^(\d{1,3})\.')
    matches = list(pattern.finditer(full))
    raw = []
    for i, m in enumerate(matches):
        num = int(m.group(1))
        start = m.start()
        end = matches[i+1].start() if i+1 < len(matches) else len(full)
        raw.append((num, full[start:end].strip()))

    # 4. 只保留题号连续递增的(1,2,3...)，防止材料里的编号误判
    clean = []
    last = 0
    for num, body in raw:
        if num == last + 1:
            clean.append((num, body))
            last = num

    # 5. 过滤图形题(含固定话术且无实质选项内容)
    questions = []
    skipped = []
    for num, body in clean:
        is_graphic = any(mk in body for mk in GRAPHIC_MARKERS)
        if is_graphic:
            skipped.append(num)
            continue
        questions.append({"id": num, "content": body})

    return questions, skipped

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "试卷.pdf"
    questions, skipped = parse_pdf(path)
    print(f"成功入库 {len(questions)} 道题")
    print(f"过滤掉(疑似图形题) {len(skipped)} 道：题号 {skipped}")
    with open("questions.json", "w", encoding="utf-8") as f:
        json.dump(questions, f, ensure_ascii=False, indent=2)
    print("已保存到 questions.json")
