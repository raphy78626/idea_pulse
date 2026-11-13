"""Access code for a temporary shared link (a Cloudflare quick tunnel).

IdeaPulse has no login. A quick tunnel gives it a public https URL with no
password option, so this gate sits in front of every route:

  - Requests from this machine pass untouched.
  - Requests that arrive through Cloudflare (they carry Cf-Ray /
    Cf-Connecting-Ip headers) must enter the access code once. A cookie then
    lets that browser through until the code expires.
  - No share file, or an expired one, means the tunnel serves nothing.

Create a code:   python share_gate.py new 24      (hours; prints the code)
End it early:    python share_gate.py end
"""
import hashlib
import hmac
import json
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

BASE = Path(__file__).parent
SHARE_FILE = BASE / ".share_access"     # {"code": "...", "expires": epoch seconds}
COOKIE = "ideapulse_share"
LOGIN_PATH = "/share/login"


def is_external(request):
    """True for a request that came in through the Cloudflare tunnel."""
    h = request.headers
    return "cf-ray" in h or "cf-connecting-ip" in h


def _load():
    try:
        d = json.loads(SHARE_FILE.read_text())
        return str(d["code"]), float(d["expires"])
    except (OSError, ValueError, KeyError, TypeError):
        return None, 0.0


def _token(code, expires):
    return hashlib.sha256(f"{code}|{expires}".encode()).hexdigest()


def new_code(hours=24.0):
    code = "-".join(secrets.token_hex(2) for _ in range(3))
    expires = time.time() + hours * 3600
    SHARE_FILE.write_text(json.dumps({"code": code, "expires": expires}))
    SHARE_FILE.chmod(0o600)
    return code, expires


def end():
    try:
        SHARE_FILE.unlink()
    except FileNotFoundError:
        pass


def _page(title, body, status):
    import theme
    css = (".gate{max-width:30em;margin-top:60px}.gate h2{font:500 30px/1.2 var(--serif);margin-bottom:12px}"
           ".gate p{color:var(--muted);margin-bottom:16px}.gate form{display:flex;gap:10px;flex-wrap:wrap}"
           ".gate input{flex:1;min-width:12em}.gate .bad{color:var(--flag)}")
    html = theme.page(title, f'<p class="brand">IdeaPulse</p><div class="gate">{body}</div>', css)
    return HTMLResponse(html, status_code=status)


def _login_page(error=""):
    err = f'<p class="bad">{error}</p>' if error else ""
    return _page("Enter the access code", (
        "<h2>Enter the access code</h2>"
        "<p>This is a private review link for IdeaPulse. Use the code you were sent with it.</p>"
        f"{err}"
        f'<form method="post" action="{LOGIN_PATH}">'
        '<input name="code" autocomplete="off" autofocus required aria-label="Access code" placeholder="xxxx-xxxx-xxxx">'
        '<button class="btn" type="submit">Open IdeaPulse</button></form>'), 401)


def _closed_page():
    return _page("Link closed", (
        "<h2>This review link has closed</h2>"
        "<p>It was open for a limited time. Ask for a new link if you still need access.</p>"), 403)


def install(app):
    @app.middleware("http")
    async def share_gate(request: Request, call_next):
        if not is_external(request):
            return await call_next(request)
        code, expires = _load()
        if not code or time.time() >= expires:
            return _closed_page()
        if request.url.path == LOGIN_PATH and request.method == "POST":
            # parse the urlencoded body directly: the server's Python has no python-multipart
            fields = parse_qs((await request.body()).decode("utf-8", "replace"))
            given = (fields.get("code") or [""])[0]
            if hmac.compare_digest(given.strip().lower(), code):
                resp = RedirectResponse("/", status_code=303)
                resp.set_cookie(COOKIE, _token(code, expires), max_age=int(expires - time.time()),
                                httponly=True, secure=True, samesite="lax")
                return resp
            return _login_page("That code is not right. Check it and try again.")
        if hmac.compare_digest(request.cookies.get(COOKIE, ""), _token(code, expires)):
            return await call_next(request)
        return _login_page()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "new":
        hrs = float(sys.argv[2]) if len(sys.argv) > 2 else 24.0
        c, e = new_code(hrs)
        print("code %s, valid until %s" % (c, time.strftime("%Y-%m-%d %H:%M", time.localtime(e))))
    elif cmd == "end":
        end()
        print("shared link closed")
    else:
        print(__doc__)
