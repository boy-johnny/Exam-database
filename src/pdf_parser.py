import os
import json
import logging
from pathlib import Path
import fitz  # PyMuPDF
import re
from typing import List, Dict, Any, Tuple, Optional
from enum import Enum, auto

# 日誌設置
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
if not logger.hasHandlers():
    logger.addHandler(handler)

# processed_data 目錄
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROCESSED_DATA_DIR = PROJECT_ROOT / "processed_data"
PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)

# 1. 從 PDF 提取純文字 ----------------------------

def extract_text_from_pdf(pdf_path: str) -> List[str]:
    """
    讀取 PDF，返回每一頁的純文字（list of str）。
    """
    text_pages = []
    with fitz.open(pdf_path) as doc:
        for page in doc:
            text_pages.append(page.get_text())
    logger.info(f"Extracted text from {pdf_path}, total {len(text_pages)} pages.")
    return text_pages

# 2. 從 PDF 首頁與檔名提取元數據 ----------------------------

def extract_metadata_from_text(pdf_text: str, filename: str) -> Dict[str, Any]:
    """
    從 PDF 首頁文字與檔名提取考試名稱、科目名稱、科目代碼、年份、期次、題數等。
    若部分資訊缺失，嘗試從檔名/資料夾結構補齊。
    """
    meta = {
        "exam_name": None,
        "subject_name": None,
        "subject_code": None,
        "subject_type": None,  # 新增：類科名稱
        "year": None,
        "period": None,
        "question_count": None,
    }
    # 1. 先抓首頁前兩行非空行作為考試名稱
    lines = [line.strip() for line in pdf_text.splitlines() if line.strip()]
    if lines:
        meta["exam_name"] = lines[0]
    # 2. 代號
    code_match = re.search(r"代[　\s]*號[：: ]*([0-9]+)", pdf_text)
    if code_match:
        meta["subject_code"] = code_match.group(1)
    # 3. 類科名稱
    type_match = re.search(r"類科名稱[：: ]*([\S ]+)", pdf_text)
    if type_match:
        meta["subject_type"] = type_match.group(1).strip()
    # 4. 科目名稱
    subject_match = re.search(r"科目名稱[：: ]*([\S ]+)", pdf_text)
    if subject_match:
        meta["subject_name"] = subject_match.group(1).strip()
    # 5. 年份與期次
    year_period_match = re.search(r"(\d{3,4})年[ _]*第?(\d+)次", pdf_text)
    if year_period_match:
        meta["year"] = int(year_period_match.group(1))
        meta["period"] = int(year_period_match.group(2))
    else:
        fn = filename
        fn_match = re.search(r"(\d{3,4})年[_ ]*第?(\d+)次", fn)
        if fn_match:
            meta["year"] = int(fn_match.group(1))
            meta["period"] = int(fn_match.group(2))
        else:
            fn_match2 = re.search(r"(\d{3,4})[ _-]?([1-4])", fn)
            if fn_match2:
                meta["year"] = int(fn_match2.group(1))
                meta["period"] = int(fn_match2.group(2))
    # 6. 題數
    qcount_match = re.search(r"題[\s　]*數[：: ]*(\d+)", pdf_text)
    if qcount_match:
        meta["question_count"] = int(qcount_match.group(1))
    logger.info(f"Extracted metadata: {meta} (from {filename})")
    return meta

# 3. 解析題目卷

class ParsingState(Enum):
    EXPECTING_QUESTION = auto()
    PARSING_QUESTION_CONTENT = auto()
    PARSING_OPTION_TEXT = auto()

def _commit_buffer(
    buffer: List[str],
    target_dict: Dict[str, Any],
    field_name: str,
    option_key: Optional[str] = None
):
    """Helper to join, strip, and commit buffered text to the target dictionary."""
    text_to_commit = "\n".join(buffer).strip()
    if text_to_commit: # Only commit if there's actual text
        if field_name == "options" and option_key:
            if option_key not in target_dict["options"]: # Ensure option key exists
                 target_dict["options"][option_key] = ""
            target_dict["options"][option_key] = text_to_commit
        elif field_name == "content":
            target_dict["content"] = text_to_commit
    buffer.clear()

def parse_questions_from_pdf_text(pdf_text: str, parsing_mode: str = "default") -> List[Dict[str, Any]]:
    """
    解析題號、題幹、選項，處理跨行、換頁等情況。
    返回題目列表，每題為 dict: {question_number, content, options}

    Args:
        pdf_text: The full text extracted from the PDF.
        parsing_mode: "default" or "strict_start".
                      "default": Allows question content to start with a digit (e.g., "52.70歲...").
                      "strict_start": Question content after "QN." cannot start with a digit (e.g., for "pKa = 6.8]").
    """
    questions_data: List[Dict[str, Any]] = []
    current_question: Dict[str, Any] = {}
    current_text_buffer: List[str] = []
    active_option_letter: Optional[str] = None
    current_state: ParsingState = ParsingState.EXPECTING_QUESTION

    if parsing_mode == "strict_start":
        question_start_pattern = re.compile(r"^\s*(\d+)\s*[．.\uFF0E](?!\d)\s*(.*)")
        logger.info("Using STRICT_START question pattern.")
    else: # default mode
        question_start_pattern = re.compile(r"^\s*(\d+)\s*[．.\uFF0E]\s*(.*)")
        logger.info("Using DEFAULT question pattern.")
    
    option_pattern = re.compile(
        r"^\s*"
        r"(?:(?:[（\(])\s*([A-Z\uFF21-\uFF3A])\s*(?:[）\)])|"
        r"([A-Z\uFF21-\uFF3A])\s*[．.\uFF0E])"
        r"\s*(.*)"
    )

    lines = pdf_text.splitlines()

    for line_idx, line_raw in enumerate(lines):
        q_match = question_start_pattern.match(line_raw)
        opt_match = option_pattern.match(line_raw)

        if q_match: # New question starts
            logger.debug(f"Line {line_idx + 1} matched QUESTION start: '{line_raw}'")
            # Finalize previous question if any
            if current_question:
                if current_state == ParsingState.PARSING_OPTION_TEXT and active_option_letter:
                    _commit_buffer(current_text_buffer, current_question, "options", active_option_letter)
                elif current_state == ParsingState.PARSING_QUESTION_CONTENT:
                    _commit_buffer(current_text_buffer, current_question, "content")
                
                if current_question.get("question_number"): # Ensure it's a valid question
                    questions_data.append(current_question)

            # Initialize new question
            current_text_buffer.clear()
            question_number = int(q_match.group(1))
            initial_content_part = q_match.group(2).strip()
            current_question = {"question_number": question_number, "content": "", "options": {}}
            
            if initial_content_part:
                current_text_buffer.append(initial_content_part)
            
            current_state = ParsingState.PARSING_QUESTION_CONTENT
            active_option_letter = None

        elif opt_match and current_question: # New option starts for the current question
            logger.debug(f"Line {line_idx + 1} matched OPTION start: '{line_raw}' for Q#{current_question.get('question_number')}")
            if current_state == ParsingState.PARSING_QUESTION_CONTENT:
                _commit_buffer(current_text_buffer, current_question, "content")
            elif current_state == ParsingState.PARSING_OPTION_TEXT and active_option_letter:
                _commit_buffer(current_text_buffer, current_question, "options", active_option_letter)
            
            current_text_buffer.clear()
            
            option_letter_raw = opt_match.group(1) or opt_match.group(2)
            normalized_letter = option_letter_raw
            if '\uFF21' <= option_letter_raw <= '\uFF3A': # Full-width A-Z
                normalized_letter = chr(ord(option_letter_raw) - (ord('\uFF21') - ord('A')))
            active_option_letter = normalized_letter
            
            option_text_part = opt_match.group(3).strip()
            if option_text_part:
                current_text_buffer.append(option_text_part)
            # Ensure option key exists, _commit_buffer will fill it later if text_buffer is not empty
            if active_option_letter and active_option_letter not in current_question["options"]:
                 current_question["options"][active_option_letter] = ""

            current_state = ParsingState.PARSING_OPTION_TEXT

        elif current_question: # Continuation of current question content or option text
            line_stripped = line_raw.strip()
            if line_stripped: # Only append non-empty lines
                # logger.debug(f"Line {line_idx + 1} is continuation: '{line_stripped}' for state {current_state}")
                if current_state == ParsingState.PARSING_QUESTION_CONTENT or \
                   current_state == ParsingState.PARSING_OPTION_TEXT:
                    current_text_buffer.append(line_stripped)
        else:
            # This line is not part of any question (e.g. header/footer, or before first question)
            # logger.debug(f"Line {line_idx + 1} skipped (no active question or not matched): '{line_raw}'")
            pass

    # Finalize the last question being processed
    if current_question:
        if current_state == ParsingState.PARSING_OPTION_TEXT and active_option_letter:
            _commit_buffer(current_text_buffer, current_question, "options", active_option_letter)
        elif current_state == ParsingState.PARSING_QUESTION_CONTENT:
            _commit_buffer(current_text_buffer, current_question, "content")
        
        if current_question.get("question_number"): # Ensure it's a valid question
            questions_data.append(current_question)
            
    logger.info(f"Parsed {len(questions_data)} questions from text.")

    # 檢查題號連續性和數量
    if questions_data:
        parsed_numbers = sorted([q["question_number"] for q in questions_data if q.get("question_number") is not None])
        if parsed_numbers:
            expected_max_number = parsed_numbers[0] + len(parsed_numbers) - 1
            if parsed_numbers[-1] > expected_max_number + 2 or len(parsed_numbers) < parsed_numbers[-1] - parsed_numbers[0] - 2 : # 允許少量不連續或末尾缺失
                logger.warning(
                    f"Potential missing or misparsed questions. Parsed {len(parsed_numbers)} questions, "
                    f"with numbers from {parsed_numbers[0]} to {parsed_numbers[-1]}. Check for discontinuities."
                )
            # 檢查是否有重複題號
            if len(parsed_numbers) != len(set(parsed_numbers)):
                logger.warning("Duplicate question numbers detected. Please review parsing logic or PDF content.")

    return questions_data

# 4. 解析答案卷 ----------------------------

def parse_answers_from_pdf_text(answer_pdf_path: str) -> Dict[int, Any]:
    """
    解析答案表格，返回題號到答案的映射。
    若遇到 # 則填 ['#']，並在 notes 備註。
    若備註區有特殊說明，也一併回傳 notes。
    This version uses page.get_text("words") for robust table parsing.
    """
    logger.info(f"Starting to parse answers from: {answer_pdf_path}")
    answers: Dict[int, List[str]] = {}
    notes: Dict[int, str] = {}
    raw_text_for_notes_pages: List[str] = []

    try:
        with fitz.open(answer_pdf_path) as doc:
            for page_num, page in enumerate(doc):
                raw_text_for_notes_pages.append(page.get_text("text"))
                
                words = page.get_text("words")
                if not words:
                    continue

                words.sort(key=lambda w: (w[1], w[0]))

                structured_lines = []
                if words:
                    current_line_buffer = []
                    Y_GROUPING_TOLERANCE = 5

                    for word_idx, word_info in enumerate(words):
                        if not current_line_buffer:
                            current_line_buffer.append(word_info)
                        else:
                            first_word_y0_in_current_line = current_line_buffer[0][1]
                            y_diff = abs(word_info[1] - first_word_y0_in_current_line)
                            is_grouping = y_diff < Y_GROUPING_TOLERANCE
                            
                            if is_grouping:
                                current_line_buffer.append(word_info)
                            else:
                                current_line_buffer.sort(key=lambda w: w[0])
                                structured_lines.append({
                                    "y0": current_line_buffer[0][1],
                                    "words": list(current_line_buffer),
                                    "text": " ".join(w[4] for w in current_line_buffer)
                                })
                                current_line_buffer = [word_info]
                    
                    if current_line_buffer:
                        current_line_buffer.sort(key=lambda w: w[0])
                        structured_lines.append({
                            "y0": current_line_buffer[0][1],
                            "words": list(current_line_buffer),
                            "text": " ".join(w[4] for w in current_line_buffer)
                        })
                
                q_num_rows_data = []
                ans_rows_data = []

                for i, line_obj in enumerate(structured_lines):
                    line_text_concat = line_obj["text"]
                    
                    if ("題號" in line_text_concat or "序" in line_text_concat) and any(char.isdigit() for char in line_text_concat):
                        current_q_numbers = []
                        for word_info in line_obj["words"]:
                            if word_info[4].isdigit():
                                current_q_numbers.append({'text': word_info[4], 'x_mid': (word_info[0] + word_info[2]) / 2, 'y0': word_info[1]})
                        if current_q_numbers:
                            q_num_rows_data.append({'index': i, 'q_numbers': current_q_numbers, 'y0': line_obj["y0"]})

                    elif "答案" in line_text_concat and (re.search(r"[A-Z\uFF21-\uFF3A#\uFF03]", line_text_concat)):
                        current_answers = []
                        for word_info in line_obj['words']:
                            word_text_original = word_info[4]
                            word_text_stripped = word_text_original.strip()
                            ans_char_to_add = None

                            if re.fullmatch(r"^[A-Z\uFF21-\uFF3A#\uFF03]$", word_text_stripped):
                                ans_char_to_add = word_text_stripped
                            elif word_text_stripped.startswith("答案") and len(word_text_stripped) == 3:
                                potential_ans_char = word_text_stripped[2]
                                if re.fullmatch(r"^[A-Z\uFF21-\uFF3A#\uFF03]$", potential_ans_char):
                                    ans_char_to_add = potential_ans_char
                            
                            if ans_char_to_add:
                                current_answers.append({
                                    'text': ans_char_to_add,
                                    'x_mid': (word_info[0] + word_info[2]) / 2,
                                    'y0': word_info[1]
                                })
                            elif word_text_stripped == "答案":
                                pass 

                        if current_answers:
                           ans_rows_data.append({'index': i, 'answers': current_answers, 'y0': line_obj["y0"]})
                
                processed_ans_row_indices = set()

                for q_row_data in q_num_rows_data:
                    best_candidate_ans_row = None
                    min_y_diff = float('inf')

                    for a_row_data in ans_rows_data:
                        if a_row_data['index'] > q_row_data['index'] and a_row_data['index'] not in processed_ans_row_indices:
                            y_diff = a_row_data['y0'] - q_row_data['y0']
                            if 0 < y_diff < 40: 
                               if y_diff < min_y_diff:
                                   min_y_diff = y_diff
                                   best_candidate_ans_row = a_row_data
                    
                    if best_candidate_ans_row:
                        processed_ans_row_indices.add(best_candidate_ans_row['index'])
                        
                        for q_idx, q_tuple in enumerate(q_row_data['q_numbers']):
                            q_text = q_tuple['text']
                            q_x = q_tuple['x_mid']
                            
                            best_ans_for_q = None
                            min_x_dist_for_q = float('inf')

                            for ans_idx, ans_tuple in enumerate(best_candidate_ans_row['answers']):
                                ans_text = ans_tuple['text']
                                ans_x = ans_tuple['x_mid']
                                x_dist = abs(q_x - ans_x)

                                if x_dist < min_x_dist_for_q and x_dist < 25:
                                    min_x_dist_for_q = x_dist
                                    best_ans_for_q = ans_text
                            
                            q_num_int = int(q_text)
                            if best_ans_for_q:
                                answers[q_num_int] = [best_ans_for_q]
                            else:
                                answers[q_num_int] = ['#']
                                logger.warning(f"Page {page_num+1}, Q {q_text} (x={q_x:.1f}, y={q_tuple['y0']:.1f}): No aligned answer found in ans_row (y={best_candidate_ans_row['y0']:.1f}). Setting to '#'.")
    except Exception as e:
        logger.error(f"Error parsing answers from {answer_pdf_path}: {e}")
        if notes:
             return {"answers": answers, "notes": notes}
        return answers

    full_raw_text_for_notes = "\n".join(raw_text_for_notes_pages)
    note_lines = [line.strip() for line in full_raw_text_for_notes.splitlines() if line.strip()]
    for j, line in enumerate(note_lines):
        if re.match(r"^\s*備\s*註", line): 
            note_text = "\n".join(note_lines[j:]) 
            for m in re.finditer(r"第(\d+)題[，,，、及和與]*(?:答案|選項)?(?:更正為|應為)?([A-D#\uFF03])(?:.*?)。", note_text):
                qn = int(m.group(1))
                corrected_ans = m.group(2)
                notes[qn] = f"答案更正為 {corrected_ans}"
                answers[qn] = [corrected_ans]

            for m in re.finditer(r"第(\d+)題[，,，]?(送分|均給分|皆給分|給分)", note_text):
                qn = int(m.group(1))
                notes[qn] = "送分"
                answers[qn] = ['送分'] 

            for m in re.finditer(r"第(\d+)題[，,，]?(?!送分|均給分|皆給分|給分|答案更正為|選項更正為|應為)([^第].*?)。", note_text):
                qn = int(m.group(1))
                note_content = m.group(2).strip() 
                if qn not in notes:
                  notes[qn] = note_content
            
            multi_q_note_pattern = r"第(\d+(?:[、,及和與]\d+)*)題(?:(?:等)|(?:各題))?[，,，]?(送分|均給分|皆給分|給分|(?:答案|選項)?(?:更正為|應為)?([A-D#\uFF03]))(?:.*?)。"
            for m in re.finditer(multi_q_note_pattern, note_text):
                q_numbers_str = m.group(1)
                q_nums = [int(qn_str) for qn_str in re.findall(r"\d+", q_numbers_str)]
                
                note_type = m.group(2) 
                corrected_ans_val = m.group(3)

                actual_note = ""
                ans_to_set = ['#']

                if corrected_ans_val: 
                    actual_note = f"答案更正為 {corrected_ans_val}"
                    ans_to_set = [corrected_ans_val]
                elif note_type == "送分" or "給分" in note_type : 
                    actual_note = "送分"
                    ans_to_set = ['送分']
                
                if actual_note:
                    for qn in q_nums:
                        notes[qn] = actual_note
                        answers[qn] = ans_to_set
            break
            
    if notes:
        return {"answers": answers, "notes": notes}
    return answers

# 5. 合併題目與答案 ----------------------------

def｀combine_questions_and_answers(questions: List[Dict[str, Any]], answers: Dict[int, Any]) -> List[Dict[str, Any]]:
    """
    將題目與答案合併，補齊 correct_answer_key、notes 等欄位。
    若遇到特殊情況，notes 標註。
    """
    # TODO: 合併邏輯
    combined = []
    return combined

# 6. 存 processed_data ----------------------------

def save_processed_data(data: Any, filename: str):
    """
    將解析後的資料存為 JSON 到 processed_data 目錄。
    """
    out_path = PROCESSED_DATA_DIR / filename
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"Saved processed data to {out_path}")

# 7. 主流程範例 ----------------------------

def determine_parsing_mode(pdf_path: str) -> str:
    """根據文件名/路徑決定題目解析模式"""
    # 默認為 'default' 模式
    mode = "default"
    
    # 示例規則：
    # 您可以根據實際情況擴展這些規則。
    # 例如，檢查路徑中是否包含特定的科目名稱或年份組合
    if "臨床鏡檢學與分子生物學" in pdf_path and "111年_第一次" in pdf_path and "題目1111鏡檢.pdf" in pdf_path:
        mode = "strict_start"
    # Add more rules here if needed for other specific PDFs
    # elif "some_other_keyword" in pdf_path and "another_condition" in pdf_path:
    #     mode = "strict_start"
        
    logger.info(f"Determined parsing_mode='{mode}' for {pdf_path}")
    return mode

def process_exam_pdfs(question_pdf_path: str, answer_pdf_path: str, processed_prefix: str):
    """
    主流程：
    1. 讀取題目卷與答案卷 PDF
    2. 提取元數據
    3. 解析題目與答案
    4. 合併
    5. 存 processed_data
    """
    # 1. 讀取 PDF
    question_text_pages = extract_text_from_pdf(question_pdf_path)
    answer_text_pages = extract_text_from_pdf(answer_pdf_path)
    # 2. 提取元數據（僅用首頁）
    meta = extract_metadata_from_text(question_text_pages[0], os.path.basename(question_pdf_path))
    # 3. 解析題目與答案
    current_parsing_mode = determine_parsing_mode(question_pdf_path)
    questions = parse_questions_from_pdf_text("\n".join(question_text_pages), parsing_mode=current_parsing_mode)
    answers_data = parse_answers_from_pdf_text(answer_pdf_path)

    # 校驗題目數量
    if meta.get("question_count") is not None:
        expected_q_count = meta["question_count"]
        actual_q_count = len(questions)
        if actual_q_count != expected_q_count:
            logger.warning(
                f"Mismatch in question count for {os.path.basename(question_pdf_path)}. "
                f"Expected (from metadata): {expected_q_count}, Parsed: {actual_q_count}. Manual review suggested."
            )
    else:
        logger.info(f"Metadata did not provide question_count for {os.path.basename(question_pdf_path)}. Skipping count check.")

    # 4. 合併
    actual_answers_map: Dict[int, List[str]]
    optional_notes_map: Optional[Dict[int, str]] = None

    if isinstance(answers_data, dict) and "answers" in answers_data:
        actual_answers_map = answers_data["answers"]
        optional_notes_map = answers_data.get("notes") # Optional_notes_map not used by current combine stub
    elif isinstance(answers_data, dict): # Should be Dict[int, List[str]]
        actual_answers_map = answers_data
    else: # Should not happen with correct parse_answers_from_pdf_text return
        logger.error("Unexpected format from parse_answers_from_pdf_text")
        actual_answers_map = {}

    combined = combine_questions_and_answers(questions, actual_answers_map) # Pass only answer map for now
                                                                            # until combine_questions_and_answers is updated for notes

    # 5. 存 processed_data
    save_processed_data({
        "meta": meta,
        "questions": combined
    }, f"{processed_prefix}_parsed.json")
    # 也可存原始文字
    save_processed_data({
        "question_text_pages": question_text_pages,
        "answer_text_pages": answer_text_pages
    }, f"{processed_prefix}_rawtext.json")
    logger.info(f"Process complete for {processed_prefix}")

# if __name__ == "__main__":
#     # --- 測試 parse_questions_from_pdf_text ---
#     # Test Case 1: "臨床生理學和病理學/111年_第一次/題目1111病理.pdf" - Should use "default" mode
#     question_pdf_path_test_pathology = "raw_data/exams/臨床血液學和血庫學/111年_第二次/題目1112血液.pdf"
#     logger.info(f"--- Testing parse_questions_from_pdf_text with {question_pdf_path_test_pathology} (MODE: default) ---")
#     question_text_pages_test_pathology = extract_text_from_pdf(question_pdf_path_test_pathology)
#     full_question_text_test_pathology = "\n".join(question_text_pages_test_pathology)
#     parsed_questions_test_pathology = parse_questions_from_pdf_text(full_question_text_test_pathology, parsing_mode="default")
    
#     print("\n=== 題目解析測試結果 (病理 - default mode) ===")
#     if parsed_questions_test_pathology:
#         print(f"Total questions parsed (Pathology, default mode): {len(parsed_questions_test_pathology)}")
#     else:
#         print("No questions were parsed (Pathology, default mode).")
#     print("--- End of Test for Pathology PDF ---\n")

#     # Test Case 2: "生物化學與臨床生化學/111年_第一次/題目1111微生物.pdf" - Should use "strict_start" mode
#     question_pdf_path_test_biochem = "raw_data/exams/臨床微生物學/111年_第一次/題目1111微生物.pdf"
#     logger.info(f"--- Testing parse_questions_from_pdf_text with {question_pdf_path_test_biochem} (MODE: strict_start) ---")
#     question_text_pages_test_biochem = extract_text_from_pdf(question_pdf_path_test_biochem)
#     full_question_text_test_biochem = "\n".join(question_text_pages_test_biochem)
#     parsed_questions_test_biochem = parse_questions_from_pdf_text(full_question_text_test_biochem, parsing_mode="strict_start")

#     print("\n=== 題目解析測試結果 (生化 - strict_start mode) ===")
#     if parsed_questions_test_biochem:
#         # (簡化輸出，只打總數和第38, 39題左右)
#         for i, q_data in enumerate(parsed_questions_test_biochem):
#             if q_data.get('question_number') in [37, 38, 39, 6, 7]: # Check around the problematic area
#                  print(f"--- Question {i+1} (Original Number: {q_data.get('question_number')}) ---")
#                  print(f"Content: {q_data.get('content')}")
#                  print("Options:")
#                  if q_data.get('options'):
#                      for opt_key, opt_text in q_data['options'].items():
#                          print(f"  {opt_key}: {opt_text}")
#                  else:
#                      print("  (No options parsed)")
#                  print("-" * 20)
#         print(f"Total questions parsed (Biochemistry, strict_start mode): {len(parsed_questions_test_biochem)}")
#     else:
#         print("No questions were parsed (Biochemistry, strict_start mode).")
#     print("--- End of Test for Biochemistry PDF ---\n")
    
#     # # --- 測試答案卷解析 (原有代碼) ---
#     # # 測試答案卷解析
#     # pdf_path = "raw_data/exams/生物化學與臨床生化學/111年_第二次/答案1112生化.pdf"
    
#     # # 1. 提取文字 (No longer needed here if parser takes path directly)
#     # # text_pages = extract_text_from_pdf(pdf_path)
#     # # logger.info(f"Extracted {len(text_pages)} pages from {pdf_path}")
    
#     # # 2. 解析答案
#     # answers_data = parse_answers_from_pdf_text(pdf_path) # MODIFIED CALL
    
#     # # 3. 輸出結果
#     # print("\n=== 答案解析結果 ===")
#     # if isinstance(answers_data, dict) and "answers" in answers_data:
#     #     # 有備註的情況
#     #     print("\n答案:")
#     #     for q_num, ans in answers_data["answers"].items():
#     #         print(f"題號 {q_num}: {ans}")
#     #     print("\n備註:")
#     #     for q_num, note in answers_data["notes"].items():
#     #         print(f"題號 {q_num}: {note}")
#     # else:
#     #     # 無備註的情況
#     #     for q_num, ans in answers_data.items():
#     #         print(f"題號 {q_num}: {ans}")
#     pass 