"""
Gemini APIを使って、論文の関連度判定・日本語翻訳・4段階分類を行うモジュール。

- REST APIを直接叩く (https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent)
- 複数論文(既定8件)をまとめて1リクエストで判定する(バッチ処理)。無料枠は1日あたりの
  リクエスト数(RPD)上限が厳しく、論文数が多い日に1論文=1リクエストだと上限に達して
  429が解消しなくなるため、リクエスト数そのものを削減する。
- バッチ間にsleepを入れてレート制限(RPM)も超えないようにする
- 429 (レート制限) の場合は指数バックオフで最大3回リトライする
- 応答は Gemini の構造化出力(responseMimeType + responseSchema)で受け取り、構文的に
  正しいJSONを保証させる。モデルがスキーマを受け付けない場合(400)はスキーマなしで再試行する
- それでもJSONが壊れている場合に備えて2段の保険を持つ:
  (1) LaTeX由来の不正なエスケープ(\\infty, \\mathcal 等)をリテラルなバックスラッシュに修復
  (2) 配列全体が復元できなければ、波括弧の対応を数えて1オブジェクトずつ救出する
  これを入れる前は、1論文に含まれた1個のバックスラッシュでバッチ8件が丸ごと
  ignore に落ちていた(2026-08-29 の本番実行では15バッチ中7バッチが全損)
- 上記でもパースできなかったバッチは全論文をスコア0・category="ignore"
  として扱い処理を止めない
- data/my_profile.md が存在する場合は、config.yml の interest_profile より優先して
  プロンプトに注入する(研究プロファイル注入。存在しなければ従来通りinterest_profileを使う)
"""

import json
import os
import re
import time

import requests

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MY_PROFILE_PATH = os.path.join(BASE_DIR, "data", "my_profile.md")
MY_PROFILE_MAX_CHARS = 4000

VALID_CATEGORIES = {"must_read", "worth_reading", "abstract_only", "ignore"}
CATEGORY_ORDER = {"must_read": 0, "worth_reading": 1, "abstract_only": 2, "ignore": 3}

DEFAULT_BATCH_SIZE = 8

# 1バッチ(既定8件)のアブストラクト全訳まで生成させるため、応答には時間がかかる。
# 120秒では読み取りタイムアウトが頻発したため(2026-08-31 の実行で3バッチが該当。
# いずれもリトライで復帰したが、実行時間が13分に伸びた)、余裕を持たせている。
GEMINI_TIMEOUT_SEC = 240

# Gemini の構造化出力(responseSchema)。OpenAPI のサブセットで型名は大文字。
RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "index": {"type": "INTEGER"},
            "score": {"type": "INTEGER"},
            "category": {"type": "STRING", "enum": sorted(VALID_CATEGORIES)},
            "title_ja": {"type": "STRING"},
            "reason": {"type": "STRING"},
            "abstract_ja": {"type": "STRING"},
            "one_liner": {"type": "STRING"},
            "check_points": {"type": "STRING"},
            "suggested_action": {"type": "STRING"},
        },
        "required": [
            "index",
            "score",
            "category",
            "title_ja",
            "reason",
            "abstract_ja",
            "one_liner",
            "check_points",
            "suggested_action",
        ],
    },
}

# パース失敗時などに使うデフォルト値
DEFAULT_JUDGEMENT = {
    "score": 0,
    "category": "ignore",
    "reason": "判定に失敗しました",
    "title_ja": "",
    "abstract_ja": "",
    "one_liner": "",
    "check_points": "",
    "suggested_action": "",
}


def load_research_profile(fallback_profile):
    """
    data/my_profile.md があればそちらを読み込み(先頭4000字に切り詰め)、
    無ければ config.yml の interest_profile (fallback_profile) を返す。
    """
    try:
        with open(MY_PROFILE_PATH, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return fallback_profile

    if len(text) > MY_PROFILE_MAX_CHARS:
        print(
            f"[judge_translate] data/my_profile.md が{MY_PROFILE_MAX_CHARS}字を超えているため、"
            f"先頭{MY_PROFILE_MAX_CHARS}字に切り詰めます(元の長さ: {len(text)}字)"
        )
        text = text[:MY_PROFILE_MAX_CHARS]

    return text


def build_feedback_context(feedback_list, limit=15):
    """
    feedback.json の中身から「興味あり/興味なし」の最新タイトルを抜き出し、
    プロンプトに埋め込むテキストを作る。
    """
    likes = [f["title"] for f in feedback_list if f.get("verdict") == "like"]
    dislikes = [f["title"] for f in feedback_list if f.get("verdict") == "dislike"]

    likes = likes[-limit:]
    dislikes = dislikes[-limit:]

    lines = []
    if likes:
        lines.append("【過去に「興味あり」と評価された論文タイトル】")
        lines.extend(f"- {t}" for t in likes)
    if dislikes:
        lines.append("【過去に「興味なし」と評価された論文タイトル】")
        lines.extend(f"- {t}" for t in dislikes)

    if not lines:
        return "(まだフィードバックの蓄積がありません)"

    return "\n".join(lines)


def _build_batch_prompt(batch, research_profile, feedback_context, score_threshold):
    entries = []
    for i, paper in enumerate(batch, start=1):
        authors = ", ".join(paper.get("authors", []))
        entries.append(
            f"### 論文{i}\n"
            f"タイトル: {paper['title']}\n"
            f"著者: {authors}\n"
            f"主カテゴリ: {paper['primary_category']}\n"
            f"アブストラクト:\n{paper['abstract']}\n"
        )
    papers_block = "\n".join(entries)
    n = len(batch)

    return f"""あなたは理論物理学(素粒子論・重力理論)の専門家アシスタントです。
以下の研究プロファイルを持つ研究者に、次の{n}件の論文それぞれが関連するかどうかを判定してください。
研究プロファイルに挙げられた研究領域との関連を重視してcategoryとscoreを判定してください。

# 研究プロファイル
{research_profile}

# 過去のフィードバック傾向(これも考慮してスコアを付けよ)
{feedback_context}

# 判定対象の論文({n}件)
{papers_block}

# 指示
各論文について、以下を行え:
1. 研究プロファイルとの関連度を 0〜10 の整数でスコア付けせよ(10が最も関連が高い)。
2. 以下の4段階のいずれかにcategoryを分類せよ:
   - "must_read": プロファイルの研究領域に直接該当し、手法・結果とも参考になる。当日中に読むべき。
   - "worth_reading": 関連分野で手法や結果が参考になる可能性がある。今週中に目を通す価値がある。
   - "abstract_only": 分野の動向として要約だけ把握すれば十分。
   - "ignore": 関連なし。
3. title_ja にはタイトルの自然な日本語訳を書け。
4. category が "must_read" または "worth_reading" の場合のみ、アブストラクト全文を
   自然な日本語に翻訳して abstract_ja に書け。それ以外は abstract_ja は空文字列("")でよい。
   ただし物理の専門用語(replica trick, bulk reconstruction, quantum extremal surface等)は
   無理に和訳せず、慣用的なカタカナまたは英語のまま残してよい。
5. reason には、この論文が具体的に何を主張・証明・計算しているかを1〜2文で書け。
   「プロファイルの研究領域に関連するため」「〇〇や△△に直接関連する」のようにテーマ名を
   並べるだけの抽象的な理由は禁止する。必ず論文の中身(対象・手法・結果)に基づいて書くこと。
   ただし、論文の中身がプロファイルの研究領域と具体的に一致する場合
   (例: Gödel時空を用いた具体的な解析を行っている、TTbar変形の具体的な計算を行っている等)
   に限り、その一致点も1文で書き加えてよい。
6. one_liner には、この論文の内容(何を扱い、どんな手法で、どんな結果を得たか)を
   2〜3文(80〜150字程度)の日本語で要約せよ。1文だけの短い要約や空文字列は不可。
   reasonと同じ内容の繰り返しにせず、必ず具体的な内容を書くこと。
7. check_points と suggested_action は category が "must_read" または "worth_reading" の
   場合のみ必須とし、それ以外は空文字列("")でよい。
   - check_points: 読む際に特に確認すべき箇所(セクション名、数式、前提条件など)
   - suggested_action: 読むために取るべき具体的な行動(所要時間の目安を含めてよい)
8. 出力するJSON文字列の中でバックスラッシュ(\\)を使ってはならない。数式はLaTeXではなく
   通常のテキストで書け(例: \\infty ではなく infinity、\\mathcal N ではなく N、
   \\textsubscript は使わず添字はそのまま書く)。バックスラッシュはJSONのエスケープ文字であり、
   不正なエスケープを含む応答は破棄される。
9. 以下のJSON配列の形式のみを出力せよ。説明文やコードフェンス(```)は不要。
   配列の要素数は必ず論文の件数({n}件)と一致させ、"index"には論文番号
   (論文1なら1、論文2なら2、...)を入れること。全ての論文番号を過不足なく1回ずつ含めること。

[{{"index": <論文番号>, "score": <0から10の整数>, "category": "<must_read|worth_reading|abstract_only|ignore>", "title_ja": "<タイトルの日本語訳>", "reason": "<論文の中身に基づく1〜2文の理由。研究テーマの核心と具体的に一致する場合のみその旨を追記>", "abstract_ja": "<アブストラクト全訳、または空文字列>", "one_liner": "<2〜3文(80〜150字程度)の日本語要約>", "check_points": "<確認すべき箇所、または空文字列>", "suggested_action": "<推奨される行動、または空文字列>"}}, ...]
"""


# JSONとして不正なエスケープ: \ の直後が許容文字でない、または \u の直後が16進4桁でない
_INVALID_ESCAPE_RE = re.compile(r'\\(?:u(?![0-9a-fA-F]{4})|(?!["\\/bfnrtu]))')


def repair_json_escapes(text):
    """
    JSON文字列中の不正なエスケープをリテラルなバックスラッシュに直す。
    Geminiが理由や翻訳の中にLaTeX($Lw_{1+\\infty}$ 等)を書くと \\i が不正なエスケープになり、
    json.loads が "Invalid \\escape" で失敗してバッチ全体が捨てられるため。
    """
    return _INVALID_ESCAPE_RE.sub(r"\\\\", text)


def salvage_json_objects(text):
    """
    配列全体のパースに失敗したときの最終防衛線。波括弧の対応を数えて1オブジェクトずつ
    取り出し、パースできたものだけを返す。1論文分が壊れていても残りを救う。
    """
    objects = []
    depth = 0
    start = None
    in_string = False
    escaped = False

    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                chunk = text[start : i + 1]
                for candidate in (chunk, repair_json_escapes(chunk)):
                    try:
                        objects.append(json.loads(candidate))
                        break
                    except json.JSONDecodeError:
                        continue
                start = None

    return objects


def _extract_json_array(text):
    """
    Gemini応答から JSON配列 部分を取り出す。```json フェンス付きにも対応。
    素のパースに失敗した場合は、不正エスケープの修復 → 1オブジェクトずつの救出、
    の順に段階的にフォールバックする。すべて失敗した場合のみ例外を投げる。
    """
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\[.*\])\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)
    else:
        first = text.find("[")
        last = text.rfind("]")
        if first != -1 and last != -1:
            text = text[first : last + 1]

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    try:
        parsed = json.loads(repair_json_escapes(text))
        print("[judge_translate] 不正なエスケープを修復してパースしました")
        return parsed
    except json.JSONDecodeError:
        pass

    salvaged = salvage_json_objects(text)
    if salvaged:
        print(f"[judge_translate] 配列の復元に失敗しましたが、{len(salvaged)}件を個別に救出しました")
        return salvaged

    raise json.JSONDecodeError("JSON配列を復元できませんでした", text[:200], 0)


def _resolve_category(parsed, score, score_threshold):
    """
    categoryが欠落・不正値の場合のフォールバック:
    既存のスコア閾値ロジックで worth_reading(スコア>=閾値) / ignore に振り分ける。
    """
    category = parsed.get("category")
    if category in VALID_CATEGORIES:
        return category
    return "worth_reading" if score >= score_threshold else "ignore"


def _build_payload(prompt, with_schema):
    generation_config = {"temperature": 0.2}
    if with_schema:
        generation_config["responseMimeType"] = "application/json"
        generation_config["responseSchema"] = RESPONSE_SCHEMA
    return {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }


def _call_gemini_api(prompt, api_key, model, max_retries=3):
    """
    Gemini APIを呼び出し、応答テキストを返す。失敗時はNoneを返す。
    構造化出力(responseSchema)付きで投げ、モデルが受け付けない場合(400)は
    スキーマなしにフォールバックして再試行する。
    """
    url = f"{GEMINI_API_BASE}/{model}:generateContent?key={api_key}"
    with_schema = True

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                url, json=_build_payload(prompt, with_schema), timeout=GEMINI_TIMEOUT_SEC
            )
            if resp.status_code == 429:
                wait = 2 ** attempt
                print(f"[judge_translate] Gemini 429(レート制限)。{wait}秒待って再試行します ({attempt}/{max_retries})")
                time.sleep(wait)
                continue
            if resp.status_code == 400 and with_schema:
                print(
                    "[judge_translate] responseSchema が拒否されました(400)。"
                    f"スキーマなしで再試行します: {resp.text[:200]}"
                )
                with_schema = False
                continue
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"[judge_translate] Gemini API呼び出し失敗 (試行{attempt}/{max_retries}): {e}")
            time.sleep(2 ** attempt)

    return None


def _judgement_from_item(item, score_threshold):
    score = int(item.get("score", 0))
    judgement = dict(DEFAULT_JUDGEMENT)
    judgement["score"] = score
    judgement["category"] = _resolve_category(item, score, score_threshold)
    judgement["reason"] = str(item.get("reason", ""))
    judgement["title_ja"] = str(item.get("title_ja", ""))
    judgement["abstract_ja"] = str(item.get("abstract_ja", ""))
    judgement["one_liner"] = str(item.get("one_liner", ""))
    judgement["check_points"] = str(item.get("check_points", ""))
    judgement["suggested_action"] = str(item.get("suggested_action", ""))
    return judgement


def parse_batch_judgements(text, batch_size, score_threshold):
    """
    バッチ応答(JSON配列)を解析し、{論文番号(1始まり): judgement辞書} を返す。
    "index"が範囲外・欠落の要素は無視する。パース失敗時は例外を投げる
    (呼び出し側でバッチ全体をフォールバックさせるため、ユニットテスト可能なように分離)。
    """
    parsed_list = _extract_json_array(text)
    results = {}
    for item in parsed_list:
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if idx < 1 or idx > batch_size:
            continue
        results[idx] = _judgement_from_item(item, score_threshold)
    return results


def judge_and_translate_papers(
    papers,
    interest_profile,
    feedback_list,
    api_key,
    model,
    score_threshold,
    batch_size=DEFAULT_BATCH_SIZE,
    sleep_sec=8,
):
    """
    論文リストをbatch_size件ずつまとめてGeminiに投げ、スコア・4段階分類・翻訳・
    チェック点・推奨行動を付与する。バッチ単位で失敗した論文はcategory="ignore"
    として扱い、全体の処理は止めない。
    """
    research_profile = load_research_profile(interest_profile)
    feedback_context = build_feedback_context(feedback_list)
    results = []

    batches = [papers[i : i + batch_size] for i in range(0, len(papers), batch_size)]

    for b, batch in enumerate(batches):
        prompt = _build_batch_prompt(batch, research_profile, feedback_context, score_threshold)
        text = _call_gemini_api(prompt, api_key, model)

        judgements_by_index = {}
        if text is not None:
            try:
                judgements_by_index = parse_batch_judgements(text, len(batch), score_threshold)
            except Exception as e:
                preview = text[:300] if text else None
                print(f"[judge_translate] バッチ{b + 1}のJSONパース失敗: {e} / raw: {preview}")
        else:
            print(f"[judge_translate] バッチ{b + 1}: Geminiから応答なし。このバッチはignore扱いとします。")

        for i, paper in enumerate(batch, start=1):
            judgement = judgements_by_index.get(i)
            if judgement is None:
                print(f"[judge_translate] バッチ{b + 1}: 論文{i}({paper['id']})の判定が結果に含まれていません。ignore扱いとします。")
                judgement = dict(DEFAULT_JUDGEMENT)
            merged = dict(paper)
            merged.update(judgement)
            results.append(merged)

        # 無料枠のレート制限(RPM)を超えないよう、バッチ間にsleepを入れる
        if b < len(batches) - 1:
            time.sleep(sleep_sec)

    return results
