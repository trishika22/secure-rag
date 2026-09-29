import os

import jwt
from jwt import PyJWKClient
from dotenv import load_dotenv
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials


load_dotenv()

TENANT_ID = os.getenv("AZURE_TENANT_ID")

API_AUDIENCE = "api://e9e64da9-5cdb-412e-9345-0b487fa416e9"

REQUIRED_SCOPE = os.getenv("AZURE_SCOPE")

ISSUER = f"https://sts.windows.net/{TENANT_ID}/"
JWKS_URL = f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys"


security = HTTPBearer()
jwks_client = PyJWKClient(JWKS_URL)


def get_current_user(request: Request):
    token = request.session.get("access_token")

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)

        print("EXPECTED ISSUER:", ISSUER)
        print("TOKEN ISSUER:", jwt.decode(token, options={"verify_signature": False}).get("iss"))


        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=API_AUDIENCE,
            issuer=ISSUER,
        )

    except Exception as e:
        print("JWT VALIDATION ERROR:", repr(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
        )


    scopes = payload.get("scp", "").split()

    if REQUIRED_SCOPE not in scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Required scope is missing",
        )

    return payload