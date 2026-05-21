"""Login/logout + admin auth dependency."""
from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from ... import auth as kauth, claims_db
from ..db_dep import get_db

router = APIRouter()


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=200)


def current_user(
    request_session: Optional[str] = Cookie(default=None, alias=kauth.SESSION_COOKIE),
) -> Optional[dict]:
    """Returns {u: username, r: role} or None. Use as FastAPI dependency."""
    return kauth.verify_session(request_session or "")


def require_admin(user: Optional[dict] = Depends(current_user)) -> dict:
    if not user:
        raise HTTPException(401, "auth required")
    if user.get("r") not in ("admin", "editor"):
        raise HTTPException(403, "insufficient role")
    return user


@router.post("/login")
def login(req: LoginRequest, response: Response,
          conn: sqlite3.Connection = Depends(get_db)) -> dict:
    row = claims_db.get_user(conn, req.username)
    if not row or not kauth.verify_password(req.password, row["password_hash"]):
        raise HTTPException(401, "invalid credentials")
    token = kauth.sign_session(row["username"], row["role"])
    response.set_cookie(
        kauth.SESSION_COOKIE, token,
        max_age=kauth.SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        # secure=True in production behind HTTPS; FastAPI doesn't auto-detect — set via env
        secure=bool(__import__("os").environ.get("KAHZAABU_SECURE_COOKIES")),
    )
    return {"username": row["username"], "role": row["role"]}


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(kauth.SESSION_COOKIE)
    return {"ok": True}


@router.get("/me")
def me(user: Optional[dict] = Depends(current_user)) -> dict:
    if not user:
        return {"authenticated": False}
    return {"authenticated": True, "username": user["u"], "role": user["r"]}
