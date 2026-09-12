# -*- coding: utf-8 -*-
"""审计 frontend/app.js 写操作入口的防连点覆盖（只读，不改任何文件）。

对每个 `method: 'POST'|'PUT'|'DELETE'` 调用点，取最内层词法作用域，判定：
  A guardBtn-in-scope : 该作用域体内出现 guardBtn
  B guardBtn-at-reg   : 有 guardBtn 调用引用了该作用域的函数名（注册处包了 guardBtn）
  C manual-busy       : 该作用域体内已有 disabled = true / dataset.busy 忙态（旧逻辑，同样防连点）
  E busy-at-caller    : 该写点所在函数被调用（如 kbOpAction 返回 thunk），调用方作用域内有 A/C 忙态
  X non-click(事件)   : 触发者是 change/input/keydown/submit 监听器（非按钮点击，本任务不改）
  N no-caller         : 该函数无任何调用点（死代码，非活入口）
  D UNCOVERED         : 活入口且无任何忙态 → 连点会发两次写请求
运行：python tools/dbg_guard_audit.py   （期望末行「未覆盖(活入口)：0 处」）
"""
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'frontend' / 'app.js'
WRITE = re.compile(r"method:\s*['\"](?:POST|PUT|DELETE)['\"]")
PAT_FN = re.compile(r'^(?:async\s+)?function\s+(\w+)')
PAT_ARROW = re.compile(r'^\s*(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[\w$]+)\s*=>')
PAT_EV = re.compile(r"addEventListener\(\s*'(click|change|input|keydown|submit)'")


def span(lines, start):
    """start 行（0基）作用域结束行；单行注册（无 '{'）即本行。"""
    if '{' not in lines[start]:
        return start
    depth, seen = 0, False
    for i in range(start, len(lines)):
        s = re.sub(r"'[^']*'|\"[^\"]*\"|//.*$", '', lines[i])
        depth += s.count('{') - s.count('}')
        seen = seen or '{' in s
        if seen and depth <= 0:
            return i
    return len(lines) - 1


def main():
    lines = SRC.read_text(encoding='utf-8').splitlines()
    scopes = []                                        # (start, end, name, kind)
    for i, s in enumerate(lines):
        m = PAT_EV.search(s)
        if m:
            scopes.append((i, span(lines, i), '', 'event:' + m.group(1)))
            continue
        m = PAT_FN.match(s) or PAT_ARROW.match(s)
        if m:
            scopes.append((i, span(lines, i), m.group(1), 'fn'))
    guards = [i for i, s in enumerate(lines) if 'guardBtn' in s and 'function guardBtn' not in s]
    busy = re.compile(r'disabled\s*=\s*true|dataset\.busy')

    def evidence(st, en, nm):
        body = '\n'.join(lines[st:en + 1])
        if 'guardBtn' in body:
            return 'A guardBtn-in-scope'
        if nm and any(re.search(r'\b' + re.escape(nm) + r'\b', lines[g]) for g in guards):
            return 'B guardBtn-at-reg'
        if busy.search(body):
            return 'C manual-busy'
        return None

    tags, uncovered, exempt = [], [], []
    for i, s in enumerate(lines):
        if not WRITE.search(s):
            continue
        chain = sorted([sc for sc in scopes if sc[0] <= i <= sc[1]], key=lambda x: -x[0])
        inner = chain[0] if chain else None
        tag = name = None
        if inner and inner[3].startswith('event:') and inner[3] != 'event:click':
            tag, name = 'X non-click(%s)' % inner[3].split(':')[1], 'line%d' % (inner[0] + 1)
        else:
            for st, en, nm, _ in chain:
                name = nm or ('click@%d' % (st + 1))
                tag = evidence(st, en, nm)
                if tag:
                    break
        if not tag:                                     # E/N：写点在 thunk 工厂或死函数里
            fn = next((c for c in chain if c[3] == 'fn' and c[2]), None)
            if fn:
                callers = [j for j, t in enumerate(lines)
                           if j != fn[0] and re.search(r'\b' + re.escape(fn[2]) + r'\b', t)
                           and re.search(re.escape(fn[2]) + r'\s*\(', t)]
                name = fn[2]
                reg = [j for j, t in enumerate(lines)
                       if re.search(r"addEventListener\(\s*'(\w+)',\s*" + re.escape(fn[2]) + r'\b', t)]
                if reg and not callers:
                    ev = re.search(r"addEventListener\(\s*'(\w+)'", lines[reg[0]]).group(1)
                    tag = ('X non-click(%s)' % ev) if ev != 'click' else 'B guardBtn-at-reg'
                elif not callers:
                    tag = 'N no-caller(非活入口)'
                elif all(any(evidence(*c[:3]) for c in scopes if c[0] <= j <= c[1]) for j in callers):
                    tag = 'E busy-at-caller'
        if not tag:
            tag, _ = 'D UNCOVERED', uncovered.append(i + 1)
        elif tag.startswith(('X ', 'N ')):
            exempt.append('%d(%s)' % (i + 1, tag[0]))
        tags.append('  L%-5d %-22s %-24s %s' % (i + 1, tag, name, s.strip()[:54]))
    print('%s  写操作调用点 %d 处\n%s' % (SRC, len(tags), '\n'.join(tags)))
    print('\n未覆盖(活入口)：%d 处%s' % (len(uncovered), ('  → ' + ','.join(map(str, uncovered))) if uncovered else ''))
    print('豁免(非点击入口/死代码)：%d 处  %s' % (len(exempt), ' '.join(exempt)))
    return 1 if uncovered else 0


if __name__ == '__main__':
    sys.exit(main())
