# 规则库 Schema（P-ENHANCE R03，schema_version=1）

## 存储结构
```
rules/
├── manifest.json              # {schema_version, levels:{builtin:N, learned:N, user:N}}
├── builtin/<category>.json    # 随代码发布的通用规则（只读；R04 起消费）
├── learned/<category>.json    # 自动学习规则（AI 审查/规则挖掘产生，可禁用）
├── user/<category>.json       # 人工规则（用户纠正/手动添加，覆盖优先）
└── schema.md                  # 本文档
```
`<category>` ∈ latex | formula | variable | spacing | metadata | layout。

## 规则字段
| 字段 | 必填 | 说明 |
|------|------|------|
| rule_id | ✓ | `R-YYYYMMDD-NNN` |
| category | ✓ | 六类之一（R04 执行顺序：latex→formula→variable→spacing→metadata→layout） |
| pattern_type | ✓ | regex \| latex_token \| dict \| layout_rule |
| pattern | ✓ | 匹配模式（regex 须可编译） |
| replacement | ✓ | 替换文本 |
| scope | - | global（默认）\| journal:<名> \| paper:<doi> |
| confidence | - | [0,1]，默认 0.7；≥0.9 可 auto_apply |
| auto_apply | - | 是否自动应用（默认 = confidence≥0.9；否则进待确认清单） |
| enabled | - | 默认 true |
| source | - | builtin \| ai_review \| user_correction \| manual \| mining |
| hits | - | 命中次数 |
| evidence | - | [{paper, before, after, review: ai\|user\|manual}] |
| version | - | 默认 1 |
| created / updated | - | ISO 时间 |

## 合并裁决（merge）
同 (category, pattern_type, pattern) 不同 replacement：
1. source 优先级：user > builtin > learned
2. 同级：confidence 高
3. 同级同置信度：version 新
输出合并规则 + 冲突清单；同 pattern 同 replacement → 合并 evidence/hits。

## 导出/导入
- 导出 bundle：`{bundle_version, schema_version, exported_at, meta, rules:[...]}` 单文件。
- 导入：schema_version 必须一致；同 rule_id 已存在 → 不覆盖，返回冲突列表。

## 迁移说明
- R03 已将引擎 KNOWN_FIXES（anomaly_detect.py 硬编码 5 条）迁移为 builtin 规则
  （variable 3 + formula 2）；R04 S6.5 将改为消费本库（替代硬编码）。
