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

# It's good practice to define constants for directory names
IMAGES_BASE_SUBDIR = "images_from_pdf" # Renamed to avoid conflict if you have other "images" dirs

# 1. 從 PDF 提取純文字 ----------------------------

def extract_text_from_pdf(pdf_path: str) -> str:
    """從 PDF 提取所有純文字。"""
    try:
        doc = fitz.open(pdf_path)
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()
        return text
    except Exception as e:
        print(f"Error extracting text from PDF {pdf_path}: {e}")
        return ""

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

def combine_questions_and_answers(questions: List[Dict[str, Any]], answers: Dict[int, Any]) -> List[Dict[str, Any]]:
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

# 8. 新增：逐頁解析題目並提取圖片的函數

def parse_questions_from_pdf(
    pdf_path: str,
    base_output_dir: str, # 例如: "processed_data" 或測試時的 "test_processed_data"
    test_id_for_images: str, # 例如: "111_first_biochemistry"
    # 以下參數將傳遞給您現有的 parse_questions_from_pdf_text
    # 您可能需要從您的配置中獲取 question_start_pattern 和 option_pattern
    # 這裡我們假設它們會被傳入，或者在函數內部根據 pdf_path 決定
    # current_parsing_mode: str # 這個由 determine_parsing_mode 決定
):
    """
    從 PDF 文件中逐頁解析題目、選項，並提取關聯的圖片。
    調用現有的 parse_questions_from_pdf_text 進行每頁的文本解析。

    Args:
        pdf_path: PDF 文件的路徑。
        base_output_dir: 存儲提取圖片等處理後數據的基礎目錄。
        test_id_for_images: 本次測驗的唯一標識，用於在 images 子目錄下創建更深層的子目錄。

    Returns:
        一個字典列表，每個字典包含題目數據及 'image_path' 和 'page_number'。
    """
    all_parsed_questions_data = []
    
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        logger.error(f"Error opening PDF {pdf_path}: {e}")
        return all_parsed_questions_data

    # 決定本次解析使用的模式
    current_parsing_mode = determine_parsing_mode(pdf_path) # 使用您已有的函數

    # 為本次測驗的圖片創建特定的輸出子目錄
    # 例如: base_output_dir/images_from_pdf/test_id_for_images/
    # IMAGES_BASE_SUBDIR 已經在文件頂部定義
    current_exam_images_dir = os.path.join(base_output_dir, IMAGES_BASE_SUBDIR, test_id_for_images)
    os.makedirs(current_exam_images_dir, exist_ok=True)
    logger.info(f"Images for {test_id_for_images} will be saved in: {current_exam_images_dir}")

    for page_num, page in enumerate(doc):
        page_actual_number = page_num + 1
        page_image_paths = [] # 存儲本頁提取出的所有圖片的路徑

        # 1. 提取並保存本頁所有圖片
        img_list = page.get_images(full=True)
        for img_index, img_info in enumerate(img_list):
            xref = img_info[0]
            try:
                pix = fitz.Pixmap(doc, xref)
                # 統一保存為 PNG
                image_filename = f"page{page_actual_number}_img{img_index + 1}.png"
                image_save_path = os.path.join(current_exam_images_dir, image_filename)
                
                if pix.n - pix.alpha < 4: # RGBA or RGB
                    pix.save(image_save_path)
                else: # CMYK images, etc., convert to RGB first
                    pix_rgb = fitz.Pixmap(fitz.csRGB, pix)
                    pix_rgb.save(image_save_path)
                    pix_rgb = None # Release memory
                
                page_image_paths.append(image_save_path)
                pix = None # Release memory
                logger.debug(f"Saved image {image_filename} from page {page_actual_number} of {pdf_path}")
            except Exception as e:
                logger.error(f"Error processing image xref {xref} on page {page_actual_number} of {pdf_path}: {e}")

        # 2. 提取本頁文本
        page_text = page.get_text("text")
        
        # 3. 使用您現有的 parse_questions_from_pdf_text 解析本頁題目
        # 注意：您的 parse_questions_from_pdf_text 內部有自己的日誌記錄
        questions_on_this_page = parse_questions_from_pdf_text(page_text, parsing_mode=current_parsing_mode)
        
        # 4. 基本圖片關聯策略 和 頁碼添加
        page_first_image_path = page_image_paths[0] if page_image_paths else None
        
        for q_data in questions_on_this_page:
            q_data['image_path'] = page_first_image_path # 關聯本頁第一張圖片 (如果存在)
            q_data['page_number'] = page_actual_number
            all_parsed_questions_data.append(q_data)

    doc.close()
    logger.info(f"Finished parsing {pdf_path}. Total questions extracted: {len(all_parsed_questions_data)}")
    return all_parsed_questions_data


if __name__ == "__main__":
    # 確保日誌在直接運行此文件時能輸出
    logger.setLevel(logging.DEBUG) # 設置為 DEBUG 以獲取更詳細的圖像保存信息
    
    print(" executing pdf_parser.py directly for testing...")
    # 打印 PyMuPDF 版本
    try:
        print(f"PyMuPDF (fitz) version: {fitz.__doc__}")
    except Exception as e_version:
        print(f"Could not retrieve fitz version: {e_version}")

    # --- 測試新的 parse_questions_from_pdf ---
    
    # 定義用於測試的正則表達式 (您應該從您的配置中加載實際的模式)
    # 這些是簡化的示例，您需要用您 parse_questions_from_pdf_text 函數實際使用的 pattern
    # 但由於 parse_questions_from_pdf 會調用 determine_parsing_mode, 
    # 而 determine_parsing_mode 又會影響 parse_questions_from_pdf_text 內部使用的 pattern,
    # 所以這裡不需要顯式傳遞 pattern 給 parse_questions_from_pdf。

    # 創建一個虛擬 PDF 用於測試
    dummy_pdf_filename = "dummy_test_exam_with_image.pdf"
    dummy_pdf_path = str(PROJECT_ROOT / dummy_pdf_filename) # 確保路徑在項目根目錄

    if not os.path.exists(dummy_pdf_path):
        try:
            doc = fitz.open() # new empty PDF
            page = doc.new_page()
            
            # 添加一些符合您 question_start_pattern 和 option_pattern 的文本
            # (請根據您 parse_questions_from_pdf_text 中的正則進行調整)
            page.insert_text(fitz.Point(50, 72), "1. 第一題的題目內容？")
            page.insert_text(fitz.Point(70, 92), "(A) 選項A的內容")
            page.insert_text(fitz.Point(70, 112), "(B) 選項B的內容")
            
            page.insert_text(fitz.Point(50, 152), "2. 第二題的題目，這題有圖。")
            page.insert_text(fitz.Point(70, 172), "（A）選項 A again")
            page.insert_text(fitz.Point(70, 192), "（B）選項 B again")
            
            # 創建一個小的像素圖 (例如 20x20 的黑色矩形)
            img_width, img_height = 20, 20
            # 黑色 RGB: (0,0,0)。數據是 R,G,B,R,G,B,...
            img_data = bytearray([0, 0, 0] * (img_width * img_height))
            
            # PyMuPDF 1.26.0: Pixmap 沒有 set_samples 方法。
            # 必須在構造時提供所有信息。
            try:
                # 直接使用標準構造函數，包含 colorspace, width, height, samples, alpha
                pix = fitz.Pixmap(fitz.csRGB, img_width, img_height, img_data, False)
            except Exception as e_pix:
                print(f"Error creating Pixmap object with (cs, w, h, samples, alpha=False): {e_pix}")
                # 嘗試不帶 alpha 參數，以防萬一
                try:
                    print("Retrying Pixmap creation without explicit alpha...")
                    pix = fitz.Pixmap(fitz.csRGB, img_width, img_height, img_data)
                except Exception as e_pix_no_alpha:
                    print(f"Error creating Pixmap object with (cs, w, h, samples): {e_pix_no_alpha}")
                    raise # 如果兩種都失敗，則重新拋出最後一個異常

            page.insert_image(fitz.Rect(50, 220, 50 + img_width*5, 220 + img_height*5), pixmap=pix) # 插入圖片
            
            doc.save(dummy_pdf_path)
            doc.close()
            print(f"Created dummy PDF for testing: {dummy_pdf_path}")
        except Exception as e:
            print(f"Could not create dummy PDF: {e}")
            dummy_pdf_path = None 
    else:
        print(f"Dummy PDF already exists: {dummy_pdf_path}")

    if dummy_pdf_path and os.path.exists(dummy_pdf_path):
        print(f"--- Testing parse_questions_from_pdf with: {dummy_pdf_path} ---")
        
        # 測試時的輸出目錄 (例如 processed_data/test_dummy_output/)
        # 我們直接在 PROJECT_ROOT 下創建 test_processed_data/
        test_base_output_dir = str(PROJECT_ROOT / "test_processed_data")
        os.makedirs(test_base_output_dir, exist_ok=True)
        
        parsed_data = parse_questions_from_pdf(
            pdf_path=dummy_pdf_path,
            base_output_dir=test_base_output_dir,
            test_id_for_images="dummy_exam_run_001"
            # current_parsing_mode is determined internally by determine_parsing_mode
        )
        
        print(f"\\n--- Parsed Data (Total: {len(parsed_data)}) ---")
        for i, q_data in enumerate(parsed_data):
            print(f"  Question (from parser): {q_data.get('question_number')}") # 您的 parser 返回 'question_number'
            print(f"    Content: {q_data.get('content', 'N/A')[:60]}...")
            print(f"    Options: {q_data.get('options', {})}")
            print(f"    Page: {q_data.get('page_number')}")
            print(f"    Image Path: {q_data.get('image_path')}")
            if q_data.get('image_path') and os.path.exists(q_data['image_path']):
                print(f"      Image file found: YES")
            elif q_data.get('image_path'):
                print(f"      Image file found: NO (Path: {q_data['image_path']})")
        print("--- End of parsing test ---")

        # 清理建議 (您可以取消註釋以在測試後自動清理)
        # print("\\n--- Cleanup ---")
        # dummy_images_full_path = os.path.join(test_base_output_dir, IMAGES_BASE_SUBDIR, "dummy_exam_run_001")
        # if os.path.exists(dummy_images_full_path):
        #     import shutil
        #     try:
        #         shutil.rmtree(dummy_images_full_path)
        #         print(f"Removed dummy images directory: {dummy_images_full_path}")
        #     except Exception as e:
        #         print(f"Error removing dummy images directory {dummy_images_full_path}: {e}")
        
        # if os.path.exists(dummy_pdf_path):
        #     try:
        #         os.remove(dummy_pdf_path)
        #         print(f"Removed dummy PDF: {dummy_pdf_path}")
        #     except Exception as e:
        #         print(f"Error removing dummy PDF {dummy_pdf_path}: {e}")
        
        # test_images_base_folder = os.path.join(test_base_output_dir, IMAGES_BASE_SUBDIR)
        # if os.path.exists(test_images_base_folder) and not os.listdir(test_images_base_folder):
        #     try:
        #         os.rmdir(test_images_base_folder) # remove IMAGES_BASE_SUBDIR if empty
        #         print(f"Removed empty base image subdir: {test_images_base_folder}")
        #     except Exception as e:
        #         print(f"Error removing {test_images_base_folder}: {e}")

        # if os.path.exists(test_base_output_dir) and not os.listdir(test_base_output_dir):
        #     try:
        #         os.rmdir(test_base_output_dir) # remove test_base_output_dir if empty
        #         print(f"Removed empty test base output dir: {test_base_output_dir}")
        #     except Exception as e:
        #         print(f"Error removing {test_base_output_dir}: {e}")

    else:
        print("Skipping test with dummy PDF as it could not be created or found.")

    # 您原有的其他 __main__ 測試可以放在這裡，或者保持註釋狀態
    # # --- 測試 parse_questions_from_pdf_text ---
    # ... (您原來的測試代碼保持原樣或刪除/調整) ...
    pass # 原來的 pass 语句可以保留或移除


# if __name__ == "__main__":
#     # --- 測試 parse_questions_from_pdf_text ---
    # Test Case 1: "臨床生理學和病理學/111年_第一次/題目1111病理.pdf" - Should use "default" mode
    # question_pdf_path_test_pathology = "raw_data/exams/臨床血液學和血庫學/111年_第二次/題目1112血液.pdf"
    # logger.info(f"--- Testing parse_questions_from_pdf_text with {question_pdf_path_test_pathology} (MODE: default) ---")
    # question_text_pages_test_pathology = extract_text_from_pdf(question_pdf_path_test_pathology)
    # full_question_text_test_pathology = "\n".join(question_text_pages_test_pathology)
    # parsed_questions_test_pathology = parse_questions_from_pdf_text(full_question_text_test_pathology, parsing_mode="default")
    
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