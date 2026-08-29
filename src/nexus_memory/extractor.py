"""Session→Memory Pipeline — extract durable facts from conversations.

Two-tier extraction:
1. LLM extraction (preferred): uses the configured model to identify durable facts
2. Heuristic extraction (fallback): pattern-based extraction that always works

Extracted facts are categorized (fact/rule/preference/belief) with confidence
scores, ready for storage in Nexus Memory with auto-supersession dedup.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ─── LLM Extraction ──────────────────────────────────────────────────────────

_EXTRACTION_PROMPT = """Extract durable facts from this AI agent conversation. Return JSON only.

Rules:
- Extract 1-5 facts that would be useful in future sessions
- Categories: fact (hard knowledge), rule (behavioral rule), preference (user preference), belief (assumption)
- Confidence: 0.9+ for explicit statements, 0.7-0.8 for inferred, <0.5 for speculation
- Skip: greetings, task progress, temporary state, obvious context, tool output
- Format each fact as a declarative statement, not a command
- Language: same as the conversation (German stays German, English stays English)
- Only extract things worth remembering weeks from now

Return: {"facts": [{"text": "...", "category": "fact|rule|preference|belief", "confidence": 0.9}]}
If no durable facts: return {"facts": []}

Conversation:
"""

_MAX_CONVERSATION_CHARS = 8000


def _load_llm_config(hermes_home: str) -> Dict[str, str]:
    """Read model config from Hermes config.yaml and .env."""
    config: Dict[str, str] = {"model": "", "base_url": "", "api_key": ""}

    # Read config.yaml
    try:
        import yaml
        config_path = os.path.join(hermes_home, "config.yaml")
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}

        model = cfg.get("model", {})
        config["model"] = model.get("default", "")
        config["base_url"] = model.get("base_url", "")
        config["api_key"] = model.get("api_key", "")

        # Resolve custom provider
        provider = model.get("provider", "")
        if provider and provider.startswith("custom:"):
            provider_name = provider[7:]
            providers = cfg.get("providers", {})
            if provider_name in providers:
                p = providers[provider_name]
                if not config["base_url"]:
                    config["base_url"] = p.get("base_url", "")
                if not config["api_key"]:
                    config["api_key"] = p.get("api_key", "")
    except Exception:
        pass

    # Read .env for API keys
    env_path = os.path.join(hermes_home, ".env")
    if os.path.exists(env_path) and not config["api_key"]:
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        key, val = line.split("=", 1)
                        key = key.strip()
                        val = val.strip().strip('"').strip("'")
                        if key == "OLLAMA_API_KEY" and not config["api_key"]:
                            config["api_key"] = val
                        elif key == "OPENAI_API_KEY" and not config["api_key"]:
                            config["api_key"] = val
        except Exception:
            pass

    # Fallback: local Ollama
    if not config["base_url"]:
        config["base_url"] = "http://localhost:11434/v1"
    if not config["api_key"]:
        config["api_key"] = "ollama"
    # Use a cheap fast model for extraction if main model is not set
    if not config["model"]:
        config["model"] = "gemma3:4b"

    return config


def _quick_health_check(base_url: str, timeout: float = 1.0) -> bool:
    """TCP probe the LLM endpoint. Returns True if reachable within timeout."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        import socket
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except Exception:
        return False


def _llm_extract(
    messages: List[Dict[str, Any]],
    hermes_home: str,
) -> List[Dict[str, Any]]:
    """Use LLM to extract facts. Returns [] on failure or no facts found."""
    config = _load_llm_config(hermes_home)
    if not config["model"]:
        logger.debug("SessionExtractor: no model configured, skipping LLM")
        return []

    # Quick health check — 1s TCP probe. If unreachable, skip to heuristic
    # immediately instead of waiting 30s for the API timeout.
    if not _quick_health_check(config["base_url"]):
        logger.debug("SessionExtractor: LLM endpoint unreachable, using heuristic")
        return []

    # Build conversation text
    conv_parts = []
    total_chars = 0
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if not isinstance(content, str):
            continue
        if role in ("user", "assistant") and content.strip():
            # Skip tool outputs and very long content
            if len(content) > 2000:
                content = content[:2000] + "..."
            line = f"{role}: {content}"
            if total_chars + len(line) > _MAX_CONVERSATION_CHARS:
                break
            conv_parts.append(line)
            total_chars += len(line)

    if not conv_parts:
        return []

    conversation = "\n".join(conv_parts)
    prompt = _EXTRACTION_PROMPT + conversation

    try:
        from openai import OpenAI
        client = OpenAI(
            base_url=config["base_url"],
            api_key=config["api_key"],
            timeout=30,
        )
        response = client.chat.completions.create(
            model=config["model"],
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            # max_tokens includes the hidden reasoning field: glm-5.3-flash "thinks"
            # even with think:false (reasoning moved to separate field, but tokens
            # still count). 500 was exhausted by reasoning alone on long
            # conversations -> empty content -> JSON parse fail (197x on 28.08.).
            # 4000 verified 29.08: clean JSON, finish_reason=stop, ~5-13s.
            # Untouched budget costs nothing (usage-based, not max-based).
            max_tokens=4000,
            extra_body={"think": False},
            timeout=30,
        )
        if response.choices:
            text = response.choices[0].message.content or ""
        else:
            text = ""

        # Parse JSON from response (handle markdown code blocks, case-insensitive)
        text = text.strip()
        if "```" in text:
            match = re.search(r"```(?:json|JSON)?\s*(.*?)```", text, re.DOTALL)
            if match:
                text = match.group(1).strip()

        data = json.loads(text)
        facts = data.get("facts", [])

        # Validate and normalize
        result = []
        for f in facts:
            if not isinstance(f, dict):
                continue
            fact_text = f.get("text", "").strip()
            category = f.get("category", "fact").strip().lower()
            confidence = f.get("confidence", 0.7)
            if not fact_text:
                continue
            if category not in ("fact", "rule", "preference", "belief"):
                category = "fact"
            try:
                confidence = float(confidence)
                confidence = max(0.0, min(1.0, confidence))
            except (TypeError, ValueError):
                confidence = 0.7
            result.append({
                "text": fact_text[:1000],
                "category": category,
                "confidence": confidence,
            })

        logger.info(
            "SessionExtractor: LLM extracted %d facts from %d messages",
            len(result), len(conv_parts),
        )
        return result

    except Exception as exc:
        logger.warning("SessionExtractor: LLM extraction failed: %s", exc)
        return []


# ─── Heuristic Extraction (fallback, always works) ──────────────────────────

# German + English patterns
_RULE_PATTERNS = [
    r"\b(?:immer|nie|NIE|never|always)\s+",          # "immer" / "nie" / "never"
    r"\b(?:darf nicht|muss|must|verboten)\s+",        # "darf nicht" / "must"
    r"(?:KEINE?|nicht löschen|don't delete)\s+",     # "KEINE" / "don't delete"
    r"(?:regel|rule)\s*:",                          # "regel:" / "rule:"
]
_PREFERENCE_PATTERNS = [
    r"(?:ich will|ich möchte|bevorzug[ea]?|prefer)\s+",  # "ich will" / "bevorzuge" / "prefer"
    r"(?:mag kein[ae]?|hasse|don't like|hate)\s+",   # "mag keine" / "hate"
    r"(?:bitte|please)\s+(?:immer|nie|nicht)\s+",   # "bitte immer" / "please don't"
]
_FACT_PATTERNS = [
    r"\b(?:lauft|läuft|running on)\s+",               # "läuft" / "running on"
    r"\b(?:version|v\d+\.\d+)\s*",                     # "version 0.5.1"
    r"\b(?:IP|URL|port)\s*[=:]\s*\S+",                 # "IP: 192.168.1.1" / "URL = ..."
]

# Corrections: user corrects agent → rule
_CORRECTION_PATTERNS = [
    r"^(?:nein|nee?|nö|falsch|wrong|nope)\s*[,.!]",  # "nein" / "nee" / "nö" / "wrong"
    r"^(?:das stimmt nicht|that.?s wrong|incorrect)\s*[,.!]",
    r"(?:hab ich dir schon gesagt|told you before|schon gesagt)\s*",
]


def _heuristic_extract(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pattern-based extraction. Always works, no external dependencies."""
    facts: List[Dict[str, Any]] = []
    seen_texts: set = set()

    prev_was_assistant = False
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if not isinstance(content, str) or not content.strip():
            prev_was_assistant = (role == "assistant")
            continue

        # Check for corrections (user corrects assistant → rule)
        if role == "user" and prev_was_assistant:
            for pattern in _CORRECTION_PATTERNS:
                if re.search(pattern, content, re.IGNORECASE):
                    # Extract the corrected statement
                    fact_text = content[:300].strip()
                    key = fact_text.lower()[:80]
                    if key not in seen_texts:
                        facts.append({
                            "text": f"User correction: {fact_text}",
                            "category": "rule",
                            "confidence": 0.8,
                        })
                        seen_texts.add(key)
                    break

        # Check preference patterns FIRST (more specific than rules)
        matched_pref = False
        for pattern in _PREFERENCE_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                sentences = re.split(r"[.!?]\s+", content)
                for sent in sentences:
                    if re.search(pattern, sent, re.IGNORECASE) and len(sent) > 10:
                        key = sent.lower()[:80]
                        if key not in seen_texts:
                            facts.append({
                                "text": sent[:300].strip(),
                                "category": "preference",
                                "confidence": 0.7,
                            })
                            seen_texts.add(key)
                        break
                matched_pref = True
                break

        # Only check rules if preference didn't match (avoids "keine" matching KEINE? rule)
        if not matched_pref:
            for pattern in _RULE_PATTERNS:
                if re.search(pattern, content, re.IGNORECASE):
                    sentences = re.split(r"[.!?]\s+", content)
                    for sent in sentences:
                        if re.search(pattern, sent, re.IGNORECASE) and len(sent) > 10:
                            key = sent.lower()[:80]
                            if key not in seen_texts:
                                facts.append({
                                    "text": sent[:300].strip(),
                                    "category": "rule",
                                    "confidence": 0.75,
                                })
                                seen_texts.add(key)
                            break
                    break

        # Check for fact patterns (only from assistant, not user)
        if role == "assistant":
            for pattern in _FACT_PATTERNS:
                if re.search(pattern, content, re.IGNORECASE):
                    sentences = re.split(r"[.!?]\s+", content)
                    for sent in sentences:
                        if re.search(pattern, sent, re.IGNORECASE) and len(sent) > 15:
                            key = sent.lower()[:80]
                            if key not in seen_texts:
                                facts.append({
                                    "text": sent[:300].strip(),
                                    "category": "fact",
                                    "confidence": 0.6,
                                })
                                seen_texts.add(key)
                            break
                    break

        prev_was_assistant = (role == "assistant")

    # Cap at 5 facts
    result = facts[:5]
    if result:
        logger.info("SessionExtractor: heuristic extracted %d facts", len(result))
    return result


# ─── Public API ──────────────────────────────────────────────────────────────

def extract_facts(
    messages: List[Dict[str, Any]],
    hermes_home: str = "",
) -> List[Dict[str, Any]]:
    """Extract durable facts from a conversation.

    Tries LLM extraction first, falls back to heuristic.
    Returns list of {"text", "category", "confidence"} dicts.
    """
    # Filter to user+assistant messages only
    relevant = [
        m for m in messages
        if m.get("role") in ("user", "assistant")
        and isinstance(m.get("content"), str)
        and m["content"].strip()
    ]
    if not relevant:
        return []

    # Try LLM first (if hermes_home is available)
    if hermes_home:
        try:
            llm_facts = _llm_extract(relevant, hermes_home)
            if llm_facts:
                return llm_facts
        except Exception as exc:
            logger.warning("SessionExtractor: LLM path failed: %s", exc)

    # Fallback to heuristic
    return _heuristic_extract(relevant)