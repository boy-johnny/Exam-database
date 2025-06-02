import json

# Step 1: Read and parse the answers
def parse_answers(ans_file):
    fullwidth_to_ascii = str.maketrans('ＡＢＣＤ', 'ABCD')
    answers = []
    with open(ans_file, 'r', encoding='utf-8') as f:
        for line in f:
            # Remove whitespace, split, and convert to ASCII
            parts = line.strip().split()
            answers.extend([a.translate(fullwidth_to_ascii) for a in parts])
    return answers

# Step 2: Load questions
with open('data/C-111-1.json', 'r', encoding='utf-8') as f:
    questions = json.load(f)

# Step 3: Merge
answers = parse_answers('public/C-111-1-Ans.md')
for i, q in enumerate(questions):
    q['answer'] = answers[i]  # i is 0-based

# Step 4: Save
with open('data/C-111-1_with_answers.json', 'w', encoding='utf-8') as f:
    json.dump(questions, f, ensure_ascii=False, indent=2)

print("Merged answers into data/C-111-1_with_answers.json")