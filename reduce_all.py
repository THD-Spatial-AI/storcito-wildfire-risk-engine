import os
import re

def reduce_docstrings(content):
    def replacer(match):
        quote = match.group(1)
        inner = match.group(2)
        inner = re.sub(r'\s+', ' ', inner).strip()
        return f"{quote}{inner}{quote}"
    content = re.sub(r'(""")([\s\S]*?)(""")', replacer, content)
    content = re.sub(r"(''')([\s\S]*?)(''')", replacer, content)
    return content

def reduce_hash_comments(content):
    lines = content.split('\n')
    new_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        match = re.match(r'^(\s*)#(.*)$', line)
        if match:
            indent = match.group(1)
            comment_text = [match.group(2).strip()]
            j = i + 1
            while j < len(lines):
                next_line = lines[j]
                if next_line.strip() == '':
                    # empty line breaks the comment block usually, but let's break it
                    break
                next_match = re.match(r'^\s*#(.*)$', next_line)
                if next_match:
                    comment_text.append(next_match.group(1).strip())
                    j += 1
                else:
                    break
            
            combined = ' '.join(comment_text)
            new_lines.append(f"{indent}# {combined}")
            i = j
        else:
            new_lines.append(line)
            i += 1
    return '\n'.join(new_lines)

for d in ['FR', 'app']:
    for root, dirs, files in os.walk(d):
        for f in files:
            if f.endswith('.py'):
                filepath = os.path.join(root, f)
                with open(filepath, 'r', encoding='utf-8') as file:
                    content = file.read()
                
                new_content = reduce_docstrings(content)
                new_content = reduce_hash_comments(new_content)
                
                if new_content != content:
                    with open(filepath, 'w', encoding='utf-8') as file:
                        file.write(new_content)
                    print(f"Updated {filepath}")
