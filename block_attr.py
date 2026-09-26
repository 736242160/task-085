#!/usr/bin/env python3
"""block_attr.py — 按块划分代码并归属每一行，输出块行清单与错误报告。

规则说明
========
1. 归属规则（每行归哪个块）
   - 维护一个"当前块栈"。扫描某一行时，先归属、后处理标记：
     该行归属于"处理该行第一个有效标记之前"的栈顶块；栈空则归全局(root)。
   - 推论：含开始标记的行归外层块（它是新块的声明头，后续行才入新块）；
     含结束标记的行归被关闭的内层块（它是该块的最后一行）。
   - 一行多个标记按从左到右顺序依次处理（归属只在行首判定一次）。

2. 豁免规则（字符串/注释里的标记不算）
   - 支持行注释(//)、块注释(/* ... */，可跨行)、字符串/字符字面量("..."、'...'，
     含 \\ 转义，不跨行；行尾未闭合的字符串视为延伸到行尾）。

3. 歧义优先级（同一行同时命中归属与豁免规则）
   - 按行内从左到右的词法扫描顺序决定：先进入的字符串/注释状态优先，
     其后的标记被豁免；正常状态下先出现的标记先生效。
   - 理由：与真实编译器分词器的单趟扫描语义一致，确定性强、无需回溯，
     不会出现"同一字符既是标记又是豁免内容"的二义。

4. 错误报告（均带行列位置）
   - E_MISMATCH: 结束标记类型与栈顶块期望的类型不符（报错后弹出栈顶块以重新同步）。
   - E_STRAY   : 结束标记出现时栈为空（忽略该标记）。
   - E_UNCLOSED: 文件结束时仍有未闭合块（报告其起始位置）。

退出码：0=无错误，1=有结构错误，2=用法错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field


# ---------------------------------------------------------------- 配置

@dataclass
class LexerConfig:
    pairs: dict = field(default_factory=lambda: {"{": "}", "(": ")", "[": "]"})
    line_comment: str = "//"
    block_comment_open: str = "/*"
    block_comment_close: str = "*/"
    string_delims: tuple = ('"', "'")


# ---------------------------------------------------------------- 数据结构

@dataclass
class Marker:
    kind: str  # 'open' | 'close'
    char: str
    line: int
    col: int


@dataclass
class Block:
    id: int
    open_char: str
    start_line: int
    start_col: int
    lines: list = field(default_factory=list)
    closed: bool = False


@dataclass
class Error:
    code: str
    line: int
    col: int
    message: str


# ---------------------------------------------------------------- 词法扫描

def scan_line(text, line_no, in_block_comment, cfg):
    """单趟扫描一行，返回 (有效标记列表, 行尾是否仍在块注释中)。

    字符串/注释内的块标记被豁免；行内从左到右，先进入的豁免状态优先。
    """
    markers = []
    closing = set(cfg.pairs.values())
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_block_comment:
            j = text.find(cfg.block_comment_close, i)
            if j == -1:
                return markers, True
            i = j + len(cfg.block_comment_close)
            in_block_comment = False
            continue
        if cfg.line_comment and text.startswith(cfg.line_comment, i):
            break  # 行注释：本行剩余内容全部豁免
        if cfg.block_comment_open and text.startswith(cfg.block_comment_open, i):
            in_block_comment = True
            i += len(cfg.block_comment_open)
            continue
        if ch in cfg.string_delims:
            delim = ch
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == delim:
                    i += 1
                    break
                i += 1
            continue  # 行尾未闭合的字符串：剩余部分视为字符串内容（豁免）
        if ch in cfg.pairs:
            markers.append(Marker("open", ch, line_no, i + 1))
        elif ch in closing:
            markers.append(Marker("close", ch, line_no, i + 1))
        i += 1
    return markers, in_block_comment


# ---------------------------------------------------------------- 归属与校验

def parse(text, cfg):
    """返回 (blocks, root_lines, errors)。"""
    blocks, errors, stack, root_lines = [], [], [], []
    in_block_comment = False
    next_id = 1

    for line_no, raw in enumerate(text.splitlines(), 1):
        markers, in_block_comment = scan_line(raw, line_no, in_block_comment, cfg)

        # 归属：处理本行标记之前的栈顶块（规则 1）
        if stack:
            stack[-1].lines.append(line_no)
        else:
            root_lines.append(line_no)

        for m in markers:
            if m.kind == "open":
                blocks.append(Block(next_id, m.char, m.line, m.col))
                next_id += 1
                stack.append(blocks[-1])
            else:
                if not stack:
                    errors.append(Error(
                        "E_STRAY", m.line, m.col,
                        f"多余的结束标记 {m.char!r}：当前没有打开的块"))
                    continue
                top = stack[-1]
                expected = cfg.pairs[top.open_char]
                if m.char != expected:
                    errors.append(Error(
                        "E_MISMATCH", m.line, m.col,
                        f"结束标记 {m.char!r} 与块 #{top.id} "
                        f"({top.open_char!r} 开于 {top.start_line}:{top.start_col}) "
                        f"期望的 {expected!r} 类型不符"))
                stack.pop()  # 恢复策略：仍弹出栈顶块以重新同步
                top.closed = True

    for b in stack:  # 栈中剩余 = 未闭合
        errors.append(Error(
            "E_UNCLOSED", b.start_line, b.start_col,
            f"块 #{b.id} ({b.open_char!r} 开于 {b.start_line}:{b.start_col}) "
            f"到文件结束仍未闭合，期望 {cfg.pairs[b.open_char]!r}"))
    return blocks, root_lines, errors


# ---------------------------------------------------------------- 输出

def format_text(blocks, root_lines, errors, sources=None):
    out = ["== 块归属结果 =="]
    for b in blocks:
        state = "已闭合" if b.closed else "未闭合"
        out.append(f"块 #{b.id}  {b.open_char} 开于 {b.start_line}:{b.start_col}  [{state}]")
        out.append(f"  行: {', '.join(map(str, b.lines)) or '(无)'}")
        if sources:
            for ln in b.lines:
                out.append(f"    {ln:>4} | {sources[ln - 1]}")
    out.append(f"全局(不属于任何块)的行: {', '.join(map(str, root_lines)) or '(无)'}")
    out.append("")
    out.append("== 错误清单 ==")
    if errors:
        for e in errors:
            out.append(f"[{e.code}] {e.line}:{e.col}  {e.message}")
    else:
        out.append("(无错误)")
    return "\n".join(out)


def to_json_obj(blocks, root_lines, errors):
    return {
        "blocks": [
            {"id": b.id, "open": b.open_char, "start": [b.start_line, b.start_col],
             "closed": b.closed, "lines": b.lines}
            for b in blocks
        ],
        "root_lines": root_lines,
        "errors": [
            {"code": e.code, "line": e.line, "col": e.col, "message": e.message}
            for e in errors
        ],
    }


# ---------------------------------------------------------------- 内置示例

DEMO_SOURCE = r"""// 头注释：这里的 { 和 } 都被豁免
#include <stdio.h>
int main() {
    printf("} 在字符串里，豁免\n");
    if (x) {
        x = 1;   // 行内注释 } 豁免
    }
    /* 块注释开始 { 豁免
       注释第二行 } 仍豁免 */
    y = 2;
}
}                      // 多余的结束标记
void f() {
    int a[3);
// 文件结束，f 的 { 块未闭合
"""


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="按块归属代码行并报告结构错误（纯标准库）。")
    ap.add_argument("input", nargs="?", help="输入文件；省略则从 stdin 读取")
    ap.add_argument("--pairs", default="{}()[]",
                    help="块标记对，默认 {}()[]")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    ap.add_argument("--show-lines", action="store_true", help="同时显示每行内容")
    ap.add_argument("--demo", action="store_true", help="运行内置示例")
    args = ap.parse_args(argv)

    pairs_arg = args.pairs.strip()
    if len(pairs_arg) % 2 != 0:
        ap.error("--pairs 必须是偶数个字符，如 {}()[]")
    cfg = LexerConfig(pairs={pairs_arg[i]: pairs_arg[i + 1]
                             for i in range(0, len(pairs_arg), 2)})

    if args.demo:
        text = DEMO_SOURCE
    elif args.input:
        with open(args.input, "r", encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()

    blocks, root_lines, errors = parse(text, cfg)

    if args.json:
        print(json.dumps(to_json_obj(blocks, root_lines, errors),
                         ensure_ascii=False, indent=2))
    else:
        sources = text.splitlines() if args.show_lines else None
        print(format_text(blocks, root_lines, errors, sources))

    if args.demo:
        return 0
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
