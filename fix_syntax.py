import re

with open('src/nexus_memory/mcp_server.py', 'r') as f:
    content = f.read()

# Fix all env var lines that got corrupted by masking
replacements = {
    'VOYAGE_API_KEY=*** "")': 'VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "")',
    'OPENAI_API_KEY=*** "")': 'OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")',
    'GOOGLE_API_KEY=*** "")': 'GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")',
    'JINA_API_KEY=*** "")': 'JINA_API_KEY = os.environ.get("JINA_API_KEY", "")',
}

for old, new in replacements.items():
    if old in content:
        content = content.replace(old, new)
        print(f'Fixed: {old.split("=")[0]}')
    else:
        print(f'Not found: {old[:30]}...')

# Fix duplicate @property available block
content = re.sub(
    r'    @property\n    def available.*?\n\n    @property\n    def available',
    '    @property\n    def available',
    content
)

with open('src/nexus_memory/mcp_server.py', 'w') as f:
    f.write(content)

# Verify syntax
import ast
ast.parse(content)
print('✅ Syntax OK')
