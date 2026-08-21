"""トレーニング記録 ダッシュボード（Notion × Streamlit）

Notion の「トレーニング記録」DB を読み書きする。
- ✍️ 今日の記録   : 今週の種目に実績（重量・ログ・達成）をスマホから入力
- 📊 グラフ       : 重量推移・自己ベスト・達成状況・今週メニューを可視化
- 📤 まとめ＆計画 : 今週のまとめをClaude用に出力 / 来週メニューを貼り付けて取り込み

すべて無料の仕組みだけで動く。実行: streamlit run app.py
"""

from __future__ import annotations  # Python 3.9 でも `float | None` 等の型注釈を使えるようにする

import datetime as dt
import html
import json
import os
import re
import time

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
from streamlit_local_storage import LocalStorage

# --- 設定 ----------------------------------------------------------------
DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "dc49803fa43a48868e54824072e2ffb1")
NOTION_VERSION = "2022-06-28"
API = "https://api.notion.com/v1"
KEY_LIFTS = ["レッグプレス", "チェストプレス(15°)", "シーテッドロー", "ラットプルダウン(ワイド)"]
SPLITS = ["Push A", "Pull A", "Legs A", "Push B", "Pull B", "Legs B"]
ACHIEVE_OPTIONS = ["✅達成", "△一部", "❌未達", "－スキップ"]
PARTS_OPTIONS = ["胸", "背中", "肩", "脚", "腕", "腹", "体幹"]
# 来週メニューの貼り付け形式
MENU_FORMAT = "分割 | 種目 | 目標 | 目標重量 | 部位(任意,カンマ区切り)"
DRAFT_KEY = "tl_draft"  # 入力中の下書きを端末(ブラウザ)に一時保存するキー
# 下書きとして保存する session_state のキー接頭辞（重量/回数/マスター/達成）。
# セット数(n_)は目標から毎回自動推定する値なので下書きに含めない。
# （含めると古い「1セット」等が端末に残り続け、毎回それが復元されてしまう）
DRAFT_PREFIXES = ("w_", "r_", "m_", "a_", "g_")
# 最終目標(3ヶ月後)を持たせるセンチネル行の週。通常の週(数値)と分けるための固定値。
# 週num が数値にならないので、週次のビュー(入力/まとめ/推移)には一切出てこない。
GOAL_WEEK = "目標"
# 目標を更新したとき、古い目標を残しておく行の週（履歴。グラフ・週次ビューには出ない）
GOAL_HISTORY_WEEK = "目標履歴"
# 最終目標の貼り付け形式（Claudeに出力してもらう）
GOAL_FORMAT = "種目 | 最終目標重量"
LB_TO_KG = 0.4536  # 記録は lb。表示で kg を併記するときの換算係数


def _secret(key: str) -> str:
    """st.secrets か環境変数から値を取得（無ければ空文字）。"""
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.environ.get(key, "")


def get_token() -> str:
    """Notion トークンを取得。"""
    return _secret("NOTION_TOKEN")


def check_password() -> bool:
    """APP_PASSWORD が設定されていればパスワード画面を出す。未設定なら素通り（ローカル用）。"""
    expected = _secret("APP_PASSWORD")
    if not expected:
        return True  # パスワード未設定（ローカル開発）なら認証不要
    if st.session_state.get("auth_ok"):
        return True
    st.markdown("### 🔒 パスワード")
    pw = st.text_input("パスワードを入力", type="password", label_visibility="collapsed")
    if pw:
        if pw == expected:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("パスワードが違います。")
    return False


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _text(prop) -> str:
    """title / rich_text プロパティを素のテキストにする。"""
    if not prop:
        return ""
    arr = prop.get("title") or prop.get("rich_text") or []
    return "".join(a.get("plain_text", "") for a in arr)


@st.cache_data(ttl=1800, show_spinner="Notion から取得中…")
def fetch_records(token: str) -> pd.DataFrame:
    """Notion DB を全ページ取得して DataFrame にする（30分キャッシュ）。page_id 付き。
    入力中に勝手に再取得されて体感がリセットされるのを防ぐため長めにする。"""
    url = f"{API}/databases/{DATABASE_ID}/query"
    rows, payload = [], {"page_size": 100}
    while True:
        # 接続エラー/タイムアウトは一過性が多いので数回リトライしてから諦める。
        # （Cloud→Notion のネットワーク瞬断でアプリごと落ちるのを防ぐ）
        for attempt in range(3):
            try:
                resp = requests.post(url, headers=_headers(token), json=payload, timeout=30)
                resp.raise_for_status()
                break
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 2:
                    raise
                time.sleep(1.5 * (attempt + 1))
        data = resp.json()
        for page in data["results"]:
            p = page["properties"]
            rows.append(
                {
                    "page_id": page["id"],
                    "created": page.get("created_time"),
                    "種目": _text(p.get("種目")),
                    "日付": (p.get("日付", {}).get("date") or {}).get("start"),
                    "週": (p.get("週", {}).get("select") or {}).get("name"),
                    "分割": (p.get("分割", {}).get("select") or {}).get("name"),
                    "部位": ", ".join(o["name"] for o in (p.get("部位", {}).get("multi_select") or [])),
                    "目標": _text(p.get("目標")),
                    "目標重量": _text(p.get("目標重量")),
                    "実績重量": (p.get("実績重量", {}) or {}).get("number"),
                    "実績ログ": _text(p.get("実績ログ")),
                    "達成": (p.get("達成", {}).get("select") or {}).get("name"),
                    "メモ": _text(p.get("メモ")),
                    "順番": (p.get("順番", {}) or {}).get("number"),
                    "体重": (p.get("体重", {}) or {}).get("number"),
                }
            )
        if data.get("has_more"):
            payload["start_cursor"] = data["next_cursor"]
        else:
            break

    df = pd.DataFrame(rows)
    df["週num"] = pd.to_numeric(df["週"], errors="coerce")
    df["日付"] = pd.to_datetime(df["日付"], errors="coerce")
    return df


def update_record(token: str, page_id: str, *, weight=None, log=None,
                  achieve=None, date=None, memo=None, bodyweight=None) -> None:
    """1行の実績を Notion に書き込む。None の項目は触らない。"""
    props = {}
    if weight is not None:
        props["実績重量"] = {"number": float(weight)}
    if log is not None and log != "":
        props["実績ログ"] = {"rich_text": [{"text": {"content": log}}]}
    if achieve:
        props["達成"] = {"select": {"name": achieve}}
    if date is not None:
        props["日付"] = {"date": {"start": date.isoformat()}}
    if memo is not None and memo != "":
        props["メモ"] = {"rich_text": [{"text": {"content": memo}}]}
    if bodyweight is not None:
        props["体重"] = {"number": float(bodyweight)}
    if not props:
        return
    resp = requests.patch(
        f"{API}/pages/{page_id}", headers=_headers(token),
        json={"properties": props}, timeout=30,
    )
    resp.raise_for_status()


def create_record(token: str, *, week, split, exercise, goal="",
                  goal_weight="", parts=None, order=None) -> None:
    """新しい行（種目）を作成する。来週メニューの取り込みに使う。"""
    props = {
        "種目": {"title": [{"text": {"content": exercise}}]},
        "週": {"select": {"name": str(week)}},
        "分割": {"select": {"name": split}},
    }
    if goal:
        props["目標"] = {"rich_text": [{"text": {"content": goal}}]}
    if goal_weight:
        props["目標重量"] = {"rich_text": [{"text": {"content": goal_weight}}]}
    if parts:
        props["部位"] = {"multi_select": [{"name": p} for p in parts]}
    if order is not None:
        props["順番"] = {"number": order}
    resp = requests.post(
        f"{API}/pages", headers=_headers(token),
        json={"parent": {"database_id": DATABASE_ID}, "properties": props}, timeout=30,
    )
    resp.raise_for_status()


def upsert_goal(token: str, df: pd.DataFrame, exercise: str, goal_weight: str,
                until_week=None) -> None:
    """最終目標(週='目標')を種目ごとに1行だけ持つ。既にあれば目標重量を更新、無ければ作成。
    更新で値が変わるときは、古い目標を履歴(週='目標履歴')として別行に残す（上書きで消さない）。"""
    existing = df[(df["週"] == GOAL_WEEK) & (df["種目"] == exercise)]
    if not existing.empty:
        # 表示側は drop_duplicates(keep="last") で最後の行を見るので、更新もそこに合わせる
        # （先頭を更新すると「保存しました」と出るのにグラフの目標が変わらない）
        old = str(existing.iloc[-1]["目標重量"] or "")
        page_id = existing.iloc[-1]["page_id"]
        if old and old != goal_weight:
            # 旧目標を履歴として保管（週num が数値にならないので週次ビューには出ない）
            create_record(
                token, week=GOAL_HISTORY_WEEK, split=GOAL_HISTORY_WEEK, exercise=exercise,
                goal=(f"〜W{until_week} の目標" if until_week else "旧目標"), goal_weight=old,
            )
        resp = requests.patch(
            f"{API}/pages/{page_id}", headers=_headers(token),
            json={"properties": {"目標重量": {"rich_text": [{"text": {"content": goal_weight}}]}}},
            timeout=30,
        )
        resp.raise_for_status()
    else:
        create_record(token, week=GOAL_WEEK, split=GOAL_WEEK,
                      exercise=exercise, goal_weight=goal_weight)


def parse_goals(text: str):
    """貼り付けた最終目標をパースして dict のリストにする（'種目 | 目標重量' の1行1種目）。"""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("```"):
            continue
        if line.startswith("種目") and "目標" in line:  # ヘッダ行を無視
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        out.append({"種目": parts[0], "目標重量": parts[1]})
    return out


def is_bodyweight(row) -> bool:
    """回数だけを記録する種目（自重・サーキット・時間系）は重量入力を出さない。
    '自重'や'周'が入っていれば数字があっても回数種目とみなす。
    例: '自重(セット増)' / '3周' / '自重20回' / 空欄 / 懸垂の'加重10lb×8+自重10' → True、
        '22.5lb(+0)' → False。
    （数字の有無だけで判定していた頃は '3周' や '自重20回' に重量欄が出てしまい、
      回数が実績重量として記録される原因になっていた）"""
    s = str(row.get("目標重量") or "")
    if "自重" in s or "周" in s:
        return True
    if not re.search(r"\d", s):
        return True
    # 単位が無く「回」だけの目標（例: '20回'）も回数種目
    return "回" in s and not re.search(r"(lb|kg)", s, re.IGNORECASE)


def parse_target_weight(text) -> float | None:
    """目標重量の文字列から目標の数値だけ取り出す。
    例: '22.5lb(+0)'→22.5、'15lb(維持)'→15、'自重'/空欄→None。
    『合計30回』のように合計回数が書かれていれば、その回数を目標とする
    （懸垂など加重メモが先に来る種目で、先頭の加重kgを誤って拾わないため）。"""
    if not text:
        return None
    s = str(text)
    m = re.search(r"合計\s*(\d+(?:\.\d+)?)\s*回", s)  # 「合計30回」→30（総回数が目標）
    if m:
        return float(m.group(1))
    if is_reps_goal(s):
        # 『加重10lb×8+自重10』のような回数種目。先頭の加重(10lb)を目標重量と
        # 誤読すると達成率が数百%になるため、末尾の合計回数を目標とみなす。
        nums = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)", s)]
        return sum(nums[1:]) if len(nums) > 1 else (nums[0] if nums else None)
    # kg で書かれた目標は lb に直す（記録が lb 規約のため）
    m = re.search(r"(\d+(?:\.\d+)?)\s*kg", s, re.IGNORECASE)
    if m:
        return float(m.group(1)) / LB_TO_KG
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def filter_by_load(done: pd.DataFrame, required: float) -> pd.DataFrame:
    """加重目標のとき、その加重以上で行った記録だけに絞る。
    懸垂『加重25lb × 合計32回』の目標に対し、自重42回を達成扱いにしないため。"""
    if not required:
        return done
    loads = done["実績ログ"].map(parse_added_load)
    return done[loads >= required]


def build_best_lookup(done: pd.DataFrame):
    """目標の種目名から自己ベストを引く関数を作る。
    目標行は分割まで書き分けられることがある（例『ショルダープレス(Push A)』は
    ダンベル、『(Push B)』はマシンで別物）。実績側の種目名は『ショルダープレス』
    のみで分割列で区別されるため、名前だけの突き合わせだと一致せず、
    その種目がグラフから丸ごと消えてしまう。末尾の (分割) を解釈して救う。"""
    if done.empty:
        return lambda name: None
    by_name = done.groupby("種目")["実績重量"].max()
    by_pair = done.groupby(["種目", "分割"])["実績重量"].max()

    def lookup(name):
        v = by_name.get(name)
        if pd.notna(v):
            return float(v)
        m = re.match(r"^(.*)\((.+)\)$", str(name or "").strip())
        if m and m.group(2) in SPLITS:
            v = by_pair.get((m.group(1).strip(), m.group(2)))
            if pd.notna(v):
                return float(v)
        return None

    return lookup


def is_reps_goal(goal_weight) -> bool:
    """回数で記録する種目か（懸垂・自重・サーキット）。
    『加重10lb×8+自重10』のように加重の lb が混ざっていても回数種目なので、
    lb/kg の有無より先に 自重/周/回 を見る（順序を逆にすると加重種目を
    重量とみなして kg 換算してしまう）。"""
    s = str(goal_weight or "")
    if not s.strip():
        return False
    if "自重" in s or "周" in s:
        return True
    return "回" in s and not re.search(r"^\s*\d+(?:\.\d+)?\s*(lb|kg)", s, re.IGNORECASE)


def parse_added_load(text) -> float:
    """加重（自重に足したおもり）の lb を取り出す。無ければ 0。
    目標側の『加重10lb × 3セット合計45回』と、実績ログ側の『+10lb 12,10,10回』の
    両方に対応する。懸垂やハンギングレッグレイズは実績重量に合計回数しか入らないため、
    加重したかどうかはこの値でしか判別できない。"""
    s = str(text or "")
    m = re.search(r"(?:加重|\+)\s*(\d+(?:\.\d+)?)\s*(lb|kg)", s, re.IGNORECASE)
    if not m:
        return 0.0
    v = float(m.group(1))
    return v / LB_TO_KG if m.group(2).lower() == "kg" else v


def is_lb_unit(goal_weight) -> bool:
    """記録が lb（重さ）かどうか。kg 併記してよい種目だけ True。
    懸垂の『4セット合計42回』『加重10lb×8+自重10』のような回数の記録を
    kg に誤換算しないための判定。"""
    s = str(goal_weight or "")
    if is_reps_goal(s):
        return False
    if not is_weight_goal(s):
        return False
    if re.search(r"(lb|kg)", s, re.IGNORECASE):
        return True
    return "回" not in s and "周" not in s


def is_weight_goal(goal_weight) -> bool:
    """目標重量が『重量（lb/kg）の目標』かどうか。現在地グラフの対象を絞るのに使う。
    '25lb'/'加重5kg…合計30回'→True、'4周'/'自重20回'（純自重・サーキット）→False。"""
    s = str(goal_weight or "")
    if not s.strip():
        return False
    if "周" in s:  # サーキット系（例: '4周'）は重量比較の対象外
        return False
    has_unit = bool(re.search(r"(lb|kg)", s, re.IGNORECASE))
    if "自重" in s and not has_unit:  # 加重もない純粋な自重は重量目標でない
        return False
    return parse_target_weight(s) is not None


def ensure_bodyweight_column(token: str) -> bool:
    """DB に number 型の「体重」列を用意する（無ければ作る）。一度だけ試行。
    作成/既存なら True、権限不足等で失敗したら False（体重機能は静かに無効化）。"""
    if "_has_bw_col" in st.session_state:
        return st.session_state["_has_bw_col"]
    ok = False
    try:
        r = requests.patch(
            f"{API}/databases/{DATABASE_ID}", headers=_headers(token),
            json={"properties": {"体重": {"number": {}}}}, timeout=30,
        )
        ok = r.status_code == 200
    except Exception:
        ok = False
    st.session_state["_has_bw_col"] = ok
    return ok


def _apply_master(ex_id: str) -> None:
    """マスター重量を、その種目の全セットの重量ボックスに反映する（on_change用）。"""
    mv = st.session_state.get(f"m_{ex_id}")
    if mv is None:
        return  # マスター未入力。ここで書き戻すと入力済みの重量が全部消える
    n = int(st.session_state.get(f"n_{ex_id}", 0) or 0)
    for s in range(n):
        st.session_state[f"w_{ex_id}_{s}"] = mv


def collect_sets(ex_id: str, bw: bool, n: int):
    """session_state から (重量, 回数) のセット一覧を作る。空セットは除く。"""
    sets = []
    for s in range(n):
        wv = 0.0 if bw else float(st.session_state.get(f"w_{ex_id}_{s}") or 0.0)
        rv = int(st.session_state.get(f"r_{ex_id}_{s}") or 0)
        if wv > 0 or rv > 0:
            sets.append((wv, rv))
    return sets


def build_log(sets, bw: bool, added=0.0):
    """セット一覧から (実績ログ文字列, 実績重量) を作る。
    自重種目は回数だけ記録し、実績重量＝合計回数（懸垂と同じ規約、推移グラフ用）。
    加重した場合はログ先頭に『+10lb』を残す。実績重量は合計回数のままなので、
    自重42回と加重42回を区別できるのはこの表記だけ。"""
    if not sets:
        return None, None
    weights = [wv for wv, _ in sets]
    if bw:
        reps = [rv for _, rv in sets]
        body = ",".join(str(r) for r in reps) + "回"
        prefix = f"+{added:g}lb " if added else ""
        return prefix + body, float(sum(reps))
    if len(set(weights)) <= 1:
        wv = weights[0]
        reps_str = ",".join(str(rv) for _, rv in sets)
        return (f"{wv:g}×{reps_str}" if wv > 0 else reps_str), (wv if wv > 0 else None)
    # 実績重量は「全セットこなした重量」。1セットだけ上げた重量は最大に数えない
    # （2セット以上こなした最大重量。無ければ最も軽い＝確実にこなした重量）。
    sustained = [w for w in set(weights) if weights.count(w) >= 2]
    top = max(sustained) if sustained else min(weights)
    return ", ".join(f"{wv:g}×{rv}" for wv, rv in sets), top


def serialize_draft() -> str:
    """入力中の値（週/分割/実施日/体重/各セット）を JSON 文字列にする（端末への下書き用）。"""
    d = {}
    for k, v in st.session_state.items():
        if k in ("week_sel", "split_sel", "bw_today") or k.startswith(DRAFT_PREFIXES):
            d[k] = v
    rd = st.session_state.get("rec_date")
    if isinstance(rd, dt.date):
        d["rec_date"] = rd.isoformat()
    return json.dumps(d, default=str)


def clear_input_state() -> None:
    """入力欄（重量/回数/マスター/セット数/達成/体重）の session_state を消す。"""
    for k in list(st.session_state.keys()):
        if k.startswith(DRAFT_PREFIXES) or k.startswith("n_") or k == "bw_today":
            del st.session_state[k]


MAX_SETS = 12  # セット数入力の上限。推定値もここに収める


def parse_set_count(goal: str, default: int = 3) -> int:
    """目標文字列からセット数を推定。例: '4×6-10'→4, '3×10'→3, '4セット'→4, '3周'→3。
    『15×3』のように回数×セットで書かれても入力欄の上限を超えないよう 1〜MAX_SETS に収める
    （超えると number_input が例外を投げ、その週の入力画面が丸ごと開けなくなる）。"""
    if not goal:
        return default
    m = re.match(r"\s*(\d+)\s*[×xX✕*]", goal)
    if m:
        return min(max(int(m.group(1)), 1), MAX_SETS)
    m = re.search(r"(\d+)\s*(セット|周|set)", goal, re.IGNORECASE)
    if m:
        return min(max(int(m.group(1)), 1), MAX_SETS)
    return default


GROWTH_CSS = """
<style>
.gr-list { display:flex; flex-direction:column; gap:.45rem; margin:.2rem 0 .6rem; }
.gr-row {
  display:grid; grid-template-columns: minmax(7rem,1.4fr) auto minmax(4.5rem,.9fr);
  align-items:center; gap:.5rem .7rem;
  background:rgba(128,140,160,.07); border:1px solid rgba(128,140,160,.18);
  border-left:3px solid #2ca02c; border-radius:9px; padding:.5rem .7rem;
}
.gr-row.flat { border-left-color:rgba(128,140,160,.45); }
.gr-name { font-size:.86rem; font-weight:600; line-height:1.25; overflow-wrap:anywhere; }
.gr-split { font-size:.66rem; font-weight:500; opacity:.65; margin-left:.3rem; white-space:nowrap; }
.gr-nums { display:flex; align-items:baseline; gap:.3rem; white-space:nowrap;
  font-variant-numeric:tabular-nums; }
.gr-from { font-size:.95rem; opacity:.5; }
.gr-arw  { font-size:.8rem; opacity:.45; }
.gr-best { font-size:1.45rem; font-weight:800; color:#2ca02c; letter-spacing:-.02em; }
.gr-row.flat .gr-best { color:inherit; opacity:.7; }
.gr-unit { font-size:.72rem; opacity:.6; }
.gr-right { text-align:right; }
.gr-delta { font-size:.82rem; font-weight:700; color:#2ca02c; white-space:nowrap; }
.gr-row.flat .gr-delta { color:inherit; opacity:.55; }
.gr-sub { font-size:.65rem; opacity:.55; white-space:nowrap; }
.gr-bar { grid-column:1 / -1; height:.38rem; border-radius:99px;
  background:rgba(128,140,160,.25); overflow:hidden; display:flex; }
.gr-bar i { display:block; height:100%; }
.gr-bar .base { background:rgba(128,140,160,.55); }
.gr-bar .gain { background:#2ca02c; }
@media (max-width:520px) {
  .gr-row { grid-template-columns: 1fr auto; }
  .gr-right { grid-column:2; }
}
</style>
"""


def growth_rows_html(rows, unit: str) -> str:
    """初回→ベストを1行ずつのカードで描く。棒は『ベストのうち、この15週で伸ばした分』
    ＝灰色が初回時点、緑が増加分。全行同じ幅なので伸びた割合を横並びで比べられる。"""
    out = ['<div class="gr-list">']
    for r in rows:
        gained = r["ベスト"] > r["初"]
        base = (r["初"] / r["ベスト"] * 100) if r["ベスト"] else 100
        name = html.escape(str(r["種目"]))
        split = html.escape(str(r["分割"] or ""))
        out.append(
            f'<div class="gr-row{"" if gained else " flat"}">'
            f'<div class="gr-name">{name}<span class="gr-split">{split}</span></div>'
            f'<div class="gr-nums"><span class="gr-from">{r["初"]:g}</span>'
            f'<span class="gr-arw">→</span>'
            f'<span class="gr-best">{r["ベスト"]:g}</span>'
            f'<span class="gr-unit">{unit}</span></div>'
            f'<div class="gr-right"><div class="gr-delta">'
            f'{"+" if r["増減"] > 0 else ""}{r["増減"]:g} {unit}</div>'
            f'<div class="gr-sub">{r["率"]:+.0f}% · W{r["初週"]}→W{r["ベスト週"]}</div></div>'
            f'<div class="gr-bar"><i class="base" style="width:{base:.1f}%"></i>'
            f'<i class="gain" style="width:{100 - base:.1f}%"></i></div>'
            f'</div>'
        )
    out.append("</div>")
    return "".join(out)


def reps_exercise_names(df: pd.DataFrame) -> set:
    """回数で記録する種目の名前一覧。週によって目標の書き方が揺れる（ブルガリアン
    スクワットが一度だけ『自重or 5lb』だった等）ので、多数決で決める。
    1週でも該当すれば回数種目、とすると重量種目が誤って除外される。"""
    votes = {}
    for ex, gw in zip(df["種目"], df["目標重量"]):
        if gw:
            votes.setdefault(ex, []).append(is_reps_goal(gw))
    return {ex for ex, v in votes.items() if sum(v) / len(v) > 0.5}


def growth_stats(df: pd.DataFrame, upto: int = 15):
    """W1〜upto の成長を集計する。返り値は (重量種目, 回数種目, 全体サマリー)。
    種目名が同じでも分割が違えば別マシン（ショルダープレスは Push A がダンベル、
    Push B がマシン）なので、必ず 種目×分割 で集計する。"""
    d = df[(df["週num"].notna()) & (df["週num"] <= upto)].copy()
    reps_names = reps_exercise_names(df)
    per = {}
    for _, r in d.dropna(subset=["実績重量"]).iterrows():
        key = (r["種目"], r["分割"])
        wk = int(r["週num"])
        per.setdefault(key, {})[wk] = max(per.get(key, {}).get(wk, 0), r["実績重量"])
    weight_rows, reps_rows = [], []
    for (ex, sp), wm in per.items():
        ws = sorted(wm)
        if len(ws) < 2:
            continue
        first, best = wm[ws[0]], max(wm.values())
        item = {"種目": ex, "分割": sp, "初週": ws[0], "初": first, "ベスト": best,
                "ベスト週": max(wm, key=wm.get), "増減": best - first,
                "率": (best / first * 100 - 100) if first else 0.0, "週数": len(ws)}
        (reps_rows if ex in reps_names else weight_rows).append(item)
    weight_rows.sort(key=lambda r: -r["増減"])
    reps_rows.sort(key=lambda r: -r["増減"])
    graded = d["達成"].notna().sum()
    summary = {
        "記録": int(d["実績重量"].notna().sum()),
        "週数": int(d["週num"].nunique()),
        "種目数": len(weight_rows) + len(reps_rows),
        "合計増加": sum(r["増減"] for r in weight_rows),
        "伸びた": sum(1 for r in weight_rows if r["増減"] > 0),
        "達成率": (d["達成"] == "✅達成").sum() / graded * 100 if graded else None,
    }
    return weight_rows, reps_rows, summary


def build_summary(df: pd.DataFrame, week: int) -> str:
    """指定週のまとめを Claude に投げる用の Markdown にする。末尾に来週用テンプレ付き。"""
    wk = df[df["週num"] == week].copy()
    lines = [f"# 📋 W{week} トレーニングまとめ", ""]

    # 達成サマリー
    graded = wk["達成"].notna().sum()
    cnt = {o: int((wk["達成"] == o).sum()) for o in ACHIEVE_OPTIONS}
    achieved = cnt["✅達成"]
    rate = f"{achieved / graded * 100:.0f}%" if graded else "—"
    lines.append("## 達成サマリー")
    lines.append(f"- 種目数: {len(wk)} / 実績入力済み: {wk['実績重量'].notna().sum()}")
    lines.append(f"- 達成内訳: ✅{cnt['✅達成']} △{cnt['△一部']} ❌{cnt['❌未達']} －{cnt['－スキップ']}")
    lines.append(f"- 達成率: {rate}")
    lines.append("")

    # メニューと実績（分割ごと）
    lines.append("## メニューと実績")
    splits_here = [s for s in SPLITS if s in set(wk["分割"])] or sorted(set(wk["分割"].dropna()))
    for sp in splits_here:
        sub = wk[wk["分割"] == sp].sort_values(["順番", "created"], na_position="last")
        if sub.empty:
            continue
        # 日付があれば添える
        d = sub["日付"].dropna()
        day = f"（{d.iloc[0].strftime('%-m/%-d')}）" if not d.empty else ""
        lines.append(f"### {sp}{day}")
        for _, r in sub.iterrows():
            goal = r["目標"] or "—"
            # 実績：ログ優先、なければ重量
            if r["実績ログ"]:
                actual = r["実績ログ"]
            elif pd.notna(r["実績重量"]):
                actual = f"{r['実績重量']:g}"
            else:
                actual = "（未実施）"
            ach = r["達成"] or ""
            lines.append(f"- {r['種目']}｜目標 {goal}｜実績 {actual}｜{ach}".rstrip("｜ "))
        lines.append("")

    # 来週メニューのテンプレ（Claudeへの指示を同梱）
    lines.append("---")
    lines.append(f"※上を踏まえてフィードバックと来週(W{week + 1})メニューをお願いします。")
    lines.append("来週メニューは下の形式で、1種目1行・コードブロックで返してください（そのままアプリに貼り込みます）:")
    lines.append("```")
    lines.append(MENU_FORMAT)
    lines.append("Push A | インクラインダンベルプレス | 4×6-10 | 22.5lb(+0) | 胸,肩")
    lines.append("```")
    return "\n".join(lines)


def parse_menu(text: str):
    """貼り付けた来週メニューをパースして dict のリストにする。"""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("```"):
            continue
        if line.startswith("分割") and "種目" in line:  # ヘッダ行を無視
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        body = []
        if len(parts) > 4 and parts[4]:
            body = [p.strip() for p in parts[4].replace("、", ",").split(",") if p.strip()]
            body = [p for p in body if p in PARTS_OPTIONS]
        out.append({
            "分割": parts[0],
            "種目": parts[1],
            "目標": parts[2] if len(parts) > 2 else "",
            "目標重量": parts[3] if len(parts) > 3 else "",
            "部位": body,
        })
    return out


# --- 起動 ----------------------------------------------------------------
st.set_page_config(page_title="トレーニング", page_icon="💪", layout="wide")

# スマホでもセット入力の[重量|回数]を縦積みさせず横一行に保つ。
# Streamlit は狭い画面だと st.columns を自動で縦積みするため、
# セット行コンテナ(st-key-setrow…)内だけ nowrap を強制する。
st.markdown(
    """
    <style>
    /* 柔らかい丸ゴシックのWebフォントを読み込む（端末に無くても確実に適用される）。
       iPhoneには丸ゴシックが標準で入っていないため、フォント名指定だけだと
       角ゴにフォールバックして変化しない。@import で M PLUS Rounded 1c を取得。 */
    @import url('https://fonts.googleapis.com/css2?family=M+PLUS+Rounded+1c:wght@400;500;700&display=swap');
    html, body, [class*="css"], .stApp, [data-testid="stAppViewContainer"],
    button, input, textarea, select, .stMarkdown, [data-testid="stMarkdownContainer"] {
        font-family: "M PLUS Rounded 1c", "Hiragino Maru Gothic ProN",
            "Hiragino Sans", "Yu Gothic", "Noto Sans JP",
            system-ui, -apple-system, sans-serif !important;
    }
    /* 文字の行間を少しだけ詰める */
    .stApp, .stMarkdown, [data-testid="stMarkdownContainer"] {
        line-height: 1.45;
    }
    /* 要素どうしの縦の余白も少しだけ詰める */
    [data-testid="stVerticalBlock"] {
        gap: 0.6rem;
    }

    /* セット入力の[重量|回数]をスマホでも横一行に保つ */
    div[class*="st-key-setrow"] div[data-testid="stHorizontalBlock"] {
        flex-wrap: nowrap;
        gap: 0.4rem;
    }
    div[class*="st-key-setrow"] div[data-testid="stColumn"] {
        min-width: 0 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("💪 トレーニング")

if not check_password():
    st.stop()

token = get_token()
if not token:
    st.error(
        "Notion トークンが未設定です。`.streamlit/secrets.toml` に "
        "`NOTION_TOKEN = \"...\"` を書くか、環境変数 NOTION_TOKEN を設定してください。"
    )
    st.stop()

has_bw_col = ensure_bodyweight_column(token)

if st.button("🔄 最新に更新"):
    st.cache_data.clear()

try:
    df = fetch_records(token)
except requests.HTTPError as e:
    st.error(f"取得に失敗しました: {e}\nトークンと、DB をインテグレーションに共有しているか確認してください。")
    st.stop()
except requests.exceptions.RequestException as e:
    # 接続エラー/タイムアウト等。アプリを落とさず、再試行を促す。
    st.warning(
        "Notion への接続に一時的に失敗しました（ネットワークの瞬断など）。"
        "少し待ってから下のボタンで再試行してください。"
    )
    if st.button("🔄 再試行"):
        st.cache_data.clear()
        st.rerun()
    with st.expander("エラー詳細"):
        st.caption(str(e))
    st.stop()

if df.empty:
    st.warning("データが空です。Notion 側で DB をインテグレーションに共有していますか？")
    st.stop()

latest_week = int(df["週num"].max()) if df["週num"].notna().any() else None

# タブではなく選択式にする。st.tabs は再実行(「最新に更新」等)のたびに先頭へ戻るが、
# segmented_control は key で選択が session_state に残るので、更新しても同じ画面のまま。
VIEWS = ["✍️ 今日の記録", "📊 グラフ", "📤 まとめ＆計画", "🏅 W1–W15"]
# segmented_control は選択中の項目をもう一度タップすると選択解除されて None を返す。
# その場合は直前の画面を保つ（そうしないと誤タップで「今日の記録」に飛ばされる）。
if "view_last" not in st.session_state:
    st.session_state["view_last"] = VIEWS[0]
if st.session_state.get("view_sel") not in VIEWS:
    st.session_state["view_sel"] = st.session_state["view_last"]
picked = st.segmented_control("表示", VIEWS, key="view_sel", label_visibility="collapsed")
view = picked if picked in VIEWS else st.session_state["view_last"]
st.session_state["view_last"] = view

# =====================================================================
# ✍️ 今日の記録 — 今週の種目に実績を埋める
# =====================================================================
if view == VIEWS[0]:
    st.subheader("今日の記録を入力")

    ls = LocalStorage()

    # --- 下書き復元：セッション開始時に1回だけ、端末に残った入力を画面へ戻す ---
    # LocalStorage() は初回に全アイテムを同期ロードするので、最初の run で getItem が
    # 確定値を返す。ここで必ず _draft_restored を立て、以降の run では復元しない
    # （以降も復元すると、このセッションが保存した下書きを読み戻して入力を上書きしてしまう）。
    if not st.session_state.get("_draft_restored"):
        raw = ls.getItem(DRAFT_KEY)  # 端末に下書きがあれば JSON 文字列、無ければ None
        st.session_state["_draft_restored"] = True
        if raw:
            try:
                draft = json.loads(raw)
            except Exception:
                draft = None
            if isinstance(draft, dict) and draft:
                for k, v in draft.items():
                    if k.startswith("n_"):
                        continue  # 旧バージョンが保存したセット数は無視（目標から再推定する）
                    if k == "rec_date" and v:
                        try:
                            st.session_state["rec_date"] = dt.date.fromisoformat(v)
                        except Exception:
                            pass
                    else:
                        st.session_state[k] = v
                st.toast("前回の未保存の入力を復元しました", icon="↩️")
                st.rerun()

    weeks = sorted([int(w) for w in df["週num"].dropna().unique()])
    c1, c2, c3 = st.columns([1, 1, 1])
    # 下書きから復元した週が今の一覧に無い（Notion側で消した等）と selectbox が
    # 例外を投げて入力画面が開けなくなるため、範囲外なら最新週に戻す。
    if weeks and st.session_state.get("week_sel") not in weeks:
        st.session_state["week_sel"] = weeks[-1]
    if "week_sel" not in st.session_state and weeks:
        st.session_state["week_sel"] = weeks[-1]
    week_sel = c1.selectbox("週", weeks, format_func=lambda w: f"W{w}", key="week_sel")

    splits_here = [s for s in SPLITS if s in set(df[df["週num"] == week_sel]["分割"])]
    split_opts = splits_here or SPLITS
    if st.session_state.get("split_sel") not in split_opts:
        st.session_state["split_sel"] = split_opts[0]
    split_sel = c2.selectbox("分割（今日のメニュー）", split_opts, key="split_sel")

    if "rec_date" not in st.session_state:
        st.session_state["rec_date"] = dt.date.today()
    rec_date = c3.date_input("実施日", key="rec_date")

    if has_bw_col:
        bw_col, _ = st.columns([1, 2])
        if "bw_today" not in st.session_state:
            st.session_state["bw_today"] = None
        bw_col.number_input(
            "体重 (kg)", min_value=0.0, step=0.1, key="bw_today",
            help="その日の体重。保存するとこの日の記録に紐づき、グラフの体重推移に出ます。",
        )

    target = df[(df["週num"] == week_sel) & (df["分割"] == split_sel)].copy()
    # 「順番」列があればそれ優先、無ければ追加順（created）で並べる
    target = target.sort_values(["順番", "created"], na_position="last").reset_index(drop=True)

    if target.empty:
        st.info("この週・分割の行がありません。「📤 まとめ＆計画」から来週メニューを取り込めます。")
    else:
        st.caption(
            f"W{week_sel} / {split_sel} … {len(target)} 種目。"
            "マスター重量を入れると全セットに一括反映。変えたいセットだけ後から個別に修正。"
        )
        for _, row in target.iterrows():
            ex_id = row["page_id"]
            bw = is_bodyweight(row)  # 自重種目なら重量欄を出さない
            goal_load = parse_added_load(row["目標重量"])  # 加重目標(懸垂等)なら > 0
            n_default = parse_set_count(row["目標"])
            # 初期値を session_state に入れておく（value= と key= の二重指定警告を避ける）
            # 重量・回数は None で初期化＝最初は空欄（0をいちいち消さなくてよい）。
            # 下書き復元で既に値が入っていれば、その値が優先される（not in で上書きしない）。
            if f"n_{ex_id}" not in st.session_state:
                st.session_state[f"n_{ex_id}"] = n_default
            if f"a_{ex_id}" not in st.session_state:
                st.session_state[f"a_{ex_id}"] = row["達成"] if row["達成"] in ACHIEVE_OPTIONS else "（未入力）"
            if not bw and f"m_{ex_id}" not in st.session_state:
                st.session_state[f"m_{ex_id}"] = None

            done_mark = "✅" if pd.notna(row["実績重量"]) else "・"
            tag = "（自重）" if bw else f"目標重量 {row['目標重量'] or '—'}"
            st.markdown(f"**{done_mark} {row['種目']}**　🎯{row['目標'] or '—'}　/　{tag}")

            if bw:
                # 加重できる自重種目（目標に『加重』とある懸垂等）は加重欄も出す。
                # これが無いと自重42回と加重42回が同じ記録になり、加重目標が
                # 自重の記録だけで達成扱いになってしまう。
                if goal_load > 0:
                    gc1, mc2, mc3 = st.columns([1.2, 1, 1.2])
                    if f"g_{ex_id}" not in st.session_state:
                        st.session_state[f"g_{ex_id}"] = None
                    gc1.number_input(
                        "加重 (lb)", min_value=0.0, step=2.5, key=f"g_{ex_id}",
                        help=f"目標は加重{goal_load:g}lb。自重のみなら空欄か0。",
                    )
                else:
                    mc2, mc3 = st.columns([1, 1.2])
            else:
                mc1, mc2, mc3 = st.columns([1.2, 1, 1.2])
                mc1.number_input(
                    "マスター重量", min_value=0.0, step=2.5, key=f"m_{ex_id}",
                    on_change=_apply_master, args=(ex_id,),
                    help="入れると下の全セットに一括反映。個別に変えたいセットだけ後で修正。",
                )
            mc2.number_input(
                "セット数", min_value=1, max_value=12, step=1, key=f"n_{ex_id}",
                on_change=_apply_master, args=(ex_id,),
            )
            mc3.selectbox("達成", ["（未入力）"] + ACHIEVE_OPTIONS, key=f"a_{ex_id}")

            n = int(st.session_state.get(f"n_{ex_id}", n_default) or n_default)
            for s in range(n):
                rkey, wkey = f"r_{ex_id}_{s}", f"w_{ex_id}_{s}"
                if rkey not in st.session_state:
                    st.session_state[rkey] = None
                if not bw and wkey not in st.session_state:
                    st.session_state[wkey] = None
                with st.container(key=f"setrow_{ex_id}_{s}"):
                    if bw:
                        st.number_input(f"回数 set{s + 1}", min_value=0, step=1, key=rkey)
                    else:
                        sc1, sc2 = st.columns(2)
                        sc1.number_input(f"重量 set{s + 1}", min_value=0.0, step=2.5, key=wkey)
                        sc2.number_input(f"回数 set{s + 1}", min_value=0, step=1, key=rkey)

            if row["実績ログ"]:
                st.caption(f"既存ログ: {row['実績ログ']}")
            st.divider()

        bw_today = st.session_state.get("bw_today") if has_bw_col else None

        # --- 下書きを端末に保存：入力が変わるたびに localStorage へ（Notionには書かない）---
        # サーバーが切れても、再ログイン時にこの下書きから画面を復元できる。
        draft_json = serialize_draft()
        if draft_json != st.session_state.get("_draft_last"):
            ls.setItem(DRAFT_KEY, draft_json, key="tl_set_draft")
            st.session_state["_draft_last"] = draft_json

        st.caption(
            "📝 入力は自動でこの端末に下書き保存され、サーバーが切れても再ログインで復元されます。"
            "Notion に記録するには下の「保存」を押してください（押すまで記録はされません）。"
        )

        b1, b2 = st.columns([2, 1])
        if b1.button("💾 保存（Notionに記録）", type="primary", use_container_width=True):
            saved, errors = 0, []
            for _, row in target.iterrows():
                ex_id = row["page_id"]
                bw = is_bodyweight(row)
                n = int(st.session_state.get(f"n_{ex_id}", 0) or 0)
                ach = st.session_state.get(f"a_{ex_id}", "（未入力）")
                sets = collect_sets(ex_id, bw, n)
                # 「達成」欄はNotionの既存値で初期化されるため、何も入力していない行でも
                # ach が埋まっている。それを保存対象にすると日付だけ今日で上書きされ、
                # 過去の実施日が静かに壊れるので、実際に変化がある行だけ保存する。
                unchanged = (ach == "（未入力）") or (ach == row["達成"])
                if not sets and unchanged:
                    continue
                log, top_weight = build_log(
                    sets, bw, added=float(st.session_state.get(f"g_{ex_id}") or 0.0))
                try:
                    update_record(
                        token, ex_id, weight=top_weight, log=log,
                        achieve=None if ach == "（未入力）" else ach,
                        date=rec_date, bodyweight=bw_today,
                    )
                    saved += 1
                except requests.HTTPError as e:
                    errors.append(str(e))
            # 体重だけ入れて種目を保存しなかった場合も、その日の1行に体重を残す
            if bw_today is not None and saved == 0 and not errors and not target.empty:
                try:
                    update_record(token, target.iloc[0]["page_id"],
                                  date=rec_date, bodyweight=bw_today)
                    saved += 1
                except requests.HTTPError as e:
                    errors.append(str(e))
            st.cache_data.clear()
            if saved and not errors:
                # 保存できたら端末の下書きも消す。残すと次回「未保存の入力を復元しました」
                # と嘘のトーストが出て、保存済みの値がまた画面に載る。
                clear_input_state()
                ls.deleteItem(DRAFT_KEY, key="tl_del_saved")
                st.session_state["_draft_last"] = None
            if saved:
                st.success(f"{saved} 件保存しました。")
            if errors:
                st.error("一部失敗: " + " / ".join(errors[:3]))
            if not saved and not errors:
                st.info("入力がありませんでした。")

        if b2.button("🗑 入力をクリア", use_container_width=True,
                     help="画面の入力と端末の下書きを消します（Notionの記録は消えません）。"):
            clear_input_state()
            ls.deleteItem(DRAFT_KEY, key="tl_del_draft")
            st.session_state["_draft_last"] = None
            st.rerun()

# =====================================================================
# 📊 グラフ
# =====================================================================
if view == VIEWS[1]:
    done = df.dropna(subset=["実績重量"]).copy()

    # 目標(3ヶ月後/今週)に対する達成率：各種目のベスト÷目標の平均（100%上限）。
    _grows = df[df["週"] == GOAL_WEEK]
    if not _grows.empty:
        _gsrc = _grows[["種目", "目標重量"]].drop_duplicates("種目", keep="last")
    elif latest_week is not None:
        _gsrc = df[df["週num"] == latest_week][["種目", "目標重量"]].drop_duplicates("種目", keep="last")
    else:
        _gsrc = pd.DataFrame(columns=["種目", "目標重量"])
    _lookup = build_best_lookup(done)
    _ratios, _reached = [], 0
    for _, _r in _gsrc.iterrows():
        if not is_weight_goal(_r["目標重量"]):
            continue
        _t = parse_target_weight(_r["目標重量"])
        _load = parse_added_load(_r["目標重量"])
        # 加重目標なら、その加重で行った記録だけを対象にする
        _b = (build_best_lookup(filter_by_load(done, _load))(_r["種目"])
              if _load else _lookup(_r["種目"]))
        if _b is None:
            _ratios.append(0.0)  # 加重目標に未挑戦。達成率0%として数える
            continue
        _ratio = float(_b) / _t
        _ratios.append(min(_ratio, 1.0))
        if _ratio >= 1:
            _reached += 1
    goal_rate = sum(_ratios) / len(_ratios) * 100 if _ratios else None

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("記録セット数", f"{len(done)}")
    c2.metric("種目数", f"{done['種目'].nunique()}")
    c3.metric("記録週数", f"{int(done['週num'].nunique())}" if done['週num'].notna().any() else "0")
    c4.metric(
        "目標達成率", f"{goal_rate:.0f}%" if goal_rate is not None else "—",
        help=(f"各種目のベスト÷目標の平均（100%上限）。目標到達 {_reached}/{len(_ratios)} 種目。"
              if _ratios else "目標重量が数値の種目がまだありません。"),
    )

    st.divider()

    # --- 🎯 目標重量に対する現在地 ---------------------------------------
    # 各種目の目標重量に対し、自己ベストがどこまで来ているかを横棒で可視化する。
    # 100%（＝目標）に赤の基準線を引き、達成/未達を色分け。
    # 目標の出どころ：最終目標(週='目標')があればそれ、無ければ今週メニューにフォールバック。
    st.subheader("🎯 目標に対する現在地")
    goal_rows = df[df["週"] == GOAL_WEEK]
    if not goal_rows.empty:
        goal_src = goal_rows[["種目", "目標重量"]].drop_duplicates("種目", keep="last")
        src_note = "最終目標（3ヶ月後）"
    elif latest_week is not None:
        goal_src = (df[df["週num"] == latest_week][["種目", "目標重量"]]
                    .drop_duplicates("種目", keep="last"))
        src_note = (f"今週(W{latest_week})の目標"
                    " ※3ヶ月後の最終目標は「📤 まとめ＆計画」タブで設定できます")
    else:
        goal_src, src_note = pd.DataFrame(columns=["種目", "目標重量"]), ""

    rows = []
    if not done.empty and not goal_src.empty:
        lookup_best = build_best_lookup(done)  # 目標名の (分割) も解釈して引く
        # 種目ごとの直近の実績（週→日付の順で最後の記録＝今の調子）
        lat = done.sort_values(["週num", "日付"])
        latest_by_name = lat.groupby("種目")["実績重量"].last()
        latest_by_pair = lat.groupby(["種目", "分割"])["実績重量"].last()

        def lookup_latest(name):
            v = latest_by_name.get(name)
            if pd.notna(v):
                return float(v)
            m = re.match(r"^(.*)\((.+)\)$", str(name or "").strip())
            if m and m.group(2) in SPLITS:
                v = latest_by_pair.get((m.group(1).strip(), m.group(2)))
                if pd.notna(v):
                    return float(v)
            return None

        for _, r in goal_src.iterrows():
            if not is_weight_goal(r["目標重量"]):
                continue  # 自重・サーキット系（周/自重）は重量の現在地に出さない
            target = parse_target_weight(r["目標重量"])
            ex = r["種目"]
            load = parse_added_load(r["目標重量"])
            if load:
                # 加重目標（懸垂『加重25lb × 合計32回』等）は、その加重で行った
                # 記録だけを見る。自重の記録で達成扱いにしない。
                sub = filter_by_load(done, load)
                bv = build_best_lookup(sub)(ex)
                lv = build_best_lookup(sub.sort_values(["週num", "日付"]).groupby(
                    ["種目", "分割"], as_index=False).tail(1))(ex) if bv is not None else None
                if bv is None:
                    rows.append({"種目": f"{ex}（加重{load:g}lb）", "目標": target,
                                 "lb": False, "ベスト": 0.0, "ベスト率": 0.0,
                                 "直近": None, "直近率": None, "未挑戦": True})
                    continue
                ex = f"{ex}（加重{load:g}lb）"
            else:
                bv = lookup_best(ex)
                if bv is None:
                    continue  # 実績がまだ無い種目は位置を出せないので非表示
                lv = lookup_latest(ex)
            rows.append({
                "種目": ex, "目標": target, "lb": is_lb_unit(r["目標重量"]),
                "ベスト": bv, "ベスト率": bv / target * 100,
                "直近": lv, "直近率": (lv / target * 100) if lv is not None else None,
                "未挑戦": False,
            })

    if rows:
        # 達成率の低い順（伸びしろ順）に並べる。上ほど目標に近い。
        gdf = pd.DataFrame(rows).sort_values("ベスト率").reset_index(drop=True)
        colors = ["#c9ccd1" if u else ("#2ca02c" if p >= 100 else "#4c78d8")
                  for p, u in zip(gdf["ベスト率"], gdf["未挑戦"])]
        labels = [("その加重では未記録" if u else f"{b:g} / {t:g}（{p:.0f}%）")
                  for b, t, p, u in zip(gdf["ベスト"], gdf["目標"], gdf["ベスト率"], gdf["未挑戦"])]
        # ホバー用に 直近 と kg 換算を渡す（棒はベスト基準、直近はタップで確認）
        cd = [[f"{b:g}", (f"{lv:g}" if lv is not None else "—"), f"{t:g}",
               (f"ベスト {b * LB_TO_KG:.1f}kg / 目標 {t * LB_TO_KG:.1f}kg" if islb
                else "回数で記録する種目（kg換算なし）")]
              for b, lv, t, islb in zip(gdf["ベスト"], gdf["直近"], gdf["目標"], gdf["lb"])]
        figg = go.Figure(go.Bar(
            y=gdf["種目"], x=gdf["ベスト率"], orientation="h",
            marker_color=colors, text=labels, textposition="outside",
            cliponaxis=False, customdata=cd,
            hovertemplate=("ベスト %{customdata[0]} / 直近 %{customdata[1]} / "
                           "目標 %{customdata[2]}（%{x:.0f}%）<br>"
                           "%{customdata[3]}<extra>%{y}</extra>"),
        ))
        figg.add_vline(x=100, line_dash="dash", line_color="#d62728",
                       annotation_text="目標", annotation_position="top")
        figg.update_layout(
            height=max(240, 46 * len(gdf) + 90),
            xaxis_title="目標達成率 (%)",
            xaxis_range=[0, max(115, gdf["ベスト率"].max() * 1.12)],
            yaxis=dict(automargin=True),
            margin=dict(l=6, r=40, t=30, b=10),
        )
        st.plotly_chart(figg, use_container_width=True)
        st.caption(
            f"{src_note}に対して、各種目の自己ベストが目標重量の何%まで来ているか。"
            "赤い点線（100%）が目標。緑＝達成／青＝途中。棒をタップすると直近の記録も出ます。"
            "自重・サーキット種目は重量比較に向かないため除外しています。"
        )
    else:
        st.info("目標重量が数値の実績がまだありません。"
                "「📤 まとめ＆計画」タブで最終目標を設定するか、今日の記録を入力してください。")

    st.divider()

    st.subheader("📈 重量の推移")
    if done.empty:
        st.info("まだ実績がありません。「今日の記録」から入力してください。")
    else:
        all_ex = sorted(done["種目"].unique())
        defaults = [e for e in KEY_LIFTS if e in all_ex] or all_ex[:3]
        left, right = st.columns([3, 1])
        with left:
            selected = st.multiselect("種目を選択", all_ex, default=defaults)
        with right:
            x_axis = st.radio("横軸", ["週", "日付"], horizontal=True)

        if selected:
            sub = done[done["種目"].isin(selected)].copy()
            xcol = "週num" if x_axis == "週" else "日付"
            sub = sub.sort_values(xcol)
            fig = px.line(
                sub, x=xcol, y="実績重量", color="種目", markers=True,
                labels={"週num": "週", "日付": "日付", "実績重量": "実績重量 / 回数"},
            )
            fig.update_layout(height=460, legend_title_text="種目", hovermode="x unified")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("種目を1つ以上選んでください。")

        col_pb, col_ac = st.columns(2)
        with col_pb:
            st.subheader("🏆 自己ベスト（種目別 最大）")
            pb = (
                done.groupby("種目")["実績重量"].max()
                .sort_values(ascending=False).reset_index()
            )
            pb.columns = ["種目", "最大実績重量"]
            # 記録は lb だが感覚を掴みやすいよう kg も併記（自重・回数系は換算しない）
            # 回数系の判定は多数決（取得順に依存する dict(zip(...)) だと
            # kg 列が出たり出なかったりする）
            reps_names = reps_exercise_names(df)
            goal_txt = {ex: gw for ex, gw in zip(df["種目"], df["目標重量"])
                        if gw and ex not in reps_names}
            pb["kg"] = [
                round(v * LB_TO_KG, 1) if is_lb_unit(goal_txt.get(ex, "")) else None
                for ex, v in zip(pb["種目"], pb["最大実績重量"])
            ]
            st.dataframe(pb, use_container_width=True, hide_index=True, height=360)
            st.caption(f"kg は lb×{LB_TO_KG} の換算。回数で記録する種目（懸垂・自重）は空欄。")

        with col_ac:
            st.subheader("🎯 達成状況の内訳")
            counts = df["達成"].value_counts().reindex(ACHIEVE_OPTIONS).dropna().reset_index()
            counts.columns = ["達成", "件数"]
            if not counts.empty:
                fig2 = px.pie(counts, names="達成", values="件数", hole=0.5)
                fig2.update_layout(height=360)
                st.plotly_chart(fig2, use_container_width=True)

    if "体重" in df.columns and df["体重"].notna().any():
        st.divider()
        st.subheader("⚖️ 体重の推移")
        bwdf = df.dropna(subset=["体重", "日付"]).copy()
        # 1日1値（同じ日に複数行に体重が付いていても代表値1つにまとめる）
        bwdf = bwdf.groupby("日付", as_index=False)["体重"].max().sort_values("日付")
        figw = px.line(bwdf, x="日付", y="体重", markers=True,
                       labels={"日付": "日付", "体重": "体重 (kg)"})
        figw.update_layout(height=320)
        st.plotly_chart(figw, use_container_width=True)

    st.divider()
    if latest_week is not None:
        st.subheader(f"📋 今週(W{latest_week})のメニュー")
        cols = ["分割", "種目", "目標", "目標重量", "実績重量", "達成", "メモ"]
        this_week = df[df["週num"] == latest_week][cols]
        st.dataframe(this_week, use_container_width=True, hide_index=True)

# =====================================================================
# 📤 まとめ＆計画 — Claudeとの週次ループ
# =====================================================================
if view == VIEWS[2]:
    # --- ① 今週のまとめを出力 ---
    st.subheader("① 今週のまとめを出力（Claudeに投げる）")
    weeks = sorted([int(w) for w in df["週num"].dropna().unique()])
    sum_week = st.selectbox(
        "まとめる週", weeks, index=len(weeks) - 1 if weeks else 0,
        format_func=lambda w: f"W{w}", key="sum_week",
    )
    summary = build_summary(df, sum_week)
    st.caption("右上のコピーアイコンで全文コピー → Claudeに貼り付け。末尾に来週メニューの返答形式も入っています。")
    st.code(summary, language="markdown")

    st.divider()

    # --- ② 来週メニューを取り込み ---
    st.subheader("② 来週メニューを取り込み（Claudeの返答を貼る）")
    next_week = (max(weeks) + 1) if weeks else 1
    nc1, nc2 = st.columns([1, 3])
    new_week = nc1.number_input("登録する週", min_value=1, step=1, value=next_week)
    st.caption(f"形式: `{MENU_FORMAT}` … 1種目1行。Claudeが返したコードブロックをそのまま貼ってOK。")
    pasted = st.text_area(
        "メニューを貼り付け", height=200,
        placeholder="Push A | インクラインダンベルプレス | 4×6-10 | 22.5lb(+0) | 胸,肩\nPush A | ショルダープレス | 3×8-12 | 15lb(維持) | 肩",
    )

    parsed = parse_menu(pasted) if pasted.strip() else []
    if parsed:
        st.write(f"**解析結果: {len(parsed)} 種目**（W{int(new_week)} として登録されます）")
        st.dataframe(pd.DataFrame(parsed), use_container_width=True, hide_index=True)
        # 既にその週が存在する場合は警告
        if int(new_week) in weeks:
            st.warning(f"W{int(new_week)} は既に存在します。取り込むと種目が**追加**されます（重複に注意）。")
        if st.button(f"➕ W{int(new_week)} として {len(parsed)} 種目を作成", type="primary"):
            made, errors = 0, []
            order_counter = {}  # 分割ごとに 1,2,3… と採番
            prog = st.progress(0.0)
            for i, m in enumerate(parsed):
                order_counter[m["分割"]] = order_counter.get(m["分割"], 0) + 1
                try:
                    create_record(
                        token, week=int(new_week), split=m["分割"], exercise=m["種目"],
                        goal=m["目標"], goal_weight=m["目標重量"], parts=m["部位"],
                        order=order_counter[m["分割"]],
                    )
                    made += 1
                except requests.HTTPError as e:
                    errors.append(f"{m['種目']}: {e}")
                prog.progress((i + 1) / len(parsed))
            st.cache_data.clear()
            if made:
                st.success(f"{made} 種目を W{int(new_week)} に作成しました。「✍️ 今日の記録」で週を W{int(new_week)} にすると選べます。")
            if errors:
                st.error("一部失敗:\n" + "\n".join(errors[:5]))
    elif pasted.strip():
        st.info("解析できる行がありませんでした。形式を確認してください（パイプ区切り）。")

    st.divider()

    # --- ③ 最終目標(3ヶ月後)を設定 ---
    st.subheader("③ 最終目標を設定（3ヶ月後・グラフの基準）")
    st.caption(
        "ここで設定した目標が「📊 グラフ」タブの『🎯 目標に対する現在地』の基準になります。"
        "毎週のメニューとは別で、更新するまで固定です。種目名は実績の種目名と一致させてください。"
    )
    goals_now = df[df["週"] == GOAL_WEEK][["種目", "目標重量"]].drop_duplicates("種目", keep="last")
    if not goals_now.empty:
        st.write(f"**現在の最終目標: {len(goals_now)} 種目**")
        st.dataframe(goals_now, use_container_width=True, hide_index=True)
    st.caption("目標を更新すると、古い目標は消えずに履歴として保管されます（下で確認できます）。")

    hist = df[df["週"] == GOAL_HISTORY_WEEK][["種目", "目標", "目標重量"]]
    if not hist.empty:
        with st.expander(f"🗂 過去の目標の履歴（{len(hist)} 件）"):
            st.dataframe(hist.rename(columns={"目標": "期間"}),
                         use_container_width=True, hide_index=True)

    # Claudeに投げる用のお願い文（コピーして貼るだけ）
    goal_prompt = (
        "3ヶ月後の最終目標を、下の形式で1種目1行・コードブロックで返してください"
        "（そのままアプリに貼り込みます。種目名は今の記録と揃えてください）:\n"
        f"```\n{GOAL_FORMAT}\nレッグプレス | 270lb\nチェストプレス(LOW angle) | 175lb\n```"
    )
    st.caption("↓ これをコピーして Claude に投げると、貼り付け用の形式で返してくれます。")
    st.code(goal_prompt, language="markdown")

    goal_pasted = st.text_area(
        "最終目標を貼り付け", height=160,
        placeholder="レッグプレス | 270lb\nチェストプレス(LOW angle) | 175lb\nシーテッドロー | 75lb",
    )
    parsed_goals = parse_goals(goal_pasted) if goal_pasted.strip() else []
    if parsed_goals:
        st.write(f"**解析結果: {len(parsed_goals)} 種目**")
        st.dataframe(pd.DataFrame(parsed_goals), use_container_width=True, hide_index=True)
        if st.button(f"🎯 {len(parsed_goals)} 種目の最終目標を保存/更新", type="primary"):
            done_cnt, errors = 0, []
            prog = st.progress(0.0)
            for i, g in enumerate(parsed_goals):
                try:
                    upsert_goal(token, df, g["種目"], g["目標重量"], until_week=latest_week)
                    done_cnt += 1
                except requests.HTTPError as e:
                    errors.append(f"{g['種目']}: {e}")
                prog.progress((i + 1) / len(parsed_goals))
            st.cache_data.clear()
            if done_cnt:
                st.success(f"{done_cnt} 種目の最終目標を保存しました。「📊 グラフ」で確認できます。")
            if errors:
                st.error("一部失敗:\n" + "\n".join(errors[:5]))
    elif goal_pasted.strip():
        st.info("解析できる行がありませんでした。`種目 | 目標重量` の形式で貼ってください。")

# =====================================================================
# 🏅 W1–W15 — 目標を切り替える前の成長記録（節目のまとめ）
# =====================================================================
if view == VIEWS[3]:
    UPTO = 15
    st.subheader(f"🏅 W1–W{UPTO} の成長記録")
    st.caption(
        f"W{UPTO + 1} から目標が切り替わったため、ここまでを一区切りとしてまとめたもの。"
        "重量は lb（kg併記）、懸垂などは回数。同じ種目名でも分割ごとに別マシンとして集計。"
    )
    wrows, rrows, summ = growth_stats(df, UPTO)
    if not wrows:
        st.info("まだ集計できる実績がありません。")
    else:
        h1, h2 = st.columns(2)
        h1.metric("積み上げた重量の合計", f"+{summ['合計増加']:g} lb",
                  help=f"= +{summ['合計増加'] * LB_TO_KG:.0f} kg")
        h2.metric("ベストを更新した種目", f"{summ['伸びた']} / {len(wrows)}")
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("記録セット数", f"{summ['記録']}")
        s2.metric("週数", f"{summ['週数']}")
        s3.metric("種目数", f"{summ['種目数']}")
        s4.metric("達成率", f"{summ['達成率']:.0f}%" if summ["達成率"] is not None else "—")

        st.divider()
        st.markdown(GROWTH_CSS, unsafe_allow_html=True)
        st.markdown("#### 初回 → ベスト（重量種目）")
        st.caption("薄い数字が最初の記録、**大きい緑がベスト**。"
                   "バーの緑はこの15週で伸ばした分。増加量の大きい順。")
        st.markdown(growth_rows_html(wrows, "lb"), unsafe_allow_html=True)
        with st.expander("kg で見る / 表で見る"):
            st.dataframe(
                pd.DataFrame([{
                    "種目": f"{r['種目']}（{r['分割']}）",
                    "初回 → ベスト (lb)": f"{r['初']:g} → {r['ベスト']:g}",
                    "初回 → ベスト (kg)": f"{r['初'] * LB_TO_KG:.1f} → {r['ベスト'] * LB_TO_KG:.1f}",
                    "増減": r["増減"], "伸び率(%)": round(r["率"]),
                    "週": f"W{r['初週']} → W{r['ベスト週']}",
                } for r in wrows]),
                use_container_width=True, hide_index=True, height=420,
            )

        if rrows:
            st.markdown("#### 初回 → ベスト（回数種目）")
            st.caption("自重・サーキット種目。重量ではなく回数（懸垂は4セット合計）で記録。")
            st.markdown(growth_rows_html(rrows, "回"), unsafe_allow_html=True)

        bw = df.dropna(subset=["体重", "日付"])
        if not bw.empty:
            st.divider()
            st.markdown("#### 体重")
            bwd = bw.groupby("日付", as_index=False)["体重"].max().sort_values("日付")
            st.metric("体重", f"{bwd['体重'].iloc[-1]:.1f} kg",
                      f"{bwd['体重'].iloc[-1] - 56.0:+.1f} kg（W1の56kgから）")
            figw = px.line(bwd, x="日付", y="体重", markers=True,
                           labels={"日付": "日付", "体重": "体重 (kg)"})
            figw.update_layout(height=300, margin=dict(l=6, r=6, t=20, b=10))
            st.plotly_chart(figw, use_container_width=True)
            st.caption("起点の 56.0kg（W1・2026/4/21）は記録がないため申告値。実測は 6/20 の 57.5kg から。")
