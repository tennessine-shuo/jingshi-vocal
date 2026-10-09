# -*- coding: utf-8 -*-
"""声纹「重新录制 / 补录本地库」回归测试。

零费用、不联网、不开麦、不动真实声纹库 —— 全部在临时目录里造数据。
模型也不加载：用 object.__new__ 绕过 __init__ 并给 embed_wav 打桩，
测的是替换语义、备份、差集检测这些**逻辑**，不是模型精度。

覆盖：
  ① LocalVoiceprint.enroll 的追加 vs 替换语义
  ② replace / remove 前的整库快照
  ③ 云端 registry 与本地库的差集（即「仅在云端不在本地」那个现象）
  ④ 声纹工具 GUI 离屏构建 + 重录模式状态机

跑法：C:\\recorder\\venv\\Scripts\\python.exe tools\\test_voiceprint_reenroll.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(r"C:\recorder")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "cloud"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FAILS: list[str] = []
OKS = 0


def check(cond: bool, label: str, detail: str = "") -> bool:
    global OKS
    if cond:
        OKS += 1
        print(f"  [ok] {label}")
    else:
        FAILS.append(label)
        print(f"  [XX] {label}" + (f" —— {detail}" if detail else ""))
    return bool(cond)


def make_wav(path: Path, sec: float = 3.0) -> Path:
    """造一段 16k/mono/16bit 的静音 wav（只为骗过时长统计，内容不参与打分）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * sec))
    return path


def stub_db(tmp: Path, vectors: dict[str, list[float]]):
    """构造一个不加载模型的 LocalVoiceprint：向量按 wav 文件名查表返回。"""
    import voiceprint_local as V

    db = object.__new__(V.LocalVoiceprint)
    db.model_path = "stub.onnx"
    db.db_path = tmp / "voiceprint_local_db.json"
    db.dim = 4
    db.persons = {}

    def fake_embed(wav_path):
        key = Path(wav_path).name
        vec = np.asarray(vectors.get(key, [0.5, 0.5, 0.5, 0.5]), dtype=np.float32)
        return vec / np.linalg.norm(vec)

    db.embed_wav = fake_embed
    return db


# ---------------------------------------------------------------- ① 语义

def test_enroll_semantics(tmp: Path) -> None:
    print("\n=== ① 追加 vs 替换 ===")
    a = make_wav(tmp / "a.wav")
    b = make_wav(tmp / "b.wav")
    db = stub_db(tmp, {"a.wav": [1, 0, 0, 0], "b.wav": [0, 1, 0, 0]})

    r1 = db.enroll("T001", "汤朔", "teacher", [str(a)])
    check(r1["added"] == 1 and r1["total_embeddings"] == 1,
          "首次注册计入 1 段", str(r1))
    check(r1["replaced"] is False, "首次注册不算替换")

    r2 = db.enroll("T001", "汤朔", "teacher", [str(b)])
    check(r2["total_embeddings"] == 2 and r2["replaced"] is False,
          "默认是追加（2 段）", str(r2))

    r3 = db.enroll("T001", "汤朔", "teacher", [str(b)], replace=True)
    check(r3["replaced"] is True and r3["dropped"] == 2,
          "替换语义上报弃用 2 段", str(r3))
    check(r3["total_embeddings"] == 1,
          "替换后只剩新样本 1 段", str(r3))
    check(db.persons["T001"]["embeddings"][0][1] == 1.0,
          "留下的是新样本向量（[0,1,0,0]）",
          str(db.persons["T001"]["embeddings"][0]))

    r4 = db.enroll("S002", "Lucas", "student", [str(a)], replace=True)
    check(r4["replaced"] is False,
          "对不存在的人用 replace 不应误报替换", str(r4))


# ---------------------------------------------------------------- ② 备份

def test_backup(tmp: Path) -> None:
    print("\n=== ② 破坏性操作前备份 ===")
    a = make_wav(tmp / "a.wav")
    db = stub_db(tmp, {"a.wav": [1, 0, 0, 0]})
    db.enroll("T001", "汤朔", "teacher", [str(a)])

    r = db.enroll("T001", "汤朔", "teacher", [str(a)], replace=True)
    bak = Path(r["backup"]) if r["backup"] else None
    check(bool(bak) and bak.exists(), "替换时生成了整库快照", str(bak))
    if bak and bak.exists():
        snap = json.loads(bak.read_text(encoding="utf-8"))
        check("T001" in (snap.get("persons") or {}),
              "快照里含替换前的人员")
        check(bak.parent.name == "voiceprint_local_backup",
              "快照落在 voiceprint_local_backup/", str(bak.parent))

    before = len(list((tmp / "voiceprint_local_backup").glob("*.json")))
    db.remove("T001")
    after = len(list((tmp / "voiceprint_local_backup").glob("*.json")))
    check(after == before + 1, "remove 也会先拍快照", f"{before} → {after}")

    db2 = stub_db(tmp / "fresh", {"a.wav": [1, 0, 0, 0]})
    r2 = db2.enroll("S001", "袁楚然", "student", [str(a)], replace=True)
    check(r2["backup"] == "" and not (tmp / "fresh" / "voiceprint_local_backup").exists(),
          "对新人 replace 不产生多余快照（库文件都还没存在）")


# ---------------------------------------------------------------- ③ 差集

def test_cloud_local_diff() -> None:
    print("\n=== ③ 云端 / 本地差集（只读真实数据）===")
    import voiceprint_enroll_gui as G

    cloud_cfg = json.loads(G.CLOUD_CONFIG.read_text(encoding="utf-8"))
    reg_path = Path(cloud_cfg.get("voiceprint", {}).get(
        "registry_file", str(G.CLOUD_DIR / "voiceprint_registry.json")))
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    local = G.read_local_persons(cloud_cfg)

    cloud_ids = {v.get("person_id") for v in reg.values() if isinstance(v, dict)}
    missing = sorted(i for i in cloud_ids if i not in local)
    print(f"  云端 {len(cloud_ids)} 人：{sorted(cloud_ids)}")
    print(f"  本地 {len(local)} 人：{sorted(local)}")
    print(f"  仅在云端（本地缺失）：{missing or '无'}")

    check(all(i in cloud_ids for i in local),
          "本地库不出现云端没有的人（本地是云端的子集或相等）")
    check(isinstance(local, dict) and all(
        isinstance(v.get("n_samples"), int) for v in local.values()),
        "read_local_persons 返回结构正确", str(local))
    # 差集本身随使用变化，不做数值断言；只保证口径自洽。
    print(f"  → 本次差集 {len(missing)} 人（工具会自动在列表里标 ✗）")


# ---------------------------------------------------------------- ④ GUI

def test_gui(tmp: Path) -> None:
    print("\n=== ④ 声纹工具 GUI（离屏）===")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    import voiceprint_enroll_gui as G

    win = G.MainWindow()
    win.show()
    app.processEvents()

    check(win.table.columnCount() == 6, "列表 6 列（含本地库 / 留存样本）",
          str(win.table.columnCount()))
    check(win.table.rowCount() == len(win._load_registry()),
          "列表行数 = 云端注册人数")
    check(hasattr(win, "missing"), "已算出云端/本地差集",
          str(getattr(win, "missing", None)))

    reg = win._load_registry()
    vpid = sorted(reg.items(), key=lambda kv: kv[1].get("person_id", ""))[0][0]
    info = reg[vpid]

    # --- 编辑对话框应带「重新录制」入口 ---
    dlg = G.EditPersonDialog(win, info, vpid=vpid, local_n=0)
    check(hasattr(dlg, "btn_rerecord"), "编辑对话框有重新录制按钮")
    check(G.EditPersonDialog.RERECORD == 2,
          "重录用独立返回码，不会与 Accepted/Rejected 混用")
    dlg.reject()

    # --- 进入重录模式 ---
    win._enter_rerecord(vpid, info)
    app.processEvents()
    check(win.mode == "rerecord", "mode 切到 rerecord")
    check(win.target and win.target["vpid"] == vpid, "记录了重录目标")
    check(not win.ed_name.isEnabled() and not win.ed_pid.isEnabled()
          and not win.cb_role.isEnabled(),
          "身份三项在重录期间被锁定")
    check(win.ed_pid.text() == info.get("person_id", ""),
          "人员 ID 未被 _suggest_person_id 冲掉", win.ed_pid.text())
    check("保存新声纹" in win.btn_enroll.text(),
          "保存按钮文案已变形", win.btn_enroll.text())
    check(win.banner.isVisibleTo(win), "重录横幅可见")
    check(win.chk_local.isVisibleTo(win), "「同时写本地库」勾选项出现")
    check(win.lbl_domain.isVisibleTo(win), "域风险提示可见")

    # 重录模式下 _suggest_person_id 必须罢工
    pid_before = win.ed_pid.text()
    win._suggest_person_id()
    check(win.ed_pid.text() == pid_before, "重录模式下不自动改写人员 ID")

    # --- 退出重录模式 ---
    win._exit_rerecord()
    app.processEvents()
    check(win.mode == "new" and win.target is None, "回到新注册模式")
    check(win.ed_name.isEnabled() and win.ed_pid.isEnabled()
          and win.cb_role.isEnabled(), "身份三项解锁")
    check(not win.banner.isVisibleTo(win), "横幅收起")
    check(not win.chk_local.isVisibleTo(win), "勾选项收起")
    check(win.chk_local.isChecked() is False, "勾选项被清空（避免误带进下次注册）")
    check("注册声纹" in win.btn_enroll.text(), "按钮文案复原", win.btn_enroll.text())
    check(win.ed_name.text() == "", "表单已清空")

    # --- 补录入口存在 ---
    check(hasattr(win, "btn_supplement") and hasattr(win, "btn_fill_all"),
          "补录按钮已挂上")
    check(win.btn_fill_all.isEnabled() and win.btn_supplement.isEnabled(),
          "补录按钮默认可用")

    # --- 选文件的起始目录应指向该人的课堂片段（远场同域）---
    clip_dirs = sorted(d for d in G.CLASSROOM_DIR.glob("*-*")
                       if d.is_dir() and any(d.glob("vp_*.wav")))
    if clip_dirs:
        who = clip_dirs[0].name.split("-")[0]
        got = G.MainWindow._clip_start_dir(who)
        check(got == clip_dirs[0], f"起始目录命中课堂片段目录（{who}）", str(got))
    d2 = G.MainWindow._clip_start_dir("绝不存在的名字ZZZ")
    check(d2.name == "enroll_samples",
          "没有课堂目录时退回 enroll_samples", str(d2))

    win.close()


def main() -> int:
    print("声纹重录 / 补录本地库 回归测试")
    tmp = Path(tempfile.mkdtemp(prefix="vp_test_"))
    try:
        test_enroll_semantics(tmp)
        test_backup(tmp)
        test_cloud_local_diff()
        test_gui(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 46}")
    print(f"通过 {OKS} 项，失败 {len(FAILS)} 项")
    for f in FAILS:
        print(f"  ✗ {f}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
