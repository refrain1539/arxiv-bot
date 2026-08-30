"""
解説書生成モジュール。

Discord で 📖 が押された論文について、PDF全文を Gemini に渡して解説書を作り、
MathJax で数式を組版した HTML を返す。

方針:
- 入力は必ず PDF 全文。アブストラクトだけでは数式が出ず、抽象的な文章にしかならない
- 専門用語は英語のまま書かせる。無理に和訳させると non-unitary が「非探偵」になる類の
  事故が起きるため、プロンプトで実例を挙げて強く禁止している
- 式は「設定(action / metric / 境界条件)」と「最終結果」だけを見せる。途中計算の式は
  載せない。導出の道筋は文章で説明させ、結果から何が言えるかを厚く書かせる
- 出力は HTML 断片。呼び出し側が render_html() でページに組み上げる

失敗の扱い:
  生成に失敗した場合、原因を "transient"(時間をおけば直る) と
  "permanent"(何度やっても直らない) に分けて返す。呼び出し側は transient なら
  次回の実行で再試行し、permanent なら諦めて処理済みにする。

環境変数: GEMINI_API_KEY / GEMINI_MODEL (任意)
"""

import base64
import html
import os
import re
import time

import requests

from arxiv_fetch import ARXIV_API_URL, _parse_atom

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE_PATH = os.path.join(BASE_DIR, "data", "my_profile.md")

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-3.6-flash"

# Gemini のリクエスト全体の上限に収めるための PDF サイズ上限。
# base64 で約4/3に膨らむため、実バイト数で見て余裕を持たせる。
MAX_PDF_BYTES = 8 * 1024 * 1024

MAX_RETRIES = 3
GEMINI_TIMEOUT_SEC = 600
PDF_TIMEOUT_SEC = 180

PROMPT_TEMPLATE = """あなたは素粒子論(hep-th)の研究者向けに、論文の解説書を書く専門家です。
添付した PDF が論文の全文です。これを読んで、日本語で解説書を書いてください。

# 読み手のプロフィール
{profile}

# 論文
タイトル: {title}
著者: {authors}
arXiv: {url}

# 用語の扱い(最重要。ここを外すと解説書として使い物になりません)
専門用語・記号・量や空間の名前は、**英語のまま**書いてください。日本語に訳さないでください。

悪い例(実際に起きた失敗):
  non-unitary → 「非探偵」「非単一的」
  pseudo-entropy → 「疑似エントロピー」
  pure dS3 → 「純粋 dS3」
  smooth → 「滑らかな」
良い例:
  non-unitary, pseudo-entropy, pure dS$_3$, smooth

- dS$_3$, AdS$_3$, $S^3$, CFT, TTbar, entanglement entropy, holography のような語は
  すべて原語・原記号のまま使ってください。
- 日本語にしてよいのは、定訳が完全に定着しているものだけです(重力、対称性、境界条件など)。
- 迷ったら英語のままにしてください。読み手はこの分野の研究者なので、原語の方が速く読めます。
- 日本語にするのは説明のつなぎ(地の文)だけです。

# 数式をどこに出すか(最重要)
読み手は結果を速く把握したいのであって、計算を追いたいわけではありません。
**式を書いてよいのは次の2箇所だけです。**

1. 設定 ... action、metric、境界条件など「何を考えているか」を定める式
2. 主な結果 ... 最終的に得られた式そのもの

**途中計算の式は書かないでください。** 展開の中間段階、変数変換の途中、
補題の式などは不要です。それらがどう繋がるかは、式ではなく**文章**で説明してください。

- 式は LaTeX で書き、インラインは $...$、独立行は $$...$$ で囲んでください。
- 式に出てくる記号が何を表すかを必ず添えてください。
- 「主な結果」では「〜が示された」で済ませず、示された式そのものを書いてください。
- 最終結果から**何が言えるか**(物理的な意味、何が新しいのか、何が可能になるのか)を
  厚く書いてください。ここがこの解説書で最も重要な部分です。

# 出力形式
HTML の断片として出力してください。<html>, <head>, <body> タグは書かないでください。
使ってよいタグは h2, h3, p, ul, ol, li, strong, em, code, blockquote だけです。
Markdown 記法(##, **, - など)は使わないでください。すべて HTML タグで書いてください。
LaTeX の $...$ と $$...$$ はそのまま残してください(MathJax が描画します)。
コードブロックや ``` は使わないでください。

# 構成
<h2>一言でいうと</h2>
  この論文が何をしたのかを3〜4行で。式は不要。

<h2>背景</h2>
  何が問題だったのか。読み手が知らない可能性がある部分だけを補う。式は最小限。

<h2>設定</h2>
  action、metric、境界条件など、何を考えているかを定める式。ここは式で書く。
  記号の定義を必ず添える。

<h2>主な結果</h2>
  最終的に得られた式。ここも式で書く。途中経過は書かない。

<h2>結果の意味</h2>
  その結果から何が言えるか。何が新しく、何が可能になったのか。
  この節を最も厚く書いてください。式は原則不要で、文章で書いてください。

<h2>導出の道筋</h2>
  どういう方針で、どこを通って結果に至るのか。**式は書かず、文章だけ**で説明してください。
  「まず〜を〜の形に書き直し、次に〜の極限をとる」のように、流れが分かる形で。

<h2>読み手の研究との接点</h2>
  プロフィールを踏まえて具体的に。関係が薄いなら薄いと正直に書く。無理に関連づけない。

<h2>読む順序と詰まりやすい箇所</h2>
  どの節から読むか、どこで手が止まるか。式は不要。

<h2>関連文献</h2>
  3件程度。PDF の参考文献欄から拾えるものは arXiv 番号も。
  PDF から確認できないものは「要確認」と明記。

# 注意
- PDF から確認できないことを断定しないでください。推測は推測と明示してください。
- 全体で3000〜5000字程度。
"""

HTML_TEMPLATE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title_esc}</title>
<script>
  window.MathJax = {{
    tex: {{ inlineMath: [['$', '$']], displayMath: [['$$', '$$']] }},
    svg: {{ fontCache: 'global' }}
  }};
</script>
<script id="MathJax-script" async
  src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
<style>
  :root {{ --fg: #1a1a1a; --bg: #fdfdfc; --muted: #6b6b6b; --rule: #e0e0dd; --accent: #7a5c3e; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --fg: #e6e6e3; --bg: #1b1b1a; --muted: #9c9c98; --rule: #343432; --accent: #c9a87c; }}
  }}
  body {{
    max-width: 46rem; margin: 0 auto; padding: 3rem 1.5rem 6rem;
    background: var(--bg); color: var(--fg);
    font-family: "Hiragino Kaku Gothic ProN", "Yu Gothic", Meiryo, system-ui, sans-serif;
    line-height: 1.85; font-size: 16.5px;
  }}
  header {{ border-bottom: 1px solid var(--rule); padding-bottom: 1.5rem; margin-bottom: 2.5rem; }}
  h1 {{ font-size: 1.5rem; line-height: 1.45; margin: 0 0 .75rem; }}
  .meta {{ color: var(--muted); font-size: .875rem; line-height: 1.7; }}
  .meta a {{ color: var(--accent); }}
  h2 {{
    font-size: 1.15rem; margin: 2.75rem 0 .9rem; padding-bottom: .35rem;
    border-bottom: 1px solid var(--rule);
  }}
  h3 {{ font-size: 1rem; margin: 1.75rem 0 .5rem; color: var(--accent); }}
  p, li {{ margin: .7rem 0; }}
  code {{
    font-family: ui-monospace, "SFMono-Regular", Consolas, monospace;
    font-size: .9em; background: rgba(127,127,127,.13); padding: .1em .35em; border-radius: 3px;
  }}
  blockquote {{
    margin: 1rem 0; padding: .3rem 0 .3rem 1rem;
    border-left: 3px solid var(--rule); color: var(--muted);
  }}
  mjx-container[display="true"] {{ overflow-x: auto; overflow-y: hidden; padding: .3rem 0; }}
  footer {{ margin-top: 4rem; padding-top: 1.25rem; border-top: 1px solid var(--rule);
            color: var(--muted); font-size: .8rem; }}
</style>
</head>
<body>
<header>
  <h1>{title_esc}</h1>
  <div class="meta">
    {authors_esc}<br>
    <a href="{url_esc}">{url_esc}</a>
  </div>
</header>
{content}
<footer>
  arxiv-bot が {model_esc} で生成した解説書です。原論文にあたって確認してください。
</footer>
</body>
</html>
"""


def load_profile(path=PROFILE_PATH):
    """興味プロファイルを読む。無ければ空扱いにする。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return "(プロフィール未設定)"


def fetch_paper_meta(arxiv_id):
    """arXiv API から1件分のメタデータを取得する。失敗したら None。"""
    try:
        resp = requests.get(
            ARXIV_API_URL,
            params={"id_list": arxiv_id, "max_results": 1},
            timeout=60,
            headers={"User-Agent": "arxiv-hep-th-bot"},
        )
        resp.raise_for_status()
        papers = _parse_atom(resp.text)
        return papers[0] if papers else None
    except Exception as e:
        print(f"[explain] メタデータの取得に失敗しました ({arxiv_id}): {e}")
        return None


def fetch_pdf(arxiv_id):
    """
    arXiv から PDF を取得する。
    戻り値: (bytes, None) または (None, "transient"|"permanent")
    """
    url = f"https://arxiv.org/pdf/{arxiv_id}"
    try:
        resp = requests.get(url, timeout=PDF_TIMEOUT_SEC, headers={"User-Agent": "arxiv-hep-th-bot"})
        resp.raise_for_status()
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", None)
        # 404 は論文自体が無い(PDF未提供など)ので、再試行しても直らない
        kind = "permanent" if status and 400 <= status < 500 else "transient"
        print(f"[explain] PDFの取得に失敗しました ({arxiv_id}): {e}")
        return None, kind
    except Exception as e:
        print(f"[explain] PDFの取得に失敗しました ({arxiv_id}): {e}")
        return None, "transient"

    if len(resp.content) > MAX_PDF_BYTES:
        print(
            f"[explain] PDFが大きすぎます ({len(resp.content) / 1024 / 1024:.1f}MB > "
            f"{MAX_PDF_BYTES / 1024 / 1024:.0f}MB): {arxiv_id}"
        )
        return None, "permanent"

    print(f"[explain] PDFを取得しました ({len(resp.content) / 1024:.0f}KB): {arxiv_id}")
    return resp.content, None


def build_prompt(paper, profile):
    return PROMPT_TEMPLATE.format(
        profile=profile,
        title=paper.get("title", ""),
        authors=", ".join(paper.get("authors", [])),
        url=paper.get("url", ""),
    )


def clean_fragment(text):
    """モデルが ```html で包んだり <body> を付けたりした場合に剥がす。"""
    text = re.sub(r"^\s*```(?:html)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    text = re.sub(r"(?is)</?(?:html|head|body)\b[^>]*>", "", text)
    text = re.sub(r"(?is)<!doctype[^>]*>", "", text)
    return text.strip()


def render_html(paper, content, model):
    """解説書のHTML断片を、MathJax入りの1枚のページに組み上げる。"""
    return HTML_TEMPLATE.format(
        title_esc=html.escape(paper.get("title", "")),
        authors_esc=html.escape(", ".join(paper.get("authors", []))),
        url_esc=html.escape(paper.get("url", "")),
        content=content,
        model_esc=html.escape(model),
    )


def _call_gemini(parts, api_key, model):
    """
    Gemini を呼び出して応答テキストを返す。
    戻り値: (text, None) または (None, "transient"|"permanent")
    """
    url = f"{GEMINI_API_BASE}/{model}:generateContent?key={api_key}"
    payload = {"contents": [{"parts": parts}], "generationConfig": {"temperature": 0.3}}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=GEMINI_TIMEOUT_SEC)
            if resp.status_code == 429:
                wait = 2 ** attempt
                print(f"[explain] Gemini 429(レート制限)。{wait}秒待って再試行します ({attempt}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            if resp.status_code == 400:
                # リクエストが大きすぎる・PDFを受け付けない等。再試行しても直らない
                print(f"[explain] Gemini が 400 を返しました: {resp.text[:300]}")
                return None, "permanent"
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"], None
        except Exception as e:
            print(f"[explain] Gemini 呼び出し失敗 (試行{attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)

    return None, "transient"


def count_formulas(content):
    """生成物にどれだけ数式が入ったかを数える(ログで質を確認するため)。"""
    display = content.count("$$") // 2
    inline = (content.count("$") - content.count("$$") * 2) // 2
    return display, max(0, inline)


def generate_explanation(arxiv_id, api_key, model=None, profile=None, title=None):
    """
    1論文分の解説書HTMLを生成する。

    戻り値: (html_str, None) または (None, "transient"|"permanent")
      transient ... 時間をおけば直る可能性がある(429・ネットワーク不調など)
      permanent ... 何度やっても直らない(PDFが大きすぎる・400など)
    """
    if not api_key:
        print("[explain] GEMINI_API_KEY が未設定のため、解説書を生成できません")
        return None, "permanent"

    model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL

    paper = fetch_paper_meta(arxiv_id)
    if paper is None:
        # メタデータが引けなくても、タイトルさえ分かれば生成は続けられる
        paper = {"title": title or arxiv_id, "authors": [], "url": f"https://arxiv.org/abs/{arxiv_id}"}

    pdf, error = fetch_pdf(arxiv_id)
    if pdf is None:
        return None, error

    parts = [
        {"text": build_prompt(paper, profile if profile is not None else load_profile())},
        {
            "inline_data": {
                "mime_type": "application/pdf",
                "data": base64.b64encode(pdf).decode("ascii"),
            }
        },
    ]

    print(f"[explain] Gemini に送信します ({arxiv_id}, モデル: {model})")
    started = time.time()
    text, error = _call_gemini(parts, api_key, model)
    if text is None:
        return None, error

    content = clean_fragment(text)
    display, inline = count_formulas(content)
    print(
        f"[explain] 生成しました ({time.time() - started:.0f}秒, {len(content)}字, "
        f"独立行の数式{display}本 / インライン{inline}個): {arxiv_id}"
    )
    return render_html(paper, content, model), None
