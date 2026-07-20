# Phase B: 可視化と安全網の設計

- **Issue**: #4 — Phase B: 可視化と安全網（ログ・--status/--stop・テスト再構成）
- **日付**: 2026-07-16
- **前提**: Phase A（#3, PR #6）のブランチ `claude/strange-khorana-503a78` の上に **stack** して作業する（同じ `main.py`/`hook.py`/テストを触るため）。

## 背景

33個の孤児プロセスが数時間気づかれなかった真因は、Phase A で根絶した二重起動そのものに加え、**観測性がゼロ**だったこと。`hook.py` が stdout/stderr/stdin を全て `DEVNULL` に流し、`main.py` は `print` のみのため、実運用（hook 経由の detached 起動）では全診断が消える。Phase A で再発は防いだが、万一の再発を早期検知できる安全網（ログ・状態確認・確実な停止手段）を整える。

## スコープ確定（Phase A 適用後の再評価）

issue #4 の項目のうち、Phase A で既に解消/無効化されたものを除外する:

- **無効**: `_is_process_alive_windows` の単体テスト追加・`WAIT_FAILED`/access-denied 修正 — `is_process_alive`（危険な `os.kill`）は Phase A で削除済み。この関数は存在しない。
- **完了済み**: `hook.py` の未使用変数 `main_module` 削除 — Phase A の hook.py 書き換えで解消済み。
- **要調整**: 「ループ本体を関数抽出せよ」— Phase A は `finally` が `rpc` を参照できるようループを意図的に `main()` 内インラインにした。単純な全体抽出は避け、presence 調整ステップのみ純粋関数に抽出する（後述）。

**本 Phase の実スコープ:**
1. 観測性（High）: `logging` + `RotatingFileHandler`、`--status` / `--stop` サブコマンド。
2. テスト品質（Medium）: 本番ループを再実装している `_run_one_iteration` を実コード検証に置換、`test_tasklist_oserror_returns_false` のモック修正。
3. Low: `time.sleep(60)` による終了/presenceクリア遅延の解消（ポーリング短縮＋割り込み可能待機）。

## ファイル構成

- **新規** `claudecode_discord_presence/logsetup.py` — ロギング設定（`LOG_FILE`, `configure_logging()`）。
- **変更** `claudecode_discord_presence/main.py` — CLI 分岐（argparse）、`_run_daemon`、`_reconcile_presence`、割り込み可能待機、`print`→`logging` 置換、`--status`/`--stop` 実装。
- **変更** `claudecode_discord_presence/hook.py` — `Popen` 失敗を握りつぶさず stderr にベストエフォート出力（回転ファイルには触れない）。
- **変更** `claudecode_discord_presence/single_instance.py` — STOP センチネルのパス定数 `STOP_FILE` を追加（PID_FILE の隣）。
- **変更** `tests/test_main.py` — `_run_one_iteration` 削除、`test_tasklist_oserror_returns_false` 修正、新規テスト群。
- **新規** `tests/test_logsetup.py`, `tests/test_cli.py`（--status/--stop）等、責務ごとに分割可。

## 設計

### 1. ロギング（`logsetup.py`）

```python
LOG_FILE = Path.home() / ".claude" / "claudecode-discord-presence.log"

def configure_logging() -> logging.Logger:
    """RotatingFileHandler(256KB × 2) + stderr StreamHandler を設定して返す。"""
```

- 出力先 `~/.claude/claudecode-discord-presence.log`、`RotatingFileHandler(maxBytes=256*1024, backupCount=2)` + `StreamHandler(stderr)`（前面実行時に見える）。
- **重要（多プロセス安全性）: 回転ファイルハンドラは「デーモンプロセスのみ」が持つ。** `configure_logging()` は `_run_daemon()` からのみ呼ぶ。デーモンはロックにより単一インスタンスが保証されるので、ログファイルの**ライターは常に1つ**になり、Windows で複数プロセスが同時にローテーションして破損/権限エラーになる問題を原理的に回避する。
  - `--status`/`--stop`（短命CLI）: **file logging しない**。結果は stdout に print。
  - `hook.py`（短命）: Popen 失敗は **stderr にベストエフォート出力**のみ（回転ファイルには触れない）。本番では stderr が DEVNULL なのでファイルには残らない — これは意図的なトレードオフ（単一ライター維持を優先）。hook 失敗は稀で、かつ「デーモンの起動ログ行が出ない」こと自体が異常のシグナルになる。
- ハンドラ多重登録を防ぐため、既に本パッケージのロガーにハンドラがあれば再設定しない（idempotent）。`print` は全廃。
- 記録する主要イベント（デーモン）:
  - 起動: PID / `__version__` / platform / poll・idle 設定値。
  - **ロック取得の勝敗**: 取得成功（勝者）/ 失敗（既存インスタンスあり → exit）。
  - RPC: connect 成功/失敗、presence 表示/クリア、切断・再接続。
  - **終了理由**: 「claude gone」「stop requested」「idle（該当時）」等を finally 直前に明示。
- レベル: 通常 INFO、失敗系は WARNING/ERROR。

### 2. CLI サブコマンド（`main.py`）

console_scripts エントリは `main:main`。`main()` 先頭で `argparse` により分岐。引数なし＝従来デーモン。

```python
def main() -> None:
    args = _parse_args()          # --status / --stop / (なし)
    if args.status:
        return _cmd_status()
    if args.stop:
        return _cmd_stop()
    _run_daemon()
```

**`--status`**: ロックを非ブロッキングで試す。取れない → 稼働中（PIDファイルからPIDを読み `running (pid N)`）。取れた → 稼働なし（即 release、`not running`）。ログファイル/PIDファイルのパスも stdout に人間向けに print（ロギングではなく print — 手動実行コマンドのため）。
- **これは「時点プローブ」であり、稼働の恒久保証ではない（本質的に TOCTOU）**。プローブ直後にデーモンが終了する可能性はある。出力もその旨を含意する表現にする。PIDファイルはロック保持中のみ authoritative（`InstanceLock.acquire()` が PID を書き、`release()` が削除する）。

**`--stop`**（STOP プロトコル — 競合を閉じるための順序が重要）:
1. `--stop` はまず `InstanceLock(PID_FILE)` を**非ブロッキングでプローブ**する。**取れた（＝稼働デーモン無し）→ センチネルを作らず** `not running` を print して終了。取れなかった（＝別プロセスが稼働中）→ PIDファイルから対象 PID を読む。
2. 稼働中の場合のみ、STOP センチネル `~/.claude/claudecode-discord-presence.stop` に**対象 PID を書き込んで**作成し、即終了（fire-and-forget）。停止確認はしない（必要なら `--status`）。
3. デーモンは各周回で `_stop_requested()` を評価: **センチネルが存在し、かつ内容 == 自分の `os.getpid()` の時だけ停止**（exit_reason="stop requested"）。内容が自PIDと不一致（古い停止要求 or 別インスタンス宛）なら、それは stale とみなして**センチネルを削除して稼働継続**。
4. デーモンは `finally` で（自PID宛だった場合の）センチネルを削除する。

このPIDマッチにより、Codex 指摘の競合が閉じる: (a) デーモン非稼働時の `--stop` はセンチネルを作らない、(b) `--stop` 直後に別セッションで新デーモンが起動しても、旧センチネルは旧PID宛なので新デーモンは無視して削除する、(c) 起動時の一括掃除ステップは不要（PID不一致判定が兼ねる）。

### 3. デーモンループの再構成（`main.py`）

```python
STOP_POLL_SEC = 2          # センチネルをチェックする粒度
POLL_INTERVAL_SEC = 15     # 60 → 15 に短縮

def _stop_requested() -> bool:
    """STOP センチネルが存在し、かつ自PID宛の時だけ True。

    自PIDと不一致（古い/別インスタンス宛）なら stale として削除し False。
    """
    try:
        target = int(STOP_FILE.read_text().strip())
    except (OSError, ValueError):
        return False
    if target == os.getpid():
        return True
    # stale: 別インスタンス宛 or 古い停止要求 → 掃除して継続
    try:
        STOP_FILE.unlink()
    except OSError:
        pass
    return False

def _sleep_until_poll() -> bool:
    """POLL_INTERVAL 秒待つ。途中で stop センチネルを検知したら即 True。"""
    for _ in range(0, POLL_INTERVAL_SEC, STOP_POLL_SEC):
        if _stop_requested():
            return True
        time.sleep(STOP_POLL_SEC)
    return _stop_requested()
```

`_run_daemon()` 構造（Phase A の lock 取得 + `try/finally` を維持）:
- 起動時の一括掃除は**不要**（`_stop_requested()` の PID 不一致判定が stale センチネルを自然に掃除する）。
- 各周回: `_stop_requested()` チェック（真なら exit_reason="stop requested" で break）→ `is_claude_running()`（偽なら exit_reason="claude gone" で break）→ `is_session_active` → `_reconcile_presence` → `_sleep_until_poll()`（真なら stop で break）。
- `finally`: `_drop_rpc(rpc)` → `lock.release()` → 自PID宛の STOP センチネルがあれば削除。exit_reason を finally 直前にログ。

**presence 調整の関数抽出:**

```python
def _reconcile_presence(active, presence_active, rpc):
    """RPC 状態機械の 1 ステップ。(rpc, presence_active) を返す。

    Phase A の3分岐（active&未表示→update / idle→clear / 継続→update）を
    そのまま移設。失敗時は _drop_rpc 経由。
    """
    return rpc, presence_active
```

`rpc` は引数で受け戻り値で返すため、`rpc` は依然 `_run_daemon` のローカルであり、Phase A の「`finally` が `rpc` を見られるようインライン」制約と両立する。`main()`/`_run_daemon` のループはこれを呼ぶだけになり、`_run_one_iteration` のループ再実装が不要になる。
- **呼び出し規約（必須）**: 呼び出し側は戻り値を必ず `rpc, presence_active = _reconcile_presence(...)` と**両方代入**すること。片方でも代入し忘れると `finally` が古い `rpc` を見てクリーンアップを取りこぼす。
- **保持すべき挙動**: Phase A の idle-clear 失敗パス（`clear()` 失敗時に presence を落とし `_drop_rpc` で接続を破棄→次周回で再接続）を**そのまま維持**する。抽出は構造の移設のみで挙動は不変。

### 4. `hook.py`

`subprocess.Popen` の失敗を握りつぶさず、**stderr にベストエフォートで1行**出力する（`configure_logging()` の回転ファイルハンドラは使わない — セクション1の「ログは単一ライター（デーモンのみ）」方針を守るため）。probe による起動スキップのログは付けない（YAGNI）。detached 起動フラグ・probe ロジック自体は Phase A のまま維持。

### 5. パス定数（`single_instance.py`）

`PID_FILE` の隣に `STOP_FILE = Path.home() / ".claude" / "claudecode-discord-presence.stop"` を追加。テストが `tmp_path` で差し替えられるようモジュール定数として公開。センチネルの**内容は対象デーモンの PID 文字列**（セクション2の STOP プロトコル参照）。

## テスト戦略

**削除/置換:**
- `TestMainLoopRpcErrors._run_one_iteration`（本番ループ再実装）→ 削除し、抽出した `_reconcile_presence` を直接テスト。
- `test_tasklist_oserror_returns_false` → `sys.platform` を `win32` に正しく patch した上で `subprocess.run` が `OSError` を投げるケースを検証する形に修正（現状はモックが `with` ブロック外で、非Windowsでは tasklist 分岐が未実行）。

**新規:**
- `_reconcile_presence` の4状態遷移を実コードで検証（update成功/失敗、clear成功/失敗）。特に **clear失敗時に `_drop_rpc` され rpc=None/presence=False** を明示検証。
- `--status`: ロック空き→`not running`、別 `InstanceLock` で占有中→`running (pid …)`。
- `_stop_requested` の PID マッチ:
  - センチネル無し→False。
  - センチネル内容==自PID→True。
  - センチネル内容!=自PID（例: 別PID/非数値）→False **かつセンチネルが削除される**（stale掃除）。
- **STOP 競合マトリクス**（Codex 指摘の窓を閉じたことの回帰テスト）:
  - デーモン非稼働で `--stop` → センチネルが作成されない、`not running` 出力。
  - 稼働中で `--stop` → センチネル作成、内容が対象PID。
  - 「旧PID宛センチネルが残存」状態で新デーモンの `_stop_requested()` → 停止せず、センチネルを掃除。
- `_sleep_until_poll`: センチネル（自PID宛）有りで即 True・無しで待機（`time.sleep` はモック）。
- `configure_logging`: `RotatingFileHandler` + stderr が設定され指定パスに出力される、多重呼び出しでハンドラが増えない（idempotent）。
- `finally` で自PID宛センチネルが削除される。

**方針:** `tmp_path` + パス定数注入で実ファイルを使い、モックは外部境界（`subprocess.run`、`time.sleep`、`Presence`）に限定。

## 完了の定義

- `logsetup.py` が追加され、**デーモンのみ** `configure_logging()`（RotatingFileHandler + stderr）を使う。CLI は stdout、hook 失敗は stderr。`print` は全廃。起動・ロック勝敗・RPC・終了理由がログに残る。ログのライターは常に単一（デーモン）。
- `--status` が稼働状態（時点プローブ）と PID/ログ/PID パスを報告、`--stop` が **稼働確認 → PID入りセンチネル作成**で稼働 main をクリーン停止させ、main が自PID宛センチネルを掃除する。非稼働時の `--stop` はセンチネルを作らない。
- ループが `POLL_INTERVAL_SEC=15` + 割り込み可能待機になり、`--stop` は約 `STOP_POLL_SEC=2` 秒で反応、Claude 終了検知/presence クリアも約15秒以内。
- `_reconcile_presence` が抽出され（両戻り値代入規約・idle-clear挙動を保持）、`_run_one_iteration` を撤去、`test_tasklist_oserror_returns_false` が修正され、STOP 競合マトリクスを含む全テスト green。

## 対象外

- CI・プラットフォーム方針・docs 整合（Phase C / #5）。
- 追加の presence 種別・アクティビティ内容の変更。

出典: Fable #2, #3(一部) / Codex main.py:105,:110,:243, hook.py:19, tests:394,:213,:144
