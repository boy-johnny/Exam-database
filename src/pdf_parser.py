import os
import json
import logging
from pathlib import Path
import fitz  # PyMuPDF
import re
from typing import List, Dict, Any, Tuple, Optional
from enum import Enum, auto
# Import from config
from config import PROJECT_ROOT, PROCESSED_DATA_DIR

# 日誌設置
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
if not logger.hasHandlers():
    logger.addHandler(handler)

# processed_data 目錄 - Now imported from config.py
# PROJECT_ROOT = Path(__file__).resolve().parent.parent # Now imported
# PROCESSED_DATA_DIR = PROJECT_ROOT / "processed_data" # Now imported
PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True) # Ensure it exists if config didn't create it

# It's good practice to define constants for directory names
IMAGES_BASE_SUBDIR = "images_from_pdf" # Renamed to avoid conflict if you have other "images" dirs

# --- Regex Pattern Strings (Module Level) ---
# Used by determine_parsing_mode and then compiled in parsing functions
DEFAULT_QUESTION_START_REGEX_STR = r"^\s*(\d+)\s*\u002E\s*(.*)"
STRICT_QUESTION_START_REGEX_STR = r"^\s*(\d+)\s*\u002E(?!\d)\s*(.*)"
OPTION_REGEX_STR = (
    r"^\s*"
    r"(?:(?:[（\(])\s*([A-Z\uFF21-\uFF3A])\s*(?:[）\)])|"  # (A) or （Ａ）
    r"([A-Z\uFF21-\uFF3A])\s*\u002E)"  # A. or Ａ．
    r"\s*(.*)"  # Option text
)
# Anchored version for matching at the beginning of a block
ANCHORED_OPTION_REGEX_STR = OPTION_REGEX_STR # Already starts with ^\s* effectively

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

    # Compile patterns based on mode using module-level strings
    if parsing_mode == "strict_start":
        question_start_pattern = re.compile(STRICT_QUESTION_START_REGEX_STR)
        logger.info("Using STRICT_START question pattern for text parsing.")
    else: # default mode
        question_start_pattern = re.compile(DEFAULT_QUESTION_START_REGEX_STR)
        logger.info("Using DEFAULT question pattern for text parsing.")
    
    option_pattern = re.compile(OPTION_REGEX_STR) # This is used for line-based matching in this func

    lines = pdf_text.splitlines()
    # Ensure logger level is appropriate for these messages to appear
    # logger.setLevel(logging.DEBUG) # Consider setting this at a higher level if needed for testing

    for line_idx, line_raw in enumerate(lines):
        # Diagnostic logging for the first ~20 lines and any lines near where a question *should* be found
        if line_idx < 20 or (current_question and line_idx < 30): # Log more initial lines
            logger.info(f"PDFParse Line {line_idx + 1}/{len(lines)}: '{line_raw[:150]}'") # Log more chars
            logger.info(f"PDFParse Repr {line_idx + 1}: {repr(line_raw[:150])}")

        q_match = question_start_pattern.match(line_raw)
        opt_match = option_pattern.match(line_raw)

        if line_idx < 20 or (current_question and line_idx < 30):
            logger.info(f"  Attempting match on: {repr(line_raw)}")
            logger.info(f"  Q_match: {bool(q_match)} (Pattern: {question_start_pattern.pattern})")
            if q_match:
                logger.info(f"    Q_match groups: {q_match.groups()}")
            logger.info(f"  Opt_match: {bool(opt_match)} (Pattern: {option_pattern.pattern})")
            if opt_match:
                logger.info(f"    Opt_match groups: {opt_match.groups()}")

        if q_match: # New question starts
            logger.info(f"Line {line_idx + 1} matched QUESTION start: '{line_raw}'") # Changed to INFO for visibility
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
            logger.info(f"Line {line_idx + 1} matched OPTION start: '{line_raw}' for Q#{current_question.get('question_number')}") # Changed to INFO
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
            # Check for major discontinuities only if there's a significant number of questions
            if len(parsed_numbers) > 5: # Arbitrary threshold to avoid warnings on very short PDFs
                expected_max_number_strict = parsed_numbers[0] + len(parsed_numbers) - 1
                # Looser check: if the last parsed number is far beyond the expected sequence based on count,
                # or if the count is much smaller than the range of numbers.
                # This allows for a few missing numbers in sequence.
                # Example: 1, 2, 5, 6, 7 (parsed_numbers[-1]=7, expected_max_number_strict=1+5-1=5). Differs.
                # Number of gaps = (parsed_numbers[-1] - parsed_numbers[0] + 1) - len(parsed_numbers)
                num_gaps = (parsed_numbers[-1] - parsed_numbers[0] + 1) - len(parsed_numbers)
                # Warn if more than, say, 20% of questions are missing or if there are more than 5 gaps
                # These thresholds can be adjusted.
                if num_gaps > max(5, len(parsed_numbers) * 0.20) :
                    logger.warning(
                        f"Potential missing or misparsed questions. Parsed {len(parsed_numbers)} questions, "
                        f"with numbers from {parsed_numbers[0]} to {parsed_numbers[-1]}. "
                        f"Detected {num_gaps} gaps in the sequence. Check for discontinuities or parsing errors."
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
                                normalized_ans_char = normalize_full_width_alpha(ans_char_to_add)
                                current_answers.append({
                                    'text': normalized_ans_char, # 使用標準化後的字符
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
                normalized_corrected_ans = normalize_full_width_alpha(corrected_ans)
                notes[qn] = f"答案更正為 {normalized_corrected_ans}" # 也可以在筆記中用標準化字符
                answers[qn] = [normalized_corrected_ans] # 使用標準化後的答案

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
                    normalized_corrected_ans_val = normalize_full_width_alpha(corrected_ans_val)
                    actual_note = f"答案更正為 {normalized_corrected_ans_val}"
                    ans_to_set = [normalized_corrected_ans_val] # 使用標準化後的答案
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

def combine_questions_and_answers(
    questions: List[Dict[str, Any]],
    answers_map: Dict[int, List[str]],
    notes_map: Optional[Dict[int, str]] = None
) -> List[Dict[str, Any]]:
    """
    將題目與答案合併，補齊 correct_answer_key、notes 等欄位。
    若遇到特殊情況，notes 標註。
    """
    combined_data = []
    if notes_map is None:
        notes_map = {} # Ensure notes_map is a dict for easier lookup

    for question in questions:
        q_num = question.get("question_number")
        if q_num is None:
            logger.warning(f"Question found without question_number: {question.get('content', 'N/A')[:50]}... Skipping answer/note merging for this item.")
            combined_data.append(question) # Add as is, or decide on error handling
            continue

        # Initialize fields to ensure they exist, even if no data is found
        question_copy = question.copy() # Work on a copy
        question_copy["correct_answer_key"] = []
        question_copy["notes"] = None

        if q_num in answers_map:
            question_copy["correct_answer_key"] = answers_map[q_num]
             # 檢查 correct_answer_key 是否為 ["#"]
            if question_copy["correct_answer_key"] == ["#"]:
                note_for_hash_answer = "答案待確認" # 或者你可以用 "答案待確認" 等更通用的描述
                
                if question_copy["notes"]:
                    # 如果已經有來自 PDF 的備註，則附加新備註
                    question_copy["notes"] = f"{question_copy['notes']}\n{note_for_hash_answer}"
                else:
                    # 否則，直接設置新備註
                    question_copy["notes"] = note_for_hash_answer
        else:
            logger.warning(f"No answer found in answers_map for question number: {q_num}. Setting empty correct_answer_key.")

        if q_num in notes_map:
            question_copy["notes"] = notes_map[q_num]
        
        combined_data.append(question_copy)
        
    return combined_data

# 6. 存 processed_data ----------------------------

def save_processed_data(data: Dict[str, Any], out_path: str):
    """保存處理後的數據到 JSON 文件。"""
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)  # Create directory if it doesn't exist
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        logger.info(f"Processed data saved to: {out_path}")
    except IOError as e:
        logger.error(f"Error saving processed data to {out_path}: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred while saving processed data to {out_path}: {e}")

def save_raw_text(text_content: str, out_path: str):
    """保存提取的純文字到文件。"""
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)  # Create directory if it doesn't exist
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(text_content)
        logger.info(f"Raw text saved to: {out_path}")
    except IOError as e:
        logger.error(f"Error saving raw text to {out_path}: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred while saving raw text to {out_path}: {e}")

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
    3. 解析題目（包含圖片）與答案
    4. 合併
    5. 存 processed_data (只存 parsed JSON)
    """
    # 1. 讀取 PDF (題目卷文字用於元數據提取)
    full_question_text = extract_text_from_pdf(question_pdf_path)
    # 答案卷的文字提取會在 parse_answers_from_pdf_text 內部處理

    # 2. 提取元數據（使用完整的題目卷文本）
    meta = extract_metadata_from_text(full_question_text, os.path.basename(question_pdf_path))
    logger.info(f"Extracted metadata: {meta} (from {os.path.basename(question_pdf_path)})")

    # --- Construct output paths based on metadata ---
    subject_name_sanitized = sanitize_for_filesystem(meta.get('subject_name', 'UnknownSubject'))
    year_str = str(meta.get('year', 'UnknownYear'))
    period_str = str(meta.get('period', 'UnknownPeriod'))
    # exam_name_sanitized = sanitize_for_filesystem(meta.get('exam_name', 'UnknownExamName')) # No longer used for dir or file name directly

    # --- Create Subject Abbreviation ---
    subject_name_original = meta.get('subject_name')
    subject_short_name = "未知簡稱" # Default
    if subject_name_original:
        if "與" in subject_name_original:
            subject_short_name = subject_name_original.split("與")[0]
        elif "和" in subject_name_original:
            subject_short_name = subject_name_original.split("和")[0]
        elif "及" in subject_name_original: # Adding "及" based on "臨床血液學及血庫學" from schema examples
            subject_short_name = subject_name_original.split("及")[0]
        else:
            # Fallback: take first 2 chars if no common separator, common for abbreviations like 生化, 血液
            subject_short_name = subject_name_original[:2] 
    subject_short_name_sanitized = sanitize_for_filesystem(subject_short_name)
    
    year_period_folder = f"{year_str}_{period_str}"

    # Base directory for this specific exam's parsed files
    # Format: parsed/科目全名/年份_期次/
    parsed_exam_dir = PROCESSED_DATA_DIR / "parsed" / subject_name_sanitized / year_period_folder
    
    # Full file path for the parsed JSON
    # Format: parsed/科目全名/年份_期次/[年份][期次][科目簡稱].json
    output_filename = f"{year_str}{period_str}{subject_short_name_sanitized}.json"
    output_parsed_filepath = parsed_exam_dir / output_filename
    # --- End of output path construction ---

    # 3. 解析題目（包含圖片）與答案
    #   3a. 解析題目卷 (這會處理圖片提取並返回包含 image_path 的題目列表)
    logger.info(f"Starting to parse questions and images from: {question_pdf_path}")
    questions = parse_questions_from_pdf(pdf_path=question_pdf_path) # This now includes image paths
    logger.info(f"Parsed {len(questions)} question structures (including image references) from {question_pdf_path}.")

    #   3b. 解析答案卷
    logger.info(f"Starting to parse answers from: {answer_pdf_path}")
    raw_answers_output = parse_answers_from_pdf_text(answer_pdf_path)

    final_answers_map: Dict[int, List[str]] = {}
    final_notes_map: Optional[Dict[int, str]] = None 

    if isinstance(raw_answers_output, dict) and "answers" in raw_answers_output:
        final_answers_map = raw_answers_output.get("answers", {})
        final_notes_map = raw_answers_output.get("notes", {})
        if not isinstance(final_answers_map, dict):
             logger.warning(f"Type of 'answers' in raw_answers_output is not dict: {type(final_answers_map)}. Defaulting to empty.")
             final_answers_map = {}
        if final_notes_map is not None and not isinstance(final_notes_map, dict):
            logger.warning(f"Type of 'notes' in raw_answers_output is not dict: {type(final_notes_map)}. Defaulting to empty.")
            final_notes_map = {} # Ensure it's a dict if not None initially
        if not final_notes_map: # If it became an empty dict, set to None
            final_notes_map = None
    elif isinstance(raw_answers_output, dict):
        # Check if it's a direct Dict[int, List[str]] answers map
        if all(isinstance(k, int) and isinstance(v, list) for k, v in raw_answers_output.items()):
            final_answers_map = raw_answers_output
        else:
            logger.warning(f"Received a dict from parse_answers_from_pdf_text that is not a direct answer map (Dict[int, List[str]]) nor the {{'answers':..., 'notes':...}} structure: {raw_answers_output}. Treating as empty answers.")
            final_answers_map = {}
    else:
        logger.warning(f"Unexpected data type from parse_answers_from_pdf_text: {type(raw_answers_output)}. Expected dict. Treating as empty answers.")
        final_answers_map = {}

    # 4. 合併
    # Check if question_count from metadata matches parsed questions if available
    meta_q_count = meta.get("question_count")
    if meta_q_count is not None:
        if len(questions) != meta_q_count:
            logger.warning(
                f"Mismatch in question count: metadata says {meta_q_count}, "
                f"parsed {len(questions)} questions from {question_pdf_path}"
            )
    else:
        logger.info(f"Metadata did not provide question_count for {os.path.basename(question_pdf_path)}. Skipping count check.")

    combined_questions_list = combine_questions_and_answers(
        questions,
        final_answers_map,
        final_notes_map # Pass the extracted notes_map
    )
    combined_data = {
        "meta": meta,
        "questions": combined_questions_list
    }

    # 5. 存 processed_data (只存 parsed JSON)
    save_processed_data(combined_data, str(output_parsed_filepath)) # Ensure path is string for os.makedirs
    
    logger.info(f"Process complete for exam based on {os.path.basename(question_pdf_path)}")
    logger.info(f"Parsed data saved to: {output_parsed_filepath}")
    # Raw text saving is removed as per request

    return combined_data, str(output_parsed_filepath)

# 8. 新增：逐頁解析題目並提取圖片的函數

def parse_questions_from_pdf(
    pdf_path: str,
    # base_output_dir: str, # 例如: "processed_data" 或測試時的 "test_processed_data" --- 會被重新定義
    # test_id_for_images: str, # 例如: "111_first_biochemistry" ---不再需要，由pdf_path推斷
    # 以下參數將傳遞給您現有的 parse_questions_from_pdf_text
    # 您可能需要從您的配置中獲取 question_start_pattern 和 option_pattern
    # 這裡我們假設它們會被傳入，或者在函數內部根據 pdf_path 決定
    # current_parsing_mode: str # 這個由 determine_parsing_mode 決定
):
    """
    從 PDF 文件中逐頁解析題目、選項，並提取關聯的圖片。
    圖片將保存到 processed_data/image/結構化路徑/原始文件名/圖片文件.png

    Args:
        pdf_path: PDF 文件的路徑。
        # base_output_dir: 將被內部設置為 PROJECT_ROOT / "processed_data"
        # test_id_for_images: 不再使用

    Returns:
        一個字典列表，每個字典包含題目數據及 'image_path' 和 'page_number'。
    """
    all_parsed_questions_data = []
    
    # 重新定義基礎輸出目錄的根 - 使用從 config 導入的 PROCESSED_DATA_DIR
    actual_image_root_dir = PROCESSED_DATA_DIR / "image" # 新的根目錄

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        logger.error(f"Error opening PDF {pdf_path}: {e}")
        return all_parsed_questions_data

    # 從 pdf_path 中提取科目、年份等結構化路徑和原始文件名
    try:
        # 例如 pdf_path = "raw_data/exams/科目A/111年_第一次/題目XYZ.pdf"
        # 我們要提取 "科目A/111年_第一次" 和 "題目XYZ"
        path_obj = Path(pdf_path)
        
        # 假設 "raw_data/exams" 是固定前綴
        # 我們需要找到 "exams" 這部分的索引
        parts = path_obj.parts
        exams_index = -1
        for i, part in enumerate(parts):
            if part == "exams":
                exams_index = i
                break
        
        if exams_index == -1 or exams_index + 1 >= len(parts) -1: # exams 後面必須有科目等文件夾，然後才是文件名
            logger.warning(f"Could not determine structured path from pdf_path: {pdf_path}. Using flat structure.")
            structured_path_parts = []
        else:
            # "科目A/111年_第一次"
            structured_path_parts = list(parts[exams_index + 1 : -1]) # exams 後面到文件名之前的部分
            
        original_pdf_filename_no_ext = path_obj.stem # "題目XYZ"

    except Exception as e_path:
        logger.error(f"Error processing pdf_path for output structure: {e_path}. Defaulting to flat structure.")
        structured_path_parts = []
        original_pdf_filename_no_ext = Path(pdf_path).stem


    # 為本次測驗的圖片創建特定的輸出子目錄
    # 例如: processed_data/image/科目A/111年_第一次/題目XYZ/
    current_exam_images_dir = actual_image_root_dir # Start with PROCESSED_DATA_DIR / "image"
    if structured_path_parts:
        current_exam_images_dir = current_exam_images_dir.joinpath(*structured_path_parts)
    current_exam_images_dir = current_exam_images_dir / original_pdf_filename_no_ext
    
    os.makedirs(current_exam_images_dir, exist_ok=True)
    logger.info(f"Images for {pdf_path} will be saved in: {current_exam_images_dir}")


    # Compile OPTION_RE_ANCHORED for block parsing if needed, or use OPTION_REGEX_STR directly with re.match
    # For block matching, we usually want patterns anchored at the start.
    # OPTION_REGEX_STR itself is suitable for re.match() if we strip the block text first.
    anchored_option_pattern_for_blocks = re.compile(OPTION_REGEX_STR) # Same as OPTION_RE_STR for re.match on stripped lines

    # Determine parsing mode for question_start_pattern
    current_parsing_mode = determine_parsing_mode(pdf_path)
    if current_parsing_mode == "strict_start":
        current_question_start_pattern_for_blocks = re.compile(STRICT_QUESTION_START_REGEX_STR)
    else:
        current_question_start_pattern_for_blocks = re.compile(DEFAULT_QUESTION_START_REGEX_STR)

    for page_num, page in enumerate(doc):
        page_actual_number = page_num + 1
        page_image_data_list = [] # 存儲本頁提取出的所有圖片的路徑和 bounding boxes

        # 1. 提取並保存本頁所有圖片
        img_list = page.get_images(full=True)
        for img_index, img_info in enumerate(img_list):
            xref = img_info[0]
            try:
                pix = fitz.Pixmap(doc, xref)
                # 文件名格式: {原始PDF文件名(不含副檔名)}-page{頁數}-img{圖片索引}.png
                image_filename = f"{original_pdf_filename_no_ext}-page{page_actual_number}-img{img_index}.png"
                image_save_path = current_exam_images_dir / image_filename
                img_bbox_on_page = page.get_image_bbox(img_info)

                if pix.n - pix.alpha < 4: # not CMYK or GRAY
                    if not os.path.exists(image_save_path):
                        pix.save(image_save_path)
                else: # CMYK: convert to RGB first
                    if not os.path.exists(image_save_path):
                        pix_rgb = fitz.Pixmap(fitz.csRGB, pix)
                        pix_rgb.save(image_save_path)
                        pix_rgb = None # Release memory
                
                # Convert absolute image_save_path to be relative to PROCESSED_DATA_DIR
                relative_image_path = os.path.relpath(image_save_path, PROCESSED_DATA_DIR)
                # Ensure platform-independent path separators (forward slashes)
                relative_image_path_posix = Path(relative_image_path).as_posix()

                page_image_data_list.append({
                    "path": relative_image_path_posix, # Store the relative POSIX path
                    "bbox": img_bbox_on_page
                })
                pix = None
                logger.debug(f"Saved image {image_filename} from page {page_actual_number}")
                
            except Exception as e:
                logger.error(f"Error processing image xref {xref} on page {page_actual_number} of {pdf_path}: {e}")

        # 2. 提取本頁文本塊 (此部分保留，因為獲取文本塊本身是有用的)
        text_blocks_on_page = []
        raw_blocks = page.get_text("blocks", sort=True) 
        for block_tuple in raw_blocks:
            if block_tuple[6] == 0: # TEXT block
                text_blocks_on_page.append({
                    "text": block_tuple[4],
                    "bbox": fitz.Rect(block_tuple[0:4])
                })
        
        # 3. 使用您現有的 parse_questions_from_pdf_text 解析本頁題目結構 (Existing)
        page_text_for_parser = page.get_text("text") 
        logger.debug(f"--- Page {page_actual_number} Raw Text for Parser ---")
        logger.debug(page_text_for_parser[:1000]) # Log first 1000 chars of page text
        logger.debug("--- End of Page Raw Text ---")
        
        questions_text_data = parse_questions_from_pdf_text(page_text_for_parser, parsing_mode=current_parsing_mode)
        
        if not questions_text_data:
            logger.warning(f"[Page {page_actual_number}] No questions parsed by parse_questions_from_pdf_text. Skipping BBox association for this page.")

        # 4. 針對每個解析出的題目，查找其 BBox 並重新關聯圖片
        for q_text_data in questions_text_data:
            current_q_dict = q_text_data.copy()
            current_q_dict['image_path'] = None # Reset from previous basic association
            current_q_dict['page_number'] = page_actual_number
            
            q_num_as_int = current_q_dict['question_number']
            
            stem_found_blocks = []
            first_option_y0_for_q = float('inf')
            active_question_parsing_state = "looking_for_stem_start"
            
            for block_idx, block_item in enumerate(text_blocks_on_page):
                block_text_content = block_item["text"]
                block_text_stripped_lines = [line.strip() for line in block_text_content.splitlines() if line.strip()]
                if not block_text_stripped_lines:
                    continue
                first_line_of_block = block_text_stripped_lines[0]

                if active_question_parsing_state == "looking_for_stem_start":
                    q_start_match = current_question_start_pattern_for_blocks.match(first_line_of_block)
                    if q_start_match and int(q_start_match.group(1)) == q_num_as_int:
                        active_question_parsing_state = "in_stem"
                        stem_found_blocks.append(block_item["bbox"])
                        option_match_in_same_block = anchored_option_pattern_for_blocks.match(q_start_match.group(2).strip())
                        if not option_match_in_same_block:
                             for L_idx, line_in_block in enumerate(block_text_stripped_lines):
                                 if L_idx == 0 and q_start_match.group(2).strip(): continue
                                 if anchored_option_pattern_for_blocks.match(line_in_block):
                                     first_option_y0_for_q = min(first_option_y0_for_q, block_item["bbox"].y0)
                                     active_question_parsing_state = "looking_for_options"
                                     break 
                             if active_question_parsing_state == "looking_for_options": break
                        else: 
                            first_option_y0_for_q = min(first_option_y0_for_q, block_item["bbox"].y0)
                            active_question_parsing_state = "looking_for_options"
                            break
                elif active_question_parsing_state == "in_stem":
                    option_match = anchored_option_pattern_for_blocks.match(first_line_of_block)
                    if option_match:
                        first_option_y0_for_q = min(first_option_y0_for_q, block_item["bbox"].y0)
                        active_question_parsing_state = "looking_for_options"
                        break
                    next_q_match = current_question_start_pattern_for_blocks.match(first_line_of_block)
                    if next_q_match and int(next_q_match.group(1)) == q_num_as_int + 1:
                        active_question_parsing_state = "looking_for_stem_start"
                        break
                    stem_found_blocks.append(block_item["bbox"])

            main_stem_bbox = None
            if stem_found_blocks:
                main_stem_bbox = stem_found_blocks[0]
                for bbox in stem_found_blocks[1:]:
                    main_stem_bbox.include_rect(bbox)
            
            if main_stem_bbox: # Only proceed if stem was found
                best_img_path_for_q = None
                min_v_dist_to_stem = float('inf')

                for img_item in page_image_data_list:
                    img_bbox = img_item["bbox"]
                    image_starts_below_stem = img_bbox.y0 > main_stem_bbox.y1
                    horizontal_overlap = (max(main_stem_bbox.x0, img_bbox.x0) < min(main_stem_bbox.x1, img_bbox.x1))

                    if image_starts_below_stem and horizontal_overlap:
                        image_ends_above_options = True
                        if first_option_y0_for_q != float('inf'):
                            if img_bbox.y1 >= first_option_y0_for_q:
                                image_ends_above_options = False
                        
                        if image_ends_above_options:
                            vertical_distance = img_bbox.y0 - main_stem_bbox.y1
                            if 0 <= vertical_distance < 150: 
                                if vertical_distance < min_v_dist_to_stem:
                                    min_v_dist_to_stem = vertical_distance
                                    best_img_path_for_q = img_item["path"] # Store the path string
                
                current_q_dict['image_path'] = best_img_path_for_q
            else:
                current_q_dict['image_path'] = None # Stem not found, so no image association
            
            all_parsed_questions_data.append(current_q_dict)

    doc.close()
    logger.info(f"Finished parsing {pdf_path}. Total questions extracted: {len(all_parsed_questions_data)}")
    return all_parsed_questions_data

# --- Helper Functions ---
def normalize_full_width_alpha(text: str) -> str:
    """Converts full-width Latin alphabet characters in a string to half-width."""
    if not text:
        return ""
    normalized_chars = []
    for char_val in [ord(c) for c in text]:
        if 0xFF21 <= char_val <= 0xFF3A:  # Full-width A-Z
            normalized_chars.append(chr(char_val - 0xFEE0))
        elif 0xFF41 <= char_val <= 0xFF5A: # Full-width a-z
            normalized_chars.append(chr(char_val - 0xFEE0))
        else:
            normalized_chars.append(chr(char_val))
    return "".join(normalized_chars)

def sanitize_for_filesystem(name: str) -> str:
    """Sanitizes a string to be used as a filename or directory name."""
    if not name:
        return "untitled"
    # Replace common problematic characters with underscore
    name = re.sub(r'[\\\\/:*?"<>|]', '_', name)
    # Remove leading/trailing whitespace and dots, and control characters
    name = name.strip(" .\\t\\n\\r\\f\\v")
    name = re.sub(r'[\\x00-\\x1F\\x7F]', '', name) # Remove control characters
    # Reduce multiple underscores to one
    name = re.sub(r'_+', '_', name)
    # Limit length
    return name[:150]

if __name__ == "__main__":
    logger.setLevel(logging.DEBUG) 
    print(" executing pdf_parser.py directly for testing...")
    try:
        print(f"PyMuPDF (fitz) version: {fitz.__doc__}")
    except Exception as e_version:
        print(f"Could not retrieve fitz version: {e_version}")

    # --- 測試 process_exam_pdfs (題目解析、答案解析與合併) ---
    print("\\n--- Testing process_exam_pdfs ---")
    
    # ** 請將以下路徑替換為您實際的測試文件路徑 **
    test_question_pdf_path = "raw_data/exams/生物化學與臨床生化學/111年_第一次/題目1111生化.pdf" # 示例：題目卷
    test_answer_pdf_path = "raw_data/exams/生物化學與臨床生化學/111年_第一次/答案1111生化.pdf"   # 示例：答案卷
    test_output_prefix = "test_biochemistry_111_1" # processed_prefix is not directly used for filename anymore in process_exam_pdfs

    if os.path.exists(test_question_pdf_path) and os.path.exists(test_answer_pdf_path):
        print(f"Processing Question PDF: {test_question_pdf_path}")
        print(f"Processing Answer PDF: {test_answer_pdf_path}")
        try:
            result_tuple = process_exam_pdfs(
                question_pdf_path=test_question_pdf_path,
                answer_pdf_path=test_answer_pdf_path,
                processed_prefix=test_output_prefix # This argument is kept for now but not used for output naming directly
            )
            
            # Construct the expected output path based on how process_exam_pdfs now saves it
            # This requires metadata from the PDF, so it's harder to predict here without parsing meta first.
            # For testing, you might need to inspect the log output from process_exam_pdfs to find the exact path.
            # Or, if process_exam_pdfs returns the path:
            if isinstance(result_tuple, tuple) and len(result_tuple) == 2:
                output_json_path = result_tuple[1] # process_exam_pdfs returns (combined_data, output_filepath)
                print(f"\\nProcessing complete. Merged data saved to: {output_json_path}")
                print("You can open this JSON file to inspect the merged questions, answers, and notes.")

                if os.path.exists(output_json_path):
                    with open(output_json_path, 'r', encoding='utf-8') as f:
                        merged_output = json.load(f)
                    
                    print("\\n--- Snippet of Merged Data ---")
                    if "meta" in merged_output:
                        print(f"Meta: {merged_output['meta']}")
                    if "questions" in merged_output and merged_output["questions"]:
                        print(f"Total questions in output: {len(merged_output['questions'])}")
                        for i, q_data in enumerate(merged_output["questions"][:2]): # 打印前2條題目
                            print(f"  Q{q_data.get('question_number')}: {q_data.get('content', 'N/A')[:50]}...")
                            print(f"    Options: {q_data.get('options', {})}")
                            print(f"    Correct Answer: {q_data.get('correct_answer_key')}")
                            print(f"    Notes: {q_data.get('notes')}")
                            print(f"    Image Path: {q_data.get('image_path')}") 
                            print(f"    Page Number: {q_data.get('page_number')}")
                    else:
                        print("No questions found in the merged output.")
                else:
                    print(f"Could not find the output file for snippet display: {output_json_path}")
            else:
                print(f"process_exam_pdfs did not return the expected output. Result: {result_tuple}")


        except Exception as e:
            print(f"Error during process_exam_pdfs test: {e}")
            logger.error(f"Error in __main__ during process_exam_pdfs test: {e}", exc_info=True)
    else:
        print(f"Skipping process_exam_pdfs test. Please ensure both question PDF and answer PDF exist at:")
        print(f"  Question PDF path: {test_question_pdf_path}")
        print(f"  Answer PDF path: {test_answer_pdf_path}")
        
    print("\\n--- End of pdf_parser.py execution ---")
    pass