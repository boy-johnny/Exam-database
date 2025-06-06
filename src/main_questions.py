import os
import logging
from pathlib import Path

# 從專案模組導入
from config import RAW_DATA_EXAMS_DIR, LOG_LEVEL, LOGS_DIR
from pdf_parser import process_exam_pdfs # 主要的PDF處理函數
from db_handler import get_or_create_subject, get_or_create_test, batch_insert_questions, get_supabase_client

# 配置日誌
# 主腳本的日誌可以有自己的特色，或者複用 config 中的設定
log_file_path = LOGS_DIR / "main_questions.log"
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(), # 輸出到控制台
        logging.FileHandler(log_file_path, encoding='utf-8') # 輸出到文件
    ]
)
logger = logging.getLogger(__name__)

def process_single_exam_set(subject_name_from_folder: str, year_period_folder_name: str, question_pdf_path: Path, answer_pdf_path: Path):
    """
    處理單一科目的特定年份期次的試卷（題目卷和答案卷）。
    1. 調用 pdf_parser 進行解析。
    2. 調用 db_handler 與資料庫交互。
    """
    logger.info(f"開始處理試卷組：科目資料夾='{subject_name_from_folder}', 年份期次='{year_period_folder_name}'")
    logger.info(f"題目卷路徑: {question_pdf_path}")
    logger.info(f"答案卷路徑: {answer_pdf_path}")

    if not question_pdf_path.exists():
        logger.error(f"題目卷未找到: {question_pdf_path}")
        return
    if not answer_pdf_path.exists():
        logger.error(f"答案卷未找到: {answer_pdf_path}")
        return

    # 1. 解析 PDF
    # process_exam_pdfs 的 processed_prefix 參數在新的檔名邏輯下不太重要，但函數簽名仍需要它
    # 可以傳遞一個基於當前處理上下文的描述性字符串
    descriptive_prefix = f"{subject_name_from_folder}_{year_period_folder_name}"
    try:
        parsed_data, output_json_path = process_exam_pdfs(
            question_pdf_path=str(question_pdf_path),
            answer_pdf_path=str(answer_pdf_path),
            processed_prefix=descriptive_prefix
        )
        logger.info(f"PDF 解析完成。結果保存在: {output_json_path}")
    except Exception as e:
        logger.error(f"處理 PDF 失敗 (科:{subject_name_from_folder}, 年期:{year_period_folder_name}): {e}", exc_info=True)
        return

    # 2. 資料庫操作
    if not parsed_data or 'meta' not in parsed_data or 'questions' not in parsed_data:
        logger.error(f"從 pdf_parser 獲取的 parsed_data 格式不正確或為空。跳過資料庫操作。")
        return

    metadata = parsed_data['meta']
    questions_list_from_parser = parsed_data['questions']

    db_client = get_supabase_client()
    if not db_client:
        logger.error("Supabase client 未初始化，無法進行資料庫操作。")
        return

    #   a. 獲取/創建 Subject
    subject_name_from_meta = metadata.get('subject_name')
    if not subject_name_from_meta:
        logger.error(f"元數據中未找到科目名稱 (subject_name)。使用資料夾名稱 '{subject_name_from_folder}' 作為備用。")
        # 可以選擇是否使用 folder name 作為 fallback，或直接報錯跳過
        # subject_name_for_db = subject_name_from_folder
        logger.error("由於元數據缺乏科目名稱，無法繼續處理此試卷的資料庫部分。")
        return

    subject_id = get_or_create_subject(subject_name_from_meta)
    if not subject_id:
        logger.error(f"獲取或創建科目 '{subject_name_from_meta}' 失敗。跳過此試卷的其餘資料庫操作。")
        return
    logger.info(f"科目 '{subject_name_from_meta}' 的 ID: {subject_id}")

    #   b. 獲取/創建 Test
    exam_name = metadata.get('exam_name')
    year = metadata.get('year')
    period = metadata.get('period')
    subject_code = metadata.get('subject_code') # 從meta提取
    question_count_from_meta = metadata.get('question_count') # 從meta提取


    if not exam_name or year is None or period is None:
        logger.error(f"元數據中缺乏考卷名稱、年份或期次。 Name: {exam_name}, Year: {year}, Period: {period}。跳過創建 Test。")
        return

    test_id = get_or_create_test(
        test_name=exam_name,
        year=int(year),
        period=int(period),
        subject_id=subject_id,
        subject_code=subject_code, # 傳遞 subject_code
        question_count=question_count_from_meta # 傳遞 question_count
        # description 和 duration_in_seconds 可以根據需要從元數據提取或設置默認值
    )
    if not test_id:
        logger.error(f"獲取或創建考卷 '{exam_name}' (年份:{year}, 期次:{period}) 失敗。跳過題目插入。")
        return
    logger.info(f"考卷 '{exam_name}' 的 ID: {test_id}")

    #   c. 準備並批量插入 Questions
    questions_for_db = []
    if not questions_list_from_parser:
        logger.info(f"解析器未返回任何題目 (科:{subject_name_from_meta}, 年期:{year_period_folder_name})。沒有題目可插入資料庫。")
    
    for q_data in questions_list_from_parser:
        if not isinstance(q_data, dict):
            logger.warning(f"在題目列表發現非字典項目: {q_data}。跳過此項目。")
            continue
        
        content = q_data.get('content')
        options = q_data.get('options')
        correct_answer_key = q_data.get('correct_answer_key')
        question_number_parsed = q_data.get('question_number')
        image_path_parsed = q_data.get('image_path') # 可能是 None
        notes_parsed = q_data.get('notes') # 可能是 None
        page_number = q_data.get('page_number') # 新增，來自 pdf_parser

        if content is None or options is None or correct_answer_key is None or question_number_parsed is None:
            logger.warning(f"題目數據不完整 (QN:{question_number_parsed})，缺少必要欄位。Content: {content is not None}, Options: {options is not None}, AnswerKey: {correct_answer_key is not None}, Number: {question_number_parsed is not None}。跳過此題目。")
            continue

        questions_for_db.append({
            "content": content,
            "options": options, # 應該是 JSONB 兼容的 dict
            "correct_answer_key": correct_answer_key, # 應該是 text[]
            "question_number": int(question_number_parsed),
            "test_id": test_id,
            "image_path": image_path_parsed, # 存儲相對路徑或 None
            "notes": notes_parsed,           # 存儲題目相關備註或 None
            "page_number": page_number,      # 存儲題目所在頁碼
            # "chapter_id": None, # 暫時為 None
            # "explanation": None, # 暫時為 None
            # "tags": None, # 暫時為 None
        })

    if questions_for_db:
        logger.info(f"準備插入 {len(questions_for_db)} 條題目到資料庫 (Test ID: {test_id})...")
        successful_inserts, failed_inserts = batch_insert_questions(questions_for_db)
        logger.info(f"批量插入題目完成。成功: {len(successful_inserts)}, 失敗: {len(failed_inserts)}.")
        if failed_inserts:
            logger.error(f"以下題目插入失敗 (Test ID: {test_id}):")
            for idx, failed_q_data in enumerate(failed_inserts):
                logger.error(f"  失敗題目 #{idx+1}: QN {failed_q_data.get('question_number', 'N/A')}, Content: {str(failed_q_data.get('content', 'N/A'))[:50]}...")
    else:
        logger.info(f"沒有準備好插入資料庫的題目 (Test ID: {test_id}).")

    logger.info(f"試卷組處理完成：'{subject_name_from_folder}', 年份期次='{year_period_folder_name}'")


def process_all_exam_pdfs():
    """
    遍歷 RAW_DATA_EXAMS_DIR 下的所有 PDF 文件，
    找到對應的題目卷和答案卷，然後進行處理。
    """
    logger.info(f"開始遍歷考古題目錄: {RAW_DATA_EXAMS_DIR}")
    if not RAW_DATA_EXAMS_DIR.exists():
        logger.error(f"原始考題目錄不存在: {RAW_DATA_EXAMS_DIR}")
        return

    processed_count = 0
    skipped_count = 0

    for subject_dir in RAW_DATA_EXAMS_DIR.iterdir():
        if subject_dir.is_dir():
            subject_name = subject_dir.name # 科目名稱，例如 "臨床血液學與血庫學"
            logger.info(f"處理科目: {subject_name}")
            for year_period_dir in subject_dir.iterdir():
                if year_period_dir.is_dir():
                    year_period_name = year_period_dir.name # 年份期次，例如 "111年_第一次"
                    logger.info(f"  處理年份/期次: {year_period_name}")

                    question_pdf: Optional[Path] = None
                    answer_pdf: Optional[Path] = None

                    # 查找題目卷和答案卷
                    # 假設檔名包含 "題目" 和 "答案"
                    # 檔名格式可能為: 題目[年份][期次][科目簡稱].pdf, 答案_[年份][期次][科目簡稱].pdf
                    # 或更通用的，如 題目1111血液.pdf
                    
                    # 簡單的查找邏輯，可以根據實際檔名模式調整
                    for pdf_file in year_period_dir.glob("*.pdf"):
                        if "題目" in pdf_file.name:
                            if question_pdf:
                                logger.warning(f"在 {year_period_dir} 中找到多個題目卷，將使用第一個找到的: {question_pdf.name} (忽略 {pdf_file.name}) ")
                            else:
                                question_pdf = pdf_file
                        elif "答案" in pdf_file.name:
                            if answer_pdf:
                                logger.warning(f"在 {year_period_dir} 中找到多個答案卷，將使用第一個找到的: {answer_pdf.name} (忽略 {pdf_file.name}) ")
                            else:
                                answer_pdf = pdf_file
                    
                    if question_pdf and answer_pdf:
                        try:
                            process_single_exam_set(subject_name, year_period_name, question_pdf, answer_pdf)
                            processed_count += 1
                        except Exception as e:
                            logger.error(f"處理科目 '{subject_name}' 年份/期次 '{year_period_name}' 時發生未預期錯誤: {e}", exc_info=True)
                            skipped_count +=1
                    else:
                        logger.warning(f"在 {year_period_dir} 中未能同時找到題目卷和答案卷。")
                        if not question_pdf:
                            logger.warning("  - 題目卷缺失")
                        if not answer_pdf:
                            logger.warning("  - 答案卷缺失")
                        skipped_count += 1
    
    logger.info(f"所有考古題目錄遍歷完成。成功處理試卷組數量: {processed_count}, 跳過/失敗數量: {skipped_count}")

if __name__ == "__main__":
    logger.info("--- main_questions.py: 開始執行考古題處理主流程 ---")
    
    # 檢查 Supabase client 是否可用
    client = get_supabase_client()
    if not client:
        logger.error("Supabase client 未初始化或初始化失敗。請檢查 config.py 和 .env 檔案以及網路連線。腳本終止。")
    else:
        logger.info("Supabase client 已成功初始化。")
        process_all_exam_pdfs()
        
    logger.info("--- main_questions.py: 考古題處理主流程執行完畢 ---") 