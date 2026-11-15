# ravens-record-core

[日本語](#日本語) | [English](#english)

---

## 日本語

**ARMORED CORE VI FIRES OF RUBICON** のランクマッチ・カスタムマッチを、ゲームの画面から自動で判定して記録する Python ライブラリです（非公式）。
戦績記録アプリ [RAVEN'S RECORD](https://ravens-record.com) の判定部分を切り出したもので、RAVEN'S RECORD 自身もこのライブラリを使っています。

ゲームのメモリやファイルには触れません。ゲームのウィンドウを撮影し、画面に出ている文字や表示を読むだけです。

### できること

- **ランクマッチ（シングル）**
  - ロビーで「対戦を開始します」を見つけて、試合の記録を始める
  - ロード画面の終わりを確かめ、試合前の映像からマップを判定する
  - 試合中のスコアのマス・勝敗の帯・リザルト画面から、最終スコアと勝敗を決める
  - 対戦前・対戦後のレート（S ランクの順位）を読む
- **カスタムマッチ（シングル）**
  - ルーム画面の設定（対戦形式・制限時間・メンバー交代方式・マップ）を読む
  - 自分が出た対戦はスコアを、観戦した対戦は ALPHA / BETA のラウンド数を記録する
  - プレイヤー名などの個人の情報は読みません
- **ログ**
  - 判定の経過を JSON Lines（1 行 1 件の JSON）で出す
  - 試合が終わるたびに、結果を 1 つの JSON ファイルに保存する

チーム戦は記録しません。

### 必要なもの

- Windows 10 / 11
- Python 3.15 以上（[uv](https://docs.astral.sh/uv/) を推奨）
- [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki)（言語データ `eng`・`jpn`・`kor` を入れる）
- ゲームの画面モードは **ボーダーレス**（フルスクリーンでは、別の画面に切り替えたときに撮影できません）

### 使い方（コマンド）

```bash
git clone https://github.com/Lond0612/ravens-record-core.git
cd ravens-record-core
uv sync
uv run python -m ravens_record_core --log-file events.jsonl
```

ゲームの起動を待ち、試合を見つけるたびに記録します。止めるときは Ctrl+C です。

| オプション | 内容 |
|---|---|
| `--data-dir DIR` | 保存先（既定は `%LOCALAPPDATA%\RavensRecordCore`） |
| `--log-file FILE` | イベントを JSON Lines でファイルに追記する |
| `--events-only` | `--log-file` に、ふつうのログ行を書かない |
| `--json` | 標準出力のログを JSON Lines にする |
| `--tesseract-dir DIR` | `tesseract.exe` のあるフォルダ（指定しなければ PATH などから探す） |
| `--no-custom` | カスタムマッチを記録しない |
| `--trigger-exe NAME` | 監視するゲームの実行ファイル名（テスト用） |

### 使い方（Python）

```python
from pathlib import Path
from ravens_record_core import Config, Hooks, Recorder


class MyHooks(Hooks):
    def on_match_saved(self, session, path):
        print("記録しました:", session.result, session.score_my, session.score_enemy, path)

    def on_custom_match(self, kind, payload, path):
        print("カスタムマッチ:", kind, payload)
        return True


Recorder(Config(data_dir=Path("my-logs")), MyHooks()).run()
```

- `Config` … 監視対象・保存先・Tesseract の場所など（`config.py`）
- `Hooks` … 記録の途中に処理を差し込むところ。必要なメソッドだけ上書きします（`hooks.py`）。
  差し込んだ処理が例外を投げても、記録は止まりません
- 状態の変化やイベントは、`ravens_record_core.logger.add_sink(関数)` で受け取れます

`import ravens_record_core` しただけでは、画面・ファイル・環境変数には何もしません。

### ログの形

**イベント（JSON Lines）**

```json
{"ts":"2026-11-15T21:00:00+09:00","level":"info","event":"status","message":"対戦中です（スコアを監視しています）","state":"in_match","msg_key":"status.inMatch"}
```

主なイベントは `status`・`match_detected`・`map_detected`・`score_confirmed`・`score_missing`・`match_saved`・`custom_match_saved` です。一覧は [`events.py`](src/ravens_record_core/events.py) を見てください。

**試合ごとの JSON**（`<保存先>/logs/`）

| ファイル | 中身 |
|---|---|
| `YYYY-MM-DD_HH-MM-SS_自分-相手_session.json` | ランクマッチ。マップ・スコア・勝敗・対戦前後のレートと順位・終わった日時・スコアの根拠など |
| `YYYY-MM-DD_HH-MM-SS_custom_自分-相手.json` | 自分が出たカスタムマッチ。読んだ設定・マップの照合結果・ラウンド数 |
| `YYYY-MM-DD_HH-MM-SS_custom_spectated_A-B.json` | 観戦したカスタムマッチ。ALPHA / BETA のラウンド数 |

読めなかったスコアは `X` です。項目の説明は [`match_session.py`](src/ravens_record_core/match_session.py) と [`matchlog.py`](src/ravens_record_core/matchlog.py) にあります。

### 仕組み（ざっくり）

| 段階 | 主なファイル |
|---|---|
| 画面の撮影（DXGI / PrintWindow / mss） | `system/screencap.py` |
| 見出し（SINGLE / TEAM / CUSTOM MATCH）・「マッチングしています」の読み取り | `detectors/mode_detector.py`・`detectors/match_status_detector.py`・`ocr/` |
| ロード画面の終わり（別プロセス） | `detectors/loading_sequence_detector.py`・`detectors/loading_screen_watcher.py` |
| マップの判定（色のヒストグラムの照合） | `detectors/map_detector.py`・`data/labels_pool.json` |
| スコア・勝敗（別プロセス） | `detectors/score_detector.py`・`detectors/match_end.py` |
| レート | `detectors/rate_detector.py`・`ocr/rate_digits.py` |
| 全体の流れ | `monitor/loop.py` |

文字の形の見本（`ocr/shape_templates.py`・`ocr/rate_digit_templates.py`）とマップの特徴量（`data/labels_pool.json`）は、ゲームの画面から作った数値のデータです。画面の画像そのものは含めていません。

### テスト

```bash
uv run python -m unittest discover -s tests
```

テストはゲームを使いません（監視対象を存在しない名前に差し替えます）。

### ライセンス・注意

- ライブラリのコードは [MIT License](LICENSE) です。
- これは非公式のツールで、株式会社フロム・ソフトウェア、株式会社バンダイナムコエンターテインメントとは関係ありません。
  ARMORED CORE は、それぞれの権利者の商標または登録商標です。
- Tesseract OCR は別のソフトウェアです（Apache License 2.0）。

---

## English

An unofficial Python library that automatically detects and records **ARMORED CORE VI FIRES OF RUBICON** ranked and custom matches from the game screen.
It is the detection core of the match-tracking app [RAVEN'S RECORD](https://ravens-record.com), which uses this library itself.

It never touches the game's memory or files. It only captures the game window and reads what is shown on screen.

### Features

- **Ranked matches (single)**: detects the match start in the lobby. It then confirms the end of the loading screen, identifies the map from the pre-match intro, and decides the final score and result. It also reads the rating (and S-rank place) before and after the match.
- **Custom matches (single)**: reads the room settings (format, time limit, swap mode, map). It records your own matches (score) and spectated matches (ALPHA / BETA rounds). It never reads player names.
- **Logs**: writes progress events as JSON Lines, and saves one JSON file per finished match.

Team matches are not recorded.

### Requirements

- Windows 10 / 11
- Python 3.15+ ([uv](https://docs.astral.sh/uv/) recommended)
- [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) with the `eng`, `jpn` and `kor` language data
- The game must run in **borderless** mode. In fullscreen, the window cannot be captured after you switch to another window.

### Command line

```bash
git clone https://github.com/Lond0612/ravens-record-core.git
cd ravens-record-core
uv sync
uv run python -m ravens_record_core --log-file events.jsonl
```

It waits for the game to start, then records every match it finds. Press Ctrl+C to stop. Run with `--help` for all options: data directory, Tesseract folder, JSON output, and disabling custom matches.

### Python API

```python
from pathlib import Path
from ravens_record_core import Config, Hooks, Recorder


class MyHooks(Hooks):
    def on_match_saved(self, session, path):
        print("saved:", session.result, session.score_my, session.score_enemy, path)


Recorder(Config(data_dir=Path("my-logs")), MyHooks()).run()
```

- Subclass `Hooks` and override only what you need (see `hooks.py`). Exceptions raised in your hooks never stop recording.
- You can receive status changes and other events with `ravens_record_core.logger.add_sink(callable)`.
- Importing the package has no side effects: it does not touch the screen, files or environment variables.

### Output

- **Events (JSON Lines)** carry `ts`, `level`, `event` and `message`, plus extra fields per event. See [`events.py`](src/ravens_record_core/events.py) for the list.
- **One JSON file per match** is written to `<data dir>/logs/`:
  - `*_session.json`: a ranked match
  - `*_custom_*.json`: a custom match you played
  - `*_custom_spectated_*.json`: a spectated custom match

  Field descriptions are in [`match_session.py`](src/ravens_record_core/match_session.py) and [`matchlog.py`](src/ravens_record_core/matchlog.py).

Code comments are written in Japanese.

### License and notice

- The code is released under the [MIT License](LICENSE).
- This is an unofficial tool. It is not affiliated with FromSoftware, Inc. or Bandai Namco Entertainment Inc. ARMORED CORE is a trademark or registered trademark of its respective owners.
- The bundled glyph templates and map features are numeric data derived from the game screen; no screenshots are included.
- Tesseract OCR is separate software (Apache License 2.0).
