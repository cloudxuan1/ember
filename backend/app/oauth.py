"""极简 OAuth 2.1 门禁（单用户）。

claude.ai 网页版 connector 的 connect 流程会探测 OAuth 发现端点
（/.well-known/oauth-*），探测不到就报连接失败——所以哪怕只有一个用户，
门禁也得按标准搭。流程：
  1. connector 收到 MCP 端点的 401 → 读 /.well-known 元数据
  2. POST /oauth/register 动态注册（DCR，无需手填 Advanced settings）
  3. 浏览器打开 /oauth/authorize → 轩输入 EMBER_OAUTH_PASSWORD → 发授权码
  4. POST /oauth/token 换 token → 之后每个 MCP 请求带 Bearer

安全模型三层：MCP_PATH 随机路径（找得到门）→ 授权页密码（轩本人才能开门）
→ Bearer token（进门后逐个请求验证）。token 是 .env 里的静态值（学旧系统
memory 的做法）：重启不丢、备份即 cp；DCR 注册表在内存里，重启丢了也无妨——
已发的 token 照常有效，客户端重连时会重新注册。

四个环境变量（生成：openssl rand -hex 32）：
  EMBER_OAUTH_PASSWORD       授权页密码
  EMBER_OAUTH_ACCESS_TOKEN   Bearer token
  EMBER_OAUTH_REFRESH_TOKEN  刷新 token
  PUBLIC_HOST                公网域名（默认 ember.cloudxuan1.com）
全部未设置时门禁关闭（本地开发直连），设置了 PASSWORD+ACCESS_TOKEN 即开启。
"""

import hashlib
import hmac
import html
import os
import secrets
import time
from base64 import urlsafe_b64encode
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

router = APIRouter()

_clients: dict[str, dict] = {}  # DCR 注册表（内存态，见模块注释）
_codes: dict[str, dict] = {}  # 授权码 → 待换 token 的上下文
CODE_TTL_SECONDS = 600
ACCESS_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60  # 告知客户端的有效期；实际 token 为静态值

# 手动配置的客户端（connector Advanced settings 填 ID/Secret 时）只允许回跳到这些前缀
MANUAL_CLIENT_REDIRECT_PREFIXES = (
    "https://claude.ai/",
    "https://claude.com/",
    "https://chatgpt.com/",
)


def _base_url() -> str:
    return f"https://{os.environ.get('PUBLIC_HOST', 'ember.cloudxuan1.com')}"


def _password() -> str:
    return os.environ.get("EMBER_OAUTH_PASSWORD", "")


def _access_token() -> str:
    return os.environ.get("EMBER_OAUTH_ACCESS_TOKEN", "")


def _refresh_token() -> str:
    return os.environ.get("EMBER_OAUTH_REFRESH_TOKEN", "")


def oauth_enabled() -> bool:
    return bool(_password() and _access_token())


def valid_bearer(authorization: str | None) -> bool:
    if not authorization or not authorization.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(authorization[7:].strip().encode(), _access_token().encode())


def _gone_when_disabled() -> JSONResponse | None:
    """门禁关闭时整套 OAuth 端点 404——不挂招牌。

    claude.ai 只要发现 /.well-known/oauth-* 返回 200 就会坚持走 OAuth 路径，
    撞上 anthropics/claude-ai-mcp#519（token 后客户端中止）。旧系统 memory
    未配置 OAuth 时这些路径 404，claude.ai 才肯走无鉴权直连。
    """
    if not oauth_enabled():
        return JSONResponse({"error": "not_found"}, status_code=404)
    return None


def unauthorized_response_headers() -> dict[str, str]:
    return {
        "WWW-Authenticate": (
            f'Bearer resource_metadata="{_base_url()}/.well-known/oauth-protected-resource"'
        )
    }


def _redirect_uri_allowed(client_id: str, redirect_uri: str) -> bool:
    client = _clients.get(client_id)
    if client is not None:
        return redirect_uri in client["redirect_uris"]
    # Advanced settings 手动填的 client_id 不经过 DCR，限死回跳前缀
    if client_id and client_id == os.environ.get("EMBER_OAUTH_CLIENT_ID", ""):
        return redirect_uri.startswith(MANUAL_CLIENT_REDIRECT_PREFIXES)
    return False


# ---------- 发现端点 ----------
# claude.ai 会带路径后缀探测（RFC 9728 的 path 形式），所以吃掉任意后缀。


def _authorization_server_metadata() -> dict:
    base = _base_url()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": [
            "none",
            "client_secret_post",
            "client_secret_basic",
        ],
    }


@router.get("/.well-known/oauth-authorization-server{_suffix:path}")
def oauth_authorization_server(_suffix: str = ""):
    return _gone_when_disabled() or _authorization_server_metadata()


@router.get("/.well-known/openid-configuration{_suffix:path}")
def openid_configuration(_suffix: str = ""):
    return _gone_when_disabled() or _authorization_server_metadata()


@router.get("/.well-known/oauth-protected-resource{_suffix:path}")
def oauth_protected_resource(_suffix: str = ""):
    if (gone := _gone_when_disabled()) is not None:
        return gone
    base = _base_url()
    mcp_path = os.environ.get("MCP_PATH", "mcp").strip("/")
    return {
        "resource": f"{base}/{mcp_path}",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
    }


# ---------- 动态注册（RFC 7591） ----------


@router.post("/oauth/register", status_code=201)
async def register(request: Request):
    if (gone := _gone_when_disabled()) is not None:
        return gone
    body = await request.json()
    redirect_uris = body.get("redirect_uris") or []
    if not isinstance(redirect_uris, list) or not all(isinstance(u, str) for u in redirect_uris):
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    client_id = secrets.token_urlsafe(16)
    client_secret = secrets.token_urlsafe(32)
    _clients[client_id] = {"redirect_uris": redirect_uris, "client_secret": client_secret}
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "client_id_issued_at": int(time.time()),
        "client_secret_expires_at": 0,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": body.get("token_endpoint_auth_method", "client_secret_post"),
        "client_name": body.get("client_name", "client"),
    }


# ---------- 授权页 ----------


def _authorize_page(params: dict[str, str], error: str = "") -> HTMLResponse:
    hidden = "\n".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in params.items()
        if v
    )
    error_html = f'<p class="err">{html.escape(error)}</p>' if error else ""
    page = f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ember 授权</title>
<style>
  body {{ font-family: system-ui, sans-serif; display: grid; place-items: center; min-height: 100vh; margin: 0; background: #1a1614; color: #eee; }}
  form {{ background: #262019; padding: 2rem; border-radius: 12px; width: min(320px, 85vw); }}
  h1 {{ font-size: 1.2rem; margin: 0 0 1rem; }} h1::before {{ content: "🔥 "; }}
  input[type=password] {{ width: 100%; box-sizing: border-box; padding: .6rem; border-radius: 8px; border: 1px solid #555; background: #1a1614; color: #eee; }}
  button {{ margin-top: 1rem; width: 100%; padding: .6rem; border: 0; border-radius: 8px; background: #d97742; color: #fff; font-size: 1rem; }}
  .err {{ color: #ff8a80; }}
</style></head><body>
<form method="post" action="/oauth/authorize">
  <h1>ember 记忆库</h1>
  <p>确认是轩本人在连接：</p>
  {error_html}
  <input type="password" name="password" placeholder="口令" autofocus>
  {hidden}
  <button type="submit">授权连接</button>
</form></body></html>"""
    status = 403 if error else 200
    return HTMLResponse(page, status_code=status)


def _authorize_params(source: dict) -> dict[str, str]:
    return {
        k: str(source.get(k) or "")
        for k in ("client_id", "redirect_uri", "response_type", "state",
                  "code_challenge", "code_challenge_method", "scope", "resource")
    }


def _validate_authorize(params: dict[str, str]) -> str | None:
    if params["response_type"] != "code":
        return "response_type 只支持 code"
    if not _redirect_uri_allowed(params["client_id"], params["redirect_uri"]):
        return "client_id 未注册或 redirect_uri 不在白名单"
    if not params["code_challenge"] or params["code_challenge_method"] != "S256":
        return "必须使用 PKCE (S256)"
    return None


@router.get("/oauth/authorize")
def authorize_form(request: Request):
    if (gone := _gone_when_disabled()) is not None:
        return gone
    params = _authorize_params(dict(request.query_params))
    problem = _validate_authorize(params)
    if problem:
        return JSONResponse({"error": "invalid_request", "error_description": problem}, status_code=400)
    return _authorize_page(params)


@router.post("/oauth/authorize")
async def authorize_submit(request: Request):
    if (gone := _gone_when_disabled()) is not None:
        return gone
    form = dict((await request.form()).items())
    params = _authorize_params(form)
    problem = _validate_authorize(params)
    if problem:
        return JSONResponse({"error": "invalid_request", "error_description": problem}, status_code=400)
    password = str(form.get("password") or "")
    if not (oauth_enabled() and hmac.compare_digest(password.encode(), _password().encode())):
        return _authorize_page(params, error="口令不对，再试试")

    code = secrets.token_urlsafe(32)
    _codes[code] = {
        "client_id": params["client_id"],
        "redirect_uri": params["redirect_uri"],
        "code_challenge": params["code_challenge"],
        "expires_at": time.time() + CODE_TTL_SECONDS,
    }
    query = {"code": code}
    if params["state"]:
        query["state"] = params["state"]
    return RedirectResponse(f"{params['redirect_uri']}?{urlencode(query)}", status_code=302)


# ---------- 换 token ----------


def _token_payload() -> dict:
    return {
        "access_token": _access_token(),
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_TTL_SECONDS,
        "refresh_token": _refresh_token() or _access_token(),
    }


def _token_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status)


@router.post("/oauth/token")
async def token(request: Request):
    if (gone := _gone_when_disabled()) is not None:
        return gone
    form = dict((await request.form()).items())
    grant_type = form.get("grant_type")

    if grant_type == "authorization_code":
        code = str(form.get("code") or "")
        ctx = _codes.pop(code, None)
        if ctx is None or ctx["expires_at"] < time.time():
            return _token_error("invalid_grant", "授权码无效或已过期")
        if form.get("redirect_uri") and form.get("redirect_uri") != ctx["redirect_uri"]:
            return _token_error("invalid_grant", "redirect_uri 与授权时不一致")
        verifier = str(form.get("code_verifier") or "")
        digest = urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if not hmac.compare_digest(digest.encode(), ctx["code_challenge"].encode()):
            return _token_error("invalid_grant", "PKCE 校验失败")
        return _token_payload()

    if grant_type == "refresh_token":
        supplied = str(form.get("refresh_token") or "")
        expected = _refresh_token() or _access_token()
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            return _token_error("invalid_grant", "refresh_token 无效")
        return _token_payload()

    return _token_error("unsupported_grant_type", f"不支持的 grant_type: {grant_type}")
