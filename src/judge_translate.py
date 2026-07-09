"""
Gemini APIを使って、論文の関連度判定・日本語翻訳・4段階分類を行うモジュール。

- REST APIを直接叩く (https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent)
- 1論文につき1リクエストとし、リクエスト間にsleepを入れて無料枠のレート制限(RPM)を超えないようにする
- 429 (レート制限) の場合は指数バックオフで最大3回リトライする
- レスポンスのJSONパースに失敗した論文は、スコア0・category="ignore"として扱い処理を止めない
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


def _build_prompt(paper, research_profile, feedback_context, score_threshold):
    authors = ", ".join(paper.get("authors", []))
    return f"""あなたは理論物理学(素粒子論・重力理論)の専門家アシスタントです。
以下の研究プロファイルを持つ研究者に、次の論文が関連するかどうかを判定してください。
「現在の研究テーマ」との関連を最重視し、reasonフィールドではプロファイルのどの項目と
関係するかを具体的に明示してください。

# 研究プロファイル
{research_profile}

# 過去のフィードバック傾向(これも考慮してスコアを付けよ)
{feedback_context}

# 判定対象の論文
タイトル: {paper['title']}
著者: {authors}
主カテゴリ: {paper['primary_category']}
アブストラクト:
{paper['abstract']}

# 指示
1. 研究プロファイルとの関連度を 0〜10 の整数でスコア付けせよ(10が最も関連が高い)。
2. 以下の4段階のいずれかにcategoryを分類せよ:
   - "must_read": 現在の研究テーマに直接関係する。当日中に読むべき。
   - "worth_reading": 関連分野で手法や結果が参考になる可能性がある。今週中に目を通す価値がある。
   - "abstract_only": 分野の動向として要約だけ把握すれば十分。
   - "ignore": 関連なし。
3. title_ja にはタイトルの自然な日本語訳を書け。
4. category が "must_read" または "worth_reading" の場合のみ、アブストラクト全文を
   自然な日本語に翻訳して abstract_ja に書け。それ以外は abstract_ja は空文字列("")でよい。
   ただし物理の専門用語(replica trick, bulk reconstruction, quantum extremal surface等)は
   無理に和訳せず、慣用的なカタカナまたは英語のまま残してよい。
5. one_liner には、この論文の内容(何を扱い、どんな手法で、どんな結果を得たか)を
   2〜3文(80〜150字程度)の日本語で要約せよ。1文だけの短い要約にはしないこと。
6. check_points と suggested_action は category が "must_read" または "worth_reading" の
   場合のみ必須とし、それ以外は空文字列("")でよい。
   - check_points: 読む際に特に確認すべき箇所(セクション名、数式、前提条件など)
   - suggested_action: 読むために取るべき具体的な行動(所要時間の目安を含めてよい)
7. 以下のJSON形式のみを出力せよ。説明文やコードフェンス(```)は不要。

{{"score": <0から10の整数>, "category": "<must_read|worth_reading|abstract_only|ignore>", "title_ja": "<タイトルの日本語訳>", "reason": "<1文の判定理由(日本語)。プロファイルのどの項目と関係するかを含める>", "abstract_ja": "<アブストラクト全訳、または空文字列>", "one_liner": "<2〜3文(80〜150字程度)の日本語要約>", "check_points": "<確認すべき箇所、または空文字列>", "suggested_action": "<推奨される行動、または空文字列>"}}
"""


def _extract_json(text):
    """Gemini応答から JSON 部分を取り出す。```json フェンス付きにも対応。"""
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)
    else:
        first = text.find("{")
        last = text.rfind("}")
        if first != -1 and last != -1:
            text = text[first : last + 1]
    return json.loads(text)


def _resolve_category(parsed, score, score_threshold):
    """
    categoryが欠落・不正値の場合のフォールバック:
    既存のスコア閾値ロジックで worth_reading(スコア>=閾値) / ignore に振り分ける。
    """
    category = parsed.get("category")
    if category in VALID_CATEGORIES:
        return category
    return "worth_reading" if score >= score_threshold else "ignore"


def _call_gemini_api(prompt, api_key, model, max_retries=3):
    """Gemini APIを呼び出し、応答テキストを返す。失敗時はNoneを返す。"""
    url = f"{GEMINI_API_BASE}/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=60)
            if resp.status_code == 429:
                wait = 2 ** attempt
                print(f"[judge_translate] Gemini 429(レート制限)。{wait}秒待って再試行します ({attempt}/{max_retries})")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"[judge_translate] Gemini API呼び出し失敗 (試行{attempt}/{max_retries}): {e}")
            time.sleep(2 ** attempt)

    return None


def parse_judgement(text, score_threshold):
    """
    Gemini応答テキストを解析し、判定結果の辞書を返す。
    パース失敗やcategory不正時はフォールバックする(ユニットテスト可能なように分離)。
    """
    judgement = dict(DEFAULT_JUDGEMENT)
    if text is None:
        return judgement

    parsed = _extract_json(text)
    score = int(parsed.get("score", 0))
    judgement["score"] = score
    judgement["category"] = _resolve_category(parsed, score, score_threshold)
    judgement["reason"] = str(parsed.get("reason", ""))
    judgement["title_ja"] = str(parsed.get("title_ja", ""))
    judgement["abstract_ja"] = str(parsed.get("abstract_ja", ""))
    judgement["one_liner"] = str(parsed.get("one_liner", ""))
    judgement["check_points"] = str(parsed.get("check_points", ""))
    judgement["suggested_action"] = str(parsed.get("suggested_action", ""))
    return judgement


def judge_and_translate_papers(papers, interest_profile, feedback_list, api_key, model, score_threshold, sleep_sec=7):
    """
    論文リストを1件ずつGeminiに投げ、スコア・4段階分類・翻訳・チェック点・推奨行動を付与する。
    失敗した論文はcategory="ignore"として扱い、全体の処理は止めない。
    """
    research_profile = load_research_profile(interest_profile)
    feedback_context = build_feedback_context(feedback_list)
    results = []

    for i, paper in enumerate(papers):
        prompt = _build_prompt(paper, research_profile, feedback_context, score_threshold)
        text = _call_gemini_api(prompt, api_key, model)

        try:
            judgement = parse_judgement(text, score_threshold)
            if text is None:
                print(f"[judge_translate] Geminiから応答なし ({paper['id']})。ignore扱いとします。")
        except Exception as e:
            preview = text[:200] if text else None
            print(f"[judge_translate] JSONパース失敗 ({paper['id']}): {e} / raw: {preview}")
            judgement = dict(DEFAULT_JUDGEMENT)

        merged = dict(paper)
        merged.update(judgement)
        results.append(merged)

        # 無料枠のレート制限(RPM)を超えないよう、リクエスト間にsleepを入れる
        if i < len(papers) - 1:
            time.sleep(sleep_sec)

    return results
