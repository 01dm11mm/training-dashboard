"""トレーニング記録 ダッシュボード（Notion × Streamlit）

Notion の「トレーニング記録」DB を読み書きする。
- ✍️ 今日の記録   : 今週の種目に実績（重量・ログ・達成）をスマホから入力
- 📊 グラフ       : 重量推移・自己ベスト・達成状況・今週メニューを可視化
- 📤 まとめ＆計画 : 今週のまとめをClaude用に出力 / 来週メニューを貼り付けて取り込み

すべて無料の仕組みだけで動く。実行: streamlit run app.py
"""

import datetime as dt
import os
import re

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

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


@st.cache_data(ttl=300, show_spinner="Notion から取得中…")
def fetch_records(token: str) -> pd.DataFrame:
    """Notion DB を全ページ取得して DataFrame にする（5分キャッシュ）。page_id 付き。"""
    url = f"{API}/databases/{DATABASE_ID}/query"
    rows, payload = [], {"page_size": 100}
    while True:
        resp = requests.post(url, headers=_headers(token), json=payload, timeout=30)
        resp.raise_for_status()
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
                  achieve=None, date=None, memo=None) -> None:
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


def _apply_master(ex_id: str) -> None:
    """マスター重量を、その種目の全セットの重量ボックスに反映する（on_change用）。"""
    mv = st.session_state.get(f"m_{ex_id}", 0.0)
    n = int(st.session_state.get(f"n_{ex_id}", 0) or 0)
    for s in range(n):
        st.session_state[f"w_{ex_id}_{s}"] = mv


def parse_set_count(goal: str, default: int = 3) -> int:
    """目標文字列からセット数を推定。例: '4×6-10'→4, '3×10'→3, '4セット'→4, '3周'→3。"""
    if not goal:
        return default
    m = re.match(r"\s*(\d+)\s*[×xX✕*]", goal)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*(セット|周|set)", goal, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return default


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

if st.button("🔄 最新に更新"):
    st.cache_data.clear()

try:
    df = fetch_records(token)
except requests.HTTPError as e:
    st.error(f"取得に失敗しました: {e}\nトークンと、DB をインテグレーションに共有しているか確認してください。")
    st.stop()

if df.empty:
    st.warning("データが空です。Notion 側で DB をインテグレーションに共有していますか？")
    st.stop()

latest_week = int(df["週num"].max()) if df["週num"].notna().any() else None

tab_input, tab_graph, tab_plan = st.tabs(["✍️ 今日の記録", "📊 グラフ", "📤 まとめ＆計画"])

# =====================================================================
# ✍️ 今日の記録 — 今週の種目に実績を埋める
# =====================================================================
with tab_input:
    st.subheader("今日の記録を入力")

    c1, c2, c3 = st.columns([1, 1, 1])
    weeks = sorted([int(w) for w in df["週num"].dropna().unique()])
    week_sel = c1.selectbox(
        "週", weeks, index=len(weeks) - 1 if weeks else 0,
        format_func=lambda w: f"W{w}",
    )
    splits_here = [s for s in SPLITS if s in set(df[df["週num"] == week_sel]["分割"])]
    split_sel = c2.selectbox("分割（今日のメニュー）", splits_here or SPLITS)
    rec_date = c3.date_input("実施日", value=dt.date.today())

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
            n_default = parse_set_count(row["目標"])
            # 初期値を session_state に入れておく（value= と key= の二重指定警告を避ける）
            if f"n_{ex_id}" not in st.session_state:
                st.session_state[f"n_{ex_id}"] = n_default
            if f"a_{ex_id}" not in st.session_state:
                st.session_state[f"a_{ex_id}"] = row["達成"] if row["達成"] in ACHIEVE_OPTIONS else "（未入力）"

            done_mark = "✅" if pd.notna(row["実績重量"]) else "・"
            st.markdown(f"**{done_mark} {row['種目']}**　🎯{row['目標'] or '—'}　/　目標重量 {row['目標重量'] or '—'}")

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
                sc1, sc2 = st.columns(2)
                sc1.number_input(f"重量 set{s + 1}", min_value=0.0, step=2.5, key=f"w_{ex_id}_{s}")
                sc2.number_input(f"回数 set{s + 1}", min_value=0, step=1, key=f"r_{ex_id}_{s}")

            if row["実績ログ"]:
                st.caption(f"既存ログ: {row['実績ログ']}")
            st.divider()

        if st.button("💾 保存", type="primary", use_container_width=True):
            saved, errors = 0, []
            for _, row in target.iterrows():
                ex_id = row["page_id"]
                n = int(st.session_state.get(f"n_{ex_id}", 0) or 0)
                ach = st.session_state.get(f"a_{ex_id}", "（未入力）")
                sets = []
                for s in range(n):
                    wv = float(st.session_state.get(f"w_{ex_id}_{s}", 0.0) or 0.0)
                    rv = int(st.session_state.get(f"r_{ex_id}_{s}", 0) or 0)
                    if wv > 0 or rv > 0:
                        sets.append((wv, rv))
                # 何も入力が無ければスキップ
                if not sets and ach == "（未入力）":
                    continue
                # ログ組み立て：重量が全セット同じなら「重量×回数,回数,…」、違えば「重量×回数, …」
                weights = [wv for wv, rv in sets]
                if not sets:
                    log, top_weight = None, None
                elif len(set(weights)) <= 1:
                    wv = weights[0]
                    reps_str = ",".join(str(rv) for _, rv in sets)
                    log = f"{wv:g}×{reps_str}" if wv > 0 else reps_str
                    top_weight = wv if wv > 0 else None
                else:
                    log = ", ".join(f"{wv:g}×{rv}" for wv, rv in sets)
                    top_weight = max(weights)
                try:
                    update_record(
                        token, ex_id,
                        weight=top_weight,
                        log=log,
                        achieve=None if ach == "（未入力）" else ach,
                        date=rec_date,
                    )
                    saved += 1
                except requests.HTTPError as e:
                    errors.append(str(e))
            st.cache_data.clear()
            if saved:
                st.success(f"{saved} 件保存しました。")
            if errors:
                st.error("一部失敗: " + " / ".join(errors[:3]))
            if not saved and not errors:
                st.info("入力がありませんでした。")

# =====================================================================
# 📊 グラフ
# =====================================================================
with tab_graph:
    done = df.dropna(subset=["実績重量"]).copy()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("記録セット数", f"{len(done)}")
    c2.metric("種目数", f"{done['種目'].nunique()}")
    c3.metric("記録週数", f"{int(done['週num'].nunique())}" if done['週num'].notna().any() else "0")
    graded = df["達成"].notna().sum()
    achieved = (df["達成"] == "✅達成").sum()
    c4.metric("達成率", f"{achieved / graded * 100:.0f}%" if graded else "—")

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
            st.dataframe(pb, use_container_width=True, hide_index=True, height=360)

        with col_ac:
            st.subheader("🎯 達成状況の内訳")
            counts = df["達成"].value_counts().reindex(ACHIEVE_OPTIONS).dropna().reset_index()
            counts.columns = ["達成", "件数"]
            if not counts.empty:
                fig2 = px.pie(counts, names="達成", values="件数", hole=0.5)
                fig2.update_layout(height=360)
                st.plotly_chart(fig2, use_container_width=True)

    st.divider()
    if latest_week is not None:
        st.subheader(f"📋 今週(W{latest_week})のメニュー")
        cols = ["分割", "種目", "目標", "目標重量", "実績重量", "達成", "メモ"]
        this_week = df[df["週num"] == latest_week][cols]
        st.dataframe(this_week, use_container_width=True, hide_index=True)

# =====================================================================
# 📤 まとめ＆計画 — Claudeとの週次ループ
# =====================================================================
with tab_plan:
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
