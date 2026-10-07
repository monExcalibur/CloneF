import re

BASE_INSTRUCTION = """Ты — эксперт по Computer Science и базам данных, а также по Java.

Формат ответа для тестов (СТРОГО):
1. После анализа напиши ===ОТВЕТЫ=== на отдельной строке.
2. После разделителя — каждый ответ с новой строки в формате: НОМЕР_ВОПРОСА:ОТВЕТ
3. Для вопросов с буквами (A,B,C): пиши слитно (ABD)
4. Для True/False: только T или F (НЕ TRUE/FALSE)
5. Для вопросов без вариантов: пронумеруй сверху вниз и напиши номера правильных ответов слитно (24)

Если на изображении задача на программирование:
- игнорируй формат тестовых ответов
- выдай только чистый код
- без комментариев
- без Markdown
- без блоков ```.

Пример:
===ОТВЕТЫ===
14:ABD
15:T
16:24

НЕ ДОБАВЛЯЙ никаких пояснений, текста, рассуждений. Только ответы после разделителя. Ничего до разделителя тоже не пиши, кроме анализа, но после разделителя — только ответы."""

def clean_answer(raw):
    if "```" in raw:
        raw = extract_code_answer(raw)
    if looks_like_code_answer(raw):
        return extract_code_answer(raw)

    text = raw
    if "===ОТВЕТЫ===" in raw:
        text = raw.split("===ОТВЕТЫ===")[-1]

    text = text.strip()
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        return "ERR"

    is_test_format = any(re.match(r"^\d+\s*:.*", l) for l in lines)

    if is_test_format:
        filtered = []
        for l in lines:
            match = re.match(r"^(\d+)\s*:(.*)", l)
            if match:
                num = match.group(1)
                ans = re.sub(r"[^A-Z0-9]", "", match.group(2).upper())
                ans = {"TRUE": "T", "FALSE": "F"}.get(ans, ans)
                if ans:
                    filtered.append(f"{num}:{ans}")
        if filtered:
            result = "\n".join(filtered)
            return result

    return text

def extract_code_answer(raw):
    text = raw.strip()
    if "```" in text:
        blocks = re.findall(r"```(?:[a-zA-Z0-9_+-]+)?\s*([\s\S]*?)```", text)
        if blocks:
            text = "\n".join(block.strip() for block in blocks if block.strip())
        else:
            text = text.replace("```", "").strip()

    if "===ОТВЕТЫ===" in text:
        text = text.split("===ОТВЕТЫ===")[-1].strip()

    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip()

def looks_like_code_answer(answer):
    if not answer or answer == "ERR":
        return False
    if "```" in answer:
        return True
    if re.search(r"^\d+\s*:\s*[A-Z0-9]+\s*$", answer, re.MULTILINE):
        return False

    markers = [
        "def ", "class ", "import ", "from ", "return ", "if ",
        "public ", "static ", "void ", "println", "System.out",
        "function ", "console.", "#include", "print(", "let ", "const ",
        "{", "}", ";", "(", ")", "[]",
        "select ", "insert ", "update ", "delete ", "from ", "where ",
        "create table", "join ", "group by", "order by"
    ]
    lowered = answer.lower()
    score = sum(1 for marker in markers if marker in lowered)
    sql_keywords = ["table", "primary key", "foreign key", "references", "values", "into"]
    score += sum(1 for kw in sql_keywords if kw in lowered)
    return score >= 2
