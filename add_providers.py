"""Safely add Google + Jina embedding to mcp_server.py"""
import ast

path = 'src/nexus_memory/mcp_server.py'
with open(path) as f:
    content = f.read()
    original = content

# 1. Add GOOGLE_API_KEY after OPENAI_API_KEY line
content = content.replace(
    'OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")',
    'OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")\nGOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")'
)

# 2. Insert Google block after OpenAI block (before Ollama)
google_block = '''
        # 3. Google / Vertex AI (cloud)
        if GOOGLE_API_KEY and GOOGLE_API_KEY.startswith("AIza"):
            try:
                import google.generativeai as genai
                genai.configure(api_key=GOOGLE_API_KEY)
                self._client = genai
                self._name = "text-embedding-004"
                self._dim = 768
                logging.info(f"Embedding: Google/{self._name} (768d, cloud)")
                return
            except Exception:
                pass

'''

# Find "3. Ollama" and insert before it
ollama_marker = '        # 3. Ollama (local service)'
content = content.replace(ollama_marker, google_block + ollama_marker)

# 3. Renumber Ollama from 3 to 5 and sentence-transformers from 4 to 6
content = content.replace(
    '        # 3. Ollama (local service)',
    '        # 5. Ollama (local service)'
)
content = content.replace(
    '        # 4. sentence-transformers (local, zero-setup fallback)',
    '        # 6. sentence-transformers (local, zero-setup fallback)'
)

# 4. Insert Jina after Google (now block 4)
jina_block = '''
        # 4. Jina (cloud, best value)
        JINA_API_KEY = os.environ.get("JINA_API_KEY", "")
        if JINA_API_KEY:
            try:
                self._client = {"api_key": JINA_API_KEY, "base_url": "https://api.jina.ai/v1"}
                self._name = "jina-embeddings-v3"
                self._dim = 1024
                logging.info(f"Embedding: Jina/{self._name} (1024d, cloud)")
                return
            except Exception:
                pass

'''

jina_insert = '        # 5. Ollama (local service)'
content = content.replace(jina_insert, jina_block + jina_insert)

# 5. Add Jina + Google handlers in embed() before sentence-transformers
jina_handler = '''        elif "jina" in (self._name or ""):
            import requests as _req
            r = _req.post(
                f"{self._client['base_url']}/embeddings",
                json={"model": self._name, "input": [text]},
                headers={"Authorization": f"Bearer {self._client['api_key']}"},
                timeout=30,
            )
            return r.json()["data"][0]["embedding"]
'''

google_handler = '''        elif "google" in str(type(self._client)).lower() or "generativeai" in str(type(self._client)).lower():
            result = self._client.embed_content(model=self._name, content=text)
            return result["embedding"]
'''

# Insert Jina handler before Ollama handler
ollama_handler = '        elif isinstance(self._client, dict):  # Ollama'
content = content.replace(ollama_handler, jina_handler + ollama_handler)

# Insert Google handler after Ollama handler
google_insert_after = '''            )
            return r.json()["embedding"]
'''
content = content.replace(google_insert_after, google_insert_after + '\n' + google_handler)

# 6. Fix duplicate property (the file should only have one 'available')
# Actually let's just replace the whole property section at the bottom
old_props = '''    @property
    def name(self) -> str: return self._name

    @property
    def dim(self) -> int: return self._dim

    @property
    def available(self) -> bool: return self._name != "none"'''
new_props = '''    @property
    def name(self) -> str: return self._name
    @property
    def dim(self) -> int: return self._dim
    @property
    def available(self) -> bool: return self._name != "none"'''
content = content.replace(old_props, new_props)

# Try to parse
try:
    ast.parse(content)
    with open(path, 'w') as f:
        f.write(content)
    print('✅ Syntax OK — saved')
except SyntaxError as e:
    print(f'❌ {e}')
    lines = content.split('\n')
    print(f'Line {e.lineno}: {lines[e.lineno-1]}')
