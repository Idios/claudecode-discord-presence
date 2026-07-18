# Phase C: プロジェクト健全化の設計

- **Issue**: #5 — Phase C: プロジェクト健全化（CI・プラットフォーム方針・docs整合）
- **日付**: 2026-07-16
- **前提**: Phase A+B（PR #6, main未マージ）のブランチ `claude/strange-khorana-503a78` の上に **stack** する。

## 背景

同種バグの再発を許した構造的ギャップ（CI不在・未検証の対応表明・docsと実装の乖離）を塞ぐ。Phase A/B でバグ自体と観測性は対処済み。Phase C は再発防止の足場（CI・正直な対応表明・実装と整合した docs）を整える。

## スコープと確定した方針

5領域を1つの spec にまとめる（個々は小さく、すべて「プロジェクト健全化」で一貫）。ブレインストーミングで確定した2つの主要判断:

- **プラットフォーム方針 = Windows専用の明記**（オプションaを選択）。プラットフォーム層分離（`_platform_*`）による mac/Linux 実装・検証は行わず、別issueに切り出す。ソース手編集の設定導線を廃止し、プロセス名は環境変数で上書き可能にする。
- **ライフサイクル = 単一の終了条件「Claude プロセスが連続不在」＋デバウンス**。idle は presence クリアのみ（終了しない）。docs を実装に合わせる。

## ファイル構成

- **新規** `.github/workflows/ci.yml` — 3OS × Python 3.10 のテストマトリクス。
- **変更** `pyproject.toml` — `dynamic = ["version"]` で version を一元化。
- **変更** `claudecode_discord_presence/main.py` — `CCDP_*` 環境変数設定、プロセス検出の厳密化、終了デバウンス。
- **変更** `tests/test_main.py` — flakyテスト修正、env設定・デバウンス・検出のテスト。
- **変更** `README.md` — idle/exit の訂正、環境変数表、Windows専用の対応表、hook スキーマ/console-script、CIバッジ、uninstall 更新。
- **変更** `CLAUDE.md` — Windows専用、ライフサイクル訂正、「Invariants and Known Traps」節の新設。

## 設計

### 1. CI（`.github/workflows/ci.yml`）

- **マトリクス**: `windows-latest` / `ubuntu-latest` / `macos-latest` × Python `3.10`（最低サポート版）。
- **各ジョブ**: checkout → `actions/setup-python` → `pip install -e .` → `python -m pytest -v`。
- 統合テスト `tests/test_single_instance_integration.py`（N hooks同時→生存1）も CI で走り、ロックの中核不変条件を担保する。
- **3OS全て required**。テストスイートはプラットフォーム非依存の純関数＋OSロック統合テストなので3OSで緑になる想定。これが「Windows専用だがコードは3OSでインポート/テスト可能」という正直な対応表明の唯一の根拠になる（mac/Linux はテストは通るが、Discord連携・実プロセス検出は未検証）。
- 前提として **flakyテスト `test_exact_boundary_timeout`（Windows mtimeずれ）を Phase C で修正**（さもないと Windows ジョブが断続的に赤になる）。修正方針: `is_session_active` の本番意味を変えず、テストが sub-ms のクロック整合に依存しない形にする（例: `os.utime` で mtime を既知値に固定して境界を決定的に検証）。

### 2. プラットフォーム方針（Windows専用の明記）

- **設定の環境変数化**（`main.py`）。`CCDP_` プレフィックスで統一:
  - `CCDP_CLAUDE_PROCESS_NAME`（既定: win32 は `claude.exe`、他は `claude`）
  - `CCDP_POLL_INTERVAL_SEC`（既定 15）/ `CCDP_IDLE_TIMEOUT_SEC`（既定 600）/ `CCDP_EXIT_CONFIRM_COUNT`（既定 3）
  - 数値は不正値（非整数・負）なら既定にフォールバック。小さなヘルパー `_env_int(name, default)` で吸収。
  - `CLIENT_ID` は Discord アプリID のため定数のまま（ユーザー設定対象外）。
- **プロセス検出の厳密化**（`is_claude_running`, win32 分岐）。現状の `CLAUDE_PROCESS_NAME.lower() in result.stdout.lower()`（緩い部分文字列一致）を、`tasklist /NH /FI "IMAGENAME eq <name>"` の各行を検査し、**プロセス名で始まる行が実在するか**で判定する形にする。`INFO:`（該当なし）や無関係文字列に反応しない。
- **POSIX 分岐**は「未検証・実験的」とコメントで明示し、`CCDP_CLAUDE_PROCESS_NAME` による上書き余地のみ残す（深追いしない=YAGNI、別issue）。「ソース手編集」導線は廃止。

### 3. ライフサイクル（デバウンス付き単一終了条件）

`main.py` に `EXIT_CONFIRM_COUNT = _env_int("CCDP_EXIT_CONFIRM_COUNT", 3)`。`_run_daemon` のループを「1回不在で即終了」から連続不在カウンタに置換:

```python
    missed = 0
    while True:
        if _stop_requested():
            exit_reason = "stop requested"; break
        if is_claude_running():
            missed = 0
        else:
            missed += 1
            if missed >= EXIT_CONFIRM_COUNT:
                exit_reason = "claude gone"; break
        active = is_session_active(projects_dir, IDLE_TIMEOUT_SEC)
        rpc, presence_active = _reconcile_presence(active, presence_active, rpc)
        if _sleep_until_poll():
            exit_reason = "stop requested"; break
```

- **一過性の tasklist 失敗を吸収**: 1回の False では終了せず、`EXIT_CONFIRM_COUNT` 回連続で初めて終了（POLL=15s × 3 ≒ Claude 終了後約45秒）。
- **単一の終了条件**: 終了は「Claude プロセス連続不在」のみ。idle は presence クリアのみで監視継続（Phase B のまま）。
- セクション2の検出厳密化と併せ、誤終了（一過性失敗）と過剰生存（緩い一致）の双方を緩和。

### 4. パッケージング

- **version 一元化**: `pyproject.toml` の `version = "0.1.0"` を削除し `dynamic = ["version"]`。`[tool.setuptools.dynamic]` に `version = {attr = "claudecode_discord_presence.__version__"}`。`__init__.py` の `__version__` が唯一の源。
- **hook 起動の信頼性**: README の `settings.json` の hook コマンドを、曖昧な `python -m claudecode_discord_presence.hook` から console-script **`claudecode-discord-presence-hook`**（`[project.scripts]` 定義済み）に変更。
- **hook JSON スキーマ修正**: 現状は `hooks` ネスト欠如で発火しない。正しい形:
  ```json
  {
    "hooks": {
      "SessionStart": [
        { "hooks": [
            { "type": "command", "command": "claudecode-discord-presence-hook" }
        ] }
      ]
    }
  }
  ```
- `hook.py` 内部の detached 起動（`sys.executable -m ...`）は同一インタプリタで堅牢＝変更不要。
- README に **pipx** を分離インストールの推奨導線として併記。

### 5. ドキュメント整合

**README.md**:
- 「idle で終了」系（冒頭・How It Works 図・Setup step4）を「Claude Code 終了で自動終了（数回連続確認）／idle は presence をクリアするだけ」に訂正。
- Configuration 表を環境変数表（`CCDP_POLL_INTERVAL_SEC` / `CCDP_IDLE_TIMEOUT_SEC` / `CCDP_EXIT_CONFIRM_COUNT` / `CCDP_CLAUDE_PROCESS_NAME`）に置換。
- Platform Support 表を Windows専用に: Windows=Supported(Tested)、macOS/Linux=Experimental/Unverified（CIでテストは通るが Discord連携・実プロセス検出は未検証、`CCDP_CLAUDE_PROCESS_NAME` で上書き可）。「main.py の `CLAUDE_PROCESS_NAME` を編集」導線を削除。
- Setup の hook を console-script + 正しい JSON スキーマに。CI バッジを追加。
- Uninstall: ロックが自動掃除するので `.pid`（Windowsは `.pid.lock`）・`.stop` の手動削除は通常不要、と更新。

**CLAUDE.md**:
- Supported Platforms を Windows専用に。ライフサイクル記述（「10分idleで終了」「polling 1分」）を実装（idle=クリア、exit=Claude連続不在、POLL=15s）に訂正。
- **「Invariants and Known Traps」節を新設**:
  - 単一インスタンスは**アトミックOSロック（`InstanceLock`）で保証**。PIDファイルの文字列内容ではない。check-then-write を復活させない。
  - Windows で **`os.kill(pid, 0)` 禁止**（プロセスを終了させてしまう）。生存ポーリングは廃止済み、ロックが担う。
  - **N hooks→1 process 統合テストを常に緑に保つ**。
  - ロギングは**デーモン専用（単一ライター）**。hook/CLI にファイルログを足さない。
  - STOP センチネルは**PIDマッチ**。終了は**デバウンス（連続不在）で単一条件**。
- Setup/Uninstall のソース手編集参照を環境変数に更新。

## テスト戦略

- **flakyテスト修正**: `test_exact_boundary_timeout` を決定的に（`os.utime` で mtime を固定した境界検証）。
- **`_env_int`**: 未設定→既定、正当な整数→採用、非整数/負→既定にフォールバック（`monkeypatch.setenv`）。
- **`CCDP_CLAUDE_PROCESS_NAME`**: 環境変数がプロセス名の解決に反映される（既存 tasklist テストを env 上書きで拡張）。
- **プロセス検出の厳密化**: `INFO: No tasks...`（該当なし）→ False、実プロセス行→ True、無関係な部分文字列→ False。
- **終了デバウンス**: 単発の `is_claude_running()==False` では終了せず、`EXIT_CONFIRM_COUNT` 連続で終了することを、`_run_daemon` を制御した反復で検証（`is_claude_running` をシーケンスで返すモック、`_sleep_until_poll`/`configure_logging` をパッチ）。
- CI 上で全 OS で全テスト green。

## 完了の定義

- `.github/workflows/ci.yml` が 3OS × py3.10 でテスト（統合テスト含む）を実行し緑。
- version が `__init__.py` に一元化（`pyproject.toml` は dynamic）。
- 設定が `CCDP_*` 環境変数で上書き可能、ソース手編集の導線が docs から消える。
- プロセス検出が厳密化され、終了が単一条件＋デバウンスになる。
- README/CLAUDE.md が実装と整合し、CLAUDE.md に「Invariants and Known Traps」節がある。
- README の hook 設定が Claude Code スキーマに一致し console-script を使う。
- flakyテストが決定的になり全テスト green。

## 対象外（別issue）

- mac/Linux の実プロセス検出（`node`＋コマンドライン検査）とプラットフォーム層分離。
- presence 内容・アクティビティ種別の変更。

出典: Fable #3,#4,#6,#7,#8,#9
