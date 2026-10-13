# -*- coding: utf-8 -*-
"""控制台离屏冒烟测试：不弹窗，把五个页面的 refresh 全跑一遍。

用途：改 console.py 后先跑这个，确认没有导入错误 / 空值崩溃 / 键名拼错。
用法：python tools/smoke_console.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

import console  # noqa: E402


def main() -> int:
    app = QApplication([])
    win = console.Console()
    print("页签：", list(win.pages))
    for i in range(3):
        win.tick()
    print("tick() 三轮无异常")
    print("  服务健康：", {k: (v["state"], v["detail"]) for k, v in win.health.items()})
    print("  录音状态：", win.rec_status.get("state"),
          "| 采集设备：", (win.rec_status.get("audio") or {}).get("device_name"))
    print("  流水线状态：", "有" if win.pipe_status else "无（未升级/未运行）")
    print("  告警：", win.alerts or "无")
    print("  教师候选：", win.teacher_choices())
    print("  选定授课教师：", win.selected_teacher or "（未选）")
    rp = win.pages["录课"]
    print("  录课页教师下拉：", [rp.cmb_teacher.itemText(i)
                                for i in range(rp.cmb_teacher.count())],
          "| 当前：", rp.cmb_teacher.currentText())
    fp = win.pages["飞书"]
    print("  飞书页状态：", fp.sts.text()[:80])
    print("  本地分段（未核验 COS）：", {t: len(v) for t, v in win.local_groups().items()} or "无")
    win.uploaded_checked_at = "smoke"
    win.check_uploaded()          # 真实核验一次，验证 COS 通路
    for _ in range(600):          # 跨线程信号要靠事件循环投递，这里手动泵
        app.processEvents()
        if win.uploaded_checked_at != "smoke":
            break
        win.tags_worker.wait(200)
        app.processEvents()
    print("  核验完成时刻：", win.uploaded_checked_at)
    s = win.local_summary()
    print("  核验结果：known=", s["known"], "| error=", (s["error"] or "无")[:80])
    print("     COS 上已有 meta 的 tag 数：", len(win.uploaded_tags),
          list(win.uploaded_tags)[:4])
    print("     已上传·保留中:", s["retained_n"], "段 /", round(s["retained_bytes"]/1048576), "MB")
    print("     未上传·真残留:", s["pending_n"], "段 /", round(s["pending_bytes"]/1048576), "MB")

    ap = win.pages["麦克风"]
    print("  发现音频设备：", [d["name"] for d in ap.devices])
    print("  下拉框当前项：", ap.combo.currentText(), "| 阈值输入框：", ap.spin.value())

    pp = win.pages["提示词"]
    print("  提示词模板：", [(i["key"], "已改" if i["modified"] else "默认")
                             for i in pp._status["items"]])
    print("  当前编辑对象：", pp.key, "| 载入字数：", len(pp.edit.toPlainText()))
    ok, why = console.prompts_mod.validate(pp.key, pp.edit.toPlainText())
    print("  出厂默认词校验：", "通过" if ok else "失败", ("| " + why) if why else "")
    print("  预览前 50 字：",
          console.prompts_mod.preview(pp.key, pp.edit.toPlainText())[:50].replace("\n", "⏎"))
    # 切到「文字版报告 · 骨架」验证多模板切换
    keys = [i["key"] for i in pp._status["items"]]
    pp.list.setCurrentRow(keys.index("eval_txt"))
    print("  切到 eval_txt：", pp.key, "| 变量按钮：",
          [pp.var_row.itemAt(i).widget().text() for i in range(pp.var_row.count())])
    # 关键回归：编辑器里打字后跑 tick，内容不能被周期刷新冲掉
    pp.edit.insertPlainText("  #未保存草稿")
    before = pp.edit.toPlainText()
    win.tick()
    print("  tick 后编辑内容保留：", "是" if pp.edit.toPlainText() == before else "否 ❌")
    pp.edit.setPlainText(pp._loaded)          # 还原，别留下草稿

    svc = console.ServiceManager()
    for k in console.SERVICES:
        h = svc.health(k)
        print(f"  {h['title']}：state={h['state']} pid={h['pid']} running={h['running']}")

    win.close()
    print("\n✅ 冒烟测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
