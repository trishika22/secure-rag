import os
import requests
import jwt
from fastapi.responses import RedirectResponse
from fastapi import FastAPI, Depends, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from urllib.parse import urlencode

from pydantic import BaseModel
from app.auth import get_current_user

from dotenv import load_dotenv

from azure.identity import ClientSecretCredential, get_bearer_token_provider
from openai import AzureOpenAI

from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse



load_dotenv()

SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
SEARCH_INDEX = os.getenv("AZURE_SEARCH_INDEX")
SEARCH_API_KEY = os.getenv("AZURE_SEARCH_API_KEY")

AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT")

credential = ClientSecretCredential(
    tenant_id=os.getenv("AZURE_TENANT_ID"),
    client_id=os.getenv("AZURE_BACKEND_CLIENT_ID"),
    client_secret=os.getenv("AZURE_BACKEND_CLIENT_SECRET"),
)

token_provider = get_bearer_token_provider(
    credential,
    "https://cognitiveservices.azure.com/.default"
)

openai_client = AzureOpenAI(
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    azure_ad_token_provider=token_provider,
    api_version="2025-04-01-preview",
)

app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET"),
    same_site="lax",
    https_only=False,
)


class AskRequest(BaseModel):
    question: str

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def home():
    return FileResponse("static/index.html")



@app.get("/health")
def health():
    return {
        "status": "ok"
    }


@app.get("/me")
def me(user=Depends(get_current_user)):
    return {
        "name": user.get("name"),
        "groups": user.get("groups", []),
        "scope": user.get("scp"),
    }

@app.get("/test-filter")
def test_filter(user=Depends(get_current_user)):
    groups = user.get("groups", [])

    search_filter = " or ".join(
        f"access eq '{group_id}'"
        for group_id in groups
    )

    return {
        "user": user.get("name"),
        "groups": groups,
        "search_filter": search_filter
    }

@app.get("/test-search")
def test_search(user=Depends(get_current_user)):
    groups = user.get("groups", [])

    search_filter = " or ".join(
        f"access eq '{group_id}'"
        for group_id in groups
    )

    search_url = (
        f"{SEARCH_ENDPOINT}/indexes/"
        f"{SEARCH_INDEX}/docs/search?api-version=2025-09-01"
    )

    search_body = {
        "search": "*",
        "filter": search_filter,
        "top": 5,
        "select": "chunk_id,parent_id,chunk,title,access"
    }

    search_headers = {
        "Content-Type": "application/json",
        "api-key": SEARCH_API_KEY
    }

    search_response = requests.post(
        search_url,
        headers=search_headers,
        json=search_body
    )

    search_response.raise_for_status()

    return {
        "user": user.get("name"),
        "groups": groups,
        "filter": search_filter,
        "results": search_response.json()["value"]
    }

@app.get("/test-rag-search")
def test_rag_search(
    question: str,
    user=Depends(get_current_user)
):
    groups = user.get("groups", [])

    search_filter = " or ".join(
        f"access eq '{group_id}'"
        for group_id in groups
    )

    search_url = (
        f"{SEARCH_ENDPOINT}/indexes/"
        f"{SEARCH_INDEX}/docs/search?api-version=2025-09-01"
    )

    search_body = {
        "vectorQueries": [
            {
                "kind": "text",
                "text": question,
                "fields": "text_vector",
                "k": 5
            }
        ],
        "filter": search_filter,
        "top": 5,
        "select": "chunk_id,parent_id,chunk,title,access"
    }


    search_headers = {
        "Content-Type": "application/json",
        "api-key": SEARCH_API_KEY
    }

    search_response = requests.post(
        search_url,
        headers=search_headers,
        json=search_body
    )

    search_response.raise_for_status()

    results = search_response.json()["value"]

    MIN_SCORE = 0.70

    authorized_relevant_results = [
        result
        for result in results
        if result.get("@search.score", 0) >= MIN_SCORE
    ]

    return {
        "question": question,
        "user": user.get("name"),
        "groups": groups,
        "filter": search_filter,
        "results": authorized_relevant_results
    }

   
@app.post("/ask")
def ask(
    request: AskRequest,
    user=Depends(get_current_user)
):
    # 1. Get user's groups
    groups = user.get("groups", [])

    if not groups:
        return {
            "question": request.question,
            "answer": "You don't have access to any documents.",
            "results": []
        }

    # 2. Build authorization filter
    search_filter = " or ".join(
        f"access eq '{group_id}'"
        for group_id in groups
    )

    # 3. Search Azure AI Search
    search_url = (
        f"{SEARCH_ENDPOINT}/indexes/"
        f"{SEARCH_INDEX}/docs/search?api-version=2025-09-01"
    )

    search_body = {
        "vectorQueries": [
            {
                "kind": "text",
                "text": request.question,
                "fields": "text_vector",
                "k": 5
            }
        ],
        "filter": search_filter,
        "top": 5,
        "select": "chunk_id,parent_id,chunk,title,access"
    }

    search_headers = {
        "Content-Type": "application/json",
        "api-key": SEARCH_API_KEY
    }

    search_response = requests.post(
        search_url,
        headers=search_headers,
        json=search_body
    )

    search_response.raise_for_status()

    results = search_response.json()["value"]

    # 4. Keep only sufficiently relevant results
    MIN_SCORE = 0.70

    authorized_relevant_results = [
        result
        for result in results
        if result.get("@search.score", 0) >= MIN_SCORE
    ]

    # 5. If nothing relevant was found, don't call the LLM
    if not authorized_relevant_results:
        return {
            "question": request.question,
            "answer": "I couldn't find relevant information in the documents you have access to.",
            "results": []
        }

    # 6. Build context for the LLM
    context = "\n\n".join(
        f"Document: {result.get('title', 'Unknown')}\n"
        f"{result.get('chunk', '')}"
        for result in authorized_relevant_results
    )

    # 7. Call Azure OpenAI
    prompt = f"""
Answer the user's question using ONLY the provided context.

If the answer cannot be found in the context, say:
"I don't have enough information in the documents I can access."

Context:
{context}

User question:
{request.question}
"""
    response = openai_client.chat.completions.create(
        model=AZURE_OPENAI_DEPLOYMENT,
        messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a secure enterprise RAG assistant. "
                        "Answer the user's question ONLY using the provided context. "
                        "Do not use outside knowledge or make up information. "
                        "If the context does not contain enough information to answer, "
                        "say: 'I couldn't find enough information in the documents you have access to.'"
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ]
        ,
        max_completion_tokens=1000
    )

    print("FINISH:", response.choices[0].finish_reason)
    print("CONTENT:", repr(response.choices[0].message.content))
    print("USAGE:", response.usage)


    answer = response.choices[0].message.content

    # 8. Return answer + sources
    return {
        "question": request.question,
        "answer": answer,
        "sources": [
            {
                "title": result.get("title"),
                "chunk_id": result.get("chunk_id"),
                "score": result.get("@search.score")
            }
            for result in authorized_relevant_results
        ]
    }


@app.get("/login")
def login():
    tenant_id = os.getenv("AZURE_TENANT_ID")
    client_id = os.getenv("AZURE_CLIENT_APP_ID")
    redirect_uri = os.getenv("AZURE_REDIRECT_URI")

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": (
        "openid profile email "
        "api://e9e64da9-5cdb-412e-9345-0b487fa416e9/access_as_user"),
        "prompt": "consent",
    }

    url = (
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize?"
        + urlencode(params)
    )

    return RedirectResponse(url)

@app.get("/auth/callback")
def auth_callback(request: Request, code: str):
    tenant_id = os.getenv("AZURE_TENANT_ID")
    client_id = os.getenv("AZURE_CLIENT_APP_ID")
    client_secret = os.getenv("AZURE_CLIENT_SECRET")
    redirect_uri = os.getenv("AZURE_REDIRECT_URI")

    token_url = (
        f"https://login.microsoftonline.com/"
        f"{tenant_id}/oauth2/v2.0/token"
    )

    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "scope": "openid profile email api://e9e64da9-5cdb-412e-9345-0b487fa416e9/access_as_user",
    }

    response = requests.post(token_url, data=data)

    if response.status_code != 200:
        return {
            "error": "Token exchange failed",
            "details": response.json(),
        }

    tokens = response.json()

    access_token = tokens.get("access_token")

    if not access_token:
        return {
            "error": "No access token received"
        }

    request.session["access_token"] = access_token

    return {
    "message": "Login successful",
    "session_has_token": "access_token" in request.session
    }

@app.get("/session")
def session_check(request: Request):
    return {
        "has_access_token": "access_token" in request.session,
        "session_secret_loaded": bool(os.getenv("SESSION_SECRET"))
    }