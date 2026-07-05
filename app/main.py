import os
import time
import uuid
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import firebase_admin
import strawberry
from fastapi import Depends, FastAPI, Header, HTTPException, status
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials as firebase_credentials
from fastapi.responses import JSONResponse
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr
from pymongo import MongoClient
from pymongo.collection import Collection
from strawberry.fastapi import GraphQLRouter


APP_NAME = os.getenv("APP_NAME", "global-identity")
JWT_ISSUER = os.getenv("JWT_ISSUER", "https://identity.lan")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "global-apps")
JWT_SECRET = os.getenv("JWT_SECRET", "replace-me")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TTL_MINUTES = int(os.getenv("ACCESS_TTL_MINUTES", "15"))
REFRESH_TTL_DAYS = int(os.getenv("REFRESH_TTL_DAYS", "14"))
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "identity")
ENABLE_FIREBASE_AUTH = os.getenv("ENABLE_FIREBASE_AUTH", "false").lower() == "true"
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")
FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")
ALLOW_LOCAL_PASSWORD_AUTH = os.getenv("ALLOW_LOCAL_PASSWORD_AUTH", "false").lower() == "true"

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
users: Collection = db["users"]
refresh_tokens: Collection = db["refresh_tokens"]


def ensure_indexes() -> None:
    users.create_index("email", unique=True)
    users.create_index("subject", unique=True)
    users.create_index(
        [("auth_provider", 1), ("firebase_uid", 1)],
        unique=True,
        sparse=True,
    )
    refresh_tokens.create_index("token_id", unique=True)
    refresh_tokens.create_index("expires_at")


def init_firebase() -> None:
    if not ENABLE_FIREBASE_AUTH:
        return

    options = {"projectId": FIREBASE_PROJECT_ID} if FIREBASE_PROJECT_ID else None
    try:
        if FIREBASE_SERVICE_ACCOUNT_JSON:
            service_account = json.loads(FIREBASE_SERVICE_ACCOUNT_JSON)
            cred = firebase_credentials.Certificate(service_account)
            firebase_admin.initialize_app(cred, options=options)
        else:
            firebase_admin.initialize_app(options=options)
    except Exception as exc:
        raise RuntimeError("Failed to initialize Firebase Admin SDK") from exc

    logger.info("Firebase authentication integration enabled")


def verify_firebase_token(id_token: str) -> dict[str, Any]:
    if not ENABLE_FIREBASE_AUTH:
        raise HTTPException(
            status_code=503,
            detail="Firebase integration is disabled",
        )

    try:
        return firebase_auth.verify_id_token(id_token, check_revoked=True)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid Firebase token") from exc


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def _encode_token(subject: str, token_type: str, ttl: timedelta, token_id: str) -> str:
    now = utc_now()
    payload = {
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "sub": subject,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": token_id,
        "typ": token_type,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def issue_tokens(subject: str) -> dict[str, Any]:
    access_jti = str(uuid.uuid4())
    refresh_jti = str(uuid.uuid4())

    access = _encode_token(
        subject=subject,
        token_type="access",
        ttl=timedelta(minutes=ACCESS_TTL_MINUTES),
        token_id=access_jti,
    )
    refresh = _encode_token(
        subject=subject,
        token_type="refresh",
        ttl=timedelta(days=REFRESH_TTL_DAYS),
        token_id=refresh_jti,
    )

    refresh_tokens.insert_one(
        {
            "token_id": refresh_jti,
            "subject": subject,
            "issued_at": utc_now(),
            "expires_at": utc_now() + timedelta(days=REFRESH_TTL_DAYS),
            "revoked": False,
        }
    )

    return {
        "token_type": "Bearer",
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": ACCESS_TTL_MINUTES * 60,
    }


def decode_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
        )
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc


def get_bearer_token(authorization: Optional[str] = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return authorization.replace("Bearer ", "", 1)


def get_current_subject(token: str = Depends(get_bearer_token)) -> str:
    claims = decode_token(token)
    if claims.get("typ") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")
    return claims["sub"]


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    display_name: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class FirebaseExchangeRequest(BaseModel):
    id_token: str


class ProfileUpdateRequest(BaseModel):
    display_name: Optional[str] = None


def require_local_password_auth_enabled() -> None:
    if not ALLOW_LOCAL_PASSWORD_AUTH:
        raise HTTPException(
            status_code=403,
            detail="Local password auth is disabled; use Firebase exchange",
        )


@strawberry.type
class UserProfile:
    subject: str
    email: str
    display_name: str
    created_at: str


@strawberry.type
class Query:
    @strawberry.field
    def me(self, info) -> Optional[UserProfile]:
        request = info.context["request"]
        auth = request.headers.get("authorization")
        if not auth or not auth.startswith("Bearer "):
            return None

        token = auth.replace("Bearer ", "", 1)
        claims = decode_token(token)
        user = users.find_one({"subject": claims["sub"]})
        if not user:
            return None

        return UserProfile(
            subject=user["subject"],
            email=user["email"],
            display_name=user["display_name"],
            created_at=user["created_at"].isoformat(),
        )


app = FastAPI(title=APP_NAME, version="0.1.0")
graphql_app = GraphQLRouter(strawberry.Schema(Query))
app.include_router(graphql_app, prefix="/graphql")


@app.on_event("startup")
def startup() -> None:
    ensure_indexes()
    init_firebase()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/.well-known/openid-configuration")
def openid_config() -> dict[str, Any]:
    return {
        "issuer": JWT_ISSUER,
        "jwks_uri": f"{JWT_ISSUER}/.well-known/jwks.json",
        "token_endpoint": f"{JWT_ISSUER}/auth/login",
        "userinfo_endpoint": f"{JWT_ISSUER}/graphql",
        "response_types_supported": ["token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": [JWT_ALGORITHM],
    }


@app.get("/.well-known/jwks.json")
def jwks() -> dict[str, Any]:
    # For HS256, downstream apps should validate through introspection endpoint.
    # Keeping this endpoint for API compatibility and future RS256 migration.
    return {"keys": []}


@app.post("/auth/register")
def register(payload: RegisterRequest) -> JSONResponse:
    require_local_password_auth_enabled()

    existing = users.find_one({"email": payload.email.lower()})
    if existing:
        raise HTTPException(status_code=409, detail="Email already exists")

    subject = str(uuid.uuid4())
    users.insert_one(
        {
            "subject": subject,
            "email": payload.email.lower(),
            "display_name": payload.display_name,
            "password_hash": hash_password(payload.password),
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
    )

    return JSONResponse({"subject": subject}, status_code=status.HTTP_201_CREATED)


@app.post("/auth/login")
def login(payload: LoginRequest) -> dict[str, Any]:
    require_local_password_auth_enabled()

    user = users.find_one({"email": payload.email.lower()})
    password_hash = user.get("password_hash") if user else None
    if not user or not password_hash or not verify_password(payload.password, password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return issue_tokens(user["subject"])


@app.post("/auth/refresh")
def refresh(payload: RefreshRequest) -> dict[str, Any]:
    claims = decode_token(payload.refresh_token)
    if claims.get("typ") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid token type")

    token_state = refresh_tokens.find_one({"token_id": claims["jti"]})
    if not token_state or token_state.get("revoked"):
        raise HTTPException(status_code=401, detail="Refresh token revoked")

    if token_state["expires_at"] < utc_now():
        raise HTTPException(status_code=401, detail="Refresh token expired")

    return issue_tokens(claims["sub"])


@app.post("/auth/firebase/exchange")
def firebase_exchange(payload: FirebaseExchangeRequest) -> dict[str, Any]:
    claims = verify_firebase_token(payload.id_token)
    firebase_uid = claims.get("uid")
    if not firebase_uid:
        raise HTTPException(status_code=401, detail="Firebase token missing uid")

    subject = f"firebase:{firebase_uid}"
    email = claims.get("email") or f"{firebase_uid}@firebase.local"
    display_name = claims.get("name") or email.split("@")[0]

    user = users.find_one({"subject": subject})
    if not user:
        users.insert_one(
            {
                "subject": subject,
                "email": email.lower(),
                "display_name": display_name,
                "auth_provider": "firebase",
                "firebase_uid": firebase_uid,
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
    else:
        updates: dict[str, Any] = {"updated_at": utc_now()}
        if user.get("email") != email.lower():
            updates["email"] = email.lower()
        if user.get("display_name") != display_name:
            updates["display_name"] = display_name
        if len(updates) > 1:
            users.update_one({"subject": subject}, {"$set": updates})

    return issue_tokens(subject)


@app.post("/auth/introspect")
def introspect(token: str = Depends(get_bearer_token)) -> dict[str, Any]:
    claims = decode_token(token)
    active = claims.get("exp", 0) > int(time.time())
    return {
        "active": active,
        "sub": claims.get("sub"),
        "iss": claims.get("iss"),
        "aud": claims.get("aud"),
        "exp": claims.get("exp"),
        "iat": claims.get("iat"),
        "typ": claims.get("typ"),
    }


@app.get("/auth/me")
def me(subject: str = Depends(get_current_subject)) -> dict[str, Any]:
    user = users.find_one({"subject": subject})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "subject": user["subject"],
        "email": user["email"],
        "display_name": user["display_name"],
        "created_at": user["created_at"],
    }


@app.patch("/auth/me")
def update_me(payload: ProfileUpdateRequest, subject: str = Depends(get_current_subject)) -> dict[str, Any]:
    updates: dict[str, Any] = {"updated_at": utc_now()}
    if payload.display_name is not None:
        updates["display_name"] = payload.display_name

    users.update_one({"subject": subject}, {"$set": updates})
    user = users.find_one({"subject": subject})

    return {
        "subject": user["subject"],
        "email": user["email"],
        "display_name": user["display_name"],
        "updated_at": user["updated_at"],
    }
