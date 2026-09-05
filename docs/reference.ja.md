# 詳細仕様

[← 玄関 README へ戻る](../README.ja.md) ｜ [🇺🇸 English](reference.md) ｜ 🔧 [エンジニア向けドキュメント](engineering.ja.md)

実装者・運用者向けに、CLI の引数・HTTP ルート・保存先・ペアリング契約の要点をまとめます。環境変数の全表は [env.md](env.md)（生成物）、ペアリングの正本は [contracts/pairing-v1.md](contracts/pairing-v1.md) です。ここに無い値はそちらが正です。

---

<a id="cli"></a>

## CLI

`caty-gateway` 1 本にサブコマンド 6 つ。`python -m caty_gateway` も同じディスパッチャです。

### setup

```text
caty-gateway setup --member MEMBER [--backend BACKEND] [--port PORT] [--name NAME]
                   [--accent ACCENT] [--public-url PUBLIC_URL]
                   [--qr-delivery {auto,tty,url}] [--yes] [--plan-only]
                   [--health-timeout HEALTH_TIMEOUT] [--reset] [--status]
                   [--no-history] [--wait]
```

| 引数 | 意味 |
|---|---|
| `--member` | メンバー id。`CATY_ID`・サービス名・保存先ディレクトリに使う。必須 |
| `--backend` | `claude` `codex` `openclaw` `hermes` `openai-compat`。`openai_compat` は正規化される |
| `--port` | 待ち受けポート（既定 8788）。使用中なら preflight で FAIL |
| `--name` `--accent` | `/identity` が返す表示名とアクセント色 |
| `--public-url` | QR に載せる到達先。省略時は Tailscale IPv4 から生成 |
| `--qr-delivery` | `tty`（端末に描画）／`url`（配信 URL を表示）／`auto` |
| `--plan-only` | 計画と実行予定の一覧を表示して終了。ファイル・サービス・状態を変更しない |
| `--no-history` | `CATY_HISTORY_DIR` を設定しない（履歴を残さない） |
| `--reset` | 再開用メタデータだけを捨てる |
| `--status` / `--wait` | 進行状態の表示／QR URL か終端状態まで待つ |
| `--health-timeout` | `/health` を待つ秒数（既定 30） |

生成される `CATY_TOKEN` は install 中にだけ生成され、計画表示では `[REDACTED]` になります。

### その他

| コマンド | 引数 |
|---|---|
| `status --member MEMBER [--wait]` | セットアップ／supervisor の状態 |
| `serve` | フォアグラウンド起動。`CATY_GATEWAY_BIND` が loopback 以外で `CATY_TOKEN` が空なら起動前に終了 |
| `qr [--qr-delivery {auto,tty,url}] [--wait-visible-seconds N]` | ペアリング QR の再発行 |
| `push open-url URL --title T [--audience A] [--session S] [--key K]` / `push media URL --title T …` | 接続中クライアントへのイベント送出。`--title` は必須。`CATY_TOKEN` は環境変数で渡す |
| `doctor --backend BACKEND [--port PORT] [--public-url URL]` | 受動 preflight。`--probe` は予約済み（このリリースでは FAIL を返す） |

### doctor の項目

共通 12 項目: OS ／ Python ／ ffmpeg ／ ffprobe ／ tailscale executable ／ tailscale login ／ tailscale IPv4 ／ port ／ public URL ／ config directory ／ state directory ／ data directory。backend 別に `claude version` `claude working directory` `claude credentials`、`codex version` とログイン状態、`openclaw` の実行ファイルとエージェント一覧、`hermes` の鍵と `/v1/models`、`openai-compat models` が加わります。1 つでも FAIL なら終了コード 1 です。

---

<a id="http"></a>

## HTTP ルート

すべて同じポートで待ち受けます。認証は `Authorization: Bearer <CATY_TOKEN>`。`setup` が生成する環境では `CATY_REQUIRE_AUTH=1` が固定で入ります。

| ルート | 認証 | 役割 |
|---|---|---|
| `GET /health` | `CATY_REQUIRE_AUTH=1` のとき要（`setup` 生成環境は常に要） | `{"ok":true,"agent":…}` |
| `GET /identity` | 要 | メンバー id・表示名・アクセント色 |
| `POST /talk2` `GET /stream/<id>` | 要 | 音声 1 ターン（STT → backend → TTS ストリーム） |
| `POST /see` | 要 | 音声 + 画面フレームを backend に渡す |
| `POST /share` `GET /history` `GET /history/…` | 要 | 添付の共有と会話履歴 |
| `GET /tts/voices` `…/preview` `…/voice-state` `…/voice-activations` | 要 | 声の一覧・プレビュー・状態 |
| `POST /push` | 要 | open-url / media イベント |
| `POST /pair/claim` | 不要（送信元制限） | QR の合言葉を長期 token に交換。tailnet か loopback からのみ |
| `POST /pair/new` `POST /pair/revoke` | 要（write） | 合言葉の再発行・失効 |

応答ヘッダとログからは Bearer 文字列が `[REDACTED]` に置換されます。会話本文は既定でログに出ません。

---

<a id="pairing"></a>

## ペアリング契約 v1（要約）

正本は [contracts/pairing-v1.md](contracts/pairing-v1.md)。ここでは運用に効く値だけを抜きます。

| 項目 | 値 |
|---|---|
| QR の中身 | `v`（=1）・`url`・`pair`（`<8 hex>.<32 hex>`）・`id`。長期 token は含まない |
| 合言葉の期限 | 600 秒（`CATY_PAIRING_TTL_SECONDS`） |
| 消費 | at-most-once。成功で削除し consumed 墓標を残す |
| 受付元 | loopback・tailnet IPv4 `100.64.0.0/10`・Tailscale IPv6 ULA `fd7a:115c:a1e0::/48` |
| 連続失敗 | 5 回で 60 秒ロックアウト、累計 50 回で失効 |
| レート | 60 秒固定窓（`CATY_PAIRING_RATE_PER_MIN`） |
| ストア | ディスク正。root 0700・レコード 0600・secret は SHA-256 のみ保存・原子的置換 |
| 1 メンバーあたり | 生きた合言葉は 1 つ。新規発行は旧を失効させる |

`CATY_PAIRING_ALLOW_NONTAILNET=1` は claim の受付元制限だけを外す非サポートのスイッチで、QR 配信側の制限（IPv4 のみ・IPv6 ULA を受けない）は変わりません。

---

<a id="paths"></a>

## 保存先と権限

| 種別 | パス | 権限 |
|---|---|---|
| 環境ファイル（Linux） | `~/.config/caty-gateway/<id>.env` | 0600 |
| 設定ディレクトリ | `~/.config/caty-gateway/<id>/` | ユーザー |
| 再開メタデータ・状態 | `~/.local/state/caty-gateway/setup/<id>.json`（成功時に削除）・`<id>.status.json` | 0600 |
| 履歴 | `~/.local/state/caty-gateway/history/<id>/`（`XDG_STATE_HOME` があればその配下） | ユーザー |
| 同梱資産のコピー・つなぎ音声 | `~/.local/share/caty-gateway/<id>/assets` `…/fillers` | ユーザー |
| ペアリングストア | `~/.local/state/caty-gateway/pairing/`（`CATY_PAIRING_DIR` で変更可） | 0700 / 0600 |
| launchd | `~/Library/LaunchAgents/ai.caty.gateway.<id>.plist`・ログ `~/Library/Logs/caty-gateway-<id>.log` | ユーザー |

---

<a id="env-tiers"></a>

## 環境変数の Tier

| Tier | 意味 | 例 |
|---|---|---|
| A | すべての設置に関係する | `CATY_ID` `CATY_BACKEND` `CATY_GATEWAY_PORT` `CATY_TOKEN` `CATY_PUBLIC_URL` |
| B | backend 別 | `CATY_CLAUDE_CWD` `CATY_GCLI_BIN` `CATY_AGENT` `CATY_HERMES_URL` `CATY_OPENAI_BASE_URL` |
| B2 | ペアリングの安全閾値 | `CATY_PAIRING_TTL_SECONDS` `CATY_PAIRING_MAX_FAILURES` |
| C | 任意機能 | `CATY_TTS_ENGINE` `CATY_AVATAR_*` `ANTHROPIC_API_KEY` `CATY_HISTORY_MAX_TURNS` |
| D | 内部・テスト用（設定しない） | `CATY_BACKEND_CONFIG_PATHS` ほか |

各変数の既定値・出現箇所・保存可否（member env 0600 / process-only）は [env.md](env.md) の表が正で、`make env-check` が生成物との差分を検査します。

---

## パッケージ

| 項目 | 値 |
|---|---|
| PyPI 名 / import 名 | `caty-gateway` / `caty_gateway` |
| Python | 3.10 以上 |
| 必須依存 | `qrcode[pil]`（Pillow は推移依存） |
| ビルド | hatchling・src layout |
| 同梱データ | `caty_gateway/assets/*.png`（表情 7 枚 + icon 1 枚・出自は [assets/PROVENANCE.md](../assets/PROVENANCE.md)）・`data/filler-texts-ja.json`・`templates/{launchd.plist,systemd.service}` |
| エントリポイント | `caty-gateway = caty_gateway.cli:main` |
