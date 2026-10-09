# -*- coding: utf-8 -*-
"""成本对比报告口径修订（2026-09-14）。

背景：原报告把「声纹 1:N」估算为「约 15 次/课 ≈ 0.06 元/录音小时」，
但代码实际按「逐轮发言时间轴」逐条调用声纹接口 —— 一节 2 小时课实测
触发 1438 次比对，单课声纹费 6.04 元（占语音账单 79%）。已修复为按音色
去重（每课 2 次）。本脚本把报告中的相关数字改为修正后口径。

用法：python revise_cost_report.py
"""
import sys
from pathlib import Path

P = Path(r"C:\recorder\reports\课堂录制系统_成本优化对比报告_20260909_落地确认.html")

REPS = [
    # ---- 1. 分项表：声纹行 ----
    (
        '<td data-page-node-id="NZhjTyqeoSJ1BGCgzKpwzO">0.0042 元/次 × 约 15 次/课</td>'
        '<td class="num" data-page-node-id="8lreDq11JxZOY6YIRr0Sqj">≈ 0.06 元</td>'
        '<td class="num" data-page-node-id="c0FLsY2ayPpXks3WGbz48P">≈ 0.06 元</td>',
        '<td data-page-node-id="NZhjTyqeoSJ1BGCgzKpwzO">4.2 元/千次 × 每课 2 次（教师 + 学生各 1 次，按音色计）</td>'
        '<td class="num" data-page-node-id="8lreDq11JxZOY6YIRr0Sqj">≈ 0.004 元</td>'
        '<td class="num" data-page-node-id="c0FLsY2ayPpXks3WGbz48P">≈ 0.004 元</td>',
    ),
    # ---- 2. 分项表：大模型行（Hy3 → DeepSeek-V4.1-Flash）----
    (
        '<td data-page-node-id="nLaE37UPXWwotgHFZm1yTi">大模型生成报告/纪要（Hy3）</td>'
        '<td data-page-node-id="nP5S6qOSEwn080Trk8eCp9">输入 1 元 / 输出 4 元每百万 token；约 1.5 万入 + 2 千出</td>',
        '<td data-page-node-id="nLaE37UPXWwotgHFZm1yTi">大模型生成报告/纪要（DeepSeek-V4.1-Flash）</td>'
        '<td data-page-node-id="nP5S6qOSEwn080Trk8eCp9">输入 1 元/百万 token 起（空闲档）；约 1.5 万入 + 2 千出</td>',
    ),
    # ---- 3. 分项表：合计行 ----
    (
        '≈ <b data-page-node-id="BSLz61CtkCrXsCb5xJRrwt">1.61 元</b></td>'
        '<td class="num" style="color:#0F6E56" data-page-node-id="LWtr7jqrgRxbdjuclIjHs4">'
        '≈ <b data-page-node-id="zsgISuUeb8FyIgfSC8axCM">0.78 元</b> ↓52%',
        '≈ <b data-page-node-id="BSLz61CtkCrXsCb5xJRrwt">1.55 元</b></td>'
        '<td class="num" style="color:#0F6E56" data-page-node-id="LWtr7jqrgRxbdjuclIjHs4">'
        '≈ <b data-page-node-id="zsgISuUeb8FyIgfSC8axCM">0.72 元</b> ↓53%',
    ),
    # ---- 4. 月度测算 20 小时 ----
    (
        '≈ 14 元</td><td class="num" data-page-node-id="CqkW6LLANA0dsJ2FXZZ1Us">≈ 8 元</td>'
        '<td class="num down" data-page-node-id="onrLFnpned1XC7wLoWFt5A">↓ 约 6 元',
        '≈ 15 元</td><td class="num" data-page-node-id="CqkW6LLANA0dsJ2FXZZ1Us">≈ 7 元</td>'
        '<td class="num down" data-page-node-id="onrLFnpned1XC7wLoWFt5A">↓ 约 8 元',
    ),
    # ---- 5. 月度测算 40 小时 ----
    (
        '≈ 46 元</td><td class="num" data-page-node-id="DlM4JDhuCvQRxR6wewJo07">≈ 23 元</td>'
        '<td class="num down" data-page-node-id="7ZdrxXCah2qS5opViDouuH">↓ 约 23 元',
        '≈ 47 元</td><td class="num" data-page-node-id="DlM4JDhuCvQRxR6wewJo07">≈ 22 元</td>'
        '<td class="num down" data-page-node-id="7ZdrxXCah2qS5opViDouuH">↓ 约 25 元',
    ),
    # ---- 6. 月度测算 60 小时 ----
    (
        '≈ 78 元</td><td class="num" data-page-node-id="EHNqOdxTRpzJNL3FUWn5H0">≈ 39 元</td>'
        '<td class="num down" data-page-node-id="sXZXgDl5cy6GOGLICdRW0Q">↓ 约 39 元',
        '≈ 78 元</td><td class="num" data-page-node-id="EHNqOdxTRpzJNL3FUWn5H0">≈ 36 元</td>'
        '<td class="num down" data-page-node-id="sXZXgDl5cy6GOGLICdRW0Q">↓ 约 42 元',
    ),
    # ---- 7. 附录：声纹计费口径 ----
    (
        '<td data-page-node-id="tTWBtp0UCJZASpAG3dcFPv">4.2 元/千次</td>'
        '<td data-page-node-id="LjzRMV7ticuvGHJPjRJUgn">声纹注册免费；按次计费</td>',
        '<td data-page-node-id="tTWBtp0UCJZASpAG3dcFPv">4.2 元/千次</td>'
        '<td data-page-node-id="LjzRMV7ticuvGHJPjRJUgn">声纹注册免费；按「音色」计费（每课 2 次），不按发言轮次计费</td>',
    ),
    # ---- 8. 附录：大模型口径 ----
    (
        '<td data-page-node-id="PDoIzez7Y6evx2HAHDvJcB">混元 Hy3（TokenHub）</td>'
        '<td data-page-node-id="qTu7UjYlfahfG7RyzgCKwP">输入 1 元 / 输出 4 元（每百万 token）</td>'
        '<td data-page-node-id="NhvpWLCmT1yFFTuxHSgu6T">2026 正式版定价</td>',
        '<td data-page-node-id="PDoIzez7Y6evx2HAHDvJcB">DeepSeek-V4.1-Flash（TokenHub 原厂直供）</td>'
        '<td data-page-node-id="qTu7UjYlfahfG7RyzgCKwP">输入 1 元 / 输出 4 元（每百万 token，空闲档；高峰翻倍）</td>'
        '<td data-page-node-id="NhvpWLCmT1yFFTuxHSgu6T">2026 正式版定价；混元 Hy3 保留为 fallback</td>',
    ),
    # ---- 9. 附录末尾：新增修订记录 ----
    (
        '<p class="note" data-page-node-id="ax8W6mbZmqDwsBh9IOEyJO">以上为估算，实际账单以腾讯云费用中心为准。'
        '专注力 HTML/xlsx、教评 PDF、会议纪要 HTML 均为本地渲染，零云端成本。</p>',
        '<p class="note" data-page-node-id="ax8W6mbZmqDwsBh9IOEyJO">以上为估算，实际账单以腾讯云费用中心为准。'
        '专注力 HTML/xlsx、教评 PDF、会议纪要 HTML 均为本地渲染，零云端成本。</p>\n'
        '    <p class="note" style="margin-top:10px; border-left:3px solid #D97706; padding-left:10px" '
        'data-page-node-id="REV20260914">'
        '<b>【2026-09-14 口径修订】</b>原「声纹 1:N ≈ 15 次/课」是估算假设，与代码实际行为不符 —— '
        '流水线当时按「逐轮发言时间轴」逐条调用声纹接口，一节 2 小时课实测触发 <b>1438 次</b>比对，'
        '单课声纹费 6.04 元（约占语音账单 79%），这正是「模型占比很小、账单占比过半」的原因。'
        '已修复为按音色去重（每课 2 次），单课降至 0.008 元。本表数字已按修正后口径更新；'
        '同时主模型由混元 Hy3 切换为 DeepSeek-V4.1-Flash（Hy3 保留为回退）。</p>',
    ),
    # ---- 10. 页脚 ----
    (
        '课堂录制系统 · 成本优化对比报告（落地确认版）　|　生成于 2026-09-09　|　含真实 ASR A/B 实测数据',
        '课堂录制系统 · 成本优化对比报告（落地确认版）　|　生成于 2026-09-09　|　声纹口径修订于 2026-09-14　|　含真实 ASR A/B 实测数据',
    ),
]


def main() -> int:
    html = P.read_text(encoding="utf-8")
    failed = []
    for i, (old, new) in enumerate(REPS, 1):
        n = html.count(old)
        if n != 1:
            failed.append((i, n, old[:70]))
            continue
        html = html.replace(old, new)
        print(f"  [{i:2d}] OK")
    if failed:
        print("\n以下替换失败（匹配数≠1）：")
        for i, n, s in failed:
            print(f"  [{i}] 匹配 {n} 次: {s}")
        return 1
    P.write_text(html, encoding="utf-8")
    print(f"\n已写回：{P}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
