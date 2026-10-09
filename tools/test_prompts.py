# -*- coding: utf-8 -*-
"""提示词模块回归测试（零费用、不调大模型、不发网络请求）。

改 prompts.py / report.py / meeting_summary.py 之后先跑这个。
覆盖：
  1. 新旧渲染**逐字一致** —— 迁移不能悄悄改变线上报告内容（最关键）
  2. 单花括号写法可直接用（降低用户编辑门槛）
  3. 未知占位符原样保留，不吞不炸
  4. 校验能拦住「删掉必需占位符」「空内容」
  5. 保存 → 热生效 → 恢复默认 的完整往返
用法：C:/recorder/venv/Scripts/python.exe tools/test_prompts.py
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(r"C:\recorder")
sys.path.insert(0, str(ROOT / "cloud"))

import prompts as P  # noqa: E402

OK = BAD = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global OK, BAD
    if cond:
        OK += 1
        print(f"  [OK]   {label}")
    else:
        BAD += 1
        print(f"  [FAIL] {label}  {extra}")


def _snap_backups() -> set[str]:
    """记录 cloud/prompts_backup/ 现有文件名。

    save() 每次保存都会往这里写一份备份，测试会反复保存 → 留下噪声备份。
    先拍快照，测完只删自己产生的那些，不动用户真实编辑留下的备份。
    """
    d = P.PROMPTS_FILE.parent / "prompts_backup"
    return {p.name for p in d.glob("*")} if d.exists() else set()


def _clean_backups(snap: set[str]) -> int:
    d = P.PROMPTS_FILE.parent / "prompts_backup"
    if not d.exists():
        return 0
    n = 0
    for p in d.glob("*"):
        if p.name not in snap:
            p.unlink()
            n += 1
    return n


def main() -> int:
    vals = {"meta": '{"student_name": "Lucas Zhang"}', "stats": '{"n": 96}',
            "transcript": "[汤朔 0-14s] 今天讲牛顿第二定律。\n[Lucas Zhang 15-22s] 合力怎么算？"}

    print("=" * 74)
    print("1) 迁移一致性：新 render() 必须与旧写法逐字相同")
    print("=" * 74)

    tpl = P.DEFAULTS["teaching_eval"]["text"]
    old = tpl.format(**vals)
    new = P.render(tpl, {**vals, "transcript": vals["transcript"]})
    check("教评报告主提示词（旧 .format vs 新 render）", old == new,
          f"长度 {len(old)} vs {len(new)}")

    tpl2 = P.DEFAULTS["meeting_minutes"]["text"]
    old2 = tpl2.format(meta=vals["meta"], transcript=vals["transcript"])
    new2 = P.render(tpl2, {"meta": vals["meta"], "transcript": vals["transcript"]})
    check("会议纪要提示词", old2 == new2, f"长度 {len(old2)} vs {len(new2)}")

    old3 = ("你是教研专家，请对以下课堂片段输出要点摘要（讲授了哪些知识点、"
            "学生的错误与疑问、各环节大致时长）。\nABC")
    new3 = P.render(P.DEFAULTS["chunk_summary"]["text"], {"chunk": "ABC"})
    check("长课分片摘要（旧字符串拼接 vs 新 render）", old3 == new3,
          f"旧 {old3[-24:]!r} 新 {new3[-24:]!r}")

    # 文字版骨架：与 report.write_eval_txt 迁移前的 lines 拼接结果比对
    old4 = "\n".join([
        "教评报告  2026年9月15日  Lucas Zhang  IG物理", "",
        "上课内容", "", "CONTENT", "",
        "知识小结", "", "KNOW",
    ])
    new4 = P.render(P.DEFAULTS["eval_txt"]["text"], {
        "date_cn": "2026年9月15日", "student": "Lucas Zhang", "course": "IG物理",
        "content_text": "CONTENT", "knowledge_text": "KNOW"})
    check("文字版报告骨架（旧 lines 拼接 vs 新 render）", old4 == new4,
          f"\n     旧 {old4!r}\n     新 {new4!r}")

    print()
    print("=" * 74)
    print("2) 占位符宽容度")
    print("=" * 74)

    single = '{"content_text": "{transcript}", "n": {count_like}}'
    r = P.render(single, {"transcript": "T"})
    check("单花括号 JSON 无需转义即可用", r == '{"content_text": "T", "n": {count_like}}', repr(r))

    double = '{{"content_text": "{transcript}"}}'
    check("双花括号（旧写法）仍兼容",
          P.render(double, {"transcript": "T"}) == '{"content_text": "T"}',
          repr(P.render(double, {"transcript": "T"})))

    check("未知占位符原样保留（不炸、不吞）",
          P.render("a{未知}b{meta}", {"meta": "M"}) == "a{未知}bM",
          repr(P.render("a{未知}b{meta}", {"meta": "M"})))

    print()
    print("=" * 74)
    print("3) 保存前校验")
    print("=" * 74)

    ok, why = P.validate("teaching_eval", "只写一句话，没有任何占位符")
    check("缺必需占位符 → 拒绝保存", not ok, why)
    check("  错误信息点出缺了谁", "transcript" in why, why)

    ok, why = P.validate("teaching_eval", "   ")
    check("空内容 → 拒绝保存", not ok, why)

    lean = '规则同上。\n\n转写：\n{transcript}'
    ok, why = P.validate("teaching_eval", lean)
    check("只缺可选数据（meta/stats）→ 允许保存但提醒",
          ok and "未使用" in why, why)

    eval_lean = "{content_text}"
    ok, why = P.validate("eval_txt", eval_lean)
    check("骨架只留正文、不要日期学生名 → 允许保存",
          ok, why)

    good = P.DEFAULTS["teaching_eval"]["text"]
    ok, why = P.validate("teaching_eval", good)
    check("默认词 → 通过且无警告", ok and not why, why)

    thin = good.replace('"content_text"', '"上课内容"')
    ok, why = P.validate("teaching_eval", thin)
    check("删掉 JSON 字段名 → 通过但给警告", ok and "字段名未出现" in why, why)

    bad_brace = good + '\n\n参考格式：{ "a": 1'   # 故意多一个未闭合的 {
    ok, why = P.validate("teaching_eval", bad_brace)
    check("花括号不配平 → 通过但给警告", ok and "花括号" in why, why)

    print()
    print("=" * 74)
    print("4) 保存 → 热生效 → 恢复默认")
    print("=" * 74)

    src = P.PROMPTS_FILE
    backup = src.with_suffix(".bak_test")
    had = src.exists()
    snap = _snap_backups()
    if had:
        shutil.copy2(src, backup)
    try:
        base = P.get_prompt("teaching_eval")
        check("初始取到默认词", "资深教研专家" in base)

        edited = base + "\n\n补充要求：若课堂有提问，单独列出学生的高频困惑。"
        ok, msg = P.save("teaching_eval", edited)
        check("保存成功", ok, msg)
        check("保存后立即热生效（无需重启）",
              "高频困惑" in P.get_prompt("teaching_eval"))
        st = P.status()
        check("状态标记为「已修改」",
              any(i["key"] == "teaching_eval" and i["modified"] for i in st["items"]),
              json.dumps(st["items"], ensure_ascii=False))
        check("落盘文件可被 json 解析且含该模板",
              src.exists() and "teaching_eval" in json.loads(
                  src.read_text(encoding="utf-8"))["prompts"])

        ok, msg = P.save("teaching_eval", "缺占位符的坏词")
        check("坏词被拒绝，且不覆盖已存的好词",
              (not ok) and "高频困惑" in P.get_prompt("teaching_eval"), msg)

        ok, msg = P.reset("teaching_eval")
        check("恢复默认成功", ok, msg)
        check("恢复后内容回到默认",
              P.get_prompt("teaching_eval") == base)
        check("状态不再标记已修改",
              not any(i["key"] == "teaching_eval" and i["modified"]
                      for i in P.status()["items"]))
    finally:
        if had:
            shutil.copy2(backup, src)
            backup.unlink()
        elif src.exists():
            src.unlink()
        _clean_backups(snap)
        P.load(force=True)

    print()
    print("=" * 74)
    print("5) 端到端：改完提示词，报告生成真的用新词（mock 大模型，零费用）")
    print("=" * 74)

    import report as R      # noqa: E402
    import meeting_summary as MS  # noqa: E402

    calls: list[str] = []
    fake = ('{"content_text": "OK", "knowledge_text": "K", "summary": "S",'
            ' "learning_progress": {"overall": 1, "topics": []},'
            ' "error_analysis": [], "knowledge_mastery": [], "time_allocation": []}')
    R._llm_chat = lambda messages, ctx: (calls.append(messages[0]["content"]), fake)[1]
    MS._llm_chat = R._llm_chat

    segs = [{"person_name": "汤朔", "start_sec": 0, "end_sec": 10,
             "text": "今天讲牛顿第二定律"}]
    meta = {"student_name": "Lucas Zhang", "course_type": "IG物理",
            "start": "2026-09-15T16:30:00"}

    rep = R.generate_report(segs, {"n": 1}, meta, {"config": {}})
    check("转写文本已注入发给大模型的词里", "今天讲牛顿第二定律" in calls[-1])
    check("课程元数据同样注入", "Lucas Zhang" in calls[-1])
    txt = R.write_eval_txt(rep, meta)
    check("文字版报告按骨架渲染",
          txt.startswith("教评报告") and "上课内容" in txt and "OK" in txt,
          repr(txt[:60]))

    src2 = P.PROMPTS_FILE
    bak2 = src2.with_suffix(".bak_test2")
    had2 = src2.exists()
    snap2 = _snap_backups()
    if had2:
        shutil.copy2(src2, bak2)
    try:
        base_tpl = P.get_prompt("teaching_eval")
        ok, msg = P.save("teaching_eval", base_tpl + "\n\n【新增】特别关注学生的主动性。")
        check("保存修改后的主提示词", ok, msg)
        calls.clear()
        R.generate_report(segs, {"n": 1}, meta, {"config": {}})
        check("→ 改完立即生效（未重启进程、未重新 import）",
              "【新增】特别关注学生的主动性" in calls[-1])

        ok, msg = P.save("eval_txt",
                         "【自定义骨架】{student}｜{course}\n{content_text}\n{knowledge_text}")
        check("保存修改后的文字版骨架", ok, msg)
        check("→ 导出格式立即变化",
              R.write_eval_txt(rep, meta).startswith("【自定义骨架】Lucas Zhang｜IG物理"),
              repr(R.write_eval_txt(rep, meta)[:40]))

        calls.clear()
        MS.generate_minutes(segs, meta, {"config": {}})
        check("会议纪要分支也走可配置提示词",
              "会议秘书" in calls[-1] and "今天讲牛顿第二定律" in calls[-1],
              calls[-1][:60] if calls else "无调用")
    finally:
        if had2:
            shutil.copy2(bak2, src2)
            bak2.unlink()
        elif src2.exists():
            src2.unlink()
        _clean_backups(snap2)
        P.load(force=True)

    print()
    print("=" * 74)
    if BAD:
        print(f"[FAIL] {BAD} 项不符合预期，{OK} 项通过")
        return 1
    print(f"[OK] 全部 {OK} 项通过：迁移逐字一致、编辑宽容、坏词拦得住、改完立即生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
