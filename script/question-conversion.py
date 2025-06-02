import re #regular expression
import json #json
import os #for path manipulation

def process_file(input_file):
    # Extract the filename without extension and create output path
    filename = os.path.splitext(os.path.basename(input_file))[0]
    output_file = f"data/{filename}.json"

    with open(input_file, "r", encoding="utf-8") as file:
        raw_text = file.read()

    # split the raw text by question numbers
    question_blocks = re.split(r'(?m)^\s*\d+\.\s*', raw_text)[1:]
    parsed_questions = []

    # Loops over each question block, gives each one an idx starting from 1.
    for idx, block in enumerate(question_blocks, start=1):
        lines = block.strip().split("\n")

        # Separate question and options
        question_lines = []
        options = {}
        parsing_question = True

        for line in lines:
            stripped_line = line.strip()
            if not stripped_line: # Skip empty lines
                continue

            option_match = re.match(r"([A-D])\.(.+)", stripped_line)
            if option_match:
                parsing_question = False # We've hit the options
                options[option_match.group(1)] = option_match.group(2).strip()
            elif parsing_question:
                # Still in the question part
                question_lines.append(stripped_line)

        # Combine all question lines into a single string
        full_question = " ".join(question_lines).strip()

        # Append the question and options to the parsed_questions list
        parsed_questions.append({
            "question_number": idx,
            "question": full_question,
            "options": options
        })

    # Save to JSON
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(parsed_questions, f, ensure_ascii=False, indent=2)
    
    print(f"Processed {input_file} -> {output_file}")

# List of input files to process
input_files = [
    "public/C-111-1.md",
    # Add more files here as needed
]

# Process each file
for input_file in input_files:
    process_file(input_file)