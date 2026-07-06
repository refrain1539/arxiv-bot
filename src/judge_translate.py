"""
Gemini APIを使って、論文の関連度判定・日本語翻訳・一言要約を行うモジュール。

- REST APIを直接叩く (https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent)
- 1論文につき1リクエストとし、リクエスト間にsleepを入れて無料枠のレート制限(RPM)を超えないようにする
- 429 (レート制限) の場合は指数バックオフで最大3回リトライする
- レスポンスのJSONパースに失敗した論文は、スコア0として扱い処理を止めない
"""

import json
import re
import time

import requests

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# パース失敗時などに使うデフォルト値
DEFAULT_JUDGEMENT = {"score": 0, "reason": "判定に失敗しました", "abstract_ja": "", "one_liner": ""}


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


def _build_prompt(paper, interest_profile, feedback_context, score_threshold):
    authors = ", ".join(paper.get("authors", []))
    return f"""あなたは理論物理学(素粒子論・重力理論)の専門家アシスタントです。
以下の興味プロファイルを持つ研究者に、次の論文が関連するかどうかを判定してください。

# 興味プロファイル
{interest_profile}

# 過去のフィードバック傾向(これも考慮してスコアを付けよ)
{feedback_context}

# 判定対象の論文
タイトル: {paper['title']}
著者: {authors}
主カテゴリ: {paper['primary_category']}
アブストラクト:
{paper['abstract']}

# 指示
1. 興味プロファイルとの関連度を 0〜10 の整数でスコア付けせよ(10が最も関連が高い)。
2. スコアが {score_threshold} 未満の場合、abstract_ja は空文字列("")で構わない(翻訳の手間を省くため)。
   スコアが {score_threshold} 以上の場合は、アブストラクト全文を自然な日本語に翻訳せよ。
   ただし物理の専門用語(replica trick, bulk reconstruction, quantum extremal surface等)は
   無理に和訳せず、慣用的なカタカナまたは英語のまま残してよい。
3. one_liner には、この論文の内容を30字程度で要約した日本語を書け。
4. 以下のJSON形式のみを出力せよ。説明文やコードフェンス(```)は不要。

{{"score": <0から10の整数>, "reason": "<1文の判定理由(日本語)>", "abstract_ja": "<アブストラクト全訳、または空文字列>", "one_liner": "<30字程度の日本語要約>"}}
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


def judge_and_translate_papers(papers, interest_profile, feedback_list, api_key, model, score_threshold, sleep_sec=7):
    """
    論文リストを1件ずつGeminiに投げ、スコア・翻訳・一言コメントを付与する。
    失敗した論文はスコア0として扱い、全体の処理は止めない。
    """
    feedback_context = build_feedback_context(feedback_list)
    results = []

    for i, paper in enumerate(papers):
        prompt = _build_prompt(paper, interest_profile, feedback_context, score_threshold)
        text = _call_gemini_api(prompt, api_key, model)

        judgement = dict(DEFAULT_JUDGEMENT)
        if text is not None:
            try:
                parsed = _extract_json(text)
                judgement["score"] = int(parsed.get("score", 0))
                judgement["reason"] = str(parsed.get("reason", ""))
                judgement["abstract_ja"] = str(parsed.get("abstract_ja", ""))
                judgement["one_liner"] = str(parsed.get("one_liner", ""))
            except Exception as e:
                preview = text[:200] if text else None
                print(f"[judge_translate] JSONパース失敗 ({paper['id']}): {e} / raw: {preview}")
        else:
            print(f"[judge_translate] Geminiから応答なし ({paper['id']})。スコア0として扱います。")

        merged = dict(paper)
        merged.update(judgement)
        results.append(merged)

        # 無料枠のレート制限(RPM)を超えないよう、リクエスト間にsleepを入れる
        if i < len(papers) - 1:
            time.sleep(sleep_sec)

    return results
