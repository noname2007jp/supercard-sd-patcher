# File: supercard_patcher.py
"""SuperCard SD Patcher - Windows 1ファイルEXE版 (公式FW向け)

supercard-archive/supercard-sd-patcher の patcher スクリプトを忠実に移植。
- gameid.c 相当: 0xAC から 6 バイトのゲームID読み出し
- trunc.c 相当: 末尾 0xFF のみを align=16 で切り詰め (パッチ適用前)
- haxdiff/1.0 形式パーサ:
    haxdiff/1.0
    @@ <offset_hex>,-<old_len>,+<new_len>
    - <old hex>
    + <new hex>
  旧データをROM現内容と照合してから書き込む (リビジョン違いを検出)
- MODE (s/S/p/r/t/T) に応じた diff 選択と .sav/.sci 出力
- .zip / .7z 入力の自動展開 (.7z は py7zr が必要)
- 番号付きSAV (sav/0286.sav 等) は --sav-file / GUI で手動指定可能

GUI: 引数なしで起動 / CLI: patcher と同じ引数形式 (MODE FILEIN FILEOUT [ROMSPEC])
"""
from __future__ import annotations

import argparse
import queue
import re
import sys
import tarfile
import tempfile
import threading
import tkinter as tk
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import py7zr  # .7z 入力用 (任意)
except Exception:
    py7zr = None


# ==================================================================
# gameid.c 相当
# ==================================================================
def read_gameid(rom_path: Path) -> str:
    with rom_path.open("rb") as f:
        f.seek(0xA0 + 12)          # 0xAC
        buf = f.read(6)            # ゲームコード4 + メーカーコード2
    if len(buf) != 6:
        raise ValueError("gameid の読み取りに失敗しました (GBA ROMではない可能性)")
    return buf.decode("ascii", "replace")


def read_title(rom_path: Path) -> str:
    with rom_path.open("rb") as f:
        f.seek(0xA0)
        buf = f.read(12)
    return buf.split(b"\0")[0].decode("ascii", "replace").strip()


# ==================================================================
# trunc.c 相当 (patcher は -a 16 で呼ぶ)。0xFF だけを削る点に注意
# ==================================================================
def trunc(data: bytes, align: int = 16) -> bytearray:
    n = len(data)
    if n == 0:
        return bytearray()
    p = n - 1
    while p > 0 and data[p] == 0xFF:
        p -= 1
    if 0 < p < n - 1:
        p += 1
    while p % align and p < n:
        p += 1
    return bytearray(data[:p])


# ==================================================================
# haxdiff/1.0 パーサ (実データで確定した形式に厳密対応)
# ==================================================================
@dataclass
class HaxRecord:
    offset: int
    old: bytes
    new: bytes


_HUNK_RE = re.compile(
    r"^@@\s+([0-9A-Fa-f]+)\s*,\s*-([0-9A-Fa-f]+)\s*,\s*\+([0-9A-Fa-f]+)\s*$")
_HEX_CHARS = re.compile(r"[^0-9A-Fa-f]")


def _unhex(s: str) -> bytes:
    s = _HEX_CHARS.sub("", s)
    if not s or len(s) % 2:
        raise ValueError(f"hex列が不正です: {s!r}")
    return bytes.fromhex(s)


def parse_haxdiff(text: str) -> tuple[list[HaxRecord], list[str]]:
    warnings: list[str] = []
    lines = text.splitlines()
    idx = 0
    if lines and lines[0].strip().lower().startswith("haxdiff/"):
        idx = 1

    records: list[HaxRecord] = []
    offset: int | None = None
    old = bytearray()
    new = bytearray()
    len_raw: tuple[str | None, str | None] = (None, None)

    def flush() -> None:
        nonlocal offset, old, new, len_raw
        if offset is None:
            return
        for raw, actual, tag in ((len_raw[0], len(old), "-"),
                                 (len_raw[1], len(new), "+")):
            if raw is None:
                continue
            # ヘッダの長さは基数未確定のため hex/dec 両方で照合し、不一致は警告のみ
            ok = int(raw, 16) == actual or (raw.isdigit() and int(raw, 10) == actual)
            if not ok:
                warnings.append(
                    f"0x{offset:X}: ヘッダ長 {tag}{raw} と実データ {actual}B が不一致")
        records.append(HaxRecord(offset, bytes(old), bytes(new)))
        offset = None

    for ln in lines[idx:]:
        s = ln.strip()
        if not s:
            continue
        m = _HUNK_RE.match(s)
        if m:
            flush()
            offset = int(m.group(1), 16)
            old, new = bytearray(), bytearray()
            len_raw = (m.group(2), m.group(3))
            continue
        if offset is not None and s.startswith("-"):
            old += _unhex(s[1:])
            continue
        if offset is not None and s.startswith("+"):
            new += _unhex(s[1:])
            continue
        raise ValueError(f"haxdiff の行を解釈できません: {s!r}")
    flush()
    if not records:
        raise ValueError("haxdiff にレコードがありません")
    return records, warnings


def apply_haxdiff(rom: bytearray, diff_path: Path, log) -> None:
    raw = diff_path.read_bytes()
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError(
            f"{diff_path.name}: テキスト形式の haxdiff ではありません。"
            f"先頭バイト: {raw[:32].hex()}")
    records, warnings = parse_haxdiff(text)
    for w in warnings:
        log(f"  [注意] {w}")
    appended = 0
    for rec in records:
        end = rec.offset + len(rec.new)
        # 追加（旧データ0バイト、または旧データがROM末尾以降）の場合
        if len(rec.old) == 0 or rec.offset >= len(rom):
            if end > len(rom):
                rom.extend(b"\xff" * (end - len(rom)))
            rom[rec.offset:end] = rec.new
            appended += len(rec.new)
            continue
        # 置換の場合: 旧データを照合
        cur = bytes(rom[rec.offset:rec.offset + len(rec.old)])
        if cur != rec.old:
            raise ValueError(
                f"{diff_path.name}: オフセット 0x{rec.offset:X} の内容が前提と一致しません"
                f" (期待 {rec.old.hex()} / 実際 {cur.hex()})"
                " → ROMのリビジョン違いの可能性があります")
        rom[rec.offset:rec.offset + len(rec.new)] = rec.new
    log(f"           -> {len(records)} ハンク適用"
        + (f" (末尾に {appended:,} バイト追加)" if appended else ""))


# ==================================================================
# MODE 解析 (patcher の case 文を忠実に再現)
#   常に 0.diff / p:+1 / r,t,T:+2 / t,T:+3
#   SAV は常に出力 (S なら more.sav) / SCI は t:isave.sci, T:more.sci
# ==================================================================
@dataclass
class ModeSpec:
    patches: list[int]
    sav: str
    sci: str | None


def parse_mode(mode: str) -> tuple[ModeSpec, list[str]]:
    unknown = sorted(set(mode) - set("sSprtT"))
    patches = [0, 1] if "p" in mode else [0]
    if set(mode) & set("rtT"):
        patches.append(2)
    if set(mode) & set("tT"):
        patches.append(3)
    sav = "more.sav" if "S" in mode else "save.sav"
    sci = "more.sci" if "T" in mode else ("isave.sci" if "t" in mode else None)
    return ModeSpec(patches, sav, sci), unknown


# ==================================================================
# リソース解決 (EXE内蔵 -> EXE隣 の順)
# ==================================================================
def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def find_data_file(rel: str) -> Path | None:
    cands = [resource_root() / rel]
    if getattr(sys, "frozen", False):
        cands.append(Path(sys.executable).resolve().parent / rel)
    for c in cands:
        if c.exists():
            return c
    return None


def default_patches_dir() -> Path:
    return find_data_file("patches") or (resource_root() / "patches")


# ==================================================================
# アーカイブ入力 (.zip / .7z) — patcher の 7z 展開相当
# ==================================================================
def depack_if_archive(filein: Path, workdir: Path, log) -> Path:
    suf = filein.suffix.lower()
    if suf == ".zip":
        with zipfile.ZipFile(filein) as zf:
            zf.extractall(workdir)
    elif suf == ".7z":
        if py7zr is None:
            raise RuntimeError(
                ".7z 入力には py7zr が必要です (build_exe.bat で同梱されます)")
        with py7zr.SevenZipFile(filein) as zf:
            zf.extractall(workdir)
    else:
        return filein
    gbas = sorted({*workdir.rglob("*.gba"), *workdir.rglob("*.GBA")})
    if not gbas:
        raise ValueError("アーカイブ内に .gba が見つかりません")
    if len(gbas) > 1:
        log(f"  [注意] 複数の .gba を検出 -> {gbas[-1].name} を使用")
    return gbas[-1]


# ==================================================================
# 例外
# ==================================================================
class MultipleMatchesError(Exception):
    def __init__(self, candidates: list[str]):
        self.candidates = candidates
        super().__init__("multiple matching roms: " + " ".join(candidates))


# ==================================================================
# パッチ処理本体 (patcher スクリプトの移植)
# ==================================================================
def patch_rom(mode: str, filein: Path, fileout: Path,
              romspec: str | None = None, patches_dir: Path | None = None,
              sav_override: Path | None = None, log=print) -> Path:
    spec, unknown = parse_mode(mode)
    if unknown:
        log(f"  [注意] 未知のMODE文字は無視されます: {''.join(unknown)}")
    if fileout.suffix.lower() != ".gba":
        raise ValueError("FILEOUT は .gba で終わる必要があります")
    patches_dir = patches_dir or default_patches_dir()

    with tempfile.TemporaryDirectory() as tds:
        td = Path(tds)
        work = td / "in"
        work.mkdir()
        src = depack_if_archive(filein, work, log)
        if not src.exists():
            raise FileNotFoundError(f"入力ファイルがありません: {src}")

        rs = romspec or read_gameid(src)
        matches = sorted(p.name for p in patches_dir.glob(f"{rs}*.txz"))
        if len(matches) > 1:
            raise MultipleMatchesError([Path(m).stem for m in matches])
        if not matches:
            raise FileNotFoundError(
                f"パッチが見つかりません: ROM code {rs} ({patches_dir})")
        stem = Path(matches[0]).stem

        log(f"■ {filein.name}")
        log(f"  ROMSPEC : {stem} ({'指定' if romspec else '自動検出: ' + rs})")

        original_size = src.stat().st_size
        rom = trunc(src.read_bytes(), 16)
        log(f"  trunc   : {original_size:,} -> {len(rom):,} バイト (align 16)")

        with tarfile.open(patches_dir / matches[0], "r:xz") as tf:
            try:
                tf.extractall(td, filter="data")
            except TypeError:
                tf.extractall(td)

        for n in spec.patches:
            diff = td / stem / f"{n}.diff"
            if not diff.exists():
                log(f"  [注意] {stem}/{n}.diff が存在しません。"
                    f"このROMではMODE '{mode}' の該当機能は利用できません")
                continue
            log(f"  適用    : {stem}/{n}.diff")
            try:
                apply_haxdiff(rom, diff, log)
            except ValueError as e:
                if "レコードがありません" in str(e):
                    log(f"  [注意] {n}.diff は空のためスキップしました")
                    continue
                # オプション機能(1,2,3.diff)の不一致は警告でスキップ、基本パッチ(0.diff)はエラー
                if n > 0:
                    log(f"  [警告] {n}.diff の適用条件に一致しません。"
                        f"このROMでは該当機能はスキップされました: {e}")
                    continue
                raise

        fileout.parent.mkdir(parents=True, exist_ok=True)
        fileout.write_bytes(rom)
        log(f"  出力    : {fileout}")

        # SAV はオリジナル同様 常に出力 (sav_override で番号付きSAV等を指定可能)
        if sav_override:
            sav_bytes = Path(sav_override).read_bytes()
            src_name = Path(sav_override).name
        else:
            sav_path = find_data_file(f"sav/{spec.sav}")
            if sav_path:
                sav_bytes = sav_path.read_bytes()
                src_name = f"sav/{spec.sav}"
            else:
                sav_bytes = b"\xff" * (0x40000 if spec.sav == "more.sav" else 0x10000)
                src_name = "ブランク生成"
                log(f"  [注意] sav/{spec.sav} 未同梱 -> 0xFF ブランクSAVを生成")
        fileout.with_suffix(".sav").write_bytes(sav_bytes)
        log(f"  SAV出力 : {fileout.with_suffix('.sav').name}"
            f" ({len(sav_bytes) // 1024}KB, {src_name})")

        if spec.sci:
            sci_path = find_data_file(f"rts/{spec.sci}")
            if not sci_path:
                raise FileNotFoundError(
                    f"rts/{spec.sci} が見つかりません (t/T MODE には必須です)")
            fileout.with_suffix(".sci").write_bytes(sci_path.read_bytes())
            log(f"  SCI出力 : {fileout.with_suffix('.sci').name}")
    return fileout


# ==================================================================
# CLI (patcher と同じ引数形式)
# ==================================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="supercard-patcher",
        description="SuperCard SD パッチャー (公式FW向け) Windows版")
    ap.add_argument("mode", help="s/S/p/r/t/T の組み合わせ (例: spr)")
    ap.add_argument("filein", type=Path, help=".gba / .zip / .7z")
    ap.add_argument("fileout", type=Path, help="出力先 (.gba で終わること)")
    ap.add_argument("romspec", nargs="?", help="複数候補時の手動指定 (例: BPRE01_REV1)")
    ap.add_argument("--patches-dir", type=Path,
                    help="patches フォルダの場所 (既定: EXE内蔵)")
    ap.add_argument("--sav-file", type=Path,
                    help="出力SAVを指定ファイルで上書き (例: sav/0286.sav)")
    args = ap.parse_args(argv)
    try:
        patch_rom(args.mode, args.filein, args.fileout, args.romspec,
                  args.patches_dir, args.sav_file)
    except MultipleMatchesError as e:
        print("multiple matching roms, pass one of "
              + " ".join(e.candidates) + " as ROMSPEC parameter", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


# ==================================================================
# GUI
# ==================================================================
MODE_ITEMS = [
    ("s", "セーブ有効化 (64KB SAV)", True),
    ("S", "大容量セーブ (256KB SAV)", False),
    ("p", "いつでもセーブ (L+R+SELECT+A)", True),
    ("r", "リスタート (L+R+X+Y+A+B)", True),
    ("t", "リアルタイムセーブ (L+R+SELECT+B)", False),
    ("T", "大容量リアルタイムセーブ", False),
]


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("SuperCard SD Patcher (公式FW向け) Windows版")
        self.geometry("780x680")
        self.minsize(660, 560)
        self.files: list[Path] = []
        self.q: queue.Queue[tuple] = queue.Queue()
        self._build()
        self.after(100, self._drain)

    def _build(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Button(top, text="ROMを追加…", command=self._add).pack(side="left")
        ttk.Button(top, text="選択を削除", command=self._remove).pack(side="left", padx=4)
        ttk.Button(top, text="クリア", command=self._clear).pack(side="left")

        self.listbox = tk.Listbox(self, height=8, selectmode="extended")
        self.listbox.pack(fill="both", padx=8)

        mf = ttk.LabelFrame(self, text="MODE (パッチオプション)", padding=8)
        mf.pack(fill="x", padx=8, pady=4)
        self.mode_vars: list[tuple[str, tk.BooleanVar]] = []
        for i, (ch, label, default) in enumerate(MODE_ITEMS):
            v = tk.BooleanVar(value=default)
            self.mode_vars.append((ch, v))
            ttk.Checkbutton(mf, text=f"{ch} : {label}", variable=v
                            ).grid(row=i // 2, column=i % 2, sticky="w", padx=8)

        self.var_romspec = tk.StringVar()
        self.var_out = tk.StringVar(value=str(Path.home() / "Desktop"))
        self.var_pdir = tk.StringVar(value=str(default_patches_dir()))
        self.var_sav = tk.StringVar()

        grid = ttk.Frame(self, padding=8)
        grid.pack(fill="x")
        ttk.Label(grid, text="ROMSPEC:").grid(row=0, column=0, sticky="w")
        ttk.Entry(grid, textvariable=self.var_romspec).grid(
            row=0, column=1, sticky="ew", padx=4)
        ttk.Label(grid, text="空欄=自動").grid(row=0, column=2, sticky="w")

        ttk.Label(grid, text="出力先:").grid(row=1, column=0, sticky="w")
        ttk.Entry(grid, textvariable=self.var_out).grid(
            row=1, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="参照…",
                   command=lambda: self._choose_dir(self.var_out, "出力フォルダ")
                   ).grid(row=1, column=2)

        ttk.Label(grid, text="パッチDB:").grid(row=2, column=0, sticky="w")
        ttk.Entry(grid, textvariable=self.var_pdir).grid(
            row=2, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="参照…",
                   command=lambda: self._choose_dir(self.var_pdir, "patchesフォルダ")
                   ).grid(row=2, column=2)

        ttk.Label(grid, text="SAV上書き:").grid(row=3, column=0, sticky="w")
        ttk.Entry(grid, textvariable=self.var_sav).grid(
            row=3, column=1, sticky="ew", padx=4)
        ttk.Button(grid, text="参照…", command=self._choose_sav).grid(row=3, column=2)
        ttk.Label(grid, text="空欄=標準 (番号付きSAV用)",
                  foreground="#555").grid(row=4, column=1, sticky="w")
        grid.columnconfigure(1, weight=1)

        self.btn_run = ttk.Button(self, text="パッチ実行", command=self._run)
        self.btn_run.pack(pady=4)

        self.logw = tk.Text(self, height=12, state="disabled", wrap="none")
        self.logw.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    @staticmethod
    def _choose_dir(var: tk.StringVar, title: str) -> None:
        d = filedialog.askdirectory(title=title)
        if d:
            var.set(d)

    def _choose_sav(self) -> None:
        f = filedialog.askopenfilename(
            title="SAVファイルを選択 (番号付きSAV等)",
            filetypes=[("SAV", "*.sav *.SAV"), ("すべてのファイル", "*.*")])
        if f:
            self.var_sav.set(f)

    def _add(self) -> None:
        paths = filedialog.askopenfilenames(
            title="GBA ROMを選択",
            filetypes=[("対応ファイル", "*.gba *.agb *.bin *.zip *.7z"),
                       ("すべてのファイル", "*.*")])
        for p in paths:
            path = Path(p)
            if path in self.files:
                continue
            self.files.append(path)
            label = path.name
            if path.suffix.lower() in (".gba", ".agb", ".bin"):
                try:
                    label += f"  [{read_gameid(path)}]  {read_title(path)}"
                except Exception:
                    label += "  [ヘッダ解析失敗]"
            self.listbox.insert("end", label)

    def _remove(self) -> None:
        for i in reversed(self.listbox.curselection()):
            del self.files[i]
            self.listbox.delete(i)

    def _clear(self) -> None:
        self.files.clear()
        self.listbox.delete(0, "end")

    def _run(self) -> None:
        if not self.files:
            messagebox.showwarning("SuperCard SD Patcher", "ROMファイルを追加してください。")
            return
        self.btn_run.configure(state="disabled")
        threading.Thread(target=self._worker, daemon=True).start()

    def _mode_string(self) -> str:
        return "".join(ch for ch, v in self.mode_vars if v.get())

    def _worker(self) -> None:
        mode = self._mode_string()
        if not mode:
            self.q.put(("log", "[NG] MODE を1つ以上選択してください"))
            self.q.put(("enable",))
            return
        romspec = self.var_romspec.get().strip() or None
        sav_text = self.var_sav.get().strip()
        sav_override = Path(sav_text).expanduser() if sav_text else None
        outdir = Path(self.var_out.get()).expanduser()
        pdir_text = self.var_pdir.get().strip()
        pdir = Path(pdir_text).expanduser() if pdir_text else None
        ok = 0
        for src in list(self.files):
            try:
                fileout = outdir / f"{src.stem}.gba"
                if fileout.resolve() == src.resolve():
                    fileout = outdir / f"{src.stem}.patched.gba"
                patch_rom(mode, src, fileout, romspec, pdir, sav_override,
                          log=lambda m: self.q.put(("log", m)))
                ok += 1
            except MultipleMatchesError as e:
                self.q.put(("log", f"[要選択] {src.name}: 候補 = {' '.join(e.candidates)}"))
                self.q.put(("multi", src.name, e.candidates))
            except Exception as e:
                self.q.put(("log", f"[NG] {src.name}: {e}"))
        self.q.put(("log", f"--- 完了: {ok}/{len(self.files)} 件成功 ---"))
        self.q.put(("enable",))

    def _on_multi(self, src_name: str, cands: list[str]) -> None:
        win = tk.Toplevel(self)
        win.title("ROMSPEC を選択")
        win.grab_set()
        ttk.Label(win, text=f"{src_name} に複数のパッチ候補があります。\n"
                            "使用する ROMSPEC を選んでください:",
                  justify="left").pack(padx=10, pady=(10, 4))
        lb = tk.Listbox(win, width=44, height=min(10, len(cands)))
        for c in cands:
            lb.insert("end", c)
        lb.selection_set(0)
        lb.pack(padx=10)

        def ok() -> None:
            sel = lb.curselection()
            if sel:
                self.var_romspec.set(lb.get(sel[0]))
            win.destroy()

        ttk.Button(win, text="この ROMSPEC を使う", command=ok).pack(pady=8)

    def _drain(self) -> None:
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "enable":
                    self.btn_run.configure(state="normal")
                    continue
                if kind == "multi":
                    self._on_multi(msg[1], msg[2])
                    continue
                self.logw.configure(state="normal")
                self.logw.insert("end", msg[1] + "\n")
                self.logw.see("end")
                self.logw.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._drain)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit(main())
    App().mainloop()
