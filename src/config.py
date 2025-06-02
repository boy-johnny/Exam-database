import os
from dotenv import load_dotenv
from pathlib import Path

# 專案根目錄 (假設 config.py 在 src/ 目錄下，則根目錄是其父目錄的父目錄)
# 如果您的結構不同，可能需要調整這裡
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 加載 .env 文件 (通常位於專案根目錄)
dotenv_path = PROJECT_ROOT / '.env'
load_dotenv(dotenv_path=dotenv_path)

# Supabase 配置
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

# AI API 配置
AI_API_KEY: str = os.getenv("AI_API_KEY", "")
AI_API_ENDPOINT: str = os.getenv("AI_API_ENDPOINT", "https://openrouter.ai/api/v1/chat/completions")
AI_MODEL_NAME: str = os.getenv("AI_MODEL_NAME", "deepseek/deepseek-r1:free") # 默認模型

# 日誌配置
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
LOGS_DIR = PROJECT_ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True) # 確保日誌目錄存在

# 原始數據目錄路徑常量 (相對於專案根目錄)
RAW_DATA_DIR = PROJECT_ROOT / "raw_data"
RAW_DATA_EXAMS_DIR = RAW_DATA_DIR / "exams"
RAW_DATA_NOTES_DIR = RAW_DATA_DIR / "notes"

# (可選) 中間處理結果存放目錄
PROCESSED_DATA_DIR = PROJECT_ROOT / "processed_data"
PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True) # 確保目錄存在


# PDF 解析相關的正則表達式模式 (示例，後續可以根據實際 PDF 調整)
# 這些只是初步的佔位符，您需要根據實際的 PDF 內容來調整它們
QUESTION_START_PATTERN = r"^(\d{1,3})\." # 匹配如 "1." "23." "100." 這樣的題號開頭
OPTION_PATTERN = r"^\(([A-D])\)" # 匹配如 "(A)" "(B)" 這樣的選項開頭


# 簡單的測試函數，用於驗證配置是否加載成功
def test_config_loading():
    print("PROJECT_ROOT:", PROJECT_ROOT)
    print("Logs directory:", LOGS_DIR)
    print("Raw data exams directory:", RAW_DATA_EXAMS_DIR)
    print("Raw data notes directory:", RAW_DATA_NOTES_DIR)
    print("Processed data directory:", PROCESSED_DATA_DIR)
    
    print("\\n--- Environment Variables ---")
    print(f"SUPABASE_URL: {'Loaded' if SUPABASE_URL else 'Not Loaded/Empty'}")
    print(f"SUPABASE_KEY: {'Loaded' if SUPABASE_KEY else 'Not Loaded/Empty'}")
    print(f"AI_API_KEY: {'Loaded' if AI_API_KEY else 'Not Loaded/Empty'}")
    print(f"AI_API_ENDPOINT: {AI_API_ENDPOINT}")
    print(f"AI_MODEL_NAME: {AI_MODEL_NAME}")
    print(f"LOG_LEVEL: {LOG_LEVEL}")

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("\\n🔴 WARNING: Supabase URL or Key is not loaded. Please check your .env file and its path.")
    if not AI_API_KEY:
        print("🟡 WARNING: AI API Key is not loaded. AI functionalities will not work.")

if __name__ == "__main__":
    # 測試配置加載
    print("--- Testing Configuration Loading ---")
    test_config_loading()
    
    # 檢查常量是否正確
    expected_exams_path = PROJECT_ROOT / "raw_data" / "exams"
    assert RAW_DATA_EXAMS_DIR == expected_exams_path, f"RAW_DATA_EXAMS_DIR is incorrect: {RAW_DATA_EXAMS_DIR}"
    
    print("\\n--- Configuration Test Complete ---")
    print("If you see 'Loaded' for your keys and paths look correct, config.py is likely working.")
    print("Ensure your .env file is in the project root directory and contains the correct keys.") 