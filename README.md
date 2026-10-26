# 株式 需給モニター（equity-flow-monitor）

株式市場で「誰が株を吸収し、誰が株を出しているか」を、無料の公開データから毎日推計してダッシュボードに表示する。

| 市場 | 状態 | データ取得 | 公開ページ |
| --- | --- | --- | --- |
| 米国株 | 最小版 | SEC EDGAR（Form 4、Form 144、424B、S-1/F-1） | `/us/` |
| 日本株 | 準備中 | — | `/jp/` |

## 構成

```
efm/
  core/store.py     保存（全市場共通）：data/<市場>/raw/<種類>/<年>/<日付>.csv.gz
  site.py           公開サイト全体を作る（全市場のページ＋トップページ）
  us/               米国版：EDGAR取得、内部者売買、IPO・増資、ダッシュボード
  jp/               日本版（今後追加）
data/<市場>/        取得したデータ（GitHub Actionsが毎日追記）
.github/workflows/  市場ごとの自動実行（us-daily.yml）
```

市場を追加するときは、`efm/<市場>/` に取得処理と `dashboard.py`（`build()`）を作り、`efm/site.py` の `MARKETS` に1行足し、`.github/workflows/<市場>-daily.yml` を追加する。

## 必要な設定（GitHubのSecrets）

- `SEC_USER_AGENT`：`氏名 メールアドレス`（SECの利用規則で必須）
- `ANTHROPIC_API_KEY`：目論見書の金額読み取りに使う（未設定なら読み取りを省略）

## 手元で動かす

```
pip install -r requirements.txt
SEC_USER_AGENT="Name you@example.com" python -m efm.us.run --max-days 1
python -m efm.site      # 保存済みデータからページだけ作り直す
python -m pytest -q
```
