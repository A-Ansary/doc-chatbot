"""Document Q&A chatbot - FastAPI backend.

Upload a business's documents (PDF, TXT, MD), then customers ask questions in the chat widget and get
answers grounded in those documents, with the source file and page for each answer.

Run:  uvicorn app.main:app --reload     then open http://localhost:8000
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import anthropic
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from dotenv import load_dotenv

from .rag import Store

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")             # keys live in .env (never commit it)
DOCS = ROOT / "data" / "docs"
DOCS.mkdir(parents=True, exist_ok=True)

BUSINESS = os.getenv("BUSINESS_NAME", "our company")
MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
ALLOWED = {".pdf", ".txt", ".md"}

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")  # fast; full flash is often overloaded on the free tier
GEMINI_FALLBACKS = ["gemini-3.1-flash-lite", "gemini-3.5-flash"]   # tried in order when the main model is out of free quota
# "claude" (paid, best answers) or "gemini" (Google's free tier). Default: Claude if its key is set, else Gemini.
PROVIDER = os.getenv("AI_PROVIDER") or ("claude" if os.getenv("ANTHROPIC_API_KEY") else "gemini")

store = Store(ROOT / "data" / "index.json")
app = FastAPI(title="Document Q&A Chatbot")

SYSTEM = f"""You are the customer support assistant for {BUSINESS}.
Answer only from the document excerpts provided in each message. If they don't contain the answer,
say you don't know and suggest contacting the team - never invent prices, policies or facts.
Keep answers short and friendly (under 120 words), in plain text with no markdown (no asterisks or #).
Reply in the customer's language. Don't list sources - the app shows them under your answer. Always end by inviting the customer to visit the gym."""


class Turn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=4000)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[Turn] = Field(default_factory=list, max_length=20)


class Refused(Exception):
    """The model declined to answer."""


def ask_claude(messages: list[dict]) -> str:
    try:
        resp = anthropic.Anthropic().beta.messages.create(   # reads ANTHROPIC_API_KEY
            model=MODEL,
            max_tokens=2000,
            system=SYSTEM,
            messages=messages,
            output_config={"effort": "low"},            # short support answers: low effort is fast and cheap
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",                        # if a request is declined, the API retries on a fallback model
        )
    except (anthropic.AuthenticationError, TypeError) as exc:   # TypeError: no credentials configured at all
        raise HTTPException(500, "Server is missing a valid ANTHROPIC_API_KEY.") from exc
    except anthropic.RateLimitError as exc:
        raise HTTPException(429, "Too many requests right now - please try again in a moment.") from exc
    except anthropic.APIStatusError as exc:
        raise HTTPException(502, f"AI service error ({exc.status_code}).") from exc
    except anthropic.APIConnectionError as exc:
        raise HTTPException(503, "Could not reach the AI service.") from exc
    if resp.stop_reason == "refusal":
        raise Refused
    return "".join(b.text for b in resp.content if b.type == "text").strip()


_gemini = None


def ask_gemini(messages: list[dict]) -> str:
    from google import genai                    # imported here so Claude-only installs don't need it
    from google.genai import errors, types
    if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
        raise HTTPException(500, "Server is missing GEMINI_API_KEY (free from Google AI Studio).")
    contents = [types.Content(role="model" if m["role"] == "assistant" else "user",
                              parts=[types.Part.from_text(text=m["content"])]) for m in messages]
    global _gemini
    _gemini = _gemini or genai.Client(http_options=types.HttpOptions(      # keep one client alive (reads GEMINI_API_KEY)
        timeout=20_000, retry_options=types.HttpRetryOptions(attempts=1)))  # fail fast, then try the next model
    config = types.GenerateContentConfig(system_instruction=SYSTEM, max_output_tokens=2000)
    # Each free model has its own quota: when one is used up (429), fall through to the next.
    busy, resp = False, None
    for model in dict.fromkeys([GEMINI_MODEL, *GEMINI_FALLBACKS]):
        for attempt in range(2):                # one quick retry when Google is momentarily overloaded
            try:
                resp = _gemini.models.generate_content(model=model, contents=contents, config=config)
                break
            except errors.ClientError as exc:
                if exc.code in (404, 429):      # quota used up, or model not offered to this key
                    break
                if exc.code in (400, 401, 403):
                    raise HTTPException(500, f"Gemini rejected the request ({exc.code}) - check GEMINI_API_KEY.") from exc
                raise HTTPException(502, f"AI service error ({exc.code}).") from exc
            except (errors.ServerError, TimeoutError, OSError):
                busy = True
                time.sleep(1)
        if resp is not None:
            break
    else:
        if busy:
            raise HTTPException(503, "The free AI is busy right now - please try again in a minute.")
        raise HTTPException(429, "The free AI quota is used up for today - it resets tomorrow.")
    if not resp.text:
        raise Refused
    text = re.sub(r"\n*\s*Sources?:.*$", "", resp.text.strip(), flags=re.S | re.I)   # the UI shows sources
    return re.sub(r"\*\*|__|^#+\s*", "", text, flags=re.M).strip()


@app.get("/")
def home() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/documents")
def documents() -> dict:
    return {"documents": store.sources()}


@app.post("/api/documents")
async def upload(file: UploadFile) -> dict:
    name = Path(file.filename or "").name
    if Path(name).suffix.lower() not in ALLOWED:
        raise HTTPException(400, "Only PDF, TXT and MD files are supported.")
    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "File is larger than 25 MB.")
    path = DOCS / name
    path.write_bytes(data)
    try:
        chunks = store.add_file(path)
    except Exception as exc:  # unreadable/corrupt file
        path.unlink(missing_ok=True)
        raise HTTPException(400, f"Could not read that file: {exc}") from exc
    return {"file": name, "chunks": chunks}


@app.delete("/api/documents/{name}")
def delete(name: str) -> dict:
    name = Path(name).name
    store.remove(name)
    (DOCS / name).unlink(missing_ok=True)
    return {"deleted": name}


@app.post("/api/chat")
def chat(body: ChatIn) -> dict:
    hits = store.search(body.message)
    fallback = not hits and 0 < len(store.chunks) <= 40
    if fallback:                       # no keyword match (e.g. an Arabic question about English documents):
        hits = store.chunks            # small knowledge base, so let the AI read all of it
    if not hits:
        return {"answer": "I couldn't find anything about that in our documents. "
                          "Please contact our team and they'll be happy to help.", "sources": []}
    context = "\n\n".join(f"<excerpt file=\"{c.source}\" page=\"{c.page}\">\n{c.text}\n</excerpt>" for c in hits)
    messages = [t.model_dump() for t in body.history]
    messages.append({"role": "user", "content": f"<documents>\n{context}\n</documents>\n\nCustomer question: {body.message}"})
    try:
        answer = ask_gemini(messages) if PROVIDER == "gemini" else ask_claude(messages)
    except Refused:
        return {"answer": "Sorry, I can't help with that one. Please contact our team.", "sources": []}
    sources = [] if fallback else sorted({(c.source, c.page) for c in hits})
    return {"answer": answer, "sources": [{"file": f, "page": p} for f, p in sources]}
