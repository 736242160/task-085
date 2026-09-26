#!/usr/bin/env python3
"""按块栈给代码行做归属，并报告块标记错误。

标记语法（工具自定义的小型可识别语法）：
- 开始标记：{  [  (
- 结束标记：}  ]  )
- 行注释：// 到行尾
- 块注释：/* ... */，可跨物理行
- 字符串：'...' 或 "..."，可跨物理行；反斜杠转义下一个字符

归属规则：
- 每行的归属块在该行开始时确定；行内标记先改变栈，但不改变本行归属。
- 开始标记使其之后的物理行进入新块，所以开始标记所在行归属于外层块。
- 结束标记使下一行回到外层块；结束标记所在行开始时仍在内层，所以归内层块。
- 字符串和注释内的标记一律豁免。豁免是“按标记出现位置”生效，不是整行豁免：
  同一行既有字符串/注释内标记、又有外部真实标记时，外部标记仍然生效。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

START_TO_END = {"{": "}", "[": "]", "(": ")"}
END_TO_START = {"}": "{", "]": "[", ")": "("}


@dataclass(frozen=True)
class Position:
    line: int
    column: int

    def __str__(self) -> str:
        return f"{self.line}:{self.column}"


@dataclass
class OwnedLine:
    number: int
    text: str
    block_id: str


@dataclass
class Block:
    id: str
    kind: str
    start: Optional[Position]
    end: Optional[Position]
    parent_id: Optional[str]
    lines: list[OwnedLine] = field(default_factory=list)


@dataclass
class BlockError:
    code: str
    message: str
    position: Position
    actual: Optional[str] = None
    expected: Optional[str] = None
    block_id: Optional[str] = None
    block_start: Optional[Position] = None


@dataclass
class PartitionResult:
    root: Block
    blocks: list[Block]
    errors: list[BlockError]

    def all_blocks(self) -> list[Block]:
        return [self.root, *self.blocks]


def parse_blocks(text: str) -> PartitionResult:
    """解析文本，返回根块/显式块、每行归属和错误清单。"""
    lines = text.splitlines()

    root = Block(
        id="root",
        kind="ROOT",
        start=Position(1, 1),
        end=None,
        parent_id=None,
    )
    blocks_by_id = {"root": root}
    explicit_blocks: list[Block] = []
    stack: list[Block] = []
    errors: list[BlockError] = []

    lex_state = "normal"
    escape_next_line = False
    next_block_number = 1

    for line_number, line in enumerate(lines, start=1):
        owner_id = stack[-1].id if stack else "root"
        index = 0

        while index < len(line):
            char = line[index]
            next_char = line[index + 1] if index + 1 < len(line) else ""

            if lex_state in {"string_single", "string_double"} and escape_next_line:
                escape_next_line = False
                index += 1
                continue

            if lex_state == "normal":
                if char == "/" and next_char == "/":
                    lex_state = "line_comment"
                    index += 2
                    continue
                if char == "/" and next_char == "*":
                    lex_state = "block_comment"
                    index += 2
                    continue
                if char in {"'", '"'}:
                    lex_state = "string_double" if char == '"' else "string_single"
                    index += 1
                    continue
                if char in START_TO_END:
                    start = Position(line_number, index + 1)
                    block = Block(
                        id=f"block_{next_block_number}",
                        kind=char,
                        start=start,
                        end=None,
                        parent_id=stack[-1].id if stack else "root",
                    )
                    next_block_number += 1
                    stack.append(block)
                    explicit_blocks.append(block)
                    blocks_by_id[block.id] = block
                elif char in END_TO_START:
                    end = Position(line_number, index + 1)
                    if not stack:
                        errors.append(
                            BlockError(
                                code="unmatched_end",
                                message=(
                                    f"结束标记 {char!r} 出现时块栈为空，"
                                    "没有可关闭的块。"
                                ),
                                position=end,
                                actual=char,
                            )
                        )
                    else:
                        top = stack[-1]
                        expected = START_TO_END[top.kind]
                        if char != expected:
                            errors.append(
                                BlockError(
                                    code="mismatched_end",
                                    message=(
                                        f"结束标记 {char!r} 与块 {top.id} 的开始标记 "
                                        f"{top.kind!r} 类型不符，应为 {expected!r}；"
                                        "本次不弹出栈顶。"
                                    ),
                                    position=end,
                                    actual=char,
                                    expected=expected,
                                    block_id=top.id,
                                    block_start=top.start,
                                )
                            )
                        else:
                            top.end = end
                            stack.pop()
                index += 1

            elif lex_state == "block_comment":
                if char == "*" and next_char == "/":
                    lex_state = "normal"
                    index += 2
                else:
                    index += 1

            elif lex_state == "line_comment":
                break

            elif lex_state in {"string_single", "string_double"}:
                if char == "\\":
                    if index + 1 >= len(line):
                        escape_next_line = True
                    index += 2
                    continue
                quote = "'" if lex_state == "string_single" else '"'
                if char == quote:
                    lex_state = "normal"
                index += 1

        if lex_state == "line_comment":
            lex_state = "normal"

        blocks_by_id[owner_id].lines.append(
            OwnedLine(number=line_number, text=line, block_id=owner_id)
        )

    for block in stack:
        errors.append(
            BlockError(
                code="unclosed_block",
                message=(
                    f"文件结束时块 {block.id} 仍未闭合；其开始标记 "
                    f"{block.kind!r} 位于 {block.start}，应为 {START_TO_END[block.kind]!r}。"
                ),
                position=block.start,
                expected=START_TO_END[block.kind],
                block_id=block.id,
                block_start=block.start,
            )
        )

    return PartitionResult(root=root, blocks=explicit_blocks, errors=errors)


def result_to_dict(result: PartitionResult) -> dict:
    return {
        "blocks": [asdict(block) for block in result.all_blocks()],
        "errors": [asdict(error) for error in result.errors],
    }


def format_report(result: PartitionResult) -> str:
    output: list[str] = []

    for block in result.all_blocks():
        if block.kind == "ROOT":
            output.append(f"块 {block.id}（隐式最外层）")
        else:
            end_text = str(block.end) if block.end is not None else "未闭合"
            output.append(
                f"块 {block.id}：开始 {block.kind} @ {block.start}，"
                f"结束 @ {end_text}，父块={block.parent_id}"
            )

        if block.lines:
            for owned_line in block.lines:
                output.append(f"  L{owned_line.number:<3} | {owned_line.text}")
        else:
            output.append("  （无归属行）")
        output.append("")

    if not result.errors:
        output.append("错误：无")
    else:
        output.append("错误：")
        for number, error in enumerate(result.errors, start=1):
            output.append(
                f"  {number}. [{error.code}] {error.position} {error.message}"
            )

    return "\n".join(output)


SAMPLE = """root before
/*
  { these markers are inside a block comment
  }
*/
{
  child one
  // { ignored line comment
  text = "skip }]"
  inline { } // [ ignored
  [
    child two
    "also skip }"
  }
  still child two because the mismatched closer did not pop
  ]
}
)
{
  nobody closes me
"""


def run_demo() -> PartitionResult:
    result = parse_blocks(SAMPLE)
    blocks = {block.id: block for block in result.all_blocks()}

    def line_numbers(block_id: str) -> list[int]:
        return [line.number for line in blocks[block_id].lines]

    assert line_numbers("root") == [1, 2, 3, 4, 5, 6, 18, 19]
    assert line_numbers("block_1") == [7, 8, 9, 10, 11, 17]
    assert line_numbers("block_2") == []
    assert line_numbers("block_3") == [12, 13, 14, 15, 16]
    assert line_numbers("block_4") == [20]

    assert blocks["block_1"].start == Position(6, 1)
    assert blocks["block_1"].end == Position(17, 1)
    assert blocks["block_2"].start == Position(10, 10)
    assert blocks["block_2"].end == Position(10, 12)
    assert blocks["block_3"].start == Position(11, 3)
    assert blocks["block_3"].end == Position(16, 3)
    assert blocks["block_4"].end is None

    assert [error.code for error in result.errors] == [
        "mismatched_end",
        "unmatched_end",
        "unclosed_block",
    ]
    assert result.errors[0].position == Position(14, 3)
    assert result.errors[0].actual == "}"
    assert result.errors[0].expected == "]"
    assert result.errors[1].position == Position(18, 1)
    assert result.errors[1].actual == ")"
    assert result.errors[2].position == Position(19, 1)
    assert result.errors[2].block_id == "block_4"

    print("=== 输入 ===")
    print(SAMPLE)
    print("=== 归属与错误报告 ===")
    print(format_report(result))
    print("演示断言全部通过。")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按块栈给代码行归属，并报告不匹配、多余结束和未闭合块。"
    )
    parser.add_argument("path", nargs="?", help="要解析的代码文件；省略时配合 --demo 使用")
    parser.add_argument("--demo", action="store_true", help="运行内置示例和断言")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.demo:
        run_demo()
        return 0

    if not args.path:
        parser.error("请提供文件路径，或使用 --demo 运行内置示例。")

    text = Path(args.path).read_text(encoding="utf-8")
    result = parse_blocks(text)

    if args.json:
        print(json.dumps(result_to_dict(result), ensure_ascii=False, indent=2))
    else:
        print(format_report(result))

    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
