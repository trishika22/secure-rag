# SecureRAG

Access-controlled RAG built on FastAPI, Azure AI Search, and Microsoft Entra ID. Authorization happens at retrieval, not in the LLM.

## Why this exists

A typical RAG application retrieves documents by relevance alone. In an enterprise, that's a problem: different users are allowed to see different documents. An HR employee should be able to retrieve HR policies, but not finance documents.

The common shortcut is to retrieve everything and instruct the LLM not to reveal what the user shouldn't see. That's unreliable, because the restricted content is already in the model's context and can leak through prompt injection or a careless answer.

**SecureRAG enforces document-level authorization at retrieval, before any content reaches the LLM.** Unauthorized chunks are never retrieved, so they can't be leaked.

## Key design decision: authorization is separate from relevance

- **Relevance** answers: *is this document useful for this question?*
- **Authorization** answers: *is this user allowed to see this document at all?*

A document can be highly relevant and still unauthorized. So authorization is a **hard filter**, not a ranking signal. Azure AI Search first restricts the index to the chunks the user is allowed to see, then runs vector and keyword retrieval within that set. Every result is both authorized and relevant.

## How it works

1. **Authentication.** The user signs in with Microsoft Entra ID. The backend's Entra app registration validates the access token and resolves who the user is and which groups they belong to.
2. **Authorization.** At ingestion, every document is tagged with the Entra group ID allowed to access it, and that tag is copied to every chunk from the document. At query time, the backend turns the user's groups into an Azure AI Search filter.
3. **Retrieval.** Hybrid (vector + keyword) search runs inside the authorized set and returns the most relevant chunks.
4. **Generation.** The authorized chunks and the user's question go to Azure OpenAI. The model is instructed to answer only from that context and to say so when the context isn't enough, instead of making something up.

## Example

| User | Group   | Can retrieve     | Cannot retrieve |
|------|---------|------------------|-----------------|
| Alice | HR      | HR policies      | Finance documents |
| Bob   | Finance | Finance documents | HR policies     |

Alice and Bob can ask the same question and get different answers, because each one's retrieval only ever sees their own documents.

## What I learned

Building secure AI systems means keeping identity, authorization, search, and LLM generation as separate layers. Security has to be enforced before generation, not requested from the model.
