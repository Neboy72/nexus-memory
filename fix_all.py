import ast, re

with open('/Users/miosha/nexus-memory/src/nexus_memory/mcp_server.py', 'r') as f:
    content = f.read()

# Find and fix all corrupted env var assignments
# Pattern: VAR_NAME=*** "") -> VAR_NAME = os.environ.get("VAR_NAME", "")
def fix_env_var(match):
    var_name = match.group(1).strip()
    return f'{var_name} = os.environ.get("{var_name}", "")'

content = re.sub(
    r'(VOYAGE_API_KEY|OPENAI_API_KEY|GOOGLE_API_KEY|JINA_API_KEY)\s*=\s*\*{3}\s*""\)',
    fix_env_var,
    content
)

# Also fix partial corruption like: VAR_NAME=os.env...EY", "")
content = re.sub(
    r'(VOYAGE_API_KEY|OPENAI_API_KEY|GOOGLE_API_KEY)\s*=os\.env.*?"\)',
    fix_env_var,
    content
)

# Fix duplicate property
content = re.sub(
    r'    @property\n    def available.*?\n(    @property\n    def available)',
    r'    @property\n    def available',
    content
)

with open('/Users/miosha/nexus-memory/src/nexus_memory/mcp_server.py', 'w') as f:
    f.write(content)

# Verify
try:
    ast.parse(content)
    print('✅ Syntax OK')
except SyntaxError as e:
    print(f'❌ {e}')
    # Show offending line
    lines = content.split('\n')
    print(f'Line {e.lineno}: {lines[e.lineno-1]}')
