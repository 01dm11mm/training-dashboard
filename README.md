# 💪 トレーニング ダッシュボード（Notion × Streamlit）

Notion の「トレーニング記録」DB を読み込み、**重量推移・自己ベスト・達成状況・今週メニュー**を
グラフで表示する。すべて無料の仕組みだけで動く（Notion API 無料 / Streamlit 無料）。

---

## 1. Notion 側の準備（最初の1回だけ）

1. **インテグレーションを作る**
   - https://www.notion.so/profile/integrations を開く → 「New integration」
   - Type: **Internal**、ワークスペースを選択 → 作成
   - 「Internal Integration Secret」をコピー（`ntn_...` または `secret_...`）

2. **DB をインテグレーションに共有する**
   - Notion で「トレーニング記録」DB を開く
   - 右上 **`•••` → Connections（接続）→ さっき作ったインテグレーションを追加**
   - これをしないとアプリからデータが見えません

---

## 2. ローカルで動かす

```bash
cd ~/training-dashboard

# 仮想環境（任意だが推奨）
python3 -m venv .venv
source .venv/bin/activate

# 依存をインストール
pip install -r requirements.txt

# トークンを設定
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
#  ↑ できた secrets.toml を開いて NOTION_TOKEN に自分のシークレットを貼る

# 起動
streamlit run app.py
```

ブラウザが自動で開きます（同じ Wi-Fi なら表示される Network URL でスマホからも見られます）。

---

## 3. スマホからいつでも見たい → 無料クラウドにデプロイ

1. このフォルダを **GitHub にプッシュ**（`secrets.toml` は `.gitignore` 済みなので上がりません）
2. https://share.streamlit.io にログイン → 「New app」→ このリポジトリ / `app.py` を選択
3. デプロイ後、**App settings → Secrets** に下記を貼る:
   ```toml
   NOTION_TOKEN = "ntn_あなたのシークレット"
   ```
4. 発行された URL をスマホのホーム画面に追加すれば、アプリのように使えます

---

## 何が見られる？

- **📈 重量の推移** … 種目を選ぶと折れ線（横軸は「週」or「日付」で切替）
- **🏆 自己ベスト** … 種目ごとの最大実績重量
- **🎯 達成状況** … ✅達成 / △一部 / ❌未達 / －スキップ の割合
- **📋 今週のメニュー** … 最新週の目標・目標重量・実績

「🔄 最新に更新」ボタンで Notion の最新データを取り直します（通常は5分キャッシュ）。

---

## 仕組み

```
Notion DB ──(REST API / requests)──> Python(pandas) ──> Streamlit + Plotly でグラフ
```

- DB ID は `app.py` の `DATABASE_ID` に設定済み（`dc49803f...`）。変えたい場合は環境変数 `NOTION_DATABASE_ID` で上書き可。
- API バージョンは安定版 `2022-06-28` に固定。
