# Medical Exam Question Database System

![banner](./public/banner.png)

A comprehensive system for processing, storing, and managing medical examination questions from PDF files. This system extracts questions, answers, and images from exam papers and stores them in a structured database for easy access and management.

## Features

- **PDF Processing**

  - Extracts questions, answers, and metadata from exam papers
  - Handles both question papers and answer papers
  - Extracts and saves images from questions
  - Supports various PDF formats and layouts
  - Handles special cases like answer corrections and notes

- **Database Management**

  - Stores questions, answers, and metadata in Supabase
  - Organizes content by subjects, chapters, and tests
  - Supports batch operations for efficient data insertion
  - Maintains relationships between questions, subjects, and tests

- **Data Organization**
  - Structured storage of exam questions and answers
  - Metadata extraction (subject, year, period, etc.)
  - Image management with proper file organization
  - Support for notes and corrections

## Project Structure

```
.
├── src/                    # Source code
│   ├── main_questions.py   # Main processing script
│   ├── pdf_parser.py       # PDF parsing and processing
│   ├── db_handler.py       # Database operations
│   └── config.py          # Configuration and constants
├── raw_data/              # Input PDF files
│   ├── exams/            # Exam papers
│   └── notes/            # Additional notes
├── processed_data/        # Processed output
│   ├── parsed/           # Parsed JSON files
│   └── images/           # Extracted images
├── archive/        # Archived ouput (stored in supabase)
└── logs/                 # Log files
└── public/               # Static Data
```

## Prerequisites

- Python 3.8 or higher
- Supabase account and project
- Required Python packages (see Installation)

## Installation

1. Clone the repository:

   ```bash
   git clone [repository-url]
   cd [repository-name]
   ```

2. Install required packages:

   ```bash
   pip install -r requirements.txt
   ```

3. Create a `.env` file in the project root with the following variables:
   ```
   SUPABASE_URL=your_supabase_url
   SUPABASE_KEY=your_supabase_key
   AI_API_KEY=your_ai_api_key
   AI_API_ENDPOINT=https://openrouter.ai/api/v1/chat/completions
   AI_MODEL_NAME=deepseek/deepseek-r1:free
   LOG_LEVEL=INFO
   ```

## Usage

1. Place your exam PDF files in the appropriate structure:

   ```
   raw_data/exams/[Subject Name]/[Year]_[Period]/[Question Paper].pdf
   raw_data/exams/[Subject Name]/[Year]_[Period]/[Answer Paper].pdf
   ```

2. Run the main processing script:

   ```bash
   python src/main_questions.py
   ```

3. The script will:
   - Process all PDF files in the raw_data directory
   - Extract questions, answers, and images
   - Store data in Supabase
   - Save processed files in the processed_data directory

## Configuration

The system can be configured through:

- `config.py`: Main configuration file
- Environment variables in `.env`
- Command-line arguments (when supported)

Key configuration options:

- Logging level
- File paths
- Database connection
- AI API settings

## Database Schema

The system uses the following main tables in Supabase:

- `subjects`: Stores subject information
- `tests`: Stores exam information
- `questions`: Stores question content and metadata
- `chapters`: Organizes content by chapters
- `notes`: Stores additional notes and explanations

## Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

## License

[Your License Here]

## Support

For support, please [contact information or issue tracker details]
