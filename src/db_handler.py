#!/usr/bin/python
# -*- coding: UTF-8 -*-
import logging
import re # <--- 新增導入
from supabase import create_client, Client
from typing import List, Dict, Any, Optional, Tuple

# 從 config.py 導入 Supabase 的 URL 和 KEY，以及日誌配置
try:
    from config import SUPABASE_URL, SUPABASE_KEY, LOG_LEVEL
except ImportError:
    print("🔴 Error: config.py not found or SUPABASE_URL/SUPABASE_KEY not defined. Make sure config.py is in the same directory or accessible via PYTHONPATH.")
    # 提供一些默認值以允許代碼至少能夠被解析，但在實際運行時會失敗
    SUPABASE_URL = ""
    SUPABASE_KEY = ""
    LOG_LEVEL = "INFO"


# 配置日誌
logging.basicConfig(level=LOG_LEVEL,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    handlers=[logging.StreamHandler()]) # 之後可以加入 FileHandler
logger = logging.getLogger(__name__)

# 初始化 Supabase client
supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase client initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize Supabase client: {e}")
        supabase = None # 確保 supabase 為 None 如果初始化失敗
else:
    logger.error("Supabase URL or Key is missing. Supabase client not initialized.")


def get_supabase_client() -> Optional[Client]:
    """返回 Supabase client 實例，如果未初始化則記錄錯誤並返回 None。"""
    if supabase is None:
        logger.error("Supabase client is not initialized. Cannot perform database operations.")
    return supabase

# +++ 新增 slug 生成函數 +++
def generate_slug(name: str) -> str:
    """為給定的名稱生成一個 URL友好的 slug。"""
    s = name.lower()
    # 移除特定標點符號，替換空格為連字符
    # 這裡的正規表達式可以根據需要調整以更好地處理中文或特定字符
    s = re.sub(r'[\s.]+', '-', s) # 將空格和點替換為連字符
    s = re.sub(r'[^a-z0-9\-一-鿿]', '', s) # 移除非字母數字、非連字符、非中文字符
    s = re.sub(r'-+', '-', s) # 將多個連字符替換為單個
    s = s.strip('-') # 移除開頭和結尾的連字符
    if not s: # 如果 slug 為空 (例如，名稱只包含特殊字符)
        # 可以基於uuid生成一個隨機slug，或者返回一個默認值
        # import uuid
        # return str(uuid.uuid4())[:8]
        return "default-slug" # 或者根據您的需求處理
    return s
# --- get_or_create 系列函數 ---

def get_or_create_subject(subject_name: str) -> Optional[str]:
    """
    根據科目名稱獲取或創建科目記錄。
    返回科目 ID (uuid)。
    """
    client = get_supabase_client()
    if not client:
        return None

    try:
        # 1. 嘗試查詢現有科目
        response = client.table("subjects").select("id").eq("name", subject_name).execute()
        logger.debug(f"Query subject response: {response}")

        if response.data:
            subject_id = response.data[0]["id"]
            logger.info(f"Found existing subject '{subject_name}' with id: {subject_id}")
            return subject_id
        else:
            # 2. 如果不存在，則創建新科目
            logger.info(f"Subject '{subject_name}' not found, creating new one...")
            subject_slug = generate_slug(subject_name) # <--- 生成 slug
            insert_data = {"name": subject_name, "slug": subject_slug} # <--- 加入 slug
            logger.info(f"Attempting to insert subject with data: {insert_data}") # 添加日誌
            insert_response = client.table("subjects").insert(insert_data).execute()
            logger.debug(f"Insert subject response: {insert_response}")
            if insert_response.data:
                subject_id = insert_response.data[0]["id"]
                logger.info(f"Created new subject '{subject_name}' with slug '{subject_slug}' and id: {subject_id}")
                return subject_id
            else:
                # 檢查 insert_response.error 是否有更多信息
                error_message = "Unknown error"
                if insert_response.error:
                    error_message = f"Code: {insert_response.error.code}, Message: {insert_response.error.message}, Details: {insert_response.error.details}, Hint: {insert_response.error.hint}"
                logger.error(f"Failed to create subject '{subject_name}'. Response: {error_message}")
                return None
    except Exception as e:
        logger.error(f"Error in get_or_create_subject for '{subject_name}': {e}", exc_info=True)
        return None

def get_or_create_chapter(chapter_title: str, subject_id: str, order: Optional[int] = None) -> Optional[str]:
    """
    根據章節標題和科目ID獲取或創建章節記錄。
    返回章節 ID (uuid)。
    """
    client = get_supabase_client()
    if not client:
        return None

    try:
        # 1. 嘗試查詢現有章節 (標題和 subject_id 必須都匹配)
        query = client.table("chapters").select("id").eq("title", chapter_title).eq("subject_id", subject_id)
        
        response = query.execute()
        logger.debug(f"Query chapter response: {response}")

        if response.data:
            chapter_id = response.data[0]["id"]
            logger.info(f"Found existing chapter '{chapter_title}' for subject_id '{subject_id}' with id: {chapter_id}")
            # (可選) 如果提供了 order，可以考慮更新現有章節的 order
            if order is not None:
                current_order_res = client.table("chapters").select("order").eq("id", chapter_id).single().execute()
                current_order = current_order_res.data.get("order") if current_order_res.data else None
                if current_order != order:
                    update_data = {"order": order}
                    update_response = client.table("chapters").update(update_data).eq("id", chapter_id).execute()
                    if update_response.data:
                        logger.info(f"Updated order for chapter '{chapter_id}' from {current_order} to {order}.")
                    else:
                        logger.warning(f"Failed to update order for chapter '{chapter_id}'. Error: {update_response.error}")
                else:
                    logger.info(f"Order for chapter '{chapter_id}' is already {order}. No update needed.")
            return chapter_id
        else:
            # 2. 如果不存在，則創建新章節
            logger.info(f"Chapter '{chapter_title}' for subject_id '{subject_id}' not found, creating new one...")
            chapter_data = {"title": chapter_title, "subject_id": subject_id}
            if order is not None:
                chapter_data["order"] = order
            
            insert_response = client.table("chapters").insert(chapter_data).execute()
            logger.debug(f"Insert chapter response: {insert_response}")

            if insert_response.data:
                chapter_id = insert_response.data[0]["id"]
                logger.info(f"Created new chapter '{chapter_title}' with id: {chapter_id}")
                return chapter_id
            else:
                logger.error(f"Failed to create chapter '{chapter_title}'. Response: {insert_response.error}")
                return None
    except Exception as e:
        logger.error(f"Error in get_or_create_chapter for '{chapter_title}': {e}", exc_info=True)
        return None

def get_or_create_test(
    test_name: str, 
    year: int, 
    period: int, 
    subject_id: str, 
    duration_in_seconds: Optional[int] = 3600, 
    description: Optional[str] = None,
    subject_code: Optional[str] = None,      # 新增參數
    question_count: Optional[int] = None     # 新增參數
) -> Optional[str]:
    """
    根據考卷全名、年份、期次和科目ID獲取或創建考卷記錄。
    duration_in_seconds 默認為 3600。
    description, subject_code, question_count 為可選。
    返回考卷 ID (uuid)。
    """
    client = get_supabase_client()
    if not client:
        return None

    try:
        # 1. 嘗試查詢現有考卷 (subject_id, year, period 應該是唯一組合)
        response = client.table("tests").select("id, name, duration_in_seconds, description") \
            .eq("subject_id", subject_id) \
            .eq("year", year) \
            .eq("period", period) \
            .maybe_single() # 使用 maybe_single() 因為 (subject_id, year, period) 應該是唯一的
        
        query_response = client.table("tests").select("id, name, duration_in_seconds, description") \
            .eq("subject_id", subject_id) \
            .eq("year", year) \
            .eq("period", period) \
            .execute()

        logger.debug(f"Query test by subject, year, period response: {query_response}")

        if query_response.data:
            if len(query_response.data) > 1:
                logger.warning(f"Found multiple tests for subject_id '{subject_id}', year {year}, period {period}. Using the first one.")
            
            test_record = query_response.data[0]
            test_id = test_record["id"]
            existing_test_name = test_record["name"]
            logger.info(f"Found existing test '{existing_test_name}' (ID: {test_id}) for subject_id '{subject_id}', year {year}, period {period}.")
            
            # Optionally, update name if it differs and is provided
            # Также обновляем subject_code и question_count, если они предоставлены и отличаются
            updates_to_perform: Dict[str, Any] = {}
            if existing_test_name != test_name:
                updates_to_perform["name"] = test_name
                logger.warning(f"Test name mismatch for ID {test_id}: provided '{test_name}', existing '{existing_test_name}'. Queuing name update.")
            
            existing_subject_code = test_record.get("subject_code")
            if subject_code is not None and existing_subject_code != subject_code:
                updates_to_perform["subject_code"] = subject_code
                logger.info(f"Updating subject_code for test ID {test_id} from '{existing_subject_code}' to '{subject_code}'.")

            existing_question_count = test_record.get("question_count")
            if question_count is not None and existing_question_count != question_count:
                updates_to_perform["question_count"] = question_count
                logger.info(f"Updating question_count for test ID {test_id} from '{existing_question_count}' to '{question_count}'.")

            # Optionally, update duration or description if they differ and are provided
            existing_duration = test_record.get("duration_in_seconds")
            if duration_in_seconds is not None and existing_duration != duration_in_seconds:
                 updates_to_perform["duration_in_seconds"] = duration_in_seconds
                 logger.info(f"Updating duration for test ID {test_id} from '{existing_duration}' to '{duration_in_seconds}'.")
            
            existing_description = test_record.get("description")
            if description is not None and existing_description != description:
                updates_to_perform["description"] = description
                logger.info(f"Updating description for test ID {test_id}.") # No need to log full old/new desc

            if updates_to_perform:
                update_response = client.table("tests").update(updates_to_perform).eq("id", test_id).execute()
                if update_response.data:
                    logger.info(f"Successfully applied updates to test ID {test_id}: {list(updates_to_perform.keys())}")
                else:
                    logger.error(f"Failed to apply updates to test ID {test_id}. Error: {update_response.error}")
            else:
                logger.info(f"No updates needed for existing test ID {test_id}.")

            return test_id
        else:
            # 2. If not found, create new test
            logger.info(f"Test '{test_name}' for subject_id '{subject_id}', year {year}, period {period} not found, creating new one...")
            test_data = {
                "name": test_name,
                "year": year,
                "period": period,
                "subject_id": subject_id,
                "duration_in_seconds": duration_in_seconds
            }
            if description is not None:
                test_data["description"] = description
            if subject_code is not None:               # 新增
                test_data["subject_code"] = subject_code # 新增
            if question_count is not None:             # 新增
                test_data["question_count"] = question_count # 新增
            
            insert_response = client.table("tests").insert(test_data).execute()
            logger.debug(f"Insert test response: {insert_response}")

            if insert_response.data:
                test_id = insert_response.data[0]["id"]
                logger.info(f"Created new test '{test_name}' with id: {test_id}")
                return test_id
            else:
                logger.error(f"Failed to create test '{test_name}'. Response: {insert_response.error}")
                return None
    except Exception as e:
        logger.error(f"Error in get_or_create_test for '{test_name}': {e}", exc_info=True)
        return None

# --- 批量插入函數 ---

def batch_insert_questions(questions_data_list: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    批量插入題目數據到 questions 表。
    返回成功插入的數據列表和插入失敗的數據列表。
    """
    client = get_supabase_client()
    if not client:
        return [], questions_data_list # 如果客戶端未初始化，所有數據都算失敗

    if not questions_data_list:
        logger.info("No questions data provided for batch insert.")
        return [], []

    successful_inserts = []
    failed_inserts = []
    response = None # 初始化 response

    try:
        # Supabase Python client 的 insert 方法本身就可以接受一個列表來進行批量插入
        # 它會嘗試將所有記錄作為一個事務插入（取決於具體實現和後端行為）
        response = client.table("questions").insert(questions_data_list, upsert=False).execute() #明確禁止upsert，如果需要則調整
        
        logger.debug(f"Batch insert questions response: {response}")

        if response.data:
            successful_inserts = response.data
            logger.info(f"Successfully batch inserted {len(successful_inserts)} questions.")
            if len(successful_inserts) != len(questions_data_list):
                logger.warning(f"Mismatch in batch insert for questions: expected {len(questions_data_list)}, got {len(successful_inserts)}. Some might have failed silently or the API behavior needs checking.")
        
        # 檢查 response.error 是否存在，並且是否有消息 (PostgrestAPIError 有 message 屬性)
        if hasattr(response, 'error') and response.error is not None and hasattr(response.error, 'message'):
            logger.error(f"Failed to batch insert questions. Error: {response.error.message}")
            failed_inserts = questions_data_list # 如果有 error，假設全部失敗
            successful_inserts = [] # 清空成功列表
        elif hasattr(response, 'error') and response.error is not None: # 如果 error 不是 None 但沒有 message 屬性
            logger.error(f"Failed to batch insert questions. Error details: {response.error}")
            failed_inserts = questions_data_list
            successful_inserts = []
        
    except Exception as e:
        logger.error(f"Exception during batch_insert_questions: {e}", exc_info=True)
        failed_inserts = questions_data_list # 發生異常，假設全部失敗
        successful_inserts = []

    # 在 try-except 外部，再次檢查 response 是否為 None (例如，如果 client is None 導致 try 塊未執行)
    if response and not hasattr(response, 'error') or (hasattr(response, 'error') and response.error is None):
        if len(successful_inserts) != len(questions_data_list):
             logger.warning(f"Batch insert for questions: {len(successful_inserts)} succeeded out of {len(questions_data_list)}. No explicit error from API, but counts differ.")
    
    return successful_inserts, failed_inserts


def batch_insert_notes(notes_data_list: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    批量插入筆記數據到 notes 表。
    返回成功插入的數據列表和插入失敗的數據列表。
    """
    client = get_supabase_client()
    if not client:
        return [], notes_data_list

    if not notes_data_list:
        logger.info("No notes data provided for batch insert.")
        return [], []
        
    successful_inserts = []
    failed_inserts = []
    response = None # 初始化 response

    try:
        response = client.table("notes").insert(notes_data_list, upsert=False).execute()
        logger.debug(f"Batch insert notes response: {response}")

        if response.data:
            successful_inserts = response.data
            logger.info(f"Successfully batch inserted {len(successful_inserts)} notes.")
            if len(successful_inserts) != len(notes_data_list):
                 logger.warning(f"Mismatch in batch insert for notes: expected {len(notes_data_list)}, got {len(successful_inserts)}.")

        if hasattr(response, 'error') and response.error is not None and hasattr(response.error, 'message'):
            logger.error(f"Failed to batch insert notes. Error: {response.error.message}")
            failed_inserts = notes_data_list
            successful_inserts = []
        elif hasattr(response, 'error') and response.error is not None:
            logger.error(f"Failed to batch insert notes. Error details: {response.error}")
            failed_inserts = notes_data_list
            successful_inserts = []

    except Exception as e:
        logger.error(f"Exception during batch_insert_notes: {e}", exc_info=True)
        failed_inserts = notes_data_list
        successful_inserts = []
    
    if response and not hasattr(response, 'error') or (hasattr(response, 'error') and response.error is None):
        if len(successful_inserts) != len(notes_data_list):
            logger.warning(f"Batch insert for notes: {len(successful_inserts)} succeeded out of {len(notes_data_list)}. No explicit error from API, but counts differ.")

    return successful_inserts, failed_inserts


# --- 主測試區塊 ---
if __name__ == "__main__":
    logger.info("--- Testing db_handler.py ---")

    if not supabase:
        logger.error("Supabase client not initialized. Exiting test.")
        exit()

    # 1. 測試 get_or_create_subject
    logger.info("\n--- Testing get_or_create_subject ---")
    test_subject_name_1 = "測試科目一 (DB Handler)"
    subject_id_1 = get_or_create_subject(test_subject_name_1)
    if subject_id_1:
        logger.info(f"Got or created subject '{test_subject_name_1}' with ID: {subject_id_1}")
    else:
        logger.error(f"Failed to get or create subject '{test_subject_name_1}'")

    # 再次調用以測試 "get" 部分
    subject_id_1_again = get_or_create_subject(test_subject_name_1)
    if subject_id_1_again and subject_id_1_again == subject_id_1:
        logger.info(f"Successfully retrieved existing subject '{test_subject_name_1}' again with ID: {subject_id_1_again}")
    elif subject_id_1_again:
        logger.error(f"Retrieved subject '{test_subject_name_1}' but ID mismatch: {subject_id_1_again} vs {subject_id_1}")
    else:
        logger.error(f"Failed to retrieve existing subject '{test_subject_name_1}'")

    test_subject_name_2 = "測試科目二 (DB Handler)"
    subject_id_2 = get_or_create_subject(test_subject_name_2)
    if subject_id_2:
        logger.info(f"Got or created subject '{test_subject_name_2}' with ID: {subject_id_2}")
    else:
        logger.error(f"Failed to get or create subject '{test_subject_name_2}'")


    # 2. 測試 get_or_create_chapter (需要一個有效的 subject_id)
    logger.info("\n--- Testing get_or_create_chapter ---")
    chapter_id_1 = None # <--- 初始化 chapter_id_1
    if subject_id_1:
        test_chapter_title_1 = "第一章：基本概念 (DB Handler)"
        chapter_id_1 = get_or_create_chapter(test_chapter_title_1, subject_id_1, order=1)
        if chapter_id_1:
            logger.info(f"Got or created chapter '{test_chapter_title_1}' with ID: {chapter_id_1}")
        else:
            logger.error(f"Failed to get or create chapter '{test_chapter_title_1}'")

        # 再次調用
        chapter_id_1_again = get_or_create_chapter(test_chapter_title_1, subject_id_1, order=1) # order 不變
        if chapter_id_1_again and chapter_id_1_again == chapter_id_1:
            logger.info(f"Successfully retrieved existing chapter '{test_chapter_title_1}' again.")
        
        # 測試更新 order
        if chapter_id_1: # 確保 chapter_id_1 存在才更新
            chapter_id_1_update_order = get_or_create_chapter(test_chapter_title_1, subject_id_1, order=10)
            if chapter_id_1_update_order and chapter_id_1_update_order == chapter_id_1:
                logger.info(f"Successfully updated order for chapter '{test_chapter_title_1}'.")


    # 3. 測試 get_or_create_test (需要一個有效的 subject_id)
    logger.info("\n--- Testing get_or_create_test ---")
    test_id_1 = None # <--- 初始化 test_id_1
    if subject_id_2:
        test_test_name_1 = "112年第一次模擬考 (DB Handler)"
        test_year_1 = 112 # MODIFIED: Added year for test
        test_period_1 = 1   # MODIFIED: Added period for test
        
        # MODIFIED: Call get_or_create_test with year and period
        test_id_1 = get_or_create_test(test_name=test_test_name_1, 
                                       year=test_year_1, 
                                       period=test_period_1, 
                                       subject_id=subject_id_2,
                                       subject_code="SC001",      # Test subject_code
                                       question_count=80)       # Test question_count
        if test_id_1:
            logger.info(f"Got or created test '{test_test_name_1}' (Year: {test_year_1}, Period: {test_period_1}) with ID: {test_id_1}")
        else:
            logger.error(f"Failed to get or create test '{test_test_name_1}' (Year: {test_year_1}, Period: {test_period_1})")
        
        if test_id_1:
            # Test retrieving the same test
            test_id_1_again = get_or_create_test(test_name=test_test_name_1, 
                                                 year=test_year_1, 
                                                 period=test_period_1, 
                                                 subject_id=subject_id_2,
                                                 subject_code="SC001",
                                                 question_count=80)
            if test_id_1_again and test_id_1_again == test_id_1:
                logger.info(f"Successfully retrieved existing test '{test_test_name_1}' (Year: {test_year_1}, Period: {test_period_1}) again.")
            # Test updating the name of the same test
            updated_test_name_1 = "112年第一次模擬考 (更新名稱)"
            test_id_1_updated_name = get_or_create_test(test_name=updated_test_name_1, 
                                                        year=test_year_1, 
                                                        period=test_period_1, 
                                                        subject_id=subject_id_2,
                                                        subject_code="SC002",      # Test updated subject_code
                                                        question_count=85)       # Test updated question_count
            if test_id_1_updated_name and test_id_1_updated_name == test_id_1:
                logger.info(f"Successfully updated name for test ID '{test_id_1}' to '{updated_test_name_1}'.")


    # 4. 測試 batch_insert_questions (需要一個有效的 test_id)
    logger.info("\n--- Testing batch_insert_questions ---")
    if test_id_1:
        questions_to_insert = [
            {
                "content": "第一題：這是什麼？(DB Handler)",
                "options": {"A": "選項A", "B": "選項B"},
                "correct_answer_key": ["A"],
                "question_number": 1,
                "test_id": test_id_1,
            },
            {
                "content": "第二題：那是什麼？(DB Handler)",
                "options": {"A": "選項C", "B": "選項D", "C": "選項E"},
                "correct_answer_key": ["B", "C"],
                "question_number": 2,
                "test_id": test_id_1,
            }
        ]
        successful_q, failed_q = batch_insert_questions(questions_to_insert)
        if successful_q:
            logger.info(f"Successfully inserted {len(successful_q)} questions:")
            for q_data in successful_q:
                logger.info(f"  Inserted question ID: {q_data.get('id')}, Number: {q_data.get('question_number')}")
        if failed_q:
            logger.error(f"Failed to insert {len(failed_q)} questions.")
    else:
        logger.warning("Skipping batch_insert_questions test as test_id_1 is not available.")

    # 5. 測試 batch_insert_notes (需要有效的 subject_id 和 chapter_id)
    logger.info("\n--- Testing batch_insert_notes ---")
    if subject_id_1 and chapter_id_1: # 使用上面創建的 subject_id_1 和 chapter_id_1
        notes_to_insert = [
            {
                "title": "筆記標題1 (DB Handler)",
                "content": "# 這是一個Markdown筆記 (DB Handler)",
                "chapter_id": chapter_id_1,
                "subject_id": subject_id_1,
                "order": 1
            },
            {
                "title": "筆記標題2 (DB Handler)",
                "content": "## 第二個筆記 (DB Handler)\n* 點1\n* 點2",
                "chapter_id": chapter_id_1,
                "subject_id": subject_id_1,
                "order": 2
            }
        ]
        successful_n, failed_n = batch_insert_notes(notes_to_insert)
        if successful_n:
            logger.info(f"Successfully inserted {len(successful_n)} notes:")
            for n_data in successful_n:
                 logger.info(f"  Inserted note ID: {n_data.get('id')}, Title: {n_data.get('title')}")
        if failed_n:
            logger.error(f"Failed to insert {len(failed_n)} notes.")
    else:
        logger.warning("Skipping batch_insert_notes test as subject_id_1 or chapter_id_1 is not available.")

    logger.info("\n--- db_handler.py Test Complete ---")
    logger.info("Please check your Supabase dashboard to verify the created/inserted records.")
    logger.info("Remember to clean up test data from your Supabase tables if necessary.") 