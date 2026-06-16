#!/usr/bin/env python3
"""Serve marketing assets."""
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
import uvicorn, sys

app = FastAPI(title="Nexus Memory — Marketing")
mdir = Path(__file__).parent

@app.get("/", response_class=HTMLResponse)
async def index():
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Nexus Memory — Marketing</title>
<style>body{{background:#0a0a1a;color:#fff;font-family:Inter,sans-serif;display:flex;
flex-direction:column;align-items:center;justify-content:center;min-height:100vh;gap:20px;}}
h1{{font-size:2rem}} a{{color:#818cf8;text-decoration:none;font-size:1.2rem;padding:12px 32px;
border:1px solid rgba(129,140,248,0.3);border-radius:8px;transition:all 0.2s}}
a:hover{{background:rgba(129,140,248,0.1);border-color:rgba(129,140,248,0.6);}}</style>
</head><body><h1>Nexus Memory — Marketing Assets</h1>
<a href="/static-poster" target="_blank">📷 Static Poster (screenshot)</a>
<a href="/animated-poster" target="_blank">✨ Animated Poster (screen record)</a>
</body></html>"""

@app.get("/static-poster", response_class=HTMLResponse)
async def static_poster():
    return (mdir / "static-poster.html").read_text()

@app.get("/animated-poster", response_class=HTMLResponse)
async def animated_poster():
    return (mdir / "animated-poster.html").read_text()

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9130
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
