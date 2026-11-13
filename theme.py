"""One look for every IdeaPulse page.

Tokens, type and the shared pieces (header with page links, headline sentence,
tabs, plain text actions, forms) live here so the five pages cannot drift apart.

The one visual idea carried across pages: a highlighter. Yellow marks what is
worth acting on (a verified pain, a Build idea, a GO offer); a blue underline
marks the second tier (a rival, a Watch idea, a LATER offer). Everything else
stays quiet.
"""
import html

FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700'
    '&family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;1,6..72,400&display=swap" rel="stylesheet">'
)

PAGES = [  # (key, path, label)
    ("ideas", "/", "Ideas"),
    ("pains", "/pains", "Pains"),
    ("money", "/money", "First customer"),
    ("launch", "/launch", "Launch directories"),
    ("outreach", "/outreach", "Outreach"),
]

CSS = """
:root{
  --ground:#141c23; --surface:#1a242d; --rule:#25313b;
  --ink:#e8e2d4; --muted:#8b979f; --faint:#5d6b75;
  --mark:#f0c64a; --mark-bg:rgba(240,198,74,.22); --rival:#7fa8d6; --flag:#e48a78;
  --serif:'Newsreader',Georgia,'Times New Roman',serif;
  --sans:'Atkinson Hyperlegible',-apple-system,'Segoe UI',sans-serif;
}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--ground);color:var(--ink);font:16px/1.55 var(--sans);
  padding:40px 32px 90px;max-width:1160px;margin:0 auto}
a{color:inherit}
a:focus-visible,button:focus-visible,summary:focus-visible,input:focus-visible,select:focus-visible{
  outline:2px solid var(--mark);outline-offset:3px;border-radius:2px}
[hidden]{display:none!important}

.top{display:flex;justify-content:space-between;align-items:baseline;gap:12px 20px;flex-wrap:wrap}
.top h1{font:500 44px/1.05 var(--serif);letter-spacing:-.01em}
.links{display:flex;flex-wrap:wrap;gap:4px 18px}
.links a{color:var(--muted);text-decoration:none;font-size:14px;white-space:nowrap}
.links a:hover{color:var(--ink)}
.links a[aria-current]{color:var(--ink);font-weight:700}
.day{color:var(--muted);font-size:14px;margin-top:18px}
.lede{font:400 23px/1.4 var(--serif);max-width:34em;margin:4px 0 0;text-wrap:balance}
.lede.solo{margin-top:16px}
.how{color:var(--muted);font-size:14px;max-width:44em;margin-top:8px}

.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:22px 0 0}
.btn,.bar button{font:700 14px/1 var(--sans);border-radius:6px;padding:11px 18px;cursor:pointer;
  border:1px solid var(--ink);background:var(--ink);color:var(--ground);text-decoration:none;display:inline-block}
.btn.second,.bar button.second{background:transparent;color:var(--ink);border-color:var(--faint)}
.btn:hover,.bar button:hover{opacity:.88}
.say{color:var(--muted);font-size:14px;margin-left:6px}

.tabs{display:flex;gap:8px 22px;flex-wrap:wrap;margin:34px 0 0;border-bottom:1px solid var(--rule)}
.tab{color:var(--muted);text-decoration:none;font-size:15px;padding:0 0 10px;margin-bottom:-1px;
  border-bottom:2px solid transparent;white-space:nowrap}
.tab:hover{color:var(--ink)}
.tab[aria-current]{color:var(--ink);border-bottom-color:var(--ink);font-weight:700}
.filters{display:flex;flex-wrap:wrap;gap:6px 16px;align-items:baseline;margin:16px 0 0;font-size:14px;color:var(--muted)}
.filters .fl{color:var(--faint);margin-right:2px}
.filters a,.filters button{color:var(--muted);text-decoration:none;background:none;border:0;font:inherit;cursor:pointer;padding:0}
.filters a:hover,.filters button:hover{color:var(--ink)}
.filters a[aria-current],.filters button[aria-pressed=true]{color:var(--ink);text-decoration:underline;
  text-decoration-color:var(--mark);text-decoration-thickness:2px;text-underline-offset:5px}

.tl{text-decoration:none}
.tl mark{color:var(--ink);background:none;box-decoration-break:clone;-webkit-box-decoration-break:clone}
.hi .tl mark{background:linear-gradient(transparent 55%,var(--mark-bg) 55%)}
.lo .tl mark{text-decoration:underline 2px var(--rival);text-underline-offset:5px}
.tl:hover mark{color:#fff}
.vw{font-weight:700}
.hi .vw{color:var(--mark)} .lo .vw{color:var(--rival)} .off .vw{color:var(--muted)}

.entry{max-width:66ch;padding:0 0 30px;margin-bottom:30px;border-bottom:1px solid var(--rule)}
.entry h2{font:500 23px/1.35 var(--serif)}
.entry.off h2{color:var(--muted)}
.verdict{font-size:14.5px;color:var(--muted);margin-top:8px}
dl.facts{display:grid;grid-template-columns:auto 1fr;gap:4px 16px;margin:12px 0 0;font-size:15px}
dl.facts dt{color:var(--muted)}
.meta{color:var(--faint);font-size:13.5px;margin-top:10px}
.acts{display:flex;flex-wrap:wrap;gap:4px 18px;margin-top:10px}
.acts a,.acts button{font:14px/1.6 var(--sans);color:var(--muted);background:none;border:0;padding:0;cursor:pointer;
  text-decoration:underline;text-decoration-color:var(--faint);text-underline-offset:3px}
.acts a:hover,.acts button:hover{color:var(--ink);text-decoration-color:var(--ink)}

input,select{font:15px/1.3 var(--sans);color:var(--ink);background:var(--surface);border:1px solid var(--rule);
  border-radius:6px;padding:10px 12px;max-width:100%}
input::placeholder{color:var(--faint)}
label{font-size:13.5px;color:var(--muted)}

.empty{color:var(--muted);padding:30px 0}
.empty p+p{margin-top:8px}
code{background:var(--surface);padding:2px 7px;border-radius:4px;color:var(--ink);font-size:14px}

@media (max-width:860px){
  body{padding:24px 16px 80px}
  .top h1{font-size:34px}
  .lede{font-size:19px}
  .entry h2{font-size:20px}
}
"""


def esc(s):
    return html.escape(str(s or ""), quote=True)


def header(current, title):
    links = "".join(
        f'<a href="{path}"{" aria-current=page" if key == current else ""}>{label}</a>'
        for key, path, label in PAGES)
    return f'<header class="top"><h1>{esc(title)}</h1><nav class="links" aria-label="Pages">{links}</nav></header>'


def page(title, body, css="", tab_title=None):
    """A full HTML document in the shared look. `css` is page-specific CSS (plain, not escaped)."""
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{esc(tab_title or title)}</title>{FONTS}<style>{CSS}{css}</style></head>'
        f'<body>{body}</body></html>')
