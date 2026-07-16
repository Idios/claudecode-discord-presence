# Phase A: 単一インスタンス保証をアトミックOSロック化する設計

- **Issue**: #3 — Phase A: 単一インスタンス保証をアトミックOSロック化して重複プロセスを根絶
- **日付**: 2026-07-16
- **スコープ**: 単一インスタンス保証のアトミック化 + RPCソケットリーク修正（Issueの「同時対応推奨」に従う）

## 背景と真因

現状の `is_already_running()`（確認）→ `write_pid_file()`（書き込み）は**アトミックでない（TOCTOU）**。SessionStart hook が同時発火すると、複数の子プロセスが揃って「起動していない」と判定して二重起動する。PIDファイルは後勝ちで上書きされ、記録されなかった側は**追跡外の孤児**として残る（メインループは `claude.exe` が消えるまで終了しないため不死身化）。これが「3時間で33プロセス累積」の根本原因。

派生問題（すべて現行設計の症状）:

- **PID再利用**: 記録PIDが無関係プロセスに再割当てされると誤検知し、ツールが起動しなくなる。
- **stale PID / 再起動残骸**: クラッシュ/再起動後に古いPIDファイルが残り、手動削除が必要（READMEに手動削除手順がある＝設計の症状）。
- **危険な `os.kill(pid, 0)`**: Windowsでは Python の `os.kill` が `TerminateProcess` を呼ぶため、生存確認のつもりで**対象プロセスをkillしてしまう**。
- **detached プロセスに SIGTERM が届かない**ため、`signal` ベースの shutdown ハンドラは事実上デッドコード。presence/PID が残留する。

## 中核方針

「確認してから書く」をやめ、**メインプロセス自身がアトミックなOSロックを取得し、取れなかった側は即終了する**設計にする。プロセスが**どのような終わり方（正常/クラッシュ/kill）をしてもOSがロックを自動解放する**ので、stale・PID再利用・生存ポーリングが原理的に不要になる。

## 設計

### 1. ロックプリミティブ（新規モジュール `single_instance.py`）

ロック取得ロジックを main.py から切り出し、単体テスト可能な小さな単位にする。

```python
class InstanceLock:
    """Hold an exclusive OS lock on a file for the process lifetime."""
    def __init__(self, path: Path): ...
    def acquire(self) -> bool:   # 非ブロッキング。取れたら True、既に他が保持なら False
    def release(self) -> None:   # ロック解放 + ファイルハンドルclose + PIDファイル削除
```

- **ロックファイル = PIDファイルを兼用**（`~/.claude/claudecode-discord-presence.pid`）。「開く → ロック → 自PIDを書き込む」を1つの流れに統合する。別ファイルは増やさない。Phase B の `--stop`/`--status` 用にPIDも残る。
- **POSIX**: `fcntl.flock(fd, LOCK_EX | LOCK_NB)`
- **Windows**: `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)`
- どちらも失敗時に例外を送出 → `acquire()` は `False` を返す。
- **ファイルハンドル（fd）は `InstanceLock` インスタンスが保持し続ける**。GC でクローズされるとロックが解放されてしまうため、`main()` のスコープでインスタンスを生存させる。
- **ロックが真実の源**。PIDファイルの内容が古くても（stale）ロック状態が正しければ問題にならない。手動削除は不要になる。
- `path` は引数で注入可能にする（テストが `tmp_path` を使い、hook が同じパスを共有できるように）。

### 2. `main()` の書き換え

**削除:**

- `is_already_running()`（確認側）
- `write_pid_file()`（`InstanceLock.acquire()` に統合）
- `is_process_alive()`（危険な `os.kill(pid, 0)`。新設計では不要）
- `signal` ベースの `shutdown` ハンドラ（detached プロセスに SIGTERM は届かずデッドコード）

**新しい構造:**

```python
def main():
    lock = InstanceLock(PID_FILE)
    if not lock.acquire():
        print("Another instance is already running. Exiting.")
        sys.exit(0)
    try:
        run_loop()          # 監視ループ本体
    finally:
        # best-effort クリーンアップ（例外・正常終了どちらでも通る）
        _drop_rpc(rpc)      # clear + close
        lock.release()      # ロック解放 + PIDファイル削除
```

- 「Claude Code が消えたら終了」「アイドルで presence クリア」等のループ判定ロジックは維持する。
- `finally` が唯一のクリーンアップ経路になり、正常 exit（Claude 消滅・アイドル終了）も例外もカバーする。
- フォアグラウンド実行での Ctrl+C は `KeyboardInterrupt` → `finally` で綺麗に片付く。SIGINT ハンドラは撤去し、`KeyboardInterrupt` を捕捉する形にする。

### 3. RPCソケットリーク修正

**現状の問題**（main.py の update/clear/connect 各所）:

- 失敗時に `rpc.close()` を呼ばず `rpc = None` で捨てる → ソケットがリーク。
- `clear()` 失敗を握りつぶして `presence_active = False` にする → 古い presence が残ったまま再試行されない。

**修正**: ヘルパーに集約する。

```python
def _drop_rpc(rpc):
    """best-effort に clear + close して接続を完全に手放す。"""
    if rpc is not None:
        try: rpc.clear()
        except Exception: pass
        try: rpc.close()
        except Exception: pass
    return None
```

ループ内の各失敗パスを統一する:

- **`update()` 失敗**（新規セッション時・継続時どちらも）→ `rpc = _drop_rpc(rpc)`、`presence_active = False`。次ループで再接続。
- **`clear()` 失敗**（アイドル遷移時）→ 握りつぶさず `rpc = _drop_rpc(rpc)`、`presence_active = False`。接続を落として次ループで再接続するので、古い presence が残り続けない。
- **Claude 消滅時の終了パス** → `finally` の `_drop_rpc(rpc)` に集約（正常時はここで一度だけ clear + close）。

原則: 「捨てる時は必ず close する」「clear 失敗時は接続を作り直す」。

### 4. `hook.py` の変更

`is_already_running()` の import/呼び出しを撤去し、ロックベースの軽量チェックに置き換える。これは**保証ではなく最適化**（保証は `main` のロックが担う）。既存インスタンスがある時に、死ぬだけの Python 子プロセスを無駄に起動しないためのもの。

```python
def main():
    probe = InstanceLock(PID_FILE)
    if not probe.acquire():
        return              # 既存インスタンスあり → 起動しない
    probe.release()         # すぐ解放し、本物の main に取らせる
    subprocess.Popen([...], ...)   # 既存の detached 起動はそのまま
```

- `os.kill` は一切使わない。
- race（probe 解放 → main 取得の隙間、または2つの hook が同時通過）があっても、`main` のロックが最終保証となるため単一インスタンスは崩れない。

## テスト戦略（TDD）

この順で実装する。

1. **統合テスト（アンカー・最初に書く）** — 「N個のプロセスが同時にロック取得を試みる → 成功は正確に1個」。
   - 実装: 小さなワーカー（`multiprocessing`、またはパスを env/argv で受ける短いスクリプト）を N 個起動。各ワーカーは `InstanceLock(tmp_path).acquire()` を呼び、成功後に短時間 sleep して全員が同時に競合する状態を作る → 成功/失敗を報告。親が集計し **成功数 == 1** を assert する。
   - このテストがあれば元の「3時間で33プロセス」バグは初日に検出できた。
2. **単体テスト（`single_instance.py`）:**
   - 1回目 `acquire()` は `True`、同じファイルへの2つ目の `InstanceLock.acquire()` は `False`。
   - `release()` 後は再度 `acquire()` が `True`。
   - PIDファイルに自PIDが書き込まれている。
3. **RPCリーク:** 各失敗パスで `close()` が呼ばれること、`clear()` 失敗時に接続が破棄されることを mock で検証（既存の `TestMainLoopRpcErrors` を拡張）。
4. **既存テストの整理:** `is_process_alive` / `is_already_running` / `write_pid_file` 関連テストは、対象の削除に合わせて削除・置換する。

## 完了の定義

- `single_instance.py` が追加され、POSIX/Windows 両方でファイルロックを実装している。
- `main()` がロック取得→`try/finally`クリーンアップ構造になり、`is_process_alive` / `is_already_running` / `write_pid_file` / signal ハンドラが削除されている。
- RPC 失敗パスがすべて `_drop_rpc` 経由で close され、`clear()` 失敗時に再接続する。
- `hook.py` がロックベースの軽量チェックを使い、`os.kill` を使わない。
- 統合テスト「N同時起動 → 生存1個」を含む全テストがグリーン。

## 対象外（このPhaseでは扱わない）

- ログ出力、`--status`/`--stop` CLI、テスト再構成（Phase B / Issue #4）
- CI・プラットフォーム方針・docs整合（Phase C / Issue #5）

出典: Fable(構造レビュー) #1,#5,#6 / Codex(技術レビュー) main.py:172, :163, :192, :223, :226
